/*
 * agentic-maps navigation mode — "drive a route", Google-style.
 *
 * Mounted by index.html via window.agenticMountNavigation(deps); everything
 * DOM lives there (ids nav-*), everything behaviour lives here:
 *
 *   · Simulation engine: the marker travels along route.geometry at each
 *     step's REAL speed (distance_m / duration_s), rAF-driven with
 *     wall-clock delta × speed factor. States: idle → running ⇄ paused
 *     → arrived → idle.
 *   · Follow camera: heading-up chase cam, 3D (pitch 60) or 2D (top-down),
 *     with speed/distance-driven auto-zoom (hysteresis bands, no ping-pong).
 *   · HUD: large instruction card (glyph, live countdown, approach bar,
 *     "Danach" preview), bottom bar (ETA clock, remaining, speed, mute,
 *     2D/3D, exit), dev sim card (pause, factor, fast-forward, jump).
 *   · Spoken announcements via the browser's own speechSynthesis (German),
 *     distance-based thresholds, speed-scaled; feature-detected so a
 *     voiceless headless browser never breaks.
 *   · Traveled path: the driven slice of the route greys out behind the
 *     marker (own line layer, repainted on a modest interval, not per frame).
 *
 * Gesture policy: peek + re-center. A user drag/wheel/rotate suspends the
 * follow camera (the simulation keeps driving) and shows a "Zentrieren"
 * chip; clicking it snaps the chase cam back. Zooming out into the globe
 * handover is fenced off with a temporary minZoom — nav happens at street
 * zooms, the globe keeps planet scale.
 */
(function () {
  'use strict';

  // -- tuning ---------------------------------------------------------
  const PITCH_3D = 60;             // chase-cam tilt
  const CAM_PAD_TOP = 0.38;        // marker low in the stage, road ahead above
  const ZOOM_NEAR = 17.8;          // approaching a turn — right on the junction
  const ZOOM_MID = 17;             // close chase, Google's street-level reading
  const ZOOM_FAR_2D = 15.2;        // fast straight, top-down
  const ZOOM_FAR_3D = 15.8;        // fast straight, tilted (pitch shows more anyway)
  const NEAR_ENTER_M = 300;        // hysteresis: closer than this => near band
  const NEAR_LEAVE_M = 550;        //   … and only further than this leaves it
  const FAR_ENTER_M = 1400;        // further than this AND fast => far band
  const FAR_LEAVE_M = 800;
  const FAR_SPEED_MS = 18;         // ~65 km/h — "fast straight" gate
  // Straight-road fast-forward: an EXTRA time compression applied only while
  // the next maneuver is far away, eased back to 1× on approach. The far/near
  // easing distances are internal on purpose; the MULTIPLIER is the second
  // slider in the sim card (×1 = aus). Announcements and countdowns are
  // distance-based, so they stay exact — only the boring kilometres between
  // the turns get shorter.
  const FF_FAR_M = 800;            // full extra compression beyond this
  const FF_NEAR_M = 400;           // plain factor again below this
  // The two sim tempo controls, both log-stepped sliders (index = slider pos).
  const FACTOR_STEPS = [1, 2, 5, 10, 25, 50, 100, 250, 500];
  const FF_STEPS = [1, 2, 4, 8, 16, 32];    // ×1 = fast-forward off
  const SIM_COLLAPSE_KEY = 'am-maps-navsim-collapsed';
  const SIM_AUTO_COLLAPSE_W = 760; // no stored preference + narrow => collapsed
  // Nav route emphasis: in nav mode the active route reads like Google's
  // navigation weight — a much heavier line in a casing, growing toward the
  // ground. The traveled-gray layer uses the same width so it swallows the
  // colour exactly. map.js draws routes at 5px in an 8px casing (see
  // _drawRoute); exit restores those numbers.
  const NAV_ROUTE_WIDTH = ['interpolate', ['linear'], ['zoom'], 15, 8, 17, 10, 19, 14];
  const NAV_CASING_WIDTH = ['interpolate', ['linear'], ['zoom'], 15, 13, 17, 15, 19, 20];
  const BASE_ROUTE_WIDTH = 5, BASE_CASING_WIDTH = 8;
  // Lane guidance only when it MATTERS (owner: "if i have still 72 km on
  // this road why does it show the lanes?"): the row appears inside a
  // distance window scaled like the announcement thresholds — motorway
  // speeds see it earlier — and only when the lane data actually
  // discriminates (some lanes valid, some not; when every lane works there
  // is no instruction to give).
  const LANE_WINDOW_M = 800;       // ordinary roads
  const LANE_WINDOW_FAST_M = 2000; // segment speed > 22 m/s (~80 km/h)
  const ARRIVE_M = 12;             // within this of the track end = arrived
  const ARRIVAL_EXIT_MS = 3200;    // arrival card lingers, then back to normal UI
  const TRAVELED_MS = 700;         // traveled-path repaint cadence
  const NAV_MIN_ZOOM = 6;          // fence: globe handover sits at z4.3

  const toRad = Math.PI / 180;
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

  /** Ground metres between two [lon,lat] — equirectangular, plenty at leg scale. */
  function metres(a, b) {
    const mean = (a[1] + b[1]) / 2 * toRad;
    const dx = (b[0] - a[0]) * Math.cos(mean);
    const dy = b[1] - a[1];
    return Math.hypot(dx, dy) * 111320;
  }

  /** Travel bearing a→b in degrees, 0 = north, clockwise. */
  function bearingDeg(a, b) {
    const dx = (b[0] - a[0]) * Math.cos((a[1] + b[1]) / 2 * toRad);
    const dy = b[1] - a[1];
    return Math.atan2(dx, dy) / toRad;
  }

  /** Shortest-path angular approach (camera heading smoothing). */
  function approachAngle(from, to, k) {
    const diff = ((to - from + 540) % 360) - 180;
    return from + diff * clamp(k, 0, 1);
  }

  /** Precompute everything the sim needs from one route.
   *
   *  marks[k] pins step k's maneuver onto the geometry: nearest vertex,
   *  scanned forward from the previous cut (the same trick map.js uses for
   *  leg spans, so a route that doubles back keeps its maneuvers ordered).
   *  Between marks[k] and marks[k+1] the marker drives at step k's own
   *  speed — distance_m/duration_s — because that is the road it is on;
   *  degenerate steps fall back to the route average.
   */
  function buildTrack(route) {
    const pts = route.geometry.map((p) => [p.lon, p.lat]);
    const cum = [0];
    for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + metres(pts[i - 1], pts[i]));
    const total = Math.max(cum[cum.length - 1], 1);
    const avgSpeed = clamp(total / Math.max((route.duration_min || 1) * 60, 1), 1.5, 60);
    const marks = [];
    let from = 0;
    for (const step of route.steps || []) {
      let best = from;
      if (step.location) {
        const cos = Math.cos(step.location.lat * toRad);
        let bestD = Infinity;
        for (let i = from; i < pts.length; i++) {
          const dx = (pts[i][0] - step.location.lon) * cos;
          const dy = pts[i][1] - step.location.lat;
          const d = dx * dx + dy * dy;
          if (d < bestD) { bestD = d; best = i; }
        }
      }
      from = best;
      const usable = (step.distance_m || 0) > 1 && (step.duration_s || 0) > 0.5;
      marks.push({
        step, idx: best, at: cum[best],
        speed: usable ? clamp(step.distance_m / step.duration_s, 1.5, 60) : avgSpeed,
      });
    }
    // Remaining seconds AFTER each mark's own segment — the ETA is real
    // driving time (per-step durations), never scaled by the sim factor.
    let rest = 0;
    for (let k = marks.length - 1; k >= 0; k--) {
      marks[k].timeAfter = rest;
      rest += marks[k].step.duration_s || 0;
    }
    return { pts, cum, total, marks, avgSpeed, totalTime: rest };
  }

  window.agenticMountNavigation = function (deps) {
    const map = deps.map;
    const $ = (id) => document.getElementById(id);

    // -- state ----------------------------------------------------------
    let active = false;
    let simState = 'idle';           // idle | running | paused | arrived
    let route = null, track = null;
    let s = 0;                       // metres along the track
    let segIdx = 0;                  // vertex cursor: cum[segIdx] <= s
    let markIdx = 0;                 // current step (largest k with at <= s)
    let factor = 1;                  // sim speed factor (tempo slider)
    let ffMax = 1;                   // straight-section fast-forward multiplier (×1 = aus)
    let camMode = '3d';              // '3d' | '2d'
    let heading = 0, zoomNow = ZOOM_MID, zoomBand = 'mid';
    let peeking = false;
    let muted = false;
    let rafId = null, lastTs = null;
    let traveledTimer = null, arrivalTimer = null;
    let marker = null;
    let prevMinZoom = null;
    let announced = {};              // upcomingIdx -> Set(threshold ids)
    let hudMarkKey = -2;             // last rendered upcoming index
    let hudLanes = null;             // discriminating lanes of the upcoming maneuver
    // North-up vs heading-up follow (the round compass button). Session
    // state on purpose: NOT reset in start(), so the choice survives
    // exit/re-enter within one page visit.
    let northUp = false;

    // -- voice ------------------------------------------------------------
    /** German utterance via the browser's native TTS. Feature-detected:
     *  a headless browser without voices simply stays silent — no external
     *  service, no error.
     *
     *  Queue discipline: `meta` is {key: maneuver index, d: threshold m}.
     *  A new announcement for a NEWER maneuver or a NEARER threshold cancels
     *  whatever still plays or waits — a stale "in 300 Metern" must never
     *  sound after the turn is already behind us. And the queue never holds
     *  more than the utterance being spoken: pending backlog is cancelled
     *  before every speak. */
    let lastUtterance = null;      // meta of the most recent speak
    function speak(text, meta) {
      if (muted || !text) return;
      try {
        if (!('speechSynthesis' in window) || typeof SpeechSynthesisUtterance === 'undefined') return;
        const synth = window.speechSynthesis;
        const supersedes = meta && lastUtterance
          && (meta.key > lastUtterance.key
              || (meta.key === lastUtterance.key && meta.d < lastUtterance.d));
        if ((supersedes && (synth.speaking || synth.pending)) || synth.pending) {
          synth.cancel();
        }
        lastUtterance = meta || lastUtterance;
        const u = new SpeechSynthesisUtterance(text);
        u.lang = 'de-DE';
        synth.speak(u);
      } catch (e) { /* no TTS here — the visual countdown carries it */ }
    }

    function cancelSpeech() {
      try { if ('speechSynthesis' in window) window.speechSynthesis.cancel(); }
      catch (e) { /* nothing to cancel */ }
    }

    /** "In 300 Metern rechts abbiegen auf …" — instruction lowered into the
     *  sentence, distances spoken the way a navi says them. */
    function announcementText(distM, step) {
      const instr = deps.instructionText(step);
      const lowered = instr.charAt(0).toLowerCase() + instr.slice(1);
      if (distM <= 60) return 'Jetzt ' + lowered;
      if (distM >= 950) {
        const km = (Math.round(distM / 100) / 10).toLocaleString('de-DE');
        return 'In ' + km + ' Kilometern ' + lowered;
      }
      return 'In ' + (Math.round(distM / 50) * 50) + ' Metern ' + lowered;
    }

    // -- track cursors ----------------------------------------------------
    function findSeg(from) {
      const cum = track.cum;
      let i = clamp(from, 0, cum.length - 2);
      while (i > 0 && cum[i] > s) i--;
      while (i < cum.length - 2 && cum[i + 1] <= s) i++;
      return i;
    }

    function position() {
      const { pts, cum } = track;
      const i = segIdx;
      const span = Math.max(cum[i + 1] - cum[i], 0.01);
      const t = clamp((s - cum[i]) / span, 0, 1);
      return [
        pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
        pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t,
      ];
    }

    /** Travel bearing at the cursor — the next segment long enough to have
     *  one (tile geometry carries near-duplicate vertices). */
    function travelBearing() {
      const { pts } = track;
      for (let i = segIdx; i < pts.length - 1; i++) {
        if (metres(pts[i], pts[i + 1]) > 2) return bearingDeg(pts[i], pts[i + 1]);
      }
      return heading;
    }

    const upcoming = () => track.marks[markIdx + 1] || null;
    const currentSpeed = () =>
      (track.marks[markIdx] ? track.marks[markIdx].speed : track.avgSpeed);
    const distToNext = () => {
      const up = upcoming();
      return up ? Math.max(up.at - s, 0) : Math.max(track.total - s, 0);
    };

    /** Real seconds to the destination (per-step durations, unscaled). */
    function remainingSeconds() {
      const up = upcoming();
      if (!up) return Math.max(track.total - s, 0) / track.avgSpeed;
      const cur = track.marks[markIdx];
      return distToNext() / Math.max(cur.speed, 0.5) + (cur.timeAfter || 0);
    }

    // -- fast-forward -------------------------------------------------------
    /** Extra time compression on long straights, eased out on approach so a
     *  demo dwells on the turns and skips the motorway monotony. Purely a
     *  multiplier on simulated time — every distance threshold (countdown,
     *  bar, announcements) fires exactly where it always would. */
    function ffMultiplier() {
      if (ffMax <= 1) return 1;
      const d = distToNext();
      if (d >= FF_FAR_M) return ffMax;
      if (d <= FF_NEAR_M) return 1;
      return 1 + (ffMax - 1) * (d - FF_NEAR_M) / (FF_FAR_M - FF_NEAR_M);
    }

    // -- announcements ------------------------------------------------------
    /** Distance-based, speed-scaled: a motorway (fast segment) announces
     *  earlier, a city street later. One utterance per crossing — at a high
     *  sim factor a single frame can hop past several thresholds, and only
     *  the most urgent one is spoken. */
    function checkAnnouncements() {
      const up = upcoming();
      if (!up) return;
      const d = distToNext();
      const scale = currentSpeed() > 22 ? 1.6 : 1;   // ~80 km/h+: earlier
      const key = markIdx + 1;
      const thresholds = announceThresholds(key);
      const fired = announced[key] || (announced[key] = new Set());
      let toFire = null;
      for (const t of thresholds) {
        if (!t.ok || d > t.d) continue;
        if (!fired.has(t.id)) toFire = t;   // keep the most urgent passed one
        fired.add(t.id);
      }
      // Fast-forward etiquette: at compressed wall-clock a far announcement
      // that would be chased by the 300 m call under ~2 s later is pure
      // noise — skip it entirely (it is marked fired above either way).
      if (toFire && toFire.id === 'far') {
        const wallSpeed = Math.max(currentSpeed() * factor * ffMultiplier(), 0.1);
        if ((d - 300 * scale) / wallSpeed < 2) toFire = null;
      }
      if (toFire) speak(announcementText(d, up.step), { key, d: toFire.d });
    }

    /** The exact announcement thresholds checkAnnouncements() uses on the
     *  segment before `up` (mark index key-1): speed-scaled, the far call
     *  gated on segment length. One source of truth — the crossing check,
     *  the post-seek settling and the skip-to-announcement button must
     *  never disagree about where an announcement lives. */
    function announceThresholds(key) {
      const up = track.marks[key], prev = track.marks[key - 1];
      if (!up || !prev) return [];
      const segLen = up.at - prev.at;
      const scale = prev.speed > 22 ? 1.6 : 1;   // ~80 km/h+: earlier
      return [
        { id: 'far', d: 1000 * scale, ok: segLen > 1500 },
        { id: 'mid', d: 300 * scale, ok: true },
        { id: 'now', d: 50, ok: true },
      ];
    }

    /** After a seek/jump, thresholds already behind the marker must not
     *  machine-gun — pre-mark everything the new position has passed.
     *  Uses the REAL thresholds (speed-scaled like checkAnnouncements),
     *  so a threshold still ahead of the marker stays armed: the
     *  skip-to-announcement jump relies on it firing naturally. */
    function settleAnnouncements() {
      announced = {};
      const up = upcoming();
      if (!up) return;
      const d = distToNext();
      const fired = (announced[markIdx + 1] = new Set());
      for (const t of announceThresholds(markIdx + 1)) {
        if (t.ok && d <= t.d) fired.add(t.id);
      }
    }

    /** The next voice-announcement point ahead of the marker, scanned along
     *  the track across maneuvers: each upcoming maneuver contributes its
     *  (speed-scaled) 1000/300/50 m points, and the nearest one still ahead
     *  wins. Muted or not — the visual countdown fires there either way. */
    function nextAnnouncePoint(fromS) {
      if (!track) return null;
      for (let key = markIdx + 1; key < track.marks.length; key++) {
        for (const t of announceThresholds(key)) {
          if (!t.ok) continue;
          const at = track.marks[key].at - t.d;
          if (at > fromS + 1) {
            return { at, threshold: t.d, id: t.id,
                     maneuverAt: track.marks[key].at,
                     speed: track.marks[key - 1].speed };
          }
        }
      }
      return null;
    }

    /** The nearest announcement point BEHIND the marker — the backward
     *  mirror of nextAnnouncePoint, from the same announceThresholds()
     *  source of truth. Landing `lead` before it puts the point AHEAD of
     *  the marker again, so it re-fires naturally on resume, and the point
     *  just stepped to is automatically excluded on the next press
     *  (it now lies ahead of `s`) — repeated presses walk backward
     *  announcement by announcement. */
    function prevAnnouncePoint(beforeS) {
      if (!track) return null;
      let best = null;
      for (let key = 1; key <= markIdx + 1 && key < track.marks.length; key++) {
        for (const t of announceThresholds(key)) {
          if (!t.ok) continue;
          const at = track.marks[key].at - t.d;
          if (at > 0 && at < beforeS - 1 && (!best || at > best.at)) {
            best = { at, threshold: t.d, id: t.id,
                     maneuverAt: track.marks[key].at,
                     speed: track.marks[key - 1].speed };
          }
        }
      }
      return best;
    }

    /** What "→ Ansage" jumps to: the next announcement point, ~2 s of
     *  sim-travel ahead of the marker — and when the marker already stands
     *  at that point's doorstep (a repeated press), the one after it, so
     *  pressing the button walks announcement to announcement. */
    function announceSkipTarget() {
      let point = nextAnnouncePoint(s);
      if (!point) return null;
      let lead = 2 * point.speed * factor;   // ~2 s at the base tempo
      if (point.at - s <= lead + 2) {
        const following = nextAnnouncePoint(point.at);
        if (following) { point = following; lead = 2 * following.speed * factor; }
      }
      return { point, lead };
    }

    /** What "« Ansage" jumps to — the backward twin. */
    function announceBackTarget() {
      const point = prevAnnouncePoint(s);
      return point ? { point, lead: 2 * point.speed * factor } : null;
    }

    /** Step maneuver-to-maneuver in either direction. Forward lands 150 m
     *  before the NEXT maneuver (the banner then counts it down); backward
     *  lands 150 m before the maneuver most recently passed, with a
     *  doorstep rule so repeated presses keep walking back. At the route
     *  start a further back-step is a graceful no-op. Works while paused
     *  (seek keeps the sim state). */
    function stepManeuver(direction) {
      if (!active) return;
      if (direction >= 0) {
        let up = upcoming();
        if (!up) { seek(Math.max(track.total - 200, s)); return; }
        // Doorstep rule forward too: already 150 m out means THIS maneuver
        // is being counted down — step to the one after it, so repeated
        // presses chain maneuver to maneuver.
        if (up.at - s <= 160 && track.marks[markIdx + 2]) up = track.marks[markIdx + 2];
        seek(Math.max(up.at - 150, s));
        return;
      }
      let k = markIdx;                       // marks[markIdx].at <= s
      while (k >= 0 && s - (track.marks[k].at - 150) < 30) k--;
      if (k < 0) return;                     // already at the very start
      seek(Math.max(track.marks[k].at - 150, 0));
    }

    // -- camera ---------------------------------------------------------
    function targetZoom() {
      const d = distToNext();
      const spd = currentSpeed();
      if (d < NEAR_ENTER_M) zoomBand = 'near';
      else if (zoomBand === 'near' && d > NEAR_LEAVE_M) zoomBand = 'mid';
      if (zoomBand !== 'near') {
        if (d > FAR_ENTER_M && spd > FAR_SPEED_MS) zoomBand = 'far';
        else if (zoomBand === 'far' && d < FAR_LEAVE_M) zoomBand = 'mid';
      }
      if (zoomBand === 'near') return ZOOM_NEAR;
      if (zoomBand === 'far') return camMode === '2d' ? ZOOM_FAR_2D : ZOOM_FAR_3D;
      return ZOOM_MID;
    }

    function updateCamera(dt, snap) {
      const target = travelBearing();
      if (marker) marker.setRotation(target);
      syncNavNeedle();
      if (peeking) return;                       // user is peeking ahead
      // North-up: the camera stays locked to north and the chevron rotates
      // instead; heading-up is the classic chase behind the arrow.
      heading = northUp ? 0
        : snap ? target : approachAngle(heading, target, dt * 3);
      const tz = targetZoom();
      zoomNow = snap ? tz : zoomNow + (tz - zoomNow) * clamp(dt * 1.4, 0, 1);
      map.map.jumpTo({
        center: position(),
        bearing: heading,
        pitch: camMode === '3d' ? PITCH_3D : 0,
        zoom: zoomNow,
        // Marker in the lower third, road ahead filling the stage.
        padding: { top: Math.round(map.el.clientHeight * CAM_PAD_TOP), bottom: 0, left: 0, right: 0 },
      });
    }

    // -- HUD --------------------------------------------------------------
    function fmtCountdown(d) {
      if (d >= 1000) return deps.fmtDistance(d);
      if (d >= 100) return (Math.round(d / 50) * 50) + ' m';
      return Math.max(Math.round(d / 10) * 10, 0) + ' m';
    }

    function fmtClock(date) {
      return String(date.getHours()).padStart(2, '0') + ':'
        + String(date.getMinutes()).padStart(2, '0');
    }

    /** One arrow per lane, drawn in the maneuver-glyph style. OSRM lists a
     *  lane's turn options in `indications`; the first real one names the
     *  lane's direction (a plain "none" is a lane that just goes on). */
    function laneGlyph(lane) {
      const ind = (lane.indications || []).find((i) => i && i !== 'none') || 'straight';
      const stroke = 'fill="none" stroke="currentColor" stroke-width="2.2" '
        + 'stroke-linecap="round" stroke-linejoin="round"';
      const arrow = (d) => '<svg viewBox="0 0 24 24" ' + stroke + '><path d="' + d + '"/></svg>';
      if (ind.indexOf('uturn') !== -1) return arrow('M8 21V12a4 4 0 018 0v4M16 16l-3-3M16 16l3-3');
      if (ind === 'sharp left') return arrow('M12 21V10H6M6 10l4-4M6 10l4 4');
      if (ind === 'sharp right') return arrow('M12 21V10h6M18 10l-4-4M18 10l-4 4');
      if (ind === 'slight left') return arrow('M12 21v-8l-4-4V5M8 5L5 8M8 5l3 3');
      if (ind === 'slight right') return arrow('M12 21v-8l4-4V5M16 5l3 3M16 5l-3 3');
      if (ind === 'left') return arrow('M12 21v-9H6M6 12l4-4M6 12l4 4');
      if (ind === 'right') return arrow('M12 21v-9h6M18 12l-4-4M18 12l-4 4');
      return arrow('M12 21V6M12 6l-4 4M12 6l4 4');
    }

    function updateHud() {
      const up = upcoming();
      const d = distToNext();
      // Countdown + approach bar every frame (they are the animation) …
      $('nav-distance').textContent = fmtCountdown(d);
      const segLen = up
        ? Math.max(up.at - track.marks[markIdx].at, 1)
        : track.total;
      $('nav-bar-fill').style.width = (clamp(1 - d / segLen, 0, 1) * 100).toFixed(1) + '%';
      // … glyph, wording and lane row only when the maneuver changes.
      const key = up ? markIdx + 1 : -1;
      if (key !== hudMarkKey) {
        hudMarkKey = key;
        if (up) {
          $('nav-glyph').innerHTML = deps.maneuverGlyph(up.step);
          $('nav-text').textContent = deps.instructionText(up.step);
        } else {
          $('nav-glyph').innerHTML = deps.maneuverGlyph({ type: 'arrive', modifier: '' });
          $('nav-text').textContent = 'Ziel voraus';
        }
        // Lane guidance at the step's own maneuver point: usable lanes
        // bright, the rest dimmed. Most steps carry none — and a row where
        // EVERY lane is valid says nothing, so only discriminating data
        // (some valid, some not) is kept; whether the row actually SHOWS is
        // the per-frame distance gate below.
        const lanes = up && up.step.lanes && up.step.lanes.length ? up.step.lanes : null;
        hudLanes = lanes && lanes.some((l) => l.valid) && lanes.some((l) => !l.valid)
          ? lanes : null;
        $('nav-lanes').innerHTML = hudLanes ? hudLanes.map((lane) =>
          '<span class="lane' + (lane.valid ? '' : ' off') + '">'
          + laneGlyph(lane) + '</span>').join('') : '';
        // "Danach": the next real maneuver. OSRM splits a roundabout into
        // 'roundabout' + 'exit roundabout', which word identically in German
        // — previewing the exit right under the entry read as a stutter.
        let after = null;
        for (let k = markIdx + 2; k < track.marks.length; k++) {
          if ((track.marks[k].step.type || '') === 'exit roundabout') continue;
          after = track.marks[k];
          break;
        }
        $('nav-next').style.display = after ? '' : 'none';
        if (after) {
          $('nav-next-glyph').innerHTML = deps.maneuverGlyph(after.step);
          $('nav-next-text').textContent = deps.instructionText(after.step);
        }
      }
      // The lane row opens only inside its distance window (speed-scaled,
      // like the announcements): 72 km before the junction the lanes are
      // noise, 800 m (2 km on a motorway) before it they are the answer.
      const laneWindow = track.marks[markIdx] && track.marks[markIdx].speed > 22
        ? LANE_WINDOW_FAST_M : LANE_WINDOW_M;
      $('nav-lanes').classList.toggle('open', !!hudLanes && d <= laneWindow);
      // Bottom bar: the remaining TIME leads; distance + arrival clock
      // secondary; the simulated speed subordinate.
      const remS = remainingSeconds();
      $('nav-time-left').textContent = deps.fmtDuration(remS / 60);
      $('nav-eta').textContent = fmtClock(new Date(Date.now() + remS * 1000));
      $('nav-remaining').textContent = deps.fmtDistance(Math.max(track.total - s, 0));
      $('nav-speed').textContent = deps.fmtSpeed(currentSpeed());
      // Dev readout.
      $('nav-sim-readout').textContent =
        (100 * s / track.total).toFixed(1) + ' % · Schritt ' + (markIdx + 1) + '/'
        + Math.max(track.marks.length, 1) + ' · ' + deps.fmtSpeed(currentSpeed())
        + (ffMax > 1 && ffMultiplier() > 1.01 ? ' · FF×' + ffMultiplier().toFixed(1) : '');
    }

    // -- traveled path ------------------------------------------------------
    function addTraveledLayer() {
      if (map.map.getSource('nav-traveled')) return;
      map.map.addSource('nav-traveled', {
        type: 'geojson',
        data: { type: 'Feature', geometry: { type: 'LineString', coordinates: [] } },
      });
      const firstSymbol = map.map.getStyle().layers.find((l) => l.type === 'symbol');
      // Colour from the nav token set (road-styles.js NAV → --nav-traveled,
      // a cool blue-gray in the navy/amber family); the layer is re-added on
      // every style restore, so a theme switch mid-nav picks up the right
      // theme's value.
      const traveledColor = (getComputedStyle(document.documentElement)
        .getPropertyValue('--nav-traveled') || '').trim() || '#a5b0c2';
      // Same beforeId as the route lines but added later — draws on top of
      // them, beneath the labels: the grey visibly swallows the colour.
      map.map.addLayer({
        id: 'nav-traveled', type: 'line', source: 'nav-traveled',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        // Same width curve as the emphasized nav route — the gray must
        // swallow the colour exactly, not peek out of it.
        paint: { 'line-color': traveledColor, 'line-width': NAV_ROUTE_WIDTH, 'line-opacity': 0.9 },
      }, firstSymbol ? firstSymbol.id : undefined);
    }

    /** Nav-weight route lines (Google's navigation reading, ~9–10px in a
     *  casing) on entry; the map-mode 5px/8px numbers back on exit. Applies
     *  to the primary line, its per-leg lines and the casing — the leg
     *  colours and the traveled gray all ride the same curve. */
    function setRouteEmphasis(on) {
      for (const r of map.routes) {
        const width = on ? NAV_ROUTE_WIDTH : BASE_ROUTE_WIDTH;
        const casing = on ? NAV_CASING_WIDTH : BASE_CASING_WIDTH;
        const lineIds = ['route-' + r.id, ...(r._legLayerIds || [])];
        try {
          if (map.map.getLayer('route-' + r.id + '-casing')) {
            map.map.setPaintProperty('route-' + r.id + '-casing', 'line-width', casing);
          }
          for (const id of lineIds) {
            if (map.map.getLayer(id)) map.map.setPaintProperty(id, 'line-width', width);
          }
        } catch (e) { /* style mid-swap */ }
      }
    }

    let traveledCount = 0;   // vertices in the grey slice (harness-visible)
    function paintTraveled() {
      const source = map.map.getSource('nav-traveled');
      if (!source || !track) return;
      const coords = track.pts.slice(0, segIdx + 1);
      coords.push(position());
      traveledCount = coords.length;
      source.setData({ type: 'Feature', geometry: { type: 'LineString', coordinates: coords } });
    }

    // A style rebuild (theme restored mid-nav) drops ad-hoc layers; the
    // runtime announces its restore pass — re-add ours right after it.
    map.el.addEventListener('agentic-map:style-restored', () => {
      if (!active) return;
      addTraveledLayer();
      paintTraveled();
      setAlternatesHidden(true);
      setRouteEmphasis(true);   // the rebuild redrew the route at map weight
    });

    // -- alternates -------------------------------------------------------
    /** Nav is committed to the primary route: the dimmed candidate lines and
     *  their light badges leave the stage and come back on exit. */
    function setAlternatesHidden(hidden) {
      for (const r of map.routes) {
        for (const id of r._altLayerIds || []) {
          try {
            if (map.map.getLayer(id)) {
              map.map.setLayoutProperty(id, 'visibility', hidden ? 'none' : 'visible');
            }
          } catch (e) { /* style mid-swap */ }
        }
        for (const badge of r._altBadgeMarkers || []) {
          badge.getElement().style.display = hidden ? 'none' : '';
        }
      }
    }

    // -- movement ---------------------------------------------------------
    function advance(dm) {
      s = clamp(s + dm, 0, track.total);
      segIdx = findSeg(segIdx);
      while (markIdx + 1 < track.marks.length && track.marks[markIdx + 1].at <= s) {
        markIdx++;
      }
      checkAnnouncements();
      if (s >= track.total - ARRIVE_M) arrive();
    }

    function frame(ts) {
      rafId = null;
      if (!active || simState !== 'running') return;
      if (lastTs === null) lastTs = ts;
      const dt = clamp((ts - lastTs) / 1000, 0, 0.25);
      lastTs = ts;
      advance(dt * factor * ffMultiplier() * currentSpeed());
      if (marker) marker.setLngLat(position());
      updateCamera(dt, false);
      updateHud();
      window.__navFrameCount = (window.__navFrameCount || 0) + 1;
      if (simState === 'running') rafId = requestAnimationFrame(frame);
    }

    function run() {
      if (simState === 'running') return;
      simState = 'running';
      lastTs = null;
      $('nav-sim-toggle').textContent = 'Pause';
      if (rafId === null) rafId = requestAnimationFrame(frame);
    }

    function pause() {
      if (simState !== 'running') return;
      simState = 'paused';
      $('nav-sim-toggle').textContent = 'Weiter';
      if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    }

    function seek(toMetres) {
      if (!active) return;
      s = clamp(toMetres, 0, track.total);
      segIdx = findSeg(0);
      markIdx = 0;
      while (markIdx + 1 < track.marks.length && track.marks[markIdx + 1].at <= s) markIdx++;
      settleAnnouncements();
      hudMarkKey = -2;
      if (marker) marker.setLngLat(position());
      updateCamera(0, true);
      updateHud();
      paintTraveled();
      if (s >= track.total - ARRIVE_M) arrive();
    }

    // -- arrival ----------------------------------------------------------
    function arrive() {
      if (simState === 'arrived') return;
      simState = 'arrived';
      if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
      s = track.total;
      segIdx = track.pts.length - 2;
      if (marker) marker.setLngLat(position());
      paintTraveled();
      updateHud();
      $('nav-arrival-sub').textContent = deps.destinationLabel();
      $('nav-arrival').classList.add('open');
      speak('Sie haben Ihr Ziel erreicht', { key: Infinity, d: 0 });
      // Back to the normal UI on its own — the route stays on the map.
      arrivalTimer = setTimeout(exit, ARRIVAL_EXIT_MS);
    }

    // -- peek + re-center ---------------------------------------------------
    // Peek intent is read from the DOM, not from MapLibre's gesture events:
    // the follow camera's per-frame jumpTo() stops the gesture handlers
    // before they ever fire dragstart, so a drag begun during follow would
    // go unnoticed there. A real pointer drag (>6 px while down) or a wheel
    // tick on the map IS the intent, whatever the handlers make of it —
    // and once peeking, the follow camera lets go, so the very same gesture
    // pans/zooms normally.
    function enterPeek() {
      if (!active || peeking) return;
      peeking = true;
      $('nav-recenter').classList.add('open');
    }

    function recenter() {
      peeking = false;
      $('nav-recenter').classList.remove('open');
      updateCamera(0, true);
    }

    let pointerDownAt = null;
    const onPointerDown = (ev) => { pointerDownAt = [ev.clientX, ev.clientY]; };
    const onPointerMove = (ev) => {
      if (!pointerDownAt || peeking) return;
      if (Math.hypot(ev.clientX - pointerDownAt[0],
                     ev.clientY - pointerDownAt[1]) > 6) enterPeek();
    };
    const onPointerEnd = () => { pointerDownAt = null; };
    const onWheel = () => enterPeek();
    const peekBindings = [
      ['pointerdown', onPointerDown], ['pointermove', onPointerMove],
      ['pointerup', onPointerEnd], ['pointercancel', onPointerEnd],
      ['wheel', onWheel],
    ];

    // -- enter / exit -------------------------------------------------------
    function start() {
      if (active) return true;
      route = deps.getRoute();
      if (!route || !route.geometry || route.geometry.length < 2) return false;
      track = buildTrack(route);
      active = true;
      simState = 'idle';
      s = 0; segIdx = 0; markIdx = 0;
      announced = {}; hudMarkKey = -2; hudLanes = null; lastUtterance = null;
      peeking = false;
      zoomBand = 'mid'; zoomNow = ZOOM_MID;
      camMode = '3d';
      document.body.classList.add('nav-mode');
      setAlternatesHidden(true);
      // Fence off the globe handover: nav happens at street zooms.
      prevMinZoom = map.map.getMinZoom();
      map.map.setMinZoom(Math.max(prevMinZoom, NAV_MIN_ZOOM));
      addTraveledLayer();
      setRouteEmphasis(true);
      // Sim card: collapsed state — explicit user preference first, else
      // auto-collapse on narrow viewports so the HUD keeps the stage.
      let storedCollapse = null;
      try { storedCollapse = localStorage.getItem(SIM_COLLAPSE_KEY); } catch (e) { /* no storage */ }
      applySimCollapsed(storedCollapse !== null
        ? storedCollapse === '1' : window.innerWidth <= SIM_AUTO_COLLAPSE_W);
      // Heading-oriented chevron, flat on the map plane; colours come from
      // the nav tokens via CSS (var(--nav-marker*)), size from .am-nav-arrow.
      const el = document.createElement('div');
      el.className = 'am-nav-arrow';
      el.innerHTML = '<svg viewBox="0 0 40 40"><circle cx="20" cy="20" r="17"/>'
        + '<path d="M20 8l9 21-9-5.5L11 29z"/></svg>';
      marker = new maplibregl.Marker({
        element: el, anchor: 'center',
        rotationAlignment: 'map', pitchAlignment: 'map',
      }).setLngLat(position()).addTo(map.map);
      for (const [ev, fn] of peekBindings) map.el.addEventListener(ev, fn);
      $('nav-camera').textContent = '2D';
      $('nav-camera').title = 'Draufsicht (2D)';
      $('nav-recenter').classList.remove('open');
      $('nav-arrival').classList.remove('open');
      syncMuteButton();
      syncCompassButton();               // northUp persists across re-entry
      updateCamera(0, true);
      updateHud();
      paintTraveled();
      traveledTimer = setInterval(paintTraveled, TRAVELED_MS);
      run();
      return true;
    }

    function exit() {
      if (!active) return;
      active = false;
      simState = 'idle';
      if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
      clearInterval(traveledTimer); traveledTimer = null;
      clearTimeout(arrivalTimer); arrivalTimer = null;
      for (const [ev, fn] of peekBindings) map.el.removeEventListener(ev, fn);
      pointerDownAt = null;
      cancelSpeech();
      if (marker) { marker.remove(); marker = null; }
      try {
        if (map.map.getLayer('nav-traveled')) map.map.removeLayer('nav-traveled');
        if (map.map.getSource('nav-traveled')) map.map.removeSource('nav-traveled');
      } catch (e) { /* style mid-swap */ }
      setRouteEmphasis(false);
      setAlternatesHidden(false);
      if (prevMinZoom !== null) map.map.setMinZoom(prevMinZoom);
      prevMinZoom = null;
      map.map.jumpTo({ padding: { top: 0, bottom: 0, left: 0, right: 0 } });
      $('nav-recenter').classList.remove('open');
      $('nav-arrival').classList.remove('open');
      document.body.classList.remove('nav-mode');
      route = null; track = null;
      if (deps.onExit) deps.onExit();
    }

    /** Adopt a changed route MID-DRIVE (the nav route-options panel added a
     *  stop, or an alternate was promoted): the marker re-snaps to the
     *  nearest vertex of the new geometry and every cursor — segments,
     *  maneuvers, announcements, HUD, traveled path — re-derives from that
     *  position via seek(). The freshly drawn line arrives at map weight,
     *  so the nav emphasis and hidden alternates are re-applied here. */
    function updateRoute(newRoute) {
      if (!active || !newRoute || !newRoute.geometry || newRoute.geometry.length < 2) return;
      const pos = position();
      route = newRoute;
      track = buildTrack(newRoute);
      hudLanes = null;
      hudMarkKey = -2;
      setAlternatesHidden(true);
      setRouteEmphasis(true);
      const cosLat = Math.cos(pos[1] * toRad);
      let best = 0, bestD = Infinity;
      for (let i = 0; i < track.pts.length; i++) {
        const dx = (track.pts[i][0] - pos[0]) * cosLat;
        const dy = track.pts[i][1] - pos[1];
        const d = dx * dx + dy * dy;
        if (d < bestD) { bestD = d; best = i; }
      }
      seek(track.cum[best]);
    }

    // -- controls -----------------------------------------------------------
    function toggleCamera() {
      camMode = camMode === '3d' ? '2d' : '3d';
      $('nav-camera').textContent = camMode === '3d' ? '2D' : '3D';
      $('nav-camera').title = camMode === '3d' ? 'Draufsicht (2D)' : 'Schrägansicht (3D)';
      if (active) updateCamera(0, true);
    }

    function syncMuteButton() {
      const btn = $('nav-mute');
      btn.classList.toggle('muted', muted);
      btn.title = muted ? 'Ansagen einschalten' : 'Ansagen stumm schalten';
      btn.querySelector('.nm-on').style.display = muted ? 'none' : '';
      btn.querySelector('.nm-off').style.display = muted ? '' : 'none';
    }

    // -- compass / north-up (the round side button) -----------------------
    let needleEl = null;
    function syncNavNeedle() {
      if (!needleEl) needleEl = document.getElementById('nav-compass-needle');
      if (!needleEl) return;
      // The needle mirrors the LIVE camera bearing (heading-up: it swings
      // with every curve; north-up: it stands still pointing up). Quantized
      // so the per-frame call only touches the DOM when it moved.
      const rounded = Math.round(-map.map.getBearing() * 2) / 2;
      if (needleEl._navRotation === rounded) return;
      needleEl._navRotation = rounded;
      needleEl.style.transform = 'rotate(' + rounded + 'deg)';
    }
    map.map.on('rotate', () => { if (active) syncNavNeedle(); });

    function syncCompassButton() {
      const btn = $('nav-compass');
      btn.classList.toggle('active', northUp);
      btn.title = northUp
        ? 'Norden oben fixiert — tippen für Fahrtrichtung oben'
        : 'Fahrtrichtung oben — tippen fixiert Norden';
    }
    $('nav-compass').addEventListener('click', () => {
      northUp = !northUp;
      syncCompassButton();
      if (active) updateCamera(0, true);
    });

    $('nav-exit').addEventListener('click', exit);
    $('nav-arrival-done').addEventListener('click', exit);
    $('nav-camera').addEventListener('click', toggleCamera);
    $('nav-recenter').addEventListener('click', recenter);
    $('nav-mute').addEventListener('click', () => {
      muted = !muted;
      if (muted) cancelSpeech();
      syncMuteButton();
    });
    $('nav-sim-toggle').addEventListener('click', () => {
      simState === 'running' ? pause() : (simState !== 'arrived' && run());
    });
    $('nav-sim-reset').addEventListener('click', () => {
      if (!active) return;
      if (simState === 'arrived') simState = 'paused';
      seek(0);
      pause();
      $('nav-sim-toggle').textContent = 'Weiter';
    });
    // Paired steppers, both granularities, both directions ("lass uns hier
    // easy vor und zurück steppen"): « walks back, » walks forward.
    $('nav-sim-jump').addEventListener('click', () => stepManeuver(1));
    $('nav-sim-prev').addEventListener('click', () => stepManeuver(-1));
    // Skip to just before the next voice announcement: land ~2 s of
    // sim-travel ahead of the threshold so the countdown/banner (and the
    // utterance, unless muted) fire naturally. seek() keeps the sim state —
    // skipping while paused jumps and stays paused.
    $('nav-sim-announce').addEventListener('click', () => {
      if (!active) return;
      const target = announceSkipTarget();
      if (!target) return;                     // nothing left to announce
      seek(clamp(target.point.at - target.lead, s + 1, track.total));
    });
    // …and its backward twin: land the same ~2 s before the PREVIOUS
    // announcement point, which then re-fires naturally on resume.
    $('nav-sim-announce-prev').addEventListener('click', () => {
      if (!active) return;
      const target = announceBackTarget();
      if (!target) return;                     // nothing behind the marker
      seek(clamp(target.point.at - target.lead, 0, track.total));
    });

    // -- tempo sliders (log steps; the slider position IS the step index) --
    function syncFactorUi() {
      $('nav-sim-factor').value = String(FACTOR_STEPS.indexOf(factor));
      $('nav-sim-factor-val').textContent = factor + '×';
    }
    function syncFfUi() {
      $('nav-sim-ffmult').value = String(FF_STEPS.indexOf(ffMax));
      $('nav-sim-ffmult-val').textContent = ffMax <= 1 ? 'aus' : '×' + ffMax;
    }
    /** Snap an arbitrary requested value onto the nearest log step. */
    function snapStep(steps, value) {
      const wanted = Math.log(Math.max(+value || 1, 0.01));
      let best = steps[0];
      for (const step of steps) {
        if (Math.abs(Math.log(step) - wanted) < Math.abs(Math.log(best) - wanted)) best = step;
      }
      return best;
    }
    $('nav-sim-factor').addEventListener('input', (ev) => {
      const at = clamp(Math.round(+ev.target.value) || 0, 0, FACTOR_STEPS.length - 1);
      factor = FACTOR_STEPS[at];
      syncFactorUi();
    });
    $('nav-sim-ffmult').addEventListener('input', (ev) => {
      const at = clamp(Math.round(+ev.target.value) || 0, 0, FF_STEPS.length - 1);
      ffMax = FF_STEPS[at];
      syncFfUi();
    });
    syncFactorUi();
    syncFfUi();

    // -- sim card collapse (chevron in the title row, persisted) ----------
    function applySimCollapsed(collapsed) {
      $('nav-sim').classList.toggle('collapsed', collapsed);
      $('nav-sim-collapse').setAttribute('aria-expanded', String(!collapsed));
    }
    $('nav-sim-collapse').addEventListener('click', () => {
      const collapsed = !$('nav-sim').classList.contains('collapsed');
      applySimCollapsed(collapsed);
      try { localStorage.setItem(SIM_COLLAPSE_KEY, collapsed ? '1' : '0'); }
      catch (e) { /* no storage */ }
    });

    // -- public surface (app + harness) --------------------------------------
    return {
      start, exit, seek, run, pause, toggleCamera, updateRoute, stepManeuver,
      state: () => simState,
      isActive: () => active,
      // Maneuver positions along the track (metres) — lets the dev tools and
      // the harness aim a seek at "the long straight" deterministically.
      // `lanesValid` alongside the count so callers can tell discriminating
      // lane data (the only kind the HUD shows) from all-valid noise.
      marks: () => (track ? track.marks.map((m) => ({
        at: m.at, speed: m.speed, type: m.step.type || '',
        lanes: (m.step.lanes || []).length,
        lanesValid: (m.step.lanes || []).filter((lane) => lane.valid).length,
      })) : []),
      // Both tempo controls snap onto their log steps — programmatic set
      // and slider agree about the only values that exist.
      setFactor: (n) => {
        factor = snapStep(FACTOR_STEPS, n);
        syncFactorUi();
      },
      setFFMultiplier: (n) => {
        ffMax = snapStep(FF_STEPS, n);
        syncFfUi();
      },
      // Back-compat shim for the old boolean toggle: on = the classic ×8.
      setFastForward: (on) => {
        ffMax = on ? 8 : 1;
        syncFfUi();
      },
      // Where "→ Ansage" would land (the button's own target choice,
      // doorstep rule included) — lets the harness assert the window exactly.
      announceTarget: () => {
        const target = active && track ? announceSkipTarget() : null;
        return target ? Object.assign({ lead: target.lead }, target.point) : null;
      },
      // The backward twin — where "« Ansage" would land.
      announceBackTarget: () => {
        const target = active && track ? announceBackTarget() : null;
        return target ? Object.assign({ lead: target.lead }, target.point) : null;
      },
      debug: () => ({
        active, state: simState, s, total: track ? track.total : 0,
        distToNext: track ? distToNext() : 0,
        markIdx, marks: track ? track.marks.length : 0,
        speed: track ? currentSpeed() : 0,
        factor, ff: ffMax > 1, ffMax, ffMult: track ? ffMultiplier() : 1,
        camMode, zoomBand, peeking, muted, northUp,
        laneRowOpen: $('nav-lanes').classList.contains('open'),
        traveledCount,
        simCollapsed: $('nav-sim').classList.contains('collapsed'),
        rafActive: rafId !== null,
      }),
    };
  };
})();

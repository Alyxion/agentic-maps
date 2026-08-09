/*
 * Country-specific route-number shields, plus the overview road lines.
 *
 * Roads are signed the way each country signs them, which is also what Google
 * Maps mirrors: Germany's Autobahn is a blue shield with white text and its
 * Bundesstraße a yellow one with black text, Switzerland/Austria/Italy sign
 * motorways green, France's autoroutes are blue and its routes nationales
 * red. The stock basemap draws one generic white lozenge for all of them,
 * which is why an A61 looked like a B44 looked like an N7.
 *
 * The `network` tag is NOT ISO-prefixed — that assumption is what silently
 * disabled the old country table. Real values observed in the tiles:
 *
 *   DE  BAB, DE:national, Landesstraßen Niedersachsen
 *   FR  FR:A-road, FR:N-road, FR:41:D-road
 *   CH  CH:motorway, ch:national, CH:cantonal, motorway
 *   AT  AT:A-road, AT:S-road          IT  IT:A-road, IT:SS, IT:national
 *   NL  NL:A, NL:N, NL:S-road         BE  BE:A-road, BE:N-road, BE:R-road
 *   PL  pl:national, pl:regional      CZ  CZ:national, cz:regional
 *   ES  ES:A-road, ES:AP-highway      DK  DK:national
 *   --  e-road (European route, green, applies across all of them)
 *
 * So the country is read from the two-letter prefix where there is one, with
 * the handful of bare names special-cased, and the signed class is read from
 * the network name before falling back to `kind` — Swiss motorways arrive
 * tagged `major_road`, so `kind` alone signs an A2 like a cantonal road.
 */
(function () {
  'use strict';

  // Line colours are deliberately NOT per country: the road rendering ships
  // as it is and reads well. Only the signage differs by country.
  const LINE_COLORS = { motorway: '#8091c8', trunk: '#f6c445' };

  // -- mid/low-zoom cartography palette ---------------------------------
  // ONE palette for the whole small-scale look — the flat map's flavor
  // overrides, the tuned overview roads AND the globe's sphere texture all
  // read from here, so the z4 globe→flat handover stays in one color family.
  //
  // Deliberately NOT Google's hues: sage/eucalyptus landcover instead of
  // mint, a cooler steel-leaning water instead of Google's sky blue, warm
  // amber motorways instead of Google's yellow-on-gray, warm off-white paper.
  // The HIERARCHY (what shows when, what dominates) is Google-informed; the
  // palette is our own.
  const CARTO = {
    light: {
      paper: '#f1efe6',        // land / earth
      water: '#96c3e3',        // ocean, lakes, wide rivers — confident, soft
      river: '#a5cbe6',        // thin rivers: lighter + thinner than lakes
      forest: '#b7cbaa',       // sage woodland masses (landcover z<8)
      grassland: '#d5dcc0',
      farmland: '#ebe6cd',     // subtle warm patches
      urban: '#e2ddd2',        // faint gray-beige town wash
      barren: '#ece4cd',
      scrub: '#c9d5b4',
      motorway: '#e2a44b',     // warm amber ribbons
      motorwayFaint: '#d6b481', // z4–5 hairline web: desaturated, reads as texture
      motorwayCasing: '#fbf7ea',
      trunk: '#eecd8d',
      primary: '#fdfdf6',      // faint white thread from ~z8
      countryBorder: 'rgba(96,89,76,0.85)',
      stateBorder: '#b3ac9d',
      cityInk: '#2c3128',      // population_rank >= 11
      cityMid: '#4a4f44',
      cityFaint: '#61665a',
      labelHalo: 'rgba(249,248,241,0.95)',
      // globe sphere texture (no landcover data there — one land tint of
      // the same family, urban smudges a step darker) + its line geometry
      globeLand: '#d3dcba',
      globeUrban: '#c4c9a8',
      globeBorder: '#60594c',
      globeStateBorder: '#a9a294',
      globeRiver: '#7fb0d6',
    },
    dark: {
      paper: '#23271f',
      water: '#22364a',
      river: '#31485e',
      forest: '#28331f',
      grassland: '#252b1e',
      farmland: '#2a2b20',
      urban: '#31312a',
      barren: '#282619',
      scrub: '#242e1e',
      motorway: '#b98a35',
      motorwayFaint: '#87703f',
      motorwayCasing: '#12130e',
      trunk: '#6f5f30',
      primary: '#454639',
      countryBorder: 'rgba(203,212,203,0.6)',
      stateBorder: '#525c54',
      cityInk: '#eef2e4',
      cityMid: '#c3cabb',
      cityFaint: '#9ba295',
      labelHalo: 'rgba(8,10,7,0.92)',
      globeLand: '#262b1f',
      globeUrban: '#33332b',
      globeBorder: '#b5c0b5',
      globeStateBorder: '#5c675f',
      globeRiver: '#3d5a75',
    },
  };

  // -- navigation HUD palette -------------------------------------------
  // Nav's identity: modern deep indigo-navy + a decisive ORANGE accent —
  // deliberately NOT Google's navigation blue (#1a73e8 family; ours sits
  // ~130+ RGB units away and far darker). The orange sits one step warmer
  // than the cartography's amber motorways, so the HUD numbers read as UI,
  // not as another road. ONE surface system top AND bottom: the
  // banner and every floating card (bottom bar, sim, arrival, re-center)
  // are the same dark-navy material with light ink and the amber carrying
  // the loud numbers (countdown, remaining time). One token set per theme,
  // exported next to CARTO and mirrored into CSS custom properties by
  // index.html (applyNavTheme) — the nav CSS reads var(--nav-*) and never
  // hardcodes these hexes.
  //
  // Contrast (WCAG, re-measured for the deepened navy + orange accent):
  // ink on banner 10.1:1 light / 11.9:1 dark, orange accent on banner
  // 5.2:1 / 6.4:1, orange time on the card 5.8:1 / 6.4:1, muted ink on the
  // card 6.2:1 / 7.0:1 — all clear of AA for normal text. The route line
  // itself is a separate identity: vivid azure #2e6be6 (models/map_route.py
  // default), NOT one of these surface tokens.
  const NAV = {
    light: {
      banner: '#1e3a6e',        // instruction banner — deep, saturated indigo navy
      bannerEdge: '#17305d',    // lane row + "Danach" tab, one step darker
      bannerBar: '#102344',     // approach-bar trough
      accent: '#f0a04a',        // decisive orange: countdown + bar fill
      ink: '#eef3fa',           // cool white text on the banner
      inkSoft: 'rgba(238,243,250,0.78)',
      surface: 'rgba(27,52,95,0.96)',  // bottom bar / sim / arrival cards — same navy material
      surfaceInk: '#eef3fa',
      surfaceMuted: '#a8bbd6',
      surfaceLine: 'rgba(168,187,214,0.28)',  // separators on the surface
      chipBg: 'rgba(238,243,250,0.12)',       // quiet chips (speed readout)
      chipInk: '#d6e1f0',
      time: '#f0a04a',          // the big remaining-time number — orange, like the countdown
      danger: '#b3402f',        // cancel — the destructive action (hover fill)
      dangerInk: '#ffffff',
      exitInk: '#f28a76',       // the round ✕ glyph: soft red, AA on the navy surface
      marker: '#1e3a6e',        // position chevron arrow — banner navy
      markerRing: '#f4f7fc',    // position chevron disc
      start: '#2b4d7d',         // "Start" button in the directions panel
      startHover: '#223a5e',
      panelAccent: '#2b4d7d',   // alt-row durations on the LIGHT panel card
      traveled: '#a5b0c2',      // behind-the-marker route gray (cool blue-gray)
    },
    dark: {
      banner: '#152a4e',
      bannerEdge: '#10223f',
      bannerBar: '#0b192f',
      accent: '#ee9c44',
      ink: '#e4ebf5',
      inkSoft: 'rgba(228,235,245,0.75)',
      surface: 'rgba(16,27,46,0.96)',
      surfaceInk: '#dfe7f2',
      surfaceMuted: '#93a7c4',
      surfaceLine: 'rgba(147,167,196,0.25)',
      chipBg: 'rgba(223,231,242,0.10)',
      chipInk: '#c6d3e6',
      time: '#ee9c44',
      danger: '#b3402f',
      dangerInk: '#ffffff',
      exitInk: '#f28a76',
      marker: '#1e3a6e',
      markerRing: '#f4f7fc',
      start: '#2b4d7d',
      startHover: '#223a5e',
      panelAccent: '#2b4d7d',   // the panel card stays light in both themes
      traveled: '#8290a6',
    },
  };

  /** Flavor-key overrides for the tuned map modes. Merged over the stock
   *  Protomaps flavor and UNDER any canvas/theme override and the payload's
   *  brand_colors (map.js does the merge) — so a branded or drained host page
   *  always wins over this tuning. */
  function cartoFlavor(flavorName) {
    const c = CARTO[flavorName === 'dark' ? 'dark' : 'light'];
    if (flavorName === 'dark') {
      return {
        background: c.water, earth: c.paper, water: c.water,
        park_a: '#273220', park_b: '#2a3623',
        wood_a: '#252f1e', wood_b: c.forest,
        scrub_a: '#232c1e', scrub_b: c.scrub,
        sand: '#2a281f', beach: '#2a281f',
        highway: '#8a6f2f',
        highway_casing_early: '#191a12', highway_casing_late: '#191a12',
        bridges_highway: '#8a6f2f', bridges_highway_casing: '#191a12',
        tunnel_highway: '#4f421f',
        boundaries: c.stateBorder,
        city_label: '#dfe5d5', city_label_halo: '#161813',
        subplace_label: '#7f867a', subplace_label_halo: '#161813',
        state_label: '#4e564c',
        ocean_label: '#7f97b3',
      };
    }
    return {
      background: c.water, earth: c.paper, water: c.water,
      park_a: '#c2d5ae', park_b: '#a8cd9a',
      wood_a: '#bdcfac', wood_b: '#adc49c',
      scrub_a: '#ccd8b6', scrub_b: '#c4d2ae',
      sand: '#eae4d2', beach: '#efe9d2', glacier: '#eef0ee',
      hospital: '#eee3dc', industrial: '#e3e1d6', school: '#ece5d8',
      pedestrian: '#eae7db', aerodrome: '#e0dfd8', zoo: '#dfe4d5',
      military: '#e0ddd0', runway: '#e8e7e0', pier: '#e3e0d5',
      buildings: '#ded9cc', railway: '#b9b3a6',
      other: '#f7f5ee', minor_service: '#f7f5ee', minor_a: '#fdfcf7',
      minor_b: '#ffffff', link: '#ffffff', major: '#ffffff',
      highway: '#f0bd62',
      highway_casing_early: '#dca43c', highway_casing_late: '#dca43c',
      bridges_highway: '#f0bd62', bridges_highway_casing: '#dca43c',
      tunnel_highway: '#ecd9ae',
      minor_casing: '#d9d4c6', minor_service_casing: '#d9d4c6',
      link_casing: '#d9d4c6',
      major_casing_early: '#d9d4c6', major_casing_late: '#d9d4c6',
      boundaries: c.stateBorder,
      city_label: '#3a3f35', city_label_halo: '#f6f5ec',
      subplace_label: '#84887a', subplace_label_halo: '#f6f5ec',
      state_label: '#a9a598',
      ocean_label: '#4a70a8',
      roads_label_major: '#8a8474', roads_label_major_halo: '#f6f5ec',
      roads_label_minor: '#9a947f', roads_label_minor_halo: '#f6f5ec',
    };
  }

  // [background, ink] or [background, ink, shape] of the route-number badge,
  // per national signage, for the three signed classes: motorway (A/Autobahn),
  // trunk (national: B, N, SS) and regional (départementale, kantonal,
  // Landesstraße).
  //
  // Shape defaults to a rounded rectangle, which is what almost every European
  // route marker is — including the green European E-routes. Germany's
  // Autobahn marker (Zeichen 405) is the exception: an elongated hexagon,
  // pointed at both ends. Shape is therefore per country, not per class.
  const BLUE = '#134094', GREEN = '#0a7a4b', EURO = '#00713c';
  const YELLOW = '#f2c200', RED = '#c8402c', WHITE = '#ffffff', INK = '#1a1d21';
  const ROAD_STYLES = {
    EU: { motorway_shield: [EURO, WHITE], trunk_shield: [EURO, WHITE], regional_shield: [EURO, WHITE] },
    DE: { motorway_shield: [BLUE, WHITE, 'hex'], trunk_shield: [YELLOW, INK], regional_shield: [YELLOW, INK] },
    AT: { motorway_shield: [GREEN, WHITE], trunk_shield: [YELLOW, INK], regional_shield: [WHITE, INK] },
    CH: { motorway_shield: [GREEN, WHITE], trunk_shield: [BLUE, WHITE], regional_shield: [WHITE, INK] },
    IT: { motorway_shield: [GREEN, WHITE], trunk_shield: [BLUE, WHITE], regional_shield: [WHITE, INK] },
    FR: { motorway_shield: [BLUE, WHITE], trunk_shield: [RED, WHITE], regional_shield: [YELLOW, INK] },
    ES: { motorway_shield: [BLUE, WHITE], trunk_shield: [RED, WHITE], regional_shield: [WHITE, INK] },
    NL: { motorway_shield: [RED, WHITE], trunk_shield: [YELLOW, INK], regional_shield: [BLUE, WHITE] },
    BE: { motorway_shield: [GREEN, WHITE], trunk_shield: [BLUE, WHITE], regional_shield: [BLUE, WHITE] },
    PL: { motorway_shield: [BLUE, WHITE], trunk_shield: [RED, WHITE], regional_shield: [WHITE, INK] },
    CZ: { motorway_shield: [BLUE, WHITE], trunk_shield: [RED, WHITE], regional_shield: [WHITE, INK] },
    DK: { motorway_shield: [BLUE, WHITE], trunk_shield: [YELLOW, INK], regional_shield: [WHITE, INK] },
    GB: { motorway_shield: [BLUE, WHITE], trunk_shield: [EURO, WHITE], regional_shield: [WHITE, INK] },
    US: { motorway_shield: ['#20305c', WHITE], trunk_shield: [WHITE, INK], regional_shield: [WHITE, INK] },
    default: { motorway_shield: [BLUE, WHITE], trunk_shield: [YELLOW, INK], regional_shield: [WHITE, INK] },
  };

  const COUNTRIES = Object.keys(ROAD_STYLES).filter((key) => key !== 'default');
  const CLASSES = ['motorway', 'trunk', 'regional'];

  /** Country code from the `network` tag, as a MapLibre expression.
   *
   *  `e-road` wins over the national network on purpose: an E-route badge is
   *  green everywhere in Europe, which is exactly how it is signed.
   */
  function countryExpr() {
    return ['let', 'net', ['coalesce', ['get', 'network'], ''],
      ['case',
        ['==', ['var', 'net'], 'e-road'], 'EU',
        ['==', ['var', 'net'], 'BAB'], 'DE',
        // Bare "motorway"/"national" appear on Swiss ways.
        ['in', ['var', 'net'], ['literal', ['motorway', 'national']]], 'CH',
        ['upcase', ['slice', ['var', 'net'], 0, 2]]]];
  }

  /** Signed class of a way.
   *
   *  Read from the network name first and only then from `kind`: Swiss
   *  motorways are tagged `CH:motorway` but come through as `major_road`, so
   *  keying on `kind` alone signed the A2 like a cantonal road. `in` does
   *  substring matching on strings, which is what keeps this to one pass over
   *  the naming conventions rather than a row per network.
   */
  function classExpr() {
    const net = ['coalesce', ['get', 'network'], ''];
    return ['let', 'net', net,
      ['case',
        ['any',
          ['in', 'motorway', ['var', 'net']],     // CH:motorway, ch:motorway, motorway
          ['==', ['var', 'net'], 'BAB'],          // Autobahn
          ['in', 'A-road', ['var', 'net']],       // FR/BE/IT/ES/NL autoroute-class
          ['in', 'AP-highway', ['var', 'net']],   // ES autopista
          ['==', ['var', 'net'], 'AT:S-road'],    // Schnellstraße, motorway-grade
          ['in', 'NL:A', ['var', 'net']]],
        'motorway',
        ['any',
          ['in', 'D-road', ['var', 'net']],       // FR départementale — yellow, not red
          ['in', 'cantonal', ['var', 'net']],
          ['in', 'regional', ['var', 'net']],
          ['in', 'NL:S-road', ['var', 'net']],    // urban route, unlike AT:S-road
          ['in', 'NL:ring', ['var', 'net']],
          ['in', 'Landes', ['var', 'net']],
          ['in', 'B-road', ['var', 'net']],
          ['in', 'R-road', ['var', 'net']]],
        'regional',
        ['==', ['get', 'kind'], 'highway'], 'motorway',
        'trunk']];
  }

  /** `${country}-${class}`, the key both the shield image and its ink use. */
  function shieldKeyExpr() {
    return ['concat', countryExpr(), '-', classExpr()];
  }

  /** Colour expression over the country/class key.
   *
   *  Built as `['match', key, k1, v1, k2, v2, …, fallback]` with the pairs
   *  pushed flat on purpose: flattening an array of [key, value] pairs would
   *  splice each key's own elements into the expression and produce a style
   *  MapLibre rejects outright ("Expected boolean but found string").
   */
  function _match(pick) {
    const expression = ['match', shieldKeyExpr()];
    for (const iso of COUNTRIES) {
      for (const kind of CLASSES) {
        expression.push(iso + '-' + kind, pick(ROAD_STYLES[iso], kind));
      }
    }
    // The fallback still has to vary by class: an unknown country drawing a
    // regional badge gets the default white plate, and white ink on it would
    // be invisible.
    const fallback = ['match', classExpr()];
    for (const kind of CLASSES) fallback.push(kind, pick(ROAD_STYLES.default, kind));
    fallback.push(pick(ROAD_STYLES.default, 'trunk'));
    expression.push(fallback);
    return expression;
  }

  /** Unchanged from what ships: one palette for every country. Kept as a
   *  function so the overview layers read the same as before. */
  function roadColor(kind) {
    return LINE_COLORS[kind];
  }

  const shieldInk = () => _match((style, kind) => style[kind + '_shield'][1]);

  // -- shield images ----------------------------------------------------
  // Drawn as SVG at runtime rather than shipped as a sprite, so the badge is
  // the real sign shape and a new country stays a table row instead of a
  // sprite rebuild.
  //
  // ONE IMAGE PER CHARACTER COUNT, exactly like the basemap's own
  // `generic_shield-1char … -5char`. The previous version registered a single
  // stretchable image and let `icon-text-fit` grow it, which is elegant right
  // up until the 9-patch decides to stretch a region it should not: the
  // hexagon's left and right points were pulled into the middle of the plate,
  // and the result read as a white rounded rectangle with a lozenge inside it.
  // A badge only ever holds two to five characters, so a handful of exact
  // images costs nothing and cannot be distorted.
  //
  // Shape comes from the table above, not from the class: the German Autobahn
  // marker is an elongated hexagon (Zeichen 405), while the E-routes, the
  // Bundesstraße plate (Zeichen 401) and almost every other European marker
  // are rectangles.
  const SHIELD_H = 17, SHIELD_R = 3, BORDER = 1;
  // Zeichen 405's real geometry: FLAT vertical sides, shallow apexes at
  // top-centre and bottom-centre (the first cut of this plate had the hexagon
  // rotated 90° — points on the left/right — which is not the German sign).
  const HEX_APEX = 2.6;       // vertical rise of the top/bottom centre points
  const CHAR_W = 6.1;         // advance of a digit at the 10 px design size
  const DESIGN_TEXT_PX = 10;  // font size the plate geometry is drawn for
  const MAX_CHARS = 6;

  /** Plate width for `chars` characters: the text box plus the room the shape
   *  needs at each end (the points, or the rounded corners). */
  function _shieldWidth(shape, chars) {
    // Flat-sided hexagon needs no extra end room beyond a small margin — the
    // apexes are on the top/bottom edges, not in the text's way.
    const ends = shape === 'hex' ? 2.5 : SHIELD_R + 1;
    return Math.round(chars * CHAR_W + 2 * ends);
  }

  function _shieldPath(shape, w, h, inset) {
    const r = SHIELD_R;
    if (shape !== 'hex') {
      return `M${inset + r},${inset}`
        + `H${w - inset - r}A${r},${r} 0 0 1 ${w - inset},${inset + r}`
        + `V${h - inset - r}A${r},${r} 0 0 1 ${w - inset - r},${h - inset}`
        + `H${inset + r}A${r},${r} 0 0 1 ${inset},${h - inset - r}`
        + `V${inset + r}A${r},${r} 0 0 1 ${inset + r},${inset}Z`;
    }
    // Flat left/right sides; top edge rises from both sides to a shallow
    // centre apex, bottom edge mirrors it — Zeichen 405 as signed (and as
    // Google draws it), not a lozenge.
    const midX = w / 2;
    const apex = Math.max(0, HEX_APEX - inset * 0.5);
    return `M${inset},${inset + apex}`
      + `L${midX},${inset}`
      + `L${w - inset},${inset + apex}`
      + `V${h - inset - apex}`
      + `L${midX},${h - inset}`
      + `L${inset},${h - inset - apex}Z`;
  }

  /** Parse "am-shield-DE-motorway-2char" into pixels of exactly that sign.
   *
   *  Unknown countries fall back to the default styling, so a network we have
   *  never seen still gets a badge rather than a missing-image warning and no
   *  shield at all.
   *
   *  SYNCHRONOUS on purpose, drawn with Canvas2D rather than by decoding an
   *  SVG <img>: MapLibre's `styleimagemissing` contract is that the image must
   *  exist by the time the handler returns — an image added a tick later is
   *  recorded (hasImage() goes true) but the symbols whose placement asked for
   *  it have already been laid out without an icon, and they stay invisible
   *  until some future tile reload. That is not a cosmetic warning: it made
   *  every route-number badge on the first paint disappear.
   */
  function shieldImage(imageId, ratio) {
    const match = /^am-shield-([A-Za-z]*)-(motorway|trunk|regional)-(\d)char$/.exec(imageId);
    if (!match) return null;
    const kind = match[2];
    const chars = Math.max(1, Math.min(MAX_CHARS, +match[3]));
    const style = ROAD_STYLES[match[1]] || ROAD_STYLES.default;
    const [bg, , shape] = style[kind + '_shield'];
    // A light keyline is what real signage has, and it keeps the badge legible
    // where it sits on top of its own road casing.
    const border = bg === WHITE ? INK : WHITE;

    const cssW = _shieldWidth(shape, chars);
    const w = Math.round(cssW * ratio), h = Math.round(SHIELD_H * ratio);
    const canvas = document.createElement('canvas');
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.scale(w / cssW, h / SHIELD_H);
    // Path2D takes the same SVG path data the previous <img> build used, so
    // the plate geometry is byte-for-byte the shape it always was.
    ctx.fillStyle = border;
    ctx.fill(new Path2D(_shieldPath(shape, cssW, SHIELD_H, 0)));
    ctx.fillStyle = bg;
    ctx.fill(new Path2D(_shieldPath(shape, cssW, SHIELD_H, BORDER)));
    return {
      // No stretchX, no content, no icon-text-fit: the image is already the
      // right size, so there is nothing for a 9-patch to get wrong.
      options: { pixelRatio: ratio },
      data: ctx.getImageData(0, 0, w, h),
    };
  }

  /** Answer MapLibre's `styleimagemissing` for our shield ids. Lazy on purpose:
   *  the style is rebuilt on every view switch, and only the badges actually
   *  on screen get drawn. */
  function registerShields(map) {
    map.on('styleimagemissing', (event) => {
      const id = event.id;
      if (!id || id.indexOf('am-shield-') !== 0) return;
      if (map.hasImage(id)) return;
      const built = shieldImage(id, Math.max(1, Math.round(window.devicePixelRatio || 1)));
      if (!built) return;
      // Added before the handler returns — see shieldImage() on why sync.
      map.addImage(id, built.data, built.options);
    });
  }

  /** Replaces the basemap's `roads_shields`, which draws one generic white
   *  lozenge for every network on earth.
   *
   *  `options.minzoom` defaults to the value the hybrid view has always used;
   *  `options.spacing`/`options.maxzoom` let the tuned map modes thin the
   *  plates out and band them by zoom. */
  function shieldLayer(source, font, options) {
    const opts = options || {};
    // Text size drives everything: the plate art is drawn for DESIGN_TEXT_PX,
    // so the icon is scaled by the same ratio and badge and number keep their
    // proportions at every zoom.
    const textSize = ['interpolate', ['linear'], ['zoom'],
      7, 8.5, 12, DESIGN_TEXT_PX, 16, 11];
    const iconSize = ['interpolate', ['linear'], ['zoom'],
      7, 8.5 / DESIGN_TEXT_PX, 12, 1, 16, 11 / DESIGN_TEXT_PX];
    const layer = {
      id: opts.id || 'roads-shields', type: 'symbol', source, 'source-layer': 'roads',
      minzoom: opts.minzoom == null ? 7 : opts.minzoom,
      filter: ['all',
        ['in', ['get', 'kind'], ['literal', ['highway', 'major_road']]],
        ['has', 'shield_text'],
        ['<=', ['length', ['get', 'shield_text']], MAX_CHARS]],
      layout: {
        'icon-image': ['concat', 'am-shield-', shieldKeyExpr(), '-',
          ['to-string', ['min', MAX_CHARS, ['length', ['get', 'shield_text']]]], 'char'],
        'icon-size': iconSize,
        'text-field': ['get', 'shield_text'],
        'text-font': [font || 'Noto Sans Medium'],
        'text-size': textSize,
        'symbol-placement': 'line',
        'symbol-spacing': opts.spacing || 250,
        'symbol-avoid-edges': true,
        // Motorway plates first, national roads second: when two badges
        // compete for the same corridor, the A-number must win.
        'symbol-sort-key': ['match', classExpr(),
          'motorway', 0, 'trunk', 1, 2],
        'icon-rotation-alignment': 'viewport',
        'text-rotation-alignment': 'viewport',
        'text-padding': 3,
      },
      paint: { 'text-color': shieldInk() },
    };
    if (opts.maxzoom != null) layer.maxzoom = opts.maxzoom;
    if (opts.motorwayOnly) {
      // The z6.5–8 band: only the motorway network is signed. The tiles
      // themselves enforce most of this (shield_text only reaches z7 tiles,
      // and there almost exclusively on `kind: highway`), the filter makes
      // it explicit.
      layer.filter = ['all',
        ['==', ['get', 'kind'], 'highway'],
        ['has', 'shield_text'],
        ['<=', ['length', ['get', 'shield_text']], MAX_CHARS]];
    }
    return layer;
  }

  /** The tuned two-band signage for the pure map modes: sparse motorway
   *  plates from z6.5 (the tiles carry shield data from their z7 level — see
   *  shieldLayer), the full per-country signage from z8. */
  function shieldLayers(source, font) {
    return [
      shieldLayer(source, font, {
        id: 'roads-shields-low', minzoom: 6.5, maxzoom: 8,
        motorwayOnly: true, spacing: 460,
      }),
      shieldLayer(source, font, {
        minzoom: 8,
        spacing: ['interpolate', ['linear'], ['zoom'], 8, 400, 11, 320, 14, 250],
      }),
    ];
  }

  /**
   * Motorway/trunk lines for regional zooms, Google-like: the motorway net
   * appears at z6 as a thin coloured thread and thickens with zoom, trunk
   * roads join at z8. Above z12 the basemap's own full street styling takes
   * over, so these fade out to avoid drawing every road twice.
   *
   * Without `flavorName` this returns EXACTLY the layer set that always
   * shipped — the hybrid view and the canvas/theme palettes stay on it. With
   * a flavor it returns the tuned mid-zoom hierarchy: cased amber motorway
   * ribbons dominant from z5, trunks a lighter amber from ~z6.2, primaries a
   * faint thread from ~z8 — no same-weight spaghetti.
   */
  function overviewRoadLayers(source, flavorName) {
    const fade = (full) => ['interpolate', ['linear'], ['zoom'], 11, full, 12.5, 0];
    if (!flavorName) {
      return [
        {
          id: 'roads-overview-trunk', type: 'line', source, 'source-layer': 'roads',
          filter: ['all', ['==', ['get', 'kind'], 'major_road'],
            ['in', ['coalesce', ['get', 'kind_detail'], ''], ['literal', ['trunk', 'primary']]]],
          minzoom: 8, maxzoom: 13,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: {
            'line-color': roadColor('trunk'),
            'line-opacity': fade(0.9),
            'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 8, 0.7, 10, 1.3, 12, 2.4],
          },
        },
        {
          id: 'roads-overview-motorway', type: 'line', source, 'source-layer': 'roads',
          filter: ['==', ['get', 'kind'], 'highway'],
          minzoom: 5, maxzoom: 13,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: {
            'line-color': roadColor('motorway'),
            'line-opacity': fade(1),
            'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 5, 0.6, 8, 1.4, 10, 2.2, 12, 3.4],
          },
        },
      ];
    }
    const c = CARTO[flavorName === 'dark' ? 'dark' : 'light'];
    // Anchors at 6/7/8 on purpose: the z6–8 band is where the old rendering
    // fell apart, so the ribbons must be fully established BY z6, not ramp
    // up somewhere past it.
    //
    // Below that, the continental band (owner: "at least show the main
    // roads here like google"): the tiles carry the merged motorway net
    // from their z3 level (probed — tools/probe_lowzoom_roads.py; nothing
    // below motorway exists there, so a fainter trunk web is moot), and it
    // is drawn from z4 as an uncased hairline in a desaturated amber — a
    // texture that knits the continent, not clutter. The z5/z6/… anchors
    // are exactly the shipped ramp, so the z5 handoff cannot pop.
    const motorwayWidth = ['interpolate', ['exponential', 1.4], ['zoom'],
      4, 0.5, 5, 1.0, 6, 1.9, 7, 2.2, 8, 2.5, 10, 3.1, 12, 4.2];
    // Trunks stay clearly subordinate: later start, thinner, paler — the
    // amber web at z6.5 must be the MOTORWAY net, not every Bundesstraße.
    const trunkWidth = ['interpolate', ['exponential', 1.4], ['zoom'],
      6.8, 0.6, 8, 1.1, 10, 1.7, 12, 2.4];
    // Same anchors as the motorway line, plus the halo.
    const motorwayCasingWidth = (extra) => ['interpolate', ['exponential', 1.4], ['zoom'],
      5, 1.0 + extra, 6, 1.9 + extra, 7, 2.2 + extra, 8, 2.5 + extra,
      10, 3.1 + extra, 12, 4.2 + extra];
    const line = { 'line-cap': 'round', 'line-join': 'round' };
    const motorwayFilter = ['==', ['get', 'kind'], 'highway'];
    const trunkFilter = ['all', ['==', ['get', 'kind'], 'major_road'],
      ['==', ['coalesce', ['get', 'kind_detail'], ''], 'trunk']];
    return [
      {
        // Primaries: a faint thread from ~z8, deliberately near-paper. The
        // stock roads_major layer takes over from z10 in the same hue.
        id: 'roads-overview-primary', type: 'line', source, 'source-layer': 'roads',
        filter: ['all', ['==', ['get', 'kind'], 'major_road'],
          ['==', ['coalesce', ['get', 'kind_detail'], ''], 'primary']],
        minzoom: 7.8, maxzoom: 13,
        layout: line,
        paint: {
          'line-color': c.primary,
          'line-opacity': ['interpolate', ['linear'], ['zoom'],
            7.8, 0, 8.5, 0.85, 11, 0.85, 12.5, 0],
          'line-width': ['interpolate', ['exponential', 1.4], ['zoom'],
            8, 0.8, 10, 1.4, 12, 2.2],
        },
      },
      {
        id: 'roads-overview-trunk-casing', type: 'line', source, 'source-layer': 'roads',
        filter: trunkFilter, minzoom: 7.5, maxzoom: 13,
        layout: line,
        paint: {
          'line-color': c.motorwayCasing,
          'line-opacity': fade(0.75),
          'line-width': ['interpolate', ['exponential', 1.4], ['zoom'],
            7.5, 2.3, 8, 2.7, 10, 3.4, 12, 4.4],
        },
      },
      {
        id: 'roads-overview-trunk', type: 'line', source, 'source-layer': 'roads',
        filter: trunkFilter, minzoom: 6.8, maxzoom: 13,
        layout: line,
        paint: {
          'line-color': c.trunk,
          'line-opacity': fade(0.9),
          'line-width': trunkWidth,
        },
      },
      {
        // The light casing is what turns a hairline into a ribbon: it
        // separates the amber from the landcover exactly the way Google's
        // white halo separates its roads from the green.
        id: 'roads-overview-motorway-casing', type: 'line', source, 'source-layer': 'roads',
        filter: motorwayFilter, minzoom: 5.5, maxzoom: 13,
        layout: line,
        paint: {
          'line-color': c.motorwayCasing,
          'line-opacity': fade(0.9),
          'line-width': motorwayCasingWidth(2.0),
        },
      },
      {
        id: 'roads-overview-motorway', type: 'line', source, 'source-layer': 'roads',
        filter: motorwayFilter, minzoom: 4, maxzoom: 13,
        layout: line,
        paint: {
          // Full amber exactly at z5 where the shipped ramp always started;
          // the z4 hairline runs desaturated so the web stays background.
          'line-color': ['interpolate', ['linear'], ['zoom'],
            4.2, c.motorwayFaint, 5, c.motorway],
          'line-opacity': ['interpolate', ['linear'], ['zoom'],
            4, 0.55, 5, 1, 11, 1, 12.5, 0],
          'line-width': motorwayWidth,
        },
      },
    ];
  }

  window.agenticRoadStyles = {
    ROAD_STYLES, roadColor, overviewRoadLayers,
    shieldLayer, shieldLayers, registerShields, shieldImage, shieldKeyExpr,
    CARTO, NAV, cartoFlavor,
  };
})();

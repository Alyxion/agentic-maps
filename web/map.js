/*
 * agentic-maps slide runtime.
 *
 * Mounts every [data-agentic-map] element from a JSON payload:
 *   { spec: MapSpec, tiles_url_template, tile_min_zoom, tile_max_zoom,
 *     attribution, vector_url_template?, vector_max_zoom?,
 *     glyphs_url_template?, sprites_base_url?, basemap_flavor?,
 *     brand_colors?, default_view?, offline? }
 * from an inline <script type="application/json" data-agentic-inline-spec> block or the
 * URL in data-agentic-spec-url.
 *
 * View modes: 'hybrid' (imagery + labels), 'satellite' (imagery only),
 * 'map-dark' / 'map-light' (official Protomaps cartography, CC0). Corporate
 * theming: payload.brand_colors overrides any flavor color key centrally.
 *
 * Decorations scale geographically: pins/highlights are sized for their
 * location's detail zoom and shrink as the camera zooms out (highlights also
 * fade out entirely far above their location) — no monster bubbles on
 * country-level views.
 *
 * Interaction: presentations are non-interactive unless spec.interactive is
 * true (or the element is data-agentic-standalone, i.e. the authoring demo).
 *
 * Step model: index -1 = overview, 0..n-1 = locations; next()/prev() fly the
 * camera; "agentic-map:location" fires on every change.
 */
(function () {
  'use strict';

  const EMPTY_COLLECTION = { type: 'FeatureCollection', features: [] };

  // Marker registry for the globe. MapLibre keeps its own marker list in a
  // private (minified) field, but the globe must mirror every marker as a
  // billboard on the sphere — search pins, stop pins, pings, geo labels,
  // whoever created them. Wrapping addTo/remove here, before any marker is
  // constructed, is the one place that sees them all.
  const trackedMarkers = new Set();
  if (typeof maplibregl !== 'undefined' && !maplibregl.Marker.prototype._agenticTracked) {
    maplibregl.Marker.prototype._agenticTracked = true;
    const originalAddTo = maplibregl.Marker.prototype.addTo;
    const originalRemove = maplibregl.Marker.prototype.remove;
    maplibregl.Marker.prototype.addTo = function (target) {
      // Register AFTER the original: MapLibre's addTo starts by calling
      // this.remove() to detach from a previous map, and that call runs
      // through the patched remove below — registering first meant every
      // marker was deleted from the registry the instant it was added.
      const result = originalAddTo.call(this, target);
      trackedMarkers.add(this);
      return result;
    };
    maplibregl.Marker.prototype.remove = function () {
      trackedMarkers.delete(this);
      return originalRemove.call(this);
    };
  }
  window.agenticMapMarkers = trackedMarkers;

  /** Largest font size at which a wrapped label stays inside its circle.
   *
   *  Measured DOM widths are useless here — an inline span that wraps reports
   *  its container's width, which is exactly the bug that let "LINDENWEG"
   *  kiss the rim. So this is geometry instead: every word becomes a line,
   *  and each line must fit the circle's CHORD at that line's height, which
   *  near the top/bottom is much narrower than the diameter. 0.74em per
   *  character covers bold Helvetica caps plus the letter-spacing.
   */
  function _pinFontSize(label, diameter) {
    const words = label.split(/\s+/).filter(Boolean);
    const lines = words.length > 1 ? words : [label];
    const radius = diameter / 2 - 4;             // inside the border
    for (let size = 14; size >= 8; size--) {
      const lineHeight = size * 1.2;
      const fits = lines.every((line, index) => {
        const offset = Math.abs((index - (lines.length - 1) / 2) * lineHeight) + size * 0.36;
        if (offset >= radius) return false;
        const chord = 2 * Math.sqrt(radius * radius - offset * offset);
        return line.length * size * 0.74 <= chord * 0.94;
      });
      if (fits) return size;
    }
    return 8;
  }
  // Where street names start. Google names residential streets from ~z14;
  // the stock basemap waited until z15 and left towns unlabelled.
  const STREET_LABEL_MINZOOM = 14;

  const registry = [];
  window.agenticMaps = registry;

  const ROAD_KINDS = ['highway', 'major_road', 'medium_road', 'minor_road'];

  // POI classes that survive the tilted view (see _syncPoiDensity): the
  // landmarks a leaned-back city still needs — transport hubs, culture,
  // civic anchors — while restaurant/shop noise waits for a flat camera.
  const POI_LANDMARK_KINDS = ['station', 'aerodrome', 'ferry_terminal',
    'museum', 'attraction', 'zoo', 'stadium', 'university', 'townhall',
    'theatre', 'peak', 'marina', 'park'];

  // Per-leg palette for multi-stop routes: the route's own colour leads (so a
  // plain A→B route and leg 1 of a longer trip look identical), the rest are
  // hues picked to stay readable over light AND dark cartography and over
  // imagery — the dark casing underneath does the separating from the map.
  // Leads with the vivid brand blue (models/map_route.py default); the old
  // amber lead washed out over yellow-green farmland, and no leg colour may
  // sit near the amber motorways or the HUD's orange accent.
  // Lowercase on purpose: legColor() compares against route.color.
  const LEG_COLORS = ['#2e6be6', '#3fb27f', '#b06ae0', '#e2574c', '#35b8c4'];

  // "Select nothing": comparing against '' would match the handful of Natural
  // Earth features that carry no ISO code (France and Norway among them) and
  // wash a translucent highlight over half of Europe.
  const NO_COUNTRY = ['==', ['get', 'iso'], '\u0000none'];

  // Label ink for the bright cartography flavor. The stock light grey washes
  // out on a projector; this is the near-black a printed map would use.
  const MAP_LABEL_INK = '#26303d';

  // Space around the globe: deep navy fading to a blue-lit atmosphere at
  // the horizon, matching the in-house global-locations viewer.
  const SKY = {
    'sky-color': '#0a1b33',
    'sky-horizon-blend': 0.5,
    'horizon-color': '#3c82c8',
    'horizon-fog-blend': 0.6,
    'fog-color': '#000a1e',
    'fog-ground-blend': 0.1,
    'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 1, 4, 0.6, 6, 0],
  };

  // Zoom at which the street net is drawn over imagery. Motorways appear at
  // regional zoom (that is where "how do I get there" gets answered); shields
  // wait until the road under them is unmistakable.
  const ROADS_OVER_MINZOOM = 7;
  const SHIELD_MINZOOM = 9;

  // Pitch above which the tile pyramid goes variable-zoom (full-res near,
  // coarser far) — the vendored engine's own gate sits at 60°, above the
  // 55–62° the 3D/nav cameras use. See _applyPitchLod for the ground truth.
  const PITCH_LOD_MIN_PITCH = 45;

  // Template URLs keep literal {z}/{x}/{y}/{fontstack}/{range} tokens —
  // new URL() would percent-encode the braces and invalidate the style.
  // Absolute stays absolute — for ANY scheme, not just http/data. A sealed
  // session addresses its tiles through a registered MapLibre protocol
  // (amap://…, see web/sealed-runtime.js); prefixing that with an origin
  // would corrupt it, and inside a srcdoc frame `location.origin` is "null"
  // anyway.
  const abs = (u) => (u.indexOf('://') !== -1 || u.startsWith('data:') ? u : location.origin + u);
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

  function formatDuration(minutes) {
    if (minutes < 60) return Math.round(minutes) + ' min';
    const h = Math.floor(minutes / 60);
    return h + ' h ' + Math.round(minutes - h * 60) + ' min';
  }

  function escapeHtml(text) {
    return String(text).replace(/[<>&"]/g,
      (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
  }

  /** Brief "via" place for one candidate, uniqueness-first (max two names).
   *
   *  `via_places` arrives PRIORITY-ordered from the server. The label
   *  prefers the highest-priority place NOT shared with the other
   *  candidates ("über Pforzheim" vs "über Heilbronn"); when the top place
   *  is shared, the distinguishing one LEADS and the shared landmark
   *  follows ("Pforzheim und Heidelberg"); when nothing distinguishes, the
   *  shared top place stands. Empty when the route carries no via places. */
  function viaPlaceLabel(route, others) {
    const places = route.via_places || [];
    if (!places.length) return '';
    const elsewhere = new Set();
    for (const other of others || []) {
      for (const p of (other.via_places || [])) elsewhere.add(p.name);
    }
    const unique = places.filter((p) => !elsewhere.has(p.name));
    if (!unique.length) return places[0].name;
    if (!elsewhere.has(places[0].name)) return places[0].name;
    return unique[0].name + ' und ' + places[0].name;
  }

  function orthoLayer(adjust, minzoom) {
    // Cross-fade in rather than switch on: the imagery source covers a
    // rectangle, and popping that rectangle over Blue Marble draws a hard
    // bright edge across half a continent. Fading over one zoom level lets
    // Blue Marble carry the wide views and hands over invisibly.
    //
    // `minzoom === null` is the UNIFIED aerial source (one server-side
    // ladder for z0-19, see /aerial): there is no second raster underneath
    // to fade over, so the layer is simply on at full strength — the band
    // handovers happen inside the endpoint, per tile.
    const full = adjust.opacity ?? 1;
    const opacity = minzoom == null ? full
      : ['interpolate', ['linear'], ['zoom'],
         minzoom - 0.6, 0, minzoom + 0.9, full];
    return {
      id: 'ortho', type: 'raster', source: 'ortho',
      paint: {
        'raster-opacity': opacity,
        'raster-saturation': adjust.saturation ?? 0,
        'raster-contrast': adjust.contrast ?? 0,
        'raster-brightness-min': adjust.brightness_min ?? 0,
        'raster-brightness-max': adjust.brightness_max ?? 1,
        'raster-fade-duration': 150,
      },
    };
  }

  function stateBoundaryOverLayer() {
    // German Länder (and equivalents) above the imagery. In hybrid the whole
    // base layer set sits UNDER the orthophotos, so without this copy the
    // state borders are painted over and simply disappear.
    return {
      id: 'boundaries-over', type: 'line', source: 'streets', 'source-layer': 'boundaries',
      filter: ['>', 'kind_detail', 2], minzoom: 4,
      paint: {
        'line-color': 'rgba(255,255,255,0.75)',
        'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.5, 8, 1.0, 12, 1.6],
        'line-dasharray': [4, 2],
      },
    };
  }

  function roadsOverLayer() {
    // Street net above the imagery (hybrid only). Ramps in from z11 so
    // streets stay readable on mid-zoom city views, stronger by z13.
    return {
      id: 'roads-over', type: 'line', source: 'streets', 'source-layer': 'roads',
      filter: ['in', 'kind', ...ROAD_KINDS], minzoom: ROADS_OVER_MINZOOM,
      paint: {
        'line-color': '#ffffff',
        'line-opacity': ['interpolate', ['linear'], ['zoom'],
          7, ['match', ['get', 'kind'], 'highway', 0.55, 0.0],
          10, ['match', ['get', 'kind'], 'highway', 0.55, 'major_road', 0.3, 0.0],
          13, ['match', ['get', 'kind'], 'highway', 0.5, 'major_road', 0.4, 0.25]],
        // The z15 anchors are the OLD curve's own interpolated values (the
        // ≤z15 rendering is untouched); from there the white net widens to
        // match the aerial streets underneath — minors ~2×, mains ~+50% in
        // the z15.5–18 band, in lockstep with the map modes' width tables.
        'line-width': ['interpolate', ['exponential', 1.6], ['zoom'],
          7, ['match', ['get', 'kind'], 'highway', 1.1, 0.5],
          11, ['match', ['get', 'kind'], 'highway', 2, 'major_road', 1.2, 0.6],
          15, ['match', ['get', 'kind'], 'highway', 5.52, 'major_road', 3.59, 2.15],
          17, ['match', ['get', 'kind'], 'highway', 19, 'major_road', 13, 11]],
      },
    };
  }

  class AgenticMap {
    constructor(el, payload) {
      this.el = el;
      this.payload = payload;
      this.spec = payload.spec;
      this.index = -1;
      this.markers = [];
      this._scaled = []; // {inner, anchorZoom, exponent, minScale, maxScale, fadeBelow}
      this.routes = [];
      // The view must be known BEFORE the first style is built, not switched
      // to afterwards. `_buildStyle` only adds the imagery layers for hybrid /
      // satellite, so a map that is constructed hybrid and then told it is a
      // street map has already built those layers and asked for aerial tiles:
      // the viewer sees a blink of orthophoto on a pure cartography slide, and
      // the credit line briefly names imagery rights that are not on screen.
      // Embeds therefore declare it up front, exactly like `data-agentic-initial`.
      this.view = el.dataset.agenticView || payload.default_view || 'hybrid';
      // Labels (cities, streets, countries) all follow one language setting.
      this.lang = payload.lang || 'de';
      this.currentFlavor = null;

      const standalone = el.hasAttribute('data-agentic-standalone');
      // Embeds set data-agentic-initial="lon,lat,zoom" BEFORE mounting: the map is
      // then CONSTRUCTED at its first real view. Without this it would spend
      // its first frames on the payload overview (all of Germany) — visibly
      // wrong for a beat, and every one of those tiles is a wasted request.
      let start = this.spec.overview || this.spec.locations[0].camera;
      const init = (el.dataset.agenticInitial || '').split(',').map(parseFloat);
      if (init.length >= 3 && init.every(isFinite)) {
        start = { center: { lon: init[0], lat: init[1] }, zoom: init[2] };
      }
      this.usePro = typeof basemaps !== 'undefined'
        && payload.vector_url_template && payload.sprites_base_url && payload.glyphs_url_template;

      this.map = new maplibregl.Map({
        container: el,
        style: this._buildStyle(this._flavorFor(this.view)),
        center: [start.center.lon, start.center.lat],
        zoom: start.zoom,
        bearing: start.bearing || 0,
        pitch: start.pitch || 0,
        maxZoom: payload.tile_max_zoom + 0.75,
        // The nav follow-cam re-crosses the same zooms constantly (auto-zoom
        // bands, band handovers of the unified aerial ladder); keep more
        // recently-evicted tiles around than the viewport-derived default so
        // fast-forward driving re-paints from cache instead of re-fetching.
        maxTileCacheZoomLevels: 8,
        attributionControl: false,
        interactive: standalone || !!this.spec.interactive,
      });
      // Pitched-view tile LOD: activated here and re-applied after every
      // style swap (setStyle builds fresh Source objects, and the per-source
      // tile-zoom functions live on them).
      this._applyPitchLod();
      this.map.on('style.load', () => this._applyPitchLod());
      // Record which style is standing, so a later setView() to the SAME view
      // is a true no-op. Without this the first one always rebuilt the style
      // (the key started undefined) — a second full style build, and a second
      // round of tile requests, for no change at all.
      this._styleKey = this._styleKeyFor(this.view);
      this._busy = 0;                     // animations nobody outside can see
      this.map.on('error', (e) => console.error('[am-map] map error:', e && e.error ? e.error : e));
      // Route-number badges are drawn on demand rather than shipped as a
      // sprite; this survives style rebuilds, which drop registered images.
      if (typeof agenticRoadStyles !== 'undefined') agenticRoadStyles.registerShields(this.map);
      // World views must fill the stage — never expose the void beyond the
      // mercator square (Google behaves the same way).
      const fitMinZoom = () => {
        this.map.setMinZoom(Math.max(Math.log2(el.clientHeight / 256), 0));
        // MapLibre sizes its canvas once; without this the map keeps the old
        // width and the rest of the stage stays empty after any resize.
        this.map.resize();
      };
      fitMinZoom();
      window.addEventListener('resize', fitMinZoom);
      // NOTE: no ResizeObserver here. resize() changes the element, which
      // re-triggers the observer — an endless loop that freezes the renderer.
      // MapLibre v5 already watches the container itself.
      this._geo = []; // geojson decorations {sourceId, layers:[..], data} — survive style switches
      // Compact ⓘ badge (Google-style); full text expands on click. Full
      // license list additionally belongs in the published package's credits.
      // The source sits in the tile template (/live/<id>/{z}/{x}/{y}); the
      // attribution query is fed from it.
      const tpl = payload.tiles_url_template || '';
      // Both proxy shapes name their source in the path: the per-band
      // /live/<id>/... and the unified /aerial/<id>/... dispatcher.
      const idOf = (t) => { const m = (t || '').match(/\/(?:live|aerial)\/([^/]+)\//); return m ? m[1] : ''; };
      // Imagery AND world layer: which one applies depends on the zoom — the
      // server decides that, the client asks about both.
      this._orthoSourceId = [idOf(tpl), idOf(payload.world_tiles_url_template)]
        .filter(Boolean).join(',');
      this._apiBase = tpl.split('/live/')[0] || '/api/v1/maps';
      // Not compact: the credit is shown permanently, so there is no need
      // for an expand button — and even less for a button that could cover
      // it (docs/concepts/map-attribution.md).
      this._attribCtl = new maplibregl.AttributionControl({
        compact: false, customAttribution: payload.attribution });
      this.map.addControl(this._attribCtl);
      // MapLibre renders the compact control expanded on first paint; collapse
      // it so the stage shows the ⓘ badge only, as intended.
      ['idle', 'moveend', 'zoomend'].forEach((ev) =>
        this.map.on(ev, () => this._refreshAttribution()));
      const collapse = () => this.el.querySelectorAll('.maplibregl-ctrl-attrib')
        .forEach((node) => node.classList.remove('maplibregl-compact-show'));
      collapse();
      setTimeout(collapse, 0);

      for (const loc of this.spec.locations) this._addDecorations(loc);
      this.map.on('zoom', () => this._rescale());
      this._rescale();
      // Buildings rise only while the camera is pitched; styledata re-syncs
      // after every rebuild (the fresh style bakes opacity 0 back in).
      this.map.on('pitch', () => this._syncBuildings());
      this.map.on('styledata', () => this._syncBuildings());
      // POI density follows the pitch too — but on pitchend, not per frame:
      // a filter swap re-runs symbol placement, which is far too heavy to do
      // continuously during a tilt gesture. styledata re-applies after every
      // style rebuild (guarded: the handler is a no-op when the standing
      // filter already matches, so its own setFilter cannot loop).
      this.map.on('pitchend', () => this._syncPoiDensity());
      this.map.on('styledata', () => this._syncPoiDensity());
      this.map.once('load', () => {
        for (const route of this.spec.routes || []) this.addRoute(route);
        this._applyVisibility();
      });
    }

    /** MapLibre v5's pitched-view LOD, activated — the vendored default
     *  leaves it dormant for every camera this app uses.
     *
     *  Ground truth in the vendored bundle (v5.24.0): per-tile variable zoom
     *  only engages when the mercator covering-tiles provider's
     *  `allowVariableZoom` answers true — `terrain || pitch > clamp(78.5 -
     *  fov/2, 0, 60)`, which with the default fov (~36.87°) is exactly 60°,
     *  ABOVE the 55–62° the 3D/nav views actually use. Measured result: a
     *  pitch-60 z16 Hamburg view covered the stage with 96 uniform z17
     *  aerial tiles — the fine-gridded horizon and the dark out-of-coverage
     *  seam of the owner's screenshots.
     *
     *  Two moves, both bounded and revertible (tools/probe_lod.py):
     *  - The gate: `allowVariableZoom` is patched once, prototype-level, to
     *    also answer true past PITCH_LOD_MIN_PITCH. The stock predicate
     *    runs first, so terrain and wide-fov setups keep their behavior.
     *  - The sources: the unified z0–19 aerial raster gets the engine's own
     *    LOD profile via the public setSourceTileLodParams (the bundle's
     *    default createCalculateTileZoomFunction(9.314, 3)); measured cover
     *    at the reference view is a z15–z18 pyramid instead of z17×96.
     *    EVERY other source is pinned to the uniform center zoom: a coarse
     *    VECTOR tile at a close display zoom draws its simplified far-zoom
     *    road geometry as ruler-straight white hairlines across the view
     *    (the "weird grid" reproduced at 53.52, 10.02, pitch 60) — the
     *    aerial pyramid is the point, the street net must never coarsen.
     *
     *  Sealed/offline payloads keep the per-band ortho+world pair whose
     *  ortho has no coarse rungs (minzoom 13) — no LOD there, and the
     *  pinning still protects the vector net past 60° pitch. Everything is
     *  wrapped in try/catch: if a vendor bump moves these internals the map
     *  degrades to stock covering behavior instead of breaking.
     */
    _applyPitchLod() {
      try {
        const proto = Object.getPrototypeOf(
          this.map.transform.getCoveringTilesDetailsProvider());
        if (!proto.__amStockAllowVariableZoom) {
          proto.__amStockAllowVariableZoom = proto.allowVariableZoom;
          const stock = proto.__amStockAllowVariableZoom;
          proto.allowVariableZoom = function (transform, options) {
            return stock.call(this, transform, options)
              || transform.pitch > PITCH_LOD_MIN_PITCH;
          };
        }
        const unified = !!this.payload.aerial_url_template
          && (this.view === 'hybrid' || this.view === 'satellite');
        const managers = this.map.style.tileManagers || {};
        for (const key of Object.keys(managers)) {
          if (key === 'ortho' && unified) continue;
          const source = managers[key].getSource();
          if (source) source.calculateTileZoom = (requestedZoom) => requestedZoom;
        }
        if (unified && this.map.getSource('ortho')) {
          this.map.setSourceTileLodParams(9.314, 3, 'ortho');
        }
      } catch (e) { /* vendored internals moved — stock covering stands */ }
    }

    // -- style ---------------------------------------------------------

    _flavorFor(view) {
      if (!this.usePro) return null;
      // Aerial views always sit on the bright basemap — dark cartography
      // under bright imagery is unreadable; pure map modes pick their flavor.
      if (view === 'hybrid' || view === 'satellite') return this.payload.hybrid_flavor || 'light';
      return view === 'map-light' ? 'light' : (this.payload.basemap_flavor || 'dark');
    }

    _worldLayer(adjust) {
      // NASA Blue Marble beneath the state orthophotos — the globe where
      // 20 cm imagery ends. Overzooms past its native z8.
      return {
        id: 'world', type: 'raster', source: 'world',
        paint: {
          'raster-saturation': (adjust.saturation ?? 0) * 0.6,
          'raster-contrast': (adjust.contrast ?? 0) * 0.6,
          'raster-fade-duration': 150,
        },
      };
    }

    _buildStyle(flavorName) {
      this.currentFlavor = flavorName;
      const adjust = this.spec.imagery || {};
      // Imagery is DECLARED only where it is drawn. A source with no layer
      // above it costs no tiles, but "costs no tiles" is a claim about
      // MapLibre's internals; leaving it out is a fact about the style. A pure
      // cartography view must not so much as mention an aerial source.
      // Without the basemap library there IS no cartography, so the fallback
      // style has nothing to draw but imagery — it keeps its sources.
      const onImagery = !this.usePro
        || this.view === 'hybrid' || this.view === 'satellite';
      // The live display path serves ONE aerial source for the whole zoom
      // range (server-side band dispatch, /aerial): MapLibre's native
      // pitched-view pyramid LOD then pulls coarser tiles of the SAME
      // source for the far rows — which is exactly what the split
      // ortho+world pair could not do (a source with minzoom 13 has no
      // coarse tiles to give, so the horizon smeared). Sealed/offline
      // payloads carry no aerial template and keep the per-band pair.
      const unified = onImagery && !!this.payload.aerial_url_template;
      const hasWorld = onImagery && !unified && !!this.payload.world_tiles_url_template;
      const sources = {};
      if (unified) {
        sources.ortho = {
          type: 'raster',
          tiles: [abs(this.payload.aerial_url_template)],
          tileSize: 256,
          minzoom: 0,
          maxzoom: this.payload.aerial_max_zoom || this.payload.tile_max_zoom,
        };
      } else if (onImagery) {
        sources.ortho = {
          type: 'raster',
          tiles: [abs(this.payload.tiles_url_template)],
          tileSize: 256,
          minzoom: this.payload.tile_min_zoom,
          maxzoom: this.payload.tile_max_zoom,
        };
      }
      if (hasWorld) {
        sources.world = {
          type: 'raster',
          tiles: [abs(this.payload.world_tiles_url_template)],
          tileSize: 256,
          minzoom: 0,
          maxzoom: this.payload.world_max_zoom || 8,
        };
      }
      if (!this.usePro) {
        const layers = [{ id: 'bg', type: 'background', paint: { 'background-color': '#141b24' } }];
        if (hasWorld) layers.push(this._worldLayer(adjust));
        layers.push(orthoLayer(adjust, unified ? null : this.payload.tile_min_zoom));
        return { version: 8, sources, layers };
      }
      sources.streets = {
        type: 'vector',
        tiles: [abs(this.payload.vector_url_template)],
        minzoom: 0,
        maxzoom: this.payload.vector_max_zoom || 15,
      };
      // Official CC0 cartography + central brand color overrides. The tuned
      // mid-zoom palette (road-styles.js CARTO) slots UNDER the canvas/theme
      // override and the payload's brand colors: a deliberately drained or
      // branded map must never be re-saturated by the cartography pass.
      const flavor = Object.assign(
        {}, basemaps.namedFlavor(flavorName),
        this._cartoTuned() ? agenticRoadStyles.cartoFlavor(flavorName) : {},
        this._flavorOverride || {},
        this.payload.brand_colors || {}
      );
      this._mergedFlavor = flavor;
      const layers = basemaps.layers('streets', flavor, { lang: this.lang });
      const firstSymbol = layers.findIndex((l) => l.type === 'symbol');
      const insertAt = firstSymbol === -1 ? layers.length : firstSymbol;
      // Map modes are cartography, full stop: the imagery layers are not
      // hidden, they are never built. Toggling visibility after a style swap
      // depended on a callback that could be missed, and then "Karte" showed
      // orthophotos.
      if (this.view === 'hybrid' || this.view === 'satellite') {
        const imagery = hasWorld
          ? [this._worldLayer(adjust), orthoLayer(adjust, this.payload.tile_min_zoom),
             stateBoundaryOverLayer(), roadsOverLayer()]
          : [orthoLayer(adjust, unified ? null : this.payload.tile_min_zoom),
             stateBoundaryOverLayer(), roadsOverLayer()];
        layers.splice(insertAt, 0, ...imagery);
      }
      this._addOverviewRoads(layers);
      this._denseStreetLabels(layers);
      this._addCountryLayers(sources, layers, flavorName);
      this._legibleLabels(layers, flavorName);
      // After _legibleLabels on purpose: the label hierarchy the tuning
      // installs (big cities darker AND bigger) must win over the uniform
      // ink _legibleLabels paints.
      this._tuneMidZoom(layers, flavorName);
      this._addBuildingsLayer(layers, flavorName);
      return {
        version: 8,
        glyphs: abs(this.payload.glyphs_url_template),
        sprite: abs(this.payload.sprites_base_url + '/' + flavorName),
        // TODO(globe): projection:{type:'globe'} + a sky/atmosphere spec is the
        // intended world view (global-locations look). The vendored
        // MapLibre build rejected the style with it — needs the vendored
        // version checked before re-enabling.
        sources,
        layers,
      };
    }

    /** The cartography tuning applies exactly where it is honest to: the
     *  pure map modes on the stock flavors. Imagery views keep their layer
     *  set byte-for-byte, and an active canvas/theme override keeps its
     *  drained palette — re-saturating it would defeat its purpose. */
    _cartoTuned() {
      return this.usePro
        && this.view !== 'hybrid' && this.view !== 'satellite'
        && !this._flavorOverride
        && typeof agenticRoadStyles !== 'undefined'
        && !!agenticRoadStyles.cartoFlavor;
    }

    /** Motorway/trunk network at regional zooms, signed the way each country
     *  signs it (see web/road-styles.js). Without it a country view has no road
     *  structure at all — the basemap only starts drawing roads at z12. */
    _addOverviewRoads(layers) {
      if (!this.usePro || typeof agenticRoadStyles === 'undefined') return;
      // On the canvas palette the route-number badges are dropped entirely.
      // They are the most saturated thing on the map, and the canvas exists
      // so that the most saturated thing is the data.
      const canvas = !!(this._flavorOverride && this._flavorOverride.__canvas);
      const tuned = this._cartoTuned();
      const firstSymbol = layers.findIndex((l) => l.type === 'symbol');
      const insertAt = firstSymbol === -1 ? layers.length : firstSymbol;
      const overview = agenticRoadStyles.overviewRoadLayers(
        'streets', tuned ? this.currentFlavor : null);
      if (canvas) {
        // Keep the network's shape — it is the geography — but drain its
        // colour so it reads as structure rather than as a category.
        for (const layer of overview) {
          layer.paint = Object.assign({}, layer.paint, { 'line-color': '#c9c8c5' });
        }
      }
      layers.splice(insertAt, 0, ...overview);

      // The basemap signs every network on earth with the same white lozenge,
      // so an Autobahn, a Bundesstraße and a route nationale were indis-
      // tinguishable. Swap that layer for the country-specific one, in place,
      // so it keeps its position in the draw order.
      const generic = layers.findIndex((l) => l.id === 'roads_shields');
      if (canvas) {
        if (generic !== -1) layers.splice(generic, 1);
        return;
      }
      const font = (layers[generic] && layers[generic].layout['text-font'] || [])[0];
      // Map modes get the two-band signage (sparse motorway plates from
      // z6.5, everything from z8); hybrid keeps the single layer it always
      // had — imagery views are contractually untouched by this pass.
      const shields = tuned && agenticRoadStyles.shieldLayers
        ? agenticRoadStyles.shieldLayers('streets', font)
        : [agenticRoadStyles.shieldLayer('streets', font)];
      if (generic === -1) layers.push(...shields);
      else layers.splice(generic, 1, ...shields);
    }

    /** Name the streets, the way Google does at the same scale.
     *
     *  The stock basemap holds minor-road labels back to z15, so a wide z14
     *  view over a town carried 15 names out of 757 named streets present in
     *  the tiles — a city rendered as unlabelled geometry. Google names
     *  residential streets from about z14.
     *
     *  Density is left to collision detection, ordered by the tiles' own
     *  `min_zoom` hint: a through-street outranks a dead-end lane, so the
     *  names that survive are the ones worth having.
     */
    _denseStreetLabels(layers) {
      const minor = layers.find((l) => l.id === 'roads_labels_minor');
      if (!minor) return;
      minor.minzoom = STREET_LABEL_MINZOOM;
      minor.layout = Object.assign({}, minor.layout, {
        // Protomaps ranks every road with the zoom it becomes worth drawing
        // at, and that ranking is already the sort key — so when labels
        // compete for space the through-street wins and the dead-end lane
        // loses. (It cannot be a filter: MapLibre allows `zoom` only in
        // layout and paint expressions, and using it in a filter is rejected
        // outright, taking the whole style down with it.)
        'symbol-sort-key': ['coalesce', ['get', 'min_zoom'], 15],
        // Slightly smaller than the major roads so the hierarchy still reads,
        // and growing with zoom rather than a flat 12 at every scale.
        'text-size': ['interpolate', ['linear'], ['zoom'], 14, 10.5, 16, 12, 18, 13],
        // Tighter than the default: at this density the padding was rejecting
        // labels that had room for themselves.
        'text-padding': 2,
      });
    }

    /** Mid/low-zoom cartography (z5–z11), Google-informed, own palette.
     *
     *  The stock basemap falls apart between country and city zoom: its
     *  `landcover` fades OUT over z5–7 while `landuse` only fades IN by z11
     *  (a washed-out dead band exactly where a country view lives), every
     *  road is a same-weight hairline, water is pale cyan and small towns
     *  compete with capitals at the same size. This step retunes the
     *  GENERATED layers by id — the upstream basemaps.js is never forked.
     *
     *  Verified against the tiles actually served (probe 2026-08):
     *  `landcover` (farmland/grassland/forest/urban_area) exists in z0–7
     *  tiles, i.e. up to display zoom 8; from z8 tiles the `landuse` layer is
     *  rich (thousands of forest/farmland/residential polygons). The two are
     *  crossfaded so the land never goes dead.
     */
    _tuneMidZoom(layers, flavorName) {
      if (!this._cartoTuned()) return;
      const dark = flavorName === 'dark';
      const c = agenticRoadStyles.CARTO[dark ? 'dark' : 'light'];
      const byId = {};
      for (const layer of layers) byId[layer.id] = layer;
      const paint = (id, patch) => {
        if (byId[id]) byId[id].paint = Object.assign({}, byId[id].paint, patch);
      };
      const layout = (id, patch) => {
        if (byId[id]) byId[id].layout = Object.assign({}, byId[id].layout || {}, patch);
      };
      const minzoom = (id, z) => {
        if (byId[id]) byId[id].minzoom = Math.max(byId[id].minzoom || 0, z);
      };

      // -- landcover: the living landmass at z5–8 -----------------------
      paint('landcover', {
        'fill-color': ['match', ['get', 'kind'],
          'grassland', c.grassland,
          'barren', c.barren,
          'urban_area', c.urban,
          'farmland', c.farmland,
          'glacier', dark ? '#2e3134' : '#eef0ee',
          'scrub', c.scrub,
          c.forest],
        // Stock faded this to 0 by z7 — the direct cause of the dead band.
        // Held at full strength until the z8 tiles (rich landuse) arrive.
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 4, 1, 7, 1, 8.2, 0],
      });
      // Parks/forests from the landuse layer pick up earlier than the stock
      // z11 so the crossfade from landcover is seamless.
      paint('landuse_park', {
        'fill-opacity': ['interpolate', ['linear'], ['zoom'],
          6.5, 0, 7.5, 0.65, 10, 0.9],
      });
      // Farmland + urban wash for the band the stock style leaves blank
      // (landuse has no residential rendering at all upstream).
      const lcAt = layers.findIndex((l) => l.id === 'landcover');
      layers.splice(lcAt + 1, 0, {
        id: 'am-landuse-mid', type: 'fill', source: 'streets',
        'source-layer': 'landuse', minzoom: 6.4, maxzoom: 14,
        filter: ['in', ['get', 'kind'], ['literal',
          ['farmland', 'meadow', 'orchard', 'vineyard', 'allotments',
           'grassland', 'residential', 'commercial', 'retail']]],
        paint: {
          'fill-color': ['match', ['get', 'kind'],
            'residential', c.urban, 'commercial', c.urban, 'retail', c.urban,
            'grassland', c.grassland, 'meadow', c.grassland,
            c.farmland],
          'fill-opacity': ['interpolate', ['linear'], ['zoom'],
            6.4, 0, 7.4, 0.55, 11, 0.45, 13.5, 0],
        },
      });

      // -- road hierarchy: nothing below primary until city approach ----
      // Below these zooms the overview ribbons (road-styles.js) own the
      // picture; the stock layers return exactly when their casings and
      // full styling are ready. Secondary/tertiary/minor stay hidden until
      // ~z10–11 — Google's discipline, and the end of the spaghetti.
      minzoom('roads_highway', 10);
      minzoom('roads_highway_casing_early', 10);
      minzoom('roads_major', 10);
      minzoom('roads_major_casing_early', 10);
      minzoom('roads_link', 10);
      minzoom('roads_minor', 11);
      minzoom('roads_minor_casing', 11);
      minzoom('roads_rail', 10);
      minzoom('roads_tunnels_highway', 10);
      minzoom('roads_tunnels_major', 10);
      minzoom('roads_tunnels_minor', 11);
      minzoom('roads_tunnels_link', 11);
      minzoom('roads_tunnels_other', 11);

      // -- close-zoom street widths: roads as drivable surfaces ----------
      // The stock width tables stop at z18 and clamp, so from ~z17.5 a
      // street stays a thin ribbon while its buildings keep growing.
      // Extend the top anchors to z20 — each new curve passes through the
      // OLD curve's z17 value, so everything below (and the mid-zoom
      // cartography this pass is tested on) is untouched. Bridges and
      // tunnels ride the same tables, and every casing's line-gap-width
      // stays in lockstep with its road's line-width.
      const exp16 = (stops) => ['interpolate', ['exponential', 1.6], ['zoom'], ...stops];
      // Widened through the z15.5–18 band against the aerial ground truth
      // (owner: side streets "silly small" next to the imagery underneath):
      // minor/service streets ~2× their previous width there, the mains
      // (highway/major/link) ~+50%. Every anchor at or below z15 and every
      // z20 top is EXACTLY the previous curve — the mid-zoom cartography
      // and the z20 maximums are frozen by their own assertions. The z15
      // anchors added to minor_service (1.32) and link (2.39) are the OLD
      // curve's own interpolated values at z15: exponential interpolation
      // is self-similar, so splitting a segment at its exact value leaves
      // everything below byte-identical.
      const CLOSE_ROAD_WIDTHS = {
        minor: [11, 0, 12.5, 0.5, 15, 2, 16, 7.5, 17, 13.5, 18, 28, 19, 38, 20, 46],
        minor_service: [13, 0, 15, 1.32, 16, 5.5, 17, 9.4, 18, 22, 19, 29, 20, 34],
        major: [6, 0, 12, 1.6, 15, 3, 16, 7.2, 17, 12.5, 18, 25, 19, 38, 20, 52],
        highway: [3, 0, 6, 1.1, 12, 1.6, 15, 5, 16, 9.5, 17, 16, 18, 30, 19, 44, 20, 58],
        link: [13, 0, 13.5, 1, 15, 2.39, 16, 5.7, 17, 10.5, 18, 21, 19, 32, 20, 42],
      };
      // Casings whose stroke should thicken a touch alongside (a fixed 1 px
      // outline around a 40 px surface reads as a rendering artifact).
      const CLOSE_CASING_STROKES = {
        roads_minor_casing: [12, 0, 12.5, 1, 18, 1.6, 20, 2.6],
        roads_major_casing_late: [9, 0, 9.5, 1, 18, 1.8, 20, 3],
        roads_link_casing: [13, 0, 13.5, 1.5, 18, 2, 20, 3],
      };
      const CASING_IDS = {
        minor: ['roads_minor_casing', 'roads_bridges_minor_casing', 'roads_tunnels_minor_casing'],
        minor_service: ['roads_minor_service_casing'],
        major: ['roads_major_casing_late', 'roads_bridges_major_casing', 'roads_tunnels_major_casing'],
        highway: ['roads_highway_casing_late', 'roads_bridges_highway_casing', 'roads_tunnels_highway_casing'],
        link: ['roads_link_casing', 'roads_bridges_link_casing', 'roads_tunnels_link_casing'],
      };
      for (const family of Object.keys(CLOSE_ROAD_WIDTHS)) {
        const width = exp16(CLOSE_ROAD_WIDTHS[family]);
        for (const prefix of ['roads_', 'roads_bridges_', 'roads_tunnels_']) {
          paint(prefix + family, { 'line-width': width });
        }
        for (const casingId of CASING_IDS[family]) {
          paint(casingId, { 'line-gap-width': width });
        }
      }
      for (const casingId of Object.keys(CLOSE_CASING_STROKES)) {
        paint(casingId, { 'line-width': exp16(CLOSE_CASING_STROKES[casingId]) });
      }

      // -- water: rivers thinner and lighter than lake edges ------------
      paint('water_river', {
        'line-color': c.river,
        'line-width': ['interpolate', ['exponential', 1.6], ['zoom'],
          9, 0, 9.5, 0.6, 14, 1.6, 18, 8],
      });
      paint('water_stream', {
        'line-color': c.river,
        'line-width': ['interpolate', ['linear'], ['zoom'], 14, 0.4, 18, 4],
      });

      // Water names stay water-coloured ink — _legibleLabels' uniform
      // near-black is right for places, wrong for a lake.
      const waterInk = dark ? '#7f97b3' : '#3f699f';
      for (const id of ['water_label_ocean', 'water_label_lakes', 'water_waterway_label']) {
        paint(id, {
          'text-color': waterInk,
          'text-halo-color': dark ? 'rgba(10,16,22,0.7)' : 'rgba(244,248,251,0.85)',
          'text-halo-width': 1.2,
        });
      }

      // -- state borders: present but subordinate to the country line ---
      paint('boundaries', {
        'line-width': ['interpolate', ['linear'], ['zoom'],
          4, 0.5, 8, 0.9, 12, 1.2],
      });

      // -- label hierarchy: capitals pop, small towns yield --------------
      const rank = ['coalesce', ['get', 'population_rank'], 0];
      paint('places_locality', {
        'text-color': ['case',
          ['>=', rank, 11], c.cityInk,
          ['>=', rank, 9], c.cityMid,
          c.cityFaint],
        'text-halo-color': c.labelHalo,
        'text-halo-width': 1.5,
      });
      layout('places_locality', {
        // The stock tiers, with the metropolis tier pushed up a step at
        // country zoom — Berlin must outrank Gera at a glance.
        'text-size': ['interpolate', ['linear'], ['zoom'],
          4, ['case', ['>=', rank, 12], 15, ['>=', rank, 10], 11, 9],
          6, ['case', ['>=', rank, 12], 18, ['>=', rank, 11], 16,
              ['>=', rank, 10], 12.5, 10.5],
          8, ['case', ['>=', rank, 11], 19, ['>=', rank, 9], 13, 11.5],
          10, ['case', ['>=', rank, 9], 20, 12],
          15, ['case', ['>=', rank, 8], 22, 12]],
        // More breathing room than stock: collisions then drop the small
        // towns first (the sort key already prefers the big ones).
        'text-padding': ['interpolate', ['linear'], ['zoom'], 5, 5, 8, 8, 12, 11],
      });
      // Region (Bundesland) names stay quiet context — _legibleLabels made
      // them the same ink as cities, which flattened the hierarchy again.
      paint('places_region', {
        'text-color': dark ? '#6f7a70' : '#8f8a7c',
        'text-halo-color': c.labelHalo,
        'text-halo-width': 1.2,
      });
    }

    /** Swap the whole basemap palette.
     *
     *  Used by the overlay gallery to drop in the neutral canvas: a map that
     *  is going to carry a data ramp must not also be using colour of its own.
     *  Passing null restores the view's stock flavour.
     */
    setFlavorOverride(flavor) {
      this._flavorOverride = flavor;
      // The style key deliberately caches on view+language, so re-setting the
      // same view is a no-op — which is right for a view switch and wrong
      // here. Invalidate it so the palette actually reaches the fills.
      this._styleKey = null;
      this.setView(this.view);
    }

    /** Cached GeoJSON for a payload URL.
     *
     *  Returns an empty collection on the first call and starts a single
     *  fetch; when that resolves the live source is updated in place, so the
     *  data arrives once no matter how often the style is rebuilt.
     */
    _geoData(sourceId, payloadKey) {
      this._geoCache = this._geoCache || {};
      if (this._geoCache[sourceId]) return this._geoCache[sourceId];
      this._geoPending = this._geoPending || {};
      if (!this._geoPending[sourceId]) {
        this._geoPending[sourceId] = fetch(abs(this.payload[payloadKey]))
          .then((response) => response.json())
          .then((data) => {
            this._geoCache[sourceId] = data;
            const live = this.map && this.map.getSource(sourceId);
            if (live && live.setData) live.setData(data);
          })
          .catch((error) => console.error('[am-map] geo fetch failed:', payloadKey, error));
      }
      return EMPTY_COLLECTION;
    }

    /** Borders and country names, worldwide and independent of any regional
     *  extract — without them a zoomed-out view is unreadable geography. */
    _addCountryLayers(sources, layers, flavorName) {
      if (!this.payload.countries_url) return;
      const dark = flavorName === 'dark';
      const onImagery = this.view === 'hybrid' || this.view === 'satellite';
      // Parsed GeoJSON, never a URL. MapLibre refetches a geojson source's
      // URL on every style rebuild, and the style is rebuilt on every view and
      // language switch — so the 1.5 MB of Natural Earth polygons came down
      // four times in a normal session, dwarfing the street tiles they exist
      // to frame. Fetched once here and pushed into the live source when it
      // lands; every later rebuild reuses the parsed object for free.
      sources.countries = { type: 'geojson', data: this._geoData('countries', 'countries_url') };
      if (this.payload.country_labels_url) {
        // Separate POINT source: labelling the polygons makes MapLibre place a
        // label in every tile they touch ("RUSSLAND" twenty times).
        sources['country-points'] = {
          type: 'geojson', data: this._geoData('country-points', 'country_labels_url'),
        };
      }
      const tuned = this._cartoTuned();
      const carto = tuned ? agenticRoadStyles.CARTO[dark ? 'dark' : 'light'] : null;
      // Tuned map modes get a border dark (light theme) / light (dark theme)
      // enough to structure the view — the near-invisible stock line was one
      // of the owner's cartography complaints.
      const border = onImagery ? 'rgba(255,255,255,0.55)'
        : carto ? carto.countryBorder
        : (dark ? 'rgba(190,205,225,0.5)' : 'rgba(90,105,125,0.45)');
      const firstSymbol = layers.findIndex((l) => l.type === 'symbol');
      const insertAt = firstSymbol === -1 ? layers.length : firstSymbol;
      // Land, worldwide. The vector basemap only has земля inside its regional
      // extracts, so beyond them the stage would be bare background — grey
      // nothing where Kazakhstan should be. Reads the MERGED flavor (carto
      // tuning, canvas/theme override and brand colors already applied), so
      // the Natural Earth land beyond the extract matches the tiles inside it.
      const flavor = this._mergedFlavor || basemaps.namedFlavor(flavorName) || {};
      const land = onImagery ? null : (this.payload.brand_colors?.earth || flavor.earth || '#e2dfda');
      if (land) {
        layers.splice(1, 0, { id: 'country-land', type: 'fill', source: 'countries',
          paint: { 'fill-color': land } });
      }
      layers.splice(insertAt, 0,
        { id: 'country-highlight-fill', type: 'fill', source: 'countries',
          filter: NO_COUNTRY,
          paint: { 'fill-color': this.payload.brand_colors?.highlight || '#7cc4ff', 'fill-opacity': 0.16 } },
        { id: 'country-highlight-line', type: 'line', source: 'countries',
          filter: NO_COUNTRY,
          paint: {
            'line-color': this.payload.brand_colors?.highlight || '#7cc4ff',
            'line-width': ['interpolate', ['linear'], ['zoom'], 2, 1.6, 6, 3, 10, 4.5],
            'line-blur': 0.4,
          } },
        // Solid (a dashed border reads as "disputed") and drawn at EVERY zoom:
        // the vector basemap only has boundaries inside its regional extracts,
        // so anything zoom-capped here means borders vanish over the next
        // country. This is the one layer that must never go missing.
        { id: 'country-borders', type: 'line', source: 'countries',
          paint: {
            'line-color': border,
            'line-width': carto
              ? ['interpolate', ['linear'], ['zoom'], 1, 0.8, 4, 1.3, 8, 2.1, 12, 2.5]
              : ['interpolate', ['linear'], ['zoom'], 1, 0.6, 4, 1.1, 8, 1.8, 12, 2.2],
          } },
      );
      // Country borders now have exactly one source (worldwide, consistent),
      // so the basemap's own country line would only double it. Its *state*
      // boundaries stay — those are the German Länder and NE has no data for
      // them at this resolution.
      for (const layer of layers) {
        if (layer.id === 'places_country') {
          layer.layout = Object.assign({}, layer.layout, { visibility: 'none' });
        }
        if (layer.id === 'boundaries_country') {
          layer.layout = Object.assign({}, layer.layout, { visibility: 'none' });
        }
        if (layer.id === 'boundaries' && layer.paint) {
          layer.paint = Object.assign({}, layer.paint);
          delete layer.paint['line-dasharray'];
        }
      }
      layers.push({
        id: 'country-labels', type: 'symbol',
        source: this.payload.country_labels_url ? 'country-points' : 'countries',
        minzoom: 1, maxzoom: 7,
        layout: {
          'text-field': ['coalesce', ['get', 'name_' + this.lang], ['get', 'name_en'], ['get', 'name']],
          'text-font': ['Noto Sans Medium'],
          'text-size': ['interpolate', ['linear'], ['zoom'], 2, 11, 5, 16, 7, 20],
          'text-letter-spacing': 0.18,
          'text-transform': 'uppercase',
          'text-max-width': 8,
        },
        paint: {
          'text-color': onImagery ? '#ffffff' : (dark ? '#d7e2f2' : MAP_LABEL_INK),
          'text-halo-color': onImagery || dark ? 'rgba(8,12,20,0.85)' : 'rgba(255,255,255,0.9)',
          'text-halo-width': 1.8,
          'text-halo-blur': 0.6,
        },
      });
    }

    /** Labels over aerial imagery need a real halo, not the basemap's hairline:
     *  white-on-photo is what made the last screenshots hard to read. */
    _legibleLabels(layers, flavorName) {
      const onImagery = this.view === 'hybrid' || this.view === 'satellite';
      if (!onImagery) {
        // Pure cartography. The stock flavors set place names in a light grey
        // that reads as fog: too pale on white, too dim on the dark theme.
        const dark = flavorName === 'dark';
        for (const layer of layers) {
          if (layer.type !== 'symbol' || !layer.paint || layer.id.startsWith('country-')) continue;
          // Route numbers are signage, not cartography: their ink is dictated
          // by the plate behind them (white on the blue Autobahn badge, black
          // on the yellow Bundesstraße one). Repainting them one theme colour
          // is what made every shield's text the same again. Covers the
          // low-zoom band layer (roads-shields-low) as well.
          if (layer.id.indexOf('roads-shields') === 0) continue;
          if (!('text-color' in layer.paint)) continue;
          layer.paint = Object.assign({}, layer.paint, dark
            ? { 'text-color': '#f2f6fb', 'text-halo-color': 'rgba(6,10,16,0.92)', 'text-halo-width': 1.5 }
            : { 'text-color': MAP_LABEL_INK, 'text-halo-color': 'rgba(255,255,255,0.9)', 'text-halo-width': 1.3 });
        }
        return;
      }
      for (const layer of layers) {
        // Motorway shields without the motorway are noise: over imagery the
        // road net only starts at ROADS_OVER_MINZOOM, so the shields wait for it.
        if (layer.id === 'roads_shields' || layer.id === 'shield_text') {
          layer.minzoom = Math.max(layer.minzoom || 0, SHIELD_MINZOOM);
        }
        if (layer.type !== 'symbol' || !layer.paint) continue;
        if (layer.id.startsWith('country-')) continue;
        layer.paint = Object.assign({}, layer.paint, {
          'text-color': '#ffffff',
          'text-halo-color': 'rgba(6,10,16,0.9)',
          'text-halo-width': 1.7,
          'text-halo-blur': 0.4,
        });
        layer.layout = Object.assign({}, layer.layout);
        const size = layer.layout['text-size'];
        // Nudge sizes up ~15%: photo backgrounds eat contrast at small sizes.
        if (typeof size === 'number') layer.layout['text-size'] = Math.round(size * 1.15);
      }
    }

    /** Extruded buildings for the Google-style close-zoom 3D view.
     *
     *  Part of every vector style from z15 (the Protomaps tiles carry a
     *  `buildings` source-layer with real `height`/`min_height` metres on
     *  most features — verified against the served tiles; unmeasured ones
     *  get a modest 8 m default). The layer is built with opacity 0 and only
     *  fades in while the camera is pitched (_syncBuildings): the flat
     *  top-down map stays clean, exactly like Google's 2D view.
     *
     *  Being part of _buildStyle means it survives every view/theme rebuild
     *  for free — the same guarantee the imagery and country layers have.
     */
    _addBuildingsLayer(layers, flavorName) {
      // Satellite is pure imagery by contract; grey boxes over it would
      // contradict "imagery only".
      if (this.view === 'satellite') return;
      const dark = flavorName === 'dark' && this.view !== 'hybrid';
      const canvas = !!(this._flavorOverride && this._flavorOverride.__canvas);
      // Slight vertical ramp (taller = lighter) + MapLibre's own vertical
      // gradient on the walls, so a city is depth-readable rather than a
      // uniform slab field. The canvas palette drains it to neutral: the
      // most saturated thing there must stay the data.
      const low = canvas ? '#c9c8c5' : dark ? '#2c3644' : '#d9d5cc';
      const high = canvas ? '#d6d5d2' : dark ? '#415062' : '#efede7';
      // Clamped: a mis-tagged height (hundreds of metres on a shed) must
      // render as a tall building, not as a monolith over the whole quarter.
      const height = ['min', ['coalesce', ['get', 'height'], 8], 400];
      const firstSymbol = layers.findIndex((l) => l.type === 'symbol');
      layers.splice(firstSymbol === -1 ? layers.length : firstSymbol, 0, {
        id: 'buildings-3d', type: 'fill-extrusion', source: 'streets',
        'source-layer': 'buildings', minzoom: 15,
        // Buildings ONLY. The tiles' `buildings` source-layer also carries
        // `building_part` (OSM 3D micro-mapping: roof volumes, and whole
        // planned towers — a 100 m `building_part` at the Elbtower site
        // rendered as a giant translucent box) and `address` points. Every
        // part shares its footprint with its parent building, so drawing
        // both classes put two coplanar walls in the depth buffer — the
        // z-fight shimmer the owner reported.
        filter: ['==', ['get', 'kind'], 'building'],
        paint: {
          'fill-extrusion-color': ['interpolate', ['linear'], height, 4, low, 80, high],
          // Grown in over ~z15–15.7 so the skyline rises as you approach
          // instead of popping in as a wall of prisms.
          'fill-extrusion-height': ['interpolate', ['linear'], ['zoom'],
            15, 0, 15.7, height],
          // Base lifted an epsilon off the ground: 2609 of 2661 probed
          // features carry no min_height, and a base polygon exactly
          // coplanar with the ground plane shimmers per-frame at pitch.
          // 0.1 m is far below anything a viewer could read as floating.
          'fill-extrusion-base': ['interpolate', ['linear'], ['zoom'],
            15, 0, 15.7, ['max', ['coalesce', ['get', 'min_height'], 0], 0.1]],
          'fill-extrusion-opacity': 0,     // pitch > 0 fades it in
          'fill-extrusion-vertical-gradient': true,
        },
      });
    }

    /** Buildings follow the camera pitch: extruded city at a tilt, clean flat
     *  cartography when looking straight down. Paint-only — cheap, animated
     *  by MapLibre's own opacity transition. */
    _syncBuildings() {
      // Fully opaque when up: any sub-1 opacity routes fill-extrusion
      // through MapLibre's translucent two-pass path (verified in the
      // vendored bundle: `1 !== opacity` forces the depth-prepass branch),
      // stacks every wall ghost-on-ghost and washes the city into fog
      // slabs over imagery. Opaque + the vertical gradient reads clean in
      // both hybrid and map modes; the fade-in transition still animates
      // through the translucent path, ending on the depth-correct one.
      const target = this.map.getPitch() > 0.5 ? 1 : 0;
      try {
        if (!this.map.getLayer('buildings-3d')) return;
        // Compared against the LIVE paint value, not a cached flag: a style
        // rebuild bakes opacity 0 back in, and a stale flag would leave a
        // pitched camera looking at a flat city after a theme switch.
        if (this.map.getPaintProperty('buildings-3d', 'fill-extrusion-opacity') === target) return;
        this.map.setPaintProperty('buildings-3d', 'fill-extrusion-opacity', target);
      } catch (e) { /* style mid-swap; the rebuilt style re-syncs */ }
    }

    /** Thin the POI layer while the camera is tilted.
     *
     *  At z16 with pitch ~55° the whole city block sheet leans into view at
     *  once, and the stock POI density (fine flat) buries the extruded
     *  buildings under hundreds of icons. While pitched past ~20° only
     *  high-rank POIs survive: stations, museums and the other landmark
     *  classes, plus anything the tiles themselves rank as important
     *  (min_zoom — the zoom Protomaps says a POI becomes worth drawing at).
     *  Restaurant/shop noise returns only when flat again or much closer
     *  (two zoom levels past its own min_zoom). Survivors also get generous
     *  text-padding so the few remaining labels breathe.
     *
     *  Applied on pitchend (placement re-runs on a filter swap — too heavy
     *  per frame) and re-applied via styledata after every style rebuild.
     *  Idempotent by construction: it compares the standing filter before
     *  touching anything, so its own setFilter/styledata echo is a no-op.
     */
    _syncPoiDensity() {
      if (!this.usePro) return;
      try {
        if (!this.map.getLayer('pois')) return;
        const pitched = this.map.getPitch() > 20;
        const currentJson = JSON.stringify(this.map.getFilter('pois') || null);
        const thinnedJson = this._poiThinnedFilter
          ? JSON.stringify(this._poiThinnedFilter) : null;
        if (pitched) {
          if (currentJson === thinnedJson) return;   // already standing
          // Whatever stands now IS the stock state to restore to — captured
          // fresh here so a style rebuild can never leave a stale copy.
          this._poiStockFilter = this.map.getFilter('pois') || null;
          this._poiStockPadding = this.map.getLayoutProperty('pois', 'text-padding');
          const rank = ['any',
            // Landmark classes: always worth a pin, even leaned back.
            ['in', ['get', 'kind'], ['literal', POI_LANDMARK_KINDS]],
            // The tiles' own importance ranking: a POI the schema draws from
            // z13 is a city-defining place, whatever its class.
            ['<=', ['coalesce', ['get', 'min_zoom'], 30], 13],
            // "Much closer": everything returns 2.5 levels past its own rank
            // (a z16 restaurant re-appears pitched from z18.5).
            ['>=', ['zoom'], ['+', ['coalesce', ['get', 'min_zoom'], 30], 2.5]],
          ];
          this._poiThinnedFilter = this._poiStockFilter
            ? ['all', this._poiStockFilter, rank] : rank;
          this.map.setFilter('pois', this._poiThinnedFilter);
          this.map.setLayoutProperty('pois', 'text-padding', 14);
        } else {
          // Only undo what this pass itself applied — a stock filter (or a
          // rebuilt style's fresh one) is left exactly as it is.
          if (thinnedJson === null || currentJson !== thinnedJson) return;
          this.map.setFilter('pois', this._poiStockFilter);
          this.map.setLayoutProperty('pois', 'text-padding',
            this._poiStockPadding === undefined ? 2 : this._poiStockPadding);
          this._poiThinnedFilter = null;
        }
      } catch (e) { /* style mid-swap; the rebuilt style re-syncs */ }
    }

    /** Relabel the whole map — cities, streets and countries in one go. */
    setLanguage(lang) {
      this.lang = lang;
      this.payload.lang = lang;
      if (!this.usePro) return;
      this.setView(this.view);   // style key includes the language
    }

    /** Frame a country and outline it (search for "Deutschland").
     *
     *  NOT keyed on iso alone: Natural Earth stamps '-99' on every feature it
     *  has no clean code for — France, Norway, Kosovo, Somaliland and four
     *  more share it — so an iso-only filter for "Frankreich" also washed the
     *  highlight over Norway and Kosovo. The English `name` is unique within
     *  the dataset and present on both the search hit and the polygons, so it
     *  disambiguates; iso keeps working alone for every real code. */
    highlightCountry(hit) {
      if (!hit) return this.clearCountry();
      const filter = (hit.iso && hit.iso !== '-99')
        ? ['==', ['get', 'iso'], hit.iso]
        : ['==', ['get', 'name'], hit.name || ''];
      this.map.setFilter('country-highlight-fill', filter);
      this.map.setFilter('country-highlight-line', filter);
      this.map.fitBounds(
        [[hit.bbox.west, hit.bbox.south], [hit.bbox.east, hit.bbox.north]],
        { padding: 80, duration: 1400 },
      );
      // The whole filter, not just the code — the style rebuild on a view or
      // language switch re-applies exactly what was standing.
      this._country = filter;
      // The globe re-draws the traced border on the sphere from this.
      this.el.dispatchEvent(new CustomEvent('agentic-map:country-changed', {
        bubbles: true, detail: { filter },
      }));
    }

    clearCountry() {
      this._country = null;
      try {
        this.map.setFilter('country-highlight-fill', NO_COUNTRY);
        this.map.setFilter('country-highlight-line', NO_COUNTRY);
      } catch (e) { /* style not built yet */ }
      this.el.dispatchEvent(new CustomEvent('agentic-map:country-changed', {
        bubbles: true, detail: { filter: null },
      }));
    }

    /** Identity of the style a view needs.
     *
     *  Label treatment and border colours depend on whether imagery is under
     *  them, so the key covers that as well as the flavor — hybrid and
     *  map-light share the 'light' flavor but must not share a style.
     */
    _styleKeyFor(view) {
      const onImagery = view === 'hybrid' || view === 'satellite';
      return this._flavorFor(view) + '|' + onImagery + '|' + this.lang;
    }

    setView(view) {
      // The attribution follows the visible layer — a view switch changes
      // whose rights apply.
      this._attribKey = null;
      setTimeout(() => this._refreshAttribution(), 0);
      // 'hybrid' | 'satellite' | 'map-dark' | 'map-light'
      this.view = view;
      const flavorName = this._flavorFor(view);
      const styleKey = this._styleKeyFor(view);
      if (this.usePro && styleKey !== this._styleKey) {
        this._styleKey = styleKey;
        this.map.setStyle(this._buildStyle(flavorName));
        const restore = () => {
          for (const route of this.routes) this._drawRoute(route, { instant: true });
          // The alternates too — their layers die with the old style exactly
          // like the primary's, and a theme switch must not silently eat the
          // other candidate routes. (Their badge markers are DOM and survive;
          // _drawAlternates re-creates only what is missing.)
          for (const route of this.routes) this._drawAlternates(route);
          for (const g of this._geo) this._drawGeo(g);
          if (this._country) {
            this.map.setFilter('country-highlight-fill', this._country);
            this.map.setFilter('country-highlight-line', this._country);
          }
          this._applyVisibility();
          this.el.dispatchEvent(new CustomEvent('agentic-map:style-restored', {
            bubbles: true, detail: { view: this.view },
          }));
        };
        // Restore on the FIRST event after the new style is usable, NOT on
        // 'idle': idle waits for every basemap tile to arrive and decode, so
        // a theme switch left routes/highlights invisible for seconds. The
        // restored layers are client-side GeoJSON — they need the style
        // object, never a single tile. 'style.load' fires per setStyle()
        // (unlike 'load', which fires once per map and once broke the
        // "Karte shows orthophotos" restore); the styledata retry is the
        // belt-and-braces path should a layer add still be refused mid-swap.
        let restored = false;
        const tryRestore = () => {
          if (restored) return;
          try { restore(); restored = true; }
          catch (e) { this.map.once('styledata', tryRestore); }
        };
        this.map.once('style.load', tryRestore);
        this.map.once('styledata', tryRestore);
      } else {
        this._applyVisibility();
      }
      this.el.dispatchEvent(new CustomEvent('agentic-map:view', { bubbles: true, detail: { view } }));
    }

    _applyVisibility() {
      if (!this.map.getStyle()) return;
      const showOrtho = this.view === 'hybrid' || this.view === 'satellite';
      for (const layer of this.map.getStyle().layers) {
        let visible = true;
        if (layer.id === 'ortho' || layer.id === 'world') visible = showOrtho;
        else if (layer.id === 'roads-over' || layer.id === 'boundaries-over') visible = this.view === 'hybrid';
        else if (layer.id.startsWith('roads-overview-')) visible = this.view !== 'satellite';
        else if (layer.id.startsWith('route-') || layer.id.startsWith('am-geo-')) visible = true;
        // Borders and country names stay put in every mode — they are the
        // geography, not decoration.
        else if (layer.id.startsWith('country-')) visible = true;
        // Buildings exist in every non-satellite style; whether they SHOW is
        // the pitch gate's job (opacity), not visibility's.
        else if (layer.id === 'buildings-3d') visible = this.view !== 'satellite';
        else if (this.view === 'satellite') visible = layer.type === 'background';
        try {
          this.map.setLayoutProperty(layer.id, 'visibility', visible ? 'visible' : 'none');
        } catch (e) { /* layer without layout */ }
      }
    }

    // -- decorations ----------------------------------------------------

    _marker(inner, lngLat, anchor, scaling) {
      const wrap = document.createElement('div');
      wrap.appendChild(inner);
      inner.style.transformOrigin = anchor === 'bottom' ? 'bottom center' : 'center';
      const marker = new maplibregl.Marker({ element: wrap, anchor })
        .setLngLat(lngLat)
        .addTo(this.map);
      this.markers.push(marker);
      if (scaling) this._scaled.push(Object.assign({ inner }, scaling));
      return marker;
    }

    /** Keep the on-map credit truthful: ask the federation which sources
     *  actually cover the current viewport and show only those.
     *
     *  A fixed line listing every source the map could ever use credits
     *  rights holders whose data is not on screen (Blue Marble at street
     *  zoom) and hides the one that is. The server resolves per tile anyway,
     *  so it can answer exactly this.
     */
    _refreshAttribution() {
      if (!this._attribCtl) return;
      // Crucial: in a pure map view NO aerial imagery is visible. Naming
      // its rights holder there is just as wrong as leaving him out when it
      // is visible — the credit follows the layer actually on screen, not
      // whatever could be loaded.
      var showsImagery = this.view === 'hybrid' || this.view === 'satellite';
      var showsVector = this.view !== 'satellite';
      var b = this.map.getBounds();
      var z = Math.round(this.map.getZoom());
      var key = [this.view, this._orthoSourceId, z, b.getWest().toFixed(2), b.getSouth().toFixed(2),
                 b.getEast().toFixed(2), b.getNorth().toFixed(2)].join('|');
      if (key === this._attribKey) return;
      this._attribKey = key;
      var self = this;
      var vector = (showsVector && this.usePro) ? '\u00a9 OpenStreetMap' : '';
      // Responses can arrive out of order: a request issued from the aerial
      // view must not overwrite the map view's later response — otherwise a
      // right ends up on the slide whose data is not on screen at all.
      var stamp = key;
      var apply = function (imagery) {
        if (self._attribKey !== stamp) return;
        var parts = [imagery, vector].filter(Boolean);
        var text = parts.join(' | ');
        self._attribText = text;
        self._attribCtl.options.customAttribution = text;
        if (self.el.dataset.dsAttribOut === 'host') {
          // Miniatures do not carry the credit themselves — the host
          // collects it (see docs/concepts/map-attribution.md).
          try {
            window.parent && window.parent.postMessage(
              { type: 'am-map-attribution', tag: self.el.dataset.dsTag || '',
                text: text }, '*');
          } catch (e) { /* no host */ }
          return;
        }
        // Re-adding is the only way to replace the control's text — it
        // only reads it when it is added.
        try {
          self.map.removeControl(self._attribCtl);
          self.map.addControl(self._attribCtl);
        } catch (e) { /* control already removed */ }
      };
      if (!showsImagery || !this._orthoSourceId) { apply(''); return; }
      // Sealed (offline): resolved at sealing time from the tiles that
      // actually shipped, and resolved PER ZOOM — the imagery ladder is banded
      // by scale, so the rights over a rooftop are not the rights over a
      // continent. It still has to run through `apply`, or a miniature whose
      // credit line the host collects would silently carry none.
      if (this.payload.offline) {
        apply(this._sealedImageryCredit(z));
        return;
      }
      var base = this._apiBase || '/api/v1/maps';
      var url = base + '/attribution?src=' + encodeURIComponent(this._orthoSourceId) +
        '&z=' + z + '&west=' + b.getWest() + '&south=' + b.getSouth() +
        '&east=' + b.getEast() + '&north=' + b.getNorth();
      fetch(url).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { apply(d ? d.text : ''); }).catch(function () { apply(''); });
    }

    /** The sealed imagery credit for a zoom level.
     *
     *  The table only has the zooms the recording actually visited, and a
     *  viewer can land between them (a wider window, the ambient drift). The
     *  NEAREST recorded zoom is the honest answer: it is the band the tiles on
     *  screen come from. Falling back to "everything ever used" would put NASA
     *  under a street map again, which is the bug this replaced.
     */
    _sealedImageryCredit(zoom) {
      const table = this.payload.attribution_zooms;
      if (!table) return this.payload.attribution || '';
      if (table[zoom] !== undefined) return table[zoom];
      let best = null;
      let bestGap = Infinity;
      for (const key of Object.keys(table)) {
        const gap = Math.abs(Number(key) - zoom);
        if (gap < bestGap) { bestGap = gap; best = table[key]; }
      }
      return best === null ? '' : best;
    }

    _scheduleLabelLayout() {
      clearTimeout(this._labelTimer);
      this._labelTimer = setTimeout(() => this._layoutLabels(), 60);
      if (!this._labelHooked) {
        this._labelHooked = true;
        // Markers travel with the camera: an arrangement once found no
        // longer holds after any flight.
        ['move', 'moveend', 'zoomend', 'idle'].forEach((ev) => {
          this.map.on(ev, () => {
            clearTimeout(this._labelTimer);
            this._labelTimer = setTimeout(() => this._layoutLabels(), 60);
          });
        });
      }
    }

    _rescale() {
      const z = this.map.getZoom();
      for (const s of this._scaled) {
        const scale = clamp(Math.pow(2, (z - s.anchorZoom) * s.exponent), s.minScale, s.maxScale);
        const faded = s.fadeBelow !== undefined && z < s.fadeBelow;
        s.inner.style.transform = 'scale(' + scale.toFixed(3) + ')';
        s.inner.style.opacity = faded ? '0' : '1';
      }
    }

    // Geographic circle polygon (meters on the ground, true footprint).
    _circlePolygon(center, radiusM, segments) {
      const coords = [];
      const latRad = center.lat * Math.PI / 180;
      for (let i = 0; i <= (segments || 64); i++) {
        const angle = (i / (segments || 64)) * 2 * Math.PI;
        coords.push([
          center.lon + (radiusM * Math.sin(angle)) / (111320 * Math.cos(latRad)),
          center.lat + (radiusM * Math.cos(angle)) / 110540,
        ]);
      }
      return coords;
    }

    /** Geo highlights (radius/polygon/line) changed. The globe listens and
     *  re-draws them as sphere ribbons; the flat layers are untouched. */
    _overlaysChanged() {
      this.el.dispatchEvent(new CustomEvent('agentic-map:overlays-changed', {
        bubbles: true, detail: { count: this._geo.length },
      }));
    }

    _drawGeo(g) {
      if (this.map.getSource(g.sourceId)) return;
      this.map.addSource(g.sourceId, { type: 'geojson', data: g.data });
      const firstSymbol = this.map.getStyle().layers.find((l) => l.type === 'symbol');
      const beforeId = firstSymbol ? firstSymbol.id : undefined;
      for (const layer of g.layers) this.map.addLayer(layer, beforeId);
    }

    _addGeoHighlight(h) {
      // A LINE highlight is not a degenerate polygon: it traces a way (a
      // street, a frontage, a boundary run) and must not be filled or closed.
      // Without it the only way to point at a street was to fake it with a
      // route, which invents a duration badge and a travel mode nobody asked
      // for — and there was no way at all to CHECK that a drawn plot sits
      // flush with the road it fronts.
      if (h.kind === 'line') return this._addLineHighlight(h);
      const ring = h.kind === 'radius'
        ? this._circlePolygon(h.at, h.radius_m)
        : h.polygon.map((p) => [p.lon, p.lat]).concat([[h.polygon[0].lon, h.polygon[0].lat]]);
      // Monotonic ids, never derived from the array length: clearing resets
      // the length, and a recycled id collides with a stale deferred draw —
      // the ghost stays, the new polygon never appears.
      this._geoSeq = (this._geoSeq || 0) + 1;
      const sourceId = 'am-geo-' + this._geoSeq;
      const g = {
        sourceId,
        data: { type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] } },
        layers: [
          { id: sourceId + '-fill', type: 'fill', source: sourceId,
            paint: { 'fill-color': h.color, 'fill-opacity': h.opacity } },
          { id: sourceId + '-line', type: 'line', source: sourceId,
            paint: { 'line-color': h.color, 'line-width': 2, 'line-opacity': 0.85,
                     'line-dasharray': [3, 2] } },
        ],
      };
      this._geo.push(g);
      // The deferred branch must re-check membership: a scene switch can clear
      // the highlights before 'idle' fires, and an orphaned draw would leave a
      // layer nothing knows how to remove.
      this.map.loaded()
        ? this._drawGeo(g)
        : this.map.once('idle', () => { if (this._geo.includes(g)) this._drawGeo(g); });
      if (h.label) {
        const chip = document.createElement('div');
        chip.className = 'am-map-geo-label';
        chip.textContent = h.label;
        chip.style.borderColor = h.color;
        this._marker(chip, ring[0], 'center', null);
      }
      this._overlaysChanged();
    }

    _addLineHighlight(h) {
      const coords = (h.line || []).map((p) => [p.lon, p.lat]);
      if (coords.length < 2) return;
      this._geoSeq = (this._geoSeq || 0) + 1;
      const sourceId = 'am-geo-' + this._geoSeq;
      const width = h.width || 6;
      const g = {
        sourceId,
        data: { type: 'Feature', geometry: { type: 'LineString', coordinates: coords } },
        layers: [
          // Casing first: a coloured way over a grey basemap street is only
          // legible if it is separated from it.
          { id: sourceId + '-casing', type: 'line', source: sourceId,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': '#10151c', 'line-width': width + 3,
                     'line-opacity': 0.55 } },
          { id: sourceId, type: 'line', source: sourceId,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': h.color, 'line-width': width,
                     'line-opacity': h.opacity == null ? 0.9 : h.opacity } },
        ],
      };
      this._geo.push(g);
      this.map.loaded()
        ? this._drawGeo(g)
        : this.map.once('idle', () => { if (this._geo.includes(g)) this._drawGeo(g); });
      if (h.label) {
        const chip = document.createElement('div');
        chip.className = 'am-map-geo-label';
        chip.textContent = h.label;
        chip.style.borderColor = h.color;
        this._marker(chip, coords[Math.floor(coords.length / 2)], 'center', null);
      }
      this._overlaysChanged();
    }

    pingAt(lngLat, label, options) {
      // Sonar-style attention rings; auto-removes unless persistent.
      const el = document.createElement('div');
      el.className = 'am-map-ping';
      // still: a static ring instead of the sonar animation — for instant/
      // print states whose final frame must be pixel-stable.
      if (options && options.still) el.classList.add('still');
      el.innerHTML = '<b></b><i></i><i></i><i></i>' + (label ? '<span>' + label + '</span>' : '');
      const marker = this._marker(el, lngLat, 'center', null);
      const ttl = options && options.ttl_ms;
      if (ttl) setTimeout(() => marker.remove(), ttl);
      this._scheduleLabelLayout();
      return marker;
    }

    /** Place ping labels so they never overlap each other or the site pin.
     *
     *  A label pinned below its dot is right until two dots sit close
     *  together — then the labels cover each other and the map underneath.
     *  Each label therefore gets a list of anchors (below, above, right,
     *  left, and diagonals) and takes the first one that is free, measured
     *  against everything already placed. Order matters: labels are placed
     *  from the top of the screen down, so the layout is stable while the
     *  camera moves.
     */
    _layoutLabels() {
      // Candidates as a pure TRANSLATION (dx, dy) relative to the marker.
      // Between `top:auto` and `bottom:22px` nothing can be animated — a
      // translation can be, and only that way does a label glide smoothly
      // to its new place instead of popping away and back in.
      const DIRS = [
        [0, 1], [0, -1], [1, 0], [-1, 0],
        [0.72, 0.72], [0.72, -0.72], [-0.72, 0.72], [-0.72, -0.72],
      ];
      const ANCHORS = [];
      [22, 46, 76, 110].forEach((d) => DIRS.forEach((u) =>
        ANCHORS.push({ dx: u[0] * d, dy: u[1] * d })));
      const labels = Array.from(this.el.querySelectorAll(
        '.am-map-ping span, .am-map-poi-label'));
      if (!labels.length) return;
      // Occupied areas: everything that is not a label but needs space.
      // Everything that claims space and cannot itself be moved aside:
      // pins, the site medallion, POI symbols, ping cores, route badges
      // and geo labels.
      const taken = Array.from(this.el.querySelectorAll(
        '.am-map-pin-bubble, .im-site-pin, .am-map-poi, .am-map-ping b, ' +
        '.am-map-route-badge, .am-map-geo-label, .am-map-highlight'))
        .map((n) => n.getBoundingClientRect())
        .filter((r) => r.width > 1 && r.height > 1);
      labels.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
      const hits = (r) => taken.some((o) =>
        r.left < o.right - 2 && r.right > o.left + 2 &&
        r.top < o.bottom - 2 && r.bottom > o.top + 2);
      labels.forEach((el) => {
        // Pin down the base position once: centred over the marker, all
        // the rest is translation.
        if (!el.dataset.agenticAnchored) {
          el.dataset.agenticAnchored = '1';
          el.style.top = '0';
          el.style.bottom = 'auto';
          el.style.left = '0';
          el.style.right = 'auto';
          el.style.transition = 'transform .3s cubic-bezier(.22,.61,.36,1)';
        }
        const w = el.offsetWidth, h = el.offsetHeight;
        let best = null, bestRect = null;
        for (const a of ANCHORS) {
          // Rectangle at this spot, without rendering to get it.
          const base = el.parentElement.getBoundingClientRect();
          const x = base.left + base.width / 2 + a.dx - w / 2;
          const y = base.top + base.height / 2 + a.dy - h / 2;
          const r = { left: x, top: y, right: x + w, bottom: y + h };
          if (!hits(r)) { best = a; bestRect = r; break; }
          if (!best) { best = a; bestRect = r; }
        }
        el.style.transform = 'translate(' +
          (best.dx - w / 2).toFixed(1) + 'px,' + (best.dy - h / 2).toFixed(1) + 'px)';
        taken.push(bestRect);
      });
    }

    pingFeatures(kinds, options) {
      // Ping POIs of the given kinds from the loaded vector tiles (e.g.
      // nearby public transport: station, tram_stop, bus_stop).
      if (!this.usePro) return 0;
      const bounds = this.map.getBounds();
      const seen = new Set();
      let count = 0;
      const features = this.map.querySourceFeatures('streets', { sourceLayer: 'pois' });
      for (const f of features) {
        const kind = f.properties && f.properties.kind;
        if (!kinds.includes(kind) || f.geometry.type !== 'Point') continue;
        const [lon, lat] = f.geometry.coordinates;
        if (!bounds.contains([lon, lat])) continue;
        const key = kind + '|' + lon.toFixed(4) + '|' + lat.toFixed(4);
        if (seen.has(key)) continue;
        seen.add(key);
        this.pingAt([lon, lat], (f.properties.name || '').split(' ')[0],
          { ttl_ms: (options && options.ttl_ms) || 8000 });
        if (++count >= ((options && options.limit) || 24)) break;
      }
      return count;
    }

    _addDecorations(loc) {
      const anchorZoom = loc.camera.zoom;
      for (const h of loc.highlights || []) {
        if (h.kind === 'radius' || h.kind === 'polygon' || h.kind === 'line') { this._addGeoHighlight(h); continue; }
        if (h.kind === 'ping') {
          const marker = this.pingAt([h.at.lon, h.at.lat], h.label, {});
          const inner = marker.getElement().firstChild;
          this._scaled.push({ inner, anchorZoom, exponent: 0.4, minScale: 0.4, maxScale: 1.0,
                              fadeBelow: anchorZoom - 3.0 });
          continue;
        }
        const el = document.createElement('div');
        el.className = 'am-map-highlight';
        el.style.width = el.style.height = h.radius_px * 2 + 'px';
        el.style.background = this._rgba(h.color, h.opacity);
        el.innerHTML = '<span>' + h.label + '</span>';
        // Geographic scaling: sized for the detail zoom, shrinks at 2^Δz and
        // disappears entirely once the camera is far above the location.
        this._marker(el, [h.at.lon, h.at.lat], 'center', {
          anchorZoom, exponent: 1.0, minScale: 0.2, maxScale: 1.1, fadeBelow: anchorZoom - 2.0,
        });
      }
      if (loc.pin) {
        const el = document.createElement('div');
        el.className = 'am-map-pin';
        const bubble = document.createElement('div');
        bubble.className = 'am-map-pin-bubble';
        bubble.style.width = bubble.style.height = loc.pin.diameter_px + 'px';
        bubble.style.borderColor = loc.pin.color;
        if (loc.pin.image_url) {
          bubble.innerHTML = '<img src="' + loc.pin.image_url + '" alt="">';
        } else if (loc.pin.label) {
          const span = document.createElement('span');
          span.textContent = loc.pin.label;
          span.style.fontSize = _pinFontSize(loc.pin.label, loc.pin.diameter_px) + 'px';
          bubble.appendChild(span);
        }
        el.appendChild(bubble);
        // Pins shrink more gently — still visible as markers at overview.
        this._marker(el, [loc.camera.center.lon, loc.camera.center.lat], 'bottom', {
          anchorZoom, exponent: 0.45, minScale: 0.38, maxScale: 1.0,
        });
      }
    }

    _rgba(hex, opacity) {
      const n = parseInt(hex.replace('#', ''), 16);
      return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + opacity + ')';
    }

    // -- routes ---------------------------------------------------------

    addRoute(route) {
      if (this.routes.some((r) => r.id === route.id)) return;
      this.routes.push(route);
      if (route.animate_ms) {
        this._drawRoute(route, { animate: true });
      } else {
        this._drawRoute(route, { instant: true });
        // Multi-stop trips carry one badge per leg (each at its own
        // midpoint); the total lives in the panel summary. A plain A→B
        // route keeps the single badge exactly as before.
        if (this._legSpans(route)) this._legBadges(route);
        else this._routeBadge(route);
      }
      this._drawAlternates(route);
      this._routesChanged();
    }

    /** Routes changed (added or cleared). The globe listens: it cannot draw
     *  routes, so while one stands it must not take the stage. */
    _routesChanged() {
      this.el.dispatchEvent(new CustomEvent('agentic-map:routes-changed', {
        bubbles: true, detail: { count: this.routes.length },
      }));
    }

    /** Colour for leg `index` of a multi-stop route. */
    legColor(route, index) {
      if (index === 0) return route.color;
      const own = (route.color || '').toLowerCase();
      const rest = LEG_COLORS.filter((c) => c !== own);
      return rest[(index - 1) % rest.length];
    }

    /** Vertex ranges of each stop-to-stop leg, or null for a plain A→B route.
     *
     *  `route.geometry` is one flat list; the route passes through every via,
     *  so each intermediate stop's nearest vertex is the cut. The scan for
     *  stop N resumes where stop N-1 cut, which keeps the cuts ordered even
     *  when the route doubles back near an earlier stop. Cached on the route
     *  object — the restore path redraws after every style swap. */
    _legSpans(route) {
      if (route._legSpans !== undefined) return route._legSpans;
      const stops = route.stops || [];
      const legs = route.legs || [];
      const geometry = route.geometry || [];
      if (stops.length < 3 || legs.length !== stops.length - 1 || geometry.length < 4) {
        route._legSpans = null;
        return null;
      }
      const cuts = [];
      let from = 0;
      for (let s = 1; s < stops.length - 1; s++) {
        const cosLat = Math.cos(stops[s].lat * Math.PI / 180);
        let best = from, bestD = Infinity;
        for (let i = from; i < geometry.length; i++) {
          const dx = (geometry[i].lon - stops[s].lon) * cosLat;
          const dy = geometry[i].lat - stops[s].lat;
          const d = dx * dx + dy * dy;
          if (d < bestD) { bestD = d; best = i; }
        }
        cuts.push(best);
        from = best;
      }
      const bounds = [0, ...cuts, geometry.length - 1];
      const spans = [];
      for (let i = 0; i < bounds.length - 1; i++) {
        spans.push({ from: bounds[i], to: bounds[i + 1] });
      }
      // Degenerate cut (two stops on the same vertex): fall back to one line
      // rather than drawing empty segments.
      route._legSpans = spans.every((s) => s.to > s.from) ? spans : null;
      return route._legSpans;
    }

    _routeBadge(route) {
      const coords = route.geometry.map((p) => [p.lon, p.lat]);
      route._badgeMarker = this._badgeAt(
        route.mode, coords[Math.floor(coords.length / 2)],
        route.duration_min, route.distance_km, null);
    }

    /** One badge per leg, at that leg's own midpoint vertex, showing the
     *  leg's duration and distance; a colour keel ties it to its line. */
    _legBadges(route) {
      const spans = this._legSpans(route);
      if (!spans) return;
      route._legBadgeMarkers = spans.map((span, index) => {
        const leg = route.legs[index];
        const mid = route.geometry[Math.floor((span.from + span.to) / 2)];
        return this._badgeAt(route.mode, [mid.lon, mid.lat],
          leg.duration_min, leg.distance_km, this.legColor(route, index));
      });
    }

    _badgeAt(mode, mid, durationMin, distanceKm, accent, options) {
      const badge = document.createElement('div');
      badge.className = 'am-map-route-badge'
        // Google's convention: the chosen route's badge is dark, every
        // not-yet-chosen alternative's badge is light — the map itself says
        // which one you are on before you commit.
        + ((options && options.light) ? ' am-map-route-badge-light' : '');
      // Drawn pictograms, not emoji: emoji render as toy-coloured glyphs that
      // differ per platform, while a stroke icon matches the badge's own type.
      const stroke = 'fill="none" stroke="currentColor" stroke-width="1.8" '
        + 'stroke-linecap="round" stroke-linejoin="round"';
      const ICONS = {
        car: '<svg viewBox="0 0 24 24" ' + stroke + '><path d="M4 15l1.5-5.5A2 2 0 017.4 8h9.2a2 2 0 011.9 1.5L20 15"/><path d="M3 15h18v4h-2m-14 0H3z"/><circle cx="7.2" cy="17" r="1.4"/><circle cx="16.8" cy="17" r="1.4"/></svg>',
        truck: '<svg viewBox="0 0 24 24" ' + stroke + '><path d="M3 7h10v9H3z"/><path d="M13 11h4l3 3v2h-7z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/></svg>',
        walk: '<svg viewBox="0 0 24 24" ' + stroke + '><circle cx="13" cy="4.5" r="1.6"/><path d="M10 20l2-5-1.5-4L8 12l-1 3M12.5 11l2 2 2.8 1M11.5 15l1.5 2 1 4"/></svg>',
        bike: '<svg viewBox="0 0 24 24" ' + stroke + '><circle cx="6" cy="17" r="3"/><circle cx="18" cy="17" r="3"/><path d="M6 17l4-8h5l3 8M10 9h3M9 17h9"/></svg>',
        transit: '<svg viewBox="0 0 24 24" ' + stroke + '><rect x="6" y="3" width="12" height="13" rx="2.5"/><path d="M6 11h12M9.5 19l-1.5 2m8-2l1.5 2"/><circle cx="9.5" cy="13.5" r="0.9"/><circle cx="14.5" cy="13.5" r="0.9"/></svg>',
      };
      const icon = ICONS[mode] || ICONS.car;
      // The host page may install a formatter (index.html's distance-units
      // setting) — the badge then shows the same text as the panel.
      const distance = distanceKm >= 0.1
        ? ' · ' + (typeof this.formatDistance === 'function'
            ? this.formatDistance(distanceKm)
            : distanceKm.toFixed(distanceKm < 10 ? 1 : 0) + ' km')
        : '';
      // A brief place line ("über Pforzheim") under the numbers — the
      // alternate badges carry it so the map itself says WHERE each
      // candidate goes, not only how long it takes.
      const via = options && options.viaLabel
        ? '<span class="via">über ' + escapeHtml(options.viaLabel) + '</span>'
        : '';
      badge.innerHTML = '<i class="glyph">' + icon + '</i>'
        + '<span class="txt"><span>' + formatDuration(durationMin) + distance
        + '</span>' + via + '</span>';
      // A leg badge carries its line's colour as a keel; the single-route
      // badge stays exactly as it always was.
      if (accent) badge.style.borderLeft = '4px solid ' + accent;
      // Markers are kept on the route object (not just pushed to
      // this.markers) so promoteAlternate()/_removeRoute() can remove exactly
      // these badges without disturbing any other route's or highlight's
      // marker — the callers store the returned marker.
      return this._marker(badge, mid, 'center', null);
    }

    /** Draw every alternate beneath the primary line, Google Maps style:
     *  dimmed, thinner, no casing, clickable to become the primary route.
     *  Each alternate already arrived fully formed in the same /route
     *  response as `route` itself (routing/base.py), so there is nothing
     *  left to fetch — only geometry to draw and a click handler to wire. */
    _drawAlternates(route) {
      if (!route.alternates || !route.alternates.length) return;
      const primaryCasing = 'route-' + route.id + '-casing';
      const beforeId = this.map.getLayer(primaryCasing) ? primaryCasing
        : this.map.getStyle().layers.find((l) => l.type === 'symbol')?.id;
      const altIds = [];
      route.alternates.forEach((alt, index) => {
        const sourceId = 'route-' + route.id + '-alt-' + index;
        altIds.push(sourceId);
        // Each part is re-created only where it is missing: after a style
        // swap the sources/layers are gone but the badge markers (DOM) and
        // the delegated click handlers survive — the restore path calls
        // this again and must not double any of them.
        if (!this.map.getSource(sourceId)) {
          this.map.addSource(sourceId, {
            type: 'geojson',
            data: {
              type: 'Feature',
              geometry: { type: 'LineString', coordinates: alt.geometry.map((p) => [p.lon, p.lat]) },
            },
          });
          this.map.addLayer({
            id: sourceId, type: 'line', source: sourceId,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': '#9aa6b5', 'line-width': 4.5, 'line-opacity': 0.7 },
          }, beforeId);
        }
        // Google shows each alternative's time on the map BEFORE you commit:
        // a light badge at the alternate's midpoint, clickable like the line.
        route._altBadgeMarkers = route._altBadgeMarkers || [];
        if (!route._altBadgeMarkers[index]) {
          const altMid = alt.geometry[Math.floor(alt.geometry.length / 2)];
          const badge = this._badgeAt(alt.mode, [altMid.lon, altMid.lat],
            alt.duration_min, alt.distance_km, null, {
              light: true,
              viaLabel: viaPlaceLabel(alt,
                [route, ...route.alternates.filter((r) => r !== alt)]),
            });
          badge.getElement().style.cursor = 'pointer';
          badge.getElement().addEventListener('click', (event) => {
            event.stopPropagation();
            const live = this.routes.find((r) => r.id === route.id);
            if (live) this.promoteAlternate(live, index);
          });
          route._altBadgeMarkers[index] = badge;
        }
        // Bound ONCE per layer id, and resolved at CLICK time. Promoting swaps
        // the route objects but keeps the layer ids, and MapLibre's delegated
        // listeners survive layer removal — so a closure over the object that
        // stood when the layer was first drawn stacks up a stale handler per
        // promote. The stale one fired first, removed every route layer, and
        // its addRoute() bailed on the duplicate id: the second click on an
        // alternate blanked the whole route.
        this._altHandlersBound = this._altHandlersBound || new Set();
        if (!this._altHandlersBound.has(sourceId)) {
          this._altHandlersBound.add(sourceId);
          this.map.on('click', sourceId, () => {
            const live = this.routes.find((r) => r.id === route.id);
            if (live) this.promoteAlternate(live, index);
          });
          this.map.on('mouseenter', sourceId, () => { this.map.getCanvas().style.cursor = 'pointer'; });
          this.map.on('mouseleave', sourceId, () => { this.map.getCanvas().style.cursor = ''; });
        }
      });
      route._altLayerIds = altIds;
    }

    /** Swap alternate `index` into the primary slot and redraw. The former
     *  primary becomes one of the promoted route's own alternates (with its
     *  `alternates` cleared, so the list never nests), so clicking a second
     *  time can swap back. */
    promoteAlternate(route, index) {
      // Operate on the LIVE route with this id, whatever object the caller
      // holds: after one promotion the caller's reference is the demoted
      // copy, and removing/re-adding based on that stale object deletes the
      // shared layer ids and then refuses to redraw them (duplicate id).
      const live = this.routes.find((r) => r.id === route.id) || route;
      const alt = live.alternates && live.alternates[index];
      if (!alt) return;
      const rest = live.alternates.filter((_, i) => i !== index);
      // The copies must NOT inherit the on-map artifacts (badge markers,
      // alt layer ids): _removeRoute(live) below nulls them on `live` only,
      // and a shallow copy taken here would keep references to the removed
      // markers. Two promotes later that copy is the primary again, and
      // _drawAlternates' "already drawn" guard (!_altBadgeMarkers[index])
      // would mistake the dead marker for a live badge and never recreate
      // the clickable selector — the owner's vanished-selectors round-trip.
      const shed = {
        _badgeMarker: null, _legBadgeMarkers: null,
        _altBadgeMarkers: null, _altLayerIds: null,
      };
      const demoted = Object.assign({}, live, shed, { alternates: [] });
      const promoted = Object.assign({}, alt, shed, {
        id: live.id, color: live.color, animate_ms: null,
        alternates: [demoted, ...rest],
      });
      this._removeRoute(live);
      const at = this.routes.indexOf(live);
      if (at >= 0) this.routes.splice(at, 1);
      this.addRoute(promoted);
      // Both entry points — the chips in the panel and a click on the drawn
      // alternate line — end here, so this event is the one place the host
      // page learns that the primary route changed.
      this.el.dispatchEvent(new CustomEvent('agentic-map:route-promoted', {
        bubbles: true, detail: { route: promoted },
      }));
      // The caller (index.html's alternates chips) needs this to re-render
      // the duration/steps panel for what is now the primary route.
      return promoted;
    }

    /** Remove one route's own map layers/sources/badge — not any other
     *  route's or decoration's marker. Shared by clearRoutes() (which then
     *  also wipes every remaining marker wholesale) and promoteAlternate()
     *  (which must leave everything else on screen untouched). */
    _removeRoute(route) {
      const sourceId = 'route-' + route.id;
      for (const id of [sourceId + '-casing', sourceId,
                        sourceId + '-link-a', sourceId + '-link-b',
                        ...(route._legLayerIds || []),
                        ...(route._altLayerIds || [])]) {
        if (this.map.getLayer(id)) this.map.removeLayer(id);
        if (this.map.getSource(id) && id !== sourceId + '-casing') this.map.removeSource(id);
      }
      for (const marker of [route._badgeMarker, ...(route._legBadgeMarkers || []),
                            ...(route._altBadgeMarkers || [])]) {
        if (!marker) continue;
        marker.remove();
        const at = this.markers.indexOf(marker);
        if (at >= 0) this.markers.splice(at, 1);
      }
      route._badgeMarker = null;
      route._legBadgeMarkers = null;
      route._altBadgeMarkers = null;
    }

    _drawRoute(route, options) {
      const sourceId = 'route-' + route.id;
      if (this.map.getSource(sourceId)) return;
      const coords = route.geometry.map((p) => [p.lon, p.lat]);
      const animate = options && options.animate;
      const line = (slice) => ({
        type: 'Feature', geometry: { type: 'LineString', coordinates: slice },
      });
      // Off-road remainders, the way Google draws them: routing snaps to the
      // street, but the place someone asked for is usually inside a plot. The
      // gap between the snapped end and the actual stop gets a dotted link,
      // so the route visibly ends ON the destination dot rather than at the
      // kerb a block away.
      const gapM = (a, b) => {
        const mean = (a[1] + b[1]) / 2 * Math.PI / 180;
        const dx = (b[0] - a[0]) * Math.cos(mean), dy = b[1] - a[1];
        return Math.hypot(dx, dy) * 111320;
      };
      const links = [];
      if (route.stops && route.stops.length >= 2) {
        const first = [route.stops[0].lon, route.stops[0].lat];
        const last = [route.stops[route.stops.length - 1].lon,
                      route.stops[route.stops.length - 1].lat];
        if (gapM(first, coords[0]) > 15) links.push({ suffix: '-link-a', at: 'start',
          seg: [first, coords[0]] });
        if (gapM(coords[coords.length - 1], last) > 15) links.push({ suffix: '-link-b',
          at: 'end', seg: [coords[coords.length - 1], last] });
      }
      const drawLink = (link) => {
        const id = sourceId + link.suffix;
        if (this.map.getSource(id)) return;
        this.map.addSource(id, { type: 'geojson', data: line(link.seg) });
        this.map.addLayer({
          id, type: 'line', source: id,
          layout: { 'line-cap': 'round' },
          paint: { 'line-color': route.color, 'line-width': 3,
                   'line-opacity': 0.85, 'line-dasharray': [0.1, 1.8] },
        }, this.map.getStyle().layers.find((l) => l.type === 'symbol')?.id);
      };
      this.map.addSource(sourceId, { type: 'geojson', data: line(animate ? coords.slice(0, 2) : coords) });
      const firstSymbol = this.map.getStyle().layers.find((l) => l.type === 'symbol');
      const beforeId = firstSymbol ? firstSymbol.id : undefined;
      // One continuous casing for the whole route — the legs sit on top of
      // it, so the seams between leg colours never show a casing gap.
      // Widths follow Google's reading weight: 5px line in 8px casing.
      this.map.addLayer({
        id: sourceId + '-casing', type: 'line', source: sourceId,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#10151c', 'line-width': 8, 'line-opacity': 0.65 },
      }, beforeId);
      const spans = animate ? null : this._legSpans(route);
      if (spans) {
        // Multi-stop: each stop-to-stop leg is its own coloured line so the
        // sections read apart at a glance ("sub routes", Google-style).
        route._legLayerIds = route._legLayerIds || [];
        spans.forEach((span, index) => {
          const id = sourceId + '-leg' + index;
          if (this.map.getSource(id)) return;
          this.map.addSource(id, {
            type: 'geojson', data: line(coords.slice(span.from, span.to + 1)),
          });
          this.map.addLayer({
            id, type: 'line', source: id,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': this.legColor(route, index), 'line-width': 5 },
          }, beforeId);
          if (route._legLayerIds.indexOf(id) === -1) route._legLayerIds.push(id);
        });
      } else {
        this.map.addLayer({
          id: sourceId, type: 'line', source: sourceId,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': route.color, 'line-width': 5 },
        }, beforeId);
      }
      if (!animate) { links.forEach(drawLink); return; }
      // The start link exists from the first frame; the end link only once
      // the head has arrived — a destination stub ahead of the racing line
      // would spoil the build-up.
      links.filter((l) => l.at === 'start').forEach(drawLink);

      // Progressive draw: the route races along the streets, a head dot
      // leading; the duration badge appears when it arrives.
      const head = document.createElement('div');
      head.className = 'am-map-route-head';
      head.style.background = route.color;
      const headMarker = this._marker(head, coords[0], 'center', null);
      const source = this.map.getSource(sourceId);
      const duration = route.animate_ms || 2500;
      const startTime = performance.now();
      // A draw-in is not a Web Animation and does not live in the host
      // document, so nothing outside can see it. Counted here instead, or a
      // screenshot pipeline photographs a half-drawn route — badge and
      // destination marker included, since both are created on completion.
      this._busy++;
      const step = (now) => {
        const t = Math.min((now - startTime) / duration, 1);
        const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        const upTo = Math.max(2, Math.round(eased * coords.length));
        source.setData(line(coords.slice(0, upTo)));
        headMarker.setLngLat(coords[Math.min(upTo, coords.length) - 1]);
        if (t < 1) {
          requestAnimationFrame(step);
        } else {
          headMarker.remove();
          links.filter((l) => l.at === 'end').forEach(drawLink);
          this._routeBadge(route);
          this._busy--;
        }
      };
      requestAnimationFrame(step);
    }

    // -- steps ----------------------------------------------------------

    get length() {
      return this.spec.locations.length;
    }

    goTo(index, options) {
      const clamped = Math.max(-1, Math.min(index, this.length - 1));
      this.index = clamped;
      const pose = clamped === -1
        ? (this.spec.overview || this.spec.locations[0].camera)
        : this.spec.locations[clamped].camera;
      this.map.flyTo({
        center: [pose.center.lon, pose.center.lat],
        zoom: pose.zoom,
        bearing: pose.bearing || 0,
        pitch: pose.pitch || 0,
        duration: options && options.instant ? 0 : this.spec.fly_duration_ms,
        essential: true,
      });
      this.el.dispatchEvent(new CustomEvent('agentic-map:location', {
        bubbles: true,
        detail: { index: clamped, location: clamped === -1 ? null : this.spec.locations[clamped] },
      }));
    }

    next() { this.goTo(this.index >= this.length - 1 ? -1 : this.index + 1); }
    prev() { this.goTo(this.index <= -1 ? this.length - 1 : this.index - 1); }

    addLocation(loc) {
      this.spec.locations.push(loc);
      this._addDecorations(loc);
      this._rescale();
      this.el.dispatchEvent(new CustomEvent('agentic-map:spec-changed', { bubbles: true }));
    }

    /** Swap the whole scene — the map app uses this to load or drop a demo
     *  scenario without reloading the page. */
    /** Drop every radius/polygon area. Separate from clearRoutes because a
     *  scene player clears both, but a route recalculation must not eat the
     *  highlights standing next to it. */
    clearHighlights() {
      for (const g of this._geo.splice(0)) {
        for (const layer of g.layers) {
          if (this.map.getLayer(layer.id)) this.map.removeLayer(layer.id);
        }
        if (this.map.getSource(g.sourceId)) this.map.removeSource(g.sourceId);
      }
      this._overlaysChanged();
    }

    loadSpec(spec) {
      this.clearRoutes();
      this.clearHighlights();
      this.spec = Object.assign({ locations: [], routes: [], fly_duration_ms: 2600 }, spec);
      this.index = -1;
      for (const loc of this.spec.locations) this._addDecorations(loc);
      for (const route of this.spec.routes || []) this.addRoute(route);
      this._rescale();
      this.el.dispatchEvent(new CustomEvent('agentic-map:spec-changed', { bubbles: true }));
    }

    /** Add a single highlight (ping / circle / radius / polygon) ad hoc. */
    addHighlight(highlight) {
      const h = Object.assign({ color: '#f5d76e', opacity: 0.18, kind: 'circle', radius_px: 90 }, highlight);
      if (h.kind === 'radius' || h.kind === 'polygon' || h.kind === 'line') return this._addGeoHighlight(h);
      if (h.kind === 'ping') return this.pingAt([h.at.lon, h.at.lat], h.label, {});
      return this._addDecorations({ id: 'ad-hoc', highlights: [h] });
    }

    /** Highlight the basemap's own label for a place instead of drawing a
     *  second one on top of it. The layer forces overlap so the name is never
     *  dropped by collision, which is what a search result must guarantee —
     *  and the ORIGINAL label is filtered out of the stock place layers while
     *  the highlight stands, so the name is rendered exactly once, not once
     *  per layer (highlight + stock label was a visible duplicate).
     *  Returns true when the basemap actually carries that name. */
    highlightPlace(name) {
      if (!this.usePro || !name) return false;

      const key = name.trim().toLowerCase();
      const layerId = 'am-place-highlight';
      const matches = ['any',
        ['==', ['downcase', ['coalesce', ['get', 'name'], '']], key],
        ['==', ['downcase', ['coalesce', ['get', 'name:' + this.lang], '']], key],
      ];
      try {
        if (this.map.getLayer(layerId)) this.map.removeLayer(layerId);
      } catch (error) { /* style mid-swap */ }
      // A previous highlight may still hold exclusion filters on the stock
      // layers (searching a second city without clearing) — put those back
      // before excluding the new name.
      this._restorePlaceFilters();
      try {
        this.map.addLayer({
        id: layerId, type: 'symbol', source: 'streets', 'source-layer': 'places',
        filter: matches,
        layout: {
          'text-field': ['coalesce', ['get', 'name:' + this.lang], ['get', 'name']],
          'text-font': ['Noto Sans Medium'],
          'text-size': 17,
          'text-allow-overlap': true,     // a search hit must always be drawn
          'text-ignore-placement': true,
          'text-anchor': 'top',
          'text-offset': [0, 0.9],
        },
        paint: {
          'text-color': '#ffffff',
          'text-halo-color': this.payload.brand_colors?.highlight || '#e2574c',
          'text-halo-width': 2.2,
          'text-halo-blur': 0.2,
          },
        });
      } catch (error) {
        // A style swap is in flight (view or language change); try again once
        // it settles rather than losing the highlight.
        this.map.once('idle', () => this.highlightPlace(name));
        return false;
      }
      // Suppress the stock rendering of this one name: every other symbol
      // layer reading the 'places' source-layer gets a "not this name" filter
      // stacked onto whatever filter it already had, remembered for restore.
      // The stock filters are LEGACY syntax (["==","kind","locality"]) and a
      // legacy/expression mix is rejected wholesale by setFilter, so the
      // original is converted to expression form before stacking.
      this._placeFilterBackup = {};
      for (const layer of this.map.getStyle().layers) {
        if (layer.type !== 'symbol' || layer.id === layerId) continue;
        if (layer.source !== 'streets' || layer['source-layer'] !== 'places') continue;
        const original = this.map.getFilter(layer.id) || null;
        try {
          this.map.setFilter(layer.id, original
            ? ['all', this._filterAsExpression(original), ['!', matches]]
            : ['!', matches]);
          this._placeFilterBackup[layer.id] = original;
        } catch (error) {
          // Unconvertible filter: better one duplicated label than a layer
          // whose filter is silently broken.
          console.error('[am-map] place-label exclusion failed:', layer.id, error);
        }
      }
      this._highlightedPlace = key;
      return this.map.querySourceFeatures('streets', {
        sourceLayer: 'places', filter: matches,
      }).length > 0;
    }

    /** Legacy filter syntax -> expression syntax, far enough for the stock
     *  basemap place filters. An input that is already an expression (first
     *  operand not a plain property-name string) passes through untouched. */
    _filterAsExpression(f) {
      if (!Array.isArray(f)) return f;
      const [op, ...rest] = f;
      if (op === 'all' || op === 'any') return [op, ...rest.map((x) => this._filterAsExpression(x))];
      if (op === 'none') return ['!', ['any', ...rest.map((x) => this._filterAsExpression(x))]];
      if (op === '!') return ['!', this._filterAsExpression(rest[0])];
      const key = rest[0];
      if (typeof key !== 'string') return f;   // already expression syntax
      const ref = key === '$type' ? ['geometry-type'] : key === '$id' ? ['id'] : ['get', key];
      if (op === 'has') return key === '$id' ? ['!=', ['id'], null] : ['has', key];
      if (op === '!has') return ['!', key === '$id' ? ['!=', ['id'], null] : ['has', key]];
      // 'match', never expression-'in': MapLibre's legacy/expression
      // classifier treats ANY ['in', ...] array as a legacy filter, which
      // reclassifies the whole combined ['all', ...] as legacy and gets it
      // rejected (filter[2][0]: "!" found). 'match' is unambiguous.
      if (op === 'in') {
        return rest.length === 2 ? ['==', ref, rest[1]]
          : ['match', ref, rest.slice(1), true, false];
      }
      if (op === '!in') {
        return ['!', rest.length === 2 ? ['==', ref, rest[1]]
          : ['match', ref, rest.slice(1), true, false]];
      }
      if (['==', '!=', '<', '<=', '>', '>='].includes(op)) return [op, ref, rest[1]];
      return f;
    }

    _restorePlaceFilters() {
      for (const [id, filter] of Object.entries(this._placeFilterBackup || {})) {
        try {
          if (this.map.getLayer(id)) this.map.setFilter(id, filter);
        } catch (error) { /* layer gone with a style swap — nothing to restore */ }
      }
      this._placeFilterBackup = null;
    }

    clearPlaceHighlight() {
      this._highlightedPlace = null;
      this._restorePlaceFilters();
      if (this.map.getLayer('am-place-highlight')) this.map.removeLayer('am-place-highlight');
    }

    /** Drop every drawn route, including alternates (the map app recomputes
     *  instead of stacking). */
    clearRoutes() {
      for (const route of this.routes) this._removeRoute(route);
      this.routes = [];
      for (const marker of this.markers.splice(0)) marker.remove();
      this._scaled.length = 0;
      this._routesChanged();
    }

    /** Re-request street tiles — call after a region extract was minted, so
     *  the view fills in without a style rebuild. */
    reloadVectors() {
      const source = this.map.getSource('streets');
      if (!source || !source.setTiles) return false;
      source.setTiles([abs(this.payload.vector_url_template)]);
      return true;
    }

    /** Clamp the street source to the depth that actually exists here.
     *
     *  The vector federation is not one archive: the nationwide extract stops
     *  at z10, a minted city region reaches z15. MapLibre has a single
     *  `maxzoom` per source, so if it is left at the deepest bundle's value
     *  the map requests tiles that do not exist everywhere else, collects
     *  404s and paints a bare grey plane — instead of over-zooming the z10
     *  data it already holds. Tracking the per-view coverage keeps a coarse
     *  but real map on screen, which is also the only thing offline can do.
     */
    setVectorDepth(maxZoom) {
      const source = this.map.getSource('streets');
      if (!source || !maxZoom) return false;
      const ceiling = this.payload.vector_max_zoom || 15;
      const next = Math.max(1, Math.min(Math.round(maxZoom), ceiling));
      if (source.maxzoom === next) return false;
      const deeper = next > source.maxzoom;
      source.maxzoom = next;
      // Growing the ceiling needs the errored tiles dropped so the new ones
      // are fetched; shrinking only needs the tile plan recomputed, and
      // re-fetching would throw away renderable tiles for nothing.
      if (deeper && source.setTiles) source.setTiles([abs(this.payload.vector_url_template)]);
      else this.map.jumpTo({ center: this.map.getCenter() });
      return true;
    }
  }

  async function loadPayload(el) {
    const inline = el.querySelector('script[type="application/json"][data-agentic-inline-spec]');
    if (inline) return JSON.parse(inline.textContent);
    const response = await fetch(el.getAttribute('data-agentic-spec-url'));
    if (!response.ok) throw new Error('map spec fetch failed: ' + response.status);
    return await response.json();
  }

  async function mountMaps(root) {
    const els = (root || document).querySelectorAll('[data-agentic-map]:not([data-agentic-map-mounted])');
    for (const el of els) {
      el.setAttribute('data-agentic-map-mounted', '');
      try {
        const instance = new AgenticMap(el, await loadPayload(el));
        registry.push(instance);
        if (el.hasAttribute('data-agentic-standalone')) {
          window.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT') return;
            if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); instance.next(); }
            if (e.key === 'ArrowLeft') { e.preventDefault(); instance.prev(); }
          });
        }
        el.dispatchEvent(new CustomEvent('agentic-map:ready', { bubbles: true, detail: { map: instance } }));
      } catch (error) {
        console.error('[am-map] mount failed', error);
      }
    }
    return registry;
  }

  /** Is this map DONE — style up, tiles in, camera still, nothing animating?
   *
   *  Screenshot and export pipelines need an answer to that, and every fixed
   *  waiting time is a race they eventually lose: a route draws for 2.2 s and
   *  lands its badge and destination marker only at the end, so a shutter that
   *  fires at 2.0 s photographs a line stopping in mid-air.
   *
   *  `isStyleLoaded()` alone is not the answer either — it goes true long
   *  before the tiles under it have decoded.
   */
  AgenticMap.prototype.settled = function () {
    try {
      const m = this.map;
      if (!m.isStyleLoaded()) return false;
      if (!m.areTilesLoaded()) return false;
      if (m.isMoving() || m.isZooming() || m.isRotating() || m.isEasing()) return false;
      return this._busy === 0;
    } catch (error) { return false; }
  };

  /** Every mounted map on this page is settled. Pages with extra ordered work
   *  (an image overlay that must be in the style) wrap this — see view.html. */
  window.agenticMapsSettled = function () {
    return registry.length > 0 && registry.every((instance) => instance.settled());
  };
  // The contract the host pipelines probe. Defined for EVERY map page, not
  // just the ones that remembered to: a page without it silently degraded the
  // probe to "the style exists", which is how a half-drawn route reached a
  // published page's screenshots.
  window.__agenticMapsReady = window.agenticMapsSettled;

  window.agenticMountMaps = mountMaps;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => mountMaps(document));
  } else {
    mountMaps(document);
  }
})();

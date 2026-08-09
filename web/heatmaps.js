/*
 * Data overlays for the canvas basemap.
 *
 * Four kinds, because they answer four different questions and Esri's own
 * gallery separates them for good reason:
 *
 *   radial     "how far, as the crow flies" — a distance falloff around one
 *              object. Honest and instant, but it ignores the street network,
 *              so it over-promises across rivers and motorways.
 *   isodistance "how far along the streets" — the same question answered on
 *              the road graph, via the routing matrix. Slower, and correct.
 *   cluster    "where is there a lot of this" — kernel density over many
 *              points, the classic heat map. Overlapping sources add up.
 *   highlight  "this area, not that one" — a flat categorical wash, no ramp.
 *
 * All four write into the same three-stop ramp so they can be read against
 * each other, and all sit above the basemap fills but below its labels: the
 * place names have to survive the overlay or the map stops being a map.
 */
(function () {
  'use strict';

  // Sequential ramp, low → high. Colour-blind safe (viridis-like: it varies in
  // lightness as well as hue, so it survives greyscale printing and the most
  // common deficiencies). Deliberately not red-green.
  // OSRM's table service caps its coordinate count; the public demo server
  // rejects more than 100 with a bare 400.
  const MATRIX_POINT_LIMIT = 95;

  const RAMP = ['#2c3d8f', '#3a7fb5', '#4bb6a8', '#c3d96b', '#f5e05c'];

  // "Volcanic" — the inferno family: black through plum and ember to a pale
  // yellow. Chosen because it is perceptually uniform, which is the whole
  // point of a continuous false-colour surface: equal steps in the data have
  // to look like equal steps, or the picture lies about where the gradient is
  // steep. It also rises monotonically in lightness, so it survives greyscale.
  const VOLCANIC = [
    [0, 4, 22], [40, 11, 84], [101, 21, 110], [159, 42, 99],
    [212, 72, 66], [243, 120, 25], [252, 187, 45], [252, 255, 164],
  ];

  /** Sample a ramp continuously. `t` in 0..1, linear between control points —
   *  this is what turns four colours into thousands of gradations. */
  function rampAt(stops, t) {
    const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(x));
    const f = x - i;
    const a = stops[i], b = stops[i + 1];
    return [
      Math.round(a[0] + (b[0] - a[0]) * f),
      Math.round(a[1] + (b[1] - a[1]) * f),
      Math.round(a[2] + (b[2] - a[2]) * f),
    ];
  }
  // Where overlays go in the draw order: above every fill, below every label.
  const BEFORE_LABELS = (map) => {
    const layers = map.getStyle().layers || [];
    const firstSymbol = layers.find((l) => l.type === 'symbol');
    return firstSymbol ? firstSymbol.id : undefined;
  };

  const rampExpression = (property, stops) => {
    const expression = ['interpolate', ['linear'], ['get', property]];
    stops.forEach((value, index) => {
      expression.push(value, RAMP[Math.min(index, RAMP.length - 1)]);
    });
    return expression;
  };

  /** Metres per pixel, so a radius given in metres draws at the right size at
   *  any zoom. MapLibre's circle radius is in screen pixels, which is the one
   *  awkward fact about using circles for ground distances. */
  function metresPerPixel(map, latitude) {
    return 156543.03392 * Math.cos(latitude * Math.PI / 180) / Math.pow(2, map.getZoom());
  }

  /**
   * Straight-line distance rings around one point.
   *
   * Drawn as concentric filled rings rather than a blurred circle: a blur
   * looks softer but cannot be read off, and the whole point of a distance
   * overlay is that someone can say "so the station is inside 800 m".
   */
  function radial(map, options) {
    const { id, centre, radii, labels } = options;
    const rings = radii.slice().sort((a, b) => b - a);   // largest first
    const features = rings.map((metres, index) => ({
      type: 'Feature',
      properties: { metres, band: rings.length - 1 - index },
      geometry: { type: 'Polygon', coordinates: [_circle(centre, metres)] },
    }));
    map.addSource(id, { type: 'geojson', data: { type: 'FeatureCollection', features } });
    map.addLayer({
      id: id + '-fill', type: 'fill', source: id,
      paint: {
        'fill-color': rampExpression('band', rings.map((_, i) => i)),
        // Each ring is drawn whole and overlaps the next, so a low opacity is
        // what turns them into a gradient instead of five flat discs.
        'fill-opacity': 0.28,
      },
    }, BEFORE_LABELS(map));
    map.addLayer({
      id: id + '-line', type: 'line', source: id,
      paint: { 'line-color': '#ffffff', 'line-width': 1, 'line-opacity': 0.65 },
    }, BEFORE_LABELS(map));
    if (labels !== false) _ringLabels(map, id, centre, rings);
    return [id + '-fill', id + '-line', id + '-labels'];
  }

  function _ringLabels(map, id, centre, rings) {
    const features = rings.map((metres) => ({
      type: 'Feature',
      properties: { label: metres >= 1000 ? (metres / 1000) + ' km' : metres + ' m' },
      // North of the centre, on the ring itself.
      geometry: { type: 'Point', coordinates: _offset(centre, 0, metres) },
    }));
    map.addSource(id + '-labels-src', {
      type: 'geojson', data: { type: 'FeatureCollection', features },
    });
    map.addLayer({
      id: id + '-labels', type: 'symbol', source: id + '-labels-src',
      layout: { 'text-field': ['get', 'label'], 'text-size': 11,
                'text-font': ['Noto Sans Medium'], 'text-offset': [0, -0.4] },
      paint: { 'text-color': '#3a4049', 'text-halo-color': '#ffffff', 'text-halo-width': 1.6 },
    });
  }

  /**
   * Travel time along the street network, sampled and interpolated.
   *
   * This is the one that costs something: the road graph only answers through
   * the routing backend, so a grid of probe points is sent to the matrix
   * service and the surface is interpolated between the answers. Coarse by
   * construction — it is a picture of reachability, not a routing result —
   * and it says so, because a smooth surface implies a precision it does not
   * have.
   */
  async function isodistance(map, options) {
    const { id, centre, radiusM = 3000, samples = 8, mode = 'car', minutes } = options;
    // The matrix service has a coordinate cap — the public OSRM demo rejects
    // anything over 100 outright. Rather than fail, thin the probe grid until
    // it fits and report the resolution actually used, so a coarse surface is
    // visibly coarse instead of silently absent.
    let rings = samples;
    let points = _grid(centre, radiusM, rings);
    while (points.length + 1 > MATRIX_POINT_LIMIT && rings > 2) {
      rings -= 1;
      points = _grid(centre, radiusM, rings);
    }
    const response = await fetch('/api/v1/maps/matrix', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ points: [{ lat: centre[1], lon: centre[0] }, ...points], mode }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || 'Matrix-Dienst nicht erreichbar');
    }
    const durations = (await response.json()).durations_min[0].slice(1);

    const features = points.map((point, index) => ({
      type: 'Feature',
      properties: { minutes: durations[index] == null ? 999 : durations[index] },
      geometry: { type: 'Point', coordinates: [point.lon, point.lat] },
    })).filter((f) => f.properties.minutes < 900);   // unreachable probes drop out

    map.addSource(id, { type: 'geojson', data: { type: 'FeatureCollection', features } });
    const bands = minutes || [2, 5, 10, 15, 20];
    map.addLayer({
      id: id + '-heat', type: 'heatmap', source: id,
      paint: {
        // Weight is inverted: near = hot. Clamped so one very close probe does
        // not flatten the rest of the ramp.
        'heatmap-weight': ['interpolate', ['linear'], ['get', 'minutes'],
          0, 1, bands[bands.length - 1], 0],
        'heatmap-intensity': 1,
        'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
          0, 'rgba(0,0,0,0)', 0.2, RAMP[0], 0.4, RAMP[1], 0.6, RAMP[2],
          0.8, RAMP[3], 1, RAMP[4]],
        // Radius has to exceed half the probe spacing or the surface breaks up
        // into visible blobs — one per sample — which reads as data rather
        // than as the sampling artefact it is. Scaled with zoom so the ground
        // footprint stays roughly constant.
        'heatmap-radius': ['interpolate', ['exponential', 2], ['zoom'],
          9, 26, 12, 78, 15, 260],
        'heatmap-opacity': 0.6,
      },
    }, BEFORE_LABELS(map));
    // The caller may want to say how coarse this is.
    return Object.assign([id + '-heat'], { rings, probes: points.length });
  }

  /**
   * Kernel density over many points — the classic heat map, and the only kind
   * where overlapping sources are meant to add up rather than occlude.
   */
  function cluster(map, options) {
    const { id, points, weightProperty = 'weight', radiusPx = 40, opacity = 0.7 } = options;
    map.addSource(id, {
      type: 'geojson',
      data: {
        type: 'FeatureCollection',
        features: points.map((p) => ({
          type: 'Feature',
          properties: { [weightProperty]: p.weight == null ? 1 : p.weight },
          geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
        })),
      },
    });
    map.addLayer({
      id: id + '-heat', type: 'heatmap', source: id,
      paint: {
        'heatmap-weight': ['coalesce', ['get', weightProperty], 1],
        'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
          0, 'rgba(0,0,0,0)', 0.15, RAMP[0], 0.35, RAMP[1], 0.6, RAMP[2],
          0.82, RAMP[3], 1, RAMP[4]],
        'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 8, radiusPx * 0.5, 16, radiusPx],
        'heatmap-opacity': opacity,
      },
    }, BEFORE_LABELS(map));
    return [id + '-heat'];
  }

  /**
   * Flat categorical wash — "these districts, in these colours". No ramp,
   * because the values are not ordered.
   */
  function highlight(map, options) {
    const { id, features, colorProperty = 'color', opacity = 0.35 } = options;
    map.addSource(id, { type: 'geojson', data: { type: 'FeatureCollection', features } });
    map.addLayer({
      id: id + '-fill', type: 'fill', source: id,
      paint: {
        'fill-color': ['coalesce', ['get', colorProperty], RAMP[1]],
        'fill-opacity': opacity,
      },
    }, BEFORE_LABELS(map));
    map.addLayer({
      id: id + '-outline', type: 'line', source: id,
      paint: {
        'line-color': ['coalesce', ['get', colorProperty], RAMP[1]],
        'line-width': 1.5, 'line-opacity': 0.9,
      },
    }, BEFORE_LABELS(map));
    return [id + '-fill', id + '-outline'];
  }

  // -- geometry helpers -------------------------------------------------

  const EARTH_M = 6378137;

  /** Circle as a polygon ring. 72 points: at a 3 km radius that is a 4 m
   *  chord error, well under a pixel at any zoom this is used at. */
  function _circle(centre, metres, steps = 72) {
    const ring = [];
    for (let i = 0; i <= steps; i++) {
      ring.push(_offset(centre, (i / steps) * 360, metres));
    }
    return ring;
  }

  /** Move from a lon/lat by `metres` along `bearing` degrees. */
  function _offset(centre, bearing, metres) {
    const radians = bearing * Math.PI / 180;
    const dNorth = Math.cos(radians) * metres;
    const dEast = Math.sin(radians) * metres;
    const lat = centre[1] + (dNorth / EARTH_M) * (180 / Math.PI);
    const lon = centre[0]
      + (dEast / (EARTH_M * Math.cos(centre[1] * Math.PI / 180))) * (180 / Math.PI);
    return [lon, lat];
  }

  /** Probe points on a disc. A square grid would waste a third of the calls on
   *  corners outside the radius, and matrix calls are the expensive part. */
  function _grid(centre, radiusM, rings) {
    const points = [];
    for (let ring = 1; ring <= rings; ring++) {
      const metres = (ring / rings) * radiusM;
      const count = Math.max(6, ring * 6);
      for (let step = 0; step < count; step++) {
        const [lon, lat] = _offset(centre, (step / count) * 360, metres);
        points.push({ lat, lon });
      }
    }
    return points;
  }

  function removeAll(map, ids) {
    for (const layerId of ids || []) {
      if (map.getLayer(layerId)) map.removeLayer(layerId);
    }
    for (const sourceId of (ids || []).map((i) => i.replace(/-(fill|line|heat|outline|labels)$/, ''))) {
      for (const candidate of [sourceId, sourceId + '-labels-src']) {
        if (map.getSource(candidate) && !_sourceInUse(map, candidate)) map.removeSource(candidate);
      }
    }
  }

  function _sourceInUse(map, sourceId) {
    return (map.getStyle().layers || []).some((l) => l.source === sourceId);
  }

  window.agenticHeat = { radial, isodistance, cluster, highlight, removeAll, RAMP };
})();

/*
 * "Canvas" — a deliberately mute basemap to project data onto.
 *
 * This is the same idea as Esri's Light Gray Canvas and Human Geography
 * basemaps, and it exists because a normal map is already using colour. Green
 * parks, blue water and yellow trunk roads all compete with an overlay, and a
 * heat ramp laid on top of them reads as a third unrelated colour. The fix is
 * not to dim the map — dimming keeps the hues and just makes them muddy — but
 * to remove chroma from it entirely.
 *
 * So: every fill, road and boundary becomes a neutral grey chosen only for its
 * *lightness*, which is what carries the geography. The single exception is
 * place names, which stay dark and legible — an unlabelled canvas tells you
 * nothing about where the data is.
 *
 * The greys are spaced to leave the mid-range free: land sits high (0.90–0.95)
 * and water low-ish (0.86), so a sequential ramp through the middle stays
 * distinguishable from both. Buildings are barely there on purpose; at the
 * zooms where analysis happens they would otherwise read as noise.
 */
(function () {
  'use strict';

  // Lightness ladder. Named by role rather than by value so a change stays
  // one edit, and so the intent survives when someone retunes it.
  const PAPER = '#f2f1ef';        // land
  const PAPER_DIM = '#e9e8e5';    // built-up / non-park landuse
  const GREEN_NEUTRAL = '#e6e6e2'; // parks and woods, chroma removed
  const WATER = '#dcdedf';        // slightly cooler and darker than land
  const ROAD_MINOR = '#fbfbfa';
  const ROAD_MAJOR = '#ffffff';
  const CASING = '#dedddb';
  const BUILDING = '#e6e5e2';
  const BOUNDARY = '#c3c2bf';

  // Names keep their contrast — the one thing that must not go grey.
  const INK = '#3a4049';
  const INK_SOFT = '#6a7280';
  const HALO = 'rgba(255,255,255,0.92)';

  /** Build the canvas flavour from the stock light one.
   *
   *  Derived rather than written out: the upstream palette has 74 keys and
   *  gains more over time, so anything not named here still gets a sane
   *  neutral instead of silently keeping its colour.
   */
  function canvasFlavor() {
    const base = Object.assign({}, basemaps.namedFlavor('light'));
    for (const key of Object.keys(base)) {
      const value = base[key];
      if (typeof value !== 'string' || value[0] !== '#') continue;
      if (/label/.test(key)) continue;             // handled explicitly below
      base[key] = _neutralise(value);
    }

    Object.assign(base, {
      background: PAPER,
      earth: PAPER,
      park_a: GREEN_NEUTRAL, park_b: GREEN_NEUTRAL,
      wood_a: GREEN_NEUTRAL, wood_b: GREEN_NEUTRAL,
      scrub_a: GREEN_NEUTRAL, scrub_b: GREEN_NEUTRAL,
      grass: GREEN_NEUTRAL,
      water: WATER, glacier: '#ececea', sand: PAPER_DIM, beach: PAPER_DIM,
      hospital: PAPER_DIM, industrial: PAPER_DIM, school: PAPER_DIM,
      pedestrian: PAPER_DIM, aerodrome: PAPER_DIM, zoo: PAPER_DIM,
      military: PAPER_DIM, runway: ROAD_MINOR, pier: PAPER_DIM,
      buildings: BUILDING,
      other: ROAD_MINOR, minor_service: ROAD_MINOR, minor_a: ROAD_MINOR,
      minor_b: ROAD_MAJOR, link: ROAD_MAJOR, major: ROAD_MAJOR, highway: ROAD_MAJOR,
      minor_casing: CASING, minor_service_casing: CASING, link_casing: CASING,
      major_casing_early: CASING, major_casing_late: CASING,
      highway_casing_early: CASING, highway_casing_late: CASING,
      boundaries: BOUNDARY, boundaries_country: '#b4b3b0',

      // The exception. Place names stay dark; road and address labels drop
      // back so they inform without competing with the overlay.
      city_label: INK, city_label_halo: HALO,
      state_label: INK_SOFT, state_label_halo: HALO,
      country_label: INK_SOFT,
      subplace_label: INK_SOFT, subplace_label_halo: HALO,
      ocean_label: '#9aa3ad',
      roads_label_major: '#9099a3', roads_label_major_halo: HALO,
      roads_label_minor: '#a3aab3', roads_label_minor_halo: HALO,
      address_label: '#adb3ba', address_label_halo: HALO,
    });
    // Marker the map runtime looks for: on a canvas the signage has to go
    // neutral too. A blue Autobahn shield and a yellow Bundesstraße plate are
    // correct on a road map and wrong here — they are saturated marks that
    // compete with the data for exactly the same attention.
    base.__canvas = true;
    return base;
  }

  /** Strip chroma, keep lightness.
   *
   *  Rec. 709 luma rather than a naive average: the eye weights green far
   *  above blue, and averaging turns a park and a lake into the same grey
   *  when they should stay a step apart.
   */
  function _neutralise(hex) {
    const value = hex.length === 4
      ? hex[1] + hex[1] + hex[2] + hex[2] + hex[3] + hex[3]
      : hex.slice(1);
    const r = parseInt(value.slice(0, 2), 16);
    const g = parseInt(value.slice(2, 4), 16);
    const b = parseInt(value.slice(4, 6), 16);
    // Pulled towards paper so the whole map sits in the light end and leaves
    // the mid-tones to the data.
    const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const lifted = Math.round(luma * 0.35 + 235 * 0.65);
    const channel = Math.max(0, Math.min(255, lifted)).toString(16).padStart(2, '0');
    return '#' + channel + channel + channel;
  }

  window.agenticCanvasFlavor = canvasFlavor;
})();

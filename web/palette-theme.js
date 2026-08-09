/*
 * Theme flavour — derive a complete basemap palette from four brand tokens.
 *
 * A host product has a designed palette; a stock road map has its own (green
 * parks, cyan water, yellow trunk roads). Embedding one inside the other
 * reads as two unrelated designs on one page. This module makes the map
 * adopt the host's palette from just four tokens:
 *
 *     agenticThemeFlavor({ paper, ink, water, accent })
 *
 *   paper  — the map's ground tone (usually near the theme's surface colour)
 *   ink    — label/contrast colour against paper
 *   water  — water tone (defaults to a desaturated steel mixed from the two)
 *   accent — reserved for future emphasis roles; carried but sparsely used
 *
 * Everything else is *derived*: geometry roles become measured mixes of
 * paper→ink (the lightness ladder carries the geography, exactly the
 * palette-canvas.js idea), the stock flavour's remaining keys are neutralised
 * toward paper so new upstream keys can never leak a foreign colour, and
 * saturated road shields are dropped (`__canvas` marker) — a blue Autobahn
 * plate is signage, not brand.
 *
 * The host convention (documented in features.md): a page that embeds maps
 * sets the CSS variables --am-map-paper/-ink/-water/-accent; the embed
 * bridge forwards them as the `th` URL parameter. Adapting maps to a new
 * theme is therefore: define four CSS variables, done.
 */
(function () {
  'use strict';

  function parse(hex) {
    var v = hex.replace('#', '').trim();
    if (v.length === 3) v = v[0] + v[0] + v[1] + v[1] + v[2] + v[2];
    return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16),
      parseInt(v.slice(4, 6), 16)];
  }
  function hex(rgb) {
    return '#' + rgb.map(function (c) {
      return Math.max(0, Math.min(255, Math.round(c)))
        .toString(16).padStart(2, '0');
    }).join('');
  }
  function mix(a, b, t) {
    var A = parse(a), B = parse(b);
    return hex([A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t,
      A[2] + (B[2] - A[2]) * t]);
  }
  function luma(rgb) {
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
  }

  function themeFlavor(tokens) {
    var paper = tokens.paper || '#f2f0eb';
    var ink = tokens.ink || '#2c3644';
    // Default water: one step of the same ladder — reads as water through
    // shape and labels, not through saturation.
    var water = tokens.water || mix(paper, ink, 0.18);
    var dark = luma(parse(paper)) < 0.5;
    // On a dark paper the ladder must step *up* in lightness for roads —
    // mixing toward ink does exactly that in both directions, since ink is
    // by definition the contrast colour.
    var step = function (t) { return mix(paper, ink, t); };

    var base = Object.assign({}, basemaps.namedFlavor(dark ? 'dark' : 'light'));
    for (var key in base) {
      var value = base[key];
      if (typeof value !== 'string' || value[0] !== '#') continue;
      if (/label/.test(key)) continue;           // handled explicitly below
      base[key] = step(0.06);                    // safe neutral for the rest
    }

    Object.assign(base, {
      background: paper, earth: paper,
      water: water, glacier: step(0.03),
      park_a: step(0.05), park_b: step(0.05),
      wood_a: step(0.06), wood_b: step(0.06),
      scrub_a: step(0.05), grass: step(0.04), sand: step(0.03),
      beach: step(0.03), hospital: step(0.04), industrial: step(0.04),
      school: step(0.04), pedestrian: step(0.04), aerodrome: step(0.05),
      zoo: step(0.04), military: step(0.05), runway: step(0.12),
      pier: step(0.05), buildings: step(0.08),
      other: step(0.1), minor_service: step(0.1), minor_a: step(0.1),
      minor_b: step(0.14), link: step(0.14), major: step(0.16),
      highway: step(0.2),
      minor_casing: step(0.22), minor_service_casing: step(0.22),
      link_casing: step(0.24), major_casing_early: step(0.24),
      major_casing_late: step(0.24), highway_casing_early: step(0.26),
      highway_casing_late: step(0.26),
      boundaries: step(0.34), boundaries_country: step(0.42),

      city_label: ink, city_label_halo: paper,
      state_label: step(0.6), state_label_halo: paper,
      country_label: step(0.65),
      subplace_label: step(0.6), subplace_label_halo: paper,
      ocean_label: mix(water, ink, 0.45),
      roads_label_major: step(0.55), roads_label_major_halo: paper,
      roads_label_minor: step(0.45), roads_label_minor_halo: paper,
      address_label: step(0.35), address_label_halo: paper,
    });
    base.__canvas = true;     // saturated shields off — signage is not brand
    base.__accent = tokens.accent || null;
    return base;
  }

  /** Parse the compact `th` URL parameter: "paper:f2f0eb,ink:2c3644,…" */
  function parseTokens(raw) {
    if (!raw) return null;
    var tokens = {};
    raw.split(',').forEach(function (pair) {
      var kv = pair.split(':');
      if (kv.length === 2 && /^[0-9a-fA-F]{3,8}$/.test(kv[1])) {
        tokens[kv[0].trim()] = '#' + kv[1].trim();
      }
    });
    return (tokens.paper || tokens.ink) ? tokens : null;
  }

  window.agenticThemeFlavor = themeFlavor;
  window.agenticThemeTokens = parseTokens;
})();

/*
 * Three.js globe that takes over from the flat map when a whole continent
 * fits the screen.
 *
 * Same construction as the in-house globe viewer: a Blue Marble sphere, an atmosphere shell
 * rendered from the inside, and a starfield behind it. What is new here is the
 * hand-off — the globe is not a separate page, it is the same camera seen from
 * far enough away that mercator stops making sense:
 *
 *   zoom >= HANDOVER_TOP     flat map only (globe canvas hidden)
 *   HANDOVER_BOTTOM..TOP     cross-fade; both are drawn, opacities sum to 1
 *   zoom <= HANDOVER_BOTTOM  globe only, drag rotates it
 *
 * Continuity is what sells it: the globe's camera distance is derived from the
 * map's zoom and its rotation from the map's centre, so the point under the
 * cursor stays under the cursor through the whole transition. Dragging the
 * globe writes the centre back to the map, so zooming in afterwards lands
 * exactly where you left off.
 */
// three.js comes from the shared llming-stage vendor bundle, not from a copy
// in here: a second three.js is a second version to keep in step with the rest
// of the product. `window.agenticStageVendor` is set by the page from the payload's
// stage_asset_prefix, so the host's own mount wins over the dev default.
// A dynamic import, because a static one needs a literal specifier and the
// prefix is only known at runtime. Top-level await is fine here: globe.js is
// itself loaded lazily, and nothing may touch the globe before three is in.
// three.module.min.js pulls three.core.min.js relative to its own URL, which
// is why both have to come from the same mount.
const THREE = await import((window.agenticStageVendor || '/_stage/vendor') + '/three.module.min.js');

// One threshold, not a band: because the globe now reproduces the map's exact
// ground span, switching is invisible — whereas drawing both during a fade
// shows any residual disagreement as a ghost sphere inside the flat map.
const HANDOVER_ZOOM = 4.3;
const EARTH_RADIUS = 1;
// The sphere's silhouette is the one place tessellation is visible, and how
// visible depends on how many DEVICE pixels the globe covers — not on CSS
// pixels and not on a constant. A 96-gon deviates 0.75 px from a true circle
// on a 1400 device-px radius, and because the facets turn with the mesh that
// error crawls along the edge while dragging. So the segment count is solved
// for the screen it is actually being drawn on (see `sphereSegmentsFor`).
const MAX_SILHOUETTE_ERROR_PX = 0.2;
const SPHERE_SEGMENTS_MIN = 96;
const SPHERE_SEGMENTS_MAX = 512;
const LIMB_SHADE_STRENGTH = 0.45;
// Render at the display's real resolution rather than a hard 2x cap, bounded
// so a 4x phone does not quadruple the fragment cost for no visible gain.
const MAX_PIXEL_RATIO = 3;

/** Segments needed so the silhouette stays within a fifth of a device pixel.
 *
 *  Sagitta of a chord subtending 2π/N on radius R is R·(1−cos(π/N)) ≈ R·π²/2N².
 *  Solving for N gives N ≥ π·√(R / 2ε). R is the largest radius the globe can
 *  be drawn at in device pixels; beyond that the limb is off-screen anyway, so
 *  its smoothness stops mattering.
 */
function sphereSegmentsFor(widthCss, heightCss, pixelRatio) {
  const radiusDevicePx = Math.max(widthCss, heightCss) * pixelRatio / 2;
  const needed = Math.PI * Math.sqrt(radiusDevicePx / (2 * MAX_SILHOUETTE_ERROR_PX));
  // Rounded up to a multiple of 32 so ordinary resizes do not keep rebuilding
  // the geometry for a segment or two.
  const stepped = Math.ceil(needed / 32) * 32;
  return clamp(stepped, SPHERE_SEGMENTS_MIN, SPHERE_SEGMENTS_MAX);
}
// Screen footprint a country must occupy before its name is drawn, and before
// any of its cities are — a country reduced to a few pixels near the limb
// cannot carry text, however important it is.
const COUNTRY_LABEL_MIN_AREA = 2200;   // px²
const CITY_HOST_MIN_AREA = 9000;       // px²
// A state capital is only meaningful once its country dominates the screen —
// roughly "the USA fills the view", not "the USA is somewhere on the globe".
const STATE_CAPITAL_MIN_AREA = 260000; // px²
// Breathing room around a label before it counts as colliding, and a ceiling
// on how many names one view carries — density is a design decision, not an
// accident of how many features happen to be in frame.
const LABEL_PADDING = 3;
const MAX_VISIBLE_LABELS = 70;
// Label selection is expensive and, worse, visually noisy when repeated: it is
// re-run only after the camera has really moved on, and never more often than
// this. In between, labels just follow the globe.
const SELECTION_INTERVAL_MS = 400;
const SELECTION_MOVE_FRACTION = 0.06;   // ~6 % of the camera distance
// Rounding the limb is geometry, not a density decision: a name whose place
// has physically gone behind the earth must be gone at once — letting it linger
// and fade leaves text floating over the wrong hemisphere. Fades stay for
// selection changes (see fadeIn/fadeOut), where they are what stops blinking.

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

/** Same wording as the flat map's badge (map.js formatDuration) — the globe
 *  badge must read as the same product, not a second formatter's opinion. */
function formatTripDuration(minutes) {
  if (minutes < 60) return Math.round(minutes) + ' min';
  const hours = Math.floor(minutes / 60);
  return hours + ' h ' + Math.round(minutes - hours * 60) + ' min';
}

/** Labels never pop. Appearing starts transparent and transitions up; leaving
 *  transitions down and only then leaves the layout, so a label that returns
 *  mid-fade simply reverses instead of blinking. */
function fadeIn(label) {
  if (label.el.style.display !== 'block') {
    label.el.style.display = 'block';
    label.el.style.opacity = '0';
    // Force a reflow so the browser animates from 0 rather than jumping.
    void label.el.offsetWidth;
  }
  label.visible = true;
}

function fadeOut(label) {
  if (!label.visible && label.el.style.display !== 'block') return;
  label.visible = false;
  label.el.style.opacity = '0';
  clearTimeout(label.hideTimer);
  label.hideTimer = setTimeout(() => {
    if (!label.visible) label.el.style.display = 'none';
  }, LABEL_FADE_MS);
}

/** Gone this instant, no transition. Used only when a place rounds the limb:
 *  the reason it disappears is that the earth is now in front of it, and a
 *  fade would draw it hovering over the far hemisphere for a quarter second. */
function hideAtOnce(label) {
  label.visible = false;
  clearTimeout(label.hideTimer);
  // display:none is what makes it instant — the opacity transition never gets
  // a chance to run on an unrendered element, and setting it back to 0 leaves
  // the label ready to fade in normally if it comes back round.
  label.el.style.display = 'none';
  label.el.style.opacity = '0';
}

const LABEL_FADE_MS = 280;
const FOV = 45;

/** Camera distance that shows EXACTLY the ground span the flat map shows.
 *
 *  Two steps, and the second one is what makes the transition seamless:
 *
 *  1. Ground span. Web Mercator's scale is latitude-dependent — a pixel at 48°N
 *     covers cos(48°) ≈ 0.67 of the ground a pixel at the equator covers. So the
 *     visible arc is `(H / (256·2^z)) · 2π · cos(lat)`, not the equator value;
 *     using the equator figure showed ~1.5× too much world at German latitudes,
 *     which is exactly the jump between the flat map and the globe.
 *
 *  2. Camera distance for that arc. Looking at the sphere's centre from distance
 *     d, the edge of a half-arc α sits at radius R·sin α and depth d − R·cos α,
 *     so it fills half the field of view when
 *         tan(fov/2) = R·sin α / (d − R·cos α)
 *     which rearranges to the expression below.
 */
function distanceForZoom(zoom, viewportHeight, latitude) {
  // Mercator's cos(lat) stretch is honoured only near the handover, where the
  // flat map is on screen and the two must agree. Further out it is faded away,
  // otherwise panning toward a pole would pull the camera into the surface —
  // in globe mode the altitude belongs to the zoom level, not to the latitude.
  const blendToGlobe = clamp((HANDOVER_ZOOM - zoom) / 1.5, 0, 1);
  // 512, not 256: MapLibre defines its zoom levels against 512 px tiles, so a
  // 256 px world is exactly twice as much ground — measured as a constant 2.0×
  // mismatch at every latitude before this was corrected.
  const worldPx = 512 * Math.pow(2, zoom);
  // Clamped: past 60 deg the Mercator factor would keep shrinking the distance
  // towards the surface, which is the "I always dive into the pole" effect.
  const clampedLatitude = clamp(latitude || 0, -60, 60);
  const latitudeScale =
    1 - (1 - Math.cos(THREE.MathUtils.degToRad(clampedLatitude))) * (1 - blendToGlobe);
  const spanRad = (viewportHeight / worldPx) * 2 * Math.PI * latitudeScale;
  const halfSpan = Math.min(spanRad / 2, Math.PI / 2 - 0.001);
  const halfFov = (FOV / 2) * (Math.PI / 180);
  return EARTH_RADIUS * Math.cos(halfSpan) + (EARTH_RADIUS * Math.sin(halfSpan)) / Math.tan(halfFov);
}

/** The globe viewer's convention, kept identical so textures and any future
 *  location markers line up with the reference implementation. */
function latLonToVec3(lat, lon, radius) {
  const phi = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);
  return new THREE.Vector3(
    -radius * Math.sin(phi) * Math.cos(theta),
    radius * Math.cos(phi),
    radius * Math.sin(phi) * Math.sin(theta),
  );
}

function starfield() {
  const count = 3500;
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  const sizes = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    // Uniform points on a large sphere: acos keeps them from clumping at the poles.
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    const r = 45 + Math.random() * 35;
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.cos(phi);
    positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);

    // A uniform grid of identical dots reads as a texture, not as a sky. Real
    // starfields are mostly faint with a few bright ones, so brightness is
    // skewed low (pow 2.2) and size follows it — bright stars bloom slightly
    // larger. The faint tint spread keeps it from looking monochrome without
    // turning into confetti.
    const magnitude = Math.pow(Math.random(), 2.2);
    const brightness = 0.30 + magnitude * 0.70;
    const warm = (Math.random() - 0.5) * 0.10;
    colors[i * 3] = Math.min(1, brightness * (1 + warm));
    colors[i * 3 + 1] = brightness;
    colors[i * 3 + 2] = Math.min(1, brightness * (1 - warm * 0.6));
    sizes[i] = 0.16 + magnitude * 0.26;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  geometry.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));

  // PointsMaterial has one size for every point, so per-star size needs a
  // shader. Kept tiny on purpose: it is the same size attenuation three.js
  // does, with the constant replaced by an attribute.
  const material = new THREE.ShaderMaterial({
    uniforms: { uScale: { value: 320.0 } },
    vertexShader: `
      attribute float aSize;
      uniform float uScale;
      varying vec3 vColor;
      void main() {
        vColor = color;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = aSize * (uScale / -mv.z);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      varying vec3 vColor;
      void main() {
        // Round off the square point sprite and soften its edge.
        float d = length(gl_PointCoord - vec2(0.5));
        if (d > 0.5) discard;
        gl_FragColor = vec4(vColor, smoothstep(0.5, 0.15, d));
      }`,
    transparent: true, depthWrite: false, vertexColors: true,
  });
  return new THREE.Points(geometry, material);
}

function atmosphere() {
  const material = new THREE.ShaderMaterial({
    uniforms: {
      uColor: { value: new THREE.Vector3(0.24, 0.51, 0.78) },
      uIntensity: { value: 0.55 },
    },
    vertexShader: `
      varying vec3 vNormal;
      void main() {
        vNormal = normalize(normalMatrix * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform vec3 uColor;
      uniform float uIntensity;
      varying vec3 vNormal;
      void main() {
        float d = dot(vNormal, vec3(0.0, 0.0, 1.0));
        float glow = pow(max(0.7 - d, 0.0), 3.0) * uIntensity;
        if (glow < 0.004) discard;
        gl_FragColor = vec4(uColor, glow);
      }`,
    transparent: true, side: THREE.BackSide, depthWrite: false,
  });
  return new THREE.Mesh(new THREE.SphereGeometry(EARTH_RADIUS * 1.045, 128, 64), material);
}

/** Soft darkening toward the limb, applied to the surface material itself.
 *
 *  Google shades the globe this way: the disc stays bright where it faces you
 *  and falls off near the edge, which is what reads as curvature — without it
 *  a textured sphere looks like a flat circular sticker.
 *
 *  Deliberately NOT a second sphere. A shell even 0.0008 radii larger
 *  overhangs the earth by more than a pixel once the globe is ~1400 px tall,
 *  and since the shading peaks exactly at the limb that overhang shows up as a
 *  dark faceted rim that wobbles as the mesh turns. Patching the surface
 *  material has no overhang, cannot z-fight, and saves a whole sphere.
 */
function applyLimbShading(material, strength) {
  material.onBeforeCompile = (shader) => {
    shader.uniforms.uShade = { value: strength };
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>',
        '#include <common>\nvarying vec3 vLimbN;\nvarying vec3 vLimbV;')
      .replace('#include <project_vertex>',
        '#include <project_vertex>\n  vLimbN = normalize(normalMatrix * normal);'
        + '\n  vLimbV = normalize(-mvPosition.xyz);');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>',
        '#include <common>\nuniform float uShade;\nvarying vec3 vLimbN;\nvarying vec3 vLimbV;')
      .replace('#include <dithering_fragment>',
        '#include <dithering_fragment>\n'
        + '  float facing = max(dot(normalize(vLimbN), normalize(vLimbV)), 0.0);\n'
        // Exponent 1.5, not 2.5: a steeper curve puts the whole falloff in the
        // outermost pixels, which measured as a 2% drop centre-to-rim.
        + '  gl_FragColor.rgb = mix(gl_FragColor.rgb, vec3(0.02, 0.06, 0.12),'
        + ' pow(1.0 - facing, 1.5) * uShade);');
  };
  material.needsUpdate = true;
  return material;
}

class DsGlobe {
  /** @param {HTMLElement} host element the map lives in
   *  @param {object} map  the AgenticMap instance to stay in sync with */
  constructor(host, map, options) {
    this.map = map;
    this.mapbox = map.map;
    this.visible = false;
    this.textureUrl = (options && options.texture) || '/assets/earth.webp';
    this.lang = map.lang || 'de';

    this.canvas = document.createElement('canvas');
    this.canvas.className = 'am-globe';
    host.appendChild(this.canvas);

    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(FOV, 1, 0.05, 200);

    this.globe = new THREE.Group();
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(EARTH_RADIUS, SPHERE_SEGMENTS_MIN, SPHERE_SEGMENTS_MIN / 2),
      applyLimbShading(new THREE.MeshBasicMaterial({ color: 0x0b1a2e }), LIMB_SHADE_STRENGTH),
    );
    this.sphere = sphere;
    this.globe.add(sphere);
    this.globe.add(atmosphere());
    this.scene.add(this.globe);
    this.scene.add(starfield());

    this.view = map.view || 'hybrid';
    map.el.addEventListener('agentic-map:view', (event) => this.setView(event.detail.view));
    new THREE.TextureLoader().load(this.textureUrl, (texture) => {
      texture.colorSpace = THREE.SRGBColorSpace;
      this._tuneTexture(texture);
      this.satelliteTexture = texture;
      this._applyTexture();
      this.render();
    });

    // Simplified biome raster for the cartographic globe (the owner's
    // "i doubt that africa is that green"): Natural Earth II, remapped into
    // the CARTO family by tools/make_globe_biomes.py — Sahara sand, Congo
    // sage, tundra pale. Loaded async next to earth.webp; until it lands
    // (or if it 404s) the sphere keeps the flat palette tint, so a missing
    // asset degrades to exactly the old look instead of a blank globe.
    this._biomes = {};
    const assetBase = this.textureUrl.replace(/[^/]*$/, '');
    for (const theme of ['light', 'dark']) {
      const image = new Image();
      image.onload = () => {
        this._biomes[theme] = image;
        this._carto = {};        // texture cache is stale once the raster lands
        this._applyTexture();
        this.render();
      };
      image.src = assetBase + 'globe-biomes' + (theme === 'dark' ? '-dark' : '') + '.webp';
    }

    this._addBorders();
    this.labelLayer = document.createElement('div');
    this.labelLayer.className = 'am-globe-labels';
    host.appendChild(this.labelLayer);
    this.labels = [];   // {lat, lon, el, vec, kind, rank}

    // -- overlay parity: everything the flat map draws exists here too ----
    // Routes/highlights as sphere ribbons (this.overlays), pins/pings/geo
    // labels as DOM billboards, one total-duration badge per route. Built
    // once per overlay CHANGE (these events), never per frame.
    // Initialized BEFORE resize()/the move listeners below: any of those can
    // fire sync() -> takeover -> _rebuildOverlays(), and a page that OPENS
    // inside globe range (a shared low-zoom link) reached that path before
    // these structures existed — the mount died halfway and left a bare blue
    // sphere with no land texture.
    this.overlays = new THREE.Group();
    this.globe.add(this.overlays);
    this.billboardLayer = document.createElement('div');
    this.billboardLayer.className = 'am-globe-billboards';
    this.labelLayer.appendChild(this.billboardLayer);
    this.badgeBillboards = [];    // rebuilt with the routes
    this.markerBillboards = [];   // mirrored from the flat map's markers
    this._markerSig = null;
    map.el.addEventListener('agentic-map:routes-changed', () => this._overlaysStale());
    map.el.addEventListener('agentic-map:route-promoted', () => this._overlaysStale());
    map.el.addEventListener('agentic-map:overlays-changed', () => this._overlaysStale());
    map.el.addEventListener('agentic-map:country-changed', () => this._overlaysStale());

    this._addLabels();
    this.resize();
    window.addEventListener('resize', () => this.resize());
    // The map drives the globe: every camera move re-derives rotation/distance.
    this.mapbox.on('move', () => this.sync());
    this.mapbox.on('zoom', () => this.sync());

    this._bindDrag();
    this.sync();
  }

  /** Drop points that add no visible shape. Natural Earth carries far more
   *  detail than a globe can show, and a weak phone pays for every vertex:
   *  points closer together than `toleranceDeg` are merged. */
  _simplify(ring, toleranceDeg) {
    if (ring.length < 3) return ring;
    const out = [ring[0]];
    let last = ring[0];
    for (let i = 1; i < ring.length - 1; i++) {
      const dx = ring[i][0] - last[0], dy = ring[i][1] - last[1];
      if (dx * dx + dy * dy >= toleranceDeg * toleranceDeg) {
        out.push(ring[i]);
        last = ring[i];
      }
    }
    out.push(ring[ring.length - 1]);
    return out;
  }

  /** A line with real width: WebGL caps lineWidth at 1 px, which makes borders
   *  look brittle and broken up close. Each segment becomes a quad offset
   *  perpendicular to its direction in the sphere's tangent plane, so the line
   *  has a true geographic width — thin from orbit, solid when you close in.
   *  Static geometry, no per-frame work: a phone renders it as one draw call. */
  _ribbonLayer(features, color, opacity, widthDeg, toleranceDeg, radiusOffset) {
    const geometry = this._ribbonGeometry(features, widthDeg, toleranceDeg, radiusOffset);
    if (!geometry) return null;
    // Depth testing MUST stay on, otherwise borders on the far side of the
    // planet shine through the globe. The layers are separated by radius
    // instead of by disabling depth, which keeps the sphere occluding them.
    const layer = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      color, transparent: true, opacity, depthWrite: false, depthTest: true,
      side: THREE.DoubleSide,
    }));
    this.globe.add(layer);
    return layer;
  }

  /** The geometry half of _ribbonLayer, shared with the overlay renderer —
   *  overlays need meshes they can dispose and rebuild, not permanent
   *  layers, but the ribbon construction is exactly the same. */
  _ribbonGeometry(features, widthDeg, toleranceDeg, radiusOffset) {
    const positions = [];
    const radius = EARTH_RADIUS * (radiusOffset || 1.0015);
    const halfWidth = THREE.MathUtils.degToRad(widthDeg) / 2;
    const addSegment = (a, b) => {
      const pa = latLonToVec3(a[1], a[0], radius);
      const pb = latLonToVec3(b[1], b[0], radius);
      const along = pb.clone().sub(pa);
      if (along.lengthSq() === 0) return;
      // Perpendicular within the tangent plane: along x up(=surface normal).
      const normal = pa.clone().normalize();
      const side = along.clone().cross(normal).normalize().multiplyScalar(halfWidth * radius);
      const a0 = pa.clone().add(side), a1 = pa.clone().sub(side);
      const b0 = pb.clone().add(side), b1 = pb.clone().sub(side);
      positions.push(a0.x, a0.y, a0.z, b0.x, b0.y, b0.z, a1.x, a1.y, a1.z);
      positions.push(b0.x, b0.y, b0.z, b1.x, b1.y, b1.z, a1.x, a1.y, a1.z);
    };
    const addRing = (ring) => {
      const simplified = this._simplify(ring, toleranceDeg);
      for (let i = 0; i < simplified.length - 1; i++) addSegment(simplified[i], simplified[i + 1]);
    };
    for (const feature of features) {
      const geometry = feature.geometry;
      if (geometry.type === 'LineString') addRing(geometry.coordinates);
      else if (geometry.type === 'MultiLineString') geometry.coordinates.forEach(addRing);
      else if (geometry.type === 'Polygon') geometry.coordinates.forEach(addRing);
      else if (geometry.type === 'MultiPolygon') {
        geometry.coordinates.forEach((polygon) => polygon.forEach(addRing));
      }
    }
    if (!positions.length) return null;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3));
    return geometry;
  }

  /** Build one LineSegments object from a set of GeoJSON line/polygon features. */
  _lineLayer(features, color, opacity) {
    const points = [];
    const pushRing = (ring) => {
      for (let i = 0; i < ring.length - 1; i++) {
        points.push(latLonToVec3(ring[i][1], ring[i][0], EARTH_RADIUS * 1.0015));
        points.push(latLonToVec3(ring[i + 1][1], ring[i + 1][0], EARTH_RADIUS * 1.0015));
      }
    };
    for (const feature of features) {
      const geometry = feature.geometry;
      if (geometry.type === 'LineString') pushRing(geometry.coordinates);
      else if (geometry.type === 'MultiLineString') geometry.coordinates.forEach(pushRing);
      else if (geometry.type === 'Polygon') geometry.coordinates.forEach(pushRing);
      else if (geometry.type === 'MultiPolygon') {
        geometry.coordinates.forEach((polygon) => polygon.forEach(pushRing));
      }
    }
    if (!points.length) return null;
    const layer = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false }),
    );
    this.globe.add(layer);
    return layer;
  }

  /** Karte / Dunkel must be a map on the globe too — no aerial. Equirectangular
   *  is a direct lon/lat → x/y mapping, so the cartographic sphere texture can
   *  simply be drawn with Canvas2D from the same country polygons the flat map
   *  uses, in the same palette. */
  _cartoTexture(dark) {
    if (!this.countryGeo) return null;
    const cacheKey = dark ? 'dark' : 'light';
    this._carto = this._carto || {};
    if (this._carto[cacheKey]) return this._carto[cacheKey];

    const width = 4096, height = 2048;
    const canvas = document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d');
    // The SAME palette family as the flat cartography (road-styles.js
    // CARTO): the z4 globe↔flat handover must read as one map, not as a
    // washed-out sphere under a sage map. Falls back to the legacy palette
    // only if road-styles.js is not on the page.
    const shared = (typeof agenticRoadStyles !== 'undefined'
      && agenticRoadStyles.CARTO) ? agenticRoadStyles.CARTO[dark ? 'dark' : 'light'] : null;
    const palette = shared
      ? { water: shared.water, land: shared.globeLand, urban: shared.globeUrban }
      : dark
        ? { water: '#0d1622', land: '#232c37', urban: 'rgba(58,68,82,0.9)' }
        : { water: '#a4d0e8', land: '#e9eddf', urban: 'rgba(214,212,203,0.95)' };
    ctx.fillStyle = palette.water;
    ctx.fillRect(0, 0, width, height);
    const project = (lon, lat) => [(lon + 180) / 360 * width, (90 - lat) / 180 * height];
    const drawGeometry = (geometry, close) => {
      const polygons = geometry.type === 'Polygon' ? [geometry.coordinates]
        : geometry.type === 'MultiPolygon' ? geometry.coordinates
        : geometry.type === 'LineString' ? [[geometry.coordinates]]
        : geometry.type === 'MultiLineString' ? geometry.coordinates.map((line) => [line])
        : [];
      for (const polygon of polygons) {
        for (const ring of polygon) {
          ctx.beginPath();
          ring.forEach((point, index) => {
            const [x, y] = project(point[0], point[1]);
            index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
          });
          if (close) { ctx.closePath(); ctx.fill(); }
          ctx.stroke();
        }
      }
    };

    ctx.fillStyle = palette.land;
    // Fills only. Every line — borders, state lines, rivers — is drawn as
    // geometry on the sphere instead, so it stays sharp at any altitude.
    ctx.strokeStyle = 'rgba(0,0,0,0)';
    ctx.lineWidth = 0;
    for (const feature of this.countryGeo.features) {
      const polygons = feature.geometry.type === 'Polygon'
        ? [feature.geometry.coordinates] : feature.geometry.coordinates;
      for (const polygon of polygons) {
        for (const ring of polygon) {
          ctx.beginPath();
          ring.forEach(([lon, lat], index) => {
            const [x, y] = project(lon, lat);
            index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
          });
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
        }
      }
    }
    // Biome base over the flat tint, clipped to OUR country polygons: the
    // raster brings the simplified landcover (sand/sage/pale), the vector
    // shapes keep the coastline crisp — the raster's own coast never shows.
    // Same equirectangular projection on both sides, so it maps 1:1. The
    // flat fill above stays underneath as the no-asset fallback.
    const biome = this._biomes && this._biomes[cacheKey];
    if (biome) {
      ctx.save();
      ctx.beginPath();
      for (const feature of this.countryGeo.features) {
        const polygons = feature.geometry.type === 'Polygon'
          ? [feature.geometry.coordinates] : feature.geometry.coordinates;
        for (const polygon of polygons) {
          for (const ring of polygon) {
            ring.forEach(([lon, lat], index) => {
              const [x, y] = project(lon, lat);
              index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
            });
            ctx.closePath();
          }
        }
      }
      // evenodd: enclave/hole rings (inland seas cut out of the land
      // polygons) must stay water, whatever their winding direction is.
      ctx.clip('evenodd');
      ctx.drawImage(biome, 0, 0, width, height);
      ctx.restore();
    }
    // Physical layers on top of the land: lakes, then rivers, then the major
    // road network — the three things Google shows at this scale.
    if (this.physicalGeo) {
      const byLayer = (name) => this.physicalGeo.features.filter((f) => f.properties.layer === name);
      ctx.fillStyle = palette.water;
      ctx.strokeStyle = palette.water;
      ctx.lineWidth = 1;
      for (const lake of byLayer('lakes')) drawGeometry(lake.geometry, true);

      // Urban areas: the grey smudges that tell you where the people are.
      ctx.fillStyle = palette.urban;
      ctx.strokeStyle = palette.urban;
      ctx.lineWidth = 0.5;
      for (const urban of byLayer('urban')) drawGeometry(urban.geometry, true);

    }

    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    this._tuneTexture(texture);
    this._carto[cacheKey] = texture;
    return texture;
  }

  /** Anisotropic filtering.
   *
   *  Most of a sphere is seen at a glancing angle, and which parts those are
   *  changes continuously while you drag — so plain trilinear mipmapping picks
   *  an over-blurred mip in one direction and aliases in the other, and the
   *  surface appears to crawl. This is the single biggest quality difference
   *  on a rotating globe, and it costs nothing in geometry.
   */
  _tuneTexture(texture) {
    const max = this.renderer.capabilities.getMaxAnisotropy();
    texture.anisotropy = Math.min(8, max);
    texture.generateMipmaps = true;
    texture.minFilter = THREE.LinearMipmapLinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.needsUpdate = true;
  }

  _applyTexture() {
    const cartographic = this.view === 'map-light' || this.view === 'map-dark';
    const texture = cartographic ? this._cartoTexture(this.view === 'map-dark') : this.satelliteTexture;
    if (!texture) return;
    this.sphere.material = applyLimbShading(
      new THREE.MeshBasicMaterial({ map: texture }), LIMB_SHADE_STRENGTH);
    // Lines stay geometry in every mode — only their colour follows the theme.
    // Cartographic colours come from the shared flat-map palette
    // (road-styles.js CARTO) so the handover stays in one family; the
    // imagery globe keeps its white lines untouched.
    const dark = this.view === 'map-dark';
    const shared = (typeof agenticRoadStyles !== 'undefined'
      && agenticRoadStyles.CARTO) ? agenticRoadStyles.CARTO[dark ? 'dark' : 'light'] : null;
    if (this.borders) {
      this.borders.visible = true;
      this.borders.material.color.set(cartographic
        ? (shared ? shared.globeBorder : (dark ? '#9db0c8' : '#46536a')) : '#ffffff');
      this.borders.material.opacity = cartographic ? (dark ? 0.8 : 0.95) : 1;
    }
    if (this.bordersCasing) {
      // The casing is only needed where the background is photographic.
      this.bordersCasing.visible = !cartographic;
    }
    if (this.rivers) {
      this.rivers.visible = cartographic;
      this.rivers.material.color.set(
        shared ? shared.globeRiver : (dark ? '#4f86b8' : '#78b4dc'));
    }
    if (this.stateLines) {
      // State lines belong on the imagery globe too — subordinate to the
      // national border, but present.
      this.stateLines.visible = true;
      this.stateLines.material.color.set(cartographic
        ? (shared ? shared.globeStateBorder : (dark ? '#6b7d95' : '#9aa8ba')) : '#ffffff');
      this.stateLines.material.opacity = cartographic ? 0.6 : 0.5;
    }
  }

  setView(view) {
    this.view = view;
    this._applyTexture();
    this.render();
  }

  /** Country outlines drawn on the sphere — the globe is geography, not a
   *  texture: without borders you cannot tell where anything is. */
  async _addBorders() {
    try {
      const data = await (await fetch('/api/v1/maps/geo/countries', { cache: 'no-cache' })).json();
      this.countryGeo = data;   // reused for the cartographic sphere texture
      const points = [];
      const pushRing = (ring) => {
        for (let i = 0; i < ring.length - 1; i++) {
          points.push(latLonToVec3(ring[i][1], ring[i][0], EARTH_RADIUS * 1.0015));
          points.push(latLonToVec3(ring[i + 1][1], ring[i + 1][0], EARTH_RADIUS * 1.0015));
        }
      };
      for (const feature of data.features) {
        const geometry = feature.geometry;
        if (geometry.type === 'Polygon') geometry.coordinates.forEach(pushRing);
        else if (geometry.type === 'MultiPolygon') {
          geometry.coordinates.forEach((polygon) => polygon.forEach(pushRing));
        }
      }
      // Casing slightly wider and underneath, bright ribbon on top.
      this.bordersCasing = this._ribbonLayer(data.features, 0x0a1119, 0.7, 0.085, 0.05, 1.0012);
      if (this.bordersCasing) this.bordersCasing.renderOrder = 1;
      this.borders = this._ribbonLayer(data.features, 0xffffff, 0.95, 0.042, 0.05, 1.0020);
      if (this.borders) this.borders.renderOrder = 3;
      this._applyTexture();
      // A country highlight set before the polygons landed can only be
      // traced now — the overlay pass needs this.countryGeo.
      if (this.overlays && this.map._country) this._overlaysStale();
      this.render();
      try {
        this.physicalGeo = await (await fetch('/api/v1/maps/geo/physical', { cache: 'no-cache' })).json();
      const byLayer = (name) => this.physicalGeo.features.filter((f) => f.properties.layer === name);
      this.rivers = this._ribbonLayer(byLayer('rivers'), 0x6fb0dc, 0.9, 0.028, 0.08, 1.0016);
      if (this.rivers) this.rivers.renderOrder = 2;
      this.stateLines = this._ribbonLayer(byLayer('states'), 0x8496ac, 0.6, 0.03, 0.06, 1.0016);
      if (this.stateLines) this.stateLines.renderOrder = 2;
        this._carto = {};          // palette cache is stale once the layers land
        this._applyTexture();
        this.render();
      } catch (error) { console.error('[globe] physical layers failed', error); }
    } catch (error) {
      console.error('[globe] borders failed', error);
    }
  }

  /** Country and city names, as HTML projected from the sphere each frame.
   *  Billboarded text in WebGL would need an SDF atlas; the DOM gives crisp
   *  type for free and lets the map's own CSS style them. */
  async _addLabels() {
    const add = (lat, lon, text, kind, rank) => {
      const el = document.createElement('div');
      el.className = 'am-globe-label am-globe-label--' + kind;
      // Belt and braces with the stylesheet: nothing is on screen until the
      // placement pass has given it a position.
      el.style.display = 'none';
      el.textContent = text;
      this.labelLayer.appendChild(el);
      const record = { lat, lon, el, kind, rank, vec: latLonToVec3(lat, lon, EARTH_RADIUS * 1.002) };
      this.labels.push(record);
      return record;
    };
    try {
      const countries = await (await fetch('/api/v1/maps/geo/countries/labels', { cache: 'no-cache' })).json();
      for (const feature of countries.features) {
        const [lon, lat] = feature.geometry.coordinates;
        const p = feature.properties;
        const label = add(lat, lon, p['name_' + this.lang] || p.name_en || p.name, 'country', 0);
      label.name = p.name;      // English name — what city records refer to
      label.bbox = p.bbox;
      }
    } catch (error) { console.error('[globe] country labels failed', error); }
    try {
      const cities = await (await fetch('/api/v1/maps/geo/cities', { cache: 'no-cache' })).json();
      for (const feature of cities.features) {
        // National capitals always; state capitals only once their country
        // dominates the view — at globe altitude everything else is clutter.
        const isCapital = feature.properties.capital;
        const isStateCapital = feature.properties.state_capital;
        if (!isCapital && !isStateCapital) continue;
        const [lon, lat] = feature.geometry.coordinates;
        const city = add(lat, lon, feature.properties.name, 'city', feature.properties.rank ?? 10);
        city.country = feature.properties.country;
        city.stateCapital = !isCapital && isStateCapital;
      }
    } catch (error) { console.error('[globe] city labels failed', error); }
    try {
      // Ocean names are what stops the water half of the globe reading as
      // empty blue. Natural Earth ranks them 0 (the five oceans) through ~6
      // (bays), which is exactly the order they should appear in as you
      // approach.
      const oceans = await (await fetch('/api/v1/maps/geo/oceans', { cache: 'no-cache' })).json();
      for (const feature of oceans.features) {
        const [lon, lat] = feature.geometry.coordinates;
        const p = feature.properties;
        add(lat, lon, p['name_' + this.lang] || p.name_en || p.name, 'ocean', p.rank ?? 5);
      }
    } catch (error) { console.error('[globe] ocean labels failed', error); }
    this.render();
  }

  /** How much screen does this country actually cover, in px²?
   *  Projects the mainland bbox corners; returns 0 when it faces away. */
  _screenArea(label, camera, half) {
    if (!label.bbox) return 0;
    const [west, south, east, north] = label.bbox;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, facing = 0;
    for (const [lat, lon] of [[south, west], [south, east], [north, west], [north, east],
                              [label.lat, label.lon]]) {
      const vec = latLonToVec3(lat, lon, EARTH_RADIUS);
      facing = Math.max(facing, vec.dot(camera.position.clone().normalize()));
      const p = vec.project(camera);
      minX = Math.min(minX, (p.x + 1) * half.x); maxX = Math.max(maxX, (p.x + 1) * half.x);
      minY = Math.min(minY, (1 - p.y) * half.y); maxY = Math.max(maxY, (1 - p.y) * half.y);
    }
    if (facing <= 0) return 0;
    return Math.max(0, maxX - minX) * Math.max(0, maxY - minY);
  }

  /** Move the already-chosen labels with the globe. Cheap: transforms only,
   *  no selection, no layout reads — this runs on every frame. */
  _updateLabelPositions() {
    const camera = this.camera;
    camera.updateMatrixWorld(true);
    const camDir = camera.position.clone().normalize();
    const horizon = EARTH_RADIUS / camera.position.length();
    const half = { x: this.width / 2, y: this.height / 2 };
    // Everything currently on screen, not just the current selection: a label
    // that was dropped is still visible for the length of its fade, and if it
    // stops tracking its coordinates it hangs in mid-air while the globe turns
    // underneath it. Position is a property of the place, opacity is a
    // property of the selection — so position is updated for both.
    for (const label of this.labels) {
      const selected = this.selected ? this.selected.has(label) : false;
      if (!selected && label.el.style.display !== 'block') continue;

      const facing = label.vec.dot(camDir);
      if (facing <= horizon) { hideAtOnce(label); continue; }
      const projected = label.vec.clone().project(camera);
      // Behind the camera, `project` mirrors the point through the origin and
      // the label lands at a nonsensical screen position — which is what piled
      // dozens of names into the top corner. The reselection path already
      // guarded this; the per-frame path did not, so anything that rounded the
      // limb between reselections stayed on screen at the wrong place.
      if (projected.z > 1) { hideAtOnce(label); continue; }
      label.el.style.transform = 'translate(-50%, -50%) translate('
        + ((projected.x + 1) * half.x) + 'px,' + ((1 - projected.y) * half.y) + 'px)';
      // Only the chosen ones get their opacity driven here; a fading label is
      // mid-transition and must be left alone, or it would snap back to full.
      if (!selected) continue;
      // Same limb ramp the selection pass uses, so a label does not change
      // brightness depending on which path last touched it.
      label.el.style.display = 'block';
      label.el.style.opacity = String(Math.min(1, (facing - horizon) / 0.08));
    }
  }

  /** Should the label set be re-chosen? Only after the camera has actually
   *  moved somewhere else — reselecting per frame is what makes a rotating
   *  globe look nervous. */
  _selectionIsStale() {
    const camera = this.camera;
    const now = performance.now();
    if (!this._lastSelection) return true;
    if (now - this._lastSelection.time < SELECTION_INTERVAL_MS) return false;
    const moved = camera.position.distanceTo(this._lastSelection.position);
    return moved > this._lastSelection.position.length() * SELECTION_MOVE_FRACTION;
  }

  /** Place labels with perspective taken seriously: a country that is turned
   *  away, or that covers only a few pixels at this altitude, contributes
   *  nothing readable — so neither it nor its cities are drawn. */
  _positionLabels() {
    if (!this.labels || !this.labels.length) return;
    const camera = this.camera;
    camera.updateMatrixWorld(true);
    const camDir = camera.position.clone().normalize();
    const half = { x: this.width / 2, y: this.height / 2 };
    // A point is over the horizon when cos(angle from the sub-camera point)
    // drops below R/d — that is the true silhouette, and it tightens as you
    // climb. A fixed threshold let labels from the far side leak through.
    const horizon = EARTH_RADIUS / camera.position.length();

    // Pass 1: which countries earn screen space?
    const areaByCountry = new Map();
    for (const label of this.labels) {
      if (label.kind !== 'country') continue;
      const area = this._screenArea(label, camera, half);
      areaByCountry.set(label.name, area);
      label.area = area;
    }

    // Pass 2: collect what is eligible, with a priority and a screen position.
    const span = this.visibleSpanDeg();
    const oceanRankLimit = span > 120 ? 0 : span > 60 ? 1 : span > 25 ? 3 : 6;
    const candidates = [];
    for (const label of this.labels) {
      const facing = label.vec.dot(camDir);
      let wanted = facing > horizon;
      if (wanted && label.kind === 'country') {
        wanted = label.area >= COUNTRY_LABEL_MIN_AREA;
      } else if (wanted && label.kind === 'ocean') {
        // Rank in, as the view closes in: the five oceans from the very top,
        // the named seas once a hemisphere no longer fills the screen, bays
        // last. `span` is the visible arc in degrees, so it shrinks as you
        // approach.
        wanted = label.rank <= oceanRankLimit;
      } else if (wanted && label.kind === 'city') {
        const hostArea = areaByCountry.get(label.country) ?? 0;
        // A city needs its country to be both present and roomy; the roomier
        // it is, the deeper into the rank list we go. State capitals wait
        // until that country genuinely fills the view.
        const minArea = label.stateCapital ? STATE_CAPITAL_MIN_AREA : CITY_HOST_MIN_AREA;
        wanted = hostArea >= minArea
          && label.rank <= 2 + Math.log2(hostArea / CITY_HOST_MIN_AREA) * 2;
      }
      if (!wanted) { fadeOut(label); continue; }
      const projected = label.vec.clone().project(camera);
      if (projected.z > 1) { fadeOut(label); continue; }
      const x = (projected.x + 1) * half.x, y = (1 - projected.y) * half.y;
      if (x < -80 || y < -30 || x > this.width + 80 || y > this.height + 30) {
        fadeOut(label);
        continue;
      }
      candidates.push({
        label, x, y, facing,
        // Countries outrank every city; among peers, bigger / more important wins.
        // Countries first, then open water, then cities: an ocean name is
        // large-scale orientation, a capital is detail.
        priority: label.kind === 'country'
          ? 1e9 + label.area
          : label.kind === 'ocean'
            ? 8e8 - label.rank * 1e6
            : (label.stateCapital ? 5e5 : 1e6) - label.rank * 1000,
      });
    }

    // Pass 3: place by priority and drop anything that would collide with an
    // already-placed name. Labels already on screen get a bonus so the set
    // stays stable while rotating instead of reshuffling every recompute.
    for (const candidate of candidates) {
      if (this.selected && this.selected.has(candidate.label)) candidate.priority *= 1.35;
    }
    candidates.sort((a, b) => b.priority - a.priority);
    const placed = [];
    const chosen = new Set();
    for (const candidate of candidates) {
      const size = this._labelSize(candidate.label);
      const box = {
        x0: candidate.x - size.w / 2 - LABEL_PADDING, x1: candidate.x + size.w / 2 + LABEL_PADDING,
        y0: candidate.y - size.h / 2 - LABEL_PADDING, y1: candidate.y + size.h / 2 + LABEL_PADDING,
      };
      const collides = placed.some((o) =>
        box.x0 < o.x1 && box.x1 > o.x0 && box.y0 < o.y1 && box.y1 > o.y0);
      if (collides || placed.length >= MAX_VISIBLE_LABELS) {
        fadeOut(candidate.label);
        continue;
      }
      placed.push(box);
      chosen.add(candidate.label);
      const el = candidate.label.el;
      // Type size is constant: rescaling per view made labels jump about while
      // panning. Which labels appear is the density control, not how big they are.
      el.style.display = 'block';
      el.style.transform = 'translate(-50%, -50%) translate(' + candidate.x + 'px,' + candidate.y + 'px)';
      // Fade out as a label approaches the limb rather than popping.
      requestAnimationFrame(() => {
        el.style.opacity = String(Math.min(1, (candidate.facing - horizon) / 0.08));
      });
    }
    for (const label of this.labels) {
      if (!chosen.has(label) && label.el.style.display === 'block' && !placed.length) continue;
    }
    this.selected = chosen;
    this._lastSelection = { time: performance.now(), position: this.camera.position.clone() };
  }

  /** Label footprint in px. Measured once per element — layout reads are
   *  expensive — and estimated from the text until that first measurement. */
  _labelSize(label) {
    if (label.size && label.size.font === label.el.style.fontSize) return label.size;
    const rect = label.el.getBoundingClientRect();
    if (rect.width > 0) {
      label.size = { w: rect.width, h: rect.height, font: label.el.style.fontSize };
      return label.size;
    }
    const perChar = label.kind === 'country' ? 10.5 : 6.4;
    return { w: label.el.textContent.length * perChar, h: label.kind === 'country' ? 16 : 14 };
  }

  resize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    this.width = rect.width; this.height = rect.height;
    this.renderer.setSize(rect.width, rect.height);  // updateStyle=true: CSS box must match the buffer
    this.camera.aspect = rect.width / Math.max(rect.height, 1);
    this.camera.updateProjectionMatrix();
    this._retessellate();
    this.sync();
  }

  /** Rebuild the sphere when the device pixels it covers change enough to
   *  matter. Moving between a laptop panel and an external display changes
   *  both the size and the pixel ratio, and a constant chosen for one of them
   *  is wrong on the other. */
  _retessellate() {
    const segments = sphereSegmentsFor(this.width, this.height, this.renderer.getPixelRatio());
    if (segments === this._segments) return;
    this._segments = segments;
    const previous = this.sphere.geometry;
    this.sphere.geometry = new THREE.SphereGeometry(EARTH_RADIUS, segments, segments / 2);
    if (previous) previous.dispose();
  }

  /** 0 = flat map, 1 = globe. Nothing suppresses the handover any more:
   *  every overlay kind — routes, pins, badges, highlights, countries — has
   *  a sphere rendition now, so the globe may always take the stage below
   *  the threshold. (The old "no globe while a route stands" stopgap existed
   *  only because the globe could not draw routes.) */
  blend(zoom) {
    return zoom <= HANDOVER_ZOOM ? 1 : 0;
  }

  sync() {
    const zoom = this.mapbox.getZoom();
    const amount = this.blend(zoom);
    const shouldShow = amount > 0.001;
    if (shouldShow !== this.visible) {
      this.visible = shouldShow;
      this.canvas.style.display = shouldShow ? 'block' : 'none';
      this.canvas.style.pointerEvents = shouldShow ? 'auto' : 'none';
      // Hide the labels HERE, not in render(): sync() returns early when the
      // globe is off, so relying on render() left globe labels floating over
      // the flat map after the switch back.
      if (this.labelLayer) this.labelLayer.style.display = shouldShow ? 'block' : 'none';
      // Only one earth on screen at a time: leaving the flat map drawn behind
      // the globe is what produced the ghost sphere over Europe.
      const flat = this.mapbox.getCanvas();
      if (flat) flat.style.visibility = shouldShow ? 'hidden' : 'visible';
      if (shouldShow) {
        // Tilt/rotate belong to the close-zoom Mercator view; the globe is
        // seen straight-on. Squaring the underlying camera here also means
        // zooming back in lands on a level map, not a leftover diagonal.
        if (this.mapbox.getPitch() || this.mapbox.getBearing()) {
          this.mapbox.jumpTo({ pitch: 0, bearing: 0 });
        }
        // Handover continuity: whatever overlays stood on the flat map are
        // on the sphere from the very first globe frame.
        this._rebuildOverlays();
      }
    }
    if (!shouldShow) return;

    // Move the CAMERA to the map's centre rather than spinning the sphere:
    // Euler angles compose in an order that tilts the poles, which is what put
    // Australia on screen while the map was over Germany.
    const center = this.mapbox.getCenter();
    const distance = distanceForZoom(zoom, this.height, center.lat);
    this.camera.position.copy(latLonToVec3(center.lat, center.lng, distance));
    this.camera.up.set(0, 1, 0);
    this.camera.lookAt(0, 0, 0);
    // Overlay ribbons are sized in screen pixels and built in geographic
    // degrees; once the altitude has drifted far enough that the built width
    // is visibly wrong, rebuild — a threshold, not a per-frame rebuild.
    if (this._builtSpan
        && Math.abs(Math.log2(this.visibleSpanDeg() / this._builtSpan)) > 0.4) {
      this._rebuildOverlays();
    }
    // Markers can change without any event (drawStopMarkers, search pins);
    // a cheap signature diff per camera move keeps the mirror honest.
    this._syncMarkerBillboards(false);
    this.render();
  }

  render() {
    if (!this.visible) {
      if (this.labelLayer) this.labelLayer.style.display = 'none';
      return;
    }
    if (this.labelLayer) this.labelLayer.style.display = 'block';
    this.renderer.render(this.scene, this.camera);
    if (this._selectionIsStale()) this._positionLabels();
    else this._updateLabelPositions();
    this._updateBillboards();
  }

  // ==================================================================
  // Overlay parity: routes, geo highlights, country emphasis, badges,
  // and mirrored markers on the sphere. Geometry is rebuilt only when an
  // overlay changes (or the altitude drifts enough to resize the ribbons);
  // per frame the only work is projecting the DOM billboards.
  // ==================================================================

  /** An overlay changed on the flat map. Rebuild if the globe is on stage;
   *  off stage the next takeover rebuilds everything anyway. */
  _overlaysStale() {
    this._markerSig = null;
    if (this.visible) this._rebuildOverlays();
    this.sync();
  }

  _rebuildOverlays() {
    // Constructor ordering is handled above, but a guard costs nothing and a
    // half-mounted globe (bare blue sphere) costs the whole map.
    if (!this.overlays) return;
    this._builtSpan = this.visibleSpanDeg();
    while (this.overlays.children.length) {
      const mesh = this.overlays.children[0];
      this.overlays.remove(mesh);
      mesh.geometry.dispose();
      mesh.material.dispose();
    }
    this._rebuildRoutes();
    this._rebuildGeo();
    this._rebuildCountry();
    this._syncMarkerBillboards(true);
  }

  /** Ribbon sized in SCREEN pixels: converted to geographic degrees at the
   *  current altitude, so a route reads as a line from any height instead of
   *  thinning into sub-pixel noise at planet scale. */
  _overlayRibbon(features, color, widthPx, radiusOffset, opacity, renderOrder) {
    const degPerPx = this.visibleSpanDeg() / Math.max(this.height, 1);
    // A tiny feature — a 400 m radius circle seen from orbit — must not
    // simplify away entirely: capping the tolerance at a fraction of the
    // feature's own extent degrades it to a small blob at the right place
    // (visibly represented) instead of silently vanishing.
    let extent = 0;
    let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
    const scan = (ring) => {
      for (const point of ring) {
        minLon = Math.min(minLon, point[0]); maxLon = Math.max(maxLon, point[0]);
        minLat = Math.min(minLat, point[1]); maxLat = Math.max(maxLat, point[1]);
      }
    };
    for (const feature of features) {
      const g = feature.geometry;
      if (g.type === 'LineString') scan(g.coordinates);
      else if (g.type === 'MultiLineString' || g.type === 'Polygon') g.coordinates.forEach(scan);
      else if (g.type === 'MultiPolygon') {
        g.coordinates.forEach((polygon) => polygon.forEach(scan));
      }
    }
    if (maxLon > minLon || maxLat > minLat) {
      extent = Math.max(maxLon - minLon, maxLat - minLat);
    }
    const tolerance = Math.min(0.6 * degPerPx, extent / 8);
    const geometry = this._ribbonGeometry(
      features, widthPx * degPerPx, tolerance, radiusOffset);
    if (!geometry) return null;
    const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      color, transparent: true, opacity,
      depthWrite: false, depthTest: true, side: THREE.DoubleSide,
    }));
    mesh.renderOrder = renderOrder;
    this.overlays.add(mesh);
    return mesh;
  }

  /** Every drawn route as sphere ribbons: one dark casing underneath (the
   *  flat map's separation idea, approximated with a single wider ribbon),
   *  each leg on top in its own colour — the same colours the flat map uses
   *  (map.legColor), so the two renditions read as one route.
   *
   *  Deliberate scale choices, both documented for the record:
   *  - Alternates are NOT drawn. A dimmed alternate at planet scale is
   *    indistinguishable clutter beside the primary, and it could not be
   *    clicked here anyway. It reappears untouched on the flat map.
   *  - ONE total badge per route, at the route's midpoint, instead of the
   *    flat map's per-leg badges — several duration chips a few pixels
   *    apart are illegible from orbit; the trip total is the number that
   *    means something at this height.
   */
  _rebuildRoutes() {
    this._clearBillboards(this.badgeBillboards);
    for (const route of (this.map.routes || [])) {
      const geometry = route.geometry || [];
      if (geometry.length < 2) continue;
      const coords = geometry.map((p) => [p.lon, p.lat]);
      const whole = { geometry: { type: 'LineString', coordinates: coords } };
      this._overlayRibbon([whole], 0x10151c, 7.5, 1.0026, 0.8, 4);
      const spans = this.map._legSpans ? this.map._legSpans(route) : null;
      if (spans) {
        spans.forEach((span, index) => {
          const slice = { geometry: { type: 'LineString',
            coordinates: coords.slice(span.from, span.to + 1) } };
          // Later legs sit a hair higher AND draw later (renderOrder):
          // out-and-back trips share motorway corridors, and at ribbon
          // widths of ~30 km the legs overlap there — three.js's distance
          // sort then picks a winner per camera angle, which made a leg
          // vanish. Deterministic stacking keeps every colour visible on
          // its own stretch.
          this._overlayRibbon([slice],
            new THREE.Color(this.map.legColor(route, index) || '#2e6be6'),
            4.5, 1.0032 + index * 0.0002, 1, 5 + index);
        });
      } else {
        this._overlayRibbon([whole],
          new THREE.Color(route.color || '#2e6be6'), 4.5, 1.0032, 1, 5);
      }
      if (route.duration_min) {
        const mid = geometry[Math.floor(geometry.length / 2)];
        const badge = document.createElement('div');
        badge.className = 'am-map-route-badge am-globe-route-badge';
        badge.innerHTML = '<span>' + formatTripDuration(route.duration_min)
          + (route.distance_km >= 1 ? ' · ' + Math.round(route.distance_km) + ' km' : '')
          + '</span>';
        this._addBillboard(this.badgeBillboards, mid.lat, mid.lon, badge, 'center');
      }
    }
  }

  /** Geo highlights (radius circles, polygons, traced lines) as ribbons in
   *  their own colour. Polygons degrade DELIBERATELY to their outline: a
   *  translucent fill would need triangulation for nothing a viewer could
   *  read at this scale — the outline says "this area is marked" just as
   *  well, and their label chips arrive via the marker mirror. */
  _rebuildGeo() {
    for (const g of (this.map._geo || [])) {
      let color = '#f5d76e';
      for (const layer of (g.layers || [])) {
        const paint = layer.paint || {};
        const candidate = paint['line-color'] || paint['fill-color'];
        // Skip the casing black — the ribbon should carry the accent.
        if (candidate && candidate !== '#10151c') color = candidate;
      }
      this._overlayRibbon([g.data], new THREE.Color(color), 3.5, 1.0030, 0.95, 5);
    }
  }

  /** The highlighted country's border, re-traced on the sphere in the accent
   *  colour. Border emphasis only: the globe's own label already names the
   *  country, and the flat map's 16% fill would need polygon triangulation
   *  to say nothing more — parity is the traced border. */
  _rebuildCountry() {
    const filter = this.map._country;
    if (!filter || !this.countryGeo) return;
    // map.js builds exactly ['==', ['get', key], value]; read it back.
    const key = Array.isArray(filter[1]) ? filter[1][1] : null;
    const value = filter[2];
    if (!key) return;
    const features = this.countryGeo.features.filter(
      (f) => f.properties && f.properties[key] === value);
    if (!features.length) return;
    const accent = (this.map.payload && this.map.payload.brand_colors
      && this.map.payload.brand_colors.highlight) || '#7cc4ff';
    this._overlayRibbon(features, new THREE.Color(accent), 4, 1.0024, 0.95, 4);
  }

  /** Mirror the flat map's DOM markers — search pin, stop pins with their
   *  colours, pings (CSS animation and all), geo label chips, the locate-me
   *  dot — as billboards projected like the globe's labels. Route badges and
   *  the animation head are excluded: the globe draws its own single badge.
   *
   *  Clones, not moves: the originals stay in the flat map's DOM untouched,
   *  which is what makes zooming back in seamless. The clone keeps every
   *  inline style (stop colour, decoration scale) except MapLibre's own
   *  positioning transform, which the billboard projection replaces. */
  _syncMarkerBillboards(force) {
    const registry = window.agenticMapMarkers;
    if (!registry) return;
    const markers = [];
    for (const marker of registry) {
      const el = marker.getElement && marker.getElement();
      if (!el || !el.isConnected || !this.map.el.contains(el)) continue;
      if (el.matches('.am-map-route-badge, .am-map-route-head')
          || el.querySelector('.am-map-route-badge, .am-map-route-head')) continue;
      markers.push(marker);
    }
    const sig = markers.map((m) => {
      const p = m.getLngLat();
      return p.lng.toFixed(4) + ',' + p.lat.toFixed(4);
    }).join(';');
    if (!force && sig === this._markerSig) return;
    this._markerSig = sig;
    this._clearBillboards(this.markerBillboards);
    for (const marker of markers) {
      const source = marker.getElement();
      const clone = source.cloneNode(true);
      clone.style.transform = '';
      clone.style.position = 'static';
      clone.style.opacity = '';
      clone.style.pointerEvents = 'none';
      clone.classList.remove('maplibregl-marker', 'maplibregl-marker-anchor-bottom',
        'maplibregl-marker-anchor-center', 'maplibregl-marker-anchor-top');
      const p = marker.getLngLat();
      // The marker's own anchor decides which pixel is the geographic point:
      // 'bottom' pins anchor at the teardrop TIP (the tip-anchor contract
      // from the flat map holds on the globe too), everything else centres.
      const anchor = marker._anchor
        || (source.classList.contains('maplibregl-marker-anchor-bottom') ? 'bottom' : 'center');
      this._addBillboard(this.markerBillboards, p.lat, p.lng, clone, anchor);
    }
  }

  _addBillboard(list, lat, lon, inner, anchor) {
    const wrap = document.createElement('div');
    wrap.className = 'am-globe-billboard';
    wrap.style.display = 'none';
    wrap.appendChild(inner);
    this.billboardLayer.appendChild(wrap);
    list.push({
      vec: latLonToVec3(lat, lon, EARTH_RADIUS * 1.004),
      el: wrap,
      anchor: anchor === 'bottom' ? 'bottom' : 'center',
    });
  }

  _clearBillboards(list) {
    for (const billboard of list.splice(0)) billboard.el.remove();
  }

  /** Per-frame projection of every billboard, with the labels' limb rule:
   *  a point that has rounded the horizon disappears AT ONCE (hideAtOnce's
   *  reasoning) — the earth is physically in front of it. */
  _updateBillboards() {
    if (!this.badgeBillboards || !this.markerBillboards) return;
    const total = this.badgeBillboards.length + this.markerBillboards.length;
    if (!total) return;
    const camera = this.camera;
    const camDir = camera.position.clone().normalize();
    const horizon = EARTH_RADIUS / camera.position.length();
    const half = { x: this.width / 2, y: this.height / 2 };
    for (const list of [this.badgeBillboards, this.markerBillboards]) {
      for (const billboard of list) {
        const facing = billboard.vec.dot(camDir);
        if (facing <= horizon) { billboard.el.style.display = 'none'; continue; }
        const projected = billboard.vec.clone().project(camera);
        if (projected.z > 1) { billboard.el.style.display = 'none'; continue; }
        const x = (projected.x + 1) * half.x;
        const y = (1 - projected.y) * half.y;
        billboard.el.style.display = 'block';
        // 'bottom' puts the element's bottom-centre — the pin tip — on the
        // projected point; 'center' is the badge/ping convention.
        billboard.el.style.transform = 'translate(' + x + 'px,' + y + 'px) '
          + (billboard.anchor === 'bottom'
            ? 'translate(-50%, -100%)' : 'translate(-50%, -50%)');
      }
    }
  }

  /** Vertical ground span currently visible, in degrees — the inverse of the
   *  camera-distance solve, used to keep dragging proportional to the view. */
  visibleSpanDeg() {
    const distance = this.camera.position.length();
    const tanHalfFov = Math.tan((FOV / 2) * (Math.PI / 180));
    let low = 0, high = Math.PI / 2;
    for (let i = 0; i < 40; i++) {
      const mid = (low + high) / 2;
      if (Math.sin(mid) / (distance - Math.cos(mid)) > tanHalfFov) high = mid; else low = mid;
    }
    return low * 2 * 180 / Math.PI;
  }

  /** Drag to spin; the map centre follows so zooming back in is continuous. */
  _bindDrag() {
    let dragging = false, lastX = 0, lastY = 0;
    this.canvas.addEventListener('pointerdown', (event) => {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      this.canvas.setPointerCapture(event.pointerId);
    });
    this.canvas.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      const dx = event.clientX - lastX, dy = event.clientY - lastY;
      lastX = event.clientX; lastY = event.clientY;
      const center = this.mapbox.getCenter();
      // Degrees per pixel must follow the altitude: the drag should move the
      // ground under the cursor, not a fixed 180 deg per screen height. Close
      // in, the visible arc is a few degrees, and the old constant sent the
      // camera over the pole on a short drag.
      const perPixel = this.visibleSpanDeg() / Math.max(this.height, 1);
      this.mapbox.jumpTo({
        center: [
          ((center.lng - dx * perPixel + 540) % 360) - 180,
          Math.max(-82, Math.min(82, center.lat + dy * perPixel)),
        ],
      });
    });
    const end = (event) => {
      dragging = false;
      if (event.pointerId !== undefined && this.canvas.hasPointerCapture(event.pointerId)) {
        this.canvas.releasePointerCapture(event.pointerId);
      }
    };
    this.canvas.addEventListener('pointerup', end);
    this.canvas.addEventListener('pointercancel', end);
    // Wheel keeps zooming the underlying map, which is what pulls you back
    // into the flat view — one continuous gesture from orbit to street.
    this.canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      this.mapbox.zoomTo(this.mapbox.getZoom() - event.deltaY * 0.002, { duration: 0 });
    }, { passive: false });
  }
}

window.agenticMountGlobe = function mountGlobe(map, options) {
  return new DsGlobe(map.el, map, options);
};
export { DsGlobe, HANDOVER_ZOOM };

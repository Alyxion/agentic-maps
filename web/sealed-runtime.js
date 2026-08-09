/*
 * The sealed side of the offline contract, running inside a sealed session.
 *
 * A sealed session is one HTML file with no server behind it, so every road
 * that normally leads to one is closed here and a store is put in its place:
 *
 *   - MapLibre asks for tiles, glyphs and sprites through a registered
 *     `amap://` protocol. A registered protocol never reaches the network —
 *     the handler below answers from memory, and that is the whole traffic of
 *     a map.
 *   - The page's own `fetch` calls (routes, GeoJSON layers) are answered by a
 *     shim. Anything the shim does not recognise is REFUSED, not forwarded: a
 *     sealed session that quietly worked because the author happened to be
 *     online would be the one bug this whole mechanism exists to prevent. The
 *     runtime payload itself never goes through this shim at all — the sealed
 *     host writes it inline (`data-agentic-inline-spec`, the same convention
 *     `web/map.js` already supports), so `map.js` never asks for a spec over
 *     the network in the first place.
 *   - `XMLHttpRequest` is answered from the same table. MapLibre still reaches
 *     for it on some paths, and refusing it outright made a sealed session log
 *     an error for a resource it was holding all along — what has to be
 *     closed is the route to the network, not the API.
 *   - `sendBeacon`, `EventSource` and `WebSocket` have no store to answer
 *     from, so they are simply closed.
 *
 * WHAT IS NOT IN THE STORE. The recording holds the tiles a session's
 * choreography proved it displays, and a viewer's window is not the
 * authored one — a wider kiosk screen at the last step can reach a hair past
 * the recorded edge. Rather than pad the file with a ring of tiles for a
 * case that may never happen, the server's degradation ladder
 * (storage/fallback.py) is reproduced here: an absent raster tile is
 * answered by cropping and upscaling an ancestor. The picture goes soft at
 * the very edge instead of showing a hole, and it costs nothing in bytes.
 */
(function (global) {
  'use strict';

  var SCHEME = 'amap://';
  // How far up the pyramid a stand-in may be fetched from. Four zooms is a 16×
  // upscale — beyond that the crop is mush and showing nothing is more honest.
  var MAX_FALLBACK_STEPS = 4;
  var TILE_PX = 256;
  // This product's own tile/route mount point (`MapsApi.mount`'s default
  // `prefix`). `storeKeyFor` tries this first; a host mounted elsewhere, or a
  // page reaching for assets outside it, passes its own prefixes via
  // `options.assetPrefixes` at `install()` time — see below.
  var DEFAULT_MAP_PREFIX = '/api/v1/maps/';

  /** The bytes, decoded once and shared by every frame in the session. */
  function SealedStore(bundle) {
    this.bundle = bundle;
    this.index = bundle.index || {};
    this.mediaTypes = bundle.media_types || [];
    this.payload = bundle.payload || {};
    this.data = decodeBase64(bundle.data || '');
    this.routes = {};
    (bundle.routes || []).forEach(function (route) { this.routes[route.key] = route.response; }, this);
    this._blobUrls = {};
    // What this session was asked for and did not have.
    //
    // A recording goes stale the moment a page is edited — move the site and
    // every frozen route is keyed to the old coordinates. The pages treat a
    // failed route as "draw nothing", so the session loses its routes and
    // looks finished: no error, no gap, just a map with no lines on it. That
    // silence is the bug. Every denial is written down here so a
    // verification harness can fail on it instead of a viewer noticing
    // months later.
    this.misses = [];
  }

  SealedStore.prototype.miss = function (kind, what) {
    this.misses.push({ kind: kind, what: String(what).slice(0, 200) });
    if (this.misses.length <= 20 && typeof console !== 'undefined') {
      console.warn('[agentic-sealed] not in bundle (' + kind + '): ' + what);
    }
  };

  /** The index key for a URL path, whichever way it happens to be spelled.
   *
   *  Keys are recorded as the browser sent them, so a font stack arrives
   *  percent-encoded ("Noto%20Sans%20Regular"). MapLibre hands a custom
   *  protocol the template it built instead, spaces and all — the same
   *  resource under a different spelling. Both are accepted rather than
   *  picking one and hoping every caller agrees.
   */
  SealedStore.prototype.resolve = function (key) {
    if (Object.prototype.hasOwnProperty.call(this.index, key)) return key;
    try {
      var encoded = encodeURI(key);
      if (Object.prototype.hasOwnProperty.call(this.index, encoded)) return encoded;
      var decoded = decodeURI(key);
      if (Object.prototype.hasOwnProperty.call(this.index, decoded)) return decoded;
    } catch (error) { /* malformed escape — not ours */ }
    return null;
  };

  SealedStore.prototype.has = function (key) {
    return this.resolve(key) !== null;
  };

  /** {bytes, mediaType} for a key, or null. */
  SealedStore.prototype.get = function (key) {
    var entry = this.index[this.resolve(key)];
    if (!entry) return null;
    return {
      bytes: this.data.subarray(entry[0], entry[0] + entry[1]),
      mediaType: this.mediaTypes[entry[2]] || 'application/octet-stream',
    };
  };

  /** A stable blob: URL for a stored asset — for the few places that need a
   *  real URL (an <img>, a MapLibre image source) rather than bytes. */
  SealedStore.prototype.url = function (key) {
    if (!this._blobUrls[key]) {
      var hit = this.get(key);
      if (!hit) return null;
      this._blobUrls[key] = URL.createObjectURL(
        new Blob([hit.bytes], { type: hit.mediaType }));
    }
    return this._blobUrls[key];
  };

  /** The frozen answer for a routing request, by exact key.
   *
   *  Exact is right: a route IS its endpoints, and a near-enough substitute
   *  would draw a line that starts somewhere the page does not. The
   *  guarantee that the key still matches belongs to whatever produces the
   *  sealed file, which should refuse to ship a recording that no longer
   *  fits — not to a tolerance here that would quietly paper over the drift.
   *
   *  Reported from inside the store rather than at the caller: the store is
   *  what knows it was asked and came up empty, so every caller gets the
   *  bookkeeping for free.
   */
  SealedStore.prototype.route = function (body) {
    var hit = this.routes[routeKey(body)];
    if (hit) return hit;
    this.miss('route', routeKey(body));
    return null;
  };

  function decodeBase64(text) {
    var binary = global.atob(text);
    var out = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
    return out;
  }

  /** Must reproduce agentic_maps.seal.recorder.route_key byte for byte. */
  function routeKey(body) {
    body = body || {};
    var point = function (value) {
      value = value || {};
      return [Number(value.lat || 0).toFixed(6), Number(value.lon || 0).toFixed(6)];
    };
    return JSON.stringify({
      alternates: Number(body.alternates || 0),
      avoid: (body.avoid || []).slice().sort(),
      end: point(body.end),
      mode: body.mode || 'car',
      start: point(body.start),
      steps: !!body.steps,
      via: (body.via || []).map(point),
    });
  }

  // -- raster degradation ladder ---------------------------------------

  var RASTER = /^\/api\/v1\/maps\/live\/([^/]+)\/(\d+)\/(\d+)\/(\d+)$/;

  /** An ancestor tile, cropped to this tile's quadrant and blown back up.
   *
   *  Same rule as the server: walk up, take the first ancestor that exists,
   *  and cut the sub-rectangle this tile occupies inside it.
   */
  function fallbackTile(store, key) {
    var match = RASTER.exec(key);
    if (!match) return null;
    var source = match[1];
    var z = +match[2], x = +match[3], y = +match[4];
    for (var up = 1; up <= MAX_FALLBACK_STEPS && z - up >= 0; up++) {
      var pz = z - up;
      var px = x >> up, py = y >> up;
      var hit = store.get('/api/v1/maps/live/' + source + '/' + pz + '/' + px + '/' + py);
      if (!hit) continue;
      var span = 1 << up;                       // ancestor covers span² tiles
      return {
        hit: hit,
        sx: (x - (px << up)) * (TILE_PX / span),
        sy: (y - (py << up)) * (TILE_PX / span),
        size: TILE_PX / span,
      };
    }
    return null;
  }

  async function renderFallback(crop) {
    var source = await createImageBitmap(
      new Blob([crop.hit.bytes], { type: crop.hit.mediaType }));
    var canvas = new OffscreenCanvas(TILE_PX, TILE_PX);
    var ctx = canvas.getContext('2d');
    // The source tile may not be 256 px (retina bundles are not, and a
    // mosaic is stored at whatever it was merged at), so scale the crop
    // rectangle by the real bitmap size rather than assuming.
    var scale = source.width / TILE_PX;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(source, crop.sx * scale, crop.sy * scale,
                  crop.size * scale, crop.size * scale, 0, 0, TILE_PX, TILE_PX);
    source.close();
    var blob = await canvas.convertToBlob({ type: 'image/png' });
    return await blob.arrayBuffer();
  }

  // -- MapLibre protocol ------------------------------------------------

  function keyOf(url) {
    var path = url.slice(SCHEME.length);
    return path.charAt(0) === '/' ? path : '/' + path;
  }

  function toArrayBuffer(bytes) {
    // A fresh buffer, not a view onto the shared store: MapLibre transfers
    // tile buffers to a worker, and a transferred view would detach the
    // whole decoded bundle — every later tile in the session would come
    // back empty.
    return bytes.slice().buffer;
  }

  function installProtocol(maplibregl, store) {
    maplibregl.addProtocol('amap', async function (params) {
      var key = keyOf(params.url);
      var hit = store.get(key);
      if (hit) {
        if (params.type === 'json') {
          return { data: JSON.parse(new TextDecoder().decode(hit.bytes)) };
        }
        if (params.type === 'string') {
          return { data: new TextDecoder().decode(hit.bytes) };
        }
        return { data: toArrayBuffer(hit.bytes) };
      }
      // A cropped ancestor is the DESIGNED answer for a viewport just outside
      // the recording, not a failure — it is not counted as a miss.
      var crop = fallbackTile(store, key);
      if (crop) return { data: await renderFallback(crop) };
      // An empty vector tile is a real answer (ocean, a gap in the extract);
      // MapLibre draws nothing and stays quiet. Erroring here would paint the
      // console red on every pan past the recorded edge.
      if (key.slice(-4) === '.mvt') return { data: new ArrayBuffer(0) };
      store.miss('tile', key);
      throw new Error('[agentic-sealed] not in bundle: ' + key);
    });
  }

  // -- network shims ----------------------------------------------------

  function jsonResponse(value) {
    return new Response(JSON.stringify(value),
      { status: 200, headers: { 'content-type': 'application/json' } });
  }

  /** The one answer table. Everything that can ask for a resource asks here.
   *
   *  Returns {status, mediaType, bytes} or null when the session simply does
   *  not have it — null is a refusal, never a reason to reach out.
   */
  function answer(store, options, url, method, body) {
    var routePath = options.routePath || (DEFAULT_MAP_PREFIX + 'route');
    var encode = function (value) {
      return new TextEncoder().encode(JSON.stringify(value));
    };

    // Routing was done once, while editing. Here it is a lookup.
    if (url.indexOf(routePath) !== -1 && String(method).toUpperCase() === 'POST') {
      var parsed = {};
      try { parsed = JSON.parse(body || '{}'); } catch (error) { parsed = {}; }
      var frozen = store.route(parsed);
      // A route the recording never saw cannot be invented. Every page
      // treats a failed route as "draw nothing" — so this has to be SAID, or
      // the page simply comes up without its routes and nothing anywhere
      // complains. The key is logged because it is the diagnosis: a start
      // coordinate that no longer matches means the page moved since it was
      // recorded.
      if (!frozen) {
        return { status: 404, mediaType: 'text/plain', bytes: new Uint8Array(0) };
      }
      return { status: 200, mediaType: 'application/json', bytes: encode(frozen) };
    }

    var key = storeKeyFor(url, options);
    var hit = key && store.get(key);
    if (hit) return { status: 200, mediaType: hit.mediaType, bytes: hit.bytes };
    return null;
  }

  function installFetch(win, store, options) {
    win.fetch = function (input, init) {
      var url = String((input && input.url) || input || '');
      var method = (init && init.method) || (input && input.method) || 'GET';
      var found = answer(store, options, url, method, init && init.body);
      if (!found) {
        store.miss('fetch', url);
        return Promise.reject(new Error(
          '[agentic-sealed] refused: this session is sealed and has no network (' + url + ')'));
      }
      return Promise.resolve(new Response(found.bytes.slice(), {
        status: found.status, headers: { 'content-type': found.mediaType },
      }));
    };
  }

  /** XHR, answered from the store as well.
   *
   *  MapLibre still reaches for XMLHttpRequest on some paths, and refusing it
   *  outright made a sealed session log an error for a resource it was
   *  holding all along. What must be closed is the ROUTE TO THE NETWORK, not
   *  the API — so this serves the same table `fetch` does and fails
   *  everything else, never touching the transport either way.
   */
  function installXhr(win, store, options) {
    var Native = win.XMLHttpRequest;
    if (!Native) return;

    function SealedRequest() {
      this.readyState = 0;
      this.status = 0;
      this.response = null;
      this.responseText = '';
      this.responseType = '';
      this.timeout = 0;
      this.withCredentials = false;
      this._headers = {};
      this._listeners = {};
    }

    SealedRequest.UNSENT = 0;
    SealedRequest.OPENED = 1;
    SealedRequest.HEADERS_RECEIVED = 2;
    SealedRequest.LOADING = 3;
    SealedRequest.DONE = 4;

    SealedRequest.prototype.open = function (method, url) {
      this._method = method;
      this._url = String(url);
      this.readyState = 1;
    };
    SealedRequest.prototype.setRequestHeader = function (name, value) {
      this._headers[name] = value;
    };
    SealedRequest.prototype.getAllResponseHeaders = function () {
      return this._mediaType ? 'content-type: ' + this._mediaType + '\r\n' : '';
    };
    SealedRequest.prototype.getResponseHeader = function (name) {
      return String(name).toLowerCase() === 'content-type' ? (this._mediaType || null) : null;
    };
    SealedRequest.prototype.addEventListener = function (type, handler) {
      (this._listeners[type] = this._listeners[type] || []).push(handler);
    };
    SealedRequest.prototype.removeEventListener = function (type, handler) {
      var list = this._listeners[type] || [];
      var at = list.indexOf(handler);
      if (at !== -1) list.splice(at, 1);
    };
    SealedRequest.prototype.abort = function () { this._aborted = true; };
    SealedRequest.prototype.overrideMimeType = function () {};

    SealedRequest.prototype._emit = function (type) {
      var event = { type: type, target: this, currentTarget: this };
      var handler = this['on' + type];
      if (typeof handler === 'function') handler.call(this, event);
      (this._listeners[type] || []).forEach(function (fn) { fn.call(this, event); }, this);
    };

    SealedRequest.prototype.send = function (body) {
      var self = this;
      var found = answer(store, options, this._url, this._method, body);
      // Asynchronous like the real thing: callers routinely attach handlers
      // after send(), and a synchronous callback would run before they exist.
      win.setTimeout(function () {
        if (self._aborted) return;
        if (!found) {
          // Name it. An XHR error event carries no URL, so a resource the
          // recording missed showed up as a bare "Error" with no way to tell
          // which one — and the fix for a missing resource always starts
          // with knowing which one it was.
          store.miss('xhr', self._url);
          self.readyState = 4;
          self.status = 0;
          self._emit('error');
          self._emit('loadend');
          return;
        }
        self.status = found.status;
        self._mediaType = found.mediaType;
        self.readyState = 4;
        var bytes = found.bytes.slice();
        if (self.responseType === 'arraybuffer') {
          self.response = bytes.buffer;
        } else if (self.responseType === 'json') {
          self.response = JSON.parse(new TextDecoder().decode(bytes));
        } else if (self.responseType === 'blob') {
          self.response = new Blob([bytes], { type: found.mediaType });
        } else {
          self.responseText = new TextDecoder().decode(bytes);
          self.response = self.responseText;
        }
        self._emit('readystatechange');
        self._emit('load');
        self._emit('loadend');
      }, 0);
    };

    win.XMLHttpRequest = SealedRequest;
  }

  /** Host-relative key for a URL a page inside the frame asked for.
   *
   *  Frames are `srcdoc`, so a relative URL resolves against the sealed
   *  file's own location — which is a file:// path or a share URL, never the
   *  host the session was recorded from. The store is keyed by the recorded
   *  path, so that is what has to be recovered here.
   *
   *  `options.mapPrefix` (default `/api/v1/maps/`) and
   *  `options.assetPrefixes` (default none) mirror the same two knobs
   *  `agentic_maps.seal.recorder.SessionRecorder` was constructed with —
   *  they must agree, or a key recorded under one prefix will never resolve
   *  under another.
   */
  function storeKeyFor(url, options) {
    options = options || {};
    if (url.indexOf(SCHEME) === 0) return keyOf(url);
    var mapPrefix = options.mapPrefix || DEFAULT_MAP_PREFIX;
    var at = url.indexOf(mapPrefix);
    if (at !== -1) return url.slice(at);
    var prefixes = options.assetPrefixes || [];
    for (var i = 0; i < prefixes.length; i++) {
      at = url.indexOf(prefixes[i]);
      if (at !== -1) return url.slice(at);
    }
    if (url.charAt(0) === '/') return url;
    return null;
  }

  function sealWindow(win) {
    var refuse = function (what) {
      return function () {
        throw new Error('[agentic-sealed] ' + what + ' is disabled: this session has no network');
      };
    };
    if (win.navigator && win.navigator.sendBeacon) {
      win.navigator.sendBeacon = function () { return false; };
    }
    if (win.EventSource) win.EventSource = refuse('EventSource');
    if (win.WebSocket) win.WebSocket = refuse('WebSocket');
  }

  /** Everything a sealed frame needs, in the order it needs it. */
  function install(win, store, options) {
    options = options || {};
    installFetch(win, store, options);
    installXhr(win, store, options);
    sealWindow(win);
    if (win.maplibregl) installProtocol(win.maplibregl, store);
    // Page code can still hold a few plain URLs — an icon, a photo. Those go
    // into an <img> or a CSS url(), neither of which a MapLibre protocol can
    // serve, so they are resolved to blob: URLs pointing at the same stored
    // bytes. A page wanting this rewrites such a literal into a call to this
    // at seal time (analogous to how `sealed_url()` rewrites the runtime
    // payload's own template fields — see `agentic_maps/seal/sealer.py`).
    win.__agenticAssetUrl = function (key) {
      return store.url(key) || key;
    };
    win.__agenticSealedStore = store;
  }

  global.agenticSealed = {
    Store: SealedStore,
    install: install,
    installProtocol: installProtocol,
    routeKey: routeKey,
    storeKeyFor: storeKeyFor,
  };
})(typeof window !== 'undefined' ? window : this);

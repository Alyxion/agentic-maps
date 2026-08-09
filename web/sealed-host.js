/*
 * Builds the map frames of a sealed session, from its own decoded data.
 *
 * Online, a page embeds `<iframe src="/view.html?…">` and a server answers
 * it. A sealed session is one file and has no server, so the same iframe
 * becomes a `srcdoc` frame this script fills in.
 *
 * Why `srcdoc` and not a blob: URL — the frames must be SAME-ORIGIN with the
 * sealed file. They share one decoded tile store (a session can embed several
 * maps; one copy of the bytes is the difference between a file you can mail
 * and one you cannot), and sharing means direct property access. A blob: URL
 * loaded from a `file://` sealed file gets an opaque origin and that access
 * is denied. `srcdoc` inherits the file's origin, so it works from a USB
 * stick and from a share link alike.
 *
 * THE LIFECYCLE IS THE CALLER'S. This has no opinion about when a map is
 * shown, hidden or torn down — nothing in agentic-maps itself embeds one
 * sealed page inside another as nested frames of a larger document, so
 * there is no host-authored "current step" script to defer to. Whatever
 * assembles the final sealed HTML (a build step, or a page written by hand)
 * calls `build(frame, pageName, query)` once per frame it wants populated,
 * and `teardown(frame)` when it is done with one — ordinary, explicit calls,
 * not a protocol inferred by watching an attribute change.
 */
(function (global) {
  'use strict';

  var BOOT = '<!doctype html><meta charset="utf-8">'
    + '<script>window.parent.__agenticSealedMaps.attach(window)<\/script>';

  /** One sealed session's worth of map frames, backed by one decoded store.
   *
   *  `bundle` is a `SealedBundle.model_dump()` (`agentic_maps/seal/sealer.py`,
   *  `bundle_json()`); `web` is a `SealedWeb.model_dump()`
   *  (`agentic_maps/seal/page_seal.py`); `runtimeSource` is the text of this
   *  session's own copy of `sealed-runtime.js`, inlined into every frame so
   *  the store shim is installed before anything else in the frame runs.
   */
  function AgenticSealedHost(bundle, web, runtimeSource) {
    this.store = new global.agenticSealed.Store(bundle);
    this.web = web;
    this.runtimeSource = runtimeSource;
    this._pending = Object.create(null);
    this._nextSlot = 0;
    // One store, shared by every frame — so the session itself is where the
    // complete picture lives: every frame's denials accumulate in one
    // `misses` list a verification harness (or a curious console) can read
    // without walking iframes.
    global.__agenticSealedStore = this.store;
  }

  /** Build one sealed embed into `frame` (an <iframe> already in the DOM),
   *  showing `pageName` (a key of `web.pages`, e.g. "view.html") with the
   *  query string a live embed would have carried (no leading "?").
   *
   *  Resolves once the map inside has actually settled
   *  (`window.__agenticMapsReady()` true) or `readyTimeoutMs` elapses —
   *  never rejects, so a caller populating several frames in a `Promise.all`
   *  is not sunk by one slow map.
   */
  AgenticSealedHost.prototype.build = function (frame, pageName, query, readyTimeoutMs) {
    var page = this.web.pages[pageName];
    if (!page) return Promise.reject(new Error('[agentic-sealed] unknown page: ' + pageName));
    var self = this;
    var slot = String(this._nextSlot++);
    frame.dataset.agenticSealSlot = slot;
    return new Promise(function (resolve) {
      self._pending[slot] = {
        page: page, query: query || '', resolve: resolve,
        readyTimeoutMs: readyTimeoutMs || 12000,
      };
      // A fresh srcdoc value always (re)navigates the frame, even to the
      // same string — this triggers `attach` below exactly once per build().
      frame.srcdoc = BOOT + '<!--' + slot + '-->';
    });
  };

  /** Called by each frame's own bootstrap as soon as its document runs. */
  AgenticSealedHost.prototype._attach = function (win) {
    var frame = win.frameElement;
    var slot = frame && frame.dataset.agenticSealSlot;
    var job = slot != null && this._pending[slot];
    if (!job) return;
    delete this._pending[slot];
    this._render(frame, win, job);
  };

  AgenticSealedHost.prototype._render = function (frame, win, job) {
    var page = job.page;
    var doc = win.document;

    doc.open();
    doc.write('<!doctype html><html><head><meta charset="utf-8">'
      // Belt to the shim's braces: even if some code found a way past the
      // refusing fetch/XHR, the frame is not permitted to open a connection.
      + '<meta http-equiv="Content-Security-Policy" content="'
      + "default-src 'self' data: blob: 'unsafe-inline' 'unsafe-eval'; "
      + "connect-src 'none'; img-src data: blob:\">"
      + '</head><body></body></html>');
    doc.close();

    page.style_refs.forEach(function (ref) { addStyle(doc, this.web.stylesheets[ref]); }, this);
    page.styles.forEach(function (css) { addStyle(doc, css); });
    doc.body.innerHTML = page.body;

    // The runtime payload rides inside the map element, using the SAME
    // `data-agentic-inline-spec` convention `web/map.js` already supports
    // for a live page — `loadPayload()` finds this before it would ever
    // fall back to fetching `data-agentic-spec-url`, so a sealed map never
    // asks the network for its spec at all.
    var mapHost = doc.querySelector('[data-agentic-map]');
    if (mapHost) {
      var json = doc.createElement('script');
      json.type = 'application/json';
      json.setAttribute('data-agentic-inline-spec', '');
      json.textContent = JSON.stringify(this.store.payload);
      mapHost.appendChild(json);
    }

    win.__agenticEmbedParams = job.query;
    // Order matters and is the page's own: the sealed runtime first (it must
    // be in place before anything can reach for the network), then MapLibre
    // and friends, then the page's own inline code.
    addScript(doc, this.runtimeSource);
    global.agenticSealed.install(win, this.store, {});
    page.scripts.forEach(function (entry) {
      addScript(doc, entry[0] === 'lib' ? this.web.libraries[entry[1]] : entry[1]);
      // MapLibre arrives mid-run; the protocol has to be registered on it
      // before the page's own script builds a map from an amap:// style.
      if (entry[0] === 'lib' && win.maplibregl && !win.__agenticProtocolReady) {
        win.__agenticProtocolReady = true;
        global.agenticSealed.installProtocol(win.maplibregl, this.store);
      }
    }, this);

    this._waitReady(frame, win, job);
  };

  /** Resolve the caller's promise once the frame's own map has settled.
   *
   *  Polls `window.__agenticMapsReady()` — the SAME contract
   *  `agentic_maps/seal/recorder.py` waited on while recording, so a frame
   *  that "was ready enough to seal" is exactly the frame that is "ready
   *  enough to show" here. No bespoke tile/style bookkeeping is reimplemented
   *  on this side of the seal.
   */
  function pollReady(win, timeoutMs, done) {
    var waited = 0;
    var poll = win.setInterval(function () {
      waited += 50;
      var ready = false;
      try { ready = !!(win.__agenticMapsReady && win.__agenticMapsReady()); }
      catch (error) { ready = false; }
      if (ready || waited >= timeoutMs) {
        win.clearInterval(poll);
        done();
      }
    }, 50);
  }

  AgenticSealedHost.prototype._waitReady = function (frame, win, job) {
    pollReady(win, job.readyTimeoutMs, function () { job.resolve(frame); });
  };

  /** Release a frame's map and give it a clean, empty document again. */
  AgenticSealedHost.prototype.teardown = function (frame) {
    var win = frame.contentWindow;
    try {
      var maps = win && win.agenticMaps;
      if (maps) maps.forEach(function (instance) { instance.map.remove(); });
    } catch (error) { /* frame already gone */ }
    frame.srcdoc = '<!doctype html><meta charset="utf-8">';
    delete frame.dataset.agenticSealSlot;
  };

  function addStyle(doc, css) {
    if (!css) return;
    var node = doc.createElement('style');
    node.textContent = css;
    doc.head.appendChild(node);
  }

  function addScript(doc, code) {
    if (!code) return;
    var node = doc.createElement('script');
    // .textContent, not a src or a document.write of the source: the library
    // text lives once in the file and is handed to each frame by reference.
    node.textContent = code;
    doc.head.appendChild(node);
  }

  global.AgenticSealedHost = AgenticSealedHost;
  // The one instance a sealed file's bootstrap script talks to.
  global.__agenticSealedMaps = {
    host: null,
    /** Create the host for this session's data. Call once, before the first
     *  `host.build(...)` — every frame's bootstrap script (`BOOT` above)
     *  reaches back through `attach` below, which only has something to do
     *  once this has run. */
    boot: function (bundle, sealedWeb, runtimeSource) {
      this.host = new AgenticSealedHost(bundle, sealedWeb, runtimeSource);
      return this.host;
    },
    attach: function (win) {
      if (this.host) this.host._attach(win);
    },
  };
})(window);

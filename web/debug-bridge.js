/*
 * Remote debugging bridge — lets an agent see this page from outside.
 *
 * Several bugs in this subproject only existed on a real machine: the shield
 * 9-patch distorting at one device pixel ratio, the globe silhouette wobbling
 * at 1400 px, labels piling into a corner. Headless reproduction failed
 * because the environment WAS the variable. This reports what the page
 * actually sees — console, uncaught errors, live map and globe state — and
 * answers evaluate requests.
 *
 * Only connects when the server says the surface is switched on
 * (AGENTIC_MAPS_DEBUG=1); otherwise the socket is never opened. See
 * agentic_maps/rest/frontend_debug.py for the security posture.
 */
(function () {
  'use strict';

  const ENDPOINT = '/api/v1/maps/debug/ws';
  const RETRY_MS = 4000;

  function mapState() {
    const wrapper = (window.agenticMaps || [])[0];
    if (!wrapper || !wrapper.map) return { attached: false };
    const map = wrapper.map;
    const centre = map.getCenter();
    const style = map.getStyle() || {};
    const layers = style.layers || [];
    const streets = map.getSource && map.getSource('streets');
    return {
      attached: true,
      view: wrapper.view,
      lang: wrapper.lang,
      centre: { lat: +centre.lat.toFixed(5), lon: +centre.lng.toFixed(5) },
      zoom: +map.getZoom().toFixed(2),
      bearing: +map.getBearing().toFixed(1),
      pitch: +map.getPitch().toFixed(1),
      hash: location.hash,
      // The two numbers behind most "why is it grey" questions.
      vectorSourceMaxZoom: streets ? streets.maxzoom : null,
      tilesLoaded: map.areTilesLoaded ? map.areTilesLoaded() : null,
      styleLoaded: map.isStyleLoaded ? map.isStyleLoaded() : null,
      layerCount: layers.length,
      shieldImages: (map.listImages ? map.listImages() : [])
        .filter((id) => id.indexOf('am-shield-') === 0).sort(),
    };
  }

  function globeState() {
    const globe = window.agenticGlobe;
    if (!globe) return { attached: false };
    const labels = [...document.querySelectorAll('.am-globe-label')]
      .filter((el) => getComputedStyle(el).display !== 'none');
    return {
      attached: true,
      visible: globe.visible,
      // Device pixels are the unit that matters for tessellation and for the
      // aliasing questions this bridge exists to answer.
      cssSize: [globe.width, globe.height],
      pixelRatio: globe.renderer ? globe.renderer.getPixelRatio() : null,
      devicePixelRatio: window.devicePixelRatio,
      sphereSegments: globe._segments,
      visibleSpanDeg: globe.visibleSpanDeg ? +globe.visibleSpanDeg().toFixed(2) : null,
      labelsOnScreen: labels.length,
      labelsByKind: ['country', 'city', 'ocean'].reduce((out, kind) => {
        out[kind] = labels.filter((el) =>
          el.classList.contains('am-globe-label--' + kind)).length;
        return out;
      }, {}),
    };
  }

  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket;
    try {
      socket = new WebSocket(scheme + '//' + location.host + ENDPOINT);
    } catch (error) {
      return;
    }

    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({ type: 'location', url: location.href }));
    });

    socket.addEventListener('message', (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (error) { return; }
      if (message.type === 'state') {
        socket.send(JSON.stringify({
          type: 'reply', id: message.id,
          value: { map: mapState(), globe: globeState(), url: location.href },
        }));
      } else if (message.type === 'evaluate') {
        let value;
        try {
          // Indirect eval: expressions are evaluated in global scope, so
          // `window.agenticMaps[0]` means what the caller expects it to mean.
          value = (0, eval)(message.expression);
          // Structured-clone what we can; fall back to a readable string.
          value = JSON.parse(JSON.stringify(value === undefined ? null : value));
        } catch (error) {
          value = { error: String(error && error.message || error) };
        }
        socket.send(JSON.stringify({ type: 'reply', id: message.id, value }));
      }
    });

    // Reconnect quietly: the server restarts often during development, and a
    // bridge that gives up after the first restart is a bridge nobody trusts.
    socket.addEventListener('close', () => setTimeout(connect, RETRY_MS));
    socket.addEventListener('error', () => socket.close());

    const send = (type, payload) => {
      if (socket.readyState === WebSocket.OPEN) {
        try { socket.send(JSON.stringify({ type, ...payload })); } catch (error) { /* dropped */ }
      }
    };

    for (const level of ['log', 'info', 'warn', 'error']) {
      const original = console[level];
      console[level] = function (...args) {
        original.apply(console, args);
        send('console', {
          level,
          text: args.map((a) => {
            try { return typeof a === 'string' ? a : JSON.stringify(a); }
            catch (error) { return String(a); }
          }).join(' ').slice(0, 2000),
        });
      };
    }
    window.addEventListener('error', (event) => send('error', {
      text: String(event.message), source: event.filename, line: event.lineno,
      stack: event.error && event.error.stack ? String(event.error.stack).slice(0, 2000) : '',
    }));
    window.addEventListener('unhandledrejection', (event) => send('error', {
      text: 'unhandled rejection: ' + String(event.reason && event.reason.message || event.reason),
      stack: event.reason && event.reason.stack ? String(event.reason.stack).slice(0, 2000) : '',
    }));
  }

  // Ask first: no socket at all unless the server has the surface enabled.
  fetch('/api/v1/maps/debug/enabled')
    .then((response) => (response.ok ? response.json() : { enabled: false }))
    .then((state) => { if (state.enabled) connect(); })
    .catch(() => {});
})();

// ============================================================================
// tracker.js — Teal Deal's tiny web-analytics tracker.
//
// Fires pageview / add_to_cart / purchase events at the collector. Keeps a
// durable anonymous visitor_id and a 30-minute session_id in localStorage, and
// prefers navigator.sendBeacon so events survive page unloads.
//
// Usage in a page:
//   <script src="tracker.js" data-collector="http://localhost:8787/collect"></script>
//   tealDeal.track('add_to_cart', { product_id: 42 });
// A pageview fires automatically on load.
// ============================================================================
(function () {
  const script = document.currentScript;
  const COLLECTOR = (script && script.dataset.collector) || 'http://localhost:8787/collect';
  const SESSION_TIMEOUT_MS = 30 * 60 * 1000;

  const uuid = () => crypto.randomUUID();

  function visitorId() {
    let v = localStorage.getItem('teal_deal_visitor');
    if (!v) { v = uuid(); localStorage.setItem('teal_deal_visitor', v); }
    return v;
  }

  function sessionId() {
    const now = Date.now();
    const last = +localStorage.getItem('teal_deal_session_ts') || 0;
    let sid = localStorage.getItem('teal_deal_session');
    if (!sid || now - last > SESSION_TIMEOUT_MS) sid = uuid();   // new session on timeout
    localStorage.setItem('teal_deal_session', sid);
    localStorage.setItem('teal_deal_session_ts', String(now));
    return sid;
  }

  function send(event) {
    // sendBeacon survives unload, unlike fetch.
    navigator.sendBeacon(COLLECTOR, new Blob([JSON.stringify(event)], { type: 'application/json' }));
  }

  function track(eventType, props = {}) {
    send({
      event_type: eventType,
      product_id: props.product_id ?? null,
      user_id: props.user_id ?? null,           // set once a visitor logs in
      event_ts: new Date().toISOString(),
      visitor_id: visitorId(),
      session_id: sessionId(),
      props: {
        path: location.pathname,
        referrer: document.referrer || 'direct',
        device: matchMedia('(max-width: 640px)').matches ? 'mobile' : 'desktop',
        ...props.extra,
      },
    });
  }

  window.tealDeal = { track, visitorId: visitorId() };
  // Auto pageview once the tab is interactive.
  if (document.readyState !== 'loading') track('pageview');
  else document.addEventListener('DOMContentLoaded', () => track('pageview'));
})();

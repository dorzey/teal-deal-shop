// ============================================================================
// tracker.js — Teal Deal's tiny web-analytics tracker.
//
// Fires pageview / add_to_cart / purchase events at the collector. Keeps a
// random per-tab session_id in sessionStorage (gone when the tab closes; no
// cookies, nothing kept across visits), and prefers navigator.sendBeacon so
// events survive page unloads.
//
// Usage in a page:
//   <script src="tracker.js" data-collector="http://localhost:8787/collect"></script>
//   tealDeal.track('add_to_cart', { product_id: 42 });
// A pageview fires automatically on load.
// ============================================================================
(function () {
  const script = document.currentScript;
  const COLLECTOR = (script && script.dataset.collector) || 'http://localhost:8787/collect';
  const uuid = () => crypto.randomUUID();

  // Per tab, never persisted across visits. If storage is blocked, one id per page load.
  // earlier versions kept a durable id in localStorage; clear it from returning browsers
  try { ['teal_deal_visitor', 'teal_deal_session', 'teal_deal_session_ts'].forEach(k => localStorage.removeItem(k)); } catch {}

  let memo;
  function sessionId() {
    try {
      let sid = sessionStorage.getItem('teal_deal_session');
      if (!sid) { sid = uuid(); sessionStorage.setItem('teal_deal_session', sid); }
      return sid;
    } catch { return memo || (memo = uuid()); }
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
      session_id: sessionId(),
      props: {
        path: location.pathname,
        referrer: document.referrer || 'direct',
        device: matchMedia('(max-width: 640px)').matches ? 'mobile' : 'desktop',
        ...props.extra,
      },
    });
  }

  window.tealDeal = { track, sessionId: sessionId() };
  // Auto pageview once the tab is interactive.
  if (document.readyState !== 'loading') track('pageview');
  else document.addEventListener('DOMContentLoaded', () => track('pageview'));
})();

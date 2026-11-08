// X-ray mode, shared by the storefront and the dashboard.
//
// The 🩻 button reveals which part of DuckDB powers what: a legend under the
// header, small links beside each feature (elements marked data-xray="…"), a tag
// on each row of the live-query drawer — and the ducks turn into x-rays (xray.css).
// Off by default; the choice is remembered, and ?xray turns it on from a link.
//
// The pages report what actually loaded (tealXray.set({ fts: true, … })) so the
// labels say what is really running — e.g. "ILIKE" when fts could not load.
(() => {
  // Every outbound link lives here; `make check-links` requests each one.
  const LINKS = {
    wasm:     'https://duckdb.org/docs/stable/clients/wasm/overview',
    haybarn:  'https://github.com/Query-farm-haybarn/haybarn-wasm',
    parquet:  'https://duckdb.org/docs/stable/data/parquet/overview',
    fts:      'https://duckdb.org/docs/stable/core_extensions/full_text_search',
    vss:      'https://duckdb.org/docs/stable/core_extensions/vss',
    duckpgq:  'https://duckdb.org/community_extensions/extensions/duckpgq',
    nearest:  'https://duckdb.org/docs/stable/sql/query_syntax/from',
    triggers: 'https://duckdb.org/docs/stable/sql/statements/create_trigger',
    ducklake: 'https://ducklake.select/',
    quack:    'https://duckdb.org/quack',
  };

  const state = {};   // reported by the pages: fts / vss / quack = true | false | undefined (still loading)

  const link = (key, text, cls = '') =>
    `<a class="xchip ${cls}" href="${LINKS[key]}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  const plain = (text, cls = '') => `<span class="xchip ${cls}">${text}</span>`;
  const ftsChip = s => s.fts === false ? plain('ILIKE · fts not loaded', 'warn')
                     : s.fts ? link('fts', 'fts · BM25') : plain('fts loading…', 'dim');
  const quackChip = s => s.quack === false ? plain('Quack not connected', 'warn') : link('quack', 'Quack');

  // The labels beside each feature: [data-xray="key"] placeholders.
  const TAGS = {
    search:   s => link('wasm', 'DuckDB-Wasm') + link('parquet', 'parquet') + ftsChip(s),
    similar:  () => link('vss', 'vss · array_distance'),
    hybrid:   () => link('duckpgq', 'DuckPGQ') + link('nearest', 'NEAREST join') + plain('server-side', 'dim'),
    collab:   () => plain('plain SQL on oltp.duckdb') + plain('server-side', 'dim'),
    checkout: () => plain('oltp.duckdb') + plain('transaction') + link('triggers', 'triggers'),
    lake:     s => link('haybarn', 'DuckDB-Wasm') + quackChip(s) + link('ducklake', 'DuckLake') + plain('read-only', 'dim'),
    oltp:     () => plain('oltp.duckdb') + plain('via the collector', 'dim'),
  };

  // The legend under the header: [chip, what it does here].
  const BAR = {
    storefront: s => [
      [link('wasm', 'DuckDB-Wasm'), 'runs in your browser. Search, filters and sorting are SQL over the catalog.'],
      [link('parquet', 'Parquet'), 'the 60-duck catalog is a Parquet file the browser queries directly.'],
      [ftsChip(s), s.fts === false ? 'could not load, so search falls back to a plain substring match.'
                                   : 'ranked text search over names and descriptions.'],
      [link('vss', 'vss'), '“Ducks like this”: the nearest ducks by vector distance, in your browser.'],
      [link('duckpgq', 'DuckPGQ') + link('nearest', 'NEAREST'), '“Since you brought them home”: a co-purchase graph walk plus similarity, server-side.'],
      [link('triggers', 'Triggers'), 'checkout is a SQL transaction on oltp.duckdb; triggers audit stock and top up the shelves.'],
      [link('ducklake', 'DuckLake'), 'everything you click is tracked into a lake — watch it on <a href="./dashboard.html">Duck HQ</a>.'],
      [plain('live queries'), 'the drawer below logs every query this page runs, tagged by feature.'],
    ],
    dashboard: s => [
      [link('haybarn', 'DuckDB-Wasm'), 'runs in your browser (Query Farm’s Haybarn build) and queries the lake directly — no API in between.'],
      [quackChip(s), s.quack === false ? 'could not connect, so the lake charts are waiting.'
                                       : 'DuckDB’s client–server protocol: the browser attaches to the server over HTTPS, read-only.'],
      [link('ducklake', 'DuckLake'), 'the event lake — snapshots and compaction. Funnel, retention and daily activity are SQL over it.'],
      [plain('oltp.duckdb'), 'revenue and order totals come from the shop’s transactional database, through the collector.'],
    ],
  };

  // Tag for a live-query row, by what the SQL is doing.
  const RULES = [
    [/match_bm25|fts_main_|create_fts_index/i, 'fts'],
    [/array_distance|array_cosine|product_vectors|_tokens|_buckets|hnsw/i, 'vss'],
    [/read_parquet|\.parquet/i, 'parquet'],
    [/^\s*(install|load)\b/i, 'extension'],
  ];
  const tag = sql => {
    for (const [re, name] of RULES) if (re.test(sql)) return `<span class="qtag">${name}</span>`;
    return '<span class="qtag none"></span>';   // keeps the SQL column aligned
  };

  const KEY = 'teal_deal_xray';
  const body = () => document.body;
  const button = () => document.getElementById('xrayBtn');

  // `onlyEmpty`: fill just the placeholders the pages built since last time. The
  // observer below uses it, so rendering never re-triggers itself.
  function render(onlyEmpty = false) {
    document.querySelectorAll('[data-xray]').forEach(el => {
      const make = TAGS[el.dataset.xray];
      if (make && !(onlyEmpty && el.innerHTML)) el.innerHTML = make(state);
    });
    if (onlyEmpty) return;
    const bar = document.getElementById('xrayBar');
    if (bar) {
      const items = (BAR[bar.dataset.page] || (() => []))(state);
      bar.innerHTML = '<div class="xraybar-title">🩻 x-ray · what is DuckDB doing here?</div><ul>' +
        items.map(([chip, text]) => `<li><span class="xchips">${chip}</span> ${text}</li>`).join('') + '</ul>';
    }
  }

  function setOn(on, remember = true) {
    body().classList.toggle('xray', on);
    const b = button();
    if (b) b.setAttribute('aria-pressed', String(on));   // the label stays put; the button lights up instead
    if (remember) { try { localStorage.setItem(KEY, on ? '1' : '0'); } catch {} }
  }

  function init() {
    let saved = null;
    try { saved = localStorage.getItem(KEY); } catch {}
    setOn(new URLSearchParams(location.search).has('xray') || saved === '1', false);
    const b = button();
    if (b) b.addEventListener('click', () => setOn(!body().classList.contains('xray')));
    render();
    // labels inside things the pages build later (cards, recommendation banners)
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        if (document.querySelector('[data-xray]:empty')) render(true);
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  // html(key): the label markup itself, for templates that re-render often (the
  // dashboard every 4 s, the cards on each keystroke) so it never flashes in late.
  window.tealXray = {
    set(patch) { Object.assign(state, patch); render(); },
    html: key => (TAGS[key] ? TAGS[key](state) : ''),
    tag, links: LINKS,
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

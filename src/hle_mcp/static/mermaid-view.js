/* mermaid-view.js — Mermaid の描画を隔離フレームに頼み、結果を受け取る。
 *
 * Mermaid のライブラリ本体は、エディタのページではなく隔離フレーム
 * （/static/mermaid-frame.html。sandbox="allow-scripts" のみ = 不透明オリジン）
 * の中でだけ動かす。本文の Mermaid 記法は外部由来になりうる文字列で、
 * ライブラリには HTML / CSS インジェクションの脆弱性が繰り返し出ているため、
 * エディタのページ（API を叩けるオリジン）から切り離す。
 *
 * このページが受け取るのは、整形式であることをフレームが確かめた SVG 文字列
 * だけで、DOM には入れず <img> の data: URL にして見せる（<img> の中の SVG は
 * スクリプトも外部の読み込みも動かない）。フレームは最初の描画の依頼が
 * あるまで作らない（Mermaid の無い文書ではライブラリを読み込まない）。
 *
 * 結果: {ok:true, url, width, height} | {ok:false, error}
 */
(function () {
  'use strict';

  const FRAME_URL = '/static/mermaid-frame.html';
  const READY_TIMEOUT = 20000;   // フレームの起動（ライブラリの読み込み）
  const RENDER_TIMEOUT = 30000;  // 1枚の描画
  const MAX_SVG_CHARS = 4000000; // これを超える SVG は data: URL にしない
  const MAX_DIM = 20000;
  const CACHE_MAX = 120;

  let frame = null;
  let readyPromise = null;
  let onReady = null;
  let onFailed = null;
  let nextId = 1;
  const pending = new Map();  // 依頼番号 → {resolve, timer}
  const inflight = new Map(); // 鍵 → Promise（同じ図の二重依頼を1本にする）
  const done = new Map();     // 鍵 → 結果（同期で引ける。古い順に捨てる）

  function currentTheme() {
    return window.HleTheme ? window.HleTheme.current() : 'light';
  }

  function keyOf(source, theme) {
    return theme + '\u0000' + source;
  }

  function teardown() {
    if (frame) frame.remove();
    frame = null;
    readyPromise = null;
    onReady = null;
    onFailed = null;
  }

  function ensureFrame() {
    if (readyPromise) return readyPromise;
    readyPromise = new Promise((resolve, reject) => {
      const el = document.createElement('iframe');
      el.className = 'mmd-frame';
      // allow-same-origin を付けない = 不透明オリジン。このフレームの中の
      // スクリプトは、エディタのページの DOM・fetch（API）に触れない
      el.setAttribute('sandbox', 'allow-scripts');
      el.setAttribute('aria-hidden', 'true');
      el.setAttribute('tabindex', '-1');
      el.title = 'Mermaid 描画用（非表示）';
      el.inert = true;
      const timer = setTimeout(() => {
        reject(new Error('Mermaid の描画フレームが起動しませんでした'));
      }, READY_TIMEOUT);
      onReady = () => { clearTimeout(timer); resolve(); };
      onFailed = (err) => { clearTimeout(timer); reject(err); };
      el.src = FRAME_URL;
      frame = el;
      document.body.appendChild(el);
    });
    // 失敗したらフレームを捨てる（次の依頼で作り直す）
    readyPromise.catch(teardown);
    return readyPromise;
  }

  /* フレームから届いた描画結果を検査して、このページが使う形にする。
     フレームの中身は信用せず、型・大きさ・形を確かめてから使う */
  function toResult(d) {
    if (d.ok === true) {
      const w = d.width;
      const h = d.height;
      const sizeOk = Number.isFinite(w) && Number.isFinite(h) &&
        w >= 1 && h >= 1 && w <= MAX_DIM && h <= MAX_DIM;
      if (typeof d.svg !== 'string' || !sizeOk ||
          !/^<svg[\s>]/.test(d.svg)) {
        return { ok: false, error: '描画結果の形式が正しくありません' };
      }
      if (d.svg.length > MAX_SVG_CHARS) {
        return { ok: false, error: '図が大きすぎて表示できません' };
      }
      return {
        ok: true,
        url: 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(d.svg),
        width: w,
        height: h,
      };
    }
    return { ok: false, error: String(d.error || '不明なエラー').slice(0, 600) };
  }

  function remember(key, result) {
    done.delete(key);
    done.set(key, result);
    while (done.size > CACHE_MAX) done.delete(done.keys().next().value);
  }

  window.addEventListener('message', (ev) => {
    if (!frame || ev.source !== frame.contentWindow) return;
    const d = ev.data;
    if (!d || typeof d !== 'object') return;
    if (d.kind === 'ready') {
      if (onReady) onReady();
    } else if (d.kind === 'failed') {
      if (onFailed) onFailed(new Error(String(d.error || '').slice(0, 300)));
    } else if (d.kind === 'rendered') {
      const entry = pending.get(d.id);
      if (!entry) return;
      pending.delete(d.id);
      clearTimeout(entry.timer);
      entry.resolve(toResult(d));
    }
  });

  /* 描画を依頼する。同じ（ソース, 配色）はキャッシュから返す。
     失敗のうち、起動や時間切れのような一時的なものは覚えない
     （構文エラーは同じ入力なら同じ結果なので覚える） */
  function render(source, theme) {
    const key = keyOf(source, theme);
    if (done.has(key)) return Promise.resolve(done.get(key));
    if (inflight.has(key)) return inflight.get(key);
    const job = ensureFrame().then(() => new Promise((resolve) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        resolve({ ok: false, error: '描画がタイムアウトしました', transient: true });
      }, RENDER_TIMEOUT);
      pending.set(id, { resolve: resolve, timer: timer });
      frame.contentWindow.postMessage(
        { kind: 'render', id: id, source: source, theme: theme }, '*');
    }), (err) => ({ ok: false, error: err.message, transient: true }));
    inflight.set(key, job);
    job.then((res) => {
      inflight.delete(key);
      if (!res.transient) remember(key, res);
    });
    return job;
  }

  /* キャッシュに結果があれば同期で返す（undo の直後に図を一瞬も空にしないため） */
  function peek(source, theme) {
    return done.get(keyOf(source, theme)) || null;
  }

  window.MermaidView = { theme: currentTheme, render: render, peek: peek };
})();

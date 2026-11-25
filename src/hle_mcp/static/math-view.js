/* math-view.js — 数式の描画を隔離フレームに頼み、結果を受け取る。
 *
 * MathJax 本体は、エディタのページではなく隔離フレーム（/static/math-frame.html。
 * sandbox="allow-scripts" のみ = 不透明オリジン）の中でだけ動かす。本文の TeX は
 * 外部由来になりうる文字列（AI や他者の文書をそのまま校正に出す）で、これを
 * 解釈するライブラリを、API を叩けるエディタのページから切り離すため。Mermaid の
 * 描画（mermaid-view.js）と同じ作り。
 *
 * このページが受け取るのは、整形式であることをフレームが確かめた SVG の文字列
 * だけで、DOM には入れず CSS の mask-image（data: URL）にして見せる。画像の中の
 * SVG はスクリプトも外部の読み込みも動かない。mask なので色は SVG ではなく
 * 文字色（currentColor）が決める（ライト・ダークの切り替えで描き直さずに済む）。
 * フレームは最初の描画の依頼があるまで作らない（数式の無い文書では、約 2MB の
 * ライブラリを読み込まない）。
 *
 * 結果: {ok:true, url, width, height, depth}（em） | {ok:false, error}
 */
(function () {
  'use strict';
  const FRAME_URL = '/static/math-frame.html';
  const READY_TIMEOUT = 20000;   // フレームの起動（ライブラリの読み込み）
  const RENDER_TIMEOUT = 15000;  // 1 つの数式（フォントの追加読み込みを含む）
  const MAX_SVG_CHARS = 1500000; // これを超える SVG は data: URL にしない
  const MAX_EM = 500;            // 幅・高さ（em）の上限
  const CACHE_MAX = 600;
  let frame = null;
  let readyPromise = null;
  let onReady = null;
  let onFailed = null;
  let nextId = 1;
  const pending = new Map();  // 依頼番号 → {resolve, timer}
  const inflight = new Map(); // 鍵 → Promise（同じ式の二重依頼を 1 本にする）
  const done = new Map();     // 鍵 → 結果（同期で引ける。古い順に捨てる）

  function keyOf(tex, display) {
    return (display ? 'D' : 'I') + '\u0000' + tex;
  }

  function teardown() {
    if (frame) frame.remove();
    frame = null;
    readyPromise = null;
    onReady = null;
    onFailed = null;
  }

  /* フレームを捨て、待っていた依頼をすべて一時的な失敗で返す（止まった
     フレームは、後ろの依頼も詰まらせる。次の依頼で作り直す） */
  function reset(message) {
    pending.forEach((entry) => {
      clearTimeout(entry.timer);
      entry.resolve({ ok: false, error: message, transient: true });
    });
    pending.clear();
    teardown();
  }

  function ensureFrame() {
    if (readyPromise) return readyPromise;
    readyPromise = new Promise((resolve, reject) => {
      const el = document.createElement('iframe');
      el.className = 'mth-frame';
      // allow-same-origin を付けない = 不透明オリジン。このフレームの中の
      // スクリプトは、エディタのページの DOM・fetch（API）に触れない
      el.setAttribute('sandbox', 'allow-scripts');
      el.setAttribute('aria-hidden', 'true');
      el.setAttribute('tabindex', '-1');
      el.title = '数式の描画用（非表示）';
      el.inert = true;
      const timer = setTimeout(() => {
        reject(new Error('数式の描画フレームが起動しませんでした'));
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

  function finite(n, lo, hi) {
    return typeof n === 'number' && Number.isFinite(n) && n >= lo && n <= hi;
  }

  /* フレームから届いた描画結果を検査して、このページが使う形にする。
     フレームの中身は信用せず、型・大きさ・形を確かめてから使う */
  function toResult(d) {
    if (d.ok === true) {
      const sizeOk = finite(d.width, 0.01, MAX_EM) &&
        finite(d.height, 0.01, MAX_EM) && finite(d.depth, -MAX_EM, MAX_EM);
      if (typeof d.svg !== 'string' || !sizeOk || !/^<svg[\s>]/.test(d.svg)) {
        return { ok: false, error: '描画結果の形式が正しくありません' };
      }
      if (d.svg.length > MAX_SVG_CHARS) {
        return { ok: false, error: '数式が大きすぎて表示できません' };
      }
      return {
        ok: true,
        url: 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(d.svg),
        width: d.width,
        height: d.height,
        depth: d.depth,
      };
    }
    return { ok: false, error: String(d.error || '不明なエラー').slice(0, 300) };
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

  /* 描画を依頼する。同じ（式, 表示の種類）はキャッシュから返す。失敗のうち、
     起動や時間切れのような一時的なものは覚えない（構文エラーは同じ入力なら
     同じ結果なので覚える） */
  function render(tex, display) {
    const key = keyOf(tex, display);
    if (done.has(key)) return Promise.resolve(done.get(key));
    if (inflight.has(key)) return inflight.get(key);
    const job = ensureFrame().then(() => new Promise((resolve) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        // 1 つの式で止まったフレームは、後ろの依頼も詰まらせる。捨てて作り直す
        reset('描画がタイムアウトしました');
      }, RENDER_TIMEOUT);
      pending.set(id, { resolve: resolve, timer: timer });
      frame.contentWindow.postMessage(
        { kind: 'render', id: id, tex: tex, display: !!display }, '*');
    }), (err) => ({ ok: false, error: err.message, transient: true }));
    inflight.set(key, job);
    job.then((res) => {
      inflight.delete(key);
      if (!res.transient) remember(key, res);
    });
    return job;
  }

  /* キャッシュに結果があれば同期で返す（undo の直後に数式を一瞬も空にしないため） */
  function peek(tex, display) {
    return done.get(keyOf(tex, display)) || null;
  }

  window.MathView = { render: render, peek: peek };
})();

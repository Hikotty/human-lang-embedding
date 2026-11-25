/* math-frame.js — MathJax を実行する隔離フレームの側のスクリプト。
 *
 * このフレームは親（校正エディタ）から postMessage で TeX を受け取り、描いた
 * SVG を文字列で返す。親は受け取った SVG を CSS の mask にして見せるだけで、
 * DOM には入れない（画像の中の SVG はスクリプトも外部の読み込みも動かない）。
 *
 * メッセージ:
 *   親 → 本フレーム  {kind:'render', id, tex, display:boolean}
 *   本フレーム → 親  {kind:'ready'}
 *                    {kind:'failed', error}
 *                    {kind:'rendered', id, ok:true, svg, width, height, depth}
 *                    {kind:'rendered', id, ok:false, error}
 *   width / height / depth は em（本文の文字の大きさに対する倍率）。depth は
 *   ベースラインより下に出る長さで、親が vertical-align に使う。
 */
(function () {
  'use strict';
  const PARENT = window.parent;
  const MAX_TEX = 20000;
  // MathJax の SVG の座標は、1em が 1000 単位
  const UNITS_PER_EM = 1000;
  let queue = Promise.resolve();

  function post(msg) {
    // こちらは不透明オリジンなので宛先は '*' になる。埋め込めるのは同一オリジンの
    // ページだけ（frame-ancestors 'self'）
    PARENT.postMessage(msg, '*');
  }

  function describe(err) {
    const raw = err && (err.message || err.str) ? (err.message || err.str) : err;
    return String(raw).replace(/\s+$/g, '').slice(0, 600);
  }

  /* TeX の構文エラーは、MathJax が <merror> の位置に赤い文言を描いた SVG として
     返す。それは数式ではないので、エラーとして取り出す */
  function texError(svg) {
    const bad = svg.querySelector('[data-mml-node="merror"]');
    if (!bad) return null;
    const msg = bad.getAttribute('data-mjx-error') ||
      (bad.querySelector('title') && bad.querySelector('title').textContent) ||
      bad.textContent || 'TeX の式を解釈できませんでした';
    return String(msg).replace(/\s+/g, ' ').trim().slice(0, 300);
  }

  /* MathJax が返した SVG を、親が CSS の mask にできる整形式の XML にする。
     幅と高さは viewBox の大きさにし、色は mask の形（アルファ）だけが使われる
     ので黒に固定する（親は background-color: currentColor で塗る）。 */
  function toMaskSvg(svg) {
    const vb = svg.viewBox && svg.viewBox.baseVal;
    if (!vb || !(vb.width > 0) || !(vb.height > 0)) {
      throw new Error('数式の大きさ（viewBox）が得られませんでした');
    }
    const width = vb.width / UNITS_PER_EM;
    const height = vb.height / UNITS_PER_EM;
    // viewBox は [x, y, w, h]。y が -(ベースラインより上の高さ) なので、
    // 下に出る長さは y + h
    const depth = (vb.y + vb.height) / UNITS_PER_EM;
    svg.setAttribute('width', String(vb.width));
    svg.setAttribute('height', String(vb.height));
    ['style', 'role', 'focusable'].forEach((a) => svg.removeAttribute(a));
    // 字形の path と <g>・<rect>・<text> だけあればよい。TeX の \href・\style・
    // \class などが入れうるリンク・スクリプト・外部を指す記述は、受け取る側が
    // 画像（mask）としてしか使わなくても、ここで落としておく
    svg.querySelectorAll(
      'script, foreignObject, iframe, object, embed, link, style, image, use'
    ).forEach((el) => el.remove());
    // \href が作るリンクは、中身（式）だけ残して外す
    svg.querySelectorAll('a').forEach((el) => el.replaceWith(...el.childNodes));
    svg.querySelectorAll('*').forEach((el) => {
      Array.from(el.attributes).forEach((a) => {
        const name = a.name.toLowerCase();
        const dangerous = name.indexOf('on') === 0 || name === 'href' ||
          name.endsWith(':href') || /url\s*\(|javascript:/i.test(a.value);
        if (name.indexOf('data-') === 0 || dangerous) el.removeAttribute(a.name);
      });
    });
    let xml = new XMLSerializer().serializeToString(svg)
      .replace(/currentColor/g, '#000');
    // 画像にされるときに整形式でないと、黙って壊れた画像になる。親へ渡す前に確かめる
    const doc = new DOMParser().parseFromString(xml, 'image/svg+xml');
    if (doc.getElementsByTagName('parsererror').length) {
      throw new Error('SVG を整形式の XML にできませんでした');
    }
    return { svg: xml, width: width, height: height, depth: depth };
  }

  async function renderOne(req) {
    const reply = { kind: 'rendered', id: req.id };
    try {
      if (typeof req.tex !== 'string' || !req.tex.trim()) {
        throw new Error('数式が空です');
      }
      if (req.tex.length > MAX_TEX) throw new Error('数式が長すぎます');
      const node = await window.MathJax.tex2svgPromise(
        req.tex, { display: req.display === true });
      const svg = node.querySelector('svg');
      if (!svg) throw new Error('MathJax から SVG が返りませんでした');
      const err = texError(svg);
      if (err) throw new Error(err);
      Object.assign(reply, { ok: true }, toMaskSvg(svg));
    } catch (err) {
      Object.assign(reply, { ok: false, error: describe(err) });
    }
    post(reply);
  }

  window.addEventListener('message', (ev) => {
    // 親以外（このフレームを開けるのは親だけだが念のため）の入力は読まない
    if (ev.source !== PARENT) return;
    const d = ev.data;
    if (!d || d.kind !== 'render' || !Number.isSafeInteger(d.id)) return;
    // 届いた順に 1 件ずつ処理する（フォントの追加読み込みが重なると遅くなる）
    queue = queue.then(() => renderOne(d));
  });

  if (!window.MathJax || !window.MathJax.startup) {
    post({ kind: 'failed', error: 'MathJax を読み込めませんでした' });
  } else {
    window.MathJax.startup.promise.then(
      () => post({ kind: 'ready' }),
      (err) => post({ kind: 'failed', error: describe(err) }));
  }
})();

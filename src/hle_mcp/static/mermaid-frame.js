/* mermaid-frame.js — Mermaid を実行する隔離フレームの側のスクリプト。
 *
 * このフレームは親（校正エディタ）から postMessage で Mermaid のソースを受け取り、
 * 描いた SVG を文字列で返す。親は受け取った SVG を <img> の data: URL にして
 * 見せるだけで、DOM には入れない（<img> の中の SVG はスクリプトも外部の
 * 読み込みも動かない）。
 *
 * メッセージ:
 *   親 → 本フレーム  {kind:'render', id, source, theme:'light'|'dark'}
 *   本フレーム → 親  {kind:'ready'}
 *                    {kind:'rendered', id, ok:true,  svg, width, height}
 *                    {kind:'rendered', id, ok:false, error}
 */
(function () {
  'use strict';

  const PARENT = window.parent;
  const MAX_SOURCE = 200000;
  // エディタ本体（style.css）と同じ書体。日本語のラベルが欠けないようにする
  const FONT = '"Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", ' +
    '"Noto Sans JP", sans-serif';

  let lastTheme = null;
  let queue = Promise.resolve();

  function post(msg) {
    // 親は不透明オリジンではないが、こちらが不透明オリジンなので宛先は '*' になる。
    // 埋め込めるのは同一オリジンのページだけ（frame-ancestors 'self'）
    PARENT.postMessage(msg, '*');
  }

  function configure(theme) {
    if (lastTheme === theme) return;
    window.mermaid.initialize({
      startOnLoad: false,
      // click / href によるリンクと生の HTML を許さない（既定の strict）。
      // loose や antiscript にはしない
      securityLevel: 'strict',
      theme: theme === 'dark' ? 'dark' : 'default',
      // 構文エラーのときに「爆弾」の絵を描かず、例外にして親へ文言を返す
      suppressErrorRendering: true,
      fontFamily: FONT,
    });
    lastTheme = theme;
  }

  /* Mermaid が返した SVG 文字列を、<img> に渡せる整形式の XML に直す。
     <img> の SVG は幅と高さを持たないと既定の大きさ（300x150）で描かれるので、
     viewBox の大きさを width / height に書く。root の style（max-width と
     背景色）は外し、背景は親側のカードの色に任せる */
  function toImageSvg(svgText) {
    const holder = document.createElement('div');
    holder.innerHTML = svgText;
    const svg = holder.querySelector('svg');
    if (!svg) throw new Error('Mermaid から SVG が返りませんでした');
    const vb = svg.viewBox && svg.viewBox.baseVal;
    if (!vb || !(vb.width > 0) || !(vb.height > 0)) {
      throw new Error('図の大きさ（viewBox）が得られませんでした');
    }
    const width = Math.min(20000, Math.max(1, Math.ceil(vb.width)));
    const height = Math.min(20000, Math.max(1, Math.ceil(vb.height)));
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    svg.removeAttribute('style');
    const xml = new XMLSerializer().serializeToString(svg);
    // <img> は整形式でない SVG を黙って壊れた画像にする。親へ渡す前にここで確かめる
    const doc = new DOMParser().parseFromString(xml, 'image/svg+xml');
    if (doc.getElementsByTagName('parsererror').length) {
      throw new Error('SVG を整形式の XML にできませんでした');
    }
    return { svg: xml, width: width, height: height };
  }

  /* render が失敗したときに残る作業用の要素（#mmdN / #dmmdN）を片付ける */
  function sweep() {
    document.querySelectorAll('body > [id^="mmd"], body > [id^="dmmd"]')
      .forEach((el) => el.remove());
  }

  function describe(err) {
    const raw = err && (err.str || err.message) ? (err.str || err.message) : err;
    return String(raw).replace(/\s+$/g, '').slice(0, 600);
  }

  async function renderOne(req) {
    const reply = { kind: 'rendered', id: req.id };
    try {
      if (typeof req.source !== 'string' || !req.source.trim()) {
        throw new Error('Mermaid のソースが空です');
      }
      if (req.source.length > MAX_SOURCE) {
        throw new Error('Mermaid のソースが長すぎます');
      }
      configure(req.theme === 'dark' ? 'dark' : 'light');
      const out = await window.mermaid.render('mmd' + req.id, req.source);
      Object.assign(reply, { ok: true }, toImageSvg(out.svg));
    } catch (err) {
      Object.assign(reply, { ok: false, error: describe(err) });
    } finally {
      sweep();
    }
    post(reply);
  }

  window.addEventListener('message', (ev) => {
    // 親以外（このフレームを開けるのは親だけだが念のため）の入力は読まない
    if (ev.source !== PARENT) return;
    const d = ev.data;
    if (!d || d.kind !== 'render' || !Number.isSafeInteger(d.id)) return;
    // Mermaid の render は同時に走らせられないので、届いた順に1件ずつ処理する
    queue = queue.then(() => renderOne(d));
  });

  if (!window.mermaid) {
    post({ kind: 'failed', error: 'Mermaid のライブラリを読み込めませんでした' });
  } else {
    post({ kind: 'ready' });
  }
})();

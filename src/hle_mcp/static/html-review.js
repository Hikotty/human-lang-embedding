/* html-review.js — HTMLレビューページ。
   レビュー対象HTMLを sandbox 付き iframe（スクリプト実行なし）で表示し、
   人間が範囲選択して「ここがこう」というコメントを付ける。本文は編集しない。

   コメントの位置は quote（選択されたレンダリング後テキスト）と
   prefix / suffix（選択の直前・直後の文脈テキスト）で永続化し、再表示時に
   テキスト検索で復元する。対象HTMLのDOMは一切書き換えず、ハイライトは
   CSS Custom Highlight API で描く（未対応ブラウザでは印が出ないだけで
   コメント機能自体は動く）。 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const CONTEXT_LEN = 50;   // prefix/suffix として保存する文脈の長さ
  const QUOTE_LIMIT = 300;  // quote の最大長（mdエディタと同じ）
  // テキストとして選択できないため、要素ごとコメント対象にできるもの
  const OBJ_SELECTOR = 'img, svg, canvas, video, audio';

  async function api(path, method, body) {
    const opts = { method: method || 'GET', headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    noteServerVersion(res.headers.get('X-HLE-Version'));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  /* 常駐Webサーバーが新しい版に入れ替わったら、再読み込みを促す。
     このページの本文は編集しないので、書きかけのコメントだけ気にすればよい */
  let loadedVersion = null;
  let updateNoticeShown = false;

  function noteServerVersion(v) {
    if (!v) return;
    if (loadedVersion === null) { loadedVersion = v; return; }
    if (v === loadedVersion || updateNoticeShown) return;
    const banner = document.getElementById('update-banner');
    if (!banner) return;
    updateNoticeShown = true;
    banner.hidden = false;
  }

  function reloadForUpdate() {
    if ((draft || sidebarBusy()) &&
        !window.confirm('書きかけのコメントは消えます。再読み込みしますか？')) {
      return;
    }
    location.reload();
  }

  function fmtDate(iso) {
    return (iso || '').replace('T', ' ')
      .replace(/\.\d+/, '').replace(/([+-]\d{2}:\d{2}|Z)$/, '');
  }

  /* HTML特殊文字をエスケープする。属性値へ埋め込むため引用符も対象にする */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  let session = null;
  let sid = null;
  let draft = null;    // { cid, quote, prefix, suffix, range }
  let lastSel = null;  // 直近の非collapsed選択 { quote, prefix, suffix, range }
  let activeCid = null;
  let editing = null;  // 編集中の対象 { cid, rid|null }
  let commentFilter = 'all'; // コメントの絞り込み（'all' | 'human' | 'ai'）
  let anchors = new Map();    // cid -> Range（現在のiframe DOM上の位置）
  let objAnchors = new Map(); // cid -> 画像・図の要素（画像コメント）
  let selectedObj = null;     // クリックで選んだ画像・図
  let regionMode = false;     // 範囲指定モード（ドラッグで範囲を囲む）
  let drawing = null;         // 囲んでいる途中の矩形 { x0, y0, x1, y1 }
  let pendingRect = null;     // 囲み終えてスクリーンショットを作っている範囲

  const frame = () => $('#doc-frame');
  const frameDoc = () => frame().contentDocument;
  const frameWin = () => frame().contentWindow;

  async function init(id) {
    sid = id;
    try {
      session = await api('/api/sessions/' + sid);
    } catch (e) {
      $('#doc-title').textContent = 'セッションが見つかりません: ' + sid;
      return;
    }
    document.title = session.title + ' - HTMLレビュー';
    renderMeta();
    initCommentFilter();
    renderComments();
    frame().addEventListener('load', onFrameLoad);
    frame().src = '/api/sessions/' + sid + '/source';
    $('#btn-comment').addEventListener('click', startCommentDraft);
    $('#btn-region').addEventListener('click', () => setRegionMode(!regionMode));
    $('#btn-complete').addEventListener('click', completeReview);
    $('#draft-submit').addEventListener('click', submitDraft);
    $('#draft-cancel').addEventListener('click', cancelDraft);
    $('#btn-update-reload').addEventListener('click', reloadForUpdate);
    const lay = layer();
    lay.addEventListener('pointerdown', onLayerDown);
    lay.addEventListener('pointermove', onLayerMove);
    lay.addEventListener('pointerup', onLayerUp);
    lay.addEventListener('pointercancel', onLayerCancel);
    lay.addEventListener('wheel', onLayerWheel, { passive: false });
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('resize', schedulePaintRegions);
    setInterval(pollRemote, 2500);
  }

  function renderMeta() {
    $('#doc-title').textContent = session.title;
    const revNo = session.revisions.length;
    const st = session.status === 'reviewed'
      ? '<span class="badge reviewed">校正完了</span>'
      : '<span class="badge">校正中</span>';
    $('#doc-meta').innerHTML = st +
      ' <span class="badge">HTML</span>' +
      ' ID: ' + esc(session.id) + '　第' + revNo + '稿' +
      (session.workspace ? '　' + esc(session.workspace) : '');
    const done = session.status === 'reviewed';
    $('#btn-complete').classList.toggle('done', done);
    $('#btn-complete').textContent = done ? '✔ 校正完了済み' : '✔ 校正完了';
  }

  /* ---------- iframe（レビュー対象ドキュメント） ---------- */
  function onFrameLoad() {
    const doc = frameDoc();
    if (!doc || !doc.body) return;
    // ハイライト用のスタイルを注入する（対象DOMの構造は変えない）
    const style = doc.createElement('style');
    style.textContent =
      '::highlight(hle-comment){background:rgba(255,213,79,.5);}' +
      '::highlight(hle-active){background:rgba(255,152,0,.65);}' +
      '::highlight(hle-draft){background:rgba(255,152,0,.5);}' +
      // 位置を特定できなくなったコメントから引用文で飛んだ先（一時的）
      '::highlight(hle-jump){background:rgba(255,152,0,.65);}' +
      // 画像・図は文字の選択で示せないので要素ごと枠線で囲む。
      // 属性だけを使い、本文テキスト（アンカーの拠り所）には触れない
      '[data-hle-obj]{outline:3px solid rgba(249,171,0,.85);' +
      'outline-offset:2px;cursor:pointer;}' +
      '[data-hle-obj="active"],[data-hle-obj="draft"]{outline-color:#f57c00;}' +
      '[data-hle-obj="sel"]{outline-color:#1a73e8;}';
    // 範囲のスクリーンショットを作るときに、この印の付いた要素は外す
    style.setAttribute('data-hle-style', '');
    (doc.head || doc.body).appendChild(style);
    doc.addEventListener('selectionchange', captureSelection);
    doc.addEventListener('click', onFrameClick);
    doc.addEventListener('focusin', onFrameFocusIn);
    doc.addEventListener('keydown', onKeyDown);
    // 範囲の枠は親ページ側の層に描くので、iframe のスクロールと、スクロールを
    // 伴わない再レイアウト（<details> の開閉、遅れて読み込まれた画像など）に
    // 合わせて動かす
    frameWin().addEventListener('scroll', schedulePaintRegions, { passive: true });
    doc.addEventListener('toggle', schedulePaintRegions, true);
    doc.addEventListener('load', schedulePaintRegions, true);
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(schedulePaintRegions);
      ro.observe(doc.documentElement);
      ro.observe(doc.body);
    }
    applyHighlights();
  }

  /* body配下のテキストノードを列挙して、全文と各ノードの開始オフセットの
     索引を作る。Range.toString() と同じ「テキストノードの連結」なので、
     選択時に保存した quote / prefix / suffix と突き合わせられる */
  function textIndex() {
    const doc = frameDoc();
    const nodes = [];
    const starts = [];
    let total = 0;
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      nodes.push(n);
      starts.push(total);
      total += n.data.length;
    }
    return { nodes: nodes, starts: starts, text: nodes.map((x) => x.data).join('') };
  }

  function rangeFromOffsets(idx, start, end) {
    const doc = frameDoc();
    const r = doc.createRange();
    let started = false;
    for (let i = 0; i < idx.nodes.length; i++) {
      const nStart = idx.starts[i];
      const nEnd = nStart + idx.nodes[i].data.length;
      if (!started && start >= nStart && start < nEnd) {
        r.setStart(idx.nodes[i], start - nStart);
        started = true;
      }
      if (started && end <= nEnd) {
        r.setEnd(idx.nodes[i], end - nStart);
        return r;
      }
    }
    return null;
  }

  /* 末尾（prefix側）または先頭（suffix側）からの一致文字数を数える */
  function matchLen(actual, expected, fromEnd) {
    let n = 0;
    while (n < actual.length && n < expected.length) {
      const a = fromEnd ? actual[actual.length - 1 - n] : actual[n];
      const b = fromEnd ? expected[expected.length - 1 - n] : expected[n];
      if (a !== b) break;
      n++;
    }
    return n;
  }

  /* quote と prefix/suffix から本文中の文字オフセットを求める。
     まず prefix+quote+suffix の完全一致を探し、なければ quote の各出現位置の
     うち前後の文脈が最も長く一致するものを選ぶ（同文言の重複に強くする） */
  function locate(idx, c) {
    const q = c.quote || '';
    if (!q) return null;
    const pre = c.prefix || '';
    const suf = c.suffix || '';
    if (pre || suf) {
      const i = idx.text.indexOf(pre + q + suf);
      if (i >= 0) {
        return { start: i + pre.length, end: i + pre.length + q.length };
      }
    }
    let best = null;
    let bestScore = -1;
    let from = 0;
    for (;;) {
      const i = idx.text.indexOf(q, from);
      if (i < 0) break;
      const before = idx.text.slice(Math.max(0, i - pre.length), i);
      const after = idx.text.slice(i + q.length, i + q.length + suf.length);
      const score = matchLen(before, pre, true) + matchLen(after, suf, false);
      if (score > bestScore) { bestScore = score; best = i; }
      from = i + 1;
    }
    return best == null ? null : { start: best, end: best + q.length };
  }

  /* 画像・図は quote による再検索ができないので、位置を「タグ名:出現順」で
     覚える。DOM順は同じソースを読み直す限り安定する */
  function elemRef(el) {
    const tag = el.tagName.toLowerCase();
    const all = frameDoc().getElementsByTagName(tag);
    const i = Array.prototype.indexOf.call(all, el);
    return i < 0 ? '' : tag + ':' + i;
  }

  function elemFromRef(ref) {
    const m = /^([A-Za-z][A-Za-z0-9-]*):(\d+)$/.exec(String(ref || ''));
    const doc = frameDoc();
    if (!m || !doc) return null;
    return doc.getElementsByTagName(m[1])[Number(m[2])] || null;
  }

  /* AIが対象を読み取れるよう、人間にも分かる名前を付ける */
  function objectLabel(el) {
    const tag = el.tagName.toLowerCase();
    const alt = el.getAttribute('alt') || el.getAttribute('aria-label') ||
      el.getAttribute('title') || '';
    if (tag === 'img') {
      const src = el.getAttribute('src') || '';
      if (/^data:/i.test(src)) return alt || '画像（埋め込み）';
      return alt || src.split(/[?#]/)[0].split('/').pop() || '画像';
    }
    if (tag === 'svg') return alt || '図（SVG）';
    if (tag === 'video' || tag === 'audio') {
      // src は要素か、最初の <source> に書かれる
      const source = el.querySelector('source[src]');
      const src = el.getAttribute('src') ||
        (source ? source.getAttribute('src') : '') || '';
      const kind = tag === 'video' ? '動画' : '音声';
      if (!src || /^data:/i.test(src)) return alt || kind + '（埋め込み）';
      return alt || src.split(/[?#]/)[0].split('/').pop() || kind;
    }
    return alt || tag;
  }

  /* 全コメントの位置を解決し直してハイライトを塗る */
  function applyHighlights() {
    anchors = new Map();
    objAnchors = new Map();
    const doc = frameDoc();
    if (!doc || !doc.body) return;
    const idx = textIndex();
    session.comments.forEach((c) => {
      // 絞り込みで隠れているコメントはハイライトも描かない
      if (!commentVisible(c)) return;
      // 範囲コメントの quote は範囲内の文字列をつないだ見出しなので、
      // 本文から探さない（位置は region で持つ）
      if (c.target === 'region') return;
      if (c.target === 'image') {
        const el = elemFromRef(c.image_ref);
        if (el) objAnchors.set(c.id, el);
        return;
      }
      const pos = locate(idx, c);
      if (!pos) return;
      const r = rangeFromOffsets(idx, pos.start, pos.end);
      if (r) anchors.set(c.id, r);
    });
    paintHighlights();
  }

  /* 画像・図の枠線を塗り直す（属性の付け外しだけを行う） */
  function paintObjects() {
    const doc = frameDoc();
    if (!doc || !doc.body) return;
    doc.querySelectorAll('[data-hle-obj]').forEach((el) => {
      el.removeAttribute('data-hle-obj');
    });
    objAnchors.forEach((el, cid) => {
      el.setAttribute('data-hle-obj', cid === activeCid ? 'active' : 'on');
    });
    if (draft && draft.element && draft.element.isConnected) {
      draft.element.setAttribute('data-hle-obj', 'draft');
    }
    if (selectedObj && selectedObj.isConnected) {
      selectedObj.setAttribute('data-hle-obj', 'sel');
    }
  }

  function paintHighlights() {
    paintObjects();
    paintRegions();
    const win = frameWin();
    if (!win || !win.CSS || !win.CSS.highlights || !win.Highlight) return;
    const normal = [];
    const active = [];
    anchors.forEach((r, cid) => {
      (cid === activeCid ? active : normal).push(r);
    });
    win.CSS.highlights.set('hle-comment', new win.Highlight(...normal));
    win.CSS.highlights.set('hle-active', new win.Highlight(...active));
    if (draft && draft.range) {
      win.CSS.highlights.set('hle-draft', new win.Highlight(draft.range));
    } else {
      win.CSS.highlights.delete('hle-draft');
    }
  }

  /* ---------- 選択の捕捉 ---------- */
  /* 親のボタンを押すとiframe内の選択が解除されることがあるため、
     selectionchange のたびに直近の選択内容を控えておく */
  function captureSelection() {
    const win = frameWin();
    const doc = frameDoc();
    if (!win || !doc || !doc.body) return;
    const sel = win.getSelection && win.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return;
    const r = sel.getRangeAt(0);
    const quote = r.toString().slice(0, QUOTE_LIMIT);
    if (!quote.trim()) return;
    try {
      const before = doc.createRange();
      before.setStart(doc.body, 0);
      before.setEnd(r.startContainer, r.startOffset);
      const after = doc.createRange();
      after.setStart(r.endContainer, r.endOffset);
      after.setEnd(doc.body, doc.body.childNodes.length);
      lastSel = {
        quote: quote,
        prefix: before.toString().slice(-CONTEXT_LEN),
        suffix: after.toString().slice(0, CONTEXT_LEN),
        range: r.cloneRange(),
      };
      // 文字列を選び直したら画像の選択は解除する（対象を1つに保つ）
      if (selectedObj) { selectedObj = null; paintHighlights(); }
    } catch (e) {
      // body外（head等）を含む選択は位置が特定できないため無視する
    }
  }

  /* iframe内の文字列の選択が要素 el に掛かっているか（空白だけの選択は
     数えない）。図の中の文字を選び終えたときに届く click を見分けるのに使う。
     別の場所に前の選択が残ったまま画像をクリックした場合は掛からないので、
     従来どおり画像の選択になる */
  function selectionTouches(el) {
    const win = frameWin();
    const sel = win && win.getSelection && win.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return false;
    if (!sel.toString().trim()) return false;
    try {
      return sel.getRangeAt(0).intersectsNode(el);
    } catch (err) {
      return false;
    }
  }

  /* iframeの表示領域の座標 (x, y) にある、コメント済みの文字列を返す。
     ハイライトの矩形そのもので判定するので、文字の外側は拾わない */
  function textAnchorAt(x, y) {
    for (const [cid, r] of anchors) {
      let rects;
      try { rects = r.getClientRects(); } catch (err) { continue; }
      for (const b of rects) {
        if (x >= b.left && x <= b.right && y >= b.top && y <= b.bottom) {
          return cid;
        }
      }
    }
    return null;
  }

  /* ハイライト済みの本文をクリックしたら対応するコメントを選択する。
     画像・図のクリックは「コメント対象の指定」として扱う。

     ただし文字列をドラッグで選び終えたときにも click は届く。押した要素と
     離した要素の共通の祖先に届くので、図（インラインSVG）の中の文字を選ぶと
     図のクリックになる。そのまま図を選ぶと、選んだ文字列が捨てられて
     図全体へのコメントになってしまうので、選択が図に掛かっているときは
     図を選ばずに、その文字列をコメント対象として残す */
  /* 動画・音声の標準プレーヤーは、操作部の上の click をページ側へ流さない
     （Chrome）。操作するとプレーヤーにフォーカスが移るので、そこで選ぶ */
  function onFrameFocusIn(e) {
    const player = e.target && e.target.closest &&
      e.target.closest('video, audio');
    if (!player || regionMode) return;
    clearJumpHighlight();
    selectedObj = player;
    lastSel = null; // 文字列の選択よりプレーヤーを対象にする
    for (const [cid, el] of objAnchors) {
      if (el === player) { activateComment(cid, false); return; }
    }
    paintHighlights();
  }

  function onFrameClick(e) {
    clearJumpHighlight();
    const doc = frameDoc();
    let obj = e.target.closest && e.target.closest(OBJ_SELECTOR);
    if (obj && selectionTouches(obj)) {
      // selectionchange より先に click が届いても選んだ文字列を取りこぼさない
      captureSelection();
      obj = null;
    }
    if (obj) {
      // 図の中でも、コメント済みの文字列の上ならそのコメントを開く
      const hit = textAnchorAt(e.clientX, e.clientY);
      if (hit) {
        selectedObj = null;
        activateComment(hit, false);
        return;
      }
      selectedObj = obj;
      lastSel = null; // 画像を選んだら直前の文字列選択は捨てる
      for (const [cid, el] of objAnchors) {
        if (el === obj) { activateComment(cid, false); return; }
      }
      paintHighlights();
      return;
    }
    if (selectedObj) { selectedObj = null; paintHighlights(); }
    if (!doc || !doc.caretRangeFromPoint) return;
    let caret = null;
    try {
      caret = doc.caretRangeFromPoint(e.clientX, e.clientY);
    } catch (err) { return; }
    if (!caret) return;
    for (const [cid, r] of anchors) {
      try {
        if (r.isPointInRange(caret.startContainer, caret.startOffset)) {
          activateComment(cid, false);
          return;
        }
      } catch (err) { /* DOMが変わった場合は無視 */ }
    }
  }

  /* ---------- コメント下書き ---------- */
  function newCid() {
    return 'c' + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 6);
  }

  function startCommentDraft() {
    if (regionMode) {
      toast('範囲指定モードです。ページ上をドラッグして範囲を囲むと、' +
        'コメント欄が開きます');
      return;
    }
    if (selectedObj && selectedObj.isConnected) {
      const ref = elemRef(selectedObj);
      if (!ref) {
        alert('この画像は位置を特定できませんでした');
        return;
      }
      const label = objectLabel(selectedObj);
      draft = {
        cid: newCid(), target: 'image', quote: label, image: label,
        image_ref: ref, element: selectedObj,
      };
      const tag = selectedObj.tagName.toLowerCase();
      const icon = tag === 'video' ? '🎬' : tag === 'audio' ? '🔊' : '🖼';
      showDraft(icon + ' ' + label);
      return;
    }
    captureSelection(); // 選択が残っていれば最新の内容に更新する
    if (!lastSel) {
      alert('表示されたページの、コメントを付けたい文字列を' +
        'ドラッグで選択するか、画像や図をクリックしてください');
      return;
    }
    draft = Object.assign({ cid: newCid(), target: 'text' }, lastSel);
    showDraft(draft.quote);
  }

  /* 下書き欄を開く。shotUrl を渡すと、AIに渡るスクリーンショットを添えて
     見せる。keepInput なら、開いている下書きの本文と種類を残したまま対象
     だけを差し替える（範囲を囲み直したとき、書いた文を消さないため） */
  function showDraft(label, shotUrl, keepInput) {
    paintHighlights();
    const box = $('#comment-draft');
    const reuse = !!keepInput && !box.hidden;
    box.hidden = false;
    const q = $('#draft-quote');
    q.textContent = '';
    q.classList.toggle('shot-quote', !!shotUrl);
    if (shotUrl) {
      const img = document.createElement('img');
      img.src = shotUrl;
      img.alt = '囲んだ範囲のスクリーンショット（AIに渡る画像）';
      q.appendChild(img);
    }
    q.appendChild(document.createTextNode(label));
    if (!reuse) {
      // 種類は毎回「指摘」から始める（前の下書きの選択を持ち越さない）
      $('#draft-tag').innerHTML = tagSelectHtml('draft-tag', 'issue');
      bindTagSelect($('#draft-tag'));
      $('#draft-text').value = '';
    }
    $('#draft-text').focus();
  }

  async function submitDraft() {
    const text = $('#draft-text').value.trim();
    if (!text) { $('#draft-text').focus(); return; }
    const body = {
      id: draft.cid, quote: draft.quote, text: text, author: 'human',
      target: draft.target || 'text', tag: selectedTag('draft-tag'),
    };
    if (draft.target === 'image') {
      body.image = draft.image;
      body.image_ref = draft.image_ref;
    } else if (draft.target === 'region') {
      // スクリーンショットはコメントと一緒に送り、サーバーが揃えて保存する
      body.region = draft.region;
      body.texts = draft.texts;
      body.screenshot = draft.shot.slice(draft.shot.indexOf(',') + 1);
    } else {
      body.prefix = draft.prefix;
      body.suffix = draft.suffix;
    }
    try {
      const comment = await api(
        '/api/sessions/' + sid + '/comments', 'POST', body);
      session.comments.push(comment);
      draft = null;
      lastSel = null;
      selectedObj = null;
      $('#comment-draft').hidden = true;
      renderComments();
      applyHighlights();
    } catch (e) {
      alert('コメントの保存に失敗しました: ' + e.message);
    }
  }

  function cancelDraft() {
    draft = null;
    selectedObj = null;
    $('#comment-draft').hidden = true;
    paintHighlights();
  }

  function authorLabel(a) {
    return a === 'ai' ? '<span class="who ai">AI</span>'
      : '<span class="who human">あなた</span>';
  }

  /* ---------- コメントの絞り込み（著者別。app.jsと同じ考え方） ----------
     「すべて / 人間 / AI」で絞る。判定はトップレベルコメントの著者
     （未指定=human）で、スレッド丸ごと（返信を含む）出し入れする。
     隠れたコメントは本文のハイライト（text の Highlight、画像・図の枠、
     範囲の枠）も描かない。ロードのたびに「すべて」に戻る */
  function initCommentFilter() {
    commentFilter = 'all';
    document.querySelectorAll('input[name="comment-filter"]').forEach((r) => {
      r.checked = r.value === 'all';
      r.addEventListener('change', () => {
        if (!r.checked) return;
        commentFilter = r.value;
        renderComments();
        applyHighlights();
      });
    });
  }

  /* トップレベルの著者（未指定=human）がフィルタに合うか */
  function commentVisible(c) {
    if (commentFilter === 'all') return true;
    const author = c && c.author === 'ai' ? 'ai' : 'human';
    return author === commentFilter;
  }

  /* ---------- コメントの種類（指摘・質問。app.jsと同じ） ----------
     指摘は本文を直してほしいもの、質問は答えてほしいだけのもの */
  const TAG_LABELS = { issue: '指摘', question: '質問' };
  const TAG_HINTS = {
    issue: 'AIはこの指摘を踏まえて本文を直します',
    question: 'AIは返信で答えるだけで、本文は直しません',
  };

  function tagOf(c) {
    return c && c.tag === 'question' ? 'question' : 'issue';
  }

  function tagBadge(c) {
    const t = tagOf(c);
    return '<span class="tag-badge ' + t + '" title="' + esc(TAG_HINTS[t]) +
      '">' + TAG_LABELS[t] + '</span>';
  }

  function tagSelectHtml(name, current) {
    const cur = current === 'question' ? 'question' : 'issue';
    const opts = ['issue', 'question'].map((t) =>
      '<label class="' + t + '"><input type="radio" name="' + name +
      '" value="' + t + '"' + (t === cur ? ' checked' : '') + '>' +
      TAG_LABELS[t] + '</label>').join('');
    return '<div class="tag-field"><div class="tag-select" role="radiogroup" ' +
      'aria-label="コメントの種類">' + opts + '</div>' +
      '<div class="tag-hint">' + esc(TAG_HINTS[cur]) + '</div></div>';
  }

  function bindTagSelect(root) {
    root.querySelectorAll('.tag-field input[type="radio"]').forEach((r) => {
      r.addEventListener('change', () => {
        const hint = r.closest('.tag-field').querySelector('.tag-hint');
        if (hint) hint.textContent = TAG_HINTS[r.value] || '';
      });
    });
  }

  function selectedTag(name) {
    const r = document.querySelector('input[name="' + name + '"]:checked');
    return r && r.value === 'question' ? 'question' : 'issue';
  }

  /* ---------- コメント/返信の編集・削除（app.jsと同じキー方式） ---------- */
  function targetKey(cid, rid) {
    return esc(cid) + (rid ? '|' + esc(rid) : '');
  }

  function parseKey(key) {
    const parts = String(key).split('|');
    return { cid: parts[0], rid: parts[1] || null };
  }

  function isEditing(cid, rid) {
    return !!editing && editing.cid === cid &&
      (editing.rid || null) === (rid || null);
  }

  function editedMark(o) {
    return o.updated_at ? '<span class="when">（編集済み）</span>' : '';
  }

  /* 編集フォーム。種類（指摘・質問）はコメント本体にだけあり、返信には無い */
  function editFormHtml(cid, rid, text, tag) {
    const key = targetKey(cid, rid);
    return (rid ? '' : tagSelectHtml('edit-tag', tag)) +
      '<textarea class="edit-text" rows="3" data-edit-input="' + key +
      '">' + esc(text) + '</textarea>' +
      '<div class="card-actions edit-actions">' +
      '<button class="primary" data-edit-save="' + key + '">保存</button>' +
      '<button data-edit-cancel="1">キャンセル</button></div>';
  }

  function bodyHtml(cid, rid, o) {
    if (isEditing(cid, rid)) return editFormHtml(cid, rid, o.text, tagOf(o));
    const key = targetKey(cid, rid);
    return '<div class="body">' + esc(o.text) + '</div>' +
      '<div class="card-actions">' +
      '<button data-edit="' + key + '">編集</button>' +
      '<button class="danger" data-del="' + key + '">削除</button></div>';
  }

  /* 返信欄（app.jsと同じ）。書いた分だけ伸ばすのはCSSが受け持つ */
  function replyRowHtml(cid) {
    return '<div class="reply-row"><textarea rows="1" placeholder="返信を入力" ' +
      'title="Enterで送信、Shift+Enterで改行" data-reply="' + esc(cid) +
      '"></textarea><button data-send="' + esc(cid) + '">送信</button></div>';
  }

  /* Enter で送信、Shift+Enter で改行。IMEの確定のEnterでは送らない */
  function isSendKey(e) {
    return e.key === 'Enter' && !e.shiftKey && !e.isComposing &&
      e.keyCode !== 229;
  }

  /* 古い稿に付けたコメントは今のページでは文が書き換わっていることがある。
     どの稿への指摘（質問）かを添え、飛んだ先が近傍になり得ることを伝える */
  function revNote(c) {
    const cur = session.revisions.length - 1;
    if (typeof c.revision !== 'number' || c.revision >= cur) return '';
    return '<span class="rev-note">第' + (c.revision + 1) + '稿への' +
      TAG_LABELS[tagOf(c)] + '</span>';
  }

  /* カード先頭の引用。範囲コメントは、AIに渡したスクリーンショットの
     縮小を添えて、どこを囲んだかを見分けられるようにする */
  function cardQuoteHtml(c, n) {
    if (c.target === 'region') {
      const url = '/api/sessions/' + encodeURIComponent(sid) + '/files/' +
        encodeURIComponent(c.image || '');
      return '<div class="quote img-quote" title="クリックでページの位置へ">' +
        '<img src="' + esc(url) + '" alt=""><span>C' + n + ': ▭ ' +
        esc(c.quote || '範囲（文字なし）') + '</span>' + revNote(c) + '</div>';
    }
    return '<div class="quote" title="クリックでページの位置へ">C' + n + ': ' +
      revNote(c) +
      esc(c.target === 'image' ? '🖼 ' + (c.image || c.quote) : c.quote) +
      '</div>';
  }

  function renderComments() {
    const list = $('#comment-list');
    if (!session.comments.length) {
      list.innerHTML = '<div class="empty-note">コメントはまだありません。<br>' +
        '文字列を選択、または画像・図をクリックして<br>' +
        '「💬 コメント」を押すと追加できます。</div>';
      return;
    }
    // 番号は全コメントの通し番号を保ち、絞り込みで隠すものだけ描かない
    const visible = session.comments
      .map((c, i) => ({ c: c, n: i + 1 }))
      .filter((x) => commentVisible(x.c));
    if (!visible.length) {
      list.innerHTML = '<div class="empty-note">この絞り込みに合う' +
        'コメントはありません。</div>';
      return;
    }
    list.innerHTML = visible.map(({ c, n: cardNo }) => {
      const i = cardNo - 1;
      const replies = (c.replies || []).map((r) =>
        '<div class="reply">' + authorLabel(r.author) +
        '<span class="when">' + esc(fmtDate(r.created_at)) + '</span>' +
        editedMark(r) + bodyHtml(c.id, r.id, r) + '</div>'
      ).join('');
      return '<div class="comment-card' +
        (c.id === activeCid ? ' active' : '') +
        '" data-cid="' + esc(c.id) + '">' +
        cardQuoteHtml(c, i + 1) +
        // 編集中は選択欄が種類を示すので、保存済みの種類のバッジは出さない
        (isEditing(c.id, null) ? '' : tagBadge(c)) + authorLabel(c.author) +
        '<span class="when">' + esc(fmtDate(c.created_at)) + '</span>' +
        editedMark(c) + bodyHtml(c.id, null, c) +
        replies + replyRowHtml(c.id) +
        '</div>';
    }).join('');
    bindTagSelect(list);
    list.querySelectorAll('.comment-card').forEach((card) => {
      card.addEventListener('click', (e) => {
        // 種類の選択（label）を押してもページへは飛ばない
        if (e.target.closest('input,button,textarea,label')) return;
        activateComment(card.dataset.cid, true);
      });
    });
    list.querySelectorAll('button[data-send]').forEach((btn) => {
      btn.addEventListener('click', () => sendReply(btn.dataset.send));
    });
    list.querySelectorAll('textarea[data-reply]').forEach((ta) => {
      ta.addEventListener('keydown', (e) => {
        if (isSendKey(e)) {
          e.preventDefault();
          sendReply(ta.dataset.reply);
        }
      });
    });
    list.querySelectorAll('button[data-edit]').forEach((btn) => {
      btn.addEventListener('click', () => startEdit(btn.dataset.edit));
    });
    list.querySelectorAll('button[data-del]').forEach((btn) => {
      btn.addEventListener('click', () => removeTarget(btn.dataset.del));
    });
    list.querySelectorAll('button[data-edit-save]').forEach((btn) => {
      btn.addEventListener('click', () => saveEdit(btn.dataset.editSave));
    });
    list.querySelectorAll('button[data-edit-cancel]').forEach((btn) => {
      btn.addEventListener('click', () => { editing = null; renderComments(); });
    });
  }

  function startEdit(key) {
    editing = parseKey(key);
    renderComments();
    const ta = document.querySelector('textarea[data-edit-input]');
    if (ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
      // 高さの上限を超える長文は欄の中でスクロールするので、末尾を見せる
      ta.scrollTop = ta.scrollHeight;
    }
  }

  async function saveEdit(key) {
    const t = parseKey(key);
    const ta = document.querySelector('textarea[data-edit-input]');
    const text = ta ? (ta.value || '').trim() : '';
    if (!text) {
      alert('本文を空にはできません。消す場合は「削除」を押してください');
      return;
    }
    const base = '/api/sessions/' + sid + '/comments/' + t.cid;
    const path = t.rid ? base + '/replies/' + t.rid + '/update' : base + '/update';
    const body = { text: text };
    if (!t.rid) body.tag = selectedTag('edit-tag');
    try {
      const updated = await api(path, 'POST', body);
      const c = session.comments.find((x) => x.id === t.cid);
      if (c && t.rid) {
        const r = (c.replies || []).find((x) => x.id === t.rid);
        if (r) Object.assign(r, updated);
      } else if (c) {
        // 指摘に戻すとサーバーは tag キーを消すので、合成せずに置き換える
        session.comments[session.comments.indexOf(c)] = updated;
      }
      editing = null;
      renderComments();
    } catch (e) {
      alert('保存に失敗しました: ' + e.message);
    }
  }

  async function removeTarget(key) {
    const t = parseKey(key);
    const label = t.rid ? 'この返信' : 'このコメント（返信も含む）';
    if (!window.confirm(label + 'を削除します。よろしいですか？')) return;
    const base = '/api/sessions/' + sid + '/comments/' + t.cid;
    const path = t.rid ? base + '/replies/' + t.rid + '/delete' : base + '/delete';
    try {
      await api(path, 'POST', {});
      const c = session.comments.find((x) => x.id === t.cid);
      if (t.rid) {
        if (c) c.replies = (c.replies || []).filter((x) => x.id !== t.rid);
      } else {
        session.comments = session.comments.filter((x) => x.id !== t.cid);
        if (activeCid === t.cid) activeCid = null;
      }
      if (editing && editing.cid === t.cid) editing = null;
      renderComments();
      applyHighlights();
    } catch (e) {
      alert('削除に失敗しました: ' + e.message);
    }
  }

  async function sendReply(cid) {
    const ta = document.querySelector('textarea[data-reply="' + cid + '"]');
    const text = (ta.value || '').trim();
    if (!text) return;
    try {
      const reply = await api(
        '/api/sessions/' + sid + '/comments/' + cid + '/replies', 'POST',
        { text: text, author: 'human' });
      const c = session.comments.find((x) => x.id === cid);
      if (c) (c.replies = c.replies || []).push(reply);
      activeCid = cid;
      renderComments();
    } catch (e) {
      alert('返信に失敗しました: ' + e.message);
    }
  }

  function activateComment(cid, scrollFrame) {
    activeCid = cid;
    document.querySelectorAll('.comment-card').forEach((card) =>
      card.classList.toggle('active', card.dataset.cid === cid));
    paintHighlights();
    clearJumpHighlight();
    if (!scrollFrame) {
      const card = document.querySelector(
        '.comment-card[data-cid="' + cid + '"]');
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    jumpToComment(cid);
  }

  /* ---------- コメントの位置へ飛ぶ ---------- */
  function rangeElement(range) {
    const sc = range.startContainer;
    return sc.nodeType === Node.ELEMENT_NODE ? sc : sc.parentElement;
  }

  /* 位置が解決できているコメントはそこへ。新しい稿で文が書き換わって
     解決できなくなったコメントは、引用文の先頭・末尾の一部で探して
     近傍へ飛び、見つけた範囲を一時的に強調する */
  function jumpToComment(cid) {
    const rc = session.comments.find((x) => x.id === cid);
    if (rc && rc.target === 'region') {
      scrollToRegion(rc.region);
      return;
    }
    const el = objAnchors.get(cid) ||
      (anchors.has(cid) ? rangeElement(anchors.get(cid)) : null);
    if (el && el.scrollIntoView) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    const c = session.comments.find((x) => x.id === cid);
    const doc = frameDoc();
    if (!c || !doc || !doc.body) return;
    if (c.target === 'image') {
      toast('この画像・図は今のページで見つかりません');
      return;
    }
    const q = String(c.quote || '').trim();
    const idx = textIndex();
    const tries = [];
    [24, 12].forEach((n) => {
      if (q.length > n) {
        tries.push({ quote: q.slice(0, n).trim(), prefix: c.prefix || '' });
        tries.push({ quote: q.slice(-n).trim(), suffix: c.suffix || '' });
      }
    });
    for (const t of tries) {
      if (t.quote.length < 6) continue;
      const pos = locate(idx, t);
      const r = pos && rangeFromOffsets(idx, pos.start, pos.end);
      if (!r) continue;
      showJump(r);
      return;
    }
    // 引用文そのものが書き換わっていても、直前・直後の文脈は残っていることが
    // 多い。その間が書き換わった場所なので、そこへ飛ぶ
    const around = locateAround(idx, c, q.length);
    const ar = around && rangeFromOffsets(idx, around.start, around.end);
    if (ar) {
      showJump(ar);
      toast('第' + ((c.revision || 0) + 1) + '稿で付けた場所の付近へ飛びました' +
        '（この部分は書き換わっています）');
      return;
    }
    toast('引用文が今のページに見つかりません（書き換わっています）');
  }

  /* 飛び先へスクロールし、範囲を一時的に強調する */
  function showJump(range) {
    rangeElement(range).scrollIntoView({ behavior: 'smooth', block: 'center' });
    const win = frameWin();
    if (win && win.CSS && win.CSS.highlights && win.Highlight) {
      win.CSS.highlights.set('hle-jump', new win.Highlight(range));
    }
  }

  /* 引用文が見つからないとき、保存してある直前（prefix）・直後（suffix）の
     文脈の一部を今のページから探し、コメントを付けた場所の字位置を求める。
     両方見つかれば、その間（書き換わった部分）。片方だけなら、見つかった側から
     引用文の長さ分。文脈が短い・見つからない・離れすぎなら null */
  function locateAround(idx, c, quoteLen) {
    const pre = String(c.prefix || '');
    const suf = String(c.suffix || '');
    let preEnd = -1;
    let sufStart = -1;
    [24, 12].forEach((n) => {
      if (preEnd < 0 && pre.length >= n) {
        const i = idx.text.indexOf(pre.slice(-n));
        if (i >= 0) preEnd = i + n;
      }
      if (sufStart < 0 && suf.length >= n) {
        const i = idx.text.indexOf(suf.slice(0, n));
        if (i >= 0) sufStart = i;
      }
    });
    const len = Math.max(quoteLen, 1);
    const total = idx.text.length;
    if (preEnd >= 0 && sufStart >= preEnd && sufStart - preEnd <= 2000) {
      // 間が空（引用文が消えた）なら、前後の少しだけを示す
      if (sufStart === preEnd) {
        return { start: Math.max(0, preEnd - 12), end: Math.min(total, sufStart + 12) };
      }
      return { start: preEnd, end: sufStart };
    }
    if (preEnd >= 0 && sufStart < 0 && preEnd < total) {
      return { start: preEnd, end: Math.min(total, preEnd + len) };
    }
    if (sufStart >= 0 && preEnd < 0 && sufStart > 0) {
      return { start: Math.max(0, sufStart - len), end: sufStart };
    }
    return null;
  }

  function clearJumpHighlight() {
    const win = frameWin();
    if (win && win.CSS && win.CSS.highlights) win.CSS.highlights.delete('hle-jump');
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2500);
  }

  /* ---------- 範囲指定モード ----------
     ページの上に透明の層を重ね、ドラッグで囲んだ矩形にコメントを付ける。
     囲んだ範囲はその場でスクリーンショットにしてコメントと一緒に保存し、
     AIは get_feedback でその画像を受け取る。層は親ページ側に置くので、
     レビュー対象のDOMには手を加えない */
  const SHOT_SCALE = 2;          // 切り抜きの解像度（CSS px に対する倍率）
  const SHOT_MAX_EDGE = 1600;    // 切り抜きの長辺の上限（サーバーと同じ）
  const SHOT_MAX_BYTES = 3 * 1024 * 1024;  // 1枚の上限（サーバーと同じ）
  const REGION_MIN = 8;          // これより小さい囲みはクリックとして扱う
  const TEXTS_MAX = 80;          // 範囲内の文字列の断片の上限（サーバーと同じ）
  const TEXTS_MAX_CHARS = 4000;

  const layer = () => $('#region-layer');

  function setRegionMode(on) {
    regionMode = !!on;
    drawing = null;
    const btn = $('#btn-region');
    btn.classList.toggle('on', regionMode);
    btn.setAttribute('aria-pressed', regionMode ? 'true' : 'false');
    layer().classList.toggle('drawing', regionMode);
    if (regionMode) {
      // 囲んでいる間は文字列・画像の選択を外す（コメントの対象を1つに保つ）
      selectedObj = null;
      lastSel = null;
      const win = frameWin();
      if (win && win.getSelection) win.getSelection().removeAllRanges();
      toast('ページ上をドラッグして、コメントを付けたい範囲を囲んでください');
    }
    paintHighlights();
  }

  function normRect(d) {
    return {
      x: Math.min(d.x0, d.x1), y: Math.min(d.y0, d.y1),
      w: Math.abs(d.x1 - d.x0), h: Math.abs(d.y1 - d.y0),
    };
  }

  /* ポインターの位置を層（＝iframe の表示領域）の座標にして、はみ出しは詰める */
  function layerPoint(e) {
    const b = layer().getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(b.width, e.clientX - b.left)),
      y: Math.max(0, Math.min(b.height, e.clientY - b.top)),
    };
  }

  function onLayerDown(e) {
    if (!regionMode || e.button !== 0 || pendingRect) return;
    e.preventDefault();
    const p = layerPoint(e);
    drawing = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    try { layer().setPointerCapture(e.pointerId); } catch (err) { /* 無視 */ }
    paintRegions();
  }

  function onLayerMove(e) {
    if (!drawing) return;
    const p = layerPoint(e);
    drawing.x1 = p.x;
    drawing.y1 = p.y;
    paintRegions();
  }

  function onLayerUp(e) {
    if (!drawing) return;
    onLayerMove(e);
    const r = normRect(drawing);
    drawing = null;
    try { layer().releasePointerCapture(e.pointerId); } catch (err) { /* 無視 */ }
    if (r.w < REGION_MIN || r.h < REGION_MIN) {
      // 囲まずにクリックしただけなら、そこにある範囲コメントを開く
      paintRegions();
      const cid = regionAt(r.x, r.y);
      if (cid) activateComment(cid, false);
      return;
    }
    createRegionDraft(r);
  }

  function onLayerCancel() {
    drawing = null;
    paintRegions();
  }

  /* 層がホイールを受け取るので、iframe のスクロールに渡す。ページの
     スクロールだけを送る（要素の内側のスクロールは範囲指定の間は送れない）。
     ctrl 付き（トラックパッドのピンチ）はブラウザの拡大に任せる */
  function onLayerWheel(e) {
    const win = frameWin();
    if (!win || e.ctrlKey) return;
    e.preventDefault();
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? win.innerHeight : 1;
    win.scrollBy(e.deltaX * unit, e.deltaY * unit);
  }

  /* Esc で囲みかけの矩形を取り消し、囲んでいなければ範囲指定モードを抜ける。
     囲み直している間もフォーカスは下書き欄に残るので、囲みかけの取り消しは
     入力欄での Esc でも効かせる */
  function onKeyDown(e) {
    if (e.key !== 'Escape' || !regionMode) return;
    if (drawing) {
      drawing = null;
      paintRegions();
      return;
    }
    const t = e.target;
    if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) return;
    setRegionMode(false);
  }

  /* 保存した範囲を、今の iframe の表示領域の座標に直す。範囲を含む図が
     分かっていれば図の枠に対する割合で置き、画面の幅が変わって図が
     伸び縮みしても枠が図に付いていくようにする */
  function regionClientRect(region) {
    if (!region) return null;
    const rel = region.rel;
    if (region.ref && Array.isArray(rel) && rel.length === 4) {
      const el = elemFromRef(region.ref);
      const b = el && el.getBoundingClientRect();
      if (b && b.width > 0 && b.height > 0) {
        return {
          x: b.left + rel[0] * b.width, y: b.top + rel[1] * b.height,
          w: rel[2] * b.width, h: rel[3] * b.height,
        };
      }
    }
    const win = frameWin();
    if (!win) return null;
    return {
      x: region.x - win.scrollX, y: region.y - win.scrollY,
      w: region.width, h: region.height,
    };
  }

  /* 範囲の9割以上を覆う画像・図のうち、いちばん小さいものを返す */
  function figureFor(r) {
    const doc = frameDoc();
    let best = null;
    let bestArea = Infinity;
    doc.querySelectorAll(OBJ_SELECTOR).forEach((el) => {
      const b = el.getBoundingClientRect();
      const iw = Math.min(r.x + r.w, b.right) - Math.max(r.x, b.left);
      const ih = Math.min(r.y + r.h, b.bottom) - Math.max(r.y, b.top);
      if (iw <= 0 || ih <= 0) return;
      const area = b.width * b.height;
      if ((iw * ih) / (r.w * r.h) >= 0.9 && area < bestArea) {
        best = el;
        bestArea = area;
      }
    });
    return best;
  }

  /* SVG の座標系（viewBox）での範囲。AIが図の要素の座標と突き合わせられる */
  function svgBox(el, r) {
    if (el.tagName.toLowerCase() !== 'svg' || !el.getScreenCTM) return null;
    const m = el.getScreenCTM();
    if (!m) return null;
    const inv = m.inverse();
    const P = frameWin().DOMPoint;
    const a = new P(r.x, r.y).matrixTransform(inv);
    const b = new P(r.x + r.w, r.y + r.h).matrixTransform(inv);
    const round = (v) => Math.round(v * 10) / 10;
    return [round(Math.min(a.x, b.x)), round(Math.min(a.y, b.y)),
      round(Math.abs(b.x - a.x)), round(Math.abs(b.y - a.y))];
  }

  /* 囲んだ矩形（表示領域の座標）から、保存する位置の情報を作る */
  function buildRegion(r) {
    const win = frameWin();
    const root = frameDoc().documentElement;
    const round = (v) => Math.round(v * 10) / 10;
    const region = {
      x: round(r.x + win.scrollX), y: round(r.y + win.scrollY),
      width: round(r.w), height: round(r.h),
      frame_width: root.clientWidth, frame_height: root.clientHeight,
    };
    const fig = figureFor(r);
    const ref = fig ? elemRef(fig) : '';
    const b = fig && fig.getBoundingClientRect();
    if (ref && b.width > 0 && b.height > 0) {
      const frac = (v) => Math.round(v * 10000) / 10000;
      region.ref = ref;
      region.rel = [frac((r.x - b.left) / b.width), frac((r.y - b.top) / b.height),
        frac(r.w / b.width), frac(r.h / b.height)];
      const box = svgBox(fig, r);
      if (box) region.svg_box = box;
    }
    return region;
  }

  /* 範囲内に表示されている文字列を、文書の順に断片で集める。文字の中心が
     範囲に入っているものだけを数え、はみ出したノードは1文字ずつ調べる。
     長い <pre> の後半を囲んでも拾えるよう、1ノードあたり2万文字まで見る */
  const TEXT_SCAN_MAX = 20000;
  function regionTexts(r) {
    const doc = frameDoc();
    const inside = (b) => (b.width > 0 || b.height > 0) &&
      b.left + b.width / 2 >= r.x && b.left + b.width / 2 <= r.x + r.w &&
      b.top + b.height / 2 >= r.y && b.top + b.height / 2 <= r.y + r.h;
    const skip = /^(script|style|title|desc|noscript|template)$/i;
    const out = [];
    let total = 0;
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
    const range = doc.createRange();
    let n;
    while ((n = walker.nextNode())) {
      const p = n.parentElement;
      if (!p || skip.test(p.tagName) || !n.data.trim()) continue;
      range.selectNodeContents(n);
      const nb = range.getBoundingClientRect();
      if ((nb.width <= 0 && nb.height <= 0) || nb.right < r.x ||
          nb.left > r.x + r.w || nb.bottom < r.y || nb.top > r.y + r.h) continue;
      let s = '';
      if (nb.left >= r.x && nb.right <= r.x + r.w &&
          nb.top >= r.y && nb.bottom <= r.y + r.h) {
        s = n.data;
      } else {
        let gap = false;
        for (let i = 0; i < Math.min(n.data.length, TEXT_SCAN_MAX); i++) {
          range.setStart(n, i);
          range.setEnd(n, i + 1);
          if (inside(range.getBoundingClientRect())) {
            s += (gap && s ? ' ' : '') + n.data[i];
            gap = false;
          } else if (s) {
            gap = true;
          }
        }
      }
      s = s.replace(/\s+/g, ' ').trim();
      if (!s) continue;
      if (out.length >= TEXTS_MAX || total + s.length > TEXTS_MAX_CHARS) break;
      out.push(s);
      total += s.length;
    }
    return out;
  }

  function regionLabel(texts) {
    const label = texts.join(' / ');
    return label.length <= 120 ? label : label.slice(0, 119) + '…';
  }

  function base64Utf8(str) {
    const bytes = new TextEncoder().encode(str);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  }

  /* 表示中の背景色。body（または html）の背景は本来ページ全体に広がるが、
     複製を画像にするとその要素の箱の中にしか塗られないので、先に塗っておく */
  function pageBackground(doc, win) {
    const clear = (c) => !c || c === 'transparent' || /rgba\(.*,\s*0\)$/.test(c);
    let bg = win.getComputedStyle(doc.documentElement).backgroundColor;
    if (clear(bg) && doc.body) bg = win.getComputedStyle(doc.body).backgroundColor;
    return clear(bg) ? '#ffffff' : bg;
  }

  /* 複製を XML として読み直せるようにする。HTML では許されても XML では
     不正な属性名（@click, :class, x-on:click など。名前空間の付いた
     xlink:href などは残る）と、XML に書けない文字を外す。コメントは
     描画に関係せず、中の "--" が XML では不正なので消す */
  const XML_NAME = /^[A-Za-z_\u00C0-\uFFEF][A-Za-z0-9._\-\u00B7\u00C0-\uFFEF]*$/;
  // XML に書けない制御文字と、対になっていないサロゲート（低サロゲートは
  // 直前の1文字を残すために捕まえる）
  const XML_BAD = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
  // 制御文字は Chrome が見える記号として描くので、同じ意味の記号
  // （U+2400 台の Control Pictures）に置き換える。幅は表示と少し違うことがある
  const xmlText = (s) => s.replace(XML_BAD, (m, lead) => {
    if (lead !== undefined) return lead;  // 対になっていない低サロゲート
    const code = m.charCodeAt(0);
    return code < 0x20 ? String.fromCharCode(0x2400 + code) : '';
  });
  function makeXmlSafe(clone) {
    [clone].concat(Array.from(clone.getElementsByTagName('*'))).forEach((el) => {
      Array.from(el.attributes).forEach((a) => {
        // HTML の要素に書かれた xmlns は名前空間の宣言にならず、直列化で
        // 宣言と二重になるので外す（描画には関係しない）
        if (a.namespaceURI === null &&
            (!XML_NAME.test(a.name) || a.name === 'xmlns')) {
          el.removeAttribute(a.name);
        } else if (xmlText(a.value) !== a.value) {
          a.value = xmlText(a.value);
        }
      });
    });
    const walker = clone.ownerDocument.createTreeWalker(
      clone, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_COMMENT);
    const comments = [];
    let n;
    while ((n = walker.nextNode())) {
      if (n.nodeType === Node.COMMENT_NODE) comments.push(n);
      else if (xmlText(n.data) !== n.data) n.data = xmlText(n.data);
    }
    comments.forEach((c) => c.remove());
  }

  /* 表示領域に対して置かれる要素（fixed）と、スクロールで貼り付く要素
     （sticky）は、複製がスクロールしていないので、そのままだと画面と違う
     所に写る。複製の側で、今の見た目の位置に置き直す。複製と表示中の文書は
     同じ構造なので、要素を文書の順に突き合わせる（複製から要素を外す前に呼ぶ）。
     入れ子のものは外側だけを置き直す */
  function freezeScrollDependent(root, clone, win) {
    const live = root.getElementsByTagName('*');
    const copy = clone.getElementsByTagName('*');
    if (live.length !== copy.length) return;
    const done = [];
    for (let i = 0; i < live.length; i++) {
      const el = live[i];
      const pos = win.getComputedStyle(el).position;
      if (pos !== 'fixed' && pos !== 'sticky') continue;
      if (done.some((d) => d.contains(el))) continue;
      done.push(el);
      const fix = pos === 'fixed' ? fixedPlacement(el, win) : stickyPlacement(el, win);
      if (fix) {
        copy[i].setAttribute('style', (copy[i].getAttribute('style') || '') + ';' + fix);
      }
    }
  }

  /* fixed を、今の表示位置に absolute で置く宣言。offsetLeft / offsetTop は
     fixed では表示領域に対する位置（transform を含まない）なので、
     transform はそのまま残して重ねがけにならない */
  function fixedPlacement(el, win) {
    let ox = 0;
    let oy = 0;
    for (let a = el.parentElement; a; a = a.parentElement) {
      const s = win.getComputedStyle(a);
      // transform などを持つ祖先があると、fixed はその祖先に対して置かれる。
      // 複製でも同じ祖先に対して置かれるので、置き直さなくてよい
      if (s.transform !== 'none' || s.filter !== 'none' ||
          s.perspective !== 'none' || /paint|layout|strict|content/.test(s.contain) ||
          /transform|filter|perspective/.test(s.willChange)) {
        return '';
      }
    }
    // absolute にすると、位置指定のある最も近い祖先が基準になる
    for (let a = el.parentElement; a && a !== el.ownerDocument.documentElement;
      a = a.parentElement) {
      if (win.getComputedStyle(a).position !== 'static') {
        const b = a.getBoundingClientRect();
        ox = b.left + win.scrollX + a.clientLeft;
        oy = b.top + win.scrollY + a.clientTop;
        break;
      }
    }
    const left = el.offsetLeft + win.scrollX - ox;
    const top = el.offsetTop + win.scrollY - oy;
    return 'position:absolute!important;inset:auto!important;left:' + left +
      'px!important;top:' + top + 'px!important;width:' + el.offsetWidth +
      'px!important;height:' + el.offsetHeight + 'px!important;margin:0!important;' +
      'box-sizing:border-box!important;min-width:0!important;max-width:none!important;' +
      'min-height:0!important;max-height:none!important';
  }

  /* 貼り付いている sticky を、流れの上の位置からのずれで relative に置く宣言。
     流れの上の位置は、一瞬だけ static にして測る（同じ処理の中で元に戻すので
     画面には出ない）。内側の要素のスクロールで貼り付くものは扱わない */
  function stickyPlacement(el, win) {
    const doc = el.ownerDocument;
    for (let a = el.parentElement; a && a !== doc.body && a !== doc.documentElement;
      a = a.parentElement) {
      const s = win.getComputedStyle(a);
      if (s.overflowX !== 'visible' || s.overflowY !== 'visible') return '';
    }
    const now = el.getBoundingClientRect();
    const saved = el.getAttribute('style');
    let flow;
    try {
      el.style.setProperty('position', 'static', 'important');
      flow = el.getBoundingClientRect();
    } finally {
      // Chrome は el.style の変更を style 属性へ遅れて書き戻すので、先に読んで
      // 確定させてから戻す（そうしないと後で style="" が残る）
      el.getAttribute('style');
      if (saved === null) el.removeAttribute('style');
      else el.setAttribute('style', saved);
    }
    const dx = now.left - flow.left;
    const dy = now.top - flow.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return '';
    return 'position:relative!important;inset:auto!important;left:' + dx +
      'px!important;top:' + dy + 'px!important';
  }

  /* 範囲（表示領域の座標）のスクリーンショットを PNG の data URL で返す。
     表示中の文書を複製して SVG の foreignObject に入れ、ブラウザ自身の
     描画で画像にしてから切り抜く。画像の大きさを表示領域（スクロールバーを
     含む innerWidth × innerHeight）と同じにし、viewBox で今のスクロール位置を
     映すので、幅で切り替わる CSS（@media）と vw / vh も表示と同じに効く。
     レイアウトの幅はスクロールバーを除いた clientWidth にする。rem は SVG の
     根要素が基準になるので、html の文字の大きさを移しておく。対象は
     スクリプトなしで表示しているので、複製と表示の見た目は一致する。
     このページが足した印（ハイライトのスタイル・画像の枠）は複製から外す */
  async function captureRegion(r) {
    const doc = frameDoc();
    const win = frameWin();
    const root = doc.documentElement;
    const lw = root.clientWidth;
    const iw = win.innerWidth;
    const ih = win.innerHeight;
    const sx = win.scrollX;
    const sy = win.scrollY;
    const dh = Math.max(root.scrollHeight, doc.body ? doc.body.scrollHeight : 0,
      sy + ih);
    const clone = root.cloneNode(true);
    freezeScrollDependent(root, clone, win);
    clone.querySelectorAll('script, link, [data-hle-style]').forEach((x) => x.remove());
    clone.querySelectorAll('[data-hle-obj]').forEach((x) => x.removeAttribute('data-hle-obj'));
    makeXmlSafe(clone);
    const xml = new XMLSerializer().serializeToString(clone);
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + iw +
      '" height="' + ih + '" viewBox="' + sx + ' ' + sy + ' ' + iw + ' ' + ih +
      '" style="font-size:' + win.getComputedStyle(root).fontSize + '">' +
      '<foreignObject x="0" y="0" width="' + lw + '" height="' + dh +
      '" style="overflow:visible">' + xml + '</foreignObject></svg>';
    const img = new Image();
    img.src = 'data:image/svg+xml;base64,' + base64Utf8(svg);
    await img.decode();
    const bg = pageBackground(doc, win);
    let scale = Math.min(SHOT_SCALE, SHOT_MAX_EDGE / Math.max(r.w, r.h));
    for (let attempt = 0; attempt < 4; attempt++) {
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.min(SHOT_MAX_EDGE, Math.round(r.w * scale)));
      canvas.height = Math.max(1, Math.min(SHOT_MAX_EDGE, Math.round(r.h * scale)));
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, r.x, r.y, r.w, r.h, 0, 0, canvas.width, canvas.height);
      const url = canvas.toDataURL('image/png');
      // 写真の多い範囲などで上限を超えたら、解像度を下げて作り直す
      if ((url.length - url.indexOf(',') - 1) * 0.75 <= SHOT_MAX_BYTES) return url;
      scale *= 0.7;
    }
    throw new Error('スクリーンショットが大きすぎます。範囲を小さくしてください');
  }

  /* 囲み終えた範囲をスクリーンショットにして、下書き欄を開く。
     下書きを書いている途中で囲み直した場合は、書いた文を残して範囲だけ替える */
  async function createRegionDraft(r) {
    pendingRect = r;
    paintRegions();
    try {
      const region = buildRegion(r);
      const texts = regionTexts(r);
      const shot = await captureRegion(r);
      draft = {
        cid: newCid(), target: 'region', region: region, texts: texts,
        quote: regionLabel(texts), shot: shot,
      };
      selectedObj = null;
      lastSel = null;
      pendingRect = null;
      showDraft('▭ ' + (draft.quote || '範囲（文字なし）'), shot, true);
    } catch (e) {
      pendingRect = null;
      paintRegions();
      alert('範囲のスクリーンショットを作れませんでした: ' +
        (e && e.message ? e.message : e));
    }
  }

  /* 範囲コメントの枠を、親ページ側の層に描き直す */
  function paintRegions() {
    const lay = layer();
    const doc = frameDoc();
    if (!lay || !session || !doc || !doc.documentElement) return;
    const root = doc.documentElement;
    // スクロールバーの上には重ねない（スクロールバーを操作できるように）
    lay.style.width = root.clientWidth + 'px';
    lay.style.height = root.clientHeight + 'px';
    const boxes = [];
    const cur = session.revisions.length - 1;
    session.comments.forEach((c, i) => {
      if (c.target !== 'region') return;
      // 絞り込みで隠れている範囲コメントは枠も描かない
      if (!commentVisible(c)) return;
      const r = regionClientRect(c.region);
      if (!r) return;
      // 前の稿で囲んだ範囲は、今の稿では図が書き換わっていることがある。
      // 同じ位置に描くが、見た目と番号で前の稿のものだと分かるようにする
      const stale = typeof c.revision === 'number' && c.revision < cur;
      boxes.push({
        r: r, cid: c.id,
        label: 'C' + (i + 1) + (stale ? '・第' + (c.revision + 1) + '稿' : ''),
        cls: (c.id === activeCid ? 'active' : '') + (stale ? ' stale' : ''),
      });
    });
    if (draft && draft.target === 'region') {
      const r = regionClientRect(draft.region);
      if (r) boxes.push({ r: r, cls: 'draft' });
    }
    const live = drawing ? normRect(drawing) : pendingRect;
    if (live) boxes.push({ r: live, cls: 'draft' });
    lay.textContent = '';
    boxes.forEach((b) => {
      const el = document.createElement('div');
      el.className = ('region-box ' + (b.cls || '')).trim();
      el.style.left = b.r.x + 'px';
      el.style.top = b.r.y + 'px';
      el.style.width = b.r.w + 'px';
      el.style.height = b.r.h + 'px';
      if (b.cid) {
        el.dataset.cid = b.cid;
        const tag = document.createElement('span');
        tag.className = 'region-tag';
        tag.textContent = b.label;
        tag.title = 'クリックでコメントを開く';
        // 範囲指定モード中でも、番号を押したときは囲み始めない
        tag.addEventListener('pointerdown', (e) => e.stopPropagation());
        tag.addEventListener('click', () => activateComment(b.cid, false));
        el.appendChild(tag);
      }
      lay.appendChild(el);
    });
  }

  let paintQueued = false;
  function schedulePaintRegions() {
    if (paintQueued) return;
    paintQueued = true;
    requestAnimationFrame(() => {
      paintQueued = false;
      paintRegions();
    });
  }

  /* 表示領域の座標 (x, y) にある範囲コメント（重なっていれば後のもの） */
  function regionAt(x, y) {
    let found = null;
    session.comments.forEach((c) => {
      if (c.target !== 'region') return;
      const r = regionClientRect(c.region);
      if (r && x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) {
        found = c.id;
      }
    });
    return found;
  }

  /* 範囲が表示領域の中央に来るよう iframe をスクロールする */
  function scrollToRegion(region) {
    const r = regionClientRect(region);
    const win = frameWin();
    if (!r || !win) return;
    win.scrollTo({
      left: win.scrollX + r.x - Math.max(0, (win.innerWidth - r.w) / 2),
      top: win.scrollY + r.y - Math.max(0, (win.innerHeight - r.h) / 2),
      behavior: 'smooth',
    });
  }

  /* ---------- ポーリング（AI返信・新リビジョン検知） ---------- */
  function sidebarBusy() {
    const el = document.activeElement;
    return el && ($('#sidebar').contains(el)) &&
      (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') &&
      (el.value || '').length > 0;
  }

  async function pollRemote() {
    if (!session) return;
    let remote;
    try {
      remote = await api('/api/sessions/' + sid);
    } catch (e) { return; }
    if (remote.revisions.length !== session.revisions.length) {
      location.reload();
      return;
    }
    if (remote.status !== session.status) {
      session.status = remote.status;
      renderMeta();
    }
    const localC = JSON.stringify(session.comments);
    // 編集中(editing)や下書き中は再描画するとフォームが消えるので見送る
    if (JSON.stringify(remote.comments) !== localC && !sidebarBusy() &&
        !draft && !editing) {
      session.comments = remote.comments;
      renderComments();
      applyHighlights();
    }
  }

  async function completeReview() {
    try {
      await api('/api/sessions/' + sid + '/status', 'POST',
        { status: 'reviewed' });
      session.status = 'reviewed';
      renderMeta();
    } catch (e) {
      alert('完了処理に失敗しました: ' + e.message);
    }
  }

  const m = location.pathname.match(/^\/session\/([A-Za-z0-9_-]+)$/);
  if (m) init(m[1]);
})();

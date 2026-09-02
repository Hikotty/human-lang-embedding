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

  async function api(path, method, body) {
    const opts = { method: method || 'GET', headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
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
  let anchors = new Map(); // cid -> Range（現在のiframe DOM上の位置）

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
    renderComments();
    frame().addEventListener('load', onFrameLoad);
    frame().src = '/api/sessions/' + sid + '/source';
    $('#btn-comment').addEventListener('click', startCommentDraft);
    $('#btn-complete').addEventListener('click', completeReview);
    $('#draft-submit').addEventListener('click', submitDraft);
    $('#draft-cancel').addEventListener('click', cancelDraft);
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
      '::highlight(hle-draft){background:rgba(255,152,0,.5);}';
    (doc.head || doc.body).appendChild(style);
    doc.addEventListener('selectionchange', captureSelection);
    doc.addEventListener('click', onFrameClick);
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

  /* 全コメントの位置を解決し直してハイライトを塗る */
  function applyHighlights() {
    anchors = new Map();
    const doc = frameDoc();
    if (!doc || !doc.body) return;
    const idx = textIndex();
    session.comments.forEach((c) => {
      const pos = locate(idx, c);
      if (!pos) return;
      const r = rangeFromOffsets(idx, pos.start, pos.end);
      if (r) anchors.set(c.id, r);
    });
    paintHighlights();
  }

  function paintHighlights() {
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
    } catch (e) {
      // body外（head等）を含む選択は位置が特定できないため無視する
    }
  }

  /* ハイライト済みの本文をクリックしたら対応するコメントを選択する */
  function onFrameClick(e) {
    const doc = frameDoc();
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
  function startCommentDraft() {
    captureSelection(); // 選択が残っていれば最新の内容に更新する
    if (!lastSel) {
      alert('表示されたページの、コメントを付けたい文字列を' +
        'ドラッグで選択してください');
      return;
    }
    const cid = 'c' + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 6);
    draft = Object.assign({ cid: cid }, lastSel);
    paintHighlights();
    $('#comment-draft').hidden = false;
    $('#draft-quote').textContent = draft.quote;
    $('#draft-text').value = '';
    $('#draft-text').focus();
  }

  async function submitDraft() {
    const text = $('#draft-text').value.trim();
    if (!text) { $('#draft-text').focus(); return; }
    try {
      const comment = await api('/api/sessions/' + sid + '/comments', 'POST', {
        id: draft.cid, quote: draft.quote, prefix: draft.prefix,
        suffix: draft.suffix, text: text, author: 'human',
      });
      session.comments.push(comment);
      draft = null;
      lastSel = null;
      $('#comment-draft').hidden = true;
      renderComments();
      applyHighlights();
    } catch (e) {
      alert('コメントの保存に失敗しました: ' + e.message);
    }
  }

  function cancelDraft() {
    draft = null;
    $('#comment-draft').hidden = true;
    paintHighlights();
  }

  function authorLabel(a) {
    return a === 'ai' ? '<span class="who ai">AI</span>'
      : '<span class="who human">あなた</span>';
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

  function editFormHtml(cid, rid, text) {
    const key = targetKey(cid, rid);
    return '<textarea class="edit-text" rows="3" data-edit-input="' + key +
      '">' + esc(text) + '</textarea>' +
      '<div class="card-actions edit-actions">' +
      '<button class="primary" data-edit-save="' + key + '">保存</button>' +
      '<button data-edit-cancel="1">キャンセル</button></div>';
  }

  function bodyHtml(cid, rid, o) {
    if (isEditing(cid, rid)) return editFormHtml(cid, rid, o.text);
    const key = targetKey(cid, rid);
    return '<div class="body">' + esc(o.text) + '</div>' +
      '<div class="card-actions">' +
      '<button data-edit="' + key + '">編集</button>' +
      '<button class="danger" data-del="' + key + '">削除</button></div>';
  }

  function renderComments() {
    const list = $('#comment-list');
    if (!session.comments.length) {
      list.innerHTML = '<div class="empty-note">コメントはまだありません。<br>' +
        '表示されたページの文字列を選択して「💬 コメント」を押すと追加できます。' +
        '</div>';
      return;
    }
    list.innerHTML = session.comments.map((c, i) => {
      const replies = (c.replies || []).map((r) =>
        '<div class="reply">' + authorLabel(r.author) +
        '<span class="when">' + esc(fmtDate(r.created_at)) + '</span>' +
        editedMark(r) + bodyHtml(c.id, r.id, r) + '</div>'
      ).join('');
      return '<div class="comment-card' +
        (c.id === activeCid ? ' active' : '') +
        '" data-cid="' + esc(c.id) + '">' +
        '<div class="quote">C' + (i + 1) + ': ' + esc(c.quote) + '</div>' +
        authorLabel(c.author) +
        '<span class="when">' + esc(fmtDate(c.created_at)) + '</span>' +
        editedMark(c) + bodyHtml(c.id, null, c) +
        replies +
        '<div class="reply-row"><input placeholder="返信を入力" data-reply="' +
        esc(c.id) + '"><button data-send="' + esc(c.id) + '">送信</button></div>' +
        '</div>';
    }).join('');
    list.querySelectorAll('.comment-card').forEach((card) => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('input,button,textarea')) return;
        activateComment(card.dataset.cid, true);
      });
    });
    list.querySelectorAll('button[data-send]').forEach((btn) => {
      btn.addEventListener('click', () => sendReply(btn.dataset.send));
    });
    list.querySelectorAll('input[data-reply]').forEach((inp) => {
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing) sendReply(inp.dataset.reply);
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
    try {
      const updated = await api(path, 'POST', { text: text });
      const c = session.comments.find((x) => x.id === t.cid);
      if (c && t.rid) {
        const r = (c.replies || []).find((x) => x.id === t.rid);
        if (r) Object.assign(r, updated);
      } else if (c) {
        Object.assign(c, updated);
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
    const inp = document.querySelector('input[data-reply="' + cid + '"]');
    const text = (inp.value || '').trim();
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
    const target = scrollFrame ? anchors.get(cid) : null;
    if (target) {
      const el = target.startContainer.nodeType === Node.ELEMENT_NODE
        ? target.startContainer : target.startContainer.parentElement;
      if (el && el.scrollIntoView) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }
    if (!scrollFrame) {
      const card = document.querySelector(
        '.comment-card[data-cid="' + cid + '"]');
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
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

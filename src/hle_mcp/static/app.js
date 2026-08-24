/* app.js — 画面制御・自動保存・コメントUI・ポーリング */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

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
    return (iso || '').replace('T', ' ').replace(/([+-]\d{2}:\d{2}|Z)$/, '');
  }

  function esc(s) {
    const div = document.createElement('div');
    div.textContent = s == null ? '' : String(s);
    return div.innerHTML;
  }

  /* ================= エディタ画面 ================= */
  let session = null;
  let sid = null;
  let dirty = false;
  let saveTimer = null;
  let draftCid = null;
  let activeCid = null;

  async function initEditorView(id) {
    sid = id;
    $('#editor-view').hidden = false;
    try {
      session = await api('/api/sessions/' + sid);
    } catch (e) {
      $('#doc-title').textContent = 'セッションが見つかりません: ' + sid;
      return;
    }
    document.title = session.title + ' - 校正レビュー';
    const rev = session.revisions[session.revisions.length - 1];
    Tracker.init($('#editor'), rev.base_text, rev.edited_html, onEdited,
      { sessionId: sid, onImageFile: handleImageFile });
    renderMeta();
    renderComments();
    $('#btn-image').addEventListener('click', () => $('#image-input').click());
    $('#image-input').addEventListener('change', (e) => {
      if (e.target.files && e.target.files.length) {
        handleImageFile(e.target.files[0]);
        e.target.value = '';
      }
    });
    $('#btn-comment').addEventListener('click', startCommentDraft);
    $('#btn-undo').addEventListener('click', () => Tracker.undo());
    $('#btn-redo').addEventListener('click', () => Tracker.redo());
    $('#btn-complete').addEventListener('click', completeReview);
    $('#draft-submit').addEventListener('click', submitDraft);
    $('#draft-cancel').addEventListener('click', cancelDraft);
    $('#editor').addEventListener('click', (e) => {
      const span = e.target.closest && e.target.closest('.cmt');
      if (span) activateComment(span.dataset.cid, false);
    });
    setInterval(pollRemote, 2500);
    window.addEventListener('beforeunload', () => {
      if (dirty) {
        navigator.sendBeacon(
          '/api/sessions/' + sid + '/document',
          new Blob([JSON.stringify({ html: Tracker.html() })],
            { type: 'application/json' })
        );
      }
    });
    setSaveState('');
  }

  function renderMeta() {
    $('#doc-title').textContent = session.title;
    const revNo = session.revisions.length;
    const st = session.status === 'reviewed'
      ? '<span class="badge reviewed">校正完了</span>'
      : '<span class="badge">校正中</span>';
    $('#doc-meta').innerHTML = st +
      ' ID: ' + esc(session.id) + '　第' + revNo + '稿' +
      (session.workspace ? '　' + esc(session.workspace) : '');
    const done = session.status === 'reviewed';
    $('#btn-complete').classList.toggle('done', done);
    $('#btn-complete').textContent = done ? '✔ 校正完了済み' : '✔ 校正完了';
  }

  /* ---------- 保存 ---------- */
  function onEdited() {
    dirty = true;
    setSaveState('未保存…');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDoc, 700);
  }

  async function saveDoc() {
    if (!dirty) return;
    dirty = false;
    setSaveState('保存中…');
    try {
      await api('/api/sessions/' + sid + '/document', 'POST',
        { html: Tracker.html() });
      setSaveState(dirty ? '未保存…' : '保存済み');
    } catch (e) {
      dirty = true;
      setSaveState('保存失敗');
    }
  }

  function setSaveState(text) { $('#save-state').textContent = text; }

  /* ---------- 画像 ---------- */
  async function handleImageFile(file) {
    if (!file || file.type.indexOf('image/') !== 0) {
      alert('画像ファイルを指定してください');
      return;
    }
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(fr.result);
        fr.onerror = () => reject(new Error('読み込み失敗'));
        fr.readAsDataURL(file);
      });
      const base64 = String(dataUrl).split(',', 2)[1] || '';
      const res = await api('/api/sessions/' + sid + '/images', 'POST',
        { name: file.name || 'pasted.png', data_base64: base64 });
      Tracker.insertImageParagraph(res.name);
      onEdited();
    } catch (e) {
      alert('画像の追加に失敗しました: ' + e.message);
    }
  }

  /* ---------- コメント ---------- */
  function startCommentDraft() {
    const r = Tracker.getRange();
    if (!r || r.collapsed) {
      alert('コメントを付けたい範囲をドラッグで選択してください');
      return;
    }
    if (draftCid) cancelDraft();
    const quote = r.toString().replace(/\u200B/g, '').slice(0, 300);
    const cid = 'c' + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 6);
    if (!Tracker.wrapRangeComment(r.cloneRange(), cid)) return;
    draftCid = cid;
    $('#comment-draft').hidden = false;
    $('#draft-quote').textContent = quote;
    $('#draft-text').value = '';
    $('#draft-text').focus();
  }

  async function submitDraft() {
    const text = $('#draft-text').value.trim();
    if (!text) { $('#draft-text').focus(); return; }
    const quote = $('#draft-quote').textContent;
    try {
      const comment = await api('/api/sessions/' + sid + '/comments', 'POST',
        { id: draftCid, quote: quote, text: text, author: 'human' });
      session.comments.push(comment);
      draftCid = null;
      $('#comment-draft').hidden = true;
      dirty = true;
      await saveDoc();
      renderComments();
    } catch (e) {
      alert('コメントの保存に失敗しました: ' + e.message);
    }
  }

  function cancelDraft() {
    if (draftCid) Tracker.unwrapComment(draftCid);
    draftCid = null;
    $('#comment-draft').hidden = true;
    onEdited();
  }

  function authorLabel(a) {
    return a === 'ai' ? '<span class="who ai">AI</span>'
      : '<span class="who human">あなた</span>';
  }

  function renderComments() {
    const list = $('#comment-list');
    if (!session.comments.length) {
      list.innerHTML = '<div class="empty-note">コメントはまだありません。<br>' +
        '本文を選択して「💬 コメント」を押すと追加できます。</div>';
      return;
    }
    list.innerHTML = session.comments.map((c, i) => {
      const replies = (c.replies || []).map((r) =>
        '<div class="reply">' + authorLabel(r.author) +
        '<span class="when">' + esc(fmtDate(r.created_at)) + '</span>' +
        '<div class="body">' + esc(r.text) + '</div></div>'
      ).join('');
      return '<div class="comment-card' +
        (c.id === activeCid ? ' active' : '') +
        '" data-cid="' + esc(c.id) + '">' +
        '<div class="quote">C' + (i + 1) + ': ' + esc(c.quote) + '</div>' +
        authorLabel(c.author) +
        '<span class="when">' + esc(fmtDate(c.created_at)) + '</span>' +
        '<div class="body">' + esc(c.text) + '</div>' +
        replies +
        '<div class="reply-row"><input placeholder="返信を入力" data-reply="' +
        esc(c.id) + '"><button data-send="' + esc(c.id) + '">送信</button></div>' +
        '</div>';
    }).join('');
    list.querySelectorAll('.comment-card').forEach((card) => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('input,button')) return;
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

  function activateComment(cid, scrollEditor) {
    activeCid = cid;
    document.querySelectorAll('#editor .cmt').forEach((s) =>
      s.classList.toggle('active', s.dataset.cid === cid));
    document.querySelectorAll('.comment-card').forEach((card) =>
      card.classList.toggle('active', card.dataset.cid === cid));
    const target = scrollEditor
      ? document.querySelector('#editor .cmt[data-cid="' + cid + '"]')
      : document.querySelector('.comment-card[data-cid="' + cid + '"]');
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
      if (dirty) await saveDoc();
      location.reload();
      return;
    }
    if (remote.status !== session.status) {
      session.status = remote.status;
      renderMeta();
    }
    const localC = JSON.stringify(session.comments);
    if (JSON.stringify(remote.comments) !== localC && !sidebarBusy() && !draftCid) {
      session.comments = remote.comments;
      renderComments();
    }
  }

  async function completeReview() {
    clearTimeout(saveTimer);
    dirty = true;
    await saveDoc();
    try {
      await api('/api/sessions/' + sid + '/status', 'POST',
        { status: 'reviewed' });
      session.status = 'reviewed';
      renderMeta();
    } catch (e) {
      alert('完了処理に失敗しました: ' + e.message);
    }
  }

  /* ================= 一覧画面 ================= */
  async function initListView() {
    $('#list-view').hidden = false;
    document.title = '校正レビュー一覧';
    const render = async () => {
      const q = encodeURIComponent($('#search-query').value || '');
      const w = encodeURIComponent($('#search-workspace').value || '');
      const data = await api('/api/sessions?query=' + q + '&workspace=' + w);
      const rows = data.sessions.map((s) =>
        '<tr><td><a href="/session/' + esc(s.id) + '">' + esc(s.title) +
        '</a></td><td>' + esc(s.id) + '</td><td>' +
        (s.status === 'reviewed' ? '校正完了' : '校正中') +
        '</td><td>' + s.revision_count + '</td><td>' + s.comment_count +
        '</td><td>' + esc(s.workspace) + '</td><td>' +
        esc(fmtDate(s.updated_at)) + '</td></tr>'
      ).join('');
      $('#session-table').innerHTML =
        '<tr><th>タイトル</th><th>ID</th><th>状態</th><th>稿</th>' +
        '<th>コメント</th><th>ワークスペース</th><th>更新日時</th></tr>' +
        (rows || '<tr><td colspan="7" class="empty-note">セッションがありません</td></tr>');
    };
    let t = null;
    ['#search-query', '#search-workspace'].forEach((sel) => {
      $(sel).addEventListener('input', () => {
        clearTimeout(t);
        t = setTimeout(render, 300);
      });
    });
    await render();
  }

  /* ================= 起動 ================= */
  const m = location.pathname.match(/^\/session\/([A-Za-z0-9_-]+)$/);
  if (m) {
    initEditorView(m[1]);
  } else {
    initListView();
  }
})();

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

  /* ================= エディタ画面 ================= */
  let session = null;
  let sid = null;
  let dirty = false;
  let saveTimer = null;
  let draftCid = null;
  let draftTarget = null; // 下書き中のコメント対象 { target, quote, image }
  let activeCid = null;
  let editing = null; // 編集中の対象 { cid, rid|null }

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
      { sessionId: sid, onImageFile: handleImageFile,
        onImageCommentsDropped: dropImageComments });
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
      if (!e.target.closest) return;
      const span = e.target.closest('.cmt');
      if (span) { activateComment(span.dataset.cid, false); return; }
      // コメント付きの画像をクリックしたら対応するカードを開く
      const p = e.target.closest('.img-para[data-cids]');
      const cid = p ? (p.dataset.cids || '').split(/\s+/)[0] : '';
      if (cid) activateComment(cid, false);
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
  function newCid() {
    return 'c' + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 6);
  }

  /* テキストの選択範囲、または選択中の画像にコメントを付ける。
     画像はドラッグ選択できないので、クリックで選んだ画像を対象にする。 */
  function startCommentDraft() {
    const r = Tracker.getRange();
    const hasRange = !!r && !r.collapsed;
    const img = hasRange ? null : Tracker.getSelectedImage();
    if (!hasRange && !img) {
      alert('コメントを付けたい範囲をドラッグで選択するか、\n' +
        '画像をクリックして選択してください');
      return;
    }
    if (draftCid) cancelDraft();
    const cid = newCid();
    if (img) {
      if (!Tracker.addImageComment(cid)) return;
      draftTarget = { target: 'image', quote: img.md, image: img.name };
      showDraft(cid, '🖼 ' + img.name);
    } else {
      const quote = r.toString().replace(/\u200B/g, '').slice(0, 300);
      if (!Tracker.wrapRangeComment(r.cloneRange(), cid)) return;
      draftTarget = { target: 'text', quote: quote, image: '' };
      showDraft(cid, quote);
    }
  }

  function showDraft(cid, label) {
    draftCid = cid;
    $('#comment-draft').hidden = false;
    $('#draft-quote').textContent = label;
    $('#draft-text').value = '';
    $('#draft-text').focus();
  }

  async function submitDraft() {
    const text = $('#draft-text').value.trim();
    if (!text) { $('#draft-text').focus(); return; }
    const t = draftTarget || { target: 'text', quote: '', image: '' };
    try {
      const comment = await api('/api/sessions/' + sid + '/comments', 'POST',
        { id: draftCid, quote: t.quote, text: text, author: 'human',
          target: t.target, image: t.image });
      session.comments.push(comment);
      draftCid = null;
      draftTarget = null;
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
    draftTarget = null;
    $('#comment-draft').hidden = true;
    onEdited();
  }

  /* 参照先の画像が消えて宛先を失ったコメントを片付ける */
  async function dropImageComments(cids) {
    for (const cid of cids) {
      try {
        await api('/api/sessions/' + sid + '/comments/' + cid + '/delete',
          'POST', {});
      } catch (e) { /* 既に無い場合は無視する */ }
      session.comments = session.comments.filter((x) => x.id !== cid);
      if (activeCid === cid) activeCid = null;
      if (editing && editing.cid === cid) editing = null;
    }
    renderComments();
  }

  function authorLabel(a) {
    return a === 'ai' ? '<span class="who ai">AI</span>'
      : '<span class="who human">あなた</span>';
  }

  /* ---------- コメント/返信の編集・削除 ---------- */
  /* 対象は "コメントID" または "コメントID|返信ID" のキーで指す。
     コメントIDは [A-Za-z0-9_-] に制限されているので "|" と衝突しない。 */
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

  /* コメントの対象表示。画像コメントはサムネイルで何を指しているか示す */
  function quoteHtml(c, n) {
    if (c.target === 'image' && c.image) {
      const url = '/api/sessions/' + encodeURIComponent(sid) +
        '/files/' + encodeURIComponent(c.image);
      return '<div class="quote img-quote"><img src="' + esc(url) +
        '" alt=""><span>C' + n + ': ' + esc(c.image) + '</span></div>';
    }
    return '<div class="quote">C' + n + ': ' + esc(c.quote) + '</div>';
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
        '本文を選択して「💬 コメント」を押すと追加できます。</div>';
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
        quoteHtml(c, i + 1) +
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
        // 本文に残るハイライトを外し、編集HTMLを保存し直す（孤児span対策）
        Tracker.unwrapComment(t.cid);
        dirty = true;
        await saveDoc();
      }
      if (editing && editing.cid === t.cid) editing = null;
      renderComments();
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

  function activateComment(cid, scrollEditor) {
    activeCid = cid;
    document.querySelectorAll('#editor .cmt').forEach((s) =>
      s.classList.toggle('active', s.dataset.cid === cid));
    document.querySelectorAll('#editor .img-para').forEach((p) =>
      p.classList.toggle('cmt-active',
        (p.dataset.cids || '').split(/\s+/).indexOf(cid) >= 0));
    document.querySelectorAll('.comment-card').forEach((card) =>
      card.classList.toggle('active', card.dataset.cid === cid));
    const target = scrollEditor
      ? (document.querySelector('#editor .cmt[data-cid="' + cid + '"]') ||
         document.querySelector('#editor .img-para[data-cids~="' + cid + '"]'))
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
    // 編集中(editing)は再描画するとフォームが消えるので取り込みを見送る
    if (JSON.stringify(remote.comments) !== localC && !sidebarBusy() &&
        !draftCid && !editing) {
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

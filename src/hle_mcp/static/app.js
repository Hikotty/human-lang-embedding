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
    noteServerVersion(res.headers.get('X-HLE-Version'));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  /* ---------- 新しい版の通知 ----------
     常駐Webサーバーはコードが更新されると無停止で新しい版に入れ替わるが、
     開いているページのJSは古いまま。応答ヘッダの版が読み込み時と変わったら
     再読み込みを促す（自動では読み込み直さない。入力中を邪魔しないため） */
  let loadedVersion = null;
  let updateNoticeShown = false;

  function noteServerVersion(v) {
    if (!v) return;
    if (loadedVersion === null) { loadedVersion = v; return; }
    if (v === loadedVersion || updateNoticeShown || !session) return;
    const banner = document.getElementById('update-banner');
    if (!banner) return;
    updateNoticeShown = true;
    banner.hidden = false;
  }

  async function reloadForUpdate() {
    // 保存を済ませてから読み込み直す。保存できなくてもブラウザに退避して
    // あれば読み込み後に復元されるので進めてよい
    const saved = await saveDoc();
    if (!saved && !backupOk) {
      alert('保存できていない校正があるため、今は再読み込みできません。\n' +
        '接続が戻ると自動で保存されます。');
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

  /* ================= エディタ画面 ================= */
  let session = null;
  let sid = null;
  let revIndex = 0;   // 編集している稿の番号（保存先の取り違えを防ぐ）
  let dirty = false;
  let saveTimer = null;
  let draftCid = null;
  let draftTarget = null; // 下書き中のコメント対象 { target, quote, image }
  let activeCid = null;
  let editing = null; // 編集中の対象 { cid, rid|null }
  let commentFilter = 'all'; // コメントの絞り込み（'all' | 'human' | 'ai'）

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
    revIndex = rev.index;
    const initialHtml = await takeBackup(rev);
    // 本文の ![](名前) を画像・動画・音声に描き分けるので、形式の表を先に取る
    await loadMediaConfig();
    Tracker.init($('#editor'), rev.base_text, initialHtml.html, onEdited,
      { sessionId: sid, onImageFile: handleMediaFiles,
        onImageCommentsDropped: dropImageComments, mediaKind: mediaKind });
    renderMeta();
    initCommentFilter();
    renderComments();
    anchorAiComments();
    $('#btn-update-reload').addEventListener('click', reloadForUpdate);
    $('#btn-image').addEventListener('click', () => $('#image-input').click());
    $('#image-input').addEventListener('change', (e) => {
      if (e.target.files && e.target.files.length) {
        handleMediaFiles(Array.from(e.target.files), null);
        e.target.value = '';
      }
    });
    // 本文の外にファイルを落としてもページを離れない（ブラウザの既定は
    // そのファイルを開く）。本文の上へのドロップは Tracker が添付として受ける
    ['dragover', 'drop'].forEach((type) => {
      window.addEventListener(type, (e) => {
        const types = e.dataTransfer ? e.dataTransfer.types : null;
        if (types && Array.prototype.indexOf.call(types, 'Files') >= 0 &&
            !e.defaultPrevented) {
          e.preventDefault();
          if (type === 'dragover') e.dataTransfer.dropEffect = 'none';
        }
      });
    });
    $('#btn-comment').addEventListener('click', startCommentDraft);
    $('#btn-undo').addEventListener('click', () => Tracker.undo());
    $('#btn-redo').addEventListener('click', () => Tracker.redo());
    initAiDiff();
    $('#btn-complete').addEventListener('click', completeReview);
    $('#draft-submit').addEventListener('click', submitDraft);
    $('#draft-cancel').addEventListener('click', cancelDraft);
    $('#editor').addEventListener('click', (e) => {
      clearJumpHighlight();
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
        // 送れなくてもブラウザに退避してあるので、次に開いたとき復元される
        navigator.sendBeacon(
          '/api/sessions/' + sid + '/document',
          new Blob([JSON.stringify({ html: Tracker.html(), revision: revIndex })],
            { type: 'application/json' })
        );
      }
    });
    setSaveState('');
    if (initialHtml.restored) {
      dirty = true;
      toast('保存できていなかった校正をブラウザから復元しました');
      saveDoc();
    }
    showLateNotice();
  }

  /* ---------- AI差分表示 ---------- */
  /* 前の稿の最終状態（人間の校正を反映済み）と今の稿を比べ、AIが変えた
     箇所を青で重ねる。人間の赤い変更と入り混じって読みにくくなり得るので
     ボタンで気軽に出し入れできるようにし、状態はセッションごとに覚える。
     第1稿には比較対象が無いのでボタン自体を出さない */
  let aiDiffOn = false;

  function setAiDiff(on) {
    aiDiffOn = on;
    const count = Tracker.setAiDiff(on ? session.prev_result_text : null);
    $('#btn-aidiff').classList.toggle('on', on);
    try {
      if (on) localStorage.setItem('hle-aidiff-' + sid, '1');
      else localStorage.removeItem('hle-aidiff-' + sid);
    } catch (e) { /* localStorage が使えない環境では覚えない */ }
    if (on && count === 0) toast('前の稿からAIによる変更はありません');
  }

  function initAiDiff() {
    if (typeof session.prev_result_text !== 'string') return;
    const btn = $('#btn-aidiff');
    btn.hidden = false;
    btn.addEventListener('click', () => setAiDiff(!aiDiffOn));
    let saved = null;
    try { saved = localStorage.getItem('hle-aidiff-' + sid); } catch (e) { /* 同上 */ }
    if (saved === '1') setAiDiff(true);
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

  /* ---------- ブラウザへの退避 ----------
     校正内容はサーバーが保存を受け付けるまでブラウザ（localStorage）にも
     置いておく。サーバーの入れ替え・停止中に打った分や、保存前にタブを
     閉じた分も、次に開いたときに復元してから保存し直せる。 */
  let backupOk = true; // 退避できているか（できない環境では再読み込みを慎重にする）

  function backupKey() { return 'hle-backup-' + sid; }

  function readBackup() {
    try {
      const b = JSON.parse(localStorage.getItem(backupKey()) || 'null');
      return b && typeof b.html === 'string' && typeof b.rev === 'number' ? b : null;
    } catch (e) { return null; }
  }

  function writeBackup() {
    try {
      localStorage.setItem(backupKey(), JSON.stringify(
        { rev: revIndex, html: Tracker.html(), at: Date.now() }));
      backupOk = true;
    } catch (e) {
      backupOk = false;
    }
  }

  function dropBackupIfSaved(html) {
    const b = readBackup();
    if (b && b.rev === revIndex && b.html === html) {
      try { localStorage.removeItem(backupKey()); } catch (e) { /* 無視 */ }
    }
  }

  /* 読み込み時に退避分を確かめ、エディタに載せるHTMLを決める。
     同じ稿への退避がサーバーの内容と違えば、保存できていなかった分なので
     そちらを使う。前の稿への退避（AIが次の稿を出す前に保存できなかった分）は
     新しい稿には載せられないので、前の稿の控えとしてサーバーに預けて消す */
  async function takeBackup(rev) {
    const b = readBackup();
    if (!b) return { html: rev.edited_html, restored: false };
    if (b.rev === rev.index) {
      if (b.html !== (rev.edited_html || '')) return { html: b.html, restored: true };
    } else if (b.rev < rev.index) {
      try {
        await api('/api/sessions/' + sid + '/document', 'POST',
          { html: b.html, revision: b.rev });
        markLateNotice();
      } catch (e) {
        return { html: rev.edited_html, restored: false }; // 次の機会に預け直す
      }
    }
    try { localStorage.removeItem(backupKey()); } catch (e) { /* 無視 */ }
    return { html: rev.edited_html, restored: false };
  }

  /* 前の稿への校正が遅れて届いた（AIの新しい稿には反映されない）ことを、
     再読み込み後の画面で知らせる */
  function markLateNotice() {
    try { sessionStorage.setItem('hle-late-' + sid, '1'); } catch (e) { /* 無視 */ }
  }

  function showLateNotice() {
    let flag = null;
    try { flag = sessionStorage.getItem('hle-late-' + sid); } catch (e) { /* 無視 */ }
    if (!flag) return;
    try { sessionStorage.removeItem('hle-late-' + sid); } catch (e) { /* 無視 */ }
    toast('AIの新しい稿が届く前の校正の一部は、前の稿の控えとして保存しました' +
      '（新しい稿には反映されていません）');
  }

  /* ---------- 保存 ----------
     保存は1本ずつ直列に送る（並行すると古い内容が後から届いて新しい内容を
     上書きしうる）。失敗したら間隔を広げながら自動で再試行し、成功するまで
     「未保存」の状態とブラウザへの退避を保つ。 */
  const RETRY_STEPS = [1000, 2000, 4000, 8000, 15000];
  let saving = null;       // 送信中の保存（Promise）
  let saveAgain = false;   // 送信中に新しい保存要求が来た
  let retryTimer = null;
  let retryCount = 0;

  function onEdited() {
    dirty = true;
    writeBackup();
    clearJumpHighlight();
    if (!retryTimer) setSaveState('未保存…');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDoc, 700);
  }

  /* 保存を実行し、未保存の変更が残っていなければ true を返す */
  function saveDoc() {
    clearTimeout(saveTimer);
    if (saving) {
      saveAgain = true;
      return saving;
    }
    saving = runSaves().finally(() => { saving = null; });
    return saving;
  }

  async function runSaves() {
    do {
      saveAgain = false;
      if (!dirty) continue;
      dirty = false;
      clearTimeout(retryTimer);
      retryTimer = null;
      const html = Tracker.html();
      setSaveState('保存中…');
      let res;
      try {
        res = await api('/api/sessions/' + sid + '/document', 'POST',
          { html: html, revision: revIndex });
      } catch (e) {
        dirty = true;
        scheduleRetry();
        return false;
      }
      retryCount = 0;
      dropBackupIfSaved(html);
      if (res && res.stale) markLateNotice();
      setSaveState(dirty ? '未保存…' : '保存済み');
    } while (saveAgain);
    return !dirty;
  }

  function scheduleRetry() {
    const wait = RETRY_STEPS[Math.min(retryCount, RETRY_STEPS.length - 1)];
    retryCount++;
    setSaveState(backupOk ? '保存できません・再試行中…（ブラウザに退避済み）'
      : '保存できません・再試行中…', true);
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => { retryTimer = null; saveDoc(); }, wait);
  }

  function setSaveState(text, warn) {
    const el = $('#save-state');
    el.textContent = text;
    el.classList.toggle('warn', !!warn);
  }

  /* ---------- メディア（画像・動画・音声） ----------
     受け付ける形式と上限はサーバー（/api/media）が持つ。取れなかったときは
     画像だけを受け付ける（サーバー側でも同じ確認をする） */
  const MEDIA_ICONS = { image: '🖼', video: '🎬', audio: '🔊' };
  const FALLBACK_MEDIA = {
    types: { '.png': { kind: 'image', mime: 'image/png' },
      '.jpg': { kind: 'image', mime: 'image/jpeg' },
      '.jpeg': { kind: 'image', mime: 'image/jpeg' },
      '.gif': { kind: 'image', mime: 'image/gif' },
      '.webp': { kind: 'image', mime: 'image/webp' } },
    max_bytes: { image: 10 * 1024 * 1024 },
    labels: { image: '画像' },
  };
  let mediaConf = FALLBACK_MEDIA;

  async function loadMediaConfig() {
    try {
      mediaConf = await api('/api/media');
    } catch (e) {
      mediaConf = FALLBACK_MEDIA;
    }
    const input = $('#image-input');
    if (input) input.accept = Object.keys(mediaConf.types).join(',');
  }

  function extOf(name) {
    const m = /\.[^./\\]+$/.exec(String(name || ''));
    return m ? m[0].toLowerCase() : '';
  }

  function mediaKind(name) {
    const t = mediaConf.types[extOf(name)];
    return t ? t.kind : null;
  }

  function mediaIcon(name) {
    return MEDIA_ICONS[mediaKind(name) || 'image'];
  }

  function sizeText(n) {
    return n >= 1024 * 1024 * 1024
      ? +(n / (1024 * 1024 * 1024)).toPrecision(3) + 'GB'
      : +(n / (1024 * 1024)).toPrecision(3) + 'MB';
  }

  /* 貼り付けた画像には名前が無い（または image.png）ので、MIME から拡張子を補う */
  function uploadName(file) {
    const name = file.name || '';
    if (mediaKind(name)) return name;
    const sub = String(file.type || '').split('/')[1] || '';
    const ext = Object.keys(mediaConf.types).find(
      (x) => mediaConf.types[x].mime === file.type) || ('.' + sub);
    return (name.replace(/\.[^.]*$/, '') || 'pasted') + ext;
  }

  /* 送る前に形式と大きさを確かめる。大きすぎるファイルを送り始めると、
     サーバーが断っても送信の途中で接続が切れて理由が伝わらないため */
  function mediaProblem(file, name) {
    const kind = mediaKind(name);
    if (!kind) {
      const exts = Object.keys(mediaConf.types).join(' ');
      return '対応していない形式です: ' + (file.name || file.type || '(不明)') +
        '\n対応: ' + exts;
    }
    const limit = mediaConf.max_bytes[kind];
    if (limit && file.size > limit) {
      return (mediaConf.labels[kind] || kind) + 'は' + sizeText(limit) +
        '以下にしてください（' + (file.name || name) + ': ' +
        sizeText(file.size) + '）';
    }
    return '';
  }

  /* ファイル（貼り付け・ドロップ・🖼ボタン）を添付し、本文に段落として入れる。
     after を渡されたらその段落の後ろから順に、渡されなければキャレットの
     段落の後ろから順に並べる */
  async function handleMediaFiles(files, after) {
    const list = Array.from(files || []);
    const problems = [];
    const ok = [];
    list.forEach((f) => {
      const name = uploadName(f);
      const why = mediaProblem(f, name);
      if (why) problems.push(why); else ok.push({ file: f, name: name });
    });
    if (problems.length) alert(problems.join('\n\n'));
    let anchor = after || null;
    for (const item of ok) {
      toast(mediaIcon(item.name) + ' ' + item.name + ' を追加しています…');
      try {
        const res = await uploadMedia(item.file, item.name);
        anchor = Tracker.insertImageParagraph(res.name, anchor);
        onEdited();
        toast(mediaIcon(res.name) + ' ' + res.name + ' を追加しました');
      } catch (e) {
        alert('「' + item.name + '」の追加に失敗しました: ' + e.message);
      }
    }
  }

  /* ファイルの中身をそのままボディで送る（base64 にしないので大きな動画でも
     メモリと時間を倍にしない） */
  async function uploadMedia(file, name) {
    const res = await fetch('/api/sessions/' + encodeURIComponent(sid) +
      '/files?name=' + encodeURIComponent(name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    noteServerVersion(res.headers.get('X-HLE-Version'));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  /* ---------- コメント ---------- */
  function newCid() {
    return 'c' + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 6);
  }

  /* テキストの選択範囲、または選択中のメディア（画像・動画・音声）にコメントを
     付ける。メディアはドラッグ選択できないので、クリックで選んだものを対象にする。 */
  function startCommentDraft() {
    const r = Tracker.getRange();
    const hasRange = !!r && !r.collapsed;
    const img = hasRange ? null : Tracker.getSelectedImage();
    if (!hasRange && !img) {
      alert('コメントを付けたい範囲をドラッグで選択するか、\n' +
        '画像・動画・音声をクリックして選択してください');
      return;
    }
    if (draftCid) cancelDraft();
    const cid = newCid();
    if (img) {
      if (!Tracker.addImageComment(cid)) return;
      draftTarget = { target: 'image', quote: img.md, image: img.name };
      showDraft(cid, MEDIA_ICONS[img.kind || 'image'] + ' ' + img.name);
    } else {
      // AI差分の幽霊テキスト（.ai-del）と改行マーカーは引用に含めない
      const box = document.createElement('div');
      box.appendChild(r.cloneContents());
      box.querySelectorAll('.ai-del, .pb').forEach((el) => el.remove());
      const quote = box.textContent.replace(/\u200B/g, '').slice(0, 300);
      if (!Tracker.wrapRangeComment(r.cloneRange(), cid)) return;
      draftTarget = { target: 'text', quote: quote, image: '' };
      showDraft(cid, quote);
    }
  }

  function showDraft(cid, label) {
    draftCid = cid;
    $('#comment-draft').hidden = false;
    $('#draft-quote').textContent = label;
    // 種類は毎回「指摘」から始める（前の下書きの選択を持ち越さない）
    $('#draft-tag').innerHTML = tagSelectHtml('draft-tag', 'issue');
    bindTagSelect($('#draft-tag'));
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
          target: t.target, image: t.image, tag: selectedTag('draft-tag') });
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

  /* ---------- コメントの絞り込み（著者別） ----------
     「すべて / 人間 / AI」でコメントを絞る。判定はトップレベルコメントの
     著者（未指定=human）で、スレッド丸ごと（返信を含む）出し入れする。
     隠れたコメントは本文のハイライトも消す。ロードのたびに「すべて」に戻る
     （UIの checked 初期状態と一致）ので、ここでは状態を記憶しない */
  function initCommentFilter() {
    commentFilter = 'all';
    document.querySelectorAll('input[name="comment-filter"]').forEach((r) => {
      r.checked = r.value === 'all';
      r.addEventListener('change', () => {
        if (!r.checked) return;
        commentFilter = r.value;
        renderComments();
      });
    });
  }

  /* トップレベルの著者（未指定=human）がフィルタに合うか */
  function commentVisible(c) {
    if (commentFilter === 'all') return true;
    const author = c && c.author === 'ai' ? 'ai' : 'human';
    return author === commentFilter;
  }

  /* 隠れているコメントは本文のハイライト（span.cmt・画像段落）も消す。
     DOMのcmt構造は壊さず、class の付け外しだけで見た目を戻す */
  function applyCommentFilterToBody() {
    const hidden = new Set();
    (session && session.comments ? session.comments : []).forEach((c) => {
      if (!commentVisible(c)) hidden.add(c.id);
    });
    document.querySelectorAll('#editor .cmt[data-cid]').forEach((span) => {
      span.classList.toggle('cmt-filtered', hidden.has(span.dataset.cid));
    });
    document.querySelectorAll('#editor .img-para[data-cids]').forEach((p) => {
      const cids = (p.dataset.cids || '').split(/\s+/).filter(Boolean);
      const anyVisible = cids.some((cid) => !hidden.has(cid));
      p.classList.toggle('cmt-filtered', cids.length > 0 && !anyVisible);
    });
  }

  /* ---------- AIコメントの本文への結び付け ----------
     AIが add_comment で付けたコメント（author==='ai'・text）は、保存された
     本文にはまだ span.cmt が無い。ロード時とポーリングのたびに、本文に
     ラッパーが無いものを quote で探して人間と同じ wrapRangeComment で包み、
     通常の保存フロー（edited_html）に乗せる。以降は人間コメントと同じく
     番号・ハイライト・カード↔本文リンクが付く。見つからないものは
     カードに未アンカー注記を出し、毎回探し直す。 */
  function isAiTextComment(c) {
    return c && c.author === 'ai' &&
      c.target !== 'image' && c.target !== 'region';
  }

  /* 未アンカーのAIコメント（quote が本文に見つからず結び付かないもの）の
     注記。返信・削除は従来どおりできる */
  function unanchoredNote(c) {
    if (isAiTextComment(c) && c.anchored === false && !hasBodyAnchor(c.id)) {
      return '<div class="unanchored-note">本文に該当箇所が見つかりません</div>';
    }
    return '';
  }

  function hasBodyAnchor(cid) {
    return !!document.querySelector('#editor .cmt[data-cid="' + cid + '"]');
  }

  function anchorAiComments() {
    if (!session || !session.comments) return;
    // 下書き・編集・コメント入力中は本文を触らない（保存の取り違えを防ぐ）
    if (draftCid || editing) return;
    let anchored = false;
    session.comments.forEach((c) => {
      if (!isAiTextComment(c) || hasBodyAnchor(c.id)) return;
      if (Tracker.anchorAiComment(c.id, c.quote, c.prefix, c.suffix)) {
        anchored = true;
      }
    });
    if (anchored) {
      onEdited();     // 通常の保存フローで edited_html に残す
      renderComments();
    }
  }

  /* ---------- コメントの種類（指摘・質問） ----------
     指摘（issue）は本文を直してほしいもの、質問（question）は答えてほしい
     だけのもの。AIは get_feedback の tag を見て、修正稿に反映するか、返信で
     答えるだけにするかを分ける。既定は指摘で、tag の無い旧データも指摘 */
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

  /* 種類の選択欄。name はページ内で一意にする（下書きと編集フォームで別） */
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

  /* 選び直したら、AIがそのコメントをどう扱うかの説明を切り替える */
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

  /* 古い稿に付けたコメントは、今の本文では文が書き換わっていることがある。
     どの稿への指摘（質問）かを添えて、飛んだ先が近傍になり得ることを伝える */
  function revNote(c) {
    const cur = session.revisions.length - 1;
    if (typeof c.revision !== 'number' || c.revision >= cur) return '';
    return '<span class="rev-note">第' + (c.revision + 1) + '稿への' +
      TAG_LABELS[tagOf(c)] + '</span>';
  }

  /* コメントの対象表示。画像コメントはサムネイルで何を指しているか示す。
     クリックで本文の該当箇所へ飛ぶ */
  function quoteHtml(c, n) {
    if (c.target === 'image' && c.image) {
      const url = '/api/sessions/' + encodeURIComponent(sid) +
        '/files/' + encodeURIComponent(c.image);
      // 動画・音声はカードを描き直すたびに読み込ませないよう、絵文字で示す
      const kind = mediaKind(c.image) || 'image';
      const thumb = kind === 'image'
        ? '<img src="' + esc(url) + '" alt="">'
        : '<span class="media-icon" aria-hidden="true">' + MEDIA_ICONS[kind] +
          '</span>';
      return '<div class="quote img-quote" title="クリックで本文の位置へ">' +
        thumb + '<span>C' + n + ': ' +
        esc(c.image) + '</span>' + revNote(c) + '</div>';
    }
    // 引用は1行で省略表示されるので、稿の注記は先頭側に置いて隠れないようにする
    return '<div class="quote" title="クリックで本文の位置へ">C' + n + ': ' +
      revNote(c) + esc(c.quote) + '</div>';
  }

  function bodyHtml(cid, rid, o) {
    if (isEditing(cid, rid)) return editFormHtml(cid, rid, o.text, tagOf(o));
    const key = targetKey(cid, rid);
    return '<div class="body">' + esc(o.text) + '</div>' +
      '<div class="card-actions">' +
      '<button data-edit="' + key + '">編集</button>' +
      '<button class="danger" data-del="' + key + '">削除</button></div>';
  }

  /* 返信欄。長い返信も書けるよう textarea にし、書いた分だけ伸ばす（CSS） */
  function replyRowHtml(cid) {
    return '<div class="reply-row"><textarea rows="1" placeholder="返信を入力" ' +
      'title="Enterで送信、Shift+Enterで改行" data-reply="' + esc(cid) +
      '"></textarea><button data-send="' + esc(cid) + '">送信</button></div>';
  }

  /* Enter で送信し、Shift+Enter は改行として入力させる。IMEの変換を確定する
     Enter では送らない（Safari は確定のkeydownで isComposing が false に
     なることがあるので keyCode 229 も見る） */
  function isSendKey(e) {
    return e.key === 'Enter' && !e.shiftKey && !e.isComposing &&
      e.keyCode !== 229;
  }

  function renderComments() {
    const list = $('#comment-list');
    if (!session.comments.length) {
      list.innerHTML = '<div class="empty-note">コメントはまだありません。<br>' +
        '本文を選択して「💬 コメント」を押すと追加できます。</div>';
      applyCommentFilterToBody();
      return;
    }
    // 番号は全コメントの通し番号を保ち、絞り込みで隠すものだけ描かない
    const visible = session.comments
      .map((c, i) => ({ c: c, n: i + 1 }))
      .filter((x) => commentVisible(x.c));
    if (!visible.length) {
      list.innerHTML = '<div class="empty-note">この絞り込みに合う' +
        'コメントはありません。</div>';
      applyCommentFilterToBody();
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
        quoteHtml(c, i + 1) + unanchoredNote(c) +
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
        // 種類の選択（label）を押しても本文へは飛ばない
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
    applyCommentFilterToBody();
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

  function activateComment(cid, scrollEditor) {
    activeCid = cid;
    document.querySelectorAll('#editor .cmt').forEach((s) =>
      s.classList.toggle('active', s.dataset.cid === cid));
    document.querySelectorAll('#editor .img-para').forEach((p) =>
      p.classList.toggle('cmt-active',
        (p.dataset.cids || '').split(/\s+/).indexOf(cid) >= 0));
    document.querySelectorAll('.comment-card').forEach((card) =>
      card.classList.toggle('active', card.dataset.cid === cid));
    clearJumpHighlight();
    if (scrollEditor) {
      jumpToComment(cid);
      return;
    }
    const card = document.querySelector('.comment-card[data-cid="' + cid + '"]');
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  /* ---------- コメントの位置へ飛ぶ ---------- */
  /* 本文側のアンカー（.cmt span、画像段落の data-cids）はその稿の編集HTML
     にしか無い。AIが次の稿を出すと本文は作り直されてアンカーは消えるが、
     コメントはセッションに残る。そこでアンカーが無いときは、引用文を今の
     本文から探して飛び、見つけた範囲を一時的に強調する。 */
  function jumpToComment(cid) {
    const anchored =
      document.querySelector('#editor .cmt[data-cid="' + cid + '"]') ||
      document.querySelector('#editor .img-para[data-cids~="' + cid + '"]');
    if (anchored) {
      anchored.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    const c = session.comments.find((x) => x.id === cid);
    if (!c) return;
    if (c.target === 'image' && c.image) {
      const p = Array.prototype.find.call(
        document.querySelectorAll('#editor .img-para'),
        (el) => el.dataset.img === c.image);
      if (!p) {
        const label = mediaConf.labels[mediaKind(c.image) || 'image'] || '画像';
        toast('この' + label + 'は今の本文にありません');
        return;
      }
      p.classList.add('cmt-active');
      p.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    const range = Tracker.findText(c.quote);
    if (!range) {
      toast('引用文が今の本文に見つかりません（書き換わっています）');
      return;
    }
    const sc = range.startContainer;
    const el = sc.nodeType === Node.ELEMENT_NODE ? sc : sc.parentElement;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    // 本文のDOMには触らず（保存されるHTMLに残さない）ハイライトだけ重ねる
    if (window.CSS && CSS.highlights && window.Highlight) {
      CSS.highlights.set('hle-jump', new Highlight(range));
    }
  }

  function clearJumpHighlight() {
    if (window.CSS && CSS.highlights) CSS.highlights.delete('hle-jump');
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2500);
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
      // 前の稿への未保存分を保存してから読み込み直す。サーバーは稿の番号を
      // 見て、新しい稿を上書きせず前の稿の控えとして受け取る。保存できな
      // かった分もブラウザに退避してあるので、読み込み後に預け直される
      if (dirty) await saveDoc();
      location.reload();
      return;
    }
    // サーバーが応答するようになったら、再試行の待ち時間を待たずに保存する
    if (dirty && retryTimer) saveDoc();
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
    // AIコメントを本文に結び付ける。undo で span が落ちても毎回やり直す
    anchorAiComments();
  }

  async function completeReview() {
    clearTimeout(saveTimer);
    dirty = true;
    // 保存できていない校正があるまま完了にすると、AIは古い内容を受け取る
    if (!(await saveDoc())) {
      alert('校正内容を保存できていないため、校正完了にできません。\n' +
        '接続が戻ると自動で保存されるので、少し待ってからもう一度押してください。');
      return;
    }
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

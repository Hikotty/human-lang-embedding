/* admin.js — 全セッションの校正結果を閲覧する管理画面 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const openDetails = new Set(); // 再描画をまたいで展開状態を保持

  async function api(path) {
    const res = await fetch(path);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
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

  function fmtDate(iso) {
    return (iso || '').replace('T', ' ')
      .replace(/\.\d+/, '').replace(/([+-]\d{2}:\d{2}|Z)$/, '');
  }

  /* 注釈テキスト（【削除:】等のマーカー）をHTML表示に変換する。
     本文由来の記号は \【 のようにエスケープされているので、マーカー展開の前に
     退避し、展開後に生の記号へ戻す（誤展開の防止）。 */
  const _ESCAPED = [
    ['\\\\\\\\', '\u0011', '\\'],
    ['\\\\【', '\u0012', '【'],
    ['\\\\】', '\u0013', '】'],
    ['\\\\〖', '\u0014', '〖'],
    ['\\\\〗', '\u0015', '〗'],
  ];

  function renderAnnotated(text) {
    let html = esc(text);
    _ESCAPED.forEach(([pattern, holder]) => {
      html = html.replace(new RegExp(pattern, 'g'), holder);
    });
    html = html
      .replace(/【削除:([\s\S]*?)】/g, '<del>$1</del>')
      .replace(/【追加:([\s\S]*?)】/g, '<ins>$1</ins>')
      .replace(/【改行追加】/g, '<span class="nl-badge">改行追加</span>')
      .replace(/【改行削除】/g, '<span class="nl-badge">改行削除</span>')
      .replace(/【画像削除:([^】]*)】/g,
        '<span class="img-badge">画像削除: $1</span>')
      .replace(/【画像追加:([^】]*)】/g,
        '<span class="img-badge">画像追加: $1</span>')
      .replace(/〖C(\d+)〗/g,
        '<span class="cmt-label">C$1[</span><span class="cmt-mark">')
      .replace(/〖\/C(\d+)〗/g,
        '</span><span class="cmt-label">]C$1</span>');
    _ESCAPED.forEach(([, holder, literal]) => {
      html = html.split(holder).join(literal);
    });
    return html;
  }

  /* コメントの種類。指摘は修正稿に反映され、質問は返信で答えるだけになる */
  function tagBadge(c) {
    return c.tag === 'question'
      ? '<span class="tag-badge question">質問</span>'
      : '<span class="tag-badge issue">指摘</span>';
  }

  function renderComments(comments) {
    if (!comments || !comments.length) {
      return '<div class="meta">コメントはありません</div>';
    }
    return comments.map((c, i) => {
      const who = c.author === 'ai'
        ? '<span class="who ai">AI</span>'
        : '<span class="who">人間</span>';
      const replies = (c.replies || []).map((r) => {
        const rwho = r.author === 'ai'
          ? '<span class="who ai">AI</span>'
          : '<span class="who">人間</span>';
        return '<div class="reply">' + rwho + esc(r.text) + '</div>';
      }).join('');
      return '<div class="admin-comment">' +
        '<div><b>C' + (c.number || i + 1) + '</b> ' + tagBadge(c) + '「' +
        esc(c.quote) + '」</div><div>' + who + esc(c.text) + '</div>' +
        replies + '</div>';
    }).join('');
  }

  function renderRevisions(detail) {
    return detail.revisions.map((rev, i) => {
      const isLast = i === detail.revisions.length - 1;
      let inner = '';
      if (rev.feedback_summary) {
        inner = '<div class="annotated">' +
          renderAnnotated(rev.feedback_summary.annotated_text) + '</div>';
      } else if (!isLast) {
        inner = '<div class="annotated">' + esc(rev.base_text) +
          '</div><div class="meta">（このリビジョンへの編集はありません）</div>';
      }
      return inner
        ? '<div class="rev-block"><div class="rev-title">第' + (i + 1) +
          '稿への校正</div>' + inner + '</div>'
        : '';
    }).join('');
  }

  async function renderDetail(container, sid) {
    container.innerHTML = '<div class="meta">読み込み中…</div>';
    try {
      const [session, fb] = await Promise.all([
        api('/api/sessions/' + sid),
        api('/api/sessions/' + sid + '/feedback'),
      ]);
      if (fb.mode === 'html') {
        // HTMLレビューは本文編集がないため、コメントだけを表示する
        container.innerHTML =
          '<div class="meta">HTMLレビュー（第' + (fb.revision_index + 1) +
          '稿）' + (fb.source_path ? '　' + esc(fb.source_path) : '') +
          '</div>' +
          '<h4>コメント (' + fb.comments.length + ')</h4>' +
          renderComments(fb.comments);
        return;
      }
      const past = renderRevisions(session);
      container.innerHTML =
        (past ? '<h4>過去リビジョンの校正（差分要約）</h4>' + past : '') +
        '<h4>現在の校正内容（第' + (fb.revision_index + 1) + '稿）</h4>' +
        '<div class="annotated">' + renderAnnotated(fb.annotated_text) +
        '</div>' +
        '<h4>校正反映後のテキスト</h4>' +
        '<div class="annotated">' + esc(fb.result_text) + '</div>' +
        '<h4>コメント (' + fb.comments.length + ')</h4>' +
        renderComments(fb.comments);
    } catch (e) {
      container.innerHTML =
        '<div class="meta">読み込みに失敗しました: ' + esc(e.message) + '</div>';
    }
  }

  function statusBadge(s) {
    return s === 'reviewed'
      ? '<span class="badge reviewed">校正完了</span>'
      : '<span class="badge">校正中</span>';
  }

  function card(s) {
    const el = document.createElement('div');
    el.className = 'admin-card';
    el.innerHTML =
      '<div class="row spread">' +
      '<h3>' + esc(s.title) + '</h3><div>' + statusBadge(s.status) +
      '</div></div>' +
      '<div class="meta">ID: ' + esc(s.id) + '　第' + s.revision_count +
      '稿　コメント' + s.comment_count + '件　' +
      (s.workspace ? esc(s.workspace) + '　' : '') +
      '更新: ' + esc(fmtDate(s.updated_at)) + '</div>' +
      '<div class="row">' +
      '<button data-toggle="' + esc(s.id) + '">校正結果を表示</button>' +
      '<a class="button" href="/session/' + esc(s.id) + '">エディタで開く</a>' +
      '</div>' +
      '<div class="admin-detail" data-detail="' + esc(s.id) + '" hidden></div>';
    return el;
  }

  async function renderList() {
    const q = encodeURIComponent($('#search-query').value || '');
    const w = encodeURIComponent($('#search-workspace').value || '');
    const data = await api(
      '/api/sessions?query=' + q + '&workspace=' + w + '&limit=200');
    const list = $('#admin-list');
    list.textContent = '';
    if (!data.sessions.length) {
      list.innerHTML = '<div class="empty-admin">セッションがありません</div>';
      return;
    }
    data.sessions.forEach((s) => {
      const el = card(s);
      list.appendChild(el);
      const btn = el.querySelector('[data-toggle]');
      const detail = el.querySelector('[data-detail]');
      btn.addEventListener('click', async () => {
        if (detail.hidden) {
          detail.hidden = false;
          btn.textContent = '校正結果を隠す';
          openDetails.add(s.id);
          await renderDetail(detail, s.id);
        } else {
          detail.hidden = true;
          btn.textContent = '校正結果を表示';
          openDetails.delete(s.id);
        }
      });
      if (openDetails.has(s.id)) btn.click();
    });
  }

  let timer = null;
  ['#search-query', '#search-workspace'].forEach((sel) => {
    $(sel).addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(renderList, 300);
    });
  });
  renderList();
})();

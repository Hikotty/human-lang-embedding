/* tracker.js — トラックチェンジエディタエンジン
 *
 * DOM規約:
 *   段落        : <p>
 *   削除        : <del>（赤斜線表示、テキストは保持）
 *   追加        : <ins>（赤文字表示）
 *   改行追加    : <ins class="pb" data-pb="ins">¶</ins>（段落末尾）
 *   改行削除    : <del class="pb" data-pb="del">¶</del>（段落末尾）
 *   コメント範囲: <span class="cmt" data-cid="...">
 *   画像コメント: <p class="img-para" data-cids="cid1 cid2">
 */
(function () {
  'use strict';

  let editor = null;
  let onChangeCb = null;
  let sessionId = '';
  let onImageFileCb = null;
  let onImageCommentsDroppedCb = null;
  let selectedImg = null; // 選択中の画像段落
  const undoStack = [];
  const redoStack = [];
  const MAX_UNDO = 200;
  let composing = false;
  let compIns = null;
  const ZWSP = '\u200B';
  const IMG_LINE = /^!\[([^\]]*)\]\(([^)\s]+)\)$/;

  // ---------- マークダウン装飾 ----------
  // 記法の記号はDOMテキストとして保持したまま、CSSで視覚的に装飾する。
  // 記号を消してリッチ要素へ置き換えると編集HTMLから原文へ戻せなくなる
  // （feedback.py の逆変換は p/del/ins/span とテキストノードだけを見る）。
  // そのため記号は .md-mk で控えめに表示するにとどめ、装飾は class で行う。
  const MD_FENCE = /^\s*(?:```|~~~)/;
  const MD_HEADING = /^(\s*#{1,6}[ \t]+)(.*)$/;
  const MD_QUOTE = /^(\s*>+[ \t]?)(.*)$/;
  const MD_UL = /^([ \t]*)([-*+][ \t]+)(.*)$/;
  const MD_OL = /^([ \t]*)(\d{1,9}[.)][ \t]+)(.*)$/;
  const MD_HR = /^\s*([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  // 表はセル区切りの | を含む行。1行だけでは表と決まらない（次の行が
  // 区切り行かで決まる）ので、判定は scanTables で行単位に前もって行う
  const MD_TABLE = /\|/;
  const MD_TASK = /^(\[[ xX]\])([ \t]+)([\s\S]*)$/;
  const MD_FOOTNOTE = /^(\s*\[\^[^\]\n]+\]:[ \t]*)([\s\S]*)$/;
  // インライン記法。コードを最優先に置き、コード内の記号を解釈しないようにする
  const MD_INLINE_SRC =
    '(`+)([^`\\n]+?)\\1' +            // `code`
    '|(\\*\\*|__|~~)([^\\n]+?)\\3' +  // **bold** __bold__ ~~strike~~
    '|([*_])([^*_\\n]+?)\\5' +        // *italic* _italic_
    // [text](url "title") と ![alt](url "title")
    '|(!?)\\[([^\\]\\n]*)\\]\\(([^)\\s]*)((?:[ \\t]+"[^"\\n]*")?)\\)' +
    '|\\[\\^([^\\]\\n]+)\\]' +        // [^1] 脚注参照
    '|<((?:https?|mailto):[^>\\s]+)>';  // <https://example.com> autolink

  // ---------- ユーティリティ ----------
  function closestEl(node, sel) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el ? el.closest(sel) : null;
  }
  function inIns(node) {
    const e = closestEl(node, 'ins');
    return e && !e.classList.contains('pb') ? e : null;
  }
  function inDel(node) {
    const e = closestEl(node, 'del');
    return e && !e.classList.contains('pb') ? e : null;
  }
  function paraOf(node) { return closestEl(node, 'p'); }
  /* 文字を入れられる段落（画像段落を除く）を返す */
  function textParaOf(node) {
    const p = paraOf(node);
    return p && !p.classList.contains('img-para') ? p : null;
  }

  function getRange() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const r = sel.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return null;
    return r;
  }

  function setCaret(node, offset) {
    const sel = window.getSelection();
    const r = document.createRange();
    r.setStart(node, offset);
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
  }
  function setCaretBefore(node) {
    setCaret(node.parentNode, Array.prototype.indexOf.call(node.parentNode.childNodes, node));
  }
  function setCaretAfter(node) {
    setCaret(node.parentNode, Array.prototype.indexOf.call(node.parentNode.childNodes, node) + 1);
  }

  // ---------- 段落外キャレットの解決 ----------
  // キャレットが #editor 直下（どの段落にも属さない位置）に置かれることがある。
  // 代表例は innerHTML を差し替えた直後（undo/redo）で、Selection が指して
  // いたノードが消えると境界点は #editor の先頭に潰れる。全選択の範囲も
  // #editor 直下から始まる。この状態で文字を入れると段落外に <ins> が生えたり
  // 文書先頭に飛んだりするので、入力の手前で最寄りのテキスト段落へ寄せる。

  /* node/offset の位置を #editor 直下の childNodes インデックスに丸める */
  function topLevelIndex(node, offset) {
    if (node === editor) return offset;
    let top = node;
    while (top.parentNode !== editor) top = top.parentNode;
    return Array.prototype.indexOf.call(editor.childNodes, top) + 1;
  }

  /* #editor 直下の index に最も近いテキスト段落を返す。
     直後の段落（先頭）→ 直前の段落（末尾）→ 後方走査 → 前方走査 の順。 */
  function nearestTextPara(index) {
    const kids = editor.childNodes;
    const ok = (n) => !!n && n.nodeType === 1 && n.tagName === 'P' &&
      !n.classList.contains('img-para');
    if (ok(kids[index])) return { p: kids[index], atEnd: false };
    if (ok(kids[index - 1])) return { p: kids[index - 1], atEnd: true };
    for (let i = index + 1; i < kids.length; i++) {
      if (ok(kids[i])) return { p: kids[i], atEnd: false };
    }
    for (let i = index - 2; i >= 0; i--) {
      if (ok(kids[i])) return { p: kids[i], atEnd: true };
    }
    return null;
  }

  /* 段落の先頭/末尾のテキスト位置にキャレットを置く（改行マーカーは避ける） */
  function caretAtParaEdge(p, atEnd) {
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    let n;
    let target = null;
    while ((n = walker.nextNode())) {
      if (closestEl(n, '.pb')) continue;
      target = n;
      if (!atEnd) break;
    }
    if (target) setCaret(target, atEnd ? target.length : 0);
    else setCaret(p, 0);
  }

  /* キャレットが段落外なら最寄りのテキスト段落へ移す。移した/移さなかった
     いずれでも現在の Range を返す。範囲選択（非 collapsed）は触らない。 */
  function normalizeCaret() {
    const r = getRange();
    if (!r || !r.collapsed) return r;
    if (textParaOf(r.startContainer)) return r;
    const index = topLevelIndex(r.startContainer, r.startOffset);
    let hit = nearestTextPara(index);
    if (!hit) {
      // 画像段落しかない文書。文字を入れる場所を作る
      const p = document.createElement('p');
      p.appendChild(document.createElement('br'));
      editor.insertBefore(p, editor.childNodes[index] || null);
      hit = { p: p, atEnd: false };
    }
    caretAtParaEdge(hit.p, hit.atEnd);
    return getRange();
  }

  /* キャレット位置を「段落番号＋段落内の文字オフセット」で表す。
     undo/redo は innerHTML を丸ごと差し替えるため、ノード参照ではなく
     この形で持ち、差し替え後に同じ位置へ戻す。 */
  function serializeCaret() {
    const r = getRange();
    if (!r) return null;
    let node = r.startContainer;
    let offset = r.startOffset;
    let p = textParaOf(node);
    if (!p) {
      const hit = nearestTextPara(topLevelIndex(node, offset));
      if (!hit) return null;
      p = hit.p;
      node = p;
      offset = hit.atEnd ? p.childNodes.length : 0;
    }
    const pre = document.createRange();
    pre.selectNodeContents(p);
    pre.setEnd(node, offset);
    return {
      para: Array.prototype.indexOf.call(editor.querySelectorAll('p'), p),
      off: pre.toString().length,
    };
  }

  function restoreCaret(pos) {
    if (!pos) return;
    const paras = editor.querySelectorAll('p');
    if (!paras.length) return;
    const p = paras[Math.min(pos.para, paras.length - 1)];
    if (p.classList.contains('img-para')) {
      const hit = nearestTextPara(topLevelIndex(p, 0));
      if (hit) caretAtParaEdge(hit.p, hit.atEnd);
      return;
    }
    let remain = pos.off;
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    let n;
    let last = null;
    while ((n = walker.nextNode())) {
      const mk = closestEl(n, '.pb');
      if (mk) {
        // 改行マーカーの中には置かず、その前後に置く
        if (remain <= 0) { setCaretBefore(mk); return; }
        if (remain <= n.length) { setCaretAfter(mk); return; }
      } else if (remain <= n.length) {
        setCaret(n, remain);
        return;
      }
      remain -= n.length;
      last = n;
    }
    if (!last) setCaret(p, 0);
    else if (closestEl(last, '.pb')) setCaretAfter(closestEl(last, '.pb'));
    else setCaret(last, last.length);
  }

  function isMarker(el) { return el.nodeType === 1 && el.classList.contains('pb'); }

  function cleanupEmpty(el) {
    // 空になった ins/del/span を除去する（ZWSPのみも空とみなす）
    while (el && el !== editor && el.nodeType === 1 &&
           /^(INS|DEL|SPAN)$/.test(el.tagName) && !isMarker(el) &&
           el.textContent.replace(/\u200B/g, '') === '') {
      const parent = el.parentNode;
      el.remove();
      el = parent;
    }
  }

  function ensureParagraphPlaceholder(p) {
    if (p && !p.hasChildNodes()) p.appendChild(document.createElement('br'));
  }

  // ---------- スナップショット (undo/redo) ----------
  // スナップショットは本文 HTML とその時点のキャレット位置の組。
  // innerHTML を差し替えると Selection は #editor の先頭に潰れるので、
  // キャレットも一緒に戻さないと次の入力が文書先頭に入ってしまう。
  function captureState() {
    return { html: editor.innerHTML, caret: serializeCaret() };
  }
  function applyState(state) {
    editor.innerHTML = state.html;
    editor.focus({ preventScroll: true });
    restoreCaret(state.caret);
    changed();
  }
  function snapshot() {
    undoStack.push(captureState());
    if (undoStack.length > MAX_UNDO) undoStack.shift();
    redoStack.length = 0;
  }
  function undo() {
    if (!undoStack.length) return;
    redoStack.push(captureState());
    applyState(undoStack.pop());
  }
  function redo() {
    if (!redoStack.length) return;
    undoStack.push(captureState());
    applyState(redoStack.pop());
  }

  function changed() {
    if (onChangeCb) onChangeCb();
  }

  // ---------- 範囲操作 ----------
  function splitBoundaries(range) {
    let { startContainer: sc, startOffset: so, endContainer: ec, endOffset: eo } = range;
    if (ec.nodeType === 3 && eo > 0 && eo < ec.length) ec.splitText(eo);
    if (sc.nodeType === 3 && so > 0 && so < sc.length) {
      const rest = sc.splitText(so);
      range.setStart(rest, 0);
      if (ec === sc) range.setEnd(rest, Math.max(0, eo - so));
    }
  }

  function collectTextNodes(range) {
    const nodes = [];
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!n.length) continue;
      if (!range.intersectsNode(n)) continue;
      if (range.comparePoint(n, 0) === 0 &&
          range.comparePoint(n, n.length) === 0) {
        nodes.push(n);
      }
    }
    return nodes;
  }

  function paragraphsInRange(range) {
    return Array.prototype.filter.call(
      editor.querySelectorAll('p'), (p) => range.intersectsNode(p));
  }

  function mergeParagraphs(prev, next) {
    // next の子を prev に移動して next を削除する
    const br = prev.querySelector(':scope > br');
    if (br) br.remove();
    while (next.firstChild) {
      if (next.firstChild.nodeName === 'BR' && next.childNodes.length === 1) {
        next.firstChild.remove();
        break;
      }
      prev.appendChild(next.firstChild);
    }
    next.remove();
  }

  function trailingMarker(p, kind) {
    // 段落末尾のマーカー（kind: 'ins'|'del'）を返す
    for (let n = p.lastChild; n; n = n.previousSibling) {
      if (n.nodeType === 3 && n.data.replace(/\u200B/g, '') === '') continue;
      if (n.nodeType === 1 && n.classList.contains('pb')) {
        if (n.tagName.toLowerCase() === kind) return n;
        continue;
      }
      break;
    }
    return null;
  }

  function trackedDeleteRange(range) {
    splitBoundaries(range);
    const paras = paragraphsInRange(range);
    const startNode = range.startContainer;
    const startOffset = range.startOffset;
    collectTextNodes(range).forEach((node) => {
      if (closestEl(node, '.pb')) return; // マーカーは削除対象外
      const ins = inIns(node);
      if (ins) {
        node.remove();
        cleanupEmpty(ins);
      } else if (inDel(node)) {
        // 既に削除済み: 何もしない
      } else {
        const del = document.createElement('del');
        node.parentNode.replaceChild(del, node);
        del.appendChild(node);
      }
    });
    // 複数段落にまたがる場合は段落境界（改行）の削除を記録する
    for (let i = paras.length - 2; i >= 0; i--) {
      const p = paras[i];
      if (!p.isConnected || !paras[i + 1].isConnected) continue;
      const insPb = trailingMarker(p, 'ins');
      if (insPb) {
        insPb.remove();
        mergeParagraphs(p, paras[i + 1]);
      } else if (!trailingMarker(p, 'del')) {
        p.appendChild(makeMarker('del'));
      }
    }
    if (startNode.isConnected) setCaret(startNode, Math.min(startOffset,
      startNode.nodeType === 3 ? startNode.length : startNode.childNodes.length));
  }

  function makeMarker(kind) {
    const m = document.createElement(kind); // 'ins' | 'del'
    m.className = 'pb';
    m.dataset.pb = kind;
    m.textContent = '\u00B6';
    m.contentEditable = 'false';
    return m;
  }

  // ---------- テキスト挿入 ----------
  function insertTextAtCaret(text) {
    // 段落外にキャレットがあれば最寄りの段落へ寄せる（文書先頭には飛ばさない）
    let r = normalizeCaret();
    if (!r) return;
    const del = inDel(r.startContainer);
    if (del) { setCaretAfter(del); r = getRange(); }
    const ins = inIns(r.startContainer);
    if (ins && r.startContainer.nodeType === 3) {
      r.startContainer.insertData(r.startOffset, text);
      setCaret(r.startContainer, r.startOffset + text.length);
    } else if (ins) {
      const t = document.createTextNode(text);
      r.insertNode(t);
      setCaret(t, t.length);
    } else {
      const el = document.createElement('ins');
      el.textContent = text;
      r.insertNode(el);
      const br = el.parentNode.nodeName === 'P' ? el.parentNode.querySelector(':scope > br') : null;
      if (br) br.remove();
      setCaret(el.firstChild, el.firstChild.length);
    }
    changed();
  }

  function trackedInsertText(text) {
    const r = getRange();
    if (!r) return;
    snapshot();
    if (!r.collapsed) trackedDeleteRange(r);
    const lines = String(text).split('\n');
    lines.forEach((line, i) => {
      if (i > 0) splitParagraphAtCaret();
      if (line) insertTextAtCaret(line);
    });
    changed();
  }

  // ---------- 段落分割（改行追加） ----------
  function splitParagraphAtCaret() {
    const r = normalizeCaret();
    if (!r) return;
    const p = textParaOf(r.startContainer);
    if (!p) return;
    const tail = document.createRange();
    tail.selectNodeContents(p);
    tail.setStart(r.startContainer, r.startOffset);
    const frag = tail.extractContents();
    const newP = document.createElement('p');
    newP.appendChild(frag);
    // 分割で生じた空要素を掃除
    newP.querySelectorAll('ins,del,span').forEach((el) => {
      if (!isMarker(el) && el.textContent.replace(/\u200B/g, '') === '') el.remove();
    });
    p.querySelectorAll('ins,del,span').forEach((el) => {
      if (!isMarker(el) && el.textContent.replace(/\u200B/g, '') === '') el.remove();
    });
    p.appendChild(makeMarker('ins'));
    p.after(newP);
    ensureParagraphPlaceholder(newP);
    ensureParagraphPlaceholder(p);
    setCaret(newP, 0);
    changed();
  }

  // ---------- 1文字削除 ----------
  function textPositions() {
    // エディタ内の全テキストノード（マーカー内・ZWSPのみを除く）
    const list = [];
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!n.length) continue;
      if (closestEl(n, '.pb')) continue;
      if (n.data.replace(/\u200B/g, '') === '') continue;
      list.push(n);
    }
    return list;
  }

  function cmpPoint(node, offset, other, otherOffset) {
    const a = document.createRange();
    a.setStart(node, offset); a.collapse(true);
    const b = document.createRange();
    b.setStart(other, otherOffset); b.collapse(true);
    return a.compareBoundaryPoints(Range.START_TO_START, b);
  }

  function findAdjacentText(r, dir) {
    // caret の前(dir=-1)/後(dir=+1)の削除対象文字位置を探す
    const nodes = textPositions();
    if (dir < 0) {
      let best = null;
      for (const n of nodes) {
        if (cmpPoint(n, n.length, r.startContainer, r.startOffset) <= 0) best = n;
        else if (n === r.startContainer && r.startOffset > 0) best = n;
      }
      if (r.startContainer.nodeType === 3 && r.startOffset > 0) {
        return { node: r.startContainer, end: r.startOffset };
      }
      return best ? { node: best, end: best.length } : null;
    }
    if (r.startContainer.nodeType === 3 && r.startOffset < r.startContainer.length) {
      return { node: r.startContainer, end: r.startOffset + 1, fwd: true };
    }
    for (const n of nodes) {
      if (cmpPoint(n, 0, r.startContainer, r.startOffset) >= 0) {
        return { node: n, end: 1, fwd: true };
      }
    }
    return null;
  }

  function charSpan(node, end) {
    // 終端位置 end の直前1文字（サロゲートペア対応）の [start, end]
    const s = node.data;
    let start = end - 1;
    if (start > 0 && /[\uDC00-\uDFFF]/.test(s[end - 1]) && /[\uD800-\uDBFF]/.test(s[end - 2])) {
      start = end - 2;
    }
    return [start, end];
  }

  function deleteParagraphBoundary(p, dir) {
    // dir<0: p の先頭で Backspace → 前の段落との境界を削除
    const target = dir < 0 ? p.previousElementSibling : p;
    if (!target || target.tagName !== 'P') return;
    const insPb = trailingMarker(target, 'ins');
    if (insPb) {
      insPb.remove();
      mergeParagraphs(target, target.nextElementSibling);
      changed();
      return;
    }
    if (!trailingMarker(target, 'del')) {
      target.appendChild(makeMarker('del'));
    }
    const marker = trailingMarker(target, 'del');
    setCaretBefore(marker);
    changed();
  }

  function trackedDeleteChar(dir) {
    const r = normalizeCaret();
    if (!r) return;
    snapshot();
    if (!r.collapsed) { trackedDeleteRange(r); changed(); return; }
    const pos = findAdjacentText(r, dir);
    const curP = paraOf(r.startContainer);
    if (!pos || paraOf(pos.node) !== curP) {
      // 段落境界: 改行の削除として扱う
      const p = dir < 0 ? curP : (curP && curP.nextElementSibling ? curP.nextElementSibling : null);
      if (dir < 0 && curP && curP.previousElementSibling) {
        deleteParagraphBoundary(curP, -1);
      } else if (dir > 0 && curP && curP.nextElementSibling) {
        deleteParagraphBoundary(curP.nextElementSibling, -1);
      } else {
        undoStack.pop(); // 変化なし
      }
      return;
    }
    const [start, end] = charSpan(pos.node, pos.end);
    const node = pos.node;
    const ins = inIns(node);
    if (ins) {
      node.deleteData(start, end - start);
      if (!node.length) { const par = node.parentNode; node.remove(); cleanupEmpty(par); }
      else setCaret(node, start);
      if (!ins.isConnected) { /* cleanupEmptyで消えた */ }
      changed();
      return;
    }
    if (inDel(node)) {
      // 既に削除済みの文字はスキップしてカーソル移動のみ
      setCaret(node, dir < 0 ? start : end);
      undoStack.pop();
      return;
    }
    // 対象文字を <del> で包む
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, end);
    splitBoundaries(range);
    const target = range.startContainer;
    const del = document.createElement('del');
    target.parentNode.replaceChild(del, target);
    del.appendChild(target);
    if (dir < 0) setCaretBefore(del); else setCaretAfter(del);
    changed();
  }

  // ---------- コメント ----------
  function wrapRangeComment(range, cid) {
    snapshot();
    splitBoundaries(range);
    const nodes = collectTextNodes(range);
    if (!nodes.length) { undoStack.pop(); return false; }
    nodes.forEach((node) => {
      if (closestEl(node, '.pb')) return;
      const span = document.createElement('span');
      span.className = 'cmt';
      span.dataset.cid = cid;
      node.parentNode.replaceChild(span, node);
      span.appendChild(node);
    });
    changed();
    return true;
  }

  /* 選択中の画像段落にコメントIDを結び付ける。
     画像は contenteditable=false のひとかたまりで、逆変換も画像段落の中身を
     読まない（feedback.py）ため <span class="cmt"> では包めない。
     そのため段落自身の data-cids にスペース区切りで持たせる。 */
  function addImageComment(cid) {
    if (!selectedImg || !selectedImg.isConnected) return false;
    snapshot();
    const cids = imageCids(selectedImg);
    if (!cids.includes(cid)) cids.push(cid);
    selectedImg.dataset.cids = cids.join(' ');
    changed();
    return true;
  }

  function unwrapComment(cid) {
    editor.querySelectorAll('span.cmt[data-cid="' + cid + '"]').forEach((span) => {
      while (span.firstChild) span.parentNode.insertBefore(span.firstChild, span);
      span.remove();
    });
    editor.querySelectorAll('.img-para[data-cids~="' + cid + '"]').forEach((p) => {
      const rest = imageCids(p).filter((x) => x !== cid);
      if (rest.length) p.dataset.cids = rest.join(' ');
      else delete p.dataset.cids;
    });
    changed();
  }

  // ---------- IME(日本語入力)対応 ----------
  function onCompositionStart() {
    composing = true;
    selectImage(null);
    snapshot();
    let r = normalizeCaret();
    if (!r) return;
    if (!r.collapsed) { trackedDeleteRange(r); r = normalizeCaret(); }
    if (!r) return;
    const del = inDel(r.startContainer);
    if (del) setCaretAfter(del);
    const r2 = getRange();
    if (!inIns(r2.startContainer)) {
      // 変換テキストが地の文に混ざらないよう ZWSP入りの ins を用意する
      const el = document.createElement('ins');
      el.textContent = ZWSP;
      r2.insertNode(el);
      setCaret(el.firstChild, 1);
      compIns = el;
    } else {
      compIns = inIns(r2.startContainer);
    }
  }

  function onCompositionEnd() {
    composing = false;
    if (compIns && compIns.isConnected) {
      // ZWSP を除去し、空なら要素ごと削除
      const walker = document.createTreeWalker(compIns, NodeFilter.SHOW_TEXT);
      let n;
      const targets = [];
      while ((n = walker.nextNode())) if (n.data.includes(ZWSP)) targets.push(n);
      targets.forEach((t) => {
        const idx = t.data.indexOf(ZWSP);
        const caretHere = window.getSelection().rangeCount &&
          window.getSelection().getRangeAt(0).startContainer === t;
        const oldOffset = caretHere ? window.getSelection().getRangeAt(0).startOffset : -1;
        t.data = t.data.replace(/\u200B/g, '');
        if (caretHere && oldOffset > idx) setCaret(t, Math.min(t.length, oldOffset - 1));
      });
      if (compIns.textContent === '') compIns.remove();
    }
    compIns = null;
    changed();
  }

  // ---------- beforeinput ハンドラ ----------
  function onBeforeInput(e) {
    if (composing || e.inputType === 'insertCompositionText') return;
    // 文字の入力・削除に移ったら画像の選択は外す。残したままだと、
    // 打った文字を直そうとした Backspace が画像の削除トグルになる
    selectImage(null);
    switch (e.inputType) {
      case 'insertText':
      case 'insertReplacementText':
        e.preventDefault();
        trackedInsertText(e.data || '');
        break;
      case 'insertParagraph':
      case 'insertLineBreak': {
        e.preventDefault();
        const r = getRange();
        if (!r) break;
        snapshot();
        if (!r.collapsed) trackedDeleteRange(r);
        splitParagraphAtCaret();
        break;
      }
      case 'deleteContentBackward':
      case 'deleteWordBackward':
      case 'deleteSoftLineBackward':
        e.preventDefault();
        trackedDeleteChar(-1);
        break;
      case 'deleteContentForward':
      case 'deleteWordForward':
      case 'deleteSoftLineForward':
        e.preventDefault();
        trackedDeleteChar(1);
        break;
      case 'deleteByCut':
      case 'deleteContent': {
        e.preventDefault();
        const r = getRange();
        if (r && !r.collapsed) { snapshot(); trackedDeleteRange(r); changed(); }
        break;
      }
      case 'insertFromPaste': {
        e.preventDefault();
        const dt = e.dataTransfer || (e.clipboardData || null);
        if (dt && dt.files && dt.files.length &&
            dt.files[0].type.indexOf('image/') === 0) {
          if (onImageFileCb) onImageFileCb(dt.files[0]);
          break;
        }
        const text = dt ? dt.getData('text/plain') : '';
        if (text) trackedInsertText(text);
        break;
      }
      case 'historyUndo':
        e.preventDefault();
        undo();
        break;
      case 'historyRedo':
        e.preventDefault();
        redo();
        break;
      default:
        // 書式変更・ドロップ等は全てブロック
        e.preventDefault();
        break;
    }
  }

  function onKeyDown(e) {
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
    } else if (meta && e.key.toLowerCase() === 'y') {
      e.preventDefault();
      redo();
    } else if ((e.key === 'Backspace' || e.key === 'Delete') &&
               selectedImg && selectedImg.isConnected) {
      e.preventDefault();
      toggleImageDeleted(selectedImg);
    }
  }

  function onEditorClick(e) {
    const p = e.target.closest && e.target.closest('.img-para');
    if (p && editor.contains(p)) {
      selectImage(p);
      // 画像は contenteditable=false なのでクリックしてもキャレットは動かない。
      // 未フォーカスの状態で focus() すると文書先頭にキャレットが置かれ、
      // 続けて入力した文字が先頭に飛ぶ。そこで画像の隣（直後の段落の先頭、
      // なければ直前の段落の末尾）に明示的に置く。直前の文字列選択も
      // これで畳まれ、コメント対象が曖昧になることもない
      editor.focus({ preventScroll: true });
      const hit = nearestTextPara(topLevelIndex(p, 0));
      if (hit) {
        caretAtParaEdge(hit.p, hit.atEnd);
      } else {
        const sel = window.getSelection();
        if (sel && sel.rangeCount && !sel.isCollapsed) sel.collapseToEnd();
      }
    } else {
      selectImage(null);
    }
  }

  // ---------- 画像段落 ----------
  function makeImageParagraph(name, md, flags) {
    const p = document.createElement('p');
    p.className = 'img-para';
    p.contentEditable = 'false';
    p.dataset.img = name;
    p.dataset.md = md;
    if (flags && flags.ins) p.dataset.ins = '1';
    if (flags && flags.del) p.dataset.del = '1';
    const img = document.createElement('img');
    img.src = '/api/sessions/' + encodeURIComponent(sessionId) +
      '/files/' + encodeURIComponent(name);
    img.alt = name;
    const caption = document.createElement('span');
    caption.className = 'img-caption';
    caption.textContent = name;
    p.appendChild(img);
    p.appendChild(caption);
    return p;
  }

  function selectImage(p) {
    if (selectedImg && selectedImg !== p) selectedImg.classList.remove('sel');
    selectedImg = p;
    if (p) p.classList.add('sel');
  }

  function imageCids(p) {
    return String((p && p.dataset.cids) || '').split(/\s+/).filter(Boolean);
  }

  /* 選択中の画像段落の情報を返す（コメント対象の指定に使う） */
  function getSelectedImage() {
    if (!selectedImg || !selectedImg.isConnected) return null;
    const name = selectedImg.dataset.img || '';
    return {
      name: name,
      md: selectedImg.dataset.md || ('![](' + name + ')'),
      cids: imageCids(selectedImg),
    };
  }

  function toggleImageDeleted(p) {
    snapshot();
    if (p.dataset.ins) {
      // 自分（人間）が追加した画像は痕跡なく削除。付いていたコメントは
      // 参照先を失うので、呼び出し側に消してもらう
      const orphans = imageCids(p);
      selectImage(null);
      p.remove();
      if (orphans.length && onImageCommentsDroppedCb) {
        onImageCommentsDroppedCb(orphans);
      }
    } else if (p.dataset.del) {
      delete p.dataset.del;
    } else {
      p.dataset.del = '1';
    }
    changed();
  }

  function insertImageParagraph(name) {
    snapshot();
    const p = makeImageParagraph(name, '![](' + name + ')', { ins: true });
    const r = getRange();
    const cur = r ? paraOf(r.startContainer) : null;
    if (cur) cur.after(p);
    else editor.appendChild(p);
    changed();
    return p;
  }

  // ---------- マークダウンの組み立て ----------
  function mdMark(text) {
    const s = document.createElement('span');
    s.className = 'md-mk';
    s.textContent = text;
    return s;
  }

  function mdIndentClass(spaces) {
    const width = String(spaces || '').replace(/\t/g, '  ').length;
    const n = Math.min(3, Math.floor(width / 2));
    return n > 0 ? ' md-in' + n : '';
  }

  /* テキストにインライン記法の装飾を当てて parent に追加する。
     記号は .md-mk のテキストノードとして残すので原文は完全に復元できる。 */
  function appendInline(parent, text) {
    if (!text) return;
    const re = new RegExp(MD_INLINE_SRC, 'g');
    let last = 0;
    let m;
    while ((m = re.exec(text))) {
      let cls, open, close, body;
      let nested = true;
      let emptyOk = false;
      if (m[1]) {
        cls = 'md-code-in'; open = m[1]; close = m[1]; body = m[2]; nested = false;
      } else if (m[3]) {
        cls = m[3] === '~~' ? 'md-s' : 'md-b';
        open = m[3]; close = m[3]; body = m[4];
      } else if (m[5]) {
        cls = 'md-i'; open = m[5]; close = m[5]; body = m[6];
      } else if (m[8] !== undefined) {
        // ![alt](url) は文中の画像。行全体が画像の行は画像段落になる
        cls = m[7] ? 'md-img' : 'md-link';
        open = m[7] + '['; close = '](' + m[9] + m[10] + ')'; body = m[8];
        emptyOk = true;  // ![](x.png) のように内側が空でも記法として扱う
        nested = !m[7];
      } else if (m[11]) {
        cls = 'md-fn'; open = '[^'; close = ']'; body = m[11]; nested = false;
      } else {
        cls = 'md-link'; open = '<'; close = '>'; body = m[12]; nested = false;
      }
      // 内側が空・前後が空白のものは記法として扱わない（素のテキストとして残す）
      if ((!body && !emptyOk) || (body && /^\s|\s$/.test(body))) continue;
      if (m.index > last) {
        parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      }
      const wrap = document.createElement('span');
      wrap.className = cls;
      wrap.appendChild(mdMark(open));
      if (nested) appendInline(wrap, body);
      else wrap.appendChild(document.createTextNode(body));
      wrap.appendChild(mdMark(close));
      parent.appendChild(wrap);
      last = m.index + m[0].length;
    }
    if (last < text.length) {
      parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  /* 表の1行をセル区切りの | で分割する。前後の空白と \| は素のテキストとして
     残し、返ったトークンを順に連結すると元の行に戻る（原文復元のため）。 */
  function splitRow(raw) {
    const m = /^([ \t]*)([\s\S]*?)([ \t]*)$/.exec(String(raw));
    const parts = [];
    const cells = [];
    if (m[1]) parts.push({ type: 'pre', text: m[1] });
    let buf = '';
    const core = m[2];
    for (let i = 0; i < core.length; i++) {
      if (core[i] === '\\' && core[i + 1] === '|') { buf += '\\|'; i++; continue; }
      if (core[i] === '|') {
        if (buf) { parts.push({ type: 'td', text: buf }); cells.push(buf); buf = ''; }
        parts.push({ type: 'mk', text: '|' });
        continue;
      }
      buf += core[i];
    }
    if (buf) { parts.push({ type: 'td', text: buf }); cells.push(buf); }
    if (m[3]) parts.push({ type: 'pre', text: m[3] });
    return { parts: parts, cells: cells };
  }

  /* 区切り行（|---|:--:|）なら各列の寄せを返す。表でなければ null。 */
  function tableAlign(raw) {
    if (!MD_TABLE.test(raw) || !/-/.test(raw)) return null;
    const cells = splitRow(raw).cells;
    if (!cells.length) return null;
    const align = [];
    for (const cell of cells) {
      const t = cell.trim();
      if (!/^:?-+:?$/.test(t)) return null;
      const left = t.startsWith(':');
      const right = t.endsWith(':');
      align.push(left && right ? 'c' : right ? 'r' : left ? 'l' : '');
    }
    return align;
  }

  /* 表ブロックを行番号ごとに割り出す。表は「| を含む行 + 区切り行」で確定し、
     以降 | を含む行が続く限り本体とみなす（GFMと同じ判定）。 */
  function scanTables(lines) {
    const info = {};
    let fence = false;
    for (let i = 0; i < lines.length; i++) {
      if (MD_FENCE.test(lines[i])) { fence = !fence; continue; }
      if (fence || i === 0) continue;
      const align = tableAlign(lines[i]);
      if (!align || info[i] || !MD_TABLE.test(lines[i - 1]) || info[i - 1]) {
        continue;
      }
      info[i - 1] = { role: 'head', align: align, first: true };
      info[i] = { role: 'sep', align: align };
      for (let j = i + 1; j < lines.length; j++) {
        if (MD_FENCE.test(lines[j]) || !MD_TABLE.test(lines[j])) break;
        info[j] = { role: 'body', align: align };
      }
    }
    return info;
  }

  /* 表の行を組み立てる。セルは span なので逆変換（p/del/ins/span しか見ない）
     を壊さず、| は .md-mk のテキストとして残る。 */
  function appendRow(p, row, align, role) {
    let col = 0;
    row.parts.forEach((part) => {
      if (part.type === 'mk') { p.appendChild(mdMark('|')); return; }
      if (part.type === 'pre') {
        p.appendChild(document.createTextNode(part.text));
        return;
      }
      const td = document.createElement('span');
      const a = align && align[col];
      td.className = 'md-td' + (a ? ' md-al-' + a : '');
      // 区切り行の --- は装飾の対象にならない
      if (role === 'sep') td.appendChild(document.createTextNode(part.text));
      else appendInline(td, part.text);
      p.appendChild(td);
      col++;
    });
  }

  /* 1行をブロック記法として解析する。state.fence でコードブロック内を追跡する。
     prefix は記号部分（.md-mk にする）、body は装飾対象の本文。
     plain=true の行はインライン記法を解釈しない。
     tinfo は scanTables が求めた表の役割（あれば表の行として組み立てる）。 */
  function mdBlock(line, state, tinfo) {
    const raw = String(line);
    if (MD_FENCE.test(raw)) {
      state.fence = !state.fence;
      return { cls: 'md-fence', prefix: raw, body: '' };
    }
    if (state.fence) return { cls: 'md-code', prefix: '', body: raw, plain: true };
    if (tinfo) {
      const role = tinfo.role;
      return {
        cls: 'md-tr' +
          (role === 'head' ? ' md-tr-head' : role === 'sep' ? ' md-tr-sep' : '') +
          (tinfo.first ? ' md-tr-first' : ''),
        row: splitRow(raw),
        align: tinfo.align,
        role: role,
      };
    }
    if (MD_HR.test(raw)) return { cls: 'md-hr', prefix: raw, body: '' };
    let m = raw.match(MD_FOOTNOTE);
    if (m) return { cls: 'md-footnote', prefix: m[1], body: m[2] };
    m = raw.match(MD_HEADING);
    if (m) {
      const level = m[1].replace(/[^#]/g, '').length;
      return { cls: 'md-h' + level, prefix: m[1], body: m[2] };
    }
    m = raw.match(MD_QUOTE);
    if (m) return { cls: 'md-quote', prefix: m[1], body: m[2] };
    m = raw.match(MD_UL);
    if (m) {
      const task = m[3].match(MD_TASK);
      if (task) {
        // - [x] のチェックボックス。記号は prefix としてテキストに残す
        const done = task[1][1] !== ' ';
        return {
          cls: 'md-li md-task' + (done ? ' md-task-done' : '') +
            mdIndentClass(m[1]),
          prefix: m[1] + m[2] + task[1] + task[2],
          body: task[3],
        };
      }
      return { cls: 'md-li' + mdIndentClass(m[1]), prefix: m[1] + m[2], body: m[3] };
    }
    m = raw.match(MD_OL);
    if (m) {
      return {
        cls: 'md-li md-ol' + mdIndentClass(m[1]),
        prefix: m[1] + m[2],
        body: m[3]
      };
    }
    return { cls: '', prefix: '', body: raw };
  }

  // ---------- 初期化 ----------
  function renderBase(text) {
    editor.textContent = '';
    const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
    const tables = scanTables(lines);
    const state = { fence: false };
    lines.forEach((line, i) => {
      const m = line.match(IMG_LINE);
      if (m && !state.fence) {
        editor.appendChild(makeImageParagraph(m[2], line, {}));
        return;
      }
      const info = mdBlock(line, state, tables[i]);
      const p = document.createElement('p');
      if (info.cls) p.className = info.cls;
      if (info.prefix) p.appendChild(mdMark(info.prefix));
      if (info.row) {
        appendRow(p, info.row, info.align, info.role);
      } else if (info.plain) {
        if (info.body) p.appendChild(document.createTextNode(info.body));
      } else {
        appendInline(p, info.body);
      }
      if (!p.hasChildNodes()) p.appendChild(document.createElement('br'));
      editor.appendChild(p);
    });
    if (!editor.hasChildNodes()) {
      const p = document.createElement('p');
      p.appendChild(document.createElement('br'));
      editor.appendChild(p);
    }
  }

  function init(el, baseText, savedHtml, onChange, opts) {
    editor = el;
    onChangeCb = onChange;
    sessionId = (opts && opts.sessionId) || '';
    onImageFileCb = (opts && opts.onImageFile) || null;
    onImageCommentsDroppedCb = (opts && opts.onImageCommentsDropped) || null;
    if (savedHtml) {
      editor.innerHTML = savedHtml;
    } else {
      renderBase(baseText);
    }
    editor.addEventListener('beforeinput', onBeforeInput);
    editor.addEventListener('compositionstart', onCompositionStart);
    editor.addEventListener('compositionend', onCompositionEnd);
    editor.addEventListener('keydown', onKeyDown);
    editor.addEventListener('click', onEditorClick);
  }

  window.Tracker = {
    init: init,
    undo: undo,
    redo: redo,
    wrapRangeComment: wrapRangeComment,
    addImageComment: addImageComment,
    unwrapComment: unwrapComment,
    insertImageParagraph: insertImageParagraph,
    getSelectedImage: getSelectedImage,
    getRange: getRange,
    html: function () {
      if (!editor) return '';
      // 選択・強調といった一時的な表示状態は保存しない
      const clone = editor.cloneNode(true);
      clone.querySelectorAll('.sel, .active, .cmt-active').forEach(function (el) {
        el.classList.remove('sel', 'active', 'cmt-active');
      });
      return clone.innerHTML;
    }
  };
})();

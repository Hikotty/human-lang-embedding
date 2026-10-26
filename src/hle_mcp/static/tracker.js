/* tracker.js — トラックチェンジエディタエンジン
 *
 * DOM規約:
 *   段落        : <p>
 *   削除        : <del>（赤斜線表示、テキストは保持）
 *   追加        : <ins>（赤文字表示）
 *   改行追加    : <ins class="pb" data-pb="ins">¶</ins>（段落末尾）
 *   改行削除    : <del class="pb" data-pb="del">¶</del>（段落末尾）
 *   コメント範囲: <span class="cmt" data-cid="...">
 *   メディア段落: <p class="img-para" data-img="名前" data-kind="image|video|audio">
 *                 （中身は <img> / <video controls> / <audio controls>）
 *   メディアへのコメント: <p class="img-para" data-cids="cid1 cid2">
 *   Mermaid の図: <div class="mmd-widget">（本文ではない。閉じフェンスの
 *                 段落の直後に置く読み取り専用の表示で、保存しない）
 */
(function () {
  'use strict';

  let editor = null;
  let onChangeCb = null;
  let sessionId = '';
  let onImageFileCb = null;
  let onImageCommentsDroppedCb = null;
  // ファイル名 → 'image' | 'video' | 'audio'。形式の定義はサーバー（/api/media）
  // にあり、app.js が受け取って渡す
  let mediaKindFn = () => null;
  let selectedImg = null; // 選択中のメディア段落（画像・動画・音声）
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
      if (closestEl(n, '.pb') || closestEl(n, '.ai-del')) continue;
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
    // AI差分の幽霊テキストはスナップショットのHTMLに含まれないので、
    // オフセットからも除いておく（復元はきれいなDOMの上で行われる）
    const box = document.createElement('div');
    box.appendChild(pre.cloneContents());
    box.querySelectorAll('.ai-del').forEach((el) => el.remove());
    return {
      para: Array.prototype.indexOf.call(editor.querySelectorAll('p'), p),
      off: box.textContent.length,
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
  /* 保存・スナップショット用に、一時的な表示状態を落とした複製を作る。
     AI差分の幽霊（span.ai-del）と選択・強調のclassは本文ではない */
  function cleanedClone() {
    const clone = editor.cloneNode(true);
    clone.querySelectorAll('.ai-del').forEach((el) => el.remove());
    // Mermaid の図は本文ではない。<div> は feedback.py の逆変換で段落の区切りに
    // なり空の行が増えるので、保存・スナップショットからは必ず落とす
    clone.querySelectorAll('.mmd-widget').forEach((el) => el.remove());
    clone.querySelectorAll('.sel, .active, .cmt-active, .ai-ins-obj')
      .forEach((el) => {
        el.classList.remove('sel', 'active', 'cmt-active', 'ai-ins-obj');
      });
    return clone;
  }

  // スナップショットは本文 HTML とその時点のキャレット位置の組。
  // innerHTML を差し替えると Selection は #editor の先頭に潰れるので、
  // キャレットも一緒に戻さないと次の入力が文書先頭に入ってしまう。
  function captureState() {
    return { html: cleanedClone().innerHTML, caret: serializeCaret() };
  }
  function applyState(state) {
    editor.innerHTML = state.html;
    editor.focus({ preventScroll: true });
    restoreCaret(state.caret);
    changed();
    // 図はスナップショットに入らず、差し替えで消えている。待たずに作り直す
    // （結果はキャッシュにあるので、空の枠を見せずに済む）
    syncMermaid();
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
    // AI差分表示中は、編集でずれた幽霊・青字の割り付けをやり直す
    if (aiDiffOps) applyAiOverlay();
    scheduleMermaid();
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
      if (closestEl(n, '.ai-del')) continue; // AI差分の幽霊は本文ではない
      if (closestEl(n, '.mmd-widget')) continue; // Mermaid の図は本文ではない
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
      if (n.nodeType === 1 && n.classList.contains('ai-del')) continue;
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
      if (closestEl(n, '.pb') || closestEl(n, '.ai-del')) continue;
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
    const target = dir < 0 ? prevPara(p) : p;
    if (!target || target.tagName !== 'P') return;
    const insPb = trailingMarker(target, 'ins');
    if (insPb) {
      insPb.remove();
      mergeParagraphs(target, nextPara(target));
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
      if (dir < 0 && curP && prevPara(curP)) {
        deleteParagraphBoundary(curP, -1);
      } else if (dir > 0 && curP && nextPara(curP)) {
        deleteParagraphBoundary(nextPara(curP), -1);
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
        // 画像・動画・音声のファイルの貼り付けは添付として扱う（形式と大きさの
        // 確認は app.js が行う）。それ以外のファイルは従来どおり文字として貼る
        const files = dt && dt.files ? Array.from(dt.files) : [];
        if (files.some(isMediaFile)) {
          if (onImageFileCb) onImageFileCb(files, null);
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

  /* 動画・音声のプレーヤーを使ったら、その段落を選ぶ（削除指定・コメントの
     対象にする）。再生・シーク・音量の操作はそのまま効かせる。キャレットを
     隣の段落へ移すと、続けて押した Space（一時停止）が本文への入力になって
     しまうので、フォーカスはプレーヤーに置いたままにする。直前の文字列選択は
     コメント対象を曖昧にするので外す */
  function selectPlayerPara(p) {
    selectImage(p);
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
      sel.removeAllRanges();
    }
  }

  function playerParaOf(node) {
    const player = node && node.closest ? node.closest('video, audio') : null;
    const p = player ? player.closest('.img-para') : null;
    return p && editor.contains(p) ? { p: p, player: player } : null;
  }

  /* Chrome の標準プレーヤーは操作部（再生ボタン・シークバー）の上の
     pointerdown / click を本文側へ一切流さない。操作するとプレーヤーに
     フォーカスが移るので、focusin で選ぶ（Tab で移った場合も同じ）。
     フォーカスが操作部の内側にあるとキー入力も本文側へ届かず、Backspace の
     削除指定が効かない。要素そのものにフォーカスを移し直すと、キーは本文へ
     届き、Space（再生・一時停止）や矢印（シーク）もプレーヤーに効く */
  let refocusing = false;
  function onEditorFocusIn(e) {
    if (refocusing) return;
    const hit = playerParaOf(e.target);
    if (!hit) return;
    selectPlayerPara(hit.p);
    refocusing = true;
    try {
      hit.player.focus({ preventScroll: true });
    } finally {
      refocusing = false;
    }
  }

  function onEditorClick(e) {
    const mmd = e.target.closest && e.target.closest('.mmd-widget');
    if (mmd && editor.contains(mmd)) {
      // Mermaid の図は contenteditable=false で選択もできない。何もしないと、
      // 未フォーカスのまま続けた入力が文書の先頭に飛ぶ（画像と同じ罠）。
      // 図の隣（直後の段落の先頭、なければ直前の段落の末尾）にキャレットを置く
      selectImage(null);
      editor.focus({ preventScroll: true });
      const near = nearestTextPara(topLevelIndex(mmd, 0));
      if (near) caretAtParaEdge(near.p, near.atEnd);
      return;
    }
    const p = e.target.closest && e.target.closest('.img-para');
    const hit = playerParaOf(e.target);
    if (hit) {
      // 動画の絵の上のクリック（操作部の外）は click まで届く
      selectPlayerPara(hit.p);
      hit.player.focus({ preventScroll: true });
      return;
    }
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

  // ---------- メディア段落（画像・動画・音声） ----------
  function kindOf(name) { return mediaKindFn(name) || 'image'; }

  function isMediaFile(f) {
    return !!f && (!!mediaKindFn(f.name || '') ||
      /^(image|video|audio)\//.test(f.type || ''));
  }

  /* ![説明](名前) の行を、拡張子に応じて画像・動画・音声の段落にする。
     どれも data-img / data-md を持つ同じ段落なので、削除指定・コメント・
     逆変換（feedback.py）は種類を問わずに同じ経路を通る */
  function makeImageParagraph(name, md, flags) {
    const p = document.createElement('p');
    const kind = kindOf(name);
    p.className = 'img-para';
    p.contentEditable = 'false';
    p.dataset.img = name;
    p.dataset.md = md;
    p.dataset.kind = kind;
    if (flags && flags.ins) p.dataset.ins = '1';
    if (flags && flags.del) p.dataset.del = '1';
    const src = '/api/sessions/' + encodeURIComponent(sessionId) +
      '/files/' + encodeURIComponent(name);
    let el;
    if (kind === 'video' || kind === 'audio') {
      el = document.createElement(kind);
      el.controls = true;
      // 開いた時点では長さと最初の絵だけを取る（全体は再生時に Range で取る）
      el.preload = 'metadata';
      if (kind === 'video') el.playsInline = true;
      el.src = src;
      el.setAttribute('aria-label', name);
    } else {
      el = document.createElement('img');
      el.src = src;
      el.alt = name;
    }
    const caption = document.createElement('span');
    caption.className = 'img-caption';
    caption.textContent = name;
    p.appendChild(el);
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

  /* 選択中のメディア段落の情報を返す（コメント対象の指定に使う） */
  function getSelectedImage() {
    if (!selectedImg || !selectedImg.isConnected) return null;
    const name = selectedImg.dataset.img || '';
    return {
      name: name,
      md: selectedImg.dataset.md || ('![](' + name + ')'),
      cids: imageCids(selectedImg),
      kind: selectedImg.dataset.kind || kindOf(name),
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

  /* 人間が追加したメディアの段落を入れる。after を渡せばその段落の後ろ
     （ドロップした位置や、続けて追加するときの直前の段落）、渡さなければ
     キャレットのある段落の後ろ、どちらも無ければ末尾に入れる */
  function insertImageParagraph(name, after) {
    snapshot();
    const p = makeImageParagraph(name, '![](' + name + ')', { ins: true });
    const r = getRange();
    let cur = after && after.isConnected && after.parentNode === editor
      ? after : null;
    if (!cur && r && editor.contains(r.startContainer)) {
      cur = paraOf(r.startContainer);
    }
    if (cur && cur.parentNode === editor) cur.after(p);
    else editor.appendChild(p);
    changed();
    return p;
  }

  // ---------- ファイルのドロップ ----------
  function hasFiles(e) {
    const types = e.dataTransfer ? e.dataTransfer.types : null;
    return !!types && Array.prototype.indexOf.call(types, 'Files') >= 0;
  }

  function onDragOver(e) {
    if (!hasFiles(e)) return; // 文字のドラッグは beforeinput 側で止める
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  }

  /* ファイルをドロップした段落の後ろに添付する。ブラウザの既定（ファイルを
     開いてページを離れる）は止める */
  function onDrop(e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length || !onImageFileCb) return;
    let p = document.elementFromPoint(e.clientX, e.clientY);
    while (p && p.parentNode !== editor) p = p.parentNode;
    onImageFileCb(files, p && p.tagName === 'P' ? p : null);
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

  // ---------- 引用文の検索（コメントの位置へ飛ぶ用） ----------
  /* 本文のテキストノードを連結した索引。コメントの quote は選択範囲の
     toString() から ZWSP を除いたものなので、同じ土台（改行マーカーと
     画像段落の中身を除き、ZWSP を取り除いた連結文字列）で照合する */
  function textIndex() {
    const chunks = [];
    let total = 0;
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (closestEl(n, '.pb') || closestEl(n, '.img-para') ||
          closestEl(n, '.ai-del') || closestEl(n, '.mmd-widget')) continue;
      const clean = n.data.replace(/\u200B/g, '');
      if (!clean) continue;
      chunks.push({ node: n, start: total, clean: clean });
      total += clean.length;
    }
    return { chunks: chunks, text: chunks.map((c) => c.clean).join('') };
  }

  /* result_text 相当の索引。textIndex() に加えて <del>（削除済み文字）も
     除く（result_text は削除を含まない）。<ins>（人間の挿入）は残す。
     AIの add_comment の quote はこの索引（＝get_feedback の result_text）に
     対して照合されているので、同じ土台で位置を復元する */
  function resultIndex() {
    const chunks = [];
    let total = 0;
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (closestEl(n, '.pb') || closestEl(n, '.img-para') ||
          closestEl(n, '.ai-del') || closestEl(n, '.mmd-widget') ||
          inDel(n)) continue;
      const clean = n.data.replace(/\u200B/g, '');
      if (!clean) continue;
      chunks.push({ node: n, start: total, clean: clean });
      total += clean.length;
    }
    return { chunks: chunks, text: chunks.map((c) => c.clean).join('') };
  }

  /* AIが付けたコメントを本文に結び付ける。quote を result 索引から探し、
     prefix/suffix で位置を一つに絞って Range を作り、人間と同じ
     wrapRangeComment に渡す。見つからなければ false を返す。 */
  function anchorAiComment(cid, quote, prefix, suffix) {
    const q = String(quote || '').replace(/[\u200B\u00B6]/g, '');
    if (!q) return false;
    const idx = resultIndex();
    const pre = String(prefix || '');
    const suf = String(suffix || '');
    const positions = [];
    let from = 0;
    for (;;) {
      const i = idx.text.indexOf(q, from);
      if (i < 0) break;
      positions.push(i);
      from = i + 1;
    }
    if (!positions.length) return false;
    let at;
    if (pre || suf) {
      const combo = idx.text.indexOf(pre + q + suf);
      at = combo >= 0 ? combo + pre.length
        : bestContextMatch(idx.text, q, positions, pre, suf);
    } else {
      at = positions[0];
    }
    if (at == null) return false;
    const s = pointAt(idx, at, false);
    const e = pointAt(idx, at + q.length, true);
    if (!s || !e) return false;
    const r = document.createRange();
    r.setStart(s.node, s.offset);
    r.setEnd(e.node, e.offset);
    return wrapRangeComment(r, cid);
  }

  /* 各出現位置の前後文脈が最もよく一致する位置を返す（サーバーと同じ） */
  function bestContextMatch(text, q, positions, pre, suf) {
    let best = null;
    let bestScore = -1;
    positions.forEach((pos) => {
      const before = text.slice(Math.max(0, pos - pre.length), pos);
      const after = text.slice(pos + q.length, pos + q.length + suf.length);
      let score = 0;
      for (let i = 0; i < before.length && i < pre.length; i++) {
        if (before[before.length - 1 - i] === pre[pre.length - 1 - i]) score++;
        else break;
      }
      for (let i = 0; i < after.length && i < suf.length; i++) {
        if (after[i] === suf[i]) score++;
        else break;
      }
      if (score > bestScore) { bestScore = score; best = pos; }
    });
    return best;
  }

  /* 索引上のオフセットを (node, ノード内オフセット) に写す。
     ZWSP は索引に入っていないので、飛ばしながら数える */
  function pointAt(idx, offset, isEnd) {
    for (const c of idx.chunks) {
      const end = c.start + c.clean.length;
      const inside = isEnd ? (offset > c.start && offset <= end)
        : (offset >= c.start && offset < end);
      if (!inside) continue;
      let need = offset - c.start;
      let raw = 0;
      while (raw < c.node.length && need > 0) {
        if (c.node.data[raw] !== ZWSP) need--;
        raw++;
      }
      return { node: c.node, offset: raw };
    }
    return null;
  }

  /* 引用文を本文から探して Range を返す。校正で文が書き換わっていて
     完全一致しないときは、先頭・末尾の一部で探して近傍に寄せる */
  function findText(quote) {
    const q = String(quote || '').replace(/[\u200B\u00B6]/g, '').trim();
    if (!q) return null;
    const idx = textIndex();
    const tries = [q];
    [24, 12].forEach((n) => {
      if (q.length > n) tries.push(q.slice(0, n).trim(), q.slice(-n).trim());
    });
    for (const key of tries) {
      if (key.length < 6 && key !== q) continue;
      const i = idx.text.indexOf(key);
      if (i < 0) continue;
      const s = pointAt(idx, i, false);
      const e = pointAt(idx, i + key.length, true);
      if (!s || !e) continue;
      const r = document.createRange();
      r.setStart(s.node, s.offset);
      r.setEnd(e.node, e.offset);
      return r;
    }
    return null;
  }

  // ---------- AI差分（前の稿とくらべてAIが変えた箇所の表示） ----------
  // 前の稿の最終状態（人間の校正を反映したテキスト）と今の稿の base_text を
  // 比べ、AIが消した文字を青い取り消し線の幽霊テキストで、AIが足した文字を
  // 青字（CSS Custom Highlight）で重ねる。今の稿の base_text は人間が編集
  // しても変わらない（編集は del/ins マークとして载るだけ）ので、差分は
  // 表示を有効にしたとき一度だけ計算し、DOMへの割り付けだけを編集のたびに
  // やり直す。幽霊（span.ai-del）は保存・undo・各種編集操作から除外される。
  let aiDiffOps = null; // {dels:[{pos,text}], inss:[{start,end}]} base_text 座標

  const AI_LINE_LIMIT = 2000;  // Myers差分の編集距離上限（行）
  const AI_CHAR_LIMIT = 1000;  // 同（文字）。超えたら塊ごと置換として扱う

  /* Myers O(ND) 差分。0=一致 / -1=aから削除 / 1=bへ追加 の配列を返す。
     編集距離が limit を超えるときは null。 */
  function myersOps(a, b, limit) {
    const N = a.length;
    const M = b.length;
    let pre = 0;
    while (pre < N && pre < M && a[pre] === b[pre]) pre++;
    let suf = 0;
    while (suf < N - pre && suf < M - pre && a[N - 1 - suf] === b[M - 1 - suf]) suf++;
    const n = N - pre - suf;
    const m = M - pre - suf;
    const cap = Math.min(n + m, limit);
    const off = cap + 1;
    const v = new Int32Array(2 * cap + 3);
    const trace = [];
    let D = -1;
    for (let d = 0; d <= cap && D < 0; d++) {
      trace.push(v.slice());
      for (let k = -d; k <= d; k += 2) {
        let x;
        if (k === -d || (k !== d && v[off + k - 1] < v[off + k + 1])) {
          x = v[off + k + 1];
        } else {
          x = v[off + k - 1] + 1;
        }
        let y = x - k;
        while (x < n && y < m && a[pre + x] === b[pre + y]) { x++; y++; }
        v[off + k] = x;
        if (x >= n && y >= m) { D = d; break; }
      }
    }
    if (D < 0) return null;
    const mid = [];
    let x = n;
    let y = m;
    for (let d = D; d > 0; d--) {
      const pv = trace[d];
      const k = x - y;
      let prevK;
      if (k === -d || (k !== d && pv[off + k - 1] < pv[off + k + 1])) {
        prevK = k + 1;
      } else {
        prevK = k - 1;
      }
      const px = pv[off + prevK];
      const py = px - prevK;
      while (x > px && y > py) { x--; y--; mid.push(0); }
      if (x === px) { y--; mid.push(1); } else { x--; mid.push(-1); }
    }
    while (x > 0 && y > 0) { x--; y--; mid.push(0); }
    mid.reverse();
    return new Array(pre).fill(0).concat(mid, new Array(suf).fill(0));
  }

  /* 1行分の文字差分を dels/inss（b座標）へ書き足す。行同士が似ていない
     （編集距離が長さの6割超）ときは行ごと置き換えとして扱い、細かい偶然の
     一致でノイズだらけの差分を出さないようにする */
  function charDiffLine(aLine, bLine, at, dels, inss) {
    const lim = Math.min(AI_CHAR_LIMIT,
      Math.ceil((aLine.length + bLine.length) * 0.6) + 1);
    const ops = myersOps(aLine, bLine, lim);
    if (!ops) {
      if (aLine) dels.push({ pos: at, text: aLine });
      if (bLine) inss.push({ start: at, end: at + bLine.length });
      return;
    }
    let ap = 0;
    let bp = 0;
    let i = 0;
    while (i < ops.length) {
      if (ops[i] === 0) { ap++; bp++; i++; continue; }
      const aStart = ap;
      const bStart = bp;
      while (i < ops.length && ops[i] !== 0) {
        if (ops[i] < 0) ap++; else bp++;
        i++;
      }
      if (ap > aStart) dels.push({ pos: at + bStart, text: aLine.slice(aStart, ap) });
      if (bp > bStart) inss.push({ start: at + bStart, end: at + bp });
    }
  }

  /* まず行単位で差分を取り、変わった塊の中だけ文字単位で詰める。
     全面的な書き換えで上限を超えたときは全体を1つの置換として返す */
  function computeAiDiff(prevText, baseText) {
    const dels = [];
    const inss = [];
    if (prevText === baseText) return { dels: dels, inss: inss };
    const a = prevText.split('\n');
    const b = baseText.split('\n');
    const starts = [0];
    b.forEach((line) => starts.push(starts[starts.length - 1] + line.length + 1));
    const total = baseText.length;
    const lineOps = myersOps(a, b, AI_LINE_LIMIT);
    if (!lineOps) {
      dels.push({ pos: 0, text: prevText + '\n' });
      inss.push({ start: 0, end: total });
      return { dels: dels, inss: inss };
    }
    let ai = 0;
    let bi = 0;
    let i = 0;
    while (i < lineOps.length) {
      if (lineOps[i] === 0) { ai++; bi++; i++; continue; }
      let dl = 0;
      let il = 0;
      while (i < lineOps.length && lineOps[i] !== 0) {
        if (lineOps[i] < 0) dl++; else il++;
        i++;
      }
      // 対応する行同士（先頭から順に組にする。行差分の塊の中に共通行は
      // 無いので、素直な組み方でよい）は文字単位で差分を取る
      const pairs = Math.min(dl, il);
      for (let j = 0; j < pairs; j++) {
        charDiffLine(a[ai + j], b[bi + j], starts[bi + j], dels, inss);
      }
      if (dl > pairs) {
        // 行が丸ごと消えた: 改行ごと消えたことを次の行頭（末尾なら文末）に示す
        const rest = a.slice(ai + pairs, ai + dl).join('\n');
        if (bi + il >= b.length) dels.push({ pos: total, text: '\n' + rest });
        else dels.push({ pos: starts[bi + il], text: rest + '\n' });
      }
      if (il > pairs) {
        // 行が丸ごと増えた: 行の改行分まで範囲に含める（表示は行内に丸める）
        const s0 = starts[bi + pairs];
        const len = starts[bi + il] - s0 - 1;
        inss.push(bi + il === b.length
          ? { start: Math.max(0, s0 - 1), end: s0 + len }
          : { start: s0, end: s0 + len + 1 });
      }
      ai += dl;
      bi += il;
    }
    return { dels: dels, inss: inss };
  }

  /* 今のDOMから base_text の行と、行内オフセット→テキストノードの索引を作る。
     人間の追加（ins）・改行マーカー・幽霊・ZWSPは base に無いので除く。
     行の組み立ては feedback.py の compose_original と同じ規約に従う */
  function baseIndex() {
    const lines = [];
    let cur = null;
    let merge = false;
    for (let el = editor.firstElementChild; el; el = el.nextElementSibling) {
      if (el.tagName !== 'P') continue;
      if (el.classList.contains('img-para')) {
        if (el.dataset.ins) { merge = false; cur = null; continue; }
        lines.push({ text: el.dataset.md || '', chunks: [], img: el, para: el });
        cur = null;
        merge = false;
        continue;
      }
      if (!(merge && cur)) {
        cur = { text: '', chunks: [], img: null, para: el };
        lines.push(cur);
      }
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) {
        if (closestEl(n, '.pb') || closestEl(n, '.ai-del') || inIns(n)) continue;
        const clean = n.data.replace(/\u200B/g, '');
        if (!clean) continue;
        cur.chunks.push({ node: n, start: cur.text.length, len: clean.length });
        cur.text += clean;
      }
      merge = !!trailingMarker(el, 'ins');
    }
    const starts = [0];
    lines.forEach((L) => starts.push(starts[starts.length - 1] + L.text.length + 1));
    return { lines: lines, starts: starts };
  }

  function aiLineOf(idx, pos) {
    let i = 0;
    while (i + 1 < idx.lines.length && idx.starts[i + 1] <= pos) i++;
    return i;
  }

  /* 行内オフセットを (テキストノード, ノード内オフセット) に写す */
  function aiPointAt(L, col, isEnd) {
    if (!L.chunks.length) return null;
    col = Math.max(0, Math.min(col, L.text.length));
    for (const c of L.chunks) {
      const end = c.start + c.len;
      const inside = isEnd ? (col > c.start && col <= end)
        : (col >= c.start && col < end);
      if (!inside) continue;
      let need = col - c.start;
      let raw = 0;
      while (raw < c.node.length && need > 0) {
        if (c.node.data[raw] !== ZWSP) need--;
        raw++;
      }
      return { node: c.node, offset: raw };
    }
    const last = L.chunks[L.chunks.length - 1];
    return col <= 0 ? { node: L.chunks[0].node, offset: 0 }
      : { node: last.node, offset: last.node.length };
  }

  /* AIが消したテキストの幽霊表示。編集不可・選択不可で、保存もされない */
  function injectAiDel(idx, pos, text) {
    const li = aiLineOf(idx, pos);
    const L = idx.lines[li];
    const col = pos - idx.starts[li];
    const span = document.createElement('span');
    span.className = 'ai-del';
    span.contentEditable = 'false';
    span.textContent = text.replace(/\n/g, '\u00B6');
    if (L.img) {
      if (col <= 0) L.img.before(span); else L.img.after(span);
      return;
    }
    const pt = aiPointAt(L, col, false);
    if (!pt) {
      const br = L.para.querySelector(':scope > br');
      if (br) br.before(span); else L.para.appendChild(span);
      return;
    }
    if (pt.offset <= 0) pt.node.before(span);
    else if (pt.offset >= pt.node.length) pt.node.after(span);
    else pt.node.splitText(pt.offset).before(span);
  }

  function clearAiOverlay() {
    editor.querySelectorAll('span.ai-del').forEach((el) => el.remove());
    editor.querySelectorAll('.ai-ins-obj').forEach((el) =>
      el.classList.remove('ai-ins-obj'));
    if (window.CSS && CSS.highlights) CSS.highlights.delete('hle-ai-ins');
  }

  function applyAiOverlay() {
    clearAiOverlay();
    if (!aiDiffOps || (!aiDiffOps.dels.length && !aiDiffOps.inss.length)) return;
    const idx = baseIndex();
    // 追加分のRangeを先に作る（幽霊挿入によるノード分割はRangeが追従する）
    const ranges = [];
    aiDiffOps.inss.forEach((ins) => {
      for (let li = aiLineOf(idx, ins.start);
           li < idx.lines.length && idx.starts[li] < ins.end; li++) {
        const L = idx.lines[li];
        const s = Math.max(ins.start, idx.starts[li]) - idx.starts[li];
        const e = Math.min(ins.end, idx.starts[li] + L.text.length) - idx.starts[li];
        if (L.img) { L.img.classList.add('ai-ins-obj'); continue; }
        if (e <= s) continue;
        const sp = aiPointAt(L, s, false);
        const ep = aiPointAt(L, e, true);
        if (!sp || !ep) continue;
        const r = document.createRange();
        r.setStart(sp.node, sp.offset);
        r.setEnd(ep.node, ep.offset);
        ranges.push(r);
      }
    });
    // 幽霊は後ろの位置から入れる（手前の分割で座標がずれないように）
    aiDiffOps.dels.slice().sort((x, y) => y.pos - x.pos).forEach((d) =>
      injectAiDel(idx, d.pos, d.text));
    if (window.CSS && CSS.highlights && window.Highlight) {
      CSS.highlights.set('hle-ai-ins', new Highlight(...ranges));
    }
  }

  /* AI差分表示の on/off。prevText（前の稿の最終テキスト）を渡すと有効化、
     null で無効化。変更箇所の数を返す */
  function setAiDiff(prevText) {
    if (prevText == null) {
      aiDiffOps = null;
      clearAiOverlay();
      return 0;
    }
    const idx = baseIndex();
    const baseText = idx.lines.map((L) => L.text).join('\n');
    aiDiffOps = computeAiDiff(
      String(prevText).replace(/\r\n?/g, '\n'), baseText);
    applyAiOverlay();
    return aiDiffOps.dels.length + aiDiffOps.inss.length;
  }

  // ---------- 前の稿で付けた場所を今の本文へ写す ----------
  // AIが次の稿を出すと本文は作り直され、前の稿のコメントのアンカー（span.cmt）は
  // 消える。引用文が書き換わっていると文字列では探せないが、前の稿の最終
  // テキストと今の稿の base_text の差分をたどれば、コメントを付けた場所に
  // 対応する位置は分かる（書き換わった文なら、その書き換わった範囲になる）。
  // AI差分と同じ方式（行単位 → 変わった塊の中だけ文字単位）で一致を取る。

  /* 2つのテキストで一致する塊 [{a, b, len}]（a・b はそれぞれの先頭からの
     文字位置。どちらも昇順）を返す。全面的な書き換えで差分の上限を超えたら
     null（位置を写せない）。書き換わった塊の中で似ていない行は一致なしとする */
  function matchBlocks(aText, bText) {
    const blocks = [];
    const push = (a, b, len) => {
      if (len <= 0) return;
      const last = blocks[blocks.length - 1];
      if (last && last.a + last.len === a && last.b + last.len === b) last.len += len;
      else blocks.push({ a: a, b: b, len: len });
    };
    if (aText === bText) {
      push(0, 0, aText.length);
      return blocks;
    }
    const a = aText.split('\n');
    const b = bText.split('\n');
    const aStarts = [0];
    a.forEach((l) => aStarts.push(aStarts[aStarts.length - 1] + l.length + 1));
    const bStarts = [0];
    b.forEach((l) => bStarts.push(bStarts[bStarts.length - 1] + l.length + 1));
    const lineOps = myersOps(a, b, AI_LINE_LIMIT);
    if (!lineOps) return null;
    // 文字単位の一致を push する。似ていない（編集距離が長さの6割超）ときは
    // 偶然の一致でノイズになるので、一致なしとして塊ごと書き換えと見なす
    const charBlocks = (aStr, bStr, aBase, bBase) => {
      const lim = Math.min(AI_CHAR_LIMIT,
        Math.ceil((aStr.length + bStr.length) * 0.6) + 1);
      const ops = myersOps(aStr, bStr, lim);
      if (!ops) return;
      let ap = 0;
      let bp = 0;
      let run = 0;
      for (let k = 0; k < ops.length; k++) {
        if (ops[k] === 0) { run++; ap++; bp++; continue; }
        if (run) { push(aBase + ap - run, bBase + bp - run, run); run = 0; }
        if (ops[k] < 0) ap++; else bp++;
      }
      if (run) push(aBase + ap - run, bBase + bp - run, run);
    };
    let ai = 0;
    let bi = 0;
    let i = 0;
    while (i < lineOps.length) {
      if (lineOps[i] === 0) {
        // 一致した行。行末の改行は、両方に次の行があれば一致に含める
        const nl = ai < a.length - 1 && bi < b.length - 1 ? 1 : 0;
        push(aStarts[ai], bStarts[bi], a[ai].length + nl);
        ai++; bi++; i++;
        continue;
      }
      let dl = 0;
      let il = 0;
      while (i < lineOps.length && lineOps[i] !== 0) {
        if (lineOps[i] < 0) dl++; else il++;
        i++;
      }
      if (dl > 0 && il > 0) {
        if (dl === il) {
          // 行数が同じなら、先頭から順に組にして1行ずつ合わせる
          for (let j = 0; j < dl; j++) {
            charBlocks(a[ai + j], b[bi + j], aStarts[ai + j], bStarts[bi + j]);
            if (j < dl - 1 || (ai + dl < a.length && bi + il < b.length)) {
              push(aStarts[ai + j + 1] - 1, bStarts[bi + j + 1] - 1, 1);
            }
          }
        } else {
          // 行数が違う（行の追加・削除を伴う）ときは、塊をまとめて合わせる
          charBlocks(a.slice(ai, ai + dl).join('\n'),
            b.slice(bi, bi + il).join('\n'), aStarts[ai], bStarts[bi]);
          if (ai + dl < a.length && bi + il < b.length) {
            push(aStarts[ai + dl] - 1, bStarts[bi + il] - 1, 1);
          }
        }
      }
      ai += dl;
      bi += il;
    }
    return blocks;
  }

  /* a の [s, e) を b の範囲に写す。一致した塊の中の端は、ずれを足すだけ。
     書き換わった部分（塊と塊の間）にかかる端は、その書き換わった部分の
     b 側の外側の端に寄せるので、文が丸ごと書き換わっていれば、その書き換え
     後の範囲全体になる */
  function mapSpan(blocks, bLen, s, e) {
    const mapPoint = (p, isEnd) => {
      let prevEndB = 0;
      for (const k of blocks) {
        const inside = isEnd ? (p > k.a && p <= k.a + k.len)
          : (p >= k.a && p < k.a + k.len);
        if (inside) return k.b + (p - k.a);
        if (isEnd ? p <= k.a : p < k.a) return isEnd ? k.b : prevEndB;
        prevEndB = k.b + k.len;
      }
      return isEnd ? bLen : prevEndB;
    };
    const ms = mapPoint(s, false);
    return { s: ms, e: Math.max(ms, mapPoint(e, true)) };
  }

  /* texts[0] の [s, e) を、texts を順にたどって最後のテキストの範囲に写す。
     途中で差分が取れない、または一致する所がまったく無い（全面的な書き換えで
     手がかりが無い）ときは null */
  function followSpan(texts, s, e) {
    let span = { s: s, e: e };
    for (let i = 0; i + 1 < texts.length; i++) {
      const blocks = matchBlocks(texts[i], texts[i + 1]);
      if (!blocks || !blocks.length) return null;
      span = mapSpan(blocks, texts[i + 1].length, span.s, span.e);
    }
    return span;
  }

  /* 今の本文の base_text（人間の校正を除いた、AIが出したとおりのテキスト）。
     followSpan の最後のテキストと、rangeFromBase の座標はこれに揃える */
  function baseText() {
    return baseIndex().lines.map((L) => L.text).join('\n');
  }

  /* base_text の [s, e) を DOM の Range にする。空の範囲（対象が丸ごと消えた
     場所）は、その行全体にする。写せなければ null */
  function rangeFromBase(s, e) {
    const idx = baseIndex();
    const n = idx.lines.length;
    if (!n) return null;
    const total = idx.starts[n] - 1;
    s = Math.max(0, Math.min(s, total));
    e = Math.max(s, Math.min(e, total));
    if (e <= s) {
      const li = aiLineOf(idx, s);
      s = idx.starts[li];
      e = s + idx.lines[li].text.length;
    }
    let sp = null;
    for (let li = aiLineOf(idx, s); li < n && !sp; li++) {
      const col = Math.max(0, s - idx.starts[li]);
      sp = aiPointAt(idx.lines[li], col, false);
    }
    let ep = null;
    for (let li = aiLineOf(idx, e); li >= 0 && !ep; li--) {
      const L = idx.lines[li];
      ep = aiPointAt(L, Math.min(e - idx.starts[li], L.text.length), true);
    }
    if (!sp || !ep) return null;
    const r = document.createRange();
    r.setStart(sp.node, sp.offset);
    r.setEnd(ep.node, ep.offset);
    if (r.collapsed) return null;
    return r;
  }

  // ---------- Mermaid 図 ----------
  // コードフェンスの言語が mermaid のブロックの直後に、描画した図を読み取り専用の
  // ウィジェット（div.mmd-widget）として挟む。ソースのテキストは今までどおり
  // 本文に残り、校正（削除・追加・コメント）はソースに対して行う。
  //
  // 図は本文ではない。保存とスナップショットからは cleanedClone で必ず落とす
  // （<div> は feedback.py の逆変換で段落の区切りになり、空の行が増えるため）。
  // 段落（<p>）の隣に <div> が挟まるので、前後の段落を辿る処理は prevPara /
  // nextPara を使う。ウィジェットはテキストノードを持たない（ラベルの文言は
  // CSS の疑似要素で出す）ので、コメントの索引や範囲の走査には現れない。
  //
  // ブロックの検出は、class ではなく今の結果の文字列（削除を除き追加を含む。
  // feedback.py の result_text と同じ規約）の行から行う。人間が編集しても
  // 図が「今の本文」に追従する。閉じていないフェンスは図にしない（書きかけの
  // フェンスが文書の残り全部を図にしてしまうのを避ける）。
  const MMD_DEBOUNCE = 400;
  const MMD_FENCE = /^\s*(`{3,}|~{3,})(.*)$/;
  let mmdTimer = null;

  function isMmdWidget(el) {
    return !!el && el.nodeType === 1 && el.classList.contains('mmd-widget');
  }
  function prevPara(p) {
    let e = p.previousElementSibling;
    while (isMmdWidget(e)) e = e.previousElementSibling;
    return e;
  }
  function nextPara(p) {
    let e = p.nextElementSibling;
    while (isMmdWidget(e)) e = e.nextElementSibling;
    return e;
  }

  /* 段落の「今の結果」の文字列。削除・改行マーカー・AI差分の幽霊は含めない */
  function paraResultText(p) {
    let s = '';
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (closestEl(n, '.pb') || closestEl(n, '.ai-del') || inDel(n)) continue;
      s += n.data;
    }
    return s.replace(/\u200B/g, '').replace(/\u00a0/g, ' ').replace(/\n/g, '');
  }

  /* 結果の行と、その行を構成する最後の段落（図を置く位置）の一覧。
     人間が削除した改行（del.pb）は次の段落と同じ行になり、削除指定の
     メディアは行ごと消える（feedback.py の _compose_lines の result と同じ） */
  function resultLines() {
    const lines = [];
    let mergePrev = false;
    for (let el = editor.firstElementChild; el; el = el.nextElementSibling) {
      if (el.tagName !== 'P') continue;
      let text;
      let mergeNext = false;
      if (el.classList.contains('img-para')) {
        if (el.dataset.del) { mergePrev = false; continue; }
        text = '';
      } else {
        text = paraResultText(el);
        mergeNext = !!trailingMarker(el, 'del');
      }
      if (lines.length && mergePrev) {
        lines[lines.length - 1].text += text;
        lines[lines.length - 1].para = el;
      } else {
        lines.push({ text: text, para: el });
      }
      mergePrev = mergeNext;
    }
    return lines;
  }

  /* ```mermaid ... ``` のブロックを {source, anchor} の配列で返す。
     anchor は閉じフェンスの行の最後の段落（図はその直後に置く） */
  function findMermaidBlocks(lines) {
    const blocks = [];
    let open = null;
    lines.forEach((L) => {
      const m = L.text.match(MMD_FENCE);
      if (!open) {
        // 開きフェンス。バッククォートのフェンスは info に ` を含められない
        if (!m || (m[1][0] === '`' && m[2].indexOf('`') >= 0)) return;
        const lang = (m[2].trim().split(/\s+/)[0] || '').toLowerCase();
        open = { ch: m[1][0], len: m[1].length, mermaid: lang === 'mermaid', src: [] };
        return;
      }
      if (m && m[1][0] === open.ch && m[1].length >= open.len && !m[2].trim()) {
        const source = open.src.join('\n');
        if (open.mermaid && source.trim()) {
          blocks.push({ source: source, anchor: L.para });
        }
        open = null;
        return;
      }
      if (open.mermaid) open.src.push(L.text);
    });
    return blocks;
  }

  function makeMermaidWidget() {
    const w = document.createElement('div');
    w.className = 'mmd-widget';
    w.contentEditable = 'false';
    w.setAttribute('role', 'img');
    w.setAttribute('aria-label', 'Mermaid の図');
    return w;
  }

  /* 描画結果をウィジェットに反映する。失敗したときは図の代わりにエラーの
     文言を出す（CSS の疑似要素。ソースのテキストは本文にそのまま残っている） */
  function showMermaid(w, res) {
    let img = w.querySelector('img');
    if (res.ok) {
      if (!img) {
        img = document.createElement('img');
        img.alt = '';
        img.draggable = false;
        img.addEventListener('error', () => showMermaid(w, {
          ok: false, error: '描画した図の画像を読み込めませんでした' }));
        w.appendChild(img);
      }
      img.width = res.width;
      img.height = res.height;
      img.src = res.url;
      w.dataset.state = 'ok';
      w.removeAttribute('data-error');
      w.setAttribute('aria-label', 'Mermaid の図');
    } else {
      if (img) img.remove();
      w.dataset.state = 'error';
      w.dataset.error = res.error;
      w.setAttribute('aria-label', 'Mermaid の図を描画できません: ' + res.error);
    }
  }

  function updateMermaidWidget(w, source) {
    const theme = window.MermaidView.theme();
    const key = theme + '\u0000' + source;
    if (w.dataset.key === key) return;
    w.dataset.key = key;
    const hit = window.MermaidView.peek(source, theme);
    if (hit) { showMermaid(w, hit); return; }
    if (!w.dataset.state) w.dataset.state = 'loading';
    window.MermaidView.render(source, theme).then((res) => {
      if (w.dataset.key === key) showMermaid(w, res);  // 古い依頼の結果は捨てる
    });
  }

  /* 今の本文に合わせて図の数・位置・中身を整える。すでに正しい位置にある
     ウィジェットは動かさずに使い回す（動かすと <img> を作り直すことになる） */
  function syncMermaid() {
    clearTimeout(mmdTimer);
    mmdTimer = null;
    if (!editor || !window.MermaidView) return;
    const blocks = findMermaidBlocks(resultLines());
    const widgets = Array.prototype.filter.call(editor.children, isMmdWidget);
    const taken = new Set();
    const slots = blocks.map((b) => {
      const next = b.anchor.nextElementSibling;
      if (isMmdWidget(next) && !taken.has(next)) { taken.add(next); return next; }
      return null;
    });
    const spare = widgets.filter((w) => !taken.has(w));
    blocks.forEach((b, i) => {
      let w = slots[i];
      if (!w) {
        w = spare.shift() || makeMermaidWidget();
        b.anchor.after(w);
      }
      updateMermaidWidget(w, b.source);
    });
    spare.forEach((w) => w.remove());
  }

  /* 編集のたびに呼ばれる。連続する入力のあいだは描き直さず、止まってから1回だけ */
  function scheduleMermaid() {
    if (!window.MermaidView) return;
    clearTimeout(mmdTimer);
    mmdTimer = setTimeout(syncMermaid, MMD_DEBOUNCE);
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
    if (opts && typeof opts.mediaKind === 'function') mediaKindFn = opts.mediaKind;
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
    editor.addEventListener('focusin', onEditorFocusIn);
    editor.addEventListener('dragover', onDragOver);
    editor.addEventListener('drop', onDrop);
    // Mermaid の図は配色に合わせて描き直す（ライト ⇄ ダーク）
    document.addEventListener('hle-theme', () => {
      editor.querySelectorAll('.mmd-widget').forEach((w) => {
        delete w.dataset.key;
      });
      syncMermaid();
    });
    syncMermaid();
  }

  window.Tracker = {
    init: init,
    undo: undo,
    redo: redo,
    wrapRangeComment: wrapRangeComment,
    anchorAiComment: anchorAiComment,
    addImageComment: addImageComment,
    unwrapComment: unwrapComment,
    insertImageParagraph: insertImageParagraph,
    getSelectedImage: getSelectedImage,
    getRange: getRange,
    findText: findText,
    setAiDiff: setAiDiff,
    followSpan: followSpan,
    baseText: baseText,
    rangeFromBase: rangeFromBase,
    html: function () {
      if (!editor) return '';
      // 選択・強調・AI差分といった一時的な表示状態は保存しない
      return cleanedClone().innerHTML;
    }
  };
})();

/* コードブロックの構文ハイライトのうち、トークンの計算だけを受け持つ。
 * 色を付ける側（本文の文字位置への対応づけと CSS Custom Highlight API への
 * 登録）は tracker.js にある。
 *
 * window.CodeHighlight:
 *   language(info)      フェンスの info（```python:app.py の "python:app.py" など）から
 *                       highlight.js の言語名を返す。色を付けない・知らない言語は null
 *   tokens(lang, code)  code を解析して [[開始, 終了, 種類], …]（文字位置、昇順、
 *                       重ならない）を返す。種類は keyword / builtin / title / attr /
 *                       number / string / meta / comment / add / del。解析できない
 *                       ときと大きすぎるときは null
 *
 * highlight.js の戻り値（HTML の文字列）は DOM に入れない。<span class="hljs-…">
 * と本文だけを読み取って、位置と種類の表に変えるだけ。 */
(function () {
  'use strict';

  // 1ブロックの解析にかける文字数の上限（細工した入力で固まるのを避ける）
  const MAX_CHARS = 60000;
  const CACHE_MAX = 64;
  // 色を付けない言語名（図・プレーンテキスト）。mermaid は図として別に描く
  const SKIP = new Set([
    'mermaid', 'text', 'txt', 'plain', 'plaintext', 'none', 'nohighlight',
  ]);

  // highlight.js のスコープ名 → 種類。'' は「色を付けない」（親の色も引き継がない）、
  // 表に無いスコープは親の種類を引き継ぐ
  const SCOPES = new Map([
    ['keyword', 'keyword'], ['selector-tag', 'title'],
    ['built_in', 'builtin'], ['type', 'builtin'],
    ['literal', 'number'], ['number', 'number'], ['symbol', 'number'],
    ['bullet', 'number'],
    ['string', 'string'], ['regexp', 'string'], ['char', 'string'],
    ['link', 'string'], ['code', 'string'],
    ['title', 'title'], ['section', 'title'], ['name', 'title'],
    ['selector-id', 'title'], ['selector-class', 'title'],
    ['attr', 'attr'], ['attribute', 'attr'], ['property', 'attr'],
    ['template-variable', 'attr'], ['selector-attr', 'attr'],
    ['selector-pseudo', 'attr'],
    ['comment', 'comment'], ['quote', 'comment'], ['doctag', 'comment'],
    ['addition', 'add'], ['deletion', 'del'],
    // タグの <, >, = や ${…} の中身、演算子は本文の色のままにする
    ['tag', ''], ['subst', ''], ['operator', ''], ['punctuation', ''],
  ]);

  /* <span class="…"> の class から種類を決める。undefined は「親を引き継ぐ」 */
  function categoryOf(cls) {
    const names = cls.split(/\s+/).filter(Boolean);
    const first = names[0] || '';
    if (first.indexOf('hljs-') !== 0) return undefined; // language-bash など
    const base = first.slice(5);
    const subs = names.slice(1);
    if (base === 'meta') {
      if (subs.indexOf('keyword_') >= 0) return 'keyword';
      if (subs.indexOf('string_') >= 0) return 'string';
      if (subs.indexOf('prompt_') >= 0) return 'builtin';
      return 'meta';
    }
    if (base === 'variable') {
      if (subs.indexOf('language_') >= 0) return 'builtin';
      if (subs.indexOf('constant_') >= 0) return 'number';
      return 'attr';
    }
    return SCOPES.get(base);
  }

  const ENTITY = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': "'" };
  const MARKUP = /<span class="([^"]*)">|<\/span>|([^<]+)/g;

  /* highlight.js の HTML を [[開始, 終了, 種類], …] に変える。出力の文字数が
     元のコードと合わない（想定外のマークアップ）ときは null */
  function parse(html, total) {
    const out = [];
    const stack = [];
    let pos = 0;
    let m;
    MARKUP.lastIndex = 0;
    while ((m = MARKUP.exec(html))) {
      if (m[1] !== undefined) {
        const cat = categoryOf(m[1]);
        stack.push(cat !== undefined ? cat : (stack.length ? stack[stack.length - 1] : ''));
      } else if (m[2] === undefined) {
        stack.pop();
      } else {
        const len = m[2].replace(/&(?:amp|lt|gt|quot|#x27);/g, (e) => ENTITY[e]).length;
        const cat = stack.length ? stack[stack.length - 1] : '';
        if (cat) {
          const last = out[out.length - 1];
          if (last && last[1] === pos && last[2] === cat) last[1] = pos + len;
          else out.push([pos, pos + len, cat]);
        }
        pos += len;
      }
    }
    return pos === total ? out : null;
  }

  function language(info) {
    if (!window.hljs) return null;
    const first = String(info || '').trim().split(/\s+/)[0] || '';
    // Zenn の ```python:app.py（言語:ファイル名）は言語の部分だけ使う
    const name = first.split(':')[0].toLowerCase();
    // 言語名は外部由来の文字列。許す文字を絞り、登録済みの文法に当たったものだけ通す
    if (!/^[a-z0-9_+#.-]{1,32}$/.test(name) || SKIP.has(name)) return null;
    return window.hljs.getLanguage(name) ? name : null;
  }

  // (言語, コード) → トークン。編集のたびに変わっていないブロックを解析し直さない
  const cache = new Map();

  function tokens(lang, code) {
    if (!lang || !window.hljs || !code || code.length > MAX_CHARS) return null;
    const key = lang + '\u0000' + code;
    if (cache.has(key)) {
      const hit = cache.get(key);
      cache.delete(key);
      cache.set(key, hit);
      return hit;
    }
    let result = null;
    try {
      const html = window.hljs.highlight(
        code, { language: lang, ignoreIllegals: true }).value;
      result = parse(html, code.length);
    } catch (e) {
      result = null;
    }
    cache.set(key, result);
    if (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
    return result;
  }

  window.CodeHighlight = { language: language, tokens: tokens };
})();

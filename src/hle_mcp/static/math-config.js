/* math-config.js — 数式の隔離フレーム（math-frame.html）で MathJax を読み込む
 * 前に、設定を置く。CSP がインラインのスクリプトを許さないので、設定も別ファイル。
 *
 * - 出力は SVG（フォントの字形を path にして、数式ごとに自己完結した SVG を作る）。
 *   親（校正エディタ）は SVG の文字列を受け取り、DOM には入れず CSS の mask に
 *   するだけなので、スクリプトも外部の読み込みも動かない。
 * - 拡張（\boldsymbol・\cancel・\color など）とフォントの追加分は、同梱した
 *   /static/mathjax/ 以下から読む。MathJax の既定の取得元（CDN）には出ない
 *   （フレームのCSPが通信を許さない）。
 * - 音声化・点字・Explorer・メニューは使わない。これらを入れたままだと、
 *   別ワーカー（speech-worker）を CDN から読もうとして、起動が終わらない。
 */
window.MathJax = {
  startup: { typeset: false },
  tex: {
    // 未定義のマクロ（\foo）を、赤い文字の「数式」として描かずエラーにする。
    // 校正では、描けない式を描けたように見せないほうが役に立つ
    packages: { '[-]': ['noundefined'] },
  },
  svg: {
    fontCache: 'none',
    // 式の中で勝手に折り返さない（折り返すと SVG が複数に分かれる）
    linebreaks: { inline: false },
    displayOverflow: 'overflow',
    fontPath: '/static/mathjax/fonts/%%FONT%%-font',
  },
  options: {
    enableMenu: false,
    enableSpeech: false,
    enableBraille: false,
    enableEnrichment: false,
    enableExplorer: false,
    menuOptions: {
      settings: {
        enrich: false,
        speech: false,
        braille: false,
        assistiveMml: false,
        collapsible: false,
      },
    },
  },
};

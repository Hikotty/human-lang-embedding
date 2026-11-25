/* HCL / Terraform の文法（highlight.js に足す自前の定義）。
 *
 * highlight.js の同梱言語には HCL が無いため、Terraform のコードブロック
 * （```hcl / ```terraform / ```tf）に色が付くようここで登録する。第三者の
 * ライブラリではなくこのリポジトリのコードで、hljs.min.js の後に読み込む。
 * 対象はブロックの見出し（resource など）、属性名、文字列と ${…} の補間、
 * ヒアドキュメント、コメント、数値、関数呼び出し。 */
(function () {
  'use strict';
  if (!window.hljs) return;

  window.hljs.registerLanguage('hcl', function (hljs) {
    const KEYWORDS = {
      keyword: 'resource data variable output locals module provider terraform ' +
        'moved import removed check backend required_providers required_version ' +
        'provisioner connection lifecycle dynamic content for in if else ' +
        'endif endfor',
      literal: 'true false null',
      built_in: 'var local each self path count',
    };
    const NUMBER = {
      scope: 'number', relevance: 0,
      begin: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/,
    };
    const FUNCTION_CALL = {
      scope: 'title.function', relevance: 0,
      begin: /[A-Za-z_][\w-]*(?=\()/,
    };
    // ${ … } と %{ … }（ディレクティブ）。$${ と %%{ は補間ではなく文字そのもの
    const INTERPOLATION = {
      scope: 'subst', begin: /[$%]\{~?/, end: /~?\}/,
      keywords: KEYWORDS, contains: [],
    };
    const STRING = {
      scope: 'string', begin: /"/, end: /"/,
      contains: [{ begin: /\$\$\{|%%\{/ }, hljs.BACKSLASH_ESCAPE, INTERPOLATION],
    };
    INTERPOLATION.contains = [STRING, NUMBER, FUNCTION_CALL];
    // <<EOT … EOT と <<-EOT … EOT。終わりの行は始まりと同じ識別子
    const HEREDOC = hljs.END_SAME_AS_BEGIN({
      scope: 'string',
      begin: /<<-?\s*([A-Za-z_]\w*)[ \t]*\n/,
      end: /^[ \t]*([A-Za-z_]\w*)[ \t]*$/,
      contains: [{ begin: /\$\$\{|%%\{/ }, INTERPOLATION],
    });
    return {
      name: 'HCL',
      aliases: ['terraform', 'tf', 'tfvars'],
      keywords: KEYWORDS,
      contains: [
        hljs.HASH_COMMENT_MODE,
        hljs.C_LINE_COMMENT_MODE,
        hljs.C_BLOCK_COMMENT_MODE,
        HEREDOC,
        STRING,
        NUMBER,
        FUNCTION_CALL,
        // name = … の name。== と => は属性の代入ではない
        { scope: 'attr', relevance: 0, begin: /[A-Za-z_][\w-]*(?=[ \t]*=(?![=>]))/ },
      ],
    };
  });
})();

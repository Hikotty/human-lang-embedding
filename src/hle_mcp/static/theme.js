/* theme.js — 画面の配色（ライト・ダーク）。全画面で読み込む。
 *
 * 配色はサーバーが <html data-theme="..."> に書いて返すので、読み込み時には
 * 何もしない（JS で後から切り替えると、一瞬だけ明るい画面が出る）。
 * 管理画面で変えたときは、同じブラウザで開いている他の画面にも
 * BroadcastChannel で知らせて、再読み込みなしで切り替える。
 */
(function () {
  'use strict';

  const THEMES = ['light', 'dark'];
  const channel = typeof BroadcastChannel === 'function'
    ? new BroadcastChannel('hle-theme') : null;

  function current() {
    const t = document.documentElement.dataset.theme;
    return THEMES.indexOf(t) >= 0 ? t : 'light';
  }

  function apply(theme) {
    if (THEMES.indexOf(theme) < 0) return;
    document.documentElement.dataset.theme = theme;
    document.dispatchEvent(new CustomEvent('hle-theme', { detail: theme }));
  }

  async function fetchTheme() {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data.theme;
  }

  /* 設定を保存し、この画面と他の画面に反映する。失敗したら例外 */
  async function save(theme) {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: theme }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    apply(data.theme);
    if (channel) channel.postMessage({ theme: data.theme });
    return data.theme;
  }

  if (channel) {
    channel.addEventListener('message', (e) => apply(e.data && e.data.theme));
  }
  // 戻る・進むでキャッシュから復元された画面は、読み込み時の配色のまま
  // なので、今の設定を取り直す
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) fetchTheme().then(apply).catch(() => {});
  });

  window.HleTheme = { THEMES: THEMES, current: current, apply: apply, save: save };
})();

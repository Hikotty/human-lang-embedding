"""配色（既定はライト、管理画面のトグルでダーク）の E2E テスト。

管理画面のトグルで切り替えると保存され、再読み込み後も、他の画面（一覧・校正
エディタ・HTMLレビュー）でも同じ配色になること、開いている他のタブも
その場で切り替わること、ダークでも文字が読める濃さであること、ライトの
見た目が変わっていないことを確かめる。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import storage

DARK_PAGE = "rgb(19, 19, 20)"
LIGHT_PAGE = "rgb(232, 234, 237)"

# 文字色と、祖先の背景を重ねた実際の背景から WCAG のコントラスト比を出す
CONTRAST_JS = """(el) => {
  const parse = (s) => {
    const m = /rgba?\\(([^)]+)\\)/.exec(s || '');
    if (!m) return [0, 0, 0, 0];
    const p = m[1].split(',').map(parseFloat);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  const chain = [];
  for (let e = el; e; e = e.parentElement) chain.unshift(e);
  let bg = [255, 255, 255];
  for (const e of chain) {
    const c = parse(getComputedStyle(e).backgroundColor);
    bg = [0, 1, 2].map((i) => c[i] * c[3] + bg[i] * (1 - c[3]));
  }
  const f = parse(getComputedStyle(el).color);
  const fg = [0, 1, 2].map((i) => f[i] * f[3] + bg[i] * (1 - f[3]));
  const lum = (c) => {
    const [r, g, b] = c.map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const a = lum(fg), b = lum(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}"""


def _collect_errors(page) -> list[str]:
    errors: list[str] = []
    page.on("console",
            lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def _bg(page, selector: str = "body") -> str:
    return page.locator(selector).first.evaluate(
        "el => getComputedStyle(el).backgroundColor")


def _theme(page) -> str:
    return page.evaluate("document.documentElement.dataset.theme")


def _is_on(page) -> bool:
    return page.locator("#dark-toggle").is_checked()


def _toggle(page) -> None:
    """人がするように、トグルの文字（ラベル）を押す。"""
    page.locator(".theme-toggle", has_text="ダークモード").click()


@pytest.fixture()
def reviewed():
    """校正済みの中身（削除・追加・コメント・AIの返信）を持つmdセッション。"""
    s = storage.create_session("本文の段落です。\n最後の段落。", "配色E2E", "/tmp/e2e")
    storage.add_comment(s["id"], "本文", "ここを直して", comment_id="c1")
    storage.add_comment(s["id"], "最後", "これは何？", comment_id="c2",
                        tag="question")
    storage.add_reply(s["id"], "c2", "締めの一文です。", author="ai")
    # AIが本文の箇所を指して付けたコメント（カードに文字の「AI」バッジが付く）。
    # 削除・追加の赤文字の見た目を変えないよう、地の文の span.cmt に結び付ける
    storage.add_comment(s["id"], "です", "この語尾の意図は？", author="ai",
                        comment_id="c3", tag="question", anchored=False)
    storage.set_document(
        s["id"],
        '<p><span class="cmt" data-cid="c1">本文</span>の<del>段落</del>'
        '<ins>文章</ins><span class="cmt" data-cid="c3">です</span>。'
        '<span class="md-link">[リンク](https://x.test)</span></p>'
        '<p><span class="cmt" data-cid="c2">最後</span>の段落。</p>')
    return s["id"]


@pytest.fixture()
def context(browser):
    ctx = browser.new_context()
    yield ctx
    ctx.close()


def test_admin_switch_saves_and_survives_reload(context, web_port):
    page = context.new_page()
    errors = _collect_errors(page)
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    # 既定はライトで、トグルはオフ
    assert _theme(page) == "light" and not _is_on(page)
    assert page.locator("#dark-toggle").get_attribute("role") == "switch"
    assert _bg(page) == LIGHT_PAGE

    _toggle(page)
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert _is_on(page)
    assert _bg(page) == DARK_PAGE
    page.wait_for_function(
        "() => fetch('/api/settings').then(r => r.json())"
        ".then(s => s.theme === 'dark')")
    assert storage.load_settings() == {"theme": "dark"}

    # 再読み込みしても、サーバーが返すHTMLの時点でダーク（ちらつかない）
    response = page.reload()
    assert b'<html data-theme="dark"' in response.body()
    assert _theme(page) == "dark" and _is_on(page)
    assert _bg(page) == DARK_PAGE

    _toggle(page)
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert not _is_on(page)
    assert _bg(page) == LIGHT_PAGE
    page.wait_for_function(
        "() => fetch('/api/settings').then(r => r.json())"
        ".then(s => s.theme === 'light')")
    assert errors == []


def test_toggle_works_from_keyboard(context, web_port):
    """Tab で入り、Space で切り替えられる。フォーカスの枠が出て、つまみが動く。"""
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    page.locator("#dark-toggle").focus()
    page.keyboard.press("Space")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert _is_on(page)
    knob = page.locator(".theme-toggle .switch")
    assert knob.evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
    # オンかどうかは色だけでなく、つまみの位置でも分かる（動き終わりを待つ）
    knob_x = ("() => getComputedStyle(document.querySelector("
              "'.theme-toggle .switch'), '::after').transform")
    page.wait_for_function(knob_x + " === 'matrix(1, 0, 0, 1, 14, 0)'")
    page.keyboard.press("Space")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.wait_for_function(knob_x + " === 'none'")
    page.wait_for_function(
        "() => fetch('/api/settings').then(r => r.json())"
        ".then(s => s.theme === 'light')")


def test_every_screen_uses_saved_dark_theme(context, web_port, reviewed):
    """一覧・校正エディタ・HTMLレビューもダークになる。レビュー対象のHTMLは
    作者が書いたとおり（白地）のまま見せる。"""
    storage.save_settings({"theme": "dark"})
    page = context.new_page()
    errors = _collect_errors(page)
    base = f"http://127.0.0.1:{web_port}"

    page.goto(base + "/")
    page.wait_for_selector("#session-table tr")
    assert _theme(page) == "dark" and _bg(page) == DARK_PAGE

    page.goto(f"{base}/session/{reviewed}")
    page.wait_for_selector("#editor p")
    assert _bg(page) == DARK_PAGE
    assert _bg(page, "#editor") == "rgb(30, 31, 32)"
    assert page.locator("#editor").evaluate(
        "el => getComputedStyle(el).color") == "rgb(227, 227, 227)"
    # 入力欄やプレーヤーなどブラウザが描く部品もダークになる
    assert page.locator("#draft-text").evaluate(
        "el => getComputedStyle(el).colorScheme") == "dark"

    h = storage.create_session(
        "<html><body><h1 id='lead'>対象</h1><p>本文</p></body></html>",
        "HTML配色", "/tmp/e2e", mode="html", source_path="/tmp/e2e/a.html")
    page.goto(f"{base}/session/{h['id']}")
    page.wait_for_function(
        "() => !!document.getElementById('doc-frame').contentDocument"
        ".querySelector('#lead')")
    assert _bg(page) == DARK_PAGE
    assert _bg(page, "#sidebar") == "rgb(24, 25, 26)"
    assert _bg(page, "#frame-wrap") == "rgb(255, 255, 255)"
    assert page.locator("#doc-frame").evaluate(
        "el => getComputedStyle(el).colorScheme") == "light"
    # 対象のHTMLの文字は黒のまま（ダークの文字色が入り込まない）
    assert page.evaluate(
        "() => { const d = document.getElementById('doc-frame').contentDocument;"
        " return getComputedStyle(d.querySelector('#lead')).color; }") \
        == "rgb(0, 0, 0)"
    assert errors == []


def test_open_tabs_follow_without_reload(context, web_port, reviewed):
    """管理画面で変えると、同じブラウザで開いている他の画面もその場で変わる。"""
    base = f"http://127.0.0.1:{web_port}"
    editor = context.new_page()
    editor.goto(f"{base}/session/{reviewed}")
    editor.wait_for_selector("#editor p")
    other_admin = context.new_page()
    other_admin.goto(f"{base}/admin")
    admin = context.new_page()
    admin.goto(f"{base}/admin")

    _toggle(admin)
    expect(editor.locator("html")).to_have_attribute("data-theme", "dark")
    assert _bg(editor) == DARK_PAGE
    # 別のタブの管理画面は、トグルも合わせる
    expect(other_admin.locator("#dark-toggle")).to_be_checked()

    _toggle(admin)
    expect(editor.locator("html")).to_have_attribute("data-theme", "light")
    assert _bg(editor) == LIGHT_PAGE
    expect(other_admin.locator("#dark-toggle")).not_to_be_checked()


def test_os_dark_setting_does_not_change_default(browser, web_port, reviewed):
    """既定はライト。OS がダークでも、トグルを入れるまではライトのまま。"""
    ctx = browser.new_context(color_scheme="dark")
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{web_port}/session/{reviewed}")
        page.wait_for_selector("#editor p")
        assert _theme(page) == "light"
        assert _bg(page) == LIGHT_PAGE
    finally:
        ctx.close()


EDITOR_TEXT = [
    "#editor p", "#editor del", "#editor ins", "#editor .cmt",
    "#editor .md-link", "#doc-title", "#doc-meta", "#sidebar h2",
    ".comment-card .body", ".comment-card .quote", ".tag-badge.issue",
    ".tag-badge.question", ".comment-card .who.human", ".comment-card .who.ai",
    "#actions .nav-link", "#save-state",
]
ADMIN_TEXT = [
    ".admin-card h3", ".admin-card .meta", ".admin-card button",
    ".admin-detail h4", ".annotated", ".annotated del", ".annotated ins",
    ".annotated .cmt-mark", ".annotated .cmt-label", ".admin-comment",
    ".admin-comment .who.ai", ".theme-toggle",
]


def _low_contrast(page, selectors):
    low = {}
    for sel in selectors:
        loc = page.locator(sel).first
        assert loc.count(), sel
        ratio = loc.evaluate(CONTRAST_JS)
        if ratio < 4.5:
            low[sel] = round(ratio, 2)
    return low


def test_dark_text_meets_contrast(context, web_port, reviewed):
    """ダークの主な文字は、背景に対して WCAG の 4.5:1 以上ある。"""
    storage.save_settings({"theme": "dark"})
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{reviewed}")
    page.wait_for_selector(".comment-card .who.ai")
    assert _low_contrast(page, EDITOR_TEXT) == {}
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    page.click("button[data-toggle]")
    page.wait_for_selector(".annotated .cmt-label")
    assert _low_contrast(page, ADMIN_TEXT) == {}


def test_light_theme_keeps_original_colors(context, web_port, reviewed):
    """色を変数にまとめ直しても、ライトの見た目は以前の色のまま。"""
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{reviewed}")
    page.wait_for_selector(".comment-card")
    style = "(el, p) => getComputedStyle(el)[p]"
    expected = [
        ("body", "backgroundColor", LIGHT_PAGE),
        ("#editor", "backgroundColor", "rgb(255, 255, 255)"),
        ("#editor", "color", "rgb(32, 33, 36)"),
        ("#editor del", "color", "rgb(197, 34, 31)"),
        ("#editor ins", "backgroundColor", "rgba(197, 34, 31, 0.06)"),
        ("#editor .cmt", "backgroundColor", "rgb(254, 239, 195)"),
        ("#editor .md-link", "color", "rgb(26, 115, 232)"),
        ("#sidebar", "backgroundColor", "rgb(248, 249, 250)"),
        ("#doc-meta", "color", "rgb(95, 99, 104)"),
        (".tag-badge.question", "color", "rgb(132, 48, 206)"),
        (".comment-card .who.ai", "color", "rgb(25, 103, 210)"),
        ("#btn-complete", "backgroundColor", "rgb(26, 115, 232)"),
    ]
    got = [(sel, prop, page.locator(sel).first.evaluate(style, prop))
           for sel, prop, _ in expected]
    assert got == expected


def test_failed_save_restores_previous_choice(context, web_port):
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    page.route("**/api/settings", lambda route: route.abort()
               if route.request.method == "POST" else route.continue_())
    messages: list[str] = []
    page.once("dialog", lambda d: (messages.append(d.message), d.accept()))
    _toggle(page)
    page.wait_for_function("() => true")
    expect(page.locator("#dark-toggle")).not_to_be_checked()
    assert messages and "配色を保存できませんでした" in messages[0]
    assert _theme(page) == "light" and _bg(page) == LIGHT_PAGE
    assert storage.load_settings() == {"theme": "light"}

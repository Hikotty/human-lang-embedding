"""ボタンのアイコン化の E2E テスト（Playwright / Chromium）。

校正エディタ・HTMLレビュー・管理画面・一覧のボタンは、文字や絵文字を持たず
Material Symbols のアイコンだけを見せる。名前は aria-label（読み上げ）と title
（ツールチップ）が持つ。確かめるのは次の3点。
1. どの画面のボタンも、見える文字が無く、アイコン（大きさのある SVG）と名前を持つ
2. 状態を持つボタン（AI差分・校正完了・範囲指定・管理画面の開閉）は、名前と見た目で状態を示す
3. 画面の操作部に絵文字が残っていない（ライトでもダークでもアイコンが見える）
"""
from __future__ import annotations
import re
import pytest
from playwright.sync_api import expect
from hle_mcp import server, storage
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u25A0-\u25FF]")
# ボタンとして並べているもの（#actions・コメント欄・下書き・バナー・管理画面のカード）
CONTROLS = ("#actions button:not([hidden]), #actions a, .comment-card button, "
            "#comment-draft button, #update-banner button, .admin-card .icon-btn, "
            ".comment-filter label")
def _controls(page) -> list[dict]:
    return page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel))
            .filter((el) => el.getClientRects().length > 0)
            .map((el) => {
                const svg = el.querySelector('svg.ico');
                const r = svg ? svg.getBoundingClientRect() : null;
                const input = el.querySelector('input');
                return {
                    html: el.outerHTML.slice(0, 120),
                    text: el.textContent.trim(),
                    label: el.getAttribute('aria-label') ||
                        (input && input.getAttribute('aria-label')) || '',
                    title: el.getAttribute('title') || '',
                    icon: !!svg && r.width >= 14 && r.height >= 14,
                    fill: svg ? getComputedStyle(svg).fill : '',
                    bg: getComputedStyle(el).backgroundColor,
                };
            })""", CONTROLS)
def _assert_icon_only(page, minimum: int) -> list[dict]:
    items = _controls(page)
    assert len(items) >= minimum, [i["html"] for i in items]
    for it in items:
        assert it["text"] == "", it["html"]       # 見える文字（日本語・絵文字）が無い
        assert it["icon"], it["html"]             # 大きさのあるアイコンがある
        assert it["label"], it["html"]            # 読み上げの名前がある
        assert it["title"], it["html"]            # ツールチップがある
        assert it["fill"] not in ("", "none"), it["html"]
    return items
def _chrome_text(page) -> str:
    return page.evaluate(
        "() => ['#topbar', '.comment-filter', '#comment-draft', '.card-actions', "
        "'.reply-row'].map((s) => Array.from(document.querySelectorAll(s))"
        ".map((e) => e.innerText).join(' ')).join(' ')")
@pytest.fixture()
def md_session():
    sid = storage.create_session("これはAIが書いた一文です。", "アイコン", "/tmp/ico")["id"]
    storage.add_revision(sid, "これはAIが書いた二文目です。")
    storage.add_comment(sid, "一文", "言い換えたい", author="human", comment_id="c1")
    server.add_comment(sid, "二文目", "根拠は？", tag="question")
    return sid
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_editor_buttons_are_icons_only(browser, web_port, md_session, theme):
    storage.save_settings({"theme": theme})
    page = browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{md_session}")
        page.wait_for_selector("#editor p")
        expect(page.locator(".comment-card")).to_have_count(2, timeout=10000)
        items = _assert_icon_only(page, 12)
        labels = [i["label"] for i in items]
        for name in ("AI差分", "元に戻す", "やり直す", "メディアを追加", "コメント",
                     "校正完了", "管理画面", "すべて", "人間", "AI", "編集", "削除", "送信"):
            assert name in labels, (name, labels)
        # 名前で探せる（読み上げと同じ名前）
        expect(page.get_by_role("button", name="元に戻す")).to_be_visible()
        expect(page.get_by_role("radio", name="人間")).to_be_visible()
        # 絞り込みのラジオの丸は見せない。透明な input がアイコン全体を覆い、
        # アイコンのどこを押しても選べる
        cover = page.evaluate(
            """() => Array.from(document.querySelectorAll('.comment-filter label')).map((l) => {
                const i = l.querySelector('input');
                const a = l.getBoundingClientRect(), b = i.getBoundingClientRect();
                return getComputedStyle(i).opacity === '0' &&
                    Math.abs(a.width - b.width) < 1 && Math.abs(a.height - b.height) < 1;
            })""")
        assert cover == [True, True, True]
        page.locator(".comment-filter label").nth(1).click(position={"x": 4, "y": 4})
        expect(page.get_by_role("radio", name="人間")).to_be_checked()
        page.get_by_role("radio", name="すべて").check()
        # 操作部に絵文字が残っていない
        assert not EMOJI.search(_chrome_text(page)), _chrome_text(page)
        # 編集フォームと下書きも同じ
        page.locator('.comment-card button[data-edit]').first.click()
        expect(page.locator(".comment-card .edit-actions .icon-btn")).to_have_count(2)
        _assert_icon_only(page, 12)
        page.locator(".comment-card button[data-edit-cancel]").click()
        page.evaluate(
            """() => { const p = document.querySelector('#editor p');
                const r = document.createRange(); r.setStart(p.firstChild, 0);
                r.setEnd(p.firstChild, 2); getSelection().removeAllRanges();
                getSelection().addRange(r); }""")
        page.click("#btn-comment")
        expect(page.locator("#comment-draft")).to_be_visible()
        _assert_icon_only(page, 14)
        page.click("#draft-cancel")
    finally:
        page.close()
def test_stateful_buttons_show_state_by_name_and_look(browser, web_port, md_session):
    page = browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{md_session}")
        page.wait_for_selector("#editor p")
        aidiff = page.locator("#btn-aidiff")
        expect(aidiff).to_have_attribute("aria-pressed", "false")
        before = aidiff.evaluate("(b) => getComputedStyle(b).backgroundColor")
        aidiff.click()
        expect(aidiff).to_have_attribute("aria-pressed", "true")
        page.mouse.move(0, 0)
        page.wait_for_timeout(300)  # 色の切り替え（0.12秒の遷移）が終わってから読む
        assert aidiff.evaluate("(b) => getComputedStyle(b).backgroundColor") != before
        aidiff.click()
        expect(aidiff).to_have_attribute("aria-pressed", "false")
        done = page.locator("#btn-complete")
        expect(done).to_have_attribute("aria-label", "校正完了")
        blue = done.evaluate("(b) => getComputedStyle(b).backgroundColor")
        page.once("dialog", lambda d: d.accept())
        done.click()
        expect(done).to_have_attribute("aria-label", "校正完了済み", timeout=5000)
        expect(done).to_have_attribute("title", "校正完了済み")
        page.mouse.move(0, 0)
        page.wait_for_timeout(300)
        assert done.evaluate("(b) => getComputedStyle(b).backgroundColor") != blue
        assert done.text_content().strip() == ""
    finally:
        page.close()
def test_html_review_buttons_are_icons_only(browser, web_port):
    sid = storage.create_session(
        "<html><body><h1>図</h1><p>本文です。</p></body></html>", "HTML", "/tmp/ico",
        mode="html")["id"]
    page = browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        page.wait_for_selector("#doc-frame")
        items = _assert_icon_only(page, 8)
        labels = [i["label"] for i in items]
        for name in ("範囲指定", "コメント", "校正完了", "一覧", "管理画面"):
            assert name in labels, (name, labels)
        region = page.locator("#btn-region")
        region.click()
        expect(region).to_have_class(re.compile(r"\bon\b"))
        page.keyboard.press("Escape")
        # 使い方の案内も、ボタンを絵文字ではなくアイコンで示す
        hint = page.locator(".hint")
        expect(hint.locator("svg.ico")).to_have_count(2)
        assert not EMOJI.search(hint.inner_text())
    finally:
        page.close()
def test_admin_and_list_buttons_are_icons_only(browser, web_port, md_session):
    page = browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/admin")
        expect(page.locator(".admin-card")).to_have_count(1)
        items = _assert_icon_only(page, 3)
        assert {"一覧", "校正結果を表示", "エディタで開く"} <= {i["label"] for i in items}
        toggle = page.locator("button[data-toggle]")
        icon_before = toggle.locator("svg path").get_attribute("d")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_have_attribute("aria-label", "校正結果を隠す")
        assert toggle.locator("svg path").get_attribute("d") != icon_before
        expect(page.locator(".admin-detail")).to_be_visible()
        # ダークモードの切り替えも、文字ではなくアイコンとスイッチ
        theme = page.locator(".theme-toggle")
        assert theme.inner_text().strip() == ""
        expect(theme.locator("svg.ico")).to_have_count(1)
        expect(page.get_by_role("switch", name="ダークモード")).to_be_attached()
        page.goto(f"http://127.0.0.1:{web_port}/")
        page.wait_for_selector("#session-table")
        _assert_icon_only(page, 1)
    finally:
        page.close()

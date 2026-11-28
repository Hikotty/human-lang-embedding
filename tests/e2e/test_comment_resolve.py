"""コメントの解決（完了済み）の E2E テスト。

人間はカードの「解決済みにする」で、AI は MCP の resolve_comment で、同じ
「解決済み」を付ける。解決済みのコメントはサイドバーの一覧から外れ、本文の
ハイライト（md の span.cmt、html の Highlight・範囲の枠）も消える。残った
カードの番号は変わらない。AI が付けた解決は、画面のポーリングで反映される。
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from hle_mcp import server, storage
from tests.conftest import make_png

YELLOW = "rgb(254, 239, 195)"
TRANSPARENT = "rgba(0, 0, 0, 0)"


def _open(browser, web_port, sid):
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
    return context, page


def _bg(locator):
    return locator.evaluate("el => getComputedStyle(el).backgroundColor")


# ---------- md エディタ ----------

def _make_md_session():
    sid = storage.create_session(
        "赤い空と青い海。", "解決E2E", "/tmp/e2e")["id"]
    storage.add_comment(sid, "赤", "一つ目", author="human", comment_id="c1")
    storage.add_comment(sid, "青", "二つ目", author="human", comment_id="c2")
    storage.add_reply(sid, "c2", "直しました", author="ai")
    storage.set_document(
        sid,
        '<p><span class="cmt" data-cid="c1">赤</span>い空と'
        '<span class="cmt" data-cid="c2">青</span>い海。</p>')
    return sid


def test_md_human_and_ai_resolve_comments(browser, web_port):
    sid = _make_md_session()
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_selector("#editor .cmt[data-cid]")
        cards = page.locator(".comment-card")
        expect(cards).to_have_count(2)
        c1 = page.locator('#editor .cmt[data-cid="c1"]')
        c2 = page.locator('#editor .cmt[data-cid="c2"]')
        assert _bg(c1) == YELLOW and _bg(c2) == YELLOW

        # 人間がカードのボタンで解決する。返信のカードにはボタンが無い
        card1 = page.locator('.comment-card[data-cid="c1"]')
        card2 = page.locator('.comment-card[data-cid="c2"]')
        expect(card2.get_by_role("button", name="解決済みにする")).to_have_count(1)
        card1.hover()
        btn = card1.get_by_role("button", name="解決済みにする")
        expect(btn).to_have_attribute("title", "解決済みにする: 一覧から外す（AIにも完了として伝わる）")
        btn.click()
        expect(cards).to_have_count(1)
        # 番号は振り直さない（残ったカードは C2 のまま）
        expect(card2.locator(".quote")).to_contain_text("C2:")
        assert _bg(c1) == TRANSPARENT and _bg(c2) == YELLOW
        saved = storage.load_session(sid)["comments"]
        assert saved[0]["resolved_by"] == "human"
        assert "resolved_at" not in saved[1]
        # 隠れたハイライトを押しても、カードは開かない
        c1.click()
        expect(page.locator(".comment-card.active")).to_have_count(0)

        # AI が resolve_comment で解決すると、ポーリングで一覧から消える
        server.resolve_comment(sid, "c2")
        expect(cards).to_have_count(0, timeout=10000)
        expect(page.locator("#comment-list .empty-note")).to_have_text(
            "未解決のコメントはありません。")
        assert _bg(c2) == TRANSPARENT
        assert storage.load_session(sid)["comments"][1]["resolved_by"] == "ai"

        # 解決しても本文の span は残る（未解決に戻せばハイライトも戻る）
        server.resolve_comment(sid, "c1", resolved=False)
        expect(cards).to_have_count(1, timeout=10000)
        expect(card1.locator(".quote")).to_contain_text("C1:")
        assert _bg(c1) == YELLOW
        assert page.locator("#editor .cmt[data-cid]").count() == 2

        # 再読み込みしても解決済みは一覧に出ない
        page.reload()
        page.wait_for_selector("#editor .cmt[data-cid]")
        expect(cards).to_have_count(1)
        expect(page.locator('.comment-card[data-cid="c1"]')).to_have_count(1)
    finally:
        context.close()


def test_md_resolve_with_author_filter(browser, web_port):
    """著者の絞り込みと解決を重ねても、解決済みは出ない。"""
    sid = _make_md_session()
    server.add_comment(sid, "い海", "AIの問い", tag="question")
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#editor .cmt[data-cid]')"
            ".length === 3", timeout=10000)
        storage.set_comment_resolved(sid, "c1", True, by="human")
        expect(page.locator(".comment-card")).to_have_count(2, timeout=10000)
        page.check('input[name="comment-filter"][value="human"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator('.comment-card[data-cid="c2"]')).to_have_count(1)
        page.check('input[name="comment-filter"][value="all"]')
        expect(page.locator(".comment-card")).to_have_count(2)
        assert _bg(page.locator('#editor .cmt[data-cid="c1"]')) == TRANSPARENT
    finally:
        context.close()


# ---------- HTMLレビュー ----------
HTML_DOC = (
    "<!doctype html><html><body><h1>見出し</h1>"
    "<p>これは重要な説明文です。</p>"
    "<p>これは補足の説明文です。</p></body></html>"
)


def _highlight_texts(page):
    return page.evaluate(
        """() => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get('hle-comment');
            return h ? [...h].map(r => r.toString()).sort() : [];
        }""")


def test_html_human_and_ai_resolve_comments(browser, web_port):
    sid = storage.create_session(
        HTML_DOC, "HTML解決E2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/d.html")["id"]
    storage.add_comment(sid, "重要", "一つ目", comment_id="c1",
                        prefix="これは", suffix="な説明文です")
    storage.add_comment(sid, "補足", "二つ目", comment_id="c2",
                        prefix="これは", suffix="の説明文です")
    storage.add_region_comment(
        sid, screenshot=make_png(40, 20), text="範囲",
        region={"x": 8, "y": 8, "width": 200, "height": 40},
        texts=["見出し"], comment_id="c3")
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_function(
            """() => {
                const win = document.getElementById('doc-frame').contentWindow;
                const h = win && win.CSS.highlights.get('hle-comment');
                return !!(h && h.size === 2);
            }""", timeout=10000)
        cards = page.locator(".comment-card")
        expect(cards).to_have_count(3)
        expect(page.locator('.region-box[data-cid="c3"]')).to_have_count(1)

        card1 = page.locator('.comment-card[data-cid="c1"]')
        card1.hover()
        card1.get_by_role("button", name="解決済みにする").click()
        expect(cards).to_have_count(2)
        assert _highlight_texts(page) == ["補足"]
        expect(page.locator('.comment-card[data-cid="c2"] .quote')).to_contain_text("C2:")
        assert storage.load_session(sid)["comments"][0]["resolved_by"] == "human"

        # 範囲コメントを解決すると、枠も消える
        card3 = page.locator('.comment-card[data-cid="c3"]')
        card3.hover()
        card3.get_by_role("button", name="解決済みにする").click()
        expect(cards).to_have_count(1)
        expect(page.locator(".region-box")).to_have_count(0)

        # AI の解決はポーリングで反映される
        server.resolve_comment(sid, "c2")
        expect(cards).to_have_count(0, timeout=10000)
        expect(page.locator("#comment-list .empty-note")).to_have_text(
            "未解決のコメントはありません。")
        assert _highlight_texts(page) == []
        fb = json.loads(server.get_feedback(sid, include_screenshots=False))
        assert fb["comments"] == []
    finally:
        context.close()

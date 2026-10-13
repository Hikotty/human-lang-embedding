"""コメントの絞り込み（著者別）の E2E テスト。

サイドバーの「すべて / 人間 / AI」で、トップレベルコメントの著者により
スレッド丸ごと（返信を含む）表示/非表示を切り替える。隠れたコメントは本文の
ハイライトも消し、「すべて」で元に戻る。既定は「すべて」で、リロードのたびに
「すべて」へ戻る。ポーリング由来の再描画でフィルタは戻らない。

「どの1変異を捕まえるか」= 本文ハイライトのフィルタ配線。
- md: applyCommentFilterToBody（span.cmt への cmt-filtered クラス付与）を外すと、
  カードは隠れてもハイライトが残り、下記のアサートが失敗する。
- html: applyHighlights のフィルタ除外を外すと、カードは隠れても
  Highlight（黄）が残り、下記のアサートが失敗する。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import server, storage


def _open(browser, web_port, sid):
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
    return context, page


# ---------- md エディタ ----------

def _make_md_session():
    """人間コメント1件（本文に span.cmt）＋AIコメント1件（ポーリングで
    本文にアンカーされる）を持つ md セッションを作る。"""
    sid = storage.create_session(
        "これはAIが書いた一文です。", "フィルタE2E", "/tmp/e2e")["id"]
    # 人間が「とても良い」を書き足し、「一文」に人間コメントを付けた状態
    storage.add_comment(
        sid, "一文", "ここは言い換えたい", author="human",
        comment_id="chuman1")
    storage.set_document(
        sid,
        "<p>これはAIが書いた<ins>とても良い</ins>"
        '<span class="cmt" data-cid="chuman1">一文</span>です。</p>')
    # AIが書き足し部分を指してコメントする（ポーリングで本文にアンカーされる）
    server.add_comment(sid, "とても良い", "この評価の根拠は？", tag="question")
    return sid


def test_md_filter_hides_cards_and_highlights(browser, web_port):
    sid = _make_md_session()
    context, page = _open(browser, web_port, sid)
    try:
        # AIコメントが本文にアンカーされるまで待つ（span.cmt が2つになる）
        page.wait_for_function(
            "() => document.querySelectorAll('#editor .cmt[data-cid]')"
            ".length === 2", timeout=10000)
        human = page.locator('#editor .cmt[data-cid="chuman1"]')
        ai = page.locator('#editor .cmt').filter(has_text="とても良い")

        yellow = "rgb(254, 239, 195)"
        transparent = "rgba(0, 0, 0, 0)"

        # 既定「すべて」: カード2件、両方のハイライトが黄色
        expect(page.locator(".comment-card")).to_have_count(2)
        assert human.evaluate(
            "el => getComputedStyle(el).backgroundColor") == yellow
        assert ai.evaluate(
            "el => getComputedStyle(el).backgroundColor") == yellow

        # 「人間」: AIカードが消え、AIハイライトも透明に、人間は残る
        page.check('input[name="comment-filter"][value="human"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator(".comment-card .who.human")).to_have_count(1)
        assert human.evaluate(
            "el => getComputedStyle(el).backgroundColor") == yellow
        assert ai.evaluate(
            "el => getComputedStyle(el).backgroundColor") == transparent

        # 「AI」: 逆になる
        page.check('input[name="comment-filter"][value="ai"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator(".comment-card .who.ai")).to_have_count(1)
        assert human.evaluate(
            "el => getComputedStyle(el).backgroundColor") == transparent
        assert ai.evaluate(
            "el => getComputedStyle(el).backgroundColor") == yellow

        # 「すべて」: 復元される
        page.check('input[name="comment-filter"][value="all"]')
        expect(page.locator(".comment-card")).to_have_count(2)
        assert human.evaluate(
            "el => getComputedStyle(el).backgroundColor") == yellow
        assert ai.evaluate(
            "el => getComputedStyle(el).backgroundColor") == yellow
    finally:
        context.close()


def test_md_filter_survives_polling_and_resets_on_reload(browser, web_port):
    sid = _make_md_session()
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#editor .cmt[data-cid]')"
            ".length === 2", timeout=10000)
        # 「AI」を選ぶ
        page.check('input[name="comment-filter"][value="ai"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        # ポーリング（約2.5秒）を1回またいでもフィルタは戻らない
        page.wait_for_timeout(3000)
        expect(page.locator('input[name="comment-filter"][value="ai"]'))\
            .to_be_checked()
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator(".comment-card .who.ai")).to_have_count(1)

        # リロードすると「すべて」に戻る
        page.reload()
        page.wait_for_selector("#editor .cmt[data-cid]")
        expect(page.locator('input[name="comment-filter"][value="all"]'))\
            .to_be_checked()
        expect(page.locator(".comment-card")).to_have_count(2)
    finally:
        context.close()


# ---------- HTMLレビュー ----------
HTML_DOC = (
    "<!doctype html><html><body><h1>見出し</h1>"
    "<p>これは重要な説明文です。</p>"
    "<p>これは補足の説明文です。</p></body></html>"
)


def _wait_frame(page):
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('h1'));
        }""")


def _make_html_session():
    """人間コメント1件＋AIコメント1件を持つ html セッションを作る。"""
    sid = storage.create_session(
        HTML_DOC, "HTMLフィルタE2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/d.html")["id"]
    # 人間が「補足」を指してコメント
    storage.add_comment(
        sid, "補足", "ここは本論に入れたい", author="human",
        prefix="これは", suffix="の説明文です")
    # AIが「重要」を指してコメント
    server.add_comment(
        sid, "重要", "この語に注目", prefix="これは", suffix="な説明文です")
    return sid


def _highlight_texts(page):
    return page.evaluate(
        """() => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get('hle-comment');
            return h ? [...h].map(r => r.toString()) : [];
        }""")


def test_html_filter_hides_cards_and_highlights(browser, web_port):
    sid = _make_html_session()
    context, page = _open(browser, web_port, sid)
    try:
        _wait_frame(page)
        # 両方のハイライトが出るまで待つ
        page.wait_for_function(
            """() => {
                const win = document.getElementById('doc-frame').contentWindow;
                const h = win.CSS.highlights.get('hle-comment');
                return !!(h && h.size === 2);
            }""", timeout=10000)

        # 既定「すべて」: カード2件、ハイライトは「重要」「補足」の2つ
        expect(page.locator(".comment-card")).to_have_count(2)
        assert set(_highlight_texts(page)) == {"重要", "補足"}

        # 「人間」: 人間（補足）だけ残る
        page.check('input[name="comment-filter"][value="human"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator(".comment-card .who.human")).to_have_count(1)
        assert _highlight_texts(page) == ["補足"]

        # 「AI」: AI（重要）だけ残る
        page.check('input[name="comment-filter"][value="ai"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator(".comment-card .who.ai")).to_have_count(1)
        assert _highlight_texts(page) == ["重要"]

        # 「すべて」: 復元される
        page.check('input[name="comment-filter"][value="all"]')
        expect(page.locator(".comment-card")).to_have_count(2)
        assert set(_highlight_texts(page)) == {"重要", "補足"}
    finally:
        context.close()


def test_html_filter_resets_on_reload(browser, web_port):
    sid = _make_html_session()
    context, page = _open(browser, web_port, sid)
    try:
        _wait_frame(page)
        page.check('input[name="comment-filter"][value="ai"]')
        expect(page.locator(".comment-card")).to_have_count(1)
        page.reload()
        _wait_frame(page)
        expect(page.locator('input[name="comment-filter"][value="all"]'))\
            .to_be_checked()
        expect(page.locator(".comment-card")).to_have_count(2)
    finally:
        context.close()

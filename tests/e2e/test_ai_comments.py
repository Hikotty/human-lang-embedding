"""AIが本文の箇所を指してコメントする（add_comment）の E2E テスト。

AIが付けたコメント（author='ai'）は、保存された本文にはまだ span.cmt が
無い。レビュー画面を開く／ポーリングのたびに、quote を本文から探して人間と
同じ経路（Tracker.wrapRangeComment）で包み、黄ハイライト・番号・カード↔本文
リンクが付く。見つからない quote は未アンカー注記付きのカードになり、返信・
削除はできる。

「どの1変異を捕まえるか」= tracker.js の anchorAiComment が wrapRangeComment を
呼ぶ配線。これを外すとハイライトが本文に付かず、下記のアサートが失敗する。
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


def test_ai_comment_anchors_to_human_added_text(browser, web_port):
    """人間が追加したテキストを指すAIコメントが、本文の該当位置に黄ハイライト
    付きのカードとして現れ、カード↔本文リンク・リロード後の残存・人間の返信の
    永続までを満たす。"""
    sid = storage.create_session(
        "これはAIが書いた一文です。", "AIコメントE2E", "/tmp/e2e")["id"]
    # 人間が「とても」を本文に書き足した状態（result_text に挿入が含まれる）
    storage.set_document(
        sid, "<p>これはAIが書いた<ins>とても良い</ins>一文です。</p>")
    # AIがその書き足し部分を指してコメントする
    out = server.add_comment(sid, "とても良い", "この評価の根拠は？", tag="question")
    assert '"anchored": false' in out

    context, page = _open(browser, web_port, sid)
    try:
        # ポーリング（約2.5秒）で本文に span.cmt が付く
        page.wait_for_selector("#editor .cmt[data-cid]", timeout=10000)
        cmt = page.locator("#editor .cmt")
        expect(cmt).to_have_text("とても良い")
        # 黄ハイライト（人間コメントと同じ見た目）。色は変えない
        bg = cmt.evaluate("el => getComputedStyle(el).backgroundColor")
        assert bg == "rgb(254, 239, 195)"
        # カードに文字の「AI」バッジが付く
        card = page.locator(".comment-card")
        expect(card.locator(".who.ai")).to_have_text("AI")
        expect(card.locator(".tag-badge.question")).to_have_text("質問")
        # 未アンカー注記は結び付いた後は出ない
        expect(card.locator(".unanchored-note")).to_have_count(0)
        # カードをクリックすると本文の該当箇所が active になる（↔リンク）
        page.click(".comment-card .quote")
        expect(cmt).to_have_class("cmt active")

        # 人間がAIコメントに返信する（author=human で永続する）
        cid = cmt.get_attribute("data-cid")
        page.fill('textarea[data-reply="' + cid + '"]', "根拠はこうです")
        page.click('button[data-send="' + cid + '"]')
        expect(page.locator(".comment-card .reply")).to_have_count(1)
        saved = storage.load_session(sid)["comments"][0]
        assert saved["replies"][0]["author"] == "human"
        assert saved["replies"][0]["text"] == "根拠はこうです"

        # リロードしても span.cmt は残る（edited_html に保存されている）
        page.reload()
        page.wait_for_selector("#editor .cmt[data-cid]")
        expect(page.locator("#editor .cmt")).to_have_text("とても良い")
    finally:
        context.close()


def test_ai_comment_unfound_quote_is_replyable(browser, web_port):
    """本文に無い quote のAIコメントは未アンカー注記付きのカードになり、
    返信・削除はできる。"""
    sid = storage.create_session("短い本文です。", "未アンカーE2E", "/tmp/e2e")["id"]
    # add_comment は本文照合を通すので、照合後に本文を変えて見つからなくする
    server.add_comment(sid, "短い", "この語について")
    # 本文を書き換えて quote を消す（コメントは残る＝未アンカー）
    storage.set_document(sid, "<p>すっかり別の本文になりました。</p>")

    context, page = _open(browser, web_port, sid)
    try:
        card = page.locator(".comment-card")
        expect(card).to_have_count(1)
        expect(card.locator(".unanchored-note")).to_have_text(
            "本文に該当箇所が見つかりません")
        expect(card.locator(".who.ai")).to_have_text("AI")
        # 本文に span.cmt は付かない
        expect(page.locator("#editor .cmt")).to_have_count(0)
        # 返信はできる
        cid = card.get_attribute("data-cid")
        page.fill('textarea[data-reply="' + cid + '"]', "人間の返信")
        page.click('button[data-send="' + cid + '"]')
        expect(card.locator(".reply")).to_have_count(1)
    finally:
        context.close()


def test_ai_comment_re_anchors_after_undo(browser, web_port):
    """undo で span.cmt が落ちても、次のポーリングで再アンカリングされる。"""
    sid = storage.create_session(
        "これはAIが書いた一文です。", "再アンカーE2E", "/tmp/e2e")["id"]
    storage.set_document(
        sid, "<p>これはAIが書いた<ins>とても良い</ins>一文です。</p>")
    server.add_comment(sid, "とても良い", "根拠は？")

    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_selector("#editor .cmt[data-cid]", timeout=10000)
        # 本文に何か入力してから undo すると、innerHTML スナップショットで
        # ラッパーが落ちることがある。undo を踏んでも次ポーリングで戻る
        page.locator("#editor").click()
        page.evaluate("() => window.Tracker.undo()")
        # 次のポーリングで span.cmt が復活する
        page.wait_for_selector("#editor .cmt[data-cid]", timeout=10000)
        expect(page.locator("#editor .cmt")).to_have_text("とても良い")
    finally:
        context.close()


# ---------- HTMLモード ----------
HTML_DOC = (
    "<!doctype html><html><body><h1>見出し</h1>"
    "<p>これは<b>重要</b>な点です。</p><p>これは別の点です。</p></body></html>"
)


def _wait_frame(page):
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('h1'));
        }""")


def test_ai_comment_html_shows_badge_and_highlight(browser, web_port):
    """HTMLレビューでも、AIコメントがAIバッジ付きカードと黄ハイライトで出る。"""
    sid = storage.create_session(
        HTML_DOC, "HTML AIコメントE2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/d.html")["id"]
    out = server.add_comment(sid, "重要", "この語に注目", prefix="これは",
                             suffix="な点です")
    assert '"mode": "html"' in out

    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        _wait_frame(page)
        card = page.locator(".comment-card")
        expect(card).to_have_count(1)
        expect(card.locator(".who.ai")).to_have_text("AI")
        # iframe 本文に locate() で復元された黄ハイライトが出る
        page.wait_for_function(
            """() => {
                const win = document.getElementById('doc-frame').contentWindow;
                const h = win.CSS.highlights.get('hle-comment');
                return !!(h && h.size);
            }""")
        txt = page.evaluate(
            """() => {
                const win = document.getElementById('doc-frame').contentWindow;
                const h = win.CSS.highlights.get('hle-comment');
                return [...h][0].toString();
            }""")
        assert txt == "重要"
    finally:
        context.close()

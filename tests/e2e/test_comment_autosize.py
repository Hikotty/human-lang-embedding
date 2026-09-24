"""コメントの入力欄（下書き・編集・返信）が書いた分だけ伸びることの E2E テスト。

欄の伸縮は CSS の field-sizing: content に任せている。伸び方そのものは
ブラウザの実装なので、ここでは実寸で次を確かめる。
- 書いた分だけ高くなり、欄の中にスクロールが出ない（全文が見えている）
- 消せば元の高さに戻る
- 上限（画面の高さの6割）で伸びるのをやめ、欄の中でスクロールする
- 既存の長いコメントを編集に開いた時点で全文が見える
- 返信欄は1行から伸び、Shift+Enter で改行、Enter で送信できる
- 切れ目のない長い文字列でもカードの枠からはみ出さない
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import storage

from .conftest import select_in_editor

# 10行のコメント。3行ぶんの初期の高さには収まらない
TEN_LINES = "\n".join(f"{i}行目のコメントです。" for i in range(1, 11))
# 上限（画面の高さの6割）を確実に超える長さ
HUNDRED_LINES = "\n".join(f"{i}行目" for i in range(1, 101))
# 折り返しの切れ目がない長い文字列
LONG_URL = "https://example.com/" + "a" * 300


def metrics(page, selector: str) -> dict:
    """欄の表示上の高さ（clientHeight）と中身全体の高さ（scrollHeight）。"""
    return page.eval_on_selector(
        selector,
        """el => ({
            h: el.clientHeight,
            sh: el.scrollHeight,
            vh: window.innerHeight,
        })""",
    )


def assert_fits(m: dict) -> None:
    """欄の中にスクロールが出ていない（全文が見えている）。"""
    assert m["sh"] <= m["h"] + 1, m


def open_draft(page) -> None:
    select_in_editor(page, 0, 0, 3)  # 「これは」
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()


def add_comment(page, sid, text: str) -> str:
    """下書き欄からコメントを追加して、そのIDを返す。"""
    open_draft(page)
    page.fill("#draft-text", text)
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    return storage.load_session(sid)["comments"][0]["id"]


def type_two_lines(page, selector: str) -> None:
    """欄をクリックして「一行目」Shift+Enter「二行目」と打つ。"""
    page.click(selector)
    page.keyboard.type("一行目")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("二行目")
    expect(page.locator(selector)).to_have_value("一行目\n二行目")


def test_draft_grows_with_text_and_shrinks_back(review):
    page, _sid = review
    open_draft(page)
    empty = metrics(page, "#draft-text")
    page.fill("#draft-text", TEN_LINES)
    grown = metrics(page, "#draft-text")
    assert grown["h"] > empty["h"] * 2  # 3行ぶんから10行ぶんへ
    assert_fits(grown)
    # 消せば初期の3行ぶんに戻る（それより低くはならない）
    page.fill("#draft-text", "短い")
    assert metrics(page, "#draft-text")["h"] == empty["h"]


def test_draft_stops_growing_at_cap_and_scrolls(review):
    page, _sid = review
    open_draft(page)
    empty = metrics(page, "#draft-text")
    page.fill("#draft-text", HUNDRED_LINES)
    m = metrics(page, "#draft-text")
    assert m["h"] > empty["h"] * 4  # 上限までは伸びる
    assert m["h"] <= m["vh"] * 0.6  # 上限で止まる
    assert m["sh"] > m["h"]         # 残りは欄の中でスクロールする
    # 伸びた欄に押し出されず、「追加」ボタンは画面内に残る
    expect(page.locator("#draft-submit")).to_be_in_viewport()


def test_edit_form_opens_with_whole_long_comment(review):
    page, sid = review
    cid = add_comment(page, sid, TEN_LINES)
    page.click(f'button[data-edit="{cid}"]')
    sel = f'textarea[data-edit-input="{cid}"]'
    expect(page.locator(sel)).to_have_value(TEN_LINES)
    m = metrics(page, sel)
    assert_fits(m)  # 開いた時点で10行すべてが見えている
    min_h = page.eval_on_selector(
        sel, "el => parseFloat(getComputedStyle(el).minHeight)")
    assert m["h"] > min_h * 2


def test_reply_box_grows_and_enter_sends(review):
    page, sid = review
    cid = add_comment(page, sid, "返信欄の確認")
    sel = f'textarea[data-reply="{cid}"]'
    one_line = metrics(page, sel)
    type_two_lines(page, sel)
    two_lines = metrics(page, sel)
    assert two_lines["h"] > one_line["h"]
    assert_fits(two_lines)
    # Enter で送信する（Shift+Enter の改行は本文に残る）
    page.keyboard.press("Enter")
    expect(page.locator(".comment-card .reply")).to_have_count(1)
    replies = storage.load_session(sid)["comments"][0]["replies"]
    assert [r["text"] for r in replies] == ["一行目\n二行目"]
    # 送信後の欄は空の1行に戻る
    expect(page.locator(sel)).to_have_value("")
    assert metrics(page, sel)["h"] == one_line["h"]


def test_reply_enter_while_ime_composing_does_not_send(review):
    """IMEの変換を確定する Enter では送信しない。"""
    page, sid = review
    cid = add_comment(page, sid, "IMEの確認")
    sel = f'textarea[data-reply="{cid}"]'
    page.fill(sel, "へんかんちゅう")
    page.eval_on_selector(
        sel,
        """el => {
            const opts = {key: 'Enter', bubbles: true, cancelable: true};
            el.dispatchEvent(new KeyboardEvent(
                'keydown', Object.assign({isComposing: true}, opts)));
            // Safari は確定の keydown で isComposing が false になり得る
            const e = new KeyboardEvent('keydown', opts);
            Object.defineProperty(e, 'keyCode', {get: () => 229});
            el.dispatchEvent(e);
        }""",
    )
    page.wait_for_timeout(500)
    assert storage.load_session(sid)["comments"][0]["replies"] == []
    expect(page.locator(sel)).to_have_value("へんかんちゅう")


def test_long_unbroken_text_stays_inside_card(review):
    """切れ目のない長いURLでも、カード本文と返信欄が枠からはみ出さない。"""
    page, sid = review
    cid = add_comment(page, sid, LONG_URL)
    page.fill(f'textarea[data-reply="{cid}"]', LONG_URL)
    m = page.eval_on_selector(
        ".comment-card",
        """card => {
            const body = card.querySelector('.body');
            const right = (sel) => card.querySelector(sel)
                .getBoundingClientRect().right;
            return {
                bodyOverflow: body.scrollWidth - body.clientWidth,
                cardRight: card.getBoundingClientRect().right,
                replyRight: right('textarea[data-reply]'),
                sendRight: right('button[data-send]'),
            };
        }""",
    )
    assert m["bodyOverflow"] <= 1  # 本文は枠内で折り返す
    # 返信欄が広がって送信ボタンを枠の外へ押し出さない
    assert m["replyRight"] < m["sendRight"] <= m["cardRight"]
    assert_fits(metrics(page, f'textarea[data-reply="{cid}"]'))


# ---------- HTMLレビューページ ----------
HTML_DOC = ("<!doctype html><html><body>"
            "<p id=\"lead\">これは重要な説明文です。</p></body></html>")


@pytest.fixture()
def html_page(browser, web_port):
    session = storage.create_session(
        HTML_DOC, "HTML伸長E2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/page.html")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('#lead'));
        }"""
    )
    yield page, session["id"]
    context.close()


def test_html_review_comment_boxes_grow(html_page):
    """HTMLレビューでも下書き欄と返信欄が書いた分だけ伸びる。"""
    page, sid = html_page
    page.evaluate(
        """() => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const node = doc.querySelector('#lead').firstChild;
            const r = doc.createRange();
            r.setStart(node, 0);
            r.setEnd(node, 3);
            const s = doc.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            doc.dispatchEvent(new Event('selectionchange'));
        }"""
    )
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    empty = metrics(page, "#draft-text")
    page.fill("#draft-text", TEN_LINES)
    grown = metrics(page, "#draft-text")
    assert grown["h"] > empty["h"] * 2
    assert_fits(grown)
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    cid = storage.load_session(sid)["comments"][0]["id"]

    sel = f'textarea[data-reply="{cid}"]'
    one_line = metrics(page, sel)
    type_two_lines(page, sel)
    assert metrics(page, sel)["h"] > one_line["h"]
    page.keyboard.press("Enter")
    expect(page.locator(".comment-card .reply")).to_have_count(1)
    replies = storage.load_session(sid)["comments"][0]["replies"]
    assert [r["text"] for r in replies] == ["一行目\n二行目"]

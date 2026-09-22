"""AI差分表示（前の稿からAIが変えた箇所を青で示す）の E2E テスト。

AIが消した文字は青い取り消し線の幽霊テキスト（span.ai-del）、足した文字は
CSS Custom Highlight（hle-ai-ins）の青字で重ねる。幽霊は保存・undo・引用の
いずれにも混入しないこと、トグルで気軽に出し入れできることを検証する。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import feedback, storage

from .conftest import select_in_editor

REV1 = "\n".join([
    "AIが書いた最初の文です。",
    "この行は書き換えられます。",
    "この行は消されます。",
    "最後の行です。",
])
REV2 = "\n".join([
    "AIが書いた最初の文です。",
    "この行は大きく書き換えられました。",
    "最後の行です。",
    "新しい行が増えました。",
])


@pytest.fixture()
def diff_review(browser, web_port):
    """第2稿まで進んだセッションのエディタページを開く。"""
    session = storage.create_session(REV1, "AI差分E2E", "/tmp/e2e")
    storage.add_revision(session["id"], REV2)
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_selector("#editor p")
    yield page, session["id"]
    context.close()


def wait_saved(page):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)


INS_TEXTS = """() => {
  const h = CSS.highlights.get('hle-ai-ins');
  return h ? [...h].map((r) => r.toString()) : null;
}"""


def test_button_hidden_on_first_revision(review):
    """第1稿には比較対象が無いのでボタンを出さない。"""
    page, sid = review
    expect(page.locator("#btn-aidiff")).to_be_hidden()


def test_toggle_shows_and_hides_ai_diff(diff_review):
    page, sid = diff_review
    btn = page.locator("#btn-aidiff")
    expect(btn).to_be_visible()
    btn.click()
    expect(btn).to_have_class("on")
    # AIが消した行が幽霊テキストとして見える（改行は¶で示す）
    dels = page.locator("#editor .ai-del")
    expect(dels).to_have_count(2)
    texts = dels.all_inner_texts()
    assert "この行は消されます。¶" in texts
    # AIが足した文字が青字ハイライトになっている
    ins_texts = page.evaluate(INS_TEXTS)
    assert any("新しい行が増えました。" in t for t in ins_texts)
    assert any("大きく" in t for t in ins_texts)
    # もう一度押すと全部消える
    btn.click()
    expect(btn).not_to_have_class("on")
    expect(page.locator("#editor .ai-del")).to_have_count(0)
    assert page.evaluate("() => CSS.highlights.has('hle-ai-ins')") is False


def test_editing_while_diff_on_keeps_save_clean(diff_review):
    """差分表示中に校正しても、保存されるHTMLに幽霊が混入しない。"""
    page, sid = diff_review
    page.locator("#btn-aidiff").click()
    expect(page.locator("#editor .ai-del")).to_have_count(2)
    select_in_editor(page, 0, 12, 12)  # 1段落目「…最初の文です。|」
    page.keyboard.type("追記")
    expect(page.locator("#editor ins").first).to_have_text("追記")
    wait_saved(page)
    html = storage.load_session(sid)["revisions"][-1]["edited_html"]
    assert "ai-del" not in html
    assert "ai-ins-obj" not in html
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == REV2
    assert "追記" in fb["result_text"]
    # 編集後もオーバーレイは張り直されて残っている
    expect(page.locator("#editor .ai-del")).to_have_count(2)


def test_undo_leaves_no_ghost_residue(diff_review):
    """差分表示中の編集をオフにしてから戻しても、幽霊が復活しない。"""
    page, sid = diff_review
    page.locator("#btn-aidiff").click()
    select_in_editor(page, 0, 12, 12)
    page.keyboard.type("X")
    expect(page.locator("#editor ins").first).to_have_text("X")
    page.locator("#btn-aidiff").click()  # オフ
    expect(page.locator("#editor .ai-del")).to_have_count(0)
    page.click("#btn-undo")
    expect(page.locator("#editor ins")).to_have_count(0)
    expect(page.locator("#editor .ai-del")).to_have_count(0)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["result_text"] == REV2


def test_comment_quote_excludes_ghost_text(diff_review):
    """幽霊テキストをまたいで選択しても、引用に幽霊が混ざらない。"""
    page, sid = diff_review
    page.locator("#btn-aidiff").click()
    # 「最後の行です。」の段落は先頭に幽霊「この行は消されます。¶」を持つ。
    # 段落全体を選択してコメントを付ける
    page.evaluate(
        """() => {
            const paras = document.querySelectorAll('#editor p');
            const p = paras[2];  // 「最後の行です。」
            const r = document.createRange();
            r.selectNodeContents(p);
            const sel = getSelection();
            sel.removeAllRanges(); sel.addRange(r);
        }"""
    )
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    expect(page.locator("#draft-quote")).to_have_text("最後の行です。")
    page.fill("#draft-text", "ここは残してよい")
    page.click("#draft-submit")
    wait_saved(page)
    c = storage.load_session(sid)["comments"][0]
    assert c["quote"] == "最後の行です。"


def test_state_persists_across_reload(diff_review):
    page, sid = diff_review
    page.locator("#btn-aidiff").click()
    expect(page.locator("#editor .ai-del")).to_have_count(2)
    page.reload()
    page.wait_for_selector("#editor p")
    expect(page.locator("#btn-aidiff")).to_have_class("on")
    expect(page.locator("#editor .ai-del")).to_have_count(2)


def test_no_changes_shows_toast(browser, web_port):
    """人間の校正をAIがそのまま反映しただけなら「変更なし」と知らせる。"""
    session = storage.create_session("こんにちは世界。\n二行目です。", "変更なしE2E", "/tmp/e2e")
    sid = session["id"]
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        page.wait_for_selector("#editor p")
        # 人間が第1稿を校正する（末尾に追記）
        select_in_editor(page, 0, 8, 8)
        page.keyboard.type("！")
        expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)
        # AIが校正どおりの第2稿を出す（prev_result と一致 → 差分なし）
        storage.add_revision(sid, "こんにちは世界。！\n二行目です。")
        expect(page.locator("#doc-meta")).to_contain_text("第2稿", timeout=10000)
        page.wait_for_selector("#editor p")
        page.locator("#btn-aidiff").click()
        expect(page.locator("#toast")).to_be_visible()
        expect(page.locator("#toast")).to_contain_text("変更はありません")
        expect(page.locator("#editor .ai-del")).to_have_count(0)
    finally:
        context.close()

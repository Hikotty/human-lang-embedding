"""トラックチェンジエディタの E2E テスト（Playwright / Chromium）。"""

from __future__ import annotations

from playwright.sync_api import expect

from hle_mcp import feedback, storage

from .conftest import BASE_TEXT, select_in_editor


def wait_saved(page):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)


def test_page_shows_document(review):
    page, sid = review
    expect(page.locator("#doc-title")).to_have_text("E2Eテスト")
    paras = page.locator("#editor p")
    expect(paras).to_have_count(2)
    expect(paras.nth(0)).to_contain_text("これはAIが書いた文章です。")
    expect(page.locator("#doc-meta")).to_contain_text("校正中")


def test_typing_creates_tracked_insert(review):
    page, sid = review
    select_in_editor(page, 0, 14, 14)  # 1段落目末尾にカーソル
    page.keyboard.type("追記")
    ins = page.locator("#editor ins")
    expect(ins).to_have_count(1)
    expect(ins).to_have_text("追記")
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert "【追加:追記】" in fb["annotated_text"]
    assert fb["result_text"].startswith("これはAIが書いた文章です。追記")
    assert fb["original_text"] == BASE_TEXT


def test_backspace_marks_char_deleted(review):
    page, sid = review
    select_in_editor(page, 0, 4, 4)  # 「これはA|Iが…」
    page.keyboard.press("Backspace")
    page.keyboard.press("Backspace")
    dels = page.locator("#editor del")
    expect(dels).to_have_count(2)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == BASE_TEXT  # 削除でも原文は保持される
    # 「A」「は」の2文字が削除扱いになる
    assert fb["result_text"].startswith("これIが")


def test_range_delete_wraps_selection(review):
    page, sid = review
    select_in_editor(page, 0, 3, 5)  # 「AI」を選択
    page.keyboard.press("Backspace")
    dels = page.locator("#editor del")
    expect(dels).to_have_count(1)
    expect(dels).to_have_text("AI")
    # 画面上はテキストが残る（赤斜線）
    expect(page.locator("#editor p").nth(0)).to_contain_text("AI")
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert {"type": "delete", "paragraph": 0, "text": "AI"} in fb["changes"]
    assert fb["result_text"] == "これはが書いた文章です。\n二段落目の文章です。"


def test_typing_over_selection_replaces(review):
    page, sid = review
    select_in_editor(page, 0, 3, 5)  # 「AI」を選択して置換入力
    page.keyboard.type("人間")
    expect(page.locator("#editor del")).to_have_text("AI")
    expect(page.locator("#editor ins")).to_have_text("人間")
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["result_text"].startswith("これは人間が書いた")
    assert fb["original_text"] == BASE_TEXT


def test_delete_own_insertion_is_removed(review):
    page, sid = review
    select_in_editor(page, 0, 13, 13)
    page.keyboard.type("XY")
    expect(page.locator("#editor ins")).to_have_text("XY")
    page.keyboard.press("Backspace")
    page.keyboard.press("Backspace")
    # 自分の追記を消した場合は痕跡が残らない
    expect(page.locator("#editor ins")).to_have_count(0)
    expect(page.locator("#editor del")).to_have_count(0)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["changes"] == []
    assert fb["result_text"] == BASE_TEXT


def test_enter_adds_newline_marker(review):
    page, sid = review
    select_in_editor(page, 0, 3, 3)
    page.keyboard.press("Enter")
    expect(page.locator("#editor p")).to_have_count(3)
    expect(page.locator("#editor ins.pb")).to_have_count(1)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == BASE_TEXT
    assert fb["result_text"] == "これは\nAIが書いた文章です。\n二段落目の文章です。"
    assert {"type": "newline_insert", "paragraph": 0, "text": ""} in fb["changes"]


def test_backspace_at_paragraph_start_marks_newline_delete(review):
    page, sid = review
    select_in_editor(page, 1, 0, 0)  # 2段落目の先頭
    page.keyboard.press("Backspace")
    # 段落は見た目上残り、改行削除マーカーが付く
    expect(page.locator("#editor p")).to_have_count(2)
    expect(page.locator("#editor del.pb")).to_have_count(1)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == BASE_TEXT
    assert fb["result_text"] == "これはAIが書いた文章です。二段落目の文章です。"


def test_undo_redo(review):
    page, sid = review
    select_in_editor(page, 0, 13, 13)
    page.keyboard.type("Z")
    expect(page.locator("#editor ins")).to_have_count(1)
    page.keyboard.press("Meta+z")
    expect(page.locator("#editor ins")).to_have_count(0)
    page.keyboard.press("Meta+Shift+z")
    expect(page.locator("#editor ins")).to_have_count(1)


def test_comment_flow_and_ai_reply_polling(review):
    page, sid = review
    select_in_editor(page, 0, 0, 3)  # 「これは」を選択
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    expect(page.locator("#draft-quote")).to_have_text("これは")
    page.fill("#draft-text", "書き出しが単調です")
    page.click("#draft-submit")

    # サイドバーとエディタ双方に反映される
    card = page.locator(".comment-card")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("書き出しが単調です")
    expect(page.locator("#editor .cmt")).to_have_text("これは")
    wait_saved(page)

    # サーバー側にも保存されている
    session = storage.load_session(sid)
    assert session["comments"][0]["text"] == "書き出しが単調です"
    cid = session["comments"][0]["id"]

    # AIの返信（MCP経由相当）がポーリングで画面に現れる
    storage.add_reply(sid, cid, "改善案を検討します", author="ai")
    expect(page.locator(".comment-card .reply")).to_contain_text(
        "改善案を検討します", timeout=8000)

    # 人間が返信への返信を送る（複数ラリー）
    page.fill(f'input[data-reply="{cid}"]', "お願いします")
    page.click(f'button[data-send="{cid}"]')
    expect(page.locator(".comment-card .reply")).to_have_count(2)
    session = storage.load_session(sid)
    assert [r["author"] for r in session["comments"][0]["replies"]] \
        == ["ai", "human"]


def test_comment_cancel_removes_highlight(review):
    page, sid = review
    select_in_editor(page, 0, 0, 3)
    page.click("#btn-comment")
    page.click("#draft-cancel")
    expect(page.locator("#editor .cmt")).to_have_count(0)
    assert storage.load_session(sid)["comments"] == []


def test_complete_button_sets_reviewed(review):
    page, sid = review
    page.click("#btn-complete")
    expect(page.locator("#doc-meta")).to_contain_text("校正完了")
    expect(page.locator("#btn-complete")).to_contain_text("校正完了済み")
    assert storage.load_session(sid)["status"] == "reviewed"


def test_new_revision_reloads_editor(review):
    page, sid = review
    storage.add_revision(sid, "AIによる第二稿です。")
    # ポーリング(2.5s)が検知してリロードされ、新しい本文が表示される
    expect(page.locator("#editor p").nth(0)).to_have_text(
        "AIによる第二稿です。", timeout=10000)
    expect(page.locator("#doc-meta")).to_contain_text("第2稿")


def test_paste_inserts_tracked_text(review):
    page, sid = review
    select_in_editor(page, 0, 14, 14)
    page.evaluate("() => navigator.clipboard.writeText('貼付テキスト')")
    page.keyboard.press("Meta+v")
    expect(page.locator("#editor ins")).to_have_text("貼付テキスト")
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert "【追加:貼付テキスト】" in fb["annotated_text"]


def test_list_view_shows_sessions(review, web_port, browser):
    page, sid = review
    lp = browser.new_page()
    lp.goto(f"http://127.0.0.1:{web_port}/")
    expect(lp.locator("#session-table a")).to_have_count(1, timeout=5000)
    expect(lp.locator("#session-table")).to_contain_text("E2Eテスト")
    lp.fill("#search-query", "存在しないもの")
    expect(lp.locator("#session-table")).to_contain_text(
        "セッションがありません", timeout=5000)
    lp.close()


def test_ime_composition_creates_tracked_insert(review):
    """日本語IME入力（変換確定）が追加扱いになり、地の文に混ざらない。"""
    page, sid = review
    select_in_editor(page, 0, 14, 14)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition",
             {"text": "にほんご", "selectionStart": 4, "selectionEnd": 4})
    cdp.send("Input.insertText", {"text": "日本語"})
    ins = page.locator("#editor ins")
    expect(ins).to_have_count(1)
    expect(ins).to_have_text("日本語")
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == BASE_TEXT
    assert "【追加:日本語】" in fb["annotated_text"]
    # ZWSPが本文に残っていない
    assert "\u200b" not in fb["result_text"]

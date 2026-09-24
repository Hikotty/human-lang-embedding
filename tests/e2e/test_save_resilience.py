"""校正内容を失わないことの E2E テスト（Playwright / Chromium）。

- 常駐デーモンの入れ替え中に入力し続けても、全文が保存される
- 保存が失敗しても自動で再試行し、入力し直さなくても保存される
- サーバーに届かないままタブを閉じても、次に開いたとき復元して保存する
- AIの新しい稿の後に届いた前の稿の編集は、新しい稿を上書きせず控えに残る
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import storage, webserver

from ..daemon_util import Daemon
from .conftest import BASE_TEXT, select_in_editor


def wait_saved(page, timeout=10000):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=timeout)


def result_text(sid: str) -> str:
    from hle_mcp import feedback
    return feedback.build_feedback(storage.load_session(sid))["result_text"]


# ---------- 常駐デーモンの入れ替え ----------
@pytest.fixture()
def daemon_page(browser, tmp_path):
    d = Daemon(tmp_path / "daemon", storage.data_dir())
    d.start()
    session = storage.create_session(BASE_TEXT, "入れ替えE2E", "/tmp/e2e")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{d.port}/session/{session['id']}")
    page.wait_for_selector("#editor p")
    yield page, session["id"], d
    context.close()
    d.stop()


def test_typing_through_daemon_rollover_loses_nothing(daemon_page):
    page, sid, d = daemon_page
    before = d.health()
    select_in_editor(page, 0, 14, 14)
    page.keyboard.type("入れ替え前")
    wait_saved(page)
    d.touch_code("feedback.py", "e2e")
    # 入れ替えが終わるまで打ち続ける（keep-alive 接続のままのブラウザで）
    typed = "入れ替え前"
    for i in range(40):
        ch = "あいうえおかきくけこ"[i % 10]
        page.keyboard.type(ch)
        typed += ch
        page.wait_for_timeout(120)
        h = d.health()
        if h and h["version"] != before["version"] and i > 10:
            break
    after = d.wait_for(lambda h: h and h["version"] != before["version"], 30, "入れ替え")
    page.keyboard.type("後")
    typed += "後"
    wait_saved(page)
    assert after["pid"] != before["pid"]
    assert result_text(sid) == BASE_TEXT.replace("です。\n", "です。" + typed + "\n", 1)
    # 開いているページには新しい版の案内が出る
    expect(page.locator("#update-banner")).to_be_visible(timeout=10000)
    # 案内から再読み込みしても内容はそのまま
    page.click("#btn-update-reload")
    page.wait_for_selector("#editor ins")
    expect(page.locator("#editor p").nth(0)).to_contain_text(typed)
    expect(page.locator("#update-banner")).to_be_hidden()


# ---------- 保存の再試行・ブラウザへの退避 ----------
def test_failed_saves_are_retried_automatically(review):
    page, sid = review
    page.route("**/api/sessions/*/document", lambda route: route.abort())
    select_in_editor(page, 0, 14, 14)
    page.keyboard.type("再試行")
    expect(page.locator("#save-state")).to_contain_text("再試行中", timeout=5000)
    expect(page.locator("#save-state")).to_have_class("warn")
    assert "再試行" not in result_text(sid)
    page.unroute("**/api/sessions/*/document")
    # 何も打たなくても、再試行で保存される
    wait_saved(page, timeout=20000)
    assert result_text(sid).startswith("これはAIが書いた文章です。再試行")


def test_complete_is_refused_while_unsaved(review):
    """保存できていないまま校正完了にすると、AIが古い内容を受け取るので止める。"""
    page, sid = review
    page.route("**/api/sessions/*/document", lambda route: route.abort())
    select_in_editor(page, 0, 14, 14)
    page.keyboard.type("未保存")
    # ダイアログは開いたままにすると click の完了待ちと噛み合わないので、
    # 受けたらすぐ閉じて文言だけ控える
    messages: list[str] = []
    page.on("dialog", lambda dlg: (messages.append(dlg.message), dlg.accept()))
    page.click("#btn-complete")
    for _ in range(100):
        if messages:
            break
        page.wait_for_timeout(100)
    assert messages and "保存できていない" in messages[0]
    expect(page.locator("#btn-complete")).to_have_text("✔ 校正完了")
    assert storage.load_session(sid)["status"] == "editing"


def test_unsaved_edits_survive_closing_the_tab_while_server_down(browser, web_port):
    """サーバーに届かないままタブを閉じても、次に開いたら復元して保存する。"""
    session = storage.create_session(BASE_TEXT, "退避E2E", "/tmp/e2e")
    sid = session["id"]
    url = f"http://127.0.0.1:{web_port}/session/{sid}"
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(url)
        page.wait_for_selector("#editor p")
        webserver.stop_server()  # サーバーが落ちた（入れ替え失敗・停止など）
        select_in_editor(page, 0, 14, 14)
        page.keyboard.type("退避")
        expect(page.locator("#save-state")).to_contain_text("ブラウザに退避済み",
                                                          timeout=5000)
        # beforeunload の送信も届かない。run_before_unload=True の close は
        # 閉じ終わりを待たないので、旧タブの再試行がサーバー復帰後に走らない
        # よう、閉じ終わるまで待つ
        with page.expect_event("close", timeout=10000):
            page.close(run_before_unload=True)
        assert page.is_closed()
        assert "退避" not in result_text(sid)

        webserver.start_server(web_port)  # 同じポートで復帰
        page = context.new_page()
        page.goto(url)
        page.wait_for_selector("#editor p")
        expect(page.locator("#toast")).to_contain_text("復元しました")
        expect(page.locator("#editor ins")).to_have_text("退避")
        wait_saved(page)
        assert result_text(sid).startswith("これはAIが書いた文章です。退避")
        # 保存できたので退避分は消えている
        assert page.evaluate(f"() => localStorage.getItem('hle-backup-{sid}')") is None
    finally:
        context.close()


# ---------- 稿の取り違え防止 ----------
def test_edit_arriving_after_new_revision_does_not_overwrite_it(review):
    """AIが次の稿を出した直後に届いた前の稿の編集は、新しい稿を壊さず控えに残す。"""
    page, sid = review
    select_in_editor(page, 0, 14, 14)
    page.keyboard.type("遅延")
    # 自動保存（700ms後）より先にAIが第2稿を出す
    storage.add_revision(sid, "AIによる第二稿です。")
    expect(page.locator("#doc-meta")).to_contain_text("第2稿", timeout=10000)
    page.wait_for_selector("#editor p")
    expect(page.locator("#toast")).to_contain_text("前の稿の控え", timeout=5000)
    s = storage.load_session(sid)
    assert s["revisions"][1]["edited_html"] is None       # 新しい稿は無傷
    assert "遅延" in (s["revisions"][0].get("late_edited_html") or "")
    expect(page.locator("#editor p").nth(0)).to_have_text("AIによる第二稿です。")


def test_backup_for_old_revision_is_deposited_after_reload(browser, web_port):
    """前の稿への未保存分がブラウザに残っていたら、読み込み時に控えとして預ける。"""
    session = storage.create_session(BASE_TEXT, "控えE2E", "/tmp/e2e")
    sid = session["id"]
    url = f"http://127.0.0.1:{web_port}/session/{sid}"
    context = browser.new_context()
    try:
        page = context.new_page()
        page.route("**/api/sessions/*/document", lambda route: route.abort())
        page.goto(url)
        page.wait_for_selector("#editor p")
        select_in_editor(page, 0, 14, 14)
        page.keyboard.type("控え")
        expect(page.locator("#save-state")).to_contain_text("再試行中", timeout=5000)
        page.close()
        storage.add_revision(sid, "AIによる第二稿です。")

        page = context.new_page()
        page.goto(url)
        page.wait_for_selector("#editor p")
        expect(page.locator("#toast")).to_contain_text("前の稿の控え", timeout=5000)
        s = storage.load_session(sid)
        assert "控え" in (s["revisions"][0].get("late_edited_html") or "")
        assert s["revisions"][1]["edited_html"] is None
        expect(page.locator("#editor p").nth(0)).to_have_text("AIによる第二稿です。")
        assert page.evaluate(f"() => localStorage.getItem('hle-backup-{sid}')") is None
    finally:
        context.close()

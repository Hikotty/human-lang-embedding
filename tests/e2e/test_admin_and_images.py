"""画像・管理画面・ボタン視認性の E2E テスト。"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import feedback, storage

from ..conftest import PNG_1PX


@pytest.fixture()
def image_review(browser, web_port, tmp_path):
    """画像入り本文のセッションを開いた state を返す。"""
    session = storage.create_session("画像の説明文です。", "画像E2E", "/tmp/e2e")
    name = storage.save_image_bytes(session["id"], "zu1.png", PNG_1PX)
    storage.update_base_text(
        session["id"], f"画像の説明文です。\n![構成図]({name})")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_selector("#editor .img-para")
    yield page, session["id"]
    context.close()


def wait_saved(page):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)


def test_image_rendered_in_editor(image_review):
    page, sid = image_review
    para = page.locator("#editor .img-para")
    expect(para).to_have_count(1)
    img = para.locator("img")
    expect(img).to_be_visible()
    # 画像が実際に読み込まれている（naturalWidth > 0）
    assert img.evaluate("el => el.naturalWidth") == 1


def test_image_tracked_delete_and_undo_toggle(image_review):
    page, sid = image_review
    para = page.locator("#editor .img-para")
    para.click()
    expect(para).to_have_class("img-para sel")
    page.keyboard.press("Backspace")
    # 削除予定マークが付くが画像自体は残る
    assert para.get_attribute("data-del") == "1"
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert {"type": "image_delete", "paragraph": 1, "text": "zu1.png"} \
        in fb["changes"]
    assert fb["result_text"] == "画像の説明文です。"
    assert "![構成図](zu1.png)" in fb["original_text"]

    # もう一度Backspaceで削除指定を取り消せる
    para.click()
    page.keyboard.press("Backspace")
    assert para.get_attribute("data-del") is None
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["changes"] == []


def test_human_inserts_image_via_button(image_review, tmp_path):
    page, sid = image_review
    # カーソルを本文段落に置いてからファイル選択で画像を追加する
    page.evaluate(
        """() => {
            const node = document.querySelector('#editor p').firstChild;
            const r = document.createRange();
            r.setStart(node, node.length); r.collapse(true);
            const sel = getSelection();
            sel.removeAllRanges(); sel.addRange(r);
        }""")
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(PNG_1PX)
    page.set_input_files("#image-input", str(shot))
    ins_para = page.locator('#editor .img-para[data-ins="1"]')
    expect(ins_para).to_have_count(1, timeout=5000)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert {"type": "image_insert", "paragraph": 1,
            "text": "screenshot.png"} in fb["changes"]
    assert "![](screenshot.png)" in fb["result_text"]
    assert "screenshot.png" not in fb["original_text"]


def test_complete_button_is_visible(review):
    """校正完了ボタンの文字色が背景色と異なる（不可視バグの回帰テスト）。"""
    page, _sid = review
    btn = page.locator("#btn-complete")
    styles = btn.evaluate(
        """el => {
            const s = getComputedStyle(el);
            return { color: s.color, bg: s.backgroundColor };
        }""")
    assert styles["color"] != styles["bg"]
    assert styles["color"] == "rgb(255, 255, 255)"
    assert styles["bg"] == "rgb(26, 115, 232)"


def test_admin_navigation_from_session(review, web_port):
    page, sid = review
    # セッション画面から管理画面へ遷移できる
    page.click('#editor-view a[href="/admin"]')
    page.wait_for_url("**/admin")
    expect(page.locator(".admin-card")).to_have_count(1, timeout=5000)
    expect(page.locator(".admin-card h3")).to_have_text("E2Eテスト")


def test_admin_shows_feedback_details(browser, web_port):
    s = storage.create_session("管理画面テスト本文。", "管理対象", "/ws/admin")
    storage.set_document(
        s["id"],
        '<p>管理画面<del>テスト</del><ins>検証</ins>'
        '<span class="cmt" data-cid="c1">本文</span>。</p>')
    storage.add_comment(s["id"], "本文", "表現を見直したい", comment_id="c1")
    storage.add_reply(s["id"], "c1", "了解です", author="ai")
    storage.add_revision(s["id"], "第二稿の本文。")  # 差分要約に圧縮される

    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    expect(page.locator(".admin-card")).to_have_count(1)
    page.click("button[data-toggle]")
    detail = page.locator(".admin-detail")
    expect(detail).to_be_visible()
    # 過去リビジョンの差分要約（del/insの装飾表示）
    expect(detail.locator("del")).to_have_text("テスト")
    expect(detail.locator("ins")).to_have_text("検証")
    # コメントスレッド
    expect(detail).to_contain_text("表現を見直したい")
    expect(detail).to_contain_text("了解です")
    # エディタへのリンク
    expect(page.locator(f'a[href="/session/{s["id"]}"]')).to_be_visible()
    context.close()


def test_admin_search_filters(browser, web_port):
    storage.create_session("x", "アルファ記事", "/ws/alpha")
    storage.create_session("y", "ベータ記事", "/ws/beta")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    expect(page.locator(".admin-card")).to_have_count(2)
    page.fill("#search-query", "アルファ")
    expect(page.locator(".admin-card")).to_have_count(1, timeout=5000)
    expect(page.locator(".admin-card h3")).to_have_text("アルファ記事")
    page.fill("#search-query", "")
    page.fill("#search-workspace", "beta")
    expect(page.locator(".admin-card h3")).to_have_text("ベータ記事",
                                                        timeout=5000)
    context.close()


# ---------- 画像へのコメント ----------

def add_image_comment(page, text: str) -> None:
    """画像を選んだ状態でコメントを追加する。"""
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    page.fill("#draft-text", text)
    page.click("#draft-submit")


def test_image_itself_can_be_commented(image_review):
    """画像をクリックで指定して、その画像自体にコメントを付けられる。"""
    page, sid = image_review
    para = page.locator("#editor .img-para")
    para.click()
    page.click("#btn-comment")
    # 下書きの対象表示が画像であることが分かる
    expect(page.locator("#draft-quote")).to_have_text("🖼 zu1.png")
    page.fill("#draft-text", "この図は配色を直してほしい")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    # サイドバーにサムネイルが出る
    expect(page.locator(".comment-card .quote.img-quote img")).to_be_visible()
    wait_saved(page)
    assert para.get_attribute("data-cids")

    fb = feedback.build_feedback(storage.load_session(sid))
    c = fb["comments"][0]
    assert c["target"] == "image"
    assert c["image"] == "zu1.png"
    assert c["text"] == "この図は配色を直してほしい"
    # コメントは本文を変えない。注釈では画像行全体が範囲になる
    assert fb["result_text"] == "画像の説明文です。\n![構成図](zu1.png)"
    assert fb["changes"] == []
    assert "〖C1〗![構成図](zu1.png)〖/C1〗" in fb["annotated_text"]


def test_commented_image_is_highlighted_and_linked_to_card(image_review):
    """コメント付きの画像はハイライトされ、クリックでカードが開く。"""
    page, _sid = image_review
    para = page.locator("#editor .img-para")
    para.click()
    add_image_comment(page, "図の指摘")
    expect(page.locator(".comment-card")).to_have_count(1)
    # いったん本文をクリックして選択を外す
    page.locator("#editor p:not(.img-para)").first.click()
    para.click()
    assert "cmt-active" in (para.get_attribute("class") or "")
    assert "active" in (
        page.locator(".comment-card").first.get_attribute("class") or "")


def test_image_comment_survives_reload(image_review):
    """画像コメントは編集HTMLに保存され、開き直しても残る。"""
    page, _sid = image_review
    page.locator("#editor .img-para").click()
    add_image_comment(page, "残るはずのコメント")
    expect(page.locator(".comment-card")).to_have_count(1)
    wait_saved(page)
    page.reload()
    page.wait_for_selector("#editor .img-para")
    expect(page.locator(".comment-card")).to_have_count(1)
    assert page.locator("#editor .img-para").get_attribute("data-cids")


def test_comment_is_dropped_when_inserted_image_is_removed(
        image_review, tmp_path):
    """人間が追加した画像を消すと、宛先を失うコメントも片付けられる。"""
    page, sid = image_review
    shot = tmp_path / "add.png"
    shot.write_bytes(PNG_1PX)
    page.set_input_files("#image-input", str(shot))
    ins = page.locator('#editor .img-para[data-ins="1"]')
    expect(ins).to_have_count(1, timeout=5000)
    ins.click()
    add_image_comment(page, "この画像は仮のものです")
    expect(page.locator(".comment-card")).to_have_count(1)
    wait_saved(page)

    ins.click()
    page.keyboard.press("Backspace")
    expect(ins).to_have_count(0)
    expect(page.locator(".comment-card")).to_have_count(0, timeout=5000)
    assert storage.load_session(sid)["comments"] == []


def test_deleting_image_comment_clears_highlight(image_review):
    """コメントを削除すると画像のハイライト（data-cids）も外れる。"""
    page, sid = image_review
    para = page.locator("#editor .img-para")
    para.click()
    add_image_comment(page, "あとで消すコメント")
    expect(page.locator(".comment-card")).to_have_count(1)
    wait_saved(page)
    page.on("dialog", lambda d: d.accept())
    page.locator('.comment-card button[data-del]').click()
    expect(page.locator(".comment-card")).to_have_count(0)
    wait_saved(page)
    assert para.get_attribute("data-cids") is None
    fb = feedback.build_feedback(storage.load_session(sid))
    assert "〖C1〗" not in fb["annotated_text"]

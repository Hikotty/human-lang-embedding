"""コメントの種類（指摘・質問）の E2E テスト。

指摘（issue）は修正稿に反映してほしいもの、質問（question）は答えて
ほしいだけのもの。AIは get_feedback の tag で扱いを分けるので、画面で
選んだ種類がそのまま保存され、フィードバックに出ることを確かめる。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import feedback, storage

from .conftest import select_in_editor

ISSUE_HINT = "AIはこの指摘を踏まえて本文を直します"
QUESTION_HINT = "AIは返信で答えるだけで、本文は直しません"


def wait_saved(page):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)


def open_draft(page, para: int = 0) -> None:
    select_in_editor(page, para, 0, 3)
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()


def submit_draft(page, text: str, count: int) -> None:
    page.fill("#draft-text", text)
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(count)


def stored(sid: str) -> list[dict]:
    return storage.load_session(sid)["comments"]


def feedback_tags(sid: str) -> list[str]:
    fb = feedback.build_feedback(storage.load_session(sid))
    return [c["tag"] for c in fb["comments"]]


def test_draft_defaults_to_issue(review):
    """何も選ばなければ指摘になり、保存データの形は従来のまま。"""
    page, sid = review
    open_draft(page)
    expect(page.locator('#draft-tag input[value="issue"]')).to_be_checked()
    expect(page.locator("#draft-tag .tag-hint")).to_have_text(ISSUE_HINT)
    submit_draft(page, "書き出しが単調です", 1)
    expect(page.locator(".comment-card .tag-badge")).to_have_text("指摘")
    assert "tag" not in stored(sid)[0]
    assert feedback_tags(sid) == ["issue"]


def test_draft_can_mark_question_and_resets_next_time(review):
    page, sid = review
    open_draft(page)
    page.click("#draft-tag label.question")
    expect(page.locator('#draft-tag input[value="question"]')).to_be_checked()
    # 選んだ種類をAIがどう扱うかが下書き欄に出る
    expect(page.locator("#draft-tag .tag-hint")).to_have_text(QUESTION_HINT)
    submit_draft(page, "この言い回しはどういう意図ですか？", 1)
    expect(page.locator(".comment-card .tag-badge.question")).to_have_text("質問")
    assert stored(sid)[0]["tag"] == "question"
    assert feedback_tags(sid) == ["question"]

    # 次の下書きは前の選択を持ち越さず、指摘から始まる
    open_draft(page, para=1)
    expect(page.locator('#draft-tag input[value="issue"]')).to_be_checked()
    expect(page.locator("#draft-tag .tag-hint")).to_have_text(ISSUE_HINT)
    submit_draft(page, "ここは直して", 2)
    wait_saved(page)
    assert feedback_tags(sid) == ["question", "issue"]


def test_edit_form_switches_tag_both_ways(review):
    """編集フォームで種類を変えられ、指摘に戻すと tag キーも消える。"""
    page, sid = review
    # 2.5秒ごとのポーリングがサーバーの内容で描き直すと、保存直後の画面の
    # 更新漏れが隠れる。セッションの再取得だけを止めて、手元の更新を確かめる
    page.route(f"**/api/sessions/{sid}", lambda route: route.abort())
    open_draft(page)
    submit_draft(page, "元のコメント", 1)
    cid = stored(sid)[0]["id"]

    page.click(f'button[data-edit="{cid}"]')
    expect(page.locator('input[name="edit-tag"][value="issue"]')).to_be_checked()
    # 編集中は選択欄が種類を示すので、保存済みのバッジは隠れる
    expect(page.locator(".comment-card .tag-badge")).to_have_count(0)
    page.click(".comment-card .tag-select label.question")
    expect(page.locator(".comment-card .tag-hint")).to_have_text(QUESTION_HINT)
    # 種類の選択を押しても、カードの「本文へ飛ぶ」は動かない
    expect(page.locator(".comment-card")).not_to_have_class("comment-card active")
    page.click(f'button[data-edit-save="{cid}"]')
    expect(page.locator(".comment-card .tag-badge.question")).to_have_text("質問")
    assert stored(sid)[0]["tag"] == "question"

    page.click(f'button[data-edit="{cid}"]')
    expect(page.locator('input[name="edit-tag"][value="question"]')).to_be_checked()
    page.click(".comment-card .tag-select label.issue")
    page.click(f'button[data-edit-save="{cid}"]')
    # 画面上もすぐ指摘に戻る（サーバーが消した tag を古い値で残さない）
    expect(page.locator(".comment-card .tag-badge.issue")).to_have_text("指摘")
    assert "tag" not in stored(sid)[0]
    assert feedback_tags(sid) == ["issue"]


def test_reply_edit_form_has_no_tag_select(review):
    """種類はコメント本体にだけあり、返信の編集フォームには出ない。"""
    page, sid = review
    open_draft(page)
    submit_draft(page, "指摘", 1)
    cid = stored(sid)[0]["id"]
    storage.add_reply(sid, cid, "AIの回答", author="ai")
    expect(page.locator(".comment-card .reply")).to_contain_text(
        "AIの回答", timeout=8000)
    rid = stored(sid)[0]["replies"][0]["id"]
    page.click(f'button[data-edit="{cid}|{rid}"]')
    expect(page.locator("textarea[data-edit-input]")).to_have_value("AIの回答")
    expect(page.locator(".comment-card .tag-select")).to_have_count(0)
    # 本体のバッジはそのまま見えている
    expect(page.locator(".comment-card .tag-badge")).to_have_text("指摘")


def test_old_revision_note_names_the_kind(review):
    """前の稿へのコメントの注記は、種類に合わせて「〜への質問」になる。"""
    page, sid = review
    open_draft(page)
    page.click("#draft-tag label.question")
    submit_draft(page, "これは何の略？", 1)
    wait_saved(page)
    storage.add_revision(sid, "AIによる第二稿です。")
    expect(page.locator("#doc-meta")).to_contain_text("第2稿", timeout=10000)
    expect(page.locator(".comment-card .rev-note")).to_have_text("第1稿への質問")
    expect(page.locator(".comment-card .tag-badge")).to_have_text("質問")


def test_admin_shows_tag_badges(browser, web_port):
    s = storage.create_session("本文。", "管理画面の種類", "/ws/tags")
    storage.add_comment(s["id"], "本文", "直して", comment_id="c1")
    storage.add_comment(s["id"], "本文", "これは何？", comment_id="c2",
                        tag="question")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    page.click("button[data-toggle]")
    comments = page.locator(".admin-detail .admin-comment")
    expect(comments).to_have_count(2)
    expect(comments.nth(0).locator(".tag-badge.issue")).to_have_text("指摘")
    expect(comments.nth(1).locator(".tag-badge.question")).to_have_text("質問")
    context.close()


# ---------- HTMLレビューページ ----------
HTML_DOC = ("<!doctype html><html><body>"
            "<p id=\"lead\">これは重要な説明文です。</p></body></html>")


@pytest.fixture()
def html_page(browser, web_port):
    session = storage.create_session(
        HTML_DOC, "HTML種類E2E", "/tmp/e2e", mode="html",
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


def select_lead(page) -> None:
    page.evaluate(
        """() => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const node = doc.querySelector('#lead').firstChild;
            const r = doc.createRange();
            r.setStart(node, 3);
            r.setEnd(node, 5);
            const s = doc.getSelection();
            s.removeAllRanges();
            s.addRange(r);
            doc.dispatchEvent(new Event('selectionchange'));
        }"""
    )


def test_html_review_question_tag_flow(html_page):
    """HTMLレビューでも質問を付けられ、編集で指摘に戻せる。"""
    page, sid = html_page
    # ポーリングでの描き直しを止め、保存直後の手元の更新を確かめる（md側と同じ）
    page.route(f"**/api/sessions/{sid}", lambda route: route.abort())
    select_lead(page)
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    expect(page.locator('#draft-tag input[value="issue"]')).to_be_checked()
    page.click("#draft-tag label.question")
    expect(page.locator("#draft-tag .tag-hint")).to_have_text(QUESTION_HINT)
    submit_draft(page, "「重要」とは何と比べて？", 1)
    expect(page.locator(".comment-card .tag-badge.question")).to_have_text("質問")
    c = stored(sid)[0]
    assert c["tag"] == "question"
    assert c["quote"] == "重要"
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["mode"] == "html" and fb["comments"][0]["tag"] == "question"

    page.click(f'button[data-edit="{c["id"]}"]')
    expect(page.locator('input[name="edit-tag"][value="question"]')).to_be_checked()
    page.click(".comment-card .tag-select label.issue")
    page.click(f'button[data-edit-save="{c["id"]}"]')
    expect(page.locator(".comment-card .tag-badge.issue")).to_have_text("指摘")
    assert "tag" not in stored(sid)[0]

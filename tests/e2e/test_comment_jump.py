"""コメントカードのクリックで本文の該当箇所へ飛ぶ機能の E2E テスト。

アンカー（.cmt span / 画像段落の data-cids）はその稿の編集HTMLにしか無く、
AIが次の稿を出すと消える。アンカーがあればそこへ、無ければ引用文を今の
本文から探して飛ぶことを、実ブラウザのスクロール位置で検証する。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import storage

from ..conftest import PNG_1PX
from .conftest import select_in_editor

TARGET = "この文はあとで書き換えられる予定の長い文章です。"
# 60段落の長文。TARGET は55段落目に置き、初期表示では画面外にする
LONG_TEXT = "\n".join(
    [f"{i + 1}番目の段落です。校正対象の本文が続きます。" for i in range(54)]
    + [TARGET]
    + [f"{i + 56}番目の段落です。校正対象の本文が続きます。" for i in range(5)]
)


@pytest.fixture()
def long_review(browser, web_port):
    session = storage.create_session(LONG_TEXT, "ジャンプE2E", "/tmp/e2e")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_selector("#editor p")
    yield page, session["id"]
    context.close()


def wait_saved(page):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)


def add_comment_on_para(page, para_index: int, start: int, end: int, text: str):
    select_in_editor(page, para_index, start, end)
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    page.fill("#draft-text", text)
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    wait_saved(page)


def scroll_wrap_to_top(page):
    page.evaluate("() => { document.getElementById('editor-wrap').scrollTop = 0; }")
    assert page.evaluate("() => document.getElementById('editor-wrap').scrollTop") == 0


def wait_visible_in_wrap(page, selector: str):
    """selector の要素が #editor-wrap の表示領域に入るまで待つ（smooth scroll対応）。"""
    page.wait_for_function(
        """(sel) => {
            const el = document.querySelector(sel);
            const wrap = document.getElementById('editor-wrap');
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const w = wrap.getBoundingClientRect();
            return r.top >= w.top && r.bottom <= w.bottom;
        }""",
        arg=selector,
    )


JUMP_INFO = """() => {
  const h = CSS.highlights.get('hle-jump');
  if (!h || !h.size) return null;
  const range = [...h][0];
  const r = range.getBoundingClientRect();
  const w = document.getElementById('editor-wrap').getBoundingClientRect();
  return { text: range.toString(), visible: r.top >= w.top && r.bottom <= w.bottom };
}"""
JUMP_VISIBLE = "() => { const f = " + JUMP_INFO + "; const i = f(); return !!(i && i.visible); }"


def wait_for_reload_to_revision(page, rev_no: int):
    expect(page.locator("#doc-meta")).to_contain_text(f"第{rev_no}稿", timeout=10000)
    page.wait_for_selector("#editor p")


def test_card_click_scrolls_to_anchor(long_review):
    """同じ稿ならアンカー（.cmt）へスクロールする。"""
    page, sid = long_review
    # 55段落目は初期表示では画面外
    paras = page.locator("#editor p")
    expect(paras).to_have_count(60)
    add_comment_on_para(page, 54, 0, 6, "ここを直す")
    expect(page.locator("#editor .cmt")).to_have_text("この文はあと")
    scroll_wrap_to_top(page)
    page.click(".comment-card .quote")
    wait_visible_in_wrap(page, "#editor .cmt")
    expect(page.locator("#editor .cmt")).to_have_class("cmt active")
    # 同じ稿への指摘なので稿の注記は付かない
    expect(page.locator(".comment-card .rev-note")).to_have_count(0)


def test_card_click_finds_quote_in_new_revision(long_review):
    """新しい稿でアンカーが消えても、引用文を探して飛び、強調する。"""
    page, sid = long_review
    add_comment_on_para(page, 54, 0, len(TARGET), "ここを直す")
    # AIが第2稿を出す（文はそのまま残っている）。ポーリングでリロードされる
    storage.add_revision(sid, LONG_TEXT.replace("1番目", "一番目", 1))
    wait_for_reload_to_revision(page, 2)
    expect(page.locator("#editor .cmt")).to_have_count(0)  # アンカーは無い
    expect(page.locator(".comment-card .rev-note")).to_have_text("第1稿への指摘")
    assert page.evaluate("() => document.getElementById('editor-wrap').scrollTop") == 0
    page.click(".comment-card .quote")
    page.wait_for_function(JUMP_VISIBLE)
    info = page.evaluate(JUMP_INFO)
    assert info["text"] == TARGET
    expect(page.locator("#toast")).to_be_hidden()  # 書き換わっていないので通知なし
    # 本文をクリックすると強調は消える（DOMには何も残っていない）
    page.locator("#editor p").nth(54).click()
    assert page.evaluate("() => CSS.highlights.has('hle-jump')") is False
    assert page.evaluate("() => document.querySelectorAll('#editor .cmt').length") == 0


REWRITTEN = "この文はあとで書き換えられる予定でした。"

# 強調した範囲が、本文の何番目の段落（0起点）にあるか
JUMP_PARA_INDEX = """() => {
  const h = CSS.highlights.get('hle-jump');
  if (!h || !h.size) return -1;
  const sc = [...h][0].startContainer;
  const p = (sc.nodeType === 1 ? sc : sc.parentElement).closest('#editor p');
  return [...document.querySelectorAll('#editor p')].indexOf(p);
}"""


def test_card_click_lands_on_rewritten_sentence(long_review):
    """文が書き換わっても、コメントを付けた場所（書き換わった文）に飛ぶ。"""
    page, sid = long_review
    add_comment_on_para(page, 54, 0, len(TARGET) - 1, "ここを直す")  # 句点を除く23文字
    storage.add_revision(sid, LONG_TEXT.replace(TARGET, REWRITTEN))
    wait_for_reload_to_revision(page, 2)
    page.click(".comment-card .quote")
    page.wait_for_function(JUMP_VISIBLE)
    info = page.evaluate(JUMP_INFO)
    # 書き換わった後の文のうち、コメントの範囲に当たる部分（句点は範囲の外）
    assert info["text"] == "この文はあとで書き換えられる予定でした"
    assert page.evaluate(JUMP_PARA_INDEX) == 54
    # 書き換わっていることは、飛んだ直後に知らせる
    expect(page.locator("#toast")).to_contain_text("書き換わっています")
    expect(page.locator("#toast")).to_contain_text("第1稿")


def test_card_click_lands_on_replaced_sentence(long_review):
    """文がまったく別の文に置き換わっていても、その置き換わった場所に飛ぶ。"""
    page, sid = long_review
    add_comment_on_para(page, 54, 0, len(TARGET), "ここを直す")
    storage.add_revision(sid, LONG_TEXT.replace(TARGET, "まったく別の文になりました。"))
    wait_for_reload_to_revision(page, 2)
    page.click(".comment-card .quote")
    page.wait_for_function(JUMP_VISIBLE)
    assert page.evaluate(JUMP_INFO)["text"] == "まったく別の文になりました。"
    assert page.evaluate(JUMP_PARA_INDEX) == 54
    expect(page.locator("#toast")).to_contain_text("書き換わっています")


def test_card_click_follows_line_shift(long_review):
    """前に段落が増えて行がずれても、書き換わった文のある場所に飛ぶ。"""
    page, sid = long_review
    add_comment_on_para(page, 54, 0, len(TARGET), "ここを直す")
    added = "\n".join(f"第2稿で足した{i}段落目です。" for i in range(5))
    storage.add_revision(sid, added + "\n" + LONG_TEXT.replace(TARGET, REWRITTEN))
    wait_for_reload_to_revision(page, 2)
    page.click(".comment-card .quote")
    page.wait_for_function(JUMP_VISIBLE)
    assert page.evaluate(JUMP_INFO)["text"] == REWRITTEN
    assert page.evaluate(JUMP_PARA_INDEX) == 59  # 54 + 足した5段落


def test_card_click_picks_the_right_one_among_duplicates(browser, web_port):
    """同じ文が複数あっても、コメントを付けた方に飛ぶ（先頭に当たった方ではなく）。"""
    lines = LONG_TEXT.split("\n")
    lines[0] = TARGET  # 先頭にも同じ文がある（コメントを付けるのは55番目の方）
    session = storage.create_session("\n".join(lines), "重複ジャンプE2E", "/tmp/e2e")
    sid = session["id"]
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        page.wait_for_selector("#editor p")
        add_comment_on_para(page, 54, 0, len(TARGET), "こっちの文")
        revised = list(lines)
        revised[10] = "第2稿で直した段落です。"  # コメントの文はそのまま残る
        storage.add_revision(sid, "\n".join(revised))
        wait_for_reload_to_revision(page, 2)
        page.click(".comment-card .quote")
        page.wait_for_function(JUMP_VISIBLE)
        assert page.evaluate(JUMP_INFO)["text"] == TARGET
        assert page.evaluate(JUMP_PARA_INDEX) == 54
        expect(page.locator("#toast")).to_be_hidden()  # 書き換わっていない
    finally:
        context.close()


def test_card_click_follows_comment_across_two_revisions(long_review):
    """第1稿のコメントは、第2稿・第3稿と書き換わっても、順にたどって飛ぶ。"""
    page, sid = long_review
    add_comment_on_para(page, 54, 0, len(TARGET), "ここを直す")
    storage.add_revision(sid, LONG_TEXT.replace(TARGET, REWRITTEN))
    wait_for_reload_to_revision(page, 2)
    added = "\n".join(f"第3稿で足した{i}段落目です。" for i in range(3))
    third = added + "\n" + LONG_TEXT.replace(TARGET, "三度目の書き換えでした。")
    storage.add_revision(sid, third)
    wait_for_reload_to_revision(page, 3)
    expect(page.locator(".comment-card .rev-note")).to_have_text("第1稿への指摘")
    page.click(".comment-card .quote")
    page.wait_for_function(JUMP_VISIBLE)
    assert page.evaluate(JUMP_PARA_INDEX) == 57  # 54 + 足した3段落
    assert "三度目の書き換えでした" in page.evaluate(JUMP_INFO)["text"]


def _edited_html(lines: list[str], target_index: int, inner: str) -> str:
    """target_index の段落だけコメント範囲（c1）付きの編集HTMLにして返す。"""
    paras = []
    for i, line in enumerate(lines):
        if i == target_index:
            paras.append(f'<p><span class="cmt" data-cid="c1">{inner}</span></p>')
        else:
            paras.append(f"<p>{line}</p>")
    return "".join(paras)


def _session_with_human_edit(web_port, browser, strip_spans: bool):
    """人間が対象の一部を消した第1稿に、AIが書き換えた第2稿を出したセッション。"""
    session = storage.create_session(LONG_TEXT, "校正後ジャンプE2E", "/tmp/e2e")
    sid = session["id"]
    storage.add_comment(sid, quote=TARGET, text="ここを直す", comment_id="c1")
    lines = LONG_TEXT.split("\n")
    # 人間が「予定の」を消した。コメントの範囲は消した文字を含む
    html = _edited_html(lines, 54, "この文はあとで書き換えられる<del>予定の</del>長い文章です。")
    storage.set_document(sid, html)
    revised = lines[:]
    revised[54] = "この文はあとで書き換えられる、長い文章でした。"
    storage.add_revision(sid, "\n".join(["先頭に足した段落です。"] + revised))
    if strip_spans:
        # 場所の保存が無かった頃のデータ（引用文で探すしかない）
        s = storage.load_session(sid)
        del s["revisions"][0]["feedback_summary"]["comment_spans"]
        storage.save_session(s)
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
    wait_for_reload_to_revision(page, 2)
    return page, context


@pytest.mark.parametrize("strip_spans", [False, True])
def test_card_click_after_human_edit_in_old_revision(browser, web_port, strip_spans):
    """人間の校正（削除）が入った稿のコメントも、場所を保存していれば正しく飛ぶ。
    場所を保存していない古いデータでも、引用文から探して飛べる。"""
    page, context = _session_with_human_edit(web_port, browser, strip_spans)
    try:
        page.click(".comment-card .quote")
        page.wait_for_function(JUMP_VISIBLE)
        assert page.evaluate(JUMP_PARA_INDEX) == 55  # 先頭に足した1段落分ずれる
        assert page.evaluate(JUMP_INFO)["text"].startswith("この文はあとで書き換えられる")
    finally:
        context.close()


def test_card_click_without_any_clue_says_not_found(long_review):
    """本文が全面的に書き換わって手がかりが無ければ、見つからない旨を通知する。"""
    page, sid = long_review
    add_comment_on_para(page, 54, 0, len(TARGET), "ここを直す")
    # 行数も違うので、行の対応から場所を割り出すこともできない
    storage.add_revision(sid, "\n".join(f"まるごと別の{i}行目。" for i in range(30)))
    wait_for_reload_to_revision(page, 2)
    page.click(".comment-card .quote")
    expect(page.locator("#toast")).to_contain_text("見つかりません")
    assert page.evaluate("() => CSS.highlights.has('hle-jump')") is False


def test_image_comment_click_finds_image_in_new_revision(browser, web_port):
    """画像コメントは、新しい稿でも同じ画像があればそこへ飛ぶ。"""
    session = storage.create_session("先頭の文です。", "画像ジャンプE2E", "/tmp/e2e")
    sid = session["id"]
    name = storage.save_image_bytes(sid, "zu1.png", PNG_1PX)
    filler = "\n".join(f"{i + 1}番目の段落です。" for i in range(50))
    storage.update_base_text(sid, f"先頭の文です。\n{filler}\n![構成図]({name})")
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        page.wait_for_selector("#editor .img-para")
        page.locator("#editor .img-para").click()
        page.click("#btn-comment")
        page.fill("#draft-text", "図を差し替える")
        page.click("#draft-submit")
        expect(page.locator(".comment-card")).to_have_count(1)
        wait_saved(page)
        storage.add_revision(sid, f"先頭の文です（第2稿）。\n{filler}\n![構成図]({name})")
        wait_for_reload_to_revision(page, 2)
        para = page.locator("#editor .img-para")
        assert para.get_attribute("data-cids") is None  # アンカーは無い
        page.click(".comment-card .quote")
        wait_visible_in_wrap(page, "#editor .img-para")
        expect(para).to_have_class("img-para cmt-active")
    finally:
        context.close()


# ---------- HTMLモード ----------
def _long_html(target_sentence: str) -> str:
    body = "".join(f"<p id='p{i}'>{i + 1}番目の段落です。</p>" for i in range(80))
    return ("<!doctype html><html><body>" + body +
            f"<p id='target'>{target_sentence}</p></body></html>")


@pytest.fixture()
def long_html_review(browser, web_port):
    session = storage.create_session(
        _long_html(TARGET), "HTMLジャンプE2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/page.html")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    wait_frame_ready(page)
    yield page, session["id"]
    context.close()


def wait_frame_ready(page):
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('#target'));
        }"""
    )


def select_target_in_frame(page, end: int):
    page.evaluate(
        """(e) => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const node = doc.querySelector('#target').firstChild;
            const r = doc.createRange();
            r.setStart(node, 0); r.setEnd(node, e);
            const sel = doc.getSelection();
            sel.removeAllRanges(); sel.addRange(r);
            doc.dispatchEvent(new Event('selectionchange'));
        }""",
        end,
    )


FRAME_TARGET_VISIBLE = """() => {
  const f = document.getElementById('doc-frame');
  const el = f.contentDocument.querySelector('#target');
  const r = el.getBoundingClientRect();
  return r.top >= 0 && r.bottom <= f.contentWindow.innerHeight;
}"""

FRAME_JUMP_INFO = """() => {
  const win = document.getElementById('doc-frame').contentWindow;
  const h = win.CSS.highlights.get('hle-jump');
  if (!h || !h.size) return null;
  const range = [...h][0];
  const r = range.getBoundingClientRect();
  return { text: range.toString(),
           visible: r.top >= 0 && r.bottom <= win.innerHeight };
}"""
FRAME_JUMP_VISIBLE = ("() => { const f = " + FRAME_JUMP_INFO +
                      "; const i = f(); return !!(i && i.visible); }")


def test_html_card_click_scrolls_frame_to_comment(long_html_review):
    page, sid = long_html_review
    assert page.evaluate(FRAME_TARGET_VISIBLE) is False  # 初期表示では画面外
    select_target_in_frame(page, len(TARGET))
    page.click("#btn-comment")
    page.fill("#draft-text", "ここを直す")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    page.evaluate(
        "() => document.getElementById('doc-frame').contentWindow.scrollTo(0, 0)")
    page.click(".comment-card .quote")
    page.wait_for_function(FRAME_TARGET_VISIBLE)


def test_html_card_click_loose_match_in_new_revision(long_html_review):
    """新しい稿で文が書き換わっていても、引用文の一部で近傍へ飛んで強調する。"""
    page, sid = long_html_review
    select_target_in_frame(page, len(TARGET))
    page.click("#btn-comment")
    page.fill("#draft-text", "ここを直す")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    storage.add_revision(sid, _long_html("この文はあとで書き換えられる予定でした。"),
                         source_path="/tmp/e2e/page.html")
    expect(page.locator("#doc-meta")).to_contain_text("第2稿", timeout=10000)
    wait_frame_ready(page)
    expect(page.locator(".comment-card .rev-note")).to_have_text("第1稿への指摘")
    # 完全一致のアンカーは解決できていない
    assert page.evaluate(
        """() => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get('hle-comment');
            return h ? h.size : 0;
        }""") == 0
    page.click(".comment-card .quote")
    page.wait_for_function(FRAME_JUMP_VISIBLE)
    info = page.evaluate(FRAME_JUMP_INFO)
    assert info["text"] == "この文はあとで書き換えら"  # 先頭12文字で一致


def _html_with_tail(sentence: str) -> str:
    """対象の文の前後に段落が続くHTML（直前・直後の文脈が両方ある）。"""
    head = "".join(f"<p>{i + 1}番目の段落です。</p>" for i in range(80))
    tail = "".join(f"<p>{i + 1}番目の後ろの段落です。</p>" for i in range(30))
    return ("<!doctype html><html><body>" + head +
            f"<p id='target'>{sentence}</p>" + tail + "</body></html>")


def _comment_on_html_target(page):
    select_target_in_frame(page, len(TARGET))
    page.click("#btn-comment")
    page.fill("#draft-text", "ここを直す")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)


def test_html_card_click_lands_between_surviving_neighbors(browser, web_port):
    """引用文がまるごと書き換わっても、前後の文が残っていればその間に飛ぶ。"""
    session = storage.create_session(
        _html_with_tail(TARGET), "HTML前後文脈E2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/page.html")
    sid = session["id"]
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        wait_frame_ready(page)
        _comment_on_html_target(page)
        storage.add_revision(sid, _html_with_tail("まったく別の文になりました。"),
                             source_path="/tmp/e2e/page.html")
        expect(page.locator("#doc-meta")).to_contain_text("第2稿", timeout=10000)
        wait_frame_ready(page)
        page.click(".comment-card .quote")
        page.wait_for_function(FRAME_JUMP_VISIBLE)
        # 直前の段落の末尾と直後の段落の先頭の間 = 書き換わった文
        assert page.evaluate(FRAME_JUMP_INFO)["text"] == "まったく別の文になりました。"
        expect(page.locator("#toast")).to_contain_text("書き換わっています")
    finally:
        context.close()


def test_html_card_click_lands_after_prefix_when_no_suffix(long_html_review):
    """直後の文脈が無い（末尾の文）ときは、直前の文脈の直後に飛ぶ。"""
    page, sid = long_html_review
    _comment_on_html_target(page)
    storage.add_revision(sid, _long_html("まったく別の文になりました。"),
                         source_path="/tmp/e2e/page.html")
    expect(page.locator("#doc-meta")).to_contain_text("第2稿", timeout=10000)
    wait_frame_ready(page)
    page.click(".comment-card .quote")
    page.wait_for_function(FRAME_JUMP_VISIBLE)
    assert page.evaluate(FRAME_JUMP_INFO)["text"] == "まったく別の文になりました。"

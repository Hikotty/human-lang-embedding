"""マークダウン装飾の E2E テスト（Playwright / Chromium）。

記法の記号はDOMテキストとして保持したまま装飾するため、装飾しても編集HTML
から原文へ無損失で戻せる。この不変条件（AIが受け取る result_text から記法が
消えないこと）を実ブラウザで検証する。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import feedback, storage

MD_TEXT = "\n".join(
    [
        "# 見出し1",
        "## 見出し2",
        "本文に**太字**と*斜体*と`コード`と~~取消~~があります。",
        "[リンク](https://example.com/a)も含みます。",
        "- 箇条書き1",
        "  - ネストした箇条書き",
        "1. 番号付き",
        "> 引用文",
        "",
        "```python",
        "def f(**kwargs):",
        "    return 1  # *ここは装飾しない*",
        "```",
        "",
        "| A | B |",
        "|---|---|",
        "| 1 | 2 |",
        "",
        "---",
        "通常の段落です。",
    ]
)


def open_editor(browser, web_port, text: str, title: str):
    """指定テキストのセッションを作り、そのエディタページを開く。"""
    session = storage.create_session(text, title, "/tmp/md")
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_selector("#editor p")
    return context, page, session["id"]


def save_current_html(page, sid: str) -> None:
    """編集を挟まずに、現在のエディタHTMLをサーバーへ保存する。"""
    ok = page.evaluate(
        """async (sid) => {
            const res = await fetch('/api/sessions/' + sid + '/document', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ html: Tracker.html() })
            });
            return res.ok;
        }""",
        sid,
    )
    assert ok, "編集HTMLの保存に失敗した"


@pytest.fixture()
def md_page(browser, web_port):
    context, page, sid = open_editor(browser, web_port, MD_TEXT, "マークダウンE2E")
    yield page, sid
    context.close()


def test_markdown_roundtrip_is_lossless(md_page):
    """装飾を当てても original_text / result_text が原文と完全一致する。"""
    page, sid = md_page
    save_current_html(page, sid)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == MD_TEXT
    assert fb["result_text"] == MD_TEXT


def test_block_syntax_gets_decoration_classes(md_page):
    page, _ = md_page
    expect(page.locator("#editor p")).to_have_count(len(MD_TEXT.split("\n")))
    expect(page.locator("#editor p.md-h1")).to_have_count(1)
    expect(page.locator("#editor p.md-h2")).to_have_count(1)
    expect(page.locator("#editor p.md-li")).to_have_count(3)
    expect(page.locator("#editor p.md-ol")).to_have_count(1)
    expect(page.locator("#editor p.md-in1")).to_have_count(1)
    expect(page.locator("#editor p.md-quote")).to_have_count(1)
    expect(page.locator("#editor p.md-fence")).to_have_count(2)
    expect(page.locator("#editor p.md-code")).to_have_count(2)
    expect(page.locator("#editor p.md-tr")).to_have_count(3)
    expect(page.locator("#editor p.md-hr")).to_have_count(1)


def test_inline_syntax_gets_decoration_classes(md_page):
    """記号は .md-mk として残るので、装飾範囲のテキストは記法込みになる。"""
    page, _ = md_page
    expect(page.locator("#editor .md-b")).to_have_text("**太字**")
    expect(page.locator("#editor .md-i")).to_have_text("*斜体*")
    expect(page.locator("#editor .md-code-in")).to_have_text("`コード`")
    expect(page.locator("#editor .md-s")).to_have_text("~~取消~~")
    expect(page.locator("#editor .md-link")).to_have_text(
        "[リンク](https://example.com/a)"
    )


def test_fenced_code_is_not_decorated(md_page):
    """コードブロック内はインライン記法を解釈しない。"""
    page, _ = md_page
    code = page.locator("#editor p.md-code").first
    expect(code).to_have_text("def f(**kwargs):")
    assert code.locator(".md-b").count() == 0
    assert page.locator("#editor p.md-code").nth(1).locator(".md-i").count() == 0


def test_editing_decorated_line_is_tracked(md_page):
    """装飾された段落の本文を編集しても、追加が追跡され原文は保持される。"""
    page, sid = md_page
    page.evaluate(
        """() => {
            const p = document.querySelector('#editor p.md-h1');
            const node = p.lastChild;  // 「見出し1」のテキストノード
            const r = document.createRange();
            r.setStart(node, node.length);
            r.collapse(true);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            document.getElementById('editor').focus();
        }"""
    )
    page.keyboard.type("A")
    expect(page.locator("#editor p.md-h1 ins")).to_have_text("A")
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == MD_TEXT
    assert fb["result_text"].split("\n")[0] == "# 見出し1A"
    assert "【追加:A】" in fb["annotated_text"]


def test_markdown_symbol_itself_can_be_corrected(md_page):
    """記号はテキストなので、記法そのもの（見出しレベル）も校正できる。"""
    page, sid = md_page
    page.evaluate(
        """() => {
            const mk = document.querySelector('#editor p.md-h2 .md-mk');
            const r = document.createRange();
            r.setStart(mk.firstChild, 1);  // 「##」の1文字目の後ろ
            r.collapse(true);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            document.getElementById('editor').focus();
        }"""
    )
    page.keyboard.press("Backspace")
    expect(page.locator("#editor p.md-h2 del")).to_have_text("#")
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"].split("\n")[1] == "## 見出し2"
    assert fb["result_text"].split("\n")[1] == "# 見出し2"


def test_html_in_base_text_is_not_interpreted(browser, web_port):
    """base_text 中のHTMLはタグとして解釈されず、原文として保持される。

    装飾のためにDOMを組み立てるようになっても、本文は必ずテキストノードとして
    挿入する（innerHTMLを使わない）ことを担保する。
    """
    text = "# <img src=x onerror=alert(1)>\n本文の**<b>タグ</b>**です。"
    context, page, sid = open_editor(browser, web_port, text, "HTMLエスケープ")
    try:
        assert page.locator("#editor img").count() == 0
        assert page.locator("#editor b").count() == 0
        expect(page.locator("#editor p.md-h1")).to_have_text(
            "# <img src=x onerror=alert(1)>"
        )
        save_current_html(page, sid)
        fb = feedback.build_feedback(storage.load_session(sid))
        assert fb["result_text"] == text
    finally:
        context.close()


def test_image_line_still_renders_as_image(browser, web_port, png_file):
    """画像行の扱いは装飾導入後も変わらない（記法パースに飲まれない）。"""
    sid = storage.create_session("差し替え前", "画像と装飾", "")["id"]
    name = storage.add_image_file(sid, str(png_file))
    text = f"## 図の説明\n![図]({name})\n- 続きの箇条書き"
    storage.update_base_text(sid, text)
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
        page.wait_for_selector("#editor p")
        expect(page.locator("#editor p.img-para")).to_have_count(1)
        expect(page.locator("#editor p.md-h2")).to_have_count(1)
        expect(page.locator("#editor p.md-li")).to_have_count(1)
        save_current_html(page, sid)
        fb = feedback.build_feedback(storage.load_session(sid))
        assert fb["result_text"] == text
    finally:
        context.close()

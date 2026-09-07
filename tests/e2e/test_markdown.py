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


# ---------- 表 ----------

TABLE_TEXT = "\n".join(
    [
        "説明文です。",
        "",
        "| 名前 | 説明 | 数 |",
        "|:---|:---:|---:|",
        "| **a** | `x` | 1 |",
        "| b | [link](https://e.test/1) | 2 |",
        "",
        "外側のパイプを省いた表:",
        "",
        "A | B",
        "--- | ---",
        "エス\\|ケープ | 2",
        "",
        "| 区切り行がないので表ではない |",
    ]
)


@pytest.fixture()
def table_page(browser, web_port):
    context, page, sid = open_editor(browser, web_port, TABLE_TEXT, "表E2E")
    yield page, sid
    context.close()


def test_table_is_rendered_as_cells(table_page):
    """表はヘッダ・区切り・本体に分かれ、セルごとに描画される。"""
    page, _ = table_page
    expect(page.locator("#editor p.md-tr")).to_have_count(7)
    expect(page.locator("#editor p.md-tr-head")).to_have_count(2)
    expect(page.locator("#editor p.md-tr-sep")).to_have_count(2)
    expect(page.locator("#editor p.md-tr-first")).to_have_count(2)
    head = page.locator("#editor p.md-tr-head").first
    expect(head.locator(".md-td")).to_have_count(3)
    expect(head.locator(".md-td").nth(0)).to_have_text("名前")
    expect(head.locator(".md-td").nth(2)).to_have_text("数")
    # 区切りの | は記号としてテキストに残る（原文復元のため）
    expect(head.locator(".md-mk")).to_have_count(4)


def test_table_alignment_classes(table_page):
    """区切り行の :--- / :---: / ---: が各列の寄せになる。"""
    page, _ = table_page
    row = page.locator("#editor p.md-tr").nth(2)
    expect(row.locator(".md-td.md-al-l")).to_have_count(1)
    expect(row.locator(".md-td.md-al-c")).to_have_count(1)
    expect(row.locator(".md-td.md-al-r")).to_have_count(1)


def test_table_cell_inline_syntax_is_decorated(table_page):
    """セルの中でも太字・コード・リンクが装飾される。"""
    page, _ = table_page
    body = page.locator("#editor p.md-tr").nth(2)
    expect(body.locator(".md-b")).to_have_text("**a**")
    expect(body.locator(".md-code-in")).to_have_text("`x`")
    expect(page.locator("#editor p.md-tr").nth(3).locator(".md-link")) \
        .to_have_text("[link](https://e.test/1)")


def test_table_without_outer_pipes(table_page):
    """GFMの外側パイプ省略形も表として扱う。"""
    page, _ = table_page
    head = page.locator("#editor p.md-tr-head").nth(1)
    expect(head.locator(".md-td")).to_have_count(2)
    expect(head.locator(".md-td").first).to_have_text("A")
    expect(head.locator(".md-mk")).to_have_count(1)


def test_pipe_line_without_separator_is_not_a_table(table_page):
    """区切り行のない | 行は表にしない（普通の文を誤検出しない）。"""
    page, _ = table_page
    last = page.locator("#editor p").last
    assert "md-tr" not in (last.get_attribute("class") or "")
    expect(last).to_have_text("| 区切り行がないので表ではない |")


def test_table_roundtrip_is_lossless(table_page):
    """セルに分けても原文が完全に復元できる（エスケープ \\| も含む）。"""
    page, sid = table_page
    save_current_html(page, sid)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == TABLE_TEXT
    assert fb["result_text"] == TABLE_TEXT


def test_editing_table_cell_is_tracked(table_page):
    """セルの中身を直しても追跡され、原文は保持される。"""
    page, sid = table_page
    page.evaluate(
        """() => {
            const rows = document.querySelectorAll('#editor p.md-tr');
            const td = rows[2].querySelectorAll('.md-td')[2];
            const node = td.firstChild;  // 「 1 」
            const r = document.createRange();
            r.setStart(node, 2);
            r.collapse(true);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            document.getElementById('editor').focus();
        }"""
    )
    page.keyboard.type("0")
    expect(page.locator("#editor p.md-tr ins")).to_have_text("0")
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == TABLE_TEXT
    assert "| **a** | `x` | 10 |" in fb["result_text"]


# ---------- タスクリスト・脚注・文中画像・自動リンク ----------

MISC_TEXT = "\n".join(
    [
        "- [ ] 未完了のタスク",
        "- [x] 完了したタスク",
        "文中に![図](zu.png)を置きます。",
        'タイトル付き[リンク](https://e.test/2 "説明")です。',
        "自動リンクは<https://e.test/3>です。",
        "脚注参照[^1]があります。",
        "",
        "[^1]: 脚注の説明文",
    ]
)


@pytest.fixture()
def misc_page(browser, web_port):
    context, page, sid = open_editor(browser, web_port, MISC_TEXT, "記法E2E")
    yield page, sid
    context.close()


def test_task_list_gets_checkbox_classes(misc_page):
    page, _ = misc_page
    expect(page.locator("#editor p.md-task")).to_have_count(2)
    expect(page.locator("#editor p.md-task-done")).to_have_count(1)
    # 記号は prefix としてテキストに残る
    expect(page.locator("#editor p.md-task-done .md-mk")).to_have_text("- [x]")


def test_inline_image_is_decorated(misc_page):
    """行全体でない ![alt](url) も画像として装飾する（! が取り残されない）。"""
    page, _ = misc_page
    expect(page.locator("#editor .md-img")).to_have_text("![図](zu.png)")
    expect(page.locator("#editor p.img-para")).to_have_count(0)


def test_link_with_title_and_autolink(misc_page):
    page, _ = misc_page
    links = page.locator("#editor .md-link")
    expect(links).to_have_count(2)
    expect(links.nth(0)).to_have_text('[リンク](https://e.test/2 "説明")')
    expect(links.nth(1)).to_have_text("<https://e.test/3>")


def test_footnote_reference_and_definition(misc_page):
    page, _ = misc_page
    expect(page.locator("#editor .md-fn")).to_have_text("[^1]")
    footnote = page.locator("#editor p.md-footnote")
    expect(footnote).to_have_count(1)
    expect(footnote.locator(".md-mk")).to_have_text("[^1]:")
    expect(footnote).to_have_text("[^1]: 脚注の説明文")


def test_misc_syntax_roundtrip_is_lossless(misc_page):
    page, sid = misc_page
    save_current_html(page, sid)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["original_text"] == MISC_TEXT
    assert fb["result_text"] == MISC_TEXT


def test_table_cells_are_laid_out_side_by_side(table_page):
    """セルが横に並び、列幅が揃う（罫線付きの表として読める）。"""
    page, _ = table_page
    rows = page.locator("#editor p.md-tr")
    body = rows.nth(2).locator(".md-td").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().toJSON())")
    assert len(body) == 3
    assert body[0]["y"] == body[1]["y"] == body[2]["y"]  # 同じ行に並ぶ
    assert body[0]["x"] < body[1]["x"] < body[2]["x"]
    assert abs(body[0]["width"] - body[1]["width"]) < 2  # 等幅
    # ヘッダ行と本体行で列の位置が揃う
    head = rows.nth(0).locator(".md-td").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().toJSON())")
    assert abs(head[1]["x"] - body[1]["x"]) < 2
    # 区切り行は罫線として低く表示される
    sep = rows.nth(1).evaluate("el => el.getBoundingClientRect().height")
    assert sep < rows.nth(0).evaluate("el => el.getBoundingClientRect().height")

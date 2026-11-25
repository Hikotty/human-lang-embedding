"""数式の描画の E2E テスト（Playwright / Chromium）。

文中の数式（$...$）は本文の位置に描き、ブロックの数式（$$ ... $$、```math）は
閉じの直後に読み取り専用の表示を挟む。確かめるのは次の5点。
1. 描ける: 日本語の文の中の式・ブロックの式・描けない式の表示
2. 式にならないもの: 金額（$100 と $200）、コード、エスケープ、文章を挟んだ $
3. 数式は本文ではない: 原文 → 装飾 → 編集HTML → result_text の完全一致、
   保存に描画の状態が混ざらない、TeX の _ や * を斜体・太字にしない
4. 校正できる: キャレットで TeX が開く、変更・コメント・undo・AI差分が式と両立する
5. 隔離: TeX で外へ取りに行かせても通信は起きず、フレームはエディタに触れない
"""
from __future__ import annotations
import re
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote
import pytest
from playwright.sync_api import expect
from hle_mcp import feedback, storage
DOC = "\n".join([
    "# 数式",                                          # 0
    "",                                                # 1
    "$V$ が膜電位、$\\tau$ が時定数、$I(t)$ が入力です。",  # 2
    "",                                                # 3
    "$$",                                              # 4
    "\\tau \\frac{dV}{dt} = -V + \\sum_{i} w_i x_i",    # 5
    "$$",                                              # 6
    "",                                                # 7
    "後ろの段落です。",                                # 8
])
MATH = "#editor .md-math"
BLOCK = "#editor .mth-widget"
class Doc:
    """開いたエディタ。コンソールのメッセージとリクエストも集める。"""
    def __init__(self, page, sid, console, requests):
        self.page = page
        self.sid = sid
        self.console = console
        self.requests = requests
@pytest.fixture()
def open_doc(browser, web_port):
    contexts = []
    def _open(text: str = "", title: str = "数式 E2E",
              sid: str | None = None) -> Doc:
        session = {"id": sid} if sid else storage.create_session(
            text, title, "/tmp/math")
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        contexts.append(context)
        page = context.new_page()
        console: list[str] = []
        requests: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        page.on("request", lambda r: requests.append(r.url))
        page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
        page.wait_for_selector("#editor p")
        return Doc(page, session["id"], console, requests)
    yield _open
    for c in contexts:
        c.close()


def settle(doc: Doc) -> None:
    """文中の式が描き終わり（data-mth が付く）、ブロックの式の読み込みも終わるまで待つ。"""
    page = doc.page
    expect(page.locator(MATH + ":not([data-mth])")).to_have_count(0, timeout=30000)
    expect(page.locator('#editor .mth-widget[data-state="loading"]')
           ).to_have_count(0, timeout=30000)


def mth_states(doc: Doc) -> list[str]:
    return doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .md-math'))"
        ".map((e) => e.dataset.mth || '')")


def tex_hidden(doc: Doc, n: int) -> bool:
    """n 番目の文中の式の TeX（と $）が隠れている（文字の大きさが 0）か。"""
    return doc.page.evaluate(
        """(n) => {
            const s = document.querySelectorAll('#editor .md-math')[n];
            return Array.from(s.children).every(
                (c) => getComputedStyle(c).fontSize === '0px');
        }""", n)


def formula_shown(doc: Doc, n: int) -> bool:
    """n 番目の文中の式が、疑似要素の mask として出ている（大きさがある）か。"""
    return doc.page.evaluate(
        """(n) => {
            const s = document.querySelectorAll('#editor .md-math')[n];
            const c = getComputedStyle(s, '::before');
            const mask = c.webkitMaskImage || c.maskImage || '';
            return c.content === '""' && parseFloat(c.width) > 2 &&
                parseFloat(c.height) > 2 && mask.startsWith('url("data:image/svg+xml');
        }""", n)


def css_svg(doc: Doc, selector: str, n: int = 0) -> str:
    """n 番目の要素の CSS 変数 --mth-img（data: URL）から戻した SVG の本文。"""
    value = doc.page.evaluate(
        """([sel, n]) => document.querySelectorAll(sel)[n]
            .style.getPropertyValue('--mth-img')""", [selector, n])
    assert value.startswith('url("data:image/svg+xml'), value[:80]
    return unquote(value[len('url("'):-len('")')].split(",", 1)[1])


def place_caret(doc: Doc, needle: str, after: bool = True) -> None:
    """needle を含むテキストノードの、needle の後ろ（前）にキャレットを置く。"""
    ok = doc.page.evaluate(
        """([needle, after]) => {
            const ed = document.getElementById('editor');
            const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
            let n;
            while ((n = w.nextNode())) {
                if (n.parentElement.closest('.mth-widget, .pb')) continue;
                const i = n.data.indexOf(needle);
                if (i < 0) continue;
                const r = document.createRange();
                r.setStart(n, after ? i + needle.length : i);
                r.collapse(true);
                const sel = getSelection();
                sel.removeAllRanges();
                sel.addRange(r);
                ed.focus();
                return true;
            }
            return false;
        }""", [needle, after])
    assert ok, f"本文に {needle!r} が無い"


def saved_feedback(doc: Doc) -> dict:
    expect(doc.page.locator("#save-state")).to_have_text("保存済み", timeout=8000)
    return feedback.build_feedback(storage.load_session(doc.sid))


def roundtrip(doc: Doc) -> dict:
    """編集せずに、いまのエディタHTMLをそのまま保存して、サーバー側の復元結果を返す。"""
    ok = doc.page.evaluate(
        """async (sid) => {
            const res = await fetch('/api/sessions/' + sid + '/document', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ html: Tracker.html() })
            });
            return res.ok;
        }""", doc.sid)
    assert ok
    return feedback.build_feedback(storage.load_session(doc.sid))


def csp_messages(doc: Doc) -> list[str]:
    return [m for m in doc.console
            if "Content Security Policy" in m or "Refused to" in m]


def real_errors(doc: Doc) -> list[str]:
    return [m for m in doc.console if m.startswith(("error", "pageerror"))]


# ---------- 描画 ----------
def test_inline_and_block_math_are_rendered_in_place(open_doc):
    """日本語の文の中の式は同じ位置に描かれ、TeX は隠れる。ブロックの式は閉じの直後に出る。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    assert mth_states(doc) == ["ok", "ok", "ok"]
    for n in range(3):
        assert tex_hidden(doc, n) and formula_shown(doc, n)
    # 式の画像は実際の SVG（字形の path がある）。\tau の式が V と別の形をしている
    v, tau = css_svg(doc, MATH, 0), css_svg(doc, MATH, 1)
    assert v.startswith("<svg") and "<path" in v and v != tau
    # 隠れているのは見た目だけ。TeX と $ は本文に残る（コメントや削除の対象）
    assert page.locator(MATH).nth(1).text_content() == "$\\tau$"
    assert page.locator("#editor p").nth(2).text_content().startswith(
        "$V$ が膜電位、$\\tau$ が時定数")
    # ブロックの式は、閉じの $$ の段落の直後
    expect(page.locator("#editor p.md-fence + .mth-widget")).to_have_count(1)
    assert page.evaluate(
        "() => document.querySelector('#editor .mth-widget')"
        ".previousElementSibling.textContent") == "$$"
    block = css_svg(doc, BLOCK)
    assert "<path" in block and len(block) > len(tau)
    natural = page.evaluate(
        "() => { const e = document.querySelector('#editor .mth-widget .mth-img');"
        " const r = e.getBoundingClientRect(); return [r.width, r.height]; }")
    assert natural[0] > 100 and natural[1] > 20
    assert csp_messages(doc) == []
    assert real_errors(doc) == []
    # 描画のフレームは1つで、エディタのページには MathJax 本体が読み込まれていない
    expect(page.locator("iframe.mth-frame")).to_have_count(1)
    assert page.evaluate("() => typeof window.MathJax") == "undefined"


def test_math_in_headings_lists_quotes_and_tables(open_doc):
    text = "\n".join([
        "## 見出し $x^2$",
        "- 箇条書き $a_i$",
        "> 引用 $\\alpha$",
        "",
        "| 列A | 列B |",
        "|---|---|",
        "| $y$ | 文字 |",
    ])
    doc = open_doc(text)
    settle(doc)
    assert mth_states(doc) == ["ok"] * 4
    assert roundtrip(doc)["result_text"] == text


def test_font_and_extension_files_come_from_this_server(open_doc):
    """\\mathbb・\\boldsymbol は追加の字形・拡張を読む。取得元は同梱のファイルで、外には出ない。"""
    doc = open_doc("$\\mathbb{R}$ と $\\boldsymbol{x}$ と $\\mathcal{L}$")
    settle(doc)
    assert mth_states(doc) == ["ok", "ok", "ok"]
    page = doc.page
    # フレームの中の読み込みも含め、このサーバー以外には通信していない
    frame = [f for f in page.frames if f.url.endswith("/static/math-frame.html")][0]
    loaded = frame.evaluate(
        "() => performance.getEntriesByType('resource').map((e) => e.name)")
    assert any(u.endswith("/static/mathjax/fonts/mathjax-newcm-font/svg/dynamic/"
                          "double-struck.js") for u in loaded)
    assert any(u.endswith("/static/mathjax/input/tex/extensions/boldsymbol.js")
               for u in loaded)
    assert all(u.startswith("http://127.0.0.1:") for u in loaded), loaded
    assert csp_messages(doc) == []


def test_no_frame_or_library_without_math(open_doc):
    """数式の無い文書では、隔離フレームも MathJax も読み込まない。"""
    doc = open_doc("普通の文章です。金額は $100 と $200 です。\n\n```python\nx = 1\n```")
    doc.page.wait_for_timeout(1500)
    expect(doc.page.locator("iframe.mth-frame")).to_have_count(0)
    expect(doc.page.locator(MATH)).to_have_count(0)
    expect(doc.page.locator(BLOCK)).to_have_count(0)
    assert not [u for u in doc.requests if "mathjax" in u or "math-frame" in u]


# ---------- どれが式になるか ----------
NOT_MATH = [
    "売上は $1,827,579.46 でした。同じく $1,206,494.46 です。",   # 金額
    "US$10 と US$20 です。",                                      # 閉じの直後が数字
    "It costs US$10 and US$20 in total.",                         # 同上（英数字だけ）
    "escaped \\$a b$ stays plain.",                               # 開きがエスケープ
    "金額は $100 と $200 です。",
    "$15000」（テンセント比較）、「３万$ と書かれていた。",      # 文章を挟んだ $
    "価格は \\$5 から \\$9 までです。",                          # エスケープした $
    "コードは `$x$` と `echo $HOME and $PATH` です。",           # コードスパン
    "空白に挟まれた $ x $ は式ではない。",                       # 内側の端が空白
    "$$ は式ではなく、$$ だけが続く。",                           # 閉じの無い $$
]


@pytest.mark.parametrize("line", NOT_MATH)
def test_lines_that_are_not_math_stay_plain_text(open_doc, line):
    """金額・コード・エスケープ・文章を挟んだ $ は式にしない。装飾も今までどおり。"""
    doc = open_doc(line)
    doc.page.wait_for_timeout(800)
    expect(doc.page.locator(MATH)).to_have_count(0)
    expect(doc.page.locator(BLOCK)).to_have_count(0)
    expect(doc.page.locator("iframe.mth-frame")).to_have_count(0)
    assert doc.page.locator("#editor p").first.text_content() == line
    assert roundtrip(doc)["result_text"] == line


def test_dollar_pairs_in_link_targets_and_image_alt_stay_text(open_doc):
    """リンク先・画像の説明・自動リンクの中の $...$ は式にせず、原文のまま残す。
    リンクの文言の中の式は式になる。"""
    text = ("[リンク](https://example.com/?a=$x$&b=$y$) と "
            "![図 $z$ です](pic.png) と <https://e.com/$w$> と "
            "[式 $v$ の説明](https://e.com/)")
    doc = open_doc(text)
    settle(doc)
    assert doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .md-math'))"
        ".map((e) => e.textContent)") == ["$v$"]
    assert doc.page.locator("#editor p").first.text_content() == text
    assert not re.search(r"[\ue000-\uf8ff]", doc.page.locator("#editor p").first.text_content())
    assert roundtrip(doc)["result_text"] == text


def test_tex_underscores_and_stars_are_not_emphasis(open_doc):
    """TeX の _ と * を斜体・太字にしない。太字の中の式も、太字の外の snake_case も壊れない。"""
    text = ("snake_case と $x_i + y_i$ と $a*b*c$ が並びます。\n\n"
            "**太字の $a_b$ です** と *強調 $c_d$*。")
    doc = open_doc(text)
    settle(doc)
    page = doc.page
    assert mth_states(doc) == ["ok"] * 4
    expect(page.locator("#editor .md-i")).to_have_count(1)   # *強調 ...* だけ
    expect(page.locator("#editor .md-b")).to_have_count(1)
    expect(page.locator("#editor .md-b .md-math")).to_have_count(1)
    expect(page.locator("#editor .md-i .md-math")).to_have_count(1)
    first = page.locator("#editor p").first
    assert first.text_content() == "snake_case と $x_i + y_i$ と $a*b*c$ が並びます。"
    assert roundtrip(doc)["result_text"] == text


def test_text_between_two_math_spans_is_kept(open_doc):
    """金額のあとに本物の式が続いても、式だけが式になる。"""
    doc = open_doc("$100 のうち $x$ と $y$ です。")
    settle(doc)
    assert [page_text for page_text in doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .md-math'))"
        ".map((e) => e.textContent)")] == ["$x$", "$y$"]


BLOCKS = {
    "three lines": "$$\n\\frac{a}{b}\n$$",
    "one line": "$$ \\frac{a}{b} $$",
    "inline open and close": "$$ \\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned} $$",
    "math fence": "```math\n\\frac{a}{b}\n```",
    "math fence tilde": "~~~math\n\\frac{a}{b}\n~~~",
    "blank line inside": "$$\n\\frac{a}{b}\n\n+ c\n$$",
}


@pytest.mark.parametrize("name", list(BLOCKS))
def test_block_math_forms_get_one_widget_after_the_last_line(open_doc, name):
    src = BLOCKS[name]
    doc = open_doc("前\n\n" + src + "\n\n後")
    settle(doc)
    expect(doc.page.locator(BLOCK)).to_have_count(1)
    expect(doc.page.locator('#editor .mth-widget[data-state="ok"]')).to_have_count(1)
    last = src.split("\n")[-1]
    assert doc.page.evaluate(
        "() => document.querySelector('#editor .mth-widget')"
        ".previousElementSibling.textContent") == last
    assert doc.page.evaluate(
        "() => document.querySelector('#editor .mth-widget')"
        ".nextElementSibling.nextElementSibling.textContent") == "後"
    assert csp_messages(doc) == []


def test_block_math_not_drawn_in_code_or_when_unclosed(open_doc):
    """コードブロックの中の $$ と、閉じの無い $$・空の $$ は式にしない。"""
    text = "\n".join([
        "```python", "$$", "x = 1", "$$", "```",   # コードの中
        "", "$$", "a = b",                           # 閉じていない（文書の最後まで）
    ])
    doc = open_doc(text)
    doc.page.wait_for_timeout(1000)
    expect(doc.page.locator(BLOCK)).to_have_count(0)
    expect(doc.page.locator("iframe.mth-frame")).to_have_count(0)
    empty = open_doc("$$\n$$\n\n```math\n```")
    empty.page.wait_for_timeout(800)
    expect(empty.page.locator(BLOCK)).to_have_count(0)


def test_tex_lines_in_block_are_not_decorated(open_doc):
    """$$ の中の行は、強調やリンクを解釈せず、コードと同じ行として出す。"""
    doc = open_doc("$$\na_i * b_i [x](y) `c`\n$$")
    settle(doc)
    page = doc.page
    expect(page.locator("#editor p").nth(1)).to_have_class(re.compile(r"\bmd-code\b"))
    expect(page.locator("#editor .md-i, #editor .md-b, #editor .md-link, "
                        "#editor .md-code-in")).to_have_count(0)
    assert page.locator("#editor p").nth(1).text_content() == "a_i * b_i [x](y) `c`"


# ---------- 数式は本文ではない ----------
def test_math_never_enters_saved_text(open_doc):
    """保存・result_text・original_text に描画の状態は混ざらず、原文と完全一致する。"""
    doc = open_doc(DOC)
    settle(doc)
    saved = doc.page.evaluate("() => Tracker.html()")
    assert "mth-widget" not in saved
    assert "data-mth" not in saved and "style=" not in saved
    assert "md-math-edit" not in saved and "title=" not in saved
    # 式の span は、見え方（data-view・contenteditable・選択の印）を持たずに残る
    tags = re.findall(r'<span class="md-math[^"]*"[^>]*>', saved)
    assert tags == ['<span class="md-math">'] * 3  # TeX と $ は本文の記号として残る
    ok = doc.page.evaluate(
        """async (sid) => {
            const res = await fetch('/api/sessions/' + sid + '/document', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ html: Tracker.html() })
            });
            return res.ok;
        }""", doc.sid)
    assert ok
    stored = storage.load_session(doc.sid)["revisions"][-1]["edited_html"]
    assert "mth-widget" not in stored and "<div" not in stored
    fb = feedback.build_feedback(storage.load_session(doc.sid))
    assert fb["original_text"] == DOC
    assert fb["result_text"] == DOC
    assert fb["changes"] == []


def test_reopened_document_redraws_without_duplicates(open_doc, web_port):
    """編集済みの文書を開き直すと、式を（保存の状態に頼らず）描き直し、ウィジェットは二重にならない。"""
    doc = open_doc(DOC)
    settle(doc)
    place_caret(doc, "後ろの段落です")
    doc.page.keyboard.type("！")
    saved_feedback(doc)
    page2 = doc.page.context.new_page()
    page2.goto(f"http://127.0.0.1:{web_port}/session/{doc.sid}")
    page2.wait_for_selector("#editor p ins")
    expect(page2.locator(BLOCK)).to_have_count(1)
    expect(page2.locator(MATH + '[data-mth="ok"]')).to_have_count(3, timeout=30000)
    expect(page2.locator('#editor .mth-widget[data-state="ok"]')).to_have_count(1)


# ---------- 描けない式 ----------
def test_broken_inline_math_shows_tex_with_reason(open_doc):
    """構文エラーと未定義のマクロは、式の代わりに TeX を残して波線で示す（理由は title）。"""
    doc = open_doc("壊れた $\\frac{a$ と未定義 $\\foo$ と正常 $x$")
    settle(doc)
    page = doc.page
    assert mth_states(doc) == ["err", "err", "ok"]
    assert not tex_hidden(doc, 0) and not formula_shown(doc, 0)
    assert not tex_hidden(doc, 1) and not formula_shown(doc, 1)
    assert tex_hidden(doc, 2) and formula_shown(doc, 2)
    titles = page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .md-math'))"
        ".map((e) => e.title)")
    assert "Missing close brace" in titles[0]
    assert "foo" in titles[1] and titles[2] == ""
    assert "wavy" in page.evaluate(
        "() => getComputedStyle(document.querySelector('#editor .md-math .md-mt'))"
        ".textDecorationLine + ' ' + getComputedStyle("
        "document.querySelector('#editor .md-math .md-mt')).textDecorationStyle")
    assert roundtrip(doc)["result_text"] == (
        "壊れた $\\frac{a$ と未定義 $\\foo$ と正常 $x$")
    # title は保存しない
    assert "title=" not in page.evaluate("() => Tracker.html()")
    assert real_errors(doc) == []


def test_broken_block_math_shows_error_and_recovers(open_doc):
    """ブロックの式の構文エラーは、式の代わりにエラーを出す。直せば式に戻る。"""
    doc = open_doc("$$\n\\frac{a\n$$\n\n後")
    page = doc.page
    expect(page.locator('#editor .mth-widget[data-state="error"]')).to_have_count(
        1, timeout=30000)
    assert page.evaluate(
        "() => document.querySelector('#editor .mth-widget').dataset.error"
    ) == "Missing close brace"
    # ウィジェットはテキストノードを持たない（文言は疑似要素）
    assert page.evaluate(
        "() => document.querySelector('#editor .mth-widget').textContent") == ""
    place_caret(doc, "\\frac{a")
    page.keyboard.type("}{b}")
    expect(page.locator('#editor .mth-widget[data-state="ok"]')).to_have_count(
        1, timeout=15000)
    fb = saved_feedback(doc)
    assert fb["result_text"] == "$$\n\\frac{a}{b}\n$$\n\n後"


def block_view(doc: Doc, n: int = 0) -> str:
    return doc.page.evaluate(
        "(n) => document.querySelectorAll('#editor .mth-widget')[n].dataset.view || ''", n)


def hidden_paras(doc: Doc) -> list[str]:
    """表示されていない（display: none の）段落の文字列。"""
    return doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor p'))"
        ".filter((p) => getComputedStyle(p).display === 'none').map((p) => p.textContent)")


def test_block_math_hides_tex_and_shows_only_the_formula(open_doc):
    """描けたブロックの式は、TeX の段落（$$ の行と中の行）を隠して式だけを見せる。
    隠すのは見た目だけで、本文・保存・result_text は変わらない。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    assert block_view(doc) == "formula"
    assert hidden_paras(doc) == ["$$", DOC.split("\n")[5], "$$"]
    box = page.evaluate(
        """() => { const w = document.querySelector('#editor .mth-widget');
            const r = w.querySelector('.mth-img').getBoundingClientRect();
            return [getComputedStyle(w).borderTopColor, r.width, r.height]; }""")
    assert box[0] == "rgba(0, 0, 0, 0)" and box[1] > 100 and box[2] > 20  # 枠の無い式
    saved = page.evaluate("() => Tracker.html()")
    assert "mth-hide" not in saved and "mth-widget" not in saved
    assert roundtrip(doc)["result_text"] == DOC


def test_click_selects_the_block_and_comment_marks_the_formula(open_doc):
    """ブロックの式をクリックすると式全体が選ばれる。コメントを付けると、式は描いたまま
    コメントの色が付き、quote は TeX になる。絞り込みで隠すと色も消える。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.locator(BLOCK).click()
    expect(page.locator(BLOCK)).to_have_class(re.compile(r"\bmth-sel\b"))
    page.click("#btn-comment")
    page.fill("#draft-text", "総和の添字を書きたい")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    place_caret(doc, "後ろの段落")
    expect(page.locator(BLOCK)).to_have_attribute("data-cmt", re.compile("on|active"))
    assert block_view(doc) == "formula"
    assert len(hidden_paras(doc)) == 3
    fb = saved_feedback(doc)
    assert [c["quote"] for c in fb["comments"]] == ["$$" + DOC.split("\n")[5] + "$$"]
    assert fb["result_text"] == DOC
    # カードを押すと、隠れた TeX ではなく見えている式へ飛び、式が強調される
    page.evaluate("() => window.scrollTo(0, 0)")
    page.locator(".comment-card .quote").first.click()
    expect(page.locator(BLOCK)).to_have_attribute("data-cmt", "active")
    page.wait_for_timeout(800)
    assert page.locator(BLOCK).is_visible()
    assert page.evaluate(
        "() => { const r = document.querySelector('#editor .mth-widget')"
        ".getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }")
    page.locator('input[name="comment-filter"][value="ai"]').check()
    expect(page.locator(BLOCK)).not_to_have_attribute("data-cmt", re.compile(".+"))


def test_double_click_block_edits_tex_and_changes_stay_visible(open_doc):
    """TeX を直したいときだけ、ダブルクリックで TeX の段落を出して編集する。直したブロックは
    外へ出ても変更（赤字）の付いた TeX を見せ、その下に直した式を添える。undo で式だけに戻る。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    before = css_svg(doc, BLOCK)
    page.evaluate(
        "() => { document.querySelector('#editor .mth-widget').__mark = 7; }")
    page.locator(BLOCK).dblclick()
    expect(page.locator(BLOCK)).to_have_attribute("data-view", "tex")
    assert hidden_paras(doc) == []
    place_caret(doc, "w_i x_i")
    page.keyboard.type(" + z^2")
    expect(page.locator("#editor p ins")).to_have_text(" + z^2")
    place_caret(doc, "後ろの段落")
    end = time.monotonic() + 15
    while time.monotonic() < end and css_svg(doc, BLOCK) == before:
        page.wait_for_timeout(100)
    assert css_svg(doc, BLOCK) != before
    assert block_view(doc) == "tex" and hidden_paras(doc) == []
    assert page.evaluate(
        "() => document.querySelector('#editor .mth-widget').__mark") == 7
    fb = saved_feedback(doc)
    assert fb["result_text"].split("\n")[5].endswith("w_i x_i + z^2")
    assert len(fb["result_text"].split("\n")) == len(DOC.split("\n"))
    for _ in range(6):
        page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#editor p ins")).to_have_count(0)
    expect(page.locator(BLOCK)).to_have_attribute("data-view", "formula")
    assert len(hidden_paras(doc)) == 3


def test_backspace_after_block_deletes_the_whole_formula(open_doc):
    """ブロックの直後の段落の先頭の Backspace は、式全体を削除にする。消した式は
    赤の取り消し線の式で見え、TeX は隠れたまま。undo で戻る。"""
    doc = open_doc("前\n\n$$\n\\frac{a}{b}\n$$\n後ろの段落")
    settle(doc)
    page = doc.page
    place_caret(doc, "後ろの段落", after=False)
    page.keyboard.press("Backspace")
    expect(page.locator(BLOCK)).to_have_attribute("data-view", "deleted")
    assert len(hidden_paras(doc)) == 3
    strike = page.evaluate(
        """() => { const b = document.querySelector('#editor .mth-widget .mth-box');
            const a = getComputedStyle(b, '::after');
            return [a.content, a.borderTopStyle,
                    getComputedStyle(document.querySelector('#editor .mth-widget')).color,
                    getComputedStyle(document.querySelector('#editor p')).color]; }""")
    assert strike[0] == '""' and strike[1] == "solid" and strike[2] != strike[3]
    fb = saved_feedback(doc)
    assert fb["result_text"] == "前\n\n\n後ろの段落"
    # キャレットは隠れた TeX の中に入らず、続けて打つと後ろの段落に入る
    page.keyboard.type("X")
    expect(page.locator("#editor p ins")).to_have_text("X")
    assert saved_feedback(doc)["result_text"] == "前\n\n\nX後ろの段落"
    for _ in range(2):
        page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#editor p del")).to_have_count(0)
    expect(page.locator(BLOCK)).to_have_attribute("data-view", "formula")
    assert saved_feedback(doc)["result_text"] == "前\n\n$$\n\\frac{a}{b}\n$$\n後ろの段落"


def test_typing_over_a_selected_block_replaces_it(open_doc):
    """式を選んで打つと、式全体が削除になり、打った文字は式の後ろに入る。"""
    doc = open_doc("$$\n\\frac{a}{b}\n$$\n\n末尾")
    settle(doc)
    page = doc.page
    page.locator(BLOCK).click()
    page.keyboard.type("Q")
    expect(page.locator("#editor p ins")).to_have_text("Q")
    expect(page.locator(BLOCK)).to_have_attribute("data-view", "deleted")
    visible_ins = page.evaluate(
        "() => getComputedStyle(document.querySelector('#editor ins')"
        ".closest('p')).display")
    assert visible_ins != "none"
    assert saved_feedback(doc)["result_text"] == "\nQ\n末尾"


def test_arrow_keys_skip_hidden_tex(open_doc):
    """矢印キーで上下に動いても、隠れた TeX の段落にキャレットが入らない。"""
    doc = open_doc("前の段落\n$$\n\\frac{a}{b}\n$$\n後の段落")
    settle(doc)
    page = doc.page
    place_caret(doc, "前の段落")
    page.keyboard.press("ArrowDown")
    page.keyboard.type("Y")
    expect(page.locator("#editor p ins")).to_have_text("Y")
    # 打った文字は見えている段落に入る（隠れた TeX の段落ではない）
    shown = page.evaluate(
        "() => getComputedStyle(document.querySelector('#editor ins')"
        ".closest('p')).display")
    assert shown != "none"
    assert block_view(doc) == "formula"
    lines = saved_feedback(doc)["result_text"].split("\n")
    assert lines[1:4] == ["$$", "\\frac{a}{b}", "$$"]

# ---------- 校正できる ----------
def click_formula(doc: Doc, n: int) -> None:
    doc.page.locator(MATH).nth(n).click()


def selection_span_index(doc: Doc) -> int:
    """キャレットがある文中の式の番号（無ければ -1）。"""
    return doc.page.evaluate(
        """() => {
            const sel = getSelection();
            const el = sel.anchorNode && (sel.anchorNode.nodeType === 1
                ? sel.anchorNode : sel.anchorNode.parentElement);
            const span = el && el.closest('.md-math');
            return span ? Array.from(
                document.querySelectorAll('#editor .md-math')).indexOf(span) : -1;
        }""")


def math_views(doc: Doc) -> list[str]:
    return doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .md-math'))"
        ".map((e) => e.dataset.view || '')")


def test_click_selects_the_formula_as_one_piece(open_doc):
    """式をクリックすると TeX は出ずに式全体が選ばれる。続けて打つと式が置き換わる（削除＋追加）。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    click_formula(doc, 1)
    expect(page.locator(MATH).nth(1)).to_have_class(re.compile(r"\bmd-math-sel\b"))
    covered = page.evaluate(
        """() => {
            const s = document.querySelectorAll('#editor .md-math')[1];
            const r = getSelection().getRangeAt(0);
            const t = document.createRange();
            t.selectNode(s);
            return r.compareBoundaryPoints(Range.START_TO_START, t) <= 0 &&
                r.compareBoundaryPoints(Range.END_TO_END, t) >= 0 &&
                r.toString() === '$\\\\tau$';
        }""")
    assert covered
    # 選んでも TeX は出ない。式は塊（contenteditable=false）のまま
    assert tex_hidden(doc, 1) and formula_shown(doc, 1)
    assert page.locator(MATH).nth(1).get_attribute("contenteditable") == "false"
    assert math_views(doc) == ["formula"] * 3
    page.keyboard.type("T")
    expect(page.locator("#editor p ins")).to_have_text("T")
    # 置き換えた式は、式のまま赤の取り消し線になる（TeX は出ない）
    expect(page.locator(MATH).nth(1)).to_have_attribute("data-view", "deleted")
    assert tex_hidden(doc, 1) and formula_shown(doc, 1)
    fb = saved_feedback(doc)
    assert fb["result_text"].split("\n")[2] == "$V$ が膜電位、T が時定数、$I(t)$ が入力です。"
    # 見え方は保存しない
    assert "md-math-sel" not in page.evaluate("() => Tracker.html()")


def test_arrow_keys_step_over_a_formula(open_doc):
    """矢印キーは式を 1 つとして飛び越える（隠れた TeX の中にキャレットが入らない）。"""
    doc = open_doc("前$x$後")
    settle(doc)
    page = doc.page
    place_caret(doc, "前")
    page.keyboard.press("ArrowRight")
    page.keyboard.type("Y")
    expect(page.locator("#editor p ins")).to_have_text("Y")
    assert math_views(doc) == ["formula"]
    assert tex_hidden(doc, 0) and formula_shown(doc, 0)
    assert saved_feedback(doc)["result_text"] == "前$x$Y後"


def test_typing_with_caret_inside_hidden_tex_goes_outside_the_formula(open_doc):
    """undo のキャレットの復元などで、キャレットが隠れた TeX の中に置かれても、打った文字は
    式の外に入る（式は壊れず、塊のまま）。式の先頭なら式の前、それ以外は式の後ろ。"""
    doc = open_doc("前$xy$後")
    settle(doc)
    page = doc.page
    for offset, expected in ((1, "前$xy$Q後"), (0, "前Q$xy$後")):
        page.evaluate(
            """(off) => {
                // undo の restoreCaret と同じく、フォーカスしてからキャレットを置く
                document.getElementById('editor').focus();
                const t = document.querySelector('#editor .md-math .md-mt').firstChild;
                const r = document.createRange();
                r.setStart(off ? t : document.querySelector('#editor .md-math .md-mk')
                    .firstChild, off ? 1 : 0);
                r.collapse(true);
                const sel = getSelection();
                sel.removeAllRanges();
                sel.addRange(r);
            }""", offset)
        page.keyboard.type("Q")
        expect(page.locator(MATH + " ins")).to_have_count(0)
        assert math_views(doc) == ["formula"]
        assert saved_feedback(doc)["result_text"] == expected
        page.keyboard.press("ControlOrMeta+z")
        expect(page.locator("#editor p ins")).to_have_count(0)


def test_double_click_edits_tex_and_changes_stay_visible(open_doc):
    """TeX を直したいときだけ、ダブルクリックで TeX を出して編集する。直した式は、
    外へ出ても変更（赤字）の付いた TeX のまま見せる（変更を見落とさない）。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.locator(MATH).nth(1).dblclick()
    expect(page.locator(MATH).nth(1)).to_have_attribute("data-view", "tex")
    assert not tex_hidden(doc, 1) and not formula_shown(doc, 1)
    assert formula_shown(doc, 0) and formula_shown(doc, 2)  # ほかの式は塊のまま
    page.keyboard.type("x")
    expect(page.locator(MATH).nth(1).locator("ins")).to_have_text("x")
    place_caret(doc, "後ろの段落")
    page.wait_for_timeout(700)
    expect(page.locator(MATH + ".md-math-edit")).to_have_count(0)
    assert math_views(doc) == ["formula", "tex", "formula"]
    fb = saved_feedback(doc)
    assert fb["result_text"].split("\n")[2] == (
        "$V$ が膜電位、$x\\tau$ が時定数、$I(t)$ が入力です。")
    assert [c for c in fb["changes"] if c["type"] == "insert"][0]["text"] == "x"


def test_backspace_deletes_the_whole_formula(open_doc):
    """式の直後の Backspace は、TeX を 1 文字ずつではなく式全体を削除にする。消した式は
    赤の取り消し線の式で見え、もう一度の Backspace はその前の文字を消す。undo で戻る。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    place_caret(doc, " が時定数", after=False)
    page.keyboard.press("Backspace")
    expect(page.locator(MATH).nth(1)).to_have_attribute("data-view", "deleted")
    assert tex_hidden(doc, 1) and formula_shown(doc, 1)
    strike = page.evaluate(
        """() => {
            const s = document.querySelectorAll('#editor .md-math')[1];
            const a = getComputedStyle(s, '::after');
            return [a.content, a.borderTopStyle, getComputedStyle(s).color,
                    getComputedStyle(document.querySelector('#editor p')).color];
        }""")
    assert strike[0] == '""' and strike[1] == "solid" and strike[2] != strike[3]
    assert page.locator(MATH).nth(1).locator("del").count() >= 3  # $ と TeX と $
    page.keyboard.press("Backspace")
    fb = saved_feedback(doc)
    assert fb["result_text"].split("\n")[2] == "$V$ が膜電位 が時定数、$I(t)$ が入力です。"
    assert fb["original_text"] == DOC
    page.keyboard.press("ControlOrMeta+z")
    page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#editor p del")).to_have_count(0)
    assert math_views(doc) == ["formula"] * 3
    assert saved_feedback(doc)["result_text"] == DOC


def test_comment_on_formula_keeps_it_drawn_and_quotes_tex(open_doc):
    """式へのコメントは quote が TeX になり、式は描いたまま、コメントの色が式に付く。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.evaluate(
        """() => {
            const s = document.querySelectorAll('#editor .md-math')[1];
            const r = document.createRange();
            r.setStartBefore(s);
            r.setEndAfter(s);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            document.getElementById('editor').focus();
        }""")
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    page.fill("#draft-text", "時定数の記号を T にしたい")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    place_caret(doc, "後ろの段落")
    expect(page.locator(MATH + ".md-math-open")).to_have_count(0)
    assert tex_hidden(doc, 1) and formula_shown(doc, 1)
    # 隠れた TeX に付いたコメントの色が、式の背景に出る
    bg = page.evaluate(
        "() => getComputedStyle(document.querySelectorAll('#editor .md-math')[1])"
        ".backgroundColor")
    plain = page.evaluate(
        "() => getComputedStyle(document.querySelectorAll('#editor .md-math')[0])"
        ".backgroundColor")
    assert bg != plain and plain == "rgba(0, 0, 0, 0)"
    fb = saved_feedback(doc)
    assert [c["quote"] for c in fb["comments"]] == ["$\\tau$"]
    assert fb["result_text"] == DOC


def test_undo_redo_bring_the_formula_back_from_cache(open_doc):
    """undo / redo は innerHTML を差し替えるが、式は待たずに同じ見え方に戻る（描き直しも空の枠も無い）。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    place_caret(doc, " が膜電位", after=False)
    page.keyboard.press("Backspace")
    expect(page.locator(MATH).nth(0)).to_have_attribute("data-view", "deleted")
    page.keyboard.press("ControlOrMeta+z")
    # キー入力が返った直後に、待たずに式の状態が揃っている（debounce を待たない）
    assert mth_states(doc) == ["ok", "ok", "ok"]
    assert math_views(doc) == ["formula"] * 3
    expect(page.locator("#editor p del")).to_have_count(0)
    expect(page.locator(BLOCK)).to_have_count(1)
    assert all(formula_shown(doc, n) for n in range(3))
    page.keyboard.press("ControlOrMeta+Shift+z")
    assert math_views(doc) == ["deleted", "formula", "formula"]
    assert formula_shown(doc, 0)
    assert saved_feedback(doc)["result_text"].split("\n")[2].startswith(" が膜電位")
    assert real_errors(doc) == []


def test_ai_diff_keeps_formulas_and_shows_only_changed_tex(open_doc):
    """AI差分の表示中も式は式のまま。AIが一部を変えた式だけ TeX（青字と幽霊）を見せ、
    AIが丸ごと足した式は青い式で見せる。切ると元に戻る。"""
    rev2 = DOC.replace("$I(t)$", "$J(t)$").replace(
        "後ろの段落です。", "後ろの段落 $z^2$ です。").replace("w_i x_i", "w_i x_i + c")
    session = storage.create_session(DOC, "AI差分と数式", "/tmp/math")
    storage.add_revision(session["id"], rev2)
    doc = open_doc(sid=session["id"])
    settle(doc)
    page = doc.page
    assert math_views(doc) == ["formula"] * 4
    page.locator("#btn-aidiff").click()
    expect(page.locator(MATH).nth(2)).to_have_attribute("data-view", "tex")
    assert math_views(doc) == ["formula", "formula", "tex", "ai-ins"]
    assert formula_shown(doc, 0) and formula_shown(doc, 3)
    assert not formula_shown(doc, 2)
    expect(page.locator(MATH).nth(2).locator(".ai-del")).to_have_count(1)
    # AIが TeX に足しただけ（消した文字の幽霊が無い）のブロックの式も、TeX（青字）を見せる
    expect(page.locator("#editor .mth-hide")).to_have_count(0)
    assert page.evaluate(
        "() => document.querySelectorAll('#editor p.md-code .ai-del').length") == 0
    assert block_view(doc) == "tex" and hidden_paras(doc) == []
    blue = page.evaluate(
        "() => getComputedStyle(document.querySelectorAll('#editor .md-math')[3]).color")
    text = page.evaluate(
        "() => getComputedStyle(document.querySelector('#editor p')).color")
    assert blue != text
    page.locator("#btn-aidiff").click()
    expect(page.locator(MATH + " .ai-del")).to_have_count(0)
    assert math_views(doc) == ["formula"] * 4
    assert block_view(doc) == "formula" and len(hidden_paras(doc)) == 3
    html = page.evaluate("() => Tracker.html()")
    assert "ai-del" not in html and "data-ai" not in html and "data-view" not in html


def test_formula_color_follows_text_color_in_dark_mode(open_doc):
    """式は文字色で塗る（mask）。ダークに切り替えても式は読める色になり、描き直さない。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    read = """() => {
        const s = document.querySelectorAll('#editor .md-math')[0];
        const b = document.querySelector('#editor .mth-widget .mth-img');
        return [getComputedStyle(s).color, getComputedStyle(s, '::before').backgroundColor,
                getComputedStyle(b).backgroundColor, getComputedStyle(b).color];
    }"""
    light = page.evaluate(read)
    assert light[0] == light[1] and light[2] == light[3]
    svg_before = css_svg(doc, MATH, 0)
    page.evaluate("() => HleTheme.save('dark')")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    dark = page.evaluate(read)
    assert dark[0] == dark[1] and dark[2] == dark[3]
    assert dark[0] != light[0]
    assert css_svg(doc, MATH, 0) == svg_before  # 色だけが変わる（描き直しは無い）
    page.evaluate("() => HleTheme.save('light')")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_select_all_and_delete_across_math(open_doc):
    """全選択して消しても壊れず、ブロックの式は削除済みの式として残り、原文は保たれる。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.locator("#editor p").first.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.press("Backspace")
    expect(page.locator(BLOCK)).to_have_attribute("data-view", "deleted")
    assert math_views(doc) == ["deleted"] * 3
    fb = saved_feedback(doc)
    assert fb["original_text"] == DOC
    assert fb["result_text"] == ""
    assert [m for m in doc.console if m.startswith("pageerror")] == []


# ---------- 数式に対応する前に保存したセッション ----------
OLD_TEXT = "\n".join([
    "行列 $E$ の $i$ 行目を取り出します。",   # 0
    "snake_case と $x_i$ です。",            # 1
    "$a$ と $b$",                            # 2（人間が「追加」を書き足した）
    "$c$",                                   # 3（人間が式の中に d を書き足した）
    "$e$ にコメント",                        # 4（式全体にコメント）
    "$$",                                    # 5
    "a_i b_i",                               # 6
    "$$",                                    # 7
])
# 数式に対応する前の renderBase が作った形（TeX の _ が斜体の記号になっている）
OLD_HTML = "".join([
    "<p>行列 $E$ の $i$ 行目を取り出します。</p>",
    '<p>snake<span class="md-i"><span class="md-mk">_</span>case と $x'
    '<span class="md-mk">_</span></span>i$ です。</p>',
    "<p>$a$ と<ins>追加</ins> $b$</p>",
    "<p>$c<ins>d</ins>$</p>",
    '<p><span class="cmt" data-cid="c1">$e$</span> にコメント</p>',
    "<p>$$</p>",
    '<p>a<span class="md-i"><span class="md-mk">_</span>i b'
    '<span class="md-mk">_</span></span>i</p>',
    "<p>$$</p>",
])


def test_session_saved_before_math_support_renders_inline_math(open_doc):
    """数式に対応する前に編集を保存したセッションでも、開くと文中の式が描かれる。
    本文の文字・変更・コメントは 1 つも動かず、式にかかる変更がある式だけ TeX のまま。"""
    sid = storage.create_session(OLD_TEXT, "保存済み", "/tmp/math")["id"]
    storage.add_comment(sid, "$e$", "記号を変えたい", author="human", comment_id="c1")
    storage.set_document(sid, OLD_HTML)
    before = feedback.build_feedback(storage.load_session(sid))
    doc = open_doc(sid=sid)
    settle(doc)
    page = doc.page
    assert page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .md-math'))"
        ".map((e) => e.textContent)") == ["$E$", "$i$", "$x_i$", "$a$", "$b$", "$e$"]
    assert math_views(doc) == ["formula"] * 6
    assert all(tex_hidden(doc, n) and formula_shown(doc, n) for n in range(6))
    # 書き足しのある $cd$ は式にしない（変更が見えなくなるため）
    assert page.locator("#editor p").nth(3).text_content() == "$cd$"
    expect(page.locator("#editor p").nth(3).locator(".md-math")).to_have_count(0)
    # TeX の _ を斜体と読んだ装飾は外れ、$$ の中の行はコードと同じ行になる
    expect(page.locator("#editor .md-i")).to_have_count(0)
    expect(page.locator("#editor p").nth(6)).to_have_class(re.compile(r"\bmd-code\b"))
    expect(page.locator("#editor p").nth(5)).to_have_class(re.compile(r"\bmd-fence\b"))
    expect(page.locator('#editor .mth-widget[data-state="ok"]')).to_have_count(1)
    # 式全体に付いたコメントは、式を囲むコメントの色が式の上に出て、quote は TeX のまま
    bg = page.evaluate(
        "() => { const s = document.querySelectorAll('#editor .md-math')[5];"
        " const c = s.closest('.cmt');"
        " return c ? getComputedStyle(c).backgroundColor : ''; }")
    assert bg not in ("", "rgba(0, 0, 0, 0)")
    # 本文・変更・コメントは開く前と同じ
    after = roundtrip(doc)
    assert after["result_text"] == before["result_text"]
    assert after["original_text"] == OLD_TEXT
    assert after["changes"] == before["changes"]
    assert [c["quote"] for c in after["comments"]] == ["$e$"]
    assert real_errors(doc) == []


def test_upgraded_session_does_not_wrap_twice(open_doc, web_port):
    """変換したあとに保存して開き直しても、式が二重にならない。"""
    sid = storage.create_session(OLD_TEXT, "保存済み", "/tmp/math")["id"]
    storage.set_document(sid, OLD_HTML)
    doc = open_doc(sid=sid)
    settle(doc)
    place_caret(doc, "にコメント")
    doc.page.keyboard.type("！")
    saved_feedback(doc)
    page2 = doc.page.context.new_page()
    page2.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
    page2.wait_for_selector("#editor p ins")
    expect(page2.locator(MATH + '[data-view="formula"]')).to_have_count(6, timeout=30000)
    expect(page2.locator("#editor .md-math .md-math")).to_have_count(0)

# ---------- 悪意ある TeX と隔離 ----------
def dangerous_parts(svg: str) -> list[str]:
    """SVG を XML として読み、実行や外部読み込みになりうる要素・属性を挙げる。"""
    bad = []
    for el in ET.fromstring(svg).iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "iframe", "object", "embed", "foreignobject", "a",
                   "image", "use", "style", "link"}:
            bad.append(f"<{tag}>")
        for key, value in el.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on") or name == "href":
                bad.append(f"{tag}@{name}")
            if re.search(r"url\s*\(|javascript:|https?://", value, re.IGNORECASE):
                bad.append(f"{tag}@{name}={value[:40]}")
    return bad


def evil_sources(base: str) -> list[str]:
    return [
        r"\href{javascript:window.__pwned=1}{x}",
        rf"\href{{{base}/href}}{{x}}",
        rf"\style{{background: url({base}/style.png)}}{{x}}",
        rf"\style{{color: red; background-image: url('{base}/style2.png')}}{{y}}",
        r"\class{a b}{x} + \cssId{c}{y}",
        r"\unicode{x1F600}",
        rf"\require{{{base}/evil.js}} z",
        r"\require{html}\style{color:red}{x}",
        r"\def\a{\a}\a",
    ]


def test_malicious_tex_does_not_execute_or_leak_dangerous_markup(open_doc):
    """リンク・スタイル・外部の読み込み・マクロの無限展開を含む TeX を描かせても、スクリプトは動かず、
    通信は 1 件も起きず、受け取った SVG にリンクも外部を指す記述も残らない。"""
    hits: list[str] = []

    class Catcher(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()

        def log_message(self, *args):  # 標準エラーを汚さない
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        sources = evil_sources(base)
        text = "\n\n".join(f"式 ${src}$ です" for src in sources)
        text += "\n\n" + "\n\n".join(f"$$\n{src}\n$$" for src in sources)
        doc = open_doc(text)
        settle(doc)
        page = doc.page
        doc.page.wait_for_timeout(1500)
        assert hits == []
        # エディタのページでも、隔離フレームの中でも、注入したコードは動いていない
        assert page.evaluate("() => window.__pwned") is None
        for frame in page.frames:
            assert frame.evaluate("() => window.__pwned") is None
        # 描けたものの SVG には、実行できる要素・属性・リンクが残らない
        ok = 0
        for sel, nodes in ((MATH, len(sources)), (BLOCK, len(sources))):
            for i in range(nodes):
                state = page.evaluate(
                    "([sel, i]) => { const e = document.querySelectorAll(sel)[i];"
                    " return e.dataset.mth || e.dataset.state; }", [sel, i])
                if state == "ok":
                    ok += 1
                    assert dangerous_parts(css_svg(doc, sel, i)) == [], (sel, i)
        assert ok >= 6  # 無限展開（と require の失敗）以外は、普通に式として描ける
        assert page.evaluate(
            "() => document.querySelectorAll('#editor a, #editor script, "
            "#editor iframe, #editor object, #editor [onerror]').length") == 0
        assert csp_messages(doc) == []
        # 無限展開のマクロはエラーになり、エディタは動き続ける
        states = mth_states(doc)
        assert "err" in states
        place_caret(doc, "です")
        page.keyboard.type("!")
        expect(page.locator("#editor p ins").first).to_have_text("!")
    finally:
        server.shutdown()
        server.server_close()


def test_frame_is_isolated_from_the_editor_page(open_doc):
    """隔離フレームは不透明オリジン: エディタの DOM にも API にも届かない。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    assert csp_messages(doc) == []  # 以下の調べ方で出る CSP の通知と混ざらないように
    attrs = page.evaluate(
        "() => document.querySelector('iframe.mth-frame').getAttribute('sandbox')")
    assert attrs == "allow-scripts"  # allow-same-origin を付けていない
    frames = [f for f in page.frames if f.url.endswith("/static/math-frame.html")]
    assert len(frames) == 1
    result = frames[0].evaluate(
        """async () => {
            const out = { origin: self.origin };
            try { void window.parent.document.title; out.parentDom = 'leak'; }
            catch (e) { out.parentDom = 'blocked'; }
            try { void document.cookie; out.cookie = 'leak'; }
            catch (e) { out.cookie = 'blocked'; }
            try { await fetch('/api/sessions'); out.fetch = 'leak'; }
            catch (e) { out.fetch = 'blocked'; }
            try { void localStorage.length; out.storage = 'leak'; }
            catch (e) { out.storage = 'blocked'; }
            return out;
        }""")
    assert result == {"origin": "null", "parentDom": "blocked",
                      "cookie": "blocked", "fetch": "blocked",
                      "storage": "blocked"}


def test_message_from_other_windows_is_ignored(open_doc):
    """フレームの結果は、そのフレームから来たものだけ受け取る。ページが勝手に出した
    メッセージで式の画像が差し替わることはない。"""
    doc = open_doc(DOC)
    settle(doc)
    before = css_svg(doc, MATH, 0)
    doc.page.evaluate(
        """() => window.postMessage({kind: 'rendered', id: 1, ok: true,
            svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            width: 1, height: 1, depth: 0}, '*')""")
    doc.page.wait_for_timeout(500)
    assert css_svg(doc, MATH, 0) == before

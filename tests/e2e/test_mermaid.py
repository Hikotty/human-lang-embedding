"""Mermaid 図の描画の E2E テスト（Playwright / Chromium）。

```mermaid のコードブロックの直後に、描画した図を読み取り専用で出す。
確かめるのは次の4点。

1. 図が実際に描かれる（日本語ラベル・主要な図種・構文エラーの表示）
2. 図は本文ではない: 保存・result_text・コメントの quote に混ざらず、
   原文 → 装飾 → 編集HTML → result_text の完全一致が保たれる
3. 編集・undo/redo・配色の切り替えに図が追従する（図を描くのは隔離フレーム）
4. 悪意ある Mermaid ソースでスクリプトが動かず、フレームはエディタに触れない
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

from .conftest import select_in_editor

DOC = "\n".join([
    "# 手順",            # 0
    "",                  # 1
    "前の段落です。",    # 2
    "",                  # 3
    "```mermaid",        # 4
    "flowchart LR",      # 5
    "  A[校正開始] --> B[AIが修正稿を出す]",  # 6
    "```",               # 7
    "",                  # 8
    "後ろの段落です。",  # 9
    "",                  # 10
    "```python",         # 11
    "print('mermaid ではない')",  # 12
    "```",               # 13
])

# 閉じフェンスの直後に本文が続く文書（段落境界の削除を図の隣で試す）
DOC_TIGHT = "```mermaid\nflowchart LR\n  A --> B\n```\n後ろの段落"

WIDGET = "#editor .mmd-widget"
SETTLED = ('#editor .mmd-widget[data-state="ok"], '
           '#editor .mmd-widget[data-state="error"]')


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

    def _open(text: str = "", title: str = "Mermaid E2E",
              sid: str | None = None) -> Doc:
        """text からセッションを作って開く。sid を渡せば、その既存のセッションを開く。"""
        session = {"id": sid} if sid else storage.create_session(
            text, title, "/tmp/mmd")
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


def settle(doc: Doc, count: int = 1) -> None:
    """図が count 個になり、すべて描き終える（読み込み中が無くなる）まで待つ。"""
    expect(doc.page.locator(WIDGET)).to_have_count(count, timeout=30000)
    expect(doc.page.locator('#editor .mmd-widget[data-state="loading"]')
           ).to_have_count(0, timeout=30000)
    expect(doc.page.locator(SETTLED)).to_have_count(count)

def svg_of(doc: Doc, n: int = 0) -> str:
    """n 番目の図の SVG 本文（<img> の data: URL を戻したもの）。"""
    src = doc.page.evaluate(
        """(n) => {
            const img = document.querySelectorAll('#editor .mmd-widget')[n]
                .querySelector('img');
            return img ? img.src : '';
        }""", n)
    assert src.startswith("data:image/svg+xml"), src[:80]
    return unquote(src.split(",", 1)[1])


def place_caret(doc: Doc, needle: str, after: bool = True) -> None:
    """needle を含むテキストノードの、needle の後ろ（前）にキャレットを置く。"""
    ok = doc.page.evaluate(
        """([needle, after]) => {
            const ed = document.getElementById('editor');
            const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
            let n;
            while ((n = w.nextNode())) {
                if (n.parentElement.closest('.mmd-widget, .pb')) continue;
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


def wait_svg(doc: Doc, pred, n: int = 0, timeout: float = 15.0) -> str:
    """n 番目の図の SVG が pred を満たすまで待ち、その SVG を返す。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            svg = svg_of(doc, n)
        except AssertionError:  # まだ画像が無い（描き直し中・エラー表示中）
            svg = ""
        if svg and pred(svg):
            return svg
        doc.page.wait_for_timeout(100)
    raise AssertionError("図が期待の内容にならなかった")


def saved_feedback(doc: Doc) -> dict:
    expect(doc.page.locator("#save-state")).to_have_text("保存済み", timeout=8000)
    return feedback.build_feedback(storage.load_session(doc.sid))


def csp_messages(doc: Doc) -> list[str]:
    return [m for m in doc.console
            if "Content Security Policy" in m or "Refused to" in m]


# ---------- 描画 ----------
def test_diagram_is_rendered_after_closing_fence(open_doc):
    """日本語ラベルの図が、閉じフェンスの直後に出る。mermaid 以外のブロックには出ない。"""
    doc = open_doc(DOC)
    settle(doc, 1)
    page = doc.page
    # 図は閉じフェンス（```）の段落の直後。前の段落の兄弟として挟まる
    expect(page.locator("#editor p.md-fence + .mmd-widget")).to_have_count(1)
    prev_text = page.evaluate(
        "() => document.querySelector('#editor .mmd-widget')"
        ".previousElementSibling.textContent")
    assert prev_text == "```"
    svg = svg_of(doc)
    assert "校正開始" in svg and "AIが修正稿を出す" in svg
    # 実際に画像として読み込めて、大きさがある（文字化けや空の画像ではない）
    natural = page.evaluate(
        "() => { const i = document.querySelector('#editor .mmd-widget img');"
        " return [i.naturalWidth, i.naturalHeight]; }")
    assert natural[0] > 100 and natural[1] > 20
    assert csp_messages(doc) == []
    assert [m for m in doc.console if m.startswith(("error", "pageerror"))] == []


DIAGRAMS = {
    "sequence": "sequenceDiagram\n  participant 人間\n  participant AI\n"
                "  人間->>AI: 校正結果を渡す\n  AI-->>人間: 修正稿を返す",
    "class": "classDiagram\n  class 文書 {\n    +String 題名\n    +校正()\n  }\n"
             "  文書 <|-- 記事",
    "state": "stateDiagram-v2\n  [*] --> 校正中\n  校正中 --> 校正完了: 完了",
    "er": "erDiagram\n  CUSTOMER ||--o{ ORDER : places",
    "gantt": "gantt\n  title 作業計画\n  dateFormat YYYY-MM-DD\n  section 設計\n"
             "  要件 :a1, 2026-10-01, 5d",
    "pie": 'pie title 内訳\n  "A" : 40\n  "B" : 60',
    "mindmap": "mindmap\n  root((中心))\n    枝1\n    枝2",
}


def test_main_diagram_types_render_without_csp_violations(open_doc):
    """主要な図種がすべて描ける。隔離フレームの厳しいCSPで eval や外部読み込みに頼らない。"""
    text = "\n\n".join(f"```mermaid\n{src}\n```" for src in DIAGRAMS.values())
    doc = open_doc(text)
    settle(doc, len(DIAGRAMS))
    states = doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .mmd-widget'))"
        ".map((w) => [w.dataset.state, w.dataset.error || ''])")
    assert states == [["ok", ""]] * len(DIAGRAMS), states
    for i in range(len(DIAGRAMS)):
        assert svg_of(doc, i).startswith("<svg")
    assert csp_messages(doc) == []
    # 図が増えても、描画用のフレームは1つ
    expect(doc.page.locator("iframe.mmd-frame")).to_have_count(1)

def test_only_closed_mermaid_fences_become_diagrams(open_doc):
    """~~~ や大文字の言語名も図になる。mermaid 以外・空・閉じていないものは図にしない。"""
    text = "\n".join([
        "```mermaid", "graph TD", "  A-->B", "```",        # 図1
        "~~~mermaid", "graph TD", "  C-->D", "~~~",        # 図2
        "```Mermaid title", "graph TD", "  E-->F", "```",  # 図3（大文字・属性つき）
        "```python", "x = 1", "```",                        # 図にしない
        "```mermaid", "```",                                # 空: 図にしない
        "```mermaid", "graph TD", "  G-->H",                # 閉じていない
    ])
    doc = open_doc(text)
    settle(doc, 3)
    anchors = doc.page.evaluate(
        "() => Array.from(document.querySelectorAll('#editor .mmd-widget'))"
        ".map((w) => w.previousElementSibling.textContent)")
    assert anchors == ["```", "~~~", "```"]


def test_no_frame_or_library_without_mermaid(open_doc):
    """Mermaid の無い文書では、隔離フレームも 5MB のライブラリも読み込まない。"""
    doc = open_doc("普通の文章です。\n\n```python\nx = 1\n```")
    doc.page.wait_for_timeout(1500)
    expect(doc.page.locator("iframe.mmd-frame")).to_have_count(0)
    expect(doc.page.locator(WIDGET)).to_have_count(0)
    assert not [u for u in doc.requests if "mermaid" in u.split("/")[-1]
                and not u.endswith("mermaid-view.js")]


# ---------- 図は本文ではない ----------
def test_diagram_never_enters_saved_text(open_doc):
    """保存・result_text・original_text に図は混ざらず、原文と完全一致する。"""
    doc = open_doc(DOC)
    settle(doc)
    saved = doc.page.evaluate("() => Tracker.html()")
    assert "mmd-widget" not in saved and "<img" not in saved
    # 保存を1回走らせる（編集を挟まず、いまのエディタHTMLをそのまま送る）
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
    assert "mmd-widget" not in stored and "<div" not in stored
    fb = feedback.build_feedback(storage.load_session(doc.sid))
    assert fb["original_text"] == DOC
    assert fb["result_text"] == DOC
    assert fb["changes"] == []


def test_reopened_document_does_not_duplicate_diagram(open_doc, browser, web_port):
    """編集済みの文書を開き直しても、図が二重にならない（保存HTMLに図が無い）。"""
    doc = open_doc(DOC)
    settle(doc)
    place_caret(doc, "前の段落です")
    doc.page.keyboard.type("！")
    saved_feedback(doc)
    page2 = doc.page.context.new_page()
    page2.goto(f"http://127.0.0.1:{web_port}/session/{doc.sid}")
    page2.wait_for_selector("#editor p ins")
    expect(page2.locator(WIDGET)).to_have_count(1)
    expect(page2.locator(SETTLED)).to_have_count(1, timeout=30000)


# ---------- 編集への追従 ----------
def test_editing_source_redraws_diagram_and_reuses_widget(open_doc):
    """ソースの編集が、止まってから図に反映される。ウィジェットは作り直さない。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.evaluate(
        "() => { document.querySelector('#editor .mmd-widget').__mark = 7; }")
    before = svg_of(doc)
    assert "ZZZ" not in before
    place_caret(doc, "AIが修正稿を出す")
    page.keyboard.type("ZZZ")
    expect(page.locator("#editor p ins")).to_have_text("ZZZ")
    wait_svg(doc, lambda s: "ZZZ" in s)
    settle(doc)
    assert page.evaluate(
        "() => document.querySelector('#editor .mmd-widget').__mark") == 7
    fb = saved_feedback(doc)
    assert "AIが修正稿を出すZZZ]" in fb["result_text"]
    # 図は本文ではないので、行数も変わらない
    assert len(fb["result_text"].split("\n")) == len(DOC.split("\n"))


def test_deleted_source_text_is_not_drawn(open_doc):
    """削除（赤い斜線）にした文字は図に出ない = 図は「今の結果」の文字列から描く。"""
    doc = open_doc(DOC)
    settle(doc)
    place_caret(doc, "AIが修正稿を出す")
    for _ in range(3):  # 「出す」の「す」「出」と、その前の「を」を消す
        doc.page.keyboard.press("Backspace")
    expect(doc.page.locator("#editor p del")).to_have_count(3)
    svg = wait_svg(doc, lambda s: "AIが修正稿を出す" not in s)
    settle(doc)
    assert "AIが修正稿" in svg


def test_syntax_error_shows_message_and_editor_survives(open_doc):
    """構文エラーは図の代わりにエラーを出す。エディタは壊れず、直せば図に戻る。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    original = svg_of(doc)
    place_caret(doc, "AIが修正稿を出す]")
    page.keyboard.type("[[[")
    expect(page.locator('#editor .mmd-widget[data-state="error"]')).to_have_count(
        1, timeout=15000)
    message = page.evaluate(
        "() => document.querySelector('#editor .mmd-widget').dataset.error")
    assert "Parse error" in message or "Expecting" in message
    expect(page.locator("#editor .mmd-widget img")).to_have_count(0)
    # 文言は疑似要素で出す。ウィジェットはテキストノードを持たない
    assert page.evaluate(
        "() => document.querySelector('#editor .mmd-widget').textContent") == ""
    # 続けて編集できる（キャレットは文書の先頭に飛ばない）
    page.keyboard.type("x")
    expect(page.locator("#editor p ins")).to_have_text("[[[x")
    for _ in range(4):  # 自分の追記の取り消しは痕跡なく消える
        page.keyboard.press("Backspace")
    expect(page.locator("#editor p ins")).to_have_count(0)
    expect(page.locator('#editor .mmd-widget[data-state="ok"]')).to_have_count(
        1, timeout=15000)
    assert svg_of(doc) == original
    assert saved_feedback(doc)["result_text"] == DOC


def test_unrelated_edit_does_not_touch_diagram(open_doc):
    """図と関係ない段落を編集しても、図は同じウィジェット・同じ画像のまま。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.evaluate(
        "() => { document.querySelector('#editor .mmd-widget').__mark = 9; }")
    src = page.evaluate(
        "() => document.querySelector('#editor .mmd-widget img').src")
    place_caret(doc, "前の段落です")
    page.keyboard.type("！")
    expect(page.locator("#editor p ins")).to_have_text("！")
    page.wait_for_timeout(1200)  # 待ち時間（400ms）を十分に越える
    assert page.evaluate(
        "() => document.querySelector('#editor .mmd-widget').__mark") == 9
    assert page.evaluate(
        "() => document.querySelector('#editor .mmd-widget img').src") == src


def count_renders(doc: Doc) -> None:
    """MermaidView.render の呼び出しを数え始める（キャッシュで済んだかの確認用）。"""
    doc.page.evaluate(
        """() => {
            window.__renders = 0;
            const orig = MermaidView.render;
            MermaidView.render = (...a) => { window.__renders++; return orig(...a); };
        }""")


def renders(doc: Doc) -> int:
    return doc.page.evaluate("() => window.__renders")


# ---------- undo / redo / 配色 ----------
IMMEDIATE_STATE = (
    "() => Array.from(document.querySelectorAll('#editor .mmd-widget'))"
    ".map((w) => w.dataset.state)")


def test_undo_redo_restore_diagram_from_cache(open_doc):
    """undo/redo は innerHTML を差し替えるが、図はすぐ戻る（描き直しも空の枠も無い）。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    original = svg_of(doc)
    place_caret(doc, "AIが修正稿を出す")
    page.keyboard.type("Z")  # 1文字ごとに undo のスナップショットが1つできる
    edited = wait_svg(doc, lambda s: "出すZ" in s)
    settle(doc)
    count_renders(doc)
    page.keyboard.press("ControlOrMeta+z")
    # キー入力が返った直後に、待たずに図が揃っている（debounce を待たない）
    assert page.evaluate(IMMEDIATE_STATE) == ["ok"]
    expect(page.locator("#editor p ins")).to_have_count(0)
    assert svg_of(doc) == original
    page.keyboard.press("ControlOrMeta+Shift+z")
    assert page.evaluate(IMMEDIATE_STATE) == ["ok"]
    expect(page.locator("#editor p ins")).to_have_text("Z")
    assert svg_of(doc) == edited
    assert renders(doc) == 0  # どちらもキャッシュから戻した
    expect(page.locator(WIDGET)).to_have_count(1)
    assert saved_feedback(doc)["result_text"].count("\n") == DOC.count("\n")


def test_theme_switch_redraws_in_dark_and_back(open_doc):
    """配色を切り替えると図もダーク用に描き直され、戻せばライトの図が（キャッシュから）戻る。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    light = svg_of(doc)
    light_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('#editor .mmd-widget'))"
        ".backgroundColor")
    page.evaluate("() => HleTheme.save('dark')")
    dark = wait_svg(doc, lambda s: s != light)
    settle(doc)
    dark_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('#editor .mmd-widget'))"
        ".backgroundColor")
    assert dark_bg != light_bg
    assert dark != light
    count_renders(doc)
    page.evaluate("() => HleTheme.save('light')")
    wait_svg(doc, lambda s: s == light)
    assert renders(doc) == 0
    expect(page.locator("iframe.mmd-frame")).to_have_count(1)
    assert csp_messages(doc) == []


# ---------- コメントと段落境界 ----------
def test_comment_on_source_text_excludes_diagram(open_doc):
    """ソースの文字列へのコメントは今までどおり付き、quote に図は混ざらない。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    before = svg_of(doc)
    select_in_editor(page, 5, 0, 9)  # 「flowchart」
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    page.fill("#draft-text", "向きは縦にしたい")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    fb = saved_feedback(doc)
    assert [c["quote"] for c in fb["comments"]] == ["flowchart"]
    assert fb["result_text"] == DOC
    expect(page.locator("#editor span.cmt")).to_have_text("flowchart")
    settle(doc)
    assert svg_of(doc) == before


def test_backspace_after_diagram_joins_lines_and_diagram_follows(open_doc):
    """図は <div> として段落の間に挟まるが、Backspace は閉じフェンスの行との境界を消す。

    閉じフェンスの行に次の行がつながると、その行はもう閉じフェンスではない。
    図は「今の本文」に追従して消える。
    """
    doc = open_doc(DOC_TIGHT)
    settle(doc)
    page = doc.page
    place_caret(doc, "後ろの段落", after=False)
    page.keyboard.press("Backspace")
    expect(page.locator("#editor p.md-fence").nth(1).locator("del.pb")
           ).to_have_count(1)
    fb = saved_feedback(doc)
    assert fb["result_text"] == "```mermaid\nflowchart LR\n  A --> B\n```後ろの段落"
    expect(page.locator(WIDGET)).to_have_count(0, timeout=10000)


def test_enter_at_closing_fence_keeps_diagram_next_to_fence(open_doc):
    """閉じフェンスの末尾で Enter しても、図はフェンスの直後に戻る（新しい行の後ろには行かない）。"""
    doc = open_doc(DOC_TIGHT)
    settle(doc)
    page = doc.page
    page.evaluate(
        """() => {
            const p = document.querySelectorAll('#editor p')[3];
            const r = document.createRange();
            r.selectNodeContents(p);
            r.collapse(false);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            document.getElementById('editor').focus();
        }""")
    page.keyboard.press("Enter")
    expect(page.locator("#editor p")).to_have_count(6)
    expect(page.locator("#editor p.md-fence + .mmd-widget")).to_have_count(
        1, timeout=10000)
    order = page.evaluate(
        "() => Array.from(document.getElementById('editor').children)"
        ".map((c) => c.tagName + (c.classList.contains('mmd-widget') ? '.w' : ''))")
    assert order == ["P", "P", "P", "P", "DIV.w", "P", "P"]
    fb = saved_feedback(doc)
    assert fb["result_text"] == DOC_TIGHT.replace("```\n後", "```\n\n後")


# ---------- 悪意あるソースと隔離 ----------
EVIL = [
    # ラベルに生の HTML（イベントハンドラ・script）
    'flowchart LR\n  A["<img src=x onerror=window.__pwned=1>"] --> B',
    "flowchart LR\n  A[<script>window.__pwned=1</script>] --> B",
    'flowchart LR\n  A["<a href=\'javascript:window.__pwned=1\'>x</a>"] --> B',
    # click によるリンクとコールバック
    'flowchart LR\n  A --> B\n  click A href "javascript:window.__pwned=1"',
    "flowchart LR\n  A --> B\n  click A call window.__pwned()",
    # classDef でスタイルを抜け出す試み
    ("stateDiagram-v2\n  classDef x fill:#f00}</style>"
     "<img src=x onerror=window.__pwned=1><style>\n  [*] --> A"),
    "sequenceDiagram\n  A->>B: <img src=x onerror=window.__pwned=1>",
    # 設定の上書きで securityLevel を緩める試み
    ('%%{init: {"securityLevel": "loose", "theme": "base"}}%%\n'
     'flowchart LR\n  A["<img src=x onerror=window.__pwned=1>"] --> B\n'
     '  click A href "javascript:window.__pwned=1"'),
]


def dangerous_parts(svg: str) -> list[str]:
    """SVG を XML として読み、実行や外部読み込みになりうる要素・属性を挙げる。

    ラベルに書いた `<img onerror=...>` が文字としてエスケープされて残るのは
    無害（<text> の中の文字）なので、文字列の検索ではなく構造で見る。
    """
    bad = []
    for el in ET.fromstring(svg).iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "iframe", "object", "embed"}:
            bad.append(f"<{tag}>")
        for key, value in el.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                bad.append(f"{tag}@{name}")
            is_data_image = tag in {"use", "image", "img"} and value.startswith("data:")
            if (name in {"href", "src", "action", "formaction"}
                    and not is_data_image
                    and re.match(r"\s*(javascript|vbscript|data|https?|//)",
                                 value, re.IGNORECASE)):
                bad.append(f"{tag}@{name}={value[:40]}")
    return bad


def test_malicious_sources_do_not_execute_or_leak_dangerous_markup(open_doc):
    text = "\n\n".join(f"```mermaid\n{src}\n```" for src in EVIL)
    doc = open_doc(text)
    settle(doc, len(EVIL))
    page = doc.page
    # エディタのページでも、隔離フレームの中でも、注入したコードは動いていない
    assert page.evaluate("() => window.__pwned") is None
    for frame in page.frames:
        assert frame.evaluate("() => window.__pwned") is None
    # 図として受け取った SVG には、実行できる要素・属性・リンクが残らない
    for i in range(len(EVIL)):
        state = page.evaluate(
            "(i) => document.querySelectorAll('#editor .mmd-widget')[i]"
            ".dataset.state", i)
        if state == "ok":
            assert dangerous_parts(svg_of(doc, i)) == [], (i, EVIL[i])
    # エディタ本文に <a href=javascript:> も外部読み込みも入っていない
    assert page.evaluate(
        "() => document.querySelectorAll('#editor a, #editor script, "
        "#editor iframe, #editor object, #editor [onerror]').length") == 0
    # フレームのCSPが止めたものは、ラベルに残る <img src="x"> の読み込みだけ
    # （Mermaid の strict は onerror を外すが <img> は残す）。インラインの
    # イベントハンドラの実行・eval・script といった拒否の通知は1件も無い
    leftovers = [m for m in csp_messages(doc)
                 if not re.match(r"error: Loading the image '[^']*/static/x' "
                                 r"violates .*\"img-src data:\"", m)]
    assert leftovers == []


def test_frame_is_isolated_from_the_editor_page(open_doc):
    """隔離フレームは不透明オリジン: エディタの DOM にも API にも届かない。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    assert csp_messages(doc) == []  # 以下の調べ方で出る CSP の通知と混ざらないように
    attrs = page.evaluate(
        "() => document.querySelector('iframe.mmd-frame').getAttribute('sandbox')")
    assert attrs == "allow-scripts"  # allow-same-origin を付けていない
    frames = [f for f in page.frames if f.url.endswith("/static/mermaid-frame.html")]
    assert len(frames) == 1
    frame = frames[0]
    result = frame.evaluate(
        """async () => {
            const out = { origin: self.origin };  // location.origin は URL 由来で "null" にならない
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


def test_mermaid_source_cannot_make_the_browser_fetch_anything(open_doc):
    """ラベルの <img> や CSS の url() で外へ取りに行かせても、通信は1件も起きない。

    本文は外部由来になりうる。Mermaid は <img src> をラベルに残すので、
    フレームのCSP（img-src data:）が止めていることを、受け側のサーバーで確かめる。
    """
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
        sources = [
            f'flowchart LR\n  A["<img src=\'{base}/label.png\'>"] --> B',
            (f"flowchart LR\n  A --> B\n  classDef x fill:#fff,"
             f"background-image:url({base}/css.png)\n  class A x"),
            f"flowchart LR\n  A --> B\n  style A fill:url({base}/style.png)",
        ]
        doc = open_doc("\n\n".join(f"```mermaid\n{s}\n```" for s in sources))
        settle(doc, len(sources))
        doc.page.wait_for_timeout(2000)
        assert hits == []
        assert doc.page.evaluate("() => window.__pwned") is None
    finally:
        server.shutdown()
        server.server_close()


def test_ai_diff_ghost_text_is_not_drawn(open_doc):
    """AI差分の幽霊（前の稿で消えた文字）は図に描かれず、図が増えたり保存に混ざったりもしない。"""
    rev2 = DOC.replace("AIが修正稿を出す", "AIが返信する")
    session = storage.create_session(DOC, "AI差分と図", "/tmp/mmd")
    storage.add_revision(session["id"], rev2)  # 第2稿 = AIがラベルを書き換えた稿
    doc = open_doc(sid=session["id"])
    settle(doc)
    assert "AIが返信する" in svg_of(doc)
    doc.page.locator("#btn-aidiff").click()
    expect(doc.page.locator("#editor .ai-del")).not_to_have_count(0)
    place_caret(doc, "前の段落です")
    doc.page.keyboard.type("！")  # 差分表示中の編集でも図は同期される
    expect(doc.page.locator("#editor p ins")).to_have_text("！")
    doc.page.wait_for_timeout(1000)
    expect(doc.page.locator(WIDGET)).to_have_count(1)
    svg = svg_of(doc)
    assert "AIが返信する" in svg and "修正稿" not in svg
    fb = saved_feedback(doc)
    assert fb["original_text"] == rev2
    html = storage.load_session(doc.sid)["revisions"][-1]["edited_html"]
    assert "mmd-widget" not in html and "ai-del" not in html


def test_click_on_diagram_puts_caret_next_to_it(open_doc):
    """図をクリックして続けて入力しても、文字は文書の先頭に飛ばず、図の隣に入る。"""
    doc = open_doc(DOC_TIGHT + "\n\n末尾")
    settle(doc)
    page = doc.page
    page.locator(WIDGET).click()
    page.keyboard.type("Q")
    ins = page.locator("#editor p ins")
    expect(ins).to_have_text("Q")
    # 直後の段落（図の次の行）の先頭に入る。先頭の段落（```mermaid）ではない
    parent = page.evaluate(
        "() => document.querySelector('#editor ins').parentElement.textContent")
    assert parent.startswith("Q後ろの段落")
    assert saved_feedback(doc)["result_text"].split("\n")[4] == "Q後ろの段落"


def test_select_all_and_delete_across_diagram(open_doc):
    """全選択して消しても壊れず、図は消え（ソースが無い）、原文は保たれる。"""
    doc = open_doc(DOC)
    settle(doc)
    page = doc.page
    page.locator("#editor p").first.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.press("Backspace")
    expect(page.locator(WIDGET)).to_have_count(0, timeout=10000)
    fb = saved_feedback(doc)
    assert fb["original_text"] == DOC
    assert fb["result_text"] == ""
    assert [m for m in doc.console if m.startswith(("pageerror",))] == []

"""HTMLレビューページのE2Eテスト。

sandbox付きiframeでの表示・範囲選択コメント・ハイライト・
対象HTML内スクリプトの遮断を実ブラウザで検証する。
"""

from __future__ import annotations

import base64
import re

import pytest
from playwright.sync_api import expect

from hle_mcp import storage

from ..conftest import PNG_1PX

PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()

HTML_DOC = """<!doctype html>
<html><head>
<style>#lead{color:rgb(200,0,0)}</style>
<script>window.__pwned=1;document.title='pwned';</script>
</head><body>
<h1>アーキテクチャ図</h1>
<p id="lead">これは重要な説明文です。</p>
<p id="sub">これは補足の説明文です。</p>
<svg viewBox="0 0 100 20"><text x="5" y="12">SVG内テキスト</text></svg>
<img id="fig" alt="構成図" width="60" height="40" src="__PNG__">
</body></html>""".replace("__PNG__", PNG_DATA_URL)


@pytest.fixture()
def html_review(browser, web_port):
    """HTMLモードのセッションを作成してレビューページを開く。"""
    session = storage.create_session(
        HTML_DOC, "HTML E2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/page.html")
    context = browser.new_context()
    page = context.new_page()
    messages: list[str] = []
    page.on("console", lambda msg: messages.append(msg.text))
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    # iframe内のドキュメントが読み込まれるまで待つ
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('#lead'));
        }"""
    )
    yield page, session["id"], messages
    context.close()


def select_in_frame(page, selector: str, start: int, end: int) -> None:
    """iframe内の要素のテキストノードに対して文字範囲選択を行う。"""
    page.evaluate(
        """([sel, s, e]) => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const node = doc.querySelector(sel).firstChild;
            const r = doc.createRange();
            r.setStart(node, s);
            r.setEnd(node, e);
            const selection = doc.getSelection();
            selection.removeAllRanges();
            selection.addRange(r);
            // selectionchangeの発火タイミングに依存しないよう明示的に流す
            doc.dispatchEvent(new Event('selectionchange'));
        }""",
        [selector, start, end],
    )


def add_comment(page, text: str) -> None:
    page.click("#btn-comment")
    expect(page.locator("#comment-draft")).to_be_visible()
    page.fill("#draft-text", text)
    page.click("#draft-submit")


def test_page_renders_with_styles_and_blocks_scripts(html_review):
    """インラインCSSは効き、対象HTML内のスクリプトは実行されない。"""
    page, _sid, messages = html_review
    color = page.evaluate(
        """() => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const win = document.getElementById('doc-frame').contentWindow;
            return win.getComputedStyle(
                doc.querySelector('#lead')).color;
        }"""
    )
    assert color == "rgb(200, 0, 0)"  # <style> が適用されている
    pwned = page.evaluate(
        "() => document.getElementById('doc-frame').contentWindow.__pwned")
    assert pwned is None  # sandboxによりスクリプトは動かない
    assert "pwned" not in page.title()
    assert page.title() == "HTML E2E - HTMLレビュー"


def test_comment_flow_with_context_capture(html_review):
    """範囲選択→コメントで quote と prefix/suffix が保存される。"""
    page, sid, _messages = html_review
    # 「これは重要な説明文です。」の「重要な説明」(3..8文字目)を選択
    select_in_frame(page, "#lead", 3, 8)
    add_comment(page, "ここは図の要点なのでもっと具体的に")
    expect(page.locator(".comment-card")).to_have_count(1)
    expect(page.locator(".comment-card .quote")).to_contain_text("重要な説明")

    s = storage.load_session(sid)
    c = s["comments"][0]
    assert c["quote"] == "重要な説明"
    assert c["text"] == "ここは図の要点なのでもっと具体的に"
    # 前後の文脈が保存されている（AIが位置特定に使う）
    assert c["prefix"].endswith("これは")
    assert c["suffix"].startswith("文です。")
    # 対象HTMLのDOMは書き換えられていない（base_textが元のまま）
    assert s["revisions"][0]["base_text"] == HTML_DOC


def test_comment_highlight_painted_and_removed(html_review):
    """コメント範囲はCSSハイライトで示され、削除で消える。"""
    page, _sid, _messages = html_review
    select_in_frame(page, "#sub", 3, 5)  # 「補足」
    add_comment(page, "この表現を変える")
    expect(page.locator(".comment-card")).to_have_count(1)
    count = page.evaluate(
        """() => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get('hle-comment');
            return h ? h.size : -1;
        }"""
    )
    assert count == 1
    page.on("dialog", lambda d: d.accept())
    page.click(".comment-card button[data-del]")
    expect(page.locator(".comment-card")).to_have_count(0)
    count = page.evaluate(
        """() => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get('hle-comment');
            return h ? h.size : 0;
        }"""
    )
    assert count == 0


def test_svg_text_selection(html_review):
    """インラインSVG内のテキストにもコメントを付けられる。"""
    page, sid, _messages = html_review
    page.evaluate(
        """() => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const node = doc.querySelector('svg text').firstChild;
            const r = doc.createRange();
            r.setStart(node, 0);
            r.setEnd(node, node.data.length);
            const selection = doc.getSelection();
            selection.removeAllRanges();
            selection.addRange(r);
            doc.dispatchEvent(new Event('selectionchange'));
        }"""
    )
    add_comment(page, "ラベルを日本語に")
    expect(page.locator(".comment-card")).to_have_count(1)
    assert storage.load_session(sid)["comments"][0]["quote"] == "SVG内テキスト"


def test_reply_and_ai_polling(html_review):
    """返信の送信と、AI返信のポーリング反映が動く。"""
    page, sid, _messages = html_review
    select_in_frame(page, "#lead", 0, 3)  # 「これは」
    add_comment(page, "冒頭を変えたい")
    expect(page.locator(".comment-card")).to_have_count(1)
    cid = storage.load_session(sid)["comments"][0]["id"]
    storage.add_reply(sid, cid, "了解です。修正します", author="ai")
    expect(page.locator(".comment-card .reply .who.ai")).to_have_count(
        1, timeout=8000)


def test_no_csp_violations(html_review):
    """レビューページとiframe内ドキュメントでCSP違反が出ていない。"""
    page, _sid, messages = html_review
    select_in_frame(page, "#lead", 3, 8)
    add_comment(page, "チェック用")
    expect(page.locator(".comment-card")).to_have_count(1)
    page.wait_for_timeout(300)
    violations = [m for m in messages if "Content Security Policy" in m]
    assert violations == []


# ---------- 画像・図へのコメント ----------

def frame_attr(page, selector: str, attr: str):
    """iframe内の要素の属性値を読む。"""
    return page.evaluate(
        """([sel, a]) => {
            const doc = document.getElementById('doc-frame').contentDocument;
            const el = doc.querySelector(sel);
            return el ? el.getAttribute(a) : null;
        }""",
        [selector, attr],
    )


def click_in_frame(page, selector: str) -> None:
    """iframe内の要素をクリックする。"""
    page.frame_locator("#doc-frame").locator(selector).click()


def test_svg_figure_can_be_commented(html_review):
    """テキストで選べない図（SVG）自体にコメントを付けられる。"""
    page, sid, _messages = html_review
    click_in_frame(page, "svg")
    page.click("#btn-comment")
    expect(page.locator("#draft-quote")).to_have_text("🖼 図（SVG）")
    page.fill("#draft-text", "この図の配色を本文と揃えて")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    expect(page.locator(".comment-card .quote")).to_contain_text("図（SVG）")

    s = storage.load_session(sid)
    c = s["comments"][0]
    assert c["target"] == "image"
    assert c["image_ref"] == "svg:0"
    assert c["text"] == "この図の配色を本文と揃えて"
    # 対象HTMLは書き換えられていない（属性はブラウザ上の表示だけ）
    assert s["revisions"][0]["base_text"] == HTML_DOC


def test_img_comment_uses_alt_as_label(html_review):
    """imgはalt（なければファイル名）を対象の名前として使う。"""
    page, sid, _messages = html_review
    click_in_frame(page, "#fig")
    page.click("#btn-comment")
    expect(page.locator("#draft-quote")).to_have_text("🖼 構成図")
    page.fill("#draft-text", "画像を新しいものに差し替えて")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    c = storage.load_session(sid)["comments"][0]
    assert c["image"] == "構成図"
    assert c["image_ref"] == "img:0"


def test_image_comment_outline_restored_after_reload(html_review):
    """画像コメントのアンカーは開き直しても解決される。"""
    page, _sid, _messages = html_review
    click_in_frame(page, "#fig")
    add_comment(page, "枠線が復元されるか")
    expect(page.locator(".comment-card")).to_have_count(1)
    page.reload()
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('#fig[data-hle-obj]'));
        }"""
    )
    assert frame_attr(page, "#fig", "data-hle-obj") in ("on", "active")


def test_clicking_commented_image_activates_card(html_review):
    """コメント付き画像をクリックすると対応するカードが開く。"""
    page, _sid, _messages = html_review
    click_in_frame(page, "#fig")
    add_comment(page, "画像への指摘")
    expect(page.locator(".comment-card")).to_have_count(1)
    select_in_frame(page, "#sub", 3, 5)  # 別の対象を選び直す
    click_in_frame(page, "#fig")
    assert "active" in (
        page.locator(".comment-card").first.get_attribute("class") or "")
    # クリック直後は「選択中」なので、mdの画像選択と同じく選択枠を優先する
    assert frame_attr(page, "#fig", "data-hle-obj") == "sel"


def test_deleting_image_comment_removes_outline(html_review):
    """画像コメントを削除すると枠線も消える。"""
    page, _sid, _messages = html_review
    click_in_frame(page, "#fig")
    add_comment(page, "あとで消す")
    expect(page.locator(".comment-card")).to_have_count(1)
    page.on("dialog", lambda d: d.accept())
    page.locator(".comment-card button[data-del]").click()
    expect(page.locator(".comment-card")).to_have_count(0)
    assert frame_attr(page, "#fig", "data-hle-obj") is None


def test_text_comment_still_works_after_image_selection(html_review):
    """画像を選んだあとでも、文字列選択のコメントに切り替えられる。"""
    page, sid, _messages = html_review
    click_in_frame(page, "#fig")
    select_in_frame(page, "#lead", 3, 8)  # 「重要な説明」
    add_comment(page, "文字列への指摘")
    c = storage.load_session(sid)["comments"][0]
    assert c["quote"] == "重要な説明"
    assert "target" not in c  # テキストコメントは従来の形のまま


# ---------- 図（インラインSVG）の中の文字への実際のマウス操作 ----------
# 上の test_svg_text_selection は選択をスクリプトで作るので click が起きない。
# 実際にドラッグすると、離したときに図の click が届き、図全体が選ばれて
# 選んだ文字列が捨てられていた（全コメントの quote が図の名前になる）。
# ここではブラウザの入力経路を通したドラッグで、その経路を検証する。

DIAGRAM_DOC = """<!doctype html>
<html><head><style>
body{margin:0;font-family:sans-serif}
.t{font:600 28px sans-serif;fill:#2d3142}
</style></head><body>
<p id="lead">図の前の段落です。</p>
<svg id="fig" viewBox="0 0 800 300" width="800" height="300"
     role="img" aria-label="構成図 v1">
<rect x="20" y="20" width="360" height="160" fill="#fde"/>
<text id="l1" class="t" x="40" y="90">左の箱のラベル</text>
<text id="l2" class="t" x="420" y="90">右の箱のラベル</text>
<text id="l3" class="t" x="40" y="250">下のラベル</text>
</svg>
</body></html>"""


@pytest.fixture()
def diagram_review(browser, web_port):
    """図解のHTMLセッションを作成してレビューページを開く。"""
    session = storage.create_session(
        DIAGRAM_DOC, "図解 E2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/diagram.html")
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('#l3'));
        }"""
    )
    yield page, session["id"]
    context.close()


def _mouse(cdp, kind: str, x: float, y: float, pressed: bool) -> None:
    cdp.send("Input.dispatchMouseEvent", {
        "type": kind, "x": x, "y": y, "clickCount": 1,
        "button": "left" if pressed or kind != "mouseMoved" else "none",
        "buttons": 1 if pressed else 0,
    })


def real_drag(page, x0: float, y0: float, x1: float, y1: float,
              steps: int = 8) -> None:
    """ブラウザの入力経路を通して、押す→動かす→離すのドラッグをする。

    Playwright の page.mouse はボタンを押したまま動かすと、ドラッグの開始を
    調べるために全フレームでスクリプトを評価する。スクリプトを許可しない
    sandbox の iframe があるとその評価が返らず、move が固まる。そこで CDP の
    Input.dispatchMouseEvent で入力を直接送る。ブラウザ側では実際のマウス
    操作と同じに扱われ、文字の選択と click イベントが起きる。
    """
    cdp = page.context.new_cdp_session(page)
    try:
        _mouse(cdp, "mouseMoved", x0, y0, False)
        _mouse(cdp, "mousePressed", x0, y0, True)
        for i in range(1, steps + 1):
            _mouse(cdp, "mouseMoved", x0 + (x1 - x0) * i / steps,
                   y0 + (y1 - y0) * i / steps, True)
        _mouse(cdp, "mouseReleased", x1, y1, False)
    finally:
        cdp.detach()


def real_click(page, x: float, y: float) -> None:
    """ブラウザの入力経路を通して、動かさずに押して離す。"""
    cdp = page.context.new_cdp_session(page)
    try:
        _mouse(cdp, "mouseMoved", x, y, False)
        _mouse(cdp, "mousePressed", x, y, True)
        _mouse(cdp, "mouseReleased", x, y, False)
    finally:
        cdp.detach()


def char_box(page, selector: str, start: int, end: int) -> dict:
    """iframe内の文字 start〜end の矩形を、ページ全体の座標で返す。"""
    return page.evaluate(
        """([sel, s, e]) => {
            const f = document.getElementById('doc-frame');
            const doc = f.contentDocument;
            const node = doc.querySelector(sel).firstChild;
            const r = doc.createRange();
            r.setStart(node, s);
            r.setEnd(node, e);
            const b = r.getBoundingClientRect();
            const fb = f.getBoundingClientRect();
            return {x: fb.left + b.left, y: fb.top + b.top,
                    w: b.width, h: b.height};
        }""",
        [selector, start, end],
    )


def drag_over_chars(page, selector: str, start: int, end: int) -> None:
    """文字 start〜end の上を左から右へドラッグして選ぶ。"""
    b = char_box(page, selector, start, end)
    y = b["y"] + b["h"] / 2
    real_drag(page, b["x"] + 1, y, b["x"] + b["w"] - 1, y)


def test_dragging_svg_text_comments_the_text_not_the_figure(diagram_review):
    """図の中の文字をドラッグで選ぶと、その文字列へのコメントになる。"""
    page, sid = diagram_review
    drag_over_chars(page, "#l1", 0, 4)  # 「左の箱の」
    page.click("#btn-comment")
    expect(page.locator("#draft-quote")).to_have_text("左の箱の")
    page.fill("#draft-text", "箱の名前を正式名称に")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)

    c = storage.load_session(sid)["comments"][0]
    assert c["quote"] == "左の箱の"
    assert "target" not in c  # 図全体（target="image"）にはならない
    # 前後の文脈には要素の間の改行も入る（Range.toString() と同じ連結）
    assert "図の前の段落です。" in c["prefix"]
    assert c["suffix"].startswith("ラベル")
    # 図の中の文字にもハイライトが付き、図は選択枠で囲まれない
    count = page.evaluate(
        """() => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get('hle-comment');
            return h ? h.size : -1;
        }"""
    )
    assert count == 1
    assert frame_attr(page, "#fig", "data-hle-obj") is None


def test_drag_across_svg_labels_keeps_whole_selection(diagram_review):
    """複数のラベルにまたがるドラッグも、選んだ文字列のまま残る。"""
    page, sid = diagram_review
    a = char_box(page, "#l1", 2, 3)  # 「箱」
    b = char_box(page, "#l2", 2, 3)  # 右のラベルの「箱」
    real_drag(page, a["x"] + 1, a["y"] + a["h"] / 2,
              b["x"] + b["w"] - 1, b["y"] + b["h"] / 2)
    page.click("#btn-comment")
    page.fill("#draft-text", "ラベルの書き方を揃える")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    c = storage.load_session(sid)["comments"][0]
    assert "target" not in c
    assert c["quote"].startswith("箱のラベル")
    assert c["quote"].endswith("右の箱")


def test_click_on_commented_svg_text_opens_card_else_selects_figure(
        diagram_review):
    """図の中のコメント済み文字列をクリックするとカードが開き、
    文字のない所をクリックすると従来どおり図が選ばれる。"""
    page, _sid = diagram_review
    drag_over_chars(page, "#l2", 0, 3)  # 「右の箱」
    page.click("#btn-comment")
    page.fill("#draft-text", "右の説明")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    card = page.locator(".comment-card").first
    assert "active" not in (card.get_attribute("class") or "")

    # ドラッグの選択が残っていると「選択の上のクリック」になるので外しておく
    page.evaluate("""() => document.getElementById('doc-frame')
        .contentDocument.getSelection().removeAllRanges()""")
    b = char_box(page, "#l2", 1, 2)
    real_click(page, b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)
    expect(card).to_have_class(re.compile(r"\bactive\b"))
    assert frame_attr(page, "#fig", "data-hle-obj") is None

    # 文字のない所（図の右下の余白）は図そのものの選択になる
    fig = page.evaluate(
        """() => {
            const f = document.getElementById('doc-frame');
            const r = f.contentDocument.querySelector('#fig')
              .getBoundingClientRect();
            const fb = f.getBoundingClientRect();
            return {x: fb.left + r.right - 20, y: fb.top + r.bottom - 20};
        }"""
    )
    real_click(page, fig["x"], fig["y"])
    page.wait_for_function(
        """() => document.getElementById('doc-frame').contentDocument
            .querySelector('#fig').getAttribute('data-hle-obj') === 'sel'""")
    page.click("#btn-comment")
    expect(page.locator("#draft-quote")).to_have_text("🖼 構成図 v1")

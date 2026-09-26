"""HTMLレビューの範囲指定モードのE2Eテスト。

ドラッグで囲んだ範囲にコメントを付け、その範囲のスクリーンショットが
実際の表示と同じ見た目で保存されること、枠が図に付いていくこと、
カードと枠が連動することを実ブラウザで検証する。マウス操作は
ブラウザの入力経路を通す（conftest.real_drag の説明を参照）。
"""

from __future__ import annotations

import base64
import re

import pytest
from playwright.sync_api import expect

from hle_mcp import storage

from ..conftest import make_png
from .conftest import real_click, real_drag, real_wheel

GREEN_PNG = "data:image/png;base64," + base64.b64encode(
    make_png(4, 3, (0, 150, 0))).decode()

# 幅で切り替わる CSS（@media）と rem を含む。スクリーンショットが表示と
# 同じになるかを、これらが効いた状態で確かめる
REGION_DOC = """<!doctype html>
<html><head><style>
html{font-size:18px}
body{margin:0;background:#f5f5f5;font-family:sans-serif;color:#2d3142}
p{margin:0;padding:1rem 24px;font-size:1.1rem}
svg{width:100%;height:auto;display:block}
.t{font:600 40px sans-serif;fill:#2d3142}
@media (max-width:700px){#lead{background:rgb(0,128,0);color:#fff;padding:2rem}}
</style></head><body>
<p id="lead">これは図の前の段落です。</p>
<svg id="fig" viewBox="0 0 1000 600" role="img" aria-label="構成図 v1">
<rect x="40" y="40" width="420" height="240" fill="#ffd9d9"/>
<text id="l1" class="t" x="70" y="170">左上の箱</text>
<rect x="540" y="40" width="420" height="240" fill="#d9e8ff"/>
<text id="l2" class="t" x="570" y="170">右上の箱</text>
<text id="l3" class="t" x="70" y="470">下のラベル</text>
<image href="__PNG__" x="600" y="380" width="160" height="120"/>
</svg>
<p id="tail" style="height:1400px">図の後ろの長い段落です。</p>
</body></html>""".replace("__PNG__", GREEN_PNG)


@pytest.fixture()
def open_review(browser, web_port):
    """範囲指定のテスト用にHTMLセッションを作り、レビューページを開く。"""
    contexts = []

    def _open(width: int = 1400, height: int = 900, dpr: float = 1,
              doc: str = REGION_DOC, ready: str = "#l3", on_browser=None):
        session = storage.create_session(
            doc, "範囲 E2E", "/tmp/e2e", mode="html",
            source_path="/tmp/e2e/region.html")
        context = (on_browser or browser).new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=dpr)
        contexts.append(context)
        page = context.new_page()
        messages: list[str] = []
        page.on("console", lambda msg: messages.append(msg.text))
        page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
        page.wait_for_function(
            """(sel) => {
                const f = document.getElementById('doc-frame');
                return !!(f && f.contentDocument &&
                          f.contentDocument.querySelector(sel));
            }""", arg=ready,
        )
        return page, session["id"], messages

    yield _open
    for c in contexts:
        try:
            c.close()
        except Exception:  # noqa: BLE001 （テスト側で閉じたブラウザのもの）
            pass


def frame_rect(page, selector: str) -> dict:
    """iframe内の要素の矩形を、ページ全体の座標で返す。"""
    return page.evaluate(
        """(sel) => {
            const f = document.getElementById('doc-frame');
            const b = f.contentDocument.querySelector(sel)
              .getBoundingClientRect();
            const fb = f.getBoundingClientRect();
            return {x: fb.left + b.left, y: fb.top + b.top,
                    w: b.width, h: b.height};
        }""",
        selector,
    )


def frame_box(page) -> dict:
    return page.locator("#doc-frame").bounding_box()


def frame_scroll(page, y: float) -> None:
    page.evaluate(
        "(y) => document.getElementById('doc-frame').contentWindow.scrollTo(0, y)",
        y)


def frame_scroll_y(page) -> float:
    return page.evaluate(
        "() => document.getElementById('doc-frame').contentWindow.scrollY")


def svg_to_page(page, vx: float, vy: float) -> tuple[float, float]:
    """図の座標系（viewBox 0 0 1000 600）の点を、ページ全体の座標に直す。"""
    r = frame_rect(page, "#fig")
    return r["x"] + vx * r["w"] / 1000, r["y"] + vy * r["h"] / 600


def draw(page, x: float, y: float, w: float, h: float) -> None:
    """範囲指定モードで、ページ座標の矩形をドラッグで囲む。"""
    real_drag(page, x, y, x + w, y + h)
    expect(page.locator("#comment-draft")).to_be_visible()
    page.wait_for_function(
        "() => { const i = document.querySelector('#draft-quote img');"
        " return !!(i && i.complete && i.naturalWidth > 0); }")


def submit(page, text: str, count: int) -> None:
    page.fill("#draft-text", text)
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(count)


def box_of(page, cid: str) -> dict:
    return page.locator(f'.region-box[data-cid="{cid}"]').bounding_box()


def compare_images(page, a_url: str, b_url: str) -> dict:
    """2枚の画像を画素で比べる（大きさ、平均の差、大きく違う画素の割合）。"""
    return page.evaluate(
        """async ([a, b]) => {
            const load = (src) => new Promise((ok, ng) => {
                const im = new Image();
                im.onload = () => ok(im);
                im.onerror = ng;
                im.src = src;
            });
            const [ia, ib] = await Promise.all([load(a), load(b)]);
            const w = ia.naturalWidth, h = ia.naturalHeight;
            if (w !== ib.naturalWidth || h !== ib.naturalHeight) {
                return {sizes: [[w, h], [ib.naturalWidth, ib.naturalHeight]]};
            }
            const px = (im) => {
                const c = document.createElement('canvas');
                c.width = w; c.height = h;
                const x = c.getContext('2d');
                x.drawImage(im, 0, 0);
                return x.getImageData(0, 0, w, h).data;
            };
            const da = px(ia), db = px(ib);
            let sum = 0, big = 0;
            for (let i = 0; i < da.length; i += 4) {
                const d = Math.max(Math.abs(da[i] - db[i]),
                    Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]));
                sum += d;
                if (d > 48) big++;
            }
            return {sizes: [[w, h]], mean: sum / (w * h), big: big / (w * h)};
        }""",
        [a_url, b_url],
    )


def png_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def test_region_comment_saves_screenshot_and_position(open_review):
    """囲んだ範囲の位置・図の座標・範囲内の文字列・画像が保存される。"""
    page, sid, _ = open_review()
    page.click("#btn-region")
    expect(page.locator("#btn-region")).to_have_class(re.compile(r"\bon\b"))
    l1 = frame_rect(page, "#l1")
    x, y, w, h = l1["x"] - 12, l1["y"] - 12, l1["w"] + 24, l1["h"] + 24
    draw(page, x, y, w, h)
    expect(page.locator("#draft-quote")).to_contain_text("▭ 左上の箱")
    submit(page, "この箱の色を変える", 1)

    c = storage.load_session(sid)["comments"][0]
    assert c["target"] == "region"
    assert c["texts"] == ["左上の箱"]  # 隣の箱やラベルは入らない
    assert c["quote"] == "左上の箱"
    fb = frame_box(page)
    reg = c["region"]
    assert reg["x"] == pytest.approx(x - fb["x"], abs=1)
    assert reg["y"] == pytest.approx(y - fb["y"], abs=1)
    assert (reg["width"], reg["height"]) == (
        pytest.approx(w, abs=1), pytest.approx(h, abs=1))
    assert reg["ref"] == "svg:0"
    # SVG の座標系での範囲に <text x="70" y="170"> の書き出し位置が入る
    sx, sy, sw, sh = reg["svg_box"]
    assert sx < 70 < sx + sw and sy < 170 < sy + sh
    assert sw < 460  # 左の箱の幅に収まる
    png = storage.get_image_path(sid, c["image"]).read_bytes()
    pw, ph = storage.png_size(png)
    assert pw == pytest.approx(w * 2, abs=1)  # 2倍の解像度で切り抜く
    assert ph == pytest.approx(h * 2, abs=1)
    assert c["screenshot_size"] == [pw, ph]
    # カードにはAIに渡した画像の縮小が付く
    thumb = page.locator(".comment-card .quote.img-quote img")
    expect(thumb).to_have_attribute(
        "src", f"/api/sessions/{sid}/files/{c['image']}")
    assert page.evaluate(
        "() => document.querySelector('.comment-card img').naturalWidth") == pw


@pytest.mark.parametrize("width, scroll, area", [
    (1400, 300, "figure"),  # スクロールした状態で、図と埋め込み画像の上
    (1000, 0, "lead"),      # 幅で切り替わる CSS と rem が効いた段落の上
])
def test_region_screenshot_matches_rendered_page(open_review, width, scroll,
                                                 area):
    """スクリーンショットは、そのとき画面に見えていた見た目と一致する。"""
    page, sid, messages = open_review(width=width, dpr=2)
    frame_scroll(page, scroll)
    page.wait_for_function(
        f"() => document.getElementById('doc-frame').contentWindow.scrollY === {scroll}")
    if area == "figure":
        x0, y0 = svg_to_page(page, 380, 330)
        x1, y1 = svg_to_page(page, 800, 520)
        x, y, w, h = round(x0), round(y0), round(x1 - x0), round(y1 - y0)
    else:
        lead = frame_rect(page, "#lead")
        # 狭い幅では段落が緑の帯になる（@media が効いている）ことを確かめる
        assert page.evaluate(
            """() => {
                const f = document.getElementById('doc-frame');
                return f.contentWindow.getComputedStyle(
                    f.contentDocument.querySelector('#lead')).backgroundColor;
            }""") == "rgb(0, 128, 0)"
        x, y = round(lead["x"] + 8), round(lead["y"] + 6)
        w, h = round(lead["w"] * 0.7), round(lead["h"] + 40)
    reference = page.screenshot(clip={"x": x, "y": y, "width": w, "height": h})
    page.click("#btn-region")
    draw(page, x, y, w, h)
    submit(page, "ここ", 1)
    c = storage.load_session(sid)["comments"][0]
    saved = storage.get_image_path(sid, c["image"]).read_bytes()
    diff = compare_images(page, png_url(saved), png_url(reference))
    assert len(diff["sizes"]) == 1, diff
    assert diff["mean"] < 2.0, diff
    assert diff["big"] < 0.005, diff
    assert not [m for m in messages if "Content Security Policy" in m]


def _make_region(page, vx0, vy0, vx1, vy1, text, count):
    """図の座標系の矩形を囲んでコメントを付け、そのコメントのIDを返す。"""
    x0, y0 = svg_to_page(page, vx0, vy0)
    x1, y1 = svg_to_page(page, vx1, vy1)
    draw(page, x0, y0, x1 - x0, y1 - y0)
    submit(page, text, count)
    return page.locator(".comment-card").nth(count - 1).get_attribute("data-cid")


def _expected_box(page, vx0, vy0, vx1, vy1) -> dict:
    x0, y0 = svg_to_page(page, vx0, vy0)
    x1, y1 = svg_to_page(page, vx1, vy1)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _assert_box(page, cid, expected, tol=1.5):
    page.wait_for_function(
        """([cid, ex, tol]) => {
            const el = document.querySelector(`.region-box[data-cid="${cid}"]`);
            if (!el) return false;
            const b = el.getBoundingClientRect();
            return Math.abs(b.x - ex.x) <= tol && Math.abs(b.y - ex.y) <= tol &&
              Math.abs(b.width - ex.width) <= tol &&
              Math.abs(b.height - ex.height) <= tol;
        }""",
        arg=[cid, expected, tol], timeout=5000,
    )


def test_region_box_follows_scroll_resize_and_reload(open_review):
    """枠はスクロールに付いていき、幅が変わって図が縮んでも図の同じ所を指す。"""
    page, _sid, _ = open_review()
    page.click("#btn-region")
    cid = _make_region(page, 540, 40, 960, 280, "右の箱", 1)
    _assert_box(page, cid, _expected_box(page, 540, 40, 960, 280))

    before = box_of(page, cid)
    frame_scroll(page, 120)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY === 120")
    _assert_box(page, cid, dict(before, y=before["y"] - 120))

    # 画面を狭めると図（width:100%）が縮む。枠も図の同じ所に付いていく
    page.set_viewport_size({"width": 1100, "height": 900})
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.innerWidth === 780")
    shrunk = _expected_box(page, 540, 40, 960, 280)
    assert shrunk["width"] < before["width"] - 50
    _assert_box(page, cid, shrunk)

    page.reload()
    page.wait_for_selector(f'.region-box[data-cid="{cid}"]')
    frame_scroll(page, 0)
    _assert_box(page, cid, _expected_box(page, 540, 40, 960, 280))


def test_card_and_region_box_are_linked(open_review):
    """カードを押すと範囲へスクロールして枠が強調され、枠の番号を押すと
    カードが開く。範囲指定モードでのクリックも、そこにある範囲を開く。"""
    page, _sid, _ = open_review()
    page.click("#btn-region")
    c1 = _make_region(page, 40, 40, 460, 280, "左", 1)
    c2 = _make_region(page, 40, 400, 460, 520, "下", 2)
    card1 = page.locator(f'.comment-card[data-cid="{c1}"]')
    card2 = page.locator(f'.comment-card[data-cid="{c2}"]')

    frame_scroll(page, 1500)  # 範囲が見えない所まで離れる
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY > 1000")
    card2.locator(".quote").click()
    expect(page.locator(f'.region-box[data-cid="{c2}"]')).to_have_class(
        re.compile(r"\bactive\b"))
    page.wait_for_function(
        """(cid) => {
            const el = document.querySelector(`.region-box[data-cid="${cid}"]`);
            const f = document.getElementById('doc-frame').getBoundingClientRect();
            if (!el) return false;
            const b = el.getBoundingClientRect();
            return b.top >= f.top && b.bottom <= f.bottom;
        }""", arg=c2)

    # 範囲指定モードで囲まずにクリックすると、そこにある範囲のカードが開く
    frame_scroll(page, 0)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY === 0")
    # 枠はスクロールの次の描画で動くので、描き直されるのを待ってから押す
    _assert_box(page, c1, _expected_box(page, 40, 40, 460, 280))
    b1 = box_of(page, c1)
    real_click(page, b1["x"] + b1["width"] / 2, b1["y"] + b1["height"] / 2)
    expect(card1).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#comment-draft")).to_be_hidden()

    # 範囲指定モードを抜けても、枠の番号から開ける
    page.click("#btn-region")
    page.locator(f'.region-box[data-cid="{c2}"] .region-tag').click()
    expect(card2).to_have_class(re.compile(r"\bactive\b"))
    expect(card1).not_to_have_class(re.compile(r"\bactive\b"))


def test_region_mode_wheel_escape_and_text_selection(open_review):
    """範囲指定中はホイールでページを送れて、文字は選択されない。
    Esc で抜けると、ふつうの文字列の選択に戻る。"""
    page, _sid, _ = open_review()
    page.click("#btn-region")
    fb = frame_box(page)
    real_wheel(page, fb["x"] + 300, fb["y"] + 300, 400)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY > 100")
    frame_scroll(page, 0)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY === 0")

    lead = frame_rect(page, "#lead")
    real_drag(page, lead["x"] + 30, lead["y"] + lead["h"] / 2,
              lead["x"] + 200, lead["y"] + lead["h"] / 2)
    # 細い帯（高さ 0）は囲みにならず、文字も選択されない
    expect(page.locator("#comment-draft")).to_be_hidden()
    assert page.evaluate(
        """() => document.getElementById('doc-frame').contentDocument
            .getSelection().toString()""") == ""

    page.keyboard.press("Escape")
    expect(page.locator("#btn-region")).not_to_have_class(re.compile(r"\bon\b"))
    assert "drawing" not in (page.locator("#region-layer").get_attribute("class") or "")
    real_drag(page, lead["x"] + 30, lead["y"] + lead["h"] / 2,
              lead["x"] + 200, lead["y"] + lead["h"] / 2)
    assert page.evaluate(
        """() => document.getElementById('doc-frame').contentDocument
            .getSelection().toString()""") != ""


def test_redrawing_keeps_draft_text_and_tag(open_review):
    """書いている途中で囲み直しても、書いた文と種類はそのまま残る。"""
    page, sid, _ = open_review()
    page.click("#btn-region")
    x0, y0 = svg_to_page(page, 40, 40)
    x1, y1 = svg_to_page(page, 460, 280)
    draw(page, x0, y0, x1 - x0, y1 - y0)
    expect(page.locator("#draft-quote")).to_contain_text("左上の箱")
    page.fill("#draft-text", "こちらの箱ではなく")
    page.locator('#draft-tag input[value="question"]').check()
    x0, y0 = svg_to_page(page, 540, 40)
    x1, y1 = svg_to_page(page, 960, 280)
    draw(page, x0, y0, x1 - x0, y1 - y0)
    expect(page.locator("#draft-quote")).to_contain_text("右上の箱")
    expect(page.locator("#draft-text")).to_have_value("こちらの箱ではなく")
    page.click("#draft-submit")
    expect(page.locator(".comment-card")).to_have_count(1)
    c = storage.load_session(sid)["comments"][0]
    assert c["texts"] == ["右上の箱"]
    assert c["tag"] == "question"
    assert storage.list_images(sid) == [c["image"]]  # 囲み直した分は残らない


def test_deleting_region_comment_removes_box_and_screenshot(open_review):
    page, sid, _ = open_review()
    page.click("#btn-region")
    cid = _make_region(page, 40, 400, 460, 520, "あとで消す", 1)
    assert storage.list_images(sid) == [f"region-{cid}.png"]
    page.on("dialog", lambda d: d.accept())
    page.locator(".comment-card button[data-del]").click()
    expect(page.locator(".comment-card")).to_have_count(0)
    expect(page.locator(".region-box")).to_have_count(0)
    assert storage.list_images(sid) == []


def test_cancelled_draft_leaves_nothing(open_review):
    page, sid, _ = open_review()
    page.click("#btn-region")
    x0, y0 = svg_to_page(page, 40, 400)
    x1, y1 = svg_to_page(page, 460, 520)
    draw(page, x0, y0, x1 - x0, y1 - y0)
    expect(page.locator(".region-box.draft")).to_have_count(1)
    page.click("#draft-cancel")
    expect(page.locator(".region-box")).to_have_count(0)
    assert storage.list_images(sid) == []
    assert storage.load_session(sid)["comments"] == []


def test_admin_page_shows_region_screenshot(open_review, web_port):
    page, sid, _ = open_review()
    page.click("#btn-region")
    _make_region(page, 40, 40, 460, 280, "管理画面で見る", 1)
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    page.click(f'button[data-toggle="{sid}"]')
    thumb = page.locator(f'[data-detail="{sid}"] img.region-thumb')
    expect(thumb).to_have_count(1)
    page.wait_for_function(
        "() => { const i = document.querySelector('img.region-thumb');"
        " return !!(i && i.complete && i.naturalWidth > 0); }")
    expect(page.locator(f'[data-detail="{sid}"]')).to_contain_text("▭ 範囲「左上の箱」")


# ---------- 撮影が画面とずれやすい条件 ----------

def capture_and_compare(page, sid, x, y, w, h, count):
    """画面の同じ範囲のスクリーンショットと、範囲指定で保存した画像を比べる。

    比べる元の画面からは、このページが重ねた層（範囲の枠）と通知を外す
    （保存する画像にも写さない設計なので）。
    """
    page.evaluate("""() => {
        document.getElementById('region-layer').style.visibility = 'hidden';
        document.getElementById('toast').hidden = true;
    }""")
    reference = page.screenshot(clip={"x": x, "y": y, "width": w, "height": h})
    page.evaluate(
        "() => { document.getElementById('region-layer').style.visibility = ''; }")
    if "on" not in (page.locator("#btn-region").get_attribute("class") or ""):
        page.click("#btn-region")
    draw(page, x, y, w, h)
    submit(page, f"範囲{count}", count)
    c = storage.load_session(sid)["comments"][count - 1]
    saved = storage.get_image_path(sid, c["image"]).read_bytes()
    return compare_images(page, png_url(saved), png_url(reference)), c


def assert_same(diff):
    assert len(diff["sizes"]) == 1, diff
    assert diff["mean"] < 2.0, diff
    assert diff["big"] < 0.005, diff


STICKY_DOC = """<!doctype html>
<html><head><style>
body{margin:0;font:16px sans-serif;background:#fff;color:#222}
header{position:sticky;top:0;height:56px;line-height:56px;padding:0 16px;
  background:rgb(230,120,120);color:#fff;z-index:2}
.row{height:120px;border-bottom:1px solid #ddd;padding:8px 16px}
#badge{position:fixed;right:24px;bottom:24px;width:140px;height:44px;
  line-height:44px;text-align:center;background:rgb(110,110,230);color:#fff}
#toast{position:fixed;left:50%;top:90px;transform:translateX(-50%);width:220px;
  height:36px;line-height:36px;text-align:center;background:rgb(40,160,90);color:#fff}
</style></head><body>
<div class="row">先頭の行</div>
<header id="hd">貼り付く見出し</header>
__ROWS__
<div id="badge">固定のバッジ</div>
<div id="toast">中央の通知</div>
</body></html>""".replace("__ROWS__", "\n".join(
    f'<div class="row">行{i}</div>' for i in range(1, 25)))


def test_screenshot_keeps_sticky_and_fixed_elements_in_place(open_review):
    """スクロールで貼り付いた見出し・画面に固定された要素も、見えている所に写る。"""
    page, sid, _ = open_review(doc=STICKY_DOC, ready="#badge", dpr=2)
    frame_scroll(page, 600)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY === 600")
    fb = frame_box(page)
    # 貼り付いた見出しと、translateX(-50%) で中央に置いた通知を含む上の帯
    diff, _ = capture_and_compare(
        page, sid, round(fb["x"] + 20), round(fb["y"]), 700, 180, 1)
    assert_same(diff)
    # 右下に固定したバッジ
    diff, _ = capture_and_compare(
        page, sid, round(fb["x"] + fb["width"] - 260),
        round(fb["y"] + fb["height"] - 140), 240, 120, 2)
    assert_same(diff)
    # 撮影のために一瞬 static にした見出しは、元のとおり sticky のまま
    assert page.evaluate(
        """() => {
            const f = document.getElementById('doc-frame');
            const h = f.contentDocument.getElementById('hd');
            return [f.contentWindow.getComputedStyle(h).position,
                    h.getAttribute('style')];
        }""") == ["sticky", None]


XML_DOC = """<!doctype html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><style>
body{margin:0;font:18px sans-serif}
.box{margin:20px;padding:20px;background:rgb(250,230,160)}
pre{margin:20px;font:14px/1.4 monospace}
</style></head><body>
<!-- 描画されないコメント -- 二重ハイフンは XML では不正 -->
<div class="box" id="box" @click="open = true" :class="{a: b}" x-on:click="go()"
     v-bind:title="t">Alpine や Vue の書き方を含む箱</div>
<p id="ctrl">制御文字\u000cを含む段落</p>
<pre id="long">__LINES__</pre>
</body></html>""".replace("__LINES__", "\n".join(
    f"行{i:03d}: 長い整形済みテキストの一部です" for i in range(1, 301)))


def test_screenshot_with_xml_invalid_markup_and_long_text(open_review):
    """XMLでは不正な属性名・制御文字・コメントがあっても撮影でき、画面と一致する。
    長い1つのテキストノードの後半を囲んでも、範囲内の文字列を拾える。"""
    page, sid, messages = open_review(doc=XML_DOC, ready="#long", dpr=2)
    box = frame_rect(page, "#box")
    # 800px 以下にする（それより広いと長辺1600pxに収めるため2倍より粗くなり、
    # 2倍の画面のスクリーンショットと大きさが揃わない）
    diff, c = capture_and_compare(
        page, sid, round(box["x"] - 10), round(box["y"] - 10),
        760, round(box["h"] + 20), 1)
    assert_same(diff)
    assert any("Alpine" in t for t in c["texts"])

    # XML に書けない制御文字があっても撮影できる（その文字は、Chrome が描く
    # 記号とは別の記号に置き換わるので、画素の一致までは求めない）
    ctrl = frame_rect(page, "#ctrl")
    draw(page, ctrl["x"], ctrl["y"] - 4, 300, ctrl["h"] + 8)
    submit(page, "制御文字", 2)

    # 長い <pre> の後半（先頭から4000文字より後ろ）を囲む
    frame_scroll(page, 4000)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY > 3000")
    fb = frame_box(page)
    draw(page, fb["x"] + 10, fb["y"] + 200, 500, 120)
    submit(page, "ここの行", 3)
    c2 = storage.load_session(sid)["comments"][2]
    nums = [int(n) for t in c2["texts"] for n in re.findall(r"行(\d{3})", t)]
    assert nums and min(nums) > 150, c2["texts"]
    assert not [m for m in messages if "範囲のスクリーンショットを作れません" in m]


SCROLLBAR_DOC = """<!doctype html>
<html><head><style>
html::-webkit-scrollbar{width:16px;height:16px}
html::-webkit-scrollbar-thumb{background:#999}
body{margin:0;font:16px sans-serif}
#band{height:120px;background:rgb(200,0,0);color:#fff}
@media (max-width:1000px){#band{background:rgb(0,128,0)}}
.vw{width:50vw;height:40px;background:rgb(0,0,200)}
.fill{height:2000px}
</style></head><body><div id="band">幅で色が変わる帯</div><div class="vw"></div>
<div class="fill"></div></body></html>"""


def test_screenshot_matches_with_visible_scrollbar(open_review, playwright):
    """スクロールバーが幅を取る環境でも、@media と vw の判定が画面と揃う。

    Playwright の headless は既定でスクロールバーを隠すので、その指定を外した
    ブラウザで確かめる（iframe の innerWidth 1008、clientWidth 992 になる）。
    """
    b = playwright.chromium.launch(ignore_default_args=["--hide-scrollbars"])
    try:
        page, sid, _ = open_review(width=1328, doc=SCROLLBAR_DOC, ready="#band",
                                   dpr=2, on_browser=b)
        sizes = page.evaluate(
            """() => { const w = document.getElementById('doc-frame').contentWindow;
                return [w.innerWidth, w.document.documentElement.clientWidth]; }""")
        assert sizes[0] == 1008 and sizes[1] < 1000, sizes
        band = frame_rect(page, "#band")
        diff, _ = capture_and_compare(
            page, sid, round(band["x"] + 10), round(band["y"] + 10),
            700, 140, 1)
        assert_same(diff)
    finally:
        b.close()


# ---------- 範囲の枠と操作 ----------

def test_escape_cancels_redraw_while_typing(open_review):
    """書きかけの下書き欄にフォーカスがあっても、囲み直しを Esc で取り消せる。"""
    from .conftest import _mouse

    page, _sid, _ = open_review()
    page.click("#btn-region")
    x0, y0 = svg_to_page(page, 40, 40)
    x1, y1 = svg_to_page(page, 460, 280)
    draw(page, x0, y0, x1 - x0, y1 - y0)
    page.fill("#draft-text", "書きかけ")
    a0, b0 = svg_to_page(page, 540, 40)
    a1, b1 = svg_to_page(page, 960, 280)
    cdp = page.context.new_cdp_session(page)
    try:
        _mouse(cdp, "mouseMoved", a0, b0, False)
        _mouse(cdp, "mousePressed", a0, b0, True)
        _mouse(cdp, "mouseMoved", a1, b1, True)
        # 下書きの枠と、囲みかけの枠の2つ
        expect(page.locator(".region-box.draft")).to_have_count(2)
        assert page.evaluate("() => document.activeElement.id") == "draft-text"
        page.keyboard.press("Escape")
        expect(page.locator(".region-box.draft")).to_have_count(1)
        _mouse(cdp, "mouseReleased", a1, b1, False)
    finally:
        cdp.detach()
    expect(page.locator("#draft-quote")).to_contain_text("左上の箱")
    expect(page.locator("#draft-text")).to_have_value("書きかけ")
    expect(page.locator("#btn-region")).to_have_class(re.compile(r"\bon\b"))


def test_svg_box_is_correct_when_scrolled(open_review):
    """スクロールした状態で囲んでも、SVG の座標系での範囲は図の座標どおり。"""
    page, sid, _ = open_review()
    frame_scroll(page, 250)
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY === 250")
    page.click("#btn-region")
    _make_region(page, 540, 300, 960, 560, "右下", 1)
    sx, sy, sw, sh = storage.load_session(sid)["comments"][0]["region"]["svg_box"]
    assert (sx, sy, sw, sh) == (pytest.approx(540, abs=2), pytest.approx(300, abs=2),
                                pytest.approx(420, abs=2), pytest.approx(260, abs=2))


DETAILS_DOC = REGION_DOC.replace(
    '<p id="lead">',
    '<details id="more"><summary>詳しく</summary>'
    '<div style="height:300px">開くと図が下がる</div></details><p id="lead">')


def test_region_box_follows_reflow_without_scroll(open_review):
    """<details> を開いて図が下がると、スクロールしなくても枠が付いていく。"""
    page, _sid, _ = open_review(doc=DETAILS_DOC)
    page.click("#btn-region")
    cid = _make_region(page, 40, 400, 460, 520, "下", 1)
    before = box_of(page, cid)
    page.evaluate("""() => { document.getElementById('doc-frame').contentDocument
        .getElementById('more').open = true; }""")
    page.wait_for_function(
        "() => document.getElementById('doc-frame').contentWindow.scrollY === 0")
    expected = _expected_box(page, 40, 400, 460, 520)
    assert expected["y"] > before["y"] + 250
    _assert_box(page, cid, expected)


def test_previous_revision_region_is_marked(open_review):
    """前の稿で囲んだ範囲は、新しい稿では前の稿のものと分かる枠になる。"""
    page, sid, _ = open_review()
    page.click("#btn-region")
    cid = _make_region(page, 40, 40, 460, 280, "前の稿への指摘", 1)
    storage.add_revision(sid, REGION_DOC)  # AIが次の稿を出した
    # ページはポーリングで新しい稿を見つけて読み直す
    page.wait_for_function(
        """(cid) => {
            const el = document.querySelector(`.region-box[data-cid="${cid}"]`);
            return !!(el && el.classList.contains('stale'));
        }""", arg=cid, timeout=10000)
    expect(page.locator(f'.region-box[data-cid="{cid}"] .region-tag')).to_have_text(
        "C1・第1稿")

"""Playwright E2Eテスト用フィクスチャ。"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from hle_mcp import storage

BASE_TEXT = "これはAIが書いた文章です。\n二段落目の文章です。"


@pytest.fixture(scope="session")
def playwright():
    """Playwright本体。sync APIは多重起動できないためセッション共有する。"""
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright):
    b = playwright.chromium.launch()
    yield b
    b.close()


@pytest.fixture()
def review(browser, web_port):
    """セッションを作成し、そのエディタページを開いた state を返す。"""
    session = storage.create_session(BASE_TEXT, "E2Eテスト", "/tmp/e2e")
    context = browser.new_context(
        permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{session['id']}")
    page.wait_for_selector("#editor p")
    yield page, session["id"]
    context.close()


def select_in_editor(page, para_index: int, start: int, end: int) -> None:
    """指定段落のテキストノードに対して文字範囲選択を行う。"""
    page.evaluate(
        """([i, s, e]) => {
            const p = document.querySelectorAll('#editor p')[i];
            const node = p.firstChild;
            const r = document.createRange();
            r.setStart(node, s);
            r.setEnd(node, e);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            document.getElementById('editor').focus();
        }""",
        [para_index, start, end],
    )


# ---------- ブラウザの入力経路を通したマウス操作 ----------

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
    操作と同じに扱われ、文字の選択・pointer イベント・click が起きる。
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


def real_wheel(page, x: float, y: float, dy: float) -> None:
    """ブラウザの入力経路を通して、(x, y) でホイールを回す。"""
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy})
    finally:
        cdp.detach()

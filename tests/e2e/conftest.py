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

"""CSP・エスケープ周りの E2E 回帰テスト。

CSPは付け方を誤るとUIを静かに壊すため、実ブラウザで
「CSP違反が1件も出ない」「主要操作が従来どおり動く」ことを確認する。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from hle_mcp import storage

from .conftest import select_in_editor


def _collect_violations(page) -> list[str]:
    """CSP違反などのコンソールエラーを集めるリストを返す。"""
    errors: list[str] = []
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


@pytest.fixture()
def csp_page(browser, web_port):
    context = browser.new_context()
    page = context.new_page()
    errors = _collect_violations(page)
    yield page, errors, web_port
    context.close()


def test_editor_has_no_csp_violation(csp_page):
    """CSP下でもスクリプト・スタイルが読め、編集と保存が通る。"""
    page, errors, web_port = csp_page
    s = storage.create_session("これはAIが書いた文章です。", "CSP検証", "/tmp/csp")
    page.goto(f"http://127.0.0.1:{web_port}/session/{s['id']}")
    page.wait_for_selector("#editor p")

    # スタイルシートが適用されている（style-src 'self' が効いている）
    assert page.locator("#editor-view #topbar").evaluate(
        "el => getComputedStyle(el).display") == "flex"

    # 編集して自動保存まで通る（script-src 'self' と connect-src 'self'）
    select_in_editor(page, 0, 0, 3)
    page.keyboard.press("Backspace")
    expect(page.locator("#editor del")).to_have_count(1)
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)

    assert [e for e in errors if "Content Security Policy" in e] == []
    assert errors == []


def test_admin_has_no_csp_violation(csp_page):
    """管理画面のインラインstyle撤去後もレイアウトが崩れない。"""
    page, errors, web_port = csp_page
    s = storage.create_session("本文", "CSP管理", "/tmp/csp")
    storage.set_document(s["id"], "<p>本文<del>旧</del><ins>新</ins></p>")
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    expect(page.locator(".admin-card")).to_have_count(1)
    page.click("button[data-toggle]")
    detail = page.locator(".admin-detail")
    expect(detail.locator("del")).to_have_text("旧")

    # インラインstyleをクラスへ移した箇所が実際に効いている
    assert page.locator(".admin-card .row.spread").evaluate(
        "el => getComputedStyle(el).justifyContent") == "space-between"

    assert [e for e in errors if "Content Security Policy" in e] == []
    assert errors == []


def test_admin_renders_escaped_markers_as_literals(csp_page):
    """本文中の【重要】がマーカーとして誤展開されず、そのまま表示される。"""
    page, errors, web_port = csp_page
    s = storage.create_session("本文", "マーカー衝突", "/tmp/csp")
    storage.set_document(
        s["id"], "<p>これは【重要】な<del>旧</del><ins>新</ins>点</p>")
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    page.click("button[data-toggle]")
    annotated = page.locator(".admin-detail .annotated").first
    expect(annotated).to_be_visible()
    # エスケープのバックスラッシュは表示されず、本文の記号として見える
    expect(annotated).to_contain_text("これは【重要】な")
    assert "\\" not in annotated.inner_text()
    # 実際の削除・追加だけが装飾される
    expect(annotated.locator("del")).to_have_text("旧")
    expect(annotated.locator("ins")).to_have_text("新")
    assert errors == []


def test_comment_id_with_quote_is_rejected(csp_page):
    """属性を抜け出すコメントIDはAPIが400で弾く。"""
    page, _errors, web_port = csp_page
    s = storage.create_session("本文", "ID検証", "/tmp/csp")
    page.goto(f"http://127.0.0.1:{web_port}/session/{s['id']}")
    page.wait_for_selector("#editor p")
    status = page.evaluate(
        """async (sid) => {
            const res = await fetch('/api/sessions/' + sid + '/comments', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    id: 'x" onmouseover="window.__pwned=1',
                    quote: 'q', text: 't', author: 'human'
                })
            });
            return res.status;
        }""",
        s["id"],
    )
    assert status == 400
    assert storage.load_session(s["id"])["comments"] == []
    assert page.evaluate("() => window.__pwned") is None


# ---------- 実ブラウザでの攻撃再現 ----------
@pytest.fixture(scope="module")
def rebinding_browser(playwright):
    """evil.test を 127.0.0.1 に解決するブラウザ。

    DNSリバインディングが成立した後の状態（ブラウザは evil.test を
    自オリジンとみなし、通信はループバックへ飛ぶ）を再現する。
    """
    b = playwright.chromium.launch(
        args=["--host-resolver-rules=MAP evil.test 127.0.0.1"]
    )
    yield b
    b.close()


def test_rebinding_cannot_open_editor_page(rebinding_browser, web_port):
    """リバインディング名ではエディタHTMLが配信されない（Host検証）。

    トップレベル遷移はブラウザのPrivate Network Accessに阻まれないため、
    サーバーのHost検証が唯一の防御になる経路。
    """
    s = storage.create_session("本文", "配信検証", "/tmp/x")
    context = rebinding_browser.new_context()
    page = context.new_page()
    res = page.goto(f"http://evil.test:{web_port}/session/{s['id']}")
    assert res.status == 403
    assert '<div id="editor"' not in page.content()
    context.close()


def test_rebinding_cannot_read_sessions(rebinding_browser, web_port):
    """リバインディング名では全セッションを読み出せない（Host検証）。"""
    s = storage.create_session("秘密の下書き", "漏洩検証", "/Users/me/secret-ws")
    context = rebinding_browser.new_context()
    page = context.new_page()
    res = page.goto(f"http://evil.test:{web_port}/api/sessions")
    assert res.status == 403
    body = res.text()
    assert "秘密の下書き" not in body
    assert "secret-ws" not in body
    assert s["id"] not in body
    context.close()


# ---------- 別オリジンからのCSRF（実ブラウザ） ----------
@pytest.fixture()
def attacker_site():
    """ループバック上の別ポートで攻撃者HTMLを配信し、そのオリジンを返す。

    攻撃者ページ自体もループバックに置く理由: ChromiumのPrivate Network
    Accessはpublic→loopbackのfetchを先に遮断してしまい、それではサーバー側の
    Origin検証を通過するかどうかを検証できない。loopback→loopbackなら
    PNAは介入せず、ポート違いで別オリジンになるためCSRFを忠実に再現できる。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Attacker(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            body = b"<html><body>attacker</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Attacker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_cross_origin_simple_request_cannot_write(browser, web_port, attacker_site):
    """悪性サイトからの text/plain no-cors POST が書き込めない（CSRF）。"""
    s = storage.create_session("原文", "CSRF検証", "/tmp/x")
    context = browser.new_context()
    page = context.new_page()
    page.goto(attacker_site + "/")
    assert page.evaluate("() => location.origin") == attacker_site
    page.evaluate(
        """async ([port, sid]) => {
            // プリフライトを回避する simple request を投げる
            await fetch('http://127.0.0.1:' + port + '/api/sessions/' + sid +
                '/document', {
                method: 'POST',
                mode: 'no-cors',
                headers: { 'Content-Type': 'text/plain' },
                body: JSON.stringify({ html: '<p>改ざん</p>' })
            }).catch(() => {});
        }""",
        [web_port, s["id"]],
    )
    page.wait_for_timeout(300)
    assert storage.load_session(s["id"])["revisions"][-1]["edited_html"] is None
    context.close()


def test_cross_origin_json_post_cannot_write(browser, web_port, attacker_site):
    """application/json でもOrigin不一致で拒否される。"""
    s = storage.create_session("原文", "CSRF検証2", "/tmp/x")
    context = browser.new_context()
    page = context.new_page()
    page.goto(attacker_site + "/")
    page.evaluate(
        """async ([port, sid]) => {
            await fetch('http://127.0.0.1:' + port + '/api/sessions/' + sid +
                '/document', {
                method: 'POST',
                mode: 'no-cors',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ html: '<p>改ざん</p>' })
            }).catch(() => {});
        }""",
        [web_port, s["id"]],
    )
    page.wait_for_timeout(300)
    assert storage.load_session(s["id"])["revisions"][-1]["edited_html"] is None
    context.close()

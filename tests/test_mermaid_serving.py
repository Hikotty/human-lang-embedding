"""Mermaid の隔離フレームの配信（ヘッダ・CSP・同梱ファイル）のテスト。

本文の Mermaid 記法は外部由来になりうる文字列で、パースするライブラリには
HTML / CSS インジェクションの脆弱性が繰り返し出ている。そのため Mermaid は
エディタのページではなく、不透明オリジンの隔離フレームの中でだけ動かす。
この隔離が配信の側で崩れていないこと（エディタ本体のCSPを緩めていない、
フレームは専用のCSPで自オリジンからしか埋め込めない）を確かめる。
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hle_mcp import storage, versioning, webserver

STATIC = versioning.STATIC_DIR
FRAME = "/static/mermaid-frame.html"


def _get(port: int, path: str, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _directives(csp: str) -> dict[str, str]:
    out = {}
    for part in csp.split(";"):
        name, _, value = part.strip().partition(" ")
        if name:
            out[name] = value.strip()
    return out


@pytest.fixture()
def sid():
    return storage.create_session("本文", "Mermaid配信", "/tmp/m")["id"]


def test_editor_csp_only_adds_same_origin_frames(web_port, sid):
    """エディタ本体のCSPに足したのは frame-src 'self' だけ。スクリプトとスタイルは緩めない。"""
    status, headers, _ = _get(web_port, f"/session/{sid}")
    assert status == 200
    d = _directives(headers["Content-Security-Policy"])
    assert d["frame-src"] == "'self'"
    assert d["script-src"] == "'self'"
    assert d["style-src"] == "'self'"
    assert d["default-src"] == "'none'"
    assert d["frame-ancestors"] == "'none'"
    assert d["connect-src"] == "'self'"
    csp = headers["Content-Security-Policy"]
    assert "unsafe-eval" not in csp and "unsafe-inline" not in csp
    assert headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("path", ["/", "/admin", "/static/tracker.js"])
def test_other_pages_do_not_get_frame_src(web_port, path):
    """フレームを埋め込むのは md のエディタだけ。一覧・管理画面・静的ファイルは従来のまま。"""
    _, headers, _ = _get(web_port, path)
    assert "frame-src" not in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_frame_page_has_its_own_csp(web_port):
    """隔離フレーム: 自オリジンのページからしか埋め込めず、通信とeval は許さない。"""
    status, headers, body = _get(web_port, FRAME)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    csp = headers["Content-Security-Policy"]
    d = _directives(csp)
    assert d["default-src"] == "'none'"
    assert d["script-src"] == "'self'"
    # Mermaid が SVG に入れる <style> と style 属性のために、このフレームの中だけ緩める
    assert d["style-src"] == "'unsafe-inline'"
    assert d["frame-ancestors"] == "'self'"
    assert d["img-src"] == "data:"
    # 通信を許す指定が無い（default-src 'none' のまま）
    for name in ("connect-src", "frame-src", "form-action"):
        assert d.get(name, "'none'") == "'none'"
    assert "unsafe-eval" not in csp
    assert "script-src 'unsafe-inline'" not in csp
    # フレームが読むスクリプトは同一ホストの2本だけ。インラインのスクリプトは無い
    scripts = re.findall(rb"<script\b[^>]*>", body)
    assert sorted(scripts) == [
        b'<script src="/static/mermaid-frame.js">',
        b'<script src="/static/mermaid.min.js">',
    ]
    assert not re.search(rb"<script[^>]*>\s*[^<\s]", body)  # 本文を持つ <script> が無い


def test_frame_static_scripts_are_served_as_javascript(web_port):
    for name in ("mermaid.min.js", "mermaid-frame.js", "mermaid-view.js"):
        status, headers, body = _get(web_port, f"/static/{name}")
        assert status == 200
        assert headers["Content-Type"].startswith("application/javascript")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert body


def test_frame_page_is_guarded_by_host_check(web_port):
    """フレームの配信も他の静的ファイルと同じ Host 検証を通る。"""
    status, _, _ = _get(web_port, FRAME, headers={"Host": "evil.example"})
    assert status == 403


def test_vendored_mermaid_matches_recorded_hash():
    """同梱した mermaid.min.js が、出所の記録（版・SHA-256）と一致する。"""
    note = (STATIC / "mermaid.VENDOR.txt").read_text(encoding="utf-8")
    recorded = re.search(r"SHA-256\s*:\s*([0-9a-f]{64})", note).group(1)
    actual = hashlib.sha256((STATIC / "mermaid.min.js").read_bytes()).hexdigest()
    assert actual == recorded
    version = re.search(r"版\s*:\s*(\d+\.\d+\.\d+)", note).group(1)
    assert f"mermaid-{version}.tgz" in note
    assert (STATIC / "mermaid.LICENSE.txt").read_text(encoding="utf-8").startswith(
        "The MIT License"
    )


def test_self_test_covers_mermaid_assets():
    """入れ替え時の自己テストが、Mermaid の同梱ファイルが返ることまで確かめる。"""
    webserver.self_test()  # 返らない・404 なら RuntimeError
    for name in ("mermaid-view.js", "mermaid-frame.html", "mermaid-frame.js",
                 "mermaid.min.js"):
        assert (Path(STATIC) / name).is_file()

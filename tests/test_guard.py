"""リクエストガードとセキュリティヘッダのテスト。

守る対象:
- Host 検証 … DNSリバインディング（悪性ドメインを127.0.0.1へ再解決する攻撃）
- Origin 検証 … 悪性サイトからのCSRF書き込み
- Content-Type 必須化 … プリフライトを回避する simple request の封じ込め
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from hle_mcp import storage


def _request(port: int, path: str, method: str = "GET", headers=None, body=None):
    """ヘッダを完全に制御してリクエストし、(status, ヘッダ, JSON) を返す。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.fixture()
def sid():
    return storage.create_session("本文", "ガードテスト", "/tmp/g")["id"]


# ---------- Host 検証（DNSリバインディング対策） ----------
def test_get_rejects_foreign_host(web_port, sid):
    """攻撃者ドメイン名でのアクセスは403。全セッション列挙を防ぐ。"""
    status, _, raw = _request(
        web_port, "/api/sessions", headers={"Host": f"evil.example:{web_port}"}
    )
    assert status == 403
    assert "Host" in json.loads(raw)["error"]


def test_get_session_rejects_foreign_host(web_port, sid):
    status, _, _ = _request(
        web_port, f"/api/sessions/{sid}", headers={"Host": "evil.example"}
    )
    assert status == 403


def test_post_rejects_foreign_host(web_port, sid):
    """リバインディング成立時の書き込みもHost検証で止まる。"""
    status, _, _ = _request(
        web_port,
        f"/api/sessions/{sid}/document",
        method="POST",
        headers={
            "Host": f"evil.example:{web_port}",
            "Content-Type": "application/json",
        },
        body={"html": "<p>改ざん</p>"},
    )
    assert status == 403
    assert storage.load_session(sid)["revisions"][-1]["edited_html"] is None


def test_localhost_host_is_allowed(web_port, sid):
    status, _, _ = _request(
        web_port, "/api/health", headers={"Host": f"localhost:{web_port}"}
    )
    assert status == 200


def test_static_asset_rejects_foreign_host(web_port):
    status, _, _ = _request(
        web_port, "/static/app.js", headers={"Host": "evil.example"}
    )
    assert status == 403


# ---------- Origin 検証（CSRF対策） ----------
def test_post_rejects_foreign_origin(web_port, sid):
    status, _, raw = _request(
        web_port,
        f"/api/sessions/{sid}/document",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
        },
        body={"html": "<p>改ざん</p>"},
    )
    assert status == 403
    assert "Origin" in json.loads(raw)["error"]
    assert storage.load_session(sid)["revisions"][-1]["edited_html"] is None


def test_post_accepts_own_origin(web_port, sid):
    """ブラウザからの正当なPOST（自オリジン）は通る。"""
    status, _, _ = _request(
        web_port,
        f"/api/sessions/{sid}/document",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{web_port}",
        },
        body={"html": "<p>校正</p>"},
    )
    assert status == 200
    assert storage.load_session(sid)["revisions"][-1]["edited_html"] == "<p>校正</p>"


def test_post_without_origin_is_allowed(web_port, sid):
    """Originを送らないローカルCLI・テストは許可する（既存挙動の維持）。"""
    status, _, _ = _request(
        web_port,
        f"/api/sessions/{sid}/status",
        method="POST",
        headers={"Content-Type": "application/json"},
        body={"status": "reviewed"},
    )
    assert status == 200
    assert storage.load_session(sid)["status"] == "reviewed"


# ---------- Content-Type 必須化（simple request 封じ） ----------
def test_post_rejects_text_plain(web_port, sid):
    """text/plain はプリフライトなしで飛ばせるため拒否する。"""
    status, _, raw = _request(
        web_port,
        f"/api/sessions/{sid}/document",
        method="POST",
        headers={"Content-Type": "text/plain"},
        body={"html": "<p>改ざん</p>"},
    )
    assert status == 403
    assert "Content-Type" in json.loads(raw)["error"]
    assert storage.load_session(sid)["revisions"][-1]["edited_html"] is None


def test_post_rejects_missing_content_type(web_port, sid):
    status, _, _ = _request(
        web_port,
        f"/api/sessions/{sid}/comments",
        method="POST",
        headers={"Content-Type": ""},
        body={"quote": "q", "text": "t"},
    )
    assert status == 403


def test_comment_update_is_guarded(web_port, sid):
    """更新・削除もPOSTのサブパスなので既存ガードがそのまま効く。

    DELETE/PATCH を増やすと _guard_reason の Origin/Content-Type 検査が
    POST 限定であるために素通りしてしまう。その回避策が効いていることを
    ここで担保する。
    """
    storage.add_comment(sid, "q", "元の本文", comment_id="c1")
    status, _, _ = _request(
        web_port,
        f"/api/sessions/{sid}/comments/c1/update",
        method="POST",
        headers={"Content-Type": "text/plain"},
        body={"text": "改ざん"},
    )
    assert status == 403
    assert storage.load_session(sid)["comments"][0]["text"] == "元の本文"


def test_comment_delete_rejects_foreign_origin(web_port, sid):
    storage.add_comment(sid, "q", "残るべき", comment_id="c1")
    status, _, raw = _request(
        web_port,
        f"/api/sessions/{sid}/comments/c1/delete",
        method="POST",
        headers={"Content-Type": "application/json",
                 "Origin": f"http://evil.example:{web_port}"},
        body={},
    )
    assert status == 403
    assert "Origin" in json.loads(raw)["error"]
    assert len(storage.load_session(sid)["comments"]) == 1


def test_post_accepts_content_type_with_charset(web_port, sid):
    status, _, _ = _request(
        web_port,
        f"/api/sessions/{sid}/document",
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body={"html": "<p>ok</p>"},
    )
    assert status == 200


# ---------- セキュリティヘッダ ----------
def test_html_security_headers(web_port, sid):
    status, headers, _ = _request(web_port, f"/session/{sid}")
    assert status == 200
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    csp = headers.get("Content-Security-Policy") or ""
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp


def test_json_security_headers(web_port):
    _, headers, _ = _request(web_port, "/api/health")
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in (headers.get("Content-Security-Policy") or "")


# ---------- メディアの添付（生のバイト列） ----------
def _upload(port: int, sid: str, name: str, data: bytes, headers: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/sessions/{sid}/files?name={name}",
        data=data, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_upload_accepts_octet_stream_from_own_origin(web_port, sid):
    status, res = _upload(web_port, sid, "clip.webm", b"webm", {
        "Content-Type": "application/octet-stream",
        "Origin": f"http://127.0.0.1:{web_port}",
    })
    assert status == 200 and res["name"] == "clip.webm"


@pytest.mark.parametrize("ctype", [
    # simple request で送れる型（プリフライトなしで他サイトから届く）
    "text/plain", "multipart/form-data; boundary=x",
    "application/x-www-form-urlencoded",
    # JSON でも添付の口には受けない（中身をファイルとして書いてしまう）
    "application/json", "",
])
def test_upload_requires_octet_stream(web_port, sid, ctype):
    status, res = _upload(web_port, sid, "clip.webm", b"webm",
                          {"Content-Type": ctype} if ctype else {})
    assert status == 403
    assert "application/octet-stream" in res["error"]
    assert storage.list_images(sid) == []


def test_upload_rejects_foreign_origin(web_port, sid):
    status, res = _upload(web_port, sid, "clip.webm", b"webm", {
        "Content-Type": "application/octet-stream",
        "Origin": "https://evil.example",
    })
    assert status == 403 and "Origin" in res["error"]
    assert storage.list_images(sid) == []


def test_octet_stream_is_not_accepted_elsewhere(web_port, sid):
    """octet-stream を許すのは添付の口だけ。他の書き込みは JSON 必須のまま。"""
    status, _, _ = _request(
        web_port, f"/api/sessions/{sid}/document", method="POST",
        headers={"Content-Type": "application/octet-stream"},
        body={"html": "<p>改ざん</p>"},
    )
    assert status == 403
    assert storage.load_session(sid)["revisions"][-1]["edited_html"] is None


def test_editor_csp_allows_same_origin_media(web_port, sid):
    """本文の動画・音声（同じオリジンの添付）を読み込めるCSPになっている。"""
    _, headers, _ = _request(web_port, f"/session/{sid}")
    csp = headers.get("Content-Security-Policy") or ""
    assert "media-src 'self'" in csp
    assert "media-src 'self' data:" not in csp  # 外部や data: は不要

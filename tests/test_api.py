"""Webサーバー JSON API の統合テスト。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from hle_mcp import storage


def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def _get_raw(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as res:
        return res.status, res.headers.get("Content-Type", ""), res.read()


def _post(port: int, path: str, body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def _status_of(err_or_call):
    try:
        err_or_call()
        return None
    except urllib.error.HTTPError as e:
        return e.code


def test_health(web_port):
    status, data = _get(web_port, "/api/health")
    assert status == 200
    assert data == {"app": "hle-mcp", "ok": True}


def test_index_and_session_page_serve_html(web_port):
    for path in ("/", "/session/abc123"):
        status, ctype, body = _get_raw(web_port, path)
        assert status == 200
        assert "text/html" in ctype
        assert b'id="editor"' in body


def test_static_files_served(web_port):
    status, ctype, _ = _get_raw(web_port, "/static/style.css")
    assert status == 200 and "text/css" in ctype
    status, ctype, _ = _get_raw(web_port, "/static/tracker.js")
    assert status == 200 and "javascript" in ctype


def test_static_path_traversal_blocked(web_port):
    # Path(name).name によりファイル名部分のみ解決される
    code = _status_of(lambda: _get_raw(web_port, "/static/..%2F..%2Fstorage.py"))
    assert code == 404


def test_unknown_paths_404(web_port):
    assert _status_of(lambda: _get(web_port, "/api/nope")) == 404
    assert _status_of(lambda: _get(web_port, "/api/sessions/missing00")) == 404


def test_sessions_list_and_filter(web_port):
    a = storage.create_session("x", "設計書レビュー", "/Users/me/alpha")
    storage.create_session("y", "記事下書き", "/Users/me/beta")
    _, data = _get(web_port, "/api/sessions")
    assert len(data["sessions"]) == 2
    _, data = _get(web_port, "/api/sessions?query=%E8%A8%AD%E8%A8%88")
    assert [s["id"] for s in data["sessions"]] == [a["id"]]
    _, data = _get(web_port, "/api/sessions?workspace=beta")
    assert len(data["sessions"]) == 1
    _, data = _get(web_port, "/api/sessions?query=zzz")
    assert data["sessions"] == []


def test_get_session_detail(web_port):
    s = storage.create_session("本文", "詳細", "/w")
    status, data = _get(web_port, f"/api/sessions/{s['id']}")
    assert status == 200
    assert data["revisions"][0]["base_text"] == "本文"


def test_post_document_and_status(web_port):
    s = storage.create_session("t", "d", "")
    status, data = _post(web_port, f"/api/sessions/{s['id']}/document",
                         {"html": "<p><del>t</del></p>"})
    assert status == 200 and data["ok"]
    assert storage.load_session(s["id"])["revisions"][0]["edited_html"] \
        == "<p><del>t</del></p>"

    status, _ = _post(web_port, f"/api/sessions/{s['id']}/status",
                      {"status": "reviewed"})
    assert status == 200
    assert storage.load_session(s["id"])["status"] == "reviewed"


def test_post_invalid_status_400(web_port):
    s = storage.create_session("t", "d", "")
    code = _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/status", {"status": "bad"}))
    assert code == 400


def test_comment_and_reply_flow(web_port):
    s = storage.create_session("t", "d", "")
    status, comment = _post(web_port, f"/api/sessions/{s['id']}/comments",
                            {"id": "c1", "quote": "引用", "text": "指摘"})
    assert status == 200 and comment["id"] == "c1"

    status, reply = _post(
        web_port, f"/api/sessions/{s['id']}/comments/c1/replies",
        {"text": "回答", "author": "ai"})
    assert status == 200 and reply["author"] == "ai"

    loaded = storage.load_session(s["id"])
    assert loaded["comments"][0]["replies"][0]["text"] == "回答"


def test_duplicate_comment_400_and_missing_comment_404(web_port):
    s = storage.create_session("t", "d", "")
    _post(web_port, f"/api/sessions/{s['id']}/comments",
          {"id": "dup", "quote": "", "text": "1"})
    code = _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments",
        {"id": "dup", "quote": "", "text": "2"}))
    assert code == 400
    code = _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments/none/replies",
        {"text": "x"}))
    assert code == 404


def test_comment_update_and_delete_endpoints(web_port):
    s = storage.create_session("t", "d", "")
    _post(web_port, f"/api/sessions/{s['id']}/comments",
          {"id": "c1", "quote": "引用", "text": "指摘"})
    _, reply = _post(web_port, f"/api/sessions/{s['id']}/comments/c1/replies",
                     {"text": "回答", "author": "ai"})
    rid = reply["id"]

    status, updated = _post(
        web_port, f"/api/sessions/{s['id']}/comments/c1/update",
        {"text": "直した指摘"})
    assert status == 200 and updated["text"] == "直した指摘"

    status, upd_reply = _post(
        web_port, f"/api/sessions/{s['id']}/comments/c1/replies/{rid}/update",
        {"text": "直した回答"})
    assert status == 200 and upd_reply["text"] == "直した回答"

    status, data = _post(
        web_port,
        f"/api/sessions/{s['id']}/comments/c1/replies/{rid}/delete", {})
    assert status == 200 and data == {"ok": True}
    loaded = storage.load_session(s["id"])
    assert loaded["comments"][0]["replies"] == []
    assert loaded["comments"][0]["text"] == "直した指摘"  # 本体は残る

    status, data = _post(
        web_port, f"/api/sessions/{s['id']}/comments/c1/delete", {})
    assert status == 200 and data == {"ok": True}
    assert storage.load_session(s["id"])["comments"] == []


def test_comment_update_delete_error_codes(web_port):
    s = storage.create_session("t", "d", "")
    _post(web_port, f"/api/sessions/{s['id']}/comments",
          {"id": "c1", "quote": "", "text": "指摘"})
    # 存在しない対象は404
    assert _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments/none/update",
        {"text": "x"})) == 404
    assert _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments/none/delete", {})) == 404
    assert _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments/c1/replies/none/delete",
        {})) == 404
    # 空文字への更新は400（削除と区別する）
    assert _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments/c1/update",
        {"text": "  "})) == 400
    assert storage.load_session(s["id"])["comments"][0]["text"] == "指摘"


def test_deleting_comment_renumbers_remaining(web_port):
    """削除するとC番号が繰り上がる。AIは get_feedback を取り直す必要がある。"""
    s = storage.create_session("t", "d", "")
    for i, cid in enumerate(("c1", "c2", "c3"), 1):
        _post(web_port, f"/api/sessions/{s['id']}/comments",
              {"id": cid, "quote": "", "text": f"{i}つ目"})
    _post(web_port, f"/api/sessions/{s['id']}/comments/c1/delete", {})
    _, fb = _get(web_port, f"/api/sessions/{s['id']}/feedback")
    assert [(c["number"], c["text"]) for c in fb["comments"]] \
        == [(1, "2つ目"), (2, "3つ目")]


def test_post_to_missing_session_404(web_port):
    code = _status_of(lambda: _post(
        web_port, "/api/sessions/nosession/document", {"html": "<p>x</p>"}))
    assert code == 404


def test_post_invalid_json_400(web_port):
    s = storage.create_session("t", "d", "")
    req = urllib.request.Request(
        f"http://127.0.0.1:{web_port}/api/sessions/{s['id']}/document",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        code = None
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400


# ---------- 画像・admin・feedback API ----------

def test_image_upload_and_serve(web_port):
    import base64

    from .conftest import PNG_1PX

    s = storage.create_session("t", "d", "")
    status, res = _post(
        web_port, f"/api/sessions/{s['id']}/images",
        {"name": "shot.png",
         "data_base64": base64.b64encode(PNG_1PX).decode()})
    assert status == 200
    assert res["name"] == "shot.png"

    status, ctype, body = _get_raw(web_port, res["url"])
    assert status == 200
    assert ctype == "image/png"
    assert body == PNG_1PX


def test_image_upload_invalid(web_port):
    s = storage.create_session("t", "d", "")
    # 不正base64
    code = _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/images",
        {"name": "a.png", "data_base64": "!!!"}))
    assert code == 400
    # 不正拡張子
    code = _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/images",
        {"name": "a.svg", "data_base64": "aGk="}))
    assert code == 400
    # 存在しないセッション
    code = _status_of(lambda: _post(
        web_port, "/api/sessions/nosession/images",
        {"name": "a.png", "data_base64": "aGk="}))
    assert code == 404


def test_missing_image_file_404(web_port):
    s = storage.create_session("t", "d", "")
    code = _status_of(
        lambda: _get_raw(web_port, f"/api/sessions/{s['id']}/files/none.png"))
    assert code == 404


def test_feedback_endpoint(web_port):
    s = storage.create_session("原文です。", "d", "")
    storage.set_document(s["id"], "<p>原文<del>です</del>。</p>")
    status, fb = _get(web_port, f"/api/sessions/{s['id']}/feedback")
    assert status == 200
    assert fb["annotated_text"] == "原文【削除:です】。"
    assert fb["result_text"] == "原文。"


def test_admin_page_served(web_port):
    status, ctype, body = _get_raw(web_port, "/admin")
    assert status == 200
    assert "text/html" in ctype
    assert "管理画面".encode() in body
    assert b"admin.js" in body

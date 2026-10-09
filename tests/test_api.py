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
    assert data["app"] == "hle-mcp" and data["ok"] is True
    # 稼働中のコードの版とディスク上の版を報告する（古いサーバーの検出用）
    assert data["version"] == data["disk_version"]
    assert len(data["version"]) == 12
    # テスト用のスレッド起動はスーパーバイザー管理下ではない
    assert data["supervised"] is False and data["supervisor_pid"] is None


def test_every_response_carries_build_version(web_port):
    """開いているページが新しい版を知れるよう、全応答に版を載せる。"""
    s = storage.create_session("t", "d", "")
    versions = set()
    for path in ("/api/health", f"/api/sessions/{s['id']}", "/static/app.js"):
        with urllib.request.urlopen(f"http://127.0.0.1:{web_port}{path}") as res:
            versions.add(res.headers.get("X-HLE-Version"))
    assert len(versions) == 1 and None not in versions


def test_post_document_with_revision_guard(web_port):
    """前の稿への保存は新しい稿を上書きせず、stale として退避される。"""
    s = storage.create_session("初稿", "d", "")
    path = f"/api/sessions/{s['id']}/document"
    _, data = _post(web_port, path, {"html": "<p>A</p>", "revision": 0})
    assert data == {"ok": True, "stale": False}
    storage.add_revision(s["id"], "第二稿")
    _, data = _post(web_port, path, {"html": "<p>B</p>", "revision": 0})
    assert data == {"ok": True, "stale": True}
    loaded = storage.load_session(s["id"])
    assert loaded["revisions"][1]["edited_html"] is None
    assert loaded["revisions"][0]["late_edited_html"] == "<p>B</p>"
    for bad in (5, "0", True):
        assert _status_of(lambda: _post(
            web_port, path, {"html": "<p>x</p>", "revision": bad})) == 400


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


def test_image_comment_endpoint(web_port):
    """画像コメントの target / image がAPI経由で保存される。"""
    s = storage.create_session("本文\n![図](a.png)", "d", "")
    status, comment = _post(
        web_port, f"/api/sessions/{s['id']}/comments",
        {"id": "ci1", "quote": "![図](a.png)", "text": "この図を差し替えて",
         "target": "image", "image": "a.png"})
    assert status == 200
    assert comment["target"] == "image" and comment["image"] == "a.png"
    _, fb = _get(web_port, f"/api/sessions/{s['id']}/feedback")
    assert fb["comments"][0]["image"] == "a.png"


def test_image_comment_endpoint_rejects_bad_anchor(web_port):
    s = storage.create_session("<p>本文</p>", "d", "", mode="html")
    assert _status_of(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments",
        {"id": "ci2", "quote": "図", "text": "x",
         "target": "image", "image_ref": "img:0 svg:1"})) == 400


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


def test_comment_tag_endpoints(web_port):
    """質問タグはAPIで付けられ、更新で付け外しでき、フィードバックに現れる。"""
    s = storage.create_session("t", "d", "")
    base = f"/api/sessions/{s['id']}/comments"
    status, c = _post(web_port, base, {
        "id": "cq", "quote": "引用", "text": "これは何？", "tag": "question"})
    assert status == 200 and c["tag"] == "question"
    status, c = _post(web_port, base, {
        "id": "ci", "quote": "引用", "text": "直して"})
    assert status == 200 and "tag" not in c  # 既定は指摘
    _, fb = _get(web_port, f"/api/sessions/{s['id']}/feedback")
    assert [(x["id"], x["tag"]) for x in fb["comments"]] \
        == [("cq", "question"), ("ci", "issue")]

    # 更新で種類を変えられる。tag を送らない更新では変わらない
    _, c = _post(web_port, f"{base}/cq/update", {"text": "直して", "tag": "issue"})
    assert "tag" not in c
    _, c = _post(web_port, f"{base}/ci/update",
                 {"text": "これは何？", "tag": "question"})
    assert c["tag"] == "question"
    _, c = _post(web_port, f"{base}/ci/update", {"text": "本文だけ"})
    assert c["tag"] == "question"


def test_comment_tag_rejects_unknown_value(web_port):
    s = storage.create_session("t", "d", "")
    base = f"/api/sessions/{s['id']}/comments"
    assert _status_of(lambda: _post(web_port, base, {
        "id": "c1", "quote": "", "text": "x", "tag": "bogus"})) == 400
    _post(web_port, base, {"id": "c2", "quote": "", "text": "x"})
    assert _status_of(lambda: _post(
        web_port, f"{base}/c2/update", {"text": "y", "tag": "bogus"})) == 400
    comments = storage.load_session(s["id"])["comments"]
    assert [(c["id"], c["text"]) for c in comments] == [("c2", "x")]


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


# ---------- メディア（画像・動画・音声） ----------
import http.client  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from hle_mcp import webserver  # noqa: E402

MEDIA_DIR = Path(__file__).parent / "fixtures" / "media"


def _raw(port: int, method: str, path: str, headers=None, body=None):
    """http.client で送り、(status, ヘッダ dict, 本文) を返す。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
    finally:
        conn.close()


def _upload(port, sid, name, data, **headers):
    hdrs = {"Content-Type": "application/octet-stream", **headers}
    from urllib.parse import quote
    return _raw(port, "POST", f"/api/sessions/{sid}/files?name={quote(name)}",
                hdrs, data)


def test_media_config_endpoint(web_port):
    status, cfg = _get(web_port, "/api/media")
    assert status == 200
    assert cfg["types"][".mp4"] == {"kind": "video", "mime": "video/mp4"}
    assert cfg["types"][".mp3"] == {"kind": "audio", "mime": "audio/mpeg"}
    assert cfg["types"][".gif"]["kind"] == "image"
    assert ".svg" not in cfg["types"]
    assert cfg["max_bytes"] == storage.MAX_MEDIA_BYTES
    assert cfg["labels"] == {"image": "画像", "video": "動画", "audio": "音声"}


@pytest.mark.parametrize(("fixture", "mime"), [
    ("clip.mp4", "video/mp4"), ("clip.mov", "video/quicktime"),
    ("clip.webm", "video/webm"), ("tone.mp3", "audio/mpeg"),
    ("tone.m4a", "audio/mp4"), ("tone.ogg", "audio/ogg"),
    ("tone.wav", "audio/wav"), ("anim.gif", "image/gif"),
])
def test_raw_upload_and_serve_media(web_port, fixture, mime):
    """生のバイト列で添付し、種類に合った Content-Type で配信する。"""
    data = (MEDIA_DIR / fixture).read_bytes()
    s = storage.create_session("t", "d", "")
    status, _, raw = _upload(web_port, s["id"], "添付 " + fixture, data)
    res = json.loads(raw)
    assert status == 200
    assert res["name"] == "添付_" + fixture  # 名前は保存用に整える
    assert res["kind"] == storage.media_kind(fixture)
    status, headers, body = _raw(web_port, "GET", res["url"])
    assert status == 200
    assert headers["content-type"] == mime
    assert headers["accept-ranges"] == "bytes"
    assert headers["content-length"] == str(len(data))
    assert body == data


def test_raw_upload_rejections(web_port, monkeypatch):
    s = storage.create_session("t", "d", "")
    # 非対応の形式
    status, _, raw = _upload(web_port, s["id"], "a.pdf", b"%PDF")
    assert status == 400 and "対応していない形式" in json.loads(raw)["error"]
    # 空
    status, _, _ = _upload(web_port, s["id"], "a.mp4", b"")
    assert status == 400
    # 存在しないセッション
    status, _, _ = _upload(web_port, "nosession", "a.mp4", b"x")
    assert status == 404
    # 上限を超える大きさは Content-Length の時点で断る
    monkeypatch.setitem(storage.MAX_MEDIA_BYTES, "video", 100)
    status, _, raw = _upload(web_port, s["id"], "big.mp4", b"0" * 101)
    assert status == 400 and "動画は" in json.loads(raw)["error"]
    assert storage.list_images(s["id"]) == []


def test_raw_upload_large_body_is_streamed_to_disk(web_port, monkeypatch):
    """JSON の上限（20MB）を超える動画も受け、分割して書く。"""
    monkeypatch.setattr(storage, "_COPY_CHUNK", 64 * 1024)
    s = storage.create_session("t", "d", "")
    data = bytes(range(256)) * (25 * 1024 * 1024 // 256)  # 25MB
    status, _, raw = _upload(web_port, s["id"], "long.mp4", data)
    assert status == 200
    name = json.loads(raw)["name"]
    assert storage.get_image_path(s["id"], name).read_bytes() == data


@pytest.mark.parametrize(("header", "status", "expect"), [
    ("bytes=0-9", 206, (0, 9)),
    ("bytes=10-", 206, (10, 99)),
    ("bytes=-5", 206, (95, 99)),
    ("bytes=90-500", 206, (90, 99)),   # 末尾を超える指定は末尾まで
    ("bytes=-500", 206, (0, 99)),
    ("bytes=0-0", 206, (0, 0)),
    ("bytes=100-", 416, None),          # 先頭がファイルの外
    ("bytes=-0", 416, None),
    ("bytes=0-1,5-6", 200, None),       # 複数範囲は扱わず全体を返す
    ("bytes=5-2", 200, None),           # 書式の誤りは無視して全体
    ("items=0-1", 200, None),
    ("bytes=abc", 200, None),
])
def test_range_requests(web_port, header, status, expect):
    data = bytes(range(100))
    s = storage.create_session("t", "d", "")
    name = storage.save_image_bytes(s["id"], "clip.mp4", data)
    got, headers, body = _raw(web_port, "GET",
                              f"/api/sessions/{s['id']}/files/{name}",
                              {"Range": header})
    assert got == status
    assert headers["accept-ranges"] == "bytes"
    if status == 206:
        a, b = expect
        assert body == data[a:b + 1]
        assert headers["content-range"] == f"bytes {a}-{b}/100"
        assert headers["content-length"] == str(b - a + 1)
    elif status == 416:
        assert headers["content-range"] == "bytes */100"
        assert body == b""
    else:
        assert body == data and "content-range" not in headers


def test_range_keeps_connection_usable(web_port):
    """206 の後も keep-alive の接続で続けて取れる（シークを繰り返す再生）。"""
    data = bytes(range(200))
    s = storage.create_session("t", "d", "")
    name = storage.save_image_bytes(s["id"], "a.webm", data)
    conn = http.client.HTTPConnection("127.0.0.1", web_port, timeout=10)
    try:
        for a, b in ((0, 1), (150, 199), (10, 20)):
            conn.request("GET", f"/api/sessions/{s['id']}/files/{name}",
                         headers={"Range": f"bytes={a}-{b}"})
            res = conn.getresponse()
            assert res.status == 206
            assert res.read() == data[a:b + 1]
    finally:
        conn.close()


def test_parse_range_unit():
    assert webserver.parse_range(None, 10) is None
    assert webserver.parse_range("bytes=0-", 10) == (0, 9)
    assert webserver.parse_range("BYTES = 2-3", 10) == (2, 3)
    assert webserver.parse_range("bytes=-3", 10) == (7, 9)
    assert webserver.parse_range("bytes=10-", 10) == "unsatisfiable"
    assert webserver.parse_range("bytes=-", 10) is None
    assert webserver.parse_range("bytes=0-1", 0) == "unsatisfiable"


def test_drain_does_not_wait_for_stalled_media_stream(monkeypatch):
    """ブラウザが受信を止めた動画の送信は、入れ替えの drain を待たせない。

    動画の要素は再生位置まで読むと受信を止め、送信側の書き込みは何分も
    返らなくなる。drain はその接続を切って即座に終わる（ブラウザは続きを
    Range で取り直す）。保存のような処理中のリクエストは従来どおり待つ。
    """
    s = storage.create_session("t", "d", "")
    name = storage.save_image_bytes(
        s["id"], "big.mp4", b"\x00" * (8 * 1024 * 1024))
    srv = webserver._Server(("127.0.0.1", 0), webserver._Handler)
    thread = threading.Thread(target=srv.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = srv.server_address[1]
    sock = socket.create_connection(("127.0.0.1", port))
    # 受信バッファを小さくして、送信側が書き込みで止まるようにする
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        sock.sendall(
            f"GET /api/sessions/{s['id']}/files/{name} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n\r\n".encode())
        assert b"200 OK" in sock.recv(1024)  # 送信が始まった
        deadline = time.monotonic() + 5
        while not srv._streams and time.monotonic() < deadline:
            time.sleep(0.02)
        assert srv._streams, "送信中の接続として登録されていない"
        srv.shutdown()
        t0 = time.monotonic()
        assert srv.drain(10.0) is True
        assert time.monotonic() - t0 < 2.0
        assert not srv._streams
    finally:
        sock.close()
        srv.server_close()

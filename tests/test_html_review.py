"""HTMLレビューモードの統合テスト（storage / feedback / Web API / MCPツール）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from hle_mcp import feedback, server, storage

HTML_DOC = (
    "<!doctype html><html><head><style>body{color:rgb(1,2,3)}</style></head>"
    "<body><h1>見出し</h1><p>これは<b>重要</b>な点です。</p>"
    "<p>これは別の点です。</p></body></html>"
)


def _html_session():
    return storage.create_session(
        HTML_DOC, "図解レビュー", "/ws", mode="html",
        source_path="/ws/diagram.html")


def _get_raw(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as res:
        return res.status, dict(res.headers), res.read()


def _post(port: int, path: str, body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def _http_status(call):
    try:
        call()
        return None
    except urllib.error.HTTPError as e:
        return e.code


# ---------- storage ----------

def test_create_session_invalid_mode_raises():
    with pytest.raises(ValueError):
        storage.create_session("x", "t", mode="pdf")


def test_session_mode_defaults_to_md_for_old_data():
    s = storage.create_session("x", "t")
    assert storage.session_mode(s) == "md"
    s.pop("mode")  # 旧フォーマットのセッションを模擬
    assert storage.session_mode(s) == "md"


def test_add_comment_stores_prefix_suffix_only_when_present():
    s = _html_session()
    c1 = storage.add_comment(s["id"], "重要", "指摘", prefix="これは", suffix="な点")
    assert (c1["prefix"], c1["suffix"]) == ("これは", "な点")
    c2 = storage.add_comment(s["id"], "別", "指摘2")
    assert "prefix" not in c2 and "suffix" not in c2


# ---------- feedback ----------

def test_build_feedback_html_mode_excludes_body():
    s = _html_session()
    storage.add_comment(s["id"], "重要な点", "図の配色を変えたい",
                        prefix="見出しこれは", suffix="です。これは別")
    fb = feedback.build_feedback(storage.load_session(s["id"]))
    assert fb["mode"] == "html"
    assert fb["source_path"] == "/ws/diagram.html"
    # HTML全文はどのキーにも含まれない（AIのコンテキストを守る）
    assert "original_text" not in fb
    assert "result_text" not in fb
    assert "annotated_text" not in fb
    assert HTML_DOC not in json.dumps(fb, ensure_ascii=False)
    c = fb["comments"][0]
    assert (c["number"], c["quote"]) == (1, "重要な点")
    assert c["prefix"] == "見出しこれは"
    assert c["suffix"] == "です。これは別"
    assert "source_path" in fb["note"] or "quote" in fb["note"]


def test_build_feedback_md_mode_unchanged():
    s = storage.create_session("原文です。", "T")
    storage.set_document(s["id"], "<p>原文<del>です</del>。</p>")
    fb = feedback.build_feedback(storage.load_session(s["id"]))
    assert fb["mode"] == "md"
    assert fb["result_text"] == "原文。"
    assert "source_path" not in fb


# ---------- Web API ----------

def test_session_page_served_by_mode(web_port):
    html_s = _html_session()
    md_s = storage.create_session("md本文", "md")
    _, _, body = _get_raw(web_port, f"/session/{html_s['id']}")
    assert b"html-review.js" in body
    _, _, body = _get_raw(web_port, f"/session/{md_s['id']}")
    assert b'id="editor"' in body


def test_source_endpoint_serves_document_with_frame_csp(web_port):
    s = _html_session()
    status, headers, body = _get_raw(
        web_port, f"/api/sessions/{s['id']}/source")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert body.decode("utf-8") == HTML_DOC
    # 同一オリジンのiframe埋め込みだけを許可する
    assert headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'self'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "script-src" not in csp  # default-src 'none' でスクリプト全禁止


def test_source_endpoint_rejects_md_and_missing(web_port):
    md_s = storage.create_session("md本文", "md")
    assert _http_status(lambda: _get_raw(
        web_port, f"/api/sessions/{md_s['id']}/source")) == 404
    assert _http_status(lambda: _get_raw(
        web_port, "/api/sessions/nosession/source")) == 404


def test_review_page_keeps_deny_frame_options(web_port):
    """レビューページ自体は従来どおり埋め込み禁止のまま。"""
    s = _html_session()
    _, headers, _ = _get_raw(web_port, f"/session/{s['id']}")
    assert headers.get("X-Frame-Options") == "DENY"
    assert "frame-src 'self'" in headers.get("Content-Security-Policy", "")


def test_comment_api_accepts_prefix_suffix(web_port):
    s = _html_session()
    status, comment = _post(
        web_port, f"/api/sessions/{s['id']}/comments",
        {"id": "c1", "quote": "重要", "text": "指摘",
         "prefix": "これは", "suffix": "な点です"})
    assert status == 200
    loaded = storage.load_session(s["id"])["comments"][0]
    assert (loaded["prefix"], loaded["suffix"]) == ("これは", "な点です")


# ---------- MCPツール ----------

def test_open_review_html_file(write_md):
    path = write_md(HTML_DOC, name="diagram.html")
    out = json.loads(server.open_review(path, "図解"))
    assert out["mode"] == "html"
    assert out["file_path"] == path
    s = storage.load_session(out["session_id"])
    assert storage.session_mode(s) == "html"
    assert s["source_path"] == path
    assert s["revisions"][0]["base_text"] == HTML_DOC


def test_open_review_html_skips_image_resolution(write_md):
    """md用の画像参照解決はHTMLでは動かない（誤検出させない）。"""
    path = write_md(
        "<html><body><p>![図](nai.png) はただの文字列</p></body></html>",
        name="page.html")
    out = json.loads(server.open_review(path, "画像なし"))
    assert out["missing_images"] == []
    assert out["images"] == []


def test_submit_revision_html_flow(write_md):
    out = json.loads(server.open_review(
        write_md(HTML_DOC, name="v1.html"), "図解"))
    sid = out["session_id"]
    rev_path = write_md(
        HTML_DOC.replace("重要", "とても重要"), name="v2.html")
    rev = json.loads(server.submit_revision(sid, rev_path, open_browser=False))
    assert rev["revision_index"] == 1
    s = storage.load_session(sid)
    assert "とても重要" in s["revisions"][1]["base_text"]
    assert s["source_path"] == rev_path  # 入力元は最新ファイルを指す


def test_submit_revision_mode_mismatch_raises(write_md):
    md_out = json.loads(server.open_review(write_md("md本文"), "md文書"))
    with pytest.raises(ValueError):
        server.submit_revision(
            md_out["session_id"], write_md(HTML_DOC, name="x.html"),
            open_browser=False)


def test_get_feedback_html_mode_via_tool(write_md):
    path = write_md(HTML_DOC, name="d.html")
    out = json.loads(server.open_review(path, "図解"))
    storage.add_comment(out["session_id"], "重要", "ここがこう",
                        prefix="これは", suffix="な点")
    fb = json.loads(server.get_feedback(out["session_id"]))
    assert fb["mode"] == "html"
    assert fb["source_path"] == path
    assert "original_text" not in fb
    assert fb["comments"][0]["prefix"] == "これは"

"""HTMLレビューモードの統合テスト（storage / feedback / Web API / MCPツール）。"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from hle_mcp import feedback, server, storage
from tests.conftest import make_png

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


# ---------- 範囲コメント（ドラッグで囲んだ範囲 + スクリーンショット） ----------

REGION = {"x": 120.5, "y": 340, "width": 400, "height": 200,
          "frame_width": 1080, "frame_height": 834,
          "ref": "svg:0", "rel": [0.1, 0.3, 0.37, 0.2],
          "svg_box": [100.0, 180.0, 370.0, 120.0]}


def _region_comment(sid: str, **kw):
    args = dict(screenshot=make_png(800, 400), text="ここの矢印の向きが逆",
                region=REGION, texts=["左上の箱", "  ", "矢印のラベル"])
    args.update(kw)
    return storage.add_region_comment(sid, **args)


def test_add_region_comment_saves_screenshot_and_fields():
    s = _html_session()
    c = _region_comment(s["id"], comment_id="c1")
    assert c["target"] == "region"
    assert c["image"] == "region-c1.png"
    assert c["screenshot_size"] == [800, 400]
    assert c["region"]["ref"] == "svg:0"
    assert c["region"]["svg_box"] == [100.0, 180.0, 370.0, 120.0]
    assert c["texts"] == ["左上の箱", "矢印のラベル"]  # 空白だけの断片は捨てる
    assert c["quote"] == "左上の箱 / 矢印のラベル"  # 見出しは文字列から作る
    saved = storage.get_image_path(s["id"], "region-c1.png").read_bytes()
    assert saved == make_png(800, 400)
    assert storage.load_session(s["id"])["comments"][0]["id"] == "c1"


@pytest.mark.parametrize("shot, message", [
    (b"GIF89a" + b"\x00" * 40, "PNG"),
    (make_png(1601, 10), "長辺"),
    (b"", "空"),
])
def test_region_screenshot_is_validated(shot, message):
    s = _html_session()
    with pytest.raises(ValueError, match=message):
        _region_comment(s["id"], screenshot=shot)
    assert storage.list_images(s["id"]) == []
    assert storage.load_session(s["id"])["comments"] == []


def test_region_screenshot_bytes_limit(monkeypatch):
    monkeypatch.setattr(storage, "REGION_SHOT_MAX_BYTES", 100)
    s = _html_session()
    with pytest.raises(ValueError, match="MB以下"):
        _region_comment(s["id"], screenshot=make_png(300, 300, (1, 2, 3)) + b"\0" * 200)


@pytest.mark.parametrize("region", [
    None,
    {"x": 0, "y": 0, "width": 0, "height": 10},
    {"x": -5, "y": 0, "width": 10, "height": 10},
    {"x": 0, "y": 0, "width": "10", "height": 10},
    {"x": 0, "y": 0, "width": float("nan"), "height": 10},
    {"x": 0, "y": 0, "width": 10, "height": 10, "ref": "svg:0\" onload=x"},
    {"x": 0, "y": 0, "width": 10, "height": 10, "ref": "svg:0", "rel": [1, 2]},
    {"x": 0, "y": 0, "width": 10, "height": 10, "ref": "svg:0",
     "rel": [0, 0, 9, 1]},
])
def test_invalid_region_rejected_without_leaving_file(region):
    s = _html_session()
    with pytest.raises(ValueError):
        _region_comment(s["id"], region=region)
    # 検証に落ちたら保存した画像も消す（片方だけ残さない）
    assert storage.list_images(s["id"]) == []


def test_region_comment_rejected_for_md_session():
    s = storage.create_session("md本文", "md")
    with pytest.raises(ValueError, match="HTMLレビュー"):
        _region_comment(s["id"])
    assert storage.list_images(s["id"]) == []


def test_region_texts_are_capped():
    s = _html_session()
    many = [f"ラベル{i}" for i in range(storage.REGION_TEXTS_MAX + 20)]
    c = _region_comment(s["id"], texts=many)
    assert len(c["texts"]) == storage.REGION_TEXTS_MAX
    assert len(c["quote"]) <= 120


def test_deleting_region_comment_removes_screenshot():
    s = _html_session()
    _region_comment(s["id"], comment_id="c1")
    _region_comment(s["id"], comment_id="c2")
    storage.delete_comment(s["id"], "c1")
    assert storage.list_images(s["id"]) == ["region-c2.png"]


def test_region_comment_can_reference_uploaded_png_only():
    """add_comment に直接名前を渡す場合も、保存済みのPNGしか受け付けない。"""
    s = _html_session()
    with pytest.raises(ValueError, match="見つかりません"):
        storage.add_comment(s["id"], "", "x", target="region",
                            image="nai.png", region=REGION)
    jpg = storage.save_image_bytes(s["id"], "a.jpg", b"\xff\xd8\xff" + b"0" * 40)
    with pytest.raises(ValueError, match="PNG"):
        storage.add_comment(s["id"], "", "x", target="region",
                            image=jpg, region=REGION)


def test_feedback_region_view():
    s = _html_session()
    _region_comment(s["id"], comment_id="c1", tag="question")
    fb = feedback.build_feedback(storage.load_session(s["id"]))
    c = fb["comments"][0]
    assert c["target"] == "region"
    assert c["tag"] == "question"
    assert c["image"] == "region-c1.png"
    assert c["screenshot_size"] == [800, 400]
    assert c["region"]["svg_box"] == [100.0, 180.0, 370.0, 120.0]
    assert c["texts"] == ["左上の箱", "矢印のラベル"]
    assert 'target="region"' in fb["note"]


def test_region_comment_api_saves_png(web_port):
    s = _html_session()
    shot = make_png(64, 32)
    status, comment = _post(
        web_port, f"/api/sessions/{s['id']}/comments",
        {"id": "c1", "text": "ここ", "target": "region", "region": REGION,
         "texts": ["左上の箱"], "quote": "左上の箱",
         "screenshot": base64.b64encode(shot).decode()})
    assert status == 200
    assert comment["image"] == "region-c1.png"
    status, headers, body = _get_raw(
        web_port, f"/api/sessions/{s['id']}/files/region-c1.png")
    assert body == shot
    assert headers.get("Content-Type") == "image/png"


@pytest.mark.parametrize("shot", ["%%%", "R0lGODlhAQABAAAAACw=", ""])
def test_region_comment_api_rejects_bad_screenshot(web_port, shot):
    s = _html_session()
    status = _http_status(lambda: _post(
        web_port, f"/api/sessions/{s['id']}/comments",
        {"id": "c1", "text": "ここ", "target": "region", "region": REGION,
         "screenshot": shot}))
    assert status == 400
    assert storage.list_images(s["id"]) == []
    assert storage.load_session(s["id"])["comments"] == []


def _call_tool(name: str, args: dict):
    """インメモリのMCPクライアントでツールを呼ぶ（別スレッドでループを回す）。"""
    import asyncio
    import threading

    from mcp.client import Client

    result: dict = {}

    async def run():
        async with Client(server.server) as client:
            result["value"] = await client.call_tool(name, args)

    t = threading.Thread(target=lambda: asyncio.run(run()))
    t.start()
    t.join(timeout=60)
    return result["value"]


def test_get_feedback_attaches_region_screenshots(write_md):
    out = json.loads(server.open_review(write_md(HTML_DOC, name="d.html"), "図解"))
    sid = out["session_id"]
    _region_comment(sid, comment_id="c1", screenshot=make_png(40, 20, (0, 0, 255)))
    storage.add_comment(sid, "重要", "文字列への指摘", comment_id="c2")

    res = _call_tool("get_feedback", {"session_id": sid})
    assert res.is_error is False
    kinds = [b.type for b in res.content]
    assert kinds == ["text", "text", "image"]
    fb = json.loads(res.content[0].text)
    c1 = fb["comments"][0]
    shot = storage.get_image_path(sid, "region-c1.png").resolve()
    assert c1["screenshot"] == str(shot)
    assert fb["attached_screenshots"] == [{"number": 1, "id": "c1"}]
    assert "C1" in res.content[1].text
    img = res.content[2]
    assert img.mime_type == "image/png"
    assert base64.b64decode(img.data) == shot.read_bytes()
    # 画像のbase64がJSON（テキスト）側に重複して入らない
    assert img.data not in res.content[0].text
    assert res.structured_content is None


def test_get_feedback_attaches_only_current_revision(write_md):
    out = json.loads(server.open_review(write_md(HTML_DOC, name="v1.html"), "図解"))
    sid = out["session_id"]
    _region_comment(sid, comment_id="old")
    server.submit_revision(sid, write_md(HTML_DOC, name="v2.html"),
                           open_browser=False)
    _region_comment(sid, comment_id="new")
    items = server.get_feedback(sid)
    fb = json.loads(items[0])
    assert fb["attached_screenshots"] == [{"number": 2, "id": "new"}]
    assert len([x for x in items if not isinstance(x, str)]) == 1
    # 前の稿のものもパスは渡す（必要なら開ける）
    assert fb["comments"][0]["screenshot"].endswith("region-old.png")


def test_get_feedback_without_screenshots_returns_text(write_md):
    out = json.loads(server.open_review(write_md(HTML_DOC, name="d.html"), "図解"))
    sid = out["session_id"]
    _region_comment(sid, comment_id="c1")
    fb = json.loads(server.get_feedback(sid, include_screenshots=False))
    assert "attached_screenshots" not in fb
    assert fb["comments"][0]["screenshot"].endswith("region-c1.png")


def test_get_feedback_caps_attached_screenshots(write_md):
    out = json.loads(server.open_review(write_md(HTML_DOC, name="d.html"), "図解"))
    sid = out["session_id"]
    for i in range(server.MAX_ATTACHED_SCREENSHOTS + 2):
        _region_comment(sid, comment_id=f"c{i}")
    items = server.get_feedback(sid)
    fb = json.loads(items[0])
    assert len(fb["attached_screenshots"]) == server.MAX_ATTACHED_SCREENSHOTS
    assert len([x for x in items if not isinstance(x, str)]) \
        == server.MAX_ATTACHED_SCREENSHOTS


def test_tool_description_explains_region_screenshots():
    from tests.test_mcp_tools import _tool_descriptions

    d = _tool_descriptions()["get_feedback"]
    assert 'target="region"' in d
    assert "include_screenshots" in d
    assert 'target="region"' in server.server.instructions


def test_region_comment_failure_after_saving_image_removes_only_new_file():
    """画像を書いた後にコメントの検証で落ちたら、今書いた画像だけを消す。"""
    s = _html_session()
    _region_comment(s["id"], comment_id="c1")
    # 同じIDは画像を別名（region-c1-1.png）で書いた後、コメントの追加で落ちる
    with pytest.raises(ValueError, match="重複"):
        _region_comment(s["id"], comment_id="c1")
    assert storage.list_images(s["id"]) == ["region-c1.png"]
    # 不正な種類も同じ（画像を書いた後に落ちる）
    with pytest.raises(ValueError, match="タグ"):
        _region_comment(s["id"], comment_id="c2", tag="bogus")
    assert storage.list_images(s["id"]) == ["region-c1.png"]
    assert [c["id"] for c in storage.load_session(s["id"])["comments"]] == ["c1"]


def test_get_feedback_md_mode_contract_via_mcp(write_md):
    """md モードの get_feedback は JSON のテキスト1つだけを返す（構造化出力なし）。

    画像を添付できるよう get_feedback は非構造化のツールにした（MCP Python SDK
    の説明でも、画像は出力スキーマを持たない content とされる）。以前の
    outputSchema {"result": string} と structuredContent は返さない。本文の
    JSON テキストは以前と同じ。
    """
    import asyncio
    import threading

    from mcp.client import Client

    out = json.loads(server.open_review(write_md("初稿です。"), "契約"))
    sid = out["session_id"]
    result: dict = {}

    async def run():
        async with Client(server.server) as client:
            result["tools"] = await client.list_tools()
            result["res"] = await client.call_tool("get_feedback", {"session_id": sid})

    t = threading.Thread(target=lambda: asyncio.run(run()))
    t.start()
    t.join(timeout=60)
    tool = next(x for x in result["tools"].tools if x.name == "get_feedback")
    assert tool.output_schema is None
    res = result["res"]
    assert [b.type for b in res.content] == ["text"]
    assert res.structured_content is None
    assert json.loads(res.content[0].text) == json.loads(server.get_feedback(sid))


def test_get_feedback_respects_total_screenshot_budget(write_md, monkeypatch):
    out = json.loads(server.open_review(write_md(HTML_DOC, name="d.html"), "図解"))
    sid = out["session_id"]
    for i in range(3):  # 同じ大きさの画像を3枚
        _region_comment(sid, comment_id=f"c{i}", screenshot=make_png(300, 200))
    one = len(storage.get_image_path(sid, "region-c0.png").read_bytes())
    monkeypatch.setattr(server, "MAX_ATTACHED_BYTES", one * 2 + 1)
    items = server.get_feedback(sid)
    fb = json.loads(items[0])
    assert [a["id"] for a in fb["attached_screenshots"]] == ["c0", "c1"]
    # 添付しなかった分もパスは渡す
    assert fb["comments"][2]["screenshot"].endswith("region-c2.png")


def test_get_feedback_survives_screenshot_vanishing(write_md, monkeypatch, tmp_path):
    """パスを決めた後に画像が消えても、その1枚が欠けるだけで失敗しない。"""
    out = json.loads(server.open_review(write_md(HTML_DOC, name="d.html"), "図解"))
    sid = out["session_id"]
    _region_comment(sid, comment_id="c1")
    monkeypatch.setattr(storage, "region_screenshot_path",
                        lambda _sid, _name: tmp_path / "消えた.png")
    fb = json.loads(server.get_feedback(sid))
    assert "attached_screenshots" not in fb
    assert fb["comments"][0]["screenshot"] == ""

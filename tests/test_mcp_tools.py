"""MCPツール関数の統合テスト。

デコレータ適用後の関数を直接呼び出して検証する
（HLE_NO_BROWSER=1、HLE_PORT=0 は conftest で設定済み）。
open_review / submit_revision はmdファイルのパスを受け取る。
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from hle_mcp import server, storage


def test_open_review_creates_session_and_server(write_md):
    md = write_md("こんにちは。")
    out = json.loads(server.open_review(md, "挨拶文", "/Users/me/prj"))
    assert out["session_id"]
    assert out["title"] == "挨拶文"
    assert out["file_path"] == md
    assert out["browser_opened"] is False  # HLE_NO_BROWSER=1
    assert out["url"].startswith("http://127.0.0.1:")

    # mdファイルの内容がそのまま登録されている
    s = storage.load_session(out["session_id"])
    assert s["revisions"][0]["base_text"] == "こんにちは。"
    assert s["workspace"] == "/Users/me/prj"

    # Webサーバーが実際に応答する
    with urllib.request.urlopen(out["url"]) as res:
        assert res.status == 200


def test_open_review_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        server.open_review(str(tmp_path / "nai.md"), "T")


def test_open_review_empty_file_raises(write_md):
    md = write_md("  \n\n")
    with pytest.raises(ValueError):
        server.open_review(md, "T")


def test_open_review_workspace_defaults_to_md_dir(tmp_path, write_md):
    md = write_md("本文。", name="docs/article.md")
    out = json.loads(server.open_review(md, "既定WS"))
    s = storage.load_session(out["session_id"])
    assert s["workspace"] == str(tmp_path / "docs")


def test_get_feedback_without_edits(write_md):
    out = json.loads(server.open_review(write_md("原文"), "T"))
    fb = json.loads(server.get_feedback(out["session_id"]))
    assert fb["original_text"] == "原文"
    assert fb["has_edits"] is False
    assert fb["comments"] == []


def test_get_feedback_missing_session_raises():
    with pytest.raises(KeyError):
        server.get_feedback("missing0")


def test_full_rally_flow(write_md):
    """open → 人間編集 → get_feedback → reply → submit_revision の複数ラリー。"""
    out = json.loads(server.open_review(write_md("初稿です。"), "ラリー", "/ws"))
    sid = out["session_id"]

    # 人間の編集とコメント（ブラウザ操作をAPIレベルで模擬）
    storage.set_document(
        sid, '<p><span class="cmt" data-cid="c1">初稿</span><del>です</del>'
             "<ins>でした</ins>。</p>")
    storage.add_comment(sid, "初稿", "もっと具体的に", comment_id="c1")
    storage.set_status(sid, "reviewed")

    fb = json.loads(server.get_feedback(sid))
    assert fb["status"] == "reviewed"
    assert fb["result_text"] == "初稿でした。"
    assert fb["comments"][0]["number"] == 1

    # AIがコメントに返信
    r = json.loads(server.reply_comment(sid, "c1", "了解しました"))
    assert r["ok"] is True
    assert r["reply"]["author"] == "ai"

    # AIが修正稿のmdファイルを提示（第2ラリー）
    rev_md = write_md("具体的な二稿です。", name="rev2.md")
    rev = json.loads(server.submit_revision(sid, rev_md, open_browser=False))
    assert rev["revision_index"] == 1
    s = storage.load_session(sid)
    assert s["status"] == "editing"
    assert s["revisions"][1]["base_text"] == "具体的な二稿です。"

    # 第2ラリーのフィードバックは新しいリビジョンを対象とする
    fb2 = json.loads(server.get_feedback(sid))
    assert fb2["revision_index"] == 1
    assert fb2["original_text"] == "具体的な二稿です。"
    # コメント履歴は引き継がれる
    assert fb2["comments"][0]["replies"][0]["author"] == "ai"


def test_submit_revision_missing_file_raises(tmp_path, write_md):
    out = json.loads(server.open_review(write_md("初稿。"), "T"))
    with pytest.raises(ValueError):
        server.submit_revision(out["session_id"], str(tmp_path / "nai.md"),
                               open_browser=False)


def test_reply_comment_missing_raises(write_md):
    out = json.loads(server.open_review(write_md("x"), "t"))
    with pytest.raises(KeyError):
        server.reply_comment(out["session_id"], "nope", "msg")


def test_list_reviews_and_get_review(write_md):
    a = json.loads(server.open_review(
        write_md("A", name="a.md"), "アルファ文書", "/Users/me/alpha"))
    json.loads(server.open_review(
        write_md("B", name="b.md"), "ベータ文書", "/Users/me/beta"))

    all_out = json.loads(server.list_reviews())
    assert all_out["count"] == 2

    q = json.loads(server.list_reviews(query="アルファ"))
    assert q["count"] == 1
    assert q["sessions"][0]["id"] == a["session_id"]

    w = json.loads(server.list_reviews(workspace="beta"))
    assert w["count"] == 1

    detail = json.loads(server.get_review(a["session_id"]))
    assert detail["title"] == "アルファ文書"
    assert detail["revisions"][0]["base_text"] == "A"
    assert detail["revisions"][0]["has_edits"] is False
    assert "edited_html" not in json.dumps(detail)


def test_tools_registered_on_mcp_server():
    """6つのツールがMCPサーバーに登録されていることを確認する。

    Playwright(sync API)がメインスレッドのasyncioループを占有していても
    動くように、別スレッドでイベントループを回す。
    """
    import asyncio
    import threading

    result: dict = {}

    def worker():
        result["tools"] = asyncio.run(server.server.list_tools())

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=30)
    names = {t.name for t in result["tools"]}
    assert names >= {
        "open_review", "get_feedback", "submit_revision",
        "reply_comment", "list_reviews", "get_review",
    }


# ---------- 画像・差分保存 ----------

def test_open_review_resolves_md_relative_image_ref(tmp_path, write_md):
    """md内の相対画像参照がmdファイルの場所を基準に自動解決される。"""
    from tests.conftest import PNG_1PX

    img = tmp_path / "docs" / "img" / "fig.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(PNG_1PX)
    md = write_md("説明文。\n![構成図](img/fig.png)", name="docs/article.md")

    out = json.loads(server.open_review(md, "相対参照"))
    assert out["images"] == ["fig.png"]
    assert out["missing_images"] == []
    s = storage.load_session(out["session_id"])
    # 参照は保存名（ベース名）に書き換えられる
    assert s["revisions"][0]["base_text"] == "説明文。\n![構成図](fig.png)"
    assert storage.list_images(out["session_id"]) == ["fig.png"]


def test_open_review_resolves_workspace_rooted_image_ref(tmp_path, write_md):
    """Zenn等の /images/... 形式はワークスペースルート基準で解決される。"""
    from tests.conftest import PNG_1PX

    img = tmp_path / "images" / "cover.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(PNG_1PX)
    md = write_md("記事本文。\n![表紙](/images/cover.png)",
                  name="articles/post.md")

    out = json.loads(server.open_review(md, "Zenn形式", str(tmp_path)))
    assert out["images"] == ["cover.png"]
    s = storage.load_session(out["session_id"])
    assert s["revisions"][0]["base_text"] == "記事本文。\n![表紙](cover.png)"


def test_open_review_reports_unresolved_image_ref(write_md):
    """解決できない画像参照は missing_images に報告され、本文は変更されない。"""
    md = write_md("本文。\n![謎](img/nai.png)\n![外部](https://x.test/a.png)")
    out = json.loads(server.open_review(md, "未解決"))
    assert out["missing_images"] == ["img/nai.png"]
    assert out["images"] == []
    s = storage.load_session(out["session_id"])
    # 未解決参照とリモートURLはそのまま残る
    assert "![謎](img/nai.png)" in s["revisions"][0]["base_text"]
    assert "![外部](https://x.test/a.png)" in s["revisions"][0]["base_text"]


def test_open_review_with_images(png_file, write_md):
    """本文で参照済みの画像は images 指定と重複しても一度だけ取り込まれる。"""
    md = write_md("説明文。\n![構成図](figure.png)")
    out = json.loads(server.open_review(md, "画像付き", "/ws",
                                        images=[str(png_file)]))
    assert out["images"] == ["figure.png"]
    s = storage.load_session(out["session_id"])
    # 本文で参照済みなので末尾追記されない
    assert s["revisions"][0]["base_text"] == "説明文。\n![構成図](figure.png)"
    assert storage.list_images(out["session_id"]) == ["figure.png"]


def test_open_review_appends_unreferenced_images(png_file, write_md):
    md = write_md("本文のみ。")
    out = json.loads(server.open_review(md, "未参照画像", "/nai/ws",
                                        images=[str(png_file)]))
    s = storage.load_session(out["session_id"])
    assert s["revisions"][0]["base_text"] == "本文のみ。\n![](figure.png)"


def test_open_review_missing_image_raises(tmp_path, write_md):
    md = write_md("x")
    with pytest.raises(ValueError):
        server.open_review(md, "t", "", images=[str(tmp_path / "no.png")])


def test_submit_revision_compacts_and_accepts_images(png_file, write_md):
    out = json.loads(server.open_review(write_md("初稿。"), "T"))
    sid = out["session_id"]
    storage.set_document(sid, "<p>初稿<ins>！</ins>。</p>")
    rev_md = write_md("第二稿。\n![追加図](figure.png)", name="rev.md")
    rev = json.loads(server.submit_revision(sid, rev_md, open_browser=False,
                                            images=[str(png_file)]))
    assert rev["images"] == ["figure.png"]
    s = storage.load_session(sid)
    # 旧リビジョンは差分要約に圧縮される
    assert s["revisions"][0]["edited_html"] is None
    assert "【追加:！】" in s["revisions"][0]["feedback_summary"]["annotated_text"]
    # 新リビジョンには画像が添付される
    assert s["revisions"][1]["base_text"].endswith("![追加図](figure.png)")
    assert storage.list_images(sid) == ["figure.png"]
    # get_review が過去の差分要約を返す
    detail = json.loads(server.get_review(sid))
    assert detail["revisions"][0]["feedback_summary"]["result_text"] == "初稿！。"


def test_submit_revision_keeps_stored_image_refs(png_file, write_md):
    """過去ラリーで添付済みの画像への参照は再解決せずそのまま使える。"""
    md = write_md("初稿。\n![図](figure.png)")
    out = json.loads(server.open_review(md, "継続参照", images=[str(png_file)]))
    sid = out["session_id"]
    # 第2稿は別ディレクトリのmdから同じ保存名を参照する
    rev_md = write_md("第二稿。\n![図](figure.png)", name="elsewhere/rev.md")
    rev = json.loads(server.submit_revision(sid, rev_md, open_browser=False))
    assert rev["missing_images"] == []
    s = storage.load_session(sid)
    assert s["revisions"][1]["base_text"] == "第二稿。\n![図](figure.png)"
    assert storage.list_images(sid) == ["figure.png"]

"""MCPツール関数の統合テスト。

デコレータ適用後の関数を直接呼び出して検証する
（HLE_NO_BROWSER=1、HLE_PORT=0 は conftest で設定済み）。
open_review / submit_revision はmdファイルのパスを受け取る。
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from hle_mcp import feedback, server, storage


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
        "reply_comment", "list_reviews", "get_review", "add_comment",
    }


def _tool_descriptions() -> dict[str, str]:
    """MCPクライアントに渡るツール説明を取り出す（別スレッドでループを回す）。"""
    import asyncio
    import threading

    result: dict = {}

    def worker():
        result["tools"] = asyncio.run(server.server.list_tools())

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=30)
    return {x.name: x.description or "" for x in result["tools"]}


def test_tool_descriptions_explain_question_tag():
    """質問タグは回答だけで本文を変えないことが、ツール説明に明示されている。"""
    d = _tool_descriptions()
    fb = d["get_feedback"]
    assert 'tag="issue"' in fb and 'tag="question"' in fb
    assert "reply_comment" in fb
    assert "本文を変えてはならない" in fb
    assert 'tag="question"' in d["submit_revision"]
    assert "本文には反映しない" in d["submit_revision"]
    assert 'tag="question"' in d["reply_comment"]
    # ツールを呼ぶ前から見える instructions にも同じ扱いを書いてある
    assert 'tag="question"' in server.server.instructions


def test_get_feedback_reports_comment_tags(write_md):
    out = json.loads(server.open_review(write_md("初稿です。"), "種類"))
    sid = out["session_id"]
    storage.add_comment(sid, "初稿", "直して", comment_id="c1")
    storage.add_comment(sid, "初稿", "なぜこの語？", comment_id="c2",
                        tag="question")
    fb = json.loads(server.get_feedback(sid))
    assert [(c["id"], c["tag"]) for c in fb["comments"]] \
        == [("c1", "issue"), ("c2", "question")]


# ---------- add_comment（AIが本文の箇所を指してコメントする） ----------

def _md_session(write_md, body: str, title: str = "AIコメント"):
    out = json.loads(server.open_review(write_md(body), title))
    return out["session_id"]


def test_add_comment_md_success_marks_unanchored(write_md):
    sid = _md_session(write_md, "これは重要な一文です。")
    out = json.loads(server.add_comment(sid, "重要", "この語の意図は？",
                                        tag="question"))
    assert out["author"] == "ai"
    assert out["anchored"] is False
    assert out["tag"] == "question"
    assert out["quote"] == "重要"
    # 前後の文脈が prefix/suffix に入る（html-review と同じ 50文字規約）
    assert out["suffix"].startswith("な一文です")
    assert out["prefix"].endswith("これは")
    # 保存層に author='ai'・anchored=False で入る
    c = storage.load_session(sid)["comments"][0]
    assert c["author"] == "ai" and c["anchored"] is False
    # 未アンカーなので get_feedback では番号無し・anchored=false・quote のみ
    fb = json.loads(server.get_feedback(sid))
    v = fb["comments"][0]
    assert v["anchored"] is False
    assert v["number"] is None
    assert v["quote"] == "重要"


def test_add_comment_md_not_found_raises(write_md):
    """本文に無い quote は not-found の ToolError（1つの誤入力を捕まえる）。"""
    sid = _md_session(write_md, "これは重要な一文です。")
    with pytest.raises(ValueError, match="見つかりません"):
        server.add_comment(sid, "存在しない語", "コメント")
    assert storage.load_session(sid)["comments"] == []


def test_add_comment_md_cross_paragraph_raises(write_md):
    """段落（改行）をまたぐ quote は明確に失敗する（この1誤入力を捕まえる）。"""
    sid = _md_session(write_md, "一段落目です。\n二段落目です。")
    with pytest.raises(ValueError, match="段落"):
        server.add_comment(sid, "一段落目です。\n二段落目です。", "コメント")
    assert storage.load_session(sid)["comments"] == []


def test_add_comment_md_multi_match_requires_prefix_suffix(write_md):
    """同じ quote が複数あって prefix/suffix 未指定なら件数入りで失敗する。"""
    sid = _md_session(write_md, "赤。\n赤。\n赤。")
    with pytest.raises(ValueError, match="3 箇所"):
        server.add_comment(sid, "赤", "コメント")
    assert storage.load_session(sid)["comments"] == []


def test_add_comment_md_prefix_suffix_disambiguates(write_md):
    """prefix/suffix を添えれば複数一致でも一意化できて成功する。"""
    sid = _md_session(write_md, "青い空。\n赤い空。\n白い空。")
    out = json.loads(server.add_comment(sid, "空", "この行の空", prefix="赤い",
                                        suffix="。"))
    assert out["anchored"] is False
    # 2行目（赤い空。）を指している
    assert out["prefix"].endswith("赤い")


def test_add_comment_missing_session_raises():
    """セッション不在は storage の KeyError がそのまま伝わる。"""
    with pytest.raises(KeyError):
        server.add_comment("missing0", "q", "t")


def test_add_comment_rejects_empty_quote_and_text(write_md):
    sid = _md_session(write_md, "本文です。")
    with pytest.raises(ValueError):
        server.add_comment(sid, "   ", "コメント")
    with pytest.raises(ValueError):
        server.add_comment(sid, "本文", "   ")
    assert storage.load_session(sid)["comments"] == []


def test_add_comment_resolves_against_edited_result_text(write_md):
    """mdモードの照合は result_text（削除除去・人間の挿入を含む）に対して行う。"""
    out = json.loads(server.open_review(write_md("原文の一文。"), "編集後"))
    sid = out["session_id"]
    # 人間が「原文」を削除し「改訂」を挿入 → result_text は「改訂の一文。」
    storage.set_document(sid, "<p><del>原文</del><ins>改訂</ins>の一文。</p>")
    assert feedback.build_feedback(storage.load_session(sid))["result_text"] \
        == "改訂の一文。"
    # 挿入語（改訂）は result_text にあるので指せる
    res = json.loads(server.add_comment(sid, "改訂", "この語に注目"))
    assert res["anchored"] is False
    # 削除語（原文）は result_text に無いので指せない
    with pytest.raises(ValueError, match="見つかりません"):
        server.add_comment(sid, "原文", "削除済みは指せない")


def test_add_comment_html_mode_success_and_not_found(write_md):
    from tests.test_html_review import HTML_DOC

    out = json.loads(server.open_review(write_md(HTML_DOC, name="d.html"), "図"))
    sid = out["session_id"]
    # タグを跨がない（空白寛容な）quote は通る
    res = json.loads(server.add_comment(sid, "重要", "ここに注目",
                                        prefix="これは", suffix="な点"))
    assert res["mode"] == "html"
    assert res["author"] == "ai"
    # prefix/suffix は渡したものを尊重する
    assert res["prefix"] == "これは" and res["suffix"] == "な点"
    with pytest.raises(ValueError, match="見つかりません"):
        server.add_comment(sid, "本文に無い語", "x")


def test_add_comment_via_in_memory_client(write_md):
    """インメモリMCPクライアント経由でも成功/失敗が反映される。"""
    from tests.test_html_review import _call_tool

    out = json.loads(server.open_review(write_md("重要な一文です。"), "Client経由"))
    sid = out["session_id"]
    ok = _call_tool("add_comment",
                    {"session_id": sid, "quote": "重要", "text": "注目"})
    assert ok.is_error is False
    body = json.loads(ok.content[0].text)
    assert body["author"] == "ai" and body["anchored"] is False
    ng = _call_tool("add_comment",
                    {"session_id": sid, "quote": "無い語", "text": "x"})
    assert ng.is_error is True
    assert "見つかりません" in ng.content[0].text


def test_add_comment_tool_description_and_instructions():
    """add_comment の用途とAI向け運用がツール説明・instructions にある。"""
    from tests.test_mcp_tools import _tool_descriptions

    d = _tool_descriptions()
    assert "add_comment" in d
    ac = d["add_comment"]
    assert "author=\"ai\"" in ac
    assert "質問" in ac and "指摘" in ac
    # get_feedback の説明にも author と add_comment の運用がある
    assert "add_comment" in d["get_feedback"]
    assert "author==\"ai\"" in d["get_feedback"]
    # instructions（ツールを呼ぶ前から見える）にも書いてある
    instr = server.server.instructions
    assert "add_comment" in instr
    assert "author" in instr
    # 既存の運用（質問は reply_comment で、傾向を skills へ）は残っている
    assert "reply_comment" in instr
    assert "skills" in instr


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
    # エディタ専用の項目（コメントの文字位置）はAIに渡さない
    assert "comment_spans" not in detail["revisions"][0]["feedback_summary"]
    assert "comment_spans" in storage.load_session(sid)["revisions"][0][
        "feedback_summary"]


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


# ---------- 動画・音声・GIF ----------
from pathlib import Path  # noqa: E402

MEDIA_DIR = Path(__file__).parent / "fixtures" / "media"


def _copy_media(dest_dir: Path, *names: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (dest_dir / n).write_bytes((MEDIA_DIR / n).read_bytes())


def test_open_review_resolves_video_audio_and_gif_refs(tmp_path, write_md):
    """![](パス) の動画・音声・GIF も画像と同じく解決・添付される。"""
    _copy_media(tmp_path / "docs" / "media", "clip.mp4", "clip.mov",
                "tone.mp3", "tone.wav", "anim.gif")
    md = write_md(
        "手順の説明。\n![操作の録画](media/clip.mp4)\n"
        "画面収録は ![](media/clip.mov) のとおり。\n"
        "![ナレーション](media/tone.mp3)\n![](media/tone.wav)\n"
        "![動き](media/anim.gif)\n![無い動画](media/none.webm)\n"
        "![PDF](media/doc.pdf)",
        name="docs/guide.md")
    (tmp_path / "docs" / "media" / "doc.pdf").write_bytes(b"%PDF")
    out = json.loads(server.open_review(md, "メディア入り"))
    assert out["images"] == ["clip.mp4", "clip.mov", "tone.mp3", "tone.wav",
                             "anim.gif"]
    # 見つからないもの・非対応の形式は missing_images で知らせる
    assert out["missing_images"] == ["media/none.webm", "media/doc.pdf"]
    s = storage.load_session(out["session_id"])
    assert s["revisions"][0]["base_text"] == (
        "手順の説明。\n![操作の録画](clip.mp4)\n"
        "画面収録は ![](clip.mov) のとおり。\n"
        "![ナレーション](tone.mp3)\n![](tone.wav)\n"
        "![動き](anim.gif)\n![無い動画](media/none.webm)\n"
        "![PDF](media/doc.pdf)")
    for n in ("clip.mp4", "tone.mp3", "anim.gif"):
        assert storage.get_image_path(out["session_id"], n).read_bytes() \
            == (MEDIA_DIR / n).read_bytes()


def test_open_review_images_param_accepts_media(tmp_path, write_md):
    _copy_media(tmp_path / "att", "clip.webm", "tone.m4a")
    md = write_md("本文のみ。")
    out = json.loads(server.open_review(
        md, "添付", images=[str(tmp_path / "att" / "clip.webm"),
                           str(tmp_path / "att" / "tone.m4a")]))
    assert out["images"] == ["clip.webm", "tone.m4a"]
    s = storage.load_session(out["session_id"])
    assert s["revisions"][0]["base_text"] == \
        "本文のみ。\n![](clip.webm)\n![](tone.m4a)"


def test_resubmitting_same_video_does_not_duplicate(tmp_path, write_md):
    """稿ごとに同じローカルの動画を参照し直しても、コピーは1つのまま。"""
    _copy_media(tmp_path / "media", "clip.mp4")
    md = write_md("初稿。\n![録画](media/clip.mp4)")
    sid = json.loads(server.open_review(md, "再提出"))["session_id"]
    rev_md = write_md("第二稿。\n![録画](media/clip.mp4)", name="rev.md")
    rev = json.loads(server.submit_revision(sid, rev_md, open_browser=False))
    assert rev["images"] == ["clip.mp4"]
    s = storage.load_session(sid)
    assert s["revisions"][1]["base_text"] == "第二稿。\n![録画](clip.mp4)"
    assert storage.list_images(sid) == ["clip.mp4"]
    # 撮り直して中身が変われば別名で添付され、本文の参照もそちらを指す
    (tmp_path / "media" / "clip.mp4").write_bytes(b"retaken")
    rev3 = write_md("第三稿。\n![録画](media/clip.mp4)", name="rev3.md")
    out3 = json.loads(server.submit_revision(sid, rev3, open_browser=False))
    assert out3["images"] == ["clip-1.mp4"]
    assert storage.load_session(sid)["revisions"][2]["base_text"] \
        == "第三稿。\n![録画](clip-1.mp4)"


def test_tool_descriptions_list_every_media_extension():
    """ツール説明の対応形式が media.MEDIA_TYPES とずれていない。"""
    doc = server.open_review.__doc__ or ""
    for ext in storage.MEDIA_TYPES:
        assert ext.lstrip(".") in doc, ext

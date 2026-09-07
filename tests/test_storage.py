"""storage モジュールのユニットテスト。"""

from __future__ import annotations

import re

import pytest

from hle_mcp import storage


def test_create_and_load_session():
    s = storage.create_session("本文です。", "タイトル", "/tmp/prj")
    loaded = storage.load_session(s["id"])
    assert loaded["id"] == s["id"]
    assert loaded["title"] == "タイトル"
    assert loaded["workspace"] == "/tmp/prj"
    assert loaded["status"] == "editing"
    assert len(loaded["revisions"]) == 1
    assert loaded["revisions"][0]["base_text"] == "本文です。"
    assert loaded["revisions"][0]["edited_html"] is None
    assert loaded["comments"] == []


def test_load_missing_session_raises():
    with pytest.raises(KeyError):
        storage.load_session("deadbeef")


def test_invalid_session_id_raises():
    with pytest.raises(KeyError):
        storage.load_session("../evil")


def test_set_document_updates_current_revision():
    s = storage.create_session("t", "d", "")
    storage.set_document(s["id"], "<p>edited</p>")
    loaded = storage.load_session(s["id"])
    assert loaded["revisions"][-1]["edited_html"] == "<p>edited</p>"


def test_set_status_valid_and_invalid():
    s = storage.create_session("t", "d", "")
    storage.set_status(s["id"], "reviewed")
    assert storage.load_session(s["id"])["status"] == "reviewed"
    with pytest.raises(ValueError):
        storage.set_status(s["id"], "bogus")


def test_add_revision_resets_status():
    s = storage.create_session("v1", "d", "")
    storage.set_status(s["id"], "reviewed")
    updated = storage.add_revision(s["id"], "v2")
    assert updated["status"] == "editing"
    assert len(updated["revisions"]) == 2
    assert updated["revisions"][1]["base_text"] == "v2"
    assert updated["revisions"][1]["index"] == 1


def test_add_comment_and_reply():
    s = storage.create_session("t", "d", "")
    c = storage.add_comment(s["id"], quote="引用", text="コメント", comment_id="c1")
    assert c["id"] == "c1"
    assert c["author"] == "human"
    assert c["revision"] == 0
    r = storage.add_reply(s["id"], "c1", "AI返信", author="ai")
    assert r["author"] == "ai"
    loaded = storage.load_session(s["id"])
    assert loaded["comments"][0]["replies"][0]["text"] == "AI返信"
    # 返信への返信（多段ラリー）はスレッドに追記されていく
    storage.add_reply(s["id"], "c1", "人間の再返信", author="human")
    loaded = storage.load_session(s["id"])
    assert [x["author"] for x in loaded["comments"][0]["replies"]] == ["ai", "human"]


def test_duplicate_comment_id_raises():
    s = storage.create_session("t", "d", "")
    storage.add_comment(s["id"], "q", "t", comment_id="dup")
    with pytest.raises(ValueError):
        storage.add_comment(s["id"], "q2", "t2", comment_id="dup")


def test_reply_to_missing_comment_raises():
    s = storage.create_session("t", "d", "")
    with pytest.raises(KeyError):
        storage.add_reply(s["id"], "nope", "x", author="ai")


def test_comment_revision_tracks_current():
    s = storage.create_session("v1", "d", "")
    storage.add_revision(s["id"], "v2")
    c = storage.add_comment(s["id"], "q", "t")
    assert c["revision"] == 1


def test_update_comment_and_reply():
    s = storage.create_session("t", "d", "")
    storage.add_comment(s["id"], "引用", "元のコメント", comment_id="c1")
    r = storage.add_reply(s["id"], "c1", "元の返信", author="ai")
    updated = storage.update_comment(s["id"], "c1", "直したコメント")
    assert updated["text"] == "直したコメント"
    assert updated["updated_at"]  # 編集済みの印が入る
    upd_reply = storage.update_reply(s["id"], "c1", r["id"], "直した返信")
    assert upd_reply["text"] == "直した返信"
    loaded = storage.load_session(s["id"])
    assert loaded["comments"][0]["text"] == "直したコメント"
    assert loaded["comments"][0]["replies"][0]["text"] == "直した返信"


def test_update_rejects_empty_text():
    """空にする意図は削除なので、黙って空カードを残さず失敗させる。"""
    s = storage.create_session("t", "d", "")
    storage.add_comment(s["id"], "q", "元の本文", comment_id="c1")
    r = storage.add_reply(s["id"], "c1", "元の返信", author="human")
    with pytest.raises(ValueError):
        storage.update_comment(s["id"], "c1", "   ")
    with pytest.raises(ValueError):
        storage.update_reply(s["id"], "c1", r["id"], "")
    loaded = storage.load_session(s["id"])
    assert loaded["comments"][0]["text"] == "元の本文"
    assert loaded["comments"][0]["replies"][0]["text"] == "元の返信"


def test_delete_comment_removes_thread():
    s = storage.create_session("t", "d", "")
    storage.add_comment(s["id"], "q", "1つ目", comment_id="c1")
    storage.add_comment(s["id"], "q", "2つ目", comment_id="c2")
    storage.add_reply(s["id"], "c1", "返信", author="ai")
    storage.delete_comment(s["id"], "c1")
    loaded = storage.load_session(s["id"])
    assert [c["id"] for c in loaded["comments"]] == ["c2"]


def test_delete_reply_keeps_comment():
    s = storage.create_session("t", "d", "")
    storage.add_comment(s["id"], "q", "t", comment_id="c1")
    r1 = storage.add_reply(s["id"], "c1", "1つ目", author="ai")
    storage.add_reply(s["id"], "c1", "2つ目", author="human")
    storage.delete_reply(s["id"], "c1", r1["id"])
    loaded = storage.load_session(s["id"])
    assert len(loaded["comments"]) == 1
    assert [r["text"] for r in loaded["comments"][0]["replies"]] == ["2つ目"]


def test_update_delete_missing_target_raises():
    s = storage.create_session("t", "d", "")
    storage.add_comment(s["id"], "q", "t", comment_id="c1")
    with pytest.raises(KeyError):
        storage.update_comment(s["id"], "nope", "x")
    with pytest.raises(KeyError):
        storage.delete_comment(s["id"], "nope")
    with pytest.raises(KeyError):
        storage.update_reply(s["id"], "c1", "nope", "x")
    with pytest.raises(KeyError):
        storage.delete_reply(s["id"], "c1", "nope")


def test_list_sessions_search_and_order():
    a = storage.create_session("x", "レポート校正", "/Users/me/prjA")
    b = storage.create_session("y", "ブログ記事", "/Users/me/prjB")
    all_sessions = storage.list_sessions()
    assert {s["id"] for s in all_sessions} == {a["id"], b["id"]}

    # タイトル部分一致
    hit = storage.list_sessions(query="ブログ")
    assert [s["id"] for s in hit] == [b["id"]]

    # ID部分一致
    hit = storage.list_sessions(query=a["id"][:6])
    assert [s["id"] for s in hit] == [a["id"]]

    # ワークスペース部分一致
    hit = storage.list_sessions(workspace="prjA")
    assert [s["id"] for s in hit] == [a["id"]]

    # 該当なし
    assert storage.list_sessions(query="存在しない") == []

    # 更新日時降順: b を更新すると先頭に来る
    storage.set_document(b["id"], "<p>x</p>")
    ordered = storage.list_sessions()
    assert ordered[0]["id"] == b["id"]


def test_list_sessions_summary_fields():
    s = storage.create_session("t", "d", "/w")
    storage.add_comment(s["id"], "q", "c")
    summary = storage.list_sessions()[0]
    assert summary["comment_count"] == 1
    assert summary["revision_count"] == 1
    assert summary["status"] == "editing"
    assert set(summary) >= {
        "id", "title", "workspace", "status", "created_at", "updated_at",
    }


def test_list_sessions_limit():
    for i in range(5):
        storage.create_session("t", f"s{i}", "")
    assert len(storage.list_sessions(limit=3)) == 3


# ---------- 画像 ----------

def test_save_image_and_get_path(png_file):
    s = storage.create_session("t", "d", "")
    name = storage.add_image_file(s["id"], str(png_file))
    assert name == "figure.png"
    path = storage.get_image_path(s["id"], name)
    assert path.read_bytes() == png_file.read_bytes()
    assert storage.list_images(s["id"]) == ["figure.png"]


def test_save_image_unique_names(png_file):
    s = storage.create_session("t", "d", "")
    n1 = storage.add_image_file(s["id"], str(png_file))
    n2 = storage.add_image_file(s["id"], str(png_file))
    assert n1 == "figure.png"
    assert n2 == "figure-1.png"


def test_image_name_sanitized():
    s = storage.create_session("t", "d", "")
    name = storage.save_image_bytes(s["id"], "../evil name!.png", b"x")
    assert name == "evil_name_.png"
    assert storage.get_image_path(s["id"], name).is_file()


def test_image_invalid_extension_rejected():
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.save_image_bytes(s["id"], "script.svg", b"<svg/>")
    with pytest.raises(ValueError):
        storage.save_image_bytes(s["id"], "noext", b"x")


def test_image_size_limit():
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.save_image_bytes(s["id"], "big.png",
                                 b"0" * (storage.MAX_IMAGE_BYTES + 1))


def test_missing_image_raises():
    s = storage.create_session("t", "d", "")
    with pytest.raises(KeyError):
        storage.get_image_path(s["id"], "nope.png")


# ---------- 差分保存（リビジョン圧縮） ----------

def test_add_revision_compacts_previous_html():
    s = storage.create_session("初稿です。", "d", "")
    storage.set_document(s["id"], "<p>初稿<del>です</del><ins>でした</ins>。</p>")
    storage.add_revision(s["id"], "第二稿。")
    loaded = storage.load_session(s["id"])
    old = loaded["revisions"][0]
    # 全文HTMLは破棄され、差分要約だけが残る
    assert old["edited_html"] is None
    fs = old["feedback_summary"]
    assert fs["annotated_text"] == "初稿【削除:です】【追加:でした】。"
    assert fs["result_text"] == "初稿でした。"
    assert [c["type"] for c in fs["changes"]] == ["delete", "insert"]


def test_add_revision_without_edits_no_summary():
    s = storage.create_session("v1", "d", "")
    storage.add_revision(s["id"], "v2")
    loaded = storage.load_session(s["id"])
    assert loaded["revisions"][0].get("feedback_summary") is None
    assert loaded["revisions"][0]["edited_html"] is None


def test_update_base_text():
    s = storage.create_session("v1", "d", "")
    storage.update_base_text(s["id"], "v1\n![](a.png)")
    assert storage.load_session(s["id"])["revisions"][0]["base_text"] \
        == "v1\n![](a.png)"
    # 編集開始後は差し替え不可
    storage.set_document(s["id"], "<p>v1</p>")
    with pytest.raises(ValueError):
        storage.update_base_text(s["id"], "x")


# ---------- コメントIDの検証（属性インジェクション防止） ----------
def test_add_comment_rejects_quote_in_id():
    """data-cid属性を抜け出すIDを拒否する。"""
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.add_comment(
            s["id"], quote="q", text="t", comment_id='x" onmouseover="alert(1)'
        )
    assert storage.load_session(s["id"])["comments"] == []


@pytest.mark.parametrize(
    "bad_id",
    ["a b", "a<b", "a'b", "c#1", "x" * 65, "../etc", "あ"],
)
def test_add_comment_rejects_invalid_ids(bad_id):
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.add_comment(s["id"], quote="q", text="t", comment_id=bad_id)


@pytest.mark.parametrize("empty", [None, ""])
def test_add_comment_generates_id_when_unspecified(empty):
    """ID未指定（None・空文字）はサーバー側で採番する。"""
    s = storage.create_session("t", "d", "")
    c = storage.add_comment(s["id"], quote="q", text="t", comment_id=empty)
    assert re.fullmatch(r"c[0-9a-f]{8}", c["id"])


def test_add_comment_accepts_browser_generated_id():
    """ブラウザが採番する形式（'c' + base36）は従来どおり受け入れる。"""
    s = storage.create_session("t", "d", "")
    c = storage.add_comment(
        s["id"], quote="q", text="t", comment_id="cmt77320wr5xa"
    )
    assert c["id"] == "cmt77320wr5xa"


# ---------- 画像へのコメント ----------
def test_add_image_comment_keeps_target_and_name():
    """画像コメントは target と対象の名前を保存する。"""
    s = storage.create_session("本文", "d", "")
    c = storage.add_comment(
        s["id"], quote="![図](a.png)", text="差し替えて",
        target="image", image="a.png",
    )
    assert c["target"] == "image"
    assert c["image"] == "a.png"
    assert "image_ref" not in c


def test_add_text_comment_omits_target_key():
    """既定（テキスト）ではキーを増やさず従来の形を保つ。"""
    s = storage.create_session("本文", "d", "")
    c = storage.add_comment(s["id"], quote="本", text="ここ")
    assert "target" not in c
    assert "image" not in c


def test_add_image_comment_normalizes_name_to_basename():
    """URLに埋め込むため、パスを含む名前はベース名へ正規化する。"""
    s = storage.create_session("本文", "d", "")
    c = storage.add_comment(
        s["id"], quote="q", text="t", target="image",
        image="../../etc/passwd.png",
    )
    assert c["image"] == "passwd.png"


def test_add_image_comment_accepts_html_anchor():
    """HTMLレビューの画像アンカー（タグ名:出現順）を受け入れる。"""
    s = storage.create_session("<p>本文</p>", "d", "", mode="html")
    c = storage.add_comment(
        s["id"], quote="図（SVG）", text="配色を直して",
        target="image", image="図（SVG）", image_ref="svg:2",
    )
    assert c["image_ref"] == "svg:2"


@pytest.mark.parametrize(
    "bad_ref",
    ["img", "img:", ":0", "img:x", "img:0 svg:1", "img[0]", "img:0;a",
     "*:0", "img:1234567"],
)
def test_add_comment_rejects_invalid_image_ref(bad_ref):
    """DOM検索に使うため、アンカーの書式を厳しく制限する。"""
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.add_comment(
            s["id"], "q", "t", target="image", image_ref=bad_ref
        )


def test_add_comment_rejects_unknown_target():
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.add_comment(s["id"], "q", "t", target="video")


def test_image_comment_requires_image_identity():
    """対象を特定できない画像コメントは受け付けない。"""
    s = storage.create_session("t", "d", "")
    with pytest.raises(ValueError):
        storage.add_comment(s["id"], "q", "t", target="image")

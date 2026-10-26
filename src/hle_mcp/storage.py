"""セッション永続化レイヤー。

レビューセッションを data/sessions/<id>.json に保存し、
ID・タイトル・ワークスペースでの検索を提供する。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import versioning
from .media import (  # noqa: F401  storage 経由でも使えるように再公開する
    MAX_MEDIA_BYTES,
    MEDIA_LABELS,
    MEDIA_TYPES,
    check_media_size,
    media_config,
    media_kind,
    media_label,
    supported_summary,
)

_LOCK = threading.RLock()

# セッションID・コメントIDに許可する文字。パス要素やHTML属性に埋め込むため
# 英数字・ハイフン・アンダースコアのみに限定する。
_ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


def data_dir() -> Path:
    """データ保存ディレクトリ（環境変数 HLE_DATA_DIR で上書き可能）。

    常駐Webサーバーのログや状態ファイルも同じ場所に置くので、
    定義は versioning.data_dir に一本化している。
    """
    return versioning.data_dir()


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    # 一覧の更新日時降順ソートを安定させるためマイクロ秒精度で記録する
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


def _new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:8]


def _session_path(session_id: str) -> Path:
    if not re.fullmatch(_ID_PATTERN, session_id):
        raise KeyError(f"不正なセッションID: {session_id!r}")
    return sessions_dir() / f"{session_id}.json"


def create_session(
    text: str,
    title: str,
    workspace: str = "",
    mode: str = "md",
    source_path: str = "",
) -> dict[str, Any]:
    """新しいレビューセッションを作成して保存する。

    mode は "md"（トラックチェンジ編集）または "html"（表示+範囲選択コメント）。
    source_path は入力元ファイルの絶対パス（AIがフィードバック時に直接読む）。
    """
    if mode not in ("md", "html"):
        raise ValueError(f"不正なモード: {mode}")
    with _LOCK:
        sid = _new_id()
        while _session_path(sid).exists():
            sid = _new_id()
        now = _now()
        session: dict[str, Any] = {
            "id": sid,
            "title": title,
            "workspace": workspace,
            "mode": mode,
            "source_path": source_path,
            "created_at": now,
            "updated_at": now,
            "status": "editing",
            "revisions": [
                {"index": 0, "base_text": text, "edited_html": None, "created_at": now}
            ],
            "comments": [],
        }
        _write(session)
        return session


def session_mode(session: dict[str, Any]) -> str:
    """セッションのモードを返す（旧データは "md" 扱い）。"""
    return session.get("mode", "md")


def load_session(session_id: str) -> dict[str, Any]:
    with _LOCK:
        path = _session_path(session_id)
        if not path.exists():
            raise KeyError(f"セッションが見つかりません: {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))


def _write(session: dict[str, Any]) -> None:
    path = _session_path(session["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def save_session(session: dict[str, Any]) -> None:
    with _LOCK:
        session["updated_at"] = _now()
        _write(session)


def list_sessions(
    query: str = "", workspace: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """セッション一覧を検索する。query はID/タイトルの部分一致、
    workspace はワークスペースパスの部分一致。"""
    with _LOCK:
        results: list[dict[str, Any]] = []
        for path in sessions_dir().glob("*.json"):
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if query and query.lower() not in (
                s.get("id", "") + " " + s.get("title", "")
            ).lower():
                continue
            if workspace and workspace.lower() not in s.get("workspace", "").lower():
                continue
            results.append(
                {
                    "id": s["id"],
                    "title": s.get("title", ""),
                    "workspace": s.get("workspace", ""),
                    "status": s.get("status", ""),
                    "revision_count": len(s.get("revisions", [])),
                    "comment_count": len(s.get("comments", [])),
                    "created_at": s.get("created_at", ""),
                    "updated_at": s.get("updated_at", ""),
                }
            )
        results.sort(key=lambda x: x["updated_at"], reverse=True)
        return results[: max(1, limit)]


def current_revision(session: dict[str, Any]) -> dict[str, Any]:
    return session["revisions"][-1]


def set_document(
    session_id: str, html: str, revision: int | None = None
) -> bool:
    """ブラウザで編集されたHTMLを保存し、それが現行リビジョンへの保存かを返す。

    revision はブラウザが編集していた稿の番号。AIが新しい稿を出した直後に
    前の稿の編集が届くと、そのまま現行リビジョンに書くと新しい稿を前の稿の
    HTMLで上書きしてしまう。そこで稿がずれていたら元の稿の late_edited_html
    に退避して捨てずに残し、False を返す（現行リビジョンには触れない）。
    revision を省略した場合は現行リビジョンへ保存する（旧クライアント互換）。
    """
    with _LOCK:
        s = load_session(session_id)
        cur = current_revision(s)
        if revision is None or revision == cur["index"]:
            cur["edited_html"] = html
            save_session(s)
            return True
        if not isinstance(revision, int) or not 0 <= revision < cur["index"]:
            raise ValueError(f"不正なリビジョン番号: {revision!r}")
        rev = s["revisions"][revision]
        rev["late_edited_html"] = html
        rev["late_edited_at"] = _now()
        save_session(s)
        return False


def set_status(session_id: str, status: str) -> dict[str, Any]:
    if status not in ("editing", "reviewed"):
        raise ValueError(f"不正なステータス: {status}")
    with _LOCK:
        s = load_session(session_id)
        s["status"] = status
        save_session(s)
        return s


def _compact_revision(session: dict[str, Any]) -> None:
    """確定した旧リビジョンの全文HTMLを破棄し、差分要約のみを保持する。

    データ量の増加を緩やかにするため、編集が終わったリビジョンは
    annotated_text / result_text / changes のフィードバック要約に置き換える
    （原文は base_text から常に復元できる）。
    """
    from . import feedback as _feedback  # storage→feedbackの一方向依存

    # 旧バージョンで作られたセッションも含め、未圧縮のリビジョンを全て圧縮する
    for rev in session["revisions"]:
        html = rev.get("edited_html")
        if not html:
            continue
        paragraphs = _feedback.parse_edited_html(html)
        # 番号付けは feedback と同じ（本文に結び付いたコメントだけ連番）
        numbers = _feedback._comment_numbers(
            session, _feedback.body_cids(paragraphs))
        rev["feedback_summary"] = {
            "annotated_text": _feedback.compose_annotated(paragraphs, numbers),
            "result_text": _feedback.compose_result(paragraphs),
            "changes": _feedback.compose_changes(paragraphs),
            # コメントを付けた場所（result_text の文字位置）。次の稿で本文が
            # 書き換わっても、差分からその場所を割り出して飛ぶのに使う
            "comment_spans": _feedback.compose_result_spans(paragraphs),
        }
        rev["edited_html"] = None


def add_revision(
    session_id: str, text: str, source_path: str | None = None
) -> dict[str, Any]:
    """AIによる修正稿を新リビジョンとして追加し、ステータスを editing に戻す。

    直前のリビジョンは差分要約に圧縮される（edited_html は破棄）。
    source_path を渡すと入力元ファイルパスを更新する。
    """
    with _LOCK:
        s = load_session(session_id)
        _compact_revision(s)
        s["revisions"].append(
            {
                "index": len(s["revisions"]),
                "base_text": text,
                "edited_html": None,
                "created_at": _now(),
            }
        )
        if source_path is not None:
            s["source_path"] = source_path
        s["status"] = "editing"
        save_session(s)
        return s


# text: 文字列への、image: 画像・図そのものへの、region: 人間がページ上を
# ドラッグで囲んだ範囲（HTMLレビューのみ）へのコメント
COMMENT_TARGETS = ("text", "image", "region")
# 画像アンカー（HTMLレビュー用）。"img:2" のようにタグ名と出現順で指す。
_IMAGE_REF_PATTERN = r"[A-Za-z][A-Za-z0-9-]{0,31}:\d{1,6}"

# 範囲コメントのスクリーンショット。AIが画像として読むので長辺と容量に
# 上限を設ける。長辺は Claude が1回に20枚を超える画像を受けると下げる
# 2000px を下回るように、容量は Bedrock の1枚 3.75MB を下回るようにする
# （大きすぎる画像はエージェントの画像読み込みを止めることもある）
REGION_SHOT_MAX_EDGE = 1600
REGION_SHOT_MAX_BYTES = 3 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 範囲内に表示されていた文字列（ソースを探す手がかり）の上限。超えた分は捨てる
REGION_TEXTS_MAX = 80
REGION_TEXTS_MAX_CHARS = 4000
_REGION_NUM_LIMIT = 1_000_000.0


def png_size(data: bytes) -> tuple[int, int]:
    """PNGの幅と高さを返す（PNGでなければ ValueError）。"""
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("スクリーンショットはPNG画像にしてください")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError("スクリーンショットの大きさが不正です")
    return width, height


def check_region_screenshot(data: bytes) -> tuple[int, int]:
    """範囲コメントのスクリーンショットを検証し、(幅, 高さ) を返す。"""
    if not data:
        raise ValueError("スクリーンショットが空です")
    if len(data) > REGION_SHOT_MAX_BYTES:
        raise ValueError(
            "スクリーンショットは"
            f"{REGION_SHOT_MAX_BYTES / (1024 * 1024):g}MB以下にしてください"
        )
    width, height = png_size(data)
    if max(width, height) > REGION_SHOT_MAX_EDGE:
        raise ValueError(
            f"スクリーンショットの長辺は{REGION_SHOT_MAX_EDGE}px以下にして"
            f"ください（{width}x{height}）"
        )
    return width, height


def _region_num(value: Any, label: str, lo: float = -_REGION_NUM_LIMIT,
                hi: float = _REGION_NUM_LIMIT) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"範囲の {label} には数値を指定してください")
    num = float(value)
    if not math.isfinite(num) or not lo <= num <= hi:
        raise ValueError(f"範囲の {label} が不正です: {value!r}")
    return round(num, 4)


def _region_box(value: Any, label: str, lo: float = -_REGION_NUM_LIMIT,
                hi: float = _REGION_NUM_LIMIT) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"範囲の {label} は4つの数値 [x, y, 幅, 高さ] にしてください")
    return [_region_num(v, label, lo, hi) for v in value]


def _check_region(region: Any) -> dict[str, Any]:
    """範囲コメントの位置を検証して、保存する形に整える。

    x / y / width / height は表示中の文書での位置（CSS px、スクロール込み）、
    frame_width / frame_height はそのときの表示領域の大きさ。ref があれば
    範囲を含む図（"svg:0" のようにタグ名と出現順）で、rel はその図の枠に
    対する割合 [x, y, 幅, 高さ]、svg_box はその SVG の座標系（viewBox）での
    範囲。ref と rel は、画面の幅が変わって図が伸び縮みしても枠を図に
    追従させるために使う。
    """
    if not isinstance(region, dict):
        raise ValueError("範囲コメントには region（位置）が必要です")
    out: dict[str, Any] = {
        "x": _region_num(region.get("x"), "x", 0),
        "y": _region_num(region.get("y"), "y", 0),
        "width": _region_num(region.get("width"), "width", 0),
        "height": _region_num(region.get("height"), "height", 0),
    }
    if out["width"] <= 0 or out["height"] <= 0:
        raise ValueError("範囲の幅と高さは正の数にしてください")
    for key in ("frame_width", "frame_height"):
        if region.get(key) is not None:
            out[key] = _region_num(region[key], key, 0)
    ref = region.get("ref")
    if ref:
        ref = str(ref)
        if not re.fullmatch(_IMAGE_REF_PATTERN, ref):
            raise ValueError(f"不正な図のアンカー: {ref!r}")
        out["ref"] = ref
        if region.get("rel") is not None:
            out["rel"] = _region_box(region["rel"], "rel", -1.0, 2.0)
        if region.get("svg_box") is not None:
            out["svg_box"] = _region_box(region["svg_box"], "svg_box")
    return out


def _check_texts(texts: Any) -> list[str]:
    """範囲内に表示されていた文字列の配列を整える（上限を超えた分は捨てる）。"""
    if texts is None:
        return []
    if not isinstance(texts, (list, tuple)):
        raise ValueError("texts は文字列の配列にしてください")
    out: list[str] = []
    total = 0
    for t in texts:
        if not isinstance(t, str):
            raise ValueError("texts は文字列の配列にしてください")
        s = t.strip()
        if not s:
            continue
        if len(out) >= REGION_TEXTS_MAX or total + len(s) > REGION_TEXTS_MAX_CHARS:
            break
        out.append(s)
        total += len(s)
    return out

# コメントの種類。issue（指摘）は本文を直してほしいもの、question（質問）は
# 答えてほしいだけのもの。AIは tag を見て、修正稿に反映するか返信だけに
# するかを分ける。既定は指摘で、tag を持たない旧データのコメントも指摘になる。
COMMENT_TAGS = ("issue", "question")
DEFAULT_COMMENT_TAG = "issue"


def _check_tag(tag: str) -> str:
    value = str(tag)
    if value not in COMMENT_TAGS:
        raise ValueError(
            f"不正なコメントタグ: {tag!r} (対応: issue=指摘, question=質問)"
        )
    return value


def _apply_tag(comment: dict[str, Any], tag: str) -> None:
    """コメントに種類を付ける。既定（指摘）はキーを持たせず従来の形を保つ。"""
    if tag == DEFAULT_COMMENT_TAG:
        comment.pop("tag", None)
    else:
        comment["tag"] = tag


def add_comment(
    session_id: str,
    quote: str,
    text: str,
    author: str = "human",
    comment_id: str | None = None,
    prefix: str = "",
    suffix: str = "",
    target: str = "text",
    image: str = "",
    image_ref: str = "",
    tag: str = DEFAULT_COMMENT_TAG,
    region: Any = None,
    texts: Any = None,
    anchored: bool = True,
) -> dict[str, Any]:
    """コメントを追加する。comment_id はブラウザ側で採番したIDを受け入れる。

    comment_id は HTML の data-cid 属性やCSSセレクタに埋め込まれるため、
    英数字・ハイフン・アンダースコアのみに制限する（属性インジェクション防止）。
    prefix / suffix は選択範囲の前後の文脈テキスト（HTMLレビューで、同じ
    quote が複数箇所に現れても位置を特定できるようにするためのもの）。

    target="image" は文字列ではなく画像そのものへのコメント。
    image は対象を表す名前（mdモードでは添付画像の保存ファイル名。URLに
    埋め込むためベース名へ正規化する）、image_ref はHTMLレビューで対象要素を
    「タグ名:出現順」で指すアンカーで、DOM検索に使うため書式を制限する。

    target="region" はHTMLレビューで人間がドラッグで囲んだ範囲へのコメント。
    image はセッションに保存済みの、その範囲のスクリーンショット（PNG）の
    ファイル名、region は位置（_check_region）、texts は範囲内に表示されて
    いた文字列。通常は add_region_comment で画像と一緒に保存する。

    tag はコメントの種類（"issue"=指摘・既定 / "question"=質問）。

    anchored は、AIが付けたテキストコメント（author="ai"・target="text"）が
    まだ本文の span.cmt に結び付いていない（未アンカー）ことを表す。既定の
    True では従来どおりキーを増やさない。author="ai" かつ target="text" で
    anchored=False のときだけ comment["anchored"]=False を書く。ブラウザの
    エディタが本文に該当箇所を見つけてラップすると、保存される edited_html
    側で人間コメントと同じ扱いになる（このキーは未アンカーの印として残り、
    feedback 側が番号付けの判定に使う）。
    """
    if target not in COMMENT_TARGETS:
        raise ValueError(
            f"不正なコメント対象: {target!r} "
            f"(対応: {', '.join(COMMENT_TARGETS)})"
        )
    tag = _check_tag(tag)
    name = Path(str(image)).name[:200]
    ref = str(image_ref)
    if ref and not re.fullmatch(_IMAGE_REF_PATTERN, ref):
        raise ValueError(f"不正な画像アンカー: {image_ref!r}")
    if target == "image" and not (name or ref):
        raise ValueError("画像コメントには image か image_ref が必要です")
    region_data: dict[str, Any] | None = None
    texts_data: list[str] = []
    if target == "region":
        region_data = _check_region(region)
        texts_data = _check_texts(texts)
        if not name:
            raise ValueError("範囲コメントにはスクリーンショット（image）が必要です")
    with _LOCK:
        s = load_session(session_id)
        shot_size: tuple[int, int] | None = None
        if region_data is not None:
            if session_mode(s) != "html":
                raise ValueError("範囲コメントはHTMLレビューでのみ使えます")
            shot_size = _stored_screenshot_size(session_id, name)
        cid = comment_id or _new_id("c")
        if not re.fullmatch(_ID_PATTERN, cid):
            raise ValueError(f"不正なコメントID: {cid!r}")
        if any(c["id"] == cid for c in s["comments"]):
            raise ValueError(f"コメントIDが重複しています: {cid}")
        comment = {
            "id": cid,
            "revision": current_revision(s)["index"],
            "quote": quote,
            "text": text,
            "author": author,
            "created_at": _now(),
            "replies": [],
        }
        if prefix or suffix:
            comment["prefix"] = str(prefix)
            comment["suffix"] = str(suffix)
        # 既定（テキストへのコメント）ではキーを増やさず、従来の形を保つ
        if target != "text":
            comment["target"] = target
        # AIが付けたテキストコメントで、まだ本文に span.cmt が無いものだけ
        # 未アンカーの印を持たせる（人間コメント・アンカー済みは従来の形）
        if author == "ai" and target == "text" and not anchored:
            comment["anchored"] = False
        if name:
            comment["image"] = name
        if ref:
            comment["image_ref"] = ref
        if region_data is not None and shot_size is not None:
            comment["region"] = region_data
            comment["texts"] = texts_data
            comment["screenshot_size"] = list(shot_size)
            if not comment["quote"]:
                comment["quote"] = region_label(texts_data)
        _apply_tag(comment, tag)
        s["comments"].append(comment)
        save_session(s)
        return comment


def region_label(texts: list[str], limit: int = 120) -> str:
    """範囲コメントの見出し（範囲内の文字列をつないだもの）を作る。"""
    label = " / ".join(texts)
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _stored_screenshot_size(session_id: str, name: str) -> tuple[int, int]:
    """保存済みのスクリーンショットを検証し、(幅, 高さ) を返す。"""
    try:
        path = get_image_path(session_id, name)
    except KeyError:
        raise ValueError(f"スクリーンショットが見つかりません: {name}") from None
    if path.stat().st_size > REGION_SHOT_MAX_BYTES:
        raise ValueError("スクリーンショットが大きすぎます")
    with open(path, "rb") as fh:
        width, height = png_size(fh.read(24))
    if max(width, height) > REGION_SHOT_MAX_EDGE:
        raise ValueError(
            f"スクリーンショットの長辺は{REGION_SHOT_MAX_EDGE}px以下にしてください"
        )
    return width, height


def add_region_comment(
    session_id: str,
    screenshot: bytes,
    text: str,
    region: Any,
    texts: Any = None,
    quote: str = "",
    author: str = "human",
    comment_id: str | None = None,
    tag: str = DEFAULT_COMMENT_TAG,
) -> dict[str, Any]:
    """範囲コメントを、その範囲のスクリーンショット（PNG）と一緒に保存する。

    画像をセッションの添付として保存してからコメントを追加する。
    コメントの検証で失敗したら保存した画像を消し、片方だけが残らないようにする。
    """
    check_region_screenshot(screenshot)
    _check_region(region)  # 画像を書く前に検証する（add_comment でも検証する）
    _check_texts(texts)
    cid = comment_id or _new_id("c")
    if not re.fullmatch(_ID_PATTERN, str(cid)):
        raise ValueError(f"不正なコメントID: {cid!r}")
    with _LOCK:
        if session_mode(load_session(session_id)) != "html":
            raise ValueError("範囲コメントはHTMLレビューでのみ使えます")
        name = save_image_bytes(session_id, f"region-{cid}.png", screenshot)
        try:
            return add_comment(
                session_id, quote=quote, text=text, author=author,
                comment_id=cid, target="region", image=name, tag=tag,
                region=region, texts=texts,
            )
        except Exception:
            (files_dir(session_id) / name).unlink(missing_ok=True)
            raise


def region_screenshot_path(session_id: str, name: str) -> Path | None:
    """範囲コメントのスクリーンショットの絶対パス（無ければ None）。"""
    if not name:
        return None
    try:
        return get_image_path(session_id, name).resolve()
    except KeyError:
        return None


def find_comment(session: dict[str, Any], comment_id: str) -> dict[str, Any]:
    for c in session["comments"]:
        if c["id"] == comment_id:
            return c
    raise KeyError(f"コメントが見つかりません: {comment_id}")


def add_reply(
    session_id: str, comment_id: str, text: str, author: str
) -> dict[str, Any]:
    """コメントスレッドに返信を追加する（人間・AI双方から利用）。"""
    with _LOCK:
        s = load_session(session_id)
        comment = find_comment(s, comment_id)
        reply = {
            "id": _new_id("r"),
            "text": text,
            "author": author,
            "created_at": _now(),
        }
        comment["replies"].append(reply)
        save_session(s)
        return reply


def _require_text(text: str, label: str) -> str:
    """編集後の本文が空になるのを拒否する。

    空にする意図は「削除」なので、黙って空カードを残さず明示的に失敗させる。
    """
    value = str(text)
    if not value.strip():
        raise ValueError(f"{label}を空にはできません")
    return value


def update_comment(
    session_id: str, comment_id: str, text: str, tag: str | None = None
) -> dict[str, Any]:
    """コメント本文を書き換える。tag を渡すとコメントの種類も変える。"""
    with _LOCK:
        s = load_session(session_id)
        comment = find_comment(s, comment_id)
        new_text = _require_text(text, "コメント本文")
        # 不正な tag で本文だけ書き換わらないよう、変更の前に両方を検証する
        new_tag = None if tag is None else _check_tag(tag)
        comment["text"] = new_text
        if new_tag is not None:
            _apply_tag(comment, new_tag)
        comment["updated_at"] = _now()
        save_session(s)
        return comment


def delete_comment(session_id: str, comment_id: str) -> dict[str, Any]:
    """コメントを返信ごと削除する。

    本文中の <span class="cmt" data-cid> はブラウザ側で外す。残っていても
    annotated_text には現れない（番号が引けないため）が、ハイライトだけが
    残った孤児spanになるので、エディタは削除と同時に編集HTMLを保存し直す。

    範囲コメントのスクリーンショットはそのコメントのためだけに撮ったもの
    なので、ほかのコメントが参照していなければ一緒に消す。
    """
    with _LOCK:
        s = load_session(session_id)
        comment = find_comment(s, comment_id)  # 存在しなければ KeyError
        s["comments"] = [c for c in s["comments"] if c["id"] != comment_id]
        save_session(s)
        shot = comment.get("image", "") if comment.get("target") == "region" else ""
        if shot and not any(c.get("image") == shot for c in s["comments"]):
            path = region_screenshot_path(session_id, shot)
            if path is not None:
                path.unlink(missing_ok=True)
        return s


def find_reply(comment: dict[str, Any], reply_id: str) -> dict[str, Any]:
    for r in comment.get("replies", []):
        if r["id"] == reply_id:
            return r
    raise KeyError(f"返信が見つかりません: {reply_id}")


def update_reply(
    session_id: str, comment_id: str, reply_id: str, text: str
) -> dict[str, Any]:
    """返信本文を書き換える。"""
    with _LOCK:
        s = load_session(session_id)
        reply = find_reply(find_comment(s, comment_id), reply_id)
        reply["text"] = _require_text(text, "返信本文")
        reply["updated_at"] = _now()
        save_session(s)
        return reply


def delete_reply(
    session_id: str, comment_id: str, reply_id: str
) -> dict[str, Any]:
    """返信をスレッドから削除する（コメント本体は残す）。"""
    with _LOCK:
        s = load_session(session_id)
        comment = find_comment(s, comment_id)
        find_reply(comment, reply_id)  # 存在しなければ KeyError
        comment["replies"] = [
            r for r in comment["replies"] if r["id"] != reply_id
        ]
        save_session(s)
        return comment


# ---------- 添付メディア（画像・動画・音声） ----------
# 形式と上限の定義は media.py。ここではファイルの保存・一覧・取り出しを扱う。
# 上限は保存のたびに media.MAX_MEDIA_BYTES を引く（テストで差し替えられる）
# 旧名（画像だけを扱っていた頃の定数）。中身はメディア全体に広げてある
IMAGE_EXTS = set(MEDIA_TYPES)
MAX_IMAGE_BYTES = MAX_MEDIA_BYTES["image"]
_COPY_CHUNK = 1024 * 1024
# 書き込み途中の一時ファイル。保存名は sanitize で "." 始まりにならないので
# 一覧と配信から確実に外せる
_PART_PREFIX = ".upload-"


def files_dir(session_id: str) -> Path:
    """セッションに紐づく添付ファイル（画像・動画・音声）の保存ディレクトリ。"""
    if not re.fullmatch(_ID_PATTERN, session_id):
        raise KeyError(f"不正なセッションID: {session_id!r}")
    d = data_dir() / "files" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_image_name(name: str) -> str:
    """ファイル名を安全な形に正規化する（拡張子は MEDIA_TYPES の許可制）。"""
    base = Path(str(name)).name
    ext = Path(base).suffix.lower()
    if ext not in MEDIA_TYPES:
        raise ValueError(
            f"対応していない形式です: {ext or '(拡張子なし)'} "
            f"(対応: {supported_summary()})"
        )
    stem = re.sub(r"[^\w\-]", "_", Path(base).stem, flags=re.UNICODE)[:60]
    return (stem or (media_kind(base) or "image")) + ext


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_COPY_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _fill_from(read, name: str, length: int | None):
    """read(n) の中身を一時ファイルへ書き写す関数を作る。

    length を渡したらちょうどその長さを読む（HTTP のボディ）。渡さなければ
    read が空を返すまで読む（ローカルファイル）。上限を超えたら ValueError。
    """
    def fill(dest: Path) -> tuple[int, str]:
        h = hashlib.sha256()
        size = 0
        with dest.open("wb") as out:
            while True:
                want = _COPY_CHUNK if length is None else min(_COPY_CHUNK, length - size)
                if want <= 0:
                    break
                chunk = read(want)
                if not chunk:
                    if length is not None:
                        raise ValueError("データが途中で途切れました")
                    break
                size += len(chunk)
                check_media_size(name, size)
                h.update(chunk)
                out.write(chunk)
        return size, h.hexdigest()

    return fill


def _store_media(session_id: str, name: str, fill, reuse_same: bool) -> str:
    """一時ファイルに書いてから保存名を決めて置く。実際の保存名を返す。

    同名があれば stem-1.ext, stem-2.ext と連番を振る。reuse_same のときは
    中身が同じ既存ファイルがあればそれを使い、複製を作らない（同じ動画を
    稿ごとに渡し直しても、そのたびにコピーが増えないようにする）。
    """
    safe = sanitize_image_name(name)
    d = files_dir(session_id)
    tmp = d / f"{_PART_PREFIX}{uuid.uuid4().hex}.part"
    try:
        size, digest = fill(tmp)
        if size == 0:
            raise ValueError("ファイルが空です")
        with _LOCK:
            stem, ext = Path(safe).stem, Path(safe).suffix
            candidate, counter = safe, 1
            while (d / candidate).exists():
                if (reuse_same and (d / candidate).stat().st_size == size
                        and _sha256_of(d / candidate) == digest):
                    return candidate
                candidate = f"{stem}-{counter}{ext}"
                counter += 1
            os.replace(tmp, d / candidate)
            return candidate
    finally:
        tmp.unlink(missing_ok=True)


def save_image_bytes(session_id: str, name: str, data: bytes) -> str:
    """メディアのバイト列を保存し、実際に保存されたファイル名を返す。

    同名があれば中身に関わらず連番の別名で保存する（範囲コメントの
    スクリーンショットはコメントと1対1で、消すときに他を巻き込まないように）。
    """
    if not data:
        raise ValueError("ファイルが空です")
    check_media_size(sanitize_image_name(name), len(data))
    view = memoryview(data)
    pos = 0

    def read(n: int) -> bytes:
        nonlocal pos
        chunk = view[pos:pos + n].tobytes()
        pos += len(chunk)
        return chunk

    return _store_media(
        session_id, name, _fill_from(read, name, len(data)), reuse_same=False
    )


def save_media_stream(session_id: str, name: str, stream, length: int) -> str:
    """HTTP のボディなど、長さの分かっているストリームを分割して保存する。"""
    if length <= 0:
        raise ValueError("ファイルが空です")
    check_media_size(sanitize_image_name(name), length)
    return _store_media(
        session_id, name, _fill_from(stream.read, name, length), reuse_same=False
    )


def add_image_file(session_id: str, source_path: str) -> str:
    """ローカルのメディアファイルをセッションにコピーして保存名を返す。

    中身が同じファイルを保存済みなら、その名前を返してコピーしない。
    """
    p = Path(source_path).expanduser()
    if not p.is_file():
        raise ValueError(f"ファイルが見つかりません: {source_path}")
    check_media_size(sanitize_image_name(p.name), p.stat().st_size)
    with p.open("rb") as f:
        return _store_media(
            session_id, p.name, _fill_from(f.read, p.name, None), reuse_same=True
        )


def get_image_path(session_id: str, name: str) -> Path:
    """保存済みファイルのパスを返す（存在しなければ KeyError）。"""
    safe = Path(str(name)).name
    path = files_dir(session_id) / safe
    if safe.startswith(".") or not path.is_file():
        raise KeyError(f"ファイルが見つかりません: {name}")
    return path


def list_images(session_id: str) -> list[str]:
    """保存済みの添付ファイル名（書き込み途中の一時ファイルは除く）。"""
    return sorted(
        p.name for p in files_dir(session_id).iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def update_base_text(session_id: str, text: str) -> dict[str, Any]:
    """現行リビジョンの base_text を差し替える（編集開始前のみ）。

    open_review / submit_revision で画像参照行を追記するために使う。
    """
    with _LOCK:
        s = load_session(session_id)
        rev = current_revision(s)
        if rev.get("edited_html"):
            raise ValueError("編集開始後の base_text は変更できません")
        rev["base_text"] = text
        save_session(s)
        return s


# ---------- 画面の設定（配色など） ----------
# 全画面に共通の設定。data/settings.json に保存し、ブラウザではなくサーバーが
# 持つ（ポートが変わるとブラウザの localStorage は別のオリジンになって消える。
# サーバーが <html data-theme> に書いて返せば、読み込み時にちらつかない）
THEMES = ("light", "dark")
DEFAULT_SETTINGS: dict[str, Any] = {"theme": "light"}


def settings_path() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "settings.json"


def load_settings() -> dict[str, Any]:
    """保存済みの設定を返す。無い・壊れている・知らない値の項目は既定値にする。

    画面を配信するたびに読むので、ファイルが壊れていても例外にしない。
    """
    settings = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(raw, dict) and raw.get("theme") in THEMES:
        settings["theme"] = raw["theme"]
    return settings


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    """設定の一部を書き換えて保存し、書き換え後の設定全体を返す。"""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("変更する設定を指定してください（例: {\"theme\": \"dark\"}）")
    unknown = sorted(set(changes) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"不明な設定です: {', '.join(unknown)}")
    if "theme" in changes and changes["theme"] not in THEMES:
        raise ValueError(
            f"配色は {' / '.join(THEMES)} のどれかを指定してください: "
            f"{changes['theme']!r}"
        )
    with _LOCK:
        settings = load_settings()
        settings.update(changes)
        path = settings_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return settings

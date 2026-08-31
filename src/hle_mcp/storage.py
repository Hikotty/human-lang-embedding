"""セッション永続化レイヤー。

レビューセッションを data/sessions/<id>.json に保存し、
ID・タイトル・ワークスペースでの検索を提供する。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# セッションID・コメントIDに許可する文字。パス要素やHTML属性に埋め込むため
# 英数字・ハイフン・アンダースコアのみに限定する。
_ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


def data_dir() -> Path:
    """データ保存ディレクトリ（環境変数 HLE_DATA_DIR で上書き可能）。"""
    root = Path(os.environ.get("HLE_DATA_DIR", _PROJECT_ROOT / "data"))
    return root


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


def create_session(text: str, title: str, workspace: str = "") -> dict[str, Any]:
    """新しいレビューセッションを作成して保存する。"""
    with _LOCK:
        sid = _new_id()
        while _session_path(sid).exists():
            sid = _new_id()
        now = _now()
        session: dict[str, Any] = {
            "id": sid,
            "title": title,
            "workspace": workspace,
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


def set_document(session_id: str, html: str) -> dict[str, Any]:
    """ブラウザで編集されたHTMLを現行リビジョンに保存する。"""
    with _LOCK:
        s = load_session(session_id)
        current_revision(s)["edited_html"] = html
        save_session(s)
        return s


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

    numbers = {c["id"]: i for i, c in enumerate(session["comments"], 1)}
    # 旧バージョンで作られたセッションも含め、未圧縮のリビジョンを全て圧縮する
    for rev in session["revisions"]:
        html = rev.get("edited_html")
        if not html:
            continue
        paragraphs = _feedback.parse_edited_html(html)
        rev["feedback_summary"] = {
            "annotated_text": _feedback.compose_annotated(paragraphs, numbers),
            "result_text": _feedback.compose_result(paragraphs),
            "changes": _feedback.compose_changes(paragraphs),
        }
        rev["edited_html"] = None


def add_revision(session_id: str, text: str) -> dict[str, Any]:
    """AIによる修正稿を新リビジョンとして追加し、ステータスを editing に戻す。

    直前のリビジョンは差分要約に圧縮される（edited_html は破棄）。
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
        s["status"] = "editing"
        save_session(s)
        return s


def add_comment(
    session_id: str,
    quote: str,
    text: str,
    author: str = "human",
    comment_id: str | None = None,
) -> dict[str, Any]:
    """コメントを追加する。comment_id はブラウザ側で採番したIDを受け入れる。

    comment_id は HTML の data-cid 属性やCSSセレクタに埋め込まれるため、
    英数字・ハイフン・アンダースコアのみに制限する（属性インジェクション防止）。
    """
    with _LOCK:
        s = load_session(session_id)
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
        s["comments"].append(comment)
        save_session(s)
        return comment


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


def update_comment(session_id: str, comment_id: str, text: str) -> dict[str, Any]:
    """コメント本文を書き換える。"""
    with _LOCK:
        s = load_session(session_id)
        comment = find_comment(s, comment_id)
        comment["text"] = _require_text(text, "コメント本文")
        comment["updated_at"] = _now()
        save_session(s)
        return comment


def delete_comment(session_id: str, comment_id: str) -> dict[str, Any]:
    """コメントを返信ごと削除する。

    本文中の <span class="cmt" data-cid> はブラウザ側で外す。残っていても
    annotated_text には現れない（番号が引けないため）が、ハイライトだけが
    残った孤児spanになるので、エディタは削除と同時に編集HTMLを保存し直す。
    """
    with _LOCK:
        s = load_session(session_id)
        find_comment(s, comment_id)  # 存在しなければ KeyError
        s["comments"] = [c for c in s["comments"] if c["id"] != comment_id]
        save_session(s)
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


# ---------- 画像ファイル ----------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def files_dir(session_id: str) -> Path:
    """セッションに紐づく画像ファイルの保存ディレクトリ。"""
    if not re.fullmatch(_ID_PATTERN, session_id):
        raise KeyError(f"不正なセッションID: {session_id!r}")
    d = data_dir() / "files" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_image_name(name: str) -> str:
    """ファイル名を安全な形に正規化する（拡張子は許可制）。"""
    base = Path(str(name)).name
    ext = Path(base).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ValueError(
            f"対応していない画像形式です: {ext or '(拡張子なし)'} "
            f"(対応: {', '.join(sorted(IMAGE_EXTS))})"
        )
    stem = re.sub(r"[^\w\-]", "_", Path(base).stem, flags=re.UNICODE)[:60]
    return (stem or "image") + ext


def save_image_bytes(session_id: str, name: str, data: bytes) -> str:
    """画像バイト列を保存し、実際に保存されたファイル名を返す。"""
    if not data:
        raise ValueError("画像データが空です")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("画像サイズは10MB以下にしてください")
    safe = sanitize_image_name(name)
    with _LOCK:
        d = files_dir(session_id)
        candidate = safe
        stem, ext = Path(safe).stem, Path(safe).suffix
        counter = 1
        while (d / candidate).exists():
            candidate = f"{stem}-{counter}{ext}"
            counter += 1
        (d / candidate).write_bytes(data)
        return candidate


def add_image_file(session_id: str, source_path: str) -> str:
    """ローカルの画像ファイルをセッションにコピーして保存名を返す。"""
    p = Path(source_path).expanduser()
    if not p.is_file():
        raise ValueError(f"画像ファイルが見つかりません: {source_path}")
    return save_image_bytes(session_id, p.name, p.read_bytes())


def get_image_path(session_id: str, name: str) -> Path:
    """保存済み画像のパスを返す（存在しなければ KeyError）。"""
    safe = Path(str(name)).name
    path = files_dir(session_id) / safe
    if not path.is_file():
        raise KeyError(f"画像が見つかりません: {name}")
    return path


def list_images(session_id: str) -> list[str]:
    return sorted(
        p.name for p in files_dir(session_id).iterdir() if p.is_file()
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

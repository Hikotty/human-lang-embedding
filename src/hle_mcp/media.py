"""添付できるメディア（画像・動画・音声）の形式と上限。

拡張子 → (種類, 配信する Content-Type) の表がここだけにあり、MCP の参照解決・
ブラウザからの追加・配信・注釈の種類名・エディタの描き分けがこれに従う。
I/O を持たないので storage と feedback の両方から使う（feedback は storage に
依存しない）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# SVG は画像でもスクリプトを持てるので、従来どおり受け付けない
MEDIA_TYPES: dict[str, tuple[str, str]] = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".avif": ("image", "image/avif"),
    ".bmp": ("image", "image/bmp"),
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"),
    ".ogv": ("video", "video/ogg"),
    ".mp3": ("audio", "audio/mpeg"),
    ".m4a": ("audio", "audio/mp4"),
    ".aac": ("audio", "audio/aac"),
    ".wav": ("audio", "audio/wav"),
    ".ogg": ("audio", "audio/ogg"),
    ".oga": ("audio", "audio/ogg"),
    ".opus": ("audio", "audio/ogg"),
    ".flac": ("audio", "audio/flac"),
}
MEDIA_LABELS = {"image": "画像", "video": "動画", "audio": "音声"}
# 1ファイルの上限。動画・音声は画面収録や録音をそのまま貼れる大きさにする。
# 保存も配信も分割して読み書きするので、大きなファイルでもメモリには載せない
MAX_MEDIA_BYTES = {
    "image": 50 * 1024 * 1024,
    "video": 1024 * 1024 * 1024,
    "audio": 200 * 1024 * 1024,
}


def media_kind(name: str) -> str | None:
    """ファイル名の拡張子からメディアの種類（image / video / audio）を返す。"""
    entry = MEDIA_TYPES.get(Path(str(name)).suffix.lower())
    return entry[0] if entry else None


def media_label(name: str) -> str:
    """注釈などに出す種類の名前。分からないもの（旧データ）は画像として扱う。"""
    return MEDIA_LABELS[media_kind(name) or "image"]


def media_config() -> dict[str, Any]:
    """ブラウザに渡す受け付け形式と上限（エディタの選択欄と事前の確認に使う）。"""
    return {
        "types": {
            ext: {"kind": kind, "mime": mime}
            for ext, (kind, mime) in MEDIA_TYPES.items()
        },
        "max_bytes": dict(MAX_MEDIA_BYTES),
        "labels": dict(MEDIA_LABELS),
    }


def supported_summary() -> str:
    """受け付ける拡張子を種類ごとにまとめた説明（エラーメッセージ用）。"""
    groups: dict[str, list[str]] = {}
    for ext, (kind, _) in MEDIA_TYPES.items():
        groups.setdefault(kind, []).append(ext)
    return " / ".join(
        f"{MEDIA_LABELS[k]}: {' '.join(v)}" for k, v in groups.items()
    )


def size_text(n: int) -> str:
    if n >= 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024 * 1024):.3g}GB"
    return f"{n / (1024 * 1024):.3g}MB"


def check_media_size(name: str, size: int) -> None:
    """種類ごとの上限を超えていれば ValueError（受け取る前の確認にも使う）。"""
    kind = media_kind(name) or "image"
    limit = MAX_MEDIA_BYTES[kind]
    if size > limit:
        raise ValueError(
            f"{MEDIA_LABELS[kind]}は{size_text(limit)}以下にしてください"
            f"（{size_text(size)}）"
        )

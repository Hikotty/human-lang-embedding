"""テスト共通フィクスチャ。"""

from __future__ import annotations

import pytest

from hle_mcp import webserver


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """各テストを独立したデータディレクトリで実行し、ブラウザ起動を抑止する。"""
    monkeypatch.setenv("HLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HLE_NO_BROWSER", "1")
    monkeypatch.setenv("HLE_PORT", "0")
    yield
    webserver.stop_server()


@pytest.fixture()
def web_port():
    """エフェメラルポートでWebサーバーを起動してポート番号を返す。"""
    port = webserver.start_server(0)
    yield port
    webserver.stop_server()


# 1x1透過PNG（テスト用の最小画像）
import base64  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def make_png(width: int, height: int, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """指定した大きさの単色PNGを作る（範囲スクリーンショットの検証用）。"""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height))
            + chunk(b"IEND", b""))


@pytest.fixture()
def png_file(tmp_path):
    """テスト用のPNGファイルを作成してパスを返す。"""
    p = tmp_path / "figure.png"
    p.write_bytes(PNG_1PX)
    return p


@pytest.fixture()
def write_md(tmp_path):
    """本文をmdファイルに書き出してパス（文字列）を返すファクトリ。"""

    def _write(text: str, name: str = "doc.md") -> str:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    return _write

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


@pytest.fixture()
def png_file(tmp_path):
    """テスト用のPNGファイルを作成してパスを返す。"""
    p = tmp_path / "figure.png"
    p.write_bytes(PNG_1PX)
    return p

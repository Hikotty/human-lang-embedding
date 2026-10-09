"""画面の設定（配色）の保存・API・画面への書き込みのテスト。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from hle_mcp import storage, webserver


def _request(port, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})}
        if method == "POST" else (headers or {}))
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------- 保存 ----------

def test_default_is_light_without_file():
    assert not storage.settings_path().exists()
    assert storage.load_settings() == {"theme": "light"}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_save_and_load_theme(theme):
    assert storage.save_settings({"theme": theme}) == {"theme": theme}
    assert storage.load_settings() == {"theme": theme}
    on_disk = json.loads(storage.settings_path().read_text(encoding="utf-8"))
    assert on_disk == {"theme": theme}
    # 一時ファイルを残さない（書き換えは置き換えで行う）
    assert not storage.settings_path().with_suffix(".tmp").exists()


@pytest.mark.parametrize("changes", [
    {"theme": "blue"}, {"theme": "system"}, {"theme": ""}, {"theme": None},
    {"theme": 1},
    {"font": "large"}, {}, "dark", None,
])
def test_invalid_settings_rejected(changes):
    storage.save_settings({"theme": "dark"})
    with pytest.raises(ValueError):
        storage.save_settings(changes)
    assert storage.load_settings() == {"theme": "dark"}  # 変わらない


@pytest.mark.parametrize("content", [
    "{壊れた", "[]", '"dark"', '{"theme": "neon"}', '{"theme": 3}', "",
    # 「OSに合わせる」があった頃の値。今はライトとして扱う
    '{"theme": "system"}',
])
def test_broken_settings_file_falls_back_to_default(content):
    """画面を配信するたびに読むので、壊れたファイルでも例外にしない。"""
    storage.settings_path().write_text(content, encoding="utf-8")
    assert storage.load_settings() == {"theme": "light"}
    # 壊れていても、保存すれば正しい内容で書き直せる
    assert storage.save_settings({"theme": "dark"}) == {"theme": "dark"}


# ---------- 画面への書き込み ----------

def test_with_theme_writes_attribute_once():
    page = b'<!doctype html>\n<html lang="ja">\n<body><p>&lt;html&gt;</p></body></html>'
    assert webserver.with_theme(page, "dark") == (
        b'<!doctype html>\n<html data-theme="dark" lang="ja">\n'
        b'<body><p>&lt;html&gt;</p></body></html>')
    # 知らない値は既定（ライト）にする。値は属性に直接入るので素通ししない
    assert b'data-theme="light"' in webserver.with_theme(page, '"><script>')


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_page_is_served_with_saved_theme(web_port, theme):
    storage.save_settings({"theme": theme})
    md = storage.create_session("本文", "md", "/tmp/x")
    html = storage.create_session("<html><body>x</body></html>", "html", "/tmp/x",
                                  mode="html", source_path="/tmp/x/a.html")
    for path in ("/", "/admin", f"/session/{md['id']}", f"/session/{html['id']}"):
        status, body = _request(web_port, path)
        assert status == 200
        assert f'<html data-theme="{theme}" lang="ja">'.encode() in body, path
        assert b'<script src="/static/theme.js"></script>' in body, path


def test_reviewed_html_document_is_not_themed(web_port):
    """iframe に入れるレビュー対象のHTMLは、作者が書いたまま返す。"""
    storage.save_settings({"theme": "dark"})
    src = "<html><body><p>対象</p></body></html>"
    s = storage.create_session(src, "html", "/tmp/x", mode="html",
                               source_path="/tmp/x/a.html")
    status, body = _request(web_port, f"/api/sessions/{s['id']}/source")
    assert status == 200 and body.decode() == src


# ---------- API ----------

def test_settings_api_round_trip(web_port):
    status, body = _request(web_port, "/api/settings")
    assert status == 200 and json.loads(body) == {"theme": "light"}
    status, body = _request(web_port, "/api/settings", "POST", {"theme": "dark"},
                            {"Origin": f"http://127.0.0.1:{web_port}"})
    assert status == 200 and json.loads(body) == {"theme": "dark"}
    assert storage.load_settings() == {"theme": "dark"}
    status, body = _request(web_port, "/api/settings")
    assert json.loads(body) == {"theme": "dark"}


def test_settings_api_rejects_invalid_value(web_port):
    status, body = _request(web_port, "/api/settings", "POST", {"theme": "neon"})
    assert status == 400 and "配色" in json.loads(body)["error"]
    status, _ = _request(web_port, "/api/settings", "POST", {"size": "L"})
    assert status == 400
    assert storage.load_settings() == {"theme": "light"}


def test_settings_api_is_guarded(web_port):
    """書き込みは他の POST と同じく Origin と Content-Type で守られる。"""
    status, _ = _request(web_port, "/api/settings", "POST", {"theme": "dark"},
                         {"Origin": "https://evil.example"})
    assert status == 403
    status, _ = _request(web_port, "/api/settings", "POST", {"theme": "dark"},
                         {"Content-Type": "text/plain"})
    assert status == 403
    status, _ = _request(web_port, "/api/settings", headers={"Host": "evil.example"})
    assert status == 403
    assert storage.load_settings() == {"theme": "light"}

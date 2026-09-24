"""常駐スーパーバイザー（daemon.py）の結合テスト。

実プロセスを起動し、コードの更新・壊れたコード・ワーカーの異常終了・
スーパーバイザー自身の更新の各場面で、保存リクエストを連続で送り続けても
失敗が出ないこと、最後に送った内容が保存されていることを確かめる。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from hle_mcp import storage, webserver

from .daemon_util import Daemon, Hammer, free_port


@pytest.fixture()
def daemon(tmp_path):
    d = Daemon(tmp_path / "daemon", storage.data_dir())
    d.start()
    yield d
    d.stop()


def _saved_html(sid: str) -> str | None:
    return storage.load_session(sid)["revisions"][0]["edited_html"]


def test_starts_supervised_with_current_code(daemon):
    h = daemon.health()
    assert h["supervised"] is True
    assert h["version"] == h["disk_version"]
    assert h["update_error"] == ""
    st = daemon.status()
    assert st["supervisor_pid"] == h["supervisor_pid"] == daemon.proc.pid
    assert st["worker_pid"] == h["pid"]


def test_code_update_rolls_over_without_failed_requests(daemon):
    sid = storage.create_session("本文", "入れ替え", "")["id"]
    before = daemon.health()
    with Hammer(daemon.port, sid) as hammer:
        daemon.wait_for(lambda h: h and hammer.ok > 20, 10, "保存の開始")
        daemon.touch_code("feedback.py", "rollover")
        after = daemon.wait_for(
            lambda h: h and h["version"] != before["version"], 30, "新しい版への入れ替え")
        # 入れ替え後もしばらく送り続ける
        daemon.wait_for(lambda h: hammer.ok > 50, 10, "入れ替え後の保存")
    assert hammer.fails == []
    assert _saved_html(sid) == hammer.last_html
    assert after["version"] == after["disk_version"]
    assert after["pid"] != before["pid"]                    # ワーカーは入れ替わった
    assert after["supervisor_pid"] == before["supervisor_pid"]  # 待ち受けは同じ
    assert "を終了させます" in daemon.log_text()


def test_broken_code_keeps_previous_worker(daemon):
    """読み込めないコードに更新されても、前の版のまま配信を続ける。"""
    sid = storage.create_session("本文", "構文エラー", "")["id"]
    before = daemon.health()
    with Hammer(daemon.port, sid) as hammer:
        daemon.edit("feedback.py", lambda s: s + "\ndef broken(:\n")
        broken = daemon.wait_for(lambda h: h and h["update_error"], 30, "更新失敗の記録")
        assert broken["version"] == before["version"]
        assert broken["pid"] == before["pid"]
        assert "SyntaxError" in broken["update_error"]
        assert "feedback.py" in broken["update_error"]
        # 直せば自動で新しい版になる
        daemon.edit("feedback.py", lambda s: s.replace("\ndef broken(:\n", "\n"))
        fixed = daemon.wait_for(
            lambda h: h and h["version"] == h["disk_version"] and not h["update_error"],
            30, "修正後の入れ替え")
    assert fixed["version"] != before["version"]
    assert hammer.fails == []
    assert _saved_html(sid) == hammer.last_html


def test_code_failing_self_test_is_not_rolled_in(daemon):
    """読み込めても画面が壊れるコードは自己テストで弾き、配信に出さない。"""
    before = daemon.health()
    daemon.edit("webserver.py", lambda s: s.replace(
        'self._send_file(_STATIC_DIR / "admin.html")',
        'raise RuntimeError("管理画面を壊した")', 1))
    h = daemon.wait_for(lambda h: h and h["update_error"], 30, "自己テスト失敗の記録")
    assert h["pid"] == before["pid"] and h["version"] == before["version"]
    # どの画面が壊れたか分かる
    assert "/admin が 500 を返しました" in h["update_error"]
    assert "管理画面を壊した" in h["update_error"]
    # 稼働中の（前の版の）ワーカーは管理画面を返せる
    with webserver._LOCAL.open(f"http://127.0.0.1:{daemon.port}/admin", timeout=5) as res:
        assert res.status == 200


def test_crashed_worker_is_respawned(daemon):
    before = daemon.health()
    os.kill(before["pid"], signal.SIGKILL)
    after = daemon.wait_for(lambda h: h and h["pid"] != before["pid"], 20, "ワーカーの再起動")
    assert after["supervisor_pid"] == before["supervisor_pid"]
    assert after["version"] == before["version"]


def test_supervisor_update_reexecs_in_place(daemon):
    """スーパーバイザー自身の更新は同じPIDで再execし、待ち受けを手放さない。"""
    sid = storage.create_session("本文", "再exec", "")["id"]
    before = daemon.health()
    with Hammer(daemon.port, sid) as hammer:
        daemon.touch_code("daemon.py", "reexec")
        after = daemon.wait_for(
            lambda h: h and h["version"] == h["disk_version"]
            and h["version"] != before["version"], 40, "再exec と入れ替え")
    assert after["supervisor_pid"] == before["supervisor_pid"] == daemon.proc.pid
    assert hammer.fails == []
    assert _saved_html(sid) == hammer.last_html
    assert "再execします" in daemon.log_text()


def test_admin_status_and_stop(daemon):
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "hle_mcp.admin", *a], env=daemon.env,
        capture_output=True, text=True, timeout=90)
    out = run("--status")
    assert out.returncode == 0
    assert json.loads(out.stdout)["supervised"] is True
    out = run("--stop")
    assert out.returncode == 0, out.stderr
    assert daemon.health() is None
    assert daemon.proc.wait(timeout=10) == 0
    assert not (daemon.data_dir / "web-status.json").exists()


def test_ensure_server_spawns_daemon_once_and_reuses_it(tmp_path):
    """MCPサーバー等からの ensure_server は常駐デーモンを1つだけ起動して使い回す。"""
    port = free_port()
    try:
        assert webserver.ensure_server(port) == port
        first = webserver.health_info(port)
        assert first["supervised"] is True
        assert webserver.ensure_server(port) == port  # 2回目は起動しない
        assert webserver.health_info(port)["supervisor_pid"] == first["supervisor_pid"]
        assert webserver.server_note(port) == ""
    finally:
        info = webserver.health_info(port) or {}
        if info.get("supervisor_pid"):
            os.kill(info["supervisor_pid"], signal.SIGTERM)
            for _ in range(100):
                if webserver.health_info(port) is None:
                    break
                time.sleep(0.2)

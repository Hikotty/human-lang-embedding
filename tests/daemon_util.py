"""常駐スーパーバイザーをテストで動かすためのヘルパー。

パッケージのソースを一時ディレクトリへ複製し、その複製からスーパーバイザーを
起動する。複製側のファイルを書き換えれば「コードの更新」を安全に再現できる
（リポジトリ本体には触れない）。データディレクトリはテスト側と共有するので、
storage で作ったセッションをデーモンがそのまま配信する。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "hle_mcp"
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daemon:
    def __init__(self, root: Path, data_dir: Path) -> None:
        self.root = root
        self.src = root / "src"
        self.pkg = self.src / "hle_mcp"
        shutil.copytree(REPO_SRC, self.pkg,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.data_dir = data_dir
        self.port = free_port()
        self.log = root / "web.log"
        self.proc: subprocess.Popen | None = None

    @property
    def env(self) -> dict:
        return dict(os.environ, PYTHONPATH=str(self.src),
                    HLE_DATA_DIR=str(self.data_dir), HLE_PORT=str(self.port),
                    HLE_NO_BROWSER="1")

    # ---------- 起動・停止 ----------
    def start(self) -> dict:
        fh = open(self.log, "ab")  # noqa: SIM115  プロセスの寿命と揃える
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hle_mcp.daemon", "--port", str(self.port)],
            env=self.env, stdout=fh, stderr=fh, cwd=str(self.root),
        )
        return self.wait_for(lambda h: h is not None, 20, "起動")

    def stop(self) -> None:
        if self.proc is None:
            return
        st = self.status()
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        # 念のためワーカーの取り残しを掃除する
        pid = st.get("worker_pid")
        if pid:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass

    # ---------- 状態 ----------
    def health(self) -> dict | None:
        try:
            with _LOCAL.open(f"http://127.0.0.1:{self.port}/api/health",
                             timeout=2) as res:
                return json.loads(res.read())
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> dict:
        try:
            return json.loads((self.data_dir / "web-status.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def wait_for(self, pred, timeout: float, what: str) -> dict:
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = self.health()
            if pred(last):
                return last
            time.sleep(0.2)
        raise AssertionError(
            f"{what}を {timeout} 秒待っても確認できません: {last}\n"
            + self.log.read_text("utf-8", "replace")[-3000:]
        )

    def log_text(self) -> str:
        return self.log.read_text("utf-8", "replace")

    # ---------- コードの書き換え ----------
    def edit(self, name: str, fn) -> None:
        path = self.pkg / name
        path.write_text(fn(path.read_text("utf-8")), encoding="utf-8")

    def touch_code(self, name: str = "feedback.py", tag: str = "") -> None:
        """意味の無い変更を加えて版を変える。"""
        self.edit(name, lambda s: s + f"\n# test change {tag or time.time()}\n")


class Hammer:
    """別スレッドから保存と読み込みを連続で送り、失敗を記録する。

    毎回新しい接続を張る（どのワーカーが受けても良いことを確かめる）。
    """

    def __init__(self, port: int, session_id: str) -> None:
        self.port = port
        self.sid = session_id
        self.fails: list[str] = []
        self.ok = 0
        self.last_html: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Hammer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:
        base = f"http://127.0.0.1:{self.port}/api/sessions/{self.sid}"
        i = 0
        while not self._stop.is_set():
            i += 1
            html = f"<p>本文<ins>{i}</ins></p>"
            req = urllib.request.Request(
                base + "/document",
                data=json.dumps({"html": html, "revision": 0}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                with _LOCAL.open(req, timeout=15) as res:
                    res.read()
                self.last_html = html
                with _LOCAL.open(base, timeout=15) as res:
                    res.read()
                self.ok += 1
            except Exception as e:  # noqa: BLE001
                self.fails.append(f"{type(e).__name__}: {e}")

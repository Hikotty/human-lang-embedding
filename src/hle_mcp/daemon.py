"""常駐Webサーバーのスーパーバイザー（`python -m hle_mcp.daemon`）。

スーパーバイザーは 127.0.0.1 の待ち受けソケットを持ち続け、実際の配信は
ワーカープロセス（webserver.run_worker）に任せる。ディスク上のコードが
変わったら次の手順で入れ替える。

1. 新しいワーカーを起動する。新ワーカーは専用ポートで自己テストを行い、
   通ったら同じ待ち受けソケットで受付を始めて READY を返す
2. この時点で新旧ワーカーが同じソケットで受付している（取りこぼしなし）
3. 旧ワーカーに SIGTERM を送る。旧ワーカーは受付をやめ、処理中の
   リクエストに応答し終えてから終了する

待ち受けソケットは一度も閉じないので、入れ替えの間に接続が拒否される
ことはない。新しいコードが壊れていて自己テストに落ちたら旧ワーカーの
まま動き続け、エラーを web-status.json に記録する（/api/health の
update_error で見える）。

このファイルと versioning.py が変わった場合はスーパーバイザー自身を
再exec する。待ち受けソケットと稼働中のワーカーはそのまま引き継ぐ。

ここでは webserver / storage など配信側のコードを import しない。
スーパーバイザーが古いコードを抱え込むと、入れ替えの判断自体が古くなるため。
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import versioning

CHECK_INTERVAL = 1.0     # コード変更の確認間隔（秒）
SETTLE_SECONDS = 2.0     # 変更が落ち着くまで待つ時間（編集途中を拾わない）
READY_TIMEOUT = 30.0     # 新ワーカーの自己テスト＋起動の上限
RETIRE_TIMEOUT = 45.0    # 旧ワーカーのドレイン待ちの上限（超えたら強制終了）
RESPAWN_BACKOFF = (1, 2, 5, 10, 30)


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[hle-daemon {stamp}] {msg}", file=sys.stderr, flush=True)


@dataclass
class Worker:
    pid: int
    version: str
    proc: subprocess.Popen | None = None  # 再exec で引き継いだワーカーは None
    retire_at: float | None = None
    exited: bool = False

    def alive(self) -> bool:
        if self.exited:
            return False
        try:
            pid, _status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.exited = True
            return False
        if pid == self.pid:
            self.exited = True
            return False
        return True

    def signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            self.exited = True


@dataclass
class Supervisor:
    sock: socket.socket
    port: int
    current: Worker | None = None
    retiring: list[Worker] = field(default_factory=list)
    last_error: str = ""
    failed_version: str = ""
    stopping: bool = False
    sup_version: str = ""
    _pending_sig: tuple | None = None
    _pending_since: float = 0.0
    _respawn_count: int = 0
    _next_respawn: float = 0.0

    # ---------- ワーカーの起動 ----------
    def start_worker(self) -> tuple[Worker | None, str]:
        fd = self.sock.fileno()
        env = dict(os.environ, HLE_SUPERVISOR_PID=str(os.getpid()))
        proc = subprocess.Popen(  # noqa: S603 (固定の引数でこのパッケージを起動する)
            [sys.executable, "-m", "hle_mcp.daemon", "--worker", "--fd", str(fd)],
            pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=None, env=env, cwd=str(versioning.PROJECT_ROOT),
        )
        line, eof = _read_line(proc.stdout, READY_TIMEOUT)
        if line.startswith("READY "):
            proc.stdout.close()
            return Worker(proc.pid, line.split(" ", 1)[1].strip(), proc), ""
        timed_out = not line and not eof
        if proc.poll() is None:
            proc.kill()
        code = proc.wait()
        proc.stdout.close()
        if line:
            return None, line.removeprefix("ERROR ").strip()
        if timed_out:
            return None, f"ワーカーが {READY_TIMEOUT:.0f} 秒以内に起動しませんでした"
        return None, f"ワーカーが起動直後に終了しました（終了コード {code}）"

    def rollover(self, target: str) -> None:
        """新しいワーカーを立ててから古いワーカーを退かせる。"""
        log(f"コードの更新を検知（{self.current.version if self.current else '-'}"
            f" → {target}）。新しいワーカーを起動します")
        new, err = self.start_worker()
        if new is None:
            self.last_error = err
            self.failed_version = target
            log(f"新しいワーカーの起動に失敗。現行のワーカーで配信を続けます: {err}")
            self.write_status()
            return
        old = self.current
        self.current = new
        self.last_error = ""
        self.failed_version = ""
        self._respawn_count = 0
        log(f"ワーカー {new.pid}（版 {new.version}）が配信を開始")
        if old is not None:
            self.retire(old)
        self.write_status()

    def retire(self, w: Worker) -> None:
        w.signal(signal.SIGTERM)
        w.retire_at = time.monotonic()
        self.retiring.append(w)
        log(f"ワーカー {w.pid}（版 {w.version}）を終了させます（処理中の応答は待つ）")

    # ---------- 監視ループ ----------
    def tick(self) -> None:
        now = time.monotonic()
        for w in list(self.retiring):
            if not w.alive():
                self.retiring.remove(w)
            elif now - (w.retire_at or now) > RETIRE_TIMEOUT:
                log(f"ワーカー {w.pid} が終わらないので強制終了します")
                w.signal(signal.SIGKILL)
        if self.current is not None and not self.current.alive():
            log(f"ワーカー {self.current.pid} が予期せず終了しました。再起動します")
            self.current = None
        if self.current is None:
            if now >= self._next_respawn:
                self.respawn()
            return
        self.check_code(now)

    def respawn(self) -> None:
        new, err = self.start_worker()
        if new is not None:
            self.current = new
            self._respawn_count = 0
            self.last_error = ""
            log(f"ワーカー {new.pid}（版 {new.version}）が配信を開始")
        else:
            # 配信できるワーカーが無い。接続は待ち受けソケットで待たされるので
            # すぐには拒否されない（ブラウザ側は保存を再試行する）
            wait = RESPAWN_BACKOFF[min(self._respawn_count, len(RESPAWN_BACKOFF) - 1)]
            self._respawn_count += 1
            self._next_respawn = time.monotonic() + wait
            self.last_error = err
            log(f"ワーカーを起動できません（{wait} 秒後に再試行）: {err}")
        self.write_status()

    def check_code(self, now: float) -> None:
        sig = versioning.stat_signature(versioning.py_files())
        if sig != self._pending_sig:
            # 変化があった（または初回）。落ち着くまで待つ
            self._pending_sig = sig
            self._pending_since = now
            return
        if now - self._pending_since < SETTLE_SECONDS:
            return
        if versioning.supervisor_version() != self.sup_version:
            self.reexec()
            return
        target = versioning.code_version()
        if target != self.current.version and target != self.failed_version:
            self.rollover(target)

    # ---------- スーパーバイザー自身の更新 ----------
    def reexec(self) -> None:
        check = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "import hle_mcp.daemon"],
            cwd=str(versioning.PROJECT_ROOT), capture_output=True, text=True,
            timeout=30,
        )
        if check.returncode != 0:
            self.last_error = "スーパーバイザーの新しいコードを読み込めません: " + (
                check.stderr.strip().splitlines() or ["?"])[-1]
            self.sup_version = versioning.supervisor_version()  # 同じ版で繰り返さない
            log(self.last_error)
            self.write_status()
            return
        log("スーパーバイザーのコードが更新されたので再execします")
        fd = self.sock.fileno()
        os.set_inheritable(fd, True)
        args = [sys.executable, "-m", "hle_mcp.daemon", "--fd", str(fd),
                "--port", str(self.port)]
        if self.current is not None:
            args += ["--adopt", f"{self.current.pid}:{self.current.version}"]
        for w in self.retiring:
            args += ["--retiring", str(w.pid)]
        os.chdir(str(versioning.PROJECT_ROOT))
        os.execv(sys.executable, args)

    # ---------- 状態の書き出し ----------
    def write_status(self) -> None:
        st = {
            "supervisor_pid": os.getpid(),
            "port": self.port,
            "worker_pid": self.current.pid if self.current else None,
            "worker_version": self.current.version if self.current else "",
            "last_error": self.last_error,
            "failed_version": self.failed_version,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        path = versioning.status_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log(f"状態ファイルを書けません: {e}")

    # ---------- 停止 ----------
    def shutdown(self) -> None:
        log("停止します。ワーカーの処理中の応答を待ちます")
        workers = ([self.current] if self.current else []) + self.retiring
        for w in workers:
            w.signal(signal.SIGTERM)
        deadline = time.monotonic() + RETIRE_TIMEOUT
        while time.monotonic() < deadline and any(w.alive() for w in workers):
            time.sleep(0.2)
        for w in workers:
            if w.alive():
                w.signal(signal.SIGKILL)
        self.sock.close()
        try:
            st = json.loads(versioning.status_path().read_text(encoding="utf-8"))
            if st.get("supervisor_pid") == os.getpid():
                versioning.status_path().unlink()
        except (OSError, ValueError):
            pass
        log("停止しました")


def _read_line(stream, timeout: float) -> tuple[str, bool]:
    """ワーカーの合図（1行）を timeout 秒まで待つ。(行, EOFに達したか) を返す。"""
    sel = selectors.DefaultSelector()
    sel.register(stream, selectors.EVENT_READ)
    buf = b""
    eof = False
    deadline = time.monotonic() + timeout
    try:
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0 or not sel.select(left):
                break
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                eof = True
                break
            buf += chunk
    finally:
        sel.close()
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip(), eof


def _listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(64)
    return sock


def run_supervisor(port: int, fd: int | None, adopt: str | None,
                   retiring: list[int]) -> int:
    if fd is None:
        try:
            sock = _listen(port)
        except OSError as e:
            log(f"127.0.0.1:{port} で待ち受けできません: {e}")
            return 2
    else:
        sock = socket.socket(fileno=fd)
    port = sock.getsockname()[1]
    sup = Supervisor(sock=sock, port=port,
                     sup_version=versioning.supervisor_version())
    for pid in retiring:
        sup.retiring.append(Worker(pid, "", retire_at=time.monotonic()))
    if adopt:
        pid, _, ver = adopt.partition(":")
        sup.current = Worker(int(pid), ver)
        log(f"再exec しました。ワーカー {pid}（版 {ver}）を引き継ぎます")
    else:
        log(f"http://127.0.0.1:{port} で待ち受けを開始（PID {os.getpid()}）")

    def on_term(*_):
        sup.stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    if sup.current is None:
        sup.respawn()
    sup.write_status()
    while not sup.stopping:
        try:
            sup.tick()
        except Exception as e:  # noqa: BLE001  監視ループは落とさない
            log(f"監視中のエラー: {type(e).__name__}: {e}")
        time.sleep(CHECK_INTERVAL)
    sup.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hle_mcp.daemon")
    p.add_argument("--port", type=int, default=8917)
    p.add_argument("--fd", type=int, default=None)
    p.add_argument("--worker", action="store_true")
    p.add_argument("--adopt", default=None)
    p.add_argument("--retiring", type=int, action="append", default=[])
    a = p.parse_args(argv)
    if a.worker:
        if a.fd is None:
            p.error("--worker には --fd が必要です")
        try:
            from . import webserver  # ワーカーだけが配信側のコードを読み込む
        except Exception as e:  # noqa: BLE001  壊れたコードを具体的に知らせる
            where = ""
            if isinstance(e, SyntaxError) and e.filename:
                where = f"（{os.path.basename(e.filename)} {e.lineno} 行目）"
            print(f"ERROR コードを読み込めません{where}: {type(e).__name__}: {e}",
                  flush=True)
            raise
        return webserver.run_worker(a.fd)
    return run_supervisor(a.port, a.fd, a.adopt, a.retiring)


if __name__ == "__main__":
    sys.exit(main())

"""管理画面を開く・常駐Webサーバーを操作するCLI（`hle-admin` コマンド）。

- 引数なし: 常駐Webサーバーが無ければ起動し、管理画面をブラウザで開いて
  すぐに終了する。Webサーバーは常駐スーパーバイザーとして残り、コードの
  更新を検知すると無停止で新しい版に入れ替わる
- --status: 稼働中のサーバーの版・入れ替え状況を表示する
- --stop:   常駐Webサーバーを止める（処理中の保存を返し終えてから止まる）
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import webbrowser

from . import versioning, webserver


def _status(port: int) -> int:
    info = webserver.health_info(port)
    if info is None:
        print(f"127.0.0.1:{port} で Webサーバーは応答していません", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    note = webserver.server_note(port)
    if note:
        print(f"[hle-admin] {note}", file=sys.stderr)
    return 0


def _stop(port: int) -> int:
    info = webserver.health_info(port) or {}
    sup = info.get("supervisor_pid")
    if not sup:
        st = webserver._our_daemon_on(port) or {}  # noqa: SLF001
        sup = st.get("supervisor_pid")
    if not sup:
        if info:
            print("[hle-admin] 稼働中のサーバーは旧方式で、ここからは止められません。"
                  f"PID {info.get('pid', '不明')} のプロセスを止めてください。",
                  file=sys.stderr)
        else:
            print(f"[hle-admin] 127.0.0.1:{port} に常駐Webサーバーはありません",
                  file=sys.stderr)
        return 1
    os.kill(int(sup), signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if webserver.health_info(port) is None and \
                webserver._our_daemon_on(port) is None:  # noqa: SLF001
            print("[hle-admin] 常駐Webサーバーを止めました", file=sys.stderr)
            return 0
        time.sleep(0.3)
    print("[hle-admin] 60秒待っても止まりませんでした。ログを確認してください: "
          f"{versioning.log_path()}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hle-admin", description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="稼働状況を表示する")
    ap.add_argument("--stop", action="store_true", help="常駐Webサーバーを止める")
    args = ap.parse_args(argv)
    port = int(os.environ.get("HLE_PORT", webserver.DEFAULT_PORT))
    if args.status:
        return _status(port)
    if args.stop:
        return _stop(port)
    actual_port = webserver.ensure_server(port)
    url = f"http://127.0.0.1:{actual_port}/admin"
    print(f"[hle-admin] 管理画面: {url}", file=sys.stderr)
    note = webserver.server_note(actual_port)
    if note:
        print(f"[hle-admin] {note}", file=sys.stderr)
    if not os.environ.get("HLE_NO_BROWSER"):
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())

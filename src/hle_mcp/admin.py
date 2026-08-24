"""管理画面をアドホックに開くCLI（`hle-admin` コマンド）。

- 既にMCPサーバー等がWebサーバーを起動していればそれを再利用して
  ブラウザを開き、すぐに終了する。
- 起動していなければ自前でWebサーバーを立ち上げ、Ctrl+C まで常駐する。
"""

from __future__ import annotations

import os
import sys
import time
import webbrowser

from . import webserver


def main() -> None:
    port = int(os.environ.get("HLE_PORT", webserver.DEFAULT_PORT))
    reused = webserver._health_ok(port)  # noqa: SLF001
    actual_port = webserver.ensure_server(port)
    url = f"http://127.0.0.1:{actual_port}/admin"
    print(f"[hle-admin] 管理画面: {url}", file=sys.stderr)
    if not os.environ.get("HLE_NO_BROWSER"):
        webbrowser.open(url)
    if reused:
        print("[hle-admin] 稼働中のサーバーを再利用しました。", file=sys.stderr)
        return
    print("[hle-admin] サーバーを起動しました。Ctrl+C で終了します。",
          file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        webserver.stop_server()
        print("[hle-admin] 終了しました。", file=sys.stderr)


if __name__ == "__main__":
    main()

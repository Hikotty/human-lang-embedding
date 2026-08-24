"""ローカルWebサーバー。

レビューエディタのUI（静的ファイル）とセッション操作のJSON APIを
127.0.0.1 上で配信する。MCPサーバープロセス内のデーモンスレッドで動く。
stdioトランスポートを壊さないよう、ログは一切stdoutに出さない。
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import feedback, storage

APP_NAME = "hle-mcp"
DEFAULT_PORT = 8917
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        print(f"[hle-web] {fmt % args}", file=sys.stderr)

    # ---------- 共通レスポンス ----------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_error_json(404, "ファイルが見つかりません")
            return
        body = path.read_bytes()
        ctype = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 20_000_000:
            raise ValueError("リクエストボディが不正です")
        raw = self.rfile.read(length)
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("JSONオブジェクトを指定してください")
        return obj

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except KeyError as e:
            self._send_error_json(404, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, f"サーバーエラー: {e}")

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/" or (len(parts) == 2 and parts[0] == "session"):
            self._send_file(_STATIC_DIR / "index.html")
        elif parsed.path == "/admin":
            self._send_file(_STATIC_DIR / "admin.html")
        elif len(parts) == 2 and parts[0] == "static":
            name = Path(parts[1]).name  # パストラバーサル防止
            self._send_file(_STATIC_DIR / name)
        elif parsed.path == "/api/health":
            self._send_json({"app": APP_NAME, "ok": True})
        elif len(parts) == 5 and parts[:2] == ["api", "sessions"] \
                and parts[3] == "files":
            storage.load_session(parts[2])  # 存在確認
            self._send_file(storage.get_image_path(parts[2], unquote(parts[4])))
        elif len(parts) == 4 and parts[:2] == ["api", "sessions"] \
                and parts[3] == "feedback":
            self._send_json(feedback.build_feedback(storage.load_session(parts[2])))
        elif parsed.path == "/api/sessions":
            q = parse_qs(parsed.query)
            self._send_json(
                {
                    "sessions": storage.list_sessions(
                        query=(q.get("query") or [""])[0],
                        workspace=(q.get("workspace") or [""])[0],
                        limit=int((q.get("limit") or ["50"])[0]),
                    )
                }
            )
        elif len(parts) == 3 and parts[:2] == ["api", "sessions"]:
            self._send_json(storage.load_session(parts[2]))
        else:
            self._send_error_json(404, "不明なパスです")

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except KeyError as e:
            self._send_error_json(404, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            self._send_error_json(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, f"サーバーエラー: {e}")

    def _route_post(self) -> None:
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) < 4 or parts[:2] != ["api", "sessions"]:
            self._send_error_json(404, "不明なパスです")
            return
        sid = parts[2]
        body = self._read_body_json()
        if parts[3] == "document" and len(parts) == 4:
            storage.set_document(sid, str(body.get("html", "")))
            self._send_json({"ok": True})
        elif parts[3] == "comments" and len(parts) == 4:
            comment = storage.add_comment(
                sid,
                quote=str(body.get("quote", "")),
                text=str(body.get("text", "")),
                author=str(body.get("author", "human")),
                comment_id=body.get("id"),
            )
            self._send_json(comment)
        elif parts[3] == "comments" and len(parts) == 6 and parts[5] == "replies":
            reply = storage.add_reply(
                sid,
                comment_id=parts[4],
                text=str(body.get("text", "")),
                author=str(body.get("author", "human")),
            )
            self._send_json(reply)
        elif parts[3] == "status" and len(parts) == 4:
            storage.set_status(sid, str(body.get("status", "")))
            self._send_json({"ok": True})
        elif parts[3] == "images" and len(parts) == 4:
            storage.load_session(sid)  # 存在確認
            try:
                data = base64.b64decode(str(body.get("data_base64", "")))
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"base64のデコードに失敗しました: {e}") from e
            name = storage.save_image_bytes(sid, str(body.get("name", "")), data)
            self._send_json(
                {"name": name, "url": f"/api/sessions/{sid}/files/{name}"}
            )
        else:
            self._send_error_json(404, "不明なパスです")


# ---------- サーバーライフサイクル ----------
_server_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_server_port: int | None = None


def start_server(port: int = DEFAULT_PORT) -> int:
    """Webサーバーをデーモンスレッドで起動し、実際のポート番号を返す。"""
    global _server, _server_port
    with _server_lock:
        if _server is not None and _server_port is not None:
            return _server_port
        srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        srv.daemon_threads = True
        thread = threading.Thread(
            target=srv.serve_forever, name="hle-web", daemon=True
        )
        thread.start()
        _server = srv
        _server_port = srv.server_address[1]
        print(f"[hle-web] http://127.0.0.1:{_server_port} で起動", file=sys.stderr)
        return _server_port


def stop_server() -> None:
    global _server, _server_port
    with _server_lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
            _server_port = None


def _health_ok(port: int) -> bool:
    """指定ポートで本アプリのヘルスチェックが通るか確認する。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=1.0
        ) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data.get("app") == APP_NAME
    except Exception:  # noqa: BLE001
        return False


def ensure_server(port: int = DEFAULT_PORT) -> int:
    """サーバーが動作していることを保証してポート番号を返す。

    - 自プロセスで起動済みならそのポートを返す
    - 指定ポートが他プロセスの本アプリで使用中ならそのまま利用する
    - 空いていれば新規に起動する（衝突時は近傍ポートを試す）
    """
    global _server_port
    if _server_port is not None:
        return _server_port
    for candidate in range(port, port + 10):
        if _health_ok(candidate):
            _server_port_cache(candidate)
            return candidate
        try:
            return start_server(candidate)
        except OSError:
            continue
    raise RuntimeError("Webサーバーのポートを確保できませんでした")


def _server_port_cache(port: int) -> None:
    """他プロセスで稼働中のサーバーを再利用する際にポートを記録する。"""
    global _server_port
    with _server_lock:
        if _server_port is None:
            _server_port = port


def session_url(port: int, session_id: str) -> str:
    return f"http://127.0.0.1:{port}/session/{session_id}"

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

# ループバックを指す正当なホスト名。DNSリバインディング対策の許可リスト。
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

# 全レスポンス共通のセキュリティヘッダ
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
)

# HTMLに与えるCSP。インラインscript/styleは使っていないので'self'のみで足りる。
_CSP_HTML = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
# HTMLレビューページ用。レビュー対象HTMLを同一オリジンのiframeで埋め込むため
# frame-src 'self' を許可する（それ以外は _CSP_HTML と同一）。
_CSP_HTML_REVIEW = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)
# iframe内に表示するレビュー対象HTML用。見た目の再現に必要なインラインCSSと
# data:画像のみ許可し、スクリプトは一切許可しない（iframeのsandbox属性でも
# 二重に遮断される）。frame-ancestors 'self' で自オリジンからの埋め込みのみ許可。
_CSP_FRAME_DOC = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; font-src data:; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)
# HTML以外（JSON・画像・静的アセット）はすべて禁止で足りる。
_CSP_STRICT = "default-src 'none'; frame-ancestors 'none'"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        print(f"[hle-web] {fmt % args}", file=sys.stderr)

    # ---------- リクエストガード ----------
    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"{h}:{port}" for h in _LOOPBACK_HOSTS} | set(_LOOPBACK_HOSTS)

    def _allowed_origins(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"http://{h}:{port}" for h in _LOOPBACK_HOSTS}

    def _guard_reason(self) -> str | None:
        """リクエストを拒否すべき理由を返す（許可する場合は None）。

        - Host: ループバック名以外を拒否する（DNSリバインディング対策）
        - Origin: 状態変更系で自オリジン以外を拒否する（CSRF対策）
          ※ Originを送らないローカルCLIやテストは許可する。CSRFはブラウザ
            由来の脅威であり、ローカルプロセスはデータを直読できるため。
        - Content-Type: POSTはapplication/json必須（simple requestの封じ込め）
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host or host not in self._allowed_hosts():
            return f"許可されないHostヘッダです: {host or '(なし)'}"
        if self.command != "POST":
            return None
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin.lower() not in self._allowed_origins():
            return f"許可されないOriginです: {origin}"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return (
                "Content-Type に application/json を指定してください: "
                f"{ctype or '(なし)'}"
            )
        return None

    def _reject(self, reason: str) -> None:
        """ガード違反を403で拒否し、接続を閉じる。

        POSTの場合は呼び出し側で _drain_body してからこれを呼ぶ
        （ボディを読み残したまま閉じるとクライアントが応答を読めない）。
        """
        self.log_message("拒否: %s", reason)
        self.close_connection = True
        self._send_json({"error": reason}, 403)

    # ---------- 共通レスポンス ----------
    def _emit(
        self, body: bytes, ctype: str, status: int, csp: str,
        frameable: bool = False,
    ) -> None:
        """レスポンスを送出する。frameable は同一オリジンのiframe埋め込みを
        許可する（HTMLレビューの対象ドキュメント配信でのみ使う）。"""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)
        for name, value in _SECURITY_HEADERS:
            if frameable and name == "X-Frame-Options":
                value = "SAMEORIGIN"
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._emit(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
            _CSP_STRICT,
        )

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _send_file(self, path: Path, csp: str | None = None) -> None:
        if not path.is_file():
            self._send_error_json(404, "ファイルが見つかりません")
            return
        if csp is None:
            csp = _CSP_HTML if path.suffix == ".html" else _CSP_STRICT
        self._emit(
            path.read_bytes(),
            _CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
            200,
            csp,
        )

    def _drain_body(self) -> None:
        """拒否したPOSTのボディを読み捨てて接続の同期を保つ。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        remaining = min(max(length, 0), 1_000_000)
        if length > remaining:
            self.close_connection = True
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

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
        reason = self._guard_reason()
        if reason:
            self._reject(reason)
            return
        try:
            self._route_get()
        except KeyError as e:
            self._send_error_json(404, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, f"サーバーエラー: {e}")

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/":
            self._send_file(_STATIC_DIR / "index.html")
        elif len(parts) == 2 and parts[0] == "session":
            # モードで配信するページを分ける。存在しないIDは従来どおり
            # index.html（app.jsが「見つかりません」を表示する）
            try:
                mode = storage.session_mode(storage.load_session(parts[1]))
            except KeyError:
                mode = "md"
            if mode == "html":
                self._send_file(
                    _STATIC_DIR / "html-review.html", csp=_CSP_HTML_REVIEW
                )
            else:
                self._send_file(_STATIC_DIR / "index.html")
        elif parsed.path == "/admin":
            self._send_file(_STATIC_DIR / "admin.html")
        elif len(parts) == 2 and parts[0] == "static":
            name = Path(parts[1]).name  # パストラバーサル防止
            self._send_file(_STATIC_DIR / name)
        elif parsed.path == "/api/health":
            self._send_json({"app": APP_NAME, "ok": True})
        elif len(parts) == 4 and parts[:2] == ["api", "sessions"] \
                and parts[3] == "source":
            # HTMLレビューの対象ドキュメント。iframeから読み込まれる。
            s = storage.load_session(parts[2])
            if storage.session_mode(s) != "html":
                raise KeyError("HTMLレビューセッションではありません")
            self._emit(
                storage.current_revision(s)["base_text"].encode("utf-8"),
                "text/html; charset=utf-8",
                200,
                _CSP_FRAME_DOC,
                frameable=True,
            )
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
            s = storage.load_session(parts[2])
            # エディタの「AI差分」表示用に、前の稿の最終状態（人間の校正を
            # 反映したテキスト）を添える。第1稿には比較対象が無い
            if storage.session_mode(s) == "md" and len(s["revisions"]) >= 2:
                s["prev_result_text"] = feedback.revision_result_text(
                    s["revisions"][-2]
                )
            self._send_json(s)
        else:
            self._send_error_json(404, "不明なパスです")

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        reason = self._guard_reason()
        if reason:
            self._drain_body()
            self._reject(reason)
            return
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
                prefix=str(body.get("prefix", "") or ""),
                suffix=str(body.get("suffix", "") or ""),
                target=str(body.get("target", "") or "text"),
                image=str(body.get("image", "") or ""),
                image_ref=str(body.get("image_ref", "") or ""),
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
        # 更新・削除もPOSTのサブパスで受ける。DELETE/PATCHを増やすと
        # _guard_reason のOrigin/Content-Type検査がPOST限定なので穴になる。
        elif parts[3] == "comments" and len(parts) == 6 and parts[5] == "update":
            comment = storage.update_comment(
                sid, parts[4], str(body.get("text", ""))
            )
            self._send_json(comment)
        elif parts[3] == "comments" and len(parts) == 6 and parts[5] == "delete":
            storage.delete_comment(sid, parts[4])
            self._send_json({"ok": True})
        elif parts[3] == "comments" and len(parts) == 8 \
                and parts[5] == "replies" and parts[7] == "update":
            reply = storage.update_reply(
                sid, parts[4], parts[6], str(body.get("text", ""))
            )
            self._send_json(reply)
        elif parts[3] == "comments" and len(parts) == 8 \
                and parts[5] == "replies" and parts[7] == "delete":
            storage.delete_reply(sid, parts[4], parts[6])
            self._send_json({"ok": True})
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

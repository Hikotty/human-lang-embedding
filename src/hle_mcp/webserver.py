"""ローカルWebサーバー。

レビューエディタのUI（静的ファイル）とセッション操作のJSON APIを
127.0.0.1 上で配信する。通常は常駐スーパーバイザー（daemon.py）の
ワーカープロセスとして動き、コードが更新されると新しいワーカーに
無停止で入れ替わる。テスト（port=0）ではこのプロセス内のスレッドで動く。
stdioトランスポートを壊さないよう、ログは一切stdoutに出さない。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import feedback, storage, versioning

APP_NAME = "hle-mcp"
DEFAULT_PORT = 8917
_STATIC_DIR = versioning.STATIC_DIR
# このプロセスが読み込んだPythonコードの版。ディスク上の版と食い違えば
# 古いコードで動いていることになる
LOADED_VERSION = versioning.code_version()

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


def build_version() -> str:
    """ブラウザに伝える版。Pythonコードと静的ファイルの両方を含む。

    開いているページはこの値が変わったら「再読み込みで新しい版になる」と
    知らせる（静的ファイルだけの変更でもページ側のJSは古いままなので）。
    """
    return f"{LOADED_VERSION}-{versioning.static_version()}"


class _Server(ThreadingHTTPServer):
    """処理中のリクエストを数え、入れ替え時に安全に止められるHTTPサーバー。

    ワーカーの入れ替えでは、新しいワーカーが同じ待ち受けソケットで受付を
    始めてから古いワーカーを drain() する。drain() は新規の受付をやめた後、
    処理中のリクエストが応答し終わるまで待つ。待機中の keep-alive 接続は
    閉じてブラウザに新しい接続（＝新しいワーカー）へ張り直させる。
    """

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self._cond = threading.Condition()
        self._inflight = 0
        self._conns: dict[socket.socket, bool] = {}  # 接続 -> 処理中か
        self.draining = False
        super().__init__(*args, **kwargs)

    def get_request(self):
        conn, addr = self.socket.accept()
        # 待ち受けソケットは複数ワーカーで共有するため非ブロッキングにしてある。
        # 受け付けた接続は通常のブロッキングで扱う
        conn.setblocking(True)
        return conn, addr

    def track(self, conn: socket.socket) -> None:
        with self._cond:
            self._conns[conn] = False

    def untrack(self, conn: socket.socket) -> None:
        with self._cond:
            self._conns.pop(conn, None)
            self._cond.notify_all()

    @contextmanager
    def request_scope(self, conn: socket.socket):
        with self._cond:
            self._inflight += 1
            self._conns[conn] = True
        try:
            yield
        finally:
            with self._cond:
                self._inflight -= 1
                if conn in self._conns:
                    self._conns[conn] = False
                self._cond.notify_all()

    def close_all(self) -> None:
        """接続をすべて切る（keep-alive の処理スレッドを残さない）。"""
        with self._cond:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def drain(self, timeout: float = 30.0) -> bool:
        """処理中のリクエストが終わるまで待つ。serve_forever を止めてから呼ぶ。"""
        with self._cond:
            self.draining = True
            idle = [c for c, busy in self._conns.items() if not busy]
        for c in idle:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._inflight > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        print(f"[hle-web] {fmt % args}", file=sys.stderr)

    # ---------- 接続の追跡（無停止入れ替えのため） ----------
    def setup(self) -> None:
        super().setup()
        if isinstance(self.server, _Server):
            self.server.track(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if isinstance(self.server, _Server):
                self.server.untrack(self.connection)

    @contextmanager
    def _scope(self):
        if isinstance(self.server, _Server):
            with self.server.request_scope(self.connection):
                yield
        else:
            yield

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
        # 入れ替えで止まる途中のワーカーは、応答後に接続を閉じて
        # ブラウザを新しいワーカーへ張り直させる
        if getattr(self.server, "draining", False):
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-HLE-Version", build_version())
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
        with self._scope():
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
            self._send_json(health_payload())
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
        with self._scope():
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
            rev = body.get("revision")
            if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
                raise ValueError("revision には稿の番号（整数）を指定してください")
            current = storage.set_document(sid, str(body.get("html", "")), rev)
            # stale=True は「前の稿への編集なので別枠に退避した」の意味
            self._send_json({"ok": True, "stale": not current})
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
                tag=str(body.get("tag", "") or storage.DEFAULT_COMMENT_TAG),
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
            # tag を送らない更新（旧クライアント）は種類を変えない
            tag = body.get("tag")
            comment = storage.update_comment(
                sid, parts[4], str(body.get("text", "")),
                tag=None if tag is None else str(tag),
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

# ループバックへの問い合わせはシステムのプロキシ設定を通さない
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def start_server(port: int = DEFAULT_PORT) -> int:
    """Webサーバーをこのプロセスのデーモンスレッドで起動し、ポート番号を返す。

    テスト（port=0 のエフェメラルポート）用。通常運用では ensure_server が
    常駐スーパーバイザーを起動する。
    """
    global _server, _server_port
    with _server_lock:
        if _server is not None and _server_port is not None:
            return _server_port
        srv = _Server(("127.0.0.1", port), _Handler)
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
            if isinstance(_server, _Server):
                _server.close_all()
            _server = None
            _server_port = None


# ---------- ヘルスチェック ----------
def _supervisor_pid() -> int | None:
    """このプロセスを管理しているスーパーバイザーのPID（居なければ None）。"""
    try:
        pid = int(os.environ.get("HLE_SUPERVISOR_PID", "0"))
    except ValueError:
        return None
    return pid if pid > 0 and os.getppid() == pid else None


def _read_status() -> dict:
    """スーパーバイザーが書き出した入れ替え状況（自分の親のものだけ読む）。"""
    sup = _supervisor_pid()
    if sup is None:
        return {}
    try:
        st = json.loads(versioning.status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) and st.get("supervisor_pid") == sup else {}


def health_payload() -> dict:
    st = _read_status()
    return {
        "app": APP_NAME,
        "ok": True,
        "version": LOADED_VERSION,
        "disk_version": versioning.code_version(),
        "pid": os.getpid(),
        "supervisor_pid": _supervisor_pid(),
        "supervised": _supervisor_pid() is not None,
        "update_error": st.get("last_error", "") if st else "",
    }


def health_info(port: int, timeout: float = 1.0) -> dict | None:
    """指定ポートで本アプリが応答すればヘルス情報を、そうでなければ None を返す。"""
    try:
        with _LOCAL.open(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_NAME else None


def _health_ok(port: int) -> bool:
    """指定ポートで本アプリのヘルスチェックが通るか確認する。"""
    return health_info(port) is not None


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


# ---------- 常駐スーパーバイザーの起動 ----------
_LOG_ROTATE_BYTES = 5_000_000


def _rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size > _LOG_ROTATE_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass


def _our_daemon_on(port: int) -> dict | None:
    """指定ポートで本アプリのスーパーバイザーが生きていれば、その状態を返す。

    ワーカーが起動できない間（コードが壊れているなど）はヘルスチェックに
    応答しないが、待ち受けソケットは生きている。これを「他アプリ」と誤認して
    隣のポートに別のデーモンを立てないために使う。
    """
    try:
        st = json.loads(versioning.status_path().read_text(encoding="utf-8"))
        pid = int(st.get("supervisor_pid") or 0)
    except (OSError, ValueError, TypeError):
        return None
    if st.get("port") != port or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return st


def spawn_daemon(port: int, wait: float = 20.0) -> bool:
    """常駐スーパーバイザーを切り離して起動し、応答するまで待つ。

    呼び出し元（MCPサーバーや hle-admin）が終了してもWebサーバーは残る。
    別プロセスが同時に起動していてポートを取れなかった場合も、そちらが
    応答すれば成功とみなす。スーパーバイザーは起動したがワーカーが
    立ち上がらない場合は、隣のポートへ逃げずにエラーにする。
    """
    log = versioning.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(log)
    with open(log, "ab") as fh:
        proc = subprocess.Popen(  # noqa: S603 (固定の引数でこのパッケージを起動する)
            [sys.executable, "-m", "hle_mcp.daemon", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
            start_new_session=True, close_fds=True,
            cwd=str(versioning.PROJECT_ROOT),
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if health_info(port) is not None:
            return True
        if proc.poll() is not None:
            break  # 起動に失敗した（ポート使用中など）
        time.sleep(0.1)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if health_info(port) is not None:
            return True
        time.sleep(0.2)
    if proc.poll() is None:
        st = _our_daemon_on(port) or {}
        raise RuntimeError(
            "Webサーバーは起動しましたが配信を始められません"
            f"（{st.get('last_error') or '原因はログを参照'}: {versioning.log_path()}）"
        )
    return False


def ensure_server(port: int = DEFAULT_PORT) -> int:
    """サーバーが動作していることを保証してポート番号を返す。

    - 自プロセスで起動済み（テスト用のスレッド起動）ならそのポートを返す
    - port=0 はテスト用。このプロセスのスレッドでエフェメラルポートに起動する
    - 指定ポートで本アプリが応答すればそれを使う。常駐スーパーバイザーは
      コードの更新を自分で検知して入れ替わるので、再利用しても古くならない
    - 空いていれば常駐スーパーバイザーを起動する（他アプリが使っていれば
      近傍ポートを試す）
    """
    if _server_port is not None:
        return _server_port
    if port == 0:
        return start_server(0)
    for candidate in range(port, port + 10):
        if health_info(candidate) is not None:
            return candidate
        st = _our_daemon_on(candidate)
        if st is not None:
            # 本アプリのデーモンだがワーカーが居ない（再起動中か、コードが
            # 壊れている）。少し待って、だめならエラーにする
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if health_info(candidate) is not None:
                    return candidate
                time.sleep(0.3)
            st = _our_daemon_on(candidate) or st
            raise RuntimeError(
                "Webサーバーのワーカーが起動できません"
                f"（{st.get('last_error') or '原因はログを参照'}: {versioning.log_path()}）"
            )
        if not _port_free(candidate):
            continue  # 本アプリ以外が使っている
        if spawn_daemon(candidate):
            return candidate
    raise RuntimeError("Webサーバーのポートを確保できませんでした")


def server_note(port: int) -> str:
    """稼働中のWebサーバーが最新コードで動いていない場合の注意書きを返す。"""
    if _server is not None:
        return ""  # このプロセスのスレッドで動くテスト用サーバー
    info = health_info(port)
    if info is None:
        return "Webサーバーに接続できません。`uv run hle-admin` で起動してください。"
    if "version" not in info:
        return (
            "このポートでは旧方式のWebサーバーが動いており、コードを更新しても"
            "反映されません。エディタが「保存済み」の状態で旧プロセスを止め、"
            "`uv run hle-admin` で起動し直してください。"
        )
    if not info.get("supervised"):
        return "常駐スーパーバイザー外で動くサーバーのため、コード更新は自動反映されません。"
    if info.get("update_error"):
        return (
            "最新コードへの切り替えに失敗したため、前の版で動いています: "
            + str(info["update_error"])
        )
    if info.get("version") != info.get("disk_version"):
        return "最新コードへ切り替え中です（数秒で反映されます）。"
    return ""


# ---------- ワーカー（スーパーバイザーから起動される配信プロセス） ----------
def self_test() -> None:
    """配信を引き受ける前に、自分のコードで主要な画面とAPIが返るか確かめる。

    共有の待ち受けソケットではなく専用のエフェメラルポートで試すので、
    壊れたコードが人間のリクエストを受けることはない。読み取りのみ。
    """
    srv = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        paths = ["/api/health", "/", "/admin", "/static/app.js",
                 "/static/tracker.js", "/api/sessions?limit=1"]
        with _LOCAL.open(base + paths[-1], timeout=10) as res:
            sessions = json.loads(res.read().decode("utf-8"))["sessions"]
        for s in sessions:
            paths += [f"/api/sessions/{s['id']}", f"/session/{s['id']}",
                      f"/api/sessions/{s['id']}/feedback"]
        for path in paths:
            try:
                with _LOCAL.open(base + path, timeout=10) as res:
                    res.read()
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("error", "")
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"{path} が {e.code} を返しました" + (f"（{detail}）" if detail else "")
                ) from None
    finally:
        srv.shutdown()
        srv.server_close()


def run_worker(fd: int) -> int:
    """スーパーバイザーから受け取った待ち受けソケットで配信する。

    標準出力はスーパーバイザーへの合図専用（READY / ERROR の1行）。
    SIGTERM を受けたら新規の受付をやめ、処理中のリクエストを返し終えてから
    終了する。スーパーバイザーが居なくなった場合も同様に終了する。
    """
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # 停止はスーパーバイザーが仕切る
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        self_test()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR 自己テストに失敗: {type(e).__name__}: {e}", flush=True)
        return 1
    sock = socket.socket(fileno=fd)
    # 複数ワーカーが同じソケットで accept するので、取り損ねてもブロックしない
    sock.setblocking(False)
    srv = _Server(sock.getsockname(), _Handler, bind_and_activate=False)
    srv.socket.close()
    srv.socket = sock
    srv.server_address = sock.getsockname()
    srv.server_name, srv.server_port = "127.0.0.1", srv.server_address[1]
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.2},
        name="hle-web", daemon=True,
    )
    thread.start()
    print(f"READY {LOADED_VERSION}", flush=True)
    sys.stdout = sys.stderr  # 以降は標準出力に書かない（パイプを詰まらせない）
    sup = int(os.environ.get("HLE_SUPERVISOR_PID", "0") or 0)
    print(f"[hle-web] worker {os.getpid()} 版 {LOADED_VERSION} で配信開始",
          file=sys.stderr)
    while not stop.wait(1.0):
        if sup and os.getppid() != sup:
            print("[hle-web] スーパーバイザーが居ないので終了します", file=sys.stderr)
            break
    srv.shutdown()
    drained = srv.drain(30.0)
    print(f"[hle-web] worker {os.getpid()} 終了"
          f"（処理中のリクエスト: {'完了' if drained else '30秒で打ち切り'}）",
          file=sys.stderr)
    return 0


def session_url(port: int, session_id: str) -> str:
    return f"http://127.0.0.1:{port}/session/{session_id}"

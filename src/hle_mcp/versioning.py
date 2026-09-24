"""コードの版（フィンガープリント）を計算する。

常駐Webサーバーが「今動いているコード」と「ディスク上の最新コード」を
比べて入れ替えを判断するために使う。スーパーバイザー（daemon.py）からも
読み込むので、標準ライブラリ以外には依存しない。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
STATIC_DIR = PKG_DIR / "static"
PROJECT_ROOT = PKG_DIR.parents[1]


def data_dir() -> Path:
    """データ保存ディレクトリ（環境変数 HLE_DATA_DIR で上書き可能）。"""
    return Path(os.environ.get("HLE_DATA_DIR", PROJECT_ROOT / "data"))


def status_path() -> Path:
    """スーパーバイザーが入れ替えの状況を書き出すファイル。"""
    return data_dir() / "web-status.json"


def log_path() -> Path:
    return data_dir() / "web.log"
# スーパーバイザー自身を構成するファイル。ここが変わったらワーカーの入れ替え
# ではなくスーパーバイザーの再exec が要る
SUPERVISOR_FILES = ("daemon.py", "versioning.py")


def py_files() -> list[Path]:
    return sorted(PKG_DIR.glob("*.py"))


def static_files() -> list[Path]:
    return sorted(p for p in STATIC_DIR.iterdir() if p.is_file())


def stat_signature(files: list[Path]) -> tuple:
    """内容を読まずに変化を検知するための安価な署名（名前・mtime・サイズ）。"""
    sig = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        sig.append((f.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def fingerprint(files: list[Path]) -> str:
    """ファイル名と内容から短いハッシュを作る。"""
    h = hashlib.sha256()
    for f in files:
        try:
            data = f.read_bytes()
        except OSError:
            continue
        h.update(f.name.encode("utf-8") + b"\0" + data + b"\0")
    return h.hexdigest()[:12]


def code_version() -> str:
    """Pythonコード（プロセスの再起動が要る部分）の版。"""
    return fingerprint(py_files())


def supervisor_version() -> str:
    return fingerprint([PKG_DIR / name for name in SUPERVISOR_FILES])


_static_lock = threading.Lock()
_static_cache: tuple[tuple, str] | None = None


def static_version() -> str:
    """静的ファイル（リクエストごとにディスクから配信される部分）の版。

    リクエストのたびに呼ばれるので、mtime とサイズが変わったときだけ
    内容を読み直す。
    """
    global _static_cache
    files = static_files()
    sig = stat_signature(files)
    with _static_lock:
        if _static_cache is not None and _static_cache[0] == sig:
            return _static_cache[1]
        ver = fingerprint(files)
        _static_cache = (sig, ver)
        return ver

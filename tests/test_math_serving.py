"""数式（MathJax）の隔離フレームの配信（ヘッダ・CSP・同梱ファイル・入れ子の配信）のテスト。
本文の TeX は外部由来になりうる文字列なので、MathJax はエディタのページではなく、
不透明オリジンの隔離フレームの中でだけ動かす（Mermaid と同じ作り）。この隔離が
配信の側で崩れていないことと、入れ子のディレクトリ（/static/mathjax/）の配信が
同梱ディレクトリの外へ出ないことを確かめる。
"""
from __future__ import annotations
import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path
import pytest
from hle_mcp import storage, versioning, webserver
STATIC = versioning.STATIC_DIR
MATHJAX = STATIC / "mathjax"
FRAME = "/static/math-frame.html"
FONT = "/static/mathjax/fonts/mathjax-newcm-font/svg/dynamic/double-struck.js"
def _get(port: int, path: str, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
def _directives(csp: str) -> dict[str, str]:
    out = {}
    for part in csp.split(";"):
        name, _, value = part.strip().partition(" ")
        if name:
            out[name] = value.strip()
    return out
def test_editor_csp_is_not_loosened_for_math(web_port):
    """エディタ本体のCSPは、数式のために緩めていない（フレームは frame-src 'self' で足りる）。"""
    sid = storage.create_session("$x$", "数式配信", "/tmp/m")["id"]
    status, headers, _ = _get(web_port, f"/session/{sid}")
    assert status == 200
    csp = headers["Content-Security-Policy"]
    d = _directives(csp)
    assert d["script-src"] == "'self'"
    assert d["style-src"] == "'self'"
    assert d["frame-src"] == "'self'"
    # mask-image の data: URL は img-src で許されている範囲に収まる
    assert d["img-src"] == "'self' data:"
    assert "unsafe-eval" not in csp and "unsafe-inline" not in csp
def test_math_frame_page_has_its_own_csp(web_port):
    """隔離フレーム: 自オリジンのページからしか埋め込めず、通信とeval は許さない。"""
    status, headers, body = _get(web_port, FRAME)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    csp = headers["Content-Security-Policy"]
    d = _directives(csp)
    assert d["default-src"] == "'none'"
    assert d["script-src"] == "'self'"
    # MathJax が SVG の要素に入れる style 属性のために、このフレームの中だけ緩める
    assert d["style-src"] == "'unsafe-inline'"
    assert d["frame-ancestors"] == "'self'"
    assert d["img-src"] == "data:"
    for name in ("connect-src", "frame-src", "form-action"):
        assert d.get(name, "'none'") == "'none'"
    assert "unsafe-eval" not in csp
    assert "script-src 'unsafe-inline'" not in csp
    # フレームが読むスクリプトは同一ホストの3本だけ。インラインのスクリプトは無い
    scripts = re.findall(rb"<script\b[^>]*>", body)
    assert scripts == [
        b'<script src="/static/math-config.js">',
        b'<script src="/static/mathjax/tex-svg.js">',
        b'<script src="/static/math-frame.js">',
    ]
    assert not re.search(rb"<script[^>]*>\s*[^<\s]", body)
def test_math_scripts_are_served_as_javascript(web_port):
    for path in ("/static/math-view.js", "/static/math-config.js",
                 "/static/math-frame.js", "/static/mathjax/tex-svg.js", FONT,
                 "/static/mathjax/input/tex/extensions/boldsymbol.js"):
        status, headers, body = _get(web_port, path)
        assert status == 200, path
        assert headers["Content-Type"].startswith("application/javascript")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert body
@pytest.mark.parametrize("path", [
    "/static/mathjax/../mermaid.min.js",
    "/static/mathjax/%2e%2e/mermaid.min.js",
    "/static/mathjax/fonts/..%2f..%2f..%2fapp.js",
    "/static/mathjax/fonts/%2e%2e/%2e%2e/app.js",
    "/static/mathjax/..%5capp.js",
    "/static/mathjax/.hidden.js",
    "/static/mathjax/VENDOR.txt",
    "/static/mathjax/SHA256SUMS",
    "/static/mathjax/fonts",
    "/static/mathjax/fonts/mathjax-newcm-font/svg/dynamic",
    "/static/mathjax/nothing.js",
    "/static/mathjax/tex-svg.js%00.txt",
])
def test_mathjax_directory_does_not_serve_anything_else(web_port, path):
    """入れ子の配信は、同梱ディレクトリの中の .js だけ。外へ出る名前・.js 以外は 404。"""
    status, _, body = _get(web_port, path)
    assert status == 404, path
    assert b"function" not in body
def test_math_assets_are_guarded_by_host_check(web_port):
    """フレームも、入れ子の配信も、他の静的ファイルと同じ Host 検証を通る。"""
    for path in (FRAME, FONT):
        status, _, _ = _get(web_port, path, headers={"Host": "evil.example"})
        assert status == 403, path
def test_mathjax_path_resolver_stays_inside_the_directory():
    root = MATHJAX.resolve()
    ok = webserver._mathjax_path(["tex-svg.js"])
    assert ok == root / "tex-svg.js"
    for bad in (["..", "app.js"], ["fonts", "..", "..", "app.js"], ["a/b.js"],
                ["%2e%2e", "app.js"], [".x.js"], ["tex-svg.txt"], ["a\\b.js"]):
        with pytest.raises(KeyError):
            webserver._mathjax_path(bad)
def test_vendored_mathjax_matches_recorded_hashes():
    """同梱した MathJax の全ファイルが、出所の記録（SHA256SUMS）と一致し、
    記録に無いファイルが紛れていない。"""
    sums = {}
    for line in (MATHJAX / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        sums[name] = digest
    assert len(sums) >= 80
    files = {p.relative_to(MATHJAX).as_posix()
             for p in MATHJAX.rglob("*.js")}
    assert files == set(sums)
    for name, digest in sums.items():
        assert hashlib.sha256((MATHJAX / name).read_bytes()).hexdigest() == digest, name
    note = (MATHJAX / "VENDOR.txt").read_text(encoding="utf-8")
    version = re.search(r"版\s*:\s*(\d+\.\d+\.\d+)", note).group(1)
    assert f"mathjax-{version}.tgz" in note
    assert f"mathjax-newcm-font-{version}.tgz" in note
    assert sums["tex-svg.js"] in note
    # 同梱の本体が、記録した版のもの
    assert f'"{version}"' in (MATHJAX / "tex-svg.js").read_text(encoding="utf-8")
    assert (MATHJAX / "LICENSE.txt").read_text(encoding="utf-8").lstrip().startswith(
        "Apache License"
    )
def test_math_font_directory_matches_the_configured_path():
    """math-config.js の svg.fontPath が指す場所に、追加の字形がある。"""
    config = (STATIC / "math-config.js").read_text(encoding="utf-8")
    assert "fontPath: '/static/mathjax/fonts/%%FONT%%-font'" in config
    dynamic = MATHJAX / "fonts" / "mathjax-newcm-font" / "svg" / "dynamic"
    assert (dynamic / "double-struck.js").is_file()
    assert (dynamic / "calligraphic.js").is_file()
def test_self_test_covers_math_assets():
    """入れ替え時の自己テストが、数式の同梱ファイルが返ることまで確かめる。"""
    webserver.self_test()  # 返らない・404 なら RuntimeError
    for name in ("math-view.js", "math-frame.html", "math-config.js",
                 "math-frame.js"):
        assert (Path(STATIC) / name).is_file()
    assert (MATHJAX / "tex-svg.js").is_file()

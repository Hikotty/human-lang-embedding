"""ボタンのアイコン（Material Symbols を写した icons.js）の出所と配信のテスト。"""
from __future__ import annotations
import hashlib
import json
import re
import urllib.request
from hle_mcp import versioning, webserver
STATIC = versioning.STATIC_DIR
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 -960 960 960" '
       'width="24"><path d="{}"/></svg>')
def _paths() -> dict[str, str]:
    src = (STATIC / "icons.js").read_text(encoding="utf-8")
    body = re.search(r"const PATHS = \{(.*?)\n  \};", src, re.S).group(1)
    return json.loads("{" + body.rstrip().rstrip(",") + "}")
def _recorded() -> dict[str, str]:
    note = (STATIC / "icons.VENDOR.txt").read_text(encoding="utf-8")
    return dict(re.findall(r"^  ([a-z_]+)\s+([0-9a-f]{64})$", note, re.M))
def test_icon_paths_match_the_recorded_material_symbols():
    """icons.js の path は、記録した SHA-256 の SVG（同じコミットの公式の素材）から
    写したもの。記録に無いアイコンは無い。"""
    paths = _paths()
    recorded = _recorded()
    assert set(paths) == set(recorded)
    assert len(paths) >= 20
    for name, d in paths.items():
        assert hashlib.sha256(SVG.format(d).encode("utf-8")).hexdigest() == recorded[name], name
    note = (STATIC / "icons.VENDOR.txt").read_text(encoding="utf-8")
    assert "10ecc14790612ca8fd164e3f4ffe9c16fc50d5b7" in note
    assert (STATIC / "icons.LICENSE.txt").read_text(encoding="utf-8").lstrip().startswith(
        "Apache License")
def test_path_data_is_plain_svg_path_syntax():
    """path は SVG のパスの記法だけ（innerHTML に組み込んでも属性を抜け出せない）。"""
    for name, d in _paths().items():
        assert re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9 .,\-]+", d), name
def test_every_icon_used_by_the_pages_exists():
    """画面とスクリプトが使うアイコンの名前は、すべて icons.js にある。"""
    names = set(_paths())
    used = set()
    for f in ("index.html", "html-review.html", "admin.html"):
        used |= set(re.findall(r'data-icon="([a-z_]+)"', (STATIC / f).read_text("utf-8")))
    for f in ("app.js", "html-review.js", "admin.js"):
        src = (STATIC / f).read_text("utf-8")
        used |= set(re.findall(r"HleIcons\.(?:html|svg|set)\((?:[a-zA-Z]+, )?'([a-z_]+)'", src))
        used |= set(re.findall(r"'(image|movie|volume_up|expand_less|expand_more)'", src))
    assert used and used <= names, used - names
def test_comment_marker_in_css_uses_the_same_path():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    from urllib.parse import quote
    assert quote(_paths()["comment"], safe="") in css
def test_icons_script_is_served_and_self_tested(web_port):
    with urllib.request.urlopen(f"http://127.0.0.1:{web_port}/static/icons.js") as res:
        assert res.status == 200
        assert res.headers["Content-Type"].startswith("application/javascript")
    webserver.self_test()
    assert "/static/icons.js" in (versioning.PKG_DIR / "webserver.py").read_text("utf-8")

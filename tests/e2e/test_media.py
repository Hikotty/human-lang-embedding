"""動画・音声・GIF の埋め込みの E2E テスト。

本文の ![説明](名前) を拡張子で <img> / <video> / <audio> に描き分け、
実際にブラウザが読み込めること（CSP・Content-Type・Range）、段落として
選択・削除指定・コメントでき、校正結果に種類付きで返ることを確かめる。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hle_mcp import feedback, storage

MEDIA_DIR = Path(__file__).parent.parent / "fixtures" / "media"
BASE = ("説明の段落です。\n![動き](anim.gif)\n![録画](clip.webm)\n"
        "![声](tone.mp3)\n![音](tone.wav)\n![](tone.ogg)\n最後の段落です。")


def _media_bytes(name: str) -> bytes:
    return (MEDIA_DIR / name).read_bytes()


def _collect_errors(page) -> list[str]:
    """コンソールのエラー（CSP違反・読み込み失敗）を集める。

    Google Chrome はページごとに /favicon.ico を取りに来て 404 を出すので除く
    （同梱の Chromium は取りに来ない）。"""
    errors: list[str] = []

    def on_console(m):
        if m.type != "error":
            return
        if (m.location or {}).get("url", "").endswith("/favicon.ico"):
            return
        errors.append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def _wait_media_ready(page, selector: str) -> None:
    """動画・音声の長さが分かる（HAVE_METADATA 以上）まで待つ。"""
    page.wait_for_function(
        "sel => [...document.querySelectorAll(sel)].every("
        "el => el.readyState >= 1 && el.duration > 0)",
        arg=selector, timeout=10000)


def wait_saved(page):
    expect(page.locator("#save-state")).to_have_text("保存済み", timeout=5000)


@pytest.fixture()
def media_review(browser, web_port):
    """GIF・動画・音声入りの本文を開いた (page, sid, errors, responses)。"""
    s = storage.create_session("仮", "メディアE2E", "/tmp/e2e")
    for name in ("anim.gif", "clip.webm", "tone.mp3", "tone.wav", "tone.ogg"):
        assert storage.save_image_bytes(s["id"], name, _media_bytes(name)) == name
    storage.update_base_text(s["id"], BASE)
    context = browser.new_context()
    page = context.new_page()
    errors = _collect_errors(page)
    responses: list[tuple[str, int, str]] = []
    page.on("response", lambda r: responses.append(
        (r.url, r.status, r.request.headers.get("range", ""))))
    page.goto(f"http://127.0.0.1:{web_port}/session/{s['id']}")
    page.wait_for_selector("#editor .img-para")
    yield page, s["id"], errors, responses
    context.close()


def test_media_rendered_by_kind_and_loaded(media_review):
    """GIF は画像、webm は動画プレーヤー、mp3/wav は音声プレーヤーになり、
    どれも実際に読み込まれる（CSP の media-src と Content-Type が正しい）。"""
    page, sid, errors, _ = media_review
    paras = page.locator("#editor .img-para")
    expect(paras).to_have_count(5)
    assert paras.evaluate_all("ps => ps.map(p => [p.dataset.kind, "
                              "p.firstElementChild.tagName])") == [
        ["image", "IMG"], ["video", "VIDEO"], ["audio", "AUDIO"],
        ["audio", "AUDIO"], ["audio", "AUDIO"]]
    gif = page.locator('#editor .img-para[data-img="anim.gif"] img')
    page.wait_for_function(
        "() => document.querySelector('#editor img').naturalWidth > 0")
    assert gif.evaluate("el => [el.naturalWidth, el.naturalHeight]") == [32, 24]
    _wait_media_ready(page, "#editor video, #editor audio")
    video = page.locator("#editor video")
    assert video.evaluate("v => [v.controls, v.videoWidth, v.videoHeight, "
                          "Math.round(v.duration)]") == [True, 64, 48, 3]
    # Ogg（Opus）は Range に応えないサーバーだと長さが Infinity になる。
    # 末尾を Range で読めるので、ここでは長さが分かる
    page.wait_for_function(
        "() => [...document.querySelectorAll('#editor audio')]"
        ".every(a => Number.isFinite(a.duration))", timeout=10000)
    durations = page.locator("#editor audio").evaluate_all(
        "as => as.map(a => [a.controls, Math.round(a.duration)])")
    assert durations == [[True, 2], [True, 1], [True, 2]]
    expect(page.locator('#editor .img-para[data-img="clip.webm"] .img-caption')
           ).to_have_text("clip.webm")
    # 開いただけでは本文は変わらない
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["changes"] == []
    assert errors == []


def test_video_is_seekable_through_range_requests(media_review):
    """サーバーが Range に 206 で応えるので、ブラウザは動画をシークできる。

    Range に応えないサーバーだと、Chrome は seekable を [0, 0] にして
    途中へ飛べない（修正前の配信で確認した挙動）。"""
    page, sid, errors, responses = media_review
    _wait_media_ready(page, "#editor video")
    seekable = page.locator("#editor video").evaluate(
        "v => [v.seekable.length, v.seekable.length ? v.seekable.end(0) : 0]")
    assert seekable[0] == 1 and seekable[1] == pytest.approx(3, abs=0.2)
    landed = page.locator("#editor video").evaluate(
        """v => new Promise((resolve) => {
            v.addEventListener('seeked', () => resolve(v.currentTime),
                               { once: true });
            v.currentTime = 2.5;
        })""")
    assert landed == pytest.approx(2.5, abs=0.2)
    media = [r for r in responses if r[0].endswith("/files/clip.webm")]
    assert media and all(st == 206 for _, st, _ in media), media
    assert all(rng.startswith("bytes=") for _, _, rng in media)
    assert errors == []


def test_click_player_selects_paragraph_without_typing(media_review):
    """プレーヤーのクリックで段落が選ばれ、続けて押した Space は本文への
    入力にならない（キャレットを隣の段落へ移さない）。Backspace で削除指定。"""
    page, sid, errors, _ = media_review
    _wait_media_ready(page, "#editor video")
    para = page.locator('#editor .img-para[data-img="clip.webm"]')
    para.locator("video").click()
    expect(para).to_have_class("img-para sel")
    assert page.evaluate("document.activeElement.tagName") == "VIDEO"
    page.keyboard.press("Space")
    page.keyboard.type("x")
    expect(page.locator("#editor ins")).to_have_count(0)
    page.keyboard.press("Backspace")
    assert para.get_attribute("data-del") == "1"
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["changes"] == [{"type": "image_delete", "paragraph": 2,
                              "text": "clip.webm", "kind": "video"}]
    assert "【動画削除:clip.webm】" in fb["annotated_text"]
    assert "![録画](clip.webm)" not in fb["result_text"]
    assert errors == []


def test_audio_controls_select_paragraph(media_review):
    """音声プレーヤーの操作部（再生ボタン）を押しても段落が選ばれる。

    Chrome の標準プレーヤーは操作部の上の pointerdown / click を本文側へ
    流さないので、フォーカスの移動で選ぶ。再生はそのまま始まり、Backspace
    で削除指定できる。"""
    page, sid, errors, _ = media_review
    _wait_media_ready(page, "#editor audio")
    para = page.locator('#editor .img-para[data-img="tone.mp3"]')
    para.locator("audio").click(position={"x": 15, "y": 15})  # 再生ボタン
    expect(para).to_have_class("img-para sel")
    page.wait_for_function(
        "() => !document.querySelector('#editor audio').paused")
    page.keyboard.press("Backspace")
    assert para.get_attribute("data-del") == "1"
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert "【音声削除:tone.mp3】" in fb["annotated_text"]
    assert errors == []


def test_comment_on_audio_player(media_review):
    page, sid, errors, _ = media_review
    _wait_media_ready(page, "#editor audio")
    page.locator('#editor .img-para[data-img="tone.mp3"] audio').click()
    page.click("#btn-comment")
    expect(page.locator("#draft-quote")).to_have_text("🔊 tone.mp3")
    page.fill("#draft-text", "ナレーションの声が小さい")
    page.click("#draft-submit")
    card = page.locator(".comment-card")
    expect(card).to_have_count(1)
    expect(card.locator(".media-icon")).to_have_text("🔊")
    para = page.locator('#editor .img-para[data-img="tone.mp3"]')
    assert para.get_attribute("data-cids")
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    c = fb["comments"][0]
    assert (c["target"], c["image"], c["kind"]) == ("image", "tone.mp3", "audio")
    assert "〖C1〗![声](tone.mp3)〖/C1〗" in fb["annotated_text"]
    # カードを押すと本文のプレーヤーの段落へ戻れる
    card.locator(".quote").click()
    expect(para).to_have_class(re.compile(r"\bcmt-active\b"))
    assert errors == []


def _caret_at_end_of_first_paragraph(page):
    page.evaluate(
        """() => {
            const node = document.querySelector('#editor p').firstChild;
            const r = document.createRange();
            r.setStart(node, node.length); r.collapse(true);
            const sel = getSelection();
            sel.removeAllRanges(); sel.addRange(r);
        }""")


def test_add_video_and_audio_via_button(media_review, tmp_path):
    """🖼 メディアのボタンで動画と音声をまとめて追加すると、選んだ順に
    キャレットの段落の後ろへ並び、そのまま再生できる。"""
    page, sid, errors, _ = media_review
    _caret_at_end_of_first_paragraph(page)
    clip = tmp_path / "画面収録.webm"
    clip.write_bytes(_media_bytes("clip.webm"))
    voice = tmp_path / "voice.ogg"
    voice.write_bytes(_media_bytes("tone.ogg"))
    page.set_input_files("#image-input", [str(clip), str(voice)])
    added = page.locator('#editor .img-para[data-ins="1"]')
    expect(added).to_have_count(2, timeout=10000)
    assert added.evaluate_all("ps => ps.map(p => [p.dataset.img, "
                              "p.dataset.kind])") == [
        ["画面収録.webm", "video"], ["voice.ogg", "audio"]]
    _wait_media_ready(page, '#editor .img-para[data-ins="1"] video')
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["result_text"].startswith(
        "説明の段落です。\n![](画面収録.webm)\n![](voice.ogg)\n![動き](anim.gif)")
    assert "【動画追加:画面収録.webm】" in fb["annotated_text"]
    assert "【音声追加:voice.ogg】" in fb["annotated_text"]
    assert storage.get_image_path(sid, "画面収録.webm").read_bytes() \
        == _media_bytes("clip.webm")
    assert errors == []


_DROP_JS = """([b64, name, type, sel]) => {
    const bin = atob(b64);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    const dt = new DataTransfer();
    dt.items.add(new File([u8], name, { type: type }));
    const target = document.querySelector(sel);
    target.scrollIntoView({ block: 'center' });
    const r = target.getBoundingClientRect();
    const opts = { dataTransfer: dt, clientX: r.left + 10,
                   clientY: r.top + r.height / 2, bubbles: true,
                   cancelable: true };
    const over = !target.dispatchEvent(new DragEvent('dragover', opts));
    const drop = !target.dispatchEvent(new DragEvent('drop', opts));
    return [over, drop];
}"""


def _b64(name: str) -> str:
    return base64.b64encode(_media_bytes(name)).decode()


def test_drop_media_file_onto_paragraph(media_review):
    """本文にファイルをドロップすると、落とした段落の後ろに添付される。
    本文の外へのドロップはページを離れず（既定の動作を止め）、何も添付しない。"""
    page, sid, errors, _ = media_review
    url = page.url
    handled = page.evaluate(_DROP_JS, [
        _b64("tone.wav"), "drop.wav", "audio/wav", "#editor > p:last-child"])
    assert handled == [True, True]
    added = page.locator('#editor .img-para[data-ins="1"]')
    expect(added).to_have_count(1, timeout=10000)
    # 最後の段落の後ろ（末尾）に入る
    assert page.locator("#editor > p").last.get_attribute("data-img") == "drop.wav"
    handled = page.evaluate(_DROP_JS, [
        _b64("tone.wav"), "outside.wav", "audio/wav", "#sidebar"])
    assert handled == [True, True]
    page.wait_for_timeout(300)
    assert page.url == url
    expect(added).to_have_count(1)
    wait_saved(page)
    fb = feedback.build_feedback(storage.load_session(sid))
    assert fb["result_text"].endswith("最後の段落です。\n![](drop.wav)")
    assert storage.list_images(sid) == sorted(
        ["anim.gif", "clip.webm", "drop.wav", "tone.mp3", "tone.ogg", "tone.wav"])
    assert errors == []


def test_paste_video_file(media_review):
    """動画ファイルの貼り付けも添付になる（キャレットの段落の後ろ）。"""
    page, sid, errors, _ = media_review
    _caret_at_end_of_first_paragraph(page)
    page.evaluate(
        """([b64]) => {
            const bin = atob(b64);
            const u8 = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
            const dt = new DataTransfer();
            dt.items.add(new File([u8], 'pasted.webm', { type: 'video/webm' }));
            const ed = document.getElementById('editor');
            ed.focus();
            ed.dispatchEvent(new InputEvent('beforeinput', {
                inputType: 'insertFromPaste', dataTransfer: dt,
                bubbles: true, cancelable: true }));
        }""", [_b64("clip.webm")])
    added = page.locator('#editor .img-para[data-ins="1"]')
    expect(added).to_have_count(1, timeout=10000)
    assert page.locator("#editor > p").nth(1).get_attribute("data-img") \
        == "pasted.webm"
    _wait_media_ready(page, '#editor .img-para[data-ins="1"] video')
    expect(page.locator("#editor ins")).to_have_count(0)  # 文字は入らない
    assert errors == []


def test_unsupported_file_is_refused_without_upload(media_review, tmp_path):
    page, sid, _, responses = media_review
    doc = tmp_path / "資料.pdf"
    doc.write_bytes(b"%PDF-1.7")
    messages: list[str] = []
    page.once("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.set_input_files("#image-input", str(doc))
    page.wait_for_function("() => true")
    expect(page.locator('#editor .img-para[data-ins="1"]')).to_have_count(0)
    page.wait_for_timeout(300)
    assert messages and "対応していない形式" in messages[0]
    assert ".mp4" in messages[0] and ".mp3" in messages[0]
    assert not [r for r in responses if "/files?name=" in r[0]]


def test_too_large_file_is_refused_before_sending(media_review, tmp_path,
                                                 monkeypatch):
    """上限はサーバーから受け取り、超えるファイルは送り始める前に断る。"""
    page, sid, _, responses = media_review
    monkeypatch.setitem(storage.MAX_MEDIA_BYTES, "video", 1000)
    page.reload()
    page.wait_for_selector("#editor .img-para")
    clip = tmp_path / "long.webm"
    clip.write_bytes(_media_bytes("clip.webm"))  # 約11KB
    messages: list[str] = []
    page.once("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.set_input_files("#image-input", str(clip))
    page.wait_for_timeout(500)
    assert messages and "動画は" in messages[0] and "long.webm" in messages[0]
    expect(page.locator('#editor .img-para[data-ins="1"]')).to_have_count(0)
    assert not [r for r in responses if "/files?name=" in r[0]]


def test_file_input_accepts_every_supported_extension(media_review):
    page, _, _, _ = media_review
    accept = page.locator("#image-input").get_attribute("accept") or ""
    assert set(accept.split(",")) == set(storage.MEDIA_TYPES)
    assert page.locator("#image-input").get_attribute("multiple") is not None
    expect(page.locator("#btn-image")).to_have_text("🖼 メディア")


def test_admin_shows_media_badges_by_kind(browser, web_port):
    s = storage.create_session("前\n![](clip.webm)\n![](a.mp3)", "管理", "/tmp/e2e")
    storage.set_document(
        s["id"],
        "<p>前</p>"
        '<p class="img-para" data-img="clip.webm" data-md="![](clip.webm)" '
        'data-del="1"></p>'
        '<p class="img-para" data-img="new.mp3" data-md="![](new.mp3)" '
        'data-ins="1"></p>')
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/admin")
    expect(page.locator(".admin-card")).to_have_count(1)
    page.click("button[data-toggle]")
    badges = page.locator(".img-badge")
    expect(badges).to_have_text(["動画削除: clip.webm", "音声追加: new.mp3"])
    context.close()


# ---------- HTMLレビュー（data: URI の動画・音声） ----------

def test_html_review_plays_and_comments_on_data_uri_media(browser, web_port):
    """HTMLレビューでも data: の動画・音声が読み込まれ（iframe の CSP の
    media-src）、要素そのものにコメントできる（audio も対象）。"""
    video = "data:video/webm;base64," + _b64("clip.webm")
    audio = "data:audio/wav;base64," + _b64("tone.wav")
    html = ("<!doctype html><html><body><h1 id='lead'>デモ</h1>"
            f"<video controls title='操作デモ' src='{video}'></video>"
            f"<p>説明の文章</p><audio controls src='{audio}'></audio>"
            "</body></html>")
    s = storage.create_session(html, "HTMLメディア", "/tmp/e2e", mode="html",
                               source_path="/tmp/e2e/page.html")
    context = browser.new_context()
    page = context.new_page()
    errors = _collect_errors(page)
    page.goto(f"http://127.0.0.1:{web_port}/session/{s['id']}")
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            const d = f && f.contentDocument;
            const els = d ? [...d.querySelectorAll('video, audio')] : [];
            return els.length === 2 &&
                   els.every((el) => el.readyState >= 1 && el.duration > 0);
        }""", timeout=10000)
    frame = page.frame_locator("#doc-frame")
    for selector, label, ref in (("audio", "🔊 音声（埋め込み）", "audio:0"),
                                 ("video", "🎬 操作デモ", "video:0")):
        frame.locator(selector).click()
        page.click("#btn-comment")
        expect(page.locator("#draft-quote")).to_have_text(label)
        page.fill("#draft-text", f"{selector}への指摘")
        page.click("#draft-submit")
        expect(page.locator("#draft-text")).to_be_hidden()
    comments = storage.load_session(s["id"])["comments"]
    assert [(c["target"], c["image_ref"], c["text"]) for c in comments] == [
        ("image", "audio:0", "audioへの指摘"),
        ("image", "video:0", "videoへの指摘")]
    assert errors == []
    context.close()


# ---------- Google Chrome（H.264 / AAC の再生） ----------
# Playwright 同梱の Chromium と Google Chrome のどちらでも mp4 / mov / m4a は
# 読めるが、利用者が実際に開くのは Chrome なので、入っていればそちらでも確かめる

@pytest.fixture()
def chrome(playwright):
    try:
        b = playwright.chromium.launch(channel="chrome")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Google Chrome を起動できない: {e}")
    yield b
    b.close()


def test_chrome_plays_mp4_mov_and_m4a(chrome, web_port):
    s = storage.create_session("仮", "Chrome再生", "/tmp/e2e")
    for name in ("clip.mp4", "clip.mov", "tone.m4a", "tone.mp3"):
        storage.save_image_bytes(s["id"], name, _media_bytes(name))
    storage.update_base_text(
        s["id"], "本文\n![](clip.mp4)\n![](clip.mov)\n![](tone.m4a)\n![](tone.mp3)")
    page = chrome.new_page()
    errors = _collect_errors(page)
    page.goto(f"http://127.0.0.1:{web_port}/session/{s['id']}")
    page.wait_for_selector("#editor .img-para")
    _wait_media_ready(page, "#editor video, #editor audio")
    got = page.locator("#editor video, #editor audio").evaluate_all(
        "els => els.map(el => [el.tagName, el.videoWidth || 0, "
        "Math.round(el.duration)])")
    assert got == [["VIDEO", 64, 3], ["VIDEO", 64, 3], ["AUDIO", 0, 2],
                   ["AUDIO", 0, 2]]
    # 再生も始まる（mov も video/quicktime のまま再生できる）
    for i in range(2):
        assert page.locator("#editor video").nth(i).evaluate(
            "v => { v.muted = true; return v.play().then(() => 'ok'); }") == "ok"
        page.wait_for_function(
            "i => document.querySelectorAll('#editor video')[i].currentTime > 0.2",
            arg=i, timeout=5000)
    assert errors == []
    page.close()

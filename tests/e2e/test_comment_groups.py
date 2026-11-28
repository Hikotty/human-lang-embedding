"""コメント一覧の区分（この稿・過去の稿・解決済み）の E2E テスト。

未解決のコメントは、今の稿へのもの（この稿）と前の稿へのもの（過去の稿）に
分かれ、解決済みはそのどちらとも別の区分にまとまる。解決済みは既定で畳まれ、
見出しのボタンで開く。開いた解決済みのカードは読むだけで、未解決に戻せ、
押すと本文の場所へ飛ぶ。区分が「この稿」だけのときは見出しを出さない。
"""

from __future__ import annotations

from playwright.sync_api import expect

from hle_mcp import storage


def _open(browser, web_port, sid):
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{web_port}/session/{sid}")
    return context, page


def _group(page, name):
    return page.locator(f'.cmt-group[data-group="{name}"]')


def _card_ids(page, name):
    return _group(page, name).locator(".comment-card").evaluate_all(
        "els => els.map(e => e.dataset.cid)")


# ---------- md エディタ ----------

def _make_md_session():
    """第1稿に c1（未解決）と c2（AIが解決）、第2稿に c3（未解決）と
    c4（人間が解決。本文に span が残る）を持つセッション。"""
    sid = storage.create_session("赤い空と青い海。", "区分E2E", "/tmp/e2e")["id"]
    storage.add_comment(sid, "赤", "一つ目", comment_id="c1")
    storage.add_comment(sid, "青", "二つ目", comment_id="c2")
    storage.add_reply(sid, "c2", "直しました", author="ai")
    storage.set_document(
        sid,
        '<p><span class="cmt" data-cid="c1">赤</span>い空と'
        '<span class="cmt" data-cid="c2">青</span>い海。</p>')
    storage.add_revision(sid, "赤い空と青い海と白い雲。")
    storage.add_comment(sid, "白", "三つ目", comment_id="c3")
    storage.add_comment(sid, "雲", "四つ目", comment_id="c4")
    storage.set_document(
        sid,
        '<p>赤い空と青い海と<span class="cmt" data-cid="c3">白</span>い'
        '<span class="cmt" data-cid="c4">雲</span>。</p>')
    storage.set_comment_resolved(sid, "c2", True, by="ai")
    storage.set_comment_resolved(sid, "c4", True, by="human")
    return sid


def test_md_lists_current_past_and_resolved_separately(browser, web_port):
    sid = _make_md_session()
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_selector("#editor .cmt[data-cid]")
        # 区分の並びと見出し（名前と件数）
        groups = page.locator(".cmt-group").evaluate_all(
            "els => els.map(e => e.dataset.group)")
        assert groups == ["current", "past", "resolved"]
        for name, title, count in (("current", "この稿", "1"),
                                   ("past", "過去の稿", "1"),
                                   ("resolved", "解決済み", "2")):
            expect(_group(page, name).locator(".cmt-group-title")).to_have_text(title)
            expect(_group(page, name).locator(".cmt-group-count")).to_have_text(count)
        assert _card_ids(page, "current") == ["c3"]
        assert _card_ids(page, "past") == ["c1"]
        # 解決済みは既定で畳まれ、カードは出ない
        assert _card_ids(page, "resolved") == []
        toggle = page.get_by_role("button", name="解決済みのコメントを表示")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        # 過去の稿のカードは、どの稿への指摘かを示す
        expect(page.locator('.comment-card[data-cid="c1"] .rev-note')).to_have_text(
            "第1稿への指摘")

        # 開くと解決済みのカードが出る。読むだけで、返信・編集・削除は無い
        toggle.click()
        expect(page.get_by_role("button", name="解決済みのコメントを隠す")) \
            .to_have_attribute("aria-expanded", "true")
        assert _card_ids(page, "resolved") == ["c2", "c4"]
        c2 = page.locator('.comment-card[data-cid="c2"]')
        expect(c2).to_have_class("comment-card resolved")
        expect(c2.locator(".resolved-note")).to_contain_text("AIが解決")
        expect(c2).to_contain_text("直しました")
        expect(c2.locator(".quote")).to_contain_text("C2:")
        expect(page.locator('.comment-card[data-cid="c4"] .resolved-note')) \
            .to_contain_text("あなたが解決")
        resolved = _group(page, "resolved")
        expect(resolved.locator("textarea")).to_have_count(0)
        expect(resolved.get_by_role("button", name="編集")).to_have_count(0)
        expect(resolved.get_by_role("button", name="削除")).to_have_count(0)
        expect(resolved.get_by_role("button", name="未解決に戻す")).to_have_count(2)

        # 解決済みのカードを押すと、隠れたハイライトの場所が一時的に強調される
        page.locator('.comment-card[data-cid="c4"] .quote').click()
        page.wait_for_function(
            "() => { const h = CSS.highlights.get('hle-jump');"
            " return !!h && [...h].map(r => r.toString()).join('') === '雲'; }")
        # 本文のハイライトは消えたまま
        assert page.locator('#editor .cmt[data-cid="c4"]').evaluate(
            "el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"

        # 未解決に戻すと、元の区分（第1稿への指摘なので過去の稿）へ戻る
        c2.hover()
        c2.get_by_role("button", name="未解決に戻す").click()
        expect(_group(page, "past").locator(".cmt-group-count")).to_have_text("2")
        assert _card_ids(page, "past") == ["c1", "c2"]
        assert _card_ids(page, "resolved") == ["c4"]
        assert "resolved_at" not in storage.load_session(sid)["comments"][1]
        # 戻したカードは、また返信できる
        expect(page.locator('.comment-card[data-cid="c2"] textarea[data-reply]')) \
            .to_have_count(1)

        # この稿のコメントを解決すると、開いている解決済みの区分へ移る
        c3 = page.locator('.comment-card[data-cid="c3"]')
        c3.hover()
        c3.get_by_role("button", name="解決済みにする").click()
        expect(_group(page, "current")).to_have_count(0)
        assert _card_ids(page, "resolved") == ["c3", "c4"]

        # 再読み込みすると、解決済みは畳んだ状態に戻る
        page.reload()
        page.wait_for_selector("#editor .cmt[data-cid]")
        expect(_group(page, "resolved").locator(".cmt-group-count")).to_have_text("2")
        assert _card_ids(page, "resolved") == []
        assert _card_ids(page, "past") == ["c1", "c2"]
    finally:
        context.close()


def test_md_no_headings_when_only_current(browser, web_port):
    """区分が「この稿」だけなら、これまでどおり見出しを出さない。"""
    sid = storage.create_session("赤い空。", "区分なし", "/tmp/e2e")["id"]
    storage.add_comment(sid, "赤", "一つ目", comment_id="c1")
    storage.set_document(sid, '<p><span class="cmt" data-cid="c1">赤</span>い空。</p>')
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_selector("#editor .cmt[data-cid]")
        expect(page.locator(".comment-card")).to_have_count(1)
        expect(page.locator(".cmt-group-head")).to_have_count(0)
        # 解決すると、未解決が無い旨と、解決済みの区分（畳んだ状態）が出る
        page.locator(".comment-card").hover()
        page.get_by_role("button", name="解決済みにする").click()
        expect(page.locator("#comment-list .empty-note")).to_have_text(
            "未解決のコメントはありません。")
        expect(_group(page, "resolved").locator(".cmt-group-count")).to_have_text("1")
        expect(page.locator(".comment-card")).to_have_count(0)
    finally:
        context.close()


def test_md_author_filter_applies_to_every_group(browser, web_port):
    sid = _make_md_session()
    storage.add_comment(sid, "白", "AIの問い", author="ai", comment_id="a1",
                        tag="question")
    context, page = _open(browser, web_port, sid)
    try:
        page.wait_for_selector("#editor .cmt[data-cid]")
        page.check('input[name="comment-filter"][value="ai"]')
        # AI のコメントは今の稿の a1 だけ。区分が1つなので見出しは無い
        assert _card_ids(page, "current") == ["a1"]
        expect(page.locator(".cmt-group-head")).to_have_count(0)
        page.check('input[name="comment-filter"][value="human"]')
        assert _card_ids(page, "current") == ["c3"]
        expect(_group(page, "resolved").locator(".cmt-group-count")).to_have_text("2")
    finally:
        context.close()


# ---------- HTMLレビュー ----------
HTML_V1 = ("<!doctype html><html><body><h1>見出し</h1>"
           "<p>これは重要な説明文です。</p></body></html>")
HTML_V2 = ("<!doctype html><html><body><h1>見出し</h1>"
           "<p>これは重要な説明文です。</p>"
           "<p>これは補足の説明文です。</p></body></html>")


def _wait_frame(page):
    page.wait_for_function(
        """() => {
            const f = document.getElementById('doc-frame');
            return !!(f && f.contentDocument &&
                      f.contentDocument.querySelector('h1'));
        }""")


def _highlight_texts(page, name="hle-comment"):
    return page.evaluate(
        """(name) => {
            const win = document.getElementById('doc-frame').contentWindow;
            const h = win.CSS.highlights.get(name);
            return h ? [...h].map(r => r.toString()).sort() : [];
        }""", name)


def test_html_lists_current_past_and_resolved_separately(browser, web_port):
    sid = storage.create_session(
        HTML_V1, "HTML区分E2E", "/tmp/e2e", mode="html",
        source_path="/tmp/e2e/d.html")["id"]
    storage.add_comment(sid, "重要", "一つ目", comment_id="c1",
                        prefix="これは", suffix="な説明文です")
    storage.add_revision(sid, HTML_V2)
    storage.add_comment(sid, "見出し", "二つ目", comment_id="c2")
    storage.add_comment(sid, "補足", "三つ目", comment_id="c3",
                        prefix="これは", suffix="の説明文です")
    storage.set_comment_resolved(sid, "c3", True, by="ai")
    context, page = _open(browser, web_port, sid)
    try:
        _wait_frame(page)
        page.wait_for_function(
            """() => {
                const win = document.getElementById('doc-frame').contentWindow;
                const h = win && win.CSS.highlights.get('hle-comment');
                return !!(h && h.size === 2);
            }""", timeout=10000)
        assert _highlight_texts(page) == ["見出し", "重要"]
        assert _card_ids(page, "current") == ["c2"]
        assert _card_ids(page, "past") == ["c1"]
        assert _card_ids(page, "resolved") == []
        expect(_group(page, "past").locator(".cmt-group-title")).to_have_text("過去の稿")

        page.get_by_role("button", name="解決済みのコメントを表示").click()
        assert _card_ids(page, "resolved") == ["c3"]
        c3 = page.locator('.comment-card[data-cid="c3"]')
        expect(c3.locator(".resolved-note")).to_contain_text("AIが解決")
        # 押すと、ハイライトを描いていない場所でも元の位置を強調する
        c3.locator(".quote").click()
        page.wait_for_function(
            """() => {
                const win = document.getElementById('doc-frame').contentWindow;
                const h = win.CSS.highlights.get('hle-jump');
                return !!h && [...h].map(r => r.toString()).join('') === '補足';
            }""")
        assert _highlight_texts(page) == ["見出し", "重要"]

        # 未解決に戻すと今の稿の区分へ戻り、ハイライトも描く
        c3.hover()
        c3.get_by_role("button", name="未解決に戻す").click()
        expect(_group(page, "resolved")).to_have_count(0)
        assert _card_ids(page, "current") == ["c2", "c3"]
        # 押した c3 は選んだまま（強調の色）で描く
        assert _highlight_texts(page) == ["見出し", "重要"]
        assert _highlight_texts(page, "hle-active") == ["補足"]
    finally:
        context.close()


# ---------- 管理画面 ----------

def test_admin_lists_resolved_comments_separately(browser, web_port):
    sid = _make_md_session()
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"http://127.0.0.1:{web_port}/admin")
        page.click("button[data-toggle]")
        detail = page.locator(".admin-detail")
        expect(detail.get_by_role("heading", name="コメント (2)", exact=True)) \
            .to_have_count(1)
        expect(detail.get_by_role(
            "heading", name="解決済みのコメント (2)", exact=True)).to_have_count(1)
        resolved = detail.locator(".admin-comment.resolved")
        expect(resolved).to_have_count(2)
        expect(resolved.nth(0)).to_contain_text("C2")
        expect(resolved.nth(0)).to_contain_text("AIが解決")
        expect(resolved.nth(0)).to_contain_text("返信1件")
        expect(resolved.nth(1)).to_contain_text("人間が解決")
        # 未解決の一覧には、解決済みのものは出ない
        expect(detail.locator(".admin-comment:not(.resolved)")).to_have_count(2)
        assert "四つ目" not in detail.locator(
            ".admin-comment:not(.resolved)").all_inner_texts()[1]
        assert sid
    finally:
        context.close()

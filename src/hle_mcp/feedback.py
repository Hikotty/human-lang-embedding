"""編集済みHTMLからフィードバックを抽出するモジュール。

ブラウザのトラックチェンジエディタが生成するHTMLを解析し、
- original_text: 校正前のテキスト（削除部を含み、追加部を除く）
- result_text:   校正後のテキスト（削除部を除き、追加部を含む）
- annotated_text: 削除/追加/コメント位置をマーカーで示した注釈付きテキスト
- changes:       変更点の構造化リスト
を生成する。

original_text / result_text / changes は本文をそのまま保持する厳密な表現で、
annotated_text だけが可読性のためのマーカー付き表現である。マーカー記号が
本文に含まれる場合の曖昧さを避けるため、annotated_text 内の本文由来の
【】〖〗 と \\ はバックスラッシュでエスケープされる。

HTML上の規約:
- 段落は <p>、削除は <del>、追加は <ins>
- 改行の追加/削除マーカーは class="pb" を持つ ins/del（中身は¶で無視）
- コメント範囲は <span class="cmt" data-cid="...">
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

_ZWSP = "\u200b"

# annotated_text のマーカー記号。本文中に同じ記号が現れると削除/追加の境界が
# 曖昧になるため（日本語では【重要】のような表記が普通に出現する）、
# 本文由来の文字はバックスラッシュでエスケープして区別する。可逆な変換であり
# result_text / changes 側は一切加工しない。
_MARKER_CHARS = "【】〖〗"
# 制御文字はマーカー解析や表示を壊すため落とす（タブのみ許可）
_CONTROL_CHARS = {c: None for c in range(0x20) if c != 0x09}
_CONTROL_CHARS[0x7F] = None


def escape_markers(text: str) -> str:
    """本文由来テキストのマーカー記号をエスケープする。

    `\\` を `\\\\` に、`【】〖〗` を `\\【` のように前置エスケープする。
    """
    out: list[str] = []
    for ch in str(text).translate(_CONTROL_CHARS):
        if ch == "\\" or ch in _MARKER_CHARS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


class _Segment:
    __slots__ = ("text", "in_del", "in_ins", "cids")

    def __init__(self, text: str, in_del: bool, in_ins: bool, cids: tuple[str, ...]):
        self.text = text
        self.in_del = in_del
        self.in_ins = in_ins
        self.cids = cids


class _Paragraph:
    __slots__ = ("segments", "pb_ins", "pb_del", "img")

    def __init__(self) -> None:
        self.segments: list[_Segment] = []
        self.pb_ins = False  # 段落末尾に「改行追加」マーカーがある
        self.pb_del = False  # 段落末尾に「改行削除」マーカーがある
        # 画像段落の場合 {"name", "md", "del", "ins", "cids"} を持つ
        self.img: dict[str, Any] | None = None


class _EditParser(HTMLParser):
    """トラックチェンジHTMLを段落ごとのセグメント列に変換する。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[_Paragraph] = []
        self._para: _Paragraph | None = None
        self._del_depth = 0
        self._ins_depth = 0
        self._marker_depth = 0
        self._cid_stack: list[str] = []

    @staticmethod
    def _is_pb(attrs: list[tuple[str, str | None]]) -> bool:
        d = dict(attrs)
        cls = (d.get("class") or "").split()
        return "pb" in cls or "data-pb" in d

    def _ensure_para(self) -> _Paragraph:
        if self._para is None:
            self._para = _Paragraph()
        return self._para

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("p", "div"):
            self._flush_para()
            self._para = _Paragraph()
            d = dict(attrs)
            if d.get("data-img"):
                name = d["data-img"]
                self._para.img = {
                    "name": name,
                    "md": d.get("data-md") or f"![]({name})",
                    "del": d.get("data-del") == "1",
                    "ins": d.get("data-ins") == "1",
                    # 画像は <span class="cmt"> で包めないので、コメントIDは
                    # 段落の data-cids（スペース区切り）で結び付けられている
                    "cids": tuple((d.get("data-cids") or "").split()),
                }
        elif tag in ("del", "ins"):
            if self._is_pb(attrs):
                self._marker_depth += 1
                para = self._ensure_para()
                if tag == "ins":
                    para.pb_ins = True
                else:
                    para.pb_del = True
            elif tag == "del":
                self._del_depth += 1
            else:
                self._ins_depth += 1
        elif tag == "span":
            d = dict(attrs)
            cid = d.get("data-cid")
            self._cid_stack.append(cid or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "div"):
            self._flush_para()
        elif tag in ("del", "ins"):
            if self._marker_depth > 0:
                # マーカー要素の終了を優先的に処理する
                self._marker_depth -= 1
            elif tag == "del" and self._del_depth > 0:
                self._del_depth -= 1
            elif tag == "ins" and self._ins_depth > 0:
                self._ins_depth -= 1
        elif tag == "span" and self._cid_stack:
            self._cid_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._marker_depth > 0:
            return  # ¶などマーカーの中身は無視
        if self._para is not None and self._para.img is not None:
            return  # 画像段落の中身（altテキスト等）は無視
        text = data.replace(_ZWSP, "").replace("\xa0", " ").replace("\n", "")
        if not text:
            return
        para = self._ensure_para()
        cids = tuple(c for c in self._cid_stack if c)
        para.segments.append(
            _Segment(text, self._del_depth > 0, self._ins_depth > 0, cids)
        )

    def _flush_para(self) -> None:
        if self._para is not None:
            self.paragraphs.append(self._para)
            self._para = None

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._flush_para()


def parse_edited_html(html: str) -> list[_Paragraph]:
    parser = _EditParser()
    parser.feed(html)
    parser.close()
    return parser.paragraphs


def _merged_segments(para: _Paragraph) -> list[_Segment]:
    """同一属性の連続セグメントを結合する。"""
    merged: list[_Segment] = []
    for seg in para.segments:
        if (
            merged
            and merged[-1].in_del == seg.in_del
            and merged[-1].in_ins == seg.in_ins
            and merged[-1].cids == seg.cids
        ):
            merged[-1].text += seg.text
        else:
            merged.append(_Segment(seg.text, seg.in_del, seg.in_ins, seg.cids))
    return merged


def _compose_lines(paragraphs: list[_Paragraph], mode: str) -> str:
    """original / result のテキストを段落単位で合成する。

    - mode="original": 追加分を除外し削除分を含める。挿入画像の行は存在しない。
    - mode="result":   削除分を除外し追加分を含める。削除画像の行は消える。
    行が丸ごと消える場合は改行ごと消える。
    """
    lines: list[str] = []
    merge_prev = False  # 直前の行と結合するか（改行マーカーによる）
    for para in paragraphs:
        if para.img is not None:
            skip = para.img["ins"] if mode == "original" else para.img["del"]
            if skip:
                merge_prev = False
                continue
            text = para.img["md"]
            merge_next = False
        elif mode == "original":
            text = "".join(s.text for s in para.segments if not s.in_ins)
            # 人間が挿入した改行(pb_ins)は元テキストには存在しない
            merge_next = para.pb_ins
        else:
            text = "".join(s.text for s in para.segments if not s.in_del)
            # 人間が削除した改行(pb_del)は校正後には存在しない
            merge_next = para.pb_del
        if lines and merge_prev:
            lines[-1] += text
        else:
            lines.append(text)
        merge_prev = merge_next
    return "\n".join(lines)


def compose_original(paragraphs: list[_Paragraph]) -> str:
    """校正前テキストを復元する（追加分を除外、削除分を含める）。"""
    return _compose_lines(paragraphs, "original")


def compose_result(paragraphs: list[_Paragraph]) -> str:
    """校正後テキストを生成する（削除分を除外、追加分を含める）。"""
    return _compose_lines(paragraphs, "result")


def compose_annotated(
    paragraphs: list[_Paragraph], comment_numbers: dict[str, int]
) -> str:
    """削除/追加/コメント位置をマーカーで示した注釈付きテキストを生成する。"""
    lines: list[str] = []
    for para in paragraphs:
        if para.img is not None:
            name = escape_markers(para.img["name"])
            if para.img["del"]:
                body = f"【画像削除:{name}】"
            elif para.img["ins"]:
                body = f"【画像追加:{name}】"
            else:
                body = escape_markers(para.img["md"])
            # 画像そのものに付いたコメントは画像行全体を範囲として示す
            nums = [
                n
                for n in (
                    comment_numbers.get(cid) for cid in para.img.get("cids", ())
                )
                if n
            ]
            opened = "".join(f"〖C{n}〗" for n in nums)
            closed = "".join(f"〖/C{n}〗" for n in reversed(nums))
            lines.append(f"{opened}{body}{closed}")
            continue
        buf: list[str] = []
        open_cids: list[str] = []
        for seg in _merged_segments(para):
            new_cids = list(seg.cids)
            # 閉じるべきコメント範囲
            for cid in reversed([c for c in open_cids if c not in new_cids]):
                n = comment_numbers.get(cid)
                if n:
                    buf.append(f"〖/C{n}〗")
                open_cids.remove(cid)
            # 開くべきコメント範囲
            for cid in [c for c in new_cids if c not in open_cids]:
                n = comment_numbers.get(cid)
                if n:
                    buf.append(f"〖C{n}〗")
                open_cids.append(cid)
            text = escape_markers(seg.text)
            if seg.in_del:
                buf.append(f"【削除:{text}】")
            elif seg.in_ins:
                buf.append(f"【追加:{text}】")
            else:
                buf.append(text)
        for cid in reversed(open_cids):
            n = comment_numbers.get(cid)
            if n:
                buf.append(f"〖/C{n}〗")
        if para.pb_ins:
            buf.append("【改行追加】")
        if para.pb_del:
            buf.append("【改行削除】")
        lines.append("".join(buf))
    return "\n".join(lines)


def compose_changes(paragraphs: list[_Paragraph]) -> list[dict[str, Any]]:
    """変更点の構造化リストを生成する。"""
    changes: list[dict[str, Any]] = []
    for i, para in enumerate(paragraphs):
        if para.img is not None:
            if para.img["del"]:
                changes.append(
                    {"type": "image_delete", "paragraph": i,
                     "text": para.img["name"]}
                )
            elif para.img["ins"]:
                changes.append(
                    {"type": "image_insert", "paragraph": i,
                     "text": para.img["name"]}
                )
            continue
        for seg in _merged_segments(para):
            if seg.in_del and seg.in_ins:
                continue  # 追加後に削除された文字は差し引きゼロ
            if seg.in_del:
                changes.append(
                    {"type": "delete", "paragraph": i, "text": seg.text}
                )
            elif seg.in_ins:
                changes.append(
                    {"type": "insert", "paragraph": i, "text": seg.text}
                )
        if para.pb_ins:
            changes.append({"type": "newline_insert", "paragraph": i, "text": ""})
        if para.pb_del:
            changes.append({"type": "newline_delete", "paragraph": i, "text": ""})
    return changes


def _comment_view(comment: dict[str, Any], number: int) -> dict[str, Any]:
    """コメント1件をAI向けの形に整える。

    target="image" は文字列ではなく画像そのものへの指摘。image が対象の名前
    （mdモードでは添付画像のファイル名）、image_ref はHTMLレビューで対象要素を
    「タグ名:出現順」で指すアンカー。
    """
    view = {
        "number": number,
        "id": comment["id"],
        "revision": comment.get("revision", 0),
        "quote": comment.get("quote", ""),
        "text": comment.get("text", ""),
        "author": comment.get("author", "human"),
        "created_at": comment.get("created_at", ""),
        # 人間が後から書き換えたコメントには updated_at が入る
        "updated_at": comment.get("updated_at", ""),
        "target": comment.get("target", "text"),
        "replies": comment.get("replies", []),
    }
    if view["target"] == "image":
        view["image"] = comment.get("image", "")
        if comment.get("image_ref"):
            view["image_ref"] = comment["image_ref"]
    return view


def build_html_feedback(session: dict[str, Any]) -> dict[str, Any]:
    """HTMLレビューセッションのフィードバック構造を生成する。

    HTML全文は意図的に含めない（AIのコンテキストを圧迫するため）。
    AIは source_path のファイルを直接読み、各コメントの quote（選択された
    レンダリング後テキスト）と prefix / suffix（選択の直前・直後の文脈）で
    該当箇所を特定する。quote はソース上でタグを跨いでいる場合がある。
    """
    rev = session["revisions"][-1]
    comment_numbers = {c["id"]: i for i, c in enumerate(session["comments"], 1)}
    comments = []
    for c in session["comments"]:
        view = _comment_view(c, comment_numbers[c["id"]])
        view["prefix"] = c.get("prefix", "")
        view["suffix"] = c.get("suffix", "")
        comments.append(view)
    return {
        "session_id": session["id"],
        "title": session["title"],
        "workspace": session.get("workspace", ""),
        "status": session["status"],
        "mode": "html",
        "revision_index": rev["index"],
        "source_path": session.get("source_path", ""),
        "comments": comments,
        "note": (
            "HTMLレビューのため本文は含まれない。source_path のファイルを"
            "直接読み、各コメントの quote（選択テキスト）と prefix / suffix"
            "（直前・直後の文脈）で該当箇所を特定して修正すること。"
            "quote はレンダリング後のテキストで、HTMLソース上ではタグを"
            "跨いでいる場合がある。"
            'target="image" のコメントは画像や図そのものへの指摘で、'
            "image_ref が対象要素を「タグ名:出現順（0起点）」で指す"
            "（例 img:2 はソース中3番目の <img>、svg:0 は最初の <svg>）。"
        ),
    }


def build_feedback(session: dict[str, Any]) -> dict[str, Any]:
    """セッション全体からAI向けフィードバック構造を生成する。"""
    if session.get("mode") == "html":
        return build_html_feedback(session)
    rev = session["revisions"][-1]
    base_text = rev["base_text"]
    html = rev.get("edited_html")
    comment_numbers = {c["id"]: i for i, c in enumerate(session["comments"], 1)}
    if html:
        paragraphs = parse_edited_html(html)
        original = compose_original(paragraphs)
        result = compose_result(paragraphs)
        annotated = compose_annotated(paragraphs, comment_numbers)
        changes = compose_changes(paragraphs)
    else:
        original = base_text
        result = base_text
        annotated = escape_markers(base_text)
        changes = []
    comments = [
        _comment_view(c, comment_numbers[c["id"]]) for c in session["comments"]
    ]
    return {
        "session_id": session["id"],
        "title": session["title"],
        "workspace": session.get("workspace", ""),
        "status": session["status"],
        "mode": "md",
        "revision_index": rev["index"],
        "has_edits": bool(html),
        "original_text": original,
        "result_text": result,
        "annotated_text": annotated,
        "changes": changes,
        "comments": comments,
    }

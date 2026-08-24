"""編集済みHTMLからフィードバックを抽出するモジュール。

ブラウザのトラックチェンジエディタが生成するHTMLを解析し、
- original_text: 校正前のテキスト（削除部を含み、追加部を除く）
- result_text:   校正後のテキスト（削除部を除き、追加部を含む）
- annotated_text: 削除/追加/コメント位置をマーカーで示した注釈付きテキスト
- changes:       変更点の構造化リスト
を生成する。

HTML上の規約:
- 段落は <p>、削除は <del>、追加は <ins>
- 改行の追加/削除マーカーは class="pb" を持つ ins/del（中身は¶で無視）
- コメント範囲は <span class="cmt" data-cid="...">
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

_ZWSP = "\u200b"


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
        # 画像段落の場合 {"name", "md", "del", "ins"} を持つ
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
            name = para.img["name"]
            if para.img["del"]:
                lines.append(f"【画像削除:{name}】")
            elif para.img["ins"]:
                lines.append(f"【画像追加:{name}】")
            else:
                lines.append(para.img["md"])
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
            if seg.in_del:
                buf.append(f"【削除:{seg.text}】")
            elif seg.in_ins:
                buf.append(f"【追加:{seg.text}】")
            else:
                buf.append(seg.text)
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


def build_feedback(session: dict[str, Any]) -> dict[str, Any]:
    """セッション全体からAI向けフィードバック構造を生成する。"""
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
        annotated = base_text
        changes = []
    comments = [
        {
            "number": comment_numbers[c["id"]],
            "id": c["id"],
            "revision": c.get("revision", 0),
            "quote": c.get("quote", ""),
            "text": c.get("text", ""),
            "author": c.get("author", "human"),
            "created_at": c.get("created_at", ""),
            "replies": c.get("replies", []),
        }
        for c in session["comments"]
    ]
    return {
        "session_id": session["id"],
        "title": session["title"],
        "workspace": session.get("workspace", ""),
        "status": session["status"],
        "revision_index": rev["index"],
        "has_edits": bool(html),
        "original_text": original,
        "result_text": result,
        "annotated_text": annotated,
        "changes": changes,
        "comments": comments,
    }

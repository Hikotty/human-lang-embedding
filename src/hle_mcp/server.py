"""human-lang-embedding MCPサーバー。

AIが生成した文章を人間がブラウザ上のトラックチェンジエディタで校正し、
そのフィードバック（削除・追加・コメント）をAIが受け取って文章を改善する
ワークフローを提供する。

ワークフロー:
1. AIが open_review で文章を登録 → ブラウザにエディタが開く
2. 人間が編集（削除は赤斜線、追加は赤文字）・コメントを行う
3. 人間が校正完了をAIに伝える → AIが get_feedback で内容を取得
4. AIが修正稿を submit_revision で再提示（複数ラリー可）
5. 必要に応じて reply_comment でコメントスレッドに返信
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser

from mcp.server.mcpserver import MCPServer

from . import feedback, storage, webserver

server = MCPServer(
    name="human-lang-embedding",
    instructions=(
        "AI生成文章の人間校正ツール。open_reviewで文章を提示し、"
        "人間の校正完了後にget_feedbackで削除・追加・コメントを取得して"
        "文章を改善する。修正稿はsubmit_revisionで再提示し、"
        "コメントにはreply_commentで返信できる。"
    ),
)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _port() -> int:
    return int(os.environ.get("HLE_PORT", webserver.DEFAULT_PORT))


def _open_browser(url: str) -> bool:
    if os.environ.get("HLE_NO_BROWSER"):
        return False
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001
        return False


def _attach_images(
    session_id: str, text: str, images: list[str] | None
) -> tuple[str, list[str]]:
    """画像ファイル群をセッションへコピーし、本文の参照を整える。

    - 本文中の ![alt](ファイル名) 参照は保存名に合わせて書き換える
    - 本文で参照されていない画像は末尾に ![](保存名) 行を追記する
    """
    stored: list[str] = []
    for src in images or []:
        original_name = os.path.basename(str(src))
        name = storage.add_image_file(session_id, str(src))
        stored.append(name)
        if name != original_name and f"({original_name})" in text:
            text = text.replace(f"({original_name})", f"({name})")
        if f"({name})" not in text:
            text = (text + "\n" if text else "") + f"![]({name})"
    if stored:
        storage.update_base_text(session_id, text)
    return text, stored


@server.tool()
def open_review(
    text: str, title: str, workspace: str = "", images: list[str] | None = None
) -> str:
    """文章の校正レビューを開始する。

    AIが生成した文章を登録し、人間が校正するためのエディタをブラウザで開く。
    人間はエディタ上で削除（赤い斜線）・追加（赤文字）・コメントを行う。
    校正が終わったら人間がその旨を伝えるので、get_feedback で結果を取得すること。

    画像を含めたい場合は images にローカルの画像ファイルパスを渡し、
    本文中で ![説明](ファイル名.png) の形式の単独行として参照する。
    本文で参照しなかった画像は文末に自動で追記される。

    Args:
        text: 校正対象の文章（プレーンテキスト、改行は段落区切り）
        title: セッションのタイトル（日本語推奨）
        workspace: 文章を生成したワークスペースの絶対パス（検索用、省略可）
        images: 添付する画像ファイルの絶対パスのリスト（png/jpg/jpeg/gif/webp）
    """
    session = storage.create_session(text=text, title=title, workspace=workspace)
    _, stored = _attach_images(session["id"], text, images)
    port = webserver.ensure_server(_port())
    url = webserver.session_url(port, session["id"])
    opened = _open_browser(url)
    return _dumps(
        {
            "session_id": session["id"],
            "title": session["title"],
            "url": url,
            "images": stored,
            "browser_opened": opened,
            "message": (
                "レビューセッションを作成しました。人間の校正完了の連絡を受けたら "
                "get_feedback で結果を取得してください。"
            ),
        }
    )


@server.tool()
def get_feedback(session_id: str) -> str:
    """人間の校正フィードバックを取得する。

    annotated_text には削除が【削除:…】、追加が【追加:…】、改行の増減が
    【改行追加】【改行削除】、コメント範囲が〖C1〗…〖/C1〗の形式で示される。
    comments の number が注釈のコメント番号に対応する。
    これらを踏まえて文章を修正し、submit_revision で再提示すること。

    Args:
        session_id: open_review が返したセッションID
    """
    session = storage.load_session(session_id)
    return _dumps(feedback.build_feedback(session))


@server.tool()
def submit_revision(
    session_id: str,
    text: str,
    open_browser: bool = True,
    images: list[str] | None = None,
) -> str:
    """フィードバックを反映した修正稿を新しいリビジョンとして提示する。

    セッションのステータスは editing に戻り、人間は再びエディタで校正できる
    （複数ラリーを前提とする）。ブラウザを開いて人間に再校正を依頼すること。
    直前のリビジョンの編集HTMLは差分要約に圧縮されて保存される。
    過去に添付済みの画像は同じ ![alt](ファイル名) 参照でそのまま使える。

    Args:
        session_id: セッションID
        text: 修正後の文章（プレーンテキスト）
        open_browser: エディタをブラウザで開くか（既定: True）
        images: 新たに添付する画像ファイルの絶対パスのリスト（省略可）
    """
    session = storage.add_revision(session_id, text)
    _, _stored = _attach_images(session_id, text, images)
    port = webserver.ensure_server(_port())
    url = webserver.session_url(port, session_id)
    opened = _open_browser(url) if open_browser else False
    return _dumps(
        {
            "session_id": session_id,
            "revision_index": session["revisions"][-1]["index"],
            "url": url,
            "browser_opened": opened,
            "message": "修正稿を登録しました。人間の再校正を待ってください。",
        }
    )


@server.tool()
def reply_comment(session_id: str, comment_id: str, message: str) -> str:
    """人間のコメントにAIとして返信する。

    返信はエディタのコメントスレッドに表示される。人間はさらに返信でき、
    多段のやり取りが可能。コメントIDは get_feedback の comments[].id を使う。

    Args:
        session_id: セッションID
        comment_id: 返信先コメントのID
        message: 返信内容
    """
    reply = storage.add_reply(session_id, comment_id, message, author="ai")
    return _dumps({"ok": True, "reply": reply})


@server.tool()
def list_reviews(query: str = "", workspace: str = "", limit: int = 20) -> str:
    """過去のレビューセッションを検索する。

    Args:
        query: セッションID・タイトルの部分一致キーワード（省略可）
        workspace: ワークスペースパスの部分一致（省略可）
        limit: 最大件数（既定: 20）
    """
    sessions = storage.list_sessions(query=query, workspace=workspace, limit=limit)
    return _dumps({"count": len(sessions), "sessions": sessions})


@server.tool()
def get_review(session_id: str) -> str:
    """セッションの詳細（メタ情報・全リビジョン・全コメント）を取得する。

    リビジョンの編集HTMLは含まれない。校正内容の解釈には get_feedback を使うこと。

    Args:
        session_id: セッションID
    """
    session = storage.load_session(session_id)
    revisions = [
        {
            "index": r["index"],
            "created_at": r["created_at"],
            "base_text": r["base_text"],
            "has_edits": bool(r.get("edited_html")),
            "feedback_summary": r.get("feedback_summary"),
        }
        for r in session["revisions"]
    ]
    return _dumps(
        {
            "id": session["id"],
            "title": session["title"],
            "workspace": session.get("workspace", ""),
            "status": session["status"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "revisions": revisions,
            "comments": session["comments"],
        }
    )


def main() -> None:
    print("[hle-mcp] MCPサーバーを起動します", file=sys.stderr)
    server.run("stdio")


if __name__ == "__main__":
    main()

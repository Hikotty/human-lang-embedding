# human-lang-embedding

AIが生成した文章を、人間がブラウザ上の**トラックチェンジ付きエディタ**（Wordの校正機能ライク）で校正し、その結果をAIにフィードバックするためのMCPサーバーです。削除・追加・コメントのラリーを繰り返すことで、AIのライティング精度を段階的に高めることを目的としています。

## ワークフロー

```
AI                        人間
│ open_review(text)        │
│ ────────────────────────▶│ ブラウザにエディタが開く
│                          │ 削除（赤斜線）・追加（赤文字）・コメント
│                          │ 「校正完了」→ チャットでAIに連絡
│ get_feedback(id)         │
│ ◀────────────────────────│ 削除・追加・コメントを構造化データで受領
│ reply_comment(...)       │ コメントに返信（スレッド化）
│ submit_revision(text)    │
│ ────────────────────────▶│ 第2稿を再校正（以降繰り返し）
```

## 主な機能

- **トラックチェンジ**: 人間が文字を消すと赤い斜線、追記すると赤文字で記録。改行の追加・削除も追跡
- **日本語IME対応**: 変換確定した文字列が正しく「追加」として記録される
- **コメント**: 任意の選択範囲にコメントを付与。右サイドバーに表示され、返信・返信への返信が可能。AIも `reply_comment` で参加できる
- **画像**: AIは `images` パラメータで画像を添付でき、本文の `![説明](ファイル名.png)` 行にインライン表示。人間も🖼ボタン/ペーストで画像を追加でき、クリック+Backspaceで削除指定できる（すべて変更として追跡）
- **複数ラリー**: `submit_revision` で修正稿を積み重ねる。過去のリビジョンは差分要約に圧縮して保存
- **検索**: ID・タイトル・ワークスペースパスでセッションを検索
- **管理画面**: 全セッションの校正結果（注釈付きテキスト・コメントスレッド・過去稿の差分）を一覧・閲覧

## セットアップ

```bash
cd /Users/hikotoi/utility/human-lang-embedding
uv sync                              # 依存関係のインストール
uv run playwright install chromium   # E2Eテストを動かす場合のみ
```

### MCP登録（Kiroの例: `~/.kiro/settings/mcp.json`）

```json
{
  "mcpServers": {
    "human-lang-embedding": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory",
               "/Users/hikotoi/utility/human-lang-embedding", "hle-mcp"],
      "autoApprove": ["open_review", "get_feedback", "submit_revision",
                      "reply_comment", "list_reviews", "get_review"]
    }
  }
}
```

## MCPツール一覧

| ツール | 役割 | 主な引数 |
|---|---|---|
| `open_review` | 校正セッションを作成しブラウザでエディタを開く | `text`, `title`, `workspace`, `images` |
| `get_feedback` | 校正結果（削除・追加・コメント）を取得 | `session_id` |
| `submit_revision` | 修正稿を新リビジョンとして提示 | `session_id`, `text`, `open_browser`, `images` |
| `reply_comment` | コメントスレッドにAIとして返信 | `session_id`, `comment_id`, `message` |
| `list_reviews` | セッションを検索 | `query`, `workspace`, `limit` |
| `get_review` | セッション詳細（全リビジョン・全コメント） | `session_id` |

`get_feedback` の `annotated_text` は次の形式で変更点を表します。

```
これは【削除:テスト】【追加:本番】です。【改行追加】
〖C1〗二行目〖/C1〗です。
【画像削除:old.png】
【画像追加:screenshot.png】
```

- `【削除:…】` / `【追加:…】` : 文字の削除・追加
- `【改行追加】` / `【改行削除】` : 段落区切りの増減
- `〖C1〗…〖/C1〗` : コメント番号1の対象範囲（`comments[].number` に対応）
- `【画像削除:名前】` / `【画像追加:名前】` : 画像の削除指定・追加

## 画面

| URL | 内容 |
|---|---|
| `/session/<ID>` | 校正エディタ（`open_review` が自動で開く） |
| `/` | セッション一覧（ID・タイトル・ワークスペースで検索） |
| `/admin` | 管理画面。全セッションの校正結果・コメント・過去稿の差分要約を閲覧 |

### エディタの操作方法

- **削除**: 文字を選択して Backspace / Delete → 赤い斜線が付く（文字は残る）
- **追加**: そのまま入力 → 赤文字で挿入される（日本語IME対応）
- **改行**: Enter で改行追加、段落先頭で Backspace で改行削除（¶マーカー）
- **自分の追記の取り消し**: 自分が追加した文字を消すと痕跡なく消える
- **コメント**: 範囲を選択して「💬 コメント」→ 右サイドバーに表示。返信可
- **画像追加**: 「🖼 画像」ボタンまたは画像のペースト → カーソル位置に挿入
- **画像削除**: 画像をクリックして選択 → Backspace（もう一度で取り消し）
- **元に戻す/やり直す**: Cmd+Z / Cmd+Shift+Z
- **校正完了**: ヘッダーの「✔ 校正完了」を押し、チャットでAIに完了を伝える

## 管理画面の開き方

2通りあります。

1. **各画面から遷移**: エディタ / 一覧のヘッダーにある「管理画面」リンク
2. **アドホックにCLIから**:

```bash
uv run --directory /Users/hikotoi/utility/human-lang-embedding hle-admin
```

MCPサーバー等が既にWebサーバーを起動していればそれを再利用してブラウザを
開いてすぐ終了します。誰も起動していなければ自前でサーバーを立ち上げて
常駐します（Ctrl+C で終了）。

## データ保存と差分保存

データはすべてこのリポジトリの `data/` 配下に保存されます。

```
data/
├── sessions/<セッションID>.json   # セッション本体
└── files/<セッションID>/          # 添付画像
```

セッションJSONは ID・タイトル・ワークスペースパス・ステータス・
リビジョン配列・コメント配列を持ちます。データ増加を緩やかにするため、
**編集中の現行リビジョンだけが全文HTML（`edited_html`）を持ち、
`submit_revision` で確定した過去リビジョンは差分要約
（`feedback_summary`: annotated_text / result_text / changes）に圧縮**され、
全文HTMLは破棄されます。原文は各リビジョンの `base_text` から常に復元できます。

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `HLE_PORT` | `8917` | Webサーバーのポート（使用中なら近傍を自動探索） |
| `HLE_DATA_DIR` | `<リポジトリ>/data` | データ保存先 |
| `HLE_NO_BROWSER` | (未設定) | 設定するとブラウザの自動起動を抑止 |

## テスト

```bash
uv run pytest                      # 全テスト（ユニット + Playwright E2E）
uv run pytest --ignore=tests/e2e   # ユニット/統合のみ
```

## 構成

```
src/hle_mcp/
├── server.py      # MCPサーバー本体（ツール定義）
├── webserver.py   # ローカルWebサーバー（UI配信 + JSON API）
├── storage.py     # セッション/画像の永続化・検索・リビジョン圧縮
├── feedback.py    # 編集HTML → フィードバック抽出
├── admin.py       # hle-admin CLI
└── static/        # エディタUI（index.html / tracker.js / app.js / admin.*）
```

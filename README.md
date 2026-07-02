# claude-status-monitor

XFCE4 パネル（タスクバー）に Claude の**利用枠の残量**を簡潔に表示するための小さなツールです。

## 何を表示するか

**通常時（公式データ）**: `残 5h:62% 7d:96%`

- **`5h:62%`** … 直近5時間枠（レートリミットのローリング枠）の**残り 62%**。
- **`7d:96%`** … 週次(7日)枠の**残り 96%**。
- 残りが 15% 以下になると先頭に `⚠` が付きます。
- ホバー（ツールチップ）で詳細：各枠の残り%/使用%、リセット時刻と残り時間。

これは Claude Code の `/usage` と同じ公式データ（`/api/oauth/usage`）です。

**フォールバック時（オフライン等）**: `~5h 1.2M ⟳2h13m`

- 公式APIに接続できない場合は、ローカルログからの**消費量（トークン数）**表示に自動で切り替わります（先頭 `~` が目印）。

## クレジット（利用枠）は消費しません

- 表示するのは**レートリミットの残量の問い合わせ**であり、プロンプト送信＝推論ではありません。したがって **Claude の利用枠（クレジット）は一切消費しません**。
- 使うのは Claude Code のログイン情報（`~/.claude/.credentials.json` のアクセストークン）で、`/api/oauth/usage` に GET するだけです。
- アクセストークンが**期限切れの時だけ**、`refresh_token` でトークン更新します（更新も推論ではないので**クレジット消費なし**）。連打で更新エンドポイントを叩きすぎないよう**安全弁**を設けています（後述）。
- 認証切れ後の更新も失敗（オフライン等）した場合は、ローカルログ（`~/.claude/projects/**/*.jsonl`）の**読み取りのみ**にフォールバック。
- `ccusage` / `npx` などは使わず、Python3 標準ライブラリのみで動作します。

> セキュリティ注: アクセストークンは実行時にローカルの認証ファイルから読み、更新時のみ書き戻します。**標準出力やログには一切出力しません**。

### トークン更新の安全弁

長時間 Claude Code を使っていないとトークンが期限切れ（発行から約8時間）になります。本ツールは自分で更新しますが、暴走で更新エンドポイントを叩き続けないよう次の歯止めを入れています:

- **期限切れの時だけ**更新（有効なうちは触らない＝Claude Code 本体と競合しない）。
- **最短 5 分に 1 回まで**＋失敗時は**指数バックオフ（最大 1 時間）**。状態は `~/.cache/claude-status-monitor/state.json` に記録。
- Claude Code と**同じロック**（`~/.claude/.credentials.json.lock` ディレクトリ／proper-lockfile 互換）で直列化。取れなければ今回はスキップし、他プロセスのロックは壊しません。
- 書き戻しは**アトミック**（temp→rename）で、他フィールドを保持し `600` 権限を維持。

## データの鮮度

`/usage` と同じ公式値を都度取得するため常に最新です。パネルは5分ごとに更新し、**アイコンをクリックすると即時更新**されます（連打対策として直近 15 秒はキャッシュ）。トークンが切れても自分で更新するので、通常はずっと%表示が維持されます。

## セットアップ

### 1. スクリプトを配置（このリポジトリに同梱）

`claude-usage-genmon.py` を任意の場所に置きます（以下の例では `/path/to/claude-status-monitor/` を実際の絶対パスに置き換えてください）:

```
/path/to/claude-status-monitor/claude-usage-genmon.py
```

動作確認:

```sh
python3 /path/to/claude-status-monitor/claude-usage-genmon.py
```

`<txt>...</txt>` と `<tool>...</tool>` が出力されれば OK。

### 2. GenMon プラグインをパネルに追加

**スクリプトで自動セットアップ（推奨）**:

```sh
./install.sh          # 既定 5 分間隔
./install.sh 60       # 例: 60 秒間隔にする
```

`install.sh` は集計スクリプトの絶対パスを自動検出し、GenMon プラグインを作成／設定してパネルを再読み込みします。冪等なので何度実行しても重複しません。取り外しは `./uninstall.sh`。

<details>
<summary>手動でセットアップする場合</summary>

**GUI**:

1. パネルを右クリック → 「新しいアイテムの追加」→ **Generic Monitor** を追加。
2. 追加された GenMon を右クリック → 「プロパティ」。
3. 次を設定:
   - **Command**: `/usr/bin/python3 /path/to/claude-status-monitor/claude-usage-genmon.py`
     （`python3` はフルパス推奨。パネルの実行環境は PATH が最小限のことがあり、素の `python3` だと起動できず空表示になる場合があります。パスは `command -v python3` で確認）
   - **Period (Seconds)**: `300`（= 5分）
   - Label は空 or `Claude` などお好みで。

**xfconf（補足・スクリプト設定）**:

GenMon インスタンス（例 `plugin-21`）に対して:

```sh
DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-21/command \
  -n -t string -s '/usr/bin/python3 /path/to/claude-status-monitor/claude-usage-genmon.py'
DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-21/update-period \
  -n -t int -s 300000   # 単位はミリ秒（300000 = 300秒）
DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-21/use-label \
  -n -t bool -s false
```

設定後、パネルを再読み込み: `DISPLAY=:0 xfce4-panel -r`

</details>

## 表示例

```
残 5h:62% 7d:96%        # 通常時: 5時間枠 残り62% / 週次 残り96%
⚠ 残 5h:8% 7d:80%      # 残り15%以下は ⚠ 付き
~5h 1.2M ⟳2h13m        # フォールバック: 公式APIに繋がらずローカル消費量表示
```

## セキュリティ

- 認証ファイル（`~/.claude/.credentials.json`）のアクセストークンを読み、`/api/oauth/usage` に問い合わせます。期限切れ時のみ `refresh_token` で更新し、その結果を書き戻します（アトミック・他フィールド保持・`600`）。**トークンは標準出力やログに一切出力しません**。
- 解析するのは残量%とトークン数のみ。ログ本文（プロンプト内容）は扱いません。
- 残量問い合わせもトークン更新も**推論ではないので、Claude の利用枠（クレジット）は消費しません**。更新の叩きすぎは安全弁で抑止（上記）。
- 依存エンドポイントは**非公開**です。Claude Code の更新で仕様が変わると公式表示が動かなくなる可能性がありますが、その場合も自動でローカル集計にフォールバックします。

## 依存する非公開エンドポイント

- 残量取得: `GET https://api.anthropic.com/api/oauth/usage`
  - ヘッダ: `Authorization: Bearer <accessToken>`, `anthropic-beta: oauth-2025-04-20`
  - 応答例: `{"five_hour":{"utilization":38.0,"resets_at":...},"seven_day":{"utilization":4.0,...}}`（`utilization` は使用率%）
- トークン更新: `POST https://platform.claude.com/v1/oauth/token`
  - ボディ: `{"grant_type":"refresh_token","refresh_token":"…","client_id":"9d1c250a-…"}`

## カスタマイズ

- 認証ファイル/ログの場所は環境変数 `CLAUDE_CREDENTIALS` / `CLAUDE_PROJECTS_DIR`、状態の場所は `XDG_CACHE_HOME` で変更可。
- 更新間隔の下限・バックオフ・キャッシュ秒数などの安全弁は `claude-usage-genmon.py` 冒頭の定数（`MIN_REFRESH_INTERVAL` 等）で調整。
- 表示文言や閾値は `render_official()` / `local_fallback()` を編集。

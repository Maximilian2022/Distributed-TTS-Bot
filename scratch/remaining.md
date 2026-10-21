# ttsbot-oss 残存タスクおよび実装計画引継書 (remaining.md)

本書は、`ttsbot-oss` ワークスペースを開いた直後から迷いなく実装を再開・完遂できるように、現在の進捗状況、アーキテクチャ方針、および残存する各実装タスクの具体的な仕様と手順をまとめた引き継ぎドキュメントです。

---

## 1. プロジェクト現況とコンテキスト

### 1.1 目標
現行の Discord TTS Bot を、誰でもGitHubからクローンして数分で利用開始できる**汎用オープンソース（OSS）プロダクト**へリファクタリングする。
稼働中の本番環境（`/root/ttsbot`）は一切止めず、独立環境（`ttsbot-oss`）で新構成を完成・検証し、最後にデータ移行を行う。

### 1.2 達成済み項目 (Initial Commit + 基礎レイヤー整備)
- [x] クリーンリポジトリの作成（機密情報・過去Git履歴・本番DBの完全排除）
- [x] `README.md`（一般向け概要）および `MANUAL.md`（詳細動作仕様書）の作成
- [x] `src/database.py`:
  - `system_credentials` テーブル作成（APIキー・各種設定値の秘匿保管）
  - `bot_tokens` テーブル作成（1〜10号機トークンのプール管理）
  - 各種CRUDメソッド（`get_credential`, `set_credential`, `get_bot_tokens`, `save_bot_token`, `delete_bot_token`, `toggle_bot_token`）の実装
- [x] `src/config.py`:
  - 1〜10号機トークン対応（`discord_tokens_map`）
  - `WEB_BASE_URL` による動的ベースURL解決
  - セッションシークレットおよび `worker_shared_secret` の自動安全生成
  - `apply_db_credentials()` メソッドの実装

---

## 2. 残存実装タスク一覧 (実行順)

### Task 1: `src/main.py` のブートストラップ改修
**目的**: 初回起動時（トークン0件）でもプロセスを終了せず、WebUIを起動して待機する。

- [ ] **トークン0件許容ブートストラップ**:
  - `tokens = config.get_discord_tokens()` が空の場合でも `sys.exit(1)` を行わない。
  - 「⚠️ Discord Bot トークンが登録されていません。WebUI（http://<host>:<port>）から登録してください。」と案内ログを出力。
- [ ] **WebServer のライフサイクル独立**:
  - 現状は `TTSBot`（1号機）の `__init__` 内で `self.web_server = WebServer(...)` を生成しているため、Bot 1号機がないとWebUIが起動しない。
  - `main()` 関数スコープで `web_server = WebServer(...)` を初期化・起動し、`bot_pool` および `db` を直接渡す設計に改修する。
- [ ] **`bot_factory` の注入とDBトークン自動起動**:
  - `main()` 内で `create_bot(bot_index: int) -> TTSBot` 関数を定義。
  - `bot_pool.set_bot_factory(create_bot)` を実行。
  - DBから `await db.get_bot_tokens(active_only=True)` を取得し、登録済みトークンがあれば `bot_pool.start_bot(idx, token)` で非同期起動。

---

### Task 2: `src/bot_pool.py` の動的Botライフサイクル実装
**目的**: WebUIからの要求に応じて、Botの起動・切断・再接続・トークン変更を無停止で動的に処理する。

- [ ] **Botインスタンス動的生成・起動メソッド**:
  - `set_bot_factory(self, factory_func)`
  - `async def start_bot(self, bot_index: int, token: str) -> bool`:
    - 既存のBotタスクがあれば安全に停止・クローズ。
    - `bot = self.bot_factory(bot_index)` で生成。
    - `self.register_bot(bot_index, bot)`
    - `task = asyncio.create_task(bot.start(token))` で起動し、`self.bot_tasks[bot_index] = task`。
- [ ] **Botインスタンス安全停止メソッド**:
  - `async def stop_bot(self, bot_index: int) -> bool`:
    - `bot = self.bots.get(bot_index)` があれば `await bot.close()`。
    - `task = self.bot_tasks.pop(bot_index, None)` でタスクをキャンセル。
    - `self.unregister_bot(bot_index)`
- [ ] **トークンCRUD連携メソッド**:
  - `async def add_or_update_token(self, bot_index: int, token: str, is_active: bool = True, note: str = "")`:
    - DB保存 (`await self.db.save_bot_token(...)`)
    - `self.config.discord_tokens_map[bot_index] = token`
    - `if is_active: await self.start_bot(bot_index, token)`
    - `else: await self.stop_bot(bot_index)`
  - `async def delete_token(self, bot_index: int)`:
    - `await self.stop_bot(bot_index)`
    - DB削除 (`await self.db.delete_bot_token(bot_index)`)
    - `self.config.discord_tokens_map.pop(bot_index, None)`
  - `async def toggle_token(self, bot_index: int, is_active: bool)`:
    - DB更新 (`await self.db.toggle_bot_token(bot_index, is_active)`)
    - 有効化ならトークンを取得して `start_bot`、無効化なら `stop_bot`。
- [ ] **プール状態集約メソッド**:
  - `get_pool_status(self) -> List[Dict[str, Any]]`:
    - 1〜10号機ごとの「登録有無、有効/無効、Gateway接続中(Ready)、Botユーザー名、ユーザーID、参加サーバー数、VC接続中情報」を辞書リストで返す。

---

### Task 3: `src/tts/manager.py` の Edge-TTS ファースト化 & 動的クレデンシャルリロード
**目的**: クラウドAPIキーがなくてもクローン後5分で動作し、WebUIからキーを入れた瞬間にAzure/Googleが利用可能になる。

- [ ] **キー未設定時の安全動作**:
  - 初期化時に Azure / Google のキーが空の場合でも警告ログを出さず、安全にスキップ（または `is_available() == False` 状態を保持）。
  - フォールバックチェーンの先頭または標準プライマリを Edge-TTS に設定可能にする。
- [ ] **動的クレデンシャルリロード (`reload_cloud_credentials`)**:
  - `def reload_cloud_credentials(self, azure_key: str = "", azure_region: str = "", google_json: str = "")`:
    - `self.azure_client` のキー・リージョンを更新。
    - `self.google_client` のJSON認証情報を再ロード（SDKクライアントの再初期化）。
    - ログに「Cloud credentials reloaded successfully」を出力。

---

### Task 4: `src/web/server.py` のバックエンドAPI拡張 & WebSocketリバースプロキシ
**目的**: WebUIからBotやAPIキーを管理し、NATの奥にあるWorkerとMasterを接続する。

- [ ] **Bot管理 API 群 (管理者権限必須)**:
  - `GET /api/system/bots`: 登録トークン一覧（下4桁以外伏字）＋接続ステータス＋参加サーバー一覧。
  - `POST /api/system/bots`: 新規トークン登録または更新（`bot_index`, `token`, `is_active`, `note`）。
  - `DELETE /api/system/bots/{idx}`: トークン削除・切断。
  - `POST /api/system/bots/{idx}/toggle`: 有効/無効切り替え。
  - `POST /api/system/bots/test`: トークン妥当性テスト（Discord APIへのテストログインまたはGateway疎通確認）。
- [ ] **クレデンシャル管理 API 群 (管理者権限必須)**:
  - `GET /api/system/credentials`: 保存済みキー一覧（伏字）。
  - `POST /api/system/credentials`: Azureキー、Google JSON、Worker共通秘密鍵、WebBaseURLの保存＆`tts_manager.reload_cloud_credentials()` 呼び出し。
  - `POST /api/system/credentials/test`: 指定プロバイダの即時テスト音声合成（テストWAV生成）。
- [ ] **WebSocket リバースプロキシ (`/api/worker/ws`)**:
  - リモートWorker（GPU PC等）からの外向き接続を受信。
  - ハンドシェイク時に `Authorization: Bearer <WORKER_SHARED_SECRET>` またはクエリパラメータのトークンを検証。
  - WAN制限（`_is_wan_request` の一律拒否）を撤廃し、Bearerトークン認証に一本化。
  - Master側の `TTSNodePool` からのリクエスト（JSON + 音声合成要求）をWebSocket経由でWorkerへ転送し、生成されたWAVバイナリを受信して呼び出し元へ返す非同期ブリッジを構築。

---

### Task 5: `src/web/static/agent.py` & `setup-worker.sh` の改修
**目的**: 自宅GPU PCや別サーバーでワンライナーを実行するだけで、ポート開放不要でMasterへ接続できるようにする。

- [ ] **`agent.py` の WebSocket クライアントモード実装**:
  - 引数に `--master-ws wss://<master-domain>/api/worker/ws --secret <token>` を追加。
  - 起動時にMasterへWebSocket接続を確立し、ハートビート（Ping/Pong）を維持。
  - 合成要求メッセージを受信したらローカルVOICEVOX/COEIROINK（localhost:50021等）にHTTPリクエストを投げ、結果WAVをMasterへWebSocket返送。
  - 切断時は指数バックオフ（1s〜30s）で自動再接続。
- [ ] **`setup-worker.sh` の自動生成**:
  - WebUIからコピー可能なワンライナー（`curl -sSL https://.../setup-worker.sh | bash -s -- --token ...`）を整備。

---

### Task 6: WebUI フロントエンド (`index.html`, `app.js`) の実装
**目的**: 非エンジニアでもブラウザから全ての操作を直感的に行えるようにする。

- [ ] **「🤖 Discord Bot 管理」画面**:
  - 最大10台のスロットカード表示（1号機〜10号機）。
  - 状態バッジ（🟢 接続中 / ⚪ 停止中 / 🔴 エラー）。
  - トークン追加・変更モーダル、有効/無効トグルスイッチ、削除ボタン。
  - 「Discord Bot 招待リンク生成」ボタン（クライアントID自動抽出または入力）。
- [ ] **「🔑 API・クレデンシャル設定」画面**:
  - Azure Speech キー・リージョン入力フォーム＆「接続テスト」ボタン。
  - Google Cloud サービスアカウントJSONアップロード/貼り付けフォーム＆「接続テスト」ボタン。
  - 外部公開ベースURL（`WEB_BASE_URL`）入力フォーム。
- [ ] **「🖥️ GPU Worker 接続」画面**:
  - 現在接続中のWorker一覧（PC名、GPU種別、Ping、担当エンジン）。
  - 「新しいWorkerを接続する」ワンライナー表示（コピーボタン付き）。

---

### Task 7: `docker-compose.yml` のプロファイル化 & 配布準備
**目的**: ユーザーの利用シーンに応じた柔軟な起動を可能にする。

- [ ] **Compose プロファイル化**:
  - `standalone`（デフォルト）: Master Bot + WebUI（Edge-TTS + OpenJTalk）。
  - `voicevox`: ローカルVOICEVOXコンテナを同梱して起動。
  - `tunnel`: Cloudflare Tunnel（`cloudflared`）を同梱して安全にWAN公開。
- [ ] **`.env.example` の最新化**:
  - 必須項目を `ADMIN_PASSWORD` のみとし、`DISCORD_TOKEN` は任意（WebUIから登録可）とする案内を記載。

---

### Task 8: 動作検証 (サンドボックス・一時ポート起動)
**目的**: 一連の新規機能が不具合なく動作することを確認する。

- [ ] テスト用 `.env` を作成（ポート `8090`、テスト用DB `data/test_bot.db`）。
- [ ] トークン0件で起動確認（WebUIが立ち上がり、トークン未設定の案内が出ること）。
- [ ] WebUIからBotトークンを登録し、Gateway接続が確立することを確認。
- [ ] Edge-TTS による音声合成と通話再生を確認。
- [ ] WebUIからAzure/Googleキーを登録し、ホットリロードで合成可能になることを確認。
- [ ] テスト用コンテナおよび一時ファイルの確実な清掃。

---

### Task 9: 本番環境（`/root/ttsbot`）からのデータ移行とSide-by-Side切替
**目的**: 既存の辞書データ・ギルド設定を100%引き継ぎ、無停止に近い形で新環境へ切り替える。

- [ ] 本番DB（`/root/ttsbot/data/bot.db`）の辞書・ギルド設定テーブルを新DBへエクスポート/インポート。
- [ ] 本番コンテナ停止と新コンテナ昇格の切替スクリプト（`switch_to_oss.sh`）の作成・検証。

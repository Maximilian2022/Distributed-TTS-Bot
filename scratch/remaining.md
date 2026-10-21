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
- [x] **トークン0件許容ブートストラップ**: トークン未設定時でもプロセスを終了せず待機。
- [x] **WebServer のライフサイクル独立**: `TTSBot` から完全に独立し、`main()` 直下で起動・管理。
- [x] **`bot_factory` の注入とDBトークン自動起動**: 動的プールへのファクトリ設定とDB保存トークンの起動時自動適用を完了。

---

### Task 2: `src/bot_pool.py` の動的Botライフサイクル実装
**目的**: WebUIからの要求に応じて、Botの起動・切断・再接続・トークン変更を無停止で動的に処理する。
- [x] **Botインスタンス動的生成・起動メソッド**: `start_bot(bot_index, token)` 実装完了。
- [x] **Botインスタンス安全停止メソッド**: `stop_bot(bot_index)` 実装完了。
- [x] **トークンCRUD連携メソッド**: `add_or_update_token`, `delete_token`, `toggle_token` 実装完了。
- [x] **プール状態集約メソッド**: `get_full_pool_status()` 実装完了（全10スロットのステータス集約）。

---

### Task 3: `src/tts/manager.py` の Edge-TTS ファースト化 & 動的クレデンシャルリロード
**目的**: クラウドAPIキーがなくてもクローン後5分で動作し、WebUIからキーを入れた瞬間にAzure/Googleが利用可能になる。
- [x] **キー未設定時の安全動作**: Azure/Google未設定時でもEdge-TTS/OpenJTalkで完全動作。
- [x] **動的クレデンシャルリロード**: `reload_cloud_credentials(...)` 実装完了。
- [x] **即時テスト音声合成**: `test_synthesis(provider, voice_name)` 実装完了。

---

### Task 4: `src/web/server.py` のバックエンドAPI拡張 & WebSocketリバースプロキシ
**目的**: WebUIからBotやAPIキーを管理し、NATの奥にあるWorkerとMasterを接続する。
- [x] **パスワード認証**: `POST /api/auth/login_password` 実装完了。
- [x] **Bot管理 API 群**: `GET /api/system/bots`, `POST /api/system/bots`, `DELETE /api/system/bots/{idx}`, `POST /api/system/bots/{idx}/toggle` 実装完了。
- [x] **クレデンシャル管理 API 群**: `GET /api/system/credentials`, `POST /api/system/credentials`, `POST /api/system/credentials/test` 実装完了。
- [x] **WebSocket リバースプロキシ**: `GET /api/worker/ws` 実装完了。

---

### Task 5: `src/web/static/agent.py` & `setup-worker.sh` の改修
**目的**: 自宅GPU PCや別サーバーでワンライナーを実行するだけで、ポート開放不要でMasterへ接続できるようにする。
- [x] **`agent.py` の WebSocket クライアントモード実装**: `--master-ws` 引数および `MASTER_WS_URL` による常時双方向接続、ローカルエンジン合成連携完了。
- [x] **`setup-worker.sh` の引数対応 & ワンライナー整備**: `--master-ws`, `--secret` 引数対応完了。

---

### Task 6: WebUI フロントエンド (`index.html`, `app.js`) の実装
**目的**: 非エンジニアでもブラウザから全ての操作を直感的に行えるようにする。
- [x] **管理者パスワードログインフォーム**: 未ログイン画面への追加とハンドラ実装完了。
- [x] **「🤖 Discord Bot 管理」画面**: 1〜10号機スロットカード、招待URL生成、モーダル設定完了。
- [x] **「🔑 API・クレデンシャル設定」画面**: Azure/Googleキー設定、Worker秘密鍵再生成、ワンライナー表示完了。

---

### Task 7: `docker-compose.yml` のプロファイル化 & 配布準備
**目的**: ユーザーの利用シーンに応じた柔軟な起動を可能にする。
- [x] **Compose プロファイル化**: デフォルトでスタンドアロン (`app`)、オプションで `--profile voicevox`, `--profile tunnel` 実装完了。プロジェクト固有ボリューム化で本番環境との競合を排除。
- [x] **`.env.example` の最新化**: 初心者向けガイド、必須/任意の整理、GUI設定案内を記載完了。

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

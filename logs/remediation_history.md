## 2026-11-26 16:27 Dependabot PR #23 (`janome>=0.5.2`)・PR #24 (`azure-cognitiveservices-speech>=1.53.1`) の互換性検証とマージ・ローカル同期
- **背景・目的**:
  - Dependabot から作成された依存パッケージ最小バージョン要件引き上げの Pull Request 2件（PR #23, PR #24）について、本番 Python `3.14` 環境との互換性を検証のうえ `main` ブランチへ取り込む。
- **確認・実施内容**:
  1. 使い捨てコンテナ (`docker run --rm madpiero/ttsbot:latest`) 上での事前互換性検証:
     - `janome`: 現行イメージにすでに `0.5.2` が導入済みで問題なし。
     - `azure-cognitiveservices-speech==1.53.1`: Linux x86_64 wheel の導入、`import azure.cognitiveservices.speech as speechsdk`、およびネイティブ C++ バインディング初期化 (`SpeechConfig`) が正常通過することを確認。
  2. GitHub PR マージおよびブランチ整理:
     - PR #24 (`build(deps):(deps): update azure-cognitiveservices-speech requirement from >=1.53.0 to >=1.53.1`) を `main` へマージし、リモートブランチを削除。
     - PR #23 (`build(deps):(deps): update janome requirement from >=0.5.0 to >=0.5.2`) を続けて `main` へマージし、リモートブランチを削除。
  3. ローカルリポジトリ同期:
     - `/root/ttsbot` にて `git pull --ff-only origin main` (`1a76f94..7a4ddb7`) を実行し、`requirements.txt` の更新を反映。
- **検証・状態**:
  - 軽微なパッチ要件引き上げであり緊急の脆弱性対応ではないため、合意に基づき稼働中の本番コンテナ (`ttsbot-app-1`) の即時再起動は実施せず無停止を維持（次回以降のビルド・更新時に自動反映）。

## 2026-11-25 21:56 Worker Agent 一時ヘルパーコンテナ (`tts-compose-helper-*`) 残留防止・自動ガベージコレクションの実装 (`v3.15.1`)
- **背景・原因**:
  - `docker` CLI を内包しないコンテナ環境（主に Windows Docker Desktop 上の Worker Agent）において、`src/web/static/agent.py` の `DockerAPI.run_compose_via_helper()` が以下の理由で `tts-compose-helper-<timestamp>` コンテナを残留させていた：
    1. `detach=True` 呼び出し時に `"AutoRemove": not detach` (`False`) が指定されたまま誰も `DELETE` せず終了するため、100% `Exited` コンテナとして残留していた。
    2. `/containers/{cid}/start` 失敗時（`Created` 状態）や `/wait` タイムアウト・例外発生時に `try ... finally` が無く削除処理に到達しないケースがあった（また秒単位命名のため同時起動時に名前衝突の可能性があった）。
    3. Docker Desktop 再起動や Agent 再起動を跨いで過去に残留した `tts-compose-helper-*` を回収する仕組みが無かった。
- **実施内容**:
  1. `src/web/static/agent.py`:
     - `DockerAPI.cleanup_stale_compose_helpers(max_age_seconds=600)` を新設。厳密に `^tts-compose-helper-(\d+)(?:-[0-9a-f]+)?$` に一致するコンテナのみを対象とし、停止済み (`exited` / `dead` / `created`) または作成から10分以上経過したスタック個体を `DELETE /containers/{cid}?force=1&v=1` で自動削除（直近実行中のヘルパーや他コンテナには一切触れない安全設計）。
     - `run_compose_via_helper()` にて：① 実行前に `cleanup_stale_compose_helpers()` を呼び出し、② コンテナ名へランダム16進サフィックスを付与し同時実行衝突を防止、③ `detach=True` 時は `AutoRemove=True`（`start` 失敗時は即時 `DELETE`）、`detach=False` 時は `AutoRemove=False` で確実にログ回収の上 `try ... finally` で無条件に `DELETE ?force=1&v=1` を実行するよう修正。
     - `WorkerAgent.startup_sequence()` 冒頭および `engine_watchdog_loop()`（5分周期）に `cleanup_stale_compose_helpers()` 呼び出しを組み込み、既存ワーカーに残っている過去の残骸もアップデート直後に自動一掃されるよう構成。
  2. `src/version.py` & `src/web/static/agent.py`:
     - `AGENT_VERSION` を `"3.15.0"` → `"3.15.1"` に更新（Master 本番反映時にハートビート相乗りで全ワーカーへ自動配信・Graceful Restart 反映させるため）。
  3. `tests/test_compose_helper_cleanup.py`:
     - GC フィルタの厳密なスコープ検証、`detach=True` の `AutoRemove=True` と `start` 失敗時削除、`detach=False` の `/wait` 例外時 `finally` 確実削除の単体テスト 3 件を追加。
- **検証・状態**:
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh`): 新規テスト 3 件合格 (`Ran 3 tests in 0.004s`, OK)、全単体テスト 245 件合格 (`Ran 245 tests in 16.478s`, OK)、一時コンテナ完全破棄確認済み。
  - 本番反映実施: `docker compose up -d --no-deps --force-recreate app` にて 19秒で Ready・`healthy` 復帰を確認。`/health` HTTP 200 OK (`status="ok"`, `is_ready=true`, `guilds_count=2`)、`/static/agent.py` 配信バージョン `3.15.1` (`cleanup_stale_compose_helpers` 含有)、および Bot #1〜#5 全号機の正常登録を確認完了（各ワーカーはハートビート経由で順次 `v3.15.1` へ自動更新・起動時に残留 `tts-compose-helper-*` を自動一掃）。

## 2026-11-25 06:26 Gateway再接続跨ぎの古いVoiceChannel参照による無人VCソロ居残り・ゴースト滞在の恒久修正
- **背景・原因**:
  - `2026-11-25 00:32:52` の `AutoJoinReconcile` VC接続リトライ中に Discord Gateway セッション無効化 (`session has been invalidated`) と `READY` 再受信が重なった際、先行タスクが掴んでいた旧 `Guild` / `VoiceChannel` インスタンス（当時の在室者 1名が凍結された孤立キャッシュ）が `vc.channel` に格納されたまま接続完了し、後続の `on_voice_state_update` や `_auto_leave_watchdog_loop` が `vc.channel.members` の凍結メンバーを見続けて 5時間以上無人退出できなかった事象の恒久対策。
- **実施内容**:
  1. `src/cogs/vc_handler.py`:
     - `_resolve_live_channel()` ヘルパーを新設し、常に `self.bot.get_guild(guild.id).get_channel(ch_id)` から最新のチャンネルインスタンスを取得するとともに、`vc.channel` が旧インスタンスを指していた場合は最新インスタンスへその場で自動修復するよう実装。
     - `connect_vc()`（ロック直後・既存接続再利用分岐・リトライ各試行前・ハンドシェイク完了後）、`on_voice_state_update()`、および `_auto_leave_watchdog_loop()` のすべてで最新チャンネル参照を通すよう統一（再利用分岐で0人だった場合の即時切断ガードも追加）。
     - `guild.voice_client` が `None` なのに `guild.me.voice.channel` だけ残存するゴーストVC滞在についても、`on_voice_state_update` / `_auto_leave_watchdog_loop` で無人検知し、`disconnect_vc()` で `guild.change_voice_state(channel=None)` により確実に切断するよう強化。また `on_ready()` でも `_auto_leave_watchdog_loop` を起動するよう変更。
  2. `src/resource_manager.py`:
     - `LocalEngineResourceManager` に `_resolve_guild_vc_channel()` を追加し、`is_any_human_in_vc()`・`sync_active_vc_models_to_worker()`・定期アイドル監視で古い `vc.channel.members` による無人判定漏れ（ローカルエンジンのスリープ阻害）を防止。
  3. `tests/test_vc_stale_channel_guard.py`:
     - 旧 `vc.channel` の自動修復、`on_voice_state_update` 即時退出、Watchdog 猶予後退出、ゴーストVC検知・`change_voice_state(channel=None)` 切断、`LocalEngineResourceManager.is_any_human_in_vc()` の計 5 件の再現単体テストを追加。
- **検証・状態**:
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh`): 新規テスト 5 件合格 (`Ran 5 tests in 0.097s`, OK)、全単体テスト 242 件合格 (`Ran 242 tests in 18.756s`, OK)、一時コンテナ・一時データ完全破棄確認済み。
  - 本番反映実施: `docker compose up -d --no-deps --force-recreate app` により切り替え完了。`/health` HTTP 200 OK (`status="ok"`, `is_ready=true`, `guilds_count=2`)、Bot #1〜#5 全号機での `Auto-leave watchdog started` 起動、および稼働中 VC (`えぺAPEエペ` / Bot #2) の自動再接続 (`Voice connection complete.`) とエラーなしを実機確認完了。

## 2026-11-18 19:29 サーバー固有ボット設定に「VCに参加していない人の読み上げは行わない」オプション (`require_vc_membership`) を実装（デフォルト OFF）
- **背景・目的**:
  - サーバーごとの WebUI 設定「⚙️ ボット設定 (サーバー固有)」にて、現在読み上げ中の VC に参加していないユーザー（別 VC 参加中を含む）のテキストメッセージを読み上げ対象外にできるオプション（デフォルト OFF）を追加する。
- **実施内容**:
  1. `src/database.py`:
     - `guild_settings` テーブル定義、起動時自動 `ALTER TABLE` マイグレーション、`get_guild_settings()`（既存・新規デフォルト `0`）、`update_guild_settings()`（`INSERT ... ON CONFLICT DO UPDATE`）へ `require_vc_membership INTEGER DEFAULT 0` を追加。
  2. `src/web/server.py`:
     - `GET /api/config` (`api_get_config`) のレスポンスおよび `POST /api/config` (`api_update_config`) の更新項目に `require_vc_membership`（boolean 正規化）を追加。
  3. `src/web/templates/index.html` & `src/web/static/app.js`:
     - 「⚙️ ボット設定 (サーバー固有)」内の `cfg-announce-vc` の直後にチェックボックス `cfg-require-vc-membership`（🔇 VCに参加していない人の読み上げは行わない（デフォルト: OFF））を追加し、`loadBotConfig()` / `saveBotConfig()` へ読み込み・保存連携を実装。
  4. `src/cogs/vc_handler.py`:
     - 純粋判定ヘルパー `_is_author_in_vc()` を実装し、`on_message()` で `guild_settings.get("require_vc_membership", 0)` が有効な場合は読み上げ中 VC (`vc.channel.id`) に参加していない送信者のメッセージを辞書クエリ・TTS 生成前に早期スキップ（`author` が `discord.Member` でない場合は `guild.get_member()` でフォールバック判定）。
  5. `tests/test_require_vc_membership.py`:
     - DB スキーマ/マイグレーション（デフォルト OFF 検証）、Web API `/api/config` GET/POST、`VCHandlerCog.on_message` の OFF/ON（未参加・別VC除外・同一VC読み上げ・`discord.User` フォールバック）を検証する単体テスト 5 件を追加。
- **検証・状態**:
  - `node --check src/web/static/app.js`: 構文エラーなし。
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh python3 -m unittest discover tests`): 全 237 件合格 (`Ran 237 tests in 17.110s`, OK)、`tests/test_require_vc_membership.py` 単体 5 件合格 (`Ran 5 tests in 0.716s`, OK)、一時コンテナ自動破棄確認済み。
  - 本番反映実施: `./scripts/rolling_update.sh` はコードマウント変更のみでは既存コンテナが再作成されないため、`docker compose up -d --no-deps --force-recreate app` で 1回限りの安全切り替えを実施。17秒で Ready、`Up 40 seconds (healthy)`、`/health` 200 OK (`status="ok"`, `is_ready=true`, `guilds_count=2`)、WebUI HTML への `cfg-require-vc-membership` 配信、および本番 DB (`data/bot.db`) の `guild_settings.require_vc_membership` カラム追加と既存2ギルドの初期値 `0` (OFF) を実機確認完了。

## 2026-11-14 20:56 ローカルTTS限定 SudachiPy 漢字かな補完・共通日本語/MMOゲーム用語辞書・❌リアクションDiscordボタン承認UIの実装
- **背景・目的**:
  - 漢字の誤読（ローカルTTSにおける文脈依存漢字や難読語・ゲーム用語の読み間違い）、MMO・ネットゲーム特有の略語/記号混在語の誤読、およびユーザーによる `❌` リアクション誤読指摘からの登録導線を改善するため、合意済みの3本柱（＋WIKIWIKI MMO用語全集ベース初期辞書・英語略語拡充）を一括実装し本番適用する。
- **実施内容**:
  1. `src/utils/common_reading_dict.json` & `src/utils/text_cleaner.py`:
     - MMO用語全集を参考に、全サーバー共通初期プリセット辞書 (`common_reading_dict.json`, **61語**: `宝珠`, `廃神`, `教えて君`, `タダゲー厨`, `Pスキル`, `3D酔い`, `DDoS攻撃`, `おk`, `完凸`, `零式`, `蛮神`, `瘴気`, `顕現`, `邂逅`, `篭もる` 等) を新設。
     - `COMMON_ACRONYMS` に MMO/ネットゲーム英語略語 (`directx`, `wb`, `debuff`, `buff`, `mob`, `melee`, `dupe`, `thx`, `thanx`, `plz`, `ez`, `rdy`, `pk`, `pt`, `npc`, `gm`, `str`, `dex`, `vit`, `int`, `mnd`, `chr`, `ddos`, `rmt`, `rmter`, `ss`, `hq`, `dmg`, `wtb`, `wts`, `wtt`, `wiz` 等) を拡充。
     - 単一パス境界ガード付き複合正規表現 `_COMMON_READING_REGEX` + O(1) ルックアップ (`data/custom_common_reading.json`) を実装し、`apply_dictionary()` でサーバー個別辞書 (`guild_dictionary`) 置換箇所をプレースホルダ保護して常に最優先適用。
     - ローカルTTS専用安全かな補完 `convert_kanji_for_local_tts()` および段階診断 `preview_common_and_local_tts()` を実装（漢字なしファストパス、`<<__BEEP:N__>>`・助詞・記号・OOV未知語・数詞＋助数詞クラスタ保護）。
  2. `src/database.py`:
     - `guild_settings`（テーブル定義・自動 `ALTER TABLE` マイグレーション・GET/UPDATE）に `local_tts_kanji_reading INTEGER DEFAULT 1` を追加。
     - `get_ai_suggestion_by_guild_word()` を新設し、`approve_ai_suggestion(suggestion_id, reading=None)` を編集後の読み上書き登録対応に拡張。
  3. `src/tts/manager.py`:
     - `resolve_cache_text()`, `_synthesize_single_chunk()`, `get_chunk_audio()`, `get_audio_path()` に `local_tts_kanji_reading` を導入。VOICEVOX / SHAREVOX / COEIROINK / OpenJTalk 等ローカルエンジン送信時のみ安全かな補完を適用し、Azure / Google / Edge 等クラウドエンジンへは原文のまま送信。キャッシュキーは ON/OFF を分離しつつ `cache_metadata.text` は元表記を保持し `purge_by_word` 互換を維持。
  4. `src/cogs/vc_handler.py`:
     - `QueueItem`・プリフェッチ・再生ループへ `local_tts_kanji_reading` を伝播。
     - `_reaction_learn_cache` (`OrderedDict`, TTL 300s / 最大500件) デバウンス付き `_learn_from_reaction()` および `MisreadSuggestionApprovalView` / `MisreadSuggestionEditModal` を実装し、`❌` リアクション指摘に対し元メッセージへのリプライUIからワンクリック登録・読み修正登録・却下および `cache_manager.purge_by_word` 即時削除を実行可能化。
  5. `src/web/server.py`, `src/web/templates/index.html`, `src/web/static/app.js`:
     - `/api/common_dict` (`GET`, `POST`, `import_batch`, `preview`, `DELETE` パス/クエリ両対応) と `/api/config` の `local_tts_kanji_reading` 読み書きを追加。
     - WebUI 単語辞書サブタブに「🎮 共通日本語・ゲーム用語辞書」カード、一般設定タブに「🈷️ ローカルTTS 漢字かな補完」チェックボックスを追加（新規JSは純粋DOM操作のみで `innerHTML` 不使用）。
  6. `tests/test_kanji_and_common_dict.py`:
     - 漢字補完の各種保護、MMO辞書/略語、サーバー辞書優先、TTSManager ローカル/クラウド分岐＆キャッシュ分離、DBマイグレーション＆上書き承認、Web API 認証/権限/CRUD、Discord ボタン/モーダル承認UIの計9テストを追加。
- **検証・本番反映結果**:
  - `node --check src/web/static/app.js` 構文エラーなし。
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh python3 -m unittest discover tests`) にて全 232 件の単体テストが合格（`Ran 232 tests in 12.886s OK`）。
  - `./scripts/rolling_update.sh --build` により本番コンテナ (`ttsbot-app-1`) をローリングアップデートし、`healthy` 復帰 (`/health` 200 OK)、本番DB `guild_settings.local_tts_kanji_reading` カラム確認、`TextCleaner.preview_common_and_local_tts` 実機動作確認完了（検証用 `/tmp/mmo_terms*` も削除済み）。

## 2026-11-14 19:15 多層 O(1) 英語カタカナ辞書アーキテクチャ・SudachiDict抽出静的辞書・e2kオフライン未知語推論・WebUI管理機能の実装
- **背景・目的**:
  - 個別語彙のハードコード拡充では新語・固有名詞への追従に限界がある一方、総当たりループ型の辞書にすると登録数増加に伴って発話遅延が悪化するため、「登録語数が増えても計算量 O(1) で遅くならない多層辞書構造」「オープン辞書からの事前静的辞書生成」「完全未知語のオフライン自動カタカナ化」「今後の追記に備えるWebUI管理口」を一括実装・本番適用する。
- **実施内容**:
  1. `scripts/build_english_kana_dict.py` & `src/utils/english_kana_dict.json`:
     - `sudachidict-core` (Apache License 2.0) の `Dictionary().entries()` から `alkana` 未収録かつ純粋なカタカナ読みを持つ固有名詞・外来語・最新IT/SNS用語 **18,420 語** を抽出し、静的コンパクト JSON 辞書 (`src/utils/english_kana_dict.json`, 555KB, ロード約12ms) を生成するビルダーを実装（将来の JMdict XML / 外部 TSV・CSV・JSON マージにも対応）。
     - 全大文字略語コードや1文字読み羅列ノイズ・`www` スラング領域の混入を自動フィルタリング。
  2. `requirements-optional.txt` & `src/utils/text_cleaner.py`:
     - `e2k>=0.8.1` (Public Domain / `numpy` 依存) をオプショナル依存関係へ追加し、コンテナイメージ `madpiero/ttsbot:latest` を更新。
     - `TextCleaner` に O(1) 多層解決 (`resolve_english_word_kana` / `convert_english_to_kana` / `preview_english_kana`) を実装：
       - **解決順序**: `カスタム共通辞書 (data/custom_english_kana.json)` → `組込定番マップ (COMMON_ACRONYMS: 89語)` → `alkana (49,216語)` → `Sudachi抽出静的辞書 (18,420語)` → `e2k LRUキャッシュ (最大10,000語)` → `e2k NGram/C2K オフライン推論 (1文あたり新規推論上限 8語ガード付き)`。
       - `www` 等の w 連続スラングは `slang_skip` として保護し後段の `normalize_slang` (`わら`) を維持。
       - `warmup_morphological_tokenizers()` へ辞書事前ロード＆`e2k` ホットウォームを組み込み、初回発話時のモデル初期化スパイク（~400ms）を解消。
       - カスタム辞書のバリデーション・アトミック保存 (`os.replace`)・リロード・検索・一括インポート API を実装。
  3. `src/web/server.py`, `src/web/templates/index.html`, `src/web/static/app.js`:
     - `/api/english_dict` (`GET`, `POST`, `DELETE`, `import_batch`, `preview`) エンドポイントを追加（参照/プレビューは認証必須、変更・削除は `_require_admin` 権限ガード＆音声キャッシュ自動パージ付き）。
     - WebUI「📖 単語辞書」サブタブ内に「🌐 共通英語カタカナ辞書 (高速 O(1) マップ + 未知語自動推論)」UI（各層統計バッジ、解決元診断付きリアルタイム変換テスト、1件/一括追加、全辞書横断検索＆ワンクリック上書き登録）を実装。
  4. `tests/test_english_kana_dict.py` & `tests/test_text_cleaner.py`:
     - 多層 O(1) ルックアップ優先度、`www` スラング保護、カスタム辞書永続化/バリデーション、`e2k` LRU キャッシュ＆8語上限ガード、`e2k` 実機推論、および `/api/english_dict` 認証・403権限・キャッシュパージの単体テストを追加。
- **検証・本番反映結果**:
  - `node --check src/web/static/app.js` 構文エラーなし。
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh python3 -m unittest discover tests`) にて全 223 件の単体テストが合格（`Ran 223 tests in 12.104s OK`、一時コンテナ自動破棄・残骸なしを確認）。
  - `./scripts/rolling_update.sh --build` により最小瞬断ローリングアップデートを実施し、`HEALTHY` 復帰・`/health` HTTP 200 OK（`"status": "ok"`, `"is_ready": true`, `"guilds_count": 2`）、全 5 台 Bot 接続、既存 VC（`わっしょい` / `どっこいしょ`）自動復旧、`e2k` ホットウォーム完了ログ (`Dict=18420+0`) を確認。
  - 本番コンテナ内ベンチマークにて `'TikTokとか'` → `'ティックトックとか'` (`0.06ms`)、`'AmazonとNetflixを見る'` → `'アマゾンとネットフリックスを見る'` (`0.09ms`)、`'草www'` → `'草わら'` (`0.05ms`)、未知語 `'PydanticとKubernetes'` → `'ピダンティックとクーベルネッテス'` (`e2k` 推論 `56.41ms` / キャッシュ後 `0.195ms`) を実機確認完了。

## 2026-11-14 18:34 英単語カタカナ変換の日本語隣接境界バグ修正 (`_RE_ENGLISH_WORD`) ＆ 主要SNS/IT/ゲーム用語拡充
- **背景・目的**:
  - `Youtubeが突然` や `TikTokとか` のように前後にスペースなく日本語が隣接する文で、既存のカタカナ変換（`YouTube -> ユーチューブ` 等）が効かずアルファベット1文字ずつ（「わいおーゆーてぃーゆーびーいー」等）読み上げられてしまう問題の修正。
- **調査と真因特定**:
  1. `src/utils/text_cleaner.py` の `_RE_ENGLISH_WORD = re.compile(r'\b[a-zA-Z]{2,}\b')` において、Python 3 の `\b` はひらがな・カタカナ・漢字などの日本語文字も `\w` として扱うため、日本語が隣接すると単語境界が成立せず `convert_english_to_kana()` が素通りしていた（単体 `Youtube` は `ユーチューブ` になるが、`Youtubeが突然` は `Youtubeが突然` のまま残存することを実機確認）。
  2. `TikTok` 等が `COMMON_ACRONYMS` および `alkana` 辞書に未収録であり、即座のカタカナ変換が行われず AI 辞書提案（WebUI承認待ち）に回っていた。
- **実施内容**:
  1. `src/utils/text_cleaner.py`:
     - `_RE_ENGLISH_WORD` を `re.compile(r'(?<![a-zA-Z0-9_])[a-zA-Z]{2,}(?![a-zA-Z0-9_])')` へ変更。日本語との境界では確実に英単語としてマッチさせつつ、前段（Step 4.5）で置換される NG ワードプレースホルダ `<<__BEEP:N__>>`（アンダースコア隣接）や `mp3` / `abc123` 等の英数字混合語の部分切り出しを防止。
     - `COMMON_ACRONYMS` へ `tiktok` (`ティックトック`), `instagram`, `github`, `chatgpt`, `openai`, `netflix`, `amazon`, `spotify`, `obs`, `vtuber`, `sns`, `api`, `bgm`, `gg`, `afk`, `brb`, `tps`, `mmo`, `pvp`, `pve`, `rta`, `ssd`, `hdd`, `os`, `vr`, `ar`, `iphone`, `ipad`, `ios`, `android`, `xbox`, `valorant`, `minecraft`, `fortnite`, `lol`, `vrchat`, `niconico`, `bilibili`, `reddit`, `wikipedia` 等の頻出SNS・IT・ゲーム略語を追加。
  2. `tests/test_text_cleaner.py`:
     - `TestEnglishKanaConversion` を追加し、日本語隣接変換（`Youtubeが突然` / `TikTokが突然` / `DiscordとGitHubとChatGPTをOBSで配信`）、`<<__BEEP:3__>>` 非破壊および英数字混在保護、日本語隣接時の未知英単語検知（`XYZABCが来た`）の単体テストを追加。
- **検証・本番反映結果**:
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh python3 -m unittest discover tests`) にて全 217 件の単体テストが合格（`Ran 217 tests in 11.788s OK`、一時コンテナ自動破棄・残骸なしを確認）。
  - `docker compose up -d --no-deps --force-recreate app` により 1 回限りの最小影響切り替えを実施し、`HEALTHY at 19s`、`/health` HTTP 200 OK（`"status": "ok"`, `"is_ready": true`, `"guilds_count": 2`）、全 5 台の Bot 接続、および `Fast Recovery` による通話中 VC（`わっしょい` / `どっこいしょ`）自動復元とエラーなしを確認。
  - 本番コンテナ (`ttsbot-app-1`) 上での実機確認にて、`TextCleaner().clean(...)` が `Youtubeが突然 -> ユーチューブが突然`、`TikTokとか -> ティックトックとか`、`禁止語YouTubeとTikTok` (`ng_words=['禁止語']`) `-> <<__BEEP:3__>> ユーチューブとティックトック` となることを確認完了。

## 2026-11-14 07:31 自動参加チャンネル・ペアリング単位の翻訳オプション（翻訳リプライ＋多言語日本語訳読み上げ）実装
- **背景・目的**:
  - 日本人中心の通常雑談チャンネルでの誤判定や翻訳待ち遅延を回避しつつ、指定された自動参加ペアのチャンネルに限り、翻訳リプライ追記に加えて「非日本語（英語・韓国語・中国語等）の投稿を Hayamimi 経由で日本語化して日本語音声で読み上げる」機能を実現する。
- **実施内容**:
  1. `src/stt/hayamimi_client.py`:
     - `should_translate_for_japanese_tts()` を実装（ひらがな・カタカナ含む文、`www` 笑い表現、サーバー単語辞書登録語、および実機検証で中国語誤判定が判明した 1〜2文字の漢字熟語「了解」「開始」等を即座に日本語ルートへバイパスし 0ms 遅延保護）。
     - `extract_translation_output()` 共通ヘルパーを実装。
  2. `src/database.py` & `src/web/server.py`:
     - `normalize_auto_join_pairs()` 共通ヘルパーを実装し、`auto_join_pairs` 各要素へ `translate_enabled`（デフォルト `False`）および `translate_lang`（デフォルト `"en"`）を追加して下位互換・サニタイズ・永続化を統一。
     - `/api/config` GET/POST へ両フィールドを連携。
  3. `src/web/templates/index.html` & `src/web/static/app.js`:
     - 「自動参加チャンネル・ペアリング設定」の各ペア行へ `🌐 翻訳` チェックボックス（デフォルト OFF）と 8言語セレクトを追加し、読み込み・保存・解除に対応。
  4. `src/cogs/vc_handler.py` & `src/cogs/commands.py`:
     - `_resolve_channel_translation_config()` によりペア単位設定をギルド全体設定より優先解決。
     - `_enqueue_tts_message()` / `_send_translation_reply()` / `_process_foreign_message_translation_and_tts()` を実装し、ペア翻訳 ON チャンネルの非日本語投稿を Hayamimi `POST /api/translate` で日本語訳して日本語音声で読み上げ＆`🌐 [JA]` リプライ追記（翻訳 API 失敗時は原文ベース読み上げへ安全フォールバック）。
     - `/translate status` にペア別翻訳有効チャンネル一覧表示を追加。
  5. `tests/test_hayamimi_stt.py`:
     - `should_translate_for_japanese_tts`、`auto_join_pairs` 正規化/DB永続化、ペア翻訳 ON/OFF チャンネルごとの多言語→日本語読み上げ・日本語原文即時読み上げ・エラー時フォールバックの単体テストを追加。
- **検証・本番反映結果**:
  - `node --check src/web/static/app.js` 構文エラーなし。
  - 一時サンドボックスコンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh python3 -m unittest discover tests`) にて全 214 件の単体テストが合格（`Ran 214 tests in 10.978s OK`、コンテナ自動破棄済み）。
  - 通話中 0 人のアイドル状態で `docker compose up -d --no-deps --force-recreate app` により切り替え、`/health` 200 OK および全 5 台の Bot 正常接続を確認。

## 2026-11-14 07:17 STT 翻訳言語選択肢化 (`/stt start`) ＆ TTS 投稿テキスト双方向自動翻訳表示 (`/translate` / WebUI) の実装
- **背景・目的**:
  - STT (`/stt start`) のリアルタイム翻訳言語 (`trans_langs`) を、自由入力ではなく Hayamimi Cloud が対応する既定8言語（英語 `en`, 中国語簡体 `zh`, 台湾語/繁体 `zh-tw`, 韓国語 `ko`, スペイン語 `es`, フランス語 `fr`, ドイツ語 `de`, ポルトガル語 `pt`）から選択肢として選べるようにする。
  - TTS 読み上げ時、日本語読み上げ音声には一切影響を与えずに、投稿されたテキストメッセージの下へ Hayamimi テキスト双方向翻訳 API (`POST /api/translate`) を用いて翻訳結果を通知なしリプライ表示する機能を追加する。
- **実施内容**:
  1. `src/stt/hayamimi_client.py`:
     - 対応8言語定数 (`HAYAMIMI_TRANSLATE_LANGUAGES` / `HAYAMIMI_VALID_LANG_CODES`)、`normalize_hayamimi_lang`、`resolve_stt_trans_langs`、`resolve_hayamimi_translate_url`、`prepare_text_for_translation`（メンション展開、URL・カスタム絵文字除去、URLのみ/絵文字のみ/数字記号のみスキップ）を実装。
     - 永続 `aiohttp.ClientSession` プーリングを用いる非同期翻訳クライアント `HayamimiTranslator` を実装。
  2. `src/database.py`:
     - `guild_settings` テーブル定義および自動マイグレーションに `tts_translate_enabled` (INTEGER DEFAULT 0) と `tts_translate_lang` (TEXT DEFAULT 'en') を追加し、`get_guild_settings` / `update_guild_settings` へ永続化対応を組み込み。
  3. `src/cogs/commands.py`:
     - `/stt start` の `trans_langs`（翻訳なし＋8言語）および新規 `trans_lang_2`（指定なし＋8言語、最大2言語同時指定対応）へ `@app_commands.choices` を適用し、`/stt status` へ稼働中翻訳言語表示を追加。
     - `/translate status` および `/translate set`（管理者/管理者ロール専用、8言語選択式）を新設。
  4. `src/cogs/vc_handler.py`:
     - `VCHandlerCog.__init__` / `cog_unload` で `HayamimiTranslator` のライフサイクル管理を追加。
     - `on_message` の再生キュー投入完了後に完全非同期バックグラウンドタスク `_translate_and_reply_message` を発火（TTS 再生レイテンシ影響ゼロ）。日本語入力→設定言語へ翻訳、他言語入力→自動判定で日本語 (`ja`) へ翻訳し、NGワード検知・無視プレフィックス・URLのみ・原文と同一テキストを除外の上で `message.reply(..., mention_author=False, allowed_mentions=discord.AllowedMentions.none())` として `🌐 [LANG] 翻訳文` を投稿。
  5. `src/web/server.py`, `src/web/templates/index.html`, `src/web/static/app.js`:
     - `/api/config` GET/POST へ `tts_translate_enabled`, `tts_translate_lang` を追加し、WebUI「サーバー別ボット設定」へ有効化チェックボックスと8言語ドロップダウンを追加。早耳 Live 設定欄のヒントへ `zh-tw` 等8言語の案内を追加。
  6. `tests/test_hayamimi_stt.py`:
     - `TestHayamimiTranslation`（ヘルパー・選択肢検証、HTTP POST クライアント、SQLite 設定永続化、VCHandler 双方向翻訳リプライ＆NGワード/URLガード、CommandsCog `/translate` テスト）を追加。
- **検証・本番反映結果**:
  - サンドボックス隔離コンテナ (`IMAGE=madpiero/ttsbot:latest ./scripts/run_sandbox_test.sh python3 -m unittest discover tests`) にて全 211 件のテストが合格（`Ran 211 tests in 12.611s OK`、一時コンテナ自動破棄済み）。
  - `node --check src/web/static/app.js` 構文チェックエラーなし。
  - 0人通話中のアイドル状態を確認のうえ `docker compose up -d --no-deps --force-recreate app` で本番コンテナ (`ttsbot-app-1`) を最小限切り替え、`healthy` 復帰、`Database initialized`、`Successfully synced 14 slash commands.`、`guild_settings` 新カラム作成 (`DB_COLS_OK: True`) を確認。
  - 本番コンテナ内から `HayamimiTranslator` を用い、日本語→`en`/`zh-tw` (`latency_ms=410.4`) および英語→日本語自動翻訳 (`latency_ms=455.0`) の両方向で `/api/translate` 実機疎通を確認完了。

## 2026-11-13 12:54 早耳 Live (Hayamimi) Discord UDP Transport 暗号化復号欠落の修正・ターゲット分離強化・再起動自動復旧実装・出力先Ch明示改善
- **背景・目的**:
  - 本番 VC（「喫煙所　全体VC」）にて `/stt start` 実行後に「発話してもチャットに出てこない」「音声がちゃんとHayamimiに飛んでいないのではないか」「そもそも出力先のChが合っているか」との報告に対応。
- **調査と真因特定**:
  1. **Discord Voice Transport 暗号化 (`aead_xchacha20_poly1305_rtpsize`) の復号欠落（主因）**:
     - `discord.py` の `SocketReader` リスナーが受け取る UDP パケットは、Transport Layer（`state.mode = aead_xchacha20_poly1305_rtpsize` と `state.secret_key`）で AEAD 暗号化された状態だったが、`VoiceStreamReceiver._on_rtp_packet` がこれを復号せずにヘッダスライスのみ行って直接 DAVE / Opus デコーダへ渡していた。
     - その結果、暗号文バイナリを Opus デコードしようとして `corrupted stream` エラーや爆音ホワイトノイズが発生し、早耳 Live の Silero VAD で人間の発話として認識されず `Transcript` が一切返却されていなかった。
  2. **DAVE E2EE 誤マッピング・ターゲット外セッション残留**:
     - `dave.set_passthrough_mode(True, 120)` の強制上書きにより非ターゲット話者のパケットも通過し、`Opcode 5`（SPEAKING）到着前の別 SSRC が誤ってターゲット話者に紐付けられ、後から `Opcode 5` でターゲット外と判明してもセッションが破棄されずに残存していた。
  3. **出力先テキストチャンネルの可視性不足と再起動継続性**:
     - DB 上の紐付け（VC `1225256556577423496` -> テキストチャンネル `#喫煙所-全体vc聞き専` (`1225267423259787305`)）自体は一致していたが、`/stt start` の応答 Embed に投稿先チャンネルが表示されていなかった。
- **実施内容**:
  1. `src/stt/voice_receiver.py`:
     - `_decrypt_transport_rtp` を実装し、`aead_xchacha20_poly1305_rtpsize` / `aead_aes256_gcm_rtpsize`（`rtpsize` 仕様の拡張ヘッダ・AAD・拡張ボディ除去および RFC 3550 パディング除去対応）および `xsalsa20_*` 系モードの完全復号を実装。
     - 強制 `set_passthrough_mode(True, 120)` を撤去し、`Opcode 5/12` で後からターゲット外話者と確定した場合に誤作成セッションを即時停止・破棄する処理を追加。
     - `📊 [RTP Stats]` に `Decoded`, `DecErr`, `TransErr`, `DaveErr`, `Mode` の統計指標を追加。
  2. `src/stt/manager.py` & `src/cogs/vc_handler.py`:
     - `STTManager` の稼働中セッションを `system_kv` (`active_stt_sessions`) に永続化し、再起動・障害復旧による `restore_active_vcs()` 完了後に VC に人間が存在すれば自動で STT を再開する `restore_persisted_sessions_for_bot` を実装。
     - 開始ログに出力先テキストチャンネル名・ID を `INFO` 出力するように改善。
  3. `src/cogs/commands.py`:
     - `/stt start` に任意の出力先を指定できる `channel` 引数を追加し、開始 Embed および `/stt status` Embed へ実際の投稿先チャンネル (`<#channel_id>`) を明示表示。
  4. `tests/test_hayamimi_stt.py`:
     - AEAD (`aead_xchacha20_poly1305_rtpsize` + RTP 拡張ヘッダ) 復号テスト、`Opcode 5` 非ターゲットセッション破棄テスト、セッション永続化＆再起動復元テストを追加。
- **検証・本番実証結果**:
  - サンドボックス隔離コンテナ (`./scripts/run_sandbox_test.sh`) にて全 206 件の単体テストが 100% 合格（一時コンテナ自動破棄済み）。
  - 本番コンテナ (`ttsbot-app-1`) を最小限の切り替えで更新後、DB からの VC & STT 自動復元が正常動作。
  - 実 VC 発話にて `Mode=aead_xchacha20_poly1305_rtpsize`, `Decoded=331 DecErr=0 TransErr=0 DaveErr=0` となり、早耳 Live から `latency=156.8ms` / `272.1ms` で確定字幕（「まだ迷ってる。」「画面をスマホを見たらね。ついついこと。」）を受信し、`#喫煙所-全体vc聞き専 (1225267423259787305)` へリアルタイム投稿されたことを本番実機で確認完了。


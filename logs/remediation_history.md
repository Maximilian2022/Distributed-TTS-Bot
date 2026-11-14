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


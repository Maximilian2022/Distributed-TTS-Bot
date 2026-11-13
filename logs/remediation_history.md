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


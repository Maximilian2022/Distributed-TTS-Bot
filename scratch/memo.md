# 緊急地震速報（EEW）誤報・取消報の挙動調査メモ

## 発生事象 (2026-10-16 12:40〜12:49)
1. 気象庁より緊急地震速報（千島列島 M9.9・誤報）が発表される。
2. その後、12:48頃に気象庁より緊急地震速報の取消報（キャンセル報）が発表される。
3. TTS Botの挙動:
   - 12:48:58に `⚠️ 【緊急地震速報 (警報)】強い揺れに警戒してください！` (code 556) を検知・発報
   - 12:48:59に `⚠️ 【緊急地震速報】緊急地震速報が発表されました` (code 554) を検知・発報
   - 結果として、取り消し報であるにもかかわらず「強い揺れに警戒してください」と2回連続で警報が読み上げられた。

## 原因分析
### 1. `src/disaster/parser.py` (`parse_eew_event`)
- P2P地震情報 API (code 556) は取消報の場合に `cancelled: true`, `areas: null`, `earthquake: null` を返す。
- しかし現在のパーサーは `cancelled` フラグを一切参照しておらず、`code == 556` であれば無条件に警報文（強い揺れに警戒してください）を返していた。
- また、`earthquake`（震源地・マグニチュード等）や `areas`（対象地域）の中身を一切解析しておらず、固定テンプレートのみを返していた。

### 2. `src/disaster/service.py` (`_create_alert_embed`, `_check_p2p_quake`)
- Discord送信用のEmbedも固定テキストであり、取消情報や震源情報が載らない。
- code 556（詳細）と code 554（検出）が同一事象でほぼ同時に届いた際に、両方とも発報してしまい二重読み上げとなっていた。

## 改善方針（相談・提案項目）
1. `cancelled: true` の判定を追加し、取消報として「【緊急地震速報（取消）】先ほどの緊急地震速報は取り消されました」と読み上げ＆Embed表示。
2. `earthquake` や `areas` を解析し、震源地情報などを盛り込めるようにする（読み上げ遅延を防ぐため簡潔に）。
3. 同一イベント（`eventId` 等）での 556 / 554 の重複アナウンス抑制。

---

# MasterDebian ttsworker-agent 再起動ループ障害調査メモ (2026-10-18)

## 発生事象
- MasterDebian (192.168.11.248) にて、`ttsworker-agent` コンテナが `Restarting (1)` ループに陥っている。

## 原因調査
- コンテナログ:
  `OSError: [Errno 98] error while attempting to bind on address ('0.0.0.0', 50030): [errno 98] address already in use`
- ポート調査:
  `lsof -i :50030` / `ss -tanp | grep 50030`
  `ESTAB 192.168.11.248:50030 -> 172.237.28.183:443 users:(("tailscaled",pid=1244,fd=27))`
- 真因:
  - Linuxのデフォルトエフェメラルポート範囲が `32768-60999`。
  - `ttsworker-agent` は `network_mode: host` かつ固定ポート `50030` を使用。
  - tailscaled のアウトバウンドHTTPS通信に、カーネルが空きエフェメラルポートとして `50030` を自動割り当てした。
  - このため `0.0.0.0:50030` への bind に失敗し、コンテナが再起動ループとなった。

## 対応方針
1. 暫定対処:
   - 競合ソケット切断または tailscaled 一時再起動によりポート 50030 を解放。
   - `ttsworker-agent` の正常稼働（Up状態）を確認。
2. 真因調査:
   - エフェメラルポート重複発生のメカニズムおよび影響範囲（MasterUbuntu、他workerポート 50021/50025/50031等）を検証。
3. 恒久対処:
   - カーネルパラメータ `net.ipv4.ip_local_reserved_ports` に固定ポート群（50021,50025,50030,50031）を永続登録。
   - `setup-worker.sh` への予約設定の組み込み。

---

# 防災速報通知の音量設定機能追加 (2026-10-21)

## 現状の仕様調査結果
- `src/cogs/vc_handler.py` の `speak_alert`:
  `QueueItem` 生成時に `volume=1.0` がハードコードされており、常に100%（最大音量基準）で固定再生されていた。
- ギルド設定テーブル（`guild_settings`）や WebUI の設定画面（オプション・防災速報）には音量設定が存在しなかった。

## 実装計画
1. **DB層 (`src/database.py`)**:
   - `guild_settings` テーブルに `disaster_volume REAL DEFAULT 1.0` カラムをマイグレーション追加。
   - `get_guild_settings`, `update_guild_settings` で `disaster_volume` (0.0〜2.0) をサポート。
2. **VC再生層 (`src/cogs/vc_handler.py`)**:
   - `speak_alert(guild, text, volume: float = 1.0)` に `volume` 引数を追加し、`QueueItem(..., volume=vol)` に適用。
3. **速報サービス層 (`src/disaster/service.py`)**:
   - `dispatch_alert` 内で `guild_settings` の `disaster_volume` を取得し、`vc_cog.speak_alert(..., volume=alert_vol)` に渡す。
4. **Web API層 (`src/web/server.py`)**:
   - `/api/config` (GET/POST) および `/api/disaster/config` (POST) で `disaster_volume` を入出力・保存。
5. **WebUI層 (`src/web/templates/index.html` & `src/web/static/app.js`)**:
   - 設定モーダルの防災速報セクションに音量スライダー（0%〜200%、プリセットボタン 50%/80%/100%/120%/150%）を設置。
   - スライダー変更時の数値更新および設定保存/読み込み処理を実装。
6. **Discordコマンド層 (`src/cogs/commands.py`)**:
   - `/disaster status` で音量表示を追加。
   - `/disaster set` で `volume` (0〜200%) パラメータを追加。
7. **テスト & 本番反映**:
   - `./scripts/run_sandbox_test.sh` による非破壊テスト。
   - `./scripts/rolling_update.sh` による無停止Blue-Greenデプロイ。
   - `./logs/remediation_history.md` への記録。

---

# 汎用OSS化プロジェクト (ttsbot-oss) 開発・移行引き継ぎメモ (2026-10-21)

## 現況ステータス
- **本番稼働**: 現本番コンテナ（`ttsbot-green`, `ttsbot-router`, `ttsbot-tunnel`）は無影響で完全稼働中。
- **新環境（`/root/ttsbot/ttsbot-oss`）**:
  - Task 1〜Task 8 までの全機能実装 & 一時コンテナ（ポート 8095）での実機テスト検証完了。
  - クローン直後のトークン0件起動、パスワードログイン、Bot動的プール（最大10台）、Webクレデンシャル登録・ホットリロード、Outbound WebSocketリバース接続（Worker NAT越え）、Edge-TTS / OpenJTalkによる完全無料合成、Docker Compose プロファイル化を完備。
  - Git コミット: `7ae2d7c` 済み。

## 次回再開時の手順（Step 3: データ移行とSide-by-Side切替）
- 詳細なステップバイステップ手順は `ttsbot-oss/scratch/remaining.md` に記載済み。
- 作業の流れ:
  1. **本番DB WALチェックポイント & コピー**:
     `sqlite3 /root/ttsbot/data/bot.db "PRAGMA wal_checkpoint(TRUNCATE);"`
     `cp /root/ttsbot/data/bot.db /root/ttsbot/ttsbot-oss/data/bot.db`
  2. **データ移行ワンライナー実行**:
     `ttsbot-oss/scratch/remaining.md` の Task 9-1 に記載された Python スクリプトを実行し、既存の単語辞書・ユーザー設定を保持したまま `bot_tokens` と `system_credentials` を生成。
  3. **本番切替 (ダウンタイム約30秒)**:
     - 旧本番停止: `cd /root/ttsbot && docker compose stop ttsbot-green ttsbot-router ttsbot-tunnel`
     - 新本番起動: `cd /root/ttsbot/ttsbot-oss && docker compose --profile tunnel up -d`
  4. **緊急ロールバック手順 (1分以内復帰)**:
     - 異常時は即座に新環境停止: `cd /root/ttsbot/ttsbot-oss && docker compose --profile tunnel down`
     - 旧本番再開: `cd /root/ttsbot && docker compose start ttsbot-green ttsbot-router ttsbot-tunnel`


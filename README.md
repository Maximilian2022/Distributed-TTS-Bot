# Distributed-TTS-Bot

<div align="center">

![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)
![Discord](https://img.shields.io/badge/Discord-pycord-5865F2.svg)

**分散コンピュート・超低遅延ストリーミング対応の次世代 Discord 音声合成（TTS）Bot**

[日本語ドキュメント](README.md) | [English Documentation](README.en.md)

</div>

---

## 📖 概要

**Distributed-TTS-Bot** は、自然な日本語音声でテキストチャットを高速読み上げする、Discord 専用の超低遅延音声合成 Bot です。

単一サーバーでの個人利用はもちろん、複数台の PC や GPU マシンを束ねる**分散コンピュート・クラスタリング**、参加者のゲーミング PC を活用する**ボランティア Worker**、複数ボイスチャンネルを同時にカバーする**動的 Bot スロット**、さらに Master/Slave の**無停止 HA 冗長化機構**を備えています。

```mermaid
graph TD
    User["Discord ユーザー<br>(テキストチャット)"] --> Discord["Discord Gateway / VC"]
    Discord --> Bot["<b>Distributed-TTS-Bot (Manager)</b><br>・先行ストリーミング制御 (20ms)<br>・スマート文分割 (30文字)<br>・投機的並列合成 & キャッシュ"]

    subgraph "分散コンピュート・ワーカープール"
        LocalFast["ローカル軽量エンジン<br>高速メモリ合成 (Edge-TTS / Open JTalk)"]
        GPUWorker["GPU推論サーバー (クラスタ)<br>高品質AI音声 (VOICEVOX / SHAREVOX / COEIROINK)"]
        WorkerVol["ボランティア Worker (参加者PC)<br>WebSocket接続 (ポート開放不要)"]
        Cloud["クラウド多段フォールバック<br>Azure / GCP / Edge-TTS"]
    end

    Bot -->|即時合成| LocalFast
    Bot -->|専用クラスタ連携| GPUWorker
    Bot -->|話者モデル自動判定| WorkerVol
    Bot -.->|遅延・タイムアウト時| Cloud

    LocalFast --> Stream["20ms Opus フレーム<br>リアルタイム再生"]
    GPUWorker --> Stream
    WorkerVol --> Stream
    Cloud --> Stream
    Stream --> Discord
```

---

## ✨ 主な機能

### 1. ⚡ 超低遅延・先行ストリーミング再生
- **多段チャンキング & 30文字ハードキャップ**: 口語文、スペース区切り、ネットスラング（`w`, `草`）、終助詞境界をミリ秒単位で賢く識別。句読点のない長文チャットでも 30 文字以内で安全にスライスして先行並列合成。
- **20ms フレーム直接送出**: 合成完了を待たずに生成された最初の音声チャンクから即座に Discord VC へストリーミング再生を開始し、発言から再生開始までの待機時間を極限まで削減。

### 2. 🖥️ 分散コンピュート & ボランティア Worker
- **ポート開放不要のアウトバウンド接続**: 一般メンバーの Windows/Linux PC から、WebSocket（WSS）経由で手軽にコンピュートノードとしてクラスタに参加可能。
- **スマート・モデルルーティング**: ボランティア機が保有しているモデル（COEIROINK の MYCOEIROINK や公式話者）を自動検知し、該当モデルのリクエストのみをピンポイントで振り分け。
- **ゲーム連動低負荷モード**: ゲーミング PC の邪魔にならない低プロセスピリオティ設定、無通信時の自動コンテナ停止（メモリ返却）に対応。

### 3. 🎯 4段階 動的 Tier ルーティング（エンジン毎の優先順位制御）
接続されているワーカーのハードウェア性能（GPU の有無、RAMDisk 常駐モデル、ストレージ速度）を自動プロファイルし、最もレイテンシの低い最適なノードへタスクを動的割り当て：
- **Tier 1: GPU + RAMDisk (最速・即時応答)**: GPU 高速推論 & メモリ常駐によるディスク I/O ゼロの超低遅延合成（デフォルト最優先）。
- **Tier 2: CPU + RAMDisk (高速・GPU温存)**: メモリ常駐モデルによる高速推論。GPU 負荷を温存しつつ瞬時に応答。
- **Tier 3: GPU + SSD (中速・大容量モデル)**: GPU パワーをフル活用した高品質 AI 音声合成。
- **Tier 4: CPU + SSD (汎用・安定フォールバック)**: 最下位フォールバック用。
- **🎛️ WebUI 動的並び替え**: 「Bot & ノード管理」画面から、エンジン毎（COEIROINK, VOICEVOX, SHAREVOX 等）に ▲/▼ ボタンで優先順位をリアルタイムにカスタマイズ可能。


### 4. ☁️ 多段クラウドフェイルオーバー（Edge-TTS ファースト）
- **初期設定不要**: 高価な GPU や API キーがなくても、無料の **Edge-TTS** または **Open JTalk** ですぐに高品質な音声合成が動作。
- **自動フォールバック**: ローカルエンジンや Worker に高負荷・障害が発生した場合、瞬時にクラウド API（Edge-TTS / Azure / GCP）へフォールバックし、通話を一切途切れさせません。

### 5. 🛡️ 完全無停止 HA（高可用性）冗長化
- **Master/Slave 自動昇格**: 主系（Master）の死活を常時監視。障害検知時に副系（Slave）が自動昇格して通話を継続。
- **Split-Brain 防護**: 分断された旧 Master からの自律降格（HTTP 410 Gone 自律停止）により多重稼働事故を完全防止。

### 6. 🎛️ 直感的な WebUI 管理ダッシュボード
- ブラウザからギルドごとの読み上げ設定、ユーザー個別辞書、Bot 稼働スロット（1〜10号機）、分散 Worker の稼働状態を一元管理。
- クラスタ追加用のワンライナー（PowerShell / Bash）を画面からワンクリックでコピー可能。

### 7. 🚫 強力な NG ワード & 配信事故防止（モデレーション）
- **リアルタイム伏字置換**: 登録された不適切語・暴言・センシティブな単語をミリ秒で検知し、`＊＊＊` へ安全に自動置換して読み上げ。
- **最長一致アルゴリズム & 高速インメモリ判定**: 単語長順のスマートスキャンにより部分一致の誤爆を徹底防止。サーバーごとのインメモリキャッシュにより読み上げ遅延はゼロ。
- **標準プリセット一括登録**: ワンコマンド・ワンクリックで荒らし・スパム対策用の推奨NGワードリストを即時インポート可能。
- **WebUI & スラッシュコマンド両対応**: Discord チャット（`/ng`）からも管理画面（GUI）からも直感的に追加・削除・一覧確認が可能。配信事故や荒らし対策に威力を発揮します。

---

## 🚀 クイックスタート

### 1. 必要要件
- **Docker / Docker Compose**（Linux / macOS / Windows Docker Desktop）
- **Discord Bot Token**（[Discord Developer Portal](https://discord.com/developers/applications) から取得）

### 2. インストール手順

```bash
# 1. リポジトリのクローン
git clone https://github.com/Maximilian2022/Distributed-TTS-Bot.git
cd Distributed-TTS-Bot

# 2. 環境設定ファイルの準備
cp .env.example .env

# 3. .env の編集 (Bot Token や管理者パスワードを設定)
nano .env

# 4. コンテナのビルドと起動
docker compose up -d --build
```

起動後、ブラウザで `http://localhost:8080`（またはサーバーのホスト名・IP）にアクセスすると管理ダッシュボードが開きます。

---

## 💬 主な Discord スラッシュコマンド

| コマンド | 説明 |
| :--- | :--- |
| `/join` | コマンドを実行したボイスチャンネルに Bot を呼び出します |
| `/leave` | Bot をボイスチャンネルから退出させます |
| `/voice` | 声色（話者）・読み上げ速度・ピッチ（音高）の変更メニューを表示します |
| `/dict add <単語> <読み>` | サーバー共通の単語辞書に新しい読みを登録します |
| `/dict delete <単語>` | 登録済みの単語を辞書から削除します |
| `/userdict` | ユーザー個人の専用単語辞書を設定します |
| `/ng add <単語>` | NGワードを登録（読み上げ時に自動で伏字化 `＊＊＊`） |
| `/ng remove <単語>` | 登録済みのNGワードを解除 |
| `/ng list` | サーバーで登録されているNGワード一覧を表示 |
| `/ng import_default` | 荒らし・暴言対策の標準NGワードプリセットを一括登録 |
| `/help` | コマンド一覧と詳しい使い方を表示します |

---

## 🔄 安全なシステムのアップデート手順 (Safe Zero-Downtime Update)

本番運用中の通話を中断させないよう、**事前イメージ取得**と**HAフェイルオーバー保護**を備えた安全なアップデートスクリプトが用意されています。

### 推奨: ワンライナーまたはスクリプト実行 (約2秒で完了)

```bash
# リポジトリ内のスクリプトを実行 (推奨・最速)
./update.sh

# または Web 配信ワンライナーで実行
curl -sSL "https://<YOUR_DOMAIN>/update.sh" | sudo bash
```

### スクリプトの安全保護メカニズム
- ⚡ **事前Pullによる最小瞬断（約2秒）**: 稼働中のコンテナを停止させずに裏で Docker Hub から最新イメージをダウンロード。コンテナ再作成時の切り替え停止時間をわずか約2秒に短縮します。
- 🛡️ **HA誤昇格の完全防止 (Split-Brain Guard)**: 更新開始時にシグナル（`data/.ha_updating`）を発行。相手側の Standby Slave が「Master ダウン」と誤認して勝手に昇格（Split-Brain）する事故を自動で防ぎます。
- 🤖 **ボランティア Worker の自動追従**: Master 本体の更新完了と同時に、接続中の全ボランティア Worker へ WebSocket で自動更新通知を発信。ワーカーも自動的に最新コードへ再起動します。
- 💾 **データベース自動バックアップ**: アップデート前に `data/` 配下の SQLite データベースおよび設定ファイルを `data_backup/` へ自動退避します。

#### オプション機能:
```bash
./update.sh --build       # Docker Hub イメージではなくローカル Dockerfile からビルド
./update.sh --clean       # アップデート後に不要となった古い Docker イメージを自動削除
./update.sh --no-cache    # キャッシュを完全破棄してビルド (--build と併用)
```

---

## 🖥️ Worker ノードの追加方法

WebUI ダッシュボードの「Bot & ノード管理」タブから、専用のワンライナーを取得して簡単に Worker を追加できます：

### 1. 常時稼働クラスタ用 Worker (Docker)
```powershell
# Windows (Docker Desktop / 管理者 PowerShell)
irm "https://<YOUR_DOMAIN>/setup-worker.ps1" | iex
```
```bash
# Linux (Debian / Ubuntu / Docker)
curl -sSL "https://<YOUR_DOMAIN>/setup-worker.sh" | sudo bash
```

### 2. ボランティア Worker (個人 PC / ポート開放不要)
```powershell
# Windows ネイティブ GUI ランチャー
$env:TTSBOT_SERVER_URL="wss://<YOUR_DOMAIN>/api/worker/ws"; $env:TTSBOT_WORKER_TOKEN="<YOUR_TOKEN>"; irm "https://<YOUR_DOMAIN>/volunteer/run_volunteer_gui.ps1" | iex
```

---

## 📜 ライセンス & 免責事項

本ソフトウェアは **[Apache License, Version 2.0](LICENSE)** のもとで公開されています。

サードパーティ製ライブラリの帰属表示、同梱モデル（MMDAgent Mei/Takumi）、および音声合成エンジン（VOICEVOX, COEIROINK, SHAREVOX）のキャラクター利用ガイドラインに関する免責事項については、**[NOTICE.md](NOTICE.md)** をご確認ください。

- [行動規範 (Code of Conduct)](CODE_OF_CONDUCT.md) ([English](CODE_OF_CONDUCT.en.md))
- [コントリビューションガイド (Contributing Guide)](CONTRIBUTING.md) ([English](CONTRIBUTING.en.md))
- [セキュリティポリシー (Security Policy)](SECURITY.md) ([English](SECURITY.en.md))

# Debian / Linux コンピュートノード (ワーカー) セットアップ手順書

本手順書は、共用サーバー等の Debian / Linux マシンを **音声合成専用のコンピュートワーカー（Compute Node）** として参加させ、Master サーバーと自動連携させるためのセットアップガイドです。

現在、セットアップは**ワンライナーで完全自動化**されており、手動でのファイル転送（scp）やモデル同期（rsync）、設定ファイル編集は一切不要です。

---

## 💡 共用マシンへの配慮・リソース制御仕様
Debian マシンが他の用途と共用であることを考慮し、自動生成される設定で以下のリソースガードが標準適用されています：
* **CPU 使用上限**: 最大 2 コア分（`cpus: "2.0"`）に制限（他プロセスを邪魔しません）
* **メモリ上限**: 各コンテナ最大 2〜3 GB に厳格制限（メモリ圧迫を防止）
* **推論スレッド**: 2 スレッドに抑制（`OMP_NUM_THREADS=2`）
* **同時処理制限**: Master 側から 1 ノードあたり同時に 1 リクエストしか流さない直列制御

---

## ステップ 1: Debian 側での Docker 準備 (初回のみ)

Debian サーバーに SSH 接続し、Docker がインストールされていない場合は導入します。

```bash
# パッケージ更新
sudo apt update && sudo apt install -y curl

# Docker のインストール (公式スクリプト)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

---

## ステップ 2: ワンライナーで自動セットアップ

Debian 上で以下のコマンドを 1 行実行するだけで、すべてのセットアップが完了します。
（`<MASTER_HOST>:<PORT>` には Master サーバーの IP またはドメインを指定してください）

```bash
# LAN 内直接接続の例:
curl -sSL http://<MASTER_HOST>:8080/setup-worker.sh | bash

# ドメイン経由の例:
curl -sSL https://<YOUR_TTSBOT_DOMAIN>/setup-worker.sh | bash
```

### スクリプトが自動で行う処理
1. 作業ディレクトリ `~/ttsbot-worker` の作成
2. リソース制限付き `docker-compose.worker.yml` の自動生成
3. Worker Agent (`ttsworker-agent`) コンテナの起動
4. Master からの話者モデル自動同期（すでに同期済みのモデルは **0秒でスキップ**）
5. 各種 TTS エンジンコンテナ（VOICEVOX, SHAREVOX, COEIROINK）の自動ビルド・起動
6. Master への自動登録（Auto-Discovery）

> **※ 状態の確認**:
> Debian 上で `docker ps` を実行し、`ttsworker-agent`, `ttsworker-voicevox`, `ttsworker-sharevox`, `ttsworker-coeiroink` の 4 コンテナが起動していれば完了です。
> Master 側の WebUI「ℹ️ システム状況」タブにも即座にノードが表示されます。

---

## ステップ 3: Master 側からのリモート管理・診断

Master サーバーには、Debian Worker を遠隔管理するための SSH 共通鍵と診断 CLI が用意されています。

### 1. 診断 CLI で全ワーカーの生存確認
Master 側からいつでも一括状況確認が可能です：
```bash
/path/to/ttsbot/scripts/check_workers.sh
```

### 2. SSH 経由での直接リモート操作
[`WorkerAccess/README.md`](file:///root/ttsbot/WorkerAccess/README.md) に従って生成した SSH 秘密鍵を使ってログインやワンライナー確認ができます：
```bash
# ログイン
ssh -i /path/to/WorkerAccess/id_rsa <USER>@<WORKER_IP>

# コンテナ状態確認
ssh -i /path/to/WorkerAccess/id_rsa <USER>@<WORKER_IP> "docker ps"

# ワーカーエージェントのログ確認
ssh -i /path/to/WorkerAccess/id_rsa <USER>@<WORKER_IP> "docker logs --tail 50 ttsworker-agent"
```


# Portainer による TTSBot デプロイ・運用ガイド

本ガイドでは、Docker 管理 GUI ツール **Portainer** を使用して、TTSBot (Master / Slave) をソースコードのビルド不要で簡単にデプロイ・運用する手順を解説します。

Docker Hub に自動ビルド・Push される公式イメージ (`madpiero/ttsbot:latest`) を利用するため、`git clone` やホスト上のファイル配置を行わずに Portainer の Web 画面上だけで完結できます。

---

## 🚀 1. Master (主系) サーバーのデプロイ手順

### ステップ 1: 新規 Stack の作成
1. Portainer のダッシュボードにログインします。
2. 左メニューから **Stacks** を選択し、右上の **+ Add stack** をクリックします。
3. **Name** に `ttsbot` と入力します。

### ステップ 2: Stack 定義の入力
**Web editor** タブを選択し、リポジトリの [portainer-stack.yml](file:///root/ttsbot/portainer-stack.yml) の内容をそのまま貼り付けます：

```yaml
version: '3.8'

services:
  app:
    image: madpiero/ttsbot:latest
    container_name: ttsbot-app
    restart: unless-stopped
    ports:
      - "${HOST_WEB_PORT:-8088}:8080"
    environment:
      - WEB_PORT=8080
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}
      - TTSBOT_HA_SECRET=${TTSBOT_HA_SECRET:-}
      - CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN:-}
    volumes:
      - ttsbot_data:/app/data
      - ttsbot_logs:/app/logs
    healthcheck:
      test: ["CMD-SHELL", "python -c 'import urllib.request, os, sys; p = os.getenv(\"WEB_PORT\", \"8080\"); sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{p}/health\", timeout=3).status == 200 else 1)' 2>/dev/null || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

volumes:
  ttsbot_data:
    name: ttsbot_data
  ttsbot_logs:
    name: ttsbot_logs
```

### ステップ 3: 環境変数の設定 (任意)
エディタ下部の **Environment variables** で **+ Add an environment variable** をクリックし、必要に応じて設定します：

| 名前 | 設定例 / 説明 | 必須 |
| :--- | :--- | :--- |
| `DISCORD_BOT_TOKEN` | `MTE...` (Discord Bot トークン) | 推奨 (Webダッシュボードからも後から設定可) |
| `TTSBOT_HA_SECRET` | 任意のランダム文字列 (HA 同期用暗号化キー) | HA 冗長化を行う場合は必須 |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cloudflare Zero Trust のトンネルトークン | 外部公開・トンネル使用時 |
| `HOST_WEB_PORT` | `8088` (ホスト側のアクセスポート) | 任意 (既定値: 8088) |

### ステップ 4: デプロイ実行
最下部の **Deploy the stack** ボタンをクリックします。
自動的に Docker Hub からイメージがダウンロードされ、数秒でコンテナが起動します。

起動後、ブラウザで `http://<サーバーのIPアドレス>:8088` にアクセスすると、TTSBot 管理ダッシュボードが表示されます。

---

## 🛡️ 2. Slave (待機系 / Standby) サーバーのデプロイ手順

Master の障害時に自動昇格する Slave ノードを Portainer 上に作成する場合の手順です。

### Stack 定義 (Slave 用)
Slave 用の Stack では、監視と自動フェイルオーバーを司る `standby-watchdog` を起動します：

```yaml
version: '3.8'

services:
  standby-watchdog:
    image: madpiero/ttsbot:latest
    container_name: ttsbot-slave-watchdog
    restart: unless-stopped
    command: ["python", "-m", "src.ha_watchdog"]
    environment:
      - TTSBOT_HA_ROLE=slave
      - TTSBOT_MASTER_URL=${TTSBOT_MASTER_URL}
      - TTSBOT_HA_SECRET=${TTSBOT_HA_SECRET}
      - TTSBOT_HA_PRIORITY=${TTSBOT_HA_PRIORITY:-1}
      - TTSBOT_CHECK_INTERVAL=5
      - TTSBOT_SYNC_INTERVAL=60
      - HOST_WEB_PORT=8088
      - WEB_PORT=8080
      - CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN:-}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ttsbot_slave_data:/app/data
      - ttsbot_slave_logs:/app/logs

  # フェイルオーバー昇格時に Watchdog が自動起動する本体コンテナ
  app:
    image: madpiero/ttsbot:latest
    container_name: ttsbot-app
    restart: unless-stopped
    ports:
      - "${HOST_WEB_PORT:-8088}:8080"
    environment:
      - WEB_PORT=8080
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}
      - TTSBOT_HA_SECRET=${TTSBOT_HA_SECRET:-}
      - CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN:-}
    volumes:
      - ttsbot_slave_data:/app/data
      - ttsbot_slave_logs:/app/logs
    profiles:
      - active

volumes:
  ttsbot_slave_data:
    name: ttsbot_slave_data
  ttsbot_slave_logs:
    name: ttsbot_slave_logs
```

### 必須環境変数 (Slave)
- `TTSBOT_MASTER_URL`: Master サーバーのベースURL (例: `https://ttsbot.kenko.land`)
- `TTSBOT_HA_SECRET`: Master 側と同じ共有シークレットキー
- `TTSBOT_HA_PRIORITY`: 優先度 (`1`: 最優先 約30秒昇格, `2`: 第2待機 約3分昇格, `3`: 第3待機 約5分昇格)

> [!NOTE]
> Slave は通常時、常駐軽量プロセス (`standby-watchdog`) のみが待機し、Master と設定・辞書データを定期暗号化同期します。Master 停止を検知すると、自動的に Docker ソケットを通じて TTSBot 本体コンテナを昇格・起動させます。

---

## 🔄 3. バージョンアップ・更新手順 (ワンクリック)

GitHub の `main` ブランチに更新が入ると、GitHub Actions によって自動的に最新イメージ (`madpiero/ttsbot:latest`) が Docker Hub に公開されます。

Portainer で最新版へ更新する手順は以下の通りです：

1. Portainer 左メニューの **Stacks** を開き、`ttsbot` をクリックします。
2. **Editor** タブを開きます。
3. **Update the stack** ボタンをクリックします。
4. ポップアップ画面で **Re-pull image and redeploy** のスイッチを **ON** にします。
5. **Update** をクリックします。

> [!TIP]
> 名前付きボリューム (`ttsbot_data`) に設定ファイルや単語辞書、セッション情報が永続化されているため、コンテナを再作成してもデータが消えることはありません。

---

## 💾 4. データの永続化とバックアップ

- **設定・辞書データ**: 名前付きボリューム `ttsbot_data` (`/app/data`)
- **動作ログ**: 名前付きボリューム `ttsbot_logs` (`/app/logs`)

Portainer 左メニューの **Volumes** から `ttsbot_data` を確認・バックアップすることが可能です。

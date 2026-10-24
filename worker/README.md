# 🎙️ TTSBot Volunteer Worker (ボランティア型コンピュートワーカー)

TTSBot の音声合成ネットワークに、あなたの PC (GPU / CPU) の処理能力を提供してコミュニティを支援するための軽量エージェントです。

---

## 🌟 特徴
- **ポート開放・ルーター設定は一切不要**: クライアントからサーバーへ向けて安全な WebSocket (WSS) で接続するため、家庭用 Wi-Fi やテザリング環境でもそのまま動作します。
- **超低遅延バイナリ通信**: 生成された音声をゼロコピーバイナリで高速返送します。
- **自動復旧**: ネットワークが瞬断しても自動で再接続します。

---

## 🛠️ 前提条件
1. **Python 3.9 以上**
2. **VOICEVOX / COEIROINK / SHAREVOX** のいずれか（または複数）がローカルで起動していること
   - VOICEVOX 公式アプリ または Docker (ポート `50021`)
   - COEIROINK 公式アプリ または Docker (ポート `50031`)
   - SHAREVOX 公式アプリ または Docker (ポート `50025`)

---

## 🚀 クイックスタート

### 1. 依存ライブラリのインストール
```bash
pip install aiohttp
```

### 2. 実行
```bash
python volunteer_worker.py \
  --server-url wss://<あなたのTTSBotドメイン>/api/worker/ws \
  --token <管理者から共有されたWorkerトークン> \
  --name "Taro-Gaming-PC"
```

### 主なオプション
| オプション | デフォルト値 | 説明 |
|---|---|---|
| `--server-url` | (必須) | サーバーの WebSocket エンドポイント URL |
| `--token` | `""` | 共有認証トークン |
| `--name` | ホスト名 | ダッシュボードに表示されるノード名 |
| `--capacity` | `2` | 同時に処理する最大タスク数 |
| `--priority` | `50` | 優先度 (数値が小さいほど優先) |
| `--voicevox-url`| `http://127.0.0.1:50021` | VOICEVOX の URL |
| `--coeiroink-url`| `http://127.0.0.1:50031` | COEIROINK の URL |
| `--sharevox-url` | `http://127.0.0.1:50025` | SHAREVOX の URL |

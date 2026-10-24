# Distributed-TTS-Bot への貢献ガイド (Contributing Guide)

[日本語版](CONTRIBUTING.md) | [English Version](CONTRIBUTING.en.md)

**Distributed-TTS-Bot** への貢献をご検討いただき、ありがとうございます！  
バグ報告、新機能の提案、ドキュメントの改善、コードのプルリクエストなど、皆様からの貢献を心より歓迎します。

---

## 行動規範 (Code of Conduct)

本プロジェクトは [行動規範 (Code of Conduct)](CODE_OF_CONDUCT.md) ([English](CODE_OF_CONDUCT.en.md)) に準拠します。参加される際は、本規範の遵守をお願いいたします。

---

## 貢献の方法

### 1. バグの報告 (Reporting Bugs)

不具合を発見した場合は、まず既存の [Issues](https://github.com/Maximilian2022/Distributed-TTS-Bot/issues) に同一の報告がないか検索してください。

新しい Issue を作成する際は、以下の情報を含めてください：
- 簡潔でわかりやすいタイトル
- 不具合の再現手順
- 期待されていた動作と、実際の挙動
- 実行環境（OS、Docker バージョン、Python バージョン、GPU の有無・型番など）
- 関連するログ抜粋（※Discord Bot Token やパスワード、API キーなどの秘密情報は必ず伏字にしてください）

### 2. 機能改善・提案 (Suggesting Enhancements)

超低遅延化、音声合成エンジンの追加、WebUI の操作性向上、信頼性の強化など、あらゆる提案を歓迎します。
- GitHub Issues に機能リクエストを起票し、機能の目的やユースケースをご説明ください。
- 大規模な機能追加や破壊的変更を伴う場合は、Pull Request を作成する前に Issue でディスカッションを行うことを推奨します。

### 3. プルリクエスト (Pull Requests) の作成手順

1. **リポジトリをフォーク**: ご自身の GitHub アカウントに本リポジトリをフォークします。
2. **作業用ブランチを作成**:
   ```bash
   git checkout -b feat/your-feature-name
   # バグ修正の場合:
   git checkout -b fix/issue-description
   ```
3. **ローカル開発環境のセットアップ**:
   - リポジトリをクローン:
     ```bash
     git clone https://github.com/<your-username>/Distributed-TTS-Bot.git
     cd Distributed-TTS-Bot
     ```
   - 設定ファイルの準備:
     ```bash
     cp .env.example .env
     ```
   - 開発コンテナのビルド・起動:
     ```bash
     docker compose up -d --build
     ```
4. **コードの変更**:
   - 既存のコメントや Docstring を尊重し、可読性の高いコードを心がけてください。
   - 非同期処理（`asyncio`）のイベントループをブロックしないよう注意してください（重い CPU 処理やファイル I/O は別スレッドへ逃がす等）。
5. **動作確認・テスト**:
   - 構文エラーがないことを検証:
     ```bash
     docker compose exec app python -m py_compile src/main.py
     ```
   - WebUI および Discord Bot が正常に動作することを確認してください。
6. **コミットメッセージ (Conventional Commits 推奨)**:
   - `feat: 新しい音声合成エンジンのサポート追加`
   - `fix: WebSocket 再接続時のタイムアウト不具合を修正`
   - `docs: セットアップ手順の更新`
   - `perf: 音声チャンクのキャッシュ処理を最適化`
7. **Pull Request を送信**:
   - フォークしたリモートブランチへ push:
     ```bash
     git push origin feat/your-feature-name
     ```
   - `main` ブランチに対して Pull Request を作成します。
   - 変更の意図、動作確認した内容をテンプレートに沿って記述してください。

---

## コーディング規約・注意事項

- **Python 3.10+ / asyncio**: 本プロジェクトは最新の非同期 Python アーキテクチャで構築されています。ブロッキング I/O を避け、非同期パターンを維持してください。
- **BOM なし UTF-8**: スクリプト（.py, .ps1, .sh）やドキュメントはすべて UTF-8（BOM なし）で保存してください。
- **機密情報のコミット禁止**: Discord Token や暗号化キー、API キーを絶対にコード内にハードコードしないでください。環境変数（`.env`）から取得してください。
- **音声レイテンシへの配慮**: 本 Bot は 0.x 秒単位のリアルタイム読み上げを最優先設計としています。合成や再生パイプラインに不要なバッファ遅延を生じさせないよう配慮をお願いします。

---

## コントリビューションのライセンス

Distributed-TTS-Bot に提出されたすべての Pull Request およびコントリビューションは、[Apache License, Version 2.0](LICENSE) のもとで提供されることに同意したものとみなされます。

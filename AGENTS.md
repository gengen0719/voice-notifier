# voice-notifier の開発指針

- Phase 1 の設計とレビュー項目は [docs/architecture.md](docs/architecture.md) を参照する。
- AWS リージョンは `ap-northeast-1` 固定。既存バケット名と対象プレフィックスは `AUDIO_BUCKET_NAME` と `AUDIO_KEY_PREFIX` から取得する。
- 既存の音声生成 Lambda と S3 バケットを維持する。アプリの CDK でバケットを作成したり、通知設定を変更したりしない。
- Phase 1 のアプリは前面表示中の MP3 配信と再生を扱う。署名付き URL は短命にし、端末には AWS 認証情報を含めない。
- 設計レビューが完了するまで AWS リソースを作成・変更しない。デプロイ前に `cdk synth` と対象環境の `cdk diff` で変更範囲を確認する。

- プルリクエストのレビューは [REVIEW.md](REVIEW.md) に従い、日本語で記述する。

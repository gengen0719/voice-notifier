# Voice Notifier

TT-Agent の make-voice が S3 に保存した MP3 を、前面表示中の iPhone で自動再生します。システム構成と API は [設計書](docs/architecture.md) を参照してください。

## CDK

Node.js と AWS CLI を用意し、デプロイ先アカウントの AWS 認証を設定します。既定では、同じ AWS アカウントの東京リージョンにデプロイ済みの `TtMakeVoiceStack` が公開する `TtMakeVoice-BucketName` を参照し、プレフィックスなしで MP3 を取り込みます。

```sh
cd infra
npm ci
npm run build
npm run synth
npx cdk diff
```

東京リージョンで CDK が未ブートストラップの場合は、対象アカウントを確認して `npx cdk bootstrap aws://<AWS_ACCOUNT_ID>/ap-northeast-1` を実行します。差分確認後、`npx cdk deploy` でデプロイします。

従来の MP3 バケットを使う場合は、合成前に `AUDIO_BUCKET_NAME` と `AUDIO_KEY_PREFIX`（末尾は `/`）を両方設定します。SSE-KMS の場合は `AUDIO_KMS_KEY_ARN` も指定します。

### S3 の直接通知を接続する

デプロイ出力の `IngestFunctionArn` をバケット管理者へ渡します。適切な既存経路がある場合は、その経路から S3 の直接通知形式で取り込み Lambda が呼ばれるようにします。S3 から新規に直接接続する場合は、バケット管理者が既存通知の設定を確認し、別途承認された変更として `s3:ObjectCreated:*`、サフィックス `.mp3` の通知を追加します。TT-Agent のバケットではプレフィックスを空欄にし、従来のバケットでは指定したプレフィックスを使います。対象イベントとフィルターが既存設定に重複する場合は、既存通知を保持できる経路の確定まで追加を保留します。通知設定全体を置き換える CLI コマンドの実行には、既存設定を含めた事前レビューが必要です。

取り込み対象は経路を接続した後に届いたイベントです。TT-Agent のバケットから削除された音声は、次に再生する順番が来た時点で飛ばします。特定の音声ファイルが繰り返し再生エラーになると、後続の再生が待機します。原因を調べる際は対象のオブジェクトの存在、S3/KMS 権限、ファイル形式を確認してください。取り込みと配信の失敗は CDK 出力の `FailureQueueUrl` で調査できます。取り込みに失敗した場合は、原因解消後にキューのメッセージに含まれる元の S3 イベントを取り込み Lambda に再投入できます。配信だけが失敗した場合、iPhone は定期同期で音声を取得します。

### S3 の接続先を変更する

バケットまたはプレフィックスを変えると、新しい接続先のキューと再生完了位置が使われます。同じ接続先へ戻すと、その接続先に記録されていた再生完了位置から再開します。接続先を区分する以前の旧形式レコードには元バケットの記録がないため、自動移行の対象外です。旧形式の環境を更新する場合は、移行前に未再生音声を確認し、引き継ぐ分の元 S3 イベントを更新後の取り込み Lambda に再投入してください。

### iPhone を登録する

IAM で管理 Lambda の `lambda:InvokeFunction` を許可された管理者が、デプロイ出力の `AdminFunctionName` を指定してコードを発行します。

```sh
aws lambda invoke --region ap-northeast-1 \
  --function-name '<AdminFunctionName>' --cli-binary-format raw-in-base64-out \
  --payload '{"action":"issue"}' /dev/stdout
```

登録コードは 10 分間有効で、一回だけ利用できます。新しく発行すると未使用の前のコードは無効になります。登録の応答が届かなかった場合は、アプリの「前回の登録を再試行」を使うと同じ端末登録を復元できます。失効・端末交換には同じコマンドで `{"action":"revoke"}` を送信します。交換した端末は、DynamoDB に保存されている未再生扱いのカタログを先頭から再生します。

Xcode で [ios/VoiceNotifier.xcodeproj](ios/VoiceNotifier.xcodeproj) を開き、Signing & Capabilities で Team と必要に応じて Bundle Identifier を設定して、iOS 17 以降の iPhone で起動します。初回画面に CDK 出力の `HttpUrl`、`WebSocketUrl` と管理者が発行したコードを入力します。

### 動作確認

- アプリを前面に表示し、make-voice で MP3 を 2 件生成します。受信した音声が一件ずつ再生されます。
- iPhone の通信を切った状態で MP3 を生成し、通信を戻して前面に復帰すると未処理音声を再生します。
- 再生完了済みの音声は同じ端末の再接続後も再生されません。
- 管理 Lambda で端末を失効させると、既存ソケットが閉じ、新規 API リクエストに認証が必要になります。

## 検証の再開

Phase 1 の実装、CDK の型チェックと `cdk synth`、Xcode 27.2 での署名なし iOS シミュレータ向け Debug ビルドは確認済みです。AWS へのデプロイ、S3 通知の接続、iPhone 実機での再生は未検証です。

1. 対象の AWS アカウント、既存バケット、プレフィックス、実際の暗号化設定を確認する。
2. 上の CDK 手順で差分を確認してデプロイし、既存通知を保持して S3 の直接通知を接続する。
3. 管理 Lambda でコードを発行し、実機で端末を登録する。
4. 上の動作確認を実施し、結果と発生したエラーを記録する。再開時は最初の未完了手順から進める。

再ビルドする場合は、リポジトリ直下で次を実行します。

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project ios/VoiceNotifier.xcodeproj -scheme VoiceNotifier \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/voice-notifier-simulator-build CODE_SIGNING_ALLOWED=NO build
```

import { App } from 'aws-cdk-lib';
import { VoiceNotifierStack } from '../lib/stack';

const app = new App();
const bucketName = process.env.AUDIO_BUCKET_NAME;
const prefix = process.env.AUDIO_KEY_PREFIX;
if (!bucketName || !prefix || !prefix.endsWith('/') || /[*?]/.test(prefix)) {
  throw new Error('Set AUDIO_BUCKET_NAME and a nonempty AUDIO_KEY_PREFIX ending in /, without IAM wildcards');
}

new VoiceNotifierStack(app, 'VoiceNotifier', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'ap-northeast-1' },
  bucketName,
  prefix,
  kmsKeyArn: process.env.AUDIO_KMS_KEY_ARN,
});

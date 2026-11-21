import * as path from 'node:path';
import {
  ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack, StackProps,
  aws_apigatewayv2 as gateway, aws_apigatewayv2_integrations as integrations,
  aws_apigatewayv2_authorizers as authorizers, aws_dynamodb as dynamodb,
  aws_iam as iam, aws_lambda as lambda, aws_lambda_event_sources as sources,
  aws_lambda_destinations as destinations, aws_logs as logs,
  aws_s3 as s3, aws_sqs as sqs,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

interface Props extends StackProps {
  bucketName: string;
  prefix: string;
  kmsKeyArn?: string;
}

export class VoiceNotifierStack extends Stack {
  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);
    const bucket = s3.Bucket.fromBucketName(this, 'ExistingAudio', props.bucketName);
    const table = new dynamodb.Table(this, 'State', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      timeToLiveAttribute: 'ttl',
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const failures = new sqs.Queue(this, 'Failures', { retentionPeriod: Duration.days(14) });
    const ws = new gateway.WebSocketApi(this, 'WebSocket');
    const wsStage = new gateway.WebSocketStage(this, 'WebSocketStage', {
      webSocketApi: ws, stageName: 'v1', autoDeploy: true,
    });
    const endpoint = wsStage.url.replace('wss://', 'https://');
    const code = lambda.Code.fromAsset(path.join(__dirname, '../../backend'), {
      exclude: ['__pycache__', '**/__pycache__', 'test_*'],
    });
    const makeFunction = (id: string, handler: string, environment: Record<string, string> = {}) =>
      new lambda.Function(this, id, {
        runtime: lambda.Runtime.PYTHON_3_12,
        code, handler: 'app.' + handler,
        timeout: Duration.seconds(15),
        logGroup: new logs.LogGroup(this, id + 'Logs', {
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
        environment: { TABLE_NAME: table.tableName, ...environment },
      });

    const audio = { AUDIO_BUCKET_NAME: bucket.bucketName, AUDIO_KEY_PREFIX: props.prefix };
    const ingest = makeFunction('Ingest', 'ingest', audio);
    table.grantReadWriteData(ingest);
    ingest.addPermission('ExistingS3', {
      principal: new iam.ServicePrincipal('s3.amazonaws.com'),
      sourceArn: bucket.bucketArn,
      sourceAccount: this.account,
    });
    ingest.configureAsyncInvoke({ onFailure: new destinations.SqsDestination(failures) });

    const api = makeFunction('HttpHandler', 'http', audio);
    table.grantReadWriteData(api);
    api.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [bucket.arnForObjects(props.prefix ? props.prefix + '*' : '*.mp3')],
    }));
    if (!props.prefix) {
      api.addToRolePolicy(new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [bucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': '*.mp3' } },
      }));
    }
    if (props.kmsKeyArn) {
      api.addToRolePolicy(new iam.PolicyStatement({
        actions: ['kms:Decrypt'], resources: [props.kmsKeyArn],
      }));
    }
    const http = new gateway.HttpApi(this, 'Http');
    const httpIntegration = new integrations.HttpLambdaIntegration('Handler', api);
    const routes: [gateway.HttpMethod, string][] = [
      [gateway.HttpMethod.POST, '/v1/devices'],
      [gateway.HttpMethod.GET, '/v1/audio'],
      [gateway.HttpMethod.POST, '/v1/audio/{audioId}/download'],
      [gateway.HttpMethod.PUT, '/v1/audio/{audioId}/played'],
    ];
    for (const [method, route] of routes) {
      http.addRoutes({ path: route, methods: [method], integration: httpIntegration });
    }
    const httpStage = http.defaultStage?.node.defaultChild as gateway.CfnStage;
    httpStage.defaultRouteSettings = { throttlingBurstLimit: 20, throttlingRateLimit: 10 };

    const auth = makeFunction('WebSocketAuth', 'authorize');
    const connections = makeFunction('Connections', 'connection', { WS_ENDPOINT: endpoint });
    const delivery = makeFunction('Delivery', 'deliver', { ...audio, WS_ENDPOINT: endpoint });
    const admin = makeFunction('Admin', 'admin', { WS_ENDPOINT: endpoint });
    table.grantReadData(auth);
    for (const fn of [connections, delivery, admin]) {
      table.grantReadWriteData(fn);
      fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['execute-api:ManageConnections'],
        resources: [this.formatArn({
          service: 'execute-api', resource: ws.apiId, arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
          resourceName: 'v1/*/@connections/*',
        })],
      }));
    }
    ws.addRoute('$connect', {
      integration: new integrations.WebSocketLambdaIntegration('Connect', connections),
      authorizer: new authorizers.WebSocketLambdaAuthorizer('DeviceAuth', auth, {
        identitySource: ['route.request.header.Authorization'],
      }),
    });
    ws.addRoute('$disconnect', { integration: new integrations.WebSocketLambdaIntegration('Disconnect', connections) });
    ws.addRoute('ping', { integration: new integrations.WebSocketLambdaIntegration('Ping', connections) });
    delivery.addEventSource(new sources.DynamoEventSource(table, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON,
      batchSize: 10,
      retryAttempts: 3,
      bisectBatchOnError: true,
      onFailure: new sources.SqsDlq(failures),
      filters: [lambda.FilterCriteria.filter({ dynamodb: {
        Keys: { pk: { S: [{ prefix: 'AUDIO#' }] } },
      } })],
    }));
    new CfnOutput(this, 'HttpUrl', { value: http.apiEndpoint });
    new CfnOutput(this, 'WebSocketUrl', { value: wsStage.url });
    new CfnOutput(this, 'IngestFunctionArn', { value: ingest.functionArn });
    new CfnOutput(this, 'AdminFunctionName', { value: admin.functionName });
    new CfnOutput(this, 'FailureQueueUrl', { value: failures.queueUrl });
  }
}

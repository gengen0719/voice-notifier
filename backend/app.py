import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

ddb = boto3.resource("dynamodb")
table = ddb.Table(os.environ["TABLE_NAME"])
s3 = boto3.client("s3", region_name="ap-northeast-1", config=Config(signature_version="s3v4"))
serializer = TypeSerializer()
BUCKET = os.getenv("AUDIO_BUCKET_NAME")
PREFIX = os.getenv("AUDIO_KEY_PREFIX")


def key(pk, sk):
    return {"pk": pk, "sk": sk}


def get(pk, sk):
    return table.get_item(Key=key(pk, sk), ConsistentRead=True).get("Item")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


SOURCE = digest(json.dumps([BUCKET, PREFIX]))
CATALOG = "AUDIO#" + SOURCE
LOOKUP = "AUDIO_ID#" + SOURCE
PLAYED = "PLAYED#" + SOURCE


def wire(item):
    return {name: serializer.serialize(value) for name, value in item.items()}


def transact(*items):
    boto3.client("dynamodb").transact_write_items(TransactItems=[
        {action: {"TableName": table.name, **parameters}}
        for action, parameters in items
    ])


def put(item, condition):
    return "Put", {"Item": wire(item), "ConditionExpression": condition}


def ingest(event, _context):
    for record in event.get("Records", []):
        if record.get("eventSource") != "aws:s3" or not record["eventName"].startswith("ObjectCreated:"):
            continue
        source = record["s3"]
        object_key = unquote_plus(source["object"]["key"])
        if source["bucket"]["name"] != BUCKET or not object_key.startswith(PREFIX) or not object_key.lower().endswith(".mp3"):
            continue
        audio_id = digest(SOURCE + "\0" + object_key)
        insert_audio(audio_id, object_key, record["eventTime"])


def insert_audio(audio_id, object_key, created_at):
    for attempt in range(10):
        if get(LOOKUP, audio_id):
            return
        counter = get("META", SOURCE)
        previous = int(counter["value"]) if counter else 0
        order = f"{previous + 1:020d}"
        audio = {**key(CATALOG, order), "audioId": audio_id, "objectKey": object_key, "createdAt": created_at}
        update = {
            "Key": wire(key("META", SOURCE)),
            "UpdateExpression": "SET #v = :next",
            "ConditionExpression": "#v = :previous" if counter else "attribute_not_exists(#v)",
            "ExpressionAttributeNames": {"#v": "value"},
            "ExpressionAttributeValues": wire({":next": previous + 1, **({":previous": previous} if counter else {})}),
        }
        try:
            transact(
                ("Update", update),
                put({**key(LOOKUP, audio_id), "order": order}, "attribute_not_exists(pk)"),
                put(audio, "attribute_not_exists(pk)"),
            )
            return
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            if attempt == 9:
                raise
            time.sleep(random.uniform(0, min(0.25, 0.01 * 2 ** attempt)))


def response(status, body=None):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body or {}),
    }


def device(event):
    headers = {name.lower(): value for name, value in (event.get("headers") or {}).items()}
    token = headers.get("authorization", "")
    if not token.startswith("Bearer ") or len(token) > 128:
        return None
    registered = get("AUTH", "device")
    if registered and hmac.compare_digest(registered["tokenHash"], digest(token[7:])):
        return registered["deviceId"]
    return None


def register(event):
    try:
        body = json.loads(event.get("body") or "{}")
        code, token = body["code"], body["token"]
        if not isinstance(code, str) or not 1 <= len(code) <= 100:
            return response(400)
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            return response(400)
    except (ValueError, KeyError, TypeError):
        return response(400)
    token_hash, code_hash = digest(token), digest(code)
    device_id = secrets.token_hex(16)
    registration = {
        **key("AUTH", "device"), "deviceId": device_id,
        "tokenHash": token_hash, "registrationCodeHash": code_hash,
    }
    try:
        transact(
            ("Delete", {
                "Key": wire(key("AUTH", "registration")),
                "ConditionExpression": "#ttl > :now AND codeHash = :hash",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": wire({":now": int(time.time()), ":hash": code_hash}),
            }),
            put(registration, "attribute_not_exists(pk)"),
        )
    except ClientError as error:
        reasons = error.response.get("CancellationReasons", [])
        if error.response["Error"]["Code"] == "TransactionCanceledException" and any(
            reason.get("Code") == "ConditionalCheckFailed" for reason in reasons
        ):
            existing = get("AUTH", "device")
            if (existing and hmac.compare_digest(existing["tokenHash"], token_hash)
                    and hmac.compare_digest(existing.get("registrationCodeHash", ""), code_hash)):
                return response(200, {"deviceId": existing["deviceId"]})
            return response(403)
        raise
    return response(201, {"deviceId": device_id})


def http(event, _context):
    route = event["routeKey"]
    if route == "POST /v1/devices":
        return register(event)
    device_id = device(event)
    if not device_id:
        return response(401)
    if route == "GET /v1/audio":
        cursor = (event.get("queryStringParameters") or {}).get("cursor")
        checkpoint = get(PLAYED, device_id)
        after = checkpoint["order"] if checkpoint else "0" * 20
        if cursor is not None:
            source, _, order = cursor.partition(":")
            if not re.fullmatch(r"[a-f0-9]{64}", source) or not re.fullmatch(r"[0-9]{20}", order):
                return response(400)
            if source == SOURCE:
                after = max(after, order)
        condition = Key("pk").eq(CATALOG) & Key("sk").gt(after)
        page = table.query(KeyConditionExpression=condition, Limit=25, ConsistentRead=True)
        items = [{"audioId": item["audioId"], "createdAt": item["createdAt"]} for item in page["Items"]]
        result = {"sourceId": SOURCE, "items": items}
        if "LastEvaluatedKey" in page:
            result["nextCursor"] = SOURCE + ":" + page["LastEvaluatedKey"]["sk"]
        return response(200, result)
    audio_id = event["pathParameters"]["audioId"]
    if len(audio_id) != 64 or any(char not in "0123456789abcdef" for char in audio_id):
        return response(404)
    lookup = get(LOOKUP, audio_id)
    if not lookup:
        return response(404)
    if route == "PUT /v1/audio/{audioId}/played":
        return acknowledge(device_id, lookup["order"])
    audio = get(CATALOG, lookup["order"])
    if not audio or not audio["objectKey"].startswith(PREFIX):
        return response(404)
    expires = 60
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": audio["objectKey"]}, ExpiresIn=expires,
    )
    return response(200, {"url": url, "expiresAt": int(time.time()) + expires})


def acknowledge(device_id, order):
    checkpoint = get(PLAYED, device_id)
    previous = checkpoint["order"] if checkpoint else "0" * 20
    if order <= previous:
        return response(200)
    if int(order) != int(previous) + 1:
        return response(409)
    try:
        table.put_item(
            Item={**key(PLAYED, device_id), "order": order},
            ConditionExpression="attribute_not_exists(pk) OR #order = :previous",
            ExpressionAttributeNames={"#order": "order"},
            ExpressionAttributeValues={":previous": previous},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        updated = get(PLAYED, device_id)
        if not updated or updated["order"] < order:
            return response(409)
    return response(200)


def authorize(event, _context):
    device_id = device(event)
    return {
        "principalId": device_id or "unauthenticated",
        "policyDocument": {"Version": "2012-10-17", "Statement": [{
            "Action": "execute-api:Invoke",
            "Effect": "Allow" if device_id else "Deny",
            "Resource": event["methodArn"],
        }]},
        "context": {"deviceId": device_id or ""},
    }


def management():
    return boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["WS_ENDPOINT"])


def remove_connection(connection_id, client):
    try:
        client.delete_connection(ConnectionId=connection_id)
    except client.exceptions.GoneException:
        pass
    table.delete_item(Key=key("WS", connection_id))


def send(connection_id, payload, client):
    try:
        client.post_to_connection(ConnectionId=connection_id, Data=json.dumps(payload).encode())
    except client.exceptions.GoneException:
        table.delete_item(Key=key("WS", connection_id))


def connection(event, _context):
    context = event["requestContext"]
    connection_id = context["connectionId"]
    route = context["routeKey"]
    if route == "$connect":
        device_id = context["authorizer"]["deviceId"]
        try:
            transact(
                ("ConditionCheck", {
                    "Key": wire(key("AUTH", "device")), "ConditionExpression": "deviceId = :id",
                    "ExpressionAttributeValues": wire({":id": device_id}),
                }),
                put({**key("WS", connection_id), "deviceId": device_id, "ttl": int(time.time()) + 7200}, "attribute_not_exists(pk)"),
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "TransactionCanceledException":
                return response(403)
            raise
    elif route == "$disconnect":
        table.delete_item(Key=key("WS", connection_id))
    elif route == "ping":
        row = get("WS", connection_id)
        registered = get("AUTH", "device")
        client = management()
        if row and registered and row["deviceId"] == registered["deviceId"]:
            if not row.get("ready"):
                try:
                    table.update_item(
                        Key=key("WS", connection_id), UpdateExpression="SET #ready = :yes",
                        ConditionExpression="attribute_exists(pk)",
                        ExpressionAttributeNames={"#ready": "ready"},
                        ExpressionAttributeValues={":yes": True},
                    )
                except ClientError as error:
                    if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
            send(connection_id, {"type": "pong", "version": 1}, client)
        else:
            remove_connection(connection_id, client)
    return response(200)


def connections():
    arguments = {"KeyConditionExpression": Key("pk").eq("WS"), "ConsistentRead": True}
    while True:
        page = table.query(**arguments)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def deliver(event, _context):
    audio_ids = [
        record["dynamodb"]["NewImage"]["audioId"]["S"]
        for record in event["Records"]
        if record["eventName"] == "INSERT" and record["dynamodb"]["Keys"]["pk"]["S"] == CATALOG
    ]
    if not audio_ids:
        return
    registered = get("AUTH", "device")
    if not registered:
        return
    client = management()
    for row in connections():
        if not row.get("ready") or row["deviceId"] != registered["deviceId"] or row["ttl"] <= time.time():
            continue
        for audio_id in audio_ids:
            send(row["sk"], {"type": "audio.available", "version": 1, "audioId": audio_id}, client)


def admin(event, _context):
    action = event.get("action")
    if action == "issue":
        if get("AUTH", "device"):
            return {"error": "Revoke the current device before registering another"}
        code = secrets.token_urlsafe(24)
        expires = int(time.time()) + 600
        transact(
            ("ConditionCheck", {
                "Key": wire(key("AUTH", "device")), "ConditionExpression": "attribute_not_exists(pk)",
            }),
            ("Put", {"Item": wire({**key("AUTH", "registration"), "codeHash": digest(code), "ttl": expires})}),
        )
        return {"code": code, "expiresAt": expires}
    if action == "revoke":
        transact(
            ("Delete", {"Key": wire(key("AUTH", "device"))}),
            ("Delete", {"Key": wire(key("AUTH", "registration"))}),
        )
        registered = get("AUTH", "device")
        client = management()
        for row in connections():
            if not registered or row["deviceId"] != registered["deviceId"]:
                remove_connection(row["sk"], client)
        return {"revoked": True}
    raise ValueError("Use issue or revoke")

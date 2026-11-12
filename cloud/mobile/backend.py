"""Mobile API behind API Gateway's Cognito JWT authorizer. Never trust owner in JSON.
No raw signals, access tokens, email addresses or presigned URLs are logged.
"""
import base64
import hashlib
import json
import os
import re
import time
import uuid
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def signup(event, context):
    allowed = {e.strip().lower() for e in os.environ.get('ALLOWED_EMAILS', '').split(',') if e.strip()}
    email = event.get('request', {}).get('userAttributes', {}).get('email', '').lower()
    if not email or email not in allowed:
        raise ValueError('Ask the project owner to add your email to AllowedSignupEmails.')
    # Cognito still requires email confirmation; do not auto-confirm or verify.
    return event


class ApiError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def valid_id(value):
    try:
        parsed = str(uuid.UUID(value))
        if value != parsed:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ApiError(400, 'Invalid UUID')


def reply(code, body):
    return {'statusCode': code, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'body': json.dumps(body, default=lambda n: int(n) if isinstance(n, Decimal) and n % 1 == 0 else float(n))}


def metadata(body):
    value = body.get('metadata')
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > 64000:
        raise ApiError(400, 'Session metadata must be an object under 64KB')
    return json.loads(json.dumps(value), parse_float=Decimal)


def handler(event, context):
    try:
        return dispatch(event)
    except ApiError as e:
        return reply(e.code, {'error': e.message})
    except (ValueError, TypeError, KeyError):
        return reply(400, {'error': 'Invalid request'})
    except ClientError:
        return reply(503, {'error': 'Storage is temporarily unavailable. Retry the local upload queue.'})
    except Exception:
        return reply(500, {'error': 'Request failed. Local recording is retained.'})


def dispatch(event):
    claims = event.get('requestContext', {}).get('authorizer', {}).get('jwt', {}).get('claims', {})
    if claims.get('token_use') != 'access' or claims.get('iss') != os.environ['ISSUER'] or claims.get('client_id') != os.environ['CLIENT_ID']:
        raise ApiError(401, 'Sign in required')
    owner = valid_id(claims.get('sub'))
    method = event.get('requestContext', {}).get('http', {}).get('method')
    path = event.get('rawPath', '').strip('/').split('/')
    if not path or path[0] != 'sessions':
        raise ApiError(404, 'Not found')
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw, validate=True).decode()
    if len(raw.encode()) > 70000:
        raise ApiError(413, 'Request too large')
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ApiError(400, 'Expected JSON object')
    table = boto3.resource('dynamodb', region_name=os.environ['AWS_REGION']).Table(os.environ['TABLE'])
    s3 = boto3.client('s3', region_name=os.environ['AWS_REGION'],
        endpoint_url=f"https://s3.{os.environ['AWS_REGION']}.amazonaws.com",
        config=Config(signature_version='s3v4',s3={'addressing_style':'virtual'}))
    pk = 'USER#' + owner

    def query(prefix):
        from boto3.dynamodb.conditions import Key
        args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with(prefix), 'ConsistentRead': True}
        while True:
            response = table.query(**args)
            yield from response['Items']
            if 'LastEvaluatedKey' not in response:
                break
            args['ExclusiveStartKey'] = response['LastEvaluatedKey']

    if len(path) == 1:
        if method == 'GET':
            # Bounded pagination; never scan or expose another partition.
            from boto3.dynamodb.conditions import Key
            args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with('SESSION#'), 'Limit': 50, 'ConsistentRead': True}
            cursor = (event.get('queryStringParameters') or {}).get('cursor')
            if cursor:
                start = json.loads(base64.urlsafe_b64decode(cursor))
                if start.get('pk') != pk or not start.get('sk', '').startswith('SESSION#'):
                    raise ApiError(400, 'Invalid cursor')
                args['ExclusiveStartKey'] = start
            result = table.query(**args)
            return reply(200, {'sessions': [{k: v for k, v in x.items() if k not in ('pk', 'sk')} for x in result['Items']],
                               'cursor': base64.urlsafe_b64encode(json.dumps(result['LastEvaluatedKey']).encode()).decode() if 'LastEvaluatedKey' in result else None})
        if method != 'POST':
            raise ApiError(405, 'Method not allowed')
        sid = valid_id(body.get('id'))
        item = {'pk': pk, 'sk': 'SESSION#' + sid, 'id': sid, 'state': 'uploading', 'metadata': metadata(body), 'created': int(time.time())}
        try:
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
        return reply(200, {'id': sid})

    sid = valid_id(path[1])
    key = {'pk': pk, 'sk': 'SESSION#' + sid}
    session = table.get_item(Key=key, ConsistentRead=True).get('Item')
    if not session:
        raise ApiError(404, 'Session not found for this account')
    prefix = 'CHUNK#' + sid + '#'
    if len(path) == 2 and method == 'GET':
        # Chunk manifest is paginated separately by sequential number.
        start = int((event.get('queryStringParameters') or {}).get('start', '0'))
        if start < 0 or start > 200000:
            raise ApiError(400, 'Invalid manifest offset')
        from boto3.dynamodb.conditions import Key
        result = table.query(KeyConditionExpression=Key('pk').eq(pk) & Key('sk').between(prefix + f'{start:08}', prefix + '99999999'), Limit=100, ConsistentRead=True)
        rows = result['Items']
        return reply(200, {'session': {k:v for k,v in session.items() if k not in ('pk','sk')},
                           'chunks': [{k:v for k,v in x.items() if k not in ('pk','sk')} for x in rows],
                           'next': int(rows[-1]['seq']) + 1 if 'LastEvaluatedKey' in result else None})
    if len(path) == 3 and path[2] == 'complete' and method == 'POST':
        n = body.get('chunks')
        if type(n) is not int or not 0 <= n <= 200000 or body.get('state') not in ('completed', 'interrupted', 'error'):
            raise ApiError(400, 'Invalid final session state or chunk count')
        meta = metadata(body)
        if session['state'] == 'complete':
            if int(session['chunks']) != n:
                raise ApiError(409, 'Completed session is immutable')
            return reply(200, {'state':'complete','chunks':n})
        # Freeze creation of new chunks before reading the manifest. Chunk
        # allocation below atomically checks this state in the same transaction.
        lock = str(uuid.uuid4())
        now = int(time.time())
        try:
            table.update_item(Key=key, UpdateExpression='SET #st=:f, finalize_token=:lock, finalize_until=:until',
                ConditionExpression='#st=:u OR (#st=:f AND finalize_until < :now)',
                ExpressionAttributeNames={'#st':'state'},
                ExpressionAttributeValues={':f':'finalizing',':u':'uploading',':lock':lock,':until':now+60,':now':now})
        except ClientError as e:
            if e.response['Error']['Code']=='ConditionalCheckFailedException':
                raise ApiError(409, 'Finalization in progress; retry shortly')
            raise
        def unfreeze():
            table.update_item(Key=key,UpdateExpression='SET #st=:st REMOVE finalize_token, finalize_until',
                ConditionExpression='finalize_token=:lock',ExpressionAttributeNames={'#st':'state'},
                ExpressionAttributeValues={':st':'uploading',':lock':lock})
        expected = 0
        for chunk in query(prefix):
            if int(chunk['seq']) != expected or chunk.get('state') != 'uploaded':
                unfreeze()
                raise ApiError(409, 'Missing or unverified chunk')
            expected += 1
        if expected != n:
            unfreeze()
            raise ApiError(409, 'Chunk count mismatch')
        if session['state'] == 'complete' and int(session['chunks']) != n:
            raise ApiError(409, 'Completed session is immutable')
        table.update_item(Key=key, UpdateExpression='SET #st=:st, chunks=:n, recording_state=:r, metadata=:m REMOVE finalize_token, finalize_until',
                          ConditionExpression='finalize_token=:lock',
                          ExpressionAttributeNames={'#st':'state'},
                          ExpressionAttributeValues={':st':'complete', ':n':n, ':r':body['state'], ':m':meta, ':lock':lock})
        return reply(200, {'state':'complete','chunks':n})
    if len(path) != 5 or path[2] != 'chunks' or method != 'POST':
        raise ApiError(404, 'Not found')
    if not re.fullmatch(r'0|[1-9][0-9]{0,5}', path[3]) or int(path[3]) >= 200000:
        raise ApiError(400, 'Invalid chunk sequence')
    seq = int(path[3])
    ck = {'pk':pk, 'sk':prefix + f'{seq:08}'}
    obj = f'users/{owner}/{sid}/{seq:08}.json.gz'
    existing = table.get_item(Key=ck, ConsistentRead=True).get('Item')
    operation = path[4]
    if operation == 'upload':
        digest, size = body.get('sha256'), body.get('bytes')
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or type(size) is not int or not 1 <= size <= 4*1024*1024:
            raise ApiError(400, 'Invalid SHA256 or chunk size')
        if session['state'] != 'uploading':
            raise ApiError(409, 'Session is finalizing or complete')
        proposed = {**ck,'seq':seq,'sha256':digest,'bytes':size,'state':'pending','object_key':obj}
        try:
            from boto3.dynamodb.types import TypeSerializer
            serialize = TypeSerializer().serialize
            # Use a separate low-level client; Table.resource's client injects
            # its own serializers and must not double-serialize these values.
            boto3.client('dynamodb',region_name=os.environ['AWS_REGION']).transact_write_items(TransactItems=[
                {'ConditionCheck':{'TableName':os.environ['TABLE'],
                    'Key':{k:serialize(v) for k,v in key.items()},
                    'ConditionExpression':'#st=:st','ExpressionAttributeNames':{'#st':'state'},
                    'ExpressionAttributeValues':{':st':serialize('uploading')}}},
                {'Put':{'TableName':os.environ['TABLE'],'Item':{k:serialize(v) for k,v in proposed.items()},
                        'ConditionExpression':'attribute_not_exists(pk)'}}])
            existing = proposed
        except ClientError as e:
            if e.response['Error']['Code'] != 'TransactionCanceledException':
                raise
            existing = table.get_item(Key=ck,ConsistentRead=True).get('Item')
            current = table.get_item(Key=key,ConsistentRead=True)['Item']
            if not existing or current['state'] != 'uploading':
                raise ApiError(409, 'Session is finalizing or complete')
        if existing['sha256'] != digest or int(existing['bytes']) != size:
            raise ApiError(409, 'Chunk sequence already binds a different payload')
        checksum = base64.b64encode(bytes.fromhex(digest)).decode()
        params = {'Bucket':os.environ['BUCKET'],'Key':obj,'ContentType':'application/gzip','ContentLength':size,
                  'ChecksumSHA256':checksum,'ChecksumAlgorithm':'SHA256','ServerSideEncryption':'AES256'}
        url = s3.generate_presigned_url('put_object',Params=params,ExpiresIn=300)
        return reply(200, {'url':url,'headers':{'Content-Type':'application/gzip','Content-Length':str(size),
            'x-amz-checksum-sha256':checksum,'x-amz-sdk-checksum-algorithm':'SHA256','x-amz-server-side-encryption':'AES256'}})
    if not existing:
        raise ApiError(404, 'Chunk not found')
    if operation == 'commit':
        try:
            head = s3.head_object(Bucket=os.environ['BUCKET'],Key=obj,ChecksumMode='ENABLED')
        except ClientError as e:
            if e.response['Error']['Code'] in ('404','NoSuchKey'):
                raise ApiError(409, 'Object upload is not present yet')
            raise
        if head['ContentLength'] != int(existing['bytes']) or head.get('ChecksumSHA256') != base64.b64encode(bytes.fromhex(existing['sha256'])).decode():
            raise ApiError(409, 'Object checksum or size mismatch')
        table.update_item(Key=ck,UpdateExpression='SET #st=:st',ExpressionAttributeNames={'#st':'state'},ExpressionAttributeValues={':st':'uploaded'})
        return reply(200,{'state':'uploaded'})
    if operation == 'download' and existing['state'] == 'uploaded':
        return reply(200,{'url':s3.generate_presigned_url('get_object',Params={'Bucket':os.environ['BUCKET'],'Key':obj},ExpiresIn=300)})
    raise ApiError(404, 'Not found')

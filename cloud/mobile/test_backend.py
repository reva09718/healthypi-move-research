"""Mocked AWS only. No deployment and no real AWS credentials required."""
import base64
import gzip
import hashlib
import json
import os
import unittest
import uuid
from unittest.mock import patch
import boto3
from moto import mock_aws
import backend

REGION='ap-southeast-1'
USER='00000000-0000-4000-8000-000000000001'
OTHER='00000000-0000-4000-8000-000000000002'

@mock_aws
class MobileTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'AWS_ACCESS_KEY_ID':'testing','AWS_SECRET_ACCESS_KEY':'testing',
            'AWS_SESSION_TOKEN':'testing','AWS_REGION':REGION,'AWS_DEFAULT_REGION':REGION,
            'BUCKET':'hpi-mobile-test','TABLE':'mobile-sessions','ISSUER':'https://issuer','CLIENT_ID':'client'})
        self.env.start();self.addCleanup(self.env.stop)
        self.s3=boto3.client('s3',region_name=REGION)
        self.s3.create_bucket(Bucket='hpi-mobile-test',CreateBucketConfiguration={'LocationConstraint':REGION})
        self.table=boto3.resource('dynamodb',region_name=REGION).create_table(TableName='mobile-sessions',BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName':x,'AttributeType':'S'} for x in ['pk','sk']],
            KeySchema=[{'AttributeName':'pk','KeyType':'HASH'},{'AttributeName':'sk','KeyType':'RANGE'}])
        self.sid=str(uuid.uuid4())
    def event(self,path,body=None,user=USER,method='POST'):
        return {'rawPath':path,'body':json.dumps(body or {}),'requestContext':{'http':{'method':method},'authorizer':{'jwt':{'claims':{
            'sub':user,'iss':'https://issuer','client_id':'client','token_use':'access'}}}}}
    def api(self,path,body=None,user=USER,method='POST'):
        r=backend.handler(self.event(path,body,user,method),None)
        return r['statusCode'],json.loads(r['body'])
    def create(self):
        self.assertEqual(self.api('/sessions',{'id':self.sid,'metadata':{'started_utc_ms':1}})[0],200)
    def plan(self,data=b'payload',seq=0):
        return self.api(f'/sessions/{self.sid}/chunks/{seq}/upload',{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
    def put(self,data=b'payload',seq=0):
        self.s3.put_object(Bucket='hpi-mobile-test',Key=f'users/{USER}/{self.sid}/{seq:08}.json.gz',Body=data,
            ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode(),ChecksumAlgorithm='SHA256',ContentType='application/gzip',ServerSideEncryption='AES256')
    def test_user_isolation_and_body_owner_ignored(self):
        self.create()
        for path in [f'/sessions/{self.sid}',f'/sessions/{self.sid}/chunks/0/upload',f'/sessions/{self.sid}/complete']:
            self.assertEqual(self.api(path,user=OTHER)[0],404)
        status,result=self.api('/sessions',user=OTHER,method='GET');self.assertEqual(status,200);self.assertEqual(result['sessions'],[])
        status,result=self.api('/sessions',{'id':str(uuid.uuid4()),'owner':USER,'metadata':{}},user=OTHER)
        self.assertEqual(status,200)
        self.assertEqual(len(self.api('/sessions',method='GET')[1]['sessions']),1)
    def test_untrusted_or_id_token_rejected(self):
        e=self.event('/sessions');e['requestContext']['authorizer']={}
        self.assertEqual(backend.handler(e,None)['statusCode'],401)
        for field,value in [('token_use','id'),('iss','https://evil'),('client_id','other')]:
            e=self.event('/sessions');e['requestContext']['authorizer']['jwt']['claims'][field]=value
            self.assertEqual(backend.handler(e,None)['statusCode'],401)
    def test_idempotent_plan_checksum_and_commit_complete(self):
        self.create();self.create()
        self.assertEqual(self.plan()[0],200);self.assertEqual(self.plan()[0],200)
        self.assertEqual(self.plan(b'different')[0],409)
        self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/commit')[0],409)
        self.put();self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/commit')[0],200)
        self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/commit')[0],200)
        final={'chunks':1,'state':'completed','metadata':{'samples':123}}
        self.assertEqual(self.api(f'/sessions/{self.sid}/complete',final)[0],200)
        self.assertEqual(self.api(f'/sessions/{self.sid}/complete',final)[0],200)
        self.assertEqual(self.plan(seq=1)[0],409)
        result=self.api(f'/sessions/{self.sid}',method='GET')[1]
        self.assertEqual(result['session']['state'],'complete');self.assertEqual(len(result['chunks']),1)
    def test_corrupt_object_not_committed(self):
        self.create();self.plan();self.put(b'wrong!!')
        self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/commit')[0],409)
        self.assertEqual(self.api(f'/sessions/{self.sid}/complete',{'chunks':1,'state':'completed','metadata':{}})[0],409)
    def test_holes_and_pending_chunks_prevent_completion(self):
        self.create();self.plan(seq=1);self.put(seq=1);self.api(f'/sessions/{self.sid}/chunks/1/commit')
        self.assertEqual(self.api(f'/sessions/{self.sid}/complete',{'chunks':1,'state':'completed','metadata':{}})[0],409)
        self.assertEqual(self.plan(seq=0)[0],200)
        self.assertEqual(self.api(f'/sessions/{self.sid}/complete',{'chunks':2,'state':'completed','metadata':{}})[0],409)
    def test_validation_and_download_scope(self):
        self.assertEqual(self.api('/sessions',{'id':'../../evil','metadata':{}})[0],400)
        self.create()
        for seq in ['-1','01','200000','../../']:
            self.assertIn(self.api(f'/sessions/{self.sid}/chunks/{seq}/upload',{'sha256':'x','bytes':1})[0],[400,404])
        self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/upload',{'sha256':'a'*64,'bytes':5000000})[0],400)
        self.plan();self.put();self.api(f'/sessions/{self.sid}/chunks/0/commit')
        self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/download')[0],200)
        self.assertEqual(self.api(f'/sessions/{self.sid}/chunks/0/download',user=OTHER)[0],404)
    def test_manifest_pagination(self):
        self.create()
        with self.table.batch_writer() as batch:
            for i in range(105):batch.put_item(Item={'pk':'USER#'+USER,'sk':f'CHUNK#{self.sid}#{i:08}','seq':i,'state':'uploaded'})
        first=self.api(f'/sessions/{self.sid}',method='GET')[1]
        self.assertEqual(len(first['chunks']),100);self.assertEqual(first['next'],100)
        e=self.event(f'/sessions/{self.sid}',method='GET');e['queryStringParameters']={'start':'100'}
        last=json.loads(backend.handler(e,None)['body']);self.assertEqual(len(last['chunks']),5);self.assertIsNone(last['next'])
    def test_signup_is_allowlisted_and_not_auto_confirmed(self):
        with patch.dict(os.environ,{'ALLOWED_EMAILS':'Person@Example.org, teammate@example.org'}):
            e={'request':{'userAttributes':{'email':'person@example.org'}},'response':{}}
            self.assertEqual(backend.signup(e,None)['response'],{})
            with self.assertRaises(ValueError):backend.signup({'request':{'userAttributes':{'email':'outsider@example.org'}}},None)
        with patch.dict(os.environ,{'ALLOWED_EMAILS':''}):
            with self.assertRaises(ValueError):backend.signup(e,None)

if __name__=='__main__':unittest.main()

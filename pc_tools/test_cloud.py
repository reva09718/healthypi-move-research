"""Offline tests for AWS storage with mocked AWS (moto): python -m unittest discover -s pc_tools -p test_cloud.py -v

These prove the upload/verify/resume logic and tamper detection. They do NOT prove behaviour
against a real AWS account, real network latency, or the watch.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                  AWS_DEFAULT_REGION="us-east-1", AWS_SESSION_TOKEN="testing")
import boto3
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

import cloud_storage as C
from research_storage import PacketBuilder, SessionStore

BUCKET, TABLE = "hpi-test-bucket", "hpi-test-sessions"


def make_local(path, packets=45, status="stopped", signals=("ecg", "ppg_wrist")):
    """Write a local encrypted session using a portable test cipher (DPAPI is Windows-only)."""
    store = SessionStore(path, cipher=C.AesGcmCipher(os.urandom(32)))
    sid = store.start(list(signals), device="AA:BB", note="secret-note-xyz")
    builder = PacketBuilder()
    for i in range(packets):
        store.append(sid, builder.make("ecg", [1000 + i, 2000 + i], 128, b"\x01\x02"))
    store.event(sid, 1.0, "connected", "connection segment 0")
    store.flush()
    if status:
        store.finish(sid, status)
    return store, sid


class FlakyS3:
    """Delegates to a real (mock) client but fails put_object for chosen call numbers."""
    def __init__(self, real, fail_calls=()):
        self.real, self.fail_calls, self.puts = real, set(fail_calls), 0

    def put_object(self, **kw):
        self.puts += 1
        if self.puts in self.fail_calls:
            raise EndpointConnectionError(endpoint_url="https://s3.test")
        return self.real.put_object(**kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


@mock_aws
class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.s3 = boto3.client("s3")
        self.s3.create_bucket(Bucket=BUCKET)
        boto3.client("dynamodb").create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}])
        self.cfg = C.CloudConfig(bucket=BUCKET, table=TABLE, chunk_packets=10,
                                 key_file=str(self.dir / "cloud.key"),
                                 sync_db=str(self.dir / "sync.db"))
        self.db = self.dir / "local.db"
        self.store, self.sid = make_local(self.db)
        self.cipher = self.store.cipher

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def sync(self, s3=None):
        cloud = C.CloudStore(self.cfg, s3=s3 or self.s3,
                             table=boto3.resource("dynamodb").Table(TABLE))
        sync = C.CloudSync(cloud, C.LocalSessions(self.db, self.cipher), C.SyncState(self.cfg.sync_db))
        self.addCleanup(sync.close)
        return sync

    def keys(self):
        return [o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]

    def upload(self):
        sync = self.sync()
        result = sync.sync_session(self.sid)
        return sync, result

    def test_roundtrip_matches_local_packets(self):
        sync, result = self.upload()
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["uploaded_chunks"], 5)  # 45 packets / 10 per chunk
        local = list(self.store.packets(self.sid))
        cloud = list(sync.cloud.packets(self.sid))
        self.assertEqual(cloud, local)
        report = sync.cloud.verify_session(self.sid)
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["packets"], report["chunks"]), (45, 5))
        meta = sync.cloud.session_meta(self.sid)
        self.assertEqual(meta["note"], "secret-note-xyz")
        self.assertEqual(meta["events"][0][2], "connected")

    def test_nothing_sensitive_is_stored_in_plaintext(self):
        self.upload()
        for key in self.keys():
            body = self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            if key.endswith("manifest.json"):
                self.assertNotIn(b"secret-note-xyz", body)
                continue
            for needle in (b"secret-note-xyz", b'"values"', b"1000", b"connected"):
                self.assertNotIn(needle, body, key)
            self.assertEqual(self.s3.head_object(Bucket=BUCKET, Key=key)["ServerSideEncryption"], "AES256")

    def test_dynamodb_index(self):
        sync, _ = self.upload()
        item = sync.cloud.table.get_item(Key={"session_id": self.sid})["Item"]
        self.assertEqual(item["status"], "complete")
        self.assertEqual(int(item["total_packets"]), 45)
        self.assertEqual([s["session_id"] for s in sync.cloud.list_sessions()], [self.sid])

    def test_tampered_chunk_is_detected(self):
        sync, _ = self.upload()
        key = [k for k in self.keys() if k.endswith("chunk-000002.bin")][0]
        blob = bytearray(self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        blob[20] ^= 1
        self.s3.put_object(Bucket=BUCKET, Key=key, Body=bytes(blob))
        report = sync.cloud.verify_session(self.sid)
        self.assertFalse(report["ok"])
        self.assertIn("hash", report["problems"][0])

    def test_swapped_chunks_are_detected(self):
        sync, _ = self.upload()
        k1 = [k for k in self.keys() if k.endswith("chunk-000001.bin")][0]
        k2 = [k for k in self.keys() if k.endswith("chunk-000002.bin")][0]
        b1 = self.s3.get_object(Bucket=BUCKET, Key=k1)["Body"].read()
        b2 = self.s3.get_object(Bucket=BUCKET, Key=k2)["Body"].read()
        self.s3.put_object(Bucket=BUCKET, Key=k1, Body=b2)
        self.s3.put_object(Bucket=BUCKET, Key=k2, Body=b1)
        self.assertFalse(sync.cloud.verify_session(self.sid)["ok"])

    def test_deleted_chunk_is_detected(self):
        sync, _ = self.upload()
        self.s3.delete_object(Bucket=BUCKET, Key=[k for k in self.keys() if k.endswith("chunk-000003.bin")][0])
        report = sync.cloud.verify_session(self.sid)
        self.assertFalse(report["ok"])
        self.assertIn("Missing chunk", report["problems"][0])

    def test_manifest_edit_dropping_a_chunk_is_detected(self):
        sync, _ = self.upload()
        key = [k for k in self.keys() if k.endswith("manifest.json")][0]
        manifest = json.loads(self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
        manifest["chunks"].pop()
        manifest["total_packets"] -= 5
        self.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(manifest).encode())
        report = sync.cloud.verify_session(self.sid)
        self.assertFalse(report["ok"])
        self.assertIn("Authentication failed", report["problems"][0])

    def test_wrong_or_missing_key_cannot_read(self):
        sync, _ = self.upload()
        other_key = self.dir / "other.key"
        other_key.write_text(os.urandom(32).hex())
        other = C.CloudStore(C.CloudConfig(bucket=BUCKET, prefix=self.cfg.prefix,
                                           key_file=str(other_key)), s3=self.s3)
        report = other.verify_session(self.sid)
        self.assertFalse(report["ok"])
        self.assertIn("key", report["problems"][0].lower())
        (self.dir / "cloud.key").unlink()  # key lost: must fail loudly, never create a new one
        lost = C.CloudStore(self.cfg, s3=self.s3, table=sync.cloud.table)
        with self.assertRaises(C.CloudConfigError):
            list(lost.packets(self.sid))

    def test_session_id_must_be_uuid(self):
        sync, _ = self.upload()
        with self.assertRaises(ValueError):
            sync.cloud.read_manifest("../../etc/passwd")

    def test_upload_during_recording_then_finalise(self):
        self.store.close()
        self.db2 = self.dir / "live.db"
        store, sid = make_local(self.db2, packets=25, status=None)
        self.cipher = store.cipher
        self.db = self.db2
        sync = self.sync()
        first = sync.sync_session(sid)  # still recording: only sealed chunks 0 and 1
        self.assertEqual((first["uploaded_chunks"], first["complete"]), (2, False))
        self.assertEqual(sync.sync_session(sid)["uploaded_chunks"], 0)
        builder = PacketBuilder(); builder.seq = 25
        for i in range(10):
            store.append(sid, builder.make("ecg", [i], 128))
        store.flush()
        store.finish(sid, "completed")
        last = sync.sync_session(sid)
        self.assertTrue(last["complete"], last)
        self.assertEqual(len(list(sync.cloud.packets(sid))), 35)
        self.assertTrue(sync.cloud.verify_session(sid)["ok"])
        store.close()
        self.store = SessionStore(self.dir / "x.db", cipher=C.AesGcmCipher(os.urandom(32)))

    def test_network_outage_then_resume_without_duplicates(self):
        flaky = FlakyS3(self.s3, fail_calls={3, 4})  # 3rd and 4th S3 writes fail
        sync = self.sync(s3=flaky)
        r1 = sync.sync_session(self.sid)
        self.assertFalse(r1["complete"])
        self.assertIn("EndpointConnectionError", r1["error"])
        self.assertEqual(r1["uploaded_chunks"], 2)       # chunks 0 and 1 made it before the outage
        self.assertEqual(sync.state.get(self.sid)["next_chunk"], 2)
        self.assertIn("EndpointConnectionError", sync.state.get(self.sid)["last_error"])
        r2 = sync.sync_session(self.sid)                 # link still down for one more call
        self.assertFalse(r2["complete"])
        self.assertEqual(r2["uploaded_chunks"], 0)
        r3 = sync.sync_session(self.sid)                 # link restored
        self.assertTrue(r3["complete"], r3)
        self.assertEqual(r3["uploaded_chunks"], 3)
        self.assertEqual(list(sync.cloud.packets(self.sid)), list(self.store.packets(self.sid)))
        self.assertEqual(len([k for k in self.keys() if "chunk-" in k]), 5)  # no duplicates

    def test_resume_after_restart_uses_saved_key_and_chunk_size(self):
        sync = self.sync(s3=FlakyS3(self.s3, fail_calls={3}))
        sync.sync_session(self.sid)
        sync.close()
        self.cfg.chunk_packets = 7  # must be ignored for a session already in progress
        sync = self.sync()
        self.assertTrue(sync.sync_session(self.sid)["complete"])
        self.assertEqual(list(sync.cloud.packets(self.sid)), list(self.store.packets(self.sid)))

    def test_sync_all_skips_active_sessions_unless_forced(self):
        active_db = self.dir / "mixed.db"
        store, active = make_local(active_db, packets=5, status=None)
        self.db, self.cipher = active_db, store.cipher
        sync = self.sync()
        out = sync.sync_all()
        self.assertEqual([(r["session_id"], r["complete"]) for r in out], [(active, False)])
        out = sync.sync_all(include_active=True)
        self.assertTrue(out[0]["complete"])
        self.assertEqual(sync.sync_all(), [])
        store.close()

    def test_empty_session_uploads_valid_empty_manifest(self):
        empty_db = self.dir / "empty.db"
        store = SessionStore(empty_db, cipher=C.AesGcmCipher(os.urandom(32)))
        sid = store.start(["ecg"]); store.finish(sid, "stopped")
        self.db, self.cipher = empty_db, store.cipher
        sync = self.sync()
        self.assertTrue(sync.sync_session(sid)["complete"])
        self.assertEqual(sync.cloud.verify_session(sid)["packets"], 0)
        store.close()

    def test_csv_export_from_cloud_matches_local_export(self):
        sync, _ = self.upload()
        a, b = self.dir / "local.csv", self.dir / "cloud.csv"
        self.store.export_csv(self.sid, a)
        sync.cloud.export_csv(self.sid, b)
        self.assertEqual(a.read_text(), b.read_text())

    def test_access_denied_on_missing_object_is_reported_not_raised(self):
        """Least-privilege IAM can turn a missing key into 403; verify must still report, not crash."""
        from botocore.exceptions import ClientError

        class DenyingS3(FlakyS3):
            def get_object(self, **kw):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
        cloud = C.CloudStore(self.cfg, s3=DenyingS3(self.s3), table=boto3.resource("dynamodb").Table(TABLE))
        report = cloud.verify_session(self.sid)
        self.assertFalse(report["ok"])
        self.assertIn("AccessDenied", report["problems"][0])

    def test_benchmark_prefix_is_inside_template_object_prefix(self):
        import re
        import storage_benchmark
        template = (C.ROOT / "cloud" / "template.yaml").read_text()
        object_prefix = re.search(r"ObjectPrefix:\s*\n\s*Type: String\s*\n\s*Default: (\S+)", template).group(1)
        source = Path(storage_benchmark.__file__).read_text()
        parser_prefix = re.search(r'"--prefix", default="([^"]+)"', source).group(1)
        self.assertTrue(parser_prefix.startswith(object_prefix + "/"), parser_prefix)

    def test_hybrid_uploader_background_thread(self):
        notes = []
        cfg, db, cipher, s3 = self.cfg, self.db, self.cipher, self.s3

        def make():
            cloud = C.CloudStore(cfg, s3=s3, table=boto3.resource("dynamodb").Table(TABLE))
            return C.CloudSync(cloud, C.LocalSessions(db, cipher), C.SyncState(cfg.sync_db))
        up = C.HybridUploader(make, self.sid, interval=0.05, backoff=0.01, notify=notes.append).start()
        time.sleep(0.2)
        up.finish()
        result = up.join(10)
        self.assertTrue(result["complete"], (result, notes))

    def test_config_loading(self):
        env = {k: os.environ.pop(v) for k, v in C.CloudConfig._ENV.items() if v in os.environ}
        try:
            with self.assertRaises(C.CloudConfigError):
                C.CloudConfig.load(config_file=None)
            cfg_file = self.dir / "cfg.json"
            cfg_file.write_text(json.dumps({"bucket": "b1", "prefix": "/p/", "chunk_packets": 5}))
            cfg = C.CloudConfig.load(config_file=cfg_file, table="t")
            self.assertEqual((cfg.bucket, cfg.prefix, cfg.chunk_packets, cfg.table), ("b1", "p", 5, "t"))
            os.environ["HPI_S3_BUCKET"] = "from-env"
            self.assertEqual(C.CloudConfig.load(config_file=cfg_file).bucket, "from-env")
            del os.environ["HPI_S3_BUCKET"]
            cfg_file.write_text(json.dumps({"bucket": "b1", "secret_access_key": "x"}))
            with self.assertRaises(C.CloudConfigError):  # credentials never belong in this file
                C.CloudConfig.load(config_file=cfg_file)
        finally:
            for k, v in env.items():
                os.environ[C.CloudConfig._ENV[k]] = v


@mock_aws
class KmsTests(unittest.TestCase):
    def test_kms_envelope_roundtrip_and_context_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            s3 = boto3.client("s3"); s3.create_bucket(Bucket=BUCKET)
            kms = boto3.client("kms")
            key_id = kms.create_key()["KeyMetadata"]["KeyId"]
            cfg = C.CloudConfig(bucket=BUCKET, kms_key_id=key_id, chunk_packets=10,
                                key_file=str(tmp / "unused.key"), sync_db=str(tmp / "s.db"))
            store, sid = make_local(tmp / "l.db", packets=23)
            cloud = C.CloudStore(cfg, s3=s3, kms=kms)
            sync = C.CloudSync(cloud, C.LocalSessions(tmp / "l.db", store.cipher), C.SyncState(cfg.sync_db))
            self.assertTrue(sync.sync_session(sid)["complete"])
            self.assertFalse((tmp / "unused.key").exists())  # no local key needed with KMS
            manifest = cloud.read_manifest(sid)
            self.assertEqual(manifest["key"]["type"], "kms")
            self.assertEqual(s3.head_object(Bucket=BUCKET, Key=cloud._key(sid, "chunk-000000.bin"))
                             ["ServerSideEncryption"], "aws:kms")
            self.assertEqual(list(cloud.packets(sid)), list(store.packets(sid)))
            # The wrapped key only unwraps for its own session (KMS encryption context).
            other = str(uuid.uuid4())
            with self.assertRaises(Exception):
                C.KmsKeyProvider(kms).unwrap(manifest["key"], other)
            store.close()


class CipherTests(unittest.TestCase):
    def test_aead_properties(self):
        c = C.AesGcmCipher(os.urandom(32))
        blob = c.encrypt(b"abc", b"ctx")
        self.assertEqual(c.decrypt(blob, b"ctx"), b"abc")
        with self.assertRaises(C.CloudIntegrityError):
            c.decrypt(blob, b"other")
        self.assertNotEqual(c.encrypt(b"abc", b"ctx"), blob)  # fresh nonce
        with self.assertRaises(ValueError):
            C.AesGcmCipher(b"short")

    def test_local_key_file_created_once_and_per_session_keys_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k" / "cloud.key"
            a = C.LocalKeyProvider(path)
            b = C.LocalKeyProvider(path)  # reuses the file
            s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
            k1, info = a.new(s1)
            self.assertEqual(b.unwrap(info, s1), k1)
            self.assertNotEqual(a.new(s2)[0], k1)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.unlink()
            with self.assertRaises(C.CloudConfigError):
                C.LocalKeyProvider(path, create=False)


if __name__ == "__main__":
    unittest.main()

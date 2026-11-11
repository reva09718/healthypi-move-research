"""AWS cloud storage for research sessions: encrypted S3 chunks + DynamoDB index.

Layout in S3 (``<prefix>`` defaults to ``hpi-research``)::

    <prefix>/sessions/<session-uuid>/chunk-000000.bin   AES-256-GCM(gzip(NDJSON packets))
    <prefix>/sessions/<session-uuid>/meta.bin           AES-256-GCM(JSON: note + connection events)
    <prefix>/sessions/<session-uuid>/manifest.json      plaintext index + SHA-256 of every chunk
                                                        + authentication tag (written last)

Security model
- Data is encrypted *on the laptop* before upload (client-side), then S3 encrypts it
  again at rest (SSE-S3 or SSE-KMS). Transport is TLS (boto3 default).
- Local recordings use Windows DPAPI, which cannot be decrypted anywhere else, so the
  uploader decrypts them locally and re-encrypts with a portable AES-256-GCM key.
- Each chunk is bound to (session id, chunk index) as AEAD associated data, so chunks
  cannot be swapped between sessions or positions. The manifest carries an AES-GCM tag
  over its canonical JSON, so removing or editing chunk entries is detected.
- Key = local key file (HKDF-derived per session) or AWS KMS data key per session.
  A lost key means the cloud data cannot be read: back the key file up separately.

Nothing here invents data: a session is either uploaded completely and verifiably or it
stays marked "uploading" with the last error recorded, and can be resumed.
"""
import base64
import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = "hpi-cloud-v1"
CIPHER_NAME = "aes256gcm-v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY_FILE = ROOT / "data" / "cloud.key"
DEFAULT_SYNC_DB = ROOT / "data" / "cloud_sync.db"
DEFAULT_CONFIG_FILE = ROOT / "data" / "cloud_config.json"


class CloudConfigError(RuntimeError):
    """Missing or invalid cloud configuration."""


class CloudIntegrityError(ValueError):
    """Cloud data failed authentication, hash, completeness or continuity checks."""


# --------------------------------------------------------------------------- config

@dataclass
class CloudConfig:
    bucket: str
    prefix: str = "hpi-research"
    region: str = None
    table: str = None            # DynamoDB table name; None = no session index
    kms_key_id: str = None       # None = local key file; set = per-session KMS data keys
    key_file: str = str(DEFAULT_KEY_FILE)
    endpoint_url: str = None     # only for LocalStack/testing
    chunk_packets: int = 2000    # packets per S3 object (~1.5-3 min at the default mix)
    sync_db: str = str(DEFAULT_SYNC_DB)

    _ENV = {"bucket": "HPI_S3_BUCKET", "prefix": "HPI_S3_PREFIX", "region": "HPI_AWS_REGION",
            "table": "HPI_DDB_TABLE", "kms_key_id": "HPI_KMS_KEY_ID",
            "key_file": "HPI_CLOUD_KEY_FILE", "endpoint_url": "HPI_AWS_ENDPOINT",
            "chunk_packets": "HPI_CHUNK_PACKETS", "sync_db": "HPI_CLOUD_SYNC_DB"}

    @classmethod
    def load(cls, config_file=DEFAULT_CONFIG_FILE, **overrides):
        """Priority: explicit overrides > environment variables > data/cloud_config.json.

        AWS credentials are never read from here: boto3's normal chain applies
        (``aws configure``, SSO profile, environment, instance role).
        """
        values = {}
        path = Path(config_file) if config_file else None
        if path and path.is_file():
            values.update(json.loads(path.read_text(encoding="utf-8")))
        for name, env in cls._ENV.items():
            if os.environ.get(env):
                values[name] = os.environ[env]
        values.update({k: v for k, v in overrides.items() if v is not None})
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise CloudConfigError("Unknown cloud setting(s): " + ", ".join(sorted(unknown)))
        if not values.get("bucket"):
            raise CloudConfigError("No S3 bucket configured. Set HPI_S3_BUCKET, pass --bucket, "
                                   "or create data/cloud_config.json.")
        values["chunk_packets"] = int(values.get("chunk_packets", 2000))
        if values["chunk_packets"] < 1:
            raise CloudConfigError("chunk_packets must be at least 1")
        values["prefix"] = str(values.get("prefix", "hpi-research")).strip("/")
        return cls(**values)


# --------------------------------------------------------------------------- crypto

class AesGcmCipher:
    """AES-256-GCM. Blob layout: 12-byte random nonce || ciphertext || 16-byte tag."""
    name = CIPHER_NAME

    def __init__(self, key):
        if len(key) != 32:
            raise ValueError("AES-256 needs a 32-byte key")
        self._aead = AESGCM(bytes(key))

    def encrypt(self, data, aad=b""):
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, data, aad or None)

    def decrypt(self, blob, aad=b""):
        if len(blob) < 28:
            raise CloudIntegrityError("Encrypted data is too short")
        try:
            return self._aead.decrypt(blob[:12], blob[12:], aad or None)
        except Exception as exc:  # cryptography.exceptions.InvalidTag
            raise CloudIntegrityError("Authentication failed: wrong key or modified data") from exc


def _aad(sid, part):
    return ("%s|%s|%s" % (FORMAT, sid, part)).encode()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _check_sid(sid):
    try:
        return str(uuid.UUID(str(sid)))
    except ValueError:
        raise ValueError("Session id must be a UUID") from None


def load_or_create_key(path=DEFAULT_KEY_FILE, create=True):
    """32-byte master key stored as 64 hex characters (file mode 0600 where supported)."""
    path = Path(path)
    if path.is_file():
        key = bytes.fromhex(path.read_text().strip())
        if len(key) != 32:
            raise CloudConfigError("Key file %s must hold 64 hex characters" % path)
        return key
    if not create:
        raise CloudConfigError("Key file %s not found. This session was encrypted with it; "
                               "restore it from your backup." % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex())
    return key


class LocalKeyProvider:
    """Per-session key = HKDF-SHA256(master key file, info=session id)."""
    kind = "local"

    def __init__(self, path=DEFAULT_KEY_FILE, create=True):
        self.master = load_or_create_key(path, create)
        self.key_id = hashlib.sha256(self.master).hexdigest()[:16]

    def _derive(self, sid):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=("%s|%s" % (FORMAT, sid)).encode()).derive(self.master)

    def new(self, sid):
        return self._derive(sid), {"type": "local", "key_id": self.key_id}

    def unwrap(self, info, sid):
        if info.get("key_id") != self.key_id:
            raise CloudIntegrityError("Local key does not match the one used for this session "
                                      "(key id %s)" % info.get("key_id"))
        return self._derive(sid)


class KmsKeyProvider:
    """One AWS KMS data key per session (envelope encryption); the KMS-wrapped key is stored
    in the manifest, so reading back needs permission to call kms:Decrypt on the CMK."""
    kind = "kms"

    def __init__(self, kms, key_id=None):
        self.kms, self.key_id = kms, key_id

    @staticmethod
    def _context(sid):
        return {"hpi-session": sid}

    def new(self, sid):
        r = self.kms.generate_data_key(KeyId=self.key_id, KeySpec="AES_256",
                                       EncryptionContext=self._context(sid))
        return r["Plaintext"], {"type": "kms", "key_id": self.key_id,
                                "wrapped": base64.b64encode(r["CiphertextBlob"]).decode()}

    def unwrap(self, info, sid):
        r = self.kms.decrypt(CiphertextBlob=base64.b64decode(info["wrapped"]),
                             EncryptionContext=self._context(sid))
        return r["Plaintext"]


# --------------------------------------------------------------------------- helpers

def network_errors():
    """Exceptions that mean 'try again later' (offline, throttled, 5xx), not 'bug'."""
    from botocore.exceptions import BotoCoreError, ClientError
    return (BotoCoreError, ClientError, OSError)


def _ddb_number(value):
    return Decimal(str(value)) if isinstance(value, float) else value


def _packet_line(packet):
    return json.dumps(packet, allow_nan=False, separators=(",", ":")).encode()


# --------------------------------------------------------------------------- cloud

class CloudStore:
    """S3 + DynamoDB access. Create one per thread (boto3 *resources* are not thread-safe)."""

    def __init__(self, cfg, s3=None, table=None, kms=None):
        self.cfg = cfg
        if s3 is None or (cfg.table and table is None) or (cfg.kms_key_id and kms is None):
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise CloudConfigError("Install cloud dependencies: python -m pip install "
                                       "-r pc_tools/requirements-cloud.txt") from exc
            session = boto3.session.Session(region_name=cfg.region)
            conf = Config(retries={"max_attempts": 6, "mode": "standard"},
                          connect_timeout=10, read_timeout=60)
            kw = dict(endpoint_url=cfg.endpoint_url) if cfg.endpoint_url else {}
            s3 = s3 or session.client("s3", config=conf, **kw)
            if cfg.table and table is None:
                table = session.resource("dynamodb", config=conf, **kw).Table(cfg.table)
            if cfg.kms_key_id and kms is None:
                kms = session.client("kms", config=conf, **kw)
            self._session, self._conf, self._kw = session, conf, kw
        else:
            self._session = None
        self.s3, self.table, self.kms = s3, table, kms
        self._local_provider = None

    # ---- keys
    def _local(self, create):
        if self._local_provider is None:
            self._local_provider = LocalKeyProvider(self.cfg.key_file, create=create)
        return self._local_provider

    def _kms(self):
        if self.kms is None:
            self.kms = self._session.client("kms", config=self._conf, **self._kw)
        return self.kms

    def upload_provider(self):
        if self.cfg.kms_key_id:
            return KmsKeyProvider(self.kms, self.cfg.kms_key_id)
        return self._local(create=True)

    def provider_for(self, info):
        """Provider able to unwrap a key described by a manifest/sync-state ``key`` entry."""
        if info.get("type") == "kms":
            return KmsKeyProvider(self._kms(), info.get("key_id"))
        if info.get("type") == "local":
            return self._local(create=False)
        raise CloudIntegrityError("Unknown key type: %r" % info.get("type"))

    # ---- object helpers
    def _key(self, sid, name):
        return "%s/sessions/%s/%s" % (self.cfg.prefix, _check_sid(sid), name)

    def _sse(self):
        if self.cfg.kms_key_id:
            return dict(ServerSideEncryption="aws:kms", SSEKMSKeyId=self.cfg.kms_key_id)
        return dict(ServerSideEncryption="AES256")

    def _put(self, key, body, **extra):
        self.s3.put_object(Bucket=self.cfg.bucket, Key=key, Body=body, **self._sse(), **extra)

    def _get(self, key):
        return self.s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read()

    # ---- DynamoDB index (optional)
    def _index(self, sid, **attrs):
        if self.table is None:
            return
        item = {"session_id": _check_sid(sid), "format": FORMAT}
        item.update({k: _ddb_number(v) for k, v in attrs.items() if v is not None})
        self.table.put_item(Item=item)

    # ---- upload
    def register_session(self, sid, row):
        self._index(sid, status="uploading", local_status=row["status"], source=row["source"],
                    started_utc=row["started_utc"], selected=row["selected"],
                    registered_utc=time.time())

    def upload_chunk(self, sid, index, packets, cipher):
        """Compress, encrypt and upload one chunk; returns its manifest record."""
        ndjson = b"\n".join(_packet_line(p) for p in packets)
        plain = gzip.compress(ndjson, compresslevel=6, mtime=0)
        blob = cipher.encrypt(plain, _aad(sid, index))
        digest = hashlib.sha256(blob).hexdigest()
        name = "chunk-%06d.bin" % index
        started = time.monotonic()
        self._put(self._key(sid, name), blob, Metadata={
            "packets": str(len(packets)), "first-seq": str(packets[0]["seq"]),
            "last-seq": str(packets[-1]["seq"]), "sha256": digest})
        return dict(index=index, object=name, packets=len(packets),
                    first_seq=packets[0]["seq"], last_seq=packets[-1]["seq"],
                    raw_bytes=len(ndjson), bytes=len(blob), sha256=digest,
                    upload_s=round(time.monotonic() - started, 4))

    def finalize_session(self, sid, row, note, events, chunks, key_info, cipher):
        """Upload private metadata + the authenticated manifest, then mark complete."""
        meta = cipher.encrypt(json.dumps({"note": note, "events": events},
                                         allow_nan=False).encode(), _aad(sid, "meta"))
        self._put(self._key(sid, "meta.bin"), meta)
        chunks = [{k: v for k, v in c.items() if k != "upload_s"} for c in chunks]
        manifest = dict(format=FORMAT, session_id=_check_sid(sid), cipher=CIPHER_NAME,
                        key=key_info, started_utc=row["started_utc"], ended_utc=row["ended_utc"],
                        status=row["status"], source=row["source"], device=row["device"],
                        selected=row["selected"], chunk_packets=row["chunk_packets"],
                        chunks=chunks, total_packets=sum(c["packets"] for c in chunks),
                        total_bytes=sum(c["bytes"] for c in chunks),
                        meta_sha256=hashlib.sha256(meta).hexdigest(), uploaded_utc=time.time())
        canon = _canonical(manifest)
        manifest["tag"] = base64.b64encode(cipher.encrypt(b"", canon)).decode()
        body = json.dumps(manifest, indent=1, sort_keys=True).encode()
        self._put(self._key(sid, "manifest.json"), body, ContentType="application/json")
        self._index(sid, status="complete", local_status=row["status"], source=row["source"],
                    started_utc=row["started_utc"], ended_utc=row["ended_utc"],
                    selected=row["selected"], total_packets=manifest["total_packets"],
                    total_bytes=manifest["total_bytes"], chunks=len(chunks),
                    manifest_key=self._key(sid, "manifest.json"),
                    manifest_sha256=hashlib.sha256(body).hexdigest(),
                    uploaded_utc=manifest["uploaded_utc"])
        return manifest

    # ---- download / verify
    def read_manifest(self, sid):
        try:
            return json.loads(self._get(self._key(sid, "manifest.json")))
        except self.s3.exceptions.NoSuchKey:
            raise CloudIntegrityError("No complete manifest for session %s (upload unfinished?)" % sid) from None

    def _cipher_for(self, manifest, sid):
        if manifest.get("format") != FORMAT or manifest.get("session_id") != sid:
            raise CloudIntegrityError("Manifest does not belong to this session or format")
        info = manifest["key"]
        cipher = AesGcmCipher(self.provider_for(info).unwrap(info, sid))
        body = {k: v for k, v in manifest.items() if k != "tag"}
        cipher.decrypt(base64.b64decode(manifest.get("tag", "")), _canonical(body))
        return cipher

    def iter_chunks(self, sid):
        """Yield one verified list of packets per chunk. Raises CloudIntegrityError on any problem."""
        sid = _check_sid(sid)
        manifest = self.read_manifest(sid)
        cipher = self._cipher_for(manifest, sid)
        previous, total = -1, 0
        for chunk in manifest["chunks"]:
            try:
                blob = self._get(self._key(sid, chunk["object"]))
            except self.s3.exceptions.NoSuchKey:
                raise CloudIntegrityError("Missing chunk %s" % chunk["object"]) from None
            if hashlib.sha256(blob).hexdigest() != chunk["sha256"]:
                raise CloudIntegrityError("Chunk %s hash does not match the manifest" % chunk["object"])
            plain = gzip.decompress(cipher.decrypt(blob, _aad(sid, chunk["index"])))
            packets = [json.loads(line) for line in plain.split(b"\n")]
            if (len(packets) != chunk["packets"] or packets[0]["seq"] != chunk["first_seq"]
                    or packets[-1]["seq"] != chunk["last_seq"]):
                raise CloudIntegrityError("Chunk %s content differs from the manifest" % chunk["object"])
            for p in packets:
                if p["session_id"] != sid or p["seq"] <= previous:
                    raise CloudIntegrityError("Packet order/session mismatch in %s" % chunk["object"])
                previous = p["seq"]
            total += len(packets)
            yield packets
        if total != manifest["total_packets"]:
            raise CloudIntegrityError("Packet total does not match the manifest")

    def packets(self, sid):
        for chunk in self.iter_chunks(sid):
            yield from chunk

    def session_meta(self, sid):
        """Decrypted note + connection events."""
        sid = _check_sid(sid)
        manifest = self.read_manifest(sid)
        cipher = self._cipher_for(manifest, sid)
        blob = self._get(self._key(sid, "meta.bin"))
        if hashlib.sha256(blob).hexdigest() != manifest["meta_sha256"]:
            raise CloudIntegrityError("meta.bin does not match the manifest")
        return json.loads(cipher.decrypt(blob, _aad(sid, "meta")))

    def verify_session(self, sid):
        """Download, authenticate and cross-check everything. Never raises on bad data."""
        report = dict(session_id=sid, ok=False, packets=0, chunks=0, problems=[], warnings=[])
        try:
            for chunk in self.iter_chunks(sid):
                report["chunks"] += 1
                report["packets"] += len(chunk)
            self.session_meta(sid)
            manifest = self.read_manifest(sid)
            listed = {self._key(sid, c["object"]) for c in manifest["chunks"]}
            listed |= {self._key(sid, "manifest.json"), self._key(sid, "meta.bin")}
            prefix = self._key(sid, "")
            token = None
            while True:
                kw = dict(Bucket=self.cfg.bucket, Prefix=prefix)
                if token:
                    kw["ContinuationToken"] = token
                page = self.s3.list_objects_v2(**kw)
                for obj in page.get("Contents", []):
                    if obj["Key"] not in listed:
                        report["warnings"].append("unlisted object " + obj["Key"])
                if not page.get("IsTruncated"):
                    break
                token = page["NextContinuationToken"]
            report["ok"] = True
        except (CloudIntegrityError, KeyError, ValueError) as exc:
            report["problems"].append("%s: %s" % (type(exc).__name__, exc))
        return report

    def export_csv(self, sid, destination):
        """Same plaintext CSV layout as SessionStore.export_csv, from verified cloud data."""
        from research_storage import SessionStore

        class _Source:
            packets = staticmethod(lambda s: self.packets(s))
        SessionStore.export_csv(_Source(), _check_sid(sid), destination)

    def list_sessions(self):
        """Complete sessions from the DynamoDB index, or S3 prefixes when no table is set."""
        if self.table is not None:
            items, kw = [], {}
            while True:
                page = self.table.scan(**kw)
                items += page["Items"]
                if "LastEvaluatedKey" not in page:
                    return sorted(items, key=lambda i: i.get("started_utc", 0), reverse=True)
                kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        out, token = [], None
        while True:
            kw = dict(Bucket=self.cfg.bucket, Prefix=self.cfg.prefix + "/sessions/", Delimiter="/")
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            out += [{"session_id": p["Prefix"].rstrip("/").rsplit("/", 1)[-1]}
                    for p in page.get("CommonPrefixes", [])]
            if not page.get("IsTruncated"):
                return out
            token = page["NextContinuationToken"]


# --------------------------------------------------------------------------- local side

class LocalSessions:
    """Read-only view of the encrypted local SQLite recording DB (safe next to the live writer)."""

    def __init__(self, db_path, cipher):
        self.cipher = cipher
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        self.db = sqlite3.connect(uri, uri=True, timeout=30)

    def close(self):
        self.db.close()

    def session(self, sid):
        r = self.db.execute("SELECT id,started_utc,ended_utc,status,source,device,selected,note,"
                            "cipher FROM research_sessions WHERE id=?", (sid,)).fetchone()
        if not r:
            raise ValueError("Session %s is not in the local database" % sid)
        if r[8] != self.cipher.name:
            raise ValueError("Session uses a different local encryption scheme")
        return dict(id=r[0], started_utc=r[1], ended_utc=r[2], status=r[3], source=r[4],
                    device=r[5], selected=json.loads(r[6]), note=r[7])

    def session_ids(self):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM research_sessions ORDER BY started_utc")]

    def max_seq(self, sid):
        return self.db.execute("SELECT MAX(seq) FROM research_packets WHERE session_id=?",
                               (sid,)).fetchone()[0]

    def packets(self, sid, low, high):
        """Decrypted packets with low <= seq < high, with the same integrity check as SessionStore."""
        out = []
        for seq, signal, received, blob in self.db.execute(
                "SELECT seq,signal,recv_utc,encrypted FROM research_packets "
                "WHERE session_id=? AND seq>=? AND seq<? ORDER BY seq", (sid, low, high)):
            packet = json.loads(self.cipher.decrypt(blob))
            if (packet["session_id"] != sid or packet["seq"] != seq or
                    packet["signal"] != signal or packet["recv_utc"] != received):
                raise ValueError("Local packet metadata does not match its protected contents")
            out.append(packet)
        return out

    def events(self, sid):
        return [list(r) for r in self.db.execute(
            "SELECT utc,elapsed_s,kind,details FROM research_events WHERE session_id=? ORDER BY id",
            (sid,))]


class SyncState:
    """Small local SQLite file remembering upload progress so uploads resume after a crash."""

    def __init__(self, path=DEFAULT_SYNC_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS cloud_sessions (
            session_id TEXT PRIMARY KEY, state TEXT NOT NULL, next_chunk INTEGER NOT NULL,
            chunk_packets INTEGER NOT NULL, key_info TEXT NOT NULL,
            last_error TEXT, updated_utc REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS cloud_chunks (
            session_id TEXT NOT NULL, idx INTEGER NOT NULL, record TEXT NOT NULL,
            PRIMARY KEY(session_id, idx));""")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, sid):
        r = self.db.execute("SELECT state,next_chunk,chunk_packets,key_info,last_error "
                            "FROM cloud_sessions WHERE session_id=?", (sid,)).fetchone()
        return None if r is None else dict(state=r[0], next_chunk=r[1], chunk_packets=r[2],
                                           key_info=json.loads(r[3]), last_error=r[4])

    def create(self, sid, key_info, chunk_packets):
        self.db.execute("INSERT INTO cloud_sessions VALUES (?,?,?,?,?,NULL,?)",
                        (sid, "uploading", 0, chunk_packets, json.dumps(key_info), time.time()))
        self.db.commit()

    def add_chunk(self, sid, index, record=None):
        """Record a finished chunk (or an empty slot when record is None) and advance."""
        if record is not None:
            self.db.execute("INSERT OR REPLACE INTO cloud_chunks VALUES (?,?,?)",
                            (sid, index, json.dumps(record)))
        self.db.execute("UPDATE cloud_sessions SET next_chunk=?, last_error=NULL, updated_utc=? "
                        "WHERE session_id=?", (index + 1, time.time(), sid))
        self.db.commit()

    def chunks(self, sid):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT record FROM cloud_chunks WHERE session_id=? ORDER BY idx", (sid,))]

    def set_state(self, sid, state=None, error=None):
        if state:
            self.db.execute("UPDATE cloud_sessions SET state=?, last_error=?, updated_utc=? "
                            "WHERE session_id=?", (state, error, time.time(), sid))
        else:
            self.db.execute("UPDATE cloud_sessions SET last_error=?, updated_utc=? "
                            "WHERE session_id=?", (error, time.time(), sid))
        self.db.commit()

    def all(self):
        return self.db.execute("SELECT session_id,state,next_chunk,last_error "
                               "FROM cloud_sessions ORDER BY updated_utc").fetchall()


class CloudSync:
    """Local-first synchronisation: the local encrypted DB is always written first and is the
    source of truth; this copies it to AWS and can be retried at any time (idempotent)."""

    def __init__(self, cloud, local, state):
        self.cloud, self.local, self.state = cloud, local, state

    @classmethod
    def from_config(cls, cfg, local_db, local_cipher, **store_kwargs):
        """Build everything for the *current thread* (boto3 resources/SQLite are thread-bound)."""
        return cls(CloudStore(cfg, **store_kwargs), LocalSessions(local_db, local_cipher),
                   SyncState(cfg.sync_db))

    def close(self):
        self.local.close()
        self.state.close()

    def sync_session(self, sid, final=False):
        """Upload every *sealed* chunk; once the session has ended (or final=True) upload the
        remainder and the manifest. Network failures are returned, not raised.

        A chunk i holds packets with i*N <= seq < (i+1)*N and is sealed once a later packet
        is committed locally, so chunk contents never change after upload.
        """
        sid = _check_sid(sid)
        result = dict(session_id=sid, uploaded_chunks=0, complete=False, error=None)
        state = self.state.get(sid)
        if state and state["state"] == "complete":
            result["complete"] = True
            return result
        row = self.local.session(sid)
        closed = final or row["status"] != "recording"
        try:
            if state is None:
                n = self.cloud.cfg.chunk_packets
                key, info = self.cloud.upload_provider().new(sid)
                self.state.create(sid, info, n)
                state = self.state.get(sid)
                self.cloud.register_session(sid, row)
            else:
                n = state["chunk_packets"]
                info = state["key_info"]
                key = self.cloud.provider_for(info).unwrap(info, sid)
            cipher = AesGcmCipher(key)
            max_seq = self.local.max_seq(sid)
            last = -1 if max_seq is None else max_seq // n
            index = state["next_chunk"]
            while index <= last and (index < last or closed):
                packets = self.local.packets(sid, index * n, (index + 1) * n)
                record = self.cloud.upload_chunk(sid, index, packets, cipher) if packets else None
                self.state.add_chunk(sid, index, record)
                result["uploaded_chunks"] += record is not None
                index += 1
            if closed and index > last:
                row["chunk_packets"] = n
                self.cloud.finalize_session(sid, row, row["note"], self.local.events(sid),
                                            self.state.chunks(sid), info, cipher)
                self.state.set_state(sid, "complete")
                result["complete"] = True
        except network_errors() as exc:
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            if self.state.get(sid):
                self.state.set_state(sid, error=result["error"])
        return result

    def sync_all(self, include_active=False):
        """Upload every local session not yet complete in the cloud. Sessions still marked
        'recording' (live or abandoned after a crash) are only finalised with include_active."""
        results = []
        for sid in self.local.session_ids():
            st = self.state.get(sid)
            if st and st["state"] == "complete":
                continue
            results.append(self.sync_session(sid, final=include_active))
        return results


class HybridUploader:
    """Background thread for hybrid mode: uploads sealed chunks while recording continues, then
    finalises after Stop. Local recording never waits for the network. If the network stays
    down the session remains resumable (``cloud_sync.py sync`` or reopening the app)."""

    def __init__(self, make_sync, sid, interval=15.0, retries=6, backoff=2.0, notify=None):
        self.make_sync, self.sid = make_sync, sid
        self.interval, self.retries, self.backoff = interval, retries, backoff
        self.notify = notify or (lambda message: None)
        self._finish = threading.Event()
        self.result = None
        self.thread = threading.Thread(target=self._run, name="hpi-cloud-upload", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        """Request final upload after the local session has been saved (non-blocking)."""
        self._finish.set()

    def join(self, timeout=None):
        self.thread.join(timeout)
        return self.result

    def _run(self):
        try:
            sync = self.make_sync()
        except Exception as exc:
            self.notify("Cloud upload unavailable: %s" % exc)
            self.result = dict(session_id=self.sid, complete=False, error=str(exc))
            return
        try:
            while not self._finish.wait(self.interval):
                r = sync.sync_session(self.sid)
                if r["uploaded_chunks"]:
                    self.notify("Cloud: %d chunk(s) uploaded" % r["uploaded_chunks"])
                elif r["error"]:
                    self.notify("Cloud upload waiting (offline?): " + r["error"])
            delay = self.backoff
            for attempt in range(self.retries + 1):
                self.result = sync.sync_session(self.sid, final=True)
                if self.result["complete"]:
                    self.notify("Cloud upload complete for session " + self.sid)
                    return
                if attempt < self.retries:
                    self.notify("Cloud finalise failed (%s); retry in %.0fs" % (self.result["error"], delay))
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
            self.notify("Cloud upload incomplete; data is safe locally. Resume with: "
                        "python pc_tools/cloud_sync.py sync")
        except Exception as exc:  # programming/config error: surface, keep local data untouched
            self.result = dict(session_id=self.sid, complete=False, error=repr(exc))
            self.notify("Cloud upload failed: %r" % exc)
        finally:
            sync.close()

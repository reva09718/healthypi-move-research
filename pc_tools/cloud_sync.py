"""Command-line tool for AWS storage of research sessions.

    python pc_tools/cloud_sync.py keygen                 # create the local AES key file (back it up!)
    python pc_tools/cloud_sync.py sync                   # upload every finished local session
    python pc_tools/cloud_sync.py upload <session-id>    # upload one session (resumes if interrupted)
    python pc_tools/cloud_sync.py status                 # upload progress recorded on this laptop
    python pc_tools/cloud_sync.py list                   # sessions in the cloud
    python pc_tools/cloud_sync.py verify <session-id>    # download, decrypt, check hashes/continuity
    python pc_tools/cloud_sync.py download <session-id> out.csv   # plaintext CSV from cloud data

Configuration: HPI_S3_BUCKET (required), HPI_DDB_TABLE, HPI_AWS_REGION, HPI_KMS_KEY_ID,
HPI_S3_PREFIX, HPI_CLOUD_KEY_FILE or data/cloud_config.json; the matching command-line flags
override them. AWS credentials come from the standard boto3 chain (``aws configure`` / SSO).
Local recordings are decrypted with Windows DPAPI, so uploading must run as the same Windows
user that recorded them.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_storage as C

DEFAULT_DB = C.ROOT / "data" / "research_sessions.db"


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bucket"); p.add_argument("--prefix"); p.add_argument("--region")
    p.add_argument("--table"); p.add_argument("--kms-key-id"); p.add_argument("--key-file")
    p.add_argument("--endpoint-url", help="LocalStack/testing only")
    p.add_argument("--chunk-packets", type=int)
    p.add_argument("--db", default=str(DEFAULT_DB), help="local encrypted recording database")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen")
    s = sub.add_parser("sync"); s.add_argument("--include-active", action="store_true",
        help="also finalise sessions still marked 'recording' (e.g. abandoned after a crash)")
    u = sub.add_parser("upload"); u.add_argument("session"); u.add_argument("--final", action="store_true")
    sub.add_parser("status"); sub.add_parser("list")
    v = sub.add_parser("verify"); v.add_argument("session")
    d = sub.add_parser("download"); d.add_argument("session"); d.add_argument("csv")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.command == "keygen":
        path = a.key_file or str(C.DEFAULT_KEY_FILE)
        existed = Path(path).is_file()
        C.load_or_create_key(path)
        print(("Key already exists: " if existed else "Created key: ") + path)
        print("Back this file up somewhere safe and separate. Without it, cloud data cannot be decrypted.")
        return 0
    cfg = C.CloudConfig.load(bucket=a.bucket, prefix=a.prefix, region=a.region, table=a.table,
                             kms_key_id=a.kms_key_id, key_file=a.key_file,
                             endpoint_url=a.endpoint_url, chunk_packets=a.chunk_packets)
    if a.command == "status":
        state = C.SyncState(cfg.sync_db)
        for sid, st, nxt, err in state.all():
            print("%s  %-9s next_chunk=%d%s" % (sid, st, nxt, "  last_error=" + err if err else ""))
        return 0
    cloud = C.CloudStore(cfg)
    if a.command == "list":
        for item in cloud.list_sessions():
            print(json.dumps(item, default=str, sort_keys=True))
        return 0
    if a.command == "verify":
        report = cloud.verify_session(a.session)
        print(json.dumps(report, indent=1))
        return 0 if report["ok"] else 2
    if a.command == "download":
        cloud.export_csv(a.session, a.csv)
        print("Wrote plaintext CSV (verified cloud data): " + a.csv)
        return 0
    from research_storage import WindowsCipher
    sync = C.CloudSync(cloud, C.LocalSessions(a.db, WindowsCipher()), C.SyncState(cfg.sync_db))
    try:
        results = ([sync.sync_session(a.session, final=a.final)] if a.command == "upload"
                   else sync.sync_all(include_active=a.include_active))
    finally:
        sync.close()
    failed = 0
    for r in results:
        print("%s uploaded_chunks=%d complete=%s%s" % (r["session_id"], r["uploaded_chunks"],
              r["complete"], "  ERROR " + r["error"] if r["error"] else ""))
        failed += not r["complete"]
    if not results:
        print("Nothing to upload.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

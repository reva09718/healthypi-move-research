"""Compare Local, Cloud (AWS) and Hybrid storage: latency, reliability, size, cost (task S7).

    python pc_tools/storage_benchmark.py --mock                  # dry run, mocked AWS (NOT representative)
    python pc_tools/storage_benchmark.py --hours 0.25            # real AWS, using HPI_S3_BUCKET etc.
    python pc_tools/storage_benchmark.py --hours 1 --outage-chunks 3 --out data/benchmarks

Packets are synthetic but follow the real recorder's mix (ECG 128 Hz in batches of 8, wrist PPG
25 Hz, finger PPG 100 Hz, ~31 packets/s). Data is written as fast as possible rather than in
real time, so "hours" only sets the data volume. Run it from the laptop you will record on, at
the network you will record on, and repeat a few times at different times of day.
Latency numbers from --mock only exercise the code path and must not be quoted.
Scalability and security are assessed in docs/AWS_STORAGE.md; this script measures what can be
measured: write latency, durability under an outage, size on disk/S3, integrity check time, cost.
"""
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_storage as C
from research_storage import PacketBuilder, SessionStore

MIX = (("ecg", 128, 8), ("ppg_wrist", 25, 5), ("ppg_finger", 100, 10))  # signal, Hz, samples/packet
PACKETS_PER_SECOND = sum(hz // n for _, hz, n in MIX)  # 16 + 5 + 10


def synthetic_packets(seconds):
    builder = PacketBuilder()
    for second in range(int(seconds)):
        for signal, hz, n in MIX:
            for k in range(hz // n):
                base = 100000 + (second * 37 + k * 11) % 5000
                yield signal, builder.make(signal, [base + j * 3 for j in range(n)], hz, bytes(n * 4))


def percentile(values, q):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1)]


def summarize_ms(samples):
    ms = [s * 1000 for s in samples]
    return dict(n=len(ms), p50_ms=round(percentile(ms, 50), 2), p95_ms=round(percentile(ms, 95), 2),
                max_ms=round(max(ms), 2) if ms else None, mean_ms=round(statistics.fmean(ms), 2) if ms else None)


def write_local(path, cipher, seconds, observer=None):
    """Record like the app does: append every packet, commit once per second of data."""
    store = SessionStore(path, cipher=cipher)
    sid = store.start([m[0] for m in MIX], source="benchmark")
    commits, count, t0, per_second = [], 0, time.monotonic(), 0
    for _, packet in synthetic_packets(seconds):
        store.append(sid, packet)
        count += 1
        per_second += 1
        if per_second == PACKETS_PER_SECOND:
            c0 = time.monotonic()
            store.flush()
            commits.append(time.monotonic() - c0)
            per_second = 0
            if observer:
                observer(store, sid, count)
    store.flush()
    store.finish(sid, "completed")
    wall = time.monotonic() - t0
    store.close()
    return sid, dict(packets=count, wall_s=round(wall, 2), packets_per_s=round(count / wall),
                     commit_latency=summarize_ms(commits), db_bytes=Path(path).stat().st_size)


def bench_cloud_upload(cloud, local_db, cipher, state_path, sid):
    sync = C.CloudSync(cloud, C.LocalSessions(local_db, cipher), C.SyncState(state_path))
    try:
        t0 = time.monotonic()
        result = sync.sync_session(sid)
        wall = time.monotonic() - t0
        chunks = sync.state.chunks(sid)
    finally:
        sync.close()
    if not result["complete"]:
        raise RuntimeError("Upload failed: %s" % result["error"])
    up = [c["upload_s"] for c in chunks]
    total = sum(c["bytes"] for c in chunks)
    return dict(chunks=len(chunks), wall_s=round(wall, 2), s3_object_bytes=total,
                raw_bytes=sum(c["raw_bytes"] for c in chunks),
                compression_ratio=round(sum(c["raw_bytes"] for c in chunks) / total, 2),
                chunk_upload_latency=summarize_ms(up),
                throughput_kib_s=round(total / 1024 / sum(up), 1) if sum(up) else None)


def bench_verify(cloud, sid):
    t0 = time.monotonic()
    report = cloud.verify_session(sid)
    return dict(ok=report["ok"], packets=report["packets"], wall_s=round(time.monotonic() - t0, 2),
                problems=report["problems"])


class OutageS3:
    """Wraps a client; while .down is set every put_object fails like a lost connection."""
    def __init__(self, real):
        self.real, self.down = real, False

    def put_object(self, **kw):
        if self.down:
            from botocore.exceptions import EndpointConnectionError
            raise EndpointConnectionError(endpoint_url="simulated-outage")
        return self.real.put_object(**kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


def bench_hybrid(cfg, make_cloud, s3_real, cipher, tmp, seconds, outage_chunks):
    """Record locally while a second connection uploads sealed chunks every ~chunk. Midway the
    network drops for `outage_chunks` chunks; recording must continue and the backlog must drain."""
    flaky = OutageS3(s3_real)
    db = Path(tmp) / "hybrid.db"
    sync_holder = {}
    log = dict(uploaded_during_recording=0, failed_attempts=0, max_backlog_chunks=0,
               seal_to_upload_s=[])
    n = cfg.chunk_packets
    sealed_at = {}

    def observer(store, sid, count):
        # Called after every per-second commit. Chunk i is sealed once a later packet is
        # committed, so after `count` packets chunks 0 .. (count-1)//n - 1 are sealed.
        if "sync" not in sync_holder:
            sync_holder["sync"] = C.CloudSync(make_cloud(flaky), C.LocalSessions(db, cipher),
                                              C.SyncState(Path(tmp) / "hybrid_sync.db"))
        sync = sync_holder["sync"]
        now = time.monotonic()
        sealed = (count - 1) // n
        for idx in range(sealed):
            sealed_at.setdefault(idx, now)
        total_chunks = max(1, math.ceil(PACKETS_PER_SECOND * seconds / n))
        start = int(total_chunks * 0.3)
        flaky.down = outage_chunks > 0 and start <= sealed < start + outage_chunks
        state = sync.state.get(sid)
        uploaded = state["next_chunk"] if state else 0
        if sealed > uploaded:  # something is waiting: try now (the app does this on a timer)
            r = sync.sync_session(sid)
            log["uploaded_during_recording"] += r["uploaded_chunks"]
            log["failed_attempts"] += bool(r["error"])
            uploaded = sync.state.get(sid)["next_chunk"]
        log["max_backlog_chunks"] = max(log["max_backlog_chunks"], sealed - uploaded)
        for idx in [i for i in sealed_at if i < uploaded]:
            log["seal_to_upload_s"].append(time.monotonic() - sealed_at.pop(idx))

    sid, local = write_local(db, cipher, seconds, observer)
    outage_seen = log["failed_attempts"] > 0
    flaky.down = False
    sync = sync_holder.get("sync") or C.CloudSync(make_cloud(flaky), C.LocalSessions(db, cipher),
                                                  C.SyncState(Path(tmp) / "hybrid_sync.db"))
    t0 = time.monotonic()
    result = sync.sync_session(sid)  # after Stop: drain backlog + finalise
    drain = time.monotonic() - t0
    verify = bench_verify(sync.cloud, sid)
    sync.close()
    local_count = local["packets"]
    return dict(local=local, uploaded_during_recording_chunks=log["uploaded_during_recording"],
                failed_upload_attempts=log["failed_attempts"], outage_exercised=outage_seen,
                max_backlog_chunks=log["max_backlog_chunks"],
                seal_to_cloud_lag=summarize_ms(log["seal_to_upload_s"]),
                post_stop_drain_s=round(drain, 2), complete=result["complete"],
                cloud_verify=verify, packets_lost=local_count - verify["packets"])


def cost_model(pricing, cloud_bytes_per_hour, chunk_packets, hours_per_month, retention_months, kms):
    chunks_per_hour = PACKETS_PER_SECOND * 3600 / chunk_packets
    gb_month_stored = cloud_bytes_per_hour * hours_per_month * retention_months / 1e9
    puts = (chunks_per_hour + 3) * hours_per_month  # chunks + meta + manifest per session (~1 session/h)
    ddb_writes = 2 * hours_per_month
    cost = dict(
        s3_storage=gb_month_stored * pricing["s3_storage_gb_month"],
        s3_requests=puts / 1000 * pricing["s3_put_per_1000"],
        dynamodb=ddb_writes / 1e6 * pricing["ddb_write_per_million"],
        kms=(hours_per_month / 10000 * pricing["kms_request_per_10000"] + pricing["kms_key_month"]) if kms else 0.0)
    cost["total_usd_per_month"] = round(sum(cost.values()), 4)
    return {k: round(v, 4) for k, v in cost.items()}


def render_markdown(r):
    loc, hyb, cloud = r["local"], r["hybrid"], r["cloud_direct"]
    lines = ["# Storage benchmark", "",
             "Target: **%s**%s" % (r["target"], " - NOT representative of real AWS" if r["target"].startswith("mock") else ""),
             "Data: %s s of synthetic recording = %d packets (%.1f MB ciphertext in S3)." % (
                 r["seconds"], loc["packets"], cloud["upload"]["s3_object_bytes"] / 1e6), "",
             "| Metric | Local | Cloud only (upload after Stop) | Hybrid |", "|---|---|---|---|",
             "| Write latency, commit p50 / p95 (ms) | %s / %s | n/a | %s / %s |" % (
                 loc["commit_latency"]["p50_ms"], loc["commit_latency"]["p95_ms"],
                 hyb["local"]["commit_latency"]["p50_ms"], hyb["local"]["commit_latency"]["p95_ms"]),
             "| Chunk upload latency p50 / p95 (ms) | n/a | %s / %s | see lag |" % (
                 cloud["upload"]["chunk_upload_latency"]["p50_ms"], cloud["upload"]["chunk_upload_latency"]["p95_ms"]),
             "| Time until data is in the cloud after Stop (s) | n/a | %s | %s |" % (
                 cloud["upload"]["wall_s"], hyb["post_stop_drain_s"]),
             "| Seal-to-cloud lag p50 / p95 (ms) | n/a | n/a | %s / %s |" % (
                 hyb["seal_to_cloud_lag"]["p50_ms"], hyb["seal_to_cloud_lag"]["p95_ms"]),
             "| Packets lost during simulated outage | 0 (local only) | all unsent data if the laptop dies | %d |" % hyb["packets_lost"],
             "| Max chunks waiting during outage (failed attempts) | n/a | n/a | %d (%d) |" % (
                 hyb["max_backlog_chunks"], hyb["failed_upload_attempts"]),
             "| Integrity check (download + verify) | n/a (DB checks on read) | ok=%s in %s s | ok=%s |" % (
                 cloud["verify"]["ok"], cloud["verify"]["wall_s"], hyb["cloud_verify"]["ok"]),
             "| Size (synthetic data; real sensor data compresses less) | %.1f MB SQLite | %.1f MB (compression %.1fx) | both |" % (
                 loc["db_bytes"] / 1e6, cloud["upload"]["s3_object_bytes"] / 1e6, cloud["upload"]["compression_ratio"]), "",
             "## Cost per month (%s)" % ("UNVERIFIED unit prices - edit cloud/pricing.json" if not r["pricing_verified_on"]
                                          else "prices checked " + r["pricing_verified_on"]), "",
             "Scenario: %s h recorded per month, kept %s months. Sizes come from synthetic data, so treat cost as a lower bound; "
             "re-estimate from a real session's manifest total_bytes." % (r["cost_scenario"]["hours_per_month"], r["cost_scenario"]["retention_months"]),
             "", "| Option | USD/month |", "|---|---|",
             "| Local only | 0.00 AWS (laptop disk only) |",
             "| Cloud only (S3+DynamoDB) | %s |" % r["cost"]["total_usd_per_month"],
             "| Hybrid (local + cloud) | %s (same AWS cost; local copy is free) |" % r["cost"]["total_usd_per_month"], ""]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=0.1, help="data volume to generate (default 0.1 h = 6 min)")
    p.add_argument("--mock", action="store_true", help="use mocked AWS (moto): checks the code path only")
    p.add_argument("--outage-chunks", type=int, default=2, help="chunks of simulated network outage in hybrid test")
    p.add_argument("--chunk-packets", type=int)
    p.add_argument("--hours-per-month", type=float, default=100)
    p.add_argument("--retention-months", type=float, default=12)
    p.add_argument("--pricing", default=str(C.ROOT / "cloud" / "pricing.json"))
    p.add_argument("--out", help="directory for benchmark.json / benchmark.md")
    p.add_argument("--bucket"); p.add_argument("--table"); p.add_argument("--region")
    p.add_argument("--kms-key-id"); p.add_argument("--prefix", default="hpi-benchmark")
    a = p.parse_args(argv)
    seconds = int(a.hours * 3600)
    if seconds < 1:
        p.error("--hours too small")

    ctx = None
    if a.mock:
        os.environ.update(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION="us-east-1")
        from moto import mock_aws
        import boto3
        ctx = mock_aws(); ctx.start()
        boto3.client("s3").create_bucket(Bucket="bench-bucket")
        boto3.client("dynamodb").create_table(
            TableName="bench-sessions", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}])
        a.bucket, a.table = "bench-bucket", "bench-sessions"
    try:
        tmp = tempfile.mkdtemp(prefix="hpi-bench-")
        cfg = C.CloudConfig.load(bucket=a.bucket, table=a.table, region=a.region, kms_key_id=a.kms_key_id,
                                 prefix=a.prefix, chunk_packets=a.chunk_packets,
                                 key_file=str(Path(tmp) / "bench.key"), sync_db=str(Path(tmp) / "sync.db"))
        # Local cipher: DPAPI on Windows (what the app uses), AES-GCM elsewhere.
        if os.name == "nt":
            from research_storage import WindowsCipher
            cipher = WindowsCipher()
        else:
            cipher = C.AesGcmCipher(os.urandom(32))
        make_cloud = lambda s3=None: C.CloudStore(cfg, s3=s3) if s3 else C.CloudStore(cfg)
        base = make_cloud()
        print("Local recording of %d s ..." % seconds, file=sys.stderr)
        db = Path(tmp) / "local.db"
        sid, local = write_local(db, cipher, seconds)
        print("Cloud-only upload ...", file=sys.stderr)
        upload = bench_cloud_upload(base, db, cipher, Path(tmp) / "cloud_only_sync.db", sid)
        verify = bench_verify(base, sid)
        print("Hybrid with %d-chunk outage ..." % a.outage_chunks, file=sys.stderr)
        hybrid = bench_hybrid(cfg, make_cloud, base.s3, cipher, tmp, seconds, a.outage_chunks)
        pricing = json.loads(Path(a.pricing).read_text())
        bytes_per_hour = upload["s3_object_bytes"] / (seconds / 3600)
        result = dict(target="mock (moto, in-process)" if a.mock else "AWS %s, bucket %s" % (cfg.region or "default", cfg.bucket),
                      seconds=seconds, chunk_packets=cfg.chunk_packets, local=local,
                      cloud_direct=dict(upload=upload, verify=verify), hybrid=hybrid,
                      pricing_verified_on=pricing.get("verified_on"),
                      cost_scenario=dict(hours_per_month=a.hours_per_month, retention_months=a.retention_months),
                      cost=cost_model(pricing, bytes_per_hour, cfg.chunk_packets, a.hours_per_month,
                                      a.retention_months, bool(cfg.kms_key_id)))
    finally:
        if ctx:
            ctx.stop()
    md = render_markdown(result)
    print(md)
    if a.out:
        out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
        (out / "benchmark.json").write_text(json.dumps(result, indent=1))
        (out / "benchmark.md").write_text(md)
        print("\nSaved to %s" % out, file=sys.stderr)
    if a.outage_chunks and not hybrid["outage_exercised"]:
        print("WARNING: no outage was simulated (run too short for the chunk size); use more --hours "
              "or a smaller --chunk-packets.", file=sys.stderr)
    return 0 if hybrid["packets_lost"] == 0 and verify["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

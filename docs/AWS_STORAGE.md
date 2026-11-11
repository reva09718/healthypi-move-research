# AWS storage, encryption and the local / cloud / hybrid comparison (S5, S6, S7)

Status: **implemented and tested against mocked AWS only** (`moto`). It has not yet been run
against a real AWS account, with the Windows DPAPI path end-to-end, or with a live watch
recording. The measured comparison (S7) must still be produced on real AWS: see
[Producing the S7 results](#producing-the-s7-results).

## What was built

| Part | File |
|---|---|
| Encrypted S3 chunks + DynamoDB index, resumable sync, hybrid background uploader | `pc_tools/cloud_storage.py` |
| Command line: `keygen`, `sync`, `upload`, `status`, `list`, `verify`, `download` | `pc_tools/cloud_sync.py` |
| Recorder GUI: "Upload to AWS while recording (hybrid)", Upload / Verify buttons | `pc_tools/research_app.py` |
| Local vs cloud vs hybrid benchmark and cost model | `pc_tools/storage_benchmark.py`, `cloud/pricing.json` |
| AWS infrastructure (private bucket, table, KMS option, least-privilege policy) | `cloud/template.yaml` (passes `cfn-lint`) |
| 20 offline tests | `pc_tools/test_cloud.py` |

## Architecture

```
watch --BLE--> laptop recorder --> local encrypted SQLite (DPAPI)   <- always written first, master copy
                                        |
                          background uploader (read-only view of the local DB)
                          decrypt -> NDJSON -> gzip -> AES-256-GCM -> HTTPS
                                        v
                    S3  <prefix>/sessions/<id>/chunk-000000.bin ...  (+ SSE-S3 or SSE-KMS at rest)
                                        meta.bin        (encrypted note + connection events)
                                        manifest.json   (chunk list, SHA-256s, auth tag; written last)
                    DynamoDB  session_id -> status, times, signals, packet/byte counts, manifest hash
```

- **Local mode**: unchanged (`research_sessions.db`).
- **Cloud mode**: `cloud_sync.py sync` after recording (upload-after-Stop).
- **Hybrid mode**: tick *Upload to AWS while recording*. Sealed chunks (default 2,000 packets,
  roughly 1-3 minutes of data) upload in the background every 15 s while recording continues;
  Stop finalises. Recording never waits for the network. If the network is down, uploads queue
  and resume later; `cloud_sync.py sync` finishes anything left. Progress is kept in
  `data/cloud_sync.db`, so a crash or restart resumes at the next chunk.

### Why S3 + DynamoDB and not RDS

Sessions are append-only streams read back whole, not queried row by row. S3 stores the chunks
cheaply and durably and needs no server. DynamoDB holds a tiny per-session index (on-demand
billing, so it costs nothing when idle). RDS would add an always-on instance (billed while idle),
patching and connection management for a query capability the project does not need. RDS is
therefore evaluated and not implemented; the reasoning, not a benchmark, is the basis. If
Ma'am wants RDS measured as well, the chunk format can be loaded into PostgreSQL later.

## Security (S6)

| Layer | What protects it |
|---|---|
| BLE link | Watch research service is encrypted (existing firmware work) |
| Laptop disk | Windows DPAPI, current user (existing) |
| Before leaving the laptop | Re-encrypted with **AES-256-GCM**. DPAPI data cannot be decrypted on another machine, so a portable key is needed. Per-session key = HKDF-SHA256(master key file, session id), or one **AWS KMS** data key per session (envelope encryption) |
| In transit | HTTPS/TLS (boto3). The bucket policy in `cloud/template.yaml` denies non-TLS and TLS < 1.2 |
| At rest | S3 SSE-S3 or SSE-KMS on top of the client-side encryption; public access blocked; versioning on |
| Integrity | Each chunk is authenticated and bound to (session id, chunk number); the manifest lists every chunk's SHA-256 and carries an AES-GCM tag over itself. Verified: modified, swapped, deleted chunks and a manifest with chunks removed are all detected (`test_cloud.py`) |
| Access | IAM policy limited to one bucket prefix and one table; no credentials are stored in the repository or in `cloud_config.json` (the loader rejects unknown keys such as a secret) |

Limits you should state honestly:

- **Lose the key file, lose the cloud data.** Back up `data/cloud.key` separately (or use KMS,
  where AWS holds the key). Anyone with the key file *and* bucket access can read the data.
- Manifest and DynamoDB metadata (times, signal list, device address, packet counts) are
  **not** encrypted. The free-text note and connection events are. Do not put identifying
  information in session metadata.
- Uploading needs the same Windows account that recorded the session (DPAPI).
- This protects stored/transmitted data. It does not make BLE capture reliable: the
  watch's roughly 45-second disconnect is still open (see `S2_S3_S4_IMPLEMENTATION.md`).

## Setup

1. Deploy the infrastructure (needs AWS CLI and an account):
   ```powershell
   aws cloudformation deploy --template-file cloud/template.yaml --stack-name hpi-research `
     --capabilities CAPABILITY_IAM `
     --parameter-overrides BucketName=<unique-bucket-name> CreateKmsKey=false
   ```
   Attach the `UploaderPolicyArn` output to the IAM user/SSO role the laptop uses, then
   `aws configure` (or `aws sso login`). Do not commit credentials.
2. Install and configure on the laptop:
   ```powershell
   python -m pip install -r pc_tools/requirements-cloud.txt
   python pc_tools/cloud_sync.py keygen          # skip if you use KMS; back the file up!
   $env:HPI_S3_BUCKET = "<unique-bucket-name>"
   $env:HPI_DDB_TABLE = "hpi-research-sessions"
   $env:HPI_AWS_REGION = "<region>"
   # optional, with CreateKmsKey=true:  $env:HPI_KMS_KEY_ID = "<KmsKeyId output>"
   ```
3. Use it: tick the hybrid box in `python pc_tools/research_app.py`, or after recording run
   `python pc_tools/cloud_sync.py sync`, then `verify <session-id>`.

## Producing the S7 results

```powershell
python pc_tools/storage_benchmark.py --mock                       # dry run, mocked AWS, proves the code path only
python pc_tools/storage_benchmark.py --hours 1 --out data/benchmarks   # real AWS, real network
```

It measures local commit latency, chunk upload latency, time-to-cloud after Stop, hybrid
behaviour under a simulated network outage (no loss expected: the local copy is written first
and the backlog drains afterwards), integrity-check time, and size. Run it several times at
different times of day, on the network you will record on, and report medians and p95.
Mocked-AWS figures must never be quoted as results.

**Cost** uses `cloud/pricing.json` (us-east-1). Its unit prices were checked against the official
AWS Price List API on 2026-11-11 (`verified_on`). `cloud/pricing.ap-south-1.json` holds the
Mumbai prices checked the same day; select it with `--pricing cloud/pricing.ap-south-1.json`.
Re-check prices before quoting them.

The synthetic data compresses about 12.5x, which real sensor data will not, so the benchmark's
cost is a **lower bound**. For the default scenario (100 h/month, kept 12 months, SSE-S3, no KMS),
the bounds are:

| Region | Synthetic compression (lower bound) | No compression (upper bound) |
|---|---|---|
| us-east-1 | 0.15 USD/month | 1.57 USD/month |
| ap-south-1 | 0.16 USD/month | 1.70 USD/month |

The upper bound assumes about 56 MB of NDJSON per recorded hour. A customer-managed KMS key adds
about 1 USD/month. Replace both bounds with a figure from the `total_bytes` of a real uploaded
session's manifest.

## Comparison framework for the report

| Criterion | Local only | Cloud (S3 + DynamoDB) | Hybrid (recommended candidate) |
|---|---|---|---|
| Write latency | Disk commit; measured, sub-millisecond to milliseconds | Network round trip per chunk; measure on real AWS | Same as local (network is off the recording path) |
| Reliability | One laptop: disk loss or theft loses everything | Durable once uploaded, but a network outage during recording needs buffering | Local copy survives outages; cloud copy survives laptop loss; resumable |
| Scalability | One machine's disk; manual sharing | S3 has no practical capacity limit; documented request rates are per prefix and far above one watch's needs | As cloud, with a local cache |
| Security | DPAPI, tied to one Windows account | Client-side AES-GCM + TLS + SSE, IAM-controlled, shareable across researchers | Both layers; largest attack surface (two copies, one key file) |
| Cost | No AWS cost | Pay for storage and requests (see benchmark cost table) | Same as cloud |
| Complexity | Lowest | AWS account, IAM, key management | Highest |

Expected recommendation: **hybrid** (local-first with background upload) for long recordings,
because a 3-5+ hour recording must not depend on a continuous internet link, while the cloud
copy gives durability and cross-device access. This is a reasoned expectation: confirm it with
the benchmark numbers before presenting it as a finding.

## Not done / next

- Run against a real AWS account and fill in the measured comparison table.
- Test the DPAPI path end-to-end on Windows (tests here use an AES stand-in for the local cipher)
  and run the GUI (checked only with stubbed Tk, plus syntax compilation).
- Phone app: the Flutter app still only displays live data; it has no recording and therefore no
  cloud upload. Reusing this chunk format from Dart is possible but not implemented.
- Optional hardening: S3 Object Lock for write-once recordings, a lifecycle policy review with
  your data-retention rules.

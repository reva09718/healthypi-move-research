# Handoff: project state for whoever continues (person or AI assistant)

Read this first, then `docs/WALKTHROUGH.md` (step-by-step procedure) and `docs/AWS_STORAGE.md`
(design, security, comparison table).

## Do not reimplement the AWS part

AWS storage is already implemented and tested against mocked AWS. The remaining work is to **deploy,
run and measure** it, not to rewrite it.

| File | What it does |
|---|---|
| `cloud/template.yaml` | CloudFormation: private versioned S3 bucket (TLS 1.2+ only, SSE), DynamoDB session table, optional KMS key, least-privilege `UploaderPolicy` limited to `hpi-research/*` and the table |
| `pc_tools/cloud_storage.py` | Client-side AES-256-GCM, chunked upload to S3, DynamoDB index, resumable sync, hybrid background uploader, verify, download |
| `pc_tools/cloud_sync.py` | CLI: `keygen`, `sync`, `upload`, `status`, `list`, `verify`, `download` |
| `pc_tools/research_app.py` | Recorder GUI with "Upload to AWS while recording (hybrid)" and Upload / Verify buttons |
| `pc_tools/storage_benchmark.py` | Local vs cloud vs hybrid benchmark and cost model (prefix `hpi-research/benchmark`) |
| `cloud/pricing.json`, `cloud/pricing.ap-south-1.json` | Unit prices verified on 2026-11-11 against the AWS Price List API |
| `pc_tools/session_report.py` | Duration, per-signal coverage, gaps and reconnections from an exported session CSV |
| Tests | `pc_tools/test_cloud.py` (22, mocked AWS), `pc_tools/test_session_report.py` (1) |

Configuration comes from `data/cloud_config.json` (`bucket`, `table`, `region`) or `HPI_*` environment
variables. AWS credentials come from the normal boto3 chain (`aws configure --profile hpi-laptop` and
`AWS_PROFILE`). They are never stored in the repository.

## Status (2026-11-12)

- AWS account created on the **Free plan**: no charges, 100 USD credits, trial until May 2027. Never
  press Upgrade. Use region **ap-south-1 (Mumbai)**.
- Next step: deploy `cloud/template.yaml` from **AWS CloudShell** (WALKTHROUGH Part B, step 5 command;
  CloudShell already has the console user's permissions, so no admin access key is needed), then create
  the `hpi-laptop` user with `UploaderPolicyArn` (Part B, step 6).
- Then: key backup (Part C), short watch recordings incl. a 10-min hybrid run and `verify` (Part D),
  3-5 h recording x2 (Part E), real-AWS benchmark x3 (Part F), comparison and recommendation (Part G).
- Not done and out of the AWS scope: laptop recorder on the new research service (wrist red/IR,
  finger red), recording in the Flutter app, IMU streaming, root cause of the ~45 s disconnect.

## Rules for credentials

- Never paste the AWS root password, root keys or admin keys into a chat or commit them.
- Deploy from CloudShell (browser). The laptop recorder uses only the limited `hpi-laptop` key, entered
  with `aws configure` on the laptop.
- `data/cloud.key` encrypts every upload. Back it up off the laptop; never commit it.
- Delete access keys and the bucket/table when the project ends (WALKTHROUGH Part H).

## Known caveats

- All automated tests use mocked AWS; no real-AWS or real-watch result exists yet. Mocked benchmark
  numbers must not be quoted.
- The synthetic benchmark data compresses ~12.5x; real cost must come from a real session's `total_bytes`
  (`cloud_sync.py list`).
- The recorder's own tests (`test_research.py`) need Windows (DPAPI, Tk).
- "Hybrid" is the expected recommendation, but only state it if the real measurements support it.

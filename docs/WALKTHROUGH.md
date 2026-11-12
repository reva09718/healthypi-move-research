# Step-by-step walkthrough: AWS setup, watch recording and the long-duration test

This is the to-do list for finishing the project with the watch, a Windows laptop and an AWS
account. Do the parts in order. Each part ends with a check, so you know it worked before moving on.
All commands are PowerShell, run from the repository folder unless stated otherwise.

## Where the project stands (against the problem statement)

| # | Problem-statement item | Status | Remaining |
|---|---|---|---|
| 1 | Study firmware, BLE protocol, Flutter app | Done: `docs/BLE_SPEC.md`, `fw/docs/RESEARCH_PPG.md`, `patches/` | - |
| 2 | Start/Stop recording of all available sensors | **Partly.** Firmware 3.1.2-continuous + app 3.0.11: phone Start all/Stop all and per-sensor continuous control. Laptop recorder: Start/Stop of host capture with storage. | The phone does not store data; the laptop cannot start continuous mode or record wrist red/IR and finger red (see `docs/HANDOFF.md`, critical gap). No IMU stream. |
| 3 | BLE communication, live visualisation, timestamped packets | Done: firmware research service (custom 3.1.1-ppg), app Live tab with all 5 PPG channels, laptop live plots, version-1 packet envelope with host timestamps | Physical phone + watch test still pending |
| 4 | Secure local storage (CSV/SQLite) | Done on the laptop: DPAPI-encrypted SQLite and CSV export | Not in the phone app |
| 5 | AWS cloud storage (S3, DynamoDB) | **Code done**: encrypted S3 chunks, DynamoDB index, hybrid upload while recording, resumable sync, verify, download | **Run on real AWS (Parts B-E below)** |
| 6 | Encryption for storage and transmission | Done: encrypted BLE service, DPAPI on disk, AES-256-GCM before upload, TLS 1.2+ enforced, SSE at rest | Back up `data/cloud.key` |
| 7 | Compare local / cloud / hybrid (latency, scalability, reliability, security, cost) | Benchmark tool and cost model ready; unit prices verified 2026-11-11 | **Real measurements (Part F) and the final recommendation (Part G)** |
| - | Continuous 3-5 h collection and storage | Not demonstrated | **Part E** |

Automated checks: 22 AWS-storage tests (mocked AWS), an end-to-end hybrid run against a local S3/DynamoDB
server with the recorder writing ~31 packets/s, and `cfn-lint` on the template. None of this is a real-AWS
or real-watch result.

---

## Part A - Laptop preparation (once, ~20 min)

1. Install **Python 3.11+** for Windows from python.org (tick *Add python.exe to PATH*; keep Tcl/Tk ticked).
2. Get the code:
   ```powershell
   git clone https://github.com/<your-account>/healthypi-move-research.git
   cd healthypi-move-research
   ```
3. Install the laptop packages:
   ```powershell
   python -m pip install -r pc_tools/requirements-research.txt -r pc_tools/requirements-cloud.txt
   ```
4. **Check:** `python -m unittest discover -s pc_tools -p "test_cloud.py"` and
   `python -m unittest discover -s pc_tools -p "test_session_report.py"` must pass. Also run
   `python -m unittest discover -s pc_tools -p "test_research.py"`: those recorder tests use real DPAPI
   and Tk, so they can only run on Windows and have not been run since this AWS work was added; note any
   failure. Then run `python pc_tools/research_app.py`, tick
   **DEMO - synthetic data**, press Start, wait 20 s, press Stop. A session labelled synthetic-demo
   appears under *Saved sessions*.

## Part B - AWS account and infrastructure (once, ~40 min)

Pick one region and use it everywhere. `ap-south-1` (Mumbai) is closest if you are in India;
the examples use it. Prices for both regions are in `cloud/pricing*.json`.

1. **Account safety.** Sign in to the AWS console as the root user, enable MFA on root, then go to
   *Billing and Cost Management -> Budgets -> Create budget -> Zero spend* or a **5 USD monthly**
   budget with an email alert. Do not use the root user after this step.
2. **Admin user for deploying** (console): *IAM -> Users -> Create user* `hpi-admin` -> *Attach policies
   directly* -> `AdministratorAccess` -> Create. Open the user -> *Security credentials -> Create access key
   -> Command Line Interface*. Keep the key ID and secret safe (password manager). Never commit them.
3. Install the **AWS CLI v2** for Windows (https://awscli.amazonaws.com/AWSCLIV2.msi), open a new
   PowerShell, and store the admin key:
   ```powershell
   aws configure --profile hpi-admin      # key ID, secret, region ap-south-1, output json
   aws sts get-caller-identity --profile hpi-admin
   ```
4. **Deploy the template.** The bucket name must be globally unique (lowercase letters, digits, hyphens):
   ```powershell
   aws cloudformation deploy --profile hpi-admin --region ap-south-1 `
     --template-file cloud/template.yaml --stack-name hpi-research --capabilities CAPABILITY_IAM `
     --parameter-overrides BucketName=hpi-research-<yourteam>-2026 CreateKmsKey=false
   aws cloudformation describe-stacks --profile hpi-admin --region ap-south-1 --stack-name hpi-research `
     --query "Stacks[0].Outputs" --output table
   ```
   Note `BucketName`, `TableName` and `UploaderPolicyArn` from the table.
   (`CreateKmsKey=true` uses a customer-managed KMS key instead of the key file; it costs ~1 USD/month.)
5. **Least-privilege laptop user** (this is what the recorder uses, not the admin user):
   ```powershell
   aws iam create-user --user-name hpi-laptop --profile hpi-admin
   aws iam attach-user-policy --user-name hpi-laptop --policy-arn <UploaderPolicyArn> --profile hpi-admin
   aws iam create-access-key --user-name hpi-laptop --profile hpi-admin
   aws configure --profile hpi-laptop     # paste the new key ID and secret, region ap-south-1
   setx AWS_PROFILE hpi-laptop            # new PowerShell windows now use the laptop user
   ```
6. **Tell the tools where the storage is.** Create `data\cloud_config.json` (the `data` folder is ignored by Git):
   ```json
   {"bucket": "hpi-research-<yourteam>-2026", "table": "hpi-research-sessions", "region": "ap-south-1"}
   ```
7. **Check** (open a new PowerShell so `AWS_PROFILE` is set): `python pc_tools/cloud_sync.py list` prints
   nothing and no error. `AccessDenied` means the policy is not attached or the profile is wrong.

## Part C - Encryption key and backup (once, 5 min) - do not skip

```powershell
python pc_tools/cloud_sync.py keygen
Get-FileHash data\cloud.key
```

Copy `data\cloud.key` to **two places that are not this laptop**, for example an encrypted USB stick
and a password manager entry (it is 64 hex characters). Run `Get-FileHash` on a backup copy and
compare it with the original. **If this file is lost, every uploaded session is unreadable.**
Never commit it, email it in plain text, or put it in the same S3 bucket.

## Part D - Short recordings with the watch (~45 min)

Before each recording: charge the watch, close the phone app **and turn off phone Bluetooth**
(the watch accepts one connection), keep the watch within 1-2 m of the laptop.

1. **5-minute local test (hybrid off).** `python pc_tools/research_app.py`, untick DEMO, untick the AWS box,
   select the signals you want, duration `300`, note e.g. `D1 local 5min wrist`, press **Start recording
   on laptop**. Accept Windows pairing/passkey prompts. When it says connected, **start the measurement on
   the watch** (the buttons only control laptop capture). Watch the live plot.
2. Saved sessions -> select it -> **Export selected session as plaintext CSV** to `data\exports\D1.csv`
   (an `D1_events.csv` is written next to it). Then:
   ```powershell
   python pc_tools/session_report.py data\exports\D1.csv --out data\exports\D1_report.md
   ```
   **Check:** every selected signal has samples; note the gaps and interruptions. This is your baseline.
3. **10-minute hybrid test.** Same as step 1 with **Upload to AWS while recording (hybrid)** ticked and
   duration `600`. The message box shows `Cloud: 1 chunk(s) uploaded` every minute or two, and
   `Cloud upload complete` shortly after Stop.
4. **Check the cloud copy:**
   ```powershell
   python pc_tools/cloud_sync.py status
   python pc_tools/cloud_sync.py verify <session-id>
   python pc_tools/cloud_sync.py download <session-id> data\exports\D3_cloud.csv
   ```
   `verify` must print `"ok": true`, and the packet count must match the local export.
   This is the first real-AWS evidence; save the outputs.
5. **Outage test (optional, recommended).** Start another 10-minute hybrid recording, turn Wi-Fi off for
   3 minutes in the middle, turn it back on. Expect `Cloud upload waiting (offline?)` messages, then the
   backlog uploads. `verify` should still give `"ok": true` with nothing missing.

## Part E - The 3-5 hour continuous recording

> **Blocked until the laptop recorder can start continuous acquisition** (see the critical gap in
> `docs/HANDOFF.md`). With the current recorder the watch only runs finite spot measurements, so a
> multi-hour run will mostly record connection time, not signals. Do Parts A-D and F now; run Part E
> after that recorder update.

**Prepare the laptop:**

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Keep the laptop plugged in. In *Device Manager -> Bluetooth -> your adapter -> Power Management*, untick
*Allow the computer to turn off this device to save power*. Pause Windows Update for the day. Use a
network you trust; hybrid mode keeps recording locally even if it drops.

**Run it:**

1. Fully charge the watch. Close the phone app and turn off phone Bluetooth.
2. Start `research_app.py`, tick hybrid, duration `0` (until Stop) or `18000` (5 h), note e.g.
   `E1 5h wrist PPG+HR+temp`. Start the measurement on the watch.
3. Keep a paper/Excel log: start time, which watch measurement you started, any time you restarted a
   measurement on the watch, anything unusual (watch screen messages, low battery, you moved away).
4. Do not close the window or put the laptop to sleep. Disconnects of about 45 s are a known open issue.
   The recorder reconnects by itself and logs every interruption.
5. At the end press **Stop** and wait for `Cloud upload complete`.

**Collect the results:**

```powershell
python pc_tools/session_report.py data\exports\E1.csv --out data\exports\E1_report.md
python pc_tools/cloud_sync.py verify <session-id>
python pc_tools/cloud_sync.py list
```

(Export E1.csv from Saved sessions first, as in Part D.) `list` shows `total_bytes` and `total_packets`
for the session in AWS. From these, report:

- Total recording duration and data span (report header)
- Per signal: samples, coverage %, number and total length of gaps (report table)
- Number of interruptions/reconnections (report header and events table)
- Whether the cloud copy verified (`"ok": true`) and how long after Stop it completed
- **Real storage size:** `total_bytes / hours recorded` = bytes per hour. Monthly cost for 100 h/month kept
  12 months is about `bytes_per_hour x 100 x 12 / 1e9 x 0.025` USD (Mumbai S3 price) plus ~0.03 USD of
  requests. This replaces the synthetic-data lower bound in `docs/AWS_STORAGE.md`.

Repeat at least once (E2) so you can tell a one-off from a pattern. Important: if a signal stops while the
link stays connected, the watch measurement probably ended (the firmware has finite measurement modes).
That is a finding to report, not a storage failure.

## Part F - Storage benchmark on real AWS (~30 min per run)

Run on the same laptop and network you record on (it uses the laptop user and `data/cloud_config.json`):

```powershell
python pc_tools/storage_benchmark.py --hours 1 --pricing cloud/pricing.ap-south-1.json --out data\benchmarks\run1
```

Repeat at different times of day (run2, run3, ... at least 3). Each run writes `benchmark.md` and
`benchmark.json`. Benchmark objects go under `hpi-research/benchmark/` in S3 and also appear in the
DynamoDB index; that is expected.
Collect into one table: local commit p50/p95, hybrid commit p50/p95, chunk upload p50/p95, time to cloud
after Stop (cloud-only vs hybrid), packets lost during the simulated outage, verify time. Quote **medians
across runs**, never the `--mock` numbers.

## Part G - Decide and write up the comparison

Fill the comparison table in `docs/AWS_STORAGE.md` with your numbers. Hybrid is the expected
recommendation, but only state it if your data supports it. Check in particular:

- Hybrid local commit latency about equal to local-only (the network is off the recording path)
- 0 packets lost in the outage test (Part D5 and the benchmark)
- Cloud copy verified for the long recordings (Part E)
- Cost from real `total_bytes` is acceptable

If one of these fails, report it and recommend what the data supports.

## Part H - Clean up AWS when the project ends

The bucket, table and KMS key are kept even if the stack is deleted (`DeletionPolicy: Retain`), so
the data is not lost by accident. To stop all charges: download what you need, then in the console empty
the bucket (including old versions), delete the bucket and the DynamoDB table, delete the stack, and delete
the `hpi-laptop` and `hpi-admin` access keys.

## Still open beyond AWS (for the report's future work)

- Laptop recorder on the new research service (wrist red/IR and finger red recording, device indices
  instead of host-only timing, Start/Stop of watch measurements from the laptop).
- Recording and storage in the Flutter phone app (the problem statement asks for app Start/Stop).
- Raw accelerometer/gyroscope streaming.
- Root cause of the ~45 s disconnect.

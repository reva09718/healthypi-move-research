# Phone recording and AWS backup — app 3.0.13+101

## Install and record

Install `HealthyPi_Move_Recording_AWS_3.0.13.apk` over the existing research app. Keep firmware **3.1.4-imu** for all 17 exposed research signal IDs. This update does not change or require reflashing that firmware. Android is the supported recording platform for this build; the Keystore/foreground service implementation is Android-specific.

1. Connect the watch and open **Live**. Use **Sensors / Start all** or individual sensor Start controls to choose what to acquire. Check contact and actual incoming data.
2. Tap **Record**, then **Local**. Recording is optional and separate from sensor Start/Stop. It captures every arriving research-channel packet, including channels not displayed. It cannot capture a sensor that is not producing data.
3. **Stop recording** seals the session; sensors continue while Live is open. A background notification also provides Stop recording. Leaving Live without recording still stops controlled acquisition, as before. While recording, switching tabs keeps acquisition active.
4. Open **Saved / AWS**, or **Settings > Research recordings / AWS**, to see sessions, sample counts, per-channel gaps, interruption state and cloud queue progress.
5. Expand a stopped session and choose **Export CSV + summary / status JSON**. Exports are unencrypted; share only where intended. The temporary export files are deleted after the share sheet returns, best-effort. A crash during export can leave temporary files in the app cache.

Foreground recording uses an Android connected-device foreground service and wake lock. Screen lock/tab navigation are supported in the implementation, but Android vendor power policies and 3–5 hour operation still require device testing. Force stop, process death, swiping the app away or a BLE disconnect can end recording. The app marks interrupted sessions and does not pretend to recover absent BLE data or automatically restart sensors. Keep the watch nearby. No iOS background recording support is claimed.

## Local, hybrid, and account boundaries

| Mode | Phone | AWS | Offline behavior |
|---|---|---|---|
| Local | Encrypted raw recording | No upload for this session | Works without AWS setup/sign-in |
| Hybrid | Same encrypted local recording, retained | Upload queued after recording stops | Local data retained; durable queue retries when app runs with connectivity/sign-in |

Before AWS deployment, use **Local device profile**. This is the phone's app sandbox, not a cloud identity or a password-protected local user account. It remains available after signing out. Once signed in, each Cognito subject gets a separate research-recording owner namespace. Each teammate uses their own account; they do not see other members' sessions. Stop recording and wait for upload activity to finish before changing accounts/configuration. Device-profile sessions are not silently moved to a signed-in account.

New research recordings are separate from the app's older Healthy Store history and its export/delete/sync controls. The older history remains device-based. The new screen lists local sessions for the active profile. Cloud backup is not yet an in-app cross-device restore browser; authorized cloud session/manifest/download API routes are provided for subsequent retrieval tools.

Hybrid uploads run after Stop and retry approximately every 30 seconds while the app process is running, with persisted exponential retry delays up to 30 minutes. **Retry queued AWS uploads** resets the delay. Opening Saved recordings restores the queue after an app restart. Uploads are not guaranteed while the app is force-stopped. A failed upload never deletes the local recording. Sessions are labelled Uploaded only after every chunk's checksum/size is confirmed by AWS and the manifest is finalized.

## Deploy in Singapore yourself

No AWS credentials are embedded in the APK. Do not send access keys/passwords to anyone. Use the identity already available in AWS CloudShell.

1. Select **Asia Pacific (Singapore), ap-southeast-1** in AWS. Open CloudShell.
2. Upload `HealthyPi_Move_Mobile_AWS_Setup.zip` with CloudShell's Actions > Upload file.
3. Run:

```bash
unzip HealthyPi_Move_Mobile_AWS_Setup.zip -d healthypi-mobile-setup
cd healthypi-mobile-setup
bash deploy.sh
```

4. When prompted, enter comma-separated **your and teammates' email addresses**. Empty allowlists deny account creation. The script creates the `healthypi-mobile` CloudFormation stack in Singapore and prints **AppConfig JSON**.
5. In the app, open **Saved / AWS > Account & AWS setup**, paste AppConfig, and Save. Each member chooses **Create account**, confirms the code sent to their email, then signs in. Passwords require 12+ characters including upper/lowercase, number and symbol. Forgot password/code reset is supported. Cognito email delivery and sandbox/service quotas must be checked in your account if confirmation emails fail.
6. Start a **new Hybrid** recording. Stop it; confirm Uploaded in Saved recordings. Turn off internet for another session; confirm Queued and readable local export, then restore internet and Retry. Verify using a second account that the first account's local/session API records are inaccessible.

To change the permitted team emails, run the deploy script again with the full desired list. Removing an email from signup does not revoke an existing user; disable that user in Cognito if required. Sign-out clears the phone's saved refresh token; it is a local sign-out, not global session revocation. Cloud administrators can disable accounts/revoke sessions in Cognito.

AWS resources incur usage charges; this is not a free-service guarantee. S3 versioning/noncurrent version expiry, DynamoDB backups, Lambda/API requests and Cognito can affect costs. Set an AWS budget and inspect Singapore pricing for your actual data volume. Buckets/tables have **Retain** policies, so deleting the stack does not delete recordings or end all storage charges. Deliberate deletion of retained research data is an owner action.

## Security and exact data contract

- Raw BLE chunks and summary/contact metadata are encrypted using per-owner Android Keystore AES-256-GCM, fresh 96-bit nonce, authenticated owner/session/sequence context. Tokens/settings use a separate native Keystore key. Keys/passwords never enter SQLite or the APK. A SHA256 of each sealed chunk detects stored corruption before decrypting. SQLite contains minimal plaintext session IDs, owner hashes, times and queue state. Android backup is disabled; uninstalling/clearing app data loses local recordings and their keys. Export/back up required data first.
- Cloud upload decrypts one chunk in memory, gzip-compresses it, and sends it over HTTPS into a private, TLS-only S3 bucket with SSE-S3 encryption. DynamoDB is encrypted at rest. **This is not end-to-end encryption against the AWS administrator**. No decrypted upload file is written to phone storage. There are no IAM access keys/client secrets in the app; Cognito is a public client.
- API Gateway verifies JWT issuer/audience/scope; the API additionally checks access-token type/client/issuer and derives owner exclusively from the verified `sub` claim. S3 URLs are short-lived, scoped to a server-chosen user/session/chunk path and signed for checksum, byte length and encryption headers. Chunk sequence numbers bind an immutable payload hash. Completion freezes new allocation and verifies contiguous committed chunks; it does not trust a client's Uploaded claim.
- New mobile stack is independent of the supplied laptop IAM uploader stack. Do not grant the laptop uploader blanket access to the mobile bucket/table if you want to preserve API user isolation. AWS account administrators inherently retain access.
- Each saved wire packet retains receive UTC microseconds, phone monotonic elapsed microseconds, boot ID, original sample index and estimated packet uptime. CSV repeats packet receive time for its samples and increments sample indices; it does not fabricate sample UTC timestamps. Counts/gaps start at each channel's first observed baseline. Duplicate/out-of-order wire packets are preserved and flagged. Contact/state transitions and firmware transmit-failure counts are retained in status events.
- Phone UTC can jump; monotonic receive time does not. Watch uptime is an estimate/FIFO read time and IMU values are nominal 25Hz snapshots, not an exact hardware-synchronized 25Hz FIFO stream. Different sensors' true acquisition clocks are not made synchronous by storage.
- Flush about once per second or at 64KB, serialized atomic chunk + metadata commits. An OS kill may lose the buffered tail since the last commit. Bounded pending memory stops with an explicit error if local storage cannot keep up. A BLE gap cannot be backfilled from the phone. Native service failure/storage errors must be checked during device validation.

The supplied four laptop AWS patch commits are retained without rewriting their history. `pc_tools/test_cloud.py` has a follow-up Windows cleanup fix: close registered SQLite readers before removing temporary test directories. Phone Cognito APIs/mobile data formats do not replace the laptop uploader's format.

## Validation still needed on your devices/account

Automated tests are offline/mocked and do not establish hardware runtime or real AWS configuration. After deployment: check email sign-up/sign-in/refresh, real presigned PUT + HeadObject checksum confirmation, unauthorized cross-user API requests, app restart retry, airplane mode retry, screen-lock collection and available storage. Collect and store **3–5 hours** of all usable signals with contact maintained, then compare saved counts, gaps, boot changes, exported values/timing and actual AWS objects. Capture battery, temperature, packet loss, local write/stop latency, cloud upload latency and actual Singapore usage costs for the project report. No 3–5h run or live AWS deployment is claimed by this code delivery.

Primary implementation references: [Android foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types), [API Gateway JWT authorizers](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html), [Cognito public authentication](https://docs.aws.amazon.com/cognito/latest/developerguide/authentication-flows-public-server-side.html), [S3 checksum uploads](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html).

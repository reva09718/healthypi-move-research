# HealthyPi Move research platform

Custom HealthyPi Move firmware, a Flutter Android app with additional live PPG
channels, and Windows laptop tools for physiological data recording and analysis.
This repository contains complete source snapshots, not Git submodule links.

The firmware and phone app extend ProtoCentral's projects. Their upstream
commits, licenses and the exact changed files are recorded below and in
[`docs/SOURCE_PROVENANCE.json`](docs/SOURCE_PROVENANCE.json). The custom research
features have been built and checked with automated tests; they have not yet
been validated as a complete system on a physical watch and phone.

## Phone recording and AWS backup release

App **3.0.13+101** adds optional encrypted local recording, CSV/status export,
separate member sign-in, and local-first hybrid AWS backup. Keep firmware
**3.1.4-imu**; no firmware change is needed for storage. Deploy the new mobile
stack yourself in Singapore using CloudShell, then paste its AppConfig into
the app. See [phone recording and deployment instructions](docs/PHONE_RECORDING_AWS.md).
The new mobile stack is separate from the supplied laptop AWS uploader stack.
Real AWS deployment and a 3–5 hour watch/phone recording run remain pending.

## IMU live release

Use app **3.0.12+100** with firmware **3.1.4-imu** for accelerometer and
gyroscope X/Y/Z streaming. Live has three-axis graphs, numerical g / degree-per-
second readings and separate Start/Stop for both motion groups. Start all can
run all six sensor groups. The finger-contact and Android startup fixes are
included. See [IMU use, protocol, sampling and limits](docs/IMU_LIVE.md).

The register snapshot stream is nominal 25 Hz, subsampled from 50 Hz sensor
operation; it is not a lossless FIFO recorder. Raw signed counts are retained,
with display-only unit conversion. Physical watch readings and long-duration
simultaneous acquisition remain unverified. All 42 targeted app tests, 11 Python
protocol tests and four host C checks passed, along with both target builds and
package verification. Older reports below are retained as versioned history.

## Finger contact fix

Firmware **3.1.3-fingerfix** replaces 3.1.2-continuous and works with the same
**3.0.11 APK**. It corrects the finger contact timestamp truncation and ignores
empty sensor frames before interpreting contact. The full one-second loss delay
is regression-tested, including recovery and long uptime. Status mapping is
unchanged; actual contact/algorithm status still needs watch verification.
See [details](fw/docs/FINGER_CONTACT_FIX.md) and the new build report.

## Android startup fix

Use app **3.0.11+99** in place of 3.0.10. The preceding APK had no native
Android plugin registration calls, preventing startup. The replacement has
all nine registrations, a build guard against missing core plugins, and a
visible startup progress/error/retry screen. Packaged DEX checks confirm the
registration fix. A physical phone cold start remains to be verified.
Firmware 3.1.2-continuous is unchanged. See [details](app/docs/STARTUP_FIX.md).

## Continuous acquisition update

The matching research versions are **app 3.0.11+99** and **firmware
3.1.2-continuous**. Live now offers Start all / Stop all and individual ECG,
GSR, wrist PPG and finger PPG controls. Continuous research mode bypasses the
30-second spot-check cutoff; chart changes do not restart acquisition. The app
checks firmware capability and shows command rejection, contact and stopping
states. All seven waveforms share one encrypted research data subscription.

**Validation:** 36 targeted Flutter tests and changed-file analysis passed;
10 Python protocol tests and host C wire/control-policy checks passed; Android
release and Nordic target builds passed. APK signature/alignment and OTA image
signature/manifest checks passed. Physical simultaneous acquisition and 3–5 hour
reliability remain unverified. Static app RAM is 99.08% used. Live view does not
add mobile storage/export or a background acquisition service.

See [installation and limitations](docs/CONTINUOUS_INSTALL.md),
[app controls](app/docs/CONTINUOUS_LIVE.md),
[firmware protocol and acquisition](fw/docs/CONTINUOUS_RESEARCH.md), and
[build reports](docs/builds/). Older versioned build reports are retained for
the preceding PPG-only release.

## Repository contents

| Directory | Contents |
|---|---|
| [`fw/`](fw/) | Firmware source, research control/status/data service, protocol codec and tests |
| [`app/`](app/) | Flutter companion app and additional live PPG displays |
| [`pc_tools/`](pc_tools/) | BLE scan/capture, encrypted research recorder, SQLite/CSV and signal analysis, AWS upload and storage benchmark |
| [`cloud/`](cloud/) | CloudFormation template (S3, DynamoDB, optional KMS, IAM policy) and benchmark price inputs |
| [`docs/`](docs/) | BLE specification, laptop implementation notes, source inventory and build reports |
| [`patches/`](patches/) | App and firmware changes relative to the recorded upstream commits |
| [`setup_nordic.ps1`](setup_nordic.ps1) | Windows Nordic SDK installation/build helper; external tooling and local signing keys are required |

## Implemented work

**Firmware, custom version `3.1.2-continuous`:**

- An encrypted research BLE service with measurement control, state updates,
  boot ID, estimated device uptime and sample indices.
- Three additional optical channels: wrist red, wrist infrared and finger red.
  The existing wrist green and finger infrared streams are retained.
- Channels from each optical sensor batch share its producer index/timestamp.
- Independent continuous Start/Stop for ECG, GSR/BioZ, wrist PPGs and finger
  PPGs; continuous targets can coexist on one owning BLE connection. The ECG
  acquisition path forwards BioZ from the shared FIFO read. Wrist selects the
  hub's continuous HR+SpO2 mode. These changes still need hardware validation.

**Flutter Android app, version `3.0.11+99`:**

- Live selection of ECG, GSR, wrist green/red/IR and finger IR/red PPG.
- Two-graph Compare mode on phones and tablets, adaptive PPG scaling, sample
  totals, latest values, inferred gaps, subscription errors and retry.
- One shared encrypted waveform subscription with per-channel decoding/continuity.
- Continuous Start all/Stop all and independent sensor group controls, with
  matching firmware capability checks and acknowledged sensor state.
- Live subscriptions stop when leaving the Live tab and pause during SMP work.
- Legacy endpoint fallback for the original PPG channels when the custom service
  is absent. Extra channels display a firmware requirement message.
- Separate Android identity: `com.protocentral.move.research`, launcher name
  **HealthyPi Move PPG**, so it can coexist with the official application.

**Laptop tools:**

- Signal selection, capture duration, Start/Stop, live plots and reconnect events.
- Host arrival timestamps, estimated per-sample times and session management.
- Windows-user DPAPI encryption of packet contents in SQLite and plaintext CSV
  export, plus signal-rate/gap/PPG validation tools.

**AWS storage (laptop):**

- Local-first hybrid mode, cloud-only upload and resumable sync: sessions are re-encrypted with
  AES-256-GCM (local key file or AWS KMS), uploaded to S3 in authenticated chunks with a
  DynamoDB session index, and can be verified and exported back to CSV.
- Benchmark and cost model for comparing local, cloud and hybrid storage.
- Details, security limits and setup: [`docs/AWS_STORAGE.md`](docs/AWS_STORAGE.md). Tested against
  mocked AWS only.

The laptop recorder still uses the original live BLE endpoints. Its GUI has not
been integrated with the new research protocol or the extra PPG channels.

## Current validation and remaining work

| Area | Recorded status |
|---|---|
| Firmware reference protocol | 10 Python tests and host C encoder/control-policy checks passed |
| Nordic target build | NCS 3.4.0 build completed; both MCUboot image signatures checked |
| Firmware memory | Signed app 786,447 bytes; static RAM 446,416 / 450,560 bytes (99.08%) |
| Flutter | Changed files passed analysis; 36 targeted tests passed |
| Android APK | Release build, signature, package identity, CRC and alignment checks passed |
| Physical phone/watch integration | Pending |
| Finger PPG recording validation | Pending |
| Continuous 3–5+ hour collection and storage | Not demonstrated |
| Raw accelerometer/gyroscope live streaming | Not added by these changes |
| AWS storage, hybrid mode, encryption for upload | Implemented; 22 offline tests with mocked AWS; real-AWS run pending ([`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md)) |
| Local vs cloud vs hybrid comparison (measured) | Benchmark tool ready; real-AWS measurements and final recommendation pending |

Build evidence is in [`docs/builds/`](docs/builds/). Automated tests and a
successful build are not evidence of uninterrupted device recording. The custom
firmware retains finite spot modes alongside continuous research mode; red/IR availability depends on
sensor mode/contact. Device uptime is estimated; indices do not prove exact
cross-sensor synchronization or lossless capture.

The phone changes in this repository add live display. They do not add continuous
mobile recording/storage of all live channels. The next milestones are finger
PPG hardware validation, complete recording integration, remaining sensor support,
and progressively longer capture tests with saved-data checks.

## Run and build

### Flutter app

Use a compatible Flutter SDK (the recorded APK used Flutter 3.47.9), Android SDK
36, JDK 17, and NDK 28.2.13676358. Local SDK paths and signing files are excluded.

```sh
cd app
flutter pub get
flutter test test/research_packet_test.dart test/live_stream_controller_test.dart test/live_screen_test.dart test/smp_lock_test.dart test/signal_view_test.dart
flutter build apk --release
```

The locally built APK is `app/build/app/outputs/flutter-apk/app-release.apk`.
Without private release signing configuration the project uses Android debug
signing for personal installation. It is a research build, not a Play Store
release. Pair the watch in this app, open Live, and start measurements on the
watch. Details: [`app/docs/RESEARCH_PPG_LIVE.md`](app/docs/RESEARCH_PPG_LIVE.md).

### Firmware

Install the nRF Connect SDK **v3.4.0** and use its matching toolchain environment.
Keep the repository's board/partition configuration and provide the required
signing keys locally; see [`docs/SIGNING.md`](docs/SIGNING.md).

For an SDK installed in a separate West workspace, from this repository root:

```sh
python fw/tools/build_research.py --sdk-root <SDK_WORKSPACE> --pristine
```

The helper sets `BOARD_ROOT` and `ZEPHYR_EXTRA_MODULES` to this checkout and builds
`healthypi_move/nrf5340/cpuapp`. Its log is written under `output/firmware/`.
The resulting OTA package is `fw/build/dfu_application.zip`. A source ZIP or a
patch is not an installable watch update. See the upstream [`fw/README.md`](fw/README.md)
and the additional channel contract in [`fw/docs/RESEARCH_PPG.md`](fw/docs/RESEARCH_PPG.md).

Reference protocol checks, from the repository root:

```sh
python -m unittest discover -s fw/tests/research_protocol -p test_protocol.py -v
gcc -std=c11 -Wall -Wextra -Werror -Ifw/app/src fw/tests/research_protocol/wire_test.c -o wire_test
```

Run the compiled `wire_test` on the host to execute the encoder checks.

### Windows laptop recorder

```powershell
python -m pip install -r pc_tools/requirements-research.txt
python pc_tools/research_app.py
```

Optional AWS storage: `python -m pip install -r pc_tools/requirements-cloud.txt`, then follow
[`docs/AWS_STORAGE.md`](docs/AWS_STORAGE.md). Cloud tests (mocked AWS):
`python -m unittest discover -s pc_tools -p test_cloud.py -v`.

Windows DPAPI protects recorded packet contents using the current Windows user.
Session metadata and exported CSVs are not all encrypted. Recordings are created
locally under the ignored `data/` directory. Test details and limitations:
[`docs/S2_S3_S4_IMPLEMENTATION.md`](docs/S2_S3_S4_IMPLEMENTATION.md).

## Upstream attribution and change review

| Component | Upstream | Base commit |
|---|---|---|
| Firmware | [ProtoCentral HealthyPi Move firmware](https://github.com/Protocentral/healthypi-move-fw) | `874f1347a6ec649cd9eef8bafb9d396d5806ca1e` |
| Flutter | [ProtoCentral HealthyPi Move Flutter app](https://github.com/Protocentral/healthypi_move_flutter) | `c58ff7a1f9a2fdd1d69be83f07bbf2fc079f90d5` |

The snapshots already contain the changes in `patches/`; do not apply those
patches again to this repository. They are provided for reviewing the research
contribution against the upstream base. Source hashes and exclusions are in the
provenance inventory. UTF-8 text line endings are normalized to LF for portable
checkouts; the inventory records both published and original workspace hashes.
Build-report source hashes refer to the original workspace files. No upstream
Git history is included.

Original copyright notices and licenses are retained. Firmware includes MIT,
Apache, BSD, Nordic-specific and font/documentation terms; read
[`fw/LICENSE.md`](fw/LICENSE.md), [`fw/THIRD_PARTY.md`](fw/THIRD_PARTY.md) and
[`app/LICENSE`](app/LICENSE). This project is not an official ProtoCentral release.

Signing keys, credentials, SDK installations, local recordings and generated
APK/OTA binaries are intentionally excluded from source control. Artifact names
and checksums from the local builds are retained in the build reports.

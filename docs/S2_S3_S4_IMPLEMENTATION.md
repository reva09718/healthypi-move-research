# S2 S3 S4 laptop implementation

Run from the project folder:

```powershell
python pc_tools/research_app.py
```

The app needs Python with Bleak and Matplotlib. Tkinter is included in the normal
Windows Python installation. These imports were available in the project environment.
If using a different Python installation:

```powershell
python -m pip install -r pc_tools/requirements-research.txt
```

## S2 Start and Stop recording

Select waveform and/or processed-reading channels, choose a duration (0 means
until Stop), optionally enter a note, and press **Start recording on laptop**.
The app scans, pairs with the watch and subscribes. Accept Windows pairing prompts
and use the watch passkey if requested. Disconnect the phone app first.

Start ECG, EDA or wrist/finger SpO2 measurements on the watch after the app connects.
Buttons in this application control **host capture**, not the watch sensor state.
Stop finalizes and commits the session. Closing the window requests the same orderly
stop. Connection setup may take time to return; do not forcibly kill Python.

Supported live endpoints: ECG, BioZ/GSR, wrist PPG, finger PPG, HR, temperature,
spot SpO2 and battery. Subscriptions do not guarantee an active sensor or valid output.
Wrist/finger modes and other sensor combinations have device restrictions. Select only
the signals needed for a particular experiment.

**Scope limitation:** the guide describes old v2.1.1 remote recording commands.
The current watch runs v3.1.0. This implementation does not send those old commands.
Full S2 remote acquisition control for every sensor remains incomplete: stock
v3.1.0 lacks that legacy interface, the examined live path has no raw IMU channel,
and spot measurements can finish before host recording stops. No firmware changes
or Flutter changes are included in this laptop implementation.

## S3 Live visualization and standardized host packets

Latest readings appear as numbers; choose a signal in the plot selector to see its
latest 12 seconds. All selected signals continue recording when another graph or
the saved-session tab is displayed. Graph buffers and queues are bounded; dropping
a UI update does not remove an already stored packet. Counts and notices identify
missing channels. A flat/empty trace is not replaced with artificial readings.

The version-1 packet envelope stores:

- Session ID and receiver-generated packet sequence.
- Signal key, original bytes (hex) and decoded values.
- UTC arrival time and monotonic elapsed arrival time.
- Receiver-generated sample index and nominal sample rate.
- Estimated sample positions within each batch, anchored to host packet arrival.
- Explicit null device timestamp and device sequence, since the watch does not supply
  those in its raw waveform payload.

Arrival-derived sample times are estimates affected by radio jitter, batching and
lost data. They do not establish exact cross-sensor synchronization or prove lossless
capture. Sensor sample values remain raw counts unless their decoder supplies units.

## S4 Local storage

New sessions use `data/research_sessions.db`. Existing `data/recordings.db`, CSV
files and the old command-line recorder remain untouched.

Each packet's values, raw bytes and protected metadata are encrypted using Windows
DPAPI, current-user scope. No plaintext recording fallback is provided. No separate
key is written beside the database. Decryption requires access to the corresponding
Windows account/profile; this is not a portable password-protected database.
Session IDs, selected signals, notes, times, status and routing metadata remain
plaintext. Do not put identifying medical information in the note field.

SQLite uses WAL and FULL synchronization, with commits about once per second and
on normal stop/disconnect. A crash may lose the uncommitted tail; it is not a zero-loss
guarantee. Abruptly abandoned sessions remain labelled `recording`, not `completed`.
Ending states distinguish `completed` (duration), `stopped` (button), and `error`
(partial). `completed_with_gaps` / `stopped_with_gaps` indicate interruptions.
Data saved before a disconnect is retained.

Open **Saved sessions**, select a session, and choose **Export selected session as
plaintext CSV**. Exports contain host timing labels, packet/sample indices and one
row per received sample. The encrypted source is retained. CSV exports are intentionally
readable and are not encrypted. Nothing is uploaded to AWS unless the optional AWS storage in
`docs/AWS_STORAGE.md` is used.

## Demonstration and checks

Tick **DEMO - synthetic data** to check the interface without a watch. Demo sessions
are labelled `synthetic-demo` in storage and DEMO on the graph. Their numbers are
generated examples, never experimental evidence. Untick it for actual capture.

```powershell
python -m unittest discover -s pc_tools -p test_research.py -v
```

Tests cover actual Windows DPAPI encryption/decryption and tamper rejection,
encrypted storage/reopening, correctly quoted multi-sample CSV export, protected
packet/metadata consistency, manual Stop, simulated BLE disconnection, malformed
payloads, temperature indications, and the Tk interface demo flow.

The watch's recurring approximately 45-second disconnect is **not fixed** here.
The recorder now saves and retries automatically after disconnections or Bluetooth
connection failures. It retries after a two-second delay plus connection cleanup,
reconnects only to the originally selected watch, and resubscribes to the selected
channels. All connection segments share one session and monotonically increasing
host counters. Stop cancels scanning/retry operations. Duration is total wall-clock
time since the first connection, including interruptions; zero means until Stop.

Graphs break across reconnects. An additional `<export-name>_events.csv` records
connection and interruption times alongside the sample CSV. Packet rows include a
`connection_segment` column. No data is fabricated for offline periods, and no
backfill of missed samples is implemented. Reconnecting does not restart a finished
watch measurement. New live-watch testing is required
before claiming reliable continuous or hours-long recording. Do not present passing
simulated tests as a successful device test.

## Implementation files

- `pc_tools/research_app.py`: Tk buttons, live plots/readings, session list/export.
- `pc_tools/research_capture.py`: BLE acquisition thread, subscriptions, lifecycle.
- `pc_tools/research_storage.py`: packet envelope, DPAPI and SQLite/CSV.
- `pc_tools/test_research.py`: offline regression checks.

This completes a laptop implementation of S3 host-side visualization/timing and S4
encrypted packet storage/export for available channels. S2 host Start/Stop is
implemented; remote sensor controls and live IMU require a separate firmware phase.

# Accelerometer and gyroscope live research release

Matching versions: Android **3.0.12+100**, firmware **3.1.4-imu**. This release
is based on the completed 3.1.3 finger-contact fix and retains it, along with the
Android startup/plugin-registration fix and prior continuous sensor controls.

## Phone use

Install the APK over the previous research app, update the watch using the OTA
ZIP, reconnect, and open Live. Accept Bluetooth pairing if requested. The channel
selector now includes **Accelerometer** and **Gyroscope**. Each shows three
coloured traces on a shared symmetric vertical scale: X red, Y green, Z blue.
The latest X/Y/Z numbers and unit appear above the trace. Compare can show both
motion plots, or one motion plot beside PPG, ECG or GSR.

Open Sensors for independent Accelerometer/Gyroscope Start/Stop, or Start all
for all six groups. Existing research firmware can still display physiological
signals; it advertises no IMU capability, so IMU Start is disabled and Start all
only starts supported groups. Keep the app foreground on Live. Leaving Live or
disconnecting requests Stop; reconnecting does not automatically restart a run.
No new background Android service or persistent mobile recording is included.

## Raw data and sampling

| Signal IDs | Meaning | Wire values | Phone display |
|---|---|---|---|
| 12, 13, 14 | accelerometer X/Y/Z | signed int32, sign-extended 16-bit register counts | raw / 4096 g |
| 15, 16, 17 | gyroscope X/Y/Z | signed int32, sign-extended 16-bit register counts | raw × 2000 / 32768 °/s |

These are raw axis register measurements. Acceleration includes gravity. The app
does not estimate orientation or subtract gravity, calibrate bias or apply a
motion filter. The display conversion preserves raw packet values in the
receiver; graph values are scaled only for rendering.

The existing accelerometer configuration is normal mode, **50 Hz, ±8g**. The
driver checks it rather than silently reporting values at a different scale or
changing step-counter settings. Gyro Start saves GYR_CONF, sets normal mode,
**50 Hz, ±2000 °/s**, waits 100 ms, and checks the register readback. Stop restores
the saved gyro configuration. Accelerometer Stop stops BLE research streaming;
the accelerometer remains available for the watch's normal step counting.

Firmware polls the latest six registers in a single I2C burst at **nominal 25 Hz**,
then batches five samples per axis. This is a subsampled register snapshot stream,
not a lossless 50 Hz FIFO recording. Scheduling and BLE load can reduce actual
yield; the firmware does not invent catch-up samples. A sensor failure stops the
affected stream; a failed gyro restoration keeps Stop latched for retry.

The existing HPR1 16-byte data header and encrypted service UUIDs are unchanged.
New samples set the signed flag. Axes in each sensor group share the batch's
index/timestamp. Track continuity separately for each axis because BLE sends can
fail independently. Time remains estimated watch uptime, not exact hardware
timestamps or proof of synchronization with ECG/PPG/GSR.

## Control extension

Continuous Start remains `[1, 4, target, request_id]`, Stop `[1, 2, target, id]`.
New targets: **5 accelerometer**, **6 gyro**. Status active-mask bits 4/5 correspond
to these targets; status capability flag bit 2 (`0x04`) advertises IMU support.
Finite Start (opcode 1) is unsupported for the IMU. Existing targets/IDs retain
their meanings. Legacy clients are not sent unsolicited target-5/6 statuses
unless that connection requested those groups. Periodic statuses remain 5 Hz;
faster IMU polling does not increase status-notification traffic.

The implementation reuses the research control thread, avoiding another RTOS
thread stack, and serializes BMI323 access with its existing driver mutex.
The legacy ACCEL_X channel used by step counting is retained; the raw axis
reader is separate. This avoids sending the step count as acceleration X.

## Validation and remaining hardware checks

Host checks cover dummy-byte removal, little-endian signed decoding, six new
wire IDs, display units, six-target admission, capability checks, independent
packet routing, Start/Stop and phone/tablet three-axis layouts. Android and Nordic
builds, package signatures and manifests are checked separately in the build
reports. No physical watch/phone IMU validation or 3–5 hour run is claimed.

On the watch, first test stationary acceleration (gravity mainly on one axis),
rotation around each axis, gyro readings at rest, gyro Stop/restart, disconnect
cancellation, step counting and simultaneous PPG/ECG/GSR. Confirm actual packet
yield, gaps, battery consumption and temperature before a long recording run.
Installed bootloader/signing-key compatibility is not established by a host
image-signature check. The firmware's static RAM budget remains tight.

Register meanings/configuration were checked against Bosch's primary sources:
[BMI323 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi323-ds000.pdf)
and [Bosch BMI3 definitions](https://github.com/boschsensortec/BMI3XY_SensorAPI/blob/main/bmi3_defs.h).

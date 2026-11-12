import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import '../ble/research_packet.dart';
import 'native_vault.dart';
import 'recording_store.dart';

class ResearchRecorder extends ChangeNotifier {
  ResearchRecorder(this.store, this.service);
  final RecordingStore store;
  final RecordingService service;
  String? id, owner, error;
  bool starting = false, stopping = false;
  bool get active => id != null;
  bool get locked => active || starting || stopping;
  int savedPackets = 0, savedSamples = 0;
  final _clock = Stopwatch();
  Timer? _timer;
  List<Map<String, dynamic>> _packets = [];
  int _bufferBytes = 0, _pendingBytes = 0, _seq = 0, _started = 0;
  Future<void> _writes = Future.value();
  Future<void>? _stopFuture;
  Map<String, int> _counts = {}, _missing = {};
  ResearchContinuity _continuity = ResearchContinuity();
  int _duplicates = 0, _malformed = 0, _reboots = 0;
  String? _status;
  Map<String, dynamic> _metadata() => {
    'format': 'hpi-phone-recording-v1',
    'started_utc_ms': _started,
    'duration_us': _clock.elapsedMicroseconds,
    'packets': savedPackets,
    'samples': savedSamples,
    'counts': Map<String, int>.of(_counts),
    'missing_after_baseline': Map<String, int>.of(_missing),
    'duplicates_or_out_of_order': _duplicates,
    'malformed_packets': _malformed,
    'channel_boot_changes': _reboots,
    'timestamp_note':
        'UTC and monotonic receive times; watch uptime is estimated, not exact inter-sensor synchronization.',
  };
  Future<void> start(String account, {required String mode}) async {
    if (locked) throw StateError('A recording is already active');
    starting = true;
    error = null;
    notifyListeners();
    try {
      _started = DateTime.now().millisecondsSinceEpoch;
      _clock.reset();
      _clock.start();
      _counts = {};
      _missing = {};
      _duplicates = 0;
      _malformed = 0;
      _reboots = 0;
      _seq = 0;
      savedPackets = 0;
      savedSamples = 0;
      _packets = [];
      _bufferBytes = 0;
      _pendingBytes = 0;
      _status = null;
      _continuity = ResearchContinuity();
      _writes = Future.value();
      _stopFuture = null;
      await service.start();
      owner = account;
      id = await store.create(account, _metadata(), mode: mode);
      _timer = Timer.periodic(const Duration(seconds: 1), (_) => _flush());
    } catch (e) {
      await service.stop();
      error = '$e';
      rethrow;
    } finally {
      starting = false;
      notifyListeners();
    }
  }

  void accept(Uint8List bytes) {
    if (!active || stopping || error != null) return;
    try {
      final p = ResearchPacket.decode(bytes);
      if (p == null) return;
      final check = _continuity.observe(p);
      final key = '${p.signalId}';
      if (check == null) {
        _duplicates++;
      } else {
        _counts[key] = (_counts[key] ?? 0) + p.values.length;
        _missing[key] = (_missing[key] ?? 0) + check.missing;
        if (check.reset) _reboots++;
      }
      // Preserve the wire packet even if duplicated, alongside continuity flags.
      savedPackets++;
      savedSamples += p.values.length;
      _add({
        'type': 'packet',
        'received_utc_us': DateTime.now().microsecondsSinceEpoch,
        'elapsed_us': _clock.elapsedMicroseconds,
        'raw_b64': base64Encode(bytes),
        'duplicate_or_out_of_order': check == null,
        'missing_before': check?.missing ?? 0,
        'boot_changed': check?.reset ?? false,
      }, bytes.length * 2 + 180);
    } on FormatException {
      _malformed++;
    }
  }

  void status(Map<String, dynamic> value) {
    if (!active || stopping || error != null) return;
    final encoded = jsonEncode(value);
    if (encoded == _status) return;
    _status = encoded;
    _add({
      'type': 'status',
      'received_utc_us': DateTime.now().microsecondsSinceEpoch,
      'elapsed_us': _clock.elapsedMicroseconds,
      'value': value,
    }, encoded.length + 128);
  }

  void _add(Map<String, dynamic> value, int size) {
    _packets.add(value);
    _bufferBytes += size;
    if (_pendingBytes + _bufferBytes > 2 * 1024 * 1024) {
      error =
          'Storage cannot keep up. Recording stopped; check free phone storage.';
      unawaited(stop(state: 'error'));
      return;
    }
    if (_bufferBytes >= 64000) _flush();
  }

  void _flush() {
    if (!active || _packets.isEmpty || error != null) return;
    final packets = _packets, size = _bufferBytes, seq = _seq++;
    final account = owner!, session = id!, meta = _metadata();
    _packets = [];
    _bufferBytes = 0;
    _pendingBytes += size;
    _writes = _writes
        .then((_) async {
          if (error != null) return;
          await store.append(account, session, seq, {
            'format': 'hpi-phone-chunk-v1',
            'seq': seq,
            'events': packets,
          }, meta);
        })
        .catchError((Object e) {
          error = 'Recording storage failed: $e';
          unawaited(stop(state: 'error'));
        })
        .whenComplete(() {
          _pendingBytes -= size;
          notifyListeners();
        });
  }

  Future<void> stop({String state = 'completed'}) =>
      _stopFuture ??= _stop(state);
  Future<void> _stop(String state) async {
    if (!active) return;
    stopping = true;
    _timer?.cancel();
    notifyListeners();
    try {
      _flush();
      await _writes;
      // On a failed write use the last atomic metadata, not uncommitted counters.
      final persisted = await store.get(owner!, id!);
      await store.finish(
        owner!,
        id!,
        error == null ? state : 'error',
        error == null ? _metadata() : {...persisted!.metadata, 'error': error},
      );
    } catch (e) {
      error = 'Could not finalize recording: $e';
      try {
        await store.finishWithLastCommittedMetadata(owner!, id!);
      } catch (_) {
        // A full/unavailable database may prevent even the state update; the
        // next app initialization still recovers its recording marker.
      }
    } finally {
      _clock.stop();
      id = null;
      stopping = false;
      try {
        await service.stop();
      } catch (e) {
        error ??= 'Could not stop background service: $e';
      }
      notifyListeners();
    }
  }
}

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../ble/research_packet.dart';
import 'live_stream_controller.dart';

const researchControl = 'a17e0001-4d52-4f56-9100-524553454152';
const researchStatus = 'a17e0002-4d52-4f56-9100-524553454152';
const _statusRoute = (service: researchService, characteristic: researchStatus);

abstract interface class ResearchControlTransport {
  Future<void> writeResearch(Uint8List bytes);
  Future<Uint8List> readResearchStatus();
}

enum ResearchTarget { ecg, gsr, wrist, finger, accel, gyro }

extension ResearchTargetInfo on ResearchTarget {
  int get id => index + 1;
  bool get isImu => index >= ResearchTarget.accel.index;
  String get label => switch (this) {
    ResearchTarget.ecg => 'ECG',
    ResearchTarget.gsr => 'GSR',
    ResearchTarget.wrist => 'Wrist PPGs',
    ResearchTarget.finger => 'Finger PPGs',
    ResearchTarget.accel => 'Accelerometer',
    ResearchTarget.gyro => 'Gyroscope',
  };
}

class ResearchStatus {
  ResearchStatus(Uint8List bytes) {
    if (bytes.length != 20 ||
        bytes[0] != 1 ||
        bytes[3] > 6 ||
        bytes[4] > 5 ||
        bytes[5] > 7 ||
        bytes[6] > 63) {
      throw const FormatException('Invalid sensor status');
    }
    request = bytes[1];
    opcode = bytes[2];
    target = bytes[3];
    result = bytes[4];
    state = bytes[5];
    mask = bytes[6];
    continuousSupported = (bytes[7] & 2) != 0;
    imuSupported = (bytes[7] & 4) != 0;
    bootId = ByteData.sublistView(bytes).getUint32(8, Endian.little);
    txFailures = ByteData.sublistView(bytes).getUint32(16, Endian.little);
  }
  late final int request,
      opcode,
      target,
      result,
      state,
      mask,
      bootId,
      txFailures;
  late final bool continuousSupported, imuSupported;
}

/// Commands are acknowledged by the watch, separately from sensor/contact state.
/// Chart changes never stop a target. Leaving Live explicitly requests Stop.
class ResearchSessionController extends ChangeNotifier {
  ResearchSessionController(this.transport);
  final LiveTransport transport;
  final states = <ResearchTarget, int>{};
  final _possiblyOwned = <ResearchTarget>{};
  int activeMask = 0;
  bool ready = false, supported = false, busy = false, imuSupported = false;
  Iterable<ResearchTarget> get availableTargets =>
      ResearchTarget.values.where((t) => !t.isImu || imuSupported);
  String? error;
  int txFailures = 0;
  int? _bootId;
  int _nextId = 0;
  bool _closed = false;
  bool _shuttingDown = false;
  StreamSubscription<Uint8List>? _subscription;
  Future<void>? _opening;
  final _pending =
      <int, ({int opcode, int target, Completer<ResearchStatus> reply})>{};

  bool active(ResearchTarget target) => (activeMask & (1 << target.index)) != 0;
  bool canStop(ResearchTarget target) =>
      active(target) || _possiblyOwned.contains(target);
  String stateLabel(ResearchTarget target) => switch (states[target] ?? 0) {
    1 => 'Starting',
    2 => target.isImu ? 'Waiting for data' : 'Waiting for contact',
    3 => 'Streaming',
    4 => 'Completed',
    5 => 'Stopped',
    6 => 'Sensor error',
    7 => 'Stopping',
    _ => active(target) ? 'Active' : 'Stopped',
  };

  void _notify() {
    if (!_closed) notifyListeners();
  }

  void _accept(Uint8List bytes) {
    try {
      final status = ResearchStatus(bytes);
      if (_bootId != null && _bootId != status.bootId) {
        states.clear();
        _possiblyOwned.clear();
        error = 'Watch restarted. Check contact and press Start again.';
        for (final request in _pending.values) {
          if (!request.reply.isCompleted) {
            request.reply.completeError(StateError('Watch restarted'));
          }
        }
        _pending.clear();
      }
      _bootId = status.bootId;
      supported = status.continuousSupported;
      imuSupported = status.imuSupported;
      activeMask = status.mask;
      txFailures = status.txFailures;
      if (status.target != 0) {
        final target = ResearchTarget.values[status.target - 1];
        states[target] = status.state;
        if (status.state >= 4 && status.state <= 6 && !active(target)) {
          _possiblyOwned.remove(target);
        }
      }
      final pending = _pending[status.request];
      if (pending != null &&
          pending.opcode == status.opcode &&
          pending.target == status.target) {
        _pending.remove(status.request);
        pending.reply.complete(status);
      }
      _notify();
    } on FormatException catch (e) {
      error = '$e';
      _notify();
    }
  }

  Future<void> open() => _opening ??= _open();
  Future<void> _open() async {
    if (_closed ||
        !transport.connected ||
        !transport.hasResearch ||
        transport is! ResearchControlTransport) {
      return;
    }
    try {
      await transport.pair().timeout(const Duration(seconds: 30));
      if (_closed) return;
      _subscription = transport
          .values(_statusRoute)
          .listen(
            _accept,
            onError: (Object e) {
              ready = false;
              error = 'Sensor controls disconnected: $e';
              _notify();
            },
          );
      await transport.enable(_statusRoute).timeout(const Duration(seconds: 10));
      _accept(
        await (transport as ResearchControlTransport)
            .readResearchStatus()
            .timeout(const Duration(seconds: 10)),
      );
      ready = true;
      if (!supported) {
        error = 'Install firmware 3.1.2-continuous for continuous Start/Stop.';
      }
    } catch (e) {
      error = 'Could not open sensor controls: $e';
    }
    _notify();
  }

  Future<ResearchStatus> _command(int opcode, ResearchTarget target) async {
    if (!transport.connected) throw StateError('Watch is disconnected');
    final id = _nextId = _nextId % 255 + 1;
    final reply = Completer<ResearchStatus>();
    _pending[id] = (opcode: opcode, target: target.id, reply: reply);
    // Attach timeout before the write so an early status/reboot cannot leave an
    // unhandled error; write success alone never counts as sensor success.
    final response = reply.future.timeout(const Duration(seconds: 8));
    // The write and reply can fail independently, including while a write is
    // still pending. Attach an error handler immediately to both paths.
    unawaited(response.then<void>((_) {}, onError: (Object _) {}));
    try {
      await (transport as ResearchControlTransport)
          .writeResearch(Uint8List.fromList([1, opcode, target.id, id]))
          .timeout(const Duration(seconds: 8));
    } catch (e) {
      _pending.remove(id);
      if (!reply.isCompleted) reply.completeError(e);
    }
    try {
      final status = await response;
      if (status.result != 0) {
        throw StateError(switch (status.result) {
          1 => 'Watch is busy with another measurement',
          3 => 'This measurement belongs to another connection',
          4 => 'Continuous mode is unsupported',
          5 => 'Firmware update is active',
          _ => 'Watch rejected the command',
        });
      }
      return status;
    } finally {
      _pending.remove(id);
    }
  }

  Future<void> setTargets(
    Iterable<ResearchTarget> targets, {
    required bool start,
  }) async {
    if (busy || _closed || (start && _shuttingDown)) return;
    busy = true;
    error = null;
    _notify();
    try {
      await open();
      if (!ready || !supported) {
        throw StateError('Firmware 3.1.2-continuous is required');
      }
      final failures = <String>[];
      for (final target in targets) {
        if (start && _shuttingDown) break;
        if (start && target.isImu && !imuSupported) {
          failures.add('${target.label}: install firmware 3.1.4-imu');
          continue;
        }
        if (!start && !active(target) && !_possiblyOwned.contains(target)) {
          continue;
        }
        if (start) _possiblyOwned.add(target);
        try {
          await _command(start ? 4 : 2, target);
        } catch (e) {
          failures.add('${target.label}: $e');
        }
      }
      if (failures.isNotEmpty) error = failures.join('\n');
    } catch (e) {
      error = '$e';
    } finally {
      busy = false;
      _notify();
    }
  }

  /// Best-effort teardown; disconnect is also a firmware-side Stop fail-safe.
  Future<void> shutdown() async {
    _shuttingDown = true;
    // A Start may still be awaiting its acknowledgement. Keep status subscribed
    // until it settles, then stop every target known to be active.
    while (busy && transport.connected && !_closed) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }
    if (!_closed && ready && transport.connected) {
      await setTargets(ResearchTarget.values, start: false);
      final deadline = DateTime.now().add(const Duration(seconds: 8));
      while (activeMask != 0 &&
          transport.connected &&
          DateTime.now().isBefore(deadline)) {
        await Future<void>.delayed(const Duration(milliseconds: 100));
      }
      if (activeMask != 0 && transport.connected) {
        error =
            'The watch has not confirmed Stop. Disconnect to request cancellation.';
      }
    }
    await _subscription?.cancel();
    _subscription = null;
    if (transport.connected) {
      try {
        await transport
            .disable(_statusRoute)
            .timeout(const Duration(seconds: 5));
      } catch (_) {
        /* Link loss removes subscriptions and cancels remote streams. */
      }
    }
    ready = false;
  }

  @override
  void dispose() {
    _closed = true;
    unawaited(_subscription?.cancel());
    for (final request in _pending.values) {
      if (!request.reply.isCompleted) {
        request.reply.completeError(StateError('Controls closed'));
      }
    }
    _pending.clear();
    super.dispose();
  }
}

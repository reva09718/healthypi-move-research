import 'dart:async';

import 'package:flutter/foundation.dart';

import '../ble/research_packet.dart';
import '../models/live_signal.dart';
import 'ble_manager.dart';
import 'connection_manager.dart';
import 'research_session_controller.dart';

typedef LiveRoute = ({String service, String characteristic});

abstract interface class LiveTransport {
  bool get connected;
  bool get hasResearch;
  Future<void> pair();
  Stream<Uint8List> values(LiveRoute route);
  Future<void> enable(LiveRoute route);
  Future<void> disable(LiveRoute route);
}

class ConnectedLiveTransport
    implements LiveTransport, ResearchControlTransport {
  ConnectedLiveTransport(this.deviceId);
  final String deviceId;
  final _cm = ConnectionManager.instance;
  final _ble = BleManager.instance;
  Future<void>? _pairing;
  @override
  Future<void> writeResearch(Uint8List bytes) => _ble.write(
    deviceId,
    researchService,
    researchControl,
    bytes,
    withoutResponse: false,
  );
  @override
  Future<Uint8List> readResearchStatus() =>
      _ble.read(deviceId, researchService, researchStatus);
  @override
  bool get connected => _cm.isConnected && _cm.deviceId == deviceId;
  @override
  bool get hasResearch => connected && _cm.hasService(researchService);
  @override
  Future<void> pair() => _pairing ??= _pair();
  Future<void> _pair() async {
    try {
      if (defaultTargetPlatform == TargetPlatform.android &&
          await _ble.isPaired(deviceId) != true) {
        await _ble.pair(deviceId);
      }
      await _cm.requestMtu(247);
    } catch (_) {
      _pairing = null;
      rethrow;
    }
  }

  @override
  Stream<Uint8List> values(LiveRoute route) =>
      _ble.notifications(deviceId, route.characteristic);
  @override
  Future<void> enable(LiveRoute route) =>
      _ble.subscribe(deviceId, route.service, route.characteristic);
  @override
  Future<void> disable(LiveRoute route) =>
      _ble.unsubscribe(deviceId, route.service, route.characteristic);
}

/// Serializes route changes so two PPG charts share one encrypted subscription.
/// A generation guard drops late notifications and late pairing/subscription work.
class LiveStreamController extends ChangeNotifier {
  LiveStreamController(this.transport, {required this.onSamples});
  final LiveTransport transport;
  final void Function(LiveSignal signal, List<double> values, bool breakBefore)
  onSamples;
  final errors = <LiveSignal, String>{};
  final active = <LiveSignal>{};
  final sampleCounts = <LiveSignal, int>{};
  final missingCounts = <LiveSignal, int>{};
  final _subscriptions = <LiveRoute, StreamSubscription<Uint8List>>{};
  Set<LiveSignal> _wanted = {};
  Future<void> _serial = Future.value();
  int _revision = 0;
  bool _closed = false;
  bool get isStreaming => active.isNotEmpty;

  Future<void> select(Set<LiveSignal> wanted, {bool force = false}) {
    if (_closed) return Future.value();
    if (!force &&
        transport.connected &&
        errors.isEmpty &&
        setEquals(_wanted, wanted) &&
        setEquals(active, wanted)) {
      return _serial;
    }
    _wanted = Set.of(wanted);
    final revision = ++_revision;
    active.clear();
    notifyListeners();
    _serial = _serial.then((_) => _apply(revision));
    return _serial;
  }

  bool _current(int revision) =>
      !_closed && revision == _revision && transport.connected;

  Future<void> _clearRoutes() async {
    final old = Map.of(_subscriptions);
    _subscriptions.clear();
    for (final entry in old.entries) {
      await entry.value.cancel();
      if (transport.connected) {
        try {
          await transport
              .disable(entry.key)
              .timeout(const Duration(seconds: 5));
        } catch (_) {
          /* A dropped link has already removed its CCC subscriptions. */
        }
      }
    }
  }

  Future<void> _apply(int revision) async {
    if (_closed || revision != _revision) return;
    await _clearRoutes();
    if (!_current(revision)) return;
    errors.clear();
    sampleCounts.clear();
    missingCounts.clear();
    final routes = <LiveRoute, Set<LiveSignal>>{};
    for (final signal in _wanted) {
      if (signal.researchRequired && !transport.hasResearch) {
        errors[signal] =
            signal.isImu
                ? 'This channel needs firmware 3.1.4-imu. Reconnect after updating the watch.'
                : 'This channel needs the custom 3.1.1-ppg firmware. Reconnect after updating the watch.';
        continue;
      }
      final route =
          transport.hasResearch
              ? (service: researchService, characteristic: researchData)
              : (
                service: signal.legacyService,
                characteristic: signal.legacyCharacteristic,
              );
      routes.putIfAbsent(route, () => {}).add(signal);
    }
    final continuity = ResearchContinuity();
    for (final entry in routes.entries) {
      if (!_current(revision)) return;
      final research = entry.key.service == researchService;
      try {
        if (research) {
          await transport.pair().timeout(const Duration(seconds: 30));
          if (!_current(revision)) return;
        }
        _subscriptions[entry.key] = transport
            .values(entry.key)
            .listen(
              (bytes) {
                if (!_current(revision)) return;
                try {
                  if (research) {
                    final packet = ResearchPacket.decode(bytes);
                    if (packet == null) return;
                    for (final signal in entry.value) {
                      if (signal.researchId != packet.signalId) continue;
                      final check = continuity.observe(packet);
                      if (check == null) continue;
                      missingCounts[signal] =
                          (missingCounts[signal] ?? 0) + check.missing;
                      _accept(
                        signal,
                        packet.values,
                        check.reset || check.missing > 0,
                      );
                    }
                  } else {
                    for (final signal in entry.value) {
                      _accept(signal, signal.decodeLegacy(bytes), false);
                    }
                  }
                } on FormatException {
                  _report(
                    entry.value,
                    'Received a malformed packet. Waiting for valid data.',
                  );
                }
              },
              onError: (Object error) {
                if (_current(revision)) {
                  active.removeAll(entry.value);
                  _report(entry.value, 'Live stream interrupted: $error');
                }
              },
            );
        await transport.enable(entry.key).timeout(const Duration(seconds: 15));
        if (!_current(revision)) return;
        active.addAll(entry.value);
      } catch (error) {
        if (!_current(revision)) return;
        await _subscriptions.remove(entry.key)?.cancel();
        try {
          await transport
              .disable(entry.key)
              .timeout(const Duration(seconds: 5));
        } catch (_) {}
        _report(
          entry.value,
          research
              ? 'Could not open the encrypted PPG stream. Accept pairing on the phone/watch, then tap Retry. ($error)'
              : 'Could not start this live stream: $error',
        );
      }
    }
    if (!_closed) notifyListeners();
  }

  void _accept(LiveSignal signal, List<double> values, bool breakBefore) {
    sampleCounts[signal] = (sampleCounts[signal] ?? 0) + values.length;
    if (errors.remove(signal) != null) notifyListeners();
    onSamples(signal, values, breakBefore);
  }

  void _report(Set<LiveSignal> signals, String message) {
    if (_closed) return;
    for (final signal in signals) {
      errors[signal] = message;
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _closed = true;
    ++_revision;
    active.clear();
    _serial = _serial.then((_) => _clearRoutes());
    super.dispose();
  }
}

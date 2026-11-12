import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:move/ble/research_packet.dart';
import 'package:move/models/live_signal.dart';

Uint8List packet({
  int signal = 9,
  int index = 10,
  int boot = 42,
  int flags = 1,
  List<int> samples = const [0, 4000000000, 4294967295],
}) {
  final bytes = ByteData(16 + samples.length * 4);
  bytes.setUint8(0, 1);
  bytes.setUint8(1, signal);
  bytes.setUint8(2, flags);
  bytes.setUint8(3, samples.length);
  bytes.setUint32(4, boot, Endian.little);
  bytes.setUint32(8, index, Endian.little);
  bytes.setUint32(12, 1234, Endian.little);
  for (int i = 0; i < samples.length; i++) {
    bytes.setUint32(16 + i * 4, samples[i], Endian.little);
  }
  return bytes.buffer.asUint8List();
}

void main() {
  test('all five PPG channels preserve unsigned counts and slice offsets', () {
    for (final signal in [3, 4, 9, 10, 11]) {
      final payload = packet(signal: signal);
      final padded = Uint8List(payload.length + 7)
        ..setRange(7, payload.length + 7, payload);
      final decoded = ResearchPacket.decode(Uint8List.sublistView(padded, 7))!;
      expect(decoded.signalId, signal);
      expect(decoded.values, [0, 4000000000, 4294967295]);
      expect(decoded.firstIndex, 10);
      expect(decoded.estimatedUptimeMs, 1234);
    }
  });
  test('rejects malformed lengths, signed PPG and multi-value scalars', () {
    expect(() => ResearchPacket.decode(Uint8List(19)), throwsFormatException);
    expect(
      () => ResearchPacket.decode(packet(flags: 3)),
      throwsFormatException,
    );
    expect(
      () => ResearchPacket.decode(packet(signal: 8)),
      throwsFormatException,
    );
    final truncated = packet().sublist(0, 24);
    expect(() => ResearchPacket.decode(truncated), throwsFormatException);
    expect(ResearchPacket.decode(packet(signal: 18)), isNull);
  });
  test('per-channel continuity handles gaps, duplicates, wrap and reboot', () {
    final tracker = ResearchContinuity();
    ResearchPacket p(int signal, int index, {int boot = 42}) =>
        ResearchPacket.decode(
          packet(signal: signal, index: index, boot: boot, samples: [1, 2]),
        )!;
    expect(tracker.observe(p(9, 0xfffffffe))!.missing, 0);
    expect(tracker.observe(p(9, 0))!.missing, 0);
    expect(tracker.observe(p(10, 100))!.missing, 0);
    expect(tracker.observe(p(9, 5))!.missing, 3);
    expect(tracker.observe(p(9, 5)), isNull);
    expect(tracker.observe(p(10, 102))!.missing, 0);
    expect(tracker.observe(p(9, 0, boot: 43))!.reset, isTrue);
  });
  test('legacy samples decode little endian and validate width', () {
    expect(
      LiveSignal.wristGreen.decodeLegacy(Uint8List.fromList([0, 40, 107, 238])),
      [4000000000],
    );
    expect(
      LiveSignal.ecg.decodeLegacy(Uint8List.fromList([244, 255, 255, 255])),
      [-12],
    );
    expect(
      () => LiveSignal.fingerIr.decodeLegacy(Uint8List(3)),
      throwsFormatException,
    );
  });
}

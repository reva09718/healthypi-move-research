import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:move/ble/research_packet.dart';
import 'package:move/utils/research_session_controller.dart';

import 'live_stream_controller_test.dart' show FakeLiveTransport;

class FakeControlTransport extends FakeLiveTransport
    implements ResearchControlTransport {
  bool supportsContinuous = true;
  bool supportsImu = false;
  bool acknowledge = true;
  int mask = 0, result = 0, boot = 42;
  final commands = <List<int>>[];
  Uint8List status({
    int id = 0,
    int opcode = 0,
    int target = 0,
    int state = 0,
  }) {
    final bytes = Uint8List(20);
    bytes.setAll(0, [
      1,
      id,
      opcode,
      target,
      result,
      state,
      mask,
      (supportsContinuous ? 3 : 1) | (supportsImu ? 4 : 0),
    ]);
    ByteData.sublistView(bytes).setUint32(8, boot, Endian.little);
    return bytes;
  }

  void publish(Uint8List bytes) =>
      streams[(service: researchService, characteristic: researchStatus)]!.add(
        bytes,
      );
  @override
  Future<Uint8List> readResearchStatus() async => status();
  @override
  Future<void> writeResearch(Uint8List bytes) async {
    commands.add(bytes.toList());
    if (!acknowledge) return;
    if (result == 0) {
      if (bytes[1] == 4) mask |= 1 << (bytes[2] - 1);
      if (bytes[1] == 2) mask &= ~(1 << (bytes[2] - 1));
    }
    publish(
      status(
        id: bytes[3],
        opcode: bytes[1],
        target: bytes[2],
        state: bytes[1] == 4 ? 2 : 5,
      ),
    );
  }
}

void main() {
  test(
    'start all enables four independent targets; stopping ECG retains others',
    () async {
      final transport = FakeControlTransport();
      final controls = ResearchSessionController(transport);
      await controls.setTargets(ResearchTarget.values, start: true);
      expect(transport.commands.map((c) => c.take(3).toList()), [
        [1, 4, 1],
        [1, 4, 2],
        [1, 4, 3],
        [1, 4, 4],
      ]);
      expect(controls.activeMask, 15);
      expect(controls.stateLabel(ResearchTarget.ecg), 'Waiting for contact');
      await controls.setTargets([ResearchTarget.ecg], start: false);
      expect(controls.activeMask, 14);
      expect(controls.stateLabel(ResearchTarget.ecg), 'Stopped');
      await controls.shutdown();
      expect(transport.mask, 0);
      controls.dispose();
    },
  );

  test('old firmware cannot accidentally run a finite spot check', () async {
    final transport = FakeControlTransport()..supportsContinuous = false;
    final controls = ResearchSessionController(transport);
    await controls.setTargets([ResearchTarget.ecg], start: true);
    expect(controls.supported, false);
    expect(controls.error, contains('3.1.2-continuous'));
    expect(transport.commands, isEmpty);
    await controls.shutdown();
    controls.dispose();
  });

  test(
    'watch busy is reported and other requested targets are attempted',
    () async {
      final transport = FakeControlTransport()..result = 1;
      final controls = ResearchSessionController(transport);
      await controls.setTargets([
        ResearchTarget.ecg,
        ResearchTarget.gsr,
      ], start: true);
      expect(transport.commands.length, 2);
      expect(controls.activeMask, 0);
      expect(controls.error, contains('busy'));
      await controls.shutdown();
      controls.dispose();
    },
  );

  test(
    'write acknowledgement alone never implies a streaming sensor',
    () async {
      final transport = FakeControlTransport()..acknowledge = false;
      final controls = ResearchSessionController(transport);
      await controls.open();
      final operation = controls.setTargets([ResearchTarget.ecg], start: true);
      await Future<void>.delayed(Duration.zero);
      expect(controls.busy, true);
      expect(controls.activeMask, 0);
      final command = transport.commands.single;
      transport.mask = 1;
      transport.publish(
        transport.status(id: command[3], opcode: 4, target: 2, state: 1),
      );
      expect(
        controls.busy,
        true,
      ); // Wrong target is not a matching acknowledgement.
      transport.publish(
        transport.status(id: command[3], opcode: 4, target: 1, state: 1),
      );
      await operation;
      expect(controls.stateLabel(ResearchTarget.ecg), 'Starting');
      transport.acknowledge = true;
      await controls.shutdown();
      controls.dispose();
    },
  );

  test(
    'sensor reboot clears prior states and does not restart measurements',
    () async {
      final transport = FakeControlTransport();
      final controls = ResearchSessionController(transport);
      await controls.setTargets([ResearchTarget.ecg], start: true);
      transport.boot++;
      transport.mask = 0;
      transport.publish(transport.status());
      expect(controls.activeMask, 0);
      expect(controls.error, contains('restarted'));
      expect(transport.commands.length, 1);
      await controls.shutdown();
      controls.dispose();
    },
  );

  test('malformed status is rejected', () {
    expect(() => ResearchStatus(Uint8List(19)), throwsFormatException);
    final transport = FakeControlTransport();
    final bytes = transport.status()..[5] = 99;
    expect(() => ResearchStatus(bytes), throwsFormatException);
  });
}

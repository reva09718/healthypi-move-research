import 'package:flutter_test/flutter_test.dart';
import 'package:move/ble/research_packet.dart';
import 'package:move/models/live_signal.dart';
import 'package:move/utils/live_stream_controller.dart';
import 'package:move/utils/research_session_controller.dart';
import 'live_stream_controller_test.dart' show FakeLiveTransport;
import 'research_session_controller_test.dart' show FakeControlTransport;
import 'research_packet_test.dart' show packet;

void main() {
  test(
    'all six IMU axes decode signed raw values and convert only for display',
    () {
      for (final signal in LiveSignal.values.where((s) => s.isImu)) {
        final decoded =
            ResearchPacket.decode(
              packet(
                signal: signal.researchId,
                flags: 3,
                samples: [-32768, -4096, 0, 4096, 32767],
              ),
            )!;
        expect(decoded.values, [-32768, -4096, 0, 4096, 32767]);
        expect(signal.rate, 25);
        expect(signal.axes.length, 3);
        expect(
          () => ResearchPacket.decode(packet(signal: signal.researchId)),
          throwsFormatException,
        );
      }
      expect(LiveSignal.accelX.displayValue(4096), 1.0);
      expect(LiveSignal.accelZ.displayValue(-32768), -8.0);
      expect(LiveSignal.gyroX.displayValue(-32768), -2000.0);
      expect(LiveSignal.gyroY.displayValue(16384), 1000.0);
    },
  );
  test(
    'interleaved IMU and PPG packets are routed to independent axes',
    () async {
      final transport = FakeLiveTransport();
      final received = <LiveSignal, List<double>>{};
      final feed = LiveStreamController(
        transport,
        onSamples: (s, v, _) => received[s] = v,
      );
      await feed.select(LiveSignal.values.toSet());
      for (final signal in LiveSignal.values.where((s) => s.isImu)) {
        transport.emit(
          packet(
            signal: signal.researchId,
            flags: 3,
            samples: [signal.researchId],
          ),
        );
      }
      transport.emit(packet(signal: 11, samples: [100]));
      expect(received.length, 7);
      expect(received[LiveSignal.accelX], [12]);
      expect(received[LiveSignal.gyroZ], [17]);
      expect(received[LiveSignal.fingerRed], [100]);
      expect(transport.enabled.length, 1);
      feed.dispose();
    },
  );
  test(
    'older firmware refuses IMU starts without sending an unsupported command',
    () async {
      final transport = FakeControlTransport();
      final controls = ResearchSessionController(transport);
      await controls.setTargets([ResearchTarget.accel], start: true);
      expect(transport.commands, isEmpty);
      expect(controls.error, contains('3.1.4-imu'));
      expect(controls.availableTargets.length, 4);
      await controls.shutdown();
      controls.dispose();
    },
  );
  test(
    'IMU capable firmware accepts six groups and stops gyro independently',
    () async {
      final transport = FakeControlTransport()..supportsImu = true;
      final controls = ResearchSessionController(transport);
      await controls.setTargets(ResearchTarget.values, start: true);
      expect(controls.activeMask, 63);
      expect(transport.commands.length, 6);
      await controls.setTargets([ResearchTarget.gyro], start: false);
      expect(controls.activeMask, 31);
      await controls.shutdown();
      controls.dispose();
      expect(transport.mask, 0);
    },
  );
}

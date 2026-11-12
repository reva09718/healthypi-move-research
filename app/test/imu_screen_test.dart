import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:move/models/device_info.dart';
import 'package:move/screens/scr_live.dart';
import 'package:move/theme/hpi_theme.dart';
import 'package:move/ui/charts/hpi_imu_waveform.dart';
import 'research_session_controller_test.dart' show FakeControlTransport;
import 'research_packet_test.dart' show packet;

void main() {
  for (final size in [const Size(360, 640), const Size(800, 1000)]) {
    testWidgets('three-axis motion graphs and six controls fit $size', (
      tester,
    ) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final transport = FakeControlTransport()..supportsImu = true;
      await tester.pumpWidget(
        MaterialApp(
          theme: HpiTheme.dark(),
          home: Scaffold(
            body: ScrLive(
              transport: transport,
              initialDevice: DeviceInfo(
                macAddress: 'watch',
                deviceName: 'Test watch',
                firstPaired: DateTime(2026),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Accelerometer').first);
      await tester.tap(find.text('Accelerometer').first);
      await tester.pumpAndSettle();
      for (int i = 12; i <= 14; i++) {
        transport.emit(
          packet(signal: i, flags: 3, samples: [i == 13 ? -4096 : 4096]),
        );
      }
      await tester.pump(const Duration(seconds: 1));
      expect(find.byType(HpiImuWaveform), findsOneWidget);
      expect(find.text('X: 1.000 g'), findsOneWidget);
      expect(find.text('Y: -1.000 g'), findsOneWidget);
      final chart = tester.widget<HpiImuWaveform>(find.byType(HpiImuWaveform));
      expect(chart.buffers.length, 3);
      expect(chart.colors.toSet().length, 3);
      await tester.tap(find.byType(Switch));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byWidgetPredicate((w) => w is DropdownButton).first,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('Gyroscope').last);
      await tester.pumpAndSettle();
      expect(find.byType(HpiImuWaveform), findsNWidgets(2));
      expect(tester.takeException(), isNull);
      await tester.tap(find.text('Start all'));
      await tester.pumpAndSettle();
      expect(transport.mask, 63);
      await tester.pumpWidget(const SizedBox());
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 50)),
      );
      await tester.pump();
      expect(transport.mask, 0);
    });
  }
}

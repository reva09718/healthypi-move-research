import 'dart:convert';
import 'dart:ffi' show DynamicLibrary;
import 'dart:io';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';
import 'package:sqlite3/open.dart';
import 'package:move/recording/native_vault.dart';
import 'package:move/recording/recording_store.dart';
import 'package:move/recording/research_recorder.dart';
import 'package:move/recording/cloud_account.dart';
import 'package:move/recording/cloud_upload.dart';
import 'package:move/recording/recording_export.dart';
import 'package:move/recording/recording_runtime.dart';
import 'package:move/models/device_info.dart';
import 'package:move/screens/scr_live.dart';
import 'package:move/theme/hpi_theme.dart';
import 'research_session_controller_test.dart' show FakeControlTransport;
import 'package:move/utils/connection_manager.dart';
import 'research_packet_test.dart' show packet;

// Portable authenticated fake for SQLite/lifecycle tests only. Production AES
// GCM is native Android Keystore; GcmVaultTest tests its actual cipher helper.
class TestCipher implements RecordingCipher {
  bool fail = false;
  @override
  Future<Uint8List> encrypt(String owner, String aad, Uint8List plain) async {
    if (fail) throw StateError('disk/cipher failure');
    final key = sha256.convert(utf8.encode(owner + aad)).bytes;
    final data = [
      for (var i = 0; i < plain.length; i++) plain[i] ^ key[i % key.length],
    ];
    return Uint8List.fromList([
      ...sha256.convert([...key, ...data]).bytes,
      ...data,
    ]);
  }

  @override
  Future<Uint8List> decrypt(String owner, String aad, Uint8List sealed) async {
    final key = sha256.convert(utf8.encode(owner + aad)).bytes,
        data = sealed.sublist(32);
    if (base64Encode(sealed.sublist(0, 32)) !=
        base64Encode(sha256.convert([...key, ...data]).bytes)) {
      throw StateError('tampered');
    }
    return Uint8List.fromList([
      for (var i = 0; i < data.length; i++) data[i] ^ key[i % key.length],
    ]);
  }
}

class TestService implements RecordingService {
  bool running = false;
  @override
  Future<void> start() async {
    running = true;
  }

  @override
  Future<void> stop() async {
    running = false;
  }
}

class TestSecrets implements SecretStore {
  final values = <String, String>{};
  @override
  Future<String?> readSecret(String key) async => values[key];
  @override
  Future<void> writeSecret(String key, String? value) async {
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }
}

const config = {
  'region': 'ap-southeast-2',
  'userPoolId': 'ap-southeast-2_test',
  'clientId': '1234567890abcdefghijkl',
  'apiBaseUrl': 'https://abc123.execute-api.ap-southeast-2.amazonaws.com',
};
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  if (Platform.isWindows) {
    final dll = Platform.environment['SQLITE3_LIBRARY_PATH'];
    if (dll != null) {
      open.overrideFor(OperatingSystem.windows, () => DynamicLibrary.open(dll));
    }
  }
  sqfliteFfiInit();
  late RecordingStore store;
  late Directory dir;
  late TestCipher cipher;
  final owner = ownerHash('a'), other = ownerHash('b');
  setUp(() async {
    dir = await Directory.systemTemp.createTemp('hpi-recording-test-');
    cipher = TestCipher();
    store = RecordingStore(
      cipher: cipher,
      factory: databaseFactoryFfi,
      path: '${dir.path}/record.db',
    );
  });
  tearDown(() async {
    await store.close();
    await dir.delete(recursive: true);
  });
  test(
    'atomic chunks, private account queries and ciphertext at rest',
    () async {
      final id = await store.create(owner, {
        'started_utc_ms': 1,
        'secret': 'private-note',
      }, mode: 'local');
      await store.append(
        owner,
        id,
        0,
        {
          'events': ['private-samples'],
        },
        {'started_utc_ms': 1, 'packets': 1},
      );
      expect(await store.get(other, id), isNull);
      expect(await store.list(other), isEmpty);
      await expectLater(store.readChunk(other, id, 0), throwsStateError);
      expect((await store.readChunk(owner, id, 0))['events'], [
        'private-samples',
      ]);
      final db = await store.database;
      final raw = (await db.query('chunks')).single['data'] as Uint8List;
      expect(
        utf8.decode(raw, allowMalformed: true),
        isNot(contains('private-samples')),
      );
      await expectLater(store.append(owner, id, 2, {}, {}), throwsStateError);
      expect((await store.get(owner, id))!.chunks, 1);
      raw[raw.length - 1] ^= 1;
      await db.update('chunks', {'data': raw});
      await expectLater(store.readChunk(owner, id, 0), throwsStateError);
    },
  );
  test(
    'process restart recovers interrupted session and persisted hybrid queue',
    () async {
      final id = await store.create(owner, {
        'started_utc_ms': 1,
      }, mode: 'hybrid');
      await store.append(
        owner,
        id,
        0,
        {'events': []},
        {'started_utc_ms': 1, 'packets': 0},
      );
      await store.close();
      store = RecordingStore(
        cipher: cipher,
        factory: databaseFactoryFfi,
        path: '${dir.path}/record.db',
      );
      await store.recoverInterrupted();
      expect((await store.get(owner, id))!.state, 'interrupted');
      expect(
        (await store.pending(
          owner,
          DateTime.now().millisecondsSinceEpoch,
        )).single.id,
        id,
      );
      await store.markChunkUploaded(owner, id, 0);
      await store.markChunkUploaded(owner, id, 0);
      expect((await store.get(owner, id))!.uploaded, 1);
    },
  );
  test(
    'all raw channels, gaps, reboot, duplicate and exact CSV values',
    () async {
      final service = TestService();
      final rec = ResearchRecorder(store, service);
      await rec.start(owner, mode: 'local');
      final id = rec.id!;
      for (var signal = 1; signal <= 17; signal++) {
        rec.accept(
          packet(
            signal: signal,
            flags: [1, 2, 6, 12, 13, 14, 15, 16, 17].contains(signal) ? 3 : 1,
            index: 0,
            samples: [123],
          ),
        );
      }
      rec.accept(packet(signal: 9, index: 3, samples: [4294967295]));
      rec.accept(packet(signal: 9, index: 3, samples: [4294967295]));
      rec.accept(packet(signal: 9, index: 0, boot: 44, samples: [7]));
      rec.status({'finger': 'waiting'});
      rec.status({'finger': 'streaming'});
      await rec.stop();
      final saved = (await store.get(owner, id))!;
      expect((saved.metadata['counts'] as Map).length, 17);
      expect(saved.metadata['missing_after_baseline']['9'], 2);
      expect(saved.metadata['duplicates_or_out_of_order'], 1);
      expect(saved.metadata['channel_boot_changes'], 1);
      final file = File('${dir.path}/export.csv');
      await exportRecording(store, saved, file);
      expect(await file.readAsString(), contains(',4294967295,'));
      expect(rec.active, false);
      expect(service.running, false);
      rec.dispose();
    },
  );
  test(
    'storage failure seals error with last committed counters and stops service',
    () async {
      final service = TestService();
      final rec = ResearchRecorder(store, service);
      await rec.start(owner, mode: 'local');
      final id = rec.id!;
      rec.accept(packet());
      cipher.fail = true;
      await rec.stop();
      cipher.fail = false;
      expect(rec.error, isNotNull);
      expect(rec.active, false);
      expect(service.running, false);
      // Final encryption failure preserves the last committed summary and
      // closes the session even when no chunk could be saved.
      expect((await store.get(owner, id))!.state, 'error');
      await store.recoverInterrupted();
      expect((await store.get(owner, id))!.chunks, 0);
      rec.dispose();
    },
  );
  test('SMP and firmware transfer cannot start during recording', () {
    final cm = ConnectionManager.instance;
    cm.setRecording(true);
    cm.setStreaming(false);
    expect(cm.isStreaming, true);
    expect(() => cm.acquireSmp('dfu'), throwsA(isA<SmpBusyException>()));
    cm.setRecording(false);
  });
  test(
    'chunk boundaries preserve exact durable packet and sample counts',
    () async {
      final rec = ResearchRecorder(store, TestService());
      await rec.start(owner, mode: 'local');
      final id = rec.id!;
      for (var i = 0; i < 500; i++) {
        rec.accept(packet(signal: 9, index: i * 3, samples: [1, 2, 3]));
      }
      await rec.stop();
      final saved = (await store.get(owner, id))!;
      expect(saved.chunks, greaterThan(1));
      expect(saved.metadata['packets'], 500);
      expect(saved.metadata['samples'], 1500);
      expect(saved.metadata['counts']['9'], 1500);
      var packets = 0;
      for (var seq = 0; seq < saved.chunks; seq++) {
        packets +=
            ((await store.readChunk(owner, id, seq))['events'] as List).length;
      }
      expect(packets, 500);
      rec.dispose();
    },
  );
  test('config only accepts Sydney public mobile endpoints', () {
    expect(CloudConfig(config).api, config['apiBaseUrl']);
    for (final bad in [
      'http://abc123.execute-api.ap-southeast-2.amazonaws.com',
      'https://example.com',
      'https://abc.execute-api.ap-south-1.amazonaws.com',
    ]) {
      expect(
        () => CloudConfig({...config, 'apiBaseUrl': bad}),
        throwsFormatException,
      );
    }
  });
  testWidgets(
    'recording keeps all channels across tab changes and stops on disconnect',
    (tester) async {
      tester.view.physicalSize = const Size(360, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final account = CloudAccount(TestSecrets()), service = TestService();
      final rec = ResearchRecorder(store, service);
      final upload = CloudUpload(store, account);
      final runtime = RecordingRuntime.testing(
        store: store,
        account: account,
        recorder: rec,
        upload: upload,
      );
      final transport = FakeControlTransport()..supportsImu = true;
      Widget screen(bool active) => MaterialApp(
        theme: HpiTheme.dark(),
        home: Scaffold(
          body: ScrLive(
            active: active,
            recordingRuntime: runtime,
            transport: transport,
            initialDevice: DeviceInfo(
              macAddress: 'watch',
              deviceName: 'Test watch',
              firstPaired: DateTime(2026),
            ),
          ),
        ),
      );
      await tester.pumpWidget(screen(true));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Start all'));
      await tester.pumpAndSettle();
      expect(transport.mask, 63);
      await tester.runAsync(() => rec.start(account.owner, mode: 'local'));
      final id = rec.id!;
      await tester.pump();
      await tester.pumpWidget(screen(false));
      await tester.pumpAndSettle();
      expect(transport.mask, 63);
      expect(service.running, true);
      // The selected chart is wrist green. A scalar and gyro packet must still
      // be captured while Live is inactive, without resubscribing or stopping.
      transport.emit(packet(signal: 8, samples: [95], index: 0));
      transport.emit(packet(signal: 17, flags: 3, samples: [123], index: 0));
      await tester.runAsync(() => rec.stop());
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 100)),
      );
      await tester.pumpAndSettle();
      expect(transport.mask, 0);
      expect(service.running, false);
      final saved = await tester.runAsync(() => store.get(account.owner, id));
      expect(saved!.metadata['counts'], {'8': 1, '17': 1});
      await tester.pumpWidget(screen(true));
      await tester.pumpAndSettle();
      await tester.runAsync(() => rec.start(account.owner, mode: 'local'));
      final interrupted = rec.id!;
      await tester.runAsync(() async {
        transport.connected = false;
        ConnectionManager.instance.setStreaming(true);
        ConnectionManager.instance.setStreaming(false);
        expect(rec.stopping, true); // Link handler started finalization.
        await rec.stop(); // Await that same interrupted finalization.
      });
      await tester.pump();
      expect(rec.active, false);
      final stopped = await tester.runAsync(
        () => store.get(account.owner, interrupted),
      );
      expect(stopped!.state, 'interrupted');
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      upload.disposeWorker();
      rec.dispose();
      expect(tester.takeException(), isNull);
    },
  );
  test(
    'hybrid retry survives offline and commits before marking uploaded',
    () async {
      final secrets = TestSecrets();
      final account = CloudAccount(
        secrets,
        client: MockClient((r) async {
          final operation = r.headers['X-Amz-Target']!;
          if (operation.endsWith('InitiateAuth')) {
            return http.Response(
              jsonEncode({
                'AuthenticationResult': {
                  'AccessToken': 'token',
                  'RefreshToken': 'refresh',
                  'ExpiresIn': 3600,
                },
              }),
              200,
            );
          }
          return http.Response(
            jsonEncode({
              'UserAttributes': [
                {
                  'Name': 'sub',
                  'Value': '00000000-0000-4000-8000-000000000001',
                },
                {'Name': 'email', 'Value': 'person@example.org'},
              ],
            }),
            200,
          );
        }),
      );
      await account.setConfig(jsonEncode(config));
      await account.signIn('person@example.org', 'password');
      final id = await store.create(account.owner, {
        'started_utc_ms': 1,
      }, mode: 'hybrid');
      await store.append(
        account.owner,
        id,
        0,
        {'events': []},
        {'started_utc_ms': 1},
      );
      await store.finish(account.owner, id, 'completed', {'started_utc_ms': 1});
      var offline = true, commit = false, put = false;
      final upload = CloudUpload(
        store,
        account,
        client: MockClient((r) async {
          if (offline) throw const SocketException('offline');
          if (r.url.path.endsWith('/upload')) {
            return http.Response(
              jsonEncode({
                'url': 'https://test.s3.ap-southeast-2.amazonaws.com/chunk',
                'headers': {'Content-Type': 'application/gzip'},
              }),
              200,
            );
          }
          if (r.method == 'PUT') {
            put = true;
            return http.Response('', 200);
          }
          if (r.url.path.endsWith('/commit')) {
            expect(put, true);
            commit = true;
          }
          if (r.url.path.endsWith('/complete')) expect(commit, true);
          return http.Response('{}', 200);
        }),
      );
      await upload.run();
      expect((await store.get(account.owner, id))!.cloudState, 'queued');
      expect((await store.get(account.owner, id))!.uploaded, 0);
      offline = false;
      await upload.run(retry: true);
      expect((await store.get(account.owner, id))!.cloudState, 'uploaded');
      expect((await store.get(account.owner, id))!.uploaded, 1);
      expect(secrets.values.values.join(), isNot(contains('password')));
      final currentOwner = account.owner;
      await account.signOut();
      expect(await store.list(account.owner), isEmpty);
      expect((await store.list(currentOwner)).length, 1);
      upload.disposeWorker();
    },
  );
}

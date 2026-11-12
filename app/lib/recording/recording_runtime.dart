import 'dart:async';
import 'native_vault.dart';
import 'cloud_account.dart';
import 'cloud_upload.dart';
import 'recording_store.dart';
import 'research_recorder.dart';

class RecordingRuntime {
  RecordingRuntime.testing({
    required this.store,
    required this.account,
    required this.recorder,
    required this.upload,
  }) {
    _initializing = Future.value();
  }
  RecordingRuntime._() {
    store = RecordingStore(cipher: vault);
    account = CloudAccount(vault);
    recorder = ResearchRecorder(store, AndroidRecordingService());
    upload = CloudUpload(store, account);
    recorder.addListener(() {
      if (!recorder.locked) unawaited(upload.run());
    });
    NativeVault.channel.setMethodCallHandler((call) async {
      if (call.method == 'stopRequested') {
        await recorder.stop(state: 'interrupted');
      }
    });
  }
  static final instance = RecordingRuntime._();
  final vault = NativeVault();
  late final RecordingStore store;
  late final CloudAccount account;
  late final ResearchRecorder recorder;
  late final CloudUpload upload;
  Future<void>? _initializing;
  Future<void> initialize() =>
      _initializing ??= _init().catchError((Object e) {
        _initializing = null;
        throw e;
      });
  Future<void> _init() async {
    await store.recoverInterrupted();
    await account.load();
    upload.start();
    unawaited(upload.run());
  }
}

import 'package:flutter/services.dart';

abstract interface class RecordingCipher {
  Future<Uint8List> encrypt(String owner, String aad, Uint8List plaintext);
  Future<Uint8List> decrypt(String owner, String aad, Uint8List encrypted);
}

abstract interface class SecretStore {
  Future<String?> readSecret(String key);
  Future<void> writeSecret(String key, String? value);
}

/// Android Keystore keys never leave the native process. Ciphertexts include
/// a random GCM nonce and authentication tag; AAD binds owner/session/sequence.
class NativeVault implements RecordingCipher, SecretStore {
  static const channel = MethodChannel(
    'com.protocentral.move/research_recording',
  );
  @override
  Future<Uint8List> encrypt(
    String owner,
    String aad,
    Uint8List plaintext,
  ) async =>
      (await channel.invokeMethod<Uint8List>('encrypt', {
        'owner': owner,
        'aad': aad,
        'data': plaintext,
      }))!;
  @override
  Future<Uint8List> decrypt(
    String owner,
    String aad,
    Uint8List encrypted,
  ) async =>
      (await channel.invokeMethod<Uint8List>('decrypt', {
        'owner': owner,
        'aad': aad,
        'data': encrypted,
      }))!;
  @override
  Future<String?> readSecret(String key) =>
      channel.invokeMethod<String>('readSecret', {'key': key});
  @override
  Future<void> writeSecret(String key, String? value) =>
      channel.invokeMethod<void>('writeSecret', {'key': key, 'value': value});
}

abstract interface class RecordingService {
  Future<void> start();
  Future<void> stop();
}

class AndroidRecordingService implements RecordingService {
  @override
  Future<void> start() =>
      NativeVault.channel.invokeMethod<void>('startRecordingService');
  @override
  Future<void> stop() =>
      NativeVault.channel.invokeMethod<void>('stopRecordingService');
}

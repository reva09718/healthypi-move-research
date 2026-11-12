import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'cloud_account.dart';
import 'recording_store.dart';

class CloudUpload extends ChangeNotifier {
  CloudUpload(this.store, this.account, {http.Client? client})
    : client = client ?? http.Client();
  final RecordingStore store;
  final CloudAccount account;
  final http.Client client;
  bool busy = false;
  String? error;
  Timer? _timer;
  void start() {
    _timer ??= Timer.periodic(
      const Duration(seconds: 30),
      (_) => unawaited(run()),
    );
  }

  void disposeWorker() {
    _timer?.cancel();
    client.close();
  }

  Future<Map<String, dynamic>> _api(
    String owner,
    String path,
    Map<String, dynamic> body,
  ) async {
    if (account.owner != owner) {
      throw StateError('Account changed; upload remains queued.');
    }
    final base = account.config!.api;
    final token = await account.accessToken();
    if (account.owner != owner) {
      throw StateError('Account changed; upload remains queued.');
    }
    final r = await client
        .post(
          Uri.parse('$base$path'),
          headers: {
            'Authorization': 'Bearer $token',
            'Content-Type': 'application/json',
          },
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 30));
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw StateError(
        'Cloud request failed (${r.statusCode}). Recording remains local.',
      );
    }
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<void> run({bool retry = false}) async {
    if (busy || !account.signedIn || account.config == null) return;
    busy = true;
    error = null;
    notifyListeners();
    final owner = account.owner;
    try {
      if (retry) await store.retryNow(owner);
      for (final session in await store.pending(
        owner,
        DateTime.now().millisecondsSinceEpoch,
      )) {
        if (account.owner != owner) break;
        try {
          await store.queueState(owner, session.id, 'uploading');
          await _api(owner, '/sessions', {
            'id': session.id,
            'metadata': session.metadata,
          });
          for (var seq = 0; seq < session.chunks; seq++) {
            if (await store.chunkUploaded(owner, session.id, seq)) continue;
            final chunk = await store.readChunk(owner, session.id, seq);
            final bytes = gzip.encode(utf8.encode(jsonEncode(chunk)));
            final digest = sha256.convert(bytes).toString();
            final plan = await _api(
              owner,
              '/sessions/${session.id}/chunks/$seq/upload',
              {'sha256': digest, 'bytes': bytes.length},
            );
            final uri = Uri.parse(plan['url'] as String);
            if (uri.scheme != 'https' ||
                !RegExp(
                  r'(^|\.)s3[.-]ap-southeast-1\.amazonaws\.com$',
                ).hasMatch(uri.host)) {
              throw StateError(
                'Cloud returned an unexpected storage endpoint.',
              );
            }
            final response = await client
                .put(
                  uri,
                  headers: Map<String, String>.from(plan['headers'] as Map),
                  body: bytes,
                )
                .timeout(const Duration(seconds: 60));
            if (response.statusCode < 200 || response.statusCode >= 300) {
              throw StateError('Chunk upload failed (${response.statusCode})');
            }
            await _api(owner, '/sessions/${session.id}/chunks/$seq/commit', {});
            await store.markChunkUploaded(owner, session.id, seq);
            notifyListeners();
          }
          await _api(owner, '/sessions/${session.id}/complete', {
            'chunks': session.chunks,
            'state': session.state,
            'metadata': session.metadata,
          });
          await store.queueState(owner, session.id, 'uploaded');
        } catch (e) {
          error = '$e';
          final attempts = (session.row['attempts']! as int) + 1;
          final delay = min(1800, 15 * pow(2, min(attempts - 1, 7)).toInt());
          await store.queueState(
            owner,
            session.id,
            'queued',
            attempts: attempts,
            retryAtMs: DateTime.now().millisecondsSinceEpoch + delay * 1000,
          );
          // One failed network/auth request is enough for this pass.
          break;
        }
      }
    } catch (e) {
      error = '$e';
    } finally {
      busy = false;
      notifyListeners();
    }
  }
}

import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:sqflite/sqflite.dart';
import 'native_vault.dart';

String newRecordingId() {
  final b = List<int>.generate(16, (_) => Random.secure().nextInt(256));
  b[6] = (b[6] & 15) | 64;
  b[8] = (b[8] & 63) | 128;
  final h = b.map((v) => v.toRadixString(16).padLeft(2, '0')).join();
  return '${h.substring(0, 8)}-${h.substring(8, 12)}-${h.substring(12, 16)}-${h.substring(16, 20)}-${h.substring(20)}';
}

String ownerHash(String identity) =>
    sha256.convert(utf8.encode(identity)).toString();
const offlineOwnerIdentity = 'local-device-v1';

class SavedRecording {
  SavedRecording(this.row, this.metadata);
  final Map<String, Object?> row;
  final Map<String, dynamic> metadata;
  String get id => row['id']! as String;
  String get owner => row['owner']! as String;
  String get state => row['state']! as String;
  String get mode => row['mode']! as String;
  int get startedMs => row['started_ms']! as int;
  int get chunks => row['chunks']! as int;
  int get uploaded => row['uploaded']! as int;
  String get cloudState => row['cloud_state']! as String;
  bool get complete => state != 'recording';
}

/// A separate database from Healthy Store: account-scoped encrypted raw BLE
/// chunks, encrypted session metadata and a durable hybrid upload queue.
/// Only IDs/times/state/queue bookkeeping remain plaintext in the app sandbox.
class RecordingStore {
  RecordingStore({required this.cipher, DatabaseFactory? factory, String? path})
    : _factory = factory,
      _path = path;
  final RecordingCipher cipher;
  final DatabaseFactory? _factory;
  final String? _path;
  Database? _database;
  Future<Database>? _opening;

  Future<Database> get database => _opening ??= _open();
  Future<Database> _open() async {
    final p = _path ?? '${await getDatabasesPath()}/research_recordings_v1.db';
    return _database = await (_factory ?? databaseFactory).openDatabase(
      p,
      options: OpenDatabaseOptions(
        version: 1,
        onConfigure: (db) async {
          await db.execute('PRAGMA foreign_keys=ON');
          await db.rawQuery('PRAGMA journal_mode=WAL');
          await db.execute('PRAGMA synchronous=FULL');
        },
        onCreate: (db, _) async {
          await db.execute(
            'CREATE TABLE sessions (owner TEXT NOT NULL, id TEXT NOT NULL, '
            'started_ms INTEGER NOT NULL, ended_ms INTEGER, state TEXT NOT NULL, mode TEXT NOT NULL, '
            'chunks INTEGER NOT NULL DEFAULT 0, uploaded INTEGER NOT NULL DEFAULT 0, '
            'cloud_state TEXT NOT NULL DEFAULT "local", retry_at_ms INTEGER NOT NULL DEFAULT 0, '
            'attempts INTEGER NOT NULL DEFAULT 0, metadata BLOB NOT NULL, PRIMARY KEY(owner,id))',
          );
          await db.execute(
            'CREATE TABLE chunks (owner TEXT NOT NULL, session_id TEXT NOT NULL, '
            'seq INTEGER NOT NULL, data BLOB NOT NULL, digest TEXT NOT NULL, '
            'uploaded INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,session_id,seq), '
            'FOREIGN KEY(owner,session_id) REFERENCES sessions(owner,id))',
          );
          await db.execute(
            'CREATE INDEX pending_uploads ON sessions(owner,mode,cloud_state,retry_at_ms)',
          );
        },
      ),
    );
  }

  Future<Uint8List> _seal(String owner, String aad, Object value) => cipher
      .encrypt(owner, aad, Uint8List.fromList(utf8.encode(jsonEncode(value))));
  Future<Map<String, dynamic>> _unseal(
    String owner,
    String aad,
    Uint8List value,
  ) async =>
      jsonDecode(utf8.decode(await cipher.decrypt(owner, aad, value)))
          as Map<String, dynamic>;
  String _metaAad(String owner, String id) =>
      'hpi-record-v1|$owner|$id|metadata';
  String _chunkAad(String owner, String id, int seq) =>
      'hpi-record-v1|$owner|$id|$seq';

  Future<String> create(
    String owner,
    Map<String, dynamic> metadata, {
    required String mode,
  }) async {
    if (mode != 'local' && mode != 'hybrid') {
      throw ArgumentError('Invalid storage mode');
    }
    final id = newRecordingId();
    final sealed = await _seal(owner, _metaAad(owner, id), metadata);
    await (await database).insert('sessions', {
      'owner': owner,
      'id': id,
      'started_ms': metadata['started_utc_ms'],
      'state': 'recording',
      'mode': mode,
      'cloud_state': mode == 'hybrid' ? 'queued' : 'local',
      'metadata': sealed,
    });
    return id;
  }

  Future<void> append(
    String owner,
    String id,
    int seq,
    Map<String, dynamic> chunk,
    Map<String, dynamic> metadata,
  ) async {
    final sealed = await _seal(owner, _chunkAad(owner, id, seq), chunk);
    final meta = await _seal(owner, _metaAad(owner, id), metadata);
    await (await database).transaction((txn) async {
      final rows = await txn.query(
        'sessions',
        columns: ['chunks', 'state'],
        where: 'owner=? AND id=?',
        whereArgs: [owner, id],
      );
      if (rows.isEmpty ||
          rows.single['state'] != 'recording' ||
          rows.single['chunks'] != seq) {
        throw StateError(
          'Recording is closed or chunk sequence is inconsistent',
        );
      }
      await txn.insert('chunks', {
        'owner': owner,
        'session_id': id,
        'seq': seq,
        'data': sealed,
        'digest': sha256.convert(sealed).toString(),
      });
      await txn.update(
        'sessions',
        {'chunks': seq + 1, 'metadata': meta},
        where: 'owner=? AND id=?',
        whereArgs: [owner, id],
      );
    });
  }

  Future<void> finish(
    String owner,
    String id,
    String state,
    Map<String, dynamic> metadata,
  ) async {
    if (!['completed', 'interrupted', 'error'].contains(state)) {
      throw ArgumentError('Invalid final state');
    }
    final meta = await _seal(owner, _metaAad(owner, id), metadata);
    await (await database).update(
      'sessions',
      {
        'state': state,
        'ended_ms': DateTime.now().millisecondsSinceEpoch,
        'metadata': meta,
      },
      where: 'owner=? AND id=?',
      whereArgs: [owner, id],
    );
  }

  Future<SavedRecording?> get(String owner, String id) async {
    final rows = await (await database).query(
      'sessions',
      where: 'owner=? AND id=?',
      whereArgs: [owner, id],
    );
    if (rows.isEmpty) return null;
    final row = rows.single;
    return SavedRecording(
      row,
      await _unseal(owner, _metaAad(owner, id), row['metadata']! as Uint8List),
    );
  }

  /// If encrypting final metadata fails, retain the last authenticated chunk
  /// summary but close the session. No fabricated unsaved counters are added.
  Future<void> finishWithLastCommittedMetadata(String owner, String id) async {
    await (await database).update(
      'sessions',
      {'state': 'error', 'ended_ms': DateTime.now().millisecondsSinceEpoch},
      where: 'owner=? AND id=? AND state=?',
      whereArgs: [owner, id, 'recording'],
    );
  }

  Future<List<SavedRecording>> list(String owner) async {
    final rows = await (await database).query(
      'sessions',
      where: 'owner=?',
      whereArgs: [owner],
      orderBy: 'started_ms DESC',
    );
    return [
      for (final row in rows)
        SavedRecording(
          row,
          await _unseal(
            owner,
            _metaAad(owner, row['id']! as String),
            row['metadata']! as Uint8List,
          ),
        ),
    ];
  }

  Future<Map<String, dynamic>> readChunk(
    String owner,
    String id,
    int seq,
  ) async {
    final rows = await (await database).query(
      'chunks',
      where: 'owner=? AND session_id=? AND seq=?',
      whereArgs: [owner, id, seq],
    );
    if (rows.isEmpty) {
      throw StateError('Recording chunk is unavailable for this account');
    }
    final bytes = rows.single['data']! as Uint8List;
    if (sha256.convert(bytes).toString() != rows.single['digest']) {
      throw StateError('Stored chunk checksum failed');
    }
    return _unseal(owner, _chunkAad(owner, id, seq), bytes);
  }

  Future<void> recoverInterrupted() async {
    // Metadata is committed atomically with each chunk. Buffered samples lost
    // with a killed process are never represented as saved data.
    await (await database).update(
      'sessions',
      {
        'state': 'interrupted',
        'ended_ms': DateTime.now().millisecondsSinceEpoch,
      },
      where: 'state=?',
      whereArgs: ['recording'],
    );
  }

  Future<List<SavedRecording>> pending(String owner, int nowMs) async =>
      (await list(owner))
          .where(
            (s) =>
                s.mode == 'hybrid' &&
                s.complete &&
                s.cloudState != 'uploaded' &&
                (s.row['retry_at_ms']! as int) <= nowMs,
          )
          .toList();
  Future<void> markChunkUploaded(String owner, String id, int seq) async {
    await (await database).transaction((txn) async {
      await txn.update(
        'chunks',
        {'uploaded': 1},
        where: 'owner=? AND session_id=? AND seq=?',
        whereArgs: [owner, id, seq],
      );
      final count =
          Sqflite.firstIntValue(
            await txn.rawQuery(
              'SELECT COUNT(*) FROM chunks WHERE owner=? AND session_id=? AND uploaded=1',
              [owner, id],
            ),
          )!;
      await txn.update(
        'sessions',
        {'uploaded': count},
        where: 'owner=? AND id=?',
        whereArgs: [owner, id],
      );
    });
  }

  Future<bool> chunkUploaded(String owner, String id, int seq) async =>
      (await (await database).query(
        'chunks',
        columns: ['uploaded'],
        where: 'owner=? AND session_id=? AND seq=?',
        whereArgs: [owner, id, seq],
      )).single['uploaded'] ==
      1;
  Future<void> queueState(
    String owner,
    String id,
    String state, {
    int retryAtMs = 0,
    int? attempts,
  }) async {
    await (await database).update(
      'sessions',
      {
        'cloud_state': state,
        'retry_at_ms': retryAtMs,
        if (attempts != null) 'attempts': attempts,
      },
      where: 'owner=? AND id=?',
      whereArgs: [owner, id],
    );
  }

  Future<void> retryNow(String owner) async => (await database)
      .update(
        'sessions',
        {'retry_at_ms': 0},
        where: 'owner=? AND cloud_state!=?',
        whereArgs: [owner, 'uploaded'],
      )
      .then((_) {});
  Future<void> close() async {
    await _database?.close();
    _database = null;
    _opening = null;
  }
}

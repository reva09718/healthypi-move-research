import 'dart:convert';
import 'dart:io';
import '../ble/research_packet.dart';
import 'recording_store.dart';

/// Stream one encrypted chunk at a time; a five-hour session is not loaded
/// into memory. Receive timestamps repeat for all samples in a BLE packet.
Future<void> exportRecording(
  RecordingStore store,
  SavedRecording session,
  File file,
) async {
  if (!session.complete) throw StateError('Stop recording before exporting');
  final sink = file.openWrite();
  try {
    sink.writeln(
      'session_id,received_utc_us,elapsed_us,signal_id,boot_id,sample_index,estimated_packet_uptime_ms,raw_value,duplicate_or_out_of_order,missing_before_packet',
    );
    for (var seq = 0; seq < session.chunks; seq++) {
      final chunk = await store.readChunk(session.owner, session.id, seq);
      for (final e in chunk['events'] as List) {
        if (e['type'] != 'packet') continue;
        final p = ResearchPacket.decode(base64Decode(e['raw_b64'] as String))!;
        for (var i = 0; i < p.values.length; i++) {
          sink.writeln(
            '${session.id},${e['received_utc_us']},${e['elapsed_us']},${p.signalId},${p.bootId},${(p.firstIndex + i) & 0xffffffff},${p.estimatedUptimeMs},${p.values[i].toInt()},${e['duplicate_or_out_of_order']},${i == 0 ? e['missing_before'] : 0}',
          );
        }
      }
      await sink.flush();
    }
  } finally {
    await sink.close();
  }
}

import 'dart:typed_data';

const researchService = 'a17e0000-4d52-4f56-9100-524553454152';
const researchData = 'a17e0003-4d52-4f56-9100-524553454152';

/// HPR1 wire contract, shared with fw/tools/research_protocol.py.
/// Device uptime is an estimate, not UTC or an exact acquisition timestamp.
class ResearchPacket {
  const ResearchPacket(
    this.signalId,
    this.bootId,
    this.firstIndex,
    this.estimatedUptimeMs,
    this.values,
  );

  final int signalId, bootId, firstIndex, estimatedUptimeMs;
  final List<double> values;

  /// Unknown signal IDs are ignored, allowing future additive extensions.
  static ResearchPacket? decode(Uint8List bytes) {
    if (bytes.length < 20) {
      throw const FormatException('Research packet too short');
    }
    final data = ByteData.sublistView(bytes);
    final version = data.getUint8(0), signal = data.getUint8(1);
    final flags = data.getUint8(2), count = data.getUint8(3);
    if (version != 1 || count == 0 || bytes.length != 16 + count * 4) {
      throw const FormatException('Invalid research packet header or length');
    }
    if (signal < 1 || signal > 17) return null;
    final signed = signal == 1 || signal == 2 || signal == 6 || signal >= 12;
    if ((flags & ~3) != 0 ||
        (flags & 1) == 0 ||
        ((flags & 2) != 0) != signed ||
        (signal >= 5 && signal <= 8 && count != 1)) {
      throw const FormatException(
        'Invalid research packet flags or sample count',
      );
    }
    return ResearchPacket(
      signal,
      data.getUint32(4, Endian.little),
      data.getUint32(8, Endian.little),
      data.getUint32(12, Endian.little),
      [
        for (int i = 0; i < count; i++)
          (signed
                  ? data.getInt32(16 + i * 4, Endian.little)
                  : data.getUint32(16 + i * 4, Endian.little))
              .toDouble(),
      ],
    );
  }
}

class ResearchContinuity {
  final _next = <int, (int, int)>{};

  /// Null means duplicate/out-of-order. Missing counts only follow a baseline.
  ({int missing, bool reset})? observe(ResearchPacket packet) {
    final previous = _next[packet.signalId];
    int missing = 0;
    final reset = previous != null && previous.$1 != packet.bootId;
    if (previous != null && !reset) {
      final delta = (packet.firstIndex - previous.$2) & 0xffffffff;
      if (delta >= 0x80000000) return null;
      missing = delta;
    }
    _next[packet.signalId] = (
      packet.bootId,
      (packet.firstIndex + packet.values.length) & 0xffffffff,
    );
    return (missing: missing, reset: reset);
  }
}

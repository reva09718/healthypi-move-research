import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'hpi_sweep_waveform.dart';

/// Three axes share one symmetric Y scale; no gravity/bias subtraction.
class HpiImuWaveform extends StatelessWidget {
  const HpiImuWaveform({
    super.key,
    required this.buffers,
    required this.colors,
    required this.scale,
    required this.minimumRange,
    required this.unit,
  });
  final List<SweepBuffer> buffers;
  final List<Color> colors;
  final double scale, minimumRange;
  final String unit;

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: Listenable.merge(buffers),
    builder:
        (context, child) => CustomPaint(
          size: Size.infinite,
          painter: _ImuPainter(buffers, colors, scale, minimumRange, unit),
        ),
  );
}

class _ImuPainter extends CustomPainter {
  _ImuPainter(
    this.buffers,
    this.colors,
    this.scale,
    this.minimumRange,
    this.unit,
  );
  final List<SweepBuffer> buffers;
  final List<Color> colors;
  final double scale, minimumRange;
  final String unit;
  @override
  void paint(Canvas canvas, Size size) {
    double extent = minimumRange;
    for (final buffer in buffers) {
      for (final value in buffer.samples) {
        if (value != null) extent = math.max(extent, value.abs() * scale * 1.1);
      }
    }
    final grid =
        Paint()
          ..color = Colors.white.withValues(alpha: 0.12)
          ..strokeWidth = 1;
    for (int row = 0; row <= 4; row++) {
      final y = size.height * row / 4;
      canvas.drawLine(Offset(0, y), Offset(size.width, y), grid);
    }
    canvas.save();
    canvas.clipRect(Offset.zero & size);
    for (int axis = 0; axis < buffers.length; axis++) {
      final buffer = buffers[axis];
      final trace =
          Paint()
            ..color = colors[axis]
            ..strokeWidth = 1.8
            ..style = PaintingStyle.stroke;
      Path? path;
      for (int i = 0; i < buffer.capacity; i++) {
        final value = buffer.samples[i];
        if (value == null) {
          if (path != null) canvas.drawPath(path, trace);
          path = null;
          continue;
        }
        final x = size.width * i / (buffer.capacity - 1);
        final y = size.height * (0.5 - value * scale / (2 * extent));
        if (path == null) {
          path = Path()..moveTo(x, y);
        } else {
          path.lineTo(x, y);
        }
      }
      if (path != null) canvas.drawPath(path, trace);
    }
    canvas.restore();
    for (final entry in [
      (extent, 0.0),
      (0.0, size.height / 2),
      (-extent, size.height - 12),
    ]) {
      final label = TextPainter(
        textDirection: TextDirection.ltr,
        text: TextSpan(
          text: '${entry.$1.toStringAsFixed(2)} $unit',
          style: const TextStyle(color: Colors.white60, fontSize: 9),
        ),
      )..layout();
      label.paint(canvas, Offset(2, entry.$2));
    }
  }

  @override
  bool shouldRepaint(covariant _ImuPainter oldDelegate) => true;
}

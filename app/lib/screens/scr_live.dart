// Copyright (c) 2024-2026 ProtoCentral
// SPDX-License-Identifier: MIT

import 'dart:async';
import 'package:flutter/material.dart';
import 'package:material_symbols_icons/symbols.dart';

import '../models/device_info.dart';
import '../models/live_signal.dart';
import '../theme/hpi_colors.dart';
import '../theme/hpi_text.dart';
import '../ui/charts/hpi_sweep_waveform.dart';
import '../ui/charts/hpi_imu_waveform.dart';
import '../ui/components/hpi_components.dart';
import '../utils/connection_manager.dart';
import '../utils/device_manager.dart';
import '../utils/live_stream_controller.dart';
import '../utils/research_session_controller.dart';

class ScrLive extends StatefulWidget {
  const ScrLive({
    super.key,
    this.initialDevice,
    this.transport,
    this.active = true,
  });
  final bool active;
  final DeviceInfo? initialDevice;
  final LiveTransport? transport;
  @override
  State<ScrLive> createState() => _ScrLiveState();
}

class _ScrLiveState extends State<ScrLive> {
  final _cm = ConnectionManager.instance;
  DeviceInfo? _device;
  LiveStreamController? _feed;
  LiveTransport? _transport;
  ResearchSessionController? _controls;
  Future<void> _controlLifecycle = Future.value();
  bool _resolving = true;
  bool _wasConnected = false;
  bool _wasSmpBusy = false;
  bool _compare = false;
  String? _connectionError;
  LiveSignal _primary = LiveSignal.wristGreen;
  LiveSignal _secondary = LiveSignal.wristRed;
  final _buffers = <LiveSignal, SweepBuffer>{};
  final _lastSample = <LiveSignal, DateTime>{};
  final _latest = <LiveSignal, double>{};
  Timer? _ticker;

  bool get _connected =>
      widget.transport?.connected ??
      (_cm.isConnected && _cm.deviceId == _device?.macAddress);
  Set<LiveSignal> get _selected => {_primary, if (_compare) _secondary};

  @override
  void initState() {
    super.initState();
    _cm.addListener(_onLink);
    DeviceManager.pairingRevision.addListener(_init);
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && widget.active && _connected) setState(() {});
    });
    _init();
  }

  @override
  void didUpdateWidget(covariant ScrLive oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.active != widget.active) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _restart();
      });
    }
  }

  @override
  void dispose() {
    _cm.removeListener(_onLink);
    DeviceManager.pairingRevision.removeListener(_init);
    _ticker?.cancel();
    final controls = _controls;
    controls?.removeListener(_onControls);
    if (controls != null) {
      unawaited(controls.shutdown().whenComplete(controls.dispose));
    }
    _feed?.removeListener(_onFeed);
    _feed?.dispose();
    _cm.setStreaming(false);
    for (final buffer in _buffers.values) {
      buffer.dispose();
    }
    super.dispose();
  }

  SweepBuffer _buffer(LiveSignal signal) => _buffers.putIfAbsent(
    signal,
    () => SweepBuffer(
      capacity: signal.rate * 8,
      gap: signal.rate ~/ 8,
      robustScale: signal.isPpg,
    ),
  );

  void _onFeed() {
    if (!mounted) return;
    _cm.setStreaming(_feed?.isStreaming ?? false);
    setState(() {});
  }

  void _onLink() {
    if (!mounted) return;
    // Streaming/SMP notifications also notify ConnectionManager listeners.
    // Only a real link transition should reconfigure subscriptions.
    final connected = _connected;
    if (connected != _wasConnected || _cm.isSmpBusy != _wasSmpBusy) {
      _wasConnected = connected;
      _wasSmpBusy = _cm.isSmpBusy;
      _restart();
    }
    setState(() {});
  }

  Future<void> _init() async {
    final device =
        widget.initialDevice ?? await DeviceManager.getPairedDevice();
    if (!mounted) return;
    if (_device?.macAddress != device?.macAddress || _feed == null) {
      final previous = _controls;
      previous?.removeListener(_onControls);
      _controls = null;
      if (previous != null) {
        _controlLifecycle = _controlLifecycle.then((_) async {
          await previous.shutdown();
          previous.dispose();
        });
      }
      _feed?.removeListener(_onFeed);
      _feed?.dispose();
      _transport =
          device == null
              ? null
              : widget.transport ?? ConnectedLiveTransport(device.macAddress);
      _feed =
          device == null
              ? null
              : LiveStreamController(
                _transport!,
                onSamples: (signal, values, breakBefore) {
                  if (!mounted || values.isEmpty) return;
                  final buffer = _buffer(signal);
                  final last = _lastSample[signal];
                  if (breakBefore ||
                      (last != null &&
                          DateTime.now().difference(last) >
                              const Duration(seconds: 3))) {
                    buffer.clear();
                  }
                  _lastSample[signal] = DateTime.now();
                  _latest[signal] = values.last;
                  buffer.addAll(values);
                },
              );
      _feed?.addListener(_onFeed);
    }
    setState(() {
      _device = device;
      _resolving = false;
    });
    _wasConnected = _connected;
    _restart();
  }

  void _restart({bool force = false}) {
    _lastSample.clear();
    _latest.clear();
    for (final buffer in _buffers.values) {
      buffer.clear();
    }
    unawaited(
      _feed?.select(
        !widget.active || _cm.isSmpBusy
            ? {}
            : _transport?.hasResearch == true
            ? LiveSignal.values.toSet()
            : _selected,
        force: force,
      ),
    );
    _controlLifecycle = _controlLifecycle.then((_) async {
      if (!mounted) return;
      final enabled = widget.active && !_cm.isSmpBusy && _connected;
      if (enabled && force && _controls != null && !_controls!.ready) {
        final old = _controls!;
        _controls = null;
        old.removeListener(_onControls);
        await old.shutdown();
        old.dispose();
      }
      if (!enabled && _controls != null) {
        final old = _controls!;
        _controls = null;
        old.removeListener(_onControls);
        await old.shutdown();
        old.dispose();
        if (mounted) setState(() {});
      } else if (enabled &&
          _controls == null &&
          _transport is ResearchControlTransport &&
          _transport!.hasResearch) {
        final controls = ResearchSessionController(_transport!);
        _controls = controls;
        controls.addListener(_onControls);
        await controls.open();
        if (mounted) setState(() {});
      }
    });
  }

  void _onControls() {
    if (mounted) setState(() {});
  }

  Widget _sensorControls() {
    final controls = _controls!;
    final enabled = controls.ready && controls.supported && !controls.busy;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: TextButton.icon(
                onPressed: () => _openSensorPanel(controls),
                icon: const Icon(Icons.tune),
                label: Text('Sensors ${controls.activeMask == 0 ? '' : 'ON'}'),
              ),
            ),
            TextButton(
              onPressed:
                  enabled
                      ? () => controls.setTargets(
                        controls.availableTargets,
                        start: true,
                      )
                      : null,
              child: const Text('Start all'),
            ),
            TextButton(
              onPressed:
                  enabled
                      ? () => controls.setTargets(
                        ResearchTarget.values,
                        start: false,
                      )
                      : null,
              child: const Text('Stop all'),
            ),
          ],
        ),
        if (controls.error != null)
          SizedBox(
            height: 44,
            child: SingleChildScrollView(
              child: Text(
                controls.error!,
                style: HpiText.supporting.copyWith(color: HpiColors.error),
              ),
            ),
          ),
      ],
    );
  }

  void _openSensorPanel(ResearchSessionController controls) {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder:
          (context) => AnimatedBuilder(
            animation: controls,
            builder: (context, _) {
              final enabled =
                  controls.ready && controls.supported && !controls.busy;
              return SafeArea(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: SingleChildScrollView(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Text('Continuous sensors', style: HpiText.sectionLabel),
                        const SizedBox(height: 12),
                        for (final target in ResearchTarget.values)
                          Row(
                            children: [
                              Expanded(
                                child: Text(
                                  '${target.label}\n${controls.stateLabel(target)}',
                                  style: HpiText.supporting,
                                ),
                              ),
                              TextButton(
                                onPressed:
                                    enabled &&
                                            (!target.isImu ||
                                                controls.imuSupported) &&
                                            !controls.active(target)
                                        ? () => controls.setTargets([
                                          target,
                                        ], start: true)
                                        : null,
                                child: const Text('Start'),
                              ),
                              TextButton(
                                onPressed:
                                    enabled && controls.canStop(target)
                                        ? () => controls.setTargets([
                                          target,
                                        ], start: false)
                                        : null,
                                child: const Text('Stop'),
                              ),
                            ],
                          ),
                        const SizedBox(height: 12),
                        Text(
                          'Wrist PPGs: green, red and IR. Finger PPGs: red and IR. '
                          'Accelerometer and gyroscope each show X/Y/Z. '
                          'Sensors can run together. Contact is required for usable signals.',
                          style: HpiText.supporting,
                        ),
                        if (controls.error != null)
                          Text(
                            controls.error!,
                            style: HpiText.supporting.copyWith(
                              color: HpiColors.error,
                            ),
                          ),
                      ],
                    ),
                  ),
                ),
              );
            },
          ),
    );
  }

  Future<void> _connect() async {
    final device = _device;
    if (device == null) return;
    setState(() {
      _resolving = true;
      _connectionError = null;
    });
    try {
      await _cm.connect(device.macAddress, name: device.displayName);
    } catch (error) {
      if (mounted) setState(() => _connectionError = '$error');
    } finally {
      if (mounted) setState(() => _resolving = false);
    }
  }

  void _select(LiveSignal signal) {
    setState(() {
      if (_compare && signal == _secondary) _secondary = _primary;
      _primary = signal;
    });
    _restart();
  }

  @override
  Widget build(BuildContext context) {
    if (_resolving) return const Center(child: CircularProgressIndicator());
    if (_device == null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              'Pair a device to stream live signals',
              style: HpiText.appBarTitle,
            ),
            const SizedBox(height: 16),
            HpiFilledButton(
              label: 'Scan for devices',
              icon: Symbols.search,
              onPressed: () => Navigator.of(context).pushNamed('/scan'),
            ),
          ],
        ),
      );
    }
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('Live signals', style: HpiText.screenTitle),
                ),
                HpiPill(
                  label: _connected ? 'CONNECTED' : 'OFFLINE',
                  color: _connected ? HpiColors.steps : HpiColors.muted,
                ),
              ],
            ),
            const SizedBox(height: 10),
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: [
                  for (final signal in LiveSignal.values.where(
                    (s) => s.isPrimaryChoice,
                  ))
                    Padding(
                      padding: const EdgeInsets.only(right: 6),
                      child: ChoiceChip(
                        label: Text(signal.displayLabel),
                        selected: _primary == signal,
                        selectedColor: signal.color.withValues(alpha: 0.22),
                        onSelected: (_) => _select(signal),
                        labelStyle: TextStyle(
                          color:
                              _primary == signal
                                  ? signal.color
                                  : HpiColors.onSurfaceVariant,
                        ),
                      ),
                    ),
                ],
              ),
            ),
            Row(
              children: [
                Switch(
                  value: _compare,
                  onChanged: (value) {
                    setState(() {
                      _compare = value;
                      if (_secondary == _primary) {
                        _secondary = LiveSignal.values.firstWhere(
                          (s) => s.isPrimaryChoice && s != _primary,
                        );
                      }
                    });
                    _restart();
                  },
                ),
                const Text('Compare'),
                const SizedBox(width: 12),
                if (_compare)
                  Expanded(
                    child: DropdownButton<LiveSignal>(
                      value: _secondary,
                      isExpanded: true,
                      underline: const SizedBox.shrink(),
                      items: [
                        for (final signal in LiveSignal.values.where(
                          (s) => s.isPrimaryChoice && s != _primary,
                        ))
                          DropdownMenuItem(
                            value: signal,
                            child: Text(signal.displayLabel),
                          ),
                      ],
                      onChanged: (signal) {
                        if (signal == null) return;
                        setState(() => _secondary = signal);
                        _restart();
                      },
                    ),
                  )
                else
                  const Spacer(),
                if (_connected)
                  IconButton(
                    tooltip: 'Retry live streams',
                    onPressed: () => _restart(force: true),
                    icon: const Icon(Symbols.refresh),
                  ),
              ],
            ),
            if (!_connected)
              Expanded(child: _disconnected())
            else ...[
              if (_controls != null) _sensorControls(),
              Expanded(child: _waveformCard(_primary)),
              if (_compare) ...[
                const SizedBox(height: 10),
                Expanded(child: _waveformCard(_secondary)),
              ],
              const SizedBox(height: 10),
              Text(
                'PPG is shown in raw counts with an adaptive scale. '
                'Continuous mode runs until Stop. Keep this app foreground on Live; '
                'leaving Live or disconnecting requests Stop. Live view does not save recordings.',
                style: HpiText.supporting,
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _waveformCard(LiveSignal signal) {
    final buffer = _buffer(signal);
    final last = _lastSample[signal];
    final idle =
        last == null ||
        DateTime.now().difference(last) > const Duration(seconds: 3);
    final error =
        _cm.isSmpBusy
            ? 'Live view is paused while the watch transfers recordings or firmware.'
            : _feed?.errors[signal];
    final count = _feed?.sampleCounts[signal] ?? 0;
    final missing = _feed?.missingCounts[signal] ?? 0;
    final useResearch = _transport?.hasResearch == true;
    final target =
        signal.isImu
            ? (signal.isAccel ? ResearchTarget.accel : ResearchTarget.gyro)
            : signal == LiveSignal.ecg
            ? ResearchTarget.ecg
            : signal == LiveSignal.gsr
            ? ResearchTarget.gsr
            : signal.isFinger
            ? ResearchTarget.finger
            : ResearchTarget.wrist;
    final imuMissing = signal.isImu && _controls?.imuSupported != true;
    final waitingHint =
        imuMissing
            ? 'Install firmware 3.1.4-imu for accelerometer and gyro streaming.'
            : _controls == null
            ? signal.waitingHint
            : '${_controls!.stateLabel(target)}. Use Sensors to Start/Stop, '
                'and maintain contact. Waiting for recent samples.';
    return HpiCard(
      waveform: true,
      padding: const EdgeInsets.all(12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  signal.isPpg ? '${signal.label} PPG' : signal.displayLabel,
                  style: HpiText.sectionLabel.copyWith(color: signal.color),
                ),
              ),
              Text(
                '${signal.rate} SPS · 8 s',
                style: HpiText.mono.copyWith(fontSize: 10),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            '$count samples · ${useResearch ? '$missing missing after baseline' : 'loss count unavailable'}'
            '${signal.isImu || _latest[signal] == null ? '' : ' · latest ${_latest[signal]!.toStringAsFixed(0)}'}',
            style: HpiText.supporting,
          ),
          if (signal.isImu)
            Row(
              children: [
                for (final axis in signal.axes)
                  Expanded(
                    child: Text(
                      '${axis.label.split(' ').last}: ${_latest[axis] == null ? '—' : axis.displayValue(_latest[axis]!).toStringAsFixed(3)} ${axis.unit}',
                      style: HpiText.mono.copyWith(
                        color: axis.color,
                        fontSize: 10,
                      ),
                    ),
                  ),
              ],
            ),
          const SizedBox(height: 8),
          Expanded(
            child: Stack(
              children: [
                Positioned.fill(
                  child:
                      signal.isImu
                          ? HpiImuWaveform(
                            buffers: [
                              for (final axis in signal.axes) _buffer(axis),
                            ],
                            colors: [
                              for (final axis in signal.axes) axis.color,
                            ],
                            scale: signal.isAccel ? 1 / 4096 : 2000 / 32768,
                            minimumRange: signal.isAccel ? 1.25 : 5,
                            unit: signal.unit,
                          )
                          : HpiSweepWaveform(
                            buffer: buffer,
                            color: signal.color,
                          ),
                ),
                if (idle || error != null)
                  Positioned.fill(
                    child: Container(
                      color: HpiColors.waveformBg.withValues(alpha: 0.88),
                      alignment: Alignment.center,
                      padding: const EdgeInsets.all(12),
                      child: SingleChildScrollView(
                        child: Text(
                          error ?? waitingHint,
                          textAlign: TextAlign.center,
                          style: HpiText.body.copyWith(
                            fontSize: 12,
                            color:
                                error == null
                                    ? HpiColors.onSurfaceVariant
                                    : HpiColors.error,
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _disconnected() => Center(
    child: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Icon(Symbols.bluetooth, size: 40, color: HpiColors.muted),
        const SizedBox(height: 12),
        Text('Connect to ${_device!.displayName}', style: HpiText.appBarTitle),
        if (_connectionError != null)
          Padding(
            padding: const EdgeInsets.all(12),
            child: Text(
              _connectionError!,
              style: const TextStyle(color: HpiColors.error),
            ),
          ),
        const SizedBox(height: 16),
        HpiFilledButton(
          label: 'Connect',
          icon: Symbols.bluetooth,
          onPressed: _connect,
        ),
      ],
    ),
  );
}

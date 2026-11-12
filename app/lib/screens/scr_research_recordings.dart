import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import '../recording/recording_runtime.dart';
import '../recording/recording_store.dart';
import '../recording/recording_export.dart';

class ScrResearchRecordings extends StatefulWidget {
  const ScrResearchRecordings({super.key});
  @override
  State<ScrResearchRecordings> createState() => _State();
}

class _State extends State<ScrResearchRecordings> {
  final runtime = RecordingRuntime.instance;
  List<SavedRecording> sessions = [];
  String? error;
  bool loading = true, exporting = false;
  @override
  void initState() {
    super.initState();
    runtime.upload.addListener(_refresh);
    _refresh();
  }

  @override
  void dispose() {
    runtime.upload.removeListener(_refresh);
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      await runtime.initialize();
      final list = await runtime.store.list(runtime.account.owner);
      if (mounted) {
        setState(() {
          sessions = list;
          loading = false;
          error = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          error = '$e';
          loading = false;
        });
      }
    }
  }

  Future<void> _export(SavedRecording s) async {
    final yes = await showDialog<bool>(
      context: context,
      builder:
          (ctx) => AlertDialog(
            title: const Text('Export unencrypted files?'),
            content: const Text(
              'The CSV contains raw samples and timestamps. A JSON file includes the session summary, contact/status events and channel definitions. Shared copies are outside the app’s encryption and account privacy controls.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(ctx, true),
                child: const Text('Export'),
              ),
            ],
          ),
    );
    if (yes != true) return;
    setState(() => exporting = true);
    Directory? folder;
    try {
      folder = await Directory(
        '${(await getTemporaryDirectory()).path}/research_exports',
      ).create(recursive: true);
      final file = File('${folder.path}/${s.id}.csv');
      final meta = File('${folder.path}/${s.id}.json');
      await exportRecording(runtime.store, s, file);
      // Stream status events as well, without loading the session in memory.
      final sink = meta.openWrite();
      try {
        sink.write(
          '{"summary":${jsonEncode(s.metadata)},"channels":${jsonEncode(recordingChannels)},"status_events":[',
        );
        var first = true;
        for (var i = 0; i < s.chunks; i++) {
          final c = await runtime.store.readChunk(s.owner, s.id, i);
          for (final e in c['events'] as List) {
            if (e['type'] == 'status') {
              if (!first) sink.write(',');
              sink.write(jsonEncode(e));
              first = false;
            }
          }
          await sink.flush();
        }
        sink.write(']}');
      } finally {
        await sink.close();
      }
      if (!mounted) return;
      final box = context.findRenderObject() as RenderBox?;
      await SharePlus.instance.share(
        ShareParams(
          files: [XFile(file.path), XFile(meta.path)],
          sharePositionOrigin:
              box == null ? null : box.localToGlobal(Offset.zero) & box.size,
        ),
      );
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('Export failed: $e')));
      }
    } finally {
      // Only the two explicitly named temporary files; never delete recordings.
      if (folder != null) {
        for (final ext in ['csv', 'json']) {
          final f = File('${folder.path}/${s.id}.$ext');
          try {
            if (await f.exists()) await f.delete();
          } catch (_) {
            /* OS cache cleanup is the fallback. */
          }
        }
      }
      if (mounted) setState(() => exporting = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('Research recordings'),
      actions: [
        IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh)),
      ],
    ),
    body:
        loading
            ? const Center(child: CircularProgressIndicator())
            : RefreshIndicator(
              onRefresh: _refresh,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  Text(
                    runtime.account.label,
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  Text(
                    runtime.account.signedIn
                        ? 'Only this account’s research recordings are shown.'
                        : 'Local device profile: recordings remain on this phone. Sign in before starting a new hybrid session.',
                  ),
                  TextButton.icon(
                    onPressed:
                        runtime.recorder.locked || runtime.upload.busy
                            ? null
                            : () async {
                              await Navigator.push(
                                context,
                                MaterialPageRoute(
                                  builder: (_) => const ScrRecordingAccount(),
                                ),
                              );
                              await _refresh();
                            },
                    icon: const Icon(Icons.account_circle),
                    label: const Text('Account & AWS setup'),
                  ),
                  TextButton.icon(
                    onPressed:
                        runtime.upload.busy || !runtime.account.signedIn
                            ? null
                            : () => runtime.upload.run(retry: true),
                    icon: const Icon(Icons.cloud_upload),
                    label: Text(
                      runtime.upload.busy
                          ? 'Uploading…'
                          : 'Retry queued AWS uploads',
                    ),
                  ),
                  if (error != null)
                    Text(
                      error!,
                      style: const TextStyle(color: Colors.redAccent),
                    ),
                  if (runtime.upload.error != null)
                    Text(
                      runtime.upload.error!,
                      style: const TextStyle(color: Colors.orangeAccent),
                    ),
                  if (sessions.isEmpty)
                    const Padding(
                      padding: EdgeInsets.all(24),
                      child: Text(
                        'No recordings for this profile. Connect the watch, start sensors on Live, then tap Record.',
                      ),
                    ),
                  for (final s in sessions)
                    Card(
                      child: ExpansionTile(
                        title: Text(
                          DateTime.fromMillisecondsSinceEpoch(
                            s.startedMs,
                          ).toLocal().toString().split('.').first,
                        ),
                        subtitle: Text(
                          '${s.state} · ${s.chunks} chunks · ${s.mode}\nAWS: ${s.cloudState} (${s.uploaded}/${s.chunks})',
                        ),
                        children: [
                          Padding(
                            padding: const EdgeInsets.all(16),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  'ID: ${s.id}\nDuration: ${((s.metadata['duration_us'] as num? ?? 0) / 1000000).toStringAsFixed(1)} s\nPackets: ${s.metadata['packets'] ?? 0}\nSamples: ${s.metadata['samples'] ?? 0}',
                                ),
                                for (final e
                                    in (s.metadata['counts'] as Map? ?? {})
                                        .entries)
                                  Text(
                                    '${recordingChannels[e.key]?['name'] ?? e.key}: ${e.value} samples · ${(s.metadata['missing_after_baseline'] as Map? ?? {})[e.key] ?? 0} missing after baseline',
                                  ),
                                Text(
                                  'Duplicate/out-of-order packets: ${s.metadata['duplicates_or_out_of_order'] ?? 0}\nChannel boot changes: ${s.metadata['channel_boot_changes'] ?? 0}',
                                ),
                                if (s.metadata['error'] != null)
                                  Text(
                                    '${s.metadata['error']}',
                                    style: const TextStyle(
                                      color: Colors.redAccent,
                                    ),
                                  ),
                                if (s.state == 'interrupted')
                                  const Text(
                                    'Interrupted: collection ended unexpectedly. Any unsaved tail and disconnect interval are unavailable.',
                                  ),
                                const Text(
                                  'Timing: phone receive UTC + monotonic time. Watch timestamps are estimates. Local copies are retained after upload.',
                                ),
                                TextButton.icon(
                                  onPressed:
                                      exporting || !s.complete
                                          ? null
                                          : () => _export(s),
                                  icon: const Icon(Icons.ios_share),
                                  label: Text(
                                    exporting
                                        ? 'Exporting…'
                                        : 'Export CSV + summary / status JSON',
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
  );
}

const recordingChannels = <String, Map<String, Object>>{
  '1': {'name': 'ECG', 'nominal_hz': 128, 'unit': 'raw count'},
  '2': {'name': 'GSR', 'nominal_hz': 32, 'unit': 'raw ADC count'},
  '3': {'name': 'Wrist green PPG', 'nominal_hz': 25, 'unit': 'raw count'},
  '4': {'name': 'Finger IR PPG', 'nominal_hz': 100, 'unit': 'raw count'},
  '5': {'name': 'Heart rate', 'unit': 'bpm'},
  '6': {'name': 'Temperature', 'unit': 'C x 100'},
  '7': {'name': 'SpO2', 'unit': 'percent'},
  '8': {'name': 'Battery', 'unit': 'percent'},
  '9': {'name': 'Wrist red PPG', 'nominal_hz': 25, 'unit': 'raw count'},
  '10': {'name': 'Wrist IR PPG', 'nominal_hz': 25, 'unit': 'raw count'},
  '11': {'name': 'Finger red PPG', 'nominal_hz': 100, 'unit': 'raw count'},
  '12': {
    'name': 'Accel X',
    'nominal_hz': 25,
    'unit': 'count; divide by 4096 for g',
  },
  '13': {
    'name': 'Accel Y',
    'nominal_hz': 25,
    'unit': 'count; divide by 4096 for g',
  },
  '14': {
    'name': 'Accel Z',
    'nominal_hz': 25,
    'unit': 'count; divide by 4096 for g',
  },
  '15': {
    'name': 'Gyro X',
    'nominal_hz': 25,
    'unit': 'count; multiply by 2000/32768 for deg/s',
  },
  '16': {
    'name': 'Gyro Y',
    'nominal_hz': 25,
    'unit': 'count; multiply by 2000/32768 for deg/s',
  },
  '17': {
    'name': 'Gyro Z',
    'nominal_hz': 25,
    'unit': 'count; multiply by 2000/32768 for deg/s',
  },
};

class ScrRecordingAccount extends StatefulWidget {
  const ScrRecordingAccount({super.key});
  @override
  State<ScrRecordingAccount> createState() => _AccountState();
}

class _AccountState extends State<ScrRecordingAccount> {
  final runtime = RecordingRuntime.instance;
  final config = TextEditingController(),
      email = TextEditingController(),
      password = TextEditingController(),
      code = TextEditingController();
  String? message;
  bool busy = false;
  @override
  void initState() {
    super.initState();
    config.text =
        runtime.account.config == null
            ? ''
            : const JsonEncoder.withIndent(
              '  ',
            ).convert(runtime.account.config!.toJson());
  }

  @override
  void dispose() {
    config.dispose();
    email.dispose();
    password.dispose();
    code.dispose();
    super.dispose();
  }

  Future<void> _run(Future<void> Function() action, String success) async {
    if (runtime.recorder.locked || runtime.upload.busy) return;
    setState(() => busy = true);
    try {
      await action();
      password.clear();
      if (mounted) setState(() => message = success);
    } catch (e) {
      if (mounted) setState(() => message = '$e');
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Account & AWS')),
    body: ListView(
      padding: const EdgeInsets.all(20),
      children: [
        Text(
          runtime.account.config == null
              ? 'AWS setup pending deployment'
              : 'Sydney · ap-southeast-2',
        ),
        const Text(
          'Deploy the included mobile stack in AWS CloudShell, then paste its AppConfig output below. No AWS access keys or passwords are needed here. Local recording works without AWS.',
        ),
        TextField(
          controller: config,
          minLines: 3,
          maxLines: 8,
          decoration: const InputDecoration(labelText: 'AppConfig JSON'),
        ),
        FilledButton(
          onPressed:
              busy
                  ? null
                  : () => _run(
                    () => runtime.account.setConfig(config.text),
                    'Configuration saved. Sign in or create your own account.',
                  ),
          child: const Text('Save AWS configuration'),
        ),
        const Divider(),
        Text(runtime.account.label),
        if (runtime.account.signedIn)
          TextButton(
            onPressed:
                busy
                    ? null
                    : () => _run(
                      runtime.account.signOut,
                      'Signed out. Local device profile is active.',
                    ),
            child: const Text('Sign out / use local device profile'),
          ),
        TextField(
          controller: email,
          keyboardType: TextInputType.emailAddress,
          autocorrect: false,
          decoration: const InputDecoration(labelText: 'Your email'),
        ),
        TextField(
          controller: password,
          obscureText: true,
          autocorrect: false,
          enableSuggestions: false,
          decoration: const InputDecoration(
            labelText: 'Password (12+ characters)',
          ),
        ),
        TextField(
          controller: code,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(
            labelText: 'Email confirmation / reset code',
          ),
        ),
        Wrap(
          spacing: 8,
          children: [
            FilledButton(
              onPressed:
                  busy || runtime.account.config == null
                      ? null
                      : () => _run(
                        () => runtime.account.signIn(email.text, password.text),
                        'Signed in. New recordings belong to this account.',
                      ),
              child: const Text('Sign in'),
            ),
            TextButton(
              onPressed:
                  busy || runtime.account.config == null
                      ? null
                      : () => _run(
                        () => runtime.account.signUp(email.text, password.text),
                        'Check your email, then enter the code and tap Confirm email.',
                      ),
              child: const Text('Create account'),
            ),
            TextButton(
              onPressed:
                  busy
                      ? null
                      : () => _run(
                        () => runtime.account.confirm(email.text, code.text),
                        'Email confirmed. You can sign in.',
                      ),
              child: const Text('Confirm email'),
            ),
            TextButton(
              onPressed:
                  busy
                      ? null
                      : () => _run(
                        () => runtime.account.resetPassword(email.text),
                        'Reset code sent. Enter it and your new password, then tap Set new password.',
                      ),
              child: const Text('Forgot password'),
            ),
            TextButton(
              onPressed:
                  busy
                      ? null
                      : () => _run(
                        () => runtime.account.resetPassword(
                          email.text,
                          code: code.text,
                          password: password.text,
                        ),
                        'Password changed. Sign in with your new password.',
                      ),
              child: const Text('Set new password'),
            ),
          ],
        ),
        if (busy) const LinearProgressIndicator(),
        if (message != null) Text(message!),
        const Text(
          'Account changes require recording and uploads to be stopped. Each teammate signs in separately. Local device sessions stay on this phone; they are not automatically moved into another account. Existing Healthy Store history is separate from these account-scoped research recordings.',
        ),
      ],
    ),
  );
}

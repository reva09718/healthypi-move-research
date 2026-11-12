// Copyright (c) 2024-2026 ProtoCentral
// SPDX-License-Identifier: MIT

// This is a basic Flutter widget test.
//
// To perform an interaction with a widget in your test, use the WidgetTester
// utility in the flutter_test package. For example, you can send tap and scroll
// gestures. You can also use WidgetTester to find child widgets in the widget
// tree, read text, and verify that the values of widget properties are correct.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:move/main.dart';
import 'dart:ffi' show DynamicLibrary;
import 'dart:io';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:sqflite/sqflite.dart' as sqflite;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';
import 'package:sqlite3/open.dart';
import 'package:move/utils/database_helper.dart';
import 'package:move/utils/auto_sync_controller.dart';
import 'package:move/recording/recording_runtime.dart';

void main() {
  testWidgets('App smoke test', (WidgetTester tester) async {
    if (Platform.isWindows &&
        Platform.environment['SQLITE3_LIBRARY_PATH'] != null) {
      open.overrideFor(
        OperatingSystem.windows,
        () =>
            DynamicLibrary.open(Platform.environment['SQLITE3_LIBRARY_PATH']!),
      );
    }
    sqfliteFfiInit();
    sqflite.databaseFactory = databaseFactoryFfiNoIsolate;
    final dir =
        (await tester.runAsync(
          () => Directory.systemTemp.createTemp('healthypi-smoke-'),
        ))!;
    await tester.runAsync(
      () => sqflite.databaseFactory.setDatabasesPath(dir.path),
    );
    SharedPreferences.setMockInitialValues({'auto_sync_enabled': false});
    PackageInfo.setMockInitialValues(
      appName: 'Move',
      packageName: 'com.protocentral.move.research',
      version: '3.0.13',
      buildNumber: '101',
      buildSignature: 'test',
    );
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (_) async => dir.path,
    );
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('com.protocentral.move/research_recording'),
      (_) async => null,
    );
    // Open both databases in the real async zone before mounting widgets;
    // otherwise fake-async + the SQLite open lock can stall teardown.
    await tester.runAsync(() async {
      await DatabaseHelper.instance.database;
      await RecordingRuntime.instance.initialize();
    });
    // Build our app and trigger a frame.
    await tester.pumpWidget(const HealthyPiApp());

    // Verify that our app builds without error.
    expect(find.byType(MaterialApp), findsOneWidget);
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 200)),
    );
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpWidget(const SizedBox());
    AutoSyncController.instance.stop();
    RecordingRuntime.instance.upload.disposeWorker();
    await tester.runAsync(() async {
      await DatabaseHelper.instance.close();
      await RecordingRuntime.instance.store.close();
      await dir.delete(recursive: true);
    });
    await tester.pump();
    expect(tester.takeException(), isNull);
  });
}

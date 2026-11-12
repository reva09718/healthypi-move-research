import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'native_vault.dart';
import 'recording_store.dart';

class CloudConfig {
  CloudConfig(Map<String, dynamic> json)
    : pool = json['userPoolId'] as String,
      client = json['clientId'] as String,
      api = (json['apiBaseUrl'] as String).replaceFirst(RegExp(r'/$'), '') {
    final uri = Uri.parse(api);
    if (json['region'] != 'ap-southeast-1' ||
        !RegExp(r'^ap-southeast-1_[A-Za-z0-9]+$').hasMatch(pool) ||
        !RegExp(r'^[a-z0-9]{10,128}$').hasMatch(client) ||
        uri.scheme != 'https' ||
        !RegExp(
          r'^[a-z0-9]+\.execute-api\.ap-southeast-1\.amazonaws\.com$',
        ).hasMatch(uri.host) ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        uri.port != 443 ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw const FormatException(
        'Paste the Singapore mobile stack AppConfig output unchanged.',
      );
    }
  }
  final String pool, client, api;
  Map<String, String> toJson() => {
    'region': 'ap-southeast-1',
    'userPoolId': pool,
    'clientId': client,
    'apiBaseUrl': api,
  };
}

/// Public Cognito app client. No client secret, IAM key or password is stored.
class CloudAccount extends ChangeNotifier {
  CloudAccount(this.secrets, {http.Client? client})
    : client = client ?? http.Client();
  final SecretStore secrets;
  final http.Client client;
  CloudConfig? config;
  String? subject, email, _refresh, _access;
  int _expires = 0;
  bool get signedIn => subject != null;
  String get owner => ownerHash(
    subject == null ? offlineOwnerIdentity : '${config!.pool}|$subject',
  );
  String get label => email ?? 'Local device profile';
  Future<void> load() async {
    final c = await secrets.readSecret('cloud_config');
    if (c != null) config = CloudConfig(jsonDecode(c) as Map<String, dynamic>);
    final s = await secrets.readSecret('cloud_account');
    if (s != null && config != null) {
      final j = jsonDecode(s) as Map<String, dynamic>;
      if (j['pool'] == config!.pool && j['client'] == config!.client) {
        subject = j['sub'] as String;
        email = j['email'] as String;
        _refresh = j['refresh'] as String;
      }
    }
    notifyListeners();
  }

  Future<void> setConfig(String raw) async {
    final next = CloudConfig(jsonDecode(raw) as Map<String, dynamic>);
    await signOut();
    await secrets.writeSecret('cloud_config', jsonEncode(next.toJson()));
    config = next;
    notifyListeners();
  }

  Future<Map<String, dynamic>> _call(
    String operation,
    Map<String, dynamic> body,
  ) async {
    if (config == null) {
      throw StateError(
        'Deploy the AWS mobile stack and configure the app first.',
      );
    }
    final response = await client
        .post(
          Uri.https('cognito-idp.ap-southeast-1.amazonaws.com', '/'),
          headers: {
            'Content-Type': 'application/x-amz-json-1.1',
            'X-Amz-Target': 'AWSCognitoIdentityProviderService.$operation',
          },
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 25));
    final j = jsonDecode(response.body) as Map<String, dynamic>;
    if (response.statusCode != 200) {
      throw StateError(
        (j['message'] ?? 'Account request failed (${response.statusCode})')
            .toString(),
      );
    }
    return j;
  }

  Future<void> signUp(String address, String password) async {
    await _call('SignUp', {
      'ClientId': config?.client,
      'Username': address.trim().toLowerCase(),
      'Password': password,
      'UserAttributes': [
        {'Name': 'email', 'Value': address.trim().toLowerCase()},
      ],
    });
  }

  Future<void> confirm(String address, String code) async {
    await _call('ConfirmSignUp', {
      'ClientId': config?.client,
      'Username': address.trim().toLowerCase(),
      'ConfirmationCode': code.trim(),
    });
  }

  Future<void> resetPassword(
    String address, {
    String? code,
    String? password,
  }) async {
    await _call(code == null ? 'ForgotPassword' : 'ConfirmForgotPassword', {
      'ClientId': config?.client,
      'Username': address.trim().toLowerCase(),
      if (code != null) 'ConfirmationCode': code.trim(),
      if (password != null) 'Password': password,
    });
  }

  Future<void> signIn(String address, String password) async {
    final j = await _call('InitiateAuth', {
      'ClientId': config?.client,
      'AuthFlow': 'USER_PASSWORD_AUTH',
      'AuthParameters': {
        'USERNAME': address.trim().toLowerCase(),
        'PASSWORD': password,
      },
    });
    if (j['AuthenticationResult'] == null) {
      throw StateError(
        'This account requires an unsupported authentication challenge.',
      );
    }
    final auth = j['AuthenticationResult'] as Map<String, dynamic>;
    final access = auth['AccessToken'] as String;
    final user = await _call('GetUser', {'AccessToken': access});
    final attrs = {
      for (final a in user['UserAttributes'] as List) a['Name']: a['Value'],
    };
    final nextSubject = attrs['sub'] as String;
    final nextEmail = attrs['email'] as String;
    final refresh = auth['RefreshToken'] as String;
    await secrets.writeSecret(
      'cloud_account',
      jsonEncode({
        'pool': config!.pool,
        'client': config!.client,
        'sub': nextSubject,
        'email': nextEmail,
        'refresh': refresh,
      }),
    );
    subject = nextSubject;
    email = nextEmail;
    _refresh = refresh;
    _accept(auth);
    notifyListeners();
  }

  void _accept(Map<String, dynamic> auth) {
    _access = auth['AccessToken'] as String;
    _expires =
        DateTime.now().millisecondsSinceEpoch +
        (auth['ExpiresIn'] as int) * 1000 -
        60000;
  }

  Future<String> accessToken() async {
    if (!signedIn || _refresh == null) {
      throw StateError('Sign in to upload this account’s recordings.');
    }
    if (_access != null && DateTime.now().millisecondsSinceEpoch < _expires) {
      return _access!;
    }
    final j = await _call('InitiateAuth', {
      'ClientId': config!.client,
      'AuthFlow': 'REFRESH_TOKEN_AUTH',
      'AuthParameters': {'REFRESH_TOKEN': _refresh},
    });
    final auth = j['AuthenticationResult'] as Map<String, dynamic>;
    final user = await _call('GetUser', {'AccessToken': auth['AccessToken']});
    final sub =
        (user['UserAttributes'] as List).firstWhere(
          (a) => a['Name'] == 'sub',
        )['Value'];
    if (sub != subject) {
      throw StateError('Account identity changed. Sign in again.');
    }
    _accept(auth);
    return _access!;
  }

  Future<void> signOut() async {
    await secrets.writeSecret('cloud_account', null);
    subject = null;
    email = null;
    _refresh = null;
    _access = null;
    _expires = 0;
    notifyListeners();
  }
}

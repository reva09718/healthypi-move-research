package com.protocentral.move

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.security.KeyStore
import java.util.concurrent.Executors
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

class MainActivity : FlutterActivity() {
    companion object { var engine: FlutterEngine? = null; var channel: MethodChannel? = null }
    private val worker = Executors.newSingleThreadExecutor()
    override fun provideFlutterEngine(context: Context): FlutterEngine? = engine
    override fun shouldDestroyEngineWithHost(): Boolean = false
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        engine = flutterEngine
        val app = applicationContext
        channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger,
            "com.protocentral.move/research_recording").also { ch ->
            ch.setMethodCallHandler { call, result ->
                if (call.method == "startRecordingService" || call.method == "stopRecordingService") {
                    try {
                        val intent = Intent(app, ResearchRecordingService::class.java)
                        if (call.method == "stopRecordingService") app.stopService(intent)
                        else {
                            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                                requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 101)
                            }
                            if (Build.VERSION.SDK_INT >= 26) app.startForegroundService(intent)
                            else app.startService(intent)
                        }
                        result.success(null)
                    } catch (e: Exception) { result.error("service", e.message, null) }
                } else worker.execute {
                    try {
                        val prefs = app.getSharedPreferences("research_vault", Context.MODE_PRIVATE)
                        val answer: Any? = when (call.method) {
                            "encrypt", "decrypt" -> {
                                val owner = call.argument<String>("owner")!!
                                require(owner.matches(Regex("[a-f0-9]{64}")))
                                val aad = call.argument<String>("aad")!!
                                val data = call.argument<ByteArray>("data")!!
                                if (call.method == "encrypt") GcmVault.seal(key("record-$owner", true), aad, data)
                                else GcmVault.open(key("record-$owner", false), aad, data)
                            }
                            "readSecret" -> {
                                val name = call.argument<String>("key")!!
                                prefs.getString(name, null)?.let {
                                    String(GcmVault.open(key("auth", false), name, Base64.decode(it, Base64.NO_WRAP)), Charsets.UTF_8)
                                }
                            }
                            "writeSecret" -> {
                                val name = call.argument<String>("key")!!
                                val value = call.argument<String>("value")
                                val edit = prefs.edit()
                                if (value == null) edit.remove(name) else edit.putString(name,
                                    Base64.encodeToString(GcmVault.seal(key("auth", true), name, value.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP))
                                check(edit.commit()) { "Could not save secure account settings" }
                                null
                            }
                            else -> throw IllegalArgumentException("Unknown vault operation")
                        }
                        Handler(Looper.getMainLooper()).post { result.success(answer) }
                    } catch (e: Exception) {
                        Handler(Looper.getMainLooper()).post { result.error("vault", "Secure storage failed: ${e.javaClass.simpleName}", null) }
                    }
                }
            }
        }
    }
    private fun key(name: String, create: Boolean): SecretKey {
        val alias = "healthypi-research-v1-$name"
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        check(create) { "Encryption key unavailable; do not overwrite existing recordings" }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).setRandomizedEncryptionRequired(true).build())
            generateKey()
        }
    }
}

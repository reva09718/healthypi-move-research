package com.protocentral.move
import org.junit.Assert.*
import org.junit.Test
import javax.crypto.AEADBadTagException
import javax.crypto.spec.SecretKeySpec

class GcmVaultTest {
    private val key = SecretKeySpec(ByteArray(32) { it.toByte() }, "AES")
    @Test fun roundtripAndUniqueNonce() {
        val data = "raw-private-signal".toByteArray()
        val a = GcmVault.seal(key, "owner-a|session|0", data)
        val b = GcmVault.seal(key, "owner-a|session|0", data)
        assertFalse(a.contentEquals(b))
        assertArrayEquals(data, GcmVault.open(key, "owner-a|session|0", a))
    }
    @Test(expected = AEADBadTagException::class) fun ownerOrSequenceChangeRejected() {
        GcmVault.open(key, "owner-b|session|0", GcmVault.seal(key, "owner-a|session|0", byteArrayOf(1,2)))
    }
    @Test(expected = AEADBadTagException::class) fun tamperingRejected() {
        val data = GcmVault.seal(key, "aad", byteArrayOf(1,2))
        data[data.lastIndex] = (data.last().toInt() xor 1).toByte()
        GcmVault.open(key, "aad", data)
    }
}

package org.nxrs.monitor

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import java.io.File
import java.net.URI
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import org.nxrs.monitor.core.*

class Enrollment(val gateway: URI, val media: URI?, val credential: Credential) {
    val policy = EndpointPolicy(gateway, listOfNotNull(media))

    override fun toString() = "<enrollment: redacted>"
}

/**
 * Non-exportable, device-local key; authenticated ciphertext only is persisted. The application
 * disables backup and never stores credentials in preferences.
 */
class EnrollmentStore(context: Context, private val name: String = "home") {
    private val file = AtomicFile(File(context.noBackupFilesDir, "$name.enrollment"))
    private val alias = "org.nxrs.monitor.enrollment.$name"

    @Serializable private class Stored(val gateway: String, val media: String?, val token: String)

    private fun key(create: Boolean): SecretKey? {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let {
            return it
        }
        if (!create) return null
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                        alias,
                        KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                    )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setUnlockedDeviceRequired(true)
                    .build()
            )
            generateKey()
        }
    }

    fun save(value: Enrollment) {
        val bytes =
            WireJson.json
                .encodeToString(
                    Stored(
                        value.gateway.toASCIIString(),
                        value.media?.toASCIIString(),
                        value.credential.encodedForStorage(),
                    )
                )
                .encodeToByteArray()
        checked(bytes.size <= 16384, Failure.TooLarge)
        val cipher =
            Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key(true)) }
        val sealed = byteArrayOf(1) + cipher.iv + cipher.doFinal(bytes)
        val stream = file.startWrite()
        try {
            stream.write(sealed)
            file.finishWrite(stream)
        } catch (error: Exception) {
            file.failWrite(stream)
            throw error
        } finally {
            bytes.fill(0)
        }
    }

    fun read(): Enrollment? {
        if (!file.baseFile.exists()) return null
        checked(file.baseFile.length() <= 16432, Failure.TooLarge)
        val sealed = file.readFully()
        checked(sealed.size in 30..16432 && sealed[0] == 1.toByte())
        val key = key(false) ?: throw ClientException(Failure.InvalidCredential)
        val cipher =
            Cipher.getInstance("AES/GCM/NoPadding").apply {
                init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, sealed.copyOfRange(1, 13)))
            }
        val plaintext =
            try {
                cipher.doFinal(sealed.copyOfRange(13, sealed.size))
            } catch (_: Exception) {
                throw ClientException(Failure.InvalidCredential)
            }
        try {
            val stored = WireJson.decode<Stored>(plaintext, 16384)
            return Enrollment(
                EndpointPolicy.parse(stored.gateway),
                stored.media?.let(EndpointPolicy::parse),
                Credential.parse(stored.token),
            )
        } finally {
            plaintext.fill(0)
        }
    }

    fun forget() {
        file.delete()
        KeyStore.getInstance("AndroidKeyStore").apply {
            load(null)
            if (containsAlias(alias)) deleteEntry(alias)
        }
    }
}

package org.nxrs.monitor

import android.content.Context
import android.util.AtomicFile
import java.io.File
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import org.nxrs.monitor.core.*

@Serializable
data class CaptureItem(
    val id: String,
    val camera: CameraId,
    val name: String,
    val created: Long,
    val durationMs: Long,
    val video: Boolean,
) {
    val filename
        get() = id + if (video) ".mp4" else ".jpg"
}

/**
 * Private originals, atomic bounded index, explicit deletion and sharing. Reservations count
 * against quota before either camera begins recording.
 */
class CaptureLibrary(context: Context) {
    private val root = File(context.filesDir, "captures")
    private val index = AtomicFile(File(root, "index.json"))
    private val lock = Mutex()
    private var loaded = false
    private var items = emptyList<CaptureItem>()
    private val reservations = mutableSetOf<String>()

    private suspend fun <T> access(action: () -> T): T =
        withContext(Dispatchers.IO + NonCancellable) {
            lock.withLock {
                if (!loaded) {
                    root.mkdirs()
                    if (index.baseFile.exists()) {
                        checked(index.baseFile.length() <= 1048576, Failure.TooLarge)
                        items = WireJson.decode<List<CaptureItem>>(index.readFully(), 1048576)
                        checked(
                            items.size <= 1000 &&
                                items.map { it.id }.distinct().size == items.size &&
                                items.all {
                                    it.id.matches(Regex("[0-9a-f-]{36}")) &&
                                        it.name.length <= 256 &&
                                        it.created >= 0 &&
                                        it.durationMs in 0..60000
                                }
                        )
                        items = items.filter { File(root, it.filename).isFile }
                    }
                    // Only our staging files are disposable after process death.
                    root
                        .listFiles()
                        ?.filter { it.name.endsWith(".pending") }
                        ?.forEach { it.delete() }
                    loaded = true
                }
                action()
            }
        }

    private fun persist() {
        val stream = index.startWrite()
        try {
            stream.write(WireJson.json.encodeToString(items).encodeToByteArray())
            index.finishWrite(stream)
        } catch (error: Exception) {
            index.failWrite(stream)
            throw error
        }
    }

    private fun capacity(bytes: Long) {
        val used = root.listFiles()?.sumOf { it.length() } ?: 0
        checked(
            items.size + reservations.size < 1000 &&
                used + reservations.size * 32L * 1024 * 1024 + bytes <= 512L * 1024 * 1024,
            Failure.TooLarge,
        )
    }

    suspend fun list(): List<CaptureItem> = access { items.sortedByDescending { it.created } }

    suspend fun photo(bytes: ByteArray, camera: Camera): CaptureItem = access {
        checked(bytes.size <= 16 * 1024 * 1024, Failure.TooLarge)
        capacity(bytes.size.toLong())
        val item =
            CaptureItem(
                UUID.randomUUID().toString(),
                camera.id,
                camera.name,
                System.currentTimeMillis(),
                0,
                false,
            )
        val destination = AtomicFile(File(root, item.filename))
        val stream = destination.startWrite()
        try {
            stream.write(bytes)
            destination.finishWrite(stream)
        } catch (error: Exception) {
            destination.failWrite(stream)
            throw error
        }
        val old = items
        items = items + item
        try {
            persist()
            item
        } catch (error: Exception) {
            items = old
            destination.delete()
            throw error
        }
    }

    suspend fun reserve(): File = access {
        capacity(32L * 1024 * 1024)
        val id = UUID.randomUUID().toString()
        reservations.add(id)
        File(root, "$id.pending")
    }

    suspend fun discard(file: File) = access {
        checked(file.parentFile == root && file.name.endsWith(".pending"))
        reservations.remove(file.name.removeSuffix(".pending"))
        file.delete()
        Unit
    }

    suspend fun finish(file: File, duration: Long, camera: Camera): CaptureItem = access {
        checked(
            file.parentFile == root &&
                reservations.contains(file.name.removeSuffix(".pending")) &&
                file.isFile &&
                file.length() in 1..(32L * 1024 * 1024) &&
                duration in 1..60000
        )
        val item =
            CaptureItem(
                file.name.removeSuffix(".pending"),
                camera.id,
                camera.name,
                System.currentTimeMillis(),
                duration,
                true,
            )
        val target = File(root, item.filename)
        checked(file.renameTo(target))
        val old = items
        items = items + item
        try {
            persist()
            reservations.remove(item.id)
            item
        } catch (error: Exception) {
            items = old
            target.delete()
            reservations.remove(item.id)
            throw error
        }
    }

    suspend fun delete(item: CaptureItem) = access {
        checked(item in items)
        checked(File(root, item.filename).delete())
        items = items - item
        persist()
    }

    suspend fun file(item: CaptureItem): File = access {
        checked(item in items)
        File(root, item.filename).also { checked(it.isFile) }
    }
}

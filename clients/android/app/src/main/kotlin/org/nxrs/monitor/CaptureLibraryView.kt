package org.nxrs.monitor

import android.content.Intent
import android.graphics.BitmapFactory
import android.widget.MediaController
import android.widget.VideoView
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.FileProvider
import java.io.File
import java.text.DateFormat
import java.util.Date
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun CaptureLibraryView(library: CaptureLibrary, dismiss: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf(emptyList<CaptureItem>()) }
    var error by remember { mutableStateOf<String?>(null) }
    var preview by remember { mutableStateOf<Pair<CaptureItem, File>?>(null) }
    suspend fun reload() {
        try {
            items = library.list()
        } catch (_: Exception) {
            error = "Saved footage could not be opened."
        }
    }
    LaunchedEffect(library) { reload() }
    Dialog(
        onDismissRequest = dismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(Modifier.fillMaxSize().safeDrawingPadding()) {
            Column(Modifier.padding(16.dp)) {
                Row(Modifier.fillMaxWidth()) {
                    Text(
                        "Saved footage",
                        style = MaterialTheme.typography.headlineSmall,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = dismiss) { Text("Done") }
                }
                Text(
                    "Private on this device. Up to 512 MB; delete footage to make room. Sharing sends only the selected original.",
                    modifier = Modifier.padding(vertical = 12.dp),
                )
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                if (items.isEmpty())
                    Text("Save a snapshot or record a clip while watching a camera.")
                LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(items, key = { it.id }) { item ->
                        Card(Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(12.dp)) {
                                Text(item.name, style = MaterialTheme.typography.titleMedium)
                                Text(
                                    "${if (item.video) "Clip · ${item.durationMs / 1000}s" else "Snapshot"} · ${DateFormat.getDateTimeInstance().format(Date(item.created))}"
                                )
                                Row {
                                    TextButton(
                                        onClick = {
                                            scope.launch {
                                                try {
                                                    preview = item to library.file(item)
                                                } catch (_: Exception) {
                                                    error = "This file could not be opened."
                                                }
                                            }
                                        }
                                    ) {
                                        Text("View")
                                    }
                                    TextButton(
                                        onClick = {
                                            scope.launch {
                                                try {
                                                    val uri =
                                                        FileProvider.getUriForFile(
                                                            context,
                                                            "${context.packageName}.captures",
                                                            library.file(item),
                                                        )
                                                    val intent =
                                                        Intent(Intent.ACTION_SEND)
                                                            .setType(
                                                                if (item.video) "video/mp4"
                                                                else "image/jpeg"
                                                            )
                                                            .putExtra(Intent.EXTRA_STREAM, uri)
                                                            .addFlags(
                                                                Intent
                                                                    .FLAG_GRANT_READ_URI_PERMISSION
                                                            )
                                                    context.startActivity(
                                                        Intent.createChooser(
                                                            intent,
                                                            "Share selected footage",
                                                        )
                                                    )
                                                } catch (_: Exception) {
                                                    error = "This file could not be shared."
                                                }
                                            }
                                        }
                                    ) {
                                        Text("Share")
                                    }
                                    TextButton(
                                        onClick = {
                                            scope.launch {
                                                try {
                                                    library.delete(item)
                                                    reload()
                                                } catch (_: Exception) {
                                                    error = "This file could not be deleted."
                                                }
                                            }
                                        }
                                    ) {
                                        Text("Delete")
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    preview?.let { (item, file) ->
        Dialog(onDismissRequest = { preview = null }) {
            Surface {
                Column(Modifier.padding(12.dp)) {
                    Text(item.name)
                    if (item.video)
                        AndroidView(
                            factory = { native ->
                                VideoView(native).apply {
                                    setVideoPath(file.absolutePath)
                                    setMediaController(MediaController(native))
                                    setOnPreparedListener {
                                        it.isLooping = false
                                        start()
                                    }
                                }
                            },
                            modifier = Modifier.fillMaxWidth().aspectRatio(4f / 3f),
                            onRelease = { it.stopPlayback() },
                        )
                    else {
                        val bitmap by
                            produceState<android.graphics.Bitmap?>(null, file) {
                                value = null
                                value =
                                    withContext(Dispatchers.IO) {
                                        BitmapFactory.decodeFile(file.absolutePath)
                                    }
                            }
                        bitmap?.let {
                            Image(
                                it.asImageBitmap(),
                                "Saved snapshot",
                                Modifier.fillMaxWidth().aspectRatio(4f / 3f),
                            )
                        }
                    }
                    TextButton(onClick = { preview = null }) { Text("Close") }
                }
            }
        }
    }
}

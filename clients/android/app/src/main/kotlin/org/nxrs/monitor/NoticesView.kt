package org.nxrs.monitor

import android.widget.ScrollView
import android.widget.TextView
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

@Composable
fun NoticesView(dismiss: () -> Unit) {
    val context = LocalContext.current
    val notices by
        produceState("") {
            value =
                withContext(Dispatchers.IO) {
                    listOf("NOTICE.txt", "Apache-2.0.txt", "WebRTC.txt").joinToString("\n\n") { name
                        ->
                        context.assets.open("licenses/$name").bufferedReader().use { it.readText() }
                    }
                }
        }
    Dialog(
        onDismissRequest = dismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(Modifier.fillMaxSize().safeDrawingPadding()) {
            Column(Modifier.padding(16.dp)) {
                Row {
                    Text(
                        "Open source notices",
                        style = MaterialTheme.typography.titleLarge,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = dismiss) { Text("Done") }
                }
                AndroidView(
                    factory = { native ->
                        ScrollView(native).apply {
                            addView(
                                TextView(native).apply {
                                    setTextColor(android.graphics.Color.WHITE)
                                    textSize = 13f
                                    setTextIsSelectable(true)
                                }
                            )
                        }
                    },
                    update = { (it.getChildAt(0) as TextView).text = notices },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
    }
}

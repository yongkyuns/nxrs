package org.nxrs.monitor

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.LocalActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.flow.distinctUntilChanged
import org.nxrs.monitor.core.*
import org.nxrs.monitor.media.AnnotatedVideoView
import org.nxrs.monitor.media.NativePeer
import org.nxrs.monitor.media.RenderedSample

class MainActivity : ComponentActivity() {
    private val model by viewModels<HomeModel>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle =
                androidx.activity.SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle =
                androidx.activity.SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
        // Invitations must never appear in recents or screen captures.
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        setContent { MonitorTheme { MonitorHome(model) } }
    }

    override fun onStart() {
        super.onStart()
        model.foreground(true)
    }

    override fun onStop() {
        if (!isChangingConfigurations) model.foreground(false)
        super.onStop()
    }
}

@Composable
fun MonitorTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme =
            darkColorScheme(
                primary = Color(0xff78dfbd),
                onPrimary = Color(0xff0b2f2a),
                secondary = Color(0xff9dafff),
                background = Color(0xff0b1519),
                surface = Color(0xff142328),
                onSurface = Color(0xffeff7f4),
                surfaceVariant = Color(0xff22343b),
                onSurfaceVariant = Color(0xffb6cac3),
            ),
        content = content,
    )
}

@Composable
private fun MonitorHome(model: HomeModel) {
    val home by model.state.collectAsStateWithLifecycle()
    val activity = LocalActivity.current
    DisposableEffect(home.enrolled, activity) {
        if (home.enrolled) activity?.window?.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        else activity?.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        onDispose { activity?.window?.clearFlags(WindowManager.LayoutParams.FLAG_SECURE) }
    }
    var showNotices by remember { mutableStateOf(false) }
    var showSettings by remember { mutableStateOf(false) }
    var showLibrary by remember { mutableStateOf(false) }
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().safeDrawingPadding().padding(horizontal = 16.dp)) {
            if (home.fullscreen == null)
                Row(
                    Modifier.fillMaxWidth().padding(vertical = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "Nxrs Home",
                        style = MaterialTheme.typography.titleLarge,
                        modifier = Modifier.weight(1f),
                    )
                    if (home.enrolled)
                        IconButton(onClick = model::refresh, enabled = !home.loading) {
                            Icon(
                                painterResource(R.drawable.ic_refresh),
                                contentDescription = "Refresh cameras",
                            )
                        }
                    else TextButton(onClick = { showNotices = true }) { Text("About") }
                    TextButton(onClick = { showLibrary = true }) { Text("Saved") }
                    if (home.enrolled)
                        TextButton(onClick = { showSettings = true }) { Text("Settings") }
                }
            if (!home.enrolled) InvitationForm(home.error, model::enroll)
            else {
                home.error?.let {
                    Text(
                        it,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(bottom = 12.dp),
                    )
                }
                if (home.cameras.isEmpty())
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Text(
                            if (home.loading) "Connecting to your home…"
                            else "No cameras are shared with this viewer key."
                        )
                    }
                else CameraGrid(home, model::visible, model::toggle, model::fullscreen)
            }
        }
    }
    if (showNotices) NoticesView { showNotices = false }
    if (showLibrary) CaptureLibraryView(model.library) { showLibrary = false }
    if (showSettings)
        AlertDialog(
            onDismissRequest = { showSettings = false },
            title = { Text("Home invitation") },
            text = {
                Column {
                    Text(
                        "Use your private network when away. Forgetting this home removes its viewer key from this device."
                    )
                    TextButton(
                        onClick = {
                            showSettings = false
                            showNotices = true
                        }
                    ) {
                        Text("About")
                    }
                }
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        showSettings = false
                        model.forget()
                    }
                ) {
                    Text("Forget home")
                }
            },
            dismissButton = { TextButton(onClick = { showSettings = false }) { Text("Done") } },
        )
    BackHandler(home.fullscreen != null) { model.fullscreen(null) }
}

@Composable
private fun InvitationForm(error: String?, enroll: (String, String, String) -> Unit) {
    var gateway by remember { mutableStateOf("") }
    var media by remember { mutableStateOf("") }
    var token by remember { mutableStateOf("") }
    Column(
        Modifier.verticalScroll(rememberScrollState()).padding(vertical = 24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Welcome home", style = MaterialTheme.typography.headlineLarge)
        Text(
            "Enter the invitation from your home owner. Your phone must be able to reach the gateway over HTTPS."
        )
        OutlinedTextField(
            gateway,
            { gateway = it.take(2048) },
            label = { Text("Home HTTPS address") },
            singleLine = true,
            keyboardOptions =
                KeyboardOptions(keyboardType = KeyboardType.Uri, autoCorrectEnabled = false),
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            media,
            { media = it.take(2048) },
            label = { Text("Video origin (if different)") },
            singleLine = true,
            keyboardOptions =
                KeyboardOptions(keyboardType = KeyboardType.Uri, autoCorrectEnabled = false),
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            token,
            { token = it.take(8192) },
            label = { Text("Viewer key") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions =
                KeyboardOptions(keyboardType = KeyboardType.Password, autoCorrectEnabled = false),
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(
            onClick = {
                enroll(gateway, media, token)
                token = ""
            },
            enabled = gateway.isNotBlank() && token.isNotBlank(),
        ) {
            Text("Connect home")
        }
        Text(
            "The viewer key is encrypted with a device-local Android Keystore key. It is excluded from backup.",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

@Composable
fun CameraGrid(
    home: HomePresentation,
    visible: (Set<CameraId>) -> Unit,
    toggle: (CameraId) -> Unit,
    fullscreen: (CameraId?) -> Unit,
    witness: ((CameraId, org.nxrs.monitor.media.RenderedSample) -> Unit)? = null,
) {
    val grid = rememberLazyGridState()
    if (home.fullscreen != null) {
        home.sessions[home.fullscreen]?.let { session ->
            FullscreenCamera(session, { fullscreen(null) }, witness)
        }
    } else {
        LaunchedEffect(grid, home.cameras) {
            snapshotFlow {
                    grid.layoutInfo.visibleItemsInfo
                        .mapNotNull { home.cameras.getOrNull(it.index)?.id }
                        .toSet()
                }
                .distinctUntilChanged()
                .collect(visible)
        }
        LazyVerticalGrid(
            columns =
                if (LocalDensity.current.fontScale >= 1.5f) GridCells.Fixed(1)
                else GridCells.Adaptive(300.dp),
            state = grid,
            verticalArrangement = Arrangement.spacedBy(12.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            contentPadding = PaddingValues(bottom = 24.dp),
        ) {
            items(home.cameras, key = { it.id.value }) { camera ->
                val session = home.sessions[camera.id]
                if (session != null)
                    CameraCard(
                        session,
                        camera.id in home.paused,
                        { toggle(camera.id) },
                        { fullscreen(camera.id) },
                        Modifier.fillMaxWidth(),
                        home.cameras.size == 1,
                        witness,
                    )
            }
        }
    }
}

@Composable
private fun CameraCard(
    session: CameraSession,
    paused: Boolean,
    toggle: () -> Unit,
    expand: () -> Unit,
    modifier: Modifier,
    singleCamera: Boolean,
    witness: ((CameraId, org.nxrs.monitor.media.RenderedSample) -> Unit)?,
) {
    val state by session.state.collectAsStateWithLifecycle()
    Card(
        modifier,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    session.camera.name,
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f),
                )
                Text(
                    state.status,
                    color =
                        if (state.live) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Box(
                Modifier.fillMaxWidth()
                    .aspectRatio(if (singleCamera) 4f / 3f else 16f / 9f)
                    .background(Color.Black),
                contentAlignment = Alignment.Center,
            ) {
                val peer = state.peer
                if (peer != null) CameraVideo(session.camera.id, peer, witness)
                else
                    Text(
                        if (paused) "Paused"
                        else if (session.retiring) "Finishing connection…" else "Ready to watch"
                    )
            }
            Text(
                when (state.presence) {
                    Presence.Person -> "Person detected"
                    Presence.Clear -> "No person detected"
                    Presence.Unavailable -> "Detection unavailable"
                },
                color =
                    if (state.presence == Presence.Person) Color(0xffffb260)
                    else MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (state.historyIncomplete)
                Text("Detection history is incomplete", style = MaterialTheme.typography.bodySmall)
            state.error?.let {
                Text(
                    it,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            state.captureMessage?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            if (singleCamera && state.live || state.recording)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(
                        onClick = session::snapshot,
                        enabled = state.live && !state.captureBusy,
                    ) {
                        Text("Snapshot")
                    }
                    TextButton(
                        onClick = session::toggleRecording,
                        enabled = state.live || state.recording,
                    ) {
                        Text(if (state.recording) "Stop clip" else "Record")
                    }
                }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                val action =
                    if (session.active && !paused) "Pause"
                    else if (session.blocked) "Retry" else "Watch"
                Button(
                    onClick = toggle,
                    enabled = !session.retiring,
                    modifier =
                        Modifier.testTag("camera-toggle-" + session.camera.id.value).semantics {
                            contentDescription = "$action ${session.camera.name}"
                        },
                ) {
                    Text(action)
                }
                OutlinedButton(onClick = expand) { Text("Expand") }
            }
        }
    }
}

@Composable
private fun CameraVideo(
    camera: CameraId,
    peer: NativePeer,
    witness: ((CameraId, RenderedSample) -> Unit)?,
) {
    val observer: ((RenderedSample) -> Unit)? =
        witness?.let { callback -> { sample -> callback(camera, sample) } }
    key(peer.identity) {
        AndroidView(
            factory = { AnnotatedVideoView(it, peer, observer) },
            modifier = Modifier.fillMaxSize(),
            update = { it.observe(observer) },
            onRelease = { it.close() },
        )
    }
}

@Composable
private fun FullscreenCamera(
    session: CameraSession,
    close: () -> Unit,
    witness: ((CameraId, org.nxrs.monitor.media.RenderedSample) -> Unit)?,
) {
    val state by session.state.collectAsStateWithLifecycle()
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        val peer = state.peer
        if (peer != null) CameraVideo(session.camera.id, peer, witness)
        Row(
            Modifier.fillMaxWidth().background(Color.Black.copy(alpha = 0.6f)).padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(session.camera.name)
                Text(state.status, style = MaterialTheme.typography.bodySmall)
            }
            TextButton(onClick = close) { Text("Back to cameras") }
        }
        Row(
            Modifier.align(Alignment.BottomCenter)
                .fillMaxWidth()
                .background(Color.Black.copy(alpha = 0.6f))
                .padding(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                when (state.presence) {
                    Presence.Person -> "Person detected"
                    Presence.Clear -> "No person detected"
                    Presence.Unavailable -> "Detection unavailable"
                },
                modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(onClick = session::snapshot, enabled = state.live && !state.captureBusy) {
                Text("Snapshot")
            }
            TextButton(
                onClick = session::toggleRecording,
                enabled = state.live || state.recording,
            ) {
                Text(if (state.recording) "Stop clip" else "Record")
            }
        }
    }
}

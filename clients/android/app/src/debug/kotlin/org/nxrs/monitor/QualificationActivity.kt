package org.nxrs.monitor

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId

/** Test-owned UI host; no exported component or production enrollment path. */
@OptIn(ExperimentalComposeUiApi::class)
class QualificationActivity : ComponentActivity() {
    companion object {
        var content: (@Composable () -> Unit)? = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent {
            MonitorTheme {
                Box(Modifier.fillMaxSize().semantics { testTagsAsResourceId = true }) {
                    content?.invoke()
                }
            }
        }
    }
}

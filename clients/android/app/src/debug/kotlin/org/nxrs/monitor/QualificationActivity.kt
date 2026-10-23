package org.nxrs.monitor

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable

/** Test-owned UI host; no exported component or production enrollment path. */
class QualificationActivity : ComponentActivity() {
    companion object {
        var content: (@Composable () -> Unit)? = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { MonitorTheme { content?.invoke() } }
    }
}

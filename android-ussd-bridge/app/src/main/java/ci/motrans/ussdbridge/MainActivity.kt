package ci.motrans.ussdbridge

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import ci.motrans.ussdbridge.databinding.ActivityMainBinding
import ci.motrans.ussdbridge.ussd.UssdController

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.bridgeUrl.text = getString(R.string.bridge_url, Config.port(this))

        binding.btnStart.setOnClickListener {
            ensurePermissions()
            BridgeForegroundService.start(this)
            refresh()
        }
        binding.btnStop.setOnClickListener {
            BridgeForegroundService.stop(this)
            refresh()
        }
        binding.btnAccessibility.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        ensurePermissions()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        val a11y = if (UssdController.accessibilityEnabled) R.string.a11y_on else R.string.a11y_off
        binding.status.text = getString(R.string.status_line, getString(a11y))
    }

    private fun ensurePermissions() {
        val needed = mutableListOf(Manifest.permission.CALL_PHONE, Manifest.permission.READ_PHONE_STATE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1)
    }
}

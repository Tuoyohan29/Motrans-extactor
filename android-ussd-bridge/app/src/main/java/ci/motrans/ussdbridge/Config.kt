package ci.motrans.ussdbridge

import android.content.Context

// Reglages de la passerelle, stockes en local. Le port doit correspondre a USSD_BRIDGE_URL
// cote extracteur (defaut http://127.0.0.1:8765).
object Config {
    private const val PREFS = "bridge_prefs"
    private const val KEY_PORT = "port"
    private const val KEY_TOKEN = "token"

    const val DEFAULT_PORT = 8765
    const val USSD_TIMEOUT_MS = 40_000L

    fun port(context: Context): Int =
        prefs(context).getInt(KEY_PORT, DEFAULT_PORT)

    fun setPort(context: Context, port: Int) =
        prefs(context).edit().putInt(KEY_PORT, port).apply()

    // Jeton partage facultatif (en-tete X-Bridge-Token). Vide = pas de controle
    // (acceptable car l'ecoute est limitee a la boucle locale 127.0.0.1).
    fun token(context: Context): String =
        prefs(context).getString(KEY_TOKEN, "") ?: ""

    fun setToken(context: Context, token: String) =
        prefs(context).edit().putString(KEY_TOKEN, token.trim()).apply()

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}

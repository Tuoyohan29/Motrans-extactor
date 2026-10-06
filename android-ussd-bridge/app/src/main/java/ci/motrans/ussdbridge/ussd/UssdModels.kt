package ci.motrans.ussdbridge.ussd

// Statuts renvoyes a l'extracteur (cf. motrans-extractor docs/PROTOCOLE.md).
object UssdStatus {
    const val WAITING_INPUT = "WAITING_INPUT" // un menu / une saisie est attendu
    const val COMPLETED = "COMPLETED" // reponse finale de l'operateur
    const val TIMEOUT = "TIMEOUT" // pas de reponse a temps
    const val FAILED = "FAILED" // echec (permission, pas de dialogue USSD...)
}

// Un ecran USSD observe : le texte affiche et s'il attend une saisie.
data class UssdScreen(
    val status: String,
    val message: String?,
    val error: String? = null,
)

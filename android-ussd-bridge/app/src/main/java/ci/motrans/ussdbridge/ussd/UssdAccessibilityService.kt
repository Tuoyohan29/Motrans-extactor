package ci.motrans.ussdbridge.ussd

import android.accessibilityservice.AccessibilityService
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.util.Log

// Lit le dialogue USSD affiche par le systeme et permet d'y repondre (navigation menu).
// C'est la partie qui remplace ce que Termux ne sait pas faire : capturer la reponse
// de l'operateur et renvoyer une saisie dans un menu.
class UssdAccessibilityService : AccessibilityService() {
    private val tag = "UssdA11y"

    private val dialogPackages = setOf(
        "com.android.phone", "com.android.server.telecom", "com.android.stk",
        "com.samsung.android.app.telephonyui", "com.oppo.phone", "com.transsion.phonecard",
    )
    private val sendWords = listOf("send", "envoyer", "ok", "valider", "repondre", "répondre")
    private val cancelWords = listOf("cancel", "annuler", "ignorer", "fermer", "dismiss")

    override fun onServiceConnected() {
        UssdController.service = this
        Log.i(tag, "service d'accessibilite connecte")
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        if (UssdController.service === this) UssdController.service = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (UssdController.service === this) UssdController.service = null
        super.onDestroy()
    }

    override fun onInterrupt() {}

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        val type = event.eventType
        if (type != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            type != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
        ) {
            return
        }
        val pkg = event.packageName?.toString() ?: ""
        val className = event.className?.toString() ?: ""
        val looksLikeDialog = pkg in dialogPackages || className.contains("AlertDialog", true) || className.contains("Dialog", true)
        if (!looksLikeDialog) return

        val root = rootInActiveWindow ?: return
        val message = extractMessage(root)
        if (message.isBlank()) return
        val hasInput = findEditable(root) != null
        UssdController.onScreen(message, hasInput)
    }

    // Remplit le champ de saisie et appuie sur "Envoyer". Vrai si la saisie a ete envoyee.
    fun submitInput(input: String): Boolean {
        val root = rootInActiveWindow ?: return false
        val field = findEditable(root) ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, input)
        }
        field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        return clickButton(root, sendWords) || performGlobalAction(GLOBAL_ACTION_DPAD_CENTER)
    }

    fun dismissDialog() {
        val root = rootInActiveWindow ?: return
        if (!clickButton(root, cancelWords)) performGlobalAction(GLOBAL_ACTION_BACK)
    }

    private fun extractMessage(root: AccessibilityNodeInfo): String {
        val parts = mutableListOf<String>()
        traverse(root) { node ->
            if (node.isEditable) return@traverse
            val text = node.text?.toString()?.trim().orEmpty()
            // On ignore les libelles de boutons pour ne garder que le message de l'operateur.
            if (text.isNotEmpty() && !node.isClickable && text !in parts) parts.add(text)
        }
        return parts.joinToString("\n")
    }

    private fun findEditable(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var found: AccessibilityNodeInfo? = null
        traverse(root) { node -> if (found == null && node.isEditable) found = node }
        return found
    }

    private fun clickButton(root: AccessibilityNodeInfo, words: List<String>): Boolean {
        var clicked = false
        traverse(root) { node ->
            if (clicked || !node.isClickable) return@traverse
            val label = node.text?.toString()?.lowercase().orEmpty()
            if (words.any { label == it || label.contains(it) }) {
                clicked = node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            }
        }
        return clicked
    }

    private fun traverse(node: AccessibilityNodeInfo?, visit: (AccessibilityNodeInfo) -> Unit) {
        if (node == null) return
        visit(node)
        for (index in 0 until node.childCount) {
            traverse(node.getChild(index), visit)
        }
    }
}

package ci.motrans.ussdbridge.ussd

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.telephony.TelephonyManager
import android.util.Log
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock

// Orchestre une session USSD entre le serveur HTTP (fil bloquant) et le service
// d'accessibilite (fil d'evenements). Une seule session a la fois.
//
// Deux mecanismes :
//  - service d'accessibilite active -> navigation dans les menus (start + reply) ;
//  - sinon -> TelephonyManager.sendUssdRequest, en un seul coup (pas de reply).
object UssdController {
    private const val TAG = "UssdController"

    @Volatile
    var service: UssdAccessibilityService? = null

    private val sessionLock = ReentrantLock()
    @Volatile
    private var queue: LinkedBlockingQueue<UssdScreen>? = null
    @Volatile
    private var lastPushed: String? = null

    val accessibilityEnabled: Boolean get() = service != null

    // Appele par le service a chaque nouvel ecran USSD lu.
    fun onScreen(message: String, hasInput: Boolean) {
        val q = queue ?: return
        if (message == lastPushed) return
        lastPushed = message
        q.offer(UssdScreen(if (hasInput) UssdStatus.WAITING_INPUT else UssdStatus.COMPLETED, message))
    }

    fun onSessionError(error: String) {
        queue?.offer(UssdScreen(UssdStatus.FAILED, null, error))
    }

    fun start(context: Context, code: String, simSlot: Int?, timeoutMs: Long): UssdScreen {
        if (!sessionLock.tryLock()) {
            return UssdScreen(UssdStatus.FAILED, null, "une autre session USSD est en cours")
        }
        try {
            queue = LinkedBlockingQueue()
            lastPushed = null
            return if (service != null) {
                dial(context, code)
                await(timeoutMs)
            } else {
                sendViaTelephony(context, code, timeoutMs)
            }
        } catch (error: Exception) {
            Log.e(TAG, "start", error)
            return UssdScreen(UssdStatus.FAILED, null, error.message ?: "erreur interne")
        } finally {
            if (sessionLock.isHeldByCurrentThread) sessionLock.unlock()
        }
    }

    fun reply(input: String, timeoutMs: Long): UssdScreen {
        val active = service
            ?: return UssdScreen(UssdStatus.FAILED, null, "navigation impossible : service d'accessibilite desactive")
        lastPushed = null
        if (!active.submitInput(input)) {
            return UssdScreen(UssdStatus.FAILED, null, "champ de saisie USSD introuvable a l'ecran")
        }
        return await(timeoutMs)
    }

    fun cancel() {
        service?.dismissDialog()
        queue = null
    }

    private fun await(timeoutMs: Long): UssdScreen {
        val screen = queue?.poll(timeoutMs, TimeUnit.MILLISECONDS)
        return screen ?: UssdScreen(UssdStatus.TIMEOUT, null, "pas de reponse de l'operateur")
    }

    private fun dial(context: Context, code: String) {
        val uri = Uri.fromParts("tel", code, null)
        val intent = Intent(Intent.ACTION_CALL, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    }

    // Repli sans accessibilite : une requete, une reponse (menus non geres).
    private fun sendViaTelephony(context: Context, code: String, timeoutMs: Long): UssdScreen {
        val manager = context.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
        val result = LinkedBlockingQueue<UssdScreen>(1)
        val handler = Handler(Looper.getMainLooper())
        val callback = object : TelephonyManager.UssdResponseCallback() {
            override fun onReceiveUssdResponse(tm: TelephonyManager, request: String, response: CharSequence) {
                result.offer(UssdScreen(UssdStatus.COMPLETED, response.toString()))
            }

            override fun onReceiveUssdResponseFailed(tm: TelephonyManager, request: String, failureCode: Int) {
                result.offer(UssdScreen(UssdStatus.FAILED, null, "USSD refuse (code $failureCode)"))
            }
        }
        try {
            manager.sendUssdRequest(code, callback, handler)
        } catch (error: SecurityException) {
            return UssdScreen(UssdStatus.FAILED, null, "permission telephone refusee")
        }
        return result.poll(timeoutMs, TimeUnit.MILLISECONDS)
            ?: UssdScreen(UssdStatus.TIMEOUT, null, "pas de reponse de l'operateur")
    }
}

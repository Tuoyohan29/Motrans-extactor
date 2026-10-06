package ci.motrans.ussdbridge.http

import android.content.Context
import android.util.Log
import ci.motrans.ussdbridge.Config
import ci.motrans.ussdbridge.ussd.UssdController
import ci.motrans.ussdbridge.ussd.UssdStatus
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicReference

// Mini serveur HTTP (bibliotheque standard, sans dependance) qui expose le contrat de la
// passerelle USSD attendu par l'extracteur (USSD_BACKEND=http) :
//   POST /ussd/start  {code, simSlot}      -> {sessionId, status, message, error}
//   POST /ussd/reply  {sessionId, input}   -> {status, message, error}
//   POST /ussd/cancel {sessionId}          -> {}
//   GET  /health                           -> {ok:true}
//
// Ecoute UNIQUEMENT sur 127.0.0.1 : seul un programme du meme telephone (Termux) peut
// l'appeler. Un jeton X-Bridge-Token facultatif ajoute un controle.
class BridgeHttpServer(private val context: Context) {
    private val tag = "BridgeHttp"
    private val pool = Executors.newCachedThreadPool()
    private val socketRef = AtomicReference<ServerSocket?>()
    @Volatile
    var sessionId: String? = null
        private set

    val running: Boolean get() = socketRef.get()?.isClosed == false

    fun start() {
        if (running) return
        val port = Config.port(context)
        val server = ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"))
        socketRef.set(server)
        pool.execute {
            Log.i(tag, "passerelle a l'ecoute sur 127.0.0.1:$port")
            while (!server.isClosed) {
                try {
                    val client = server.accept()
                    pool.execute { handle(client) }
                } catch (error: Exception) {
                    if (!server.isClosed) Log.w(tag, "accept: ${error.message}")
                }
            }
        }
    }

    fun stop() {
        socketRef.getAndSet(null)?.let { runCatching { it.close() } }
    }

    private fun handle(client: Socket) {
        client.use { socket ->
            try {
                val reader = BufferedReader(InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8))
                val requestLine = reader.readLine() ?: return
                val parts = requestLine.split(" ")
                if (parts.size < 2) return respond(socket, 400, error("requete invalide"))
                val method = parts[0]
                val path = parts[1]

                var contentLength = 0
                var token = ""
                while (true) {
                    val line = reader.readLine() ?: break
                    if (line.isEmpty()) break
                    val idx = line.indexOf(":")
                    if (idx <= 0) continue
                    val name = line.substring(0, idx).trim().lowercase()
                    val value = line.substring(idx + 1).trim()
                    if (name == "content-length") contentLength = value.toIntOrNull() ?: 0
                    if (name == "x-bridge-token") token = value
                }

                val expected = Config.token(context)
                if (expected.isNotEmpty() && token != expected) {
                    return respond(socket, 401, error("jeton invalide"))
                }

                val body = if (contentLength > 0) {
                    val buffer = CharArray(contentLength)
                    var read = 0
                    while (read < contentLength) {
                        val n = reader.read(buffer, read, contentLength - read)
                        if (n < 0) break
                        read += n
                    }
                    String(buffer, 0, read)
                } else {
                    ""
                }
                route(socket, method, path, body)
            } catch (error: Exception) {
                Log.e(tag, "handle", error)
                runCatching { respond(socket, 500, error(error.message ?: "erreur interne")) }
            }
        }
    }

    private fun route(socket: Socket, method: String, path: String, body: String) {
        val json = if (body.isNotBlank()) runCatching { JSONObject(body) }.getOrNull() ?: JSONObject() else JSONObject()
        when {
            method == "GET" && path == "/health" -> {
                respond(socket, 200, JSONObject().put("ok", true).put("accessibility", UssdController.accessibilityEnabled))
            }
            method == "POST" && path == "/ussd/start" -> handleStart(socket, json)
            method == "POST" && path == "/ussd/reply" -> handleReply(socket, json)
            method == "POST" && path == "/ussd/cancel" -> {
                UssdController.cancel()
                sessionId = null
                respond(socket, 200, JSONObject())
            }
            else -> respond(socket, 404, error("route inconnue"))
        }
    }

    private fun handleStart(socket: Socket, json: JSONObject) {
        val code = json.optString("code").trim()
        if (code.isEmpty()) return respond(socket, 400, error("code manquant"))
        val simSlot = if (json.has("simSlot") && !json.isNull("simSlot")) json.optInt("simSlot") else null
        val id = "s-" + System.currentTimeMillis()
        sessionId = id
        val screen = UssdController.start(context, code, simSlot, Config.USSD_TIMEOUT_MS)
        if (screen.status != UssdStatus.WAITING_INPUT) sessionId = null
        respond(socket, 200, screenJson(screen).put("sessionId", id))
    }

    private fun handleReply(socket: Socket, json: JSONObject) {
        val id = json.optString("sessionId")
        if (sessionId == null || id != sessionId) {
            return respond(socket, 409, error("session USSD inconnue ou terminee"))
        }
        val input = json.optString("input")
        val screen = UssdController.reply(input, Config.USSD_TIMEOUT_MS)
        if (screen.status != UssdStatus.WAITING_INPUT) sessionId = null
        respond(socket, 200, screenJson(screen).put("sessionId", id))
    }

    private fun screenJson(screen: ci.motrans.ussdbridge.ussd.UssdScreen): JSONObject {
        val out = JSONObject().put("status", screen.status)
        screen.message?.let { out.put("message", it) }
        screen.error?.let { out.put("error", it) }
        return out
    }

    private fun error(message: String) = JSONObject().put("error", message)

    private fun respond(socket: Socket, status: Int, body: JSONObject) {
        val payload = body.toString().toByteArray(StandardCharsets.UTF_8)
        val reason = when (status) {
            200 -> "OK"; 400 -> "Bad Request"; 401 -> "Unauthorized"
            404 -> "Not Found"; 409 -> "Conflict"; else -> "Internal Server Error"
        }
        val out: OutputStream = socket.getOutputStream()
        val header = "HTTP/1.1 $status $reason\r\n" +
            "Content-Type: application/json; charset=utf-8\r\n" +
            "Content-Length: ${payload.size}\r\n" +
            "Connection: close\r\n\r\n"
        out.write(header.toByteArray(StandardCharsets.UTF_8))
        out.write(payload)
        out.flush()
    }
}

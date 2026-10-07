"""Fausse passerelle USSD (contrat USSD_BACKEND=http), pour tester l'Extracteur sans
l'application Android ni de SIM.

Implemente les memes routes que `android-ussd-bridge` :
  POST /ussd/start  {code, simSlot?}    -> {sessionId, status, message}
  POST /ussd/reply  {sessionId, input}  -> {status, message}
  POST /ussd/cancel {sessionId}         -> {}
  GET  /health                          -> {ok: true}

Elle simule un arbre de menus : chemin (touches jointes par « / », racine = "")
-> {"message": "...", "status"?}. `status` par defaut : WAITING_INPUT si le noeud a des
enfants, sinon COMPLETED.

    python tools/fake_bridge.py --tree tools/mock_ussd_tree.json --port 8765
    # puis, cote Extracteur : USSD_BACKEND=http  USSD_BRIDGE_URL=http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class BridgeServer:
    """Serveur de passerelle simulee. `tree` : {chemin -> {message, status?}}."""

    def __init__(self, tree: dict[str, dict[str, Any]], port: int = 0):
        self.tree = tree
        self._sessions: dict[str, list[str]] = {}
        self._counter = 0
        self._lock = threading.Lock()
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _read(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "route inconnue"})

            def do_POST(self):
                body = self._read()
                if self.path == "/ussd/start":
                    self._send(200, server_self.start(body.get("code", "")))
                elif self.path == "/ussd/reply":
                    self._send(200, server_self.reply(body.get("sessionId", ""), str(body.get("input", ""))))
                elif self.path == "/ussd/cancel":
                    server_self.cancel(body.get("sessionId", ""))
                    self._send(200, {})
                else:
                    self._send(404, {"error": "route inconnue"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread: threading.Thread | None = None

    # --- Logique de l'arbre ------------------------------------------------------

    def _node(self, path: list[str]) -> dict[str, Any] | None:
        return self.tree.get("/".join(path))

    def _has_children(self, path: list[str]) -> bool:
        prefix = ("/".join(path) + "/") if path else ""
        key = "/".join(path)
        return any(k.startswith(prefix) and k != key for k in self.tree)

    def _screen(self, path: list[str]) -> dict[str, Any]:
        node = self._node(path)
        if node is None:
            return {"status": "FAILED", "error": "chemin inexistant"}
        status = node.get("status") or ("WAITING_INPUT" if self._has_children(path) else "COMPLETED")
        return {"status": status, "message": node.get("message", "")}

    def start(self, code: str) -> dict[str, Any]:
        with self._lock:
            self._counter += 1
            session_id = f"s{self._counter}"
            self._sessions[session_id] = []
        return {"sessionId": session_id, **self._screen([])}

    def reply(self, session_id: str, value: str) -> dict[str, Any]:
        with self._lock:
            path = self._sessions.get(session_id)
            if path is None:
                return {"status": "FAILED", "error": "session inconnue"}
            path.append(value)
            path = list(path)
        return {"sessionId": session_id, **self._screen(path)}

    def cancel(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    # --- Cycle de vie ------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start_background(self) -> "BridgeServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", type=Path, default=Path(__file__).with_name("mock_ussd_tree.json"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    data = json.loads(args.tree.read_text(encoding="utf-8"))
    tree = data.get("tree", data)
    server = BridgeServer(tree, args.port)
    print(f"Fausse passerelle USSD sur {server.url} ({len(tree)} ecran(s))")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

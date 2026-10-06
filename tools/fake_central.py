"""Faux Central, pour faire tourner l'Extracteur sans Balanceur.

Sert les opérations d'un fichier JSON (une liste), une par une, et affiche tout ce que
l'Extracteur envoie. Ne décide rien : c'est juste un tuyau de test.

    python tools/fake_central.py --operations tools/sample_operations.json --port 8080
    # puis, dans .env : CENTRAL_URL=http://127.0.0.1:8080  EXTRACTOR_TOKEN=dev
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

lock = threading.Lock()
pending: list[dict] = []
received: dict[str, list] = {"heartbeats": [], "events": [], "results": []}


class Handler(BaseHTTPRequestHandler):
    token = "dev"
    quiet = False

    def _reply(self, status: int, body=None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._reply(401, {"error": "jeton invalide"})
            return False
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"null")

    def _show(self, label: str, body) -> None:
        if not self.quiet:
            print(f"--> {label}\n{json.dumps(body, ensure_ascii=False, indent=2)}", flush=True)

    def do_GET(self):
        if not self._authorized():
            return
        parts = self.path.strip("/").split("/")
        if parts[-2:] == ["operations", "next"]:
            with lock:
                operation = pending.pop(0) if pending else None
            if operation is None:
                return self._reply(204)
            self._show("opération envoyée", operation)
            return self._reply(200, {"operation": operation})
        if len(parts) == 4 and parts[2] == "operations":
            return self._reply(200, {"operationId": parts[3], "status": "UNKNOWN"})
        self._reply(404, {"error": "route inconnue"})

    def do_POST(self):
        if not self._authorized():
            return
        body = self._body()
        parts = self.path.strip("/").split("/")
        with lock:
            if parts[-1] == "heartbeat":
                received["heartbeats"].append(body)
            elif parts[-1] == "events":
                received["events"].append(body)
                self._show(f"événement {body.get('type')}", body)
            elif parts[-1] == "result":
                received["results"].append(body)
                self._show(f"bilan {body.get('operationId')}", body)
            else:
                return self._reply(404, {"error": "route inconnue"})
        self._reply(200, {"ok": True})

    def log_message(self, *args):
        pass


def serve(port: int, operations: list[dict], token: str = "dev", quiet: bool = False) -> ThreadingHTTPServer:
    pending.extend(operations)
    Handler.token, Handler.quiet = token, quiet
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--operations", type=Path, help="fichier JSON : liste d'opérations à distribuer")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default="dev")
    args = parser.parse_args()
    ops = json.loads(args.operations.read_text(encoding="utf-8")) if args.operations else []
    server = serve(args.port, ops, args.token)
    print(f"Faux Central sur http://127.0.0.1:{args.port} ({len(ops)} opération(s) en attente)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

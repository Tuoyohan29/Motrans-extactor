"""Point d'entrée de l'Extracteur Motrans.

Démarrage -> identification -> connexion au Central -> heartbeat -> attente d'une opération.

    python main.py            # boucle principale
    python main.py --check    # contrôle de la configuration et du téléphone, puis sortie
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from communication.client import CentralClient, CentralError
from communication.heartbeat import Heartbeat
from communication.reporter import Reporter
from config import BASE_DIR, VERSION, Config
from core.executor import Executor
from core.state import StateManager
from device.health import HealthMonitor
from device.info import collect_device_info
from sms.listener import SmsListener
from sms.reader import FileSmsReader, create_reader
from storage.local_state import LocalState
from ussd.launcher import create_launcher
from utils.helpers import command_exists, new_id, run_command
from utils.logger import get_logger, register_secret, setup_logging
from utils.splash import show_splash

log = get_logger("main")


def identify(config: Config, local_state: LocalState) -> str:
    """Identifiant de l'Extracteur : EXTRACTOR_ID du .env, sinon généré une fois et conservé."""
    if not local_state.get("install_id"):
        local_state.update(install_id=new_id())
    extractor_id = config.extractor_id or local_state.get("extractor_id") or f"EXT-{new_id()[:8].upper()}"
    if local_state.get("extractor_id") != extractor_id:
        local_state.update(extractor_id=extractor_id)
    return extractor_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extracteur Motrans (Termux)")
    parser.add_argument("--env", type=Path, default=BASE_DIR / ".env", help="fichier de configuration")
    parser.add_argument("--check", action="store_true", help="vérifier la configuration et le téléphone puis quitter")
    parser.add_argument("--explore", metavar="CODE", help="explorer les menus USSD à partir de CODE (ex. '*144#') puis quitter")
    parser.add_argument("--operator", default="?", help="opérateur associé à --explore")
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.env)
    except ValueError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level, config.data_dir / "logs" if config.log_to_file else None)
    for secret in config.secrets.values():
        register_secret(secret)
    register_secret(config.extractor_token)

    problems = config.problems()
    if problems:
        for problem in problems:
            log.error(problem)
        return 2

    local_state = LocalState(config.data_dir / "state.json")
    extractor_id = identify(config, local_state)
    if config.splash and not args.check and not args.explore:
        show_splash(extractor_id, VERSION, config.ussd_backend, config.sms_backend)
    log.info("Extracteur %s v%s (USSD : %s, SMS : %s)", extractor_id, VERSION, config.ussd_backend, config.sms_backend)

    state = StateManager(local_state)
    if config.central_backend == "firestore":
        from communication.firestore_client import FirestoreClient
        client = FirestoreClient(config.firebase_project_id, config.firebase_api_key, extractor_id,
                                 config.extractor_operators, timeout=config.http_timeout,
                                 lease_seconds=config.operation_lease_sec,
                                 max_attempts=config.operation_max_attempts, base=config.firestore_base)
        log.info("Mode base : écriture directe Firestore (projet %s, opérateurs %s)",
                 config.firebase_project_id, ", ".join(config.extractor_operators))
    else:
        client = CentralClient(config.central_url, config.extractor_token, extractor_id, config.http_timeout)
    reporter = Reporter(client, local_state, extractor_id, state)

    reader = create_reader(config)
    sms_listener = SmsListener(reader, local_state, config)
    launcher = create_launcher(config, reader.add if isinstance(reader, FileSmsReader) else None)
    health = HealthMonitor(config, state, sms_listener)
    device_info = collect_device_info(config, local_state.get("install_id"))
    if config.real_device:
        from device.info import sim_operator_warning
        sim_warning = sim_operator_warning(device_info, config.extractor_operators)
        if sim_warning:
            log.warning(sim_warning)

    if args.check:
        report = health.check()
        print(json.dumps({"extractorId": extractor_id, "deviceInfo": device_info, "health": report},
                         ensure_ascii=False, indent=2))
        try:
            client.heartbeat({"extractorId": extractor_id, "status": "CHECK", "deviceInfo": device_info})
            print("Central : joignable")
        except CentralError as exc:
            print(f"Central : injoignable ({exc})")
        return 0 if report["ok"] else 1

    if args.explore:
        from core.operation import Operation
        from ussd.explorer import UssdExplorer
        operation = Operation.from_dict({"operationId": "EXPLORE", "operator": args.operator,
                                         "type": "explore", "ussdCode": args.explore})
        plan = operation.dial_plan(config.secrets)
        result = UssdExplorer(launcher, config).explore(plan.code).to_dict()
        print(json.dumps({"operator": args.operator, **result}, ensure_ascii=False, indent=2))
        return 0 if not result["stoppedReason"] or result["truncated"] else 1

    if config.wake_lock and command_exists("termux-wake-lock"):
        run_command(["termux-wake-lock"], config.termux_cmd_timeout)
        log.info("Verrou de veille Termux activé")

    stop_event = threading.Event()
    executor = Executor(config, launcher, sms_listener, reporter, state, local_state, stop_event)

    def request_stop(signum, _frame):
        log.info("Arrêt demandé (signal %s) : fin de l'opération en cours puis sortie", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    sms_listener.prime()
    executor.recover_interrupted()

    heartbeat = Heartbeat(client, state, health, device_info, local_state, extractor_id, config.heartbeat_interval)
    heartbeat.beat()  # premier contact et premier contrôle de santé avant d'accepter du travail
    heartbeat.start()

    log.info("En attente d'opérations")
    while not stop_event.is_set():
        try:
            reporter.flush()
            executor.report_idle_sms()
            if state.can_accept_operation():
                operation = client.next_operation()
                state.mark_contact(True)
                if operation:
                    executor.run(operation)
                    continue  # enchaîne tout de suite s'il y a d'autres opérations
        except CentralError as exc:
            state.mark_contact(False)
            if exc.retryable:
                log.debug("Central injoignable : %s", exc)
            else:
                log.error("Le Central refuse l'Extracteur (jeton ? identifiant ?) : %s", exc)
        except Exception:
            log.exception("Erreur dans la boucle principale")
        stop_event.wait(config.poll_interval)

    heartbeat.stop()
    reporter.flush()
    if config.wake_lock and command_exists("termux-wake-unlock"):
        run_command(["termux-wake-unlock"], config.termux_cmd_timeout)
    log.info("Extracteur arrêté")
    return 0


if __name__ == "__main__":
    sys.exit(main())

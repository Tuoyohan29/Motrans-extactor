"""Tests du backend Firestore direct, avec un faux Firestore en mémoire (sans réseau)."""

import json
import time
import unittest
import urllib.parse


def _now():
    return int(time.time() * 1000)


def _future():
    return _now() + 10 * 60 * 1000

from communication.client import CentralError
from communication.firestore_client import FirestoreClient, _dec, _dec_fields, _enc, _enc_fields


class FakeFirestore:
    """Mini-Firestore : GET doc, POST :commit (updateMask + transforms + préconditions),
    POST :runQuery. Suffisant pour les appels de FirestoreClient."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.clock = 0

    def transport(self, method, url, body):
        path = urllib.parse.urlparse(url).path.split("/documents", 1)[1]
        if method == "GET":
            return self._get(path)
        if path == ":runQuery":
            return self._run_query(body)
        if path == ":commit":
            return self._commit(body)
        return 404, {"error": {"message": "route inconnue"}}

    def _get(self, path):
        key = path.lstrip("/")
        doc = self.docs.get(key)
        if not doc:
            return 404, {"error": {"status": "NOT_FOUND"}}
        return 200, {"name": key, "fields": doc["fields"], "updateTime": doc["updateTime"]}

    def _run_query(self, body):
        coll = body["structuredQuery"]["from"][0]["collectionId"]
        wanted = body["structuredQuery"]["where"]["fieldFilter"]["value"]["stringValue"]
        out = []
        for key, doc in self.docs.items():
            if key.split("/")[0] != coll:
                continue
            if _dec_fields(doc).get("status") == wanted:
                out.append({"document": {"name": key, "fields": doc["fields"], "updateTime": doc["updateTime"]}})
        return 200, out

    def _commit(self, body):
        # Vérifie toutes les préconditions avant d'appliquer (atomique).
        for write in body["writes"]:
            key = write["update"]["name"].split("/documents/", 1)[1]
            cond = write.get("currentDocument")
            if cond and "exists" in cond and cond["exists"] is False and key in self.docs:
                return 409, {"error": {"status": "FAILED_PRECONDITION"}}
            if cond and "updateTime" in cond:
                existing = self.docs.get(key)
                if not existing or existing["updateTime"] != cond["updateTime"]:
                    return 400, {"error": {"status": "FAILED_PRECONDITION"}}
        for write in body["writes"]:
            self._apply(write)
        return 200, {"writeResults": [{} for _ in body["writes"]]}

    def _apply(self, write):
        key = write["update"]["name"].split("/documents/", 1)[1]
        self.clock += 1
        doc = self.docs.setdefault(key, {"fields": {}, "updateTime": ""})
        for field, value in write["update"]["fields"].items():
            doc["fields"][field] = value
        for transform in write.get("updateTransforms", []):
            field = transform["fieldPath"]
            current = _dec(doc["fields"].get(field, {"arrayValue": {"values": []}}))
            if not isinstance(current, list):
                current = []
            for value in transform["appendMissingElements"]["values"]:
                current.append(_dec(value))
            doc["fields"][field] = _enc(current)
        doc["updateTime"] = f"t{self.clock}"

    # Helpers de test
    def seed(self, coll, doc_id, fields):
        self.clock += 1
        self.docs[f"{coll}/{doc_id}"] = {"fields": _enc_fields(fields), "updateTime": f"t{self.clock}"}

    def field(self, coll, doc_id, name):
        return _dec_fields(self.docs[f"{coll}/{doc_id}"]).get(name)


def client(fake, extractor_id="EXT-T", operators=("orange",)):
    return FirestoreClient("proj", "key", extractor_id, list(operators), transport=fake.transport)


class CodecTest(unittest.TestCase):
    def test_round_trip(self):
        data = {"s": "x", "i": 5, "f": 1.5, "b": True, "n": None,
                "list": [1, "a", {"k": 2}], "map": {"deep": [True, None]}}
        self.assertEqual(_dec(_enc(data)), data)

    def test_bool_before_int(self):
        self.assertEqual(_enc(True), {"booleanValue": True})
        self.assertEqual(_enc(3), {"integerValue": "3"})


class FirestoreClientTest(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFirestore()
        self.c = client(self.fs)

    def test_auto_register_then_merge(self):
        self.c.heartbeat({"status": "IDLE", "deviceInfo": {"model": "X"}})
        self.assertEqual(self.fs.field("extractors", "EXT-T", "operators"), ["orange"])
        self.assertEqual(self.fs.field("extractors", "EXT-T", "status"), "active")
        self.assertEqual(self.fs.field("extractors", "EXT-T", "lastStatus"), "IDLE")
        # 2e battement : ne réécrit pas operators/status (merge présence only)
        self.fs.docs["extractors/EXT-T"]["fields"]["operators"] = _enc(["orange", "mtn"])
        self.c.heartbeat({"status": "BUSY"})
        self.assertEqual(self.fs.field("extractors", "EXT-T", "operators"), ["orange", "mtn"])
        self.assertEqual(self.fs.field("extractors", "EXT-T", "lastStatus"), "BUSY")

    def test_claim_and_result_goes_to_review(self):
        self.fs.seed("operations", "OP1", {"status": "queued", "operator": "orange", "type": "bundle_purchase",
                                           "ussdCode": "*144#", "amount": 1000, "attempts": 0, "createdAtMs": 10,
                                           "parameters": {"steps": []}, "history": []})
        op = self.c.next_operation()
        self.assertEqual(op["operationId"], "OP1")
        self.assertEqual(self.fs.field("operations", "OP1", "status"), "assigned")
        self.assertEqual(self.fs.field("operations", "OP1", "assignedTo"), "EXT-T")
        self.assertEqual(self.fs.field("operations", "OP1", "attempts"), 1)

        self.c.post_result("OP1", {"outcome": "EXECUTED", "ussd": {"status": "COMPLETED"}})
        self.assertEqual(self.fs.field("operations", "OP1", "status"), "needs_review")
        self.assertEqual(self.fs.field("operations", "OP1", "needsReviewReason"), "execute_a_verifier")
        self.assertEqual(self.fs.field("operations", "OP1", "result")["outcome"], "EXECUTED")

    def test_operator_filter(self):
        self.fs.seed("operations", "OPM", {"status": "queued", "operator": "mtn", "createdAtMs": 1, "attempts": 0})
        self.assertIsNone(self.c.next_operation())  # extracteur orange ne prend pas une op mtn

    def test_oldest_first(self):
        self.fs.seed("operations", "NEW", {"status": "queued", "operator": "orange", "createdAtMs": 50, "attempts": 0,
                                           "ussdCode": "*1#", "parameters": {}, "history": []})
        self.fs.seed("operations", "OLD", {"status": "queued", "operator": "orange", "createdAtMs": 10, "attempts": 0,
                                           "ussdCode": "*1#", "parameters": {}, "history": []})
        self.assertEqual(self.c.next_operation()["operationId"], "OLD")

    def test_claim_lost_tries_next(self):
        self.fs.seed("operations", "OP1", {"status": "queued", "operator": "orange", "createdAtMs": 10, "attempts": 0,
                                           "ussdCode": "*1#", "parameters": {}, "history": []})
        original_commit = self.fs._commit
        calls = {"n": 0}

        def flaky_commit(body):
            calls["n"] += 1
            if calls["n"] == 1:  # simule une prise concurrente sur le 1er claim
                return 400, {"error": {"status": "FAILED_PRECONDITION"}}
            return original_commit(body)

        self.fs._commit = flaky_commit
        # un seul candidat : le claim échoue -> None (pas d'autre à tenter)
        self.assertIsNone(self.c.next_operation())
        self.assertEqual(self.fs.field("operations", "OP1", "status"), "queued")

    def test_event_idempotent(self):
        self.c.post_event({"eventId": "E1", "operationId": "OP1", "type": "SMS_RECEIVED", "seq": 1, "payload": {"x": 1}})
        self.assertIn("operation_events/E1", self.fs.docs)
        before = self.fs.docs["operation_events/E1"]["updateTime"]
        self.c.post_event({"eventId": "E1", "operationId": "OP1", "type": "SMS_RECEIVED", "seq": 1, "payload": {"x": 2}})
        self.assertEqual(self.fs.docs["operation_events/E1"]["updateTime"], before)  # inchangé (doublon)

    def test_catalog_ingest(self):
        self.c.post_event({"eventId": "EC", "type": "CATALOG_DISCOVERED",
                           "payload": {"operator": "orange", "rootCode": "*144#",
                                       "catalog": [{"name": "1 Go", "price": 1000, "volume": 1, "volumeUnit": "Go",
                                                    "validity": 7, "validityUnit": "jours", "path": ["2", "1"],
                                                    "optionKey": "2"}]}})
        entries = [k for k in self.fs.docs if k.startswith("discovered_catalog/orange_")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(_dec_fields(self.fs.docs[entries[0]])["price"], 1000)

    def _seed_assigned(self, doc_id, *, lease, attempts=1, operator="orange"):
        self.fs.seed("operations", doc_id, {"status": "assigned", "operator": operator, "assignedTo": "OTHER",
                                            "attempts": attempts, "leaseExpiresAtMs": lease, "createdAtMs": 10,
                                            "ussdCode": "*1#", "parameters": {}, "history": []})

    def test_reclaim_expired_requeues(self):
        self._seed_assigned("OPX", lease=1, attempts=1)  # bail expiré, essais < max
        self.assertEqual(self.c.reclaim_stalled(), 1)
        self.assertEqual(self.fs.field("operations", "OPX", "status"), "queued")
        self.assertEqual(self.fs.field("operations", "OPX", "assignedTo"), "")

    def test_reclaim_not_expired_untouched(self):
        self._seed_assigned("OPX", lease=_future())
        self.assertEqual(self.c.reclaim_stalled(), 0)
        self.assertEqual(self.fs.field("operations", "OPX", "status"), "assigned")

    def test_reclaim_max_attempts_to_review(self):
        self._seed_assigned("OPX", lease=1, attempts=3)  # plafond atteint
        self.c.reclaim_stalled()
        self.assertEqual(self.fs.field("operations", "OPX", "status"), "needs_review")
        self.assertEqual(self.fs.field("operations", "OPX", "needsReviewReason"), "lease_expired_max_attempts")

    def test_reclaim_operator_filter(self):
        self._seed_assigned("OPX", lease=1, operator="mtn")
        self.assertEqual(self.c.reclaim_stalled(), 0)  # extracteur orange ne reprend pas une op mtn

    def test_next_operation_reclaims_then_claims(self):
        self._seed_assigned("OPX", lease=1, attempts=1)
        op = self.c.next_operation()
        self.assertEqual(op["operationId"], "OPX")
        self.assertEqual(self.fs.field("operations", "OPX", "status"), "assigned")
        self.assertEqual(self.fs.field("operations", "OPX", "assignedTo"), "EXT-T")

    def test_renew_lease_extends(self):
        self.fs.seed("operations", "OPX", {"status": "assigned", "operator": "orange", "assignedTo": "EXT-T",
                                           "leaseExpiresAtMs": 1, "createdAtMs": 10})
        self.c.renew_lease("OPX")
        self.assertGreater(self.fs.field("operations", "OPX", "leaseExpiresAtMs"), _now())

    def test_server_error_is_retryable(self):
        def boom(method, url, body):
            return 503, {"error": {"message": "indisponible"}}
        c = FirestoreClient("p", "k", "E", ["orange"], transport=boom)
        with self.assertRaises(CentralError) as ctx:
            c.heartbeat({"status": "IDLE"})
        self.assertTrue(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()

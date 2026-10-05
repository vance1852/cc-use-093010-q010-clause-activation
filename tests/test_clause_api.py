from __future__ import annotations

import json
import sqlite3
import unittest

from clause_tracking.api import JsonApplication
from clause_tracking.service import ClauseTrackingService


class ClauseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ClauseTrackingService(self.connection))
        self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "sec", "display_name": "秘书处", "role": "secretariat"}).encode("utf-8"))
        self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "dela", "display_name": "代表甲", "role": "delegate"}).encode("utf-8"))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "sec"):
        return self.app.handle(
            "POST", path,
            headers={"X-Actor-Id": actor},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _get(self, path: str, actor: str = "sec"):
        return self.app.handle("GET", path, headers={"X-Actor-Id": actor})

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_flow_over_http(self) -> None:
        response = self._post("/dialogues", {"dialogue_id": "dlg", "title": "对话"})
        self.assertEqual(response.status, 201)
        response = self._post("/dialogues/dlg/participants", {"participant_id": "P1", "display_name": "一方"})
        self.assertEqual(response.status, 201)
        response = self._post(
            "/authorizations",
            {"authorization_id": "a1", "dialogue_id": "dlg", "participant_id": "P1", "delegate_id": "dela"},
        )
        self.assertEqual(response.status, 201)
        response = self._post(
            "/clauses", {"clause_id": "c1", "dialogue_id": "dlg", "clause_code": "C1", "title": "条款"}
        )
        self.assertEqual(response.status, 201)
        response = self._post(
            "/revisions",
            {"revision_id": "base", "clause_id": "c1", "language": "zh", "title": "基线", "body": "正文",
             "kind": "baseline"},
        )
        self.assertEqual(response.status, 201)
        response = self._post(
            "/revisions",
            {"revision_id": "r1", "clause_id": "c1", "language": "zh", "title": "修订", "body": "修订正文"},
            actor="dela",
        )
        self.assertEqual(response.status, 201)
        response = self._post("/revisions/r1/supports", {"participant_id": "P1"}, actor="dela")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["support_count"], 1)
        response = self._post(
            "/seals", {"seal_id": "s1", "clause_id": "c1", "revision_id": "r1", "threshold": "majority"}
        )
        self.assertEqual(response.status, 201)
        self.assertTrue(response.body["passed"])
        response = self._post("/acceptances", {"revision_id": "r1", "participant_id": "P1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "in_force")
        response = self._get("/clauses/c1/status?at=2030-01-01T00:00:00Z", actor="sec")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["revisions"][-1]["bindings"][0]["binding"], "in_force")

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle(
            "POST", "/dialogues",
            body=json.dumps({"dialogue_id": "d", "title": "t"}, ensure_ascii=False).encode("utf-8"),
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", headers={"X-Actor-Id": "sec"})
        self.assertEqual(response.status, 404)

    def test_overdue_endpoint_reverse_lookup(self) -> None:
        self._post("/dialogues", {"dialogue_id": "dlg", "title": "对话"})
        self._post("/dialogues/dlg/participants", {"participant_id": "P1", "display_name": "一方"})
        self._post(
            "/authorizations",
            {"authorization_id": "a1", "dialogue_id": "dlg", "participant_id": "P1", "delegate_id": "dela"},
        )
        self._post("/clauses", {"clause_id": "c1", "dialogue_id": "dlg", "clause_code": "C1", "title": "条款"})
        self._post("/revisions", {"revision_id": "base", "clause_id": "c1", "language": "zh",
                                  "title": "基", "body": "文", "kind": "baseline"})
        self._post("/revisions", {"revision_id": "r1", "clause_id": "c1", "language": "zh",
                                  "title": "改", "body": "改文"}, actor="dela")
        self._post("/revisions/r1/supports", {"participant_id": "P1"}, actor="dela")
        self._post("/seals", {"seal_id": "s1", "clause_id": "c1", "revision_id": "r1"})
        self._post("/acceptances", {"revision_id": "r1", "participant_id": "P1"})
        self._post("/follow_ups", {
            "action_id": "fu1", "revision_id": "r1", "participant_id": "P1", "code": "REPORT",
            "description": "报告", "responsible_participant_id": "P1", "due_at": "2020-01-01T00:00:00Z",
        })
        response = self._get("/follow_ups/overdue")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["count"], 1)
        item = response.body["overdue"][0]
        self.assertEqual(item["clause_code"], "C1")
        self.assertEqual(item["authorization"]["delegate_id"], "dela")
        self.assertEqual(item["seal"]["seal_id"], "s1")


if __name__ == "__main__":
    unittest.main()

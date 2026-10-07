from __future__ import annotations

import json
import sqlite3
import unittest

from clause_tracking.api import JsonApplication
from clause_tracking.service import ClauseTrackingService


SECRETARIAT = {"x-actor-id": "sec"}
DELEGATE = {"x-actor-id": "del-a"}
AUDITOR = {"x-actor-id": "aud"}


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ClauseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ClauseTrackingService(self.connection))
        self.app.handle("POST", "/users", body=_body(
            {"user_id": "sec", "display_name": "秘书处", "role": "secretariat"}))
        self.app.handle("POST", "/users", body=_body(
            {"user_id": "del-a", "display_name": "代表A", "role": "delegate"}))
        self.app.handle("POST", "/users", body=_body(
            {"user_id": "aud", "display_name": "审计", "role": "auditor"}))
        self.app.handle("POST", "/instruments", SECRETARIAT, _body(
            {"instrument_id": "inst-1", "title": "金砖特殊经济区合作文件"}))
        self.app.handle("POST", "/instruments/inst-1/participants", SECRETARIAT, _body(
            {"participant_id": "PA", "display_name": "参与方A"}))
        self.app.handle("POST", "/instruments/inst-1/authorizations", SECRETARIAT, _body({
            "authorization_id": "auth-a", "participant_id": "PA", "delegate_id": "del-a",
            "capabilities": ["revise", "vote", "sign", "reserve", "fulfill"],
            "valid_from": "2020-01-01T00:00:00Z", "valid_until": "2099-01-01T00:00:00Z",
        }))
        self.app.handle("POST", "/instruments/inst-1/baseline", SECRETARIAT, _body({
            "clauses": [{"clause_id": "c1", "title": "条款一", "body": "内容一"}],
        }))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("POST", "/instruments", body=_body(
            {"instrument_id": "x", "title": "x"}))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/missing")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_negotiation_flow_over_http(self) -> None:
        response = self.app.handle("POST", "/instruments/inst-1/revisions", DELEGATE, _body({
            "revision_id": "rev-1", "participant_id": "PA",
            "changes": [{"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "修订"}],
        }))
        self.assertEqual(response.status, 201)
        response = self.app.handle("POST", "/revisions/rev-1/merge", SECRETARIAT)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["version_no"], 2)
        response = self.app.handle("POST", "/instruments/inst-1/vote_rounds", SECRETARIAT, _body(
            {"vote_round_id": "vr-1", "version_no": 2}))
        self.assertEqual(response.status, 201)
        response = self.app.handle("POST", "/vote_rounds/vr-1/ballots", DELEGATE, _body(
            {"participant_id": "PA", "choice": "support"}))
        self.assertEqual(response.status, 200)
        response = self.app.handle("POST", "/vote_rounds/vr-1/seal", SECRETARIAT, _body(
            {"expected_revision": 1}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["support_count"], 1)
        response = self.app.handle("POST", "/vote_rounds/vr-1/seal", SECRETARIAT, _body(
            {"expected_revision": 1}))
        self.assertEqual(response.status, 409)
        response = self.app.handle("POST", "/instruments/inst-1/acceptances", DELEGATE, _body(
            {"participant_id": "PA"}))
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["support_count"], 1)
        response = self.app.handle(
            "POST", "/instruments/inst-1/clauses/c1/force", SECRETARIAT)
        self.assertEqual(response.status, 201)
        response = self.app.handle("GET", "/instruments/inst-1/binding", AUDITOR)
        self.assertEqual(response.status, 200)
        participant = response.body["participants"][0]
        self.assertEqual(participant["clauses"][0]["status"], "in_force")
        response = self.app.handle(
            "GET", "/instruments/inst-1/binding?as_of=2020-06-01T00:00:00Z", AUDITOR)
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["participants"][0]["accepted"])
        response = self.app.handle("GET", "/instruments/inst-1/versions")
        self.assertEqual(response.status, 200)
        self.assertEqual([v["origin"] for v in response.body["versions"]], ["baseline", "merge"])
        response = self.app.handle("GET", "/instruments/inst-1/audit", AUDITOR)
        self.assertEqual(response.status, 200)
        self.assertGreaterEqual(len(response.body["events"]), 5)
        response = self.app.handle("GET", "/instruments/inst-1/audit", DELEGATE)
        self.assertEqual(response.status, 403)

    def test_condition_and_overdue_routes(self) -> None:
        response = self.app.handle("POST", "/instruments/inst-1/conditions", SECRETARIAT, _body({
            "condition_id": "cond-1", "clause_id": "c1", "description": "完成评估",
            "applies_to": "*", "owner_participant_id": "PA", "due_at": "2020-06-01T00:00:00Z",
        }))
        self.assertEqual(response.status, 201)
        response = self.app.handle("GET", "/instruments/inst-1/overdue", AUDITOR)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["overdue_conditions"][0]["owner_participant_id"], "PA")
        response = self.app.handle("POST", "/conditions/cond-1/fulfill", DELEGATE, _body(
            {"evidence_ref": "sha256:" + "1" * 64}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "fulfilled")
        response = self.app.handle("POST", "/conditions/cond-1/revoke", SECRETARIAT, _body(
            {"evidence_ref": "sha256:" + "2" * 64}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "pending")
        self.assertEqual(
            [event["event_type"] for event in response.body["events"]], ["fulfill", "revoke"])


if __name__ == "__main__":
    unittest.main()

"""条款协商与生效跟踪的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ClauseError, ValidationFailed
from .service import ClauseTrackingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到条款跟踪领域服务，便于无网络单元测试。"""

    def __init__(self, service: ClauseTrackingService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _query(target: str) -> dict[str, str]:
        parsed = parse_qs(urlparse(target).query)
        return {key: values[-1] for key, values in parsed.items()}

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = self._query(target)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)  # noqa: E731

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)

            if method == "POST" and path == "/dialogues":
                result = self.service.create_dialogue(
                    actor(), payload["dialogue_id"], payload["title"]
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "dialogues" and parts[2] == "participants":
                result = self.service.register_participant(
                    actor(), parts[1], payload["participant_id"], payload["display_name"]
                )
                return Response(201, result)

            if method == "POST" and path == "/authorizations":
                result = self.service.grant_authorization(
                    actor(), payload["authorization_id"], payload["dialogue_id"],
                    payload["participant_id"], payload["delegate_id"], payload.get("scopes"),
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "authorizations" and parts[2] == "revoke":
                result = self.service.revoke_authorization(actor(), parts[1], payload["reason"])
                return Response(200, result)

            if method == "POST" and path == "/clauses":
                result = self.service.create_clause(
                    actor(), payload["clause_id"], payload["dialogue_id"], payload["clause_code"],
                    payload["title"], independent=bool(payload.get("independent", True)),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "clauses" and parts[2] == "chain":
                return Response(200, self.service.get_clause_chain(actor(), parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "clauses" and parts[2] == "status":
                if "at" not in query:
                    raise ValidationFailed("缺少查询参数 at")
                return Response(200, self.service.status_at(actor(), parts[1], query["at"]))

            if method == "POST" and path == "/revisions":
                result = self.service.propose_revision(
                    actor(), payload["revision_id"], payload["clause_id"], payload["language"],
                    payload["title"], payload["body"], kind=payload.get("kind", "amendment"),
                    parent_revision_id=payload.get("parent_revision_id"),
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "translations":
                result = self.service.register_translation(
                    actor(), payload["translation_id"], parts[1], payload["language"],
                    payload["title"], payload["body"], payload["correspondence"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "supports":
                result = self.service.add_support(actor(), parts[1], payload["participant_id"])
                return Response(200, result)

            if method == "POST" and path == "/conflicts":
                result = self.service.mark_conflict(
                    actor(), payload["revision_a_id"], payload["revision_b_id"], payload["reason"]
                )
                return Response(201, result)

            if method == "POST" and path == "/reservations":
                result = self.service.record_reservation(
                    actor(), payload["reservation_id"], payload["revision_id"],
                    payload["participant_id"], payload["note"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "withdraw":
                result = self.service.withdraw_reservation(actor(), parts[1], payload["reason"])
                return Response(200, result)

            if method == "POST" and path == "/seals":
                result = self.service.seal_ballot(
                    actor(), payload["seal_id"], payload["clause_id"], payload["revision_id"],
                    threshold=payload.get("threshold", "majority"),
                    expected_participation=payload.get("expected_participation"),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "seals":
                return Response(200, self.service.get_seal(parts[1]))

            if method == "POST" and path == "/acceptances":
                result = self.service.accept_text(
                    actor(), payload["revision_id"], payload["participant_id"]
                )
                return Response(200, result)
            if method == "POST" and path == "/terminations":
                result = self.service.terminate_effectiveness(
                    actor(), payload["revision_id"], payload["participant_id"], payload["reason"]
                )
                return Response(200, result)

            if method == "POST" and path == "/preconditions":
                result = self.service.register_precondition(
                    actor(), payload["precondition_id"], payload["revision_id"],
                    payload["participant_id"], payload["code"], payload["description"],
                    payload["responsible_participant_id"], payload["due_at"],
                )
                return Response(201, result)
            if method == "GET" and path == "/preconditions/pending":
                return Response(200, self.service.pending_preconditions(actor(), at=query.get("at")))
            if method == "POST" and len(parts) == 3 and parts[0] == "preconditions" and parts[2] == "satisfy":
                result = self.service.satisfy_precondition(
                    actor(), parts[1], payload["evidence_sha256"], payload["evidence_summary"]
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "preconditions" and parts[2] == "waive":
                result = self.service.waive_precondition(actor(), parts[1], payload["reason"])
                return Response(200, result)

            if method == "POST" and path == "/follow_ups":
                result = self.service.register_follow_up(
                    actor(), payload["action_id"], payload["revision_id"], payload["participant_id"],
                    payload["code"], payload["description"], payload["responsible_participant_id"],
                    payload["due_at"],
                )
                return Response(201, result)
            if method == "GET" and path == "/follow_ups/overdue":
                return Response(200, self.service.overdue_actions(actor(), at=query.get("at")))
            if method == "POST" and len(parts) == 3 and parts[0] == "follow_ups" and parts[2] == "complete":
                result = self.service.complete_follow_up(
                    actor(), parts[1], payload["evidence_sha256"], payload["evidence_summary"]
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "follow_ups" and parts[2] == "revoke":
                result = self.service.revoke_follow_up(actor(), parts[1], payload["reason"])
                return Response(200, result)

            if method == "GET" and path == "/audit":
                return Response(200, {"events": self.service.audit_trail(
                    actor(), query.get("entity_type"))})

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ClauseError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClauseTracking/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动条款协商与生效跟踪 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("clause-tracking.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(ClauseTrackingService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

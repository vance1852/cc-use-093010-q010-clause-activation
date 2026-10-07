"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import ClauseTrackingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

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

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                result = self.service.create_user(payload["user_id"], payload["display_name"], payload["role"])
                return Response(201, result)
            if method == "POST" and path == "/instruments":
                result = self.service.create_instrument(
                    self._actor(normalized_headers), payload["instrument_id"], payload["title"])
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "instruments":
                return Response(200, self.service.get_instrument(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "participants":
                result = self.service.add_participant(
                    self._actor(normalized_headers), parts[1], payload["participant_id"], payload["display_name"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "authorizations":
                result = self.service.grant_authorization(
                    self._actor(normalized_headers), payload["authorization_id"], parts[1],
                    payload["participant_id"], payload["delegate_id"], payload["capabilities"],
                    payload["valid_from"], payload["valid_until"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "authorizations" and parts[2] == "revoke":
                result = self.service.revoke_authorization(
                    self._actor(normalized_headers), parts[1], payload.get("reason", ""))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "baseline":
                result = self.service.create_baseline(
                    self._actor(normalized_headers), parts[1], payload["clauses"], payload.get("note", ""))
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "revisions":
                result = self.service.propose_revision(
                    self._actor(normalized_headers), payload["revision_id"], parts[1],
                    payload["participant_id"], payload["changes"], payload.get("note", ""))
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "merge":
                result = self.service.merge_revision(self._actor(normalized_headers), parts[1])
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "withdraw":
                result = self.service.withdraw_revision(
                    self._actor(normalized_headers), parts[1], payload.get("reason", ""))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "editorial":
                result = self.service.create_editorial_version(
                    self._actor(normalized_headers), parts[1], payload["note"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "translations":
                result = self.service.register_translation(
                    self._actor(normalized_headers), payload["translation_id"], parts[1],
                    payload["clause_id"], payload["clause_sha256"], payload["language"], payload["text"])
                return Response(201, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "translations":
                version_no = query.get("version_no", [None])[0]
                result = self.service.translation_status(
                    parts[1], None if version_no is None else int(version_no))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "acceptances":
                result = self.service.sign_acceptance(
                    self._actor(normalized_headers), parts[1], payload["participant_id"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "reservations":
                result = self.service.declare_reservation(
                    self._actor(normalized_headers), parts[1], payload["participant_id"],
                    payload["clause_id"], payload["statement"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "withdraw":
                result = self.service.withdraw_reservation(self._actor(normalized_headers), int(parts[1]))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "conditions":
                result = self.service.define_condition(
                    self._actor(normalized_headers), payload["condition_id"], parts[1],
                    payload["clause_id"], payload["description"], payload["applies_to"],
                    payload["owner_participant_id"], payload["due_at"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "conditions" and parts[2] == "fulfill":
                result = self.service.fulfill_condition(
                    self._actor(normalized_headers), parts[1], payload["evidence_ref"], payload.get("note", ""))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "conditions" and parts[2] == "revoke":
                result = self.service.revoke_condition(
                    self._actor(normalized_headers), parts[1], payload["evidence_ref"], payload.get("note", ""))
                return Response(200, result)
            if method == "POST" and len(parts) == 5 and parts[0] == "instruments" and parts[2] == "clauses" and parts[4] == "force":
                result = self.service.declare_clause_in_force(
                    self._actor(normalized_headers), parts[1], parts[3])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "vote_rounds":
                result = self.service.open_vote_round(
                    self._actor(normalized_headers), payload["vote_round_id"], parts[1],
                    int(payload["version_no"]))
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "vote_rounds" and parts[2] == "ballots":
                result = self.service.cast_ballot(
                    self._actor(normalized_headers), parts[1], payload["participant_id"], payload["choice"])
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "vote_rounds" and parts[2] == "seal":
                result = self.service.seal_vote_round(
                    self._actor(normalized_headers), parts[1], int(payload["expected_revision"]))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "actions":
                result = self.service.define_action(
                    self._actor(normalized_headers), payload["action_id"], parts[1],
                    payload["clause_id"], payload["description"],
                    payload["owner_participant_id"], payload["due_at"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "actions" and parts[2] == "complete":
                result = self.service.complete_action(
                    self._actor(normalized_headers), parts[1], payload["evidence_ref"])
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "actions" and parts[2] == "cancel":
                result = self.service.cancel_action(
                    self._actor(normalized_headers), parts[1], payload["reason"])
                return Response(200, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "versions":
                return Response(200, self.service.version_chain(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "binding":
                result = self.service.binding_report(
                    self._actor(normalized_headers), parts[1], query.get("as_of", [None])[0])
                return Response(200, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "overdue":
                result = self.service.overdue_report(
                    self._actor(normalized_headers), parts[1], query.get("as_of", [None])[0])
                return Response(200, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "audit":
                result = self.service.audit_trail(self._actor(normalized_headers), parts[1])
                return Response(200, {"events": result})
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication, lock=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClauseTracking/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            # 单个 SQLite 连接按请求串行调度，与 BEGIN IMMEDIATE 的单写者模型一致
            if lock is None:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            else:
                with lock:
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
    parser.add_argument("--database", type=Path, default=Path("clause_tracking.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    application = JsonApplication(ClauseTrackingService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application, threading.Lock()))
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

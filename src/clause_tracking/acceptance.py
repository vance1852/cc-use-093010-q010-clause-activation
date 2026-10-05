"""条款协商与生效跟踪的完整产品流程离线验收。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import ClauseTrackingService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory(prefix="clause-tracking-") as temporary:
        connection = connect(Path(temporary) / "clause.sqlite3")
        try:
            service = ClauseTrackingService(connection, clock)

            for user_id, role in (
                ("sec-1", "secretariat"), ("del-a", "delegate"), ("del-b", "delegate"),
                ("del-c", "delegate"), ("del-i", "delegate"), ("aud-1", "auditor"),
            ):
                service.create_user(user_id, user_id, role)

            service.create_dialogue("sec-1", "brics-sez-2026", "金砖国家特殊经济区合作对话")
            for pid, name in (("BR", "巴西"), ("RU", "俄罗斯"), ("IN", "印度"), ("CN", "中国")):
                service.register_participant("sec-1", "brics-sez-2026", pid, name)

            service.grant_authorization("sec-1", "auth-br", "brics-sez-2026", "BR", "del-a")
            service.grant_authorization("sec-1", "auth-ru", "brics-sez-2026", "RU", "del-b")
            service.grant_authorization("sec-1", "auth-cn", "brics-sez-2026", "CN", "del-c")
            service.grant_authorization("sec-1", "auth-in", "brics-sez-2026", "IN", "del-i")

            # 条款一：数据流动（独立条款）。
            service.create_clause("sec-1", "clause-data", "brics-sez-2026", "DATA-1", "跨境数据流动")
            service.propose_revision(
                "sec-1", "rev-base", "clause-data", "zh", "跨境数据流动基线",
                "各方应允许特殊经济区之间的数据跨境流动。", kind="baseline",
            )
            service.propose_revision(
                "del-a", "rev-a1", "clause-data", "zh", "数据流动修订A",
                "各方应允许特殊经济区之间的数据跨境流动，并互认数据保护评估。",
            )
            service.propose_revision(
                "del-b", "rev-b1", "clause-data", "zh", "数据流动修订B",
                "数据跨境流动须逐案审批，不适用互认安排。",
            )
            service.mark_conflict("sec-1", "rev-a1", "rev-b1", "互认评估与逐案审批不能并行")
            service.register_translation(
                "sec-1", "tr-a1-en", "rev-a1", "en", "Cross-border data flow",
                "Parties shall allow cross-border data flows and mutually recognize data protection assessments.",
                {"p1": "p1", "p2": "p2"},
            )

            service.add_support("del-a", "rev-a1", "BR")
            # 重复签署不增加支持数。
            service.add_support("del-a", "rev-a1", "BR")
            service.add_support("del-c", "rev-a1", "CN")
            service.add_support("del-i", "rev-a1", "IN")
            service.record_reservation("del-b", "res-ru-1", "rev-a1", "RU", "需国内批准后才能支持互认")

            seal = service.seal_ballot(
                "sec-1", "seal-a1", "clause-data", "rev-a1",
                threshold="majority", expected_participation=4,
            )
            # 重复封存只能成功一次（并发场景下由 UNIQUE 与行级条件共同保证）。
            repeat_seal_error = None
            try:
                service.seal_ballot("sec-1", "seal-a1-again", "clause-data", "rev-a1")
            except Exception as exc:  # noqa: BLE001 - 验收记录错误类型
                repeat_seal_error = type(exc).__name__
            # 冲突修订在伙伴通过后不能再封存通过。
            conflict_seal_error = None
            try:
                service.seal_ballot("sec-1", "seal-b1", "clause-data", "rev-b1")
            except Exception as exc:  # noqa: BLE001
                conflict_seal_error = type(exc).__name__

            clock.advance(hours=1)
            # BR：先挂前置条件（国内批准、数据保护评估），再接受文本，因此停留“已接受”。
            service.register_precondition(
                "sec-1", "pc-br-approval", "rev-a1", "BR", "DOMESTIC_APPROVAL",
                "等待巴西国内批准", "BR", "2026-11-01T00:00:00Z",
            )
            service.register_precondition(
                "sec-1", "pc-br-dpia", "rev-a1", "BR", "DATA_PROTECTION_ASSESSMENT",
                "完成数据保护评估", "BR", "2026-10-20T00:00:00Z",
            )
            service.accept_text("sec-1", "rev-a1", "BR")
            # CN 无前置条件：接受即生效。
            service.accept_text("sec-1", "rev-a1", "CN")

            clock.advance(hours=1)
            service.satisfy_precondition("del-a", "pc-br-dpia", "d" * 64, "数据保护评估报告已归档")
            # 历史时点重放：此时 BR 仅“已接受”，CN 已生效。
            before = service.status_at("aud-1", "clause-data", "2026-10-01T09:30:00Z")
            mid = service.status_at("aud-1", "clause-data", "2026-10-01T10:30:00Z")

            clock.advance(hours=1)
            service.satisfy_precondition("del-a", "pc-br-approval", "e" * 64, "国内批准文书已交换")
            # 完成重放：同一证据重复提交返回同一结果，不产生重复事件。
            replayed = service.satisfy_precondition("del-a", "pc-br-approval", "e" * 64, "国内批准文书已交换")

            service.register_follow_up(
                "sec-1", "fu-br-report", "rev-a1", "BR", "ANNUAL_REPORT",
                "每年交换特殊经济区数据流动报告", "BR", "2026-10-10T00:00:00Z",
            )
            service.register_follow_up(
                "sec-1", "fu-cn-report", "rev-a1", "CN", "ANNUAL_REPORT",
                "每年交换特殊经济区数据流动报告", "CN", "2026-10-15T00:00:00Z",
            )

            clock.advance(days=20)
            overdue = service.overdue_actions("aud-1")

            # 行动完成与撤销都要留下可重放依据。
            service.complete_follow_up("del-c", "fu-cn-report", "f" * 64, "首份年度报告已提交")
            revoke = service.revoke_follow_up("sec-1", "fu-br-report", "BR 改由双边渠道报告")

            status = service.status_at("aud-1", "clause-data", service._now())
            chain = service.get_clause_chain("aud-1", "clause-data")
            pending = service.pending_preconditions("aud-1")
            events = service.audit_trail("aud-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    bindings_now = {
        (item["revision_id"], b["participant_id"]): b["binding"]
        for item in status["revisions"] for b in item["bindings"]
    }
    overdue_map = {item["action_id"]: item for item in overdue["overdue"]}
    return {
        "status": "ok",
        "schema": schema,
        "seal_passed": seal["passed"],
        "seal_support_count": seal["support_count"],
        "seal_reservation_count": seal["reservation_count"],
        "repeat_seal_error": repeat_seal_error,
        "conflict_seal_error": conflict_seal_error,
        "cn_state_at_0930": _binding(before, "rev-a1", "CN"),
        "br_state_at_1030": _binding(mid, "rev-a1", "BR"),
        "cn_state_at_1030": _binding(mid, "rev-a1", "CN"),
        "br_state_after": bindings_now.get(("rev-a1", "BR")),
        "cn_state": bindings_now.get(("rev-a1", "CN")),
        "precondition_replay_status": replayed["status"],
        "overdue_count_at_day20": overdue["count"],
        "overdue_traces_clause": overdue_map["fu-br-report"]["clause_code"],
        "overdue_traces_authorization_delegate": overdue_map["fu-br-report"]["authorization"]["delegate_id"],
        "overdue_traces_seal": overdue_map["fu-br-report"]["seal"]["seal_id"],
        "revoked_follow_up": revoke["status"],
        "chain_revision_count": len(chain["revisions"]),
        "remaining_pending_conditions": pending["count"],
        "event_count": len(events),
        "replay_basis_sha256_len": len(events[0]["basis_sha256"]),
    }


def _binding(status: dict[str, object], revision_id: str, participant_id: str) -> str | None:
    for item in status["revisions"]:  # type: ignore[union-attr]
        if item["revision_id"] == revision_id:
            for binding in item["bindings"]:
                if binding["participant_id"] == participant_id:
                    return binding["binding"]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行条款协商与生效跟踪离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

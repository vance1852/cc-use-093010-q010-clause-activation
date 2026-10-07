"""条款协商与生效跟踪完整流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import Conflict, InvalidState
from .service import ClauseTrackingService
from .storage import connect, inspect_schema


ALL_CAPABILITIES = ["revise", "vote", "sign", "reserve", "fulfill"]
VALID_FROM = "2020-01-01T00:00:00Z"
VALID_UNTIL = "2099-12-31T23:59:59Z"
OVERDUE_DUE = "2020-06-01T00:00:00Z"
FUTURE_DUE = "2099-06-01T00:00:00Z"


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"离线验收失败: {message}")


def _binding_statuses(report: dict, participant_id: str) -> dict[str, str]:
    participant = next(
        item for item in report["participants"] if item["participant_id"] == participant_id
    )
    return {clause["clause_id"]: clause["status"] for clause in participant["clauses"]}


def run(workspace: Path) -> dict[str, object]:
    del workspace  # 验收不依赖外部文件，只使用内存中的临时数据库
    with tempfile.TemporaryDirectory(prefix="clause-tracking-") as temporary:
        database = Path(temporary) / "clause-tracking.sqlite3"
        connection = connect(database)
        try:
            service = ClauseTrackingService(connection)
            service.create_user("sec-1", "秘书处专员", "secretariat")
            service.create_user("del-cn", "中方代表", "delegate")
            service.create_user("del-in", "印方代表", "delegate")
            service.create_user("del-br", "巴方代表", "delegate")
            service.create_user("aud-1", "审计人员", "auditor")

            service.create_instrument("sec-1", "brics-sez-2026", "金砖特殊经济区合作文件")
            for participant_id, name in (
                ("CN-SEZ", "中国特殊经济区"), ("IN-SEZ", "印度特殊经济区"), ("BR-SEZ", "巴西特殊经济区")
            ):
                service.add_participant("sec-1", "brics-sez-2026", participant_id, name)
            for authorization_id, delegate_id, participant_id in (
                ("auth-cn", "del-cn", "CN-SEZ"),
                ("auth-in", "del-in", "IN-SEZ"),
                ("auth-br", "del-br", "BR-SEZ"),
            ):
                service.grant_authorization(
                    "sec-1", authorization_id, "brics-sez-2026", participant_id, delegate_id,
                    ALL_CAPABILITIES, VALID_FROM, VALID_UNTIL,
                )

            baseline = service.create_baseline("sec-1", "brics-sez-2026", [
                {"clause_id": "data-flow", "title": "跨境数据流动", "body": "参与方应允许经评估的数据跨境流动。"},
                {"clause_id": "tariff-facilitation", "title": "关税便利", "body": "区内货物享受关税便利安排。"},
                {"clause_id": "dispute-resolution", "title": "争端解决", "body": "争端提交联合委员会协商解决。"},
            ], note="提案基线")
            _expect(baseline["version_no"] == 1, "基线版本应为 1")

            service.propose_revision("del-cn", "rev-1", "brics-sez-2026", "CN-SEZ", [
                {"clause_id": "data-flow", "change_type": "amend",
                 "title": "跨境数据流动", "body": "参与方应允许经数据保护评估的数据跨境流动。"},
            ])
            service.propose_revision("del-in", "rev-2", "brics-sez-2026", "IN-SEZ", [
                {"clause_id": "data-flow", "change_type": "amend",
                 "title": "跨境数据流动", "body": "数据跨境流动须逐案审批。"},
            ])
            merged = service.merge_revision("sec-1", "rev-1")
            _expect(merged["version_no"] == 2, "第一次合并应产生版本 2")
            try:
                service.merge_revision("sec-1", "rev-2")
            except Conflict:
                pass
            else:
                raise RuntimeError("离线验收失败: 冲突修订不应同时合并")
            service.withdraw_revision("del-in", "rev-2", "与已合并修订冲突，重新提案")
            service.propose_revision("del-in", "rev-3", "brics-sez-2026", "IN-SEZ", [
                {"clause_id": "tariff-facilitation", "change_type": "amend",
                 "title": "关税便利", "body": "区内货物凭原产地声明享受关税便利。"},
            ])
            merged = service.merge_revision("sec-1", "rev-3")
            _expect(merged["version_no"] == 3, "第二次合并应产生版本 3")

            chain = service.version_chain("brics-sez-2026")
            clause_digests = {
                clause["clause_id"]: clause["clause_sha256"] for clause in chain["versions"][-1]["clauses"]
            }
            for clause_id, text in (
                ("data-flow", "Parties shall allow cross-border data flows subject to assessment."),
                ("tariff-facilitation", "Goods enjoy tariff facilitation with origin declaration."),
                ("dispute-resolution", "Disputes go to the joint committee."),
            ):
                service.register_translation(
                    "sec-1", f"tr-{clause_id}-en", "brics-sez-2026", clause_id,
                    clause_digests[clause_id], "en", text,
                )
            translations = service.translation_status("brics-sez-2026")
            _expect(
                all(t["current"] for c in translations["clauses"] for t in c["translations"]),
                "登记后译文应对应当前原文",
            )

            service.open_vote_round("sec-1", "vote-v3", "brics-sez-2026", 3)
            service.cast_ballot("del-cn", "vote-v3", "CN-SEZ", "support")
            service.cast_ballot("del-in", "vote-v3", "IN-SEZ", "support")
            service.cast_ballot("del-br", "vote-v3", "BR-SEZ", "support")
            sealed = service.seal_vote_round("sec-1", "vote-v3", 1)
            _expect(sealed["support_count"] == 3 and sealed["state"] == "sealed", "表决封存计数应为 3")
            try:
                service.seal_vote_round("sec-1", "vote-v3", 1)
            except InvalidState:
                pass
            else:
                raise RuntimeError("离线验收失败: 并发封存只能成功一次")

            for delegate_id, participant_id in (
                ("del-cn", "CN-SEZ"), ("del-in", "IN-SEZ"), ("del-br", "BR-SEZ")
            ):
                service.sign_acceptance(delegate_id, "brics-sez-2026", participant_id)
            repeated = service.sign_acceptance("del-cn", "brics-sez-2026", "CN-SEZ")
            _expect(repeated["support_count"] == 3, "重复签署不应增加支持数")

            service.define_condition(
                "sec-1", "cond-data", "brics-sez-2026", "data-flow",
                "完成数据保护评估", "*", "CN-SEZ", FUTURE_DUE,
            )
            service.define_condition(
                "sec-1", "cond-tariff-in", "brics-sez-2026", "tariff-facilitation",
                "提交国内批准文书", "IN-SEZ", "IN-SEZ", OVERDUE_DUE,
            )
            service.define_condition(
                "sec-1", "cond-dispute", "brics-sez-2026", "dispute-resolution",
                "联合委员会章程备案", "*", "BR-SEZ", FUTURE_DUE,
            )
            service.fulfill_condition("del-cn", "cond-data", "sha256:" + "a" * 64, "评估报告已归档")

            service.declare_clause_in_force("sec-1", "brics-sez-2026", "data-flow")
            service.declare_clause_in_force("sec-1", "brics-sez-2026", "tariff-facilitation")
            try:
                service.declare_clause_in_force("sec-1", "brics-sez-2026", "dispute-resolution")
            except InvalidState:
                pass
            else:
                raise RuntimeError("离线验收失败: 全局前置条件未满足时条款不应生效")

            service.declare_reservation(
                "del-br", "brics-sez-2026", "BR-SEZ", "tariff-facilitation",
                "巴方对农产品关税便利保留分步实施权利",
            )

            binding = service.binding_report("aud-1", "brics-sez-2026")
            statuses = _binding_statuses(binding, "CN-SEZ")
            _expect(statuses["data-flow"] == "in_force", "中方数据流动条款应已生效")
            _expect(statuses["dispute-resolution"] == "force_not_declared", "争端解决条款应未生效")
            statuses = _binding_statuses(binding, "IN-SEZ")
            _expect(statuses["tariff-facilitation"] == "conditions_pending", "印方关税条款应等待条件")
            statuses = _binding_statuses(binding, "BR-SEZ")
            _expect(statuses["tariff-facilitation"] == "in_force_with_reservation", "巴方关税条款应带保留生效")

            service.define_action(
                "sec-1", "act-1", "brics-sez-2026", "data-flow",
                "提交年度数据流动统计", "BR-SEZ", OVERDUE_DUE,
            )
            service.define_action(
                "sec-1", "act-2", "brics-sez-2026", "data-flow",
                "建立数据评估联络点", "IN-SEZ", FUTURE_DUE,
            )
            service.complete_action("del-in", "act-2", "sha256:" + "b" * 64)

            service.revoke_condition("sec-1", "cond-data", "sha256:" + "c" * 64, "复核发现评估已过期")
            binding = service.binding_report("aud-1", "brics-sez-2026")
            _expect(
                _binding_statuses(binding, "CN-SEZ")["data-flow"] == "conditions_pending",
                "撤销条件完成后条款应回到等待条件状态",
            )
            service.fulfill_condition("del-cn", "cond-data", "sha256:" + "d" * 64, "更新评估报告")
            condition = service.get_condition("cond-data")
            event_types = [event["event_type"] for event in condition["events"]]
            _expect(event_types == ["fulfill", "revoke", "fulfill"], "条件完成与撤销应可重放")

            overdue = service.overdue_report("aud-1", "brics-sez-2026")
            overdue_action = next(a for a in overdue["overdue_actions"] if a["action_id"] == "act-1")
            _expect(overdue_action["clause"]["clause_id"] == "data-flow", "逾期行动应反查条款")
            _expect(overdue_action["authorization"]["authorization_id"] == "auth-br", "逾期行动应反查授权")
            _expect(
                any(event["evidence_ref"] == "sha256:" + "d" * 64 for event in overdue_action["evidence"]),
                "逾期行动应反查条件证据",
            )
            overdue_condition = next(
                c for c in overdue["overdue_conditions"] if c["condition_id"] == "cond-tariff-in"
            )
            _expect(
                overdue_condition["owner_participant_id"] == "IN-SEZ"
                and overdue_condition["due_at"] == OVERDUE_DUE,
                "未完成条件应保留责任人与期限",
            )

            editorial = service.create_editorial_version("sec-1", "brics-sez-2026", "统一条款编号与排版")
            _expect(editorial["version_no"] == 4, "整理后应产生版本 4")
            _expect(
                editorial["content_sha256"] == chain["versions"][-1]["content_sha256"],
                "整理文本不应改变内容摘要",
            )
            sealed_after = service.get_vote_round("vote-v3")
            _expect(
                sealed_after["support_count"] == 3 and sealed_after["state"] == "sealed",
                "整理文本不应改变已封存的表决事实",
            )
            binding = service.binding_report("aud-1", "brics-sez-2026")
            cn = next(p for p in binding["participants"] if p["participant_id"] == "CN-SEZ")
            _expect(cn["effective_version_no"] == 4, "接受版本应沿整理链前进到版本 4")
            _expect(
                _binding_statuses(binding, "CN-SEZ")["data-flow"] == "in_force",
                "重新满足条件后条款应恢复生效",
            )

            audit = service.audit_trail("aud-1", "brics-sez-2026")
            _expect(len(audit) >= 20, "审计轨迹应覆盖全部关键事实")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "instrument_id": "brics-sez-2026",
        "head_version_no": 4,
        "support_count": 3,
        "sealed_vote": {"vote_round_id": "vote-v3", "support_count": 3, "state": "sealed"},
        "in_force_clauses": ["data-flow", "tariff-facilitation"],
        "condition_event_types": event_types,
        "overdue_actions": [a["action_id"] for a in overdue["overdue_actions"]],
        "overdue_conditions": [c["condition_id"] for c in overdue["overdue_conditions"]],
        "audit_event_count": len(audit),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行条款协商与生效跟踪的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from clause_tracking.clock import FrozenClock
from clause_tracking.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from clause_tracking.service import ClauseTrackingService
from clause_tracking.storage import connect


ALL_CAPABILITIES = ["revise", "vote", "sign", "reserve", "fulfill"]
VALID_FROM = "2026-01-01T00:00:00Z"
VALID_UNTIL = "2027-01-01T00:00:00Z"


class ClauseTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))
        self.service = ClauseTrackingService(self.connection, self.clock)
        self.service.create_user("sec", "秘书处", "secretariat")
        self.service.create_user("del-a", "代表A", "delegate")
        self.service.create_user("del-b", "代表B", "delegate")
        self.service.create_user("aud", "审计", "auditor")
        self.service.create_instrument("sec", "inst-1", "金砖特殊经济区合作文件")
        self.service.add_participant("sec", "inst-1", "PA", "参与方A")
        self.service.add_participant("sec", "inst-1", "PB", "参与方B")
        self.service.grant_authorization(
            "sec", "auth-a", "inst-1", "PA", "del-a", ALL_CAPABILITIES, VALID_FROM, VALID_UNTIL)
        self.service.grant_authorization(
            "sec", "auth-b", "inst-1", "PB", "del-b", ALL_CAPABILITIES, VALID_FROM, VALID_UNTIL)
        self.service.create_baseline("sec", "inst-1", [
            {"clause_id": "c1", "title": "条款一", "body": "内容一"},
            {"clause_id": "c2", "title": "条款二", "body": "内容二"},
        ])

    def tearDown(self) -> None:
        self.connection.close()

    def _status(self, report: dict, participant_id: str, clause_id: str) -> str:
        participant = next(
            item for item in report["participants"] if item["participant_id"] == participant_id)
        clause = next(item for item in participant["clauses"] if item["clause_id"] == clause_id)
        return clause["status"]

    # ---- 修订冲突 ---------------------------------------------------

    def test_conflicting_revisions_cannot_both_merge(self) -> None:
        self.service.propose_revision("del-a", "rev-1", "inst-1", "PA", [
            {"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "甲方版本"},
        ])
        self.service.propose_revision("del-b", "rev-2", "inst-1", "PB", [
            {"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "乙方版本"},
        ])
        merged = self.service.merge_revision("sec", "rev-1")
        self.assertEqual(merged["version_no"], 2)
        with self.assertRaises(Conflict):
            self.service.merge_revision("sec", "rev-2")
        self.assertEqual(self.service.get_revision("rev-2")["state"], "open")
        # 重新基于当前文本后可以合并
        self.service.withdraw_revision("del-b", "rev-2", "重新提案")
        self.service.propose_revision("del-b", "rev-3", "inst-1", "PB", [
            {"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "乙方新版本"},
        ])
        merged = self.service.merge_revision("sec", "rev-3")
        self.assertEqual(merged["version_no"], 3)

    def test_non_conflicting_revisions_merge_in_sequence(self) -> None:
        self.service.propose_revision("del-a", "rev-1", "inst-1", "PA", [
            {"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "修订一"},
        ])
        self.service.propose_revision("del-b", "rev-2", "inst-1", "PB", [
            {"clause_id": "c2", "change_type": "amend", "title": "条款二", "body": "修订二"},
        ])
        self.service.merge_revision("sec", "rev-1")
        merged = self.service.merge_revision("sec", "rev-2")
        clauses = {clause["clause_id"]: clause for clause in merged["clauses"]}
        self.assertEqual(clauses["c1"]["body"], "修订一")
        self.assertEqual(clauses["c1"]["clause_version"], 2)
        self.assertEqual(clauses["c2"]["body"], "修订二")
        self.assertEqual(clauses["c2"]["clause_version"], 2)

    def test_revision_add_and_remove_clause(self) -> None:
        self.service.propose_revision("del-a", "rev-1", "inst-1", "PA", [
            {"clause_id": "c3", "change_type": "add", "title": "条款三", "body": "新增内容"},
            {"clause_id": "c2", "change_type": "remove"},
        ])
        merged = self.service.merge_revision("sec", "rev-1")
        clause_ids = [clause["clause_id"] for clause in merged["clauses"]]
        self.assertEqual(clause_ids, ["c1", "c3"])
        with self.assertRaises(ValidationFailed):
            self.service.propose_revision("del-a", "rev-2", "inst-1", "PA", [
                {"clause_id": "c3", "change_type": "add", "title": "x", "body": "y"},
            ])

    # ---- 表决封存 ---------------------------------------------------

    def test_sealed_vote_facts_survive_editorial_reorganization(self) -> None:
        self.service.open_vote_round("sec", "vr-1", "inst-1", 1)
        self.service.cast_ballot("del-a", "vr-1", "PA", "support")
        self.service.cast_ballot("del-b", "vr-1", "PB", "object")
        sealed = self.service.seal_vote_round("sec", "vr-1", 1)
        self.assertEqual((sealed["support_count"], sealed["object_count"]), (1, 1))
        editorial = self.service.create_editorial_version("sec", "inst-1", "统一编号排版")
        self.assertEqual(editorial["version_no"], 2)
        after = self.service.get_vote_round("vr-1")
        self.assertEqual(after["state"], "sealed")
        self.assertEqual((after["support_count"], after["object_count"], after["abstain_count"]), (1, 1, 0))
        self.assertEqual(len(after["ballots"]), 2)
        with self.assertRaises(InvalidState):
            self.service.cast_ballot("del-a", "vr-1", "PA", "abstain")

    def test_seal_requires_current_revision(self) -> None:
        self.service.open_vote_round("sec", "vr-1", "inst-1", 1)
        self.service.seal_vote_round("sec", "vr-1", 1)
        with self.assertRaises(InvalidState):
            self.service.seal_vote_round("sec", "vr-1", 1)

    def test_concurrent_seal_only_succeeds_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            setup_connection = connect(path)
            try:
                service = ClauseTrackingService(setup_connection, self.clock)
                service.create_user("sec", "秘书处", "secretariat")
                service.create_instrument("sec", "inst-1", "合作文件")
                service.create_baseline("sec", "inst-1", [
                    {"clause_id": "c1", "title": "条款一", "body": "内容一"},
                ])
                service.open_vote_round("sec", "vr-1", "inst-1", 1)
            finally:
                setup_connection.close()
            outcomes: list[str] = []
            barrier = threading.Barrier(2)

            def seal() -> None:
                connection = connect(path)
                try:
                    service = ClauseTrackingService(connection, self.clock)
                    barrier.wait(timeout=10)
                    service.seal_vote_round("sec", "vr-1", 1)
                    outcomes.append("sealed")
                except InvalidState:
                    outcomes.append("rejected")
                finally:
                    connection.close()

            threads = [threading.Thread(target=seal) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(sorted(outcomes), ["rejected", "sealed"])
            connection = connect(path)
            try:
                round_row = ClauseTrackingService(connection, self.clock).get_vote_round("vr-1")
            finally:
                connection.close()
            self.assertEqual(round_row["state"], "sealed")
            self.assertEqual(round_row["revision"], 2)

    def test_ballot_recast_keeps_single_entry(self) -> None:
        self.service.open_vote_round("sec", "vr-1", "inst-1", 1)
        self.service.cast_ballot("del-a", "vr-1", "PA", "support")
        self.service.cast_ballot("del-a", "vr-1", "PA", "abstain")
        self.service.cast_ballot("del-b", "vr-1", "PB", "support")
        sealed = self.service.seal_vote_round("sec", "vr-1", 1)
        self.assertEqual(len(sealed["ballots"]), 2)
        self.assertEqual((sealed["support_count"], sealed["abstain_count"]), (1, 1))

    # ---- 签署 -------------------------------------------------------

    def test_duplicate_signature_does_not_increase_support(self) -> None:
        first = self.service.sign_acceptance("del-a", "inst-1", "PA")
        second = self.service.sign_acceptance("del-a", "inst-1", "PA")
        self.assertEqual(first["support_count"], 1)
        self.assertEqual(second["support_count"], 1)
        events = self.connection.execute(
            "SELECT count(*) FROM acceptance_events WHERE instrument_id='inst-1'").fetchone()[0]
        self.assertEqual(events, 2)
        third = self.service.sign_acceptance("del-b", "inst-1", "PB")
        self.assertEqual(third["support_count"], 2)

    # ---- 独立生效 ---------------------------------------------------

    def test_independent_clauses_enter_force_separately(self) -> None:
        self.service.define_condition(
            "sec", "cond-1", "inst-1", "c1", "完成数据保护评估", "*", "PA", "2026-12-31T00:00:00Z")
        self.service.define_condition(
            "sec", "cond-2", "inst-1", "c2", "国内批准", "*", "PB", "2026-12-31T00:00:00Z")
        self.service.fulfill_condition("del-a", "cond-1", "sha256:" + "1" * 64)
        self.service.declare_clause_in_force("sec", "inst-1", "c1")
        with self.assertRaises(InvalidState):
            self.service.declare_clause_in_force("sec", "inst-1", "c2")
        with self.assertRaises(Conflict):
            self.service.declare_clause_in_force("sec", "inst-1", "c1")
        self.service.fulfill_condition("del-b", "cond-2", "sha256:" + "2" * 64)
        result = self.service.declare_clause_in_force("sec", "inst-1", "c2")
        self.assertEqual(result["clause_id"], "c2")

    # ---- 条件与可重放 -----------------------------------------------

    def test_condition_fulfill_and_revoke_are_replayable(self) -> None:
        self.service.define_condition(
            "sec", "cond-1", "inst-1", "c1", "完成评估", "*", "PA", "2026-12-31T00:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.service.fulfill_condition("del-a", "cond-1", "")
        self.service.fulfill_condition("del-a", "cond-1", "sha256:" + "a" * 64, "评估归档")
        with self.assertRaises(InvalidState):
            self.service.fulfill_condition("del-a", "cond-1", "sha256:" + "b" * 64)
        with self.assertRaises(ValidationFailed):
            self.service.revoke_condition("sec", "cond-1", "")
        self.service.revoke_condition("sec", "cond-1", "sha256:" + "c" * 64, "评估过期")
        condition = self.service.get_condition("cond-1")
        self.assertEqual(condition["state"], "pending")
        self.assertEqual([event["event_type"] for event in condition["events"]], ["fulfill", "revoke"])
        self.assertEqual(
            [event["evidence_ref"] for event in condition["events"]],
            ["sha256:" + "a" * 64, "sha256:" + "c" * 64])
        audit = self.service.audit_trail("aud", "inst-1")
        condition_events = [
            event["event_type"] for event in audit if event["entity_id"] == "cond-1"]
        self.assertEqual(
            condition_events, ["condition.defined", "condition.fulfilled", "condition.revoked"])

    def test_unfulfilled_condition_keeps_owner_and_deadline(self) -> None:
        self.service.define_condition(
            "sec", "cond-1", "inst-1", "c1", "提交批准文书", "PB", "PB", "2026-01-01T00:00:00Z")
        overdue = self.service.overdue_report("aud", "inst-1")
        entry = next(c for c in overdue["overdue_conditions"] if c["condition_id"] == "cond-1")
        self.assertEqual(entry["owner_participant_id"], "PB")
        self.assertEqual(entry["due_at"], "2026-01-01T00:00:00Z")
        self.service.fulfill_condition("del-b", "cond-1", "sha256:" + "d" * 64)
        overdue = self.service.overdue_report("aud", "inst-1")
        self.assertEqual(overdue["overdue_conditions"], [])

    # ---- 约束力报告 -------------------------------------------------

    def test_binding_report_distinguishes_stages(self) -> None:
        self.service.define_condition(
            "sec", "cond-1", "inst-1", "c1", "完成评估", "*", "PA", "2026-12-31T00:00:00Z")
        report = self.service.binding_report("aud", "inst-1")
        pa = next(p for p in report["participants"] if p["participant_id"] == "PA")
        self.assertFalse(pa["accepted"])
        self.service.sign_acceptance("del-a", "inst-1", "PA")
        self.service.sign_acceptance("del-b", "inst-1", "PB")
        report = self.service.binding_report("aud", "inst-1")
        self.assertEqual(self._status(report, "PA", "c1"), "force_not_declared")
        self.service.fulfill_condition("del-a", "cond-1", "sha256:" + "e" * 64)
        self.service.declare_clause_in_force("sec", "inst-1", "c1")
        self.service.declare_clause_in_force("sec", "inst-1", "c2")
        self.service.declare_reservation("del-b", "inst-1", "PB", "c2", "乙方保留分步实施")
        report = self.service.binding_report("aud", "inst-1")
        self.assertEqual(self._status(report, "PA", "c1"), "in_force")
        self.assertEqual(self._status(report, "PB", "c2"), "in_force_with_reservation")
        # 撤销条件完成后回到等待条件状态
        self.service.revoke_condition("sec", "cond-1", "sha256:" + "f" * 64)
        report = self.service.binding_report("aud", "inst-1")
        self.assertEqual(self._status(report, "PA", "c1"), "conditions_pending")
        clause = next(
            c for c in next(p for p in report["participants"] if p["participant_id"] == "PA")["clauses"]
            if c["clause_id"] == "c1")
        self.assertEqual(clause["unfulfilled_conditions"][0]["condition_id"], "cond-1")

    def test_binding_report_as_of_past_moment(self) -> None:
        self.service.sign_acceptance("del-a", "inst-1", "PA")
        self.clock.advance(hours=2)
        self.service.declare_clause_in_force("sec", "inst-1", "c1")
        early = self.service.binding_report("aud", "inst-1", as_of="2026-10-07T09:00:00Z")
        self.assertEqual(self._status(early, "PA", "c1"), "force_not_declared")
        later = self.service.binding_report("aud", "inst-1", as_of="2026-10-07T10:00:00Z")
        self.assertEqual(self._status(later, "PA", "c1"), "in_force")
        before_sign = self.service.binding_report("aud", "inst-1", as_of="2026-10-07T07:00:00Z")
        pa = next(p for p in before_sign["participants"] if p["participant_id"] == "PA")
        self.assertFalse(pa["accepted"])

    def test_acceptance_binds_accepted_version_until_editorial(self) -> None:
        self.service.declare_clause_in_force("sec", "inst-1", "c1")
        self.service.declare_clause_in_force("sec", "inst-1", "c2")
        self.service.sign_acceptance("del-a", "inst-1", "PA")
        self.service.propose_revision("del-b", "rev-1", "inst-1", "PB", [
            {"clause_id": "c2", "change_type": "remove"},
        ])
        self.service.merge_revision("sec", "rev-1")
        self.service.create_editorial_version("sec", "inst-1", "整理编号")
        report = self.service.binding_report("aud", "inst-1")
        pa = next(p for p in report["participants"] if p["participant_id"] == "PA")
        self.assertEqual(pa["accepted_version_no"], 1)
        self.assertEqual(pa["effective_version_no"], 1)
        self.assertEqual([c["clause_id"] for c in pa["clauses"]], ["c1", "c2"])
        # 重新签署后约束文本前进到最新版本
        self.service.sign_acceptance("del-a", "inst-1", "PA")
        report = self.service.binding_report("aud", "inst-1")
        pa = next(p for p in report["participants"] if p["participant_id"] == "PA")
        self.assertEqual(pa["effective_version_no"], 3)
        self.assertEqual([c["clause_id"] for c in pa["clauses"]], ["c1"])

    # ---- 逾期行动反查 -----------------------------------------------

    def test_overdue_action_traces_clause_authorization_evidence(self) -> None:
        self.service.define_condition(
            "sec", "cond-1", "inst-1", "c1", "完成评估", "*", "PA", "2026-12-31T00:00:00Z")
        self.service.fulfill_condition("del-a", "cond-1", "sha256:" + "a" * 64)
        self.service.sign_acceptance("del-b", "inst-1", "PB")
        self.service.define_action(
            "sec", "act-1", "inst-1", "c1", "提交年度统计", "PB", "2026-01-01T00:00:00Z")
        overdue = self.service.overdue_report("aud", "inst-1")
        self.assertEqual(len(overdue["overdue_actions"]), 1)
        action = overdue["overdue_actions"][0]
        self.assertEqual(action["clause"]["clause_id"], "c1")
        self.assertEqual(action["clause"]["title"], "条款一")
        self.assertEqual(action["acceptance"]["authorization_id"], "auth-b")
        self.assertEqual(action["authorization"]["delegate_id"], "del-b")
        self.assertEqual(action["authorization"]["capabilities"], sorted(ALL_CAPABILITIES))
        self.assertEqual(action["evidence"][0]["evidence_ref"], "sha256:" + "a" * 64)
        self.service.complete_action("del-b", "act-1", "sha256:" + "0" * 64)
        overdue = self.service.overdue_report("aud", "inst-1")
        self.assertEqual(overdue["overdue_actions"], [])

    # ---- 授权 -------------------------------------------------------

    def test_delegate_requires_valid_authorization(self) -> None:
        self.service.create_user("del-x", "未授权代表", "delegate")
        with self.assertRaises(Forbidden):
            self.service.sign_acceptance("del-x", "inst-1", "PA")
        # 能力不足的授权
        self.service.grant_authorization(
            "sec", "auth-limited", "inst-1", "PA", "del-x", ["vote"], VALID_FROM, VALID_UNTIL)
        with self.assertRaises(Forbidden):
            self.service.sign_acceptance("del-x", "inst-1", "PA")
        # 已过期的授权
        self.service.create_user("del-y", "过期代表", "delegate")
        self.service.grant_authorization(
            "sec", "auth-expired", "inst-1", "PA", "del-y",
            ALL_CAPABILITIES, "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        with self.assertRaises(Forbidden):
            self.service.sign_acceptance("del-y", "inst-1", "PA")
        # 已撤销的授权
        self.service.revoke_authorization("sec", "auth-limited", "代表离任")
        self.service.open_vote_round("sec", "vr-1", "inst-1", 1)
        with self.assertRaises(Forbidden):
            self.service.cast_ballot("del-x", "vr-1", "PA", "support")

    def test_authorization_validity_window(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.grant_authorization(
                "sec", "auth-bad", "inst-1", "PA", "del-a",
                ALL_CAPABILITIES, "2027-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.service.grant_authorization(
                "sec", "auth-bad2", "inst-1", "PA", "del-a",
                ["fly"], VALID_FROM, VALID_UNTIL)

    # ---- 保留意见 ---------------------------------------------------

    def test_reservation_lifecycle(self) -> None:
        self.service.declare_clause_in_force("sec", "inst-1", "c1")
        self.service.sign_acceptance("del-a", "inst-1", "PA")
        reservation = self.service.declare_reservation("del-a", "inst-1", "PA", "c1", "保留解释")
        with self.assertRaises(Conflict):
            self.service.declare_reservation("del-a", "inst-1", "PA", "c1", "重复保留")
        report = self.service.binding_report("aud", "inst-1")
        self.assertEqual(self._status(report, "PA", "c1"), "in_force_with_reservation")
        self.service.withdraw_reservation("del-a", reservation["reservation_id"])
        report = self.service.binding_report("aud", "inst-1")
        self.assertEqual(self._status(report, "PA", "c1"), "in_force")
        with self.assertRaises(InvalidState):
            self.service.withdraw_reservation("del-a", reservation["reservation_id"])

    # ---- 翻译对应 ---------------------------------------------------

    def test_translation_correspondence_tracks_source_digest(self) -> None:
        chain = self.service.version_chain("inst-1")
        digest_c1 = next(
            c["clause_sha256"] for c in chain["versions"][0]["clauses"] if c["clause_id"] == "c1")
        self.service.register_translation("sec", "tr-1", "inst-1", "c1", digest_c1, "en", "Clause one")
        with self.assertRaises(Conflict):
            self.service.register_translation("sec", "tr-2", "inst-1", "c1", digest_c1, "en", "Clause 1")
        with self.assertRaises(ValidationFailed):
            self.service.register_translation("sec", "tr-3", "inst-1", "c1", "0" * 64, "en", "Ghost")
        # 修订原文后旧译文不再对应当前版本
        self.service.propose_revision("del-a", "rev-1", "inst-1", "PA", [
            {"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "新内容"},
        ])
        self.service.merge_revision("sec", "rev-1")
        status = self.service.translation_status("inst-1")
        entry = next(c for c in status["clauses"] if c["clause_id"] == "c1")
        self.assertFalse(entry["translations"][0]["current"])
        new_digest = entry["clause_sha256"]
        self.service.register_translation("sec", "tr-4", "inst-1", "c1", new_digest, "en", "Clause one revised")
        status = self.service.translation_status("inst-1")
        entry = next(c for c in status["clauses"] if c["clause_id"] == "c1")
        currents = {t["translation_id"]: t["current"] for t in entry["translations"]}
        self.assertEqual(currents, {"tr-1": False, "tr-4": True})

    # ---- 版本链 -----------------------------------------------------

    def test_version_chain_records_lineage(self) -> None:
        self.service.propose_revision("del-a", "rev-1", "inst-1", "PA", [
            {"clause_id": "c1", "change_type": "amend", "title": "条款一", "body": "修订"},
        ])
        self.service.merge_revision("sec", "rev-1")
        self.service.create_editorial_version("sec", "inst-1", "排版")
        chain = self.service.version_chain("inst-1")
        versions = chain["versions"]
        self.assertEqual([v["version_no"] for v in versions], [1, 2, 3])
        self.assertEqual([v["origin"] for v in versions], ["baseline", "merge", "editorial"])
        self.assertIsNone(versions[0]["parent_version_no"])
        self.assertEqual(versions[1]["parent_version_no"], 1)
        self.assertEqual(versions[1]["origin_revision_id"], "rev-1")
        self.assertEqual(versions[2]["parent_version_no"], 2)
        self.assertEqual(versions[2]["content_sha256"], versions[1]["content_sha256"])
        revision = chain["revisions"][0]
        self.assertEqual(revision["state"], "merged")
        self.assertEqual(revision["merged_into_version_no"], 2)
        self.assertEqual(revision["authorization_id"], "auth-a")

    # ---- 角色分工 ---------------------------------------------------

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_instrument("del-a", "inst-2", "越权")
        with self.assertRaises(Forbidden):
            self.service.merge_revision("del-a", "rev-x")
        with self.assertRaises(Forbidden):
            self.service.define_condition(
                "del-a", "cond-x", "inst-1", "c1", "x", "*", "PA", "2026-12-31T00:00:00Z")
        with self.assertRaises(Forbidden):
            self.service.sign_acceptance("sec", "inst-1", "PA")
        with self.assertRaises(Forbidden):
            self.service.binding_report("del-a", "inst-1")
        with self.assertRaises(Forbidden):
            self.service.audit_trail("del-a", "inst-1")

    def test_unknown_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_instrument("missing")
        with self.assertRaises(NotFound):
            self.service.get_revision("missing")
        with self.assertRaises(NotFound):
            self.service.get_condition("missing")
        with self.assertRaises(NotFound):
            self.service.get_action("missing")
        with self.assertRaises(NotFound):
            self.service.get_vote_round("missing")


if __name__ == "__main__":
    unittest.main()

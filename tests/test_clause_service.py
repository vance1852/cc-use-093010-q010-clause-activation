from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from clause_tracking.clock import FrozenClock
from clause_tracking.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from clause_tracking.jsonio import content_digest
from clause_tracking.service import ClauseTrackingService
from clause_tracking.storage import connect


class ClauseServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = ClauseTrackingService(self.connection, self.clock)
        for user_id, role in (
            ("sec", "secretariat"),
            ("dela", "delegate"), ("delb", "delegate"), ("delc", "delegate"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_dialogue("sec", "dlg", "测试对话")
        for pid in ("P1", "P2", "P3"):
            self.service.register_participant("sec", "dlg", pid, pid)
        self.service.grant_authorization("sec", "auth-p1", "dlg", "P1", "dela")
        self.service.grant_authorization("sec", "auth-p2", "dlg", "P2", "delb")
        self.service.grant_authorization("sec", "auth-p3", "dlg", "P3", "delc")

    def tearDown(self) -> None:
        self.connection.close()

    def clause_with_passed_revision(self, clause_id: str = "c1", revision_id: str = "r1") -> str:
        self.service.create_clause("sec", clause_id, "dlg", "C-1", "条款一")
        self.service.propose_revision(
            "sec", f"{revision_id}-base", clause_id, "zh", "基线", "基线文本", kind="baseline"
        )
        self.service.propose_revision("dela", revision_id, clause_id, "zh", "修订", "修订文本")
        self.service.add_support("dela", revision_id, "P1")
        self.service.add_support("delb", revision_id, "P2")
        self.service.seal_ballot("sec", f"seal-{revision_id}", clause_id, revision_id)
        return revision_id


class VersionChainTests(ClauseServiceTestBase):
    def test_baseline_amendment_chain_and_duplicate_dedup(self) -> None:
        self.service.create_clause("sec", "c1", "dlg", "C-1", "条款一")
        base = self.service.propose_revision("sec", "base", "c1", "zh", "基线", "基线文本", kind="baseline")
        self.assertEqual(base["revision_no"], 1)
        self.assertEqual(base["kind"], "baseline")
        am1 = self.service.propose_revision("dela", "am1", "c1", "zh", "修订一", "修订一文本")
        self.assertEqual(am1["revision_no"], 2)
        self.assertEqual(am1["parent_revision_id"], "base")
        am2 = self.service.propose_revision(
            "delb", "am2", "c1", "zh", "修订二", "修订二文本", parent_revision_id="base"
        )
        # 允许以基线为父版本分叉，编号仍在条款内单调递增。
        self.assertEqual(am2["revision_no"], 3)
        self.assertEqual(am2["parent_revision_id"], "base")

        # 内容完全相同的重复提案不产生新版本。
        again = self.service.propose_revision("delc", "am1-copy", "c1", "zh", "修订一", "修订一文本")
        self.assertEqual(again["revision_id"], "am1")
        count = self.connection.execute("SELECT count(*) FROM clause_revisions WHERE clause_id='c1'").fetchone()[0]
        self.assertEqual(count, 3)

    def test_second_baseline_rejected(self) -> None:
        self.service.create_clause("sec", "c1", "dlg", "C-1", "条款一")
        self.service.propose_revision("sec", "base", "c1", "zh", "基线", "文本", kind="baseline")
        with self.assertRaises(Conflict):
            self.service.propose_revision("sec", "base2", "c1", "zh", "基线2", "文本2", kind="baseline")

    def test_translation_correspondence(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.register_translation(
            "sec", "tr-en", revision_id, "en", "Amendment", "English text", {"1": "1", "2": "2"}
        )
        chain = self.service.get_clause_chain("aud", "c1")
        translation = chain["revisions"][1]["translations"][0]
        self.assertEqual(translation["language"], "en")
        self.assertEqual(translation["correspondence"], {"1": "1", "2": "2"})
        with self.assertRaises(Conflict):
            self.service.register_translation(
                "sec", "tr-en-2", revision_id, "en", "Amendment", "English text", {"1": "1"}
            )


class SupportAndSealTests(ClauseServiceTestBase):
    def test_duplicate_signature_does_not_increase_support(self) -> None:
        revision_id = self.clause_with_passed_revision()
        # P1 在封存前已支持；这里直接验证计数逻辑：新增 P3 支持后重复 P3。
        self.service.create_clause("sec", "c2", "dlg", "C-2", "条款二")
        self.service.propose_revision("sec", "c2-base", "c2", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "c2-r1", "c2", "zh", "修订", "新文本")
        first = self.service.add_support("delc", "c2-r1", "P3")
        second = self.service.add_support("delc", "c2-r1", "P3")
        self.assertEqual(first["support_count"], second["support_count"])
        events = [
            row["event_type"]
            for row in self.connection.execute(
                "SELECT event_type FROM event_journal WHERE entity_type='clause_revision' "
                "AND entity_id=? ORDER BY event_id", ("c2-r1",)
            ).fetchall()
        ]
        self.assertEqual(events.count("support.added"), 1)
        # 封存数据同样不受重复签署影响。
        self.service.add_support("dela", "c2-r1", "P1")
        seal = self.service.seal_ballot("sec", "seal-c2", "c2", "c2-r1")
        self.assertEqual(seal["support_count"], 2)

    def test_seal_can_only_succeed_once_under_concurrency(self) -> None:
        self.service.create_clause("sec", "cc", "dlg", "C-X", "并发条款")
        self.service.propose_revision("sec", "cc-base", "cc", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "cc-r1", "cc", "zh", "修订", "新文本")
        for delegate, pid in (("dela", "P1"), ("delb", "P2")):
            self.service.add_support(delegate, "cc-r1", pid)

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "concurrency.sqlite3")
            backup = sqlite3.connect(db_path)
            self.connection.backup(backup)
            backup.close()

            outcomes: list[object] = []

            def worker(index: int) -> None:
                connection = connect(db_path)
                try:
                    service = ClauseTrackingService(connection)
                    try:
                        service.seal_ballot("sec", f"seal-{index}", "cc", "cc-r1")
                        outcomes.append("ok")
                    except Exception as exc:  # noqa: BLE001
                        outcomes.append(type(exc).__name__)
                finally:
                    connection.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            verifier = connect(db_path)
            seal_count = verifier.execute(
                "SELECT count(*) FROM clause_seals WHERE revision_id='cc-r1'"
            ).fetchone()[0]
            verifier.close()

        self.assertEqual(len(outcomes), 8)
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(seal_count, 1)

    def test_sealed_ballot_is_immutable_fact(self) -> None:
        revision_id = self.clause_with_passed_revision()
        seal = self.service.get_seal("seal-r1")
        self.assertTrue(seal["passed"])
        self.assertEqual(seal["ballot"]["support"], ["P1", "P2"])
        # 事后再整理文本：登记新修订、撤回授权都不能改变封存事实。
        with self.assertRaises(Conflict):
            self.service.seal_ballot("sec", "seal-r1-again", "c1", revision_id)
        with self.assertRaises(InvalidState):
            self.service.add_support("delc", revision_id, "P3")
        # 封存后仍可开启新一轮修订，但旧封存事实保持不变。
        self.service.propose_revision("delc", "r1-next", "c1", "zh", "下一轮修订", "新文本",
                                      parent_revision_id=revision_id)
        seal_again = self.service.get_seal("seal-r1")
        self.assertEqual(seal_again["ballot"], seal["ballot"])

    def test_conflicting_revisions_cannot_both_merge(self) -> None:
        self.service.create_clause("sec", "cf", "dlg", "C-F", "冲突条款")
        self.service.propose_revision("sec", "cf-base", "cf", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "cf-a", "cf", "zh", "方案甲", "甲方文本")
        self.service.propose_revision("delb", "cf-b", "cf", "zh", "方案乙", "乙方文本")
        self.service.mark_conflict("sec", "cf-a", "cf-b", "二者互斥")
        self.service.add_support("dela", "cf-a", "P1")
        self.service.add_support("delb", "cf-a", "P2")
        sealed_a = self.service.seal_ballot("sec", "seal-cf-a", "cf", "cf-a")
        self.assertTrue(sealed_a["passed"])
        # 伙伴已通过封存：乙即使凑齐支持也不能合并。
        self.service.add_support("delc", "cf-b", "P3")
        with self.assertRaises(InvalidState):
            self.service.seal_ballot("sec", "seal-cf-b", "cf", "cf-b")
        # 反向也成立：先封存乙则甲不能再通过（新条款验证）。
        self.service.create_clause("sec", "cf2", "dlg", "C-G", "冲突条款二")
        self.service.propose_revision("sec", "cf2-base", "cf2", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "cf2-a", "cf2", "zh", "甲", "甲文本")
        self.service.propose_revision("delb", "cf2-b", "cf2", "zh", "乙", "乙文本")
        self.service.mark_conflict("sec", "cf2-a", "cf2-b", "互斥")
        self.service.add_support("delb", "cf2-b", "P2")
        self.service.add_support("delc", "cf2-b", "P3")
        self.service.seal_ballot("sec", "seal-cf2-b", "cf2", "cf2-b")
        self.service.add_support("dela", "cf2-a", "P1")
        with self.assertRaises(InvalidState):
            self.service.seal_ballot("sec", "seal-cf2-a", "cf2", "cf2-a")

    def test_conflict_requires_same_clause(self) -> None:
        self.service.create_clause("sec", "x1", "dlg", "X1", "条款X1")
        self.service.create_clause("sec", "x2", "dlg", "X2", "条款X2")
        self.service.propose_revision("sec", "x1-b", "x1", "zh", "基线", "条款X1正文", kind="baseline")
        self.service.propose_revision("sec", "x2-b", "x2", "zh", "基线", "条款X2正文", kind="baseline")
        with self.assertRaises(ValidationFailed):
            self.service.mark_conflict("sec", "x1-b", "x2-b", "跨条款")

    def test_conflict_cannot_be_marked_after_both_sealed_passed(self) -> None:
        self.service.create_clause("sec", "rc", "dlg", "C-RC", "事后冲突条款")
        self.service.propose_revision("sec", "rc-base", "rc", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "rc-a", "rc", "zh", "甲", "甲文本")
        self.service.propose_revision("delb", "rc-b", "rc", "zh", "乙", "乙文本")
        self.service.add_support("dela", "rc-a", "P1")
        self.service.add_support("delb", "rc-a", "P2")
        self.service.seal_ballot("sec", "seal-rc-a", "rc", "rc-a")
        # 乙另立支持并通过封存（此时尚未登记冲突，故允许）。
        self.service.add_support("delc", "rc-b", "P3")
        self.service.add_support("dela", "rc-b", "P1")
        self.service.seal_ballot("sec", "seal-rc-b", "rc", "rc-b")
        # 两份封存均为既成事实后，不能再追溯宣称它们冲突。
        with self.assertRaises(InvalidState):
            self.service.mark_conflict("sec", "rc-a", "rc-b", "事后互斥")

    def test_reservation_blocks_support_and_seal_counts(self) -> None:
        self.service.create_clause("sec", "rv", "dlg", "C-R", "保留条款")
        self.service.propose_revision("sec", "rv-base", "rv", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "rv-r1", "rv", "zh", "修订", "新文本")
        self.service.record_reservation("delb", "res-1", "rv-r1", "P2", "需要国内批准")
        with self.assertRaises(Conflict):
            self.service.add_support("delb", "rv-r1", "P2")
        self.service.add_support("dela", "rv-r1", "P1")
        seal = self.service.seal_ballot("sec", "seal-rv", "rv", "rv-r1", threshold="unanimity")
        self.assertFalse(seal["passed"])
        self.assertEqual(seal["reservation_count"], 1)


class AuthorizationTests(ClauseServiceTestBase):
    def test_delegate_needs_authorization(self) -> None:
        # aud 不是任何参与方代表。
        with self.assertRaises(Forbidden):
            self.service.add_support("aud", "r1", "P1")

    def test_revoked_authorization_forbids_further_actions(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.revoke_authorization("sec", "auth-p1", "授权期满")
        with self.assertRaises(Forbidden):
            self.service.record_reservation("dela", "res-late", revision_id, "P1", "事后保留")

    def test_scoped_authorization_enforced(self) -> None:
        # P2 原有全权授权撤销后，换发仅含保留意见权限的授权。
        self.service.revoke_authorization("sec", "auth-p2", "改用范围授权")
        self.service.grant_authorization(
            "sec", "auth-p2-res-only", "dlg", "P2", "delb", scopes=["reservation.write"]
        )
        self.service.create_clause("sec", "sc", "dlg", "C-S", "范围条款")
        self.service.propose_revision("sec", "sc-base", "sc", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "sc-r1", "sc", "zh", "修订", "新文本")
        with self.assertRaises(Forbidden):
            self.service.add_support("delb", "sc-r1", "P2")
        self.service.record_reservation("delb", "res-sc", "sc-r1", "P2", "仅授权保留意见")


class EffectivenessTests(ClauseServiceTestBase):
    def test_independent_clauses_take_effect_separately(self) -> None:
        r1 = self.clause_with_passed_revision("c1", "r1")
        self.service.create_clause("sec", "c2", "dlg", "C-2", "条款二")
        self.service.propose_revision("sec", "r2-base", "c2", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("delc", "r2", "c2", "zh", "修订", "新文本")
        self.service.add_support("delc", "r2", "P3")
        self.service.add_support("dela", "r2", "P1")
        self.service.seal_ballot("sec", "seal-r2", "c2", "r2")

        # 条款一只需 P1 接受即生效，与条款二的进度无关。
        effect = self.service.accept_text("sec", r1, "P1")
        self.assertEqual(effect["state"], "in_force")
        # 条款二对 P2 尚无任何接受记录。
        with self.assertRaises(NotFound):
            self.service._effectiveness_row("r2", "P2")

    def test_non_independent_clause_requires_whole_package(self) -> None:
        self.service.create_clause("sec", "pkg1", "dlg", "P-1", "组合一", independent=False)
        self.service.create_clause("sec", "pkg2", "dlg", "P-2", "组合二", independent=False)
        self.service.propose_revision("sec", "pk1-base", "pkg1", "zh", "基线", "t", kind="baseline")
        self.service.propose_revision("dela", "pk1-r1", "pkg1", "zh", "修订", "x")
        self.service.add_support("dela", "pk1-r1", "P1")
        self.service.add_support("delb", "pk1-r1", "P2")
        self.service.seal_ballot("sec", "seal-pk1", "pkg1", "pk1-r1")
        with self.assertRaises(InvalidState):
            self.service.accept_text("sec", "pk1-r1", "P1")

        self.service.propose_revision("sec", "pk2-base", "pkg2", "zh", "基线", "t", kind="baseline")
        self.service.propose_revision("dela", "pk2-r1", "pkg2", "zh", "修订", "x")
        self.service.add_support("dela", "pk2-r1", "P1")
        self.service.add_support("delb", "pk2-r1", "P2")
        self.service.seal_ballot("sec", "seal-pk2", "pkg2", "pk2-r1")
        self.assertEqual(self.service.accept_text("sec", "pk1-r1", "P1")["state"], "in_force")

    def test_preconditions_hold_effect_until_all_settled(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.register_precondition(
            "sec", "pc1", revision_id, "P1", "APPROVAL", "国内批准", "P1", "2026-11-01T00:00:00Z"
        )
        self.service.register_precondition(
            "sec", "pc2", revision_id, "P1", "DPIA", "数据保护评估", "P1", "2026-10-20T00:00:00Z"
        )
        accepted = self.service.accept_text("sec", revision_id, "P1")
        self.assertEqual(accepted["state"], "accepted")

        pending = self.service.pending_preconditions("aud", at="2026-11-02T00:00:00Z")
        self.assertEqual(pending["count"], 2)
        self.assertTrue(all(item["overdue"] for item in pending["pending"]))
        self.assertEqual(pending["pending"][0]["responsible_participant_id"], "P1")

        self.service.satisfy_precondition("dela", "pc2", "a" * 64, "评估完成")
        self.assertEqual(self.service._effectiveness_row(revision_id, "P1")["state"], "accepted")
        self.service.satisfy_precondition("dela", "pc1", "b" * 64, "批准完成")
        self.assertEqual(self.service._effectiveness_row(revision_id, "P1")["state"], "in_force")
        self.assertEqual(self.service.pending_preconditions("aud")["count"], 0)

    def test_precondition_replay_and_conflicting_evidence(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.register_precondition(
            "sec", "pc1", revision_id, "P1", "APPROVAL", "国内批准", "P1", "2026-11-01T00:00:00Z"
        )
        self.service.accept_text("sec", revision_id, "P1")
        first = self.service.satisfy_precondition("dela", "pc1", "a" * 64, "批准")
        # 同证据重放：返回既有结果，不新增事件。
        event_count_before = self.connection.execute("SELECT count(*) FROM event_journal").fetchone()[0]
        replayed = self.service.satisfy_precondition("dela", "pc1", "a" * 64, "批准")
        event_count_after = self.connection.execute("SELECT count(*) FROM event_journal").fetchone()[0]
        self.assertEqual(first["status"], replayed["status"])
        self.assertEqual(event_count_before, event_count_after)
        # 不同证据冲突。
        with self.assertRaises(Conflict):
            self.service.satisfy_precondition("dela", "pc1", "c" * 64, "另一份批准")

    def test_waive_precondition_also_enacts_and_is_replayable(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.register_precondition(
            "sec", "pc1", revision_id, "P1", "APPROVAL", "国内批准", "P1", "2026-11-01T00:00:00Z"
        )
        self.service.accept_text("sec", revision_id, "P1")
        self.service.waive_precondition("sec", "pc1", "经外交渠道确认免批")
        self.assertEqual(self.service._effectiveness_row(revision_id, "P1")["state"], "in_force")
        with self.assertRaises(InvalidState):
            self.service.waive_precondition("sec", "pc1", "再次豁免")

    def test_concurrent_satisfy_only_one_settles(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.register_precondition(
            "sec", "pc-x", revision_id, "P1", "APPROVAL", "批准", "P1", "2026-11-01T00:00:00Z"
        )
        self.service.accept_text("sec", revision_id, "P1")
        outcomes: list[object] = []

        def worker(evidence: str) -> None:
            connection = sqlite3.connect(self.db_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                service = ClauseTrackingService(connection)
                try:
                    service.satisfy_precondition("dela", "pc-x", evidence, "并发证据")
                    outcomes.append("ok")
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(type(exc).__name__)
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as directory:
            self.db_path = str(Path(directory) / "satisfy.sqlite3")
            backup = sqlite3.connect(self.db_path)
            self.connection.backup(backup)
            backup.close()
            threads = [
                threading.Thread(target=worker, args=(f"{i:0{64}d}",))
                for i in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(outcomes), ["Conflict", "ok"])


class FollowUpTests(ClauseServiceTestBase):
    def _effective(self) -> str:
        revision_id = self.clause_with_passed_revision()
        self.service.accept_text("sec", revision_id, "P1")
        return revision_id

    def test_overdue_reverse_lookup_carries_clause_authorization_and_seal(self) -> None:
        revision_id = self._effective()
        self.service.register_follow_up(
            "sec", "fu1", revision_id, "P1", "REPORT", "提交报告", "P1", "2026-10-10T00:00:00Z"
        )
        self.clock.advance(days=20)
        overdue = self.service.overdue_actions("aud")
        self.assertEqual(overdue["count"], 1)
        item = overdue["overdue"][0]
        self.assertEqual(item["clause_code"], "C-1")
        self.assertEqual(item["revision_id"], revision_id)
        self.assertEqual(item["authorization"]["delegate_id"], "dela")
        self.assertEqual(item["seal"]["seal_id"], "seal-r1")
        self.assertGreaterEqual(item["overdue_days"], 11)
        self.assertEqual(item["binding"]["state"], "in_force")

    def test_complete_and_revoke_leave_replayable_basis(self) -> None:
        revision_id = self._effective()
        self.service.register_follow_up(
            "sec", "fu1", revision_id, "P1", "REPORT", "报告", "P1", "2026-12-31T00:00:00Z"
        )
        self.service.register_follow_up(
            "sec", "fu2", revision_id, "P1", "NOTICE", "通知", "P1", "2026-12-31T00:00:00Z"
        )
        done = self.service.complete_follow_up("dela", "fu1", "c" * 64, "报告已交")
        self.assertEqual(done["status"], "done")
        # 完成可重放。
        again = self.service.complete_follow_up("dela", "fu1", "c" * 64, "报告已交")
        self.assertEqual(again["status"], "done")
        with self.assertRaises(Conflict):
            self.service.complete_follow_up("dela", "fu1", "9" * 64, "另一份证据")
        revoked = self.service.revoke_follow_up("sec", "fu2", "不再需要")
        self.assertEqual(revoked["status"], "revoked")
        with self.assertRaises(InvalidState):
            self.service.revoke_follow_up("sec", "fu2", "重复撤销")
        with self.assertRaises(InvalidState):
            self.service.complete_follow_up("dela", "fu2", "d" * 64, "撤销后完成")

        events = self.service.audit_trail("aud", "follow_up")
        kinds = {event["event_type"] for event in events}
        self.assertIn("follow_up.completed", kinds)
        self.assertIn("follow_up.revoked", kinds)
        for event in events:
            expected = content_digest([
                event["entity_type"], event["entity_id"], event["event_type"], event["actor_id"],
                event["recorded_at"], event["payload"],
            ])
            self.assertEqual(event["basis_sha256"], expected)


class PointInTimeTests(ClauseServiceTestBase):
    def test_status_at_replays_history_and_termination_keeps_past(self) -> None:
        revision_id = self.clause_with_passed_revision()
        self.service.accept_text("sec", revision_id, "P1")
        self.clock.advance(days=1)
        in_force_day = self.service._now()
        self.service.register_follow_up(
            "sec", "fu1", revision_id, "P1", "R", "行动", "P1", "2026-12-01T00:00:00Z"
        )
        self.clock.advance(days=1)
        self.service.terminate_effectiveness("sec", revision_id, "P1", "P1 退出本安排")

        # 表决/接受之前的时点：版本尚不存在。
        before = self.service.status_at("aud", "c1", "2026-10-01T07:59:00Z")
        self.assertEqual(before["revisions"], [])

        at_seal = self.service.status_at(
            "aud", "c1", "2026-10-01T08:30:00Z"
        )["revisions"][-1]
        self.assertTrue(at_seal["sealed"])
        self.assertEqual(
            at_seal["bindings"],
            [{"participant_id": "P1", "binding": "in_force", "effective_at": "2026-10-01T08:00:00Z"}],
        )

        at_day1 = self.service.status_at("aud", "c1", in_force_day)
        binding = {
            (r["revision_id"], b["participant_id"]): b["binding"]
            for r in at_day1["revisions"] for b in r["bindings"]
        }
        self.assertEqual(binding[(revision_id, "P1")], "in_force")

        at_now = self.service.status_at("aud", "c1", self.service._now())
        binding_now = {
            (r["revision_id"], b["participant_id"]): b["binding"]
            for r in at_now["revisions"] for b in r["bindings"]
        }
        self.assertEqual(binding_now[(revision_id, "P1")], "terminated")
        # 但历史时点的答案不变。
        history = self.service.status_at("aud", "c1", in_force_day)
        historical = {
            (r["revision_id"], b["participant_id"]): b["binding"]
            for r in history["revisions"] for b in r["bindings"]
        }
        self.assertEqual(historical[(revision_id, "P1")], "in_force")

    def test_reservation_participant_cannot_accept_until_withdrawn(self) -> None:
        self.service.create_clause("sec", "c9", "dlg", "C-9", "保留方条款")
        self.service.propose_revision("sec", "c9-base", "c9", "zh", "基线", "文本", kind="baseline")
        self.service.propose_revision("dela", "c9-r1", "c9", "zh", "修订", "新文本")
        self.service.add_support("dela", "c9-r1", "P1")
        self.service.add_support("delc", "c9-r1", "P3")
        self.service.record_reservation("delb", "res-c9", "c9-r1", "P2", "等待授权")
        seal = self.service.seal_ballot("sec", "seal-c9", "c9", "c9-r1")
        self.assertTrue(seal["passed"])
        with self.assertRaises(InvalidState):
            self.service.accept_text("sec", "c9-r1", "P2")
        self.service.withdraw_reservation("delb", "res-c9", "授权已到位")
        self.assertEqual(self.service.accept_text("sec", "c9-r1", "P2")["state"], "in_force")


if __name__ == "__main__":
    unittest.main()

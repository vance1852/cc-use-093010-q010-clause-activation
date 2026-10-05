from __future__ import annotations

import unittest
from pathlib import Path

from clause_tracking.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class ClauseAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertTrue(result["seal_passed"])
        self.assertEqual(result["seal_support_count"], 3)
        self.assertEqual(result["seal_reservation_count"], 1)
        # 并发/重复封存只能成功一次；冲突修订不能同时合并。
        self.assertEqual(result["repeat_seal_error"], "Conflict")
        self.assertEqual(result["conflict_seal_error"], "InvalidState")
        # 阶段区分：条件未齐时 BR 仅已接受，CN 已生效。
        self.assertEqual(result["br_state_at_1030"], "accepted")
        self.assertEqual(result["cn_state_at_1030"], "in_force")
        self.assertEqual(result["br_state_after"], "in_force")
        # 条件完成可重放；逾期行动可反查条款、授权、封存。
        self.assertEqual(result["precondition_replay_status"], "satisfied")
        self.assertEqual(result["overdue_count_at_day20"], 2)
        self.assertEqual(result["overdue_traces_clause"], "DATA-1")
        self.assertEqual(result["overdue_traces_authorization_delegate"], "del-a")
        self.assertEqual(result["overdue_traces_seal"], "seal-a1")
        # 撤销留痕、版本链与事件依据完整。
        self.assertEqual(result["revoked_follow_up"], "revoked")
        self.assertEqual(result["chain_revision_count"], 3)
        self.assertEqual(result["remaining_pending_conditions"], 0)
        self.assertGreater(result["event_count"], 10)
        self.assertEqual(result["replay_basis_sha256_len"], 64)


if __name__ == "__main__":
    unittest.main()

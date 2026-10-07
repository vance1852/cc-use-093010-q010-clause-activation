from __future__ import annotations

import unittest
from pathlib import Path

from clause_tracking.acceptance import run as run_clause_tracking
from cooperation_assurance.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation_count"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["input_sha256"]), 64)

    def test_clause_tracking_acceptance(self) -> None:
        result = run_clause_tracking(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["head_version_no"], 4)
        self.assertEqual(result["support_count"], 3)
        self.assertEqual(result["sealed_vote"]["support_count"], 3)
        self.assertEqual(result["in_force_clauses"], ["data-flow", "tariff-facilitation"])
        self.assertEqual(result["condition_event_types"], ["fulfill", "revoke", "fulfill"])
        self.assertEqual(result["overdue_actions"], ["act-1"])
        self.assertEqual(result["overdue_conditions"], ["cond-tariff-in"])
        self.assertEqual(result["schema"]["missing_tables"], [])


if __name__ == "__main__":
    unittest.main()

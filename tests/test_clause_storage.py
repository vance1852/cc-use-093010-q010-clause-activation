from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from clause_tracking.storage import connect, initialize, inspect_schema, transaction


class ClauseStorageTests(unittest.TestCase):
    def test_initialize_is_repeatable(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            initialize(connection)
            initialize(connection)
            summary = inspect_schema(connection)
        finally:
            connection.close()
        self.assertEqual(summary["missing_tables"], [])
        self.assertEqual(summary["schema_version"], "1")

    def test_transaction_rolls_back_on_error(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.execute("CREATE TABLE items(value TEXT NOT NULL)")
        with self.assertRaises(RuntimeError):
            with transaction(connection):
                connection.execute("INSERT INTO items(value) VALUES('x')")
                raise RuntimeError("stop")
        count = connection.execute("SELECT count(*) FROM items").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_connect_enables_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "test.sqlite3")
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                connection.close()

    def test_seal_unique_per_clause_revision(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        initialize(connection)
        connection.execute(
            "INSERT INTO dialogues(dialogue_id,title,created_at) VALUES('d','t','2026-10-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO participants(participant_id,display_name,created_at) "
            "VALUES('p','一方','2026-10-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO users(user_id,display_name,role) VALUES('s','秘书处','secretariat')"
        )
        connection.execute(
            "INSERT INTO clauses(clause_id,dialogue_id,clause_code,title,created_by,created_at) "
            "VALUES('c','d','C','条款','s','2026-10-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO clause_revisions(revision_id,clause_id,revision_no,kind,language,title,body,"
            "content_sha256,created_by,created_at) VALUES('r','c',1,'baseline','zh','t','b',?,"
            "'s','2026-10-01T00:00:00Z')",
            ("a" * 64,),
        )
        ballot = '{"x":1}'
        common = (
            "c", "r", "s", "2026-10-02T00:00:00Z", ballot, 1, 1, 0, "majority", 1,
        )
        connection.execute(
            "INSERT INTO clause_seals(seal_id,clause_id,revision_id,sealed_by,sealed_at,ballot_json,"
            "participation,support_count,reservation_count,threshold,passed) "
            "VALUES('s1',?,?,?,?,?,?,?,?,?,?)", common,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO clause_seals(seal_id,clause_id,revision_id,sealed_by,sealed_at,ballot_json,"
                "participation,support_count,reservation_count,threshold,passed) "
                "VALUES('s2',?,?,?,?,?,?,?,?,?,?)", common,
            )
        connection.close()


if __name__ == "__main__":
    unittest.main()

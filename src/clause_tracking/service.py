"""条款协商与生效跟踪的领域用例。

版本链：提案基线 -> 修订合并 -> 文本整理，每一步都留下不可变事实；
签署、保留、前置条件、表决与生效声明分别记录，秘书处可以按任一时点
重放并说明文本对各参与方的约束。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from .clock import SystemClock, isoformat
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "secretariat": {
        "instrument.create", "participant.add",
        "authorization.grant", "authorization.revoke",
        "version.baseline", "version.editorial",
        "revision.merge", "revision.withdraw",
        "translation.register",
        "condition.define", "condition.revoke",
        "action.define", "action.cancel",
        "vote.open", "vote.seal",
        "clause.declare_force",
        "report.read", "audit.read",
    },
    "delegate": {
        "revision.propose", "revision.withdraw",
        "acceptance.sign",
        "reservation.declare", "reservation.withdraw",
        "vote.cast",
        "condition.fulfill", "action.complete",
    },
    "auditor": {"report.read", "audit.read"},
}

CAPABILITIES = ("revise", "vote", "sign", "reserve", "fulfill")

# 代表执行各类操作时需要持有的授权能力。
ACTION_CAPABILITY = {
    "revision.propose": "revise",
    "acceptance.sign": "sign",
    "reservation.declare": "reserve",
    "reservation.withdraw": "reserve",
    "vote.cast": "vote",
    "condition.fulfill": "fulfill",
    "action.complete": "fulfill",
}


def _clause_digest(clause_id: str, title: str, body: str) -> str:
    return content_digest([{"clause_id": clause_id, "title": title, "body": body}])


def _version_digest(clauses: list[Mapping[str, Any]]) -> str:
    ordered = sorted(clauses, key=lambda item: item["clause_id"])
    return content_digest([
        {
            "clause_id": clause["clause_id"],
            "clause_version": clause["clause_version"],
            "title": clause["title"],
            "body": clause["body"],
        }
        for clause in ordered
    ])


class ClauseTrackingService:
    """在单个 SQLite 连接上提供条款协商与生效跟踪的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---- 基础辅助 -------------------------------------------------

    def _now(self) -> str:
        return isoformat(self.clock.now())

    @staticmethod
    def _parse_time(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 必须是 ISO 时间字符串")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailed(f"{field} 不是有效 ISO 时间: {value}") from exc
        if parsed.tzinfo is None:
            raise ValidationFailed(f"{field} 必须带时区")
        return isoformat(parsed)

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _instrument(self, instrument_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM instruments WHERE instrument_id=?", (instrument_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"合作文件不存在: {instrument_id}")
        return row

    def _participant(self, instrument_id: str, participant_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM participants WHERE instrument_id=? AND participant_id=?",
            (instrument_id, participant_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"参与方不存在: {participant_id}")
        return row

    def _check_authorization(
        self, instrument_id: str, participant_id: str, delegate_id: str, capability: str
    ) -> sqlite3.Row:
        """代表必须持有参与方覆盖当前时点、覆盖所需能力的有效授权。"""

        now = self._now()
        row = self.connection.execute(
            "SELECT a.* FROM authorizations a "
            "JOIN authorization_capabilities c ON c.authorization_id=a.authorization_id "
            "WHERE a.instrument_id=? AND a.participant_id=? AND a.delegate_id=? "
            "AND c.capability=? AND a.revoked_at IS NULL AND a.valid_from<=? AND a.valid_until>? "
            "ORDER BY a.created_at DESC, a.authorization_id DESC LIMIT 1",
            (instrument_id, participant_id, delegate_id, capability, now, now),
        ).fetchone()
        if row is None:
            raise Forbidden(f"代表 {delegate_id} 未持有参与方 {participant_id} 的 {capability} 有效授权")
        return row

    def _audit(
        self,
        instrument_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(instrument_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (instrument_id, entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def _head_version(self, instrument_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM text_versions WHERE instrument_id=? ORDER BY version_no DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        if row is None:
            raise InvalidState("合作文件还没有文本基线")
        return row

    def _clause_map(self, instrument_id: str, version_no: int) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM clause_versions WHERE instrument_id=? AND version_no=?",
            (instrument_id, version_no),
        ).fetchall()
        return {row["clause_id"]: row for row in rows}

    def _condition_state(self, condition_id: str, as_of: str | None = None) -> str:
        if as_of is None:
            row = self.connection.execute(
                "SELECT event_type FROM condition_events WHERE condition_id=? "
                "ORDER BY event_id DESC LIMIT 1",
                (condition_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT event_type FROM condition_events WHERE condition_id=? AND created_at<=? "
                "ORDER BY event_id DESC LIMIT 1",
                (condition_id, as_of),
            ).fetchone()
        if row is not None and row["event_type"] == "fulfill":
            return "fulfilled"
        return "pending"

    def _effective_version_no(self, instrument_id: str, version_no: int, as_of: str) -> int:
        """接受的版本沿纯整理版本链前进：内容不变，约束事实不变。"""

        rows = self.connection.execute(
            "SELECT version_no, origin FROM text_versions "
            "WHERE instrument_id=? AND version_no>? AND created_at<=? ORDER BY version_no",
            (instrument_id, version_no, as_of),
        ).fetchall()
        current = version_no
        for row in rows:
            if row["version_no"] != current + 1 or row["origin"] != "editorial":
                break
            current = row["version_no"]
        return current

    # ---- 用户与目录 -----------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def create_instrument(self, actor_id: str, instrument_id: str, title: str) -> dict[str, Any]:
        self._require(actor_id, "instrument.create")
        if not instrument_id.strip() or not title.strip():
            raise ValidationFailed("合作文件编号和标题不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO instruments(instrument_id,title,created_by,created_at) VALUES(?,?,?,?)",
                    (instrument_id.strip(), title.strip(), actor_id, self._now()),
                )
                self._audit(instrument_id.strip(), "instrument", instrument_id.strip(),
                            "instrument.created", actor_id, {"title": title.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"合作文件已存在: {instrument_id}") from exc
        return {"instrument_id": instrument_id.strip(), "title": title.strip()}

    def add_participant(
        self, actor_id: str, instrument_id: str, participant_id: str, display_name: str
    ) -> dict[str, Any]:
        self._require(actor_id, "participant.add")
        self._instrument(instrument_id)
        if not participant_id.strip() or not display_name.strip():
            raise ValidationFailed("参与方编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO participants(instrument_id,participant_id,display_name,added_by,added_at) "
                    "VALUES(?,?,?,?,?)",
                    (instrument_id, participant_id.strip(), display_name.strip(), actor_id, self._now()),
                )
                self._audit(instrument_id, "participant", participant_id.strip(),
                            "participant.added", actor_id, {"display_name": display_name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"参与方已存在: {participant_id}") from exc
        return {"instrument_id": instrument_id, "participant_id": participant_id.strip()}

    # ---- 代表授权 ---------------------------------------------------

    def grant_authorization(
        self,
        actor_id: str,
        authorization_id: str,
        instrument_id: str,
        participant_id: str,
        delegate_id: str,
        capabilities: list[str],
        valid_from: str,
        valid_until: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "authorization.grant")
        self._instrument(instrument_id)
        self._participant(instrument_id, participant_id)
        delegate = self._user(delegate_id)
        if delegate["role"] != "delegate":
            raise ValidationFailed("被授权代表必须是 delegate 角色")
        caps = sorted(set(capabilities))
        if not caps or any(cap not in CAPABILITIES for cap in caps):
            raise ValidationFailed("授权能力必须是 revise/vote/sign/reserve/fulfill 的组合")
        start = self._parse_time(valid_from, "valid_from")
        end = self._parse_time(valid_until, "valid_until")
        if not start < end:
            raise ValidationFailed("授权有效期必须满足 valid_from < valid_until")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO authorizations(authorization_id,instrument_id,participant_id,delegate_id,"
                    "valid_from,valid_until,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (authorization_id, instrument_id, participant_id, delegate_id,
                     start, end, actor_id, self._now()),
                )
                for capability in caps:
                    self.connection.execute(
                        "INSERT INTO authorization_capabilities(authorization_id,capability) VALUES(?,?)",
                        (authorization_id, capability),
                    )
                self._audit(instrument_id, "authorization", authorization_id,
                            "authorization.granted", actor_id,
                            {"participant_id": participant_id, "delegate_id": delegate_id,
                             "capabilities": caps, "valid_from": start, "valid_until": end})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"授权编号冲突: {authorization_id}") from exc
        return self.get_authorization(authorization_id)

    def get_authorization(self, authorization_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE authorization_id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"授权不存在: {authorization_id}")
        caps = self.connection.execute(
            "SELECT capability FROM authorization_capabilities WHERE authorization_id=? ORDER BY capability",
            (authorization_id,),
        ).fetchall()
        return dict(row) | {"capabilities": [cap["capability"] for cap in caps]}

    def revoke_authorization(self, actor_id: str, authorization_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "authorization.revoke")
        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE authorization_id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"授权不存在: {authorization_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE authorizations SET revoked_at=? WHERE authorization_id=? AND revoked_at IS NULL",
                (self._now(), authorization_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("授权已经撤销")
            self._audit(row["instrument_id"], "authorization", authorization_id,
                        "authorization.revoked", actor_id, {"reason": reason})
        return self.get_authorization(authorization_id)

    # ---- 文本版本链 -------------------------------------------------

    @staticmethod
    def _parse_clauses(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValidationFailed("条款数组不能为空")
        seen: set[str] = set()
        parsed: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("条款必须是对象")
            clause_id = str(item.get("clause_id", "")).strip()
            title = str(item.get("title", "")).strip()
            body = str(item.get("body", "")).strip()
            if not clause_id or not title or not body:
                raise ValidationFailed("条款编号、标题和正文不能为空")
            if clause_id in seen:
                raise ValidationFailed(f"条款编号重复: {clause_id}")
            seen.add(clause_id)
            parsed.append({"clause_id": clause_id, "title": title, "body": body})
        return parsed

    def _insert_version(
        self,
        instrument_id: str,
        origin: str,
        clauses: list[Mapping[str, Any]],
        actor_id: str,
        note: str,
        origin_revision_id: str | None = None,
    ) -> dict[str, Any]:
        head = self.connection.execute(
            "SELECT version_no FROM text_versions WHERE instrument_id=? ORDER BY version_no DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        version_no = 1 if head is None else head["version_no"] + 1
        parent = None if head is None else head["version_no"]
        digest = _version_digest(clauses)
        self.connection.execute(
            "INSERT INTO text_versions(instrument_id,version_no,parent_version_no,origin,origin_revision_id,"
            "note,content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (instrument_id, version_no, parent, origin, origin_revision_id,
             note, digest, actor_id, self._now()),
        )
        for clause in clauses:
            self.connection.execute(
                "INSERT INTO clause_versions(instrument_id,version_no,clause_id,clause_version,title,body,clause_sha256) "
                "VALUES(?,?,?,?,?,?,?)",
                (instrument_id, version_no, clause["clause_id"], clause["clause_version"],
                 clause["title"], clause["body"],
                 _clause_digest(clause["clause_id"], clause["title"], clause["body"])),
            )
        return self._version_view(instrument_id, version_no)

    def _version_view(self, instrument_id: str, version_no: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM text_versions WHERE instrument_id=? AND version_no=?",
            (instrument_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound(f"文本版本不存在: {version_no}")
        clauses = self.connection.execute(
            "SELECT clause_id,clause_version,title,body,clause_sha256 FROM clause_versions "
            "WHERE instrument_id=? AND version_no=? ORDER BY clause_id",
            (instrument_id, version_no),
        ).fetchall()
        return dict(row) | {"clauses": [dict(clause) for clause in clauses]}

    def create_baseline(
        self, actor_id: str, instrument_id: str, clauses: Any, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "version.baseline")
        self._instrument(instrument_id)
        parsed = self._parse_clauses(clauses)
        for clause in parsed:
            clause["clause_version"] = 1
        try:
            with transaction(self.connection, immediate=True):
                view = self._insert_version(instrument_id, "baseline", parsed, actor_id, note)
                self._audit(instrument_id, "instrument", instrument_id, "version.baselined", actor_id,
                            {"version_no": 1, "content_sha256": view["content_sha256"],
                             "clauses": [clause["clause_id"] for clause in parsed]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("合作文件已存在文本基线") from exc
        return view

    def propose_revision(
        self,
        actor_id: str,
        revision_id: str,
        instrument_id: str,
        participant_id: str,
        changes: Any,
        note: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "revision.propose")
        self._instrument(instrument_id)
        self._participant(instrument_id, participant_id)
        authorization = self._check_authorization(instrument_id, participant_id, actor_id, "revise")
        try:
            with transaction(self.connection, immediate=True):
                head = self._head_version(instrument_id)
                base_clauses = self._clause_map(instrument_id, head["version_no"])
                parsed = self._parse_changes(changes, base_clauses)
                self.connection.execute(
                    "INSERT INTO revisions(revision_id,instrument_id,base_version_no,state,participant_id,"
                    "proposed_by,authorization_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (revision_id, instrument_id, head["version_no"], "open", participant_id,
                     actor_id, authorization["authorization_id"], note, self._now()),
                )
                for change in parsed:
                    self.connection.execute(
                        "INSERT INTO revision_changes(revision_id,clause_id,change_type,title,body) "
                        "VALUES(?,?,?,?,?)",
                        (revision_id, change["clause_id"], change["change_type"],
                         change.get("title"), change.get("body")),
                    )
                self._audit(instrument_id, "revision", revision_id, "revision.proposed", actor_id,
                            {"participant_id": participant_id, "base_version_no": head["version_no"],
                             "authorization_id": authorization["authorization_id"],
                             "changes": [c["clause_id"] for c in parsed]})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"修订编号冲突: {revision_id}") from exc
        return self.get_revision(revision_id)

    @staticmethod
    def _parse_changes(raw: Any, base_clauses: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValidationFailed("修订变更数组不能为空")
        seen: set[str] = set()
        parsed: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("修订变更必须是对象")
            clause_id = str(item.get("clause_id", "")).strip()
            change_type = str(item.get("change_type", "")).strip()
            title = str(item.get("title", "")).strip()
            body = str(item.get("body", "")).strip()
            if not clause_id:
                raise ValidationFailed("修订变更缺少条款编号")
            if clause_id in seen:
                raise ValidationFailed(f"同一修订不能重复变更条款: {clause_id}")
            if change_type not in {"add", "amend", "remove"}:
                raise ValidationFailed(f"未知变更类型: {change_type}")
            if change_type == "add" and clause_id in base_clauses:
                raise ValidationFailed(f"条款已存在，不能新增: {clause_id}")
            if change_type in {"amend", "remove"} and clause_id not in base_clauses:
                raise ValidationFailed(f"条款不存在于基线版本: {clause_id}")
            if change_type in {"add", "amend"} and (not title or not body):
                raise ValidationFailed("新增或修改条款必须提供标题和正文")
            seen.add(clause_id)
            parsed.append({"clause_id": clause_id, "change_type": change_type,
                           "title": title or None, "body": body or None})
        return parsed

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"修订不存在: {revision_id}")
        changes = self.connection.execute(
            "SELECT clause_id,change_type,title,body FROM revision_changes WHERE revision_id=? ORDER BY clause_id",
            (revision_id,),
        ).fetchall()
        return dict(row) | {"changes": [dict(change) for change in changes]}

    def merge_revision(self, actor_id: str, revision_id: str) -> dict[str, Any]:
        """合并修订；与已合并修订冲突的修订不能同时合并。"""

        self._require(actor_id, "revision.merge")
        revision = self.get_revision(revision_id)
        instrument_id = revision["instrument_id"]
        with transaction(self.connection, immediate=True):
            current = self.connection.execute(
                "SELECT state FROM revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if current["state"] != "open":
                raise InvalidState("修订已合并或已撤回")
            head = self._head_version(instrument_id)
            base_clauses = self._clause_map(instrument_id, revision["base_version_no"])
            head_clauses = self._clause_map(instrument_id, head["version_no"])

            def touched_version(clauses: Mapping[str, sqlite3.Row], clause_id: str) -> int | None:
                row = clauses.get(clause_id)
                return None if row is None else row["clause_version"]

            conflicting = sorted(
                change["clause_id"]
                for change in revision["changes"]
                if touched_version(base_clauses, change["clause_id"])
                != touched_version(head_clauses, change["clause_id"])
            )
            if conflicting:
                raise Conflict("修订与已合并的修订冲突，需要重新基于当前文本: " + ", ".join(conflicting))
            new_clauses = [
                {"clause_id": clause_id, "clause_version": row["clause_version"],
                 "title": row["title"], "body": row["body"]}
                for clause_id, row in head_clauses.items()
            ]
            for change in revision["changes"]:
                if change["change_type"] == "add":
                    new_clauses.append({"clause_id": change["clause_id"], "clause_version": 1,
                                        "title": change["title"], "body": change["body"]})
                elif change["change_type"] == "amend":
                    for clause in new_clauses:
                        if clause["clause_id"] == change["clause_id"]:
                            clause["clause_version"] += 1
                            clause["title"] = change["title"]
                            clause["body"] = change["body"]
                else:
                    new_clauses = [c for c in new_clauses if c["clause_id"] != change["clause_id"]]
            view = self._insert_version(instrument_id, "merge", new_clauses, actor_id,
                                        f"合并修订 {revision_id}", origin_revision_id=revision_id)
            self.connection.execute(
                "UPDATE revisions SET state='merged',merged_into_version_no=?,merged_at=? WHERE revision_id=?",
                (view["version_no"], self._now(), revision_id),
            )
            self._audit(instrument_id, "revision", revision_id, "revision.merged", actor_id,
                        {"merged_into_version_no": view["version_no"],
                         "content_sha256": view["content_sha256"]})
        return view

    def withdraw_revision(self, actor_id: str, revision_id: str, reason: str) -> dict[str, Any]:
        revision = self.get_revision(revision_id)
        if revision["state"] != "open":
            raise InvalidState("修订已合并或已撤回")
        user = self._user(actor_id)
        if user["role"] == "secretariat":
            self._require(actor_id, "revision.withdraw")
        else:
            self._require(actor_id, "revision.withdraw")
            if revision["proposed_by"] != actor_id:
                raise Forbidden("只有提案代表或秘书处可以撤回修订")
            self._check_authorization(revision["instrument_id"], revision["participant_id"],
                                      actor_id, "revise")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE revisions SET state='withdrawn' WHERE revision_id=? AND state='open'",
                (revision_id,),
            )
            self._audit(revision["instrument_id"], "revision", revision_id,
                        "revision.withdrawn", actor_id, {"reason": reason})
        return self.get_revision(revision_id)

    def create_editorial_version(
        self, actor_id: str, instrument_id: str, note: str
    ) -> dict[str, Any]:
        """整理文本：逐条保留条款内容，不改变已封存的表决与约束事实。"""

        self._require(actor_id, "version.editorial")
        self._instrument(instrument_id)
        if not note.strip():
            raise ValidationFailed("整理版本必须说明整理内容")
        with transaction(self.connection, immediate=True):
            head = self._head_version(instrument_id)
            head_clauses = self._clause_map(instrument_id, head["version_no"])
            clauses = [
                {"clause_id": clause_id, "clause_version": row["clause_version"],
                 "title": row["title"], "body": row["body"]}
                for clause_id, row in head_clauses.items()
            ]
            view = self._insert_version(instrument_id, "editorial", clauses, actor_id, note.strip())
            self._audit(instrument_id, "instrument", instrument_id, "version.reorganized", actor_id,
                        {"version_no": view["version_no"], "from_version_no": head["version_no"],
                         "content_sha256": view["content_sha256"], "note": note.strip()})
        return view

    # ---- 翻译对应 ---------------------------------------------------

    def register_translation(
        self,
        actor_id: str,
        translation_id: str,
        instrument_id: str,
        clause_id: str,
        clause_sha256: str,
        language: str,
        text: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "translation.register")
        self._instrument(instrument_id)
        if not language.strip() or not text.strip():
            raise ValidationFailed("译文语言和正文不能为空")
        if len(clause_sha256) != 64:
            raise ValidationFailed("译文对应的原文摘要必须是 64 位 SHA-256")
        source = self.connection.execute(
            "SELECT 1 FROM clause_versions WHERE instrument_id=? AND clause_id=? AND clause_sha256=? LIMIT 1",
            (instrument_id, clause_id, clause_sha256.lower()),
        ).fetchone()
        if source is None:
            raise ValidationFailed("译文对应的原文摘要不存在于任何文本版本")
        digest = content_digest([{"clause_sha256": clause_sha256.lower(),
                                  "language": language.strip(), "text": text}])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO translations(translation_id,instrument_id,clause_id,clause_sha256,language,"
                    "text,translation_sha256,registered_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (translation_id, instrument_id, clause_id, clause_sha256.lower(),
                     language.strip(), text, digest, actor_id, self._now()),
                )
                self._audit(instrument_id, "translation", translation_id, "translation.registered",
                            actor_id, {"clause_id": clause_id, "clause_sha256": clause_sha256.lower(),
                                       "language": language.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一原文摘要的该语言译文已登记") from exc
        return {"translation_id": translation_id, "clause_id": clause_id,
                "language": language.strip(), "translation_sha256": digest}

    def translation_status(self, instrument_id: str, version_no: int | None = None) -> dict[str, Any]:
        self._instrument(instrument_id)
        if version_no is None:
            version_no = self._head_version(instrument_id)["version_no"]
        clauses = self._clause_map(instrument_id, version_no)
        if not clauses:
            raise NotFound(f"文本版本不存在: {version_no}")
        rows = self.connection.execute(
            "SELECT * FROM translations WHERE instrument_id=? ORDER BY clause_id,language,created_at",
            (instrument_id,),
        ).fetchall()
        entries = []
        for clause_id in sorted(clauses):
            clause = clauses[clause_id]
            translations = [
                {"translation_id": row["translation_id"], "language": row["language"],
                 "clause_sha256": row["clause_sha256"],
                 "current": row["clause_sha256"] == clause["clause_sha256"],
                 "created_at": row["created_at"]}
                for row in rows
                if row["clause_id"] == clause_id
            ]
            entries.append({"clause_id": clause_id, "clause_version": clause["clause_version"],
                            "clause_sha256": clause["clause_sha256"], "translations": translations})
        return {"instrument_id": instrument_id, "version_no": version_no, "clauses": entries}

    # ---- 签署接受 ---------------------------------------------------

    def sign_acceptance(
        self, actor_id: str, instrument_id: str, participant_id: str
    ) -> dict[str, Any]:
        """代表签署接受当前文本；重复签署只追加事实，不增加支持数。"""

        self._require(actor_id, "acceptance.sign")
        self._instrument(instrument_id)
        self._participant(instrument_id, participant_id)
        authorization = self._check_authorization(instrument_id, participant_id, actor_id, "sign")
        with transaction(self.connection, immediate=True):
            head = self._head_version(instrument_id)
            self.connection.execute(
                "INSERT INTO acceptance_events(instrument_id,participant_id,version_no,authorization_id,"
                "signed_by,signed_at) VALUES(?,?,?,?,?,?)",
                (instrument_id, participant_id, head["version_no"],
                 authorization["authorization_id"], actor_id, self._now()),
            )
            self._audit(instrument_id, "participant", participant_id, "acceptance.signed", actor_id,
                        {"version_no": head["version_no"],
                         "authorization_id": authorization["authorization_id"]})
            version_no = head["version_no"]
        support_count = self.connection.execute(
            "SELECT count(DISTINCT participant_id) FROM acceptance_events WHERE instrument_id=?",
            (instrument_id,),
        ).fetchone()[0]
        return {"instrument_id": instrument_id, "participant_id": participant_id,
                "version_no": version_no, "support_count": support_count}

    # ---- 保留意见 ---------------------------------------------------

    def declare_reservation(
        self, actor_id: str, instrument_id: str, participant_id: str, clause_id: str, statement: str
    ) -> dict[str, Any]:
        self._require(actor_id, "reservation.declare")
        self._instrument(instrument_id)
        self._participant(instrument_id, participant_id)
        if not statement.strip():
            raise ValidationFailed("保留意见内容不能为空")
        authorization = self._check_authorization(instrument_id, participant_id, actor_id, "reserve")
        try:
            with transaction(self.connection, immediate=True):
                head = self._head_version(instrument_id)
                if clause_id not in self._clause_map(instrument_id, head["version_no"]):
                    raise NotFound(f"条款不存在于当前文本版本: {clause_id}")
                cursor = self.connection.execute(
                    "INSERT INTO reservations(instrument_id,participant_id,clause_id,statement,"
                    "authorization_id,declared_by,declared_at) VALUES(?,?,?,?,?,?,?)",
                    (instrument_id, participant_id, clause_id, statement.strip(),
                     authorization["authorization_id"], actor_id, self._now()),
                )
                self._audit(instrument_id, "reservation", str(cursor.lastrowid),
                            "reservation.declared", actor_id,
                            {"participant_id": participant_id, "clause_id": clause_id,
                             "authorization_id": authorization["authorization_id"]})
                reservation_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise Conflict("该参与方已对此条款存在有效保留") from exc
        return self.get_reservation(reservation_id)

    def get_reservation(self, reservation_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"保留意见不存在: {reservation_id}")
        return dict(row)

    def withdraw_reservation(self, actor_id: str, reservation_id: int) -> dict[str, Any]:
        self._require(actor_id, "reservation.withdraw")
        row = self.get_reservation(reservation_id)
        if row["withdrawn_at"] is not None:
            raise InvalidState("保留意见已经撤回")
        authorization = self._check_authorization(
            row["instrument_id"], row["participant_id"], actor_id, "reserve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE reservations SET withdrawn_by=?,withdrawn_at=?,withdrawal_authorization_id=? "
                "WHERE reservation_id=? AND withdrawn_at IS NULL",
                (actor_id, self._now(), authorization["authorization_id"], reservation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("保留意见已经撤回")
            self._audit(row["instrument_id"], "reservation", str(reservation_id),
                        "reservation.withdrawn", actor_id,
                        {"participant_id": row["participant_id"], "clause_id": row["clause_id"],
                         "authorization_id": authorization["authorization_id"]})
        return self.get_reservation(reservation_id)

    # ---- 前置条件 ---------------------------------------------------

    def define_condition(
        self,
        actor_id: str,
        condition_id: str,
        instrument_id: str,
        clause_id: str,
        description: str,
        applies_to: str,
        owner_participant_id: str,
        due_at: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "condition.define")
        self._instrument(instrument_id)
        self._participant(instrument_id, owner_participant_id)
        if applies_to != "*":
            self._participant(instrument_id, applies_to)
        if not description.strip():
            raise ValidationFailed("前置条件描述不能为空")
        head = self._head_version(instrument_id)
        if clause_id not in self._clause_map(instrument_id, head["version_no"]):
            raise NotFound(f"条款不存在于当前文本版本: {clause_id}")
        due = self._parse_time(due_at, "due_at")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO conditions(condition_id,instrument_id,clause_id,description,applies_to,"
                    "owner_participant_id,due_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (condition_id, instrument_id, clause_id, description.strip(), applies_to,
                     owner_participant_id, due, actor_id, self._now()),
                )
                self._audit(instrument_id, "condition", condition_id, "condition.defined", actor_id,
                            {"clause_id": clause_id, "applies_to": applies_to,
                             "owner_participant_id": owner_participant_id, "due_at": due})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"前置条件编号冲突: {condition_id}") from exc
        return self.get_condition(condition_id)

    def _condition_row(self, condition_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM conditions WHERE condition_id=?", (condition_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"前置条件不存在: {condition_id}")
        return row

    def get_condition(self, condition_id: str) -> dict[str, Any]:
        row = self._condition_row(condition_id)
        events = self.connection.execute(
            "SELECT event_id,event_type,evidence_ref,note,actor_id,authorization_id,created_at "
            "FROM condition_events WHERE condition_id=? ORDER BY event_id",
            (condition_id,),
        ).fetchall()
        return dict(row) | {
            "state": self._condition_state(condition_id),
            "events": [dict(event) for event in events],
        }

    def fulfill_condition(
        self, actor_id: str, condition_id: str, evidence_ref: str, note: str = ""
    ) -> dict[str, Any]:
        """记录条件完成；必须附可重放证据引用。"""

        self._require(actor_id, "condition.fulfill")
        condition = self._condition_row(condition_id)
        if not evidence_ref.strip():
            raise ValidationFailed("条件完成必须附可重放证据引用")
        authorization = self._check_authorization(
            condition["instrument_id"], condition["owner_participant_id"], actor_id, "fulfill")
        with transaction(self.connection, immediate=True):
            if self._condition_state(condition_id) == "fulfilled":
                raise InvalidState("条件已处于满足状态")
            self.connection.execute(
                "INSERT INTO condition_events(condition_id,event_type,evidence_ref,note,actor_id,"
                "authorization_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (condition_id, "fulfill", evidence_ref.strip(), note, actor_id,
                 authorization["authorization_id"], self._now()),
            )
            self._audit(condition["instrument_id"], "condition", condition_id,
                        "condition.fulfilled", actor_id,
                        {"clause_id": condition["clause_id"], "evidence_ref": evidence_ref.strip(),
                         "authorization_id": authorization["authorization_id"]})
        return self.get_condition(condition_id)

    def revoke_condition(
        self, actor_id: str, condition_id: str, evidence_ref: str, note: str = ""
    ) -> dict[str, Any]:
        """撤销条件完成记录；撤销同样留下可重放依据。"""

        self._require(actor_id, "condition.revoke")
        condition = self._condition_row(condition_id)
        if not evidence_ref.strip():
            raise ValidationFailed("撤销条件完成必须附可重放依据")
        with transaction(self.connection, immediate=True):
            if self._condition_state(condition_id) != "fulfilled":
                raise InvalidState("条件尚未满足，不能撤销")
            self.connection.execute(
                "INSERT INTO condition_events(condition_id,event_type,evidence_ref,note,actor_id,"
                "authorization_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (condition_id, "revoke", evidence_ref.strip(), note, actor_id, None, self._now()),
            )
            self._audit(condition["instrument_id"], "condition", condition_id,
                        "condition.revoked", actor_id,
                        {"clause_id": condition["clause_id"], "evidence_ref": evidence_ref.strip()})
        return self.get_condition(condition_id)

    # ---- 条款生效 ---------------------------------------------------

    def declare_clause_in_force(
        self, actor_id: str, instrument_id: str, clause_id: str
    ) -> dict[str, Any]:
        """独立条款分别生效：要求该条款的全局前置条件均已满足。"""

        self._require(actor_id, "clause.declare_force")
        self._instrument(instrument_id)
        with transaction(self.connection, immediate=True):
            head = self._head_version(instrument_id)
            clause = self._clause_map(instrument_id, head["version_no"]).get(clause_id)
            if clause is None:
                raise NotFound(f"条款不存在于当前文本版本: {clause_id}")
            pending = sorted(
                row["condition_id"]
                for row in self.connection.execute(
                    "SELECT condition_id FROM conditions WHERE instrument_id=? AND clause_id=? AND applies_to='*'",
                    (instrument_id, clause_id),
                ).fetchall()
                if self._condition_state(row["condition_id"]) != "fulfilled"
            )
            if pending:
                raise InvalidState("仍存在未满足的全局前置条件: " + ", ".join(pending))
            try:
                self.connection.execute(
                    "INSERT INTO clause_force(instrument_id,clause_id,clause_sha256,declared_by,declared_at) "
                    "VALUES(?,?,?,?,?)",
                    (instrument_id, clause_id, clause["clause_sha256"], actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"条款已声明生效: {clause_id}") from exc
            self._audit(instrument_id, "clause", clause_id, "clause.in_force", actor_id,
                        {"clause_sha256": clause["clause_sha256"],
                         "version_no": head["version_no"]})
        return {"instrument_id": instrument_id, "clause_id": clause_id,
                "clause_sha256": clause["clause_sha256"], "declared_at": self._now()}

    # ---- 表决 -------------------------------------------------------

    def open_vote_round(
        self, actor_id: str, vote_round_id: str, instrument_id: str, version_no: int
    ) -> dict[str, Any]:
        self._require(actor_id, "vote.open")
        self._instrument(instrument_id)
        self._version_view(instrument_id, version_no)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO vote_rounds(vote_round_id,instrument_id,version_no,state,opened_by,opened_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (vote_round_id, instrument_id, version_no, "open", actor_id, self._now()),
                )
                self._audit(instrument_id, "vote_round", vote_round_id, "vote.opened", actor_id,
                            {"version_no": version_no})
        except sqlite3.IntegrityError as exc:
            raise Conflict("表决轮次编号冲突或该版本已有表决") from exc
        return self.get_vote_round(vote_round_id)

    def get_vote_round(self, vote_round_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM vote_rounds WHERE vote_round_id=?", (vote_round_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"表决轮次不存在: {vote_round_id}")
        ballots = self.connection.execute(
            "SELECT participant_id,choice,authorization_id,cast_by,cast_at FROM ballots "
            "WHERE vote_round_id=? ORDER BY participant_id",
            (vote_round_id,),
        ).fetchall()
        return dict(row) | {"ballots": [dict(ballot) for ballot in ballots]}

    def cast_ballot(
        self, actor_id: str, vote_round_id: str, participant_id: str, choice: str
    ) -> dict[str, Any]:
        self._require(actor_id, "vote.cast")
        if choice not in {"support", "object", "abstain"}:
            raise ValidationFailed(f"未知表决选择: {choice}")
        round_row = self.get_vote_round(vote_round_id)
        self._participant(round_row["instrument_id"], participant_id)
        authorization = self._check_authorization(
            round_row["instrument_id"], participant_id, actor_id, "vote")
        with transaction(self.connection, immediate=True):
            state = self.connection.execute(
                "SELECT state FROM vote_rounds WHERE vote_round_id=?", (vote_round_id,)
            ).fetchone()["state"]
            if state != "open":
                raise InvalidState("表决轮次已封存，不能再投票")
            self.connection.execute(
                "INSERT INTO ballots(vote_round_id,participant_id,choice,authorization_id,cast_by,cast_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(vote_round_id,participant_id) DO UPDATE SET choice=excluded.choice,"
                "authorization_id=excluded.authorization_id,cast_by=excluded.cast_by,cast_at=excluded.cast_at",
                (vote_round_id, participant_id, choice, authorization["authorization_id"],
                 actor_id, self._now()),
            )
            self._audit(round_row["instrument_id"], "vote_round", vote_round_id,
                        "vote.cast", actor_id,
                        {"participant_id": participant_id, "choice": choice,
                         "authorization_id": authorization["authorization_id"]})
        return self.get_vote_round(vote_round_id)

    def seal_vote_round(
        self, actor_id: str, vote_round_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """封存表决事实；基于乐观版本控制，并发封存只能成功一次。"""

        self._require(actor_id, "vote.seal")
        round_row = self.get_vote_round(vote_round_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE vote_rounds SET state='sealed',revision=revision+1,sealed_by=?,sealed_at=?,"
                "support_count=(SELECT count(*) FROM ballots WHERE vote_round_id=? AND choice='support'),"
                "object_count=(SELECT count(*) FROM ballots WHERE vote_round_id=? AND choice='object'),"
                "abstain_count=(SELECT count(*) FROM ballots WHERE vote_round_id=? AND choice='abstain') "
                "WHERE vote_round_id=? AND state='open' AND revision=?",
                (actor_id, self._now(), vote_round_id, vote_round_id, vote_round_id,
                 vote_round_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("表决轮次已封存或版本已变化")
            self._audit(round_row["instrument_id"], "vote_round", vote_round_id,
                        "vote.sealed", actor_id, {"from_revision": expected_revision})
        return self.get_vote_round(vote_round_id)

    # ---- 后续行动 ---------------------------------------------------

    def define_action(
        self,
        actor_id: str,
        action_id: str,
        instrument_id: str,
        clause_id: str,
        description: str,
        owner_participant_id: str,
        due_at: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "action.define")
        self._instrument(instrument_id)
        self._participant(instrument_id, owner_participant_id)
        if not description.strip():
            raise ValidationFailed("后续行动描述不能为空")
        head = self._head_version(instrument_id)
        if clause_id not in self._clause_map(instrument_id, head["version_no"]):
            raise NotFound(f"条款不存在于当前文本版本: {clause_id}")
        due = self._parse_time(due_at, "due_at")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO actions(action_id,instrument_id,clause_id,description,owner_participant_id,"
                    "due_at,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (action_id, instrument_id, clause_id, description.strip(),
                     owner_participant_id, due, "open", actor_id, self._now()),
                )
                self._audit(instrument_id, "action", action_id, "action.defined", actor_id,
                            {"clause_id": clause_id, "owner_participant_id": owner_participant_id,
                             "due_at": due})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"后续行动编号冲突: {action_id}") from exc
        return self.get_action(action_id)

    def get_action(self, action_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"后续行动不存在: {action_id}")
        return dict(row)

    def complete_action(
        self, actor_id: str, action_id: str, evidence_ref: str
    ) -> dict[str, Any]:
        self._require(actor_id, "action.complete")
        action = self.get_action(action_id)
        if not evidence_ref.strip():
            raise ValidationFailed("行动完成必须附证据引用")
        authorization = self._check_authorization(
            action["instrument_id"], action["owner_participant_id"], actor_id, "fulfill")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE actions SET state='completed',completed_by=?,completed_at=?,"
                "completion_evidence_ref=?,completion_authorization_id=? "
                "WHERE action_id=? AND state='open'",
                (actor_id, self._now(), evidence_ref.strip(),
                 authorization["authorization_id"], action_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("行动已关闭")
            self._audit(action["instrument_id"], "action", action_id, "action.completed", actor_id,
                        {"clause_id": action["clause_id"], "evidence_ref": evidence_ref.strip(),
                         "authorization_id": authorization["authorization_id"]})
        return self.get_action(action_id)

    def cancel_action(self, actor_id: str, action_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "action.cancel")
        action = self.get_action(action_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE actions SET state='cancelled',cancelled_by=?,cancelled_at=?,cancel_reason=? "
                "WHERE action_id=? AND state='open'",
                (actor_id, self._now(), reason, action_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("行动已关闭")
            self._audit(action["instrument_id"], "action", action_id, "action.cancelled", actor_id,
                        {"clause_id": action["clause_id"], "reason": reason})
        return self.get_action(action_id)

    # ---- 查询与报告 -------------------------------------------------

    def get_instrument(self, instrument_id: str) -> dict[str, Any]:
        row = self._instrument(instrument_id)
        participants = self.connection.execute(
            "SELECT participant_id,display_name,added_at FROM participants WHERE instrument_id=? "
            "ORDER BY participant_id",
            (instrument_id,),
        ).fetchall()
        head = self.connection.execute(
            "SELECT version_no FROM text_versions WHERE instrument_id=? ORDER BY version_no DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        support_count = self.connection.execute(
            "SELECT count(DISTINCT participant_id) FROM acceptance_events WHERE instrument_id=?",
            (instrument_id,),
        ).fetchone()[0]
        return dict(row) | {
            "participants": [dict(participant) for participant in participants],
            "head_version_no": None if head is None else head["version_no"],
            "support_count": support_count,
        }

    def version_chain(self, instrument_id: str) -> dict[str, Any]:
        self._instrument(instrument_id)
        versions = self.connection.execute(
            "SELECT version_no FROM text_versions WHERE instrument_id=? ORDER BY version_no",
            (instrument_id,),
        ).fetchall()
        revisions = self.connection.execute(
            "SELECT revision_id FROM revisions WHERE instrument_id=? ORDER BY created_at,revision_id",
            (instrument_id,),
        ).fetchall()
        return {
            "instrument_id": instrument_id,
            "versions": [self._version_view(instrument_id, row["version_no"]) for row in versions],
            "revisions": [self.get_revision(row["revision_id"]) for row in revisions],
        }

    def binding_report(
        self, actor_id: str, instrument_id: str, as_of: str | None = None
    ) -> dict[str, Any]:
        """说明任一时点文本对各参与方的约束：接受、条件满足与真正生效分开。"""

        self._require(actor_id, "report.read")
        self._instrument(instrument_id)
        moment = self._parse_time(as_of, "as_of") if as_of else self._now()
        participants = self.connection.execute(
            "SELECT participant_id,display_name FROM participants WHERE instrument_id=? "
            "ORDER BY participant_id",
            (instrument_id,),
        ).fetchall()
        force_rows = {
            row["clause_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM clause_force WHERE instrument_id=? AND declared_at<=?",
                (instrument_id, moment),
            ).fetchall()
        }
        condition_rows = self.connection.execute(
            "SELECT * FROM conditions WHERE instrument_id=? AND created_at<=?",
            (instrument_id, moment),
        ).fetchall()
        reservation_rows = self.connection.execute(
            "SELECT * FROM reservations WHERE instrument_id=? AND declared_at<=? "
            "AND (withdrawn_at IS NULL OR withdrawn_at>?)",
            (instrument_id, moment, moment),
        ).fetchall()
        report_participants = []
        for participant in participants:
            participant_id = participant["participant_id"]
            acceptance = self.connection.execute(
                "SELECT * FROM acceptance_events WHERE instrument_id=? AND participant_id=? "
                "AND signed_at<=? ORDER BY acceptance_id DESC LIMIT 1",
                (instrument_id, participant_id, moment),
            ).fetchone()
            if acceptance is None:
                report_participants.append({
                    "participant_id": participant_id,
                    "accepted": False,
                    "accepted_version_no": None,
                    "effective_version_no": None,
                    "clauses": [],
                })
                continue
            effective = self._effective_version_no(
                instrument_id, acceptance["version_no"], moment)
            clauses = self._clause_map(instrument_id, effective)
            clause_entries = []
            for clause_id in sorted(clauses):
                clause = clauses[clause_id]
                force = force_rows.get(clause_id)
                applicable = [
                    row for row in condition_rows
                    if row["clause_id"] == clause_id
                    and row["applies_to"] in ("*", participant_id)
                ]
                unfulfilled = [
                    row for row in applicable
                    if self._condition_state(row["condition_id"], moment) != "fulfilled"
                ]
                reservation = next(
                    (row for row in reservation_rows
                     if row["clause_id"] == clause_id and row["participant_id"] == participant_id),
                    None,
                )
                if force is None:
                    status = "force_not_declared"
                elif unfulfilled:
                    status = "conditions_pending"
                elif reservation is not None:
                    status = "in_force_with_reservation"
                else:
                    status = "in_force"
                clause_entries.append({
                    "clause_id": clause_id,
                    "clause_version": clause["clause_version"],
                    "title": clause["title"],
                    "status": status,
                    "force_declared_at": None if force is None else force["declared_at"],
                    "unfulfilled_conditions": [
                        {"condition_id": row["condition_id"], "description": row["description"],
                         "owner_participant_id": row["owner_participant_id"], "due_at": row["due_at"]}
                        for row in unfulfilled
                    ],
                    "reservation": None if reservation is None else reservation["statement"],
                })
            report_participants.append({
                "participant_id": participant_id,
                "accepted": True,
                "accepted_version_no": acceptance["version_no"],
                "effective_version_no": effective,
                "signed_at": acceptance["signed_at"],
                "clauses": clause_entries,
            })
        return {
            "instrument_id": instrument_id,
            "as_of": moment,
            "participants": report_participants,
        }

    def overdue_report(
        self, actor_id: str, instrument_id: str, as_of: str | None = None
    ) -> dict[str, Any]:
        """逾期未完成的条件与行动；从行动可反查条款、授权和证据。"""

        self._require(actor_id, "report.read")
        self._instrument(instrument_id)
        moment = self._parse_time(as_of, "as_of") if as_of else self._now()
        overdue_conditions = []
        condition_rows = self.connection.execute(
            "SELECT * FROM conditions WHERE instrument_id=? AND created_at<=? AND due_at<? "
            "ORDER BY condition_id",
            (instrument_id, moment, moment),
        ).fetchall()
        for row in condition_rows:
            if self._condition_state(row["condition_id"], moment) == "pending":
                overdue_conditions.append({
                    "condition_id": row["condition_id"],
                    "clause_id": row["clause_id"],
                    "description": row["description"],
                    "applies_to": row["applies_to"],
                    "owner_participant_id": row["owner_participant_id"],
                    "due_at": row["due_at"],
                })
        head = self.connection.execute(
            "SELECT version_no FROM text_versions WHERE instrument_id=? AND created_at<=? "
            "ORDER BY version_no DESC LIMIT 1",
            (instrument_id, moment),
        ).fetchone()
        clause_map = {} if head is None else self._clause_map(instrument_id, head["version_no"])
        overdue_actions = []
        action_rows = self.connection.execute(
            "SELECT * FROM actions WHERE instrument_id=? AND state='open' AND due_at<? "
            "ORDER BY action_id",
            (instrument_id, moment),
        ).fetchall()
        for action in action_rows:
            clause = clause_map.get(action["clause_id"])
            acceptance = self.connection.execute(
                "SELECT * FROM acceptance_events WHERE instrument_id=? AND participant_id=? "
                "AND signed_at<=? ORDER BY acceptance_id DESC LIMIT 1",
                (instrument_id, action["owner_participant_id"], moment),
            ).fetchone()
            authorization = None
            if acceptance is not None:
                authorization = self.get_authorization(acceptance["authorization_id"])
            evidence = self.connection.execute(
                "SELECT ce.condition_id,ce.event_type,ce.evidence_ref,ce.actor_id,ce.created_at "
                "FROM condition_events ce JOIN conditions c ON c.condition_id=ce.condition_id "
                "WHERE c.instrument_id=? AND c.clause_id=? AND ce.created_at<=? "
                "ORDER BY ce.event_id",
                (instrument_id, action["clause_id"], moment),
            ).fetchall()
            overdue_actions.append({
                "action_id": action["action_id"],
                "clause_id": action["clause_id"],
                "description": action["description"],
                "owner_participant_id": action["owner_participant_id"],
                "due_at": action["due_at"],
                "clause": None if clause is None else {
                    "clause_id": clause["clause_id"],
                    "clause_version": clause["clause_version"],
                    "title": clause["title"],
                },
                "acceptance": None if acceptance is None else {
                    "signed_by": acceptance["signed_by"],
                    "signed_at": acceptance["signed_at"],
                    "version_no": acceptance["version_no"],
                    "authorization_id": acceptance["authorization_id"],
                },
                "authorization": authorization,
                "evidence": [dict(event) for event in evidence],
            })
        return {
            "instrument_id": instrument_id,
            "as_of": moment,
            "overdue_conditions": overdue_conditions,
            "overdue_actions": overdue_actions,
        }

    def audit_trail(self, actor_id: str, instrument_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        self._instrument(instrument_id)
        rows = self.connection.execute(
            "SELECT * FROM audit_events WHERE instrument_id=? ORDER BY event_id",
            (instrument_id,),
        ).fetchall()
        return [
            {key: row[key] for key in ("event_id", "entity_type", "entity_id", "event_type",
                                       "actor_id", "created_at")}
            | {"payload": json.loads(row["payload_json"])}
            for row in rows
        ]

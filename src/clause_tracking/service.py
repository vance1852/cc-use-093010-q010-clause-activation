"""条款协商与生效跟踪的领域用例。

阶段模型：提案基线 -> 修订（支持/保留意见）-> 封存表决（不可变事实）
-> 按参与方接受 -> 前置条件满足后生效 -> 后续行动闭环。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .clock import SystemClock, isoformat, parse_utc
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest, require_sha256
from .storage import initialize, transaction


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "secretariat": {
        "dialogue.write", "participant.write", "authorization.write",
        "clause.write", "revision.write", "translation.write", "conflict.write",
        "seal.write", "effectiveness.write", "condition.write", "action.write",
        "condition.review", "action.review",
        "report.read", "audit.read",
    },
    "delegate": {
        "clause.write", "revision.write", "translation.write",
        "support.write", "reservation.write", "condition.write", "action.write",
        "condition.evidence", "action.evidence", "report.read",
    },
    "auditor": {"report.read", "audit.read"},
}


class ClauseTrackingService:
    """在单个 SQLite 连接上提供条款协商与生效跟踪的全部操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # -- 基础工具 ----------------------------------------------------------

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        if permission not in ROLE_PERMISSIONS[row["role"]]:
            raise Forbidden(f"角色 {row['role']} 无权执行 {permission}")
        return row

    def _event(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        """向只追加日志记录一条事件；basis_sha256 覆盖事件全部字段，可供外部重放核对。"""

        recorded_at = self._now()
        basis = content_digest(
            [entity_type, entity_id, event_type, actor_id, recorded_at, payload]
        )
        self.connection.execute(
            "INSERT INTO event_journal(entity_type,entity_id,event_type,actor_id,recorded_at,"
            "payload_json,basis_sha256) VALUES(?,?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, recorded_at,
             canonical_json(payload), basis),
        )

    def _dialogue(self, dialogue_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM dialogues WHERE dialogue_id=?", (dialogue_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"对话不存在: {dialogue_id}")
        return row

    def _clause(self, clause_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM clauses WHERE clause_id=?", (clause_id,)).fetchone()
        if row is None:
            raise NotFound(f"条款不存在: {clause_id}")
        return row

    def _revision(self, revision_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM clause_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"条款版本不存在: {revision_id}")
        return row

    def _require_dialogue_participant(self, dialogue_id: str, participant_id: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM dialogue_participants WHERE dialogue_id=? AND participant_id=?",
            (dialogue_id, participant_id),
        ).fetchone()
        if row is None:
            raise ValidationFailed(f"参与方未加入本对话: {participant_id}")

    def _active_authorization(
        self, delegate_id: str, participant_id: str, permission: str | None = None
    ) -> sqlite3.Row:
        """返回受权代表当前有效的授权；scope 非空时还必须包含相应权限。"""

        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE delegate_id=? AND participant_id=? AND revoked_at IS NULL",
            (delegate_id, participant_id),
        ).fetchone()
        if row is None:
            raise Forbidden(f"用户 {delegate_id} 不持有参与方 {participant_id} 的有效授权")
        scopes = json.loads(row["scope_json"])
        if permission is not None and scopes and permission not in scopes:
            raise Forbidden(f"授权范围不包含 {permission}")
        return row

    @staticmethod
    def _text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 必须是非空字符串")
        return value.strip()

    def _due(self, value: Any) -> str:
        try:
            return parse_utc(value if isinstance(value, str) else str(value))
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc

    # -- 用户与基础资料 ----------------------------------------------------

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

    def create_dialogue(self, actor_id: str, dialogue_id: str, title: str) -> dict[str, Any]:
        self._require(actor_id, "dialogue.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dialogues(dialogue_id,title,created_at) VALUES(?,?,?)",
                    (dialogue_id, self._text(title, "title"), self._now()),
                )
                self._event("dialogue", dialogue_id, "dialogue.created", actor_id, {"title": title})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"对话已存在: {dialogue_id}") from exc
        return {"dialogue_id": dialogue_id, "title": title.strip()}

    def register_participant(
        self, actor_id: str, dialogue_id: str, participant_id: str, display_name: str
    ) -> dict[str, Any]:
        self._require(actor_id, "participant.write")
        self._dialogue(dialogue_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT OR IGNORE INTO participants(participant_id,display_name,created_at) VALUES(?,?,?)",
                    (participant_id, self._text(display_name, "display_name"), self._now()),
                )
                self.connection.execute(
                    "INSERT INTO dialogue_participants(dialogue_id,participant_id,joined_at) VALUES(?,?,?)",
                    (dialogue_id, participant_id, self._now()),
                )
                self._event("dialogue", dialogue_id, "participant.registered", actor_id,
                            {"participant_id": participant_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"参与方已加入本对话: {participant_id}") from exc
        return {"dialogue_id": dialogue_id, "participant_id": participant_id}

    def grant_authorization(
        self,
        actor_id: str,
        authorization_id: str,
        dialogue_id: str,
        participant_id: str,
        delegate_id: str,
        scopes: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "authorization.write")
        self._dialogue(dialogue_id)
        self._require_dialogue_participant(dialogue_id, participant_id)
        user = self.connection.execute(
            "SELECT 1 FROM users WHERE user_id=? AND active=1", (delegate_id,)
        ).fetchone()
        if user is None:
            raise NotFound(f"用户不存在或已停用: {delegate_id}")
        scope_list = sorted({self._text(item, "scope") for item in (scopes or [])})
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO authorizations(authorization_id,dialogue_id,participant_id,delegate_id,"
                    "scope_json,granted_at,granted_by) VALUES(?,?,?,?,?,?,?)",
                    (authorization_id, dialogue_id, participant_id, delegate_id,
                     canonical_json(scope_list), self._now(), actor_id),
                )
                self._event("authorization", authorization_id, "authorization.granted", actor_id, {
                    "dialogue_id": dialogue_id, "participant_id": participant_id,
                    "delegate_id": delegate_id, "scopes": scope_list,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("授权编号冲突或该代表已持有此参与方的有效授权") from exc
        return {"authorization_id": authorization_id, "participant_id": participant_id,
                "delegate_id": delegate_id, "scopes": scope_list}

    def revoke_authorization(
        self, actor_id: str, authorization_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "authorization.write")
        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE authorization_id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise NotFound("授权不存在")
        if row["revoked_at"] is not None:
            raise InvalidState("授权已经撤销")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE authorizations SET revoked_at=?,revoked_by=?,revoke_reason=? "
                "WHERE authorization_id=? AND revoked_at IS NULL",
                (self._now(), actor_id, self._text(reason, "reason"), authorization_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("授权状态已变化")
            self._event("authorization", authorization_id, "authorization.revoked", actor_id,
                        {"reason": reason, "participant_id": row["participant_id"]})
        return {"authorization_id": authorization_id, "status": "revoked"}

    # -- 条款与版本链 ------------------------------------------------------

    def create_clause(
        self, actor_id: str, clause_id: str, dialogue_id: str, clause_code: str, title: str,
        *, independent: bool = True,
    ) -> dict[str, Any]:
        self._require(actor_id, "clause.write")
        self._dialogue(dialogue_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO clauses(clause_id,dialogue_id,clause_code,title,independent,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (clause_id, dialogue_id, self._text(clause_code, "clause_code"),
                     self._text(title, "title"), 1 if independent else 0, actor_id, self._now()),
                )
                self._event("clause", clause_id, "clause.created", actor_id,
                            {"dialogue_id": dialogue_id, "clause_code": clause_code,
                             "independent": bool(independent)})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"条款编号或代码冲突: {clause_id}") from exc
        return {"clause_id": clause_id, "dialogue_id": dialogue_id, "clause_code": clause_code.strip(),
                "independent": bool(independent)}

    def propose_revision(
        self,
        actor_id: str,
        revision_id: str,
        clause_id: str,
        language: str,
        title: str,
        body: str,
        *,
        kind: str = "amendment",
        parent_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """登记提案基线或修订。

        修订编号在条款内顺序递增；允许以任意既有版本为父版本提出分叉修订，
        从而让互相冲突的修订并行存在（合并时再互斥裁决）。内容相同的重复
        提案返回既有版本，不产生新版本。
        """

        self._require(actor_id, "revision.write")
        self._clause(clause_id)
        if kind not in {"baseline", "amendment"}:
            raise ValidationFailed("修订类型必须是 baseline 或 amendment")
        language = self._text(language, "language")
        title = self._text(title, "title")
        if not isinstance(body, str) or not body.strip():
            raise ValidationFailed("body 必须是非空字符串")
        digest = content_digest([{"language": language, "title": title, "body": body}])

        with transaction(self.connection, immediate=True):
            duplicate = self.connection.execute(
                "SELECT revision_id FROM clause_revisions WHERE clause_id=? AND content_sha256=?",
                (clause_id, digest),
            ).fetchone()
            if duplicate is not None:
                return self.get_revision(duplicate["revision_id"])

            latest = self.connection.execute(
                "SELECT revision_no FROM clause_revisions WHERE clause_id=? ORDER BY revision_no DESC LIMIT 1",
                (clause_id,),
            ).fetchone()

            if kind == "baseline":
                if latest is not None:
                    raise Conflict("该条款已经存在提案基线")
                revision_no, parent_id = 1, None
            else:
                if latest is None:
                    raise InvalidState("条款尚无提案基线，无法提出修订")
                if parent_revision_id is None:
                    parent_id = self.connection.execute(
                        "SELECT revision_id FROM clause_revisions WHERE clause_id=? "
                        "ORDER BY revision_no DESC LIMIT 1", (clause_id,)
                    ).fetchone()["revision_id"]
                else:
                    parent = self._revision(parent_revision_id)
                    if parent["clause_id"] != clause_id:
                        raise ValidationFailed("父版本不属于同一条款")
                    parent_id = parent["revision_id"]
                revision_no = latest["revision_no"] + 1

            self.connection.execute(
                "INSERT INTO clause_revisions(revision_id,clause_id,revision_no,parent_revision_id,kind,"
                "language,title,body,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id, clause_id, revision_no, parent_id, kind, language, title, body,
                 digest, actor_id, self._now()),
            )
            self._event("clause_revision", revision_id, f"revision.{kind}_proposed", actor_id, {
                "clause_id": clause_id, "revision_no": revision_no,
                "parent_revision_id": parent_id, "content_sha256": digest,
            })
        return self.get_revision(revision_id)

    def register_translation(
        self,
        actor_id: str,
        translation_id: str,
        revision_id: str,
        language: str,
        title: str,
        body: str,
        correspondence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """登记某版本的翻译文本及其与基准语言文本的段落对应关系。"""

        self._require(actor_id, "translation.write")
        self._revision(revision_id)
        language = self._text(language, "language")
        title = self._text(title, "title")
        body = self._text(body, "body")
        if not isinstance(correspondence, Mapping) or not correspondence:
            raise ValidationFailed("correspondence 必须是非空对象（基准段落 -> 译文段落）")
        digest = content_digest([{"language": language, "title": title, "body": body}])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO revision_translations(translation_id,revision_id,language,title,body,"
                    "content_sha256,correspondence_json,registered_by,registered_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (translation_id, revision_id, language, title, body, digest,
                     canonical_json(dict(correspondence)), actor_id, self._now()),
                )
                self._event("clause_revision", revision_id, "translation.registered", actor_id,
                            {"translation_id": translation_id, "language": language,
                             "content_sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("翻译编号冲突，或该版本已有此语言的翻译") from exc
        return {"translation_id": translation_id, "revision_id": revision_id, "language": language}

    def mark_conflict(
        self, actor_id: str, revision_a_id: str, revision_b_id: str, reason: str
    ) -> dict[str, Any]:
        """登记两个互相冲突的修订；冲突修订在封存合并时互斥。"""

        self._require(actor_id, "conflict.write")
        a, b = self._revision(revision_a_id), self._revision(revision_b_id)
        if a["clause_id"] != b["clause_id"]:
            raise ValidationFailed("只有同一议题（条款）的修订才能标记冲突")
        if a["revision_id"] == b["revision_id"]:
            raise ValidationFailed("版本不能与自身冲突")
        passed_seals = self.connection.execute(
            "SELECT count(*) FROM clause_seals WHERE passed=1 AND revision_id IN (?,?)",
            (a["revision_id"], b["revision_id"]),
        ).fetchone()[0]
        if passed_seals == 2:
            raise InvalidState("两个修订均已封存通过，既成表决事实不能再追溯标记冲突")
        lo, hi = sorted((a["revision_id"], b["revision_id"]))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO revision_conflicts(revision_a_id,revision_b_id,reason,marked_by,marked_at) "
                    "VALUES(?,?,?,?,?)",
                    (lo, hi, self._text(reason, "reason"), actor_id, self._now()),
                )
                self._event("clause_revision", lo, "revision.conflict_marked", actor_id,
                            {"revision_a_id": lo, "revision_b_id": hi, "reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("这两个修订之间已经登记过冲突") from exc
        return {"revision_a_id": lo, "revision_b_id": hi}

    # -- 支持、保留意见与封存表决 -----------------------------------------

    def add_support(
        self, actor_id: str, revision_id: str, participant_id: str
    ) -> dict[str, Any]:
        """参与方通过其授权代表对修订表示支持。重复签署不增加支持数。"""

        self._require(actor_id, "support.write")
        revision = self._revision(revision_id)
        clause = self._clause(revision["clause_id"])
        self._require_dialogue_participant(clause["dialogue_id"], participant_id)
        auth = self._active_authorization(actor_id, participant_id, "support.write")
        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT seal_id FROM clause_seals WHERE revision_id=?", (revision_id,)
            ).fetchone() is not None:
                raise InvalidState("版本已经封存表决，不能再追加支持")
            existing = self.connection.execute(
                "SELECT 1 FROM revision_supports WHERE revision_id=? AND participant_id=?",
                (revision_id, participant_id),
            ).fetchone()
            if self.connection.execute(
                "SELECT 1 FROM reservations WHERE revision_id=? AND participant_id=? AND withdrawn_at IS NULL",
                (revision_id, participant_id),
            ).fetchone() is not None:
                raise Conflict("该参与方已登记未撤回的保留意见，不能再表示支持")
            if existing is None:
                self.connection.execute(
                    "INSERT INTO revision_supports(revision_id,participant_id,authorization_id,"
                    "supported_by,supported_at) VALUES(?,?,?,?,?)",
                    (revision_id, participant_id, auth["authorization_id"], actor_id, self._now()),
                )
                self._event("clause_revision", revision_id, "support.added", actor_id,
                            {"participant_id": participant_id})
            # 重复签署：不插入新行、不增加支持数、不产生新事件。
            count = self.connection.execute(
                "SELECT count(*) FROM revision_supports WHERE revision_id=?", (revision_id,)
            ).fetchone()[0]
        return {"revision_id": revision_id, "participant_id": participant_id, "support_count": count}

    def record_reservation(
        self, actor_id: str, reservation_id: str, revision_id: str, participant_id: str, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        revision = self._revision(revision_id)
        clause = self._clause(revision["clause_id"])
        self._require_dialogue_participant(clause["dialogue_id"], participant_id)
        auth = self._active_authorization(actor_id, participant_id, "reservation.write")
        if self.connection.execute(
            "SELECT seal_id FROM clause_seals WHERE revision_id=?", (revision_id,)
        ).fetchone() is not None:
            raise InvalidState("版本已经封存表决，不能再登记保留意见")
        if self.connection.execute(
            "SELECT 1 FROM revision_supports WHERE revision_id=? AND participant_id=?",
            (revision_id, participant_id),
        ).fetchone():
            raise Conflict("已表示支持的参与方不能再登记保留意见")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,revision_id,participant_id,note,"
                    "authorization_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (reservation_id, revision_id, participant_id, self._text(note, "note"),
                     auth["authorization_id"], actor_id, self._now()),
                )
                self._event("clause_revision", revision_id, "reservation.recorded", actor_id,
                            {"reservation_id": reservation_id, "participant_id": participant_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该参与方对此版本已有未撤回的保留意见") from exc
        return {"reservation_id": reservation_id, "status": "open"}

    def withdraw_reservation(
        self, actor_id: str, reservation_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("保留意见不存在")
        self._active_authorization(actor_id, row["participant_id"], "reservation.write")
        if row["withdrawn_at"] is not None:
            raise InvalidState("保留意见已经撤回")
        # 封存后允许撤回保留意见以加入文本；封存快照中的历史事实不被改写。
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE reservations SET withdrawn_at=?,withdrawn_by=?,withdraw_reason=? "
                "WHERE reservation_id=? AND withdrawn_at IS NULL",
                (self._now(), actor_id, self._text(reason, "reason"), reservation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("保留意见状态已变化")
            self._event("clause_revision", row["revision_id"], "reservation.withdrawn", actor_id,
                        {"reservation_id": reservation_id, "participant_id": row["participant_id"]})
        return {"reservation_id": reservation_id, "status": "withdrawn"}

    def seal_ballot(
        self,
        actor_id: str,
        seal_id: str,
        clause_id: str,
        revision_id: str,
        *,
        threshold: str = "majority",
        expected_participation: int | None = None,
    ) -> dict[str, Any]:
        """封存对某版本的表决事实。

        已封存事实不可改变：同一（条款，版本）再次封存（即使并发）只能成功
        一次；封存时的名单与统计原样固化到 ballot_json，事后整理文本不影响
        这份事实。封存通过即视为该修订并入（merged）。
        """

        self._require(actor_id, "seal.write")
        clause = self._clause(clause_id)
        revision = self._revision(revision_id)
        if revision["clause_id"] != clause_id:
            raise ValidationFailed("版本不属于该条款")
        if threshold not in {"majority", "unanimity", "two_thirds"}:
            raise ValidationFailed("未知通过门槛")
        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT seal_id FROM clause_seals WHERE clause_id=? AND revision_id=?",
                (clause_id, revision_id),
            ).fetchone() is not None:
                raise Conflict("该条款版本已经封存表决；已封存事实不能重复封存")

            members = [
                row["participant_id"]
                for row in self.connection.execute(
                    "SELECT participant_id FROM dialogue_participants WHERE dialogue_id=? "
                    "ORDER BY participant_id", (clause["dialogue_id"],)
                ).fetchall()
            ]
            participation = len(members)
            if expected_participation is not None and expected_participation != participation:
                raise InvalidState(
                    f"参与方数量已变化：期望 {expected_participation}，实际 {participation}"
                )

            supports = {
                row["participant_id"]
                for row in self.connection.execute(
                    "SELECT participant_id FROM revision_supports WHERE revision_id=?", (revision_id,)
                ).fetchall()
            }
            reservation_rows = self.connection.execute(
                "SELECT reservation_id,participant_id,note FROM reservations "
                "WHERE revision_id=? AND withdrawn_at IS NULL", (revision_id,)
            ).fetchall()
            reserved = {row["participant_id"] for row in reservation_rows}
            if supports & reserved:
                raise InvalidState("同一参与方不能同时支持并保留意见")

            # 互相冲突的修订不能同时合并：任一冲突伙伴已封存通过即拒绝。
            for partner_id in self._conflict_partners(revision_id):
                partner_sealed = self.connection.execute(
                    "SELECT passed FROM clause_seals WHERE revision_id=?", (partner_id,)
                ).fetchone()
                if partner_sealed is not None and partner_sealed["passed"]:
                    raise InvalidState("冲突修订已封存通过，不能同时合并本修订")

            support_count, reservation_count = len(supports), len(reserved)
            if threshold == "majority":
                passed = participation > 0 and support_count * 2 > participation
            elif threshold == "two_thirds":
                passed = participation > 0 and support_count * 3 >= participation * 2
            else:
                passed = participation > 0 and support_count == participation and reservation_count == 0

            sealed_at = self._now()
            ballot = {
                "dialogue_id": clause["dialogue_id"],
                "clause_id": clause_id,
                "revision_id": revision_id,
                "threshold": threshold,
                "participants": members,
                "support": sorted(supports),
                "reservations": [
                    {"participant_id": row["participant_id"], "note": row["note"],
                     "reservation_id": row["reservation_id"]}
                    for row in sorted(reservation_rows, key=lambda r: r["participant_id"])
                ],
                "content_sha256": revision["content_sha256"],
                "sealed_at": sealed_at,
            }
            ballot_digest = content_digest([ballot])

            self.connection.execute(
                "INSERT INTO clause_seals(seal_id,clause_id,revision_id,sealed_by,sealed_at,ballot_json,"
                "participation,support_count,reservation_count,threshold,passed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (seal_id, clause_id, revision_id, actor_id, sealed_at, canonical_json(ballot),
                 participation, support_count, reservation_count, threshold, 1 if passed else 0),
            )
            for participant_id in members:
                if participant_id in supports:
                    position, reservation_id = "support", None
                elif participant_id in reserved:
                    position = "reservation"
                    reservation_id = next(
                        row["reservation_id"] for row in reservation_rows
                        if row["participant_id"] == participant_id
                    )
                else:
                    continue
                self.connection.execute(
                    "INSERT INTO ballot_votes(seal_id,participant_id,position,reservation_id) "
                    "VALUES(?,?,?,?)",
                    (seal_id, participant_id, position, reservation_id),
                )
            if passed:
                self.connection.execute(
                    "UPDATE clause_revisions SET merged_at=? WHERE revision_id=?",
                    (sealed_at, revision_id),
                )
            self._event("clause", clause_id, "ballot.sealed", actor_id, {
                "seal_id": seal_id, "revision_id": revision_id, "passed": bool(passed),
                "support_count": support_count, "reservation_count": reservation_count,
                "ballot_sha256": ballot_digest,
            })
        return self.get_seal(seal_id)

    def get_seal(self, seal_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM clause_seals WHERE seal_id=?", (seal_id,)
        ).fetchone()
        if row is None:
            raise NotFound("封存记录不存在")
        data = dict(row)
        data["ballot"] = json.loads(data.pop("ballot_json"))
        data["passed"] = bool(data["passed"])
        return data

    # -- 接受、前置条件与生效 ---------------------------------------------

    def accept_text(
        self, actor_id: str, revision_id: str, participant_id: str
    ) -> dict[str, Any]:
        """参与方接受封存通过的文本；若无待办前置条件则同时生效。"""

        self._require(actor_id, "effectiveness.write")
        revision = self._revision(revision_id)
        clause = self._clause(revision["clause_id"])
        self._require_dialogue_participant(clause["dialogue_id"], participant_id)
        seal = self.connection.execute(
            "SELECT * FROM clause_seals WHERE revision_id=? AND passed=1", (revision_id,)
        ).fetchone()
        if seal is None:
            raise InvalidState("只有封存表决通过的文本才能被接受")
        # 封存票样是历史事实，不可改写；但保留方事后撤回保留即可加入，
        # 因此能否接受以保留意见的当前状态为准。
        open_reservation = self.connection.execute(
            "SELECT 1 FROM reservations WHERE revision_id=? AND participant_id=? AND withdrawn_at IS NULL",
            (revision_id, participant_id),
        ).fetchone()
        if open_reservation is not None:
            raise InvalidState("提出保留意见的参与方在撤回保留前不能接受文本")
        # 非独立条款作为一揽子组合：组合内全部条款通过封存后才能接受。
        if not clause["independent"]:
            unpacked = self.connection.execute(
                "SELECT c.clause_id FROM clauses c WHERE c.dialogue_id=? AND c.independent=0",
                (clause["dialogue_id"],),
            ).fetchall()
            for item in unpacked:
                passed_seal = self.connection.execute(
                    "SELECT 1 FROM clause_seals s JOIN clause_revisions r ON r.revision_id=s.revision_id "
                    "WHERE r.clause_id=? AND s.passed=1", (item["clause_id"],)
                ).fetchone()
                if passed_seal is None:
                    raise InvalidState("一揽子组合中尚有条款未通过封存，不能单独接受")
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT * FROM effectiveness WHERE revision_id=? AND participant_id=?",
                (revision_id, participant_id),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO effectiveness(revision_id,participant_id,state,seal_id,accepted_at,"
                    "updated_by,updated_at) VALUES(?,?,'accepted',?,?,?,?)",
                    (revision_id, participant_id, seal["seal_id"], self._now(), actor_id, self._now()),
                )
                self._event("effectiveness", f"{revision_id}:{participant_id}", "text.accepted", actor_id,
                            {"revision_id": revision_id, "participant_id": participant_id,
                             "seal_id": seal["seal_id"]})
            elif existing["state"] == "accepted":
                pass  # 重复接受：幂等，不产生新事件。
            elif existing["state"] == "in_force":
                return dict(existing)
            else:
                raise InvalidState("已经终止的接受不能重新接受")
            self._enact_if_ready(revision_id, participant_id, actor_id)
        return self._effectiveness_row(revision_id, participant_id)

    def register_precondition(
        self,
        actor_id: str,
        precondition_id: str,
        revision_id: str,
        participant_id: str,
        code: str,
        description: str,
        responsible_participant_id: str,
        due_at: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "condition.write")
        revision = self._revision(revision_id)
        clause = self._clause(revision["clause_id"])
        self._require_dialogue_participant(clause["dialogue_id"], participant_id)
        self._require_dialogue_participant(clause["dialogue_id"], responsible_participant_id)
        effect = self.connection.execute(
            "SELECT state FROM effectiveness WHERE revision_id=? AND participant_id=?",
            (revision_id, participant_id),
        ).fetchone()
        if effect is not None and effect["state"] == "in_force":
            raise InvalidState("文本已经生效，不能追加前置条件")
        due = self._due(due_at)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO preconditions(precondition_id,revision_id,participant_id,code,description,"
                    "status,responsible_participant_id,due_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?, 'pending', ?,?,?,?)",
                    (precondition_id, revision_id, participant_id, self._text(code, "code"),
                     self._text(description, "description"), responsible_participant_id,
                     due, actor_id, self._now()),
                )
                self._event("precondition", precondition_id, "precondition.registered", actor_id, {
                    "revision_id": revision_id, "participant_id": participant_id, "code": code,
                    "responsible_participant_id": responsible_participant_id, "due_at": due,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("前置条件编号冲突，或该版本对该参与方已有同编号条件") from exc
        return self.get_precondition(precondition_id)

    def _precondition(self, precondition_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM preconditions WHERE precondition_id=?", (precondition_id,)
        ).fetchone()
        if row is None:
            raise NotFound("前置条件不存在")
        return row

    def get_precondition(self, precondition_id: str) -> dict[str, Any]:
        return dict(self._precondition(precondition_id))

    def pending_preconditions(self, actor_id: str, *, at: str | None = None) -> dict[str, Any]:
        """列出未完成的前置条件；持续保留责任人、期限与逾期标记。"""

        self._require(actor_id, "report.read")
        point = self._now() if at is None else self._due(at)
        rows = self.connection.execute(
            "SELECT p.*,c.clause_id,c.clause_code,c.dialogue_id "
            "FROM preconditions p JOIN clause_revisions r ON r.revision_id=p.revision_id "
            "JOIN clauses c ON c.clause_id=r.clause_id "
            "WHERE p.status='pending' ORDER BY p.due_at,p.precondition_id"
        ).fetchall()
        items = [dict(row) | {"overdue": row["due_at"] < point} for row in rows]
        return {"at": point, "pending": items, "count": len(items)}

    def satisfy_precondition(
        self,
        actor_id: str,
        precondition_id: str,
        evidence_sha256: str,
        evidence_summary: str,
    ) -> dict[str, Any]:
        """登记条件完成证据并推进状态。

        重复提交同一证据直接返回既有结果（可重放、不重复计数）；提交不同
        证据则冲突。全部条件办结后参与方绑定自动生效。
        """

        self._require(actor_id, "condition.evidence")
        digest = require_sha256(evidence_sha256, "evidence_sha256")
        summary = self._text(evidence_summary, "evidence_summary")
        row = self._precondition(precondition_id)
        if row["status"] == "satisfied":
            if row["evidence_sha256"] != digest:
                raise Conflict("条件已完成，但提交了不同的完成证据")
            return dict(row)
        if row["status"] == "waived":
            raise InvalidState("条件已被豁免，无需完成")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE preconditions SET status='satisfied',evidence_sha256=?,evidence_summary=?,"
                "completed_by=?,completed_at=? WHERE precondition_id=? AND status='pending'",
                (digest, summary, actor_id, self._now(), precondition_id),
            )
            if cursor.rowcount != 1:
                raise Conflict("前置条件状态已变化，完成未生效")
            self._event("precondition", precondition_id, "precondition.satisfied", actor_id, {
                "precondition_id": precondition_id, "revision_id": row["revision_id"],
                "participant_id": row["participant_id"], "code": row["code"],
                "evidence_sha256": digest, "evidence_summary": summary,
            })
            self._enact_if_ready(row["revision_id"], row["participant_id"], actor_id)
        return self.get_precondition(precondition_id)

    def waive_precondition(
        self, actor_id: str, precondition_id: str, reason: str
    ) -> dict[str, Any]:
        """秘书处基于凭据豁免前置条件；豁免与完成一样进入可重放日志。"""

        self._require(actor_id, "condition.review")
        row = self._precondition(precondition_id)
        if row["status"] != "pending":
            raise InvalidState("只有待完成的前置条件可以豁免")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE preconditions SET status='waived',evidence_summary=?,completed_by=?,completed_at=? "
                "WHERE precondition_id=? AND status='pending'",
                (self._text(reason, "reason"), actor_id, self._now(), precondition_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("前置条件状态已变化")
            self._event("precondition", precondition_id, "precondition.waived", actor_id,
                        {"reason": reason, "revision_id": row["revision_id"],
                         "participant_id": row["participant_id"]})
            self._enact_if_ready(row["revision_id"], row["participant_id"], actor_id)
        return self.get_precondition(precondition_id)

    def _enact_if_ready(self, revision_id: str, participant_id: str, actor_id: str) -> bool:
        """前置条件全部办结且文本已接受时，使该参与方绑定生效（须在事务内调用）。"""

        pending = self.connection.execute(
            "SELECT count(*) FROM preconditions WHERE revision_id=? AND participant_id=? AND status='pending'",
            (revision_id, participant_id),
        ).fetchone()[0]
        if pending:
            return False
        effect = self.connection.execute(
            "SELECT * FROM effectiveness WHERE revision_id=? AND participant_id=?",
            (revision_id, participant_id),
        ).fetchone()
        if effect is None or effect["state"] != "accepted":
            return False
        effective_at = self._now()
        self.connection.execute(
            "UPDATE effectiveness SET state='in_force',effective_at=?,updated_by=?,updated_at=? "
            "WHERE revision_id=? AND participant_id=? AND state='accepted'",
            (effective_at, actor_id, self._now(), revision_id, participant_id),
        )
        self._event("effectiveness", f"{revision_id}:{participant_id}", "clause.in_force", actor_id,
                    {"revision_id": revision_id, "participant_id": participant_id,
                     "effective_at": effective_at})
        return True

    # -- 后续行动 ----------------------------------------------------------

    def register_follow_up(
        self,
        actor_id: str,
        action_id: str,
        revision_id: str,
        participant_id: str,
        code: str,
        description: str,
        responsible_participant_id: str,
        due_at: str,
    ) -> dict[str, Any]:
        """登记生效后需要落实的后续行动；未完成的行动持续保留责任人和期限。"""

        self._require(actor_id, "action.write")
        revision = self._revision(revision_id)
        clause = self._clause(revision["clause_id"])
        self._require_dialogue_participant(clause["dialogue_id"], participant_id)
        self._require_dialogue_participant(clause["dialogue_id"], responsible_participant_id)
        effect = self.connection.execute(
            "SELECT state FROM effectiveness WHERE revision_id=? AND participant_id=?",
            (revision_id, participant_id),
        ).fetchone()
        if effect is None or effect["state"] not in {"accepted", "in_force"}:
            raise InvalidState("参与方接受文本后才能登记后续行动")
        due = self._due(due_at)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO follow_up_actions(action_id,revision_id,participant_id,code,description,"
                    "status,responsible_participant_id,due_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?, 'open', ?,?,?,?)",
                    (action_id, revision_id, participant_id, self._text(code, "code"),
                     self._text(description, "description"), responsible_participant_id,
                     due, actor_id, self._now()),
                )
                self._event("follow_up", action_id, "follow_up.registered", actor_id, {
                    "revision_id": revision_id, "participant_id": participant_id, "code": code,
                    "responsible_participant_id": responsible_participant_id, "due_at": due,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("行动编号冲突，或该版本对该参与方已有同编号行动") from exc
        return self.get_follow_up(action_id)

    def _action(self, action_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM follow_up_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise NotFound("后续行动不存在")
        return row

    def get_follow_up(self, action_id: str) -> dict[str, Any]:
        return dict(self._action(action_id))

    def complete_follow_up(
        self, actor_id: str, action_id: str, evidence_sha256: str, evidence_summary: str
    ) -> dict[str, Any]:
        self._require(actor_id, "action.evidence")
        digest = require_sha256(evidence_sha256, "evidence_sha256")
        summary = self._text(evidence_summary, "evidence_summary")
        row = self._action(action_id)
        if row["status"] == "done":
            if row["evidence_sha256"] != digest:
                raise Conflict("行动已完成，但提交了不同的完成证据")
            return dict(row)
        if row["status"] == "revoked":
            raise InvalidState("行动已经撤销，不能完成")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE follow_up_actions SET status='done',evidence_sha256=?,evidence_summary=?,"
                "completed_by=?,completed_at=? WHERE action_id=? AND status='open'",
                (digest, summary, actor_id, self._now(), action_id),
            )
            if cursor.rowcount != 1:
                raise Conflict("行动状态已变化，完成未生效")
            self._event("follow_up", action_id, "follow_up.completed", actor_id, {
                "revision_id": row["revision_id"], "participant_id": row["participant_id"],
                "code": row["code"], "evidence_sha256": digest, "evidence_summary": summary,
            })
        return self.get_follow_up(action_id)

    def revoke_follow_up(
        self, actor_id: str, action_id: str, reason: str
    ) -> dict[str, Any]:
        """撤销后续行动；撤销与完成都写入只追加日志，留下可重放依据。"""

        self._require(actor_id, "action.review")
        row = self._action(action_id)
        if row["status"] != "open":
            raise InvalidState("只有开放中的后续行动可以撤销")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE follow_up_actions SET status='revoked',revoked_by=?,revoked_at=?,revoke_reason=? "
                "WHERE action_id=? AND status='open'",
                (actor_id, self._now(), self._text(reason, "reason"), action_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("行动状态已变化")
            self._event("follow_up", action_id, "follow_up.revoked", actor_id, {
                "revision_id": row["revision_id"], "participant_id": row["participant_id"],
                "code": row["code"], "reason": reason,
            })
        return self.get_follow_up(action_id)

    def terminate_effectiveness(
        self, actor_id: str, revision_id: str, participant_id: str, reason: str
    ) -> dict[str, Any]:
        """终止某参与方对某条款文本的约束（如退出、废止）；历史事实仍然保留。"""

        self._require(actor_id, "effectiveness.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE effectiveness SET state='terminated',terminated_at=?,terminate_reason=?,"
                "updated_by=?,updated_at=? WHERE revision_id=? AND participant_id=? "
                "AND state IN ('accepted','in_force')",
                (self._now(), self._text(reason, "reason"), actor_id, self._now(),
                 revision_id, participant_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("没有可终止的生效记录")
            self._event("effectiveness", f"{revision_id}:{participant_id}", "effectiveness.terminated",
                        actor_id, {"revision_id": revision_id, "participant_id": participant_id,
                                   "reason": reason})
        return self._effectiveness_row(revision_id, participant_id)

    def _effectiveness_row(self, revision_id: str, participant_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM effectiveness WHERE revision_id=? AND participant_id=?",
            (revision_id, participant_id),
        ).fetchone()
        if row is None:
            raise NotFound("生效记录不存在")
        return dict(row)

    # -- 查询：时点状态与逾期反查 -----------------------------------------

    def status_at(self, actor_id: str, clause_id: str, at: str) -> dict[str, Any]:
        """说明在任一时点，条款文本对哪些参与方具有何种约束。

        结论只依据只追加事件日志重放，不读取当前状态列：事后整理文本或
        终止约束都不会改变历史时点的答案。
        """

        self._require(actor_id, "report.read")
        self._clause(clause_id)
        point = self._due(at)

        revisions = self.connection.execute(
            "SELECT revision_id,revision_no,kind,title,content_sha256,created_at "
            "FROM clause_revisions WHERE clause_id=? ORDER BY revision_no", (clause_id,)
        ).fetchall()
        revision_ids = {row["revision_id"] for row in revisions}
        seals = {
            row["revision_id"]: row
            for row in self.connection.execute(
                "SELECT seal_id,revision_id,passed,sealed_at FROM clause_seals "
                "WHERE clause_id=? AND sealed_at<=?", (clause_id, point)
            ).fetchall()
        }
        events = self.connection.execute(
            "SELECT event_type,recorded_at,payload_json FROM event_journal "
            "WHERE entity_type='effectiveness' AND recorded_at<=? ORDER BY event_id", (point,)
        ).fetchall()

        states: dict[tuple[str, str], str] = {}
        effective_at: dict[tuple[str, str], str] = {}
        for event in events:
            payload = json.loads(event["payload_json"])
            key = (payload.get("revision_id"), payload.get("participant_id"))
            if key[0] not in revision_ids:
                continue
            if event["event_type"] == "text.accepted":
                if states.get(key) != "in_force":
                    states[key] = "accepted"
            elif event["event_type"] == "clause.in_force":
                states[key] = "in_force"
                effective_at[key] = payload.get("effective_at") or event["recorded_at"]
            elif event["event_type"] == "effectiveness.terminated":
                states[key] = "terminated"

        result_revisions = []
        for revision in revisions:
            if revision["created_at"] > point:
                continue
            seal = seals.get(revision["revision_id"])
            bindings = [
                {"participant_id": participant_id, "binding": state,
                 "effective_at": effective_at.get((revision["revision_id"], participant_id))}
                for (revision_id, participant_id), state in sorted(states.items())
                if revision_id == revision["revision_id"]
            ]
            result_revisions.append({
                "revision_id": revision["revision_id"],
                "revision_no": revision["revision_no"],
                "kind": revision["kind"],
                "title": revision["title"],
                "content_sha256": revision["content_sha256"],
                "sealed": seal is not None,
                "ballot_passed": None if seal is None else bool(seal["passed"]),
                "bindings": bindings,
            })
        return {"clause_id": clause_id, "at": point, "revisions": result_revisions}

    def overdue_actions(self, actor_id: str, *, at: str | None = None) -> dict[str, Any]:
        """从逾期行动反查对应条款、表决封存、代表授权与证据链。"""

        self._require(actor_id, "report.read")
        point = self._now() if at is None else self._due(at)
        rows = self.connection.execute(
            "SELECT a.*,c.clause_id,c.clause_code,c.dialogue_id,r.revision_no,r.title AS revision_title "
            "FROM follow_up_actions a "
            "JOIN clause_revisions r ON r.revision_id=a.revision_id "
            "JOIN clauses c ON c.clause_id=r.clause_id "
            "WHERE a.status='open' AND a.due_at<? ORDER BY a.due_at,a.action_id",
            (point,),
        ).fetchall()
        items = []
        for row in rows:
            authorization = self.connection.execute(
                "SELECT az.authorization_id,az.delegate_id,az.participant_id,az.granted_at,az.revoked_at "
                "FROM revision_supports rs "
                "JOIN authorizations az ON az.authorization_id=rs.authorization_id "
                "WHERE rs.revision_id=? AND rs.participant_id=?",
                (row["revision_id"], row["participant_id"]),
            ).fetchone()
            effect = self.connection.execute(
                "SELECT state,effective_at,accepted_at FROM effectiveness "
                "WHERE revision_id=? AND participant_id=?",
                (row["revision_id"], row["participant_id"]),
            ).fetchone()
            preconditions = [
                dict(item)
                for item in self.connection.execute(
                    "SELECT precondition_id,code,status,responsible_participant_id,due_at,"
                    "evidence_sha256,evidence_summary,completed_by,completed_at "
                    "FROM preconditions WHERE revision_id=? AND participant_id=? "
                    "ORDER BY precondition_id",
                    (row["revision_id"], row["participant_id"]),
                ).fetchall()
            ]
            items.append({
                "action_id": row["action_id"],
                "code": row["code"],
                "description": row["description"],
                "due_at": row["due_at"],
                "overdue_days": _days_between(row["due_at"], point),
                "revision_id": row["revision_id"],
                "revision_no": row["revision_no"],
                "revision_title": row["revision_title"],
                "clause_id": row["clause_id"],
                "clause_code": row["clause_code"],
                "dialogue_id": row["dialogue_id"],
                "participant_id": row["participant_id"],
                "responsible_participant_id": row["responsible_participant_id"],
                "binding": None if effect is None else dict(effect),
                "preconditions": preconditions,
                "authorization": None if authorization is None else dict(authorization),
                "seal": self._seal_brief(row["revision_id"]),
            })
        return {"at": point, "overdue": items, "count": len(items)}

    def _seal_brief(self, revision_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT seal_id,sealed_at,support_count,reservation_count,threshold,passed,ballot_json "
            "FROM clause_seals WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            return None
        return {"seal_id": row["seal_id"], "sealed_at": row["sealed_at"],
                "support_count": row["support_count"], "reservation_count": row["reservation_count"],
                "threshold": row["threshold"], "passed": bool(row["passed"]),
                "ballot_sha256": content_digest([json.loads(row["ballot_json"])])}

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        row = self._revision(revision_id)
        data = dict(row)
        data["merged"] = data["merged_at"] is not None
        return data

    def get_clause_chain(self, actor_id: str, clause_id: str) -> dict[str, Any]:
        """返回条款的完整版本链：基线、修订、翻译对应、冲突与封存事实。"""

        self._require(actor_id, "report.read")
        clause = dict(self._clause(clause_id))
        clause["independent"] = bool(clause["independent"])
        revisions = []
        for row in self.connection.execute(
            "SELECT * FROM clause_revisions WHERE clause_id=? ORDER BY revision_no", (clause_id,)
        ).fetchall():
            translations = self.connection.execute(
                "SELECT language,content_sha256,correspondence_json FROM revision_translations "
                "WHERE revision_id=? ORDER BY language", (row["revision_id"],)
            ).fetchall()
            supports = self.connection.execute(
                "SELECT participant_id,authorization_id FROM revision_supports WHERE revision_id=? "
                "ORDER BY participant_id", (row["revision_id"],)
            ).fetchall()
            seal = self.connection.execute(
                "SELECT seal_id,passed,sealed_at,support_count,reservation_count,ballot_json "
                "FROM clause_seals WHERE revision_id=?", (row["revision_id"],)
            ).fetchone()
            revisions.append({
                "revision_id": row["revision_id"],
                "revision_no": row["revision_no"],
                "kind": row["kind"],
                "parent_revision_id": row["parent_revision_id"],
                "language": row["language"],
                "title": row["title"],
                "content_sha256": row["content_sha256"],
                "merged": row["merged_at"] is not None,
                "translations": [
                    {"language": item["language"], "content_sha256": item["content_sha256"],
                     "correspondence": json.loads(item["correspondence_json"])}
                    for item in translations
                ],
                "supports": [
                    {"participant_id": item["participant_id"], "authorization_id": item["authorization_id"]}
                    for item in supports
                ],
                "conflicts_with": self._conflict_partners(row["revision_id"]),
                "seal": None if seal is None else {
                    "seal_id": seal["seal_id"], "passed": bool(seal["passed"]),
                    "sealed_at": seal["sealed_at"], "support_count": seal["support_count"],
                    "reservation_count": seal["reservation_count"],
                    "ballot_sha256": content_digest([json.loads(seal["ballot_json"])]),
                },
            })
        return {"clause": clause, "revisions": revisions}

    def _conflict_partners(self, revision_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT revision_a_id,revision_b_id FROM revision_conflicts "
            "WHERE revision_a_id=? OR revision_b_id=?", (revision_id, revision_id)
        ).fetchall()
        return tuple(
            row["revision_b_id"] if row["revision_a_id"] == revision_id else row["revision_a_id"]
            for row in rows
        )

    def audit_trail(self, actor_id: str, entity_type: str | None = None) -> list[dict[str, Any]]:
        """导出事件日志；每条事件附带 basis_sha256 以便外部重放核对。"""

        self._require(actor_id, "audit.read")
        sql = (
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,recorded_at,payload_json,basis_sha256 "
            "FROM event_journal {where} ORDER BY event_id"
        )
        if entity_type:
            rows = self.connection.execute(sql.format(where="WHERE entity_type=?"), (entity_type,)).fetchall()
        else:
            rows = self.connection.execute(sql.format(where="")).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]


def _days_between(start: str, end: str) -> int:
    """按 UTC 日期计算逾期天数（end 晚于 start 时为正）。"""

    from datetime import datetime

    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return (end_dt.date() - start_dt.date()).days

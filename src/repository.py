"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cash_entitlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    instrument TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    per_share REAL NOT NULL,
                    share_quantity INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    posted_amount REAL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    posted_by TEXT,
                    posted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_entitlements_record ON cash_entitlements(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_entitlements_status ON cash_entitlements(status);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], entitlement_op: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            entitlement_note = None
            if entitlement_op is not None:
                if entitlement_op["op"] == "create_pending":
                    ent = entitlement_op["entitlement"]
                    cursor = connection.execute(
                        "INSERT INTO cash_entitlements(record_id,instrument,currency,per_share,share_quantity,amount,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (record_id, ent["instrument"], ent["currency"], float(ent["per_share"]), int(ent["share_quantity"]), round(float(ent["amount"]), 2), "pending", actor_id, now),
                    )
                    payload["cash_entitlement_id"] = int(cursor.lastrowid)
                    entitlement_note = {"cash_entitlement_id": int(cursor.lastrowid), "status": "pending", "amount": round(float(ent["amount"]), 2)}
                elif entitlement_op["op"] == "post":
                    ent_id = int(entitlement_op["entitlement_id"])
                    ent_row = connection.execute("SELECT amount,status FROM cash_entitlements WHERE id=? AND record_id=?", (ent_id, record_id)).fetchone()
                    if ent_row is None:
                        connection.rollback()
                        raise ValidationError("现金权益不存在")
                    if ent_row["status"] != "pending":
                        connection.rollback()
                        raise ValidationError("现金权益已入账")
                    posted_amount = round(float(entitlement_op["posted_amount"]), 2)
                    if posted_amount != round(float(ent_row["amount"]), 2):
                        connection.rollback()
                        raise ValidationError("入账金额与现金权益金额不一致")
                    connection.execute(
                        "UPDATE cash_entitlements SET status='booked',posted_amount=?,posted_by=?,posted_at=? WHERE id=?",
                        (posted_amount, actor_id, now, ent_id),
                    )
                    entitlement_note = {"cash_entitlement_id": ent_id, "status": "booked", "posted_amount": posted_amount}
            if entitlement_note is not None:
                details = dict(details)
                details["cash_entitlement"] = entitlement_note
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    @staticmethod
    def _entitlement_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        amount = round(float(item["amount"]), 2)
        item["reconcilable"] = item["status"] == "booked" and (item["posted_amount"] is None or round(float(item["posted_amount"]), 2) == amount)
        return item

    def list_entitlements(self, record_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(int(record_id))
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cash_entitlements%s ORDER BY id DESC LIMIT ?" % where, tuple(params)
            ).fetchall()
        return [self._entitlement_row(row) for row in rows]

    def get_entitlement(self, entitlement_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cash_entitlements WHERE id=?", (int(entitlement_id),)).fetchone()
        if row is None:
            raise NotFound("现金权益不存在")
        return self._entitlement_row(row)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

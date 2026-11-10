"""
Request store: the ONLY module that talks to SQLite.

Three tables
------------
  requests   one row per purchase request (PR-0001): who, the current status, the current version
  versions   one row per submission of that request: the fields, the 8 results, the agent's action.
             Nothing is ever overwritten, so a reviewer can see what changed between v1 and v2.
  events     the history a person reads: submitted, resubmitted (with what changed), the agent's
             action, the reviewer's decision with the name and the reason.

Rules followed everywhere below (the same as Project 2):
1. Parameterised queries (`?`), never values inside f-strings.
2. Each change and its history events are written in ONE transaction: the history can never
   disagree with the request.
3. Money is stored as text ("46.20") and read back as Decimal: never a float.

Two rules that only exist because requests are about money:
4. **Approved requests use up the budget.** The budget check reads the remaining amount *after*
   the requests that people already approved. Otherwise two requests of 6,000 could each pass a
   10,500 budget line, and both be approved.
5. **Approval re-runs the checks.** Between the agent's check and the reviewer's click, another
   request may have used the budget. The reviewer approves only if the request still passes, and
   only the exact version they looked at.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .agent import Assessment, DecisionError, assess, human_decision
from .checks import CheckResult, Outcome, money
from .data import ReferenceData, reference_data
from .models import Action, Request, Status

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    ref              TEXT UNIQUE,
    requester        TEXT NOT NULL,
    department       TEXT NOT NULL,
    status           TEXT NOT NULL,
    current_version  INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    decided_by       TEXT,
    decision_reason  TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    ref         TEXT NOT NULL REFERENCES requests(ref),
    version     INTEGER NOT NULL,
    at          TEXT NOT NULL,
    fields      TEXT NOT NULL,      -- the Request as JSON (money as text)
    total_usd   TEXT NOT NULL,
    budget_line TEXT,
    results     TEXT NOT NULL,      -- the 8 check results as JSON
    action      TEXT NOT NULL,
    PRIMARY KEY (ref, version)
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ref     TEXT NOT NULL REFERENCES requests(ref),
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
"""

# The requester fixes their own request: these statuses can be resubmitted.
RESUBMITTABLE = {Status.NEEDS_INFO, Status.POLICY_ISSUE, Status.RETURNED}
FINAL = {Status.APPROVED, Status.REJECTED}
LOCKED_FIELDS = ("requester",)       # a resubmission is the same person's request, not a new one


class StoreError(Exception):
    """An action the store refuses, with a message a person can read."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def changes(old: dict, new: dict) -> list[str]:
    """'supplier: (empty) -> Al-Noor Trading' for every field that changed between two versions."""
    def show(v):
        return "(empty)" if v in ("", None) else str(v)
    return [f"{k}: {show(old.get(k))} -> {show(new.get(k))}" for k in new if show(old.get(k)) != show(new.get(k))]


@dataclass
class Version:
    version: int
    at: str
    request: Request
    results: list[CheckResult]
    action: Action


@dataclass
class Record:
    ref: str
    requester: str
    department: str
    status: Status
    current_version: int
    created_at: str
    updated_at: str
    decided_by: str | None
    decision_reason: str | None
    latest: Version                  # the version shown in the form (prefilled for resubmission)


class _Rows(list):
    """A finished query: rows are read immediately, so nothing is read later, outside the lock."""
    def __init__(self, cur: sqlite3.Cursor):
        super().__init__(cur.fetchall())
        self.lastrowid = cur.lastrowid

    def fetchall(self) -> list:
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class SharedConnection(sqlite3.Connection):
    """One connection is shared by every Streamlit session (get_store is cache_resource),
    so it needs one lock. Without it, a reviewer who opens the queue while another session
    is inside submit() can see the new row before its reference (PR-xxxx) is set and crash
    (found on the live server for Project 2, 2026-10-01; Project 3 has the same shape).
    The lock covers a whole transaction (`with conn:`) and every statement, and each query
    is read to the end before the lock is released."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def __enter__(self):
        self._lock.acquire()
        return super().__enter__()

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self._lock.release()

    def execute(self, sql, params=()):
        with self._lock:
            return _Rows(super().execute(sql, params))

    def executescript(self, script):
        with self._lock:
            return super().executescript(script)


class Store:
    def __init__(self, path: str | Path = "procurement.db", ref: ReferenceData | None = None):
        self.path = str(path)
        self._base_ref = ref
        self.conn = sqlite3.connect(self.path, check_same_thread=False, factory=SharedConnection)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def reset(self) -> None:
        """Delete every request (demo button). PR numbers start again at PR-0001."""
        with self.conn:
            for table in ("events", "versions", "requests"):
                self.conn.execute(f"DELETE FROM {table}")            # table names are constants, not input
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('requests', 'events')")

    # --- reference data with the budget already used ---------------------------------------
    def committed_by_line(self) -> dict[str, Decimal]:
        """Total of APPROVED requests per budget line (rule 4)."""
        rows = self.conn.execute(
            "SELECT v.budget_line, v.total_usd FROM requests r JOIN versions v "
            "ON v.ref = r.ref AND v.version = r.current_version WHERE r.status = ?",
            (Status.APPROVED.value,)).fetchall()
        used: dict[str, Decimal] = {}
        for row in rows:
            used[row["budget_line"]] = used.get(row["budget_line"], Decimal("0")) + Decimal(row["total_usd"])
        return used

    def reference(self) -> ReferenceData:
        base = self._base_ref or reference_data()
        used = self.committed_by_line()
        if not used:
            return base
        lines = {code: replace(line, committed=line.committed + used.get(code, Decimal("0")))
                 for code, line in base.budget_lines.items()}
        return replace(base, budget_lines=lines)

    def assess(self, request: Request) -> Assessment:
        """The agent, run against the budget that is really left."""
        return assess(request, self.reference())

    # --- writing ----------------------------------------------------------------------------
    def _event(self, ref: str, actor: str, event: str, detail: str = "", at: str | None = None) -> None:
        self.conn.execute("INSERT INTO events (ref, at, actor, event, detail) VALUES (?, ?, ?, ?, ?)",
                          (ref, at or now(), actor, event, detail))

    def _version(self, ref: str, version: int, a: Assessment, at: str) -> None:
        self.conn.execute(
            "INSERT INTO versions (ref, version, at, fields, total_usd, budget_line, results, action) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ref, version, at, a.request.model_dump_json(), str(a.request.total), a.request.budget_line or None,
             json.dumps([{"code": c.code, "name": c.name, "outcome": c.outcome.value, "message": c.message,
                          "fix": c.fix, "field": c.field} for c in a.results], ensure_ascii=False),
             a.action.value))

    def _agent_event(self, ref: str, a: Assessment, at: str) -> None:
        self._event(ref, "agent", a.action.value, a.headline, at)

    def submit(self, request: Request) -> tuple[str, Assessment]:
        """A new request: check it, save version 1, return its reference (PR-0001)."""
        a = self.assess(request)
        at = now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO requests (requester, department, status, current_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)", (request.requester, request.department, a.status.value, at, at))
            ref = f"PR-{cur.lastrowid:04d}"
            self.conn.execute("UPDATE requests SET ref = ? WHERE seq = ?", (ref, cur.lastrowid))
            self._version(ref, 1, a, at)
            self._event(ref, request.requester, "submitted", f"v1 · {request.item} · {money(request.total)} USD", at)
            self._agent_event(ref, a, at)
        return ref, a

    def resubmit(self, ref: str, request: Request) -> Assessment:
        """The requester fixed the request: a NEW version, checked again from zero (T13)."""
        record = self.get(ref)
        if record.status not in RESUBMITTABLE:
            raise StoreError(f"{ref} is {record.status.value}: only requests that need information, have a "
                             "policy issue or were returned can be resubmitted.")
        old = json.loads(record.latest.request.model_dump_json())
        new = json.loads(request.model_dump_json())
        for f in LOCKED_FIELDS:
            if old[f] != new[f]:
                raise StoreError(f"The {f} cannot change on a resubmission. Submit a new request instead.")
        diff = changes(old, new)
        if not diff:
            raise StoreError("Nothing changed since the last version. Change the request, then resubmit.")
        a = self.assess(request)
        version, at = record.current_version + 1, now()
        with self.conn:
            self._version(ref, version, a, at)
            self.conn.execute("UPDATE requests SET status = ?, current_version = ?, updated_at = ?, "
                              "decided_by = NULL, decision_reason = NULL WHERE ref = ?",
                              (a.status.value, version, at, ref))
            self._event(ref, request.requester, "resubmitted", f"v{version} · " + "; ".join(diff), at)
            self._agent_event(ref, a, at)
        return a

    def decide(self, ref: str, decision: str, reviewer: str, reason: str = "", version: int | None = None) -> Status:
        """The reviewer's `finish` action. `version` is the one they looked at (rule 5)."""
        record = self.get(ref)
        if version is not None and version != record.current_version:
            raise DecisionError(f"{ref} changed while you were reviewing it (now v{record.current_version}). "
                                "Open it again.")
        status = human_decision(record.status, decision, reviewer, record.requester, reason)
        if status is Status.APPROVED:                    # rule 5: still compliant right now?
            again = self.assess(record.latest.request)
            if again.action is not Action.SEND_FOR_REVIEW:
                problems = "; ".join(f"{c.code} {c.message}" for c in again.failed + again.missing)
                raise DecisionError(f"Cannot approve: the request no longer passes the checks. {problems}")
        reviewer = " ".join(reviewer.split())
        at = now()
        with self.conn:
            self.conn.execute("UPDATE requests SET status = ?, updated_at = ?, decided_by = ?, decision_reason = ? "
                              "WHERE ref = ?", (status.value, at, reviewer, reason.strip() or None, ref))
            self._event(ref, reviewer, status.value, reason.strip(), at)
        return status

    # --- reading ----------------------------------------------------------------------------
    def _versions(self, ref: str, only: int | None = None) -> list[Version]:
        sql, args = "SELECT * FROM versions WHERE ref = ?", [ref]
        if only is not None:
            sql, args = sql + " AND version = ?", args + [only]
        out = []
        for row in self.conn.execute(sql + " ORDER BY version", args).fetchall():
            results = [CheckResult(r["code"], r["name"], Outcome(r["outcome"]), r["message"], r["fix"], r["field"])
                       for r in json.loads(row["results"])]
            out.append(Version(row["version"], row["at"], Request.model_validate_json(row["fields"]),
                               results, Action(row["action"])))
        return out

    def get(self, ref: str) -> Record:
        row = self.conn.execute("SELECT * FROM requests WHERE ref = ?", ((ref or "").strip().upper(),)).fetchone()
        if not row:
            raise StoreError(f"No request {ref}.")
        latest = self._versions(row["ref"], row["current_version"])[0]
        return Record(row["ref"], row["requester"], row["department"], Status(row["status"]),
                      row["current_version"], row["created_at"], row["updated_at"], row["decided_by"],
                      row["decision_reason"], latest)

    def versions(self, ref: str) -> list[Version]:
        return self._versions(self.get(ref).ref)

    def history(self, ref: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT at, actor, event, detail FROM events WHERE ref = ? ORDER BY id", (ref,)).fetchall()]

    def list(self, status: Status | None = None, requester: str | None = None) -> list[Record]:
        sql, args = "SELECT ref FROM requests WHERE ref IS NOT NULL", []   # skip a row still being written
        if status:
            sql, args = sql + " AND status = ?", args + [status.value]
        if requester:
            sql, args = sql + " AND lower(requester) = lower(?)", args + [requester.strip()]
        return [self.get(r["ref"]) for r in self.conn.execute(sql + " ORDER BY seq", args).fetchall()]

    def queue(self) -> list[Record]:
        """What reviewers work on: requests the agent sent for human review, oldest first."""
        return self.list(Status.AWAITING_REVIEW)

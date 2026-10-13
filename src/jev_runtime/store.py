"""Append-only application ledger; the runtime owner is the trusted writer."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import canonical, digest


class Store:
    def __init__(self, path, *, readonly=False):
        path = Path(path)
        self.path = path.resolve()
        if readonly:
            if not self.path.is_file():
                raise ValueError(
                    "No saved history at "
                    + str(self.path)
                    + "; start onboarding with your selected output folder first"
                )
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            try:
                self.verify()
            except BaseException:
                self.db.close()
                raise
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
            body TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_kind ON events(kind)")
        for operation in ("UPDATE", "DELETE"):
            self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS no_{operation.lower()}
                BEFORE {operation} ON events BEGIN
                SELECT RAISE(ABORT, 'immutable ledger'); END""")
        self.db.commit()
        self.verify()

    def close(self):
        self.db.close()

    def verify(self):
        previous = "0" * 64
        for seq, identifier, kind, body, prior, hashed in self.db.execute(
            "SELECT * FROM events ORDER BY seq"
        ):
            if prior != previous or digest([identifier, kind, body, prior]) != hashed:
                raise ValueError("Ledger integrity failure")
            previous = hashed

    def append(self, kind, payload):
        return self.append_many([(kind, payload)])[0]

    def append_many(self, records, *, identifiers=None, expected_head=None):
        records = list(records)
        identifiers = (
            list(identifiers) if identifiers is not None else [str(uuid4()) for _ in records]
        )
        if (
            len(identifiers) != len(records)
            or len(set(identifiers)) != len(identifiers)
            or any(not isinstance(identifier, str) or not identifier for identifier in identifiers)
        ):
            raise ValueError("Each atomic record needs a distinct nonempty identifier")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.verify()
            row = self.db.execute("SELECT id,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            if expected_head is not None and (not row or row[0] != expected_head):
                raise ValueError("Ledger changed before recording the reviewed transition; retry")
            previous = row[1] if row else "0" * 64
            for identifier, (kind, payload) in zip(identifiers, records, strict=True):
                body = canonical(
                    {"recorded_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
                )
                hashed = digest([identifier, kind, body, previous])
                self.db.execute(
                    "INSERT INTO events(id,kind,body,previous,hash) VALUES(?,?,?,?,?)",
                    (identifier, kind, body, previous, hashed),
                )
                previous = hashed
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return identifiers

    def events(self, kinds=()):
        self.verify()
        rows = self.db.execute("SELECT seq,id,kind,body FROM events ORDER BY seq")
        return [
            {"seq": seq, "id": identifier, "kind": kind, **json.loads(body)}
            for seq, identifier, kind, body in rows
            if not kinds or kind in kinds
        ]

    def get(self, identifier, kind):
        self.verify()
        row = self.db.execute("SELECT kind,body FROM events WHERE id=?", (identifier,)).fetchone()
        if row is None or row[0] != kind:
            raise ValueError("Missing or wrong record type")
        return json.loads(row[1])["payload"]

    def records(self, kind):
        self.verify()
        return [
            (identifier, json.loads(body)["payload"])
            for identifier, body in self.db.execute(
                "SELECT id,body FROM events WHERE kind=? ORDER BY seq", (kind,)
            )
        ]

    def record_intent(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Human intent cannot be empty")
        return self.append("human_intent", {"text": text, "sha256": digest(text)})

    def intent_source(self, identifier):
        """Resolve a source snapshot without relabeling assistant text as human intent."""
        self.verify()
        row = self.db.execute("SELECT kind FROM events WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Missing human intent")
        if row[0] == "human_intent":
            return self.get(identifier, "human_intent")
        bundle = self.get(identifier, "intent_bundle")
        return {
            "human_statements": [
                {"id": source, **self.get(source, "human_intent")}
                for source in bundle["utterance_ids"]
            ],
            "conversation_context": bundle["context"],
            **(
                {"operator_context": bundle["operator_context"]}
                if "operator_context" in bundle
                else {}
            ),
        }

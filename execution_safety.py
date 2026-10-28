import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ExecutionStateError(RuntimeError):
    pass


class DuplicateIntentError(ExecutionStateError):
    pass


class UnresolvedExecutionError(ExecutionStateError):
    pass


ACTIVE_STATUSES = {
    "PREPARED",
    "SUBMITTING",
    "SUBMISSION_UNKNOWN",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "FILLED_EXIT_REQUIRED",
    "REJECTED_REVIEW",
    "RECOVERY_REQUIRED",
}

TRANSITIONS = {
    "PREPARED": {"SUBMITTING", "ABORTED"},
    "SUBMITTING": {
        "ACKNOWLEDGED",
        "SUBMISSION_UNKNOWN",
        "PARTIALLY_FILLED",
        "FILLED_EXIT_REQUIRED",
        "REJECTED_REVIEW",
        "RECOVERY_REQUIRED",
    },
    "SUBMISSION_UNKNOWN": {
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED_EXIT_REQUIRED",
        "REJECTED_REVIEW",
        "RECOVERY_REQUIRED",
    },
    "ACKNOWLEDGED": {
        "PARTIALLY_FILLED",
        "FILLED_EXIT_REQUIRED",
        "REJECTED_REVIEW",
        "RECOVERY_REQUIRED",
    },
    "PARTIALLY_FILLED": {
        "PARTIALLY_FILLED",
        "FILLED_EXIT_REQUIRED",
        "REJECTED_REVIEW",
        "RECOVERY_REQUIRED",
    },
    "FILLED_EXIT_REQUIRED": {"RECOVERY_REQUIRED"},
    "REJECTED_REVIEW": {"RECOVERY_REQUIRED"},
    "RECOVERY_REQUIRED": set(),
    "ABORTED": set(),
}


def default_journal_path():
    from os import environ

    local_app_data = environ.get("LOCALAPPDATA")
    root = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / ".local" / "share"
    )
    return root / "MarketExpertGemini" / "execution-state.sqlite3"


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


class ExecutionJournal:
    """Durable local order-intent journal; it never communicates with a broker."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_journal_path()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS order_intents (
                        id INTEGER PRIMARY KEY,
                        intent_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        broker_order_id TEXT,
                        filled_quantity INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS order_events (
                        id INTEGER PRIMARY KEY,
                        intent_id INTEGER NOT NULL REFERENCES order_intents(id),
                        occurred_at TEXT NOT NULL,
                        previous_status TEXT,
                        status TEXT NOT NULL,
                        details_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS order_events_intent_id
                        ON order_events(intent_id);
                    """
                )
                connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not initialize the local execution journal: {exc}"
            ) from exc

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _json(value):
        try:
            return json.dumps(value, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Execution journal data must be finite JSON data.") from exc

    def _record_transition(
        self,
        connection,
        row,
        status,
        details,
        broker_order_id=None,
        filled_quantity=None,
        allow_close=False,
    ):
        previous_status = row["status"]
        if status == "CLOSED" and allow_close and previous_status in ACTIVE_STATUSES:
            pass
        elif status not in TRANSITIONS[previous_status]:
            raise ExecutionStateError(
                f"Invalid execution-state transition: "
                f"{previous_status} -> {status}."
            )
        occurred_at = _timestamp()
        connection.execute(
            """
            UPDATE order_intents
            SET status = ?, updated_at = ?,
                broker_order_id = COALESCE(?, broker_order_id),
                filled_quantity = COALESCE(?, filled_quantity)
            WHERE id = ?
            """,
            (
                status,
                occurred_at,
                broker_order_id,
                filled_quantity,
                row["id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO order_events
                (intent_id, occurred_at, previous_status, status, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                occurred_at,
                previous_status,
                status,
                self._json(details),
            ),
        )

    @staticmethod
    def _get_row(connection, intent_key):
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM order_intents WHERE intent_key = ?",
            (intent_key,),
        ).fetchone()

    def create_intent(self, intent_key, payload):
        if not isinstance(intent_key, str) or not intent_key.strip():
            raise ValueError("An idempotency key is required for every intent.")
        if len(intent_key) > 200:
            raise ValueError("The idempotency key must be at most 200 characters.")
        if not isinstance(payload, dict):
            raise ValueError("Order intent payload must be an object.")
        payload_json = self._json(payload)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._get_row(connection, intent_key):
                    raise DuplicateIntentError(
                        "This order intent key already exists; refusing a duplicate."
                    )
                active = connection.execute(
                    "SELECT intent_key, status FROM order_intents "
                    "WHERE status NOT IN ('CLOSED', 'ABORTED') LIMIT 1"
                ).fetchone()
                if active:
                    raise UnresolvedExecutionError(
                        f"Execution {active[0]} is {active[1]}; reconcile it before "
                        "creating another intent."
                    )
                now = _timestamp()
                cursor = connection.execute(
                    """
                    INSERT INTO order_intents
                        (intent_key, created_at, updated_at, status, payload_json)
                    VALUES (?, ?, ?, 'PREPARED', ?)
                    """,
                    (intent_key, now, now, payload_json),
                )
                connection.execute(
                    """
                    INSERT INTO order_events
                        (intent_id, occurred_at, previous_status, status, details_json)
                    VALUES (?, ?, NULL, 'PREPARED', '{}')
                    """,
                    (cursor.lastrowid, now),
                )
            return self.get_intent(intent_key)
        except sqlite3.IntegrityError as exc:
            if "intent_key" in str(exc).lower():
                raise DuplicateIntentError(
                    "This order intent key already exists; refusing a duplicate."
                ) from exc
            raise ExecutionStateError(
                f"Could not create the local order intent: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not create the local order intent: {exc}"
            ) from exc

    def get_intent(self, intent_key):
        try:
            with self._connection() as connection:
                row = self._get_row(connection, intent_key)
                if row is None:
                    return None
                return self._public_intent(row)
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not read the local order intent: {exc}"
            ) from exc

    def unresolved_intents(self):
        try:
            with self._connection() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT intent_key, status, updated_at FROM order_intents "
                    "WHERE status NOT IN ('CLOSED', 'ABORTED') ORDER BY id"
                ).fetchall()
                return [
                    {
                        "intent_key": row["intent_key"],
                        "status": row["status"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not read unresolved executions: {exc}"
            ) from exc

    def assert_no_unresolved(self):
        unresolved = self.unresolved_intents()
        if unresolved:
            details = ", ".join(
                f"{item['intent_key']} ({item['status']})" for item in unresolved
            )
            raise UnresolvedExecutionError(
                "Unresolved execution journal entries require recovery before "
                f"proposals can continue: {details}."
            )

    @staticmethod
    def _public_intent(row):
        return {
            "intent_key": row["intent_key"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]),
            "broker_order_id": row["broker_order_id"],
            "filled_quantity": row["filled_quantity"],
        }

    def _transition(self, intent_key, status, details=None, broker_order_id=None):
        if status not in TRANSITIONS:
            raise ValueError(f"Unknown execution status: {status}.")
        if status == "CLOSED":
            raise ExecutionStateError(
                "Execution can only be closed after a verified flat broker snapshot "
                "and explicit operator review."
            )
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise ValueError("Execution transition details must be an object.")
        details_json = self._json(details)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._get_row(connection, intent_key)
                if row is None:
                    raise ExecutionStateError(
                        f"Execution intent {intent_key!r} does not exist."
                    )
                self._record_transition(
                    connection,
                    row,
                    status,
                    json.loads(details_json),
                    broker_order_id=broker_order_id,
                )
                updated = self._get_row(connection, intent_key)
                return self._public_intent(updated)
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not update the local order intent: {exc}"
            ) from exc

    def mark_submission_started(self, intent_key):
        return self._transition(intent_key, "SUBMITTING")

    def mark_submission_unknown(self, intent_key, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An explanation is required for an unknown submission.")
        return self._transition(
            intent_key,
            "SUBMISSION_UNKNOWN",
            {"reason": reason.strip()},
        )

    def record_acknowledgement(self, intent_key, broker_order_id):
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise ValueError("A broker order ID is required for acknowledgement.")
        return self._transition(
            intent_key,
            "ACKNOWLEDGED",
            broker_order_id=broker_order_id.strip(),
        )

    def record_rejection(self, intent_key, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An explanation is required for a rejected order.")
        return self._transition(
            intent_key,
            "REJECTED_REVIEW",
            {"reason": reason.strip()},
        )

    def record_fill(self, intent_key, filled_quantity):
        if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int):
            raise ValueError("Filled quantity must be a positive integer.")
        if filled_quantity <= 0:
            raise ValueError("Filled quantity must be a positive integer.")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._get_row(connection, intent_key)
                if row is None:
                    raise ExecutionStateError(
                        f"Execution intent {intent_key!r} does not exist."
                    )
                intended_quantity = row["payload_json"]
                try:
                    intended_quantity = json.loads(intended_quantity)["quantity"]
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise ExecutionStateError(
                        "Order intent is missing its required quantity."
                    ) from exc
                if (
                    isinstance(intended_quantity, bool)
                    or not isinstance(intended_quantity, int)
                    or intended_quantity <= 0
                    or filled_quantity < row["filled_quantity"]
                    or filled_quantity > intended_quantity
                ):
                    raise ValueError(
                        "Filled quantity must be monotonic and cannot exceed "
                        "the intended quantity."
                    )
                status = (
                    "FILLED_EXIT_REQUIRED"
                    if filled_quantity == intended_quantity
                    else "PARTIALLY_FILLED"
                )
                self._record_transition(
                    connection,
                    row,
                    status,
                    {"filled_quantity": filled_quantity},
                    filled_quantity=filled_quantity,
                )
                return self._public_intent(self._get_row(connection, intent_key))
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not record the broker fill: {exc}"
            ) from exc

    def recover_after_restart(self):
        """Fail closed after process restart; never infer that an order was unsent."""
        recovered = []
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM order_intents "
                    "WHERE status NOT IN ('CLOSED', 'ABORTED')"
                ).fetchall()
                for row in rows:
                    if row["status"] == "PREPARED":
                        status = "ABORTED"
                        details = {"reason": "Process restarted before submission."}
                    elif row["status"] == "RECOVERY_REQUIRED":
                        continue
                    else:
                        status = "RECOVERY_REQUIRED"
                        details = {
                            "previous_status": row["status"],
                            "reason": (
                                "Process restarted; reconcile broker state manually. "
                                "Do not resubmit this intent."
                            ),
                        }
                    self._record_transition(
                        connection, row, status, details
                    )
                    recovered.append(row["intent_key"])
            return recovered
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not recover the local execution journal: {exc}"
            ) from exc

    def resolve_flat_after_manual_review(
        self, intent_key, broker_snapshot, operator_confirmed=False
    ):
        if operator_confirmed is not True:
            raise ExecutionStateError(
                "An explicit operator confirmation is required after reviewing "
                "the current broker snapshot."
            )
        if not isinstance(broker_snapshot, dict):
            raise ExecutionStateError("A read-only broker snapshot is required.")
        captured_at = broker_snapshot.get("captured_at")
        try:
            captured = datetime.fromisoformat(captured_at)
            if captured.tzinfo is None:
                raise ValueError("Snapshot timestamp must include a timezone.")
            snapshot_age = datetime.now(timezone.utc) - captured.astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ExecutionStateError(
                "A valid timezone-aware broker snapshot timestamp is required."
            ) from exc
        if snapshot_age < timedelta(0) or snapshot_age > timedelta(minutes=2):
            raise ExecutionStateError(
                "Broker snapshot is stale; run a fresh read-only reconciliation."
            )
        required_lists = (
            "pending_orders",
            "partial_fill_orders",
            "rejected_orders",
            "uncertain_orders",
            "positions",
            "discrepancies",
        )
        if (
            broker_snapshot.get("read_only") is not True
            or broker_snapshot.get("status") != "NO_DISCREPANCY_DETECTED"
            or any(not isinstance(broker_snapshot.get(key), list) for key in required_lists)
            or any(
                position.get("net_quantity") != 0
                for position in broker_snapshot["positions"]
                if isinstance(position, dict)
            )
            or any(
                not isinstance(position, dict)
                for position in broker_snapshot["positions"]
            )
            or any(broker_snapshot[key] for key in required_lists if key != "positions")
        ):
            raise ExecutionStateError(
                "Broker state is not a verified flat, discrepancy-free snapshot."
            )
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._get_row(connection, intent_key)
                if row is None:
                    raise ExecutionStateError(
                        f"Execution intent {intent_key!r} does not exist."
                    )
                self._record_transition(
                    connection,
                    row,
                    "CLOSED",
                    {"reason": "Operator reviewed a current flat broker snapshot."},
                    allow_close=True,
                )
                return self._public_intent(self._get_row(connection, intent_key))
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not resolve the local execution intent: {exc}"
            ) from exc

    def events(self, intent_key):
        try:
            with self._connection() as connection:
                connection.row_factory = sqlite3.Row
                row = self._get_row(connection, intent_key)
                if row is None:
                    return []
                event_rows = connection.execute(
                    """
                    SELECT occurred_at, previous_status, status, details_json
                    FROM order_events WHERE intent_id = ? ORDER BY id
                    """,
                    (row["id"],),
                ).fetchall()
                return [
                    {
                        "occurred_at": event["occurred_at"],
                        "previous_status": event["previous_status"],
                        "status": event["status"],
                        "details": json.loads(event["details_json"]),
                    }
                    for event in event_rows
                ]
        except sqlite3.Error as exc:
            raise ExecutionStateError(
                f"Could not read execution journal history: {exc}"
            ) from exc

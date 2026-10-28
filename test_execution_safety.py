from datetime import datetime, timezone

import pytest

from execution_safety import (
    DuplicateIntentError,
    ExecutionJournal,
    ExecutionStateError,
    UnresolvedExecutionError,
)


def make_journal(tmp_path):
    return ExecutionJournal(tmp_path / "execution.sqlite3")


def make_snapshot(**overrides):
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "status": "NO_DISCREPANCY_DETECTED",
        "pending_orders": [],
        "partial_fill_orders": [],
        "rejected_orders": [],
        "uncertain_orders": [],
        "positions": [{"symbol": "ABC", "net_quantity": 0}],
        "discrepancies": [],
    }
    snapshot.update(overrides)
    return snapshot


def test_intent_key_prevents_duplicate_and_unresolved_intents_block_new_entries(tmp_path):
    journal = make_journal(tmp_path)
    journal.create_intent("session-1:ABC:BUY", {"quantity": 2})

    with pytest.raises(DuplicateIntentError, match="refusing a duplicate"):
        journal.create_intent("session-1:ABC:BUY", {"quantity": 2})

    journal.mark_submission_started("session-1:ABC:BUY")
    journal.mark_submission_unknown("session-1:ABC:BUY", "connection timeout")

    with pytest.raises(UnresolvedExecutionError, match="reconcile it"):
        journal.create_intent("session-1:XYZ:BUY", {"quantity": 1})


def test_partial_and_full_fills_are_durable_and_require_exit_review(tmp_path):
    journal = make_journal(tmp_path)
    journal.create_intent("intent-1", {"quantity": 3})
    journal.mark_submission_started("intent-1")
    journal.record_acknowledgement("intent-1", "broker-123")

    partial = journal.record_fill("intent-1", 1)
    assert partial["status"] == "PARTIALLY_FILLED"
    assert partial["filled_quantity"] == 1
    journal.record_fill("intent-1", 2)
    with pytest.raises(ValueError, match="monotonic"):
        journal.record_fill("intent-1", 1)

    filled = journal.record_fill("intent-1", 3)
    assert filled["status"] == "FILLED_EXIT_REQUIRED"
    assert filled["filled_quantity"] == 3
    with pytest.raises(UnresolvedExecutionError):
        journal.create_intent("intent-2", {"quantity": 1})


def test_fill_event_can_arrive_before_submission_acknowledgement(tmp_path):
    journal = make_journal(tmp_path)
    journal.create_intent("intent-1", {"quantity": 2})
    journal.mark_submission_started("intent-1")

    filled = journal.record_fill("intent-1", 2)

    assert filled["status"] == "FILLED_EXIT_REQUIRED"
    assert filled["broker_order_id"] is None


def test_restart_never_retries_uncertain_order_and_requires_reconciliation(tmp_path):
    path = tmp_path / "execution.sqlite3"
    journal = ExecutionJournal(path)
    journal.create_intent("intent-1", {"quantity": 1})
    journal.mark_submission_started("intent-1")

    restarted = ExecutionJournal(path)
    assert restarted.recover_after_restart() == ["intent-1"]
    assert restarted.get_intent("intent-1")["status"] == "RECOVERY_REQUIRED"
    with pytest.raises(ExecutionStateError, match="Invalid execution-state transition"):
        restarted.mark_submission_started("intent-1")
    with pytest.raises(UnresolvedExecutionError):
        restarted.create_intent("intent-2", {"quantity": 1})


def test_supervised_startup_guard_detects_and_blocks_restart_recovery(
    tmp_path, monkeypatch
):
    import importlib.util
    from pathlib import Path

    module_path = Path(__file__).with_name("trading-agent.py")
    spec = importlib.util.spec_from_file_location("trading_agent_guard_test", module_path)
    trading_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trading_agent)

    path = tmp_path / "execution.sqlite3"
    journal = ExecutionJournal(path)
    journal.create_intent("intent-1", {"quantity": 1})
    journal.mark_submission_started("intent-1")

    monkeypatch.setenv("ANGELONE_TRADING_MODE", "supervised")
    monkeypatch.setenv("ANGELONE_PLACE_ORDER", "false")
    monkeypatch.setattr(
        trading_agent, "ExecutionJournal", lambda: ExecutionJournal(path)
    )

    with pytest.raises(UnresolvedExecutionError, match="recovery before proposals"):
        trading_agent.main()
    assert ExecutionJournal(path).get_intent("intent-1")["status"] == "RECOVERY_REQUIRED"


def test_restart_aborts_intent_that_was_never_marked_for_submission(tmp_path):
    journal = make_journal(tmp_path)
    journal.create_intent("prepared-only", {"quantity": 1})

    assert journal.recover_after_restart() == ["prepared-only"]
    assert journal.get_intent("prepared-only")["status"] == "ABORTED"
    assert journal.create_intent("next-intent", {"quantity": 1})["status"] == "PREPARED"


def test_only_explicit_review_of_flat_read_only_snapshot_can_close_intent(tmp_path):
    journal = make_journal(tmp_path)
    journal.create_intent("intent-1", {"quantity": 1})
    journal.mark_submission_started("intent-1")
    journal.mark_submission_unknown("intent-1", "timeout")
    journal.recover_after_restart()

    with pytest.raises(ExecutionStateError, match="explicit operator confirmation"):
        journal.resolve_flat_after_manual_review(
            "intent-1", make_snapshot(), operator_confirmed=False
        )
    with pytest.raises(ExecutionStateError, match="verified flat"):
        journal.resolve_flat_after_manual_review(
            "intent-1",
            make_snapshot(positions=[{"symbol": "ABC", "net_quantity": 1}]),
            operator_confirmed=True,
        )
    with pytest.raises(ExecutionStateError, match="verified flat"):
        journal.resolve_flat_after_manual_review(
            "intent-1",
            make_snapshot(pending_orders=[{"order_id": "pending-1"}]),
            operator_confirmed=True,
        )

    resolved = journal.resolve_flat_after_manual_review(
        "intent-1", make_snapshot(), operator_confirmed=True
    )
    assert resolved["status"] == "CLOSED"
    assert journal.events("intent-1")[-1]["status"] == "CLOSED"


def test_stale_broker_snapshot_cannot_resolve_execution(tmp_path):
    journal = make_journal(tmp_path)
    journal.create_intent("intent-1", {"quantity": 1})

    with pytest.raises(ExecutionStateError, match="stale"):
        journal.resolve_flat_after_manual_review(
            "intent-1",
            make_snapshot(captured_at="2020-01-01T00:00:00+00:00"),
            operator_confirmed=True,
        )


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True])
def test_fill_quantity_must_be_a_positive_bounded_integer(tmp_path, quantity):
    journal = make_journal(tmp_path)
    journal.create_intent("intent-1", {"quantity": 1})
    journal.mark_submission_started("intent-1")
    journal.record_acknowledgement("intent-1", "broker-123")

    with pytest.raises(ValueError):
        journal.record_fill("intent-1", quantity)


def test_intent_payload_rejects_non_finite_values(tmp_path):
    journal = make_journal(tmp_path)

    with pytest.raises(ValueError, match="finite JSON"):
        journal.create_intent("intent-1", {"quantity": 1, "price": float("nan")})

import base64
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import local_vault

module_path = Path(__file__).with_name("trading-agent.py")
spec = importlib.util.spec_from_file_location("trading_agent", str(module_path))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_candles(changes):
    candles = []
    close = 100.0
    start = datetime.now(module.MARKET_TIMEZONE).replace(
        hour=9, minute=15, second=0, microsecond=0
    )
    for index, change in enumerate(changes):
        open_price = close
        close += change
        high = max(open_price, close) + 0.15
        low = min(open_price, close) - 0.15
        candles.append([
            (start + timedelta(minutes=5 * index)).isoformat(),
            open_price,
            high,
            low,
            close,
            1000,
        ])
    return candles


def test_calculate_indicators_and_find_bullish_confluence():
    changes = [change for _ in range(15) for change in (0.2, -0.1, -0.05)]
    candles = make_candles(changes)

    indicators = module.calculate_indicators(candles)
    plan = module.analyze_market("RELIANCE", indicators, max_risk=100, max_quantity=5)

    assert indicators["ema_short"] > indicators["ema_long"]
    assert indicators["rsi"] == pytest.approx(51.61, abs=0.1)
    assert plan["action"] == "BUY"
    assert plan["quantity"] <= 5
    assert plan["risk"] <= 100
    assert plan["target"] > plan["entry_price"] > plan["stop_loss"]
    assert plan["stop_loss"] == pytest.approx(round(plan["entry_price"] * 0.99, 2))
    assert plan["target"] == pytest.approx(round(plan["entry_price"] * 1.02, 2))


def test_position_size_respects_configured_lot_size():
    changes = [change for _ in range(15) for change in (0.2, -0.1, -0.05)]
    plan = module.analyze_market(
        "RELIANCE",
        module.calculate_indicators(make_candles(changes)),
        max_risk=100,
        max_quantity=50,
        lot_size=10,
    )

    assert plan["quantity"] % 10 == 0
    assert plan["quantity"] <= 50


def test_conflicting_or_overbought_indicators_produce_hold():
    candles = make_candles([0.3] * 30)
    indicators = module.calculate_indicators(candles)

    assert indicators["rsi"] == 100
    assert module.analyze_market("RELIANCE", indicators, 100, 5)["action"] == "HOLD"


def test_bearish_confluence_produces_sell():
    changes = [change for _ in range(15) for change in (-0.2, 0.1, 0.05)]
    indicators = module.calculate_indicators(make_candles(changes))

    assert indicators["ema_short"] < indicators["ema_long"]
    assert 30 < indicators["rsi"] <= 50
    assert module.analyze_market("TCS", indicators, 100, 5)["action"] == "SELL"


def test_flat_market_has_neutral_rsi_and_holds():
    indicators = module.calculate_indicators(make_candles([0] * 30))

    assert indicators["rsi"] == 50
    assert module.analyze_market("RELIANCE", indicators, 100, 5)["action"] == "HOLD"


def test_insufficient_candles_are_rejected():
    with pytest.raises(ValueError, match="At least"):
        module.calculate_indicators(make_candles([0.1] * 10))


def test_manual_trade_plan_requires_directionally_valid_stops():
    with pytest.raises(ValueError, match="ordered consistently"):
        module.generate_trade_plan(
            "RELIANCE", "bullish", "BANKING", "trend", 100, 102, 108, 1, 100
        )


def test_paper_ledger_opens_then_closes_on_stop(tmp_path):
    ledger_path = tmp_path / "paper_trades.json"
    first_candles = make_candles([0.1] * 30)
    plan = {
        "symbol": "RELIANCE",
        "action": "BUY",
        "entry_price": 103,
        "stop_loss": 100,
        "target": 109,
        "quantity": 1,
    }

    opened = module.update_paper_ledger(first_candles, plan, ledger_path)
    assert opened["open_trade"]["status"] == "OPEN"

    later_candle = [
        (datetime.fromisoformat(first_candles[-1][0]) + timedelta(minutes=5)).isoformat(),
        102,
        103,
        99,
        100,
        1000,
    ]
    closed = module.update_paper_ledger(first_candles + [later_candle], {"symbol": "RELIANCE", "action": "HOLD"}, ledger_path)

    assert closed["open_trade"]["status"] == "CLOSED"
    assert closed["open_trade"]["exit_reason"] == "STOP_LOSS"
    assert closed["realized_pnl"] == -3


def test_paper_closing_tracks_net_pnl_and_elapsed_time(tmp_path):
    candles = make_candles([0.1] * 30)
    plan = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "entry_price": 103,
        "stop_loss": 100,
        "target": 109,
        "quantity": 1,
    }
    ledger_path = tmp_path / "paper_trades.json"
    module.update_paper_ledger(candles, plan, ledger_path)

    later_candle = [
        (datetime.fromisoformat(candles[-1][0]) + timedelta(minutes=5)).isoformat(),
        102,
        103,
        99,
        100,
        1000,
    ]
    ledger = module.update_paper_ledger(
        candles + [later_candle],
        {"symbol": "RELIANCE", "action": "HOLD"},
        ledger_path,
        fee_estimator=lambda position, exit_price: 1.25,
    )
    trade = ledger["open_trade"]

    assert trade["gross_pnl"] == -3
    assert trade["fees_and_taxes"] == 1.25
    assert trade["net_pnl"] == -4.25
    assert trade["duration_seconds"] == 300


def test_paper_monitor_status_persists_explanation_without_order_claims(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(module, "PAPER_STATUS_PATH", status_path)

    module.write_paper_monitor_status(
        "running",
        decision={
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "action": "HOLD",
            "reasoning": "EMA, VWAP, and RSI do not align.",
            "market_indicators": {
                "close": 100,
                "ema_short": 99,
                "ema_long": 101,
                "vwap": 100,
                "rsi": 48,
                "atr": 1.2,
                "candle_time": "2026-10-20T13:00:00+05:30",
            },
        },
        ledger={"open_trade": None},
    )

    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["latest_decision"]["action"] == "HOLD"
    assert saved["latest_decision"]["market_indicators"]["rsi"] == 48
    assert "no broker order was submitted" in saved["latest_decision"]["paper_execution"]


def test_report_identifies_paper_mode_and_session_outcomes():
    report = module.build_end_of_day_report(
        datetime(2026, 10, 18).date(),
        "paper",
        [],
        {"available_cash": 10000, "net": 10000, "utilised_margin": 0},
    )

    assert "PAPER TRADING REPORT" in report
    assert "Stop-loss exits: 0" in report
    assert "Target exits: 0" in report
    assert "Available cash/margin at report time: Rs 10000.00" in report


def test_margin_and_charge_estimators_use_intraday_orders(monkeypatch):
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.10")
    requests = []

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        if "margin/v1/batch" in url:
            return Response({"status": True, "data": {"totalmarginrequired": 2500}})
        return Response({"status": True, "data": {"summary": {"total_charges": 12.5}}})

    monkeypatch.setattr(module.requests, "post", fake_post)

    margin = module.estimate_required_margin(
        "jwt", "key", "NFO", "12345", "BUY", 200, 50
    )
    fees = module.estimate_round_trip_charges(
        "jwt", "key", "NFO", "OPTSTK", "12345", "BUY", 200, 204, 50
    )

    assert margin == 2500
    assert fees == 12.5
    assert requests[0][1]["json"][0]["producttype"] == "INTRADAY"
    charge_orders = requests[1][1]["json"]["orders"]
    assert len(charge_orders) == 2
    assert [order["transaction_type"] for order in charge_orders] == ["BUY", "SELL"]
    assert all(order["product_type"] == "INTRADAY" for order in charge_orders)


def test_read_only_reconciliation_uses_only_get_requests_and_matches_fills(monkeypatch):
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.10")
    calls = []

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    order_rows = [{
        "orderid": "order-1",
        "tradingsymbol": "RELIANCE-EQ",
        "exchange": "NSE",
        "symboltoken": "2885",
        "transactiontype": "BUY",
        "producttype": "INTRADAY",
        "ordertype": "MARKET",
        "quantity": "2",
        "filledshares": "2",
        "averageprice": "100.5",
        "orderstatus": "complete",
    }]
    trade_rows = [{
        "orderid": "order-1",
        "tradingsymbol": "RELIANCE-EQ",
        "exchange": "NSE",
        "symboltoken": "2885",
        "transactiontype": "BUY",
        "producttype": "INTRADAY",
        "fillsize": "2",
        "fillprice": "100.5",
        "filltime": "10:15:00",
    }]
    position_rows = [{
        "tradingsymbol": "RELIANCE-EQ",
        "exchange": "NSE",
        "symboltoken": "2885",
        "producttype": "INTRADAY",
        "netqty": "2",
        "buyqty": "2",
        "sellqty": "0",
        "buyavgprice": "100.5",
        "sellavgprice": "0",
    }]
    data_by_path = {
        "/rest/secure/angelbroking/order/v1/getOrderBook": order_rows,
        "/rest/secure/angelbroking/order/v1/getTradeBook": trade_rows,
        "/rest/secure/angelbroking/order/v1/getPosition": position_rows,
    }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        endpoint = next(path for path in data_by_path if url.endswith(path))
        return Response({"status": True, "data": data_by_path[endpoint]})

    monkeypatch.setattr(module.requests, "get", fake_get)
    result = module.get_broker_reconciliation("jwt", "api")

    assert result["read_only"] is True
    assert result["status"] == "NO_DISCREPANCY_DETECTED"
    assert result["pending_order_count"] == 0
    assert result["partial_fill_order_count"] == 0
    assert result["rejected_order_count"] == 0
    assert result["positions"][0]["reconciliation"] == "MATCHED"
    assert len(calls) == 3
    assert all("json" not in kwargs and kwargs["timeout"] == 30 for _, kwargs in calls)
    assert all("placeOrder" not in url and "cancelOrder" not in url for url, _ in calls)


def test_broker_instrument_verification_requires_exact_exchange_symbol_and_token(
    monkeypatch,
):
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.10")
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": True,
                "data": [{
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE-EQ",
                    "symboltoken": "2885",
                }],
            }

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(module.requests, "post", fake_post)

    result = module.verify_broker_instrument(
        "jwt", "api", "NSE", "RELIANCE-EQ", "2885"
    )

    assert result["verified"] is True
    assert result["symbol"] == "RELIANCE-EQ"
    assert calls[0][1]["json"] == {
        "exchange": "NSE",
        "searchscrip": "RELIANCE-EQ",
    }
    assert "searchScrip" in calls[0][0]


def test_broker_instrument_verification_rejects_token_mismatch(monkeypatch):
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.10")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": True,
                "data": [{
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE-EQ",
                    "symboltoken": "wrong-token",
                }],
            }

    monkeypatch.setattr(module.requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(RuntimeError, match="did not resolve to exactly one"):
        module.verify_broker_instrument(
            "jwt", "api", "NSE", "RELIANCE-EQ", "2885"
        )


def test_read_only_reconciliation_flags_pending_orders_and_position_mismatch():
    orders = [{
        "orderid": "order-1",
        "tradingsymbol": "ABC",
        "exchange": "NSE",
        "symboltoken": "123",
        "transactiontype": "BUY",
        "producttype": "INTRADAY",
        "quantity": "2",
        "filledshares": "0",
        "averageprice": "0",
        "orderstatus": "open",
    }]
    trades = [{
        "orderid": "order-2",
        "tradingsymbol": "ABC",
        "exchange": "NSE",
        "symboltoken": "123",
        "transactiontype": "BUY",
        "producttype": "INTRADAY",
        "fillsize": "3",
        "fillprice": "100",
    }]
    positions = [{
        "tradingsymbol": "ABC",
        "exchange": "NSE",
        "symboltoken": "123",
        "producttype": "INTRADAY",
        "netqty": "2",
    }]

    result = module.build_broker_reconciliation(orders, trades, positions)

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["pending_order_count"] == 1
    assert result["positions"][0]["reconciliation"] == "MISMATCH"
    assert result["discrepancies"][0]["status"] == "MISMATCH"
    assert "not proof" in result["warning"]


@pytest.mark.parametrize(
    ("status", "filled_quantity", "expected_bucket"),
    [
        ("partial", "1", "partial_fill_orders"),
        ("rejected", "0", "rejected_orders"),
    ],
)
def test_read_only_reconciliation_flags_partial_and_rejected_orders(
    status, filled_quantity, expected_bucket
):
    orders = [{
        "orderid": "order-review",
        "tradingsymbol": "ABC",
        "exchange": "NSE",
        "symboltoken": "123",
        "transactiontype": "BUY",
        "producttype": "INTRADAY",
        "ordertype": "MARKET",
        "quantity": "2",
        "filledshares": filled_quantity,
        "averageprice": "100",
        "orderstatus": status,
    }]

    result = module.build_broker_reconciliation(orders, [], [])

    assert result[expected_bucket][0]["order_id"] == "order-review"
    assert result["status"] == "REVIEW_REQUIRED"
    if expected_bucket == "partial_fill_orders":
        assert result["pending_orders"] == result["partial_fill_orders"]


def test_proposal_gate_allows_only_verified_flat_clear_state():
    snapshot = {
        "read_only": True,
        "pending_orders": [],
        "partial_fill_orders": [],
        "rejected_orders": [],
        "uncertain_orders": [],
        "positions": [{
            "symbol": "ABC",
            "net_quantity": 0,
        }],
        "discrepancies": [],
    }

    assert module.require_clear_broker_state_for_proposals(snapshot) is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pending_orders", [{"symbol": "ABC"}], "pending order"),
        ("partial_fill_orders", [{"symbol": "ABC"}], "partial-fill order"),
        ("rejected_orders", [{"symbol": "ABC"}], "rejected order"),
        ("uncertain_orders", [{"symbol": "ABC"}], "unknown status"),
        ("positions", [{"symbol": "ABC", "net_quantity": 1}], "open/unknown position"),
        ("positions", [{"symbol": "ABC", "net_quantity": "unknown"}], "open/unknown position"),
        ("discrepancies", [{"symbol": "ABC"}], "reconciliation discrepancy"),
    ],
)
def test_proposal_gate_blocks_unsafe_or_uncertain_broker_state(
    field, value, message
):
    snapshot = {
        "read_only": True,
        "pending_orders": [],
        "partial_fill_orders": [],
        "rejected_orders": [],
        "uncertain_orders": [],
        "positions": [],
        "discrepancies": [],
    }
    snapshot[field] = value

    with pytest.raises(RuntimeError, match=message):
        module.require_clear_broker_state_for_proposals(snapshot)


def test_proposal_gate_fails_closed_on_missing_snapshot_fields():
    with pytest.raises(RuntimeError, match="pending-order state is incomplete"):
        module.require_clear_broker_state_for_proposals({"read_only": True})


def test_zero_position_without_tradebook_row_is_not_misreported_as_discrepancy():
    positions = [{
        "tradingsymbol": "ABC",
        "exchange": "NSE",
        "symboltoken": "123",
        "producttype": "INTRADAY",
        "netqty": "0",
    }]

    result = module.build_broker_reconciliation([], [], positions)

    assert result["discrepancies"] == []
    assert result["status"] == "NO_DISCREPANCY_DETECTED"


@pytest.mark.parametrize(
    ("orders", "trades", "positions", "message"),
    [
        ([{"orderstatus": "complete"}], [], [], "required tradingsymbol"),
        ([], [{"transactiontype": "BUY"}], [], "required exchange"),
        ([], [], [{"exchange": "NSE"}], "required symboltoken"),
    ],
)
def test_read_only_reconciliation_rejects_incomplete_broker_records(
    orders, trades, positions, message
):
    with pytest.raises(RuntimeError, match=message):
        module.build_broker_reconciliation(orders, trades, positions)


def test_read_only_api_rejects_wrong_data_shape(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"status": True, "data": {"unexpected": "object"}}

    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: Response())
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.10")

    with pytest.raises(RuntimeError, match="non-list data"):
        module._read_only_api_rows(
            "jwt",
            "api",
            "/rest/secure/angelbroking/order/v1/getPosition",
            "Position request",
        )


def test_read_only_api_fails_closed_on_http_error(monkeypatch):
    class Response:
        status_code = 401

        @staticmethod
        def json():
            return {"status": False, "message": "Unauthorized"}

    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: Response())
    monkeypatch.setenv("ANGELONE_CLIENT_PUBLIC_IP", "203.0.113.10")

    with pytest.raises(RuntimeError, match="Order book request failed: Unauthorized"):
        module._read_only_api_rows(
            "jwt",
            "api",
            "/rest/secure/angelbroking/order/v1/getOrderBook",
            "Order book request",
        )


def test_broker_api_json_rejects_non_object_response():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return []

    with pytest.raises(RuntimeError, match="invalid response shape"):
        module._api_json(Response(), "Read-only account request")


def test_email_report_prompts_securely_for_missing_app_password(monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("REPORT_TO_EMAIL", "recipient@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "test-app-password")
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["connection"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(module.smtplib, "SMTP", FakeSMTP)

    module.email_end_of_day_report("PAPER TRADING REPORT")

    assert sent["connection"] == ("smtp.gmail.com", 587, 30)
    assert sent["tls"] is True
    assert sent["login"] == ("sender@example.com", "test-app-password")
    assert sent["message"]["From"] == "sender@example.com"
    assert sent["message"]["To"] == "recipient@example.com"


def test_test_email_subject_is_not_mislabeled_as_live_report(monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "test-app-password")
    subject = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            subject["value"] = message["Subject"]

    monkeypatch.setattr(module.smtplib, "SMTP", FakeSMTP)
    module.send_test_email()

    assert "EMAIL DELIVERY TEST" in subject["value"]


def test_generate_totp_matches_rfc6238_sha1_vector():
    assert module.generate_totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", timestamp=59) == "287082"


def test_generate_totp_rejects_invalid_secret():
    with pytest.raises(ValueError, match="Base32"):
        module.generate_totp("not-a-valid-base32-secret!")


def test_supervised_session_requires_exact_premarket_authorization(capsys):
    session_date = datetime(2026, 10, 20).date()
    authorized = module.request_supervised_session_authorization(
        session_date,
        10000,
        approval_input=lambda prompt: "AUTHORIZE PROPOSAL SESSION 2026-10-20",
    )
    output = capsys.readouterr().out

    assert authorized is True
    assert "Rs 10000.00" in output
    assert "Rs 100.00" in output
    assert "will not place" in output


def test_supervised_session_rejects_inexact_authorization():
    authorized = module.request_supervised_session_authorization(
        datetime(2026, 10, 20).date(),
        10000,
        approval_input=lambda prompt: "yes",
    )

    assert authorized is False


def test_supervised_proposal_is_display_only(capsys):
    plan = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "entry_price": 100,
        "stop_loss": 99,
        "target": 102,
        "quantity": 1,
        "margin_required": 1000,
        "estimated_charges_at_target": 2,
        "estimated_net_profit_at_target": 0,
        "estimated_charges_at_stop": 2,
        "max_proposal_loss": 10,
    }
    margins = {"available_cash": 5000}

    module.request_supervised_proposal(
        plan,
        margins,
        proposal_risk=3,
        remaining_risk=72,
    )
    output = capsys.readouterr().out

    assert "entry_price" in output
    assert "stop_loss" in output
    assert "available_margin" in output
    assert "proposal_risk_reserved" in output
    assert "No order was submitted." in output


def test_proposal_stop_risk_includes_charges():
    plan = {
        "entry_price": 100,
        "stop_loss": 99,
        "quantity": 1,
        "estimated_charges_at_stop": 2,
    }

    assert module.estimate_proposal_stop_risk(plan) == 3


def test_one_percent_margin_budget_is_validated():
    assert module.one_percent_of_margin(10000) == 100
    assert module.one_percent_of_margin(123.45) == pytest.approx(1.2345)
    with pytest.raises(ValueError, match="positive finite"):
        module.one_percent_of_margin(0)


def test_supervised_proposal_sizing_uses_one_percent_required_margin():
    plan = {
        "action": "BUY",
        "entry_price": 100,
        "stop_loss": 99,
        "target": 102,
        "quantity": 5,
    }
    checked = []

    def margin_estimator(action, price, quantity):
        return quantity * 200

    def charge_estimator(action, entry, exit_price, quantity):
        checked.append((exit_price, quantity))
        return 0.5 * quantity

    sized = module.size_supervised_proposal(
        plan,
        max_quantity=5,
        lot_size=1,
        available_margin=2000,
        margin_estimator=margin_estimator,
        charge_estimator=charge_estimator,
    )

    assert sized["quantity"] == 5
    assert sized["margin_required"] == 1000
    assert sized["max_proposal_loss"] == 10
    assert module.estimate_proposal_stop_risk(sized) == 7.5
    assert checked == [(99, 5), (102, 5)]


def test_supervised_proposal_sizing_holds_when_risk_exceeds_one_percent_margin():
    plan = {
        "action": "BUY",
        "entry_price": 100,
        "stop_loss": 99,
        "target": 102,
        "quantity": 2,
    }

    sized = module.size_supervised_proposal(
        plan,
        max_quantity=2,
        lot_size=1,
        available_margin=10000,
        margin_estimator=lambda action, price, quantity: quantity * 50,
        charge_estimator=lambda action, entry, exit_price, quantity: 0,
    )

    assert sized is None


def test_supervised_proposal_sizing_reduces_quantity_to_respect_risk_cap():
    plan = {
        "action": "BUY",
        "entry_price": 100,
        "stop_loss": 99,
        "target": 102,
        "quantity": 5,
    }

    sized = module.size_supervised_proposal(
        plan,
        max_quantity=5,
        lot_size=1,
        available_margin=900,
        margin_estimator=lambda action, price, quantity: quantity * 200,
        charge_estimator=lambda action, entry, exit_price, quantity: 0.5 * quantity,
    )

    assert sized["quantity"] == 4
    assert sized["margin_required"] == 800
    assert sized["max_proposal_loss"] == 8
    assert module.estimate_proposal_stop_risk(sized) == 6


def test_backtest_enters_next_open_and_includes_optional_round_trip_fees(monkeypatch):
    candles = make_candles([0] * 22)
    entry_time = (
        datetime.fromisoformat(candles[-1][0]) + timedelta(minutes=5)
    ).isoformat()
    candles.append([entry_time, 100, 102.5, 99.9, 102.25, 1000])
    seen = []

    def fake_analyze(symbol, indicators, max_risk, max_quantity, lot_size):
        seen.append(indicators["candle_time"])
        return {"action": "BUY"}

    monkeypatch.setattr(module, "analyze_market", fake_analyze)
    result = module.backtest_strategy(
        candles,
        "RELIANCE",
        max_risk=200,
        max_quantity=2,
        round_trip_fee_per_unit=0.5,
    )

    assert seen == [candles[-2][0]]
    assert result["trades"] == 1
    trade = result["trades_detail"][0]
    assert trade["entry_price"] == 100
    assert trade["exit_price"] == 102
    assert trade["exit_reason"] == "TARGET"
    assert trade["gross_pnl"] == 4
    assert trade["estimated_fees"] == 1
    assert trade["net_pnl"] == 3
    assert result["net_pnl"] == 3


def test_backtest_assumes_stop_before_target_and_marks_fees_unknown(monkeypatch):
    candles = make_candles([0] * 22)
    entry_time = (
        datetime.fromisoformat(candles[-1][0]) + timedelta(minutes=5)
    ).isoformat()
    candles.append([entry_time, 100, 102.5, 98.5, 101, 1000])
    monkeypatch.setattr(
        module,
        "analyze_market",
        lambda *args: {"action": "BUY"},
    )

    result = module.backtest_strategy(
        candles, "RELIANCE", max_risk=200, max_quantity=2
    )

    assert result["trades_detail"][0]["exit_reason"] == "STOP_LOSS"
    assert result["trades_detail"][0]["gross_pnl"] == -2
    assert result["trades_detail"][0]["net_pnl"] is None
    assert result["net_pnl"] is None
    assert result["fees_included"] is False


def test_backtest_rejects_out_of_order_candles():
    candles = make_candles([0] * 22)
    candles.append([
        (datetime.fromisoformat(candles[-1][0]) + timedelta(minutes=5)).isoformat(),
        100,
        100.15,
        99.85,
        100,
        1000,
    ])
    candles[-1], candles[-2] = candles[-2], candles[-1]

    with pytest.raises(ValueError, match="in strictly increasing"):
        module.backtest_strategy(candles, "RELIANCE", 100, 1)


def test_backtest_csv_requires_ohlcv_columns(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("timestamp,open,close\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing columns"):
        module.load_backtest_csv(csv_path)


def test_backtest_csv_reads_header_case_insensitively(tmp_path):
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "Timestamp,Open,High,Low,Close,Volume\n"
        "2026-10-20T09:15:00+05:30,100,101,99,100.5,10\n",
        encoding="utf-8",
    )

    assert module.load_backtest_csv(csv_path) == [
        ["2026-10-20T09:15:00+05:30", 100, 101, 99, 100.5, 10]
    ]


def test_export_candles_uses_local_credentials_and_writes_csv(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("ANGELONE_CLIENT_CODE", "local-client")
    monkeypatch.setenv("ANGELONE_PASSWORD", "local-password")
    monkeypatch.setenv("ANGELONE_API_KEY", "local-api-key")
    monkeypatch.setenv("ANGELONE_SYMBOL_TOKEN", "123")
    monkeypatch.setenv("ANGELONE_SYMBOL", "RELIANCE")
    monkeypatch.setenv("ANGELONE_EXCHANGE", "NSE")
    monkeypatch.delenv("ANGELONE_TOTP_SECRET", raising=False)
    monkeypatch.setattr(
        module.getpass,
        "getpass",
        lambda prompt: "123456",
    )
    login_args = {}
    monkeypatch.setattr(
        module,
        "login",
        lambda client, password, totp, api_key: login_args.update({
            "client": client,
            "password": password,
            "totp": totp,
            "api_key": api_key,
        }) or "test-jwt",
    )
    start = (datetime.now(module.MARKET_TIMEZONE) - timedelta(days=1)).replace(
        hour=9, minute=15, second=0, microsecond=0
    )
    end = start + timedelta(minutes=5)
    expected_candles = [[start.isoformat(), 100, 101, 99, 100.5, 10]]
    monkeypatch.setattr(
        module,
        "get_historical_candles",
        lambda jwt, api_key, exchange, token, from_time, to_time: expected_candles,
    )
    output = tmp_path / "candles.csv"

    module.export_candles_command([
        "--export-candles", str(output),
        "--start", start.strftime("%Y-%m-%d %H:%M"),
        "--end", end.strftime("%Y-%m-%d %H:%M"),
    ])

    assert login_args == {
        "client": "local-client",
        "password": "local-password",
        "totp": "123456",
        "api_key": "local-api-key",
    }
    assert output.read_text(encoding="utf-8").splitlines() == [
        "timestamp,open,high,low,close,volume",
        f"{start.isoformat()},100,101,99,100.5,10",
    ]
    assert "local-password" not in capsys.readouterr().out


def test_local_vault_migration_encrypts_secrets_and_removes_env_entries(
    tmp_path, monkeypatch
):
    env_path = tmp_path / ".env"
    vault_path = tmp_path / "vault.dpapi"
    env_path.write_text(
        "ANGELONE_CLIENT_CODE=local-client\n"
        "ANGELONE_PASSWORD='private password'\n"
        "TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN\n"
        "ANGELONE_SYMBOL=RELIANCE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(local_vault, "_protect_data", base64.b64encode)
    monkeypatch.setattr(
        local_vault,
        "_unprotect_data",
        base64.b64decode,
    )

    count, removed = local_vault.migrate_env_secrets(env_path, vault_path)

    assert count == 2
    assert removed == 3
    assert "ANGELONE_CLIENT_CODE" not in env_path.read_text(encoding="utf-8")
    assert "ANGELONE_PASSWORD" not in env_path.read_text(encoding="utf-8")
    assert "ANGELONE_SYMBOL=RELIANCE" in env_path.read_text(encoding="utf-8")
    assert b"private password" not in vault_path.read_bytes()
    assert local_vault.load_secrets(vault_path) == {
        "ANGELONE_CLIENT_CODE": "local-client",
        "ANGELONE_PASSWORD": "private password",
    }


def test_local_vault_rejects_wrong_account_or_corrupted_blob(tmp_path):
    vault_path = tmp_path / "vault.dpapi"
    vault_path.write_bytes(local_vault.VAULT_MAGIC + b"not-encrypted-correctly")

    with pytest.raises(RuntimeError, match="Could not read local encrypted secrets vault"):
        local_vault.load_secrets(vault_path)


def test_local_vault_saves_and_merges_secrets(tmp_path, monkeypatch):
    vault_path = tmp_path / "vault.dpapi"
    monkeypatch.setattr(local_vault, "_protect_data", base64.b64encode)
    monkeypatch.setattr(local_vault, "_unprotect_data", base64.b64decode)

    assert local_vault.save_secrets(
        {"ANGELONE_CLIENT_CODE": "local-client"}, vault_path
    ) == 1
    assert local_vault.save_secrets(
        {"ANGELONE_API_KEY": "local-key"}, vault_path
    ) == 2
    assert local_vault.load_secrets(vault_path) == {
        "ANGELONE_CLIENT_CODE": "local-client",
        "ANGELONE_API_KEY": "local-key",
    }


def test_local_vault_requires_windows_dpapi_when_not_on_windows(monkeypatch):
    monkeypatch.setattr(local_vault, "os", SimpleNamespace(name="posix"))

    with pytest.raises(RuntimeError, match="available only on Windows"):
        local_vault._protect_data(b"secret")


def test_supervised_mode_cannot_run_unattended(monkeypatch):
    monkeypatch.setenv("ANGELONE_TRADING_MODE", "supervised")
    monkeypatch.setenv("ANGELONE_PLACE_ORDER", "false")

    with pytest.raises(RuntimeError, match="interactive terminal"):
        module.main(unattended=True)


@pytest.mark.parametrize(
    ("mode", "place_order"),
    [("live", "false"), ("paper", "true"), ("supervised", "true")],
)
def test_live_order_configuration_is_rejected_before_login(
    monkeypatch, mode, place_order
):
    monkeypatch.setenv("ANGELONE_TRADING_MODE", mode)
    monkeypatch.setenv("ANGELONE_PLACE_ORDER", place_order)

    with pytest.raises(RuntimeError, match="Real order submission is disabled"):
        module.main()


def test_unattended_paper_mode_requires_totp_and_mail_secrets(monkeypatch):
    monkeypatch.setenv("ANGELONE_TRADING_MODE", "paper")
    monkeypatch.setenv("ANGELONE_PLACE_ORDER", "false")
    monkeypatch.delenv("ANGELONE_TOTP_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="ANGELONE_TOTP_SECRET"):
        module.main(unattended=True)

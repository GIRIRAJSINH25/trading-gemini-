import ipaddress
from datetime import datetime
from math import isfinite


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def build_live_readiness_report(config):
    """Evaluate local readiness inputs without logging in or sending broker requests."""
    checks = []

    public_ip = str(config.get("client_public_ip", "")).strip()
    try:
        if ipaddress.ip_address(public_ip).version != 4:
            raise ValueError
        checks.append(_check("client_public_ipv4", "PASS", "A valid IPv4 value is configured."))
    except ValueError:
        checks.append(_check(
            "client_public_ipv4", "FAIL", "Configure the broker-registered public IPv4."
        ))

    mode = str(config.get("trading_mode", "paper")).strip().lower()
    if mode in {"paper", "supervised"}:
        checks.append(_check(
            "trading_mode", "PASS", f"Configured mode is {mode}; live submission is disabled."
        ))
    else:
        checks.append(_check(
            "trading_mode", "FAIL", "Only paper or proposal-only supervised mode is permitted."
        ))
    if config.get("place_order_enabled") is True:
        checks.append(_check(
            "live_submission_switch",
            "FAIL",
            "ANGELONE_PLACE_ORDER must remain false; the live submission path is disabled.",
        ))
    else:
        checks.append(_check(
            "live_submission_switch",
            "PASS",
            "Real order submission is disabled.",
        ))
    checks.append(_check(
        "local_broker_credentials_present",
        "PASS" if config.get("credentials_configured") is True else "FAIL",
        "Required credential fields are present in the local process environment."
        if config.get("credentials_configured") is True
        else "Required broker credential fields are not configured locally.",
    ))
    checks.append(_check(
        "broker_authentication_and_account_permissions",
        "PASS" if config.get("broker_authenticated") is True else "UNVERIFIED",
        "Broker login succeeded during read-only preflight; segment-specific order "
        "permissions remain unverified."
        if config.get("broker_authenticated") is True
        else "This preflight has not verified broker authentication or permissions.",
    ))

    exchange = str(config.get("exchange", "")).strip().upper()
    symbol = str(config.get("symbol", "")).strip().upper()
    token = str(config.get("symbol_token", "")).strip()
    identity_configured = (
        bool(exchange)
        and bool(symbol)
        and bool(token)
        and not token.startswith("YOUR_")
    )
    checks.append(_check(
        "instrument_identity_configured",
        "PASS" if identity_configured else "FAIL",
        "Exchange, symbol, and token are present."
        if identity_configured
        else "Set a real exchange, symbol, and symbol token.",
    ))
    checks.append(_check(
        "instrument_identity_broker_verified",
        "PASS" if config.get("broker_instrument_verified") is True else "UNVERIFIED",
        "Exact exchange, trading symbol, and token matched a current broker search."
        if config.get("broker_instrument_verified") is True
        else "Symbol/token/exchange have not been checked against a current broker instrument master.",
    ))

    instrument_type = str(config.get("instrument_type", "")).strip()
    product_type = str(config.get("product_type", "")).strip()
    tick_size = _positive_finite(config.get("tick_size"))
    lot_size = _positive_integer(config.get("lot_size"))
    quantity = _positive_integer(config.get("quantity"))
    instrument_details_ok = bool(instrument_type and product_type and tick_size and lot_size)
    checks.append(_check(
        "instrument_contract_details",
        "PASS" if instrument_details_ok else "FAIL",
        "Instrument type, product type, tick size, and lot size are configured."
        if instrument_details_ok
        else "Configure instrument type, product type, verified tick size, and lot size.",
    ))
    quantity_ok = bool(
        quantity
        and lot_size
        and quantity >= lot_size
        and quantity % lot_size == 0
    )
    checks.append(_check(
        "quantity_matches_lot_size",
        "PASS" if quantity_ok else "FAIL",
        "Configured quantity is a positive multiple of lot size."
        if quantity_ok
        else "Quantity must be a positive multiple of lot size.",
    ))
    max_daily_trades = _positive_integer(config.get("max_daily_trades"))
    checks.append(_check(
        "daily_trade_limit",
        "PASS" if max_daily_trades else "FAIL",
        "A positive daily trade limit is configured."
        if max_daily_trades
        else "Configure a positive maximum daily trade count.",
    ))
    checks.append(_check(
        "broker_instrument_terms_verified",
        "UNVERIFIED",
        "Price bands, expiry/strike, minimum quantity, tick and lot sizes are not broker-verified.",
    ))

    order_type = str(config.get("order_type", "")).strip().upper()
    validity = str(config.get("order_validity", "")).strip().upper()
    checks.append(_check(
        "order_type_and_validity",
        "PASS" if order_type and validity else "FAIL",
        "Order type and time-in-force are configured."
        if order_type and validity
        else "Configure order type and order validity for the intended segment.",
    ))
    checks.append(_check(
        "broker_order_permissions_verified",
        "UNVERIFIED",
        "Account-specific permissions and order/product compatibility have not been verified.",
    ))

    start = _parse_time(config.get("session_start"))
    end = _parse_time(config.get("session_end"))
    session_ok = bool(start and end and start < end)
    checks.append(_check(
        "session_window",
        "PASS" if session_ok else "FAIL",
        "Configured session uses valid increasing HH:MM times."
        if session_ok
        else "Configure a valid session start and a later session end.",
    ))
    checks.append(_check(
        "exchange_calendar_and_special_sessions",
        "UNVERIFIED",
        "Exchange holidays and special sessions are not checked by this bot.",
    ))
    checks.append(_check(
        "market_data_validation_and_staleness",
        "PASS",
        "The trading loop validates candle data and blocks entry on stale market data.",
    ))

    stop = _positive_finite(config.get("stop_loss_percent"))
    target = _positive_finite(config.get("target_percent"))
    checks.append(_check(
        "risk_limits",
        "PASS" if stop and stop <= 0.01 and target else "FAIL",
        "Configured stop is positive and no greater than 1%; target is positive."
        if stop and stop <= 0.01 and target
        else "Stop/target configuration violates the configured risk policy.",
    ))
    checks.append(_check(
        "margin_fee_slippage_and_gap_risk",
        "UNVERIFIED",
        "Broker margin and fee figures are estimates; slippage, gaps, and realized loss "
        "cannot be bounded by the planned stop.",
    ))
    margin_available = config.get("broker_margin_available")
    checks.append(_check(
        "account_margin_snapshot",
        "PASS" if margin_available is True else "FAIL" if margin_available is False else "UNVERIFIED",
        "A positive available-funds value was returned by the read-only broker margin API."
        if margin_available is True
        else "Broker reported no positive available funds."
        if margin_available is False
        else "Current account funds and required margin have not been verified.",
    ))
    broker_state_status = config.get("broker_state_status")
    checks.append(_check(
        "read_only_broker_order_and_position_state",
        "PASS"
        if broker_state_status == "NO_DISCREPANCY_DETECTED"
        else "BLOCKED"
        if broker_state_status is not None
        else "UNVERIFIED",
        "Read-only broker order/trade/position snapshot reported no discrepancies."
        if broker_state_status == "NO_DISCREPANCY_DETECTED"
        else "Broker order, position, or trade state requires manual review."
        if broker_state_status is not None
        else "A read-only order/trade/position snapshot has not been verified.",
    ))

    for name in (
        "broker_managed_linked_protective_exits",
        "partial_fill_exit_management",
        "protective_exit_rejection_and_trigger_handling",
        "verified_live_order_submission_adapter",
        "broker_side_duplicate_prevention",
        "ambiguous_network_submission_recovery",
        "live_position_restart_reconciliation",
        "broker_kill_switch_and_session_close",
        "broker_supported_test_environment_validation",
    ):
        checks.append(_check(
            name,
            "BLOCKED",
            "This behavior is not implemented and verified for the target account/product.",
        ))

    checks.append(_check(
        "local_execution_journal",
        "PASS",
        "Durable local intent journal exists; it is not a broker idempotency mechanism.",
    ))
    checks.append(_check(
        "paper_strategy_validation",
        "UNVERIFIED",
        "A configured strategy and mocked tests do not establish live trading performance.",
    ))

    statuses = {check["status"] for check in checks}
    ready = not statuses.intersection({"FAIL", "UNVERIFIED", "BLOCKED"})
    return {
        "live_ready": ready,
        "submission_enabled": False,
        "checks": checks,
        "summary": (
            "Not ready for live trading. Live order submission remains disabled."
            if not ready
            else "All listed checks passed."
        ),
    }


def _positive_finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def _positive_integer(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or str(parsed) != str(value).strip() or parsed <= 0:
        return None
    return parsed


def _parse_time(value):
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (TypeError, ValueError):
        return None

import os
import argparse
import getpass
import json
import csv
import ipaddress
import sys
import smtplib
import uuid
import time
import threading
import base64
import hashlib
import hmac
import struct
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from math import floor, isfinite
from pathlib import Path

import requests
from dotenv import load_dotenv
from local_vault import (
    load_secrets,
    load_secrets_into_environment,
    migrate_env_secrets,
    save_secrets,
)
from execution_safety import ExecutionJournal
from live_readiness import build_live_readiness_report

from telegram_alert import (
    discover_private_chat_id,
    listen_for_status_commands,
    send_telegram_message,
    send_telegram_test_message,
)

load_dotenv()
load_secrets_into_environment()

BASE_URL = "https://apiconnect.angelone.in"
MARKET_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name="IST")
EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
STOP_LOSS_PERCENT = 0.01
TARGET_PERCENT = 0.02
POLL_INTERVAL_SECONDS = 300
PAPER_LEDGER_PATH = Path(__file__).with_name("paper_trades.json")
PAPER_STATUS_PATH = Path(__file__).with_name("paper_monitor_status.json")


def _ema(values, period):
    if len(values) < period:
        raise ValueError(f"At least {period} candles are required for EMA.")
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def calculate_indicators(candles):
    """Calculate EMA, session VWAP, RSI, and ATR from SmartAPI candle rows."""
    parsed = []
    for candle in candles or []:
        if len(candle) < 6:
            raise ValueError("Each candle must contain timestamp, OHLC, and volume.")
        timestamp, open_price, high, low, close, volume = candle[:6]
        numeric_values = [float(open_price), float(high), float(low), float(close), float(volume)]
        if not all(isfinite(value) for value in numeric_values):
            raise ValueError("Candle OHLC values and volume must be finite numbers.")
        row = {
            "timestamp": str(timestamp),
            "open": numeric_values[0],
            "high": numeric_values[1],
            "low": numeric_values[2],
            "close": numeric_values[3],
            "volume": numeric_values[4],
        }
        if (
            min(row["open"], row["high"], row["low"], row["close"]) <= 0
            or row["volume"] < 0
            or row["high"] < max(row["open"], row["close"], row["low"])
            or row["low"] > min(row["open"], row["close"], row["high"])
        ):
            raise ValueError("Candle OHLC values or volume are invalid.")
        parsed.append(row)

    if len(parsed) < EMA_LONG_PERIOD + 1:
        raise ValueError(f"At least {EMA_LONG_PERIOD + 1} candles are required.")

    closes = [row["close"] for row in parsed]
    total_volume = sum(row["volume"] for row in parsed)
    vwap = (
        sum(((row["high"] + row["low"] + row["close"]) / 3) * row["volume"] for row in parsed)
        / total_volume
        if total_volume
        else closes[-1]
    )

    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    recent_changes = changes[-RSI_PERIOD:]
    average_gain = sum(max(change, 0) for change in recent_changes) / RSI_PERIOD
    average_loss = sum(max(-change, 0) for change in recent_changes) / RSI_PERIOD
    if average_gain == 0 and average_loss == 0:
        rsi = 50.0
    elif average_loss == 0:
        rsi = 100.0
    elif average_gain == 0:
        rsi = 0.0
    else:
        rsi = 100 - (100 / (1 + average_gain / average_loss))

    true_ranges = []
    for index, row in enumerate(parsed):
        previous_close = parsed[index - 1]["close"] if index else row["close"]
        true_ranges.append(
            max(
                row["high"] - row["low"],
                abs(row["high"] - previous_close),
                abs(row["low"] - previous_close),
            )
        )
    atr = sum(true_ranges[-ATR_PERIOD:]) / ATR_PERIOD

    return {
        "close": closes[-1],
        "ema_short": _ema(closes, EMA_SHORT_PERIOD),
        "ema_long": _ema(closes, EMA_LONG_PERIOD),
        "vwap": vwap,
        "rsi": rsi,
        "atr": atr,
        "candle_time": parsed[-1]["timestamp"],
    }


def analyze_market(symbol, indicators, max_risk, max_quantity, lot_size=1):
    """Return a confluence-based signal; incomplete or conflicting data means HOLD."""
    if (
        (max_risk is not None and (not isfinite(max_risk) or max_risk <= 0))
        or max_quantity <= 0
        or lot_size <= 0
    ):
        raise ValueError("Maximum risk, quantity, and lot size must be positive.")

    close = indicators["close"]
    ema_short = indicators["ema_short"]
    ema_long = indicators["ema_long"]
    vwap = indicators["vwap"]
    rsi = indicators["rsi"]
    atr = indicators["atr"]

    long_conditions = (ema_short > ema_long, close > vwap, 50 <= rsi < 70)
    short_conditions = (ema_short < ema_long, close < vwap, 30 < rsi <= 50)
    action = "HOLD"
    reasons = []
    if all(long_conditions):
        action = "BUY"
        reasons.append("EMA trend, price versus VWAP, and RSI agree bullishly.")
    elif all(short_conditions):
        action = "SELL"
        reasons.append("EMA trend, price versus VWAP, and RSI agree bearishly.")
    else:
        reasons.append("EMA, VWAP, and RSI do not show a complete aligned setup.")

    if atr <= 0:
        action = "HOLD"
        reasons.append("ATR is zero, so a protective stop cannot be calculated.")
    if action == "HOLD":
        return {
            "symbol": symbol.upper(),
            "agent_name": "Market Expert Gemini",
            "action": action,
            "confluence": round(sum(long_conditions) / 3 if sum(long_conditions) > sum(short_conditions) else sum(short_conditions) / 3, 2),
            "market_indicators": indicators,
            "reasoning": " ".join(reasons),
        }

    entry_price = close
    stop_distance = entry_price * STOP_LOSS_PERCENT
    stop_loss = entry_price - stop_distance if action == "BUY" else entry_price + stop_distance
    target_distance = entry_price * TARGET_PERCENT
    target = entry_price + target_distance if action == "BUY" else entry_price - target_distance
    risk_limited_quantity = (
        max_quantity
        if max_risk is None
        else floor(max_risk / stop_distance / lot_size) * lot_size
    )
    quantity = min(max_quantity, risk_limited_quantity)
    quantity = quantity // lot_size * lot_size
    if quantity < 1:
        return {
            "symbol": symbol.upper(),
            "agent_name": "Market Expert Gemini",
            "action": "HOLD",
            "confluence": 1.0,
            "market_indicators": indicators,
            "reasoning": "Calculated stop distance exceeds the configured per-trade risk budget.",
        }

    return {
        "symbol": symbol.upper(),
        "agent_name": "Market Expert Gemini",
        "action": action,
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target": round(target, 2),
        "quantity": quantity,
        "risk": round(stop_distance * quantity, 2),
        "max_risk": round(max_risk, 2) if max_risk is not None else None,
        "confluence": 1.0,
        "market_indicators": indicators,
        "reasoning": (
            " ".join(reasons)
            + f" Stop loss is {STOP_LOSS_PERCENT:.0%} from entry; target is {TARGET_PERCENT:.0%}."
        ),
    }


def _candle_datetime(candle_time):
    parsed = datetime.fromisoformat(str(candle_time).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MARKET_TIMEZONE)
    return parsed.astimezone(MARKET_TIMEZONE)


def get_account_margins(jwt_token, api_key):
    response = requests.get(
        f"{BASE_URL}/rest/secure/angelbroking/user/v1/getRMS",
        headers=build_headers(jwt_token, api_key),
        timeout=30,
    )
    data = _api_json(response, "Account margin request").get("data") or {}
    parsed = {}
    for field in ("availablecash", "net", "utilisedmargin"):
        try:
            parsed[field] = float(data[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Angel One did not return a valid {field} margin value.") from exc
        if not isfinite(parsed[field]):
            raise RuntimeError(f"Angel One returned a non-finite {field} margin value.")
    return {
        "available_cash": parsed["availablecash"],
        "net": parsed["net"],
        "utilised_margin": parsed["utilisedmargin"],
    }


def _read_only_api_rows(jwt_token, api_key, endpoint, operation):
    response = requests.get(
        f"{BASE_URL}{endpoint}",
        headers=build_headers(jwt_token, api_key),
        timeout=30,
    )
    payload = _api_json(response, operation)
    if payload.get("status") is not True:
        raise RuntimeError(f"{operation} failed: broker did not confirm success.")
    rows = payload.get("data")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise RuntimeError(f"{operation} failed: broker returned a non-list data field.")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{operation} failed: broker returned a malformed record.")
    return rows


def _row_value(row, *keys, default=None):
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return default


def _read_only_quantity(row, field, *aliases, default="0"):
    value = _row_value(row, field, *aliases, default=default)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Broker returned invalid {field} in read-only account data.") from exc
    if not isfinite(numeric) or not numeric.is_integer():
        raise RuntimeError(f"Broker returned invalid {field} in read-only account data.")
    return int(numeric)


def _read_only_price(row, field, *aliases, default="0"):
    value = _row_value(row, field, *aliases, default=default)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Broker returned invalid {field} in read-only account data.") from exc
    if not isfinite(numeric):
        raise RuntimeError(f"Broker returned invalid {field} in read-only account data.")
    return numeric


def _safe_broker_text(value):
    return str(value).strip()[:120] if value is not None else ""


def _required_broker_text(row, field, *aliases):
    value = _safe_broker_text(_row_value(row, field, *aliases))
    if not value:
        raise RuntimeError(
            f"Broker response is missing required {field} in read-only account data."
        )
    return value


def build_broker_reconciliation(order_rows, trade_rows, position_rows, snapshot_time=None):
    """Build a conservative account snapshot from read-only broker records."""
    if any(not isinstance(rows, list) for rows in (order_rows, trade_rows, position_rows)):
        raise ValueError("Broker order, trade, and position records must be lists.")
    captured_at = (snapshot_time or datetime.now(MARKET_TIMEZONE)).isoformat()
    orders = []
    pending_orders = []
    partial_fill_orders = []
    rejected_orders = []
    uncertain_orders = []
    for row in order_rows:
        if not isinstance(row, dict):
            raise ValueError("Broker order book contains a malformed record.")
        status = _safe_broker_text(
            _row_value(row, "orderstatus", "status")
        )
        if not status:
            raise RuntimeError("Broker order book record is missing its order status.")
        order = {
            "order_id": _safe_broker_text(_row_value(row, "orderid", "orderId")),
            "symbol": _required_broker_text(row, "tradingsymbol", "symbol"),
            "exchange": _required_broker_text(row, "exchange"),
            "symbol_token": _required_broker_text(row, "symboltoken", "token"),
            "side": _required_broker_text(
                row, "transactiontype", "transactionType"
            ).upper(),
            "product": _required_broker_text(row, "producttype", "productType"),
            "order_type": _safe_broker_text(_row_value(row, "ordertype", "orderType")),
            "quantity": _read_only_quantity(row, "quantity", "qty", default=None),
            "filled_quantity": _read_only_quantity(row, "filledshares", "filledShares"),
            "average_price": _read_only_price(row, "averageprice", "averagePrice"),
            "status": status,
        }
        if (
            order["quantity"] <= 0
            or order["filled_quantity"] < 0
            or order["filled_quantity"] > order["quantity"]
        ):
            raise RuntimeError("Broker order book contains invalid order quantities.")
        orders.append(order)
        normalized_status = status.casefold()
        has_partial_fill = (
            0 < order["filled_quantity"] < order["quantity"]
            or "partial" in normalized_status
        )
        if has_partial_fill:
            partial_fill_orders.append(order)
            pending_orders.append(order)
        if normalized_status in {"rejected", "reject"}:
            rejected_orders.append(order)
        if not has_partial_fill and (
            normalized_status in {"open", "pending", "trigger pending"}
            or "pending" in normalized_status
            or "open" in normalized_status
            or "trigger" in normalized_status
        ):
            if order not in pending_orders:
                pending_orders.append(order)
        elif not has_partial_fill and normalized_status not in {
            "complete", "completed", "rejected", "cancelled", "canceled"
        }:
            uncertain_orders.append(order)

    trades = []
    signed_fills = {}
    for row in trade_rows:
        if not isinstance(row, dict):
            raise ValueError("Broker trade book contains a malformed record.")
        side = _safe_broker_text(_row_value(row, "transactiontype", "transactionType")).upper()
        if side not in {"BUY", "SELL"}:
            raise RuntimeError("Broker returned an unknown transaction side in trade book.")
        exchange = _required_broker_text(row, "exchange")
        token = _required_broker_text(row, "symboltoken", "token")
        product = _required_broker_text(row, "producttype", "productType")
        quantity = _read_only_quantity(
            row, "fillsize", "fillSize", "quantity", "qty", default=None
        )
        key = (exchange, token, product)
        signed_fills[key] = signed_fills.get(key, 0) + (quantity if side == "BUY" else -quantity)
        trades.append({
            "order_id": _safe_broker_text(_row_value(row, "orderid", "orderId")),
            "symbol": _required_broker_text(row, "tradingsymbol", "symbol"),
            "exchange": exchange,
            "symbol_token": token,
            "side": side,
            "product": product,
            "quantity": quantity,
            "price": _read_only_price(row, "fillprice", "fillPrice", "price"),
            "time": _safe_broker_text(_row_value(row, "filltime", "fillTime", "filltimestamp")),
        })

    positions = []
    discrepancies = []
    for row in position_rows:
        if not isinstance(row, dict):
            raise ValueError("Broker position book contains a malformed record.")
        exchange = _required_broker_text(row, "exchange")
        token = _required_broker_text(row, "symboltoken", "token")
        product = _required_broker_text(row, "producttype", "productType")
        net_quantity = _read_only_quantity(
            row, "netqty", "netQty", "netquantity", default=None
        )
        key = (exchange, token, product)
        has_trade_basis = key in signed_fills
        trade_quantity = signed_fills.get(key)
        position = {
            "symbol": _required_broker_text(row, "tradingsymbol", "symbol"),
            "exchange": exchange,
            "symbol_token": token,
            "product": product,
            "net_quantity": net_quantity,
            "buy_quantity": _read_only_quantity(row, "buyqty", "buyQty"),
            "sell_quantity": _read_only_quantity(row, "sellqty", "sellQty"),
            "average_buy_price": _read_only_price(row, "buyavgprice", "buyAvgPrice"),
            "average_sell_price": _read_only_price(row, "sellavgprice", "sellAvgPrice"),
            "tradebook_net_quantity": trade_quantity if has_trade_basis else None,
            "reconciliation": (
                "MATCHED"
                if has_trade_basis and trade_quantity == net_quantity
                else "MISMATCH"
                if has_trade_basis
                else "NO_TRADEBOOK_BASIS"
            ),
        }
        positions.append(position)
        if position["reconciliation"] != "MATCHED" and net_quantity != 0:
            discrepancies.append({
                "symbol": position["symbol"],
                "exchange": exchange,
                "symbol_token": token,
                "product": product,
                "status": position["reconciliation"],
            })

    for key, fill_quantity in signed_fills.items():
        if not any(
            (position["exchange"], position["symbol_token"], position["product"]) == key
            for position in positions
        ) and fill_quantity != 0:
            discrepancies.append({
                "exchange": key[0],
                "symbol_token": key[1],
                "product": key[2],
                "status": "TRADEBOOK_POSITION_MISSING",
            })

    return {
        "captured_at": captured_at,
        "read_only": True,
        "order_count": len(orders),
        "trade_count": len(trades),
        "position_count": len(positions),
        "pending_order_count": len(pending_orders),
        "partial_fill_order_count": len(partial_fill_orders),
        "rejected_order_count": len(rejected_orders),
        "uncertain_order_count": len(uncertain_orders),
        "orders": orders,
        "pending_orders": pending_orders,
        "partial_fill_orders": partial_fill_orders,
        "rejected_orders": rejected_orders,
        "uncertain_orders": uncertain_orders,
        "trades": trades,
        "positions": positions,
        "discrepancies": discrepancies,
        "status": (
            "REVIEW_REQUIRED"
            if pending_orders or partial_fill_orders or rejected_orders
            or uncertain_orders or discrepancies
            else "NO_DISCREPANCY_DETECTED"
        ),
        "warning": (
            "Read-only snapshot only. Tradebook/position matching is a consistency check, "
            "not proof that protective exits exist or that the account is safe."
        ),
    }


def get_broker_reconciliation(jwt_token, api_key):
    """Read order book, trade book, and positions; never sends an order request."""
    order_rows = _read_only_api_rows(
        jwt_token,
        api_key,
        "/rest/secure/angelbroking/order/v1/getOrderBook",
        "Order book request",
    )
    trade_rows = _read_only_api_rows(
        jwt_token,
        api_key,
        "/rest/secure/angelbroking/order/v1/getTradeBook",
        "Trade book request",
    )
    position_rows = _read_only_api_rows(
        jwt_token,
        api_key,
        "/rest/secure/angelbroking/order/v1/getPosition",
        "Position request",
    )
    return build_broker_reconciliation(order_rows, trade_rows, position_rows)


def verify_broker_instrument(jwt_token, api_key, exchange, symbol, symbol_token):
    if not all(
        isinstance(value, str) and value.strip()
        for value in (exchange, symbol, symbol_token)
    ):
        raise ValueError("Exchange, exact trading symbol, and symbol token are required.")
    response = requests.post(
        f"{BASE_URL}/rest/secure/angelbroking/order/v1/searchScrip",
        json={"exchange": exchange.strip().upper(), "searchscrip": symbol.strip()},
        headers=build_headers(jwt_token, api_key),
        timeout=30,
    )
    data = _api_json(response, "Instrument search")
    if data.get("status") is not True:
        raise RuntimeError("Instrument search failed: broker did not confirm success.")
    rows = data.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Instrument search failed: broker returned malformed records.")
    expected = (
        exchange.strip().upper(),
        symbol.strip().upper(),
        symbol_token.strip(),
    )
    exact_matches = [
        row for row in rows
        if (
            _safe_broker_text(row.get("exchange")).upper(),
            _safe_broker_text(row.get("tradingsymbol")).upper(),
            _safe_broker_text(row.get("symboltoken")),
        ) == expected
    ]
    if len(exact_matches) != 1:
        raise RuntimeError(
            "Configured exchange, exact trading symbol, and symbol token did not "
            "resolve to exactly one Angel One instrument."
        )
    return {
        "verified": True,
        "exchange": expected[0],
        "symbol": expected[1],
        "symbol_token": expected[2],
    }


def require_clear_broker_state_for_proposals(snapshot):
    """Block proposal generation when account state is open, pending, or uncertain."""
    if not isinstance(snapshot, dict) or snapshot.get("read_only") is not True:
        raise RuntimeError("Broker state is unavailable or not a verified read-only snapshot.")
    if not isinstance(snapshot.get("pending_orders"), list):
        raise RuntimeError("Broker pending-order state is incomplete; proposal blocked.")
    if not isinstance(snapshot.get("partial_fill_orders"), list):
        raise RuntimeError("Broker partial-fill state is incomplete; proposal blocked.")
    if not isinstance(snapshot.get("rejected_orders"), list):
        raise RuntimeError("Broker rejected-order state is incomplete; proposal blocked.")
    if not isinstance(snapshot.get("uncertain_orders"), list):
        raise RuntimeError("Broker order-status state is incomplete; proposal blocked.")
    if not isinstance(snapshot.get("positions"), list):
        raise RuntimeError("Broker position state is incomplete; proposal blocked.")
    if not isinstance(snapshot.get("discrepancies"), list):
        raise RuntimeError("Broker reconciliation state is incomplete; proposal blocked.")
    pending_count = len(snapshot["pending_orders"])
    partial_fill_count = len(snapshot["partial_fill_orders"])
    rejected_count = len(snapshot["rejected_orders"])
    uncertain_count = len(snapshot["uncertain_orders"])
    open_positions = [
        position
        for position in snapshot["positions"]
        if not isinstance(position, dict) or position.get("net_quantity") != 0
    ]
    discrepancies = snapshot["discrepancies"]
    if (
        pending_count or partial_fill_count or rejected_count
        or uncertain_count or open_positions or discrepancies
    ):
        symbols = sorted({
            str(position.get("symbol") or "unknown")
            for position in open_positions
            if isinstance(position, dict)
        })
        details = []
        if pending_count:
            details.append(f"{pending_count} pending order(s)")
        if partial_fill_count:
            details.append(f"{partial_fill_count} partial-fill order(s)")
        if rejected_count:
            details.append(f"{rejected_count} rejected order(s) needing review")
        if uncertain_count:
            details.append(f"{uncertain_count} order(s) with unknown status")
        if open_positions:
            details.append(
                f"{len(open_positions)} open/unknown position(s)"
                + (f" ({', '.join(symbols)})" if symbols else "")
            )
        if discrepancies:
            details.append(f"{len(discrepancies)} reconciliation discrepancy(ies)")
        raise RuntimeError(
            "Broker account is not clear for a new proposal: "
            + "; ".join(details)
            + ". Review account manually; proposal blocked."
        )


def estimate_required_margin(jwt_token, api_key, exchange, symbol_token, action, price, quantity):
    response = requests.post(
        f"{BASE_URL}/rest/secure/angelbroking/margin/v1/batch",
        json=[{
            "exchange": exchange,
            "qty": int(quantity),
            "price": float(price),
            "producttype": "INTRADAY",
            "token": symbol_token,
            "tradeType": action,
        }],
        headers=build_headers(jwt_token, api_key),
        timeout=30,
    )
    data = _api_json(response, "Order margin estimate").get("data") or {}
    required = data.get("totalmarginrequired")
    if required is None or not isfinite(float(required)):
        raise RuntimeError("Angel One did not return a valid required-margin estimate.")
    return float(required)


def estimate_round_trip_charges(
    jwt_token, api_key, exchange, symbol, symbol_token, action, entry_price, exit_price, quantity
):
    opposite_action = "SELL" if action == "BUY" else "BUY"
    response = requests.post(
        f"{BASE_URL}/rest/secure/angelbroking/brokerage/v1/estimateCharges",
        json={"orders": [
            {
                "product_type": "INTRADAY",
                "transaction_type": action,
                "quantity": str(quantity),
                "price": str(entry_price),
                "exchange": exchange,
                "symbol_name": symbol,
                "token": symbol_token,
            },
            {
                "product_type": "INTRADAY",
                "transaction_type": opposite_action,
                "quantity": str(quantity),
                "price": str(exit_price),
                "exchange": exchange,
                "symbol_name": symbol,
                "token": symbol_token,
            },
        ]},
        headers=build_headers(jwt_token, api_key),
        timeout=30,
    )
    data = _api_json(response, "Brokerage and tax estimate").get("data") or {}
    summary = data.get("summary") or data
    total_charges = summary.get("total_charges")
    if total_charges is None or not isfinite(float(total_charges)):
        raise RuntimeError("Angel One did not return a valid total-charge estimate.")
    return float(total_charges)


def close_paper_position(position, exit_price, exit_time, exit_reason, fee_estimator=None):
    direction = 1 if position["action"] == "BUY" else -1
    gross_pnl = (exit_price - position["entry_price"]) * direction * position["quantity"]
    fees = None
    fee_error = None
    if fee_estimator:
        try:
            fees = float(fee_estimator(position, exit_price))
            if not isfinite(fees) or fees < 0:
                raise ValueError("Charge estimate must be a finite non-negative amount.")
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            fee_error = str(exc)
    entry_time = _candle_datetime(position["entry_time"])
    exit_datetime = _candle_datetime(exit_time)
    position.update({
        "status": "CLOSED",
        "exit_time": exit_datetime.isoformat(),
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "duration_seconds": max(0, int((exit_datetime - entry_time).total_seconds())),
        "gross_pnl": round(gross_pnl, 2),
        "fees_and_taxes": round(fees, 2) if fees is not None else None,
        "net_pnl": round(gross_pnl - fees, 2) if fees is not None else None,
        "fee_estimate_error": fee_error,
    })


def update_paper_ledger(
    candles,
    plan,
    ledger_path=PAPER_LEDGER_PATH,
    fee_estimator=None,
    force_close=False,
):
    """Simulate fills and exits from subsequent candle highs/lows; never sends broker orders."""
    if ledger_path.exists():
        try:
            trades = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read paper ledger {ledger_path}: {exc}") from exc
        if not isinstance(trades, list):
            raise ValueError("Paper ledger must contain a JSON list.")
    else:
        trades = []

    symbol = plan["symbol"]
    open_trade = next(
        (trade for trade in reversed(trades) if trade.get("symbol") == symbol and trade.get("status") == "OPEN"),
        None,
    )
    if open_trade:
        entry_time = _candle_datetime(open_trade["entry_time"])
        for candle in candles:
            candle_time = _candle_datetime(candle[0])
            if candle_time <= entry_time:
                continue
            open_price, high, low = float(candle[1]), float(candle[2]), float(candle[3])
            action = open_trade["action"]
            stop_hit = low <= open_trade["stop_loss"] if action == "BUY" else high >= open_trade["stop_loss"]
            target_hit = high >= open_trade["target"] if action == "BUY" else low <= open_trade["target"]
            if not stop_hit and not target_hit:
                continue
            # If both levels occur in one candle, assume the adverse stop filled first.
            exit_reason = "STOP_LOSS" if stop_hit else "TARGET"
            exit_price = (
                open_price if stop_hit and (
                    (action == "BUY" and open_price < open_trade["stop_loss"])
                    or (action == "SELL" and open_price > open_trade["stop_loss"])
                ) else open_trade["stop_loss"]
            ) if stop_hit else (
                open_price if (
                    (action == "BUY" and open_price > open_trade["target"])
                    or (action == "SELL" and open_price < open_trade["target"])
                ) else open_trade["target"]
            )
            close_paper_position(
                open_trade,
                exit_price,
                candle[0],
                exit_reason,
                fee_estimator,
            )
            break

    if open_trade and open_trade.get("status") == "OPEN" and force_close:
        close_paper_position(
            open_trade,
            float(candles[-1][4]),
            candles[-1][0],
            "MARKET_CLOSE",
            fee_estimator,
        )

    if (
        (not open_trade or open_trade.get("status") != "OPEN")
        and not force_close
        and plan.get("action") in {"BUY", "SELL"}
    ):
        trades.append({
            "symbol": symbol,
            "action": plan["action"],
            "status": "OPEN",
            "entry_time": candles[-1][0],
            "entry_price": float(plan["entry_price"]),
            "stop_loss": plan["stop_loss"],
            "target": plan["target"],
            "quantity": plan["quantity"],
            "exchange": plan.get("exchange"),
            "risk": plan.get("risk"),
            "margin_required": plan.get("margin_required"),
            "estimated_charges_at_target": plan.get("estimated_charges_at_target"),
            "estimated_net_profit_at_target": plan.get("estimated_net_profit_at_target"),
            "estimated_charges_at_stop": plan.get("estimated_charges_at_stop"),
        })
        open_trade = trades[-1]

    mark_price = float(candles[-1][4]) if candles else None
    unrealized_pnl = 0.0
    if open_trade and open_trade.get("status") == "OPEN" and mark_price is not None:
        direction = 1 if open_trade["action"] == "BUY" else -1
        unrealized_pnl = (mark_price - open_trade["entry_price"]) * direction * open_trade["quantity"]

    today = datetime.now(MARKET_TIMEZONE).date().isoformat()
    symbol_trades = [
        trade for trade in trades
        if trade.get("symbol") == symbol and trade.get("entry_time", "").startswith(today)
    ]
    closed_trades = [trade for trade in symbol_trades if trade.get("status") == "CLOSED"]
    gross_pnl = round(sum(trade.get("gross_pnl", 0) for trade in closed_trades), 2)
    net_pnl = (
        round(sum(trade["net_pnl"] for trade in closed_trades), 2)
        if closed_trades and all(trade.get("net_pnl") is not None for trade in closed_trades)
        else None
    )
    try:
        ledger_path.write_text(json.dumps(trades, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not write paper ledger {ledger_path}: {exc}") from exc
    return {
        "open_trade": open_trade,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "closed_trades": len(closed_trades),
        "trades": symbol_trades,
    }


def build_end_of_day_report(
    report_date,
    mode,
    trades,
    margins,
    margin_error=None,
    proposal_count=0,
    proposal_risk_reserved=0,
):
    report_title = (
        "PAPER TRADING REPORT"
        if mode == "paper"
        else "SUPERVISED PROPOSAL REPORT — NO LIVE ORDERS PLACED"
        if mode == "supervised"
        else "LIVE MODE REPORT — NO LIVE ORDERS PLACED"
    )
    day_trades = [
        trade for trade in trades
        if trade.get("entry_time", "").startswith(report_date.isoformat())
    ]
    closed = [trade for trade in day_trades if trade.get("status") == "CLOSED"]
    gross_pnl = sum(trade.get("gross_pnl", 0) for trade in closed)
    fees_known = all(trade.get("net_pnl") is not None for trade in closed)
    net_pnl = sum(trade["net_pnl"] for trade in closed) if closed and fees_known else None

    lines = [
        "Market Expert Gemini",
        report_title,
        f"Date: {report_date.isoformat()} (IST)",
        f"Instrument trades recorded: {len(day_trades)}",
        f"Closed trades: {len(closed)}",
        f"Stop-loss exits: {sum(trade.get('exit_reason') == 'STOP_LOSS' for trade in closed)}",
        f"Target exits: {sum(trade.get('exit_reason') == 'TARGET' for trade in closed)}",
        f"End-of-session exits: {sum(trade.get('exit_reason') == 'MARKET_CLOSE' for trade in closed)}",
        f"Gross realized P/L: Rs {gross_pnl:.2f}",
        (
            f"Estimated net P/L after broker-estimated charges/taxes: Rs {net_pnl:.2f}"
            if net_pnl is not None
            else "Net P/L: unavailable because the broker charge estimate failed or no trades closed."
        ),
    ]
    if margins:
        lines.extend([
            f"Available cash/margin at report time: Rs {margins['available_cash']:.2f}",
            f"Net account balance: Rs {margins['net']:.2f}",
            f"Utilized margin: Rs {margins['utilised_margin']:.2f}",
        ])
    else:
        lines.append(f"Account margin unavailable: {margin_error or 'no valid margin response.'}")
    if mode == "supervised":
        lines.append(f"Supervised proposals presented: {proposal_count}")
        lines.append(
            f"Proposal risk budget reserved: Rs {proposal_risk_reserved:.2f}"
        )
        lines.append(
            "Session authorization covered proposals only; no order was submitted."
        )
    elif mode != "paper":
        lines.append("Live order execution is disabled; no real trades were placed by this bot.")

    if day_trades:
        lines.append("\nTrade details:")
        for index, trade in enumerate(day_trades, 1):
            entry_time = _candle_datetime(trade["entry_time"])
            lines.append(
                f"{index}. {trade['symbol']} {trade.get('exchange') or ''} {trade['action']} "
                f"qty={trade['quantity']} entry={trade['entry_price']:.2f} "
                f"at {entry_time.strftime('%H:%M:%S')} IST"
            )
            if trade.get("status") == "CLOSED":
                exit_time = _candle_datetime(trade["exit_time"])
                seconds = trade.get("duration_seconds", 0)
                lines.append(
                    f"   Exit={trade['exit_price']:.2f} at {exit_time.strftime('%H:%M:%S')} IST; "
                    f"result={trade['exit_reason']}; held={seconds // 60}m {seconds % 60}s; "
                    f"gross P/L=Rs {trade.get('gross_pnl', 0):.2f}; "
                    f"fees/taxes={trade.get('fees_and_taxes') if trade.get('fees_and_taxes') is not None else 'unavailable'}; "
                    f"net P/L={trade.get('net_pnl') if trade.get('net_pnl') is not None else 'unavailable'}"
                )
            else:
                lines.append(
                    f"   Still open at report time; stop={trade['stop_loss']:.2f}, "
                    f"target={trade['target']:.2f}."
                )
            if trade.get("margin_required") is not None:
                lines.append(f"   Estimated order margin: Rs {trade['margin_required']:.2f}.")
            if trade.get("estimated_net_profit_at_target") is not None:
                lines.append(
                    "   Expected target P/L after estimated charges: "
                    f"Rs {trade['estimated_net_profit_at_target']:.2f}; "
                    "estimated fees/taxes on stop: "
                    f"Rs {trade.get('estimated_charges_at_stop', 0):.2f}."
                )

    lines.extend([
        "",
        "Fees/taxes are Angel One API estimates, not a tax filing or guaranteed final bill.",
        "Trading involves risk. This report does not imply or guarantee profit.",
    ])
    return "\n".join(lines)


def request_supervised_session_authorization(
    session_date,
    available_margin,
    approval_input=input,
):
    max_session_loss = one_percent_of_margin(available_margin)
    confirmation = f"AUTHORIZE PROPOSAL SESSION {session_date.isoformat()}"
    print("\nPRE-MARKET SESSION AUTHORIZATION — PROPOSALS ONLY")
    print(f"Verified pre-market available margin: Rs {available_margin:.2f}")
    print("Maximum estimated stop loss per proposal: 1% of that order's required margin.")
    print(
        f"Maximum session proposal-risk budget: 1% of available margin "
        f"(Rs {max_session_loss:.2f})."
    )
    print("This authorization will not place, modify, or cancel any broker order.")
    print(f"Type exactly: {confirmation}")
    return approval_input("> ").strip() == confirmation


def one_percent_of_margin(margin):
    if not isfinite(margin) or margin <= 0:
        raise ValueError("Margin must be a positive finite amount.")
    return margin * 0.01


def estimate_proposal_stop_risk(plan):
    return (
        abs(plan["entry_price"] - plan["stop_loss"]) * plan["quantity"]
        + plan["estimated_charges_at_stop"]
    )


def size_supervised_proposal(
    plan,
    max_quantity,
    lot_size,
    available_margin,
    margin_estimator,
    charge_estimator,
):
    quantity_limit = min(max_quantity, plan["quantity"])
    quantity = quantity_limit // lot_size * lot_size
    while quantity >= lot_size:
        required_margin = margin_estimator(plan["action"], plan["entry_price"], quantity)
        if not isfinite(required_margin) or required_margin <= 0:
            raise RuntimeError("Order margin estimate must be a positive finite value.")
        if required_margin <= available_margin:
            stop_charges = charge_estimator(
                plan["action"], plan["entry_price"], plan["stop_loss"], quantity
            )
            target_charges = charge_estimator(
                plan["action"], plan["entry_price"], plan["target"], quantity
            )
            if (
                not isfinite(stop_charges)
                or stop_charges < 0
                or not isfinite(target_charges)
                or target_charges < 0
            ):
                raise RuntimeError("Broker charge estimates must be finite and non-negative.")
            candidate = dict(plan)
            candidate.update({
                "quantity": quantity,
                "risk": round(
                    abs(plan["entry_price"] - plan["stop_loss"]) * quantity, 2
                ),
                "margin_required": required_margin,
                "estimated_charges_at_stop": stop_charges,
                "estimated_charges_at_target": target_charges,
                "estimated_net_profit_at_target": round(
                    abs(plan["target"] - plan["entry_price"]) * quantity
                    - target_charges,
                    2,
                ),
            })
            risk_cap = one_percent_of_margin(required_margin)
            if estimate_proposal_stop_risk(candidate) <= risk_cap:
                candidate["max_proposal_loss"] = round(risk_cap, 2)
                return candidate
        quantity -= lot_size
    return None


def request_supervised_proposal(plan, margins, proposal_risk, remaining_risk):
    proposal = {
        "symbol": plan["symbol"],
        "exchange": plan["exchange"],
        "action": plan["action"],
        "entry_price": plan["entry_price"],
        "stop_loss": plan["stop_loss"],
        "target": plan["target"],
        "quantity": plan["quantity"],
        "risk": plan.get("risk"),
        "confluence": plan.get("confluence"),
        "reasoning": plan.get("reasoning"),
        "available_margin": margins["available_cash"],
        "required_margin": plan["margin_required"],
        "max_proposal_loss": plan["max_proposal_loss"],
        "estimated_charges_at_target": plan["estimated_charges_at_target"],
        "estimated_net_profit_at_target": plan["estimated_net_profit_at_target"],
        "estimated_charges_at_stop": plan["estimated_charges_at_stop"],
        "estimated_loss_at_stop_including_charges": round(
            estimate_proposal_stop_risk(plan), 2
        ),
        "proposal_risk_reserved": round(proposal_risk, 2),
        "session_risk_budget_remaining_after_proposal": round(remaining_risk, 2),
    }
    print("\nSUPERVISED TRADE PROPOSAL — SESSION AUTHORIZED REVIEW ONLY")
    print(json.dumps(proposal, indent=2))
    print("No order was submitted.")


def email_end_of_day_report(report, allow_prompt=True):
    recipient = os.getenv("REPORT_TO_EMAIL", "").strip()
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    if not smtp_password or smtp_password.startswith("YOUR_"):
        if not allow_prompt:
            raise RuntimeError("Set SMTP_PASSWORD in the server's protected .env for unattended email.")
        smtp_password = getpass.getpass(
            f"Gmail app password for {smtp_user} (input hidden): "
        ).strip()
    if not recipient or not smtp_host or not smtp_user or not smtp_password:
        raise RuntimeError(
            "Email is not configured. Check REPORT_TO_EMAIL, SMTP_HOST, SMTP_PORT, "
            "SMTP_USER, and enter a Gmail app password when prompted."
        )

    if "EMAIL DELIVERY TEST" in report:
        mode = "EMAIL DELIVERY TEST"
    elif "PAPER TRADING REPORT" in report:
        mode = "PAPER TRADING REPORT"
    else:
        mode = "LIVE MODE REPORT"
    report_date = datetime.now(MARKET_TIMEZONE).date().isoformat()
    message = EmailMessage()
    message["Subject"] = f"Market Expert Gemini — {mode} — {report_date}"
    message["From"] = smtp_user
    message["To"] = recipient
    message.set_content(report)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def send_test_email():
    email_end_of_day_report(
        "Market Expert Gemini\n"
        "EMAIL DELIVERY TEST\n"
        "This is a test message to verify report email delivery.\n"
        "Mode: paper trading. No market data or trade was used."
    )
    print("Test email accepted by Gmail SMTP for delivery.")


def load_paper_trades(ledger_path=PAPER_LEDGER_PATH):
    if not ledger_path.exists():
        return []
    try:
        trades = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read paper ledger {ledger_path}: {exc}") from exc
    if not isinstance(trades, list):
        raise ValueError("Paper ledger must contain a JSON list.")
    return trades


def generate_totp(secret, timestamp=None):
    """Generate a six-digit RFC 6238 TOTP from a Base32 secret."""
    normalized = "".join((secret or "").split()).upper()
    if not normalized:
        raise ValueError("ANGELONE_TOTP_SECRET is empty.")
    try:
        key = base64.b32decode(normalized + "=" * ((8 - len(normalized) % 8) % 8), casefold=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("ANGELONE_TOTP_SECRET must be a valid Base32 secret.") from exc
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def telegram_bot_status():
    mode = os.getenv("ANGELONE_TRADING_MODE", "paper").strip().lower()
    if mode not in {"paper", "supervised", "live"}:
        mode = "invalid configuration"
    trades = load_paper_trades()
    open_trades = [trade for trade in trades if trade.get("status") == "OPEN"]
    today = datetime.now(MARKET_TIMEZONE).date().isoformat()
    today_closed = [
        trade for trade in trades
        if trade.get("status") == "CLOSED"
        and trade.get("entry_time", "").startswith(today)
    ]
    net_values = [trade.get("net_pnl") for trade in today_closed]
    net_summary = (
        f"Estimated net P/L today: Rs {sum(net_values):.2f}"
        if net_values and all(value is not None for value in net_values)
        else "Estimated net P/L today: unavailable until broker charge estimates exist."
    )
    open_summary = (
        "\n".join(
            f"- {trade.get('symbol', 'Unknown')} {trade.get('action', 'Unknown')} "
            f"qty {trade.get('quantity', '?')}; entry Rs {float(trade.get('entry_price', 0)):.2f}"
            for trade in open_trades
        )
        if open_trades
        else "No open paper positions in the local ledger."
    )
    heartbeat_file = Path(__file__).with_name("paper_monitor_status.json")
    monitor_status = "Paper market monitor: no recent status recorded."
    if heartbeat_file.exists():
        try:
            heartbeat = json.loads(heartbeat_file.read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(heartbeat["updated_at"])
            age = datetime.now(MARKET_TIMEZONE) - updated.astimezone(MARKET_TIMEZONE)
            if heartbeat.get("state") == "closed" and updated.astimezone(MARKET_TIMEZONE).date() == datetime.now(MARKET_TIMEZONE).date():
                monitor_status = "Paper market monitor: session closed for today; Telegram listener remains online."
            elif age <= timedelta(minutes=15):
                monitor_status = f"Paper market monitor: {heartbeat.get('state', 'unknown')}."
            else:
                monitor_status = "Paper market monitor: offline or last update is stale."
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            monitor_status = "Paper market monitor: status file is invalid or unreadable."
    mode_note = (
        "Supervised proposals require the interactive terminal; Telegram cannot approve trades.\n"
        if mode == "supervised"
        else ""
    )
    return (
        "<b>Market Expert Gemini</b>\n"
        "Telegram listener: online\n"
        f"Configured mode: {mode.upper()} (live orders blocked)\n"
        f"{mode_note}"
        f"{monitor_status}\n"
        f"Today's closed paper trades: {len(today_closed)}\n"
        f"{net_summary}\n"
        f"{open_summary}\n\n"
        "Status uses the local paper ledger and monitor heartbeat; it does not query Angel One."
    )


def write_paper_monitor_status(state, detail=None, decision=None, ledger=None):
    payload = {
        "updated_at": datetime.now(MARKET_TIMEZONE).isoformat(),
        "state": state,
    }
    if detail:
        payload["detail"] = detail
    if decision:
        indicators = decision.get("market_indicators") or {}
        paper_trade = (ledger or {}).get("open_trade") or {}
        if paper_trade.get("status") == "CLOSED":
            paper_execution = (
                f"Paper simulation recorded {paper_trade.get('exit_reason', 'an exit')}; "
                "no broker order was submitted."
            )
        elif paper_trade.get("status") == "OPEN":
            paper_execution = (
                "Paper ledger position is open (simulated); no broker order was submitted."
            )
        else:
            paper_execution = (
                "No paper position is open; no broker order was submitted."
            )
        payload["latest_decision"] = {
            "updated_at": payload["updated_at"],
            "symbol": decision.get("symbol"),
            "exchange": decision.get("exchange"),
            "action": decision.get("action"),
            "reasoning": decision.get("reasoning"),
            "confluence": decision.get("confluence"),
            "market_indicators": {
                key: indicators[key]
                for key in ("close", "ema_short", "ema_long", "vwap", "rsi", "atr", "candle_time")
                if key in indicators
            },
            "entry_price": decision.get("entry_price"),
            "stop_loss": decision.get("stop_loss"),
            "target": decision.get("target"),
            "quantity": decision.get("quantity"),
            "paper_execution": paper_execution,
        }
    status_path = PAPER_STATUS_PATH
    try:
        status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not update paper monitor status: {exc}") from exc


def run_telegram_service():
    stop_event = threading.Event()
    try:
        listen_for_status_commands(stop_event, telegram_bot_status)
    except KeyboardInterrupt:
        stop_event.set()
        print("\nTelegram listener stopped.")


class MarketExpertGemini:
    """Analyze current Angel One candles using transparent technical rules."""

    def assess(self, symbol: str, candles, max_risk: float, max_quantity: int, lot_size=1):
        indicators = calculate_indicators(candles)
        return analyze_market(symbol, indicators, max_risk, max_quantity, lot_size)


def generate_trade_plan(
    symbol: str,
    market_condition: str,
    sector: str,
    strategy: str,
    entry_price: float,
    stop_loss: float,
    target: float,
    quantity: int,
    max_risk: float,
):
    """Validate a manually supplied trade plan; this is not a market signal."""
    numeric_values = [entry_price, stop_loss, target, quantity, max_risk]
    if not all(isfinite(value) for value in numeric_values):
        raise ValueError("Trade inputs must be finite numbers.")
    if entry_price <= 0 or stop_loss <= 0 or target <= 0 or quantity <= 0 or max_risk <= 0:
        raise ValueError("Trade inputs must be positive and greater than zero.")
    action = (market_condition or "").strip().upper()
    if action not in {"BUY", "BULLISH", "UPTREND", "SELL", "BEARISH", "DOWNTREND"}:
        raise ValueError("market_condition must describe a bullish or bearish setup.")
    action = "BUY" if action in {"BUY", "BULLISH", "UPTREND"} else "SELL"
    if (action == "BUY" and not stop_loss < entry_price < target) or (
        action == "SELL" and not target < entry_price < stop_loss
    ):
        raise ValueError("Entry, stop loss, and target must be ordered consistently with the trade direction.")

    risk = abs(entry_price - stop_loss) * quantity
    if risk > max_risk:
        raise ValueError(f"Calculated risk ({risk:.2f}) exceeds max_risk ({max_risk:.2f}).")
    return {
        "symbol": symbol.upper(),
        "agent_name": "Market Expert Gemini",
        "action": action,
        "market_condition": market_condition,
        "sector": sector.upper(),
        "strategy": strategy.lower(),
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss),
        "target": float(target),
        "quantity": int(quantity),
        "max_risk": float(max_risk),
        "risk": round(risk, 2),
        "confluence": None,
        "estimated_profit": round(abs(target - entry_price) * quantity, 2),
        "paper_trade_ready": True,
        "live_trade_ready": False,
        "reasoning": "Manually supplied trade plan validated; no market signal was computed.",
    }


def build_headers(jwt_token: str, api_key: str):
    public_ip = os.getenv("ANGELONE_CLIENT_PUBLIC_IP", "").strip()
    try:
        if ipaddress.ip_address(public_ip).version != 4:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Set ANGELONE_CLIENT_PUBLIC_IP to your public IPv4 address.") from exc

    mac_address = ":".join(f"{uuid.getnode():012x}"[index:index + 2] for index in range(0, 12, 2))
    headers = {
        "X-PrivateKey": api_key,
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": public_ip,
        "X-MACAddress": mac_address,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if jwt_token:
        headers["Authorization"] = "Bearer " + jwt_token
    return headers


def _api_json(response, operation):
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{operation} failed: Angel One returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{operation} failed: Angel One returned an invalid response shape.")
    if response.status_code != 200 or not data.get("status", True):
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            message = str(response.status_code)
        raise RuntimeError(f"{operation} failed: {message}")
    return data


def login(client_code: str, password: str, totp: str, api_key: str):
    """Login to Angel One SmartAPI and return the JWT token."""
    url = f"{BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"
    payload = {
        "clientcode": client_code,
        "password": password,
        "totp": totp,
    }
    response = requests.post(url, json=payload, headers=build_headers("", api_key), timeout=30)
    data = _api_json(response, "Login")

    jwt_token = (data.get("data") or {}).get("jwtToken")
    if not jwt_token:
        raise RuntimeError("JWT token not found in successful login response.")

    return jwt_token


def get_historical_candles(jwt_token, api_key, exchange, symbol_token, start, end):
    """Fetch same-day five-minute candles using Angel One's historical API."""
    url = f"{BASE_URL}/rest/secure/angelbroking/historical/v1/getCandleData"
    payload = {
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }
    response = requests.post(
        url,
        json=payload,
        headers=build_headers(jwt_token, api_key),
        timeout=30,
    )
    data = _api_json(response, "Historical candle request")
    candles = data.get("data")
    if not isinstance(candles, list) or not candles:
        raise RuntimeError("Angel One returned no candle data for this symbol and time range.")
    return candles


def get_ltp(jwt_token: str, api_key: str, exchange: str, symbol: str, symbol_token: str):
    """Fetch the current last-traded price from SmartAPI."""
    url = f"{BASE_URL}/rest/secure/angelbroking/order/v1/getLtpData"
    headers = build_headers(jwt_token, api_key)
    payload = {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "symboltoken": symbol_token,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    data = _api_json(response, "LTP request")
    ltp = (data.get("data") or {}).get("ltp")
    if ltp is None or float(ltp) <= 0:
        raise RuntimeError("Angel One returned an invalid last-traded price.")
    return float(ltp)


def backtest_strategy(
    candles,
    symbol,
    max_risk,
    max_quantity,
    lot_size=1,
    round_trip_fee_per_unit=None,
):
    """Replay close-based signals at the next candle open; conservatively resolve intrabar exits."""
    if not isfinite(max_risk) or max_risk <= 0:
        raise ValueError("Backtest risk per trade must be a positive finite amount.")
    if max_quantity <= 0 or lot_size <= 0 or max_quantity < lot_size:
        raise ValueError("Backtest quantity and lot size must be positive and compatible.")
    if round_trip_fee_per_unit is not None and (
        not isfinite(round_trip_fee_per_unit) or round_trip_fee_per_unit < 0
    ):
        raise ValueError("Round-trip fee per unit must be finite and non-negative.")

    normalized = []
    for row in candles:
        if len(row) < 6:
            raise ValueError("Each backtest row needs timestamp, OHLC, and volume.")
        timestamp = str(row[0])
        prices_and_volume = [float(value) for value in row[1:6]]
        normalized.append([timestamp, *prices_and_volume])
    if len(normalized) < EMA_LONG_PERIOD + 2:
        raise ValueError(
            f"Backtesting requires at least {EMA_LONG_PERIOD + 2} candles."
        )
    for index in range(1, len(normalized)):
        if _candle_datetime(normalized[index][0]) <= _candle_datetime(normalized[index - 1][0]):
            raise ValueError("Backtest candles must be in strictly increasing timestamp order.")
    calculate_indicators(normalized)

    def close_trade(position, price, timestamp, reason):
        direction = 1 if position["action"] == "BUY" else -1
        gross = round(
            (price - position["entry_price"]) * direction * position["quantity"], 2
        )
        fee = (
            round(round_trip_fee_per_unit * position["quantity"], 2)
            if round_trip_fee_per_unit is not None
            else None
        )
        trades.append({
            "symbol": symbol.upper(),
            "action": position["action"],
            "quantity": position["quantity"],
            "entry_time": position["entry_time"],
            "entry_price": round(position["entry_price"], 2),
            "stop_loss": round(position["stop_loss"], 2),
            "target": round(position["target"], 2),
            "exit_time": timestamp,
            "exit_price": round(price, 2),
            "exit_reason": reason,
            "gross_pnl": gross,
            "estimated_fees": fee,
            "net_pnl": round(gross - fee, 2) if fee is not None else None,
        })

    def intrabar_exit(position, candle):
        timestamp, open_price, high, low, close = candle[:5]
        if position["action"] == "BUY":
            if open_price <= position["stop_loss"]:
                return open_price, "STOP_LOSS"
            if open_price >= position["target"]:
                return position["target"], "TARGET"
            stop_hit = low <= position["stop_loss"]
            target_hit = high >= position["target"]
        else:
            if open_price >= position["stop_loss"]:
                return open_price, "STOP_LOSS"
            if open_price <= position["target"]:
                return position["target"], "TARGET"
            stop_hit = high >= position["stop_loss"]
            target_hit = low <= position["target"]
        if stop_hit:
            return position["stop_loss"], "STOP_LOSS"
        if target_hit:
            return position["target"], "TARGET"
        return None

    trades = []
    position = None
    for index in range(EMA_LONG_PERIOD + 1, len(normalized)):
        candle = normalized[index]
        candle_date = _candle_datetime(candle[0]).date()
        if position is not None:
            position_date = _candle_datetime(position["entry_time"]).date()
            if candle_date != position_date:
                previous = normalized[index - 1]
                close_trade(position, previous[4], previous[0], "SESSION_END")
                position = None
                continue
            exit_result = intrabar_exit(position, candle)
            if exit_result:
                close_trade(position, exit_result[0], candle[0], exit_result[1])
                position = None
                continue
            if index == len(normalized) - 1:
                close_trade(position, candle[4], candle[0], "DATA_END")
                position = None
            elif _candle_datetime(normalized[index + 1][0]).date() != candle_date:
                close_trade(position, candle[4], candle[0], "SESSION_END")
                position = None
            continue

        history = normalized[:index]
        indicators = calculate_indicators(history)
        current_session = [
            row for row in history
            if _candle_datetime(row[0]).date() == _candle_datetime(history[-1][0]).date()
        ]
        volume = sum(row[5] for row in current_session)
        if volume:
            indicators["vwap"] = sum(
                ((row[2] + row[3] + row[4]) / 3) * row[5]
                for row in current_session
            ) / volume
        plan = analyze_market(
            symbol, indicators, max_risk, max_quantity, lot_size
        )
        if plan["action"] not in {"BUY", "SELL"}:
            continue
        if candle_date != _candle_datetime(history[-1][0]).date():
            continue

        entry = candle[1]
        risk_per_unit = entry * STOP_LOSS_PERCENT
        quantity = min(
            max_quantity,
            floor(max_risk / risk_per_unit / lot_size) * lot_size,
        )
        quantity = quantity // lot_size * lot_size
        if quantity < lot_size:
            continue
        direction = 1 if plan["action"] == "BUY" else -1
        position = {
            "action": plan["action"],
            "quantity": quantity,
            "entry_time": candle[0],
            "entry_price": entry,
            "stop_loss": entry * (1 - STOP_LOSS_PERCENT * direction),
            "target": entry * (1 + TARGET_PERCENT * direction),
        }
        exit_result = intrabar_exit(position, candle)
        if exit_result:
            close_trade(position, exit_result[0], candle[0], exit_result[1])
            position = None
        elif index == len(normalized) - 1:
            close_trade(position, candle[4], candle[0], "DATA_END")
            position = None
        elif _candle_datetime(normalized[index + 1][0]).date() != candle_date:
            close_trade(position, candle[4], candle[0], "SESSION_END")
            position = None

    gross_values = [trade["gross_pnl"] for trade in trades]
    net_values = [trade["net_pnl"] for trade in trades]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for result in net_values if round_trip_fee_per_unit is not None else gross_values:
        cumulative += result
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    wins = sum(value > 0 for value in (net_values if round_trip_fee_per_unit is not None else gross_values))
    return {
        "symbol": symbol.upper(),
        "candles": len(normalized),
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_percent": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "gross_pnl": round(sum(gross_values), 2),
        "net_pnl": round(sum(net_values), 2) if round_trip_fee_per_unit is not None else None,
        "fees_included": round_trip_fee_per_unit is not None,
        "max_drawdown": round(max_drawdown, 2),
        "trades_detail": trades,
        "caveats": [
            "Signals use data only through the previous candle and enter at the next candle open.",
            "Stop is assumed before target when both occur in one candle.",
            "Backtest estimates omit slippage, liquidity, rejected orders, and unmodeled fees.",
            "No backtest result predicts or guarantees future performance.",
        ],
    }


def load_backtest_csv(csv_path):
    path = Path(csv_path)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise ValueError("Backtest CSV must have a header row.")
            fields = {field.strip().lower(): field for field in reader.fieldnames if field}
            required = ("timestamp", "open", "high", "low", "close", "volume")
            missing = [field for field in required if field not in fields]
            if missing:
                raise ValueError(
                    "Backtest CSV is missing columns: " + ", ".join(missing)
                )
            candles = []
            for row_number, row in enumerate(reader, start=2):
                try:
                    candles.append([
                        row[fields["timestamp"]],
                        float(row[fields["open"]]),
                        float(row[fields["high"]]),
                        float(row[fields["low"]]),
                        float(row[fields["close"]]),
                        float(row[fields["volume"]]),
                    ])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid OHLCV data in CSV row {row_number}."
                    ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not read backtest CSV {path}: {exc}") from exc
    return candles


def run_backtest_command(arguments):
    parser = argparse.ArgumentParser(
        description="Replay the rule-based strategy on historical OHLCV CSV data."
    )
    parser.add_argument("--backtest", required=True, metavar="CSV")
    parser.add_argument("--risk", required=True, type=float, metavar="INR")
    parser.add_argument("--quantity", required=True, type=int, metavar="MAX_QTY")
    parser.add_argument("--lot-size", type=int, default=1)
    parser.add_argument(
        "--round-trip-fee-per-unit",
        type=float,
        help="Optional total entry+exit fee estimate per unit; omitted means net P/L unavailable.",
    )
    parser.add_argument("--symbol", default="BACKTEST")
    options = parser.parse_args(arguments)
    result = backtest_strategy(
        load_backtest_csv(options.backtest),
        options.symbol,
        options.risk,
        options.quantity,
        options.lot_size,
        options.round_trip_fee_per_unit,
    )
    print(json.dumps(result, indent=2))


def export_candles_command(arguments):
    parser = argparse.ArgumentParser(
        description="Export five-minute Angel One candles to a local CSV."
    )
    parser.add_argument("--export-candles", required=True, metavar="CSV")
    parser.add_argument("--start", required=True, help="IST time: YYYY-MM-DD HH:MM")
    parser.add_argument("--end", required=True, help="IST time: YYYY-MM-DD HH:MM")
    options = parser.parse_args(arguments)
    try:
        start = datetime.strptime(options.start, "%Y-%m-%d %H:%M").replace(
            tzinfo=MARKET_TIMEZONE
        )
        end = datetime.strptime(options.end, "%Y-%m-%d %H:%M").replace(
            tzinfo=MARKET_TIMEZONE
        )
    except ValueError as exc:
        raise ValueError("Start and end must use YYYY-MM-DD HH:MM IST format.") from exc
    if start >= end:
        raise ValueError("Export start time must be earlier than the end time.")
    if end > datetime.now(MARKET_TIMEZONE):
        raise ValueError("Export end time cannot be in the future.")

    output_path = Path(options.export_candles)
    if output_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing candle file: {output_path}"
        )
    client_code = os.getenv("ANGELONE_CLIENT_CODE", "").strip()
    password = os.getenv("ANGELONE_PASSWORD", "").strip()
    api_key = os.getenv("ANGELONE_API_KEY", "").strip()
    for name, value in (
        ("ANGELONE_CLIENT_CODE", client_code),
        ("ANGELONE_PASSWORD", password),
        ("ANGELONE_API_KEY", api_key),
    ):
        if not value or value.startswith("YOUR_"):
            prompt = f"{name}: "
            value = (getpass.getpass if "PASSWORD" in name or "API_KEY" in name else input)(prompt).strip()
            if not value:
                raise ValueError(f"{name} is required for candle export.")
            if name == "ANGELONE_CLIENT_CODE":
                client_code = value
            elif name == "ANGELONE_PASSWORD":
                password = value
            else:
                api_key = value
    totp_secret = os.getenv("ANGELONE_TOTP_SECRET", "").strip()
    if totp_secret and not totp_secret.startswith("YOUR_"):
        totp = generate_totp(totp_secret)
    else:
        totp = getpass.getpass("Current six-digit Angel One TOTP: ").strip()
    if not totp:
        raise ValueError("A current TOTP is required for Angel One login.")
    exchange = os.getenv("ANGELONE_EXCHANGE", "NSE").strip().upper()
    symbol = os.getenv("ANGELONE_SYMBOL", "RELIANCE").strip().upper()
    symbol_token = os.getenv("ANGELONE_SYMBOL_TOKEN", "").strip()
    if not symbol_token or symbol_token.startswith("YOUR_"):
        raise ValueError("Set ANGELONE_SYMBOL_TOKEN in .env before exporting candles.")

    jwt_token = login(client_code, password, totp, api_key)
    candles = get_historical_candles(
        jwt_token, api_key, exchange, symbol_token, start, end
    )
    if not candles:
        raise RuntimeError("Angel One returned no candles for the requested range.")
    previous_time = None
    for index, candle in enumerate(candles, start=1):
        if len(candle) < 6:
            raise RuntimeError(
                f"Angel One returned a malformed historical candle at row {index}."
            )
        timestamp = _candle_datetime(candle[0])
        numeric = [float(value) for value in candle[1:6]]
        open_price, high, low, close, volume = numeric
        if (
            not all(isfinite(value) for value in numeric)
            or min(open_price, high, low, close) <= 0
            or volume < 0
            or high < max(open_price, close, low)
            or low > min(open_price, close, high)
            or (previous_time is not None and timestamp <= previous_time)
        ):
            raise RuntimeError(
                f"Angel One returned invalid or out-of-order candle data at row {index}."
            )
        previous_time = timestamp
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for candle in candles:
                writer.writerow(candle[:6])
    except OSError as exc:
        raise RuntimeError(f"Could not write candle CSV {output_path}: {exc}") from exc
    print(f"Exported {len(candles)} five-minute candles for {symbol} to {output_path}.")


def check_broker_state_command():
    client_code = os.getenv("ANGELONE_CLIENT_CODE", "").strip()
    password = os.getenv("ANGELONE_PASSWORD", "").strip()
    api_key = os.getenv("ANGELONE_API_KEY", "").strip()
    if not client_code or not password or not api_key:
        raise RuntimeError(
            "Configure ANGELONE_CLIENT_CODE, ANGELONE_PASSWORD, and ANGELONE_API_KEY "
            "in the local encrypted vault first."
        )
    totp_secret = os.getenv("ANGELONE_TOTP_SECRET", "").strip()
    if totp_secret and not totp_secret.startswith("YOUR_"):
        totp = generate_totp(totp_secret)
    else:
        totp = getpass.getpass("Current six-digit Angel One TOTP: ").strip()
    if not totp:
        raise ValueError("A current TOTP is required for the read-only account check.")
    jwt_token = login(client_code, password, totp, api_key)
    snapshot = get_broker_reconciliation(jwt_token, api_key)
    print(json.dumps(snapshot, indent=2))
    print(
        "Read-only account check complete. No orders were placed, modified, or cancelled."
    )


def recover_execution_journal_command():
    journal = ExecutionJournal()
    journal.recover_after_restart()
    unresolved = journal.unresolved_intents()
    if not unresolved:
        print("No unresolved local execution intents require recovery.")
        return
    print("Unresolved local execution intents:")
    for intent in unresolved:
        print(f"- {intent['intent_key']}: {intent['status']}")

    client_code = os.getenv("ANGELONE_CLIENT_CODE", "").strip()
    password = os.getenv("ANGELONE_PASSWORD", "").strip()
    api_key = os.getenv("ANGELONE_API_KEY", "").strip()
    if not client_code or not password or not api_key:
        raise RuntimeError(
            "Configure Angel One credentials in the local encrypted vault to run "
            "a read-only reconciliation before resolving journal entries."
        )
    totp_secret = os.getenv("ANGELONE_TOTP_SECRET", "").strip()
    if totp_secret and not totp_secret.startswith("YOUR_"):
        totp = generate_totp(totp_secret)
    else:
        totp = getpass.getpass("Current six-digit Angel One TOTP: ").strip()
    if not totp:
        raise ValueError("A current TOTP is required for execution-journal recovery.")
    jwt_token = login(client_code, password, totp, api_key)
    snapshot = get_broker_reconciliation(jwt_token, api_key)
    print(
        "Read-only broker snapshot: "
        f"{snapshot['status']}; positions={snapshot['position_count']}; "
        f"pending={snapshot['pending_order_count']}; "
        f"partial_fills={snapshot['partial_fill_order_count']}; "
        f"rejected={snapshot['rejected_order_count']}; "
        f"uncertain={snapshot['uncertain_order_count']}; "
        f"discrepancies={len(snapshot['discrepancies'])}."
    )
    if snapshot["status"] != "NO_DISCREPANCY_DETECTED":
        raise RuntimeError(
            "Broker state needs review; execution journal remains unresolved."
        )
    phrase = f"CONFIRM FLAT ACCOUNT {datetime.now(MARKET_TIMEZONE).date().isoformat()}"
    confirmation = input(
        "Only after checking the Angel One app, type exactly "
        f"{phrase!r} to resolve these local intents: "
    ).strip()
    if confirmation != phrase:
        raise RuntimeError("Execution-journal recovery was not confirmed.")
    for intent in unresolved:
        journal.resolve_flat_after_manual_review(
            intent["intent_key"],
            snapshot,
            operator_confirmed=True,
        )
    print(
        f"Resolved {len(unresolved)} local intent(s) after explicit confirmation "
        "and a fresh flat read-only broker snapshot. No broker order was sent."
    )


def live_readiness_command(broker_evidence=None):
    broker_evidence = broker_evidence or {}
    report = build_live_readiness_report({
        "client_public_ip": os.getenv("ANGELONE_CLIENT_PUBLIC_IP", ""),
        "broker_authenticated": broker_evidence.get("authenticated"),
        "broker_instrument_verified": broker_evidence.get("instrument_verified"),
        "broker_margin_available": broker_evidence.get("margin_available"),
        "broker_state_status": broker_evidence.get("broker_state_status"),
        "credentials_configured": all(
            os.getenv(name, "").strip()
            for name in (
                "ANGELONE_CLIENT_CODE",
                "ANGELONE_PASSWORD",
                "ANGELONE_API_KEY",
            )
        ),
        "trading_mode": os.getenv("ANGELONE_TRADING_MODE", "paper"),
        "place_order_enabled": os.getenv(
            "ANGELONE_PLACE_ORDER", "false"
        ).strip().lower() == "true",
        "exchange": os.getenv("ANGELONE_EXCHANGE", ""),
        "symbol": os.getenv("ANGELONE_SYMBOL", ""),
        "symbol_token": os.getenv("ANGELONE_SYMBOL_TOKEN", ""),
        "instrument_type": os.getenv("ANGELONE_INSTRUMENT_TYPE", ""),
        "product_type": os.getenv("ANGELONE_PRODUCT_TYPE", ""),
        "tick_size": os.getenv("ANGELONE_TICK_SIZE", ""),
        "lot_size": os.getenv("ANGELONE_LOT_SIZE", ""),
        "quantity": os.getenv("ANGELONE_ORDER_QUANTITY", ""),
        "max_daily_trades": os.getenv("ANGELONE_MAX_DAILY_TRADES", ""),
        "order_type": os.getenv("ANGELONE_ORDER_TYPE", ""),
        "order_validity": os.getenv("ANGELONE_ORDER_VALIDITY", ""),
        "session_start": os.getenv("ANGELONE_SESSION_START", ""),
        "session_end": os.getenv("ANGELONE_SESSION_END", ""),
        "stop_loss_percent": STOP_LOSS_PERCENT,
        "target_percent": TARGET_PERCENT,
    })
    print(json.dumps(report, indent=2))


def broker_preflight_command():
    client_code = os.getenv("ANGELONE_CLIENT_CODE", "").strip()
    password = os.getenv("ANGELONE_PASSWORD", "").strip()
    api_key = os.getenv("ANGELONE_API_KEY", "").strip()
    if not client_code or not password or not api_key:
        raise RuntimeError(
            "Configure Angel One credentials in the local encrypted vault before "
            "running the read-only broker preflight."
        )
    totp_secret = os.getenv("ANGELONE_TOTP_SECRET", "").strip()
    if totp_secret and not totp_secret.startswith("YOUR_"):
        totp = generate_totp(totp_secret)
    else:
        totp = getpass.getpass("Current six-digit Angel One TOTP: ").strip()
    if not totp:
        raise ValueError("A current TOTP is required for the read-only broker preflight.")

    exchange = os.getenv("ANGELONE_EXCHANGE", "").strip().upper()
    symbol = os.getenv("ANGELONE_SYMBOL", "").strip().upper()
    symbol_token = os.getenv("ANGELONE_SYMBOL_TOKEN", "").strip()
    if not exchange or not symbol or not symbol_token or symbol_token.startswith("YOUR_"):
        raise RuntimeError(
            "Set the exact Angel One exchange, trading symbol, and symbol token "
            "before the read-only broker preflight."
        )

    jwt_token = login(client_code, password, totp, api_key)
    instrument = verify_broker_instrument(
        jwt_token, api_key, exchange, symbol, symbol_token
    )
    margins = get_account_margins(jwt_token, api_key)
    snapshot = get_broker_reconciliation(jwt_token, api_key)
    evidence = {
        "authenticated": True,
        "instrument_verified": instrument["verified"],
        "margin_available": margins["available_cash"] > 0,
        "broker_state_status": snapshot["status"],
    }
    print(
        json.dumps(
            {
                "read_only": True,
                "instrument": instrument,
                "available_margin_positive": evidence["margin_available"],
                "broker_state_status": snapshot["status"],
                "pending_order_count": snapshot["pending_order_count"],
                "partial_fill_order_count": snapshot["partial_fill_order_count"],
                "rejected_order_count": snapshot["rejected_order_count"],
                "uncertain_order_count": snapshot["uncertain_order_count"],
                "position_count": snapshot["position_count"],
                "discrepancy_count": len(snapshot["discrepancies"]),
                "live_submission_enabled": False,
            },
            indent=2,
        )
    )
    live_readiness_command(evidence)
    print(
        "Read-only preflight complete. No order was placed, modified, or cancelled."
    )


def main(unattended=False):
    trading_mode = os.getenv("ANGELONE_TRADING_MODE", "paper").strip().lower()
    if trading_mode not in {"paper", "supervised", "live"}:
        raise ValueError("ANGELONE_TRADING_MODE must be 'paper', 'supervised', or 'live'.")
    if trading_mode == "live" or os.getenv("ANGELONE_PLACE_ORDER", "false").strip().lower() == "true":
        raise RuntimeError(
            "Real order submission is disabled until the bot can place, verify, and manage "
            "broker-side 1% stop-loss/target exits for every supported Angel One segment. "
            "Use ANGELONE_TRADING_MODE=paper or supervised."
        )
    if trading_mode == "supervised" and unattended:
        raise RuntimeError(
            "Supervised proposal mode requires an interactive terminal and cannot run unattended. "
            "Use ANGELONE_TRADING_MODE=paper for unattended simulation."
        )
    execution_journal = None
    if trading_mode == "supervised":
        execution_journal = ExecutionJournal()
        execution_journal.recover_after_restart()
        execution_journal.assert_no_unresolved()
    if unattended:
        totp_secret = os.getenv("ANGELONE_TOTP_SECRET", "").strip()
        if not totp_secret or totp_secret.startswith("YOUR_"):
            raise RuntimeError(
                "Unattended paper monitoring needs ANGELONE_TOTP_SECRET configured securely "
                "on the server. Do not paste it into chat or commit it."
            )
        generate_totp(totp_secret)
        smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        if not smtp_password or smtp_password.startswith("YOUR_"):
            raise RuntimeError(
                "Unattended end-of-day email needs SMTP_PASSWORD configured securely on the server."
            )

    def get_config_or_prompt(name, prompt, secret=False):
        value = os.getenv(name, "").strip()
        if not value or value.startswith("YOUR_"):
            if unattended:
                raise RuntimeError(f"Set {name} in the protected server .env for unattended mode.")
            return (getpass.getpass if secret else input)(prompt).strip()
        return value

    client_code = get_config_or_prompt("ANGELONE_CLIENT_CODE", "Angel One client code: ")
    password = get_config_or_prompt("ANGELONE_PASSWORD", "Angel One password: ", secret=True)
    api_key = get_config_or_prompt("ANGELONE_API_KEY", "Angel One API key: ", secret=True)
    if not client_code or not password or not api_key:
        raise ValueError("Missing client code, password, or API key.")

    exchange = os.getenv("ANGELONE_EXCHANGE", "NSE").strip().upper()
    symbol = os.getenv("ANGELONE_SYMBOL", "RELIANCE").strip().upper()
    symbol_token = os.getenv("ANGELONE_SYMBOL_TOKEN", "").strip()
    max_risk = (
        None
        if trading_mode == "supervised"
        else float(os.getenv("ANGELONE_MAX_RISK", "0"))
    )
    max_quantity = int(os.getenv("ANGELONE_ORDER_QUANTITY", "1"))
    max_daily_trades = int(os.getenv("ANGELONE_MAX_DAILY_TRADES", "3"))
    lot_size = int(os.getenv("ANGELONE_LOT_SIZE", "1"))
    session_start_text = os.getenv("ANGELONE_SESSION_START", "09:15")
    session_end_text = os.getenv("ANGELONE_SESSION_END", "15:30")
    if lot_size < 1:
        raise ValueError("ANGELONE_LOT_SIZE must be at least 1.")
    try:
        session_start_time = datetime.strptime(session_start_text, "%H:%M").time()
        session_end_time = datetime.strptime(session_end_text, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Session times must use 24-hour HH:MM format.") from exc
    if session_start_time >= session_end_time:
        raise ValueError("Session end must be later than session start.")
    if not symbol_token or symbol_token == "YOUR_SYMBOL_TOKEN":
        raise ValueError("Set ANGELONE_SYMBOL_TOKEN to the real Angel One symbol token.")
    if trading_mode == "paper" and max_daily_trades < 1:
        raise ValueError("ANGELONE_MAX_DAILY_TRADES must be at least 1.")
    if (
        (trading_mode == "paper" and max_risk <= 0)
        or max_quantity < lot_size
    ):
        raise ValueError(
            "Set ANGELONE_MAX_RISK above zero and ANGELONE_ORDER_QUANTITY "
            "to at least one configured lot."
        )
    now = datetime.now(MARKET_TIMEZONE)
    session_start = now.replace(
        hour=session_start_time.hour,
        minute=session_start_time.minute,
        second=0,
        microsecond=0,
    )
    session_end = now.replace(
        hour=session_end_time.hour,
        minute=session_end_time.minute,
        second=0,
        microsecond=0,
    )
    if now.weekday() >= 5:
        raise RuntimeError("No weekday market session today. Run the bot on a trading weekday.")
    if now > session_end:
        raise RuntimeError("The market session has ended; run the bot before 15:30 IST for today's report.")
    if trading_mode == "supervised":
        if now >= session_start:
            raise RuntimeError(
                "Supervised session authorization must be given before the configured market open."
            )
    if trading_mode == "paper" and now < session_start:
        wait_seconds = (session_start - now).total_seconds()
        print(f"Waiting until the 09:15 IST session open ({int(wait_seconds)} seconds).")
        time.sleep(wait_seconds)

    totp_secret = os.getenv("ANGELONE_TOTP_SECRET", "").strip()
    if totp_secret and not totp_secret.startswith("YOUR_"):
        totp = generate_totp(totp_secret)
    else:
        totp = input("Enter the current TOTP code now: ").strip()
        if not totp:
            raise ValueError("A current TOTP code or ANGELONE_TOTP_SECRET is required to log in.")
    jwt_token = login(client_code, password, totp, api_key)
    margins = None
    margin_error = None
    max_session_loss = None
    if trading_mode == "supervised":
        require_clear_broker_state_for_proposals(
            get_broker_reconciliation(jwt_token, api_key)
        )
        margins = get_account_margins(jwt_token, api_key)
        if margins["available_cash"] <= 0:
            raise RuntimeError(
                "Pre-market available margin must be positive before authorizing proposals."
            )
        max_session_loss = one_percent_of_margin(margins["available_cash"])
        if not request_supervised_session_authorization(
            datetime.now(MARKET_TIMEZONE).date(), margins["available_cash"]
        ):
            raise RuntimeError("Supervised proposal session was not authorized; stopping.")
        now = datetime.now(MARKET_TIMEZONE)
        if now >= session_start:
            raise RuntimeError(
                "Pre-market authorization completed after market open; stopping this session."
            )
        wait_seconds = (session_start - now).total_seconds()
        print(f"Waiting until the configured market open ({int(wait_seconds)} seconds).")
        time.sleep(wait_seconds)
    latest_candles = []
    market_plan = {
        "symbol": symbol,
        "exchange": exchange,
        "action": "HOLD",
        "reasoning": "No market analysis has run yet.",
    }
    ledger_path = PAPER_LEDGER_PATH
    trade_date = datetime.now(MARKET_TIMEZONE).date()
    proposal_count = 0
    proposal_risk_reserved = 0.0

    while True:
        now = datetime.now(MARKET_TIMEZONE)
        query_end = min(now, session_end)
        latest_candles = get_historical_candles(
            jwt_token, api_key, exchange, symbol_token, session_start, query_end
        )
        is_market_open = now.weekday() < 5 and session_start <= now <= session_end
        if is_market_open or now >= session_end:
            current_price = get_ltp(jwt_token, api_key, exchange, symbol, symbol_token)
            latest_candle = list(latest_candles[-1])
            latest_candle[2] = max(float(latest_candle[2]), current_price)
            latest_candle[3] = min(float(latest_candle[3]), current_price)
            latest_candle[4] = current_price
            latest_candles[-1] = latest_candle

        if len(latest_candles) < EMA_LONG_PERIOD + 1:
            market_plan = {
                "symbol": symbol,
                "exchange": exchange,
                "action": "HOLD",
                "confluence": 0.0,
                "reasoning": f"Waiting for at least {EMA_LONG_PERIOD + 1} candles before analyzing.",
            }
        else:
            market_plan = MarketExpertGemini().assess(
                symbol, latest_candles, max_risk, max_quantity, lot_size
            )
            market_plan["exchange"] = exchange

        last_candle_time = _candle_datetime(latest_candles[-1][0])
        is_fresh = timedelta(0) <= now - last_candle_time <= timedelta(minutes=10)
        if not is_market_open or not is_fresh:
            market_plan["action"] = "HOLD"
            market_plan["reasoning"] += " Market is closed or the latest candle is stale; new entries are blocked."

        margin_error = None
        try:
            margins = get_account_margins(jwt_token, api_key)
            margin_error = None
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            margins = None
            margin_error = str(exc)
        fee_estimator = lambda position, exit_price: estimate_round_trip_charges(
            jwt_token,
            api_key,
            position.get("exchange") or exchange,
            position["symbol"],
            symbol_token,
            position["action"],
            position["entry_price"],
            exit_price,
            position["quantity"],
        )
        trades = load_paper_trades(ledger_path) if trading_mode == "paper" else []
        today_trades = [
            trade for trade in trades
            if trade.get("entry_time", "").startswith(trade_date.isoformat())
            and trade.get("symbol") == symbol
        ]
        daily_count = len(today_trades)
        if (
            trading_mode == "paper"
            and daily_count >= max_daily_trades
            and market_plan["action"] in {"BUY", "SELL"}
        ):
            market_plan["action"] = "HOLD"
            market_plan["reasoning"] += " Configured maximum daily trade count has been reached."

        if market_plan["action"] in {"BUY", "SELL"}:
            try:
                if margins is None:
                    raise RuntimeError(f"Available margin could not be verified: {margin_error}")
                if trading_mode == "supervised":
                    execution_journal.assert_no_unresolved()
                    require_clear_broker_state_for_proposals(
                        get_broker_reconciliation(jwt_token, api_key)
                    )

                    def margin_estimator(action, entry_price, quantity):
                        return estimate_required_margin(
                            jwt_token,
                            api_key,
                            exchange,
                            symbol_token,
                            action,
                            entry_price,
                            quantity,
                        )

                    def charge_estimator(action, entry_price, exit_price, quantity):
                        return estimate_round_trip_charges(
                            jwt_token,
                            api_key,
                            exchange,
                            symbol,
                            symbol_token,
                            action,
                            entry_price,
                            exit_price,
                            quantity,
                        )

                    sized_plan = size_supervised_proposal(
                        market_plan,
                        max_quantity,
                        lot_size,
                        margins["available_cash"],
                        margin_estimator,
                        charge_estimator,
                    )
                    if sized_plan is None:
                        market_plan["action"] = "HOLD"
                        market_plan["reasoning"] += (
                            " No permitted lot size meets both available-margin and "
                            "1%-of-required-margin estimated-loss limits."
                        )
                    else:
                        candidate_risk = estimate_proposal_stop_risk(sized_plan)
                        if proposal_risk_reserved + candidate_risk > max_session_loss:
                            market_plan["action"] = "HOLD"
                            market_plan["reasoning"] += (
                                " Estimated stop loss including charges exceeds the "
                                "remaining 1%-of-pre-market-margin session budget."
                            )
                        else:
                            market_plan = sized_plan
                else:
                    market_plan["margin_required"] = estimate_required_margin(
                        jwt_token,
                        api_key,
                        exchange,
                        symbol_token,
                        market_plan["action"],
                        market_plan["entry_price"],
                        market_plan["quantity"],
                    )
                    if market_plan["margin_required"] > margins["available_cash"]:
                        market_plan["action"] = "HOLD"
                        market_plan["reasoning"] += " Required margin exceeds account available cash."
                    else:
                        market_plan["estimated_charges_at_target"] = estimate_round_trip_charges(
                            jwt_token, api_key, exchange, symbol, symbol_token,
                            market_plan["action"], market_plan["entry_price"],
                            market_plan["target"], market_plan["quantity"],
                        )
                        market_plan["estimated_net_profit_at_target"] = round(
                            (abs(market_plan["target"] - market_plan["entry_price"]) * market_plan["quantity"])
                            - market_plan["estimated_charges_at_target"],
                            2,
                        )
                        market_plan["estimated_charges_at_stop"] = estimate_round_trip_charges(
                            jwt_token, api_key, exchange, symbol, symbol_token,
                            market_plan["action"], market_plan["entry_price"],
                            market_plan["stop_loss"], market_plan["quantity"],
                        )
            except (RuntimeError, ValueError, requests.RequestException) as exc:
                market_plan["action"] = "HOLD"
                market_plan["reasoning"] += f" Trade blocked: margin/charge validation failed: {exc}"

        force_close = now >= session_end
        if force_close:
            market_plan["action"] = "HOLD"
        if trading_mode == "supervised" and market_plan["action"] in {"BUY", "SELL"}:
            execution_journal.assert_no_unresolved()
            proposal_risk = estimate_proposal_stop_risk(market_plan)
            proposal_risk_reserved += proposal_risk
            proposal_count += 1
            request_supervised_proposal(
                market_plan,
                margins,
                proposal_risk,
                max_session_loss - proposal_risk_reserved,
            )
        if trading_mode == "paper":
            ledger = update_paper_ledger(
                latest_candles,
                market_plan,
                ledger_path,
                fee_estimator=fee_estimator,
                force_close=force_close,
            )
            write_paper_monitor_status(
                "running",
                f"{symbol} {market_plan['action']}",
                decision=market_plan,
                ledger=ledger,
            )
        else:
            ledger = {"closed_trades": 0, "realized_pnl": 0, "net_pnl": None}

        print(
            f"\nMarket Expert Gemini — {trading_mode.upper()} MODE — "
            f"{now.isoformat()}"
        )
        print(json.dumps(market_plan, indent=2))
        if margins:
            print(
                f"Available funds: Rs {margins['available_cash']:.2f}; "
                f"utilized margin: Rs {margins['utilised_margin']:.2f}"
            )
        else:
            print(f"Account margin unavailable: {margin_error}")
        if trading_mode == "paper":
            print(
                f"Closed paper trades: {ledger['closed_trades']}; "
                f"gross realized P/L: Rs {ledger['realized_pnl']:.2f}; "
                f"net realized P/L: "
                f"{'unavailable' if ledger['net_pnl'] is None else f'Rs {ledger['net_pnl']:.2f}'}"
            )
        else:
            print(
                f"Supervised proposals presented this run: {proposal_count}; "
                f"risk reserved: Rs {proposal_risk_reserved:.2f}/"
                f"Rs {max_session_loss:.2f}; "
                "real order submissions: disabled."
            )

        if force_close:
            report = build_end_of_day_report(
                trade_date,
                trading_mode,
                load_paper_trades(ledger_path) if trading_mode == "paper" else [],
                margins,
                margin_error,
                proposal_count=proposal_count,
                proposal_risk_reserved=proposal_risk_reserved,
            )
            email_end_of_day_report(report, allow_prompt=not unattended)
            if trading_mode == "paper":
                write_paper_monitor_status("closed")
            print(f"End-of-session {trading_mode} report emailed successfully.")
            return

        time.sleep(min(POLL_INTERVAL_SECONDS, max(1, int((session_end - now).total_seconds()))))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--check-broker-state":
        check_broker_state_command()
    elif len(sys.argv) == 2 and sys.argv[1] == "--recover-execution-journal":
        recover_execution_journal_command()
    elif len(sys.argv) == 2 and sys.argv[1] == "--live-readiness":
        live_readiness_command()
    elif len(sys.argv) == 2 and sys.argv[1] == "--broker-preflight":
        broker_preflight_command()
    elif len(sys.argv) == 2 and sys.argv[1] == "--configure-local-secrets":
        secret_names = (
            "ANGELONE_CLIENT_CODE",
            "ANGELONE_PASSWORD",
            "ANGELONE_API_KEY",
            "ANGELONE_TOTP_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "SMTP_PASSWORD",
        )
        configured = {}
        for name in secret_names:
            value = getpass.getpass(
                f"{name} (leave blank to keep existing or skip): "
            ).strip()
            if value:
                configured[name] = value
        count = save_secrets(configured)
        load_secrets_into_environment()
        required_missing = [
            name for name in (
                "ANGELONE_CLIENT_CODE",
                "ANGELONE_PASSWORD",
                "ANGELONE_API_KEY",
            )
            if name not in load_secrets()
        ]
        print(f"Verified {count} secret values in the current Windows user's encrypted vault.")
        if required_missing:
            print("Still required for Angel One API access: " + ", ".join(required_missing))
    elif len(sys.argv) == 2 and sys.argv[1] == "--secure-local-secrets":
        count, removed = migrate_env_secrets()
        print(
            f"Encrypted {count} configured secret values into the current Windows "
            f"user's local vault; removed {removed} secret settings from .env."
        )
    elif len(sys.argv) >= 2 and sys.argv[1] == "--backtest":
        run_backtest_command(sys.argv[1:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--export-candles":
        export_candles_command(sys.argv[1:])
    elif len(sys.argv) == 2 and sys.argv[1] == "--test-email":
        send_test_email()
    elif len(sys.argv) == 2 and sys.argv[1] == "--test-telegram":
        send_telegram_test_message()
    elif len(sys.argv) == 2 and sys.argv[1] == "--discover-telegram-chat":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = discover_private_chat_id(bot_token)
        if chat_id is None:
            raise SystemExit(
                "No private /start message found. Open your bot in Telegram, press Start, "
                "then run this command again."
            )
        print(f"Private Telegram chat ID: {chat_id}")
    elif len(sys.argv) == 2 and sys.argv[1] == "--telegram-service":
        run_telegram_service()
    elif len(sys.argv) == 2 and sys.argv[1] == "--paper-monitor":
        main(unattended=True)
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit(
            "Usage: py -3 trading-agent.py "
            "[--test-email | --test-telegram | --discover-telegram-chat | "
            "--telegram-service | --paper-monitor | --recover-execution-journal | "
            "--live-readiness | "
            "--broker-preflight | "
            "--export-candles CSV "
            "--start 'YYYY-MM-DD HH:MM' --end 'YYYY-MM-DD HH:MM' | "
            "--backtest CSV --risk INR --quantity N | --check-broker-state | "
            "--secure-local-secrets | "
            "--configure-local-secrets]"
        )

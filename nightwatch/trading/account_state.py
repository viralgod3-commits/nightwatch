"""Deterministic reconciliation of a REST baseline with newer user-stream events.

Binance's account endpoints do not form an atomic snapshot. Events received
while a REST read is in flight must be replayed before publishing that baseline.
This module is deliberately independent of Qt and transport timing.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def exchange_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "false"}:
        return str(value).lower() == "true"
    return None


def position_key(row: dict) -> tuple[str, str]:
    return str(row.get("symbol", "")), str(row.get("positionSide", "BOTH"))


def event_timestamp(event: dict, row: dict | None = None) -> int:
    try:
        return int((row or {}).get('T') or event.get('T') or event.get('E') or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def older_than_row(stamp: int, row: dict) -> bool:
    try:
        return stamp > 0 and int(row.get('updateTime') or 0) > stamp
    except (TypeError, ValueError, OverflowError):
        return False


def replay_account_events(snapshot: dict, events) -> dict:
    result = deepcopy(snapshot)
    account = result.setdefault("account", {})
    for event in events:
        event_type = event.get("e")
        if event_type == "ACCOUNT_UPDATE":
            update = event.get("a") or {}
            stamp = event_timestamp(event)
            balances = {str(row.get("asset")): row for row in account.get("assets", [])}
            # Stream wallet balances are not free collateral. Invalidate sizing
            # until the follow-up REST read computes margin and open-order usage.
            account.pop("availableBalance", None)
            for balance in balances.values():
                balance.pop("availableBalance", None)
            for row in update.get("B", []):
                asset = str(row.get("a", ""))
                if asset:
                    if older_than_row(stamp, balances.get(asset, {})):
                        continue
                    balances[asset] = {**balances.get(asset, {}), "asset": asset,
                                       "walletBalance": row.get("wb", "0"),
                                       "crossWalletBalance": row.get("cw", "0"),
                                       "updateTime": stamp or balances.get(asset, {}).get('updateTime', 0)}
            account["assets"] = list(balances.values())
            positions = {position_key(row): row for row in account.get("positions", [])}
            for row in update.get("P", []):
                key = str(row.get("s", "")), str(row.get("ps", "BOTH"))
                if not key[0]:
                    continue
                if older_than_row(stamp, positions.get(key, {})):
                    continue
                positions[key] = {**positions.get(key, {}), "symbol": key[0],
                                  "positionSide": key[1], "positionAmt": row.get("pa", "0"),
                                  "entryPrice": row.get("ep", "0"),
                                  "breakEvenPrice": row.get("bep", row.get("ep", "0")),
                                  "unrealizedProfit": row.get("up", "0"),
                                  "marginType": row.get("mt", "cross"),
                                  "isolatedWallet": row.get("iw", "0"),
                                  "updateTime": stamp or positions.get(key, {}).get('updateTime', 0)}
            account["positions"] = list(positions.values())
            result["positionRisk"] = deepcopy(account["positions"])
        elif event_type == "ACCOUNT_CONFIG_UPDATE":
            config = event.get("ac") or {}
            symbol = str(config.get("s", ""))
            if symbol and config.get("l") is not None:
                configs = {str(row.get("symbol")): row for row in result.get("symbolConfig", [])}
                configs[symbol] = {**configs.get(symbol, {}), "symbol": symbol, "leverage": config["l"]}
                result["symbolConfig"] = list(configs.values())
                for row in account.get("positions", []):
                    if row.get("symbol") == symbol:
                        row["leverage"] = config["l"]
            mode = exchange_bool((event.get("ai") or {}).get("j"))
            if mode is not None:
                result.setdefault("accountConfig", {})["multiAssetsMargin"] = mode
                account["multiAssetsMargin"] = mode
        elif event_type in {"ORDER_TRADE_UPDATE", "ALGO_UPDATE"}:
            algo = event_type == "ALGO_UPDATE"
            source = event.get("o") or event.get("a") or event.get("algoOrder") or {}
            symbol = str(source.get("s") or source.get("symbol") or "")
            scope = str(result.get("ordersScope") or "ALL")
            if not symbol or scope not in {"ALL", symbol}:
                continue
            client = str(source.get("caid") or source.get("clientAlgoId") or source.get("c") or source.get("clientOrderId") or "")
            identifier = source.get("aid") or source.get("algoId") if algo else source.get("i") or source.get("orderId")
            status = str(source.get("X") or source.get("algoStatus") or source.get("status") or source.get("orderStatus") or "NEW").upper()
            field = "algoOrders" if algo else "orders"
            id_field, client_field = ("algoId", "clientAlgoId") if algo else ("orderId", "clientOrderId")
            rows = result.setdefault(field, [])
            matching = [row for row in rows if row.get("symbol") == symbol and
                        ((client and str(row.get(client_field) or "") == client) or
                         (identifier is not None and str(row.get(id_field)) == str(identifier)))]
            row = dict(matching[0]) if matching else {}
            stamp = event_timestamp(event, source)
            if older_than_row(stamp, row):
                continue
            rows[:] = [item for item in rows if item not in matching]
            row.update(symbol=symbol, status=status, updateTime=stamp or row.get('updateTime', 0))
            if client:
                row[client_field] = client
            if identifier is not None:
                row[id_field] = identifier
            for target, aliases in {
                "side": ("S", "side"), "type": ("o", "type", "orderType"),
                "positionSide": ("ps", "positionSide"), "price": ("p", "price"),
                "origQty": ("q", "origQty", "quantity"), "executedQty": ("z", "aq", "executedQty"),
                "avgPrice": ("ap", "avgPrice"), "stopPrice": ("sp", "triggerPrice", "stopPrice"),
                "reduceOnly": ("R", "reduceOnly"), "closePosition": ("cp", "closePosition"),
                "timeInForce": ("f", "timeInForce"), "workingType": ("wt", "workingType"),
            }.items():
                key = next((alias for alias in aliases if alias in source), None)
                if key is not None:
                    row[target] = source[key]
            if status in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
                rows.append(row)
    return result

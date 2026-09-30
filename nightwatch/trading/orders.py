"""Shortcut recognition and deterministic quick-order, protection, and smart-exit builders."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Protocol

from ..models import Candle, SymbolRules
from ..models import quantize_step, safe_float, validate_step


class BalanceProvider(Protocol):
    """Read-only account view needed to size a quick order."""

    def available_balance(self, asset: str) -> float: ...


def is_shift_letter_shortcut(value: str) -> bool:
    parts = str(value).split("+")
    return (
        len(parts) == 2
        and parts[0].casefold() == "shift"
        and len(parts[1]) == 1
        and parts[1].isalpha()
    )


def is_smart_exit_shortcut(value: str) -> bool:
    return str(value).replace(" ", "").casefold() == "ctrl+shift+x"


def protection_quantities(
    filled_quantity: float,
    targets: list[dict[str, Any]],
    rules: SymbolRules,
) -> list[str]:
    """Allocate the quantized total, assigning rounding residual to the last target."""
    filled = Decimal(str(filled_quantity))
    if not filled.is_finite() or filled <= 0:
        raise ValueError("The filled quantity must be positive before protection orders are built.")
    percentages = [Decimal(str(safe_float(target.get("percent")))) for target in targets]
    if any(not percent.is_finite() or percent <= 0 for percent in percentages):
        raise ValueError("Every protection target needs a positive close percentage.")
    total_percent = sum(percentages, Decimal(0))
    if total_percent > Decimal("100.000001"):
        raise ValueError("Protection close percentages cannot total more than 100%.")
    if not targets:
        return []
    budget = Decimal(quantize_step(str(filled * min(total_percent, Decimal(100)) / 100), rules.market_step))
    assigned = Decimal(0)
    quantities: list[str] = []
    for index, percent in enumerate(percentages):
        amount = budget - assigned if index == len(percentages) - 1 else Decimal(
            quantize_step(str(filled * percent / 100), rules.market_step)
        )
        if amount <= 0 or not (rules.min_market_qty <= float(amount) <= rules.max_market_qty):
            raise ValueError("A protection target is outside the exchange market-quantity range after rounding.")
        quantities.append(format(amount.normalize(), "f"))
        assigned += amount
    return quantities


def build_quick_order_request(
    gateway: BalanceProvider,
    symbol: str,
    rules: SymbolRules,
    side: str,
    preset: dict[str, Any],
    mark_price: float,
    best_bid: float,
    best_ask: float,
    hedge_mode: bool | None,
) -> dict[str, Any]:
    """Build one deterministic shortcut order from the saved quick preset."""
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Quick order side must be BUY or SELL.")
    if hedge_mode is None:
        raise ValueError(
            "Binance position mode is still loading. Refresh the account before using quick orders."
        )
    collateral_percent = safe_float(preset.get("collateral_percent"))
    if not (0 < collateral_percent <= 100):
        raise ValueError("Collateral allocation must be between 0% and 100%.")
    leverage = int(safe_float(preset.get("leverage"), 0))
    if not (1 <= leverage <= 125):
        raise ValueError("Leverage must be between 1× and 125×.")
    available = gateway.available_balance(rules.margin_asset)
    if available <= 0:
        raise ValueError("Refresh the account before using collateral-based shortcuts.")

    aggressive_reference = best_ask if side == "BUY" else best_bid
    passive_reference = best_bid if side == "BUY" else best_ask
    reference = aggressive_reference or mark_price or passive_reference
    if reference <= 0:
        raise ValueError("A current mark or order-book price is required.")

    mode = str(preset.get("order_mode") or "MARKET").upper()
    slippage_enabled = bool(preset.get("slippage_enabled"))
    if mode not in {"MARKET", "LIMIT"}:
        raise ValueError("Quick order mode must be MARKET or LIMIT.")
    time_in_force = str(preset.get("time_in_force") or "IOC").upper()
    if slippage_enabled and time_in_force == "GTX":
        raise ValueError("Slippage-capped orders cannot use GTX (post-only). Choose IOC, FOK or GTC.")
    order_type = "LIMIT" if mode == "LIMIT" or slippage_enabled else "MARKET"
    limit_price = None
    if order_type == "LIMIT":
        price = Decimal(str(passive_reference or mark_price or reference))
        if slippage_enabled:
            slippage = Decimal(str(preset.get("max_slippage_percent", 0)))
            if not slippage.is_finite() or not (0 < slippage <= 10):
                raise ValueError("Maximum slippage must be between 0% and 10%.")
            bound = Decimal(str(reference)) * (1 + (slippage if side == "BUY" else -slippage) / 100)
            price = bound
        limit_price = quantize_step(str(price), rules.tick_size, rounding=ROUND_DOWN if side == "BUY" else ROUND_UP)
        if not (Decimal(str(rules.min_price)) <= Decimal(limit_price) <= Decimal(str(rules.max_price))):
            raise ValueError("Rounded limit price is outside the exchange price range.")
        if slippage_enabled and ((side == "BUY" and Decimal(limit_price) > bound) or
                                 (side == "SELL" and Decimal(limit_price) < bound)):
            raise ValueError("The exchange tick size cannot represent the slippage cap.")
    # Reserve against the maximum entry price, including a capped BUY limit.
    sizing_price = max(Decimal(str(reference)), Decimal(limit_price)) if limit_price else Decimal(str(reference))
    notional = Decimal(str(available)) * Decimal(str(collateral_percent)) / 100 * leverage
    step = rules.market_step if order_type == "MARKET" else rules.lot_step
    quantity = quantize_step(str(notional / sizing_price), step)
    quantity_value = safe_float(quantity)
    minimum_qty = rules.min_market_qty if order_type == "MARKET" else rules.min_qty
    maximum_qty = rules.max_market_qty if order_type == "MARKET" else rules.max_qty
    if not (minimum_qty <= quantity_value <= maximum_qty):
        raise ValueError(
            f"Resulting size must be between {minimum_qty:g} and {maximum_qty:g}."
        )
    if rules.min_notional and Decimal(quantity) * sizing_price < Decimal(str(rules.min_notional)):
        raise ValueError(
            f"Order value must be at least {rules.min_notional:g} {rules.quote_asset}."
        )

    position_side = "LONG" if hedge_mode and side == "BUY" else "SHORT" if hedge_mode else "BOTH"
    reduce_only = bool(preset.get("reduce_only"))
    if hedge_mode and reduce_only:
        raise ValueError("Reduce-only shortcuts are available in one-way mode; use a close preset in hedge mode.")
    order: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
        "positionSide": position_side,
    }
    if reduce_only:
        order["reduceOnly"] = True
    if order_type == "LIMIT":
        order["price"] = limit_price
        order["timeInForce"] = time_in_force

    protections: dict[str, list[dict[str, float]]] = {"tp": [], "sl": []}
    direction = 1.0 if side == "BUY" else -1.0
    for key, enabled_key, distance_key, close_key, direction_sign in (
        ("tp", "take_profit_enabled", "take_profit_percent", "take_profit_close_percent", direction),
        ("sl", "stop_loss_enabled", "stop_loss_percent", "stop_loss_close_percent", -direction),
    ):
        if not preset.get(enabled_key):
            continue
        distance = safe_float(preset.get(distance_key))
        close_percent = safe_float(preset.get(close_key))
        if distance <= 0 or not (0 < close_percent <= 100):
            raise ValueError("Quick TP/SL distance and close percentage must be positive.")
        trigger = reference * (1.0 + direction_sign * distance / 100.0)
        protections[key].append(
            {
                "price": safe_float(quantize_step(str(trigger), rules.tick_size)),
                "percent": close_percent,
            }
        )
    return {
        "order": order,
        "protections": protections,
        "rules": rules,
        "position_intent": "REDUCE" if reduce_only else "OPEN",
        "collateral_asset": rules.margin_asset,
        "collateral_required": 0.0 if reduce_only else float(Decimal(quantity) * sizing_price / leverage),
    }


def build_magnetic_rail_order_request(
    gateway: BalanceProvider,
    symbol: str,
    rules: SymbolRules,
    rail_state: dict[str, Any],
    mark_price: float,
    best_bid: float,
    best_ask: float,
    hedge_mode: bool | None,
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one executable order from the chart-side magnetic rail state.

    The chart owns only interaction/presentation state. This builder turns that
    state into the same request shape consumed by ``MainWindow._submit_order``;
    final exchange validation remains in ``TradingGateway``.

    Entry rails size from available collateral. Explicit REDUCE / TP / SL rails
    size from the matching live position so a risk-reduction control can never
    derive quantity from free collateral and accidentally increase exposure.
    """
    symbol = str(symbol).upper().strip()
    side = str(rail_state.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Magnetic rail side must be BUY or SELL.")
    if hedge_mode is None:
        raise ValueError(
            "Position mode is still loading. Refresh the account before using the magnetic rail."
        )

    rail_price = safe_float(rail_state.get("railPrice"))
    if rail_price <= 0:
        raise ValueError("Magnetic rail price is unavailable.")
    allocation = int(safe_float(rail_state.get("sizePercent"), 0))
    if allocation not in {25, 50, 75, 100}:
        raise ValueError("Magnetic rail allocation must be 25%, 50%, 75% or 100%.")
    leverage = int(safe_float(rail_state.get("leverage"), 0))
    if not (1 <= leverage <= 125):
        raise ValueError("Magnetic rail leverage must be between 1× and 125×.")

    role = str(rail_state.get("orderRole") or "ENTRY").upper()
    if role not in {"ENTRY", "TP", "SL"}:
        role = "ENTRY"
    reducing = bool(rail_state.get("reduceOnly")) or role in {"TP", "SL"}
    order_type = str(rail_state.get("orderType") or "LIMIT").upper()
    allowed = {
        "LIMIT",
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }
    if order_type not in allowed:
        raise ValueError("Unsupported magnetic rail order type.")




    if role == "TP" and order_type not in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
        order_type = (
            "TAKE_PROFIT_MARKET"
            if order_type in {"STOP_MARKET", "TRAILING_STOP_MARKET"}
            else "TAKE_PROFIT"
        )
    elif role == "SL" and order_type not in {"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"}:
        order_type = "STOP_MARKET" if order_type == "TAKE_PROFIT_MARKET" else "STOP"
    elif role == "ENTRY" and order_type in {
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }:


        order_type = "LIMIT"

    market_quantity = order_type in {
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }
    step = rules.market_step if market_quantity else rules.lot_step
    minimum_qty = rules.min_market_qty if market_quantity else rules.min_qty
    maximum_qty = rules.max_market_qty if market_quantity else rules.max_qty

    position_side = "BOTH"
    if reducing:
        matching: list[dict[str, Any]] = []
        for row in positions:
            if str(row.get("symbol") or "").upper() != symbol:
                continue
            amount = safe_float(row.get("positionAmt"))
            if abs(amount) <= 0:
                continue
            row_side = str(row.get("positionSide") or "BOTH").upper()
            if hedge_mode:
                if side == "SELL" and row_side == "LONG" and amount > 0:
                    matching.append(row)
                elif side == "BUY" and row_side == "SHORT" and amount < 0:
                    matching.append(row)
            elif row_side == "BOTH":
                if side == "SELL" and amount > 0:
                    matching.append(row)
                elif side == "BUY" and amount < 0:
                    matching.append(row)
        if not matching:
            direction = "long" if side == "SELL" else "short"
            raise ValueError(f"No {direction} {symbol} position is available to reduce.")
        if len(matching) > 1:
            raise ValueError("Select an unambiguous position before using a reduce rail.")
        position = matching[0]
        position_side = str(position.get("positionSide") or "BOTH").upper()
        total_quantity = abs(safe_float(position.get("positionAmt")))
        quantity = quantize_step(
            str(total_quantity * allocation / 100.0),
            step,
        )
    else:
        available = gateway.available_balance(rules.margin_asset)
        if available <= 0:
            raise ValueError("Refresh the account before using collateral-based rail sizing.")
        sizing_price = rail_price or mark_price or best_ask or best_bid
        if sizing_price <= 0:
            raise ValueError("A valid rail or market price is required for sizing.")
        notional = available * allocation / 100.0 * leverage
        quantity = quantize_step(str(notional / sizing_price), step)
        position_side = (
            "LONG" if hedge_mode and side == "BUY"
            else "SHORT" if hedge_mode
            else "BOTH"
        )

    quantity_value = safe_float(quantity)
    if not (minimum_qty <= quantity_value <= maximum_qty):
        raise ValueError(
            f"Resulting size must be between {minimum_qty:g} and {maximum_qty:g}."
        )
    if not reducing and rules.min_notional and quantity_value * rail_price < rules.min_notional:
        raise ValueError(
            f"Order value must be at least {rules.min_notional:g} {rules.quote_asset}."
        )

    order: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
        "positionSide": position_side,
    }
    if reducing and position_side == "BOTH":
        order["reduceOnly"] = True

    time_in_force = str(rail_state.get("timeInForce") or "GTC").upper()
    if time_in_force not in {"GTC", "GTX", "IOC", "FOK"}:
        time_in_force = "GTC"
    working_type = str(rail_state.get("workingType") or "CONTRACT_PRICE").upper()
    if working_type not in {"CONTRACT_PRICE", "MARK_PRICE"}:
        working_type = "CONTRACT_PRICE"
    price_protect = bool(rail_state.get("priceProtect", True))
    limit_offset = safe_float(rail_state.get("limitOffsetPercent"))

    if order_type == "LIMIT":
        order["price"] = quantize_step(str(rail_price), rules.tick_size)
        order["timeInForce"] = time_in_force
    elif order_type in {"STOP", "TAKE_PROFIT"}:
        order["triggerPrice"] = quantize_step(str(rail_price), rules.tick_size)
        limit_price = rail_price * (1.0 + limit_offset / 100.0)
        order["price"] = quantize_step(str(limit_price), rules.tick_size)
        order["timeInForce"] = time_in_force
        order["workingType"] = working_type
        order["priceProtect"] = price_protect
    elif order_type in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
        order["triggerPrice"] = quantize_step(str(rail_price), rules.tick_size)
        order["workingType"] = working_type
        order["priceProtect"] = price_protect
    else:
        callback = safe_float(rail_state.get("callbackRate"), 0.5)
        if not (0.1 <= callback <= 10.0):
            raise ValueError("Trailing callback rate must be between 0.1% and 10%.")
        order["activatePrice"] = quantize_step(str(rail_price), rules.tick_size)
        order["callbackRate"] = callback
        order["workingType"] = working_type

    return {
        "order": order,
        "protections": {"tp": [], "sl": []},
        "rules": rules,
        "position_intent": "REDUCE" if reducing else "OPEN",
        "collateral_asset": rules.margin_asset,
        "collateral_required": 0.0 if reducing else quantity_value * sizing_price / leverage,
        "requires_arm": False,
        "source": "magnetic_rail",
        "rail_draft_id": int(safe_float(rail_state.get("railDraftId"), 0)),
    }


def build_smart_exit_orders(
    position: dict[str, Any],
    candles: list[Candle],
    rules: SymbolRules,
    best_bid: float,
    best_ask: float,
    reserved_quantity: float = 0.0,
) -> list[dict[str, Any]]:
    """Build a passive exit ladder from nearby, very recent swing levels."""
    amount = safe_float(position.get("positionAmt"))
    position_side = str(position.get("positionSide") or "BOTH").upper()
    is_long = position_side == "LONG" or (position_side == "BOTH" and amount > 0)
    is_short = position_side == "SHORT" or (position_side == "BOTH" and amount < 0)
    if not (is_long or is_short):
        raise ValueError("No directional position is selected.")

    history = list(candles[-37:-1] if len(candles) > 1 else [])
    if len(history) < 7:
        raise ValueError("Not enough recent candles are loaded to locate an exit level.")
    reference = (best_ask if is_long else best_bid) or history[-1].close
    tick_decimal = Decimal(str(rules.tick_size))
    tick = float(tick_decimal)
    if reference <= 0 or tick <= 0:
        raise ValueError("A live order book and valid tick size are required.")

    true_ranges: list[float] = []
    window = history[-15:]
    start_index = len(history) - len(window)
    previous_close = (
        history[start_index - 1].close
        if start_index > 0
        else window[0].open
    )
    for candle in window:
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
        previous_close = candle.close
    atr = max(sum(true_ranges) / max(1, len(true_ranges)), tick)
    offset = max(tick, atr * 0.04)
    maximum_distance = max(atr * 3.2, reference * 0.006)
    candidates: list[tuple[float, int]] = []
    span = 2
    for index in range(span, len(history) - span):
        candle = history[index]
        local = history[index - span : index + span + 1]
        age = len(history) - 1 - index
        if is_long and candle.high >= max(item.high for item in local):
            if tick < candle.high - reference <= maximum_distance:
                candidates.append((candle.high - offset, age))
        elif is_short and candle.low <= min(item.low for item in local):
            if tick < reference - candle.low <= maximum_distance:
                candidates.append((candle.low + offset, age))
    for window in (6, 12, 24):
        if len(history) < window:
            continue
        level = (
            max(candle.high for candle in history[-window:]) - offset
            if is_long
            else min(candle.low for candle in history[-window:]) + offset
        )
        distance = level - reference if is_long else reference - level
        if tick < distance <= maximum_distance:
            candidates.append((level, window))
    candidates.sort(key=lambda item: (abs(item[0] - reference), item[1]))

    levels: list[str] = []
    minimum_separation = max(tick * 2.0, atr * 0.06)
    for raw_level, _age in candidates:
        if is_long:
            raw_level = max(raw_level, best_ask or reference)
        else:
            raw_level = min(raw_level, best_bid or reference)
        price = safe_float(quantize_step(str(raw_level), rules.tick_size))
        if is_long and best_bid > 0 and price <= best_bid:
            maker_floor = Decimal(str(best_bid)) + tick_decimal
            price = safe_float(quantize_step(str(maker_floor), rules.tick_size))
        if is_short and best_ask > 0 and price >= best_ask:
            maker_ceiling = Decimal(str(best_ask)) - tick_decimal
            price = safe_float(quantize_step(str(maker_ceiling), rules.tick_size))
        if price <= 0 or any(abs(price - safe_float(value)) < minimum_separation for value in levels):
            continue
        levels.append(quantize_step(str(price), rules.tick_size))
        if len(levels) == 3:
            break
    if not levels:
        raise ValueError(
            "No nearby recent swing high was found."
            if is_long
            else "No nearby recent swing low was found."
        )

    step = Decimal(str(rules.lot_step))
    remaining = max(0.0, abs(amount) - max(0.0, reserved_quantity))
    total = (Decimal(str(remaining)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    minimum = Decimal(str(max(0.0, rules.min_qty)))
    if total <= 0 or total < minimum:
        raise ValueError("No unreserved position quantity remains for a smart exit.")
    weights_by_count = {
        1: (Decimal("1"),),
        2: (Decimal("0.6"), Decimal("0.4")),
        3: (Decimal("0.5"), Decimal("0.3"), Decimal("0.2")),
    }
    quantities: list[Decimal] = []
    selected_levels: list[str] = []
    for count in range(len(levels), 0, -1):
        weights = weights_by_count[count]
        trial = [
            (total * weight / step).to_integral_value(rounding=ROUND_DOWN) * step
            for weight in weights[:-1]
        ]
        trial.append(total - sum(trial, Decimal("0")))
        if all(
            quantity >= minimum
            and (
                rules.min_notional <= 0
                or float(quantity) * safe_float(levels[index]) >= rules.min_notional
            )
            for index, quantity in enumerate(trial)
        ):
            quantities = trial
            selected_levels = levels[:count]
            break
    if not quantities:
        raise ValueError("The position is too small for the available exit levels.")

    side = "SELL" if is_long else "BUY"
    orders: list[dict[str, Any]] = []
    for price, quantity in zip(selected_levels, quantities):
        order: dict[str, Any] = {
            "symbol": str(position.get("symbol")),
            "side": side,
            "positionSide": position_side,
            "type": "LIMIT",
            "timeInForce": "GTX",
            "quantity": format(quantity.normalize(), "f"),
            "price": price,
        }
        if position_side == "BOTH":
            order["reduceOnly"] = True
        orders.append(order)
    return orders


import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from PySide6 import QtCore, QtWidgets

from ..models import DiagnosticsPort


_LOG = logging.getLogger(__name__)

def flag(value: Any) -> bool:
    return value is True or str(value).lower() == 'true'


class RailAmendments(QtCore.QObject):
    def __init__(self, owner, *, diagnostics: DiagnosticsPort | None = None):
        super().__init__(owner)
        self.owner = owner
        self._diagnostics = diagnostics
        self.gateway = owner.trading_gateway
        self.pending: dict[str, dict[str, Any]] = {}
        self.busy: set[tuple[str, str, str]] = set()
        self.gateway.request_succeeded.connect(self._succeeded)
        self.gateway.request_failed.connect(self._failed)

    @staticmethod
    def identity(order):
        algo = bool(order.get('algoId') or order.get('clientAlgoId') or order.get('_source') == 'ALGO')
        return (str(order.get('symbol')), 'ALGO' if algo else 'STANDARD',
                str(order.get('algoId') or order.get('orderId') or order.get('clientAlgoId') or order.get('clientOrderId') or ''))

    def _notice(self, message):
        self.owner.statusBar().showMessage(message, 12000)

    def _resolve_order_preview(self, order, accepted):
        chart = order.get('_railChart') if isinstance(order, dict) else None
        draft = int(order.get('_railDraftId') or 0) if isinstance(order, dict) else 0
        if chart is not None and draft > 0:
            try:
                chart.resolve_rail_amendment(draft, accepted=bool(accepted))
            except (AttributeError, RuntimeError) as exc:
                self._preview_diagnostic(draft, accepted, exc)

    def _resolve_item_preview(self, item, accepted):
        chart = item.get('chart')
        draft = int(item.get('draft') or 0)
        if chart is not None and draft > 0:
            try:
                chart.resolve_rail_amendment(draft, accepted=bool(accepted))
            except (AttributeError, RuntimeError) as exc:
                self._preview_diagnostic(draft, accepted, exc)

    def _preview_diagnostic(self, draft: int, accepted: bool, error: Exception) -> None:
        message = f"Preview resolution skipped: draft={draft} accepted={bool(accepted)} error={type(error).__name__}"
        _LOG.debug(message)
        if self._diagnostics is not None:
            self._diagnostics.warning("RAIL PREVIEW", message)

    def amend(self, order):
        order = dict(order)
        identity = self.identity(order)
        if identity in self.busy:
            self._resolve_order_preview(order, False)
            message = f"{identity[0]} · order {identity[2]} already has a pending amendment; duplicate not sent."
            self._notice(message)
            self.owner.alerts_panel.append_alert("RAIL AMENDMENT BLOCKED", message)
            return
        if not self.gateway.has_credentials():
            self._resolve_order_preview(order, False)
            self._notice('RAIL · API credentials required')
            return
        symbol = str(order.get('symbol') or '').upper()
        rules = self.owner.symbol_rules.get(symbol)
        if rules is None or not identity[2]:
            self._resolve_order_preview(order, False)
            self._notice('RAIL · exchange identity or symbol rules unavailable')
            return
        kind = str(order.get('type') or order.get('orderType') or '').upper()
        try:
            tick = Decimal(rules.tick_size)
            snapped = (Decimal(str(order['newPrice'])) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
            new_price = validate_step(format(snapped, 'f'), rules.tick_size, 'Price')
            if not rules.min_price <= float(new_price) <= rules.max_price:
                raise ValueError('Price is outside symbol limits')
            context = {'rules': rules, 'existing_order_replacement': True}
            if kind == 'LIMIT' and identity[1] == 'STANDARD':
                request = {'symbol': symbol, 'side': order['side'], 'price': new_price,
                           'quantity': validate_step(str(order.get('origQty') or order.get('quantity')), rules.lot_step, 'Quantity'),
                           '_minimumExecutedQty': str(order.get('executedQty') or order.get('cumQty') or '0')}
                if order.get('orderId'):
                    request['orderId'] = order['orderId']
                else:
                    request['origClientOrderId'] = order['clientOrderId']
                if flag(order.get('reduceOnly')):
                    request['reduceOnly'] = 'true'
                stage = 'modify'
            else:
                if kind not in {'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'TRAILING_STOP_MARKET'}:
                    raise ValueError('This order has no amendable working price')
                request = self._replacement(order, new_price, rules)
                request = self.gateway._validate_order_payload(request, context)
                self.gateway._validate_position_mode(request, False)
                stage = 'cancel'
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            self._resolve_order_preview(order, False)
            self._notice(f'RAIL AMEND REJECTED · {exc}')
            return
        if stage == 'cancel':
            detail = (f"{symbol} {order.get('side')} {kind}\n"
                      f"New {'activation' if kind == 'TRAILING_STOP_MARKET' else 'trigger'}: {new_price}\n"
                      f"Quantity: {request.get('quantity', 'Close position')}\n")
            if request.get('price'):
                detail += f"Limit price stays: {request['price']}\n"
            detail += ('\nCancel this order, then submit its replacement?\n'
                       'If replacement fails, the original will already be canceled.')
            answer = QtWidgets.QMessageBox.question(self.owner, 'Replace conditional order', detail,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                self._resolve_order_preview(order, False)
                return
        item = {'identity': identity, 'original': order, 'request': request, 'context': context,
                'stage': stage, 'chart': order.get('_railChart'), 'draft': int(order.get('_railDraftId') or 0)}
        old_client = str(order.get('clientAlgoId') or order.get('clientOrderId') or '')
        item['protection'] = {name: dict(getattr(self.owner, name, {}).get(old_client, {}))
                              for name in ('active_protection_legs', 'pending_protections')}
        item['old_client'] = old_client
        self.busy.add(identity)
        self._lock(item, True)
        if stage == 'modify':
            request_id = self.gateway.submit_modify(request, rules)
        else:
            built = self.owner._magnetic_rail_cancel_payload(symbol, order)
            if built is None:
                self._resolve_item_preview(item, False)
                self._finish(item, 'RAIL · missing cancel identity; original unchanged')
                return
            request_id = self.gateway.submit_cancel(*built)
        self._track(request_id, item)

    def _replacement(self, order, new_price, rules):
        kind = str(order.get('type') or order.get('orderType')).upper()
        result = {key: order[key] for key in ('symbol', 'side', 'positionSide', 'workingType',
                  'priceProtect', 'selfTradePreventionMode', 'goodTillDate') if key in order}
        if str(order.get('timeInForce')) != 'GTD':
            result.pop('goodTillDate', None)
        result['type'] = kind
        if flag(order.get('closePosition')):
            result['closePosition'] = True
        else:
            remaining = Decimal(str(order.get('origQty') or order.get('quantity'))) - Decimal(str(order.get('executedQty') or '0'))
            if remaining <= 0:
                raise ValueError('No remaining quantity')
            result['quantity'] = validate_step(str(remaining), rules.lot_step, 'Quantity')
            if flag(order.get('reduceOnly')):
                result['reduceOnly'] = True
        if kind in {'STOP', 'TAKE_PROFIT'}:
            result['timeInForce'] = order.get('timeInForce', 'GTC')
            price_match = order.get('priceMatch')
            if price_match and price_match != 'NONE':
                result['priceMatch'] = price_match
            else:
                result['price'] = validate_step(str(order.get('price')), rules.tick_size, 'Limit price')
        if kind == 'TRAILING_STOP_MARKET':
            result['activatePrice'] = new_price
            result['callbackRate'] = order.get('callbackRate') or order.get('priceRate')
        else:
            result['triggerPrice'] = new_price
        result['newClientOrderId'] = self.gateway.client_order_id('nwr')
        return result

    def _lock(self, item, enabled):
        chart = item.get('chart')
        if chart is not None:
            try:
                chart.set_rail_amend_pending(item['draft'], enabled)
            except RuntimeError:
                pass

    def _track(self, request_id, item):
        if self.gateway.request_was_admitted(request_id):
            self.pending[request_id] = item
            self._notice(f"RAIL · {item['stage'].upper()} PENDING · {item['identity'][0]}")
        else:
            message = ('REPLACEMENT NOT SENT · original canceled; no replacement active'
                       if item['stage'] == 'replace' else 'AMENDMENT NOT SENT · refresh order status')
            self._resolve_item_preview(item, False)
            self._finish(item, message)

    def _succeeded(self, request_id, result):
        item = self.pending.pop(request_id, None)
        if item is None:
            return
        if item['stage'] == 'cancel':
            payload = result if isinstance(result, dict) else {}
            status = str(payload.get('status') or payload.get('algoStatus') or '').upper()

            algo_cancelled = (item['identity'][1] == 'ALGO' and str(payload.get('code')) == '200'
                              and str(payload.get('algoId') or payload.get('clientAlgoId') or '') == item['identity'][2])
            if status not in {'CANCELED', 'CANCELLED'} and not algo_cancelled:
                self._finish(item, 'REPLACEMENT STOPPED · cancellation not confirmed; refresh order status')
                return
            item['stage'] = 'replace'
            request_id = self.gateway.submit_order(item['request'], item['context'])
            self._track(request_id, item)
            return
        if item['stage'] == 'replace':
            payload = result if isinstance(result, dict) else {}
            new_client = str(payload.get('clientAlgoId') or payload.get('clientOrderId') or item['request']['newClientOrderId'])
            status = str(payload.get('status') or payload.get('algoStatus') or 'NEW').upper()
            if status in {'NEW', 'PARTIALLY_FILLED', 'PENDING', 'ACCEPTED'}:
                for name, plan in item.get('protection', {}).items():
                    if plan:
                        mapping = getattr(self.owner, name)
                        mapping.pop(item['old_client'], None)
                        mapping[new_client] = plan
        self._resolve_item_preview(item, True)
        self._finish(item, 'RAIL · exchange confirmed amendment; refreshing order state')

    def _failed(self, request_id, message, uncertain):
        item = self.pending.pop(request_id, None)
        if item is None:
            return
        if item['stage'] == 'replace':
            detail = ('Original canceled; replacement outcome UNKNOWN. Reconcile before retrying.' if uncertain
                      else 'Original canceled; replacement FAILED. No replacement is active.')
        else:
            detail = 'Outcome UNKNOWN; refreshing exchange state. No replacement sent.' if uncertain else 'Request rejected; refreshing exchange state.'
        if uncertain:
            self.pending[request_id] = item
        else:
            self._resolve_item_preview(item, False)
        self._finish(item, f'{detail} {message}', release=not uncertain)
        if item['stage'] == 'replace':
            QtWidgets.QMessageBox.warning(self.owner, 'Order replacement failed', f'{detail}\n{message}')

    def _finish(self, item, message, *, release=True):
        if release:
            self.busy.discard(item['identity'])
            self._lock(item, False)
        self._notice(message)
        self.gateway.refresh_account(None, all_open_orders=True)


def edit_active_rail_settings(owner):
    dialog = QtWidgets.QDialog(owner)
    dialog.setWindowTitle('Active order rails')
    layout = QtWidgets.QFormLayout(dialog)
    cfg = owner.magnetic_rail_config
    style = QtWidgets.QComboBox()
    style.addItems(['solid', 'dash', 'dot'])
    style.setCurrentText(cfg['active_line_style'])
    animation = QtWidgets.QComboBox()
    animation.addItems(['packets', 'pulse', 'scan', 'off'])
    animation.setCurrentText(cfg['active_animation'])
    opacity = QtWidgets.QSpinBox()
    opacity.setRange(10, 45)
    opacity.setSuffix('%')
    opacity.setValue(cfg['active_opacity'])
    width = QtWidgets.QDoubleSpinBox()
    width.setRange(0.5, 1.2)
    width.setSingleStep(0.1)
    width.setValue(cfg['active_width'])
    for label, widget in [('Line style', style), ('Animation', animation), ('Opacity', opacity), ('Width', width)]:
        layout.addRow(label, widget)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        owner.set_magnetic_rail_config({**cfg, 'active_line_style': style.currentText(),
            'active_animation': animation.currentText(), 'active_opacity': opacity.value(), 'active_width': width.value()})

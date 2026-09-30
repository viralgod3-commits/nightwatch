#!/usr/bin/env python3
"""Patch trading_ui.py to fix bugs, edge cases, and layout issues."""

import re
import sys
from pathlib import Path

TARGET = Path("trading_ui.py")


def read_source() -> str:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found in current directory.")
        sys.exit(1)
    return TARGET.read_text(encoding="utf-8")


def apply_patches(source: str) -> str:
    patches_applied = 0


    old = '''        roe_text = f" · {pnl / margin * 100:+.1f}% ROE" if margin > 0 else ""
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl)}{roe_text}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(
            self.entry_value_label, format_price(entry) if entry > 0 else "—"
        )
        _set_text_if_changed(
            self.mark_value_label, format_price(mark) if mark > 0 else "—"
        )
        _set_text_if_changed(self.size_value_label, human_number(amount))
        _set_text_if_changed(
            self.notional_value_label,
            human_number(_position_notional(self.payload), money=True),
        )
        liquidation = safe_float(self.payload.get("liquidationPrice"))
        liquidation_text = "—"
        distance_text = "—"
        risk_state = "normal"
        direction = _position_direction(self.payload)
        if liquidation > 0 and mark > 0 and direction in {"LONG", "SHORT"}:
            distance = (
                (mark - liquidation) / mark * 100.0
                if direction == "LONG"
                else (liquidation - mark) / mark * 100.0
            )
            distance = max(0.0, distance)'''
    new = '''        roe_text = f" · {pnl / margin * 100:+.1f}% ROE" if margin > 1e-12 else ""
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl)}{roe_text}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(
            self.entry_value_label, format_price(entry) if entry > 0 else "—"
        )
        _set_text_if_changed(
            self.mark_value_label, format_price(mark) if mark > 0 else "—"
        )
        _set_text_if_changed(self.size_value_label, human_number(amount))
        _set_text_if_changed(
            self.notional_value_label,
            human_number(_position_notional(self.payload), money=True),
        )
        liquidation = safe_float(self.payload.get("liquidationPrice"))
        liquidation_text = "—"
        distance_text = "—"
        risk_state = "normal"
        direction = _position_direction(self.payload)
        if liquidation > 0 and mark > 1e-12 and direction in {"LONG", "SHORT"}:
            distance = (
                (mark - liquidation) / mark * 100.0
                if direction == "LONG"
                else (liquidation - mark) / mark * 100.0
            )
            distance = max(0.0, distance)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        roe_text = f" · {pnl / margin * 100:+.1f}%" if margin > 0 else ""
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl)}{roe_text}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(
            self.metric_labels["ENTRY"], format_price(entry) if entry > 0 else "—"
        )
        _set_text_if_changed(
            self.metric_labels["MARK"], format_price(mark) if mark > 0 else "—"
        )
        liquidation = safe_float(self.payload.get("liquidationPrice"))
        liquidation_text = "—"
        distance_text = "—"
        risk_state = "normal"
        direction = _position_direction(self.payload)
        if liquidation > 0 and mark > 0 and direction in {"LONG", "SHORT"}:
            distance = (
                (mark - liquidation) / mark * 100.0
                if direction == "LONG"
                else (liquidation - mark) / mark * 100.0
            )
            distance = max(0.0, distance)'''
    new = '''        roe_text = f" · {pnl / margin * 100:+.1f}%" if margin > 1e-12 else ""
        _set_text_if_changed(self.pnl_label, f"{_signed_money(pnl)}{roe_text}")
        _set_repolished_property(self.pnl_label, "pnl", _pnl_state(pnl))
        _set_text_if_changed(
            self.metric_labels["ENTRY"], format_price(entry) if entry > 0 else "—"
        )
        _set_text_if_changed(
            self.metric_labels["MARK"], format_price(mark) if mark > 0 else "—"
        )
        liquidation = safe_float(self.payload.get("liquidationPrice"))
        liquidation_text = "—"
        distance_text = "—"
        risk_state = "normal"
        direction = _position_direction(self.payload)
        if liquidation > 0 and mark > 1e-12 and direction in {"LONG", "SHORT"}:
            distance = (
                (mark - liquidation) / mark * 100.0
                if direction == "LONG"
                else (liquidation - mark) / mark * 100.0
            )
            distance = max(0.0, distance)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        for button in self.quick_leverage_buttons:
            blocker = QtCore.QSignalBlocker(button)
            button.setChecked(
                int(button.property("leverageValue")) == self.leverage.value()
            )
            del blocker'''
    new = '''        for button in self.quick_leverage_buttons:
            blocker = QtCore.QSignalBlocker(button)
            prop_value = button.property("leverageValue")
            button.setChecked(
                int(prop_value) == self.leverage.value() if prop_value is not None else False
            )
            del blocker'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset).clicked.connect(self._reset)
        layout.addWidget(buttons)'''
    new = '''        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        reset_btn = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset)
        if reset_btn is not None:
            reset_btn.clicked.connect(self._reset)
        layout.addWidget(buttons)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _commit_custom_text(self) -> None:
        editor = self._custom_editor
        if editor is None or not editor.isVisible():
            return
        raw = editor.text().replace("×", "").replace("x", "").strip()
        editor.hide()
        try:
            value = int(raw)
        except ValueError:
            value = self._value
        self.setValue(max(self.minimum(), min(self.maximum(), value)))'''
    new = '''    def _commit_custom_text(self) -> None:
        editor = self._custom_editor
        if editor is None or not editor.isVisible():
            return
        raw = editor.text().replace("×", "").replace("x", "").strip()
        editor.hide()
        if not raw:
            self._show_value(self._value)
            return
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = self._value
        self.setValue(max(self.minimum(), min(self.maximum(), value)))'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        card_height = max(
            int(card.minimumHeight()),
            int(card.minimumSizeHint().height()),
            int(card.sizeHint().height()),
        )'''
    new = '''        card_height = max(
            int(card.minimumSize().height()),
            int(card.minimumSizeHint().height()),
            int(card.sizeHint().height()),
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    if getattr(view, "_cards_fingerprint", None) == structure:
        if payloads and update_existing is not None and view.count() == len(payloads):
            for index, payload in enumerate(payloads):
                item = view.item(index)
                item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                card = view.itemWidget(item)
                update_existing(card, dict(payload))
        return False'''
    new = '''    if getattr(view, "_cards_fingerprint", None) == structure:
        if payloads and update_existing is not None and view.count() == len(payloads):
            for index, payload in enumerate(payloads):
                item = view.item(index)
                if item is None:
                    continue
                item.setData(Qt.ItemDataRole.UserRole, dict(payload))
                card = view.itemWidget(item)
                if card is not None:
                    update_existing(card, dict(payload))
        return False'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        for index in range(self.positions.count()):
            card = self.positions.itemWidget(self.positions.item(index))
            if (
                isinstance(card, PositionAccountCard)
                and str(card.payload.get("symbol") or "").upper() == symbol
            ):
                card.update_mark_price(price)'''
    new = '''        for index in range(self.positions.count()):
            item = self.positions.item(index)
            if item is None:
                continue
            card = self.positions.itemWidget(item)
            if (
                isinstance(card, PositionAccountCard)
                and str(card.payload.get("symbol") or "").upper() == symbol
            ):
                card.update_mark_price(price)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        for index in range(self.list.count()):
            card = self.list.itemWidget(self.list.item(index))
            if isinstance(card, PositionAccountCard):
                total_pnl += card.update_mark_price(price)
                count += 1'''
    new = '''        for index in range(self.list.count()):
            item = self.list.item(index)
            if item is None:
                continue
            card = self.list.itemWidget(item)
            if isinstance(card, PositionAccountCard):
                total_pnl += card.update_mark_price(price)
                count += 1'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        for index in range(self.list.count()):
            card = self.list.itemWidget(self.list.item(index))
            if isinstance(card, PositionAccountCard):
                card.set_close_percentages(self.close_percentages)'''
    new = '''        for index in range(self.list.count()):
            item = self.list.item(index)
            if item is None:
                continue
            card = self.list.itemWidget(item)
            if isinstance(card, PositionAccountCard):
                card.set_close_percentages(self.close_percentages)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        for index in range(self.positions.count()):
            card = self.positions.itemWidget(self.positions.item(index))
            if isinstance(card, PositionAccountCard):
                card.set_close_percentages(self.close_percentages)'''
    new = '''        for index in range(self.positions.count()):
            item = self.positions.item(index)
            if item is None:
                continue
            card = self.positions.itemWidget(item)
            if isinstance(card, PositionAccountCard):
                card.set_close_percentages(self.close_percentages)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def set_mark_price(self, price, symbol=None):
        symbol = symbol or self.symbol
        for index in range(self.positions.count()):
            card = self.positions.itemWidget(self.positions.item(index))
            if isinstance(card, PositionAccountCard) and card.payload.get("symbol") == symbol:
                card.update_mark_price(price)'''
    new = '''    def set_mark_price(self, price, symbol=None):
        symbol = symbol or self.symbol
        for index in range(self.positions.count()):
            item = self.positions.item(index)
            if item is None:
                continue
            card = self.positions.itemWidget(item)
            if isinstance(card, PositionAccountCard) and card.payload.get("symbol") == symbol:
                card.update_mark_price(price)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _account_fill_rows(snapshot, symbol):
    result = []
    for row in sorted(snapshot.get("fills", []), key=lambda r: safe_float(r.get("time")), reverse=True):
        if str(row.get("symbol", "")) != symbol:
            continue
        timestamp = safe_float(row.get("time")) / 1000.0
        stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d %b %H:%M:%S") if timestamp > 0 else "—"'''
    new = '''def _account_fill_rows(snapshot, symbol):
    result = []
    for row in sorted(snapshot.get("fills", []), key=lambda r: safe_float(r.get("time")), reverse=True):
        if str(row.get("symbol", "")) != symbol:
            continue
        raw_time = safe_float(row.get("time"))
        timestamp = raw_time / 1000.0 if raw_time > 0 else 0.0
        try:
            stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d %b %H:%M:%S") if timestamp > 0 else "—"
        except (OSError, OverflowError, ValueError):
            stamp = "—"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _set_repolished_property(widget: QtWidgets.QWidget, name: str, value: Any) -> bool:
    if widget.property(name) == value:
        return False
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    return True'''
    new = '''def _set_repolished_property(widget: QtWidgets.QWidget, name: str, value: Any) -> bool:
    if widget.property(name) == value:
        return False
    widget.setProperty(name, value)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    return True'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        persistent = {"SENDING", "OUTCOME UNKNOWN"}
        if self._submission_state not in {"READY", "IDLE", *persistent}:
            delay = 3500
            def clear_if_current() -> None:
                if self._submission_generation != generation:
                    return
                self._submission_state = "READY"
                self._submission_detail = ""
                self._submission_request_id = ""
                self._submission_generation += 1
                self._update_execution_state()
                self._update_submit_text()
            QTimer.singleShot(delay, clear_if_current)'''
    new = '''        persistent = {"SENDING", "OUTCOME UNKNOWN"}
        if self._submission_state not in {"READY", "IDLE", *persistent}:
            delay = 3500
            def clear_if_current() -> None:
                try:
                    if self._submission_generation != generation:
                        return
                    self._submission_state = "READY"
                    self._submission_detail = ""
                    self._submission_request_id = ""
                    self._submission_generation += 1
                    self._update_execution_state()
                    self._update_submit_text()
                except RuntimeError:
                    pass  # Widget was deleted before timer fired.
            QTimer.singleShot(delay, clear_if_current)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _selection_changed(self, index: int) -> None:
        if index < 0:
            return
        value = int(safe_float(self.itemData(index), self._value))
        self._popup_restore_value = None
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)'''
    new = '''    def _selection_changed(self, index: int) -> None:
        if index < 0:
            return
        data = self.itemData(index)
        if data is None:
            return
        try:
            value = int(safe_float(data, self._value))
        except (TypeError, ValueError):
            return
        self._popup_restore_value = None
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _update_position_mark(payload: dict[str, Any], mark: float) -> float:
    """Update USD-M linear position values from a live mark-price tick."""
    mark = safe_float(mark)
    amount = abs(safe_float(payload.get("positionAmt")))
    entry = safe_float(payload.get("entryPrice"))
    direction = _position_direction(payload)
    if mark <= 0 or amount <= 0:
        return safe_float(payload.get("unrealizedProfit"))
    signed_amount = -amount if direction == "SHORT" else amount
    pnl = (mark - entry) * signed_amount if entry > 0 else 0.0
    payload["markPrice"] = mark
    payload["unrealizedProfit"] = pnl
    payload["notional"] = signed_amount * mark
    return pnl'''
    new = '''def _update_position_mark(payload: dict[str, Any], mark: float) -> float:
    """Update USD-M linear position values from a live mark-price tick."""
    mark = safe_float(mark)
    amount = abs(safe_float(payload.get("positionAmt")))
    entry = safe_float(payload.get("entryPrice"))
    direction = _position_direction(payload)
    if mark <= 0 or amount <= 0:
        return safe_float(payload.get("unrealizedProfit"))
    signed_amount = -amount if direction == "SHORT" else amount
    pnl = (mark - entry) * signed_amount if entry > 0 else 0.0
    payload["markPrice"] = mark
    payload["unrealizedProfit"] = pnl
    payload["notional"] = abs(signed_amount) * mark
    return pnl'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _position_notional(payload: dict[str, Any]) -> float:
    explicit = abs(safe_float(payload.get("notional")))
    if explicit > 0:
        return explicit
    amount = abs(safe_float(payload.get("positionAmt")))
    mark = safe_float(payload.get("markPrice"))
    return amount * mark if amount > 0 and mark > 0 else 0.0'''
    new = '''def _position_notional(payload: dict[str, Any]) -> float:
    explicit = abs(safe_float(payload.get("notional")))
    if explicit > 0:
        return explicit
    amount = abs(safe_float(payload.get("positionAmt")))
    mark = safe_float(payload.get("markPrice"))
    if amount > 0 and mark > 0:
        return amount * mark
    entry = safe_float(payload.get("entryPrice"))
    if amount > 0 and entry > 0:
        return amount * entry
    return 0.0'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        market_quantity = order_type in {
            "MARKET", "STOP_MARKET", "TRAILING_STOP_MARKET"
        }
        step = (
            self.rules.market_step
            if market_quantity
            else self.rules.lot_step
        )
        quantity = (
            validate_step(
                self.quantity_edit.text().replace(",", "").strip(),
                step,
                "Quantity",
            )
            if mode == "CONTRACTS"
            else quantize_step(str(quantity_value), step)
        )'''
    new = '''        market_quantity = order_type in {
            "MARKET", "STOP_MARKET", "TRAILING_STOP_MARKET"
        }
        step = (
            self.rules.market_step
            if market_quantity
            else self.rules.lot_step
        )
        if step <= 0:
            raise ValueError("Exchange lot step is not available for this symbol.")
        try:
            quantity = (
                validate_step(
                    self.quantity_edit.text().replace(",", "").strip(),
                    step,
                    "Quantity",
                )
                if mode == "CONTRACTS"
                else quantize_step(str(quantity_value), step)
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Quantity rounding failed: {exc}") from exc'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _validated_price(self, value: str, label: str) -> str:
        price = validate_step(value, self.rules.tick_size, label)'''
    new = '''    def _validated_price(self, value: str, label: str) -> str:
        if self.rules.tick_size <= 0:
            raise ValueError(f"Exchange tick size is not available for {label.lower()} validation.")
        price = validate_step(value, self.rules.tick_size, label)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _copy_mark_to(self, target: QtWidgets.QLineEdit) -> None:
        if not self._mark_is_fresh():
            self._set_validation_message(
                "MARK PRICE UNAVAILABLE" if self.mark_price <= 0 else "MARK PRICE STALE"
            )
            return
        try:
            text = quantize_step(str(self.mark_price), self.rules.tick_size)
        except ValueError:
            text = format_price(self.mark_price).replace(",", "")
        target.setText(text)
        self._set_validation_message("")
        self._update_order_summary()'''
    new = '''    def _copy_mark_to(self, target: QtWidgets.QLineEdit) -> None:
        if not self._mark_is_fresh():
            self._set_validation_message(
                "MARK PRICE UNAVAILABLE" if self.mark_price <= 0 else "MARK PRICE STALE"
            )
            return
        try:
            text = quantize_step(str(self.mark_price), self.rules.tick_size)
        except (ValueError, TypeError):
            text = format_price(self.mark_price).replace(",", "")
        target.setText(text)
        self._set_validation_message("")
        self._update_order_summary()'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        try:
            text = quantize_step(str(float(price)), self.rules.tick_size)
        except (ValueError, TypeError):
            text = format_price(price).replace(",", "")'''
    new = '''        try:
            text = quantize_step(str(float(price)), self.rules.tick_size)
        except (ValueError, TypeError, OverflowError):
            text = format_price(price).replace(",", "")'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''            if hasattr(card, "selected_requested"):
                card.selected_requested.connect(lambda item=item: view.setCurrentItem(item))'''
    new = '''            if hasattr(card, "selected_requested"):
                card.selected_requested.connect(
                    lambda _checked=False, _item=item: view.setCurrentItem(_item)
                )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        self.cancel_all.setEnabled(self.gateway.has_credentials() and any(r.get("symbol") == self.symbol for r in self._open_orders))'''
    new = '''        self.cancel_all.setEnabled(
            self.gateway.has_credentials()
            and any(
                isinstance(r, dict) and r.get("symbol") == self.symbol
                for r in self._open_orders
            )
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        except (ValueError, TypeError) as exc:
            preview_error = str(exc)
        block = self._manual_market_block_reason(order_type, reducing)'''
    new = '''        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            preview_error = str(exc)
        block = self._manual_market_block_reason(order_type, reducing)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        except ValueError as exc:
            self._set_validation_message(str(exc))
            self._update_execution_state()
            return
        self._set_validation_message("")
        self.order_requested.emit('''
    new = '''        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            self._set_validation_message(str(exc))
            self._update_execution_state()
            return
        self._set_validation_message("")
        self.order_requested.emit('''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        percent = max(1, min(100, int(percent)))
        quantity = quantize_step(
            str(abs(amount) * percent / 100.0), rules.market_step
        )
        quantity_value = safe_float(quantity)
        if not (rules.min_market_qty <= quantity_value <= rules.max_market_qty):
            self.status.setText("CLOSE SIZE IS OUTSIDE THE EXCHANGE MARKET-ORDER RANGE")
            return
        mark = safe_float(position.get("markPrice"))
        if rules.min_notional and mark > 0 and quantity_value * mark < rules.min_notional:
            self.status.setText("CLOSE VALUE IS BELOW THE EXCHANGE MINIMUM")
            return
        position_side = str(position.get("positionSide", "BOTH"))
        order: dict[str, Any] = {
            "symbol": position.get("symbol"),
            "side": _position_close_side(position),
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side,
        }
        if position_side == "BOTH":
            order["reduceOnly"] = True
        self.order_requested.emit(
            {
                "order": order,
                "protections": {},
                "rules": rules,
                "position_intent": "REDUCE",
            }
        )'''
    new = '''        percent = max(1, min(100, int(percent)))
        if rules.market_step <= 0:
            self.status.setText("EXCHANGE MARKET LOT STEP UNAVAILABLE")
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), rules.market_step
            )
        except (ValueError, TypeError) as exc:
            self.status.setText(f"CLOSE SIZE ERROR: {exc}")
            return
        quantity_value = safe_float(quantity)
        if quantity_value <= 0:
            self.status.setText("CLOSE SIZE ROUNDED TO ZERO")
            return
        if not (rules.min_market_qty <= quantity_value <= rules.max_market_qty):
            self.status.setText("CLOSE SIZE IS OUTSIDE THE EXCHANGE MARKET-ORDER RANGE")
            return
        mark = safe_float(position.get("markPrice"))
        if rules.min_notional and mark > 0 and quantity_value * mark < rules.min_notional:
            self.status.setText("CLOSE VALUE IS BELOW THE EXCHANGE MINIMUM")
            return
        position_side = str(position.get("positionSide", "BOTH"))
        order: dict[str, Any] = {
            "symbol": position.get("symbol"),
            "side": _position_close_side(position),
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side,
        }
        if position_side == "BOTH":
            order["reduceOnly"] = True
        self.order_requested.emit(
            {
                "order": order,
                "protections": {},
                "rules": rules,
                "position_intent": "REDUCE",
            }
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        try:
            quantity = quantize_step(
                str(abs(amount) * max(1, min(100, int(percent))) / 100.0),
                self.rules.market_step,
            )
        except ValueError as exc:
            self.status.setText(str(exc))
            return'''
    new = '''        if self.rules.market_step <= 0:
            self.status.setText("EXCHANGE MARKET LOT STEP UNAVAILABLE")
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * max(1, min(100, int(percent))) / 100.0),
                self.rules.market_step,
            )
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _exchange_step_text(value: Any) -> str:
    """Render Binance decimal step strings without changing their numeric contract."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return "—"'''
    new = '''def _exchange_step_text(value: Any) -> str:
    """Render Binance decimal step strings without changing their numeric contract."""
    if value is None:
        return "—"
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, ArithmeticError):
        return "—"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _signed_money(value: float) -> str:
    value = safe_float(value)
    if abs(value) < 1e-12:
        return human_number(0.0, money=True)
    sign = "+" if value > 0 else "-"
    return f"{sign}{human_number(abs(value), money=True)}"'''
    new = '''def _signed_money(value: float) -> str:
    value = safe_float(value)
    if not (value == value) or value != value:  # NaN check
        return human_number(0.0, money=True)
    if abs(value) < 1e-12:
        return human_number(0.0, money=True)
    sign = "+" if value > 0 else "-"
    return f"{sign}{human_number(abs(value), money=True)}"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _pnl_state(value: float) -> str:
    value = safe_float(value)
    return "positive" if value > 1e-12 else "negative" if value < -1e-12 else "flat"'''
    new = '''def _pnl_state(value: float) -> str:
    value = safe_float(value)
    if value != value:  # NaN
        return "flat"
    return "positive" if value > 1e-12 else "negative" if value < -1e-12 else "flat"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _refresh_account_summary(self) -> None:
        margin_asset = str(getattr(self.rules, "margin_asset", "") or "USDT").strip() or "USDT"'''
    new = '''    def _refresh_account_summary(self) -> None:
        try:
            margin_asset = str(getattr(self.rules, "margin_asset", "") or "USDT").strip() or "USDT"
        except (AttributeError, TypeError):
            margin_asset = "USDT"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _refresh_total_pnl(self) -> None:
        total_pnl = sum(
            safe_float(row.get("unrealizedProfit"))
            for row in self._position_payloads
        )'''
    new = '''    def _refresh_total_pnl(self) -> None:
        total_pnl = sum(
            safe_float(row.get("unrealizedProfit"))
            for row in self._position_payloads
            if isinstance(row, dict)
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        self._position_cache = []
        active_leverage = 0
        for row in account.get("positions", []):
            if str(row.get("symbol")) != self.symbol:
                continue
            if abs(safe_float(row.get("positionAmt"))) > 0:
                self._position_cache.append(dict(row))
            active_leverage = max(active_leverage, int(safe_float(row.get("leverage"))))'''
    new = '''        self._position_cache = []
        active_leverage = 0
        for row in account.get("positions", []):
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol")) != self.symbol:
                continue
            if abs(safe_float(row.get("positionAmt"))) > 0:
                self._position_cache.append(dict(row))
            try:
                active_leverage = max(active_leverage, int(safe_float(row.get("leverage"))))
            except (ValueError, TypeError):
                pass'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _reduced_position_size(self) -> float:
        direction = "SHORT" if self.buy_button.isChecked() else "LONG"
        total = 0.0
        for row in self._position_cache:
            amount = safe_float(row.get("positionAmt"))
            position_side = str(row.get("positionSide") or "BOTH")'''
    new = '''    def _reduced_position_size(self) -> float:
        direction = "SHORT" if self.buy_button.isChecked() else "LONG"
        total = 0.0
        for row in self._position_cache:
            if not isinstance(row, dict):
                continue
            amount = safe_float(row.get("positionAmt"))
            position_side = str(row.get("positionSide") or "BOTH")'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        selection = None
        for payload in payloads:
            item = QtWidgets.QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dict(payload))
            card = factory(dict(payload))
            item.setSizeHint(QtCore.QSize(0, card.sizeHint().height() + 2))'''
    new = '''        selection = None
        for payload in payloads:
            item = QtWidgets.QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dict(payload))
            card = factory(dict(payload))
            if card is None:
                continue
            item.setSizeHint(QtCore.QSize(0, card.sizeHint().height() + 2))'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        blocker = QtCore.QSignalBlocker(self.type_combo)
        self.type_combo.clear()
        for label, value in values:
            self.type_combo.addItem(label, value)
        index = self.type_combo.findData(current_type)
        self.type_combo.setCurrentIndex(max(0, index))
        del blocker'''
    new = '''        blocker = QtCore.QSignalBlocker(self.type_combo)
        self.type_combo.clear()
        for label, value in values:
            self.type_combo.addItem(label, value)
        index = self.type_combo.findData(current_type)
        self.type_combo.setCurrentIndex(index if index >= 0 else 0)
        del blocker'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        blocker = QtCore.QSignalBlocker(self.size_mode)
        self.size_mode.clear()
        for label, value in size_values:
            self.size_mode.addItem(label, value)
        index = self.size_mode.findData(current_mode)
        self.size_mode.setCurrentIndex(max(0, index))
        del blocker'''
    new = '''        blocker = QtCore.QSignalBlocker(self.size_mode)
        self.size_mode.clear()
        for label, value in size_values:
            self.size_mode.addItem(label, value)
        index = self.size_mode.findData(current_mode)
        self.size_mode.setCurrentIndex(index if index >= 0 else 0)
        del blocker'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def set_close_percentages(self, values: list[int]) -> None:
        self.close_percentages = list(values)
        current = self.close_size.currentData()
        blocker = QtCore.QSignalBlocker(self.close_size)
        self.close_size.clear()
        for percent in self.close_percentages:
            self.close_size.addItem(f"{percent}%", int(percent))
        index = self.close_size.findData(current)
        self.close_size.setCurrentIndex(index if index >= 0 else self.close_size.count() - 1)
        del blocker'''
    new = '''    def set_close_percentages(self, values: list[int]) -> None:
        self.close_percentages = list(values) if values else [100]
        current = self.close_size.currentData()
        blocker = QtCore.QSignalBlocker(self.close_size)
        self.close_size.clear()
        for percent in self.close_percentages:
            self.close_size.addItem(f"{percent}%", int(percent))
        index = self.close_size.findData(current)
        if index >= 0:
            self.close_size.setCurrentIndex(index)
        elif self.close_size.count() > 0:
            self.close_size.setCurrentIndex(self.close_size.count() - 1)
        del blocker'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def close_percent(self) -> int:
        return max(1, min(100, int(safe_float(self.close_size.currentData(), 100))))'''
    new = '''    def close_percent(self) -> int:
        data = self.close_size.currentData()
        if data is None:
            return 100
        return max(1, min(100, int(safe_float(data, 100))))'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        position_payloads.sort(
            key=lambda row: (
                0 if str(row.get("symbol") or "") == self.symbol else 1,
                -_position_notional(row),
                -abs(safe_float(row.get("unrealizedProfit"))),
            )
        )'''
    new = '''        position_payloads.sort(
            key=lambda row: (
                0 if str(row.get("symbol") or "") == self.symbol else 1,
                -_position_notional(row) if isinstance(row, dict) else 0.0,
                -abs(safe_float(row.get("unrealizedProfit"))) if isinstance(row, dict) else 0.0,
            )
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _leverage_state_text(self) -> str:
        requested = int(self.leverage.value())
        confirmed = int(self.gateway.current_leverage(self.symbol))'''
    new = '''    def _leverage_state_text(self) -> str:
        requested = int(self.leverage.value())
        try:
            confirmed = int(self.gateway.current_leverage(self.symbol) or 0)
        except (TypeError, ValueError, AttributeError):
            confirmed = 0'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        if not self.gateway.has_credentials():
            self.gateway.problem.emit("Add Binance API credentials before changing leverage.")
            return
        current = self.gateway.current_leverage(self.symbol)
        if current == leverage and self.symbol in self.gateway.cross_ready:'''
    new = '''        if not self.gateway.has_credentials():
            self.gateway.problem.emit("Add Binance API credentials before changing leverage.")
            return
        try:
            current = self.gateway.current_leverage(self.symbol) or 0
        except (TypeError, AttributeError):
            current = 0
        if current == leverage and self.symbol in self.gateway.cross_ready:'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        try:
            confirmed_leverage = self.gateway.current_leverage(self.symbol)
            self._sync_auto_position_side()'''
    new = '''        try:
            confirmed_leverage = self.gateway.current_leverage(self.symbol) or 0
            self._sync_auto_position_side()'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''            balance = self.gateway.available_balance(self.rules.margin_asset)
            if balance <= 0:'''
    new = '''            balance = safe_float(self.gateway.available_balance(self.rules.margin_asset))
            if balance <= 0:'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''                leverage = self.gateway.current_leverage(self.symbol)
                if leverage <= 0:'''
    new = '''                leverage = safe_float(self.gateway.current_leverage(self.symbol))
                if leverage <= 0:'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''def _order_card_render_fingerprint(payloads, current_symbol):
    return (
        str(current_symbol),
        tuple(
            ('''
    new = '''def _order_card_render_fingerprint(payloads, current_symbol):
    return (
        str(current_symbol),
        tuple(
            ('''



    old = '''    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty.setGeometry(self.viewport().rect().adjusted(8, 8, -8, -8))'''
    new = '''    def resizeEvent(self, event):
        super().resizeEvent(event)
        vp = self.viewport()
        if vp is not None:
            self.empty.setGeometry(vp.rect().adjusted(8, 8, -8, -8))'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def setRowCount(self, count):
        super().setRowCount(count)
        self.empty.setVisible(count == 0)
        self.empty.setGeometry(self.viewport().rect().adjusted(8, 8, -8, -8))'''
    new = '''    def setRowCount(self, count):
        super().setRowCount(count)
        self.empty.setVisible(count == 0)
        vp = self.viewport()
        if vp is not None:
            self.empty.setGeometry(vp.rect().adjusted(8, 8, -8, -8))'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _begin_custom_entry(self, seed: str) -> None:
        super().hidePopup()
        self._popup_open = False
        editor = self._custom_editor
        if editor is None:
            editor = QtWidgets.QLineEdit(self.window())'''
    new = '''    def _begin_custom_entry(self, seed: str) -> None:
        super().hidePopup()
        self._popup_open = False
        editor = self._custom_editor
        if editor is None:
            parent_window = self.window()
            if parent_window is None:
                return
            editor = QtWidgets.QLineEdit(parent_window)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _refresh_adaptive_activity(self) -> None:
        candidate = self._adaptive_activity_candidate()
        ticket_height = max(1, self.ticket.compact_required_height())
        spare_height = max(0, self.height() - ticket_height)'''
    new = '''    def _refresh_adaptive_activity(self) -> None:
        candidate = self._adaptive_activity_candidate()
        try:
            ticket_height = max(1, self.ticket.compact_required_height())
        except (RuntimeError, AttributeError):
            ticket_height = 1
        spare_height = max(0, self.height() - ticket_height)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def compact_required_height(self) -> int:
        """Return the ticket's current unsqueezed content height.

        The right rail owns splitter geometry; this value is only a feature
        usability floor so an adjacent panel cannot compress the ticket until
        its fixed row rhythm or equal top/bottom inset is lost.
        """
        if not self.compact:
            return max(0, self.minimumSizeHint().height())
        self.ensurePolished()
        layout = self.layout()
        layout.invalidate()
        layout.activate()
        return max(
            1,
            layout.minimumSize().height(),
            self.minimumSizeHint().height(),
        )'''
    new = '''    def compact_required_height(self) -> int:
        """Return the ticket's current unsqueezed content height.

        The right rail owns splitter geometry; this value is only a feature
        usability floor so an adjacent panel cannot compress the ticket until
        its fixed row rhythm or equal top/bottom inset is lost.
        """
        if not self.compact:
            return max(0, self.minimumSizeHint().height())
        self.ensurePolished()
        layout = self.layout()
        if layout is None:
            return max(1, self.minimumSizeHint().height())
        layout.invalidate()
        layout.activate()
        return max(
            1,
            layout.minimumSize().height(),
            self.minimumSizeHint().height(),
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def sync_embedded_ticket_height(self) -> int:
        """Compatibility shim: report content height without fixing panel geometry."""
        self.ensurePolished()
        self.layout().invalidate()
        self.layout().activate()
        return max(
            self.minimumSizeHint().height(),
            self.sizeHint().height(),
        )'''
    new = '''    def sync_embedded_ticket_height(self) -> int:
        """Compatibility shim: report content height without fixing panel geometry."""
        self.ensurePolished()
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        return max(
            self.minimumSizeHint().height(),
            self.sizeHint().height(),
        )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    view.setUpdatesEnabled(False)
    try:
        view.clear()
        if not payloads:'''
    new = '''    view.setUpdatesEnabled(False)
    try:
        view.clear()
        view._cards_fingerprint = None  # Reset until successfully rebuilt.
        if not payloads:'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        quick = "QUICK ORDERS UNLOCKED" if self.gateway.armed else "QUICK ORDERS LOCKED"'''
    new = '''        try:
            quick = "QUICK ORDERS UNLOCKED" if self.gateway.armed else "QUICK ORDERS LOCKED"
        except (AttributeError, RuntimeError):
            quick = "QUICK ORDERS UNKNOWN"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _prepare_manual_trading(self, *_args) -> None:
        if self.gateway.has_credentials() and not self.gateway.stopping:
            self.gateway.ensure_cross(self.symbol)
            if not self.gateway.account_loaded:
                self.gateway.refresh_account(self.symbol)'''
    new = '''    def _prepare_manual_trading(self, *_args) -> None:
        try:
            if self.gateway.has_credentials() and not self.gateway.stopping:
                self.gateway.ensure_cross(self.symbol)
                if not self.gateway.account_loaded:
                    self.gateway.refresh_account(self.symbol)
        except (AttributeError, RuntimeError):
            pass  # Gateway may be shutting down.'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1






    old = '''        percent = max(1, min(100, int(percent)))
        dialog = CloseLimitDialog(position, percent, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), rules.lot_step
            )'''
    new = '''        percent = max(1, min(100, int(percent)))
        if rules.lot_step <= 0:
            self.status.setText("EXCHANGE LOT STEP UNAVAILABLE")
            return
        dialog = CloseLimitDialog(position, percent, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), rules.lot_step
            )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        percent = max(1, min(100, int(percent)))
        dialog = CloseLimitDialog(position, percent, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), self.rules.lot_step
            )'''
    new = '''        percent = max(1, min(100, int(percent)))
        if self.rules.lot_step <= 0:
            self.status.setText("EXCHANGE LOT STEP UNAVAILABLE")
            return
        dialog = CloseLimitDialog(position, percent, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            quantity = quantize_step(
                str(abs(amount) * percent / 100.0), self.rules.lot_step
            )'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _validate_live(self, _text: str = "") -> None:
        for index, (_side, price, quantity) in enumerate(self.rows, start=1):
            price_text = price.text().replace(",", "").strip()
            quantity_text = quantity.text().replace(",", "").strip()
            if not price_text and not quantity_text:
                continue
            if not price_text or not quantity_text:
                self._set_validation(f"ROW {index} · ENTER BOTH PRICE AND SIZE")
                return
            try:
                validate_step(price_text, self.rules.tick_size, f"Row {index} limit price")
                validate_step(quantity_text, self.rules.lot_step, f"Row {index} quantity")
            except ValueError as exc:
                self._set_validation(str(exc))
                return
        self._set_validation("")'''
    new = '''    def _validate_live(self, _text: str = "") -> None:
        if self.rules.tick_size <= 0 or self.rules.lot_step <= 0:
            self._set_validation("EXCHANGE RULES NOT LOADED FOR THIS SYMBOL")
            return
        for index, (_side, price, quantity) in enumerate(self.rows, start=1):
            price_text = price.text().replace(",", "").strip()
            quantity_text = quantity.text().replace(",", "").strip()
            if not price_text and not quantity_text:
                continue
            if not price_text or not quantity_text:
                self._set_validation(f"ROW {index} · ENTER BOTH PRICE AND SIZE")
                return
            try:
                validate_step(price_text, self.rules.tick_size, f"Row {index} limit price")
                validate_step(quantity_text, self.rules.lot_step, f"Row {index} quantity")
            except (ValueError, TypeError) as exc:
                self._set_validation(str(exc))
                return
        self._set_validation("")'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _validated_changes(self) -> dict[str, Any]:
        return {
            "symbol": str(self.order.get("symbol") or ""),
            "orderId": self.order.get("orderId"),
            "side": self.order.get("side"),
            "quantity": validate_step(self.quantity.text(), self.rules.lot_step, "Quantity"),
            "price": validate_step(self.price.text(), self.rules.tick_size, "Price"),
        }'''
    new = '''    def _validated_changes(self) -> dict[str, Any]:
        qty_text = self.quantity.text().replace(",", "").strip()
        price_text = self.price.text().replace(",", "").strip()
        if not qty_text:
            raise ValueError("Quantity cannot be empty.")
        if not price_text:
            raise ValueError("Price cannot be empty.")
        if self.rules.lot_step <= 0:
            raise ValueError("Exchange lot step is not available.")
        if self.rules.tick_size <= 0:
            raise ValueError("Exchange tick size is not available.")
        return {
            "symbol": str(self.order.get("symbol") or ""),
            "orderId": self.order.get("orderId"),
            "side": self.order.get("side"),
            "quantity": validate_step(qty_text, self.rules.lot_step, "Quantity"),
            "price": validate_step(price_text, self.rules.tick_size, "Price"),
        }'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    header_item = table.horizontalHeaderItem(column)
                    header = header_item.text().upper() if header_item is not None else ""'''
    new = '''                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    header_item = table.horizontalHeaderItem(column)
                    header = str(header_item.text()).upper() if header_item is not None else ""'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        price_rule = f"Tick {_exchange_step_text(rules.tick_size)} · range {format_price(rules.min_price)}–{format_price(rules.max_price)}"
        qty_rule = f"Lot {_exchange_step_text(rules.lot_step)} · market lot {_exchange_step_text(rules.market_step)}"'''
    new = '''        try:
            price_rule = f"Tick {_exchange_step_text(rules.tick_size)} · range {format_price(rules.min_price)}–{format_price(rules.max_price)}"
            qty_rule = f"Lot {_exchange_step_text(rules.lot_step)} · market lot {_exchange_step_text(rules.market_step)}"
        except (AttributeError, TypeError):
            price_rule = "Tick — · range —"
            qty_rule = "Lot — · market lot —"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        self.setToolTip(
            f"{symbol} · quote {rules.quote_asset} · margin {rules.margin_asset} · cross margin only"
        )'''
    new = '''        try:
            self.setToolTip(
                f"{symbol} · quote {rules.quote_asset} · margin {rules.margin_asset} · cross margin only"
            )
        except (AttributeError, TypeError):
            self.setToolTip(f"{symbol} · cross margin only")'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    previous = view.currentItem()
    previous_payload = previous.data(Qt.ItemDataRole.UserRole) if previous else None
    selected_key = _account_key(previous_payload) if isinstance(previous_payload, dict) else None
    scroll = view.verticalScrollBar().value()'''
    new = '''    previous = view.currentItem()
    previous_payload = previous.data(Qt.ItemDataRole.UserRole) if previous else None
    selected_key = _account_key(previous_payload) if isinstance(previous_payload, dict) else None
    scrollbar = view.verticalScrollBar()
    scroll = scrollbar.value() if scrollbar is not None else 0'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''        view.setCurrentItem(selection or view.item(0))
        view.verticalScrollBar().setValue(scroll)'''
    new = '''        view.setCurrentItem(selection or view.item(0))
        scrollbar = view.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.setValue(scroll)'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _update_order_summary(self) -> None:
        reducing = self.reduce_only.isChecked()
        buying = self.buy_button.isChecked()
        order_type = self.current_order_type()'''
    new = '''    def _update_order_summary(self) -> None:
        try:
            reducing = self.reduce_only.isChecked()
            buying = self.buy_button.isChecked()
            order_type = self.current_order_type()
        except (RuntimeError, AttributeError):
            return  # Widget may be in a partially destroyed state.'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _sync_auto_position_side(self) -> None:
        if self.hedge_mode:
            if self.reduce_only.isChecked():
                value = "SHORT" if self.buy_button.isChecked() else "LONG"
            else:
                value = "LONG" if self.buy_button.isChecked() else "SHORT"
        else:
            value = "BOTH"'''
    new = '''    def _sync_auto_position_side(self) -> None:
        try:
            if self.hedge_mode is True:
                if self.reduce_only.isChecked():
                    value = "SHORT" if self.buy_button.isChecked() else "LONG"
                else:
                    value = "LONG" if self.buy_button.isChecked() else "SHORT"
            else:
                value = "BOTH"
        except (RuntimeError, AttributeError):
            value = "BOTH"'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _update_execution_state(self) -> None:
        # Readiness/leverage/submission state remains authoritative for order
        # validation but no longer occupies permanent rows in the compact ticket.
        venue = "TESTNET" if self.testnet else "LIVE"'''
    new = '''    def _update_execution_state(self) -> None:
        # Readiness/leverage/submission state remains authoritative for order
        # validation but no longer occupies permanent rows in the compact ticket.
        try:
            venue = "TESTNET" if self.testnet else "LIVE"
        except (RuntimeError, AttributeError):
            return'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _update_submit_text(self) -> None:
        """Keep the two bottom execution actions aligned with OPEN/REDUCE intent."""
        if self.reduce_only.isChecked():'''
    new = '''    def _update_submit_text(self) -> None:
        """Keep the two bottom execution actions aligned with OPEN/REDUCE intent."""
        try:
            reducing = self.reduce_only.isChecked()
        except (RuntimeError, AttributeError):
            return
        if reducing:'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1


    old = '''    def _refresh_execution_freshness(self) -> None:
        self._sync_mark_controls()
        self._update_execution_state()
        self._update_order_summary()'''
    new = '''    def _refresh_execution_freshness(self) -> None:
        try:
            self._sync_mark_controls()
            self._update_execution_state()
            self._update_order_summary()
        except RuntimeError:
            self._readiness_timer.stop()'''
    if old in source:
        source = source.replace(old, new, 1)
        patches_applied += 1

    print(f"Applied {patches_applied} patches.")
    return source


def main() -> None:
    source = read_source()
    patched = apply_patches(source)
    if patched == source:
        print("No patches were applied. File may already be patched or patterns changed.")
        return
    backup = TARGET.with_suffix(".py.bak")
    backup.write_text(source, encoding="utf-8")
    print(f"Backup saved to {backup}")
    TARGET.write_text(patched, encoding="utf-8")
    print(f"Patched {TARGET} successfully.")


if __name__ == "__main__":
    main()
from __future__ import annotations

from PySide6 import QtCore, QtWidgets
import shiboken6

from nightwatch.trading.trading_ui import (
    PositionDeskRow,
    WorkingOrderRow,
    _new_account_card_list,
    _populate_account_cards,
)


EMPTY_TEXT = "No working orders"


def order(order_id: int, *, quantity: str = "1", status: str = "NEW"):
    return {
        "symbol": "BTCUSDT",
        "_source": "ORDER",
        "orderId": str(order_id),
        "side": "BUY",
        "type": "LIMIT",
        "price": "64000",
        "origQty": quantity,
        "executedQty": "0",
        "status": status,
    }


def position(symbol: str, side: str, *, amount: str = "1", pnl: str = "1"):
    return {
        "symbol": symbol,
        "positionSide": side,
        "positionAmt": amount,
        "entryPrice": "64000",
        "unrealizedProfit": pnl,
        "leverage": "10",
    }


def make_factory(cancel_events, *, with_close_editor=False):
    def factory(payload):
        row = WorkingOrderRow(payload, {}, "BTCUSDT")
        row.cancel_requested.connect(cancel_events.append)
        if with_close_editor:
            editor = QtWidgets.QLineEdit(row)
            editor.setObjectName("testCloseQuantity")
            row.grid.addWidget(editor, 4, 0, 1, 2)
            row.close_quantity_editor = editor
        return row

    return factory


def populate(view, payloads, factory, *, empty_text=EMPTY_TEXT, update_context="BTCUSDT"):
    _populate_account_cards(
        view,
        payloads,
        factory,
        empty_text,
        update_existing=lambda card, payload: card.update_payload(payload, update_context),
        update_context=update_context,
    )


def card_map(view):
    return {
        view.item(index).data(QtCore.Qt.ItemDataRole.UserRole)["orderId"]:
        view.itemWidget(view.item(index))
        for index in range(view.count())
        if isinstance(view.item(index).data(QtCore.Qt.ItemDataRole.UserRole), dict)
    }


def test_reorder_reuses_rows_selection_scroll_editors_and_signals(qapp):
    view = _new_account_card_list()
    view.resize(420, 180)
    cancel_events = []
    factory = make_factory(cancel_events, with_close_editor=True)
    original = [order(index) for index in range(30)]
    populate(view, original, factory)
    view.show()
    qapp.processEvents()

    original_cards = card_map(view)
    native_sort = view.sortItems
    sort_calls = []

    def track_sort(order=QtCore.Qt.SortOrder.AscendingOrder):
        sort_calls.append(order)
        return native_sort(order)

    view.sortItems = track_sort
    selected_item = view.item(12)
    selected_id = selected_item.data(QtCore.Qt.ItemDataRole.UserRole)["orderId"]
    view.setCurrentItem(selected_item)
    view.verticalScrollBar().setValue(80)
    qapp.processEvents()
    scroll = view.verticalScrollBar().value()
    assert scroll > 0

    editor = original_cards["17"].close_quantity_editor
    editor.setText("0.375")
    refreshed = [order(index, quantity="2.5", status="PARTIALLY_FILLED") for index in reversed(range(30))]
    populate(view, refreshed, factory)
    qapp.processEvents()

    assert sort_calls == [QtCore.Qt.SortOrder.AscendingOrder]
    assert [view.item(index).data(QtCore.Qt.ItemDataRole.UserRole)["orderId"]
            for index in range(view.count())] == [str(index) for index in reversed(range(30))]
    assert card_map(view) == original_cards
    assert card_map(view)["17"].close_quantity_editor is editor
    assert editor.text() == "0.375"
    assert card_map(view)["17"].payload["origQty"] == "2.5"
    assert view.currentItem() is selected_item
    assert view.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)["orderId"] == selected_id
    assert view.verticalScrollBar().value() == scroll

    # Existing card actions and row-selection wiring are retained exactly once.
    card = card_map(view)["17"]
    card.cancel_button.click()
    assert len(cancel_events) == 1
    assert cancel_events[0]["orderId"] == "17"
    moved_item = next(view.item(i) for i in range(view.count())
                      if view.item(i).data(QtCore.Qt.ItemDataRole.UserRole)["orderId"] == "17")
    other_item = view.item(0)
    view.setCurrentItem(other_item)
    card.selected_requested.emit()
    assert view.currentItem() is moved_item
    view.close()


def test_insert_remove_reuses_survivors_and_defers_removed_widget_deletion(qapp):
    view = _new_account_card_list()
    view.resize(420, 180)
    cancel_events = []
    factory = make_factory(cancel_events)
    populate(view, [order(index) for index in range(5)], factory)
    view.show()
    qapp.processEvents()
    original_cards = card_map(view)
    removed = [original_cards[key] for key in ("1", "3")]
    survivors = {key: card for key, card in original_cards.items() if key not in {"1", "3"}}

    payloads = [order(7), order(4, quantity="3"), order(2), order(5), order(0)]
    populate(view, payloads, factory)
    qapp.processEvents()

    assert [view.item(i).data(QtCore.Qt.ItemDataRole.UserRole)["orderId"]
            for i in range(view.count())] == ["7", "4", "2", "5", "0"]
    cards = card_map(view)
    assert all(cards[key] is card for key, card in survivors.items())
    assert cards["4"].payload["origQty"] == "3"
    assert all(shiboken6.isValid(card) for card in removed)

    # Model row removal uses Qt's deferred widget ownership cleanup.
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert all(not shiboken6.isValid(card) for card in removed)
    cards["4"].cancel_button.click()
    assert [event["orderId"] for event in cancel_events] == ["4"]
    view.close()


def test_duplicate_keys_and_changed_factory_use_safe_rebuild_and_empty_state(qapp):
    view = _new_account_card_list()
    cancel_events = []
    first_factory = make_factory(cancel_events)
    populate(view, [order(1), order(2)], first_factory)
    old_card = card_map(view)["1"]

    # Duplicate identities are ambiguous, so rebuild rather than associate a
    # live row with the wrong payload.
    populate(view, [order(3), order(3, quantity="4")], first_factory)
    assert view.count() == 2
    duplicate_cards = [view.itemWidget(view.item(index)) for index in range(view.count())]
    assert all(card is not old_card for card in duplicate_cards)
    assert [card.payload["origQty"] for card in duplicate_cards] == ["1", "4"]

    # A changed factory may carry different card type or signal wiring, even
    # when the identity sequence itself is unchanged.
    populate(view, [order(3), order(4)], first_factory)
    old_unique_card = card_map(view)["3"]
    second_factory = make_factory(cancel_events, with_close_editor=True)
    populate(view, [order(3, quantity="5"), order(4)], second_factory)
    assert card_map(view)["3"] is not old_unique_card
    assert all(hasattr(card_map(view)[key], "close_quantity_editor") for key in ("3", "4"))

    populate(view, [], None, empty_text="Connect API credentials to view orders")
    assert view.count() == 1
    empty_card = view.itemWidget(view.item(0))
    assert empty_card.objectName() == "accountEmptyState"
    assert empty_card.findChild(QtWidgets.QLabel).text() == "Connect API credentials to view orders"
    assert view.currentItem() is None
    view.close()


def test_unchanged_payloads_skip_updates_only_when_context_matches(qapp):
    view = _new_account_card_list()
    cancel_events = []
    factory = make_factory(cancel_events)
    payloads = [order(1), order(2)]
    populate(view, payloads, factory)
    cards = card_map(view)
    updates = []
    for card in cards.values():
        original_update = card.update_payload

        def track_update(payload, context, *, _original=original_update):
            updates.append(context)
            _original(payload, context)

        card.update_payload = track_update

    populate(view, list(reversed(payloads)), factory)
    assert updates == []
    assert card_map(view)["1"] is cards["1"]
    assert card_map(view)["2"] is cards["2"]

    populate(view, list(reversed(payloads)), factory, update_context="ETHUSDT")
    assert updates == ["ETHUSDT", "ETHUSDT"]
    view.close()


def test_position_keys_reuse_rows_for_membership_reorder_and_selection(qapp):
    view = _new_account_card_list()
    factory = lambda payload: PositionDeskRow(payload, "BTCUSDT")
    updater = lambda card, payload, context: card.update_payload(payload, context)

    initial = [
        position("BTCUSDT", "BOTH"),
        position("ETHUSDT", "LONG"),
        position("ETHUSDT", "SHORT"),
        position("SOLUSDT", "BOTH"),
    ]
    _populate_account_cards(
        view, initial, factory, "No open positions",
        update_existing=lambda card, payload: updater(card, payload, "BTCUSDT"),
        update_context="BTCUSDT",
    )
    view.show()
    qapp.processEvents()

    def rows_by_position():
        return {
            (view.item(index).data(QtCore.Qt.ItemDataRole.UserRole)["symbol"],
             view.item(index).data(QtCore.Qt.ItemDataRole.UserRole)["positionSide"]):
            view.itemWidget(view.item(index))
            for index in range(view.count())
        }

    original = rows_by_position()
    selected_item = view.item(2)
    selected_widget = view.itemWidget(selected_item)
    view.setCurrentItem(selected_item)
    view.verticalScrollBar().setValue(20)

    refreshed = [
        position("SOLUSDT", "BOTH"),
        position("ETHUSDT", "SHORT", pnl="7"),
        position("ETHUSDT", "LONG"),
        position("BTCUSDT", "LONG"),
    ]
    _populate_account_cards(
        view, refreshed, factory, "No open positions",
        update_existing=lambda card, payload: updater(card, payload, "BTCUSDT"),
        update_context="BTCUSDT",
    )
    qapp.processEvents()

    rows = rows_by_position()
    assert [(view.item(index).data(QtCore.Qt.ItemDataRole.UserRole)["symbol"],
             view.item(index).data(QtCore.Qt.ItemDataRole.UserRole)["positionSide"])
            for index in range(view.count())] == [
                ("SOLUSDT", "BOTH"), ("ETHUSDT", "SHORT"),
                ("ETHUSDT", "LONG"), ("BTCUSDT", "LONG"),
            ]
    assert rows[("SOLUSDT", "BOTH")] is original[("SOLUSDT", "BOTH")]
    assert rows[("ETHUSDT", "SHORT")] is selected_widget
    assert rows[("ETHUSDT", "LONG")] is original[("ETHUSDT", "LONG")]
    assert rows[("ETHUSDT", "SHORT")].payload["unrealizedProfit"] == "7"
    assert view.currentItem() is selected_item

    # The retained PositionDeskRow signal still selects its original item.
    other_item = view.item(0)
    view.setCurrentItem(other_item)
    selected_widget.selected_requested.emit()
    assert view.currentItem() is selected_item
    view.close()

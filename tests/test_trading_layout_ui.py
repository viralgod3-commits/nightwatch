"""UI checks for independent columns and the consolidated execution/account panel."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PySide6 import QtCore, QtWidgets, QtTest

from nightwatch.constants import RIGHT_PANEL_NAMES
from nightwatch.models import SymbolRules
from nightwatch.theme import RIGHT_LAYOUT_PRESETS, THEMES
from nightwatch.trading.trading_ui import TradingWorkspace
from nightwatch.ui.dialogs import RightPanelPresetsDialog
from nightwatch.ui.panels import PanelSpec, RightRailController, RightRailState, panel_ids
from nightwatch.utilities import TextRole, load_app_fonts, typography_font


@pytest.fixture
def workspace(qapp, gateway, monkeypatch):
    load_app_fonts(qapp, str(Path(__file__).resolve().parents[1] / 'nightwatch'))
    monkeypatch.setattr(gateway, 'refresh_account', lambda *a, **k: None)
    monkeypatch.setattr(gateway, 'ensure_cross', lambda *a, **k: None)
    widget = TradingWorkspace(THEMES['Nightwatch'], gateway)
    widget.set_symbol('BTCUSDT', SymbolRules())
    widget.resize(330, 540)
    widget.show()
    for _ in range(4):
        qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def settle(qapp):
    for _ in range(6):
        qapp.processEvents()


def account_snapshot():
    return {'ordersScope': 'ALL', 'fillsSymbol': 'BTCUSDT', 'hedgeMode': False,
            'account': {'availableBalance': '1200.50', 'positions': [
                {'symbol': 'BTCUSDT', 'positionSide': 'BOTH', 'positionAmt': '0.25',
                 'entryPrice': '64000', 'markPrice': '65000', 'unrealizedProfit': '250',
                 'leverage': '10', 'positionInitialMargin': '1600', 'liquidationPrice': '59000'},
                {'symbol': 'ETHUSDT', 'positionSide': 'BOTH', 'positionAmt': '-2',
                 'entryPrice': '3200', 'markPrice': '3100', 'unrealizedProfit': '200',
                 'leverage': '5', 'positionInitialMargin': '1280', 'liquidationPrice': '3800'}],
                'assets': [{'asset': 'USDT', 'walletBalance': '2000',
                            'availableBalance': '1200.50', 'unrealizedProfit': '450'}]},
            'orders': [{'symbol': 'BTCUSDT', 'orderId': '42', 'side': 'BUY', 'type': 'LIMIT',
                        'price': '63000', 'origQty': '0.1', 'executedQty': '0.025', 'status': 'PARTIALLY_FILLED'},
                       {'symbol': 'ETHUSDT', 'orderId': '43', 'side': 'SELL', 'type': 'LIMIT',
                        'price': '3300', 'origQty': '1', 'executedQty': '0', 'status': 'NEW'}],
            'algoOrders': [{'symbol': 'BTCUSDT', 'algoId': '99', 'side': 'SELL',
                            'orderType': 'STOP_MARKET', 'triggerPrice': '62000',
                            'quantity': '0.25', 'algoStatus': 'NEW', 'reduceOnly': True}],
            'fills': [{'symbol': 'BTCUSDT', 'id': '7', 'side': 'BUY', 'price': '64000',
                       'qty': '0.25', 'realizedPnl': '0', 'commission': '3.2',
                       'commissionAsset': 'USDT', 'time': 1728000000000}]}


def click_tab(bar, index):
    QtTest.QTest.mouseClick(bar, QtCore.Qt.MouseButton.LeftButton,
                           pos=bar.tabRect(index).center())


def test_view_switch_stays_in_panel_and_preserves_ticket_and_selection(workspace, qapp):
    ticket = workspace.ticket
    ticket.type_combo.setCurrentIndex(ticket.type_combo.findData('LIMIT'))
    ticket.price_edit.setText('63000')
    ticket.quantity_edit.setText('0.1')
    workspace.gateway.snapshot_ready.emit(account_snapshot())
    workspace.select_position(account_snapshot()['account']['positions'][1])
    settle(qapp)
    selected = workspace.positions.currentItem()
    click_tab(workspace.view_tabs, 1)
    settle(qapp)
    assert workspace.current_page() == 1
    assert workspace.account_frame.isVisible() and not ticket.isVisible()
    assert workspace.account_frame.parentWidget() is workspace.pages
    assert workspace.view_tabs.isVisible()
    assert workspace.selected_position()['symbol'] == 'ETHUSDT'
    click_tab(workspace.view_tabs, 0)
    settle(qapp)
    assert ticket.isVisible() and workspace.positions.currentItem() is selected
    assert (ticket.price_edit.text(), ticket.quantity_edit.text()) == ('63000', '0.1')


@pytest.mark.parametrize('width,height', [(300, 240), (330, 440), (520, 380), (760, 600)])
@pytest.mark.parametrize('kind', ['MARKET', 'LIMIT', 'STOP', 'TRAILING_STOP_MARKET'])
def test_ticket_controls_remain_reachable_at_different_sizes(workspace, qapp, width, height, kind):
    workspace.resize(width, height)
    ticket = workspace.ticket
    ticket.type_combo.setCurrentIndex(ticket.type_combo.findData(kind))
    settle(qapp)
    assert workspace.size() == QtCore.QSize(width, height)
    assert workspace.view_tabs.geometry().right() < workspace.view_tabs.parentWidget().width()
    assert ticket.buy_button.isVisible() and ticket.sell_button.isVisible()
    for button in (ticket.buy_button, ticket.sell_button):
        rect = QtCore.QRect(button.mapTo(workspace, QtCore.QPoint()), button.size())
        assert workspace.rect().contains(rect)
        assert button.width() >= 100
    viewport = ticket.ticket_scroll.viewport()
    for control in ticket._field_rows.values():
        if not control.isVisible():
            continue
        origin = control.mapTo(viewport, QtCore.QPoint())
        assert origin.x() >= 0 and origin.x() + control.width() <= viewport.width()
    for edit, mark in ((ticket.price_edit, ticket.price_mark_button),
                       (ticket.trigger_edit, ticket.trigger_mark_button),
                       (ticket.activation_edit, ticket.activation_mark_button)):
        if edit.isVisible():
            assert edit.height() == mark.height()
            assert edit.mapTo(viewport, QtCore.QPoint()).y() == mark.mapTo(viewport, QtCore.QPoint()).y()
    if ticket.type_combo.isVisible():
        selector = ticket._field_rows['type']
        source = ticket._field_rows['source']
        assert selector.y() == source.y()
        assert selector.geometry().right() < source.geometry().left()
    assert ticket.quantity_edit.font().pointSizeF() == typography_font(TextRole.TABLE_VALUE).pointSizeF()
    ticket.ticket_scroll.verticalScrollBar().setValue(ticket.ticket_scroll.verticalScrollBar().maximum())
    settle(qapp)
    assert ticket.buy_button.isVisible() and ticket.execution_state_label.isVisible()


def test_filled_conditional_inputs_survive_reflow_and_only_tif_has_help(workspace, qapp):
    from nightwatch.utilities import tooltip_controller, tooltips_allowed
    ticket = workspace.ticket
    ticket.type_combo.setCurrentIndex(ticket.type_combo.findData('STOP'))
    ticket.price_edit.setText('63000')
    ticket.trigger_edit.setText('63500')
    ticket.quantity_edit.setText('0.1')
    for width in (300, 760, 330):
        workspace.resize(width, 440)
        settle(qapp)
        assert (ticket.price_edit.text(), ticket.trigger_edit.text(), ticket.quantity_edit.text()) == ('63000', '63500', '0.1')
        for edit in (ticket.price_edit, ticket.trigger_edit):
            assert edit._hint.isVisible()
            assert edit.textMargins().left() > edit._hint.width()
            assert edit.rect().contains(edit._hint.geometry())
    controller = tooltip_controller()
    controller.request(ticket.price_edit, 'Price', ticket.price_edit.mapToGlobal(QtCore.QPoint()))
    assert not controller._timer.isActive()
    assert not tooltips_allowed(ticket.price_edit)
    assert tooltips_allowed(ticket.time_in_force)
    for index in range(ticket.time_in_force.count()):
        ticket.time_in_force.setCurrentIndex(index)
        assert ticket.time_in_force.toolTip() == ticket.time_in_force.itemData(index, QtCore.Qt.ItemDataRole.ToolTipRole)


def test_account_tabs_cancel_and_close_keep_correct_payloads(workspace, gateway, monkeypatch, qapp):
    cancellations = []
    monkeypatch.setattr(gateway, 'submit_cancel', lambda payload, algo: cancellations.append((payload, algo)))
    gateway.snapshot_ready.emit(account_snapshot())
    workspace.set_page(1)
    settle(qapp)
    assert workspace.tabs.count() == 4
    assert [workspace.tabs.tabText(i).split()[0] for i in range(4)] == ['Positions', 'Orders', 'Fills', 'Balances']
    assert workspace.cancel_all_button.isHidden()
    click_tab(workspace.tabs.tabBar(), 1)
    settle(qapp)
    assert workspace.cancel_all_button.isVisible()
    for index in range(workspace.orders.count()):
        card = workspace.orders.itemWidget(workspace.orders.item(index))
        if card.payload.get('symbol') == 'BTCUSDT':
            card.cancel_button.click()
    assert ({'symbol': 'BTCUSDT', 'orderId': '42'}, False) in cancellations
    assert ({'symbol': 'BTCUSDT', 'algoId': '99'}, True) in cancellations
    assert all(payload['symbol'] == 'BTCUSDT' for payload, _ in cancellations)
    closed = []
    orders = []
    workspace.order_requested.connect(orders.append)
    workspace.account_frame.details.close_requested.connect(lambda payload, percent: closed.append((payload, percent)))
    workspace.select_position(account_snapshot()['account']['positions'][0])
    workspace.account_frame.details.close_menu.actions()[0].menu().actions()[1].trigger()
    assert closed[-1][0]['symbol'] == 'BTCUSDT' and closed[-1][1] == 50
    assert orders[-1]['order']['side'] == 'SELL'
    assert float(orders[-1]['order']['quantity']) == .125
    assert orders[-1]['order']['reduceOnly'] is True


@pytest.mark.parametrize('width,height', [(300, 280), (330, 440), (600, 440)])
def test_account_rows_and_metrics_reflow_without_resetting_state(workspace, qapp, width, height):
    workspace.gateway.snapshot_ready.emit(account_snapshot())
    workspace.select_position(account_snapshot()['account']['positions'][0])
    workspace.set_page(1)
    item = workspace.positions.currentItem()
    card = workspace.positions.itemWidget(item)
    workspace.resize(width, height)
    settle(qapp)
    assert workspace.size() == QtCore.QSize(width, height)
    assert workspace.positions.currentItem() is item
    assert workspace.positions.itemWidget(item) is card
    workspace.set_mark_price(66000, 'BTCUSDT')
    assert workspace.selected_position()['unrealizedProfit'] == 500.0
    details = workspace.account_frame.details
    assert details.pnl_label.font().pointSizeF() == typography_font(TextRole.MARKET_VALUE_EMPHASIZED).pointSizeF()
    assert details.trade_button.width() >= 55
    assert details.values['entry'].geometry().right() < details.width()
    for index in range(4):
        workspace.tabs.setCurrentIndex(index)
        settle(qapp)
        assert workspace.tabs.currentWidget().isVisible()
        if index == 3:
            assert workspace.balances.horizontalScrollBar().maximum() == 0
        assert workspace.view_tabs.isVisible()
        assert workspace.tabs.tabBar().geometry().right() < workspace.account_frame.width()
    capture = os.environ.get('NIGHTWATCH_TRADING_QA_DIR')
    if capture:
        Path(capture).mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(('positions', 'orders', 'fills', 'balances')):
            workspace.tabs.setCurrentIndex(index)
            settle(qapp)
            workspace.grab().save(str(Path(capture) / f'{name}-{width}x{height}.png'))
        workspace.set_page(0)
        settle(qapp)
        workspace.grab().save(str(Path(capture) / f'trade-{width}x{height}.png'))


@pytest.mark.parametrize('name,full_height', [('Balanced', None), ('Book focus', 'Market depth'),
                                            ('Tape focus', 'Large trades'), ('Wide desk', 'Market depth')])
def test_presets_have_independent_columns_and_preserve_full_height(qapp, tmp_path, name, full_height):
    settings = QtCore.QSettings(str(tmp_path / 'layout.ini'), QtCore.QSettings.Format.IniFormat)
    specs = [PanelSpec(pid, title, QtWidgets.QWidget, 300, 90, 240)
             for title, pid in zip(RIGHT_PANEL_NAMES, ('depth', 'trading', 'trades', 'watchlist'))]
    controller = RightRailController(settings, specs, RIGHT_LAYOUT_PRESETS, initial_preset=name)
    controller.apply_preset(name, RIGHT_LAYOUT_PRESETS[name])
    controller.rail.resize(960 if name == 'Wide desk' else 660, 720)
    controller.rail.show()
    settle(qapp)
    root = controller.state.root
    assert root.axis == 'h'
    assert 'orders' not in panel_ids(root)
    assert len(root.children) == (3 if name == 'Wide desk' else 2)
    if full_height:
        panel = controller.sections[full_height]
        assert panel.height() >= controller._canvas.height() - 2
        assert panel.mapTo(controller._canvas, QtCore.QPoint()).y() == 0
    trading = controller.sections['Trading / positions']
    market = controller.sections['Market depth']
    assert trading.mapTo(controller._canvas, QtCore.QPoint()).x() != market.mapTo(controller._canvas, QtCore.QPoint()).x() or name == 'Tape focus'
    controller.capture_geometry()
    controller.save_state()
    restored = RightRailState.from_dict(json.loads(settings.value('right_rail/state_v5')))
    assert panel_ids(restored.root) == panel_ids(root)
    controller.rail.close()
    controller.deleteLater()
    settle(qapp)


def test_legacy_orders_removed_and_preset_rename_retains_topology(qapp):
    old = {'version': 5, 'column_mode': 2, 'panels': {'orders': {'enabled': True}},
           'root': {'kind': 'split', 'id': 'legacy', 'axis': 'h', 'weights': [.5, .5],
                    'children': [{'kind': 'panel', 'id': 'orders'}, {'kind': 'panel', 'id': 'depth'}]}}
    restored = RightRailState.from_dict(old)
    assert panel_ids(restored.root) == ['depth'] and 'orders' not in restored.unresolved_panels
    dialog = RightPanelPresetsDialog(RIGHT_LAYOUT_PRESETS)
    edit, checks = dialog.rows[0]
    edit.setText('My balanced desk')
    assert 'Orders' not in checks
    assert dialog.definitions()['My balanced desk']['tree'] == RIGHT_LAYOUT_PRESETS['Balanced']['tree']
    dialog.close()
    dialog.deleteLater()

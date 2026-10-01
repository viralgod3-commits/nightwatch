from dataclasses import replace

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from nightwatch.models import OrderFlowSnapshot, OrderFlowTradePrint
from nightwatch.orderbook.orderbook_ui import (
    TradesTapeWidget, _TradePriceDelegate, _TradesTapeModel,
    _decimal_places_from_step, format_book_price,
)
from nightwatch.utilities import TextRole, TypographyController, typography_font


def trade(sequence, price, *, side='BUY', outcome='UNRESOLVED', salience=1):
    return OrderFlowTradePrint(
        sequence=sequence, event_time_ms=1700000000000 + sequence * 1000,
        received_monotonic=sequence / 10, price=price, quantity=1,
        notional=price, aggressor_side=side, normal_notional=price,
        rpi_notional=0, relative_size=1, salience_class=salience, outcome=outcome,
    )


def snapshot(trades, sequence=1):
    return OrderFlowSnapshot(
        symbol='ETHUSDT', sequence=sequence, generated_monotonic=10,
        ready=True, live=True, bbo_source='depth', depth_age_seconds=0,
        bbo_age_seconds=0, trade_age_seconds=0,
        recent_prints=tuple(trades), large_trade_threshold=100,
    )


@pytest.mark.parametrize('price,previous,changed', [
    ('2248.73', '2247.74', {3, 6}),  # Unchanged digit between changed digits.
    ('2248.86', '2248.66', {5}),  # Unchanged trailing digit stays regular.
    ('2248.75', '2248.75', set()),
    ('2248.75', '', set()),
    ('99.90', '100.00', {0, 1, 3}),
    ('100.00', '99.90', {0, 1, 2, 4}),
    ('100.01', '99.99', {0, 1, 2, 4, 5}),
    ('100000', '99999', {0, 1, 2, 3, 4, 5}),
    ('1234.50', '1234.40', {5}),
    ('0.00000012', '0.00000010', {9}),
    ('0.00000525', '0.00000475', {7, 8}),
    ('1.200', '1.2', set()),
    ('0.1', '0.01', {2}),
])
def test_price_emphasis_compares_individual_decimal_places(price, previous, changed):
    mask = _TradePriceDelegate.changed_digits(price, previous)
    assert len(mask) == len(price)
    assert {position for position, emphasis in enumerate(mask) if emphasis} == changed


@pytest.mark.parametrize('tick,price,expected', [
    (1, 100000, '100000'),
    (0.1, 65432.1, '65432.1'),
    (0.01, 2248.7, '2248.70'),
    (0.25, 124.5, '124.50'),
    (0.125, 0.375, '0.375'),
    (0.00001, 0.01234, '0.01234'),
    (1e-8, 0.00000012, '0.00000012'),
    (2.5e-7, 0.00000525, '0.00000525'),
    (1e-10, 1e-10, '0.0000000001'),
    (1e-16, 1e-16, '0.0000000000000001'),
])
def test_price_precision_follows_tick_without_exponent_or_lost_zeros(tick, price, expected):
    assert format_book_price(price, _decimal_places_from_step(tick)) == expected


@pytest.mark.parametrize('outcome,symbol,explanation', [
    ('FOLLOW_THROUGH', '✓', 'follow-through'),
    ('REJECTED', '×', 'did not meet the follow-through threshold'),
    ('UNRESOLVED', '…', 'pending'),
    ('UNKNOWN', '—', 'unknown'),
])
def test_results_are_compact_with_explanations_in_tooltips(qapp, outcome, symbol, explanation):
    model = _TradesTapeModel()
    model.decimals = 2
    model.set_trades([trade(1, 2248.7, outcome=outcome)])
    index = model.index(0, 4)
    assert index.data() == symbol
    assert explanation in index.data(Qt.ItemDataRole.ToolTipRole)
    assert 'Price: 2248.70' in index.data(Qt.ItemDataRole.ToolTipRole)
    assert index.data(Qt.ItemDataRole.TextAlignmentRole) == int(Qt.AlignmentFlag.AlignCenter)


def test_typography_states_select_static_faces_and_keep_role_preferences(qapp, monkeypatch):
    import nightwatch.utilities as utilities
    monkeypatch.setattr(utilities, '_NUMERIC_FONT_FAMILIES', {
        ('normal', 400): 'Registered regular',
        ('normal', 700): 'Registered bold',
    })
    controller = TypographyController()
    controller.configure({TextRole.TABLE_VALUE: {'size': 13.5}}, notify=False)
    regular = controller.font(TextRole.TABLE_VALUE, state='trade_price_regular')
    changed = controller.font(TextRole.TABLE_VALUE, state='trade_price_changed')
    assert regular.families()[0] == 'Registered regular'
    assert changed.families()[0] == 'Registered bold'
    assert regular.weight() == QtGui.QFont.Weight.Normal
    assert changed.weight() == QtGui.QFont.Weight.Bold
    assert regular.pointSizeF() == changed.pointSizeF() == 13.5
    assert controller.font(TextRole.TABLE_VALUE).weight() == QtGui.QFont.Weight.Medium
    assert controller.font(TextRole.TABLE_VALUE, emphasized=True).weight() == QtGui.QFont.Weight.DemiBold
    regular.setPointSizeF(99)
    assert controller.font(TextRole.TABLE_VALUE, state='trade_price_regular').pointSizeF() == 13.5


@pytest.mark.parametrize('width,price,previous,side', [
    (120, 2248.73, 2247.74, 'BUY'),
    (120, 2248.86, 2248.66, 'SELL'),
    (45, 0.00000012, 0.00000010, 'BUY'),  # Narrow cell keeps emphasis when elided.
])
def test_price_painter_uses_one_side_color_and_clips_to_the_cell(qapp, width, price, previous, side):
    model = _TradesTapeModel()
    model.decimals = 8 if price < 1 else 2
    model.set_trades([trade(2, price, side=side), trade(1, previous)])
    delegate = _TradePriceDelegate()
    image = QtGui.QImage(width + 20, 36, QtGui.QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.black)
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = QtCore.QRect(10, 4, width, 28)
    painter = QtGui.QPainter(image)
    try:
        delegate.paint(painter, option, model.index(0, 2))
    finally:
        painter.end()
    ink = [(x, y, image.pixelColor(x, y)) for y in range(image.height())
           for x in range(image.width()) if image.pixelColor(x, y).value()]
    assert ink
    assert all(14 <= x < width + 5 and 4 <= y < 32 for x, y, _ in ink)
    if side == 'BUY':
        assert all(color.red() == 0 for _, _, color in ink)
    else:
        assert all(color.red() > 3 * color.green() for _, _, color in ink)
    layout, line = next(reversed(delegate._layouts.values()))
    assert layout.font().weight() == QtGui.QFont.Weight.Normal
    assert all(span.format.fontWeight() == QtGui.QFont.Weight.Bold for span in layout.formats())
    assert all(span.format.foreground().style() == Qt.BrushStyle.NoBrush for span in layout.formats())
    if width == 120:
        positions = {i for span in layout.formats() for i in range(span.start, span.start + span.length)}
        assert positions == ({3, 6} if side == 'BUY' else {5})
    else:
        assert layout.text().startswith('…') and layout.text().endswith('12')
        assert line.naturalTextWidth() <= width - 9
        assert [(span.start, span.length) for span in layout.formats()] == [(len(layout.text()) - 1, 1)]


def test_layout_cache_is_bounded_and_reuses_shaped_text(qapp):
    delegate = _TradePriceDelegate()
    regular = typography_font(TextRole.TABLE_VALUE, state='trade_price_regular')
    changed = typography_font(TextRole.TABLE_VALUE, state='trade_price_changed')
    device = QtGui.QImage(120, 28, QtGui.QImage.Format.Format_ARGB32)
    for value in range(delegate.LAYOUT_CACHE_LIMIT + 10):
        text = f'{value}.00'
        delegate._layout(text, (False,) * len(text), regular, changed, device)
    assert len(delegate._layouts) == delegate.LAYOUT_CACHE_LIMIT
    assert all(key[0] != '0.00' for key in delegate._layouts)
    text = f'{delegate.LAYOUT_CACHE_LIMIT + 9}.00'
    before = delegate._layout(text, (False,) * len(text), regular, changed, device)
    after = delegate._layout(text, (False,) * len(text), regular, changed, device)
    assert before is after


def test_prepend_and_outcome_updates_keep_model_rows_and_neighbor_comparison(qapp):
    model = _TradesTapeModel()
    model.decimals = 2
    rows = [trade(3, 2248.73), trade(2, 2247.74), trade(1, 2247.74)]
    model.set_trades(rows)
    resets, updates = [], []
    model.modelReset.connect(lambda: resets.append(True))
    model.dataChanged.connect(lambda first, last, roles: updates.append((first.row(), first.column(), last.column())))
    rows = [trade(4, 2248.86), *rows[:2]]
    model.set_trades(rows)
    assert [row[0].sequence for row in model.rows] == [4, 3, 2]
    assert not resets
    assert _TradePriceDelegate.changed_digits(model.index(1, 2).data(), model.index(2, 2).data()) == (
        False, False, False, True, False, False, True,
    )
    model.set_trades([replace(rows[0], outcome='FOLLOW_THROUGH'), *rows[1:]])
    assert model.index(0, 4).data() == '✓'
    assert updates == [(0, 4, 4)] and not resets


def test_all_and_large_modes_compare_preceding_displayed_trade_and_update_results(qapp):
    widget = TradesTapeWidget({})
    widget.set_market('ETHUSDT', tick_size=0.0001)
    rows = [trade(1, 1.3300), trade(2, 1.3310, salience=0), trade(3, 1.3318)]
    widget.set_order_flow_snapshot(snapshot(rows))
    assert widget.model.rowCount() == 0  # Ingestion while hidden does not mutate the table.
    widget.resize(480, 250)
    widget.show()
    widget.set_panel_active(True)
    widget._refresh_table()
    qapp.processEvents()
    assert [row[0].sequence for row in widget.model.rows] == [3, 1]
    mask = _TradePriceDelegate.changed_digits(widget.model.index(0, 2).data(), widget.model.index(1, 2).data())
    assert {i for i, changed in enumerate(mask) if changed} == {4, 5}
    widget.set_mode('ALL', emit=False)
    widget._refresh_table()
    assert [row[0].sequence for row in widget.model.rows] == [3, 2, 1]
    mask = _TradePriceDelegate.changed_digits(widget.model.index(0, 2).data(), widget.model.index(1, 2).data())
    assert {i for i, changed in enumerate(mask) if changed} == {5}
    widget.set_order_flow_snapshot(snapshot([*rows[:-1], replace(rows[-1], outcome='FOLLOW_THROUGH')], sequence=2))
    widget._refresh_table()
    assert widget.model.index(0, 4).data() == '✓'
    widget.set_mode('LARGE', emit=False)
    widget._refresh_table()
    assert widget.model.index(0, 4).data() == '✓'
    widget.close()
    widget.deleteLater()

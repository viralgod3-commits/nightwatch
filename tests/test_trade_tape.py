from dataclasses import replace
import math
import time

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from nightwatch.models import OrderFlowSnapshot, OrderFlowTradePrint
from nightwatch.orderbook.tape import TapeFrame, make_patch
from nightwatch.orderbook.orderbook_ui import (
    TradesTapeWidget, _TradePriceDelegate, _TradesTapeModel,
    _decimal_places_from_step, format_book_price,
)
from nightwatch.utilities import TextRole, TypographyController, typography_font


def set_trades(model, trades):
    """Exercise the same seed/delta protocol that the worker delivers."""
    entries = tuple((model.identity(record), record) for record in trades)
    base = tuple(zip(model.keys, (row[0] for row in model.rows))) if model.rows else None
    model.set_frame(TapeFrame('test', 1, 'ETHUSDT', 'ALL', 'test-stream',
                              2 if base else 1, 1 if base else None, 100,
                              make_patch(entries, base)))


def wait_for(qapp, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert predicate(), 'Asynchronous tape did not reach the expected display'


def trade(sequence, price, *, side='BUY', outcome='UNRESOLVED', salience=1, quantity=1, direction=0):
    return OrderFlowTradePrint(
        sequence=sequence, event_time_ms=1700000000000 + sequence * 1000,
        received_monotonic=sequence / 10, price=price, quantity=quantity,
        notional=price * quantity, aggressor_side=side, normal_notional=price,
        rpi_notional=0, relative_size=1, salience_class=salience, outcome=outcome, outcome_direction=direction,
    )


def snapshot(trades, sequence=1):
    return OrderFlowSnapshot(
        symbol='ETHUSDT', sequence=sequence, generated_monotonic=10,
        ready=True, live=True, bbo_source='depth', depth_age_seconds=0,
        bbo_age_seconds=0, trade_age_seconds=0,
        recent_prints=tuple(trades), large_trade_threshold=100,
    )


@pytest.mark.parametrize('price,previous,changed', [
    ('2248.73', '2247.74', {3, 4, 5, 6}),  # Emphasize the decimal point and entire suffix.
    ('2248.86', '2248.66', {5, 6}),  # Include the unchanged trailing digit.
    ('2248.75', '2248.75', set()),
    ('2248.75', '', set(range(7))),
    ('99.90', '100.00', set(range(5))),
    ('100.00', '99.90', set(range(6))),
    ('100.01', '99.99', set(range(6))),
    ('500.00', '1500.00', set(range(6))),  # A removed leading place is still a price change.
    ('9.01', '109.01', set(range(4))),
    ('100000', '99999', {0, 1, 2, 3, 4, 5}),
    ('1234.50', '1234.40', {5, 6}),
    ('0.00000012', '0.00000010', {9}),
    ('0.00000525', '0.00000475', {7, 8, 9}),
    ('1.200', '1.2', set()),
    ('0.1', '0.01', {2}),
])
def test_price_emphasis_starts_at_first_differing_decimal_place(price, previous, changed):
    mask = _TradePriceDelegate.emphasis_mask(price, previous)
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
    set_trades(model, [trade(1, 2248.7, outcome=outcome)])
    index = model.index(0, 2)
    assert index.data() == symbol
    assert explanation in index.data(Qt.ItemDataRole.ToolTipRole)
    assert 'Price: 2248.70' in index.data(Qt.ItemDataRole.ToolTipRole)
    assert index.data(Qt.ItemDataRole.TextAlignmentRole) == int(Qt.AlignmentFlag.AlignCenter)


@pytest.mark.parametrize('width,price,previous,side', [
    (120, 2248.73, 2247.74, 'BUY'),
    (120, 2248.86, 2248.66, 'SELL'),
    (45, 0.00000012, 0.00000010, 'BUY'),  # Narrow cell keeps emphasis when elided.
])
def test_price_painter_uses_one_side_color_and_clips_to_the_cell(qapp, width, price, previous, side):
    model = _TradesTapeModel()
    model.decimals = 8 if price < 1 else 2
    set_trades(model, [trade(2, price, side=side), trade(1, previous)])
    delegate = _TradePriceDelegate()
    image = QtGui.QImage(width + 20, 36, QtGui.QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.black)
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = QtCore.QRect(10, 4, width, 28)
    painter = QtGui.QPainter(image)
    try:
        delegate.paint(painter, option, model.index(0, 0))
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
    spans = [span for span in layout.formats() if span.format.fontWeight() == QtGui.QFont.Weight.Bold]
    color = model.index(0, 0).data(Qt.ItemDataRole.ForegroundRole)
    assert all(span.format.foreground().color().getRgb()[:3] == color.getRgb()[:3] for span in layout.formats())
    for span in layout.formats():
        opacity = span.format.foreground().color().alphaF()
        if span in spans:
            assert opacity == 1.0
        else:
            assert 0.49 <= opacity <= 0.51
    if width == 120:
        positions = {i for span in spans for i in range(span.start, span.start + span.length)}
        assert positions == ({3, 4, 5, 6} if side == 'BUY' else {5, 6})
    else:
        assert layout.text().startswith('…') and layout.text().endswith('12')
        assert line.naturalTextWidth() <= width - 9
        assert [(span.start, span.length) for span in spans] == [(len(layout.text()) - 1, 1)]


def test_layout_cache_is_bounded_and_reuses_shaped_text(qapp):
    delegate = _TradePriceDelegate()
    regular = typography_font(TextRole.TABLE_VALUE, state='trade_price_regular')
    changed = typography_font(TextRole.TABLE_VALUE, state='trade_price_changed')
    device = QtGui.QImage(120, 28, QtGui.QImage.Format.Format_ARGB32)
    color = QtGui.QColor('#00C56A')
    for value in range(delegate.LAYOUT_CACHE_LIMIT + 10):
        text = f'{value}.00'
        delegate._layout(text, (False,) * len(text), regular, changed, device, color)
    assert len(delegate._layouts) == delegate.LAYOUT_CACHE_LIMIT
    assert all(key[0] != '0.00' for key in delegate._layouts)
    text = f'{delegate.LAYOUT_CACHE_LIMIT + 9}.00'
    before = delegate._layout(text, (False,) * len(text), regular, changed, device, color)
    after = delegate._layout(text, (False,) * len(text), regular, changed, device, color)
    assert before is after


def test_prepend_and_outcome_updates_keep_model_rows_and_neighbor_comparison(qapp):
    model = _TradesTapeModel()
    model.decimals = 2
    rows = [trade(3, 2248.73), trade(2, 2247.74), trade(1, 2247.74)]
    set_trades(model, rows)
    resets, updates = [], []
    model.modelReset.connect(lambda: resets.append(True))
    model.dataChanged.connect(lambda first, last, roles: updates.append((first.row(), first.column(), last.column())))
    rows = [trade(4, 2248.86), *rows[:2]]
    set_trades(model, rows)
    assert [row[0].sequence for row in model.rows] == [4, 3, 2]
    assert not resets
    assert _TradePriceDelegate.emphasis_mask(model.index(1, 0).data(), model.index(2, 0).data()) == (
        False, False, False, True, True, True, True,
    )
    set_trades(model, [replace(rows[0], outcome='FOLLOW_THROUGH'), *rows[1:]])
    assert model.index(0, 2).data() == '✓'
    assert updates == [(0, 2, 2)] and not resets


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
    wait_for(qapp, lambda: [row[0].sequence for row in widget.model.rows] == [3, 1])
    assert [row[0].sequence for row in widget.model.rows] == [3, 1]
    mask = _TradePriceDelegate.emphasis_mask(widget.model.index(0, 0).data(), widget.model.index(1, 0).data())
    assert {i for i, changed in enumerate(mask) if changed} == {4, 5}
    widget.set_mode('ALL', emit=False)
    widget._refresh_table()
    wait_for(qapp, lambda: [row[0].sequence for row in widget.model.rows] == [3, 2, 1])
    assert [row[0].sequence for row in widget.model.rows] == [3, 2, 1]
    mask = _TradePriceDelegate.emphasis_mask(widget.model.index(0, 0).data(), widget.model.index(1, 0).data())
    assert {i for i, changed in enumerate(mask) if changed} == {5}
    widget.set_order_flow_snapshot(snapshot([*rows[:-1], replace(rows[-1], outcome='FOLLOW_THROUGH')], sequence=2))
    widget._refresh_table()
    wait_for(qapp, lambda: widget.model.index(0, 2).data() == '✓' and not widget._pending_tape_frame)
    assert widget.model.index(0, 2).data() == '✓'
    widget.set_mode('LARGE', emit=False)
    widget._refresh_table()
    wait_for(qapp, lambda: [row[0].sequence for row in widget.model.rows] == [3, 1]
             and widget.model.index(0, 2).data() == '✓' and not widget._pending_tape_frame)
    assert widget.model.index(0, 2).data() == '✓'
    widget.close()
    widget.deleteLater()


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
@pytest.mark.parametrize('ratio', [1, 2])
@pytest.mark.parametrize('hinting', ['full', 'none'])
def test_changed_digit_is_visibly_brighter_in_rendered_pixels(qapp, monkeypatch, side, ratio, hinting):
    import nightwatch.orderbook.orderbook_ui as tape
    controller = TypographyController()
    controller.configure({TextRole.TABLE_VALUE: {'hinting': hinting}}, notify=False)
    monkeypatch.setattr(tape, 'typography_font', controller.font)

    def digit_ink(previous):
        model = _TradesTapeModel()
        model.decimals = 1
        set_trades(model, [trade(2, 83459.9, side=side), trade(1, previous)])
        delegate = _TradePriceDelegate()
        image = QtGui.QImage(128 * ratio, 28 * ratio, QtGui.QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.black)
        option = QtWidgets.QStyleOptionViewItem()
        option.rect = QtCore.QRect(0, 0, 128, 28)
        painter = QtGui.QPainter(image)
        try:
            delegate.paint(painter, option, model.index(0, 0))
        finally:
            painter.end()
        _, line = next(reversed(delegate._layouts.values()))
        origin = 123 - line.naturalTextWidth()
        left = math.floor((origin + line.cursorToX(6)[0]) * ratio)
        right = math.ceil((origin + line.cursorToX(7)[0]) * ratio)
        pixels = [image.pixelColor(x, y) for x in range(left, right) for y in range(image.height())]
        values = [color.green() if side == 'BUY' else color.red() for color in pixels]
        return sum(values), max(values)

    normal_mass, normal_peak = digit_ink(83459.9)
    changed_mass, changed_peak = digit_ink(83459.8)
    assert changed_mass >= normal_mass * 1.8
    assert changed_peak >= normal_peak * 1.6


def test_cached_price_text_keeps_buy_and_sell_colors_separate(qapp):
    delegate = _TradePriceDelegate()
    regular = typography_font(TextRole.TABLE_VALUE, state='trade_price_regular')
    changed = typography_font(TextRole.TABLE_VALUE, state='trade_price_changed')
    image = QtGui.QImage(128, 28, QtGui.QImage.Format.Format_ARGB32)
    text = '83459.9'
    mask = delegate.emphasis_mask(text, '83459.8')
    buy = QtGui.QColor('#00C56A')
    sell = QtGui.QColor('#F0143E')
    buy_layout = delegate._layout(text, mask, regular, changed, image, buy)
    sell_layout = delegate._layout(text, mask, regular, changed, image, sell)
    assert buy_layout is not sell_layout
    assert len(delegate._layouts) == 2
    for layout, expected in ((buy_layout[0], buy), (sell_layout[0], sell)):
        assert all(span.format.foreground().color().getRgb()[:3] == expected.getRgb()[:3]
                   for span in layout.formats())


@pytest.mark.parametrize('text,bright', [
    ('0.2865753', {0, 1}), ('2.1916195', {0, 1}),
    ('0.00000001', {0, 1}), ('250', {0, 1, 2}),
    ('$12.34K', {1, 2, 3, 6}), ('$1,250.00', {1, 2, 3, 4, 5, 6}),
])
def test_size_hierarchy_matches_reference_whole_units_and_quieter_fractions(text, bright):
    assert {i for i, value in enumerate(_TradePriceDelegate.amount_emphasis_mask(text)) if value} == bright


@pytest.mark.parametrize('direction,side,outcome,expected', [
    (1, 'BUY', 'FOLLOW_THROUGH', 'buy'),
    (-1, 'SELL', 'FOLLOW_THROUGH', 'sell'),
    (-1, 'BUY', 'REJECTED', 'sell'),
    (1, 'SELL', 'REJECTED', 'buy'),
    (1, 'BUY', 'REJECTED', 'buy'),  # Weak upward movement isn't a reversal.
    (0, 'BUY', 'REJECTED', 'muted'),
    (0, 'SELL', 'UNRESOLVED', 'muted'),
])
def test_result_color_uses_observed_direction(qapp, direction, side, outcome, expected):
    model = _TradesTapeModel()
    set_trades(model, [trade(1, 100, direction=direction, side=side, outcome=outcome)])
    assert model.index(0, 2).data(Qt.ItemDataRole.ForegroundRole) == getattr(model, expected)


@pytest.mark.parametrize('width', [220, 300, 480])
def test_tape_keeps_four_columns_result_and_independent_quantity_switch(qapp, width):
    from PySide6.QtTest import QTest
    widget = TradesTapeWidget({})
    widget.set_market('ETHUSDT', tick_size=0.01)
    widget.resize(width, 250)
    widget.show()
    widget.set_panel_active(True)
    widget.set_order_flow_snapshot(snapshot([trade(1, 100, quantity=0.00000001, outcome='FOLLOW_THROUGH', direction=1)]))
    widget._refresh_table()
    wait_for(qapp, lambda: widget.model.rowCount() == 1)
    assert widget.model.columnCount() == 4
    assert [widget.model.headerData(i, Qt.Orientation.Horizontal) for i in range(4)] == ['PRICE', 'SIZE', 'TAG', 'TIME']
    assert not hasattr(widget, 'title')
    assert widget.table.horizontalHeader().isVisible()
    assert not widget.table.isColumnHidden(2)
    assert widget.model.index(0, 1).data() == '$0.000001'
    rect = widget.table.visualRect(widget.model.index(0, 2))
    assert rect.width() >= 28 and widget.table.viewport().rect().contains(rect)
    units = []
    widget.value_mode_changed.connect(units.append)
    QTest.mouseClick(widget.units_button, Qt.MouseButton.LeftButton)
    widget._refresh_table()
    assert widget.value_mode() == 'base'
    assert widget.model.index(0, 0).data() == '100.00'
    assert widget.model.index(0, 1).data() == '0.00000001'
    assert widget.model.index(0, 2).data() == '✓'
    assert widget.units_button.text() == 'Qty'
    assert units == ['base']
    QTest.mouseClick(widget.units_button, Qt.MouseButton.LeftButton)
    widget._refresh_table()
    assert widget.value_mode() == 'quote'
    assert widget.units_button.text() == 'Value'
    widget.close()
    widget.deleteLater()


def test_size_painter_keeps_whole_units_bright_and_fractions_dim(qapp):
    model = _TradesTapeModel()
    model.value_mode = 'base'
    set_trades(model, [trade(1, 100, quantity=0.2865753)])
    delegate = _TradePriceDelegate(amount=True)
    image = QtGui.QImage(128, 28, QtGui.QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.black)
    option = QtWidgets.QStyleOptionViewItem()
    option.rect = image.rect()
    painter = QtGui.QPainter(image)
    try:
        delegate.paint(painter, option, model.index(0, 1))
    finally:
        painter.end()
    layout, _line = next(reversed(delegate._layouts.values()))
    bright = {i for span in layout.formats() if span.format.fontWeight() == QtGui.QFont.Weight.Bold
              for i in range(span.start, span.start + span.length)}
    assert bright == {0, 1}
    assert layout.formats()[-1].format.foreground().color().alphaF() < 0.51


@pytest.mark.parametrize('midpoint,outcome,direction', [(100.02, 'FOLLOW_THROUGH', 1),
    (100.005, 'REJECTED', 1), (100, 'REJECTED', 0), (99.98, 'REJECTED', -1)])
def test_outcome_snapshot_carries_actual_direction_for_result_color(midpoint, outcome, direction):
    from nightwatch.orderbook.backend import OrderFlowAnalyzer
    analyzer = OrderFlowAnalyzer('ETHUSDT', tick_size=0.01)
    item = replace(trade(1, 100, side='buy'), received_monotonic=1,
                   reference_midpoint=100, outcome_threshold=0.01)
    analyzer._recent_prints.append(item)
    analyzer._unresolved_prints.append(item)
    analyzer._update_print_outcomes(1 + analyzer.PRINT_OUTCOME_SECONDS + 0.01,
                                    midpoint - 0.005, midpoint + 0.005)
    result = analyzer._recent_prints_snapshot(1 + analyzer.PRINT_OUTCOME_SECONDS + 0.01)[0]
    assert result.outcome == outcome
    assert result.outcome_direction == direction
    analyzer.reset('ETHUSDT', tick_size=0.01)
    assert not analyzer._print_outcome_directions

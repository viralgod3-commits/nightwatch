from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtGui, QtWidgets, QtTest
from PySide6.QtCore import Qt

from nightwatch.models import Candle
from nightwatch.leadership import (
    HOUR, HISTORY_HOURS, LeadershipTimelineWidget, RotationScannerWidget, SectorOverviewWidget,
    _sector_overview_classify, _sector_overview_closed_window_ready,
    _sector_overview_tail_limit, _sector_overview_map_candles, _prepare_leaders,
    _SectorAnalysis, _SectorOverviewVolumeBars, DETAIL_ROLE, LEADER_COLORS,
    _prepare_rotation, RotationBubbleChart,
)
from nightwatch.trading.trading_ui import TradingWorkspace, AccountDataTable


def candles(count, step=HOUR, end=None):
    end = end or count * step
    return [Candle((stamp - step) / 1000, 100, 101, 99, 100, 10, 1000)
            for stamp in range(end - (count - 1) * step, end + 1, step)]


@pytest.mark.parametrize('symbol,metadata,expected', [
    ('1000PEPEUSDT', {}, 'Memes'), ('1000BONKUSDT', {}, 'Memes'),
    ('1INCHUSDT', {}, 'DeFi'), ('UNKNOWNUSDT', {'underlyingSubType': ['chain infrastructure']}, 'Infrastructure'),
    ('UNKNOWNUSDT', {'underlyingSubType': ['chain']}, None),
    ('UNKNOWNUSDT', {'underlyingSubType': ['AI & Big Data']}, 'AI'),
])
def test_sector_classification_handles_multipliers_and_tag_boundaries(symbol, metadata, expected):
    assert _sector_overview_classify(symbol, metadata) == expected


def test_internal_history_hole_is_not_ready_and_is_backfilled():
    rows = candles(64)
    rows.pop(45)
    assert not _sector_overview_closed_window_ready(rows, 64 * HOUR, HOUR, 56)
    assert _sector_overview_tail_limit(rows, 64 * HOUR, HOUR, 96, 56) >= 19
    assert _sector_overview_closed_window_ready(candles(64), 64 * HOUR, HOUR, 56)


def test_daily_gap_is_not_counted_as_twenty_day_coverage():
    day = 24 * HOUR
    rows = candles(21, day)
    rows.pop(10)
    assert not _sector_overview_closed_window_ready(rows, 21 * day, day, 20)


def test_twenty_day_mean_excludes_forming_candle_and_gapped_pairs():
    day = 24 * HOUR
    rows = candles(21, day)
    forming = Candle(21 * day / 1000, 999, 999, 999, 999, 1, 1)
    gapped = rows[:]
    gapped.pop(10)
    model = _SectorAnalysis({'symbols': ['GOOD', 'GAP'], 'daily': {'GOOD': rows + [forming], 'GAP': gapped},
                             'end_hour': 21 * day, 'tickers': {'GOOD': {'c': '110'}, 'GAP': {'c': '110'}}})
    assert model._daily_means() == {'GOOD': 100}
    assert model._above_20d(model._daily_means()) == (1, 1)


def test_sector_history_uses_fixed_cohort_instead_of_changing_members():
    end = 60 * HOUR
    model = _SectorAnalysis({'symbols': ['FULL', 'SHORT'], 'sectors': {'FULL': 'AI', 'SHORT': 'AI'},
                             'hourly': {'BTCUSDT': _sector_overview_map_candles(candles(60, end=end), HOUR)},
                             'end_hour': end})
    model._relative_value = lambda symbol, frame, at: 1 if symbol == 'FULL' else (100 if at == end else None)
    values, covered = model._fixed_history_cohort('AI', '1h', 24, relative=True)
    assert covered == 1 and values == [1] * 24
    model._volume_value = lambda *a: None
    values, covered = model._fixed_history_cohort('AI', '1h', 12, relative=False)
    assert covered == 0 and values == [None] * 12


def test_volume_history_preserves_unknown_as_distinct_from_zero(qapp):
    widget = _SectorOverviewVolumeBars()
    widget.set_values([None, 0, 100])
    assert widget.values == [None, 0, 100]
    widget.resize(120, 50)
    assert not widget.grab().isNull()  # Paint the unknown marker and genuine zero.
    widget.deleteLater()


def test_week_span_has_replay_and_warmup_history():
    assert HISTORY_HOURS >= 168 + 24 + 1


def test_account_table_numeric_sort_keeps_payloads_and_selection(qapp):
    table = AccountDataTable(('ASSET', 'WALLET'))
    table.setHorizontalHeaderLabels(('ASSET', 'WALLET'))
    rows = [(('A', '9.00'), {'asset': 'A', 'walletBalance': '9', 'marker': 'old-a'}),
            (('B', '100.00'), {'asset': 'B', 'walletBalance': '100', 'marker': 'old-b'})]
    TradingWorkspace._fill_table(table, rows)
    table.sortItems(1, Qt.SortOrder.DescendingOrder)
    assert table.item(0, 0).text() == 'B'
    table.selectRow(0)
    refreshed = [(values, {**payload, 'marker': 'new-' + payload['asset']}) for values, payload in rows]
    TradingWorkspace._fill_table(table, refreshed)
    assert table.item(0, 0).data(Qt.ItemDataRole.UserRole)['marker'] == 'new-B'
    changed = [(('A', '900.00'), {'asset': 'A', 'walletBalance': '900'}), rows[1]]
    TradingWorkspace._fill_table(table, changed)
    assert table.item(table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)['asset'] == 'B'
    assert table.updatesEnabled()
    table.deleteLater()


def leader_result(symbol='TESTUSDT'):
    return _prepare_leaders({'cursor': 24 * HOUR, 'hours': 24, 'symbols': [symbol],
                             'series': {}, 'spot_series': {}, 'categories': {}, 'query': '',
                             'sort_mode': 'state', 'tickers': {}, 'valid_symbols': set()})


def test_leaders_missing_spark_endpoints_do_not_freeze_updates(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    widget.symbols = ['TESTUSDT']
    monkeypatch.setattr(widget, '_render_changes', lambda *a: None)
    monkeypatch.setattr(widget, '_render_facts', lambda *a: None)
    monkeypatch.setattr(widget, '_schedule_details', lambda *a: None)
    widget._render(leader_result())
    assert widget.table.updatesEnabled()
    widget.shutdown()
    widget.deleteLater()


def test_leaders_restore_painting_after_render_exception(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    widget.symbols = ['TESTUSDT']
    monkeypatch.setattr(widget, 'identity', lambda *a: (_ for _ in ()).throw(ValueError('bad data')))
    with pytest.raises(ValueError):
        widget._render(leader_result())
    assert widget.table.updatesEnabled()
    widget.shutdown()
    widget.deleteLater()


def test_live_leader_price_updates_without_history_analysis(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    widget.active = True
    widget.symbols = ['TESTUSDT']
    widget.table.setColumnCount(11)
    widget.table.setRowCount(1)
    symbol = QtWidgets.QTableWidgetItem('TEST')
    symbol.setData(Qt.ItemDataRole.UserRole, 'TESTUSDT')
    widget.table.setItem(0, 1, symbol)
    widget.table.setItem(0, 3, QtWidgets.QTableWidgetItem('100'))
    monkeypatch.setattr(widget, 'render', lambda: pytest.fail('A live price update must not recompute histories'))
    widget.update_tickers([{'s': 'TESTUSDT', 'c': '110'}])
    assert '110' in widget.table.item(0, 3).text()
    widget.replay.blockSignals(True)
    widget.replay.setValue(20)
    widget.update_tickers([{'s': 'TESTUSDT', 'c': '120'}])
    assert widget.replay.value() == widget.replay.minimum() == widget.replay.maximum() == 24
    assert '120' in widget.table.item(0, 3).text()
    widget.shutdown()
    widget.deleteLater()


def test_leaders_ignore_legacy_replay_and_keep_live_cursor(qapp, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / 'legacy.ini'), QtCore.QSettings.Format.IniFormat)
    settings.setValue('markets/leadership/replay', 8)
    settings.setValue('markets/leadership/span', 168)
    widget = LeadershipTimelineWidget({})
    try:
        widget.end = 30 * HOUR
        widget.restore_ui_state(settings)
        assert widget.cursor_end() == widget.end
        assert widget.span.currentData() == 24
        widget.save_ui_state(settings)
        assert not settings.contains('markets/leadership/replay')
    finally:
        widget.shutdown()
        widget.deleteLater()


@pytest.mark.parametrize('state', ['Leading', 'Improving', 'Cooling', 'Lagging'])
def test_leader_state_color_matches_row_marker_badge_and_score(qapp, state):
    widget = LeadershipTimelineWidget({})
    try:
        widget.symbols = ['TESTUSDT']
        prepared = leader_result()
        prepared['metrics']['TESTUSDT'].update(state=state, score=35, usd1=-1)
        widget._render(prepared)
        row = widget.table.item(0, 1).data(DETAIL_ROLE)
        badge = widget.table.item(0, 8).data(DETAIL_ROLE)
        score = widget.table.item(0, 7).data(DETAIL_ROLE)
        returns = widget.table.item(0, 4).data(DETAIL_ROLE)
        assert row['state_color'] == badge['foreground'] == score['foreground'] == LEADER_COLORS[state]
        assert returns['foreground'] == widget.theme['red']
        assert score['background'] != returns['background']
    finally:
        widget.shutdown()
        widget.deleteLater()


def test_rotation_does_not_apply_leader_price_columns(qapp):
    widget = RotationScannerWidget({})
    widget._refresh_live_prices()  # Separate RS/RVol table schema.
    widget.shutdown()
    widget.deleteLater()


def test_rotation_keeps_quiet_and_neutral_coins_from_completed_history(qapp):
    end = 30 * HOUR
    symbols = [f'QUIET{i}USDT' for i in range(8)]
    series = {'BTCUSDT': dict(zip(range(HOUR, end + HOUR, HOUR), candles(30)))}
    for index, symbol in enumerate(symbols):
        rows = candles(30)
        for n, row in enumerate(rows):
            close = 100 * (1 + index * n * n / 1_000_000)
            opened = 100 * (1 + index * (n - 1) ** 2 / 1_000_000)
            rows[n] = Candle(row.time, opened, max(opened, close) + 1,
                             min(opened, close) - 1, close, 10, 1_000_000)
        series[symbol] = dict(zip(range(HOUR, end + HOUR, HOUR), rows))
    prepared = _prepare_rotation(dict(cursor=end, hours=4, symbols=symbols,
                                     series=series, spot_series={}))
    assert len(prepared['points']) == len(symbols)
    assert all(abs(point['x']) < .5 and abs(point['y']) < .2 for point in prepared['points'])
    original = deepcopy(prepared)
    widget = RotationScannerWidget({})
    try:
        widget.end, widget.symbols = end, symbols
        widget.candidate_mode.setCurrentIndex(widget.candidate_mode.findData('all'))
        widget._prepared_analysis = prepared
        widget._render(prepared)
        widget.resize(1440, 900)
        widget.show()
        qapp.processEvents()
        widget.bubbles.grab()
        heads = {point['symbol'] for point, _at, _radius in widget.bubbles._hits
                 if point['_sample_index'] == 3}
        assert heads == set(symbols)
        assert widget.table.rowCount() == len(symbols)
        neutral = next(group for group in widget.bubbles._geometry
                       if group['point']['symbol'] == symbols[0])
        assert neutral['positions'][-1] == widget.bubbles.map_point(0, 0)
        chosen = QtTest.QSignalSpy(widget.bubbles.chosen)
        QtTest.QTest.mouseClick(widget.bubbles, Qt.MouseButton.LeftButton,
                               pos=neutral['positions'][-1].toPoint())
        assert chosen.count() == 1 and chosen.at(0) == [symbols[0]]
        assert widget.selected == symbols[0]
        # Existing page filters still constrain both the table and the map.
        widget.search.setText(symbols[-1])
        widget._render_view(prepared)
        assert [point['symbol'] for point in widget.bubbles.points] == [symbols[-1]]
        assert widget.table.rowCount() == 1
        assert prepared == original
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()


def test_rotation_dense_universe_keeps_every_bubble_on_canvas(qapp):
    points = [dict(symbol=f'COIN{i:03}USDT', x=.02 + i / 10_000, y=.01 + i / 100_000,
                   trail=[(.01, .01), (.015, .009), (.018, .0095), (.02 + i / 10_000, .01 + i / 100_000)],
                   volume=100 + i, quadrant='Leading') for i in range(160)]
    chart = RotationBubbleChart()
    try:
        chart.resize(750, 366)
        chart.set_points(points, selected=points[0]['symbol'])
        chart.grab()
        heads = [(point, at) for point, at, _radius in chart._hits if point['_sample_index'] == 3]
        assert {point['symbol'] for point, _at in heads} == {point['symbol'] for point in points}
        assert all(chart.plot_rect().contains(at) for _point, at in heads)
        assert chart.points == points
        chosen = QtTest.QSignalSpy(chart.chosen)
        QtTest.QTest.keyClick(chart, Qt.Key.Key_Right)
        assert chosen.at(0) == [points[1]['symbol']]
    finally:
        chart.deleteLater()


def test_leader_trading_views_reuse_snapshot_and_preserve_selection(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    try:
        widget.symbols = ['TESTUSDT']
        prepared = leader_result()
        prepared['metrics']['TESTUSDT'].update(rs1=1.25, rs4=-2.5, rs24=3.75, delta=.5,
            rvol=1.8, volume_share=12.5, share_delta=-.25, spot_share=35, spot_delta=.75)
        original = deepcopy(prepared)
        widget._render(prepared)
        items = [widget.table.item(0, column) for column in range(widget.table.columnCount())]
        monkeypatch.setattr(widget, 'render', lambda: pytest.fail('A view preset must reuse the snapshot'))
        monkeypatch.setattr(widget, '_request_details', lambda: pytest.fail('A view preset must not request details'))
        widget.view_selector.setCurrentIndex(widget.view_selector.findData('strength'))
        assert [widget.table.item(0, column).text() for column in (11, 12, 13, 14)] == [
            '+1.25%', '-2.50%', '+3.75%', '+0.50 pp']
        assert all(not widget.table.isColumnHidden(column) for column in (11, 12, 13, 14))
        assert all(widget.table.isColumnHidden(column) for column in (15, 16, 17, 18, 19))
        header = widget.table.horizontalHeader()
        assert [header.logicalIndex(position) for position in range(8)] == [0, 1, 2, 3, 11, 12, 13, 14]
        widget.view_selector.setCurrentIndex(widget.view_selector.findData('participation'))
        assert [widget.table.item(0, column).text() for column in (15, 16, 17, 18, 19)] == [
            '1.80×', '12.50%', '-0.25 pp', '35.00%', '+0.75 pp']
        assert all(not widget.table.isColumnHidden(column) for column in (15, 16, 17, 18, 19))
        assert widget.table.isColumnHidden(11)
        assert widget.table.item(0, 12).data(DETAIL_ROLE)['foreground'] == widget.theme['red']
        assert widget.selected == 'TESTUSDT'
        assert all(widget.table.item(0, column) is item for column, item in enumerate(items))
        assert prepared == original
    finally:
        widget.shutdown()
        widget.deleteLater()


def test_leader_context_uses_selected_cache_and_expires_live_spread(qapp, monkeypatch):
    widget = LeadershipTimelineWidget({})
    clock = [1000.0]
    monkeypatch.setattr('nightwatch.leadership.time.monotonic', lambda: clock[0])
    try:
        end = 24 * HOUR
        widget.end, widget.symbols = end, ['TESTUSDT']
        widget._render(leader_result())
        widget.details[('TESTUSDT', end, True)] = dict(at=1000, book_at=1000,
            oi=[{'timestamp': end - HOUR, 'sumOpenInterest': '100'},
                {'timestamp': end, 'sumOpenInterest': '110'}],
            funding=[{'fundingTime': end - 4 * HOUR, 'fundingRate': '.00005'},
                     {'fundingTime': end, 'fundingRate': '.00005'}],
            book={'bids': [['99.99', '100']], 'asks': [['100.01', '100']]})
        monkeypatch.setattr(widget, '_request_details', lambda: pytest.fail('Opening context must reuse the cache'))
        widget.resize(1440, 900)
        widget.show()
        qapp.processEvents()
        before = widget.main_content.geometry()
        widget.context_button.click()
        qapp.processEvents()
        assert widget.main_content.geometry() == before
        assert widget.trade_values['funding'].text() == '+0.010%'
        assert widget.trade_values['oi'].text() == '+10.00%'
        assert widget.trade_values['spread'].text() == '0.020%'
        assert widget.trade_values['rs1'].text() == '—'
        assert widget.context_expiry_timer.isActive()
        clock[0] += 91
        widget.context_expiry_timer.timeout.emit()
        assert widget.trade_values['spread'].text() == '—'
        assert not widget.context_expiry_timer.isActive()
        widget.metrics['OTHERUSDT'] = dict(price=20)
        widget.select_symbol('OTHERUSDT')
        assert widget.context_symbol.text() == 'OTHER'
        assert all(widget.trade_values[key].text() == '—' for key in ('funding', 'oi', 'spread'))
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize('view,custom', [('strength', False), ('participation', False), ('participation', True)])
def test_leader_trading_view_and_custom_columns_survive_restart(qapp, tmp_path, view, custom):
    settings = QtCore.QSettings(str(tmp_path / 'leaders.ini'), QtCore.QSettings.Format.IniFormat)
    widget, restored = LeadershipTimelineWidget({}), LeadershipTimelineWidget({})
    try:
        widget.view_selector.setCurrentIndex(widget.view_selector.findData(view))
        if custom:
            widget.column_actions[11].setChecked(True)
        widget.context_button.setChecked(True)
        widget.save_ui_state(settings)
        restored.restore_ui_state(settings)
        assert restored.view_selector.currentData() == ('custom' if custom else view)
        assert restored.context_button.isChecked()
        assert [restored.table.isColumnHidden(column) for column in range(20)] == [
            widget.table.isColumnHidden(column) for column in range(20)]
        assert [restored.table.horizontalHeader().logicalIndex(position) for position in range(20)] == [
            widget.table.horizontalHeader().logicalIndex(position) for position in range(20)]
    finally:
        widget.shutdown()
        restored.shutdown()
        widget.deleteLater()
        restored.deleteLater()


@pytest.mark.parametrize('workspace', [LeadershipTimelineWidget, SectorOverviewWidget, RotationScannerWidget])
@pytest.mark.parametrize('width,height', [(800, 900), (1080, 900), (1920, 900), (2560, 900), (960, 540), (1280, 720)])
def test_market_views_fill_viewport_with_controls_on_left(qapp, workspace, width, height):
    widget = workspace({})
    try:
        widget.resize(width, height)
        widget.show()
        for _ in range(5):
            qapp.processEvents()
        assert widget.size() == QtCore.QSize(width, height)
        assert widget.refresh_button.isVisible()
        assert widget.controls_scroll.horizontalScrollBar().maximum() == 0
        assert widget.controls_scroll.geometry().right() < widget.main_content.geometry().left()
        assert widget.main_content.width() > width * .7
        assert widget.main_content.height() > widget.height() * .95
        assert widget.table.isVisible()
        redundant_titles = {
            'Leaders', 'Sectors', 'Rotation', 'Sector participation',
            'Sector leadership', 'Improving candidates', 'Rotation candidates',
        }
        for label in widget.findChildren(QtWidgets.QLabel):
            if label.isVisible() and label.text():
                assert label.text() not in redundant_titles
                assert not label.text().casefold().startswith(('find relative', 'find broad', 'find developing'))
                assert label.palette().color(QtGui.QPalette.ColorRole.WindowText).lightnessF() > .2, label.text()
            if label.isVisible() and label.buddy() is not None:
                assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
        assert not widget.grab().isNull()
        if isinstance(widget, (SectorOverviewWidget, RotationScannerWidget)):
            assert widget.detail_drawer.isHidden()
            before = widget.main_content.geometry()
            widget.context_button.click()
            qapp.processEvents()
            assert widget.detail_drawer.isVisible()
            assert widget.main_content.geometry() == before
            assert widget.width() == width
            assert widget.detail_drawer.geometry().right() < widget.main_content.width()
            widget.context_button.click()
            assert widget.detail_drawer.isHidden()
        else:
            assert widget.table.height() > widget.height() * .95
            assert not any(slider.isVisible() for slider in widget.findChildren(QtWidgets.QSlider))
        widget.resize(960, 540)
        for _ in range(5):
            qapp.processEvents()
        assert widget.size() == QtCore.QSize(960, 540)
        refresh_bottom = widget.refresh_button.mapTo(widget, widget.refresh_button.rect().bottomLeft()).y()
        assert refresh_bottom < widget.height()
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()


def test_market_filter_and_disclosure_settings_remain_independent(qapp, tmp_path):
    settings = QtCore.QSettings(str(tmp_path / 'markets.ini'), QtCore.QSettings.Format.IniFormat)
    pages = [LeadershipTimelineWidget({}), SectorOverviewWidget({}), RotationScannerWidget({})]
    restored = []
    try:
        leaders, sectors, rotation = pages
        leaders.span.setCurrentIndex(leaders.span.findData(24))
        leaders.liquidity.setCurrentIndex(leaders.liquidity.findData(100_000_000))
        sectors.context_button.setChecked(True)
        sectors.timeframe_buttons['1d'].click()
        sectors.liquidity.setCurrentIndex(sectors.liquidity.findData(50_000_000))
        rotation.span.setCurrentIndex(rotation.span.findData(12))
        rotation.liquidity.setCurrentIndex(rotation.liquidity.findData(5_000_000))
        rotation.compare_toggle.setChecked(True)
        for page in pages:
            page.save_ui_state(settings)
        settings.sync()
        restored = [LeadershipTimelineWidget({}), SectorOverviewWidget({}), RotationScannerWidget({})]
        for page in restored:
            page.restore_ui_state(settings)
        leaders, sectors, rotation = restored
        assert (leaders.span.currentData(), sectors.timeframe, rotation.span.currentData()) == (24, '1d', 12)
        assert tuple(page.liquidity.currentData() for page in restored) == (100_000_000, 50_000_000, 5_000_000)
        assert sectors.context_button.isChecked()
        assert not rotation.context_button.isChecked()
        assert rotation.compare_toggle.isChecked() and not rotation.comparison_panel.isHidden()
        rotation.reset_button.click()
        assert (rotation.span.currentData(), rotation.liquidity.currentData(), rotation.limit.currentData()) == (4, 20_000_000, 0)
        assert leaders.liquidity.currentData() == 100_000_000
        assert sectors.liquidity.currentData() == 50_000_000
    finally:
        for page in pages + restored:
            page.shutdown()
            page.deleteLater()


def test_sector_table_activation_selects_matching_detail(qapp):
    widget = SectorOverviewWidget({})
    try:
        widget.table.setRowCount(1)
        widget.table.setItem(0, 0, QtWidgets.QTableWidgetItem('DeFi'))
        widget.table.cellActivated.emit(0, 0)
        assert widget.selected_sector == 'DeFi'
    finally:
        widget.shutdown()
        widget.deleteLater()


def test_sector_table_selection_follows_sector_when_rank_changes(qapp):
    widget = SectorOverviewWidget({})
    try:
        metrics = {sector: {'members': 0} for sector in widget.tiles}
        metrics['AI'] = {'members': 2, 'performance': 1.0}
        metrics['DeFi'] = {'members': 2, 'performance': 2.0}
        performances = {frame: {'AI': 1.0, 'DeFi': 2.0} for frame in ('15m', '1h', '4h', '1d')}
        widget._render_table(metrics, performances)
        assert widget.table.currentRow() == 1
        assert widget.table.item(1, 0).text() == widget.selected_sector == 'AI'
        metrics['AI']['performance'] = 3.0
        widget._render_table(metrics, performances)
        assert widget.table.currentRow() == 0
        assert widget.table.item(0, 0).text() == widget.selected_sector == 'AI'
    finally:
        widget.shutdown()
        widget.deleteLater()


@pytest.mark.parametrize('workspace', [LeadershipTimelineWidget, SectorOverviewWidget, RotationScannerWidget])
def test_market_refresh_shortcut_uses_page_action(qapp, workspace):
    widget = workspace({})
    try:
        widget.resize(1080, 900)
        widget.show()
        widget.activateWindow()
        widget.refresh_button.setFocus()
        qapp.processEvents()
        clicks = QtTest.QSignalSpy(widget.refresh_button.clicked)
        QtTest.QTest.keyClick(widget.refresh_button, Qt.Key.Key_F5)
        assert clicks.count() == 1
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()


def test_sector_partial_failure_is_retried(qapp, monkeypatch):
    widget = SectorOverviewWidget({})
    widget.end_hour = 60 * HOUR
    widget.end_15m = widget.end_hour
    monkeypatch.setattr(widget, 'render', lambda: None)
    widget._batch_finished({'end_hour': widget.end_hour, 'end_15m': widget.end_15m,
                            'requests': {'TESTUSDT': {'futures_1h': True}},
                            'data': {'TESTUSDT': {'errors': {'futures_1h': 'timeout'}}}})
    assert ('TESTUSDT', 'futures_1h') not in widget._component_attempts
    assert ('TESTUSDT', 'futures_1h') in widget._component_retries
    widget.shutdown()
    widget.deleteLater()


def test_sector_request_uses_complete_indicator_lookback(qapp):
    widget = SectorOverviewWidget({})
    widget.end_hour = 60 * HOUR
    widget.end_15m = widget.end_hour
    widget.hourly['TESTUSDT'] = _sector_overview_map_candles(candles(24, end=widget.end_hour), HOUR)
    assert widget._request_spec('TESTUSDT')['futures_1h']
    widget.hourly['TESTUSDT'] = _sector_overview_map_candles(candles(56, end=widget.end_hour), HOUR)
    assert not widget._request_spec('TESTUSDT')['futures_1h']
    widget.shutdown()
    widget.deleteLater()


def test_fonts_are_resolved_independently_of_cwd(qapp, tmp_path, monkeypatch):
    from nightwatch import utilities
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(utilities, '_register_font', lambda root, filename: seen.append(root) or ('Sans Serif', ''))
    utilities.load_app_fonts(qapp, str(tmp_path / 'project' / 'nightwatch'))
    assert set(seen) == {str(tmp_path / 'project' / 'fonts')}


def test_shell_arrow_assets_resolve_and_render_independently_of_cwd(qapp, tmp_path, monkeypatch):
    from pathlib import Path
    from PySide6.QtSvg import QSvgRenderer
    from nightwatch import theme
    monkeypatch.chdir(tmp_path)
    root = Path(theme.__file__).resolve().parent.parent / 'assets'
    style = theme.build_shell_stylesheet(theme.DEFAULT_THEME_NAME, theme.ui_palette(theme.THEMES[theme.DEFAULT_THEME_NAME]))
    for name in ['dropdown-arrow.svg', 'spin-up-arrow.svg', 'spin-down-arrow.svg']:
        path = root / name
        assert str(path).replace('\\', '/') in style
        assert QSvgRenderer(str(path)).isValid()


def test_dom_process_resolves_the_same_font_root(qapp, tmp_path, monkeypatch):
    from pathlib import Path
    from nightwatch import utilities
    from nightwatch.orderbook import orderbook_ui
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('QT_FONT_DPI', '96')
    monkeypatch.setattr(utilities, 'load_app_fonts', lambda app, root: seen.append(root))
    monkeypatch.setattr(orderbook_ui, '_DomRasterWorkerCanvas', lambda theme: None)
    orderbook_ui._DomRasterProcess({'dpi': 96, 'theme': {}})
    assert seen == [str(Path(orderbook_ui.__file__).resolve().parents[1])]


@pytest.mark.parametrize('stored,expected', [
    (None, ('1m', '5m', '15m', '1h', '4h', '1d', '1w', '1M')),
    ([], ('1m', '5m', '15m', '1h', '4h', '1d', '1w', '1M')),
    ('12h', ('1m', '5m', '15m', '1h', '4h', '1d', '1w', '1M')),
    (['1M', '3m', '30m', '3m', {}, 'invalid'], ('3m', '30m', '1M')),
    (['12h', '1h', '6h'], ('1h', '6h', '12h')),
])
def test_market_bar_selection_restores_supported_order(stored, expected):
    from nightwatch.constants import normalized_market_bar_timeframes
    assert normalized_market_bar_timeframes(stored) == expected


def test_market_bar_selection_bounds_numbered_shortcuts():
    from nightwatch.constants import TIMEFRAMES, normalized_market_bar_timeframes
    assert len(normalized_market_bar_timeframes(TIMEFRAMES)) == 9


def test_timeframe_rebuild_preserves_current_interval_and_clears_old_actions(qapp):
    from nightwatch.ui.market_widgets import TimeframeStrip
    strip = TimeframeStrip()
    selected = []
    strip.activated.connect(lambda index: selected.append(strip.currentData()))
    strip.setItems(('3m', '30m', '1d'), '12h')
    assert strip.currentData() == '12h'
    assert not any(button.isChecked() for button in strip._buttons)
    assert strip._external_data == '12h'
    assert [action.text() for action in strip._collapsed_menu.actions()] == ['3m\t1', '30m\t2', '1D\t3']
    assert selected == []
    strip._activate_index(1)
    assert selected == ['30m']
    assert strip._external_data is None
    strip.setCollapsed(True)
    strip.setItems(('1w', '1M'), '30m')
    assert strip.currentData() == '30m'
    assert strip._collapsed_button.text() == '30m'
    assert len(strip._collapsed_actions.actions()) == 2
    assert len(strip._group.buttons()) == 2
    assert all(not action.shortcut().toString() for action in strip._collapsed_menu.actions())
    from PySide6.QtTest import QTest
    strip.show()
    strip._show_collapsed_menu()
    qapp.processEvents()
    QTest.keyClick(strip._collapsed_menu, Qt.Key.Key_1)
    assert strip.currentData() == '1w'
    assert not strip._collapsed_menu.isVisible()
    strip._activate_index(1)
    assert selected == ['30m', '1w', '1M']
    strip.close()
    strip.deleteLater()


def test_narrow_market_bar_collapses_favorites_before_hiding_data(qapp):
    from nightwatch.theme import DEFAULT_THEME_NAME, THEMES, ui_palette
    from nightwatch.ui.market_widgets import MarketStatsWidget
    from nightwatch.utilities import InstrumentBar
    stats = MarketStatsWidget(ui_palette(THEMES[DEFAULT_THEME_NAME]), compact=True)
    bar = InstrumentBar(stats)
    bar.resize(840, 48)
    bar.show()
    qapp.processEvents()
    assert bar.timeframes._collapsed
    assert bar.identity_control.isVisible()
    assert all(card.isVisible() for card in bar.metric_controls)
    order = [bar.row.itemAt(index).widget() for index in range(bar.row.count())]
    assert order == [bar.context_slot, bar.market_group]
    assert [bar.market_row.itemAt(i).widget() for i in range(bar.market_row.count()) if bar.market_row.itemAt(i).widget() not in bar.metric_separators] == [stats.cards['last'], *bar.metric_controls]
    bar.resize(1200, 42)
    qapp.processEvents()
    assert not bar.timeframes._collapsed
    assert max(card.width() for card in bar.metric_controls) - min(card.width() for card in bar.metric_controls) <= 1
    assert all(card.width() > 104 for card in bar.metric_controls)
    assert bar.identity_control.width() < bar.width() // 2
    assert bar.market_group.x() >= bar.context_slot.width() + 8
    stats.set_timeframes(('30m',), '30m')
    qapp.processEvents()
    assert bar.context_slot.width() == bar.timeframes.expandedWidth() + 8
    bar.close()
    bar.deleteLater()


def test_hover_tooltip_uses_latest_value_and_cancels_on_leave(qapp, monkeypatch):
    from PySide6 import QtGui
    from nightwatch.utilities import tooltip_controller
    controller = tooltip_controller()
    widget = QtWidgets.QLabel('Visible')
    widget.resize(200, 40)
    widget.show()
    qapp.processEvents()
    point = widget.mapToGlobal(widget.rect().center())
    QtGui.QCursor.setPos(point)
    shown = []
    monkeypatch.setattr(QtWidgets.QToolTip, 'showText', lambda *args: shown.append(args))
    controller.request(widget, 'Old value', point)
    controller.request(widget, 'New value < 2', point)
    assert shown == []
    assert controller._timer.isActive()
    assert controller._timer.interval() == 150
    assert qapp.style().styleHint(QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay) == 150
    controller._show_pending()
    assert shown[-1][1] == '<qt>New value &lt; 2</qt>'
    controller.eventFilter(widget, QtCore.QEvent(QtCore.QEvent.Type.Leave))
    assert not controller._timer.isActive()
    assert controller._owner is None
    controller.request(widget, 'Deleted owner', point)
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(widget, QtCore.QEvent.Type.DeferredDelete)
    controller._show_pending()
    assert controller._owner is None


def test_tooltip_keeps_truncated_text_and_escapes_plain_content(qapp):
    from nightwatch.utilities import tooltip_controller, set_tooltip_theme
    from nightwatch.theme import DEFAULT_THEME_NAME, THEMES, ui_palette
    controller = tooltip_controller()
    label = QtWidgets.QLabel('Visible text')
    label.resize(200, 30)
    assert controller._redundant(label, 'Visible text')
    label.resize(20, 30)
    assert not controller._redundant(label, 'Visible text')
    assert controller._formatted('A < B & C') == '<qt>A &lt; B &amp; C</qt>'
    rich = '<table><tr><td>Funding</td></tr></table>'
    assert controller._formatted(rich) == rich
    assert qapp.style().styleHint(QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay) == 150
    assert qapp.style().styleHint(QtWidgets.QStyle.StyleHint.SH_ToolTip_FallAsleepDelay) == 0
    original_sheet = qapp.styleSheet()
    set_tooltip_theme(ui_palette(THEMES[DEFAULT_THEME_NAME]))
    assert qapp.styleSheet() == original_sheet, 'Tooltip styling must preserve application styling'

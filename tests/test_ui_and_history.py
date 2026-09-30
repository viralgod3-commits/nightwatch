from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt

from nightwatch.models import Candle
from nightwatch.leadership import (
    HOUR, HISTORY_HOURS, LeadershipTimelineWidget, RotationScannerWidget, SectorOverviewWidget,
    _sector_overview_classify, _sector_overview_closed_window_ready,
    _sector_overview_tail_limit, _sector_overview_map_candles, _prepare_leaders,
    _SectorAnalysis, _SectorOverviewVolumeBars,
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
    assert '120' not in widget.table.item(0, 3).text()
    widget.shutdown()
    widget.deleteLater()


def test_rotation_does_not_apply_leader_price_columns(qapp):
    widget = RotationScannerWidget({})
    widget._refresh_live_prices()  # Separate RS/RVol table schema.
    widget.shutdown()
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
    monkeypatch.setattr(utilities, '_register_font', lambda root, filename: seen.append(root) or 'Sans Serif')
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

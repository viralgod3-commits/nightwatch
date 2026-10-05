"""Market-view regressions: frozen snapshots, truthful trails and persistent detail."""
from copy import deepcopy
from dataclasses import replace
import math
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from PySide6.QtCore import Qt

from nightwatch.leadership import (
    HOUR, RotationBubbleChart, RotationScannerWidget, SectorOverviewWidget,
    LeadershipTimelineWidget, _bubble_color, _prepare_rotation,
)
from nightwatch.models import Candle


def history(end=40 * HOUR):
    return {at: Candle((at - HOUR) / 1000, 100, 102, 99, 101, 10, 2_000_000)
            for at in range(HOUR, end + 1, HOUR)}


def point(symbol='TESTUSDT', x=2, y=1, volume=1):
    return dict(symbol=symbol, x=x, y=y, volume=volume,
                trail=[(x - .8, y + .4), (x - .5, y + .1), (x - .2, y + .3), (x, y)])


def close(widget):
    if hasattr(widget, 'shutdown'):
        widget.shutdown()
    widget.close()
    widget.deleteLater()


@pytest.mark.parametrize('x,y', [(-2, 1), (2, 1), (-2, -1), (2, -1)])
def test_trail_preserves_every_observed_segment_heading(qapp, x, y):
    chart = RotationBubbleChart()
    try:
        chart.resize(800, 450)
        data = point(x=x, y=y)
        group = chart._trail_geometry(data, 10)
        for index in range(3):
            observed = chart.map_point(*data['trail'][index + 1]) - chart.map_point(*data['trail'][index])
            drawn = group['positions'][index + 1] - group['positions'][index]
            assert observed.x() * drawn.y() - observed.y() * drawn.x() == pytest.approx(0, abs=1e-8)
            assert observed.x() * drawn.x() + observed.y() * drawn.y() > 0
    finally:
        close(chart)


def test_stationary_trail_does_not_invent_a_heading(qapp):
    chart = RotationBubbleChart()
    try:
        data = point()
        data['trail'] = [(2, 1)] * 4
        group = chart._trail_geometry(data, 10)
        assert all(position == group['positions'][-1] for position in group['positions'])
    finally:
        close(chart)


def test_overlap_translation_keeps_shape_and_selection_keeps_membership(qapp):
    chart = RotationBubbleChart()
    try:
        chart.resize(800, 450)
        points = [point(f'PAIR{i}USDT', volume=10 + i) for i in range(8)]
        original = deepcopy(points)
        chart.set_points(points)
        chart.grab()
        before = {g['point']['symbol']: list(g['positions']) for g in chart._geometry}
        assert set(before) == {'PAIR5USDT', 'PAIR6USDT', 'PAIR7USDT'}
        heads = [(g['positions'][-1], g['radii'][-1]) for g in chart._geometry]
        for i, (at, radius) in enumerate(heads):
            for other, other_radius in heads[i + 1:]:
                assert math.hypot(at.x() - other.x(), at.y() - other.y()) >= radius + other_radius + 5
        for group in chart._geometry:
            source = chart._trail_geometry(group['point'], group['radii'][-1])
            offsets = [a - b for a, b in zip(group['positions'], source['positions'])]
            assert all(offset.y() == pytest.approx(0) for offset in offsets)
            assert all(offset.x() == pytest.approx(offsets[0].x()) for offset in offsets)
            for at, radius in zip(group['positions'], group['radii']):
                assert chart.plot_rect().contains(QtCore.QRectF(at.x()-radius, at.y()-radius, 2*radius, 2*radius))
        chart.set_points(points, 'PAIR0USDT')
        chart.grab()
        assert {g['point']['symbol']: g['positions'] for g in chart._geometry} == before
        assert points == original
        assert chart._labels
        chosen = QtTest.QSignalSpy(chart.chosen)
        selected, box = chart._labels[0]
        QtTest.QTest.mouseClick(chart, Qt.MouseButton.LeftButton, pos=box.center().toPoint())
        assert chosen.at(0) == [selected['symbol']]
    finally:
        close(chart)


def test_each_quadrant_has_three_representatives_and_colors_are_stable(qapp):
    chart = RotationBubbleChart()
    try:
        points = [point(f'Q{q}P{i}USDT', x=x, y=y, volume=i)
                  for q, (x, y) in enumerate([(-2, 1), (2, 1), (-2, -1), (2, -1)])
                  for i in range(15)]
        chart.set_points(points)
        assert len(chart.points) == 12
        assert all(sum(p['x'] == x and p['y'] == y for p in chart.points) == 3
                   for x, y in [(-2, 1), (2, 1), (-2, -1), (2, -1)])
        first = {p['symbol']: _bubble_color(p) for p in chart.points}
        chart.set_points(list(reversed(points)))
        assert {p['symbol']: _bubble_color(p) for p in chart.points} == first
        assert len({_bubble_color(p) for p in points}) >= 12
    finally:
        close(chart)


def test_rotation_waits_for_benchmark_then_freezes_until_explicit_refresh(qapp):
    widget = RotationScannerWidget()
    end = 40 * HOUR
    snapshot = dict(end=end, clock_offset=0, symbols=('TESTUSDT',), series={'TESTUSDT': history()},
                    spot={}, categories={})
    source = SimpleNamespace(watchlist=None, can_load=lambda: True, valid_symbols={'TESTUSDT', 'BTCUSDT'},
                             tickers={}, details={}, sector_hourly_snapshot=lambda: snapshot,
                             _load_pending=True, task=None, fetched_for={}, retry_after={}, errors={})
    widget.leadership = source
    try:
        widget._leaders_changed()
        assert widget._rotation_snapshot_end is None
        snapshot['series']['BTCUSDT'] = history()
        source.fetched_for['BTCUSDT'] = end
        widget._leaders_changed()
        assert widget._rotation_snapshot_end is None  # A partially loaded cohort is not published.
        source.fetched_for['TESTUSDT'] = end
        widget._leaders_changed()
        assert widget._rotation_snapshot_end == end
        published = widget.series['TESTUSDT'][end]
        generation = widget.generation
        snapshot['series']['TESTUSDT'][end] = replace(published, close=120)
        widget.update_tickers([{'s': 'TESTUSDT', 'c': '999', 'q': '1000000000'}])
        widget._leaders_changed()
        widget.refresh(force=True)  # Tab activation must also leave the same hour intact.
        assert widget.series['TESTUSDT'][end] == published
        assert widget.generation == generation
        widget._reload_snapshot()
        assert widget.series['TESTUSDT'][end].close == 120
        assert widget.generation == generation + 1
        assert widget.rest is None and widget.db is None
    finally:
        widget.leadership = None
        close(widget)


def test_rotation_window_uses_completed_candles_and_preserves_history_gaps():
    end = 40 * HOUR
    series = {'BTCUSDT': history(), 'TESTUSDT': history()}
    original = deepcopy(series)
    state = dict(cursor=end, symbols=['TESTUSDT'], series=series, spot_series={})
    before = _prepare_rotation(dict(state, hours=4))
    series['TESTUSDT'][end + HOUR] = replace(series['TESTUSDT'][end], close=10000)
    assert _prepare_rotation(dict(state, hours=4)) == before
    series['TESTUSDT'][end] = replace(series['TESTUSDT'][end], close=110)
    one = _prepare_rotation(dict(state, hours=1))
    four = _prepare_rotation(dict(state, hours=4))
    assert len(one['charts']['TESTUSDT']['relative']) == 2
    assert len(four['charts']['TESTUSDT']['relative']) == 5
    del series['TESTUSDT'][end - 2*HOUR]
    assert not _prepare_rotation(dict(state, hours=4))['points']
    assert original['TESTUSDT'][end].close == 101


def test_sector_selection_updates_cached_detail_without_work_and_peer_opens_chart(qapp, monkeypatch):
    widget = SectorOverviewWidget()
    try:
        widget._prepared_analysis = {'metrics': {
            'DeFi': dict(performance=2.4, volume_share=12, share_delta=.5,
                         leaders=[('AAVEUSDT', 2.5)], improving=[('UNIUSDT', 1.2)])}}
        widget._prepared_sector_key = widget._analysis_view_key()
        monkeypatch.setattr(widget, 'render', lambda: pytest.fail('Selecting a sector must reuse its computed metrics'))
        widget._select_sector('DeFi')
        assert widget.detail.title.text() == 'DeFi'
        assert widget.detail.performance.text() == '+2.40%'
        assert widget.detail.metric_cards['share'].value.text() == '12.0%'
        assert widget.detail.improving.rows[0][2].text() == '+1.2 pp'
        opened = QtTest.QSignalSpy(widget.symbol_selected)
        widget.detail.leaders.rows[0][1].click()
        assert opened.at(0) == ['AAVEUSDT']
    finally:
        close(widget)


def test_empty_sector_universe_clears_previous_values(qapp):
    widget = SectorOverviewWidget()
    try:
        widget.detail.update_data('AI', dict(performance=5, leaders=[('TAOUSDT', 4)]), '4H')
        widget._prepared_analysis = {'metrics': {'AI': {'performance': 5}}}
        widget.table.setRowCount(2)
        widget.active = True
        widget.render()
        assert not widget._prepared_analysis
        assert widget.table.rowCount() == 0
        assert widget.detail.performance.text() == '—'
        assert not widget.detail.leaders.rows[0][1].isEnabled()
    finally:
        close(widget)


@pytest.mark.parametrize('width,height', [(800, 900), (960, 540), (1280, 720), (1920, 1080)])
def test_sector_detail_stays_present_after_restore_and_resize(qapp, tmp_path, width, height):
    settings = QtCore.QSettings(str(tmp_path/'sectors.ini'), QtCore.QSettings.Format.IniFormat)
    settings.setValue('markets/sectors/context_visible_v2', False)
    widget = SectorOverviewWidget()
    try:
        widget.restore_ui_state(settings)
        widget.resize(width, height)
        widget.show()
        for _ in range(5):
            qapp.processEvents()
        assert widget.size() == QtCore.QSize(width, height)
        assert widget.detail_drawer.isVisible() and widget.detail.isVisible()
        assert widget.context_button.isHidden()
        assert not widget.detail_drawer.geometry().intersects(widget.overview_scroll.geometry())
        assert widget.detail_drawer.horizontalScrollBar().maximum() == 0
        for tile in widget.tiles.values():
            assert tile.performance.geometry().bottom() <= tile.bars.geometry().top()
            assert tile.bars.geometry().bottom() <= tile.share.geometry().top()
        assert widget.grab().width() >= width
    finally:
        close(widget)


def test_leaders_overview_exposes_strength_and_volume_without_clipped_state_buttons(qapp):
    widget = LeadershipTimelineWidget()
    try:
        widget.resize(1280, 720)
        widget.show()
        qapp.processEvents()
        assert not widget.table.isColumnHidden(12)
        assert not widget.table.isColumnHidden(15)
        assert widget.table.isColumnHidden(2)
        for button in widget.state_buttons.values():
            assert button.fontMetrics().horizontalAdvance(button.text()) + 8 <= button.width()
    finally:
        close(widget)


def test_overlapping_neutral_heads_separate_without_invented_movement(qapp):
    chart = RotationBubbleChart()
    try:
        chart.resize(700, 400)
        points = [dict(symbol=f'FLAT{i}USDT', x=0, y=0, volume=1, trail=[(0, 0)]*4) for i in range(3)]
        chart.set_points(points)
        chart.grab()
        heads = [group['positions'][-1] for group in chart._geometry]
        assert len({head.x() for head in heads}) == 3
        assert all(head.y() == chart.plot_rect().center().y() for head in heads)
        for group in chart._geometry:
            assert all(at == group['positions'][-1] for at in group['positions'])
    finally:
        close(chart)


def test_saved_leaders_preset_is_not_overridden_by_stale_column_preferences(qapp, tmp_path):
    settings = QtCore.QSettings(str(tmp_path/'leaders.ini'), QtCore.QSettings.Format.IniFormat)
    settings.setValue('markets/leadership/view', 'overview')
    settings.setValue('markets/leadership/column_12', False)
    settings.setValue('markets/leadership/column_15', False)
    widget = LeadershipTimelineWidget()
    try:
        widget.restore_ui_state(settings)
        assert widget.view_selector.currentData() == 'overview'
        assert not widget.table.isColumnHidden(12)
        assert not widget.table.isColumnHidden(15)
    finally:
        close(widget)

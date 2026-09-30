import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.pop('BINANCE_API_KEY', None)
os.environ.pop('BINANCE_API_SECRET', None)

import pytest
import time
from PySide6 import QtWidgets


@pytest.fixture(scope='session')
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def gateway(qapp, monkeypatch):
    from nightwatch.networking.binance import BinanceRest
    from nightwatch.trading.gateway import TradingGateway
    monkeypatch.setattr(BinanceRest, '_ensure_time_sync_loop', lambda self: None)
    monkeypatch.setattr(TradingGateway, '_restore_placement_journal', lambda self: None)
    instance = TradingGateway()
    instance.api_key, instance.api_secret = 'test-key', 'test-secret'
    instance._journal_loading = False
    instance.account_can_trade = True
    instance.hedge_mode = False
    instance.multi_assets_margin = False
    instance._last_snapshot_mono = time.monotonic()
    monkeypatch.setattr(instance, 'connect_session', lambda: None)
    yield instance
    instance.stop()

"""Offline regression checks for exact orderbook price formatting and painting."""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets
from nightwatch.models import ORDER_FLOW_AGGREGATION_MULTIPLIERS, OrderFlowSnapshot
from nightwatch.orderbook.orderbook_ui import _DomRasterCanvas, _decimal_places_from_step
from nightwatch.theme import ORDERBOOK_THEMES
from nightwatch.utilities import load_app_fonts


class RecordingPainter(QtGui.QPainter):
    def __init__(self, image):
        super().__init__(image)
        self.texts = []

    def drawText(self, *args):
        self.texts.append(args[-1])
        return super().drawText(*args)


class PriceChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        load_app_fonts(cls.app, str(ROOT))

    def test_grouped_precision_matches_exact_decimal_step(self):
        for tick in (1e-7, 1e-6, 1e-8, 1e-12, 1e-16, .25, .0025, .00000025):
            for multiplier in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
                expected = max(0, min(16, -(Decimal(str(tick)) * multiplier).normalize().as_tuple().exponent))
                with self.subTest(tick=tick, multiplier=multiplier):
                    self.assertEqual(_decimal_places_from_step(tick, multiplier), expected)

    def test_full_prices_are_painted_after_grouping_resize_and_symbol_switch(self):
        canvas = _DomRasterCanvas(ORDERBOOK_THEMES['Nightwatch'])
        self.assertEqual(canvas._price_font.pixelSize(), 11)
        image = QtGui.QImage(1000, 100, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        painter = RecordingPainter(image)
        try:
            for symbol, tick, value in (
                ('SKRUSDT', 1e-7, .0174242),
                ('SHIBUSDT', 1e-8, .00001234),
                ('BTCUSDT', .1, 100123.4),
                ('PRECISEUSDT', 1e-12, .000000017424),
                ('SKRUSDT', 1e-7, .0174242),
            ):
                canvas.set_symbol(symbol)
                canvas.set_price_tick_size(tick)
                snapshot = OrderFlowSnapshot(
                    symbol=symbol, sequence=1, generated_monotonic=1,
                    ready=True, live=True, bbo_source='depth',
                    depth_age_seconds=0, bbo_age_seconds=0, trade_age_seconds=0,
                    best_bid=value, best_ask=value + tick, midpoint=value + tick / 2,
                )
                for depth in (True, False):
                    canvas.set_book_depth_enabled(depth, emit=False)
                    for multiplier in ORDER_FLOW_AGGREGATION_MULTIPLIERS:
                        canvas.set_aggregation_multiplier(multiplier, emit=False)
                        price = canvas._bucket_price_for_multiplier(value, 'bid', multiplier)
                        for width in (140, 260, 360):
                            with self.subTest(symbol=symbol, depth=depth, multiplier=multiplier, width=width):
                                canvas._refresh_geometry_for_size(width, 520, snapshot)
                                text = canvas._price_text(price)
                                if price <= 0:
                                    self.assertEqual(text, '—')
                                    continue
                                precision = (canvas._grouped_price_decimals if depth else canvas.price_decimals)
                                self.assertEqual(text, f'{price:.{precision}f}')
                                self.assertNotIn('e', text.lower())
                                rect = canvas._column_rect('price', 0, 24)
                                canvas._draw_price_text(painter, rect, text, QtGui.QColor('white'), pad=4 if depth else 3)
                                self.assertEqual(painter.texts[-1], text)
                                self.assertFalse(canvas._price_text_overflow)
                canvas.set_book_depth_enabled(True, emit=False)
                canvas.set_aggregation_multiplier(1, emit=False)
                self.assertEqual(canvas._price_text(value), f'{value:.{canvas.price_decimals}f}')
        finally:
            painter.end()
            canvas.close()


if __name__ == '__main__':
    unittest.main()

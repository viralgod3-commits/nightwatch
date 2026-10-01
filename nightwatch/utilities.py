"""Shared Qt presentation: typography, widgets, icons and UI geometry."""
from __future__ import annotations

# ========================================================================
# typography
# ========================================================================
import os
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from . import constants


# Shared order-book design tokens. The canvas and trade tape use the same
# directional colors so switching views never changes the meaning of color.


class TextRole:
    """Semantic text roles. Widgets declare meaning; this module owns rendering."""

    UI_BODY = "ui_body"
    UI_LABEL = "ui_label"
    UI_CAPTION = "ui_caption"
    UI_HEADING = "ui_heading"
    WORKSPACE_TITLE = "workspace_title"
    WORKSPACE_SUBTITLE = "workspace_subtitle"
    PANEL_TITLE = "panel_title"
    UI_CONTROL = "ui_control"
    UI_CONTROL_COMPACT = "ui_control_compact"
    ICON_FALLBACK = "icon_fallback"
    TRADING_TICKET = "trading_ticket"
    TRADING_TICKET_VALUE = "trading_ticket_value"
    TRADING_DESK_CONTROL = "trading_desk_control"
    TRADING_DESK_VALUE = "trading_desk_value"
    TRADING_DESK_CAPTION = "trading_desk_caption"
    TRADING_DESK_AMOUNT = "trading_desk_amount"
    TRADING_DESK_PNL = "trading_desk_pnl"
    UI_GLYPH = "ui_glyph"
    INSTRUMENT_SYMBOL = "instrument_symbol"
    TOP_TICKER_SYMBOL = "top_ticker_symbol"
    MARKET_VALUE = "market_value"
    MARKET_VALUE_EMPHASIZED = "market_value_emphasized"
    MARKET_VALUE_LARGE = "market_value_large"
    MARKET_VALUE_HERO = "market_value_hero"
    TABLE_TEXT = "table_text"
    TABLE_VALUE = "table_value"
    CHART_AXIS = "chart_axis"
    CHART_OVERLAY = "chart_overlay"
    ORDERBOOK_CONTROL = "orderbook_control"
    ORDERBOOK_CONTROL_ARROW = "orderbook_control_arrow"
    ORDERBOOK_VALUE = "orderbook_value"
    ORDERBOOK_PRICE = "orderbook_price"
    ORDERBOOK_METRIC = "orderbook_metric"
    ORDERBOOK_LABEL = "orderbook_label"
    ORDERBOOK_SYMBOL = "orderbook_symbol"
    ORDERBOOK_FOOTER_VALUE = "orderbook_footer_value"
    ORDERBOOK_CENTER_PRICE = "orderbook_center_price"
    MAGNETIC_RAIL_PRICE = "magnetic_rail_price"
    RAIL_LABEL = "rail_label"
    RAIL_CONTROL = "rail_control"
    ALERT_TEXT = "alert_text"
    NEWS_TEXT = "news_text"
    STATUS_TEXT = "status_text"
    STATUS_MESSAGE = "status_message"
    STATUS_LATENCY = "status_latency"


TYPOGRAPHY_ROLE_LABELS: dict[str, str] = {
    TextRole.UI_BODY: "UI body",
    TextRole.UI_LABEL: "UI labels / captions",
    TextRole.UI_CAPTION: "Small UI captions",
    TextRole.UI_HEADING: "Headings",
    TextRole.WORKSPACE_TITLE: "Workspace / display titles",
    TextRole.WORKSPACE_SUBTITLE: "Workspace subtitles",
    TextRole.PANEL_TITLE: "Panel titles",
    TextRole.UI_CONTROL: "Buttons / controls",
    TextRole.UI_CONTROL_COMPACT: "Compact buttons / controls",
    TextRole.ICON_FALLBACK: "Fallback icon glyphs",
    TextRole.TRADING_TICKET: "Compact execution-ticket controls",
    TextRole.TRADING_TICKET_VALUE: "Compact execution-ticket numeric values",
    TextRole.TRADING_DESK_CONTROL: "Position Desk controls",
    TextRole.TRADING_DESK_VALUE: "Position Desk numeric values",
    TextRole.TRADING_DESK_CAPTION: "Position Desk captions",
    TextRole.TRADING_DESK_AMOUNT: "Position Desk close quantity",
    TextRole.TRADING_DESK_PNL: "Position Desk selected PnL",
    TextRole.UI_GLYPH: "UI glyph controls",
    TextRole.INSTRUMENT_SYMBOL: "Instrument / symbol identifiers",
    TextRole.TOP_TICKER_SYMBOL: "Top-bar ticker identifier",
    TextRole.MARKET_VALUE: "Market / execution values",
    TextRole.MARKET_VALUE_EMPHASIZED: "Emphasized market values",
    TextRole.MARKET_VALUE_LARGE: "Large market / dashboard values",
    TextRole.MARKET_VALUE_HERO: "Hero market / dashboard values",
    TextRole.TABLE_TEXT: "Table prose / status",
    TextRole.TABLE_VALUE: "Table numeric values",
    TextRole.CHART_AXIS: "Chart axes",
    TextRole.CHART_OVERLAY: "Chart overlay values",
    TextRole.ORDERBOOK_CONTROL: "Order-book controls",
    TextRole.ORDERBOOK_CONTROL_ARROW: "Order-book control arrows",
    TextRole.ORDERBOOK_VALUE: "Order-book row quantities / flow",
    TextRole.ORDERBOOK_PRICE: "Order-book row prices",
    TextRole.ORDERBOOK_METRIC: "Order-book metrics",
    TextRole.ORDERBOOK_LABEL: "Order-book labels",
    TextRole.ORDERBOOK_SYMBOL: "Order-book symbol",
    TextRole.ORDERBOOK_FOOTER_VALUE: "Order-book footer counts",
    TextRole.ORDERBOOK_CENTER_PRICE: "Order-book center price",
    TextRole.MAGNETIC_RAIL_PRICE: "Magnetic rail exact price",
    TextRole.RAIL_LABEL: "Magnetic rail labels",
    TextRole.RAIL_CONTROL: "Magnetic rail inline controls",
    TextRole.ALERT_TEXT: "Alert text",
    TextRole.NEWS_TEXT: "News / signal text",
    TextRole.STATUS_TEXT: "Status text",
    TextRole.STATUS_MESSAGE: "Status message",
    TextRole.STATUS_LATENCY: "Status latency",
}


def _font_profile(
    family: str,
    size: float,
    weight: int,
    *,
    hinting: str,
    fixed_pitch: bool = False,
    numeric_width: str = "normal",
    size_mode: str = "point",
) -> dict[str, Any]:
    return {
        "family": family,
        "size_mode": "pixel" if size_mode == "pixel" else "point",
        "size": float(size),
        "weight": int(weight),
        "numeric_width": str(numeric_width),
        "letter_spacing": 100.0,
        "word_spacing": 0.0,
        "stretch": 100,
        "kerning": not fixed_pitch,
        "fixed_pitch": bool(fixed_pitch),
        "hinting": hinting,
        "antialias": "prefer",
        "quality": "quality",
        "no_subpixel": False,
    }


TYPOGRAPHY_DEFAULTS: dict[str, dict[str, Any]] = {
    TextRole.UI_BODY: _font_profile("ui", 9.0, 400, hinting="vertical"),
    # Small uppercase labels were previously 9-10 px. 8.5 pt keeps the dense
    # geometry while giving 96-DPI/1080p displays materially more raster detail.
    TextRole.UI_LABEL: _font_profile("ui", 8.5, 500, hinting="vertical"),
    TextRole.UI_CAPTION: _font_profile("ui", 8.0, 400, hinting="vertical"),
    TextRole.UI_HEADING: _font_profile("ui", 11.0, 600, hinting="vertical"),
    TextRole.WORKSPACE_TITLE: _font_profile("ui", 17.0, 600, hinting="vertical"),
    TextRole.WORKSPACE_SUBTITLE: _font_profile("ui", 8.5, 400, hinting="vertical"),
    TextRole.PANEL_TITLE: _font_profile("ui", 10.5, 600, hinting="vertical"),
    TextRole.UI_CONTROL: _font_profile("ui", 9.0, 400, hinting="vertical"),
    TextRole.UI_CONTROL_COMPACT: _font_profile("ui", 8.25, 400, hinting="vertical"),
    TextRole.ICON_FALLBACK: _font_profile("ui", 6.5, 600, hinting="vertical"),
    TextRole.TRADING_TICKET: _font_profile("ui", 7.75, 400, hinting="vertical"),
    TextRole.TRADING_TICKET_VALUE: _font_profile("numeric", 8.5, 500, hinting="full", fixed_pitch=True),
    TextRole.TRADING_DESK_CONTROL: _font_profile("ui", 10.0, 400, hinting="vertical"),
    TextRole.TRADING_DESK_VALUE: _font_profile("numeric", 10.5, 500, hinting="full", fixed_pitch=True),
    TextRole.TRADING_DESK_CAPTION: _font_profile("ui", 9.0, 400, hinting="vertical"),
    TextRole.TRADING_DESK_AMOUNT: _font_profile("numeric", 21.0, 500, hinting="full", fixed_pitch=True, numeric_width="extended"),
    TextRole.TRADING_DESK_PNL: _font_profile("numeric", 20.0, 500, hinting="full", fixed_pitch=True, numeric_width="extended"),
    TextRole.UI_GLYPH: _font_profile("ui", 11.0, 400, hinting="vertical"),
    # Instrument identifiers are alphabetic labels, not tabular numeric data.
    # Keep their size terminal-dense while avoiding full numeric hinting, which
    # can make glyphs such as uppercase T appear disproportionately heavy.
    TextRole.INSTRUMENT_SYMBOL: _font_profile("ui", 9.5, 500, hinting="vertical"),
    TextRole.TOP_TICKER_SYMBOL: _font_profile("ui", 9.25, 400, hinting="vertical"),
    TextRole.MARKET_VALUE: _font_profile("numeric", 9.0, 500, hinting="full", fixed_pitch=True),
    TextRole.MARKET_VALUE_EMPHASIZED: _font_profile("numeric", 9.0, 600, hinting="full", fixed_pitch=True),
    TextRole.MARKET_VALUE_LARGE: _font_profile("numeric", 13.0, 500, hinting="full", fixed_pitch=True, numeric_width="extended"),
    TextRole.MARKET_VALUE_HERO: _font_profile("numeric", 17.0, 600, hinting="full", fixed_pitch=True, numeric_width="extended"),
    TextRole.TABLE_TEXT: _font_profile("ui", 9.0, 400, hinting="vertical"),
    TextRole.TABLE_VALUE: _font_profile("numeric", 9.0, 500, hinting="full", fixed_pitch=True),
    TextRole.CHART_AXIS: _font_profile("numeric", 8.5, 400, hinting="vertical", fixed_pitch=True),
    TextRole.CHART_OVERLAY: _font_profile("numeric", 9.0, 500, hinting="full", fixed_pitch=True),
    # Painted DOM text is pixel-sized so row geometry stays deterministic.
    # Comfortable default spacing and tabular numerics support quick scanning;
    # the center price remains the strongest element in the ladder.
    TextRole.ORDERBOOK_CONTROL: _font_profile("ui", 9.0, 500, hinting="vertical"),
    TextRole.ORDERBOOK_CONTROL_ARROW: _font_profile("ui", 7.2, 500, hinting="vertical"),
    TextRole.ORDERBOOK_VALUE: _font_profile("numeric", 13.0, 500, hinting="full", fixed_pitch=True, size_mode="pixel"),
    TextRole.ORDERBOOK_PRICE: _font_profile("numeric", 13.0, 500, hinting="full", fixed_pitch=True, size_mode="pixel"),
    TextRole.ORDERBOOK_METRIC: _font_profile("numeric", 12.0, 500, hinting="full", fixed_pitch=True, size_mode="pixel"),
    TextRole.ORDERBOOK_LABEL: _font_profile("ui", 11.0, 400, hinting="vertical", size_mode="pixel"),
    TextRole.ORDERBOOK_SYMBOL: _font_profile("ui", 14.0, 600, hinting="vertical", size_mode="pixel"),
    TextRole.ORDERBOOK_FOOTER_VALUE: _font_profile("numeric", 11.0, 500, hinting="full", fixed_pitch=True, size_mode="pixel"),
    TextRole.ORDERBOOK_CENTER_PRICE: _font_profile("numeric", 20.0, 600, hinting="full", fixed_pitch=True, size_mode="pixel"),
    TextRole.MAGNETIC_RAIL_PRICE: _font_profile("numeric", 8.5, 500, hinting="vertical", fixed_pitch=True),
    TextRole.RAIL_LABEL: _font_profile("ui", 7.5, 500, hinting="vertical"),
    TextRole.RAIL_CONTROL: _font_profile("ui", 6.75, 600, hinting="vertical"),
    TextRole.ALERT_TEXT: {
        **_font_profile("ui", 9.0, 400, hinting="vertical"),
        # Grayscale antialiasing avoids RGB fringe/shimmer on small text.
        "no_subpixel": True,
    },
    TextRole.NEWS_TEXT: {
        **_font_profile("ui", 9.0, 400, hinting="vertical"),
        # Moving glyphs remain antialiased but do not use color subpixels,
        # which prevents chromatic shimmer while the marquee advances.
        "no_subpixel": True,
    },
    TextRole.STATUS_TEXT: _font_profile("ui", 7.5, 400, hinting="vertical"),
    TextRole.STATUS_MESSAGE: _font_profile("ui", 7.5, 400, hinting="vertical"),
    TextRole.STATUS_LATENCY: _font_profile("numeric", 7.5, 400, hinting="vertical", fixed_pitch=True),
}

TYPOGRAPHY_GLOBAL_DEFAULTS: dict[str, Any] = {
    "painter_text_antialias": True,
    # Applied by entrypoint before QApplication. 'auto' = Round on Windows,
    # PassThrough elsewhere, matching the previous Nightwatch behavior.
    "dpi_rounding": "auto",
}

# Typography-adjacent settings live here too so modules never own font roles,
# font files, static weights, or font-size constraints.
DEV_UI_STATUS_FONT_DEFAULTS: dict[str, str] = {
    "text_font_role": TextRole.STATUS_TEXT,
    "message_font_role": TextRole.STATUS_MESSAGE,
    "latency_font_role": TextRole.STATUS_LATENCY,
}

TYPOGRAPHY_STATE_WEIGHTS: dict[str, int] = {
    "attention": 600,
    "trade_price_regular": 400,
    "trade_price_changed": 700,
}

TYPOGRAPHY_STATE_OPACITIES: dict[str, float] = {
    "trade_price_regular": 0.5,
    "trade_price_changed": 1.0,
}

TYPOGRAPHY_LIMITS: dict[str, dict[str, int]] = {
    TextRole.ORDERBOOK_PRICE: {"min_pixel_size": 9},
    TextRole.TABLE_VALUE: {"min_pixel_size": 9},
}

TYPOGRAPHY_FONT_FILES: dict[str, str] = {
    "ui_regular": "Inter-Regular.ttf",
    "ui_medium": "Inter-Medium.ttf",
    "ui_semibold": "Inter-SemiBold.ttf",
    "numeric_regular": "Iosevka-Regular.ttf",
    "numeric_medium": "Iosevka-Medium.ttf",
    "numeric_semibold": "Iosevka-SemiBold.ttf",
    "numeric_bold": "Iosevka-Bold.ttf",
    "numeric_extended_medium": "Iosevka-ExtendedMedium.ttf",
    "numeric_extended_semibold": "Iosevka-ExtendedSemiBold.ttf",
}


_LEGACY_TEXT_ROLES: dict[str, str] = {
    "topMetricTitle": TextRole.UI_LABEL,
    "metricTitle": TextRole.UI_LABEL,
    "tradeFieldLabel": TextRole.UI_LABEL,
    "tradingSectionLabel": TextRole.UI_LABEL,
    "subtleLabel": TextRole.UI_CAPTION,
    "metricHoverName": TextRole.UI_LABEL,
    "metricHoverStamp": TextRole.UI_CAPTION,
    "dialogHeading": TextRole.UI_HEADING,
    "workspaceHeading": TextRole.UI_HEADING,
    "controlSectionTitle": TextRole.UI_HEADING,
    "leadershipSubtitle": TextRole.WORKSPACE_SUBTITLE,
    "framelessCloseButton": TextRole.UI_GLYPH,
    "watchlistGroupMenu": TextRole.UI_GLYPH,
    "topMetricValue": TextRole.MARKET_VALUE,
    "metricValue": TextRole.MARKET_VALUE,
    "topTickerLast": TextRole.MARKET_VALUE,
    "topTickerSymbol": TextRole.TOP_TICKER_SYMBOL,
    "globalSymbolSearch": TextRole.INSTRUMENT_SYMBOL,
    "metricHoverValue": TextRole.MARKET_VALUE_EMPHASIZED,
    "tradeAccountSummary": TextRole.MARKET_VALUE,
    "tradingDeskStatus": TextRole.UI_CAPTION,
    "terminalLatency": TextRole.STATUS_LATENCY,
    "statusMessage": TextRole.STATUS_MESSAGE,
    "connectionStatus": TextRole.STATUS_TEXT,
    "accountCardPnl": TextRole.MARKET_VALUE_EMPHASIZED,
    "accountTotalPnl": TextRole.MARKET_VALUE_LARGE,
    "leadershipTitle": TextRole.WORKSPACE_TITLE,
    "leadersMetricTitle": TextRole.UI_CAPTION,
    "leadersMetricSub": TextRole.UI_CAPTION,
    "leadersSideTitle": TextRole.PANEL_TITLE,
    "leadersTableCaption": TextRole.UI_CAPTION,
    "leadersTinyLabel": TextRole.UI_CAPTION,
    "leadersBigPrice": TextRole.MARKET_VALUE_EMPHASIZED,
    "leadersCandidatePrice": TextRole.MARKET_VALUE,
    "leadersCandidateChange": TextRole.MARKET_VALUE,
    "leadersMetric": TextRole.MARKET_VALUE,
    "leadersMetricValue": TextRole.MARKET_VALUE_LARGE,
    "leadersRank": TextRole.MARKET_VALUE,
    "leadersRankChange": TextRole.MARKET_VALUE,
    "leadersDistributionValue": TextRole.MARKET_VALUE,
    "sectorTitle": TextRole.WORKSPACE_TITLE,
    "sectorPanelTitle": TextRole.PANEL_TITLE,
    "sectorMuted": TextRole.UI_CAPTION,
    "sectorStatTitle": TextRole.UI_CAPTION,
    "sectorStatValue": TextRole.MARKET_VALUE_LARGE,
    "sectorTileName": TextRole.PANEL_TITLE,
    "sectorTilePerformance": TextRole.MARKET_VALUE_HERO,
    "sectorTileShare": TextRole.MARKET_VALUE,
    "sectorDetailMetric": TextRole.MARKET_VALUE_LARGE,
    "sectorTimeframe": TextRole.UI_CONTROL_COMPACT,
    "timeframeStripButton": TextRole.UI_CONTROL_COMPACT,
    "metricDialogHeading": TextRole.UI_HEADING,
    "metricDialogSubheading": TextRole.UI_CAPTION,
    "metricControlLabel": TextRole.UI_LABEL,
    "metricHoverNote": TextRole.UI_CAPTION,
    "lastPrice": TextRole.MARKET_VALUE_EMPHASIZED,
    "searchResultCount": TextRole.MARKET_VALUE,
    "settingsHeading": TextRole.UI_HEADING,
    "settingsPageHeading": TextRole.UI_HEADING,
    "settingsSubheading": TextRole.UI_LABEL,
    "auxChartStatus": TextRole.UI_CAPTION,
    "timeInForceCycle": TextRole.UI_CONTROL_COMPACT,
}


def _weight_enum(value: int) -> QtGui.QFont.Weight:
    value = int(value)
    if value >= 700:
        return QtGui.QFont.Weight.Bold
    if value >= 600:
        return QtGui.QFont.Weight.DemiBold
    if value >= 500:
        return QtGui.QFont.Weight.Medium
    return QtGui.QFont.Weight.Normal


_UI_FONT_FAMILY = "Inter"
_NUMERIC_FONT_FAMILY = "Iosevka"
_NUMERIC_FONT_FAMILIES: dict[tuple[str, int], str] = {}


def _numeric_font_family(width: str, weight: int) -> str:
    """Return the closest bundled Iosevka static face registered with Qt."""
    normalized_width = "extended" if width == "extended" else "normal"
    candidates = (500, 600) if normalized_width == "extended" else (400, 500, 600, 700)
    nearest = min(candidates, key=lambda candidate: abs(candidate - int(weight)))
    return (
        _NUMERIC_FONT_FAMILIES.get((normalized_width, nearest))
        or _NUMERIC_FONT_FAMILY
        or "Iosevka"
    )


def _set_font_feature(font: QtGui.QFont, tag: str, value: int) -> None:
    """Set one OpenType feature when supported by the runtime Qt version."""
    setter = getattr(font, "setFeature", None)
    tag_type = getattr(QtGui.QFont, "Tag", None)
    if not callable(setter) or tag_type is None:
        return
    feature_tag = None
    factory = getattr(tag_type, "fromString", None)
    if callable(factory):
        try:
            feature_tag = factory(tag)
        except (TypeError, ValueError):
            feature_tag = None
    if feature_tag is None:
        factory = getattr(tag_type, "fromValue", None)
        if callable(factory):
            try:
                feature_tag = factory(int.from_bytes(tag.encode("ascii"), "big"))
            except (TypeError, ValueError):
                feature_tag = None
    if feature_tag is not None:
        try:
            setter(feature_tag, int(value))
        except (TypeError, ValueError):
            # PySide bindings before feature-tag support matured may expose the
            # method without accepting the Tag wrapper. Keep font loading safe.
            pass


def _apply_numeric_opentype_features(font: QtGui.QFont) -> None:
    # Financial numerics benefit from an unmistakable zero and uniform lining
    # figures. Contextual/discretionary alternates and fraction substitution can
    # make rapidly changing values less stable, so keep them explicitly disabled.
    for tag, value in (
        ("zero", 1),
        ("lnum", 1),
        ("onum", 0),
        ("calt", 0),
        ("dlig", 0),
        ("frac", 0),
    ):
        _set_font_feature(font, tag, value)


class TypographyController(QtCore.QObject):
    """Single runtime authority for Nightwatch text roles and raster settings."""

    changed = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self._profiles = {role: dict(values) for role, values in TYPOGRAPHY_DEFAULTS.items()}
        self._globals = dict(TYPOGRAPHY_GLOBAL_DEFAULTS)
        # QFont construction resolves family fallbacks, style strategy and font
        # database metadata. Keep one fully configured template per semantic
        # request and return a cheap implicitly-shared copy to callers so a
        # caller that mutates its font cannot poison the cached template.
        self._font_cache: dict[tuple[str, bool, int | None], QtGui.QFont] = {}

    def configure(
        self,
        overrides: dict[str, dict[str, Any]] | None = None,
        global_overrides: dict[str, Any] | None = None,
        *,
        notify: bool = True,
    ) -> None:
        self._profiles = {role: dict(values) for role, values in TYPOGRAPHY_DEFAULTS.items()}
        for role, values in (overrides or {}).items():
            if role not in self._profiles or not isinstance(values, dict):
                continue
            profile = self._profiles[role]
            for key, value in values.items():
                if key in profile:
                    profile[key] = value
            self._sanitize_profile(profile)
        self._globals = dict(TYPOGRAPHY_GLOBAL_DEFAULTS)
        for key, value in (global_overrides or {}).items():
            if key in self._globals:
                self._globals[key] = value
        self._globals["painter_text_antialias"] = bool(
            self._globals.get("painter_text_antialias", True)
        )
        dpi_rounding = str(self._globals.get("dpi_rounding", "auto")).lower()
        if dpi_rounding not in {
            "auto", "round", "passthrough", "round_prefer_floor", "floor", "ceil"
        }:
            dpi_rounding = "auto"
        self._globals["dpi_rounding"] = dpi_rounding
        self.clear_font_cache()
        if notify:
            self.refresh_all()
            self.changed.emit()

    @staticmethod
    def _sanitize_profile(profile: dict[str, Any]) -> None:
        profile["family"] = "numeric" if str(profile.get("family")) == "numeric" else "ui"
        profile["size_mode"] = "pixel" if str(profile.get("size_mode")) == "pixel" else "point"
        try:
            profile["size"] = max(5.0, min(24.0, float(profile.get("size", 9.0))))
        except (TypeError, ValueError):
            profile["size"] = 9.0
        try:
            weight = int(profile.get("weight", 400))
        except (TypeError, ValueError):
            weight = 400
        profile["weight"] = min((400, 500, 600, 700), key=lambda value: abs(value - weight))
        profile["numeric_width"] = (
            "extended" if str(profile.get("numeric_width", "normal")) == "extended" else "normal"
        )
        for key, default, minimum, maximum in (
            ("letter_spacing", 100.0, 75.0, 140.0),
            ("word_spacing", 0.0, -8.0, 20.0),
            ("stretch", 100, 50, 200),
        ):
            try:
                value = float(profile.get(key, default))
            except (TypeError, ValueError):
                value = float(default)
            value = max(float(minimum), min(float(maximum), value))
            profile[key] = int(round(value)) if key == "stretch" else value
        profile["kerning"] = bool(profile.get("kerning", True))
        profile["fixed_pitch"] = bool(profile.get("fixed_pitch", False))
        profile["hinting"] = str(profile.get("hinting", "default"))
        if profile["hinting"] not in {"default", "none", "vertical", "full"}:
            profile["hinting"] = "default"
        profile["antialias"] = str(profile.get("antialias", "default"))
        if profile["antialias"] not in {"default", "prefer", "none"}:
            profile["antialias"] = "default"
        profile["quality"] = str(profile.get("quality", "default"))
        if profile["quality"] not in {"default", "quality", "match"}:
            profile["quality"] = "default"
        profile["no_subpixel"] = bool(profile.get("no_subpixel", False))

    def profile(self, role: str) -> dict[str, Any]:
        return dict(self._profiles.get(role, self._profiles[TextRole.UI_BODY]))

    def globals(self) -> dict[str, Any]:
        return dict(self._globals)

    def clear_font_cache(self) -> None:
        self._font_cache.clear()


    def font(self, role: str, *, emphasized: bool = False, state: str | None = None) -> QtGui.QFont:
        resolved_role = role if role in self._profiles else TextRole.UI_BODY
        state_weight = TYPOGRAPHY_STATE_WEIGHTS.get(state)
        cache_key = (resolved_role, bool(emphasized), state_weight)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return QtGui.QFont(cached)

        profile = dict(self._profiles[resolved_role])
        if emphasized:
            profile["weight"] = 600
        if state_weight is not None:
            profile["weight"] = state_weight
        numeric = profile["family"] == "numeric"
        family = (
            _numeric_font_family(
                str(profile.get("numeric_width", "normal")),
                int(profile["weight"]),
            )
            if numeric
            else _UI_FONT_FAMILY or "Inter"
        )
        # Bundled static TTFs are first choice on both platforms. Keep numeric
        # fallback monospace even before bootstrap or when a glyph is missing.
        fallback = (
            ("Consolas", "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono")
            if os.name == "nt"
            else ("DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono", "Consolas")
        ) if numeric else ("Noto Sans", "DejaVu Sans", "Segoe UI", "Arial")
        font = QtGui.QFont()
        font.setFamilies(list(dict.fromkeys((family, *fallback))))
        font.setFixedPitch(bool(profile["fixed_pitch"]))
        if profile["size_mode"] == "pixel":
            font.setPixelSize(max(1, int(round(float(profile["size"])))))
        else:
            font.setPointSizeF(float(profile["size"]))
        font.setWeight(_weight_enum(int(profile["weight"])))
        font.setStyleHint(
            QtGui.QFont.StyleHint.Monospace
            if profile["family"] == "numeric"
            else QtGui.QFont.StyleHint.SansSerif
        )
        # Let DirectWrite/FreeType choose their native raster path rather than
        # forcing outline selection; explicit tuner rendering choices still apply.
        strategy = QtGui.QFont.StyleStrategy.PreferDefault
        if profile["antialias"] == "prefer":
            strategy |= QtGui.QFont.StyleStrategy.PreferAntialias
        elif profile["antialias"] == "none":
            strategy |= QtGui.QFont.StyleStrategy.NoAntialias
        if profile["quality"] == "quality":
            strategy |= QtGui.QFont.StyleStrategy.PreferQuality
        elif profile["quality"] == "match":
            strategy |= QtGui.QFont.StyleStrategy.PreferMatch
        if profile["no_subpixel"]:
            strategy |= QtGui.QFont.StyleStrategy.NoSubpixelAntialias
        font.setStyleStrategy(strategy)
        hinting = {
            "default": QtGui.QFont.HintingPreference.PreferDefaultHinting,
            "none": QtGui.QFont.HintingPreference.PreferNoHinting,
            "vertical": QtGui.QFont.HintingPreference.PreferVerticalHinting,
            "full": QtGui.QFont.HintingPreference.PreferFullHinting,
        }[profile["hinting"]]
        font.setHintingPreference(hinting)
        font.setLetterSpacing(
            QtGui.QFont.SpacingType.PercentageSpacing,
            float(profile["letter_spacing"]),
        )
        font.setWordSpacing(float(profile["word_spacing"]))
        font.setStretch(int(profile["stretch"]))
        font.setKerning(bool(profile["kerning"]))
        if numeric:
            _apply_numeric_opentype_features(font)
        self._font_cache[cache_key] = QtGui.QFont(font)
        return QtGui.QFont(font)

    def inferred_role(self, widget: QtWidgets.QWidget) -> str | None:
        explicit = str(widget.property("textRole") or "")
        if explicit in TYPOGRAPHY_DEFAULTS:
            return explicit
        named = _LEGACY_TEXT_ROLES.get(widget.objectName())
        if named:
            return named
        if isinstance(widget, QtWidgets.QHeaderView):
            return TextRole.UI_LABEL
        if isinstance(widget, QtWidgets.QTableView):
            # Mixed-content tables are prose-first. Numeric columns should opt
            # into TABLE_VALUE through item/model FontRole instead of forcing
            # symbols, status and descriptions into the monospace data font.
            return TextRole.TABLE_TEXT
        if isinstance(widget, QtWidgets.QAbstractSpinBox):
            return TextRole.MARKET_VALUE
        if isinstance(widget, QtWidgets.QGroupBox):
            return TextRole.UI_LABEL
        if isinstance(widget, QtWidgets.QAbstractButton):
            return TextRole.UI_CONTROL
        if isinstance(widget, (QtWidgets.QComboBox, QtWidgets.QTabBar)):
            return TextRole.UI_CONTROL
        if isinstance(widget, QtWidgets.QLabel):
            return TextRole.UI_BODY
        if isinstance(widget, QtWidgets.QLineEdit):
            return TextRole.UI_BODY
        if isinstance(widget, QtWidgets.QListView):
            return TextRole.UI_BODY
        return None

    def apply_widget(self, widget: QtWidgets.QWidget) -> None:
        role = self.inferred_role(widget)
        if role is not None:
            widget.setFont(self.font(role))
        if isinstance(widget, QtWidgets.QTableView):
            for getter_name in ("horizontalHeader", "verticalHeader"):
                getter = getattr(widget, getter_name, None)
                if callable(getter):
                    header = getter()
                    if header is not None:
                        header.setProperty("textRole", TextRole.UI_LABEL)
                        header.setFont(self.font(TextRole.UI_LABEL))
        if isinstance(widget, QtWidgets.QComboBox):
            view = widget.view()
            if view is not None and role is not None:
                view.setFont(self.font(role))

    def refresh_all(self) -> None:
        application = QtWidgets.QApplication.instance()
        if application is None:
            return
        application.setFont(self.font(TextRole.UI_BODY))
        for window in application.topLevelWidgets():
            apply_typography(window)


_TYPOGRAPHY_CONTROLLER = TypographyController()


def typography_controller() -> TypographyController:
    return _TYPOGRAPHY_CONTROLLER


def typography_font(role: str, *, emphasized: bool = False, state: str | None = None) -> QtGui.QFont:
    """Return the centrally defined font for one semantic text role."""
    return _TYPOGRAPHY_CONTROLLER.font(role, emphasized=emphasized, state=state)


def typography_font_at_pixel_size(font: QtGui.QFont, pixel_size: int) -> QtGui.QFont:
    """Return a copy resized for adaptive layout without changing role defaults."""
    resized = QtGui.QFont(font)
    resized.setPixelSize(max(1, int(pixel_size)))
    return resized


def typography_min_pixel_size(role: str) -> int:
    """Return a centralized adaptive lower bound for pixel-sized text."""
    limits = TYPOGRAPHY_LIMITS.get(role, {})
    return max(1, int(limits.get("min_pixel_size", 1)))


def typography_state_weight(name: str) -> int:
    """Return a centralized QSS/state weight for non-role transient emphasis."""
    return int(TYPOGRAPHY_STATE_WEIGHTS.get(str(name), 400))


def typography_state_opacity(name: str) -> float:
    """Return the central text intensity for one presentation state."""
    return float(TYPOGRAPHY_STATE_OPACITIES.get(str(name), 1.0))


def configure_typography(
    overrides: dict[str, dict[str, Any]] | None = None,
    global_overrides: dict[str, Any] | None = None,
    *,
    notify: bool = True,
) -> None:
    _TYPOGRAPHY_CONTROLLER.configure(overrides, global_overrides, notify=notify)


def set_text_role(widget: QtWidgets.QWidget, role: str) -> None:
    if role not in TYPOGRAPHY_DEFAULTS:
        raise ValueError(f"Unknown text role: {role}")
    widget.setProperty("textRole", role)
    _TYPOGRAPHY_CONTROLLER.apply_widget(widget)


def apply_typography(root: QtCore.QObject) -> None:
    if isinstance(root, QtWidgets.QWidget):
        _TYPOGRAPHY_CONTROLLER.apply_widget(root)
    for widget in root.findChildren(QtWidgets.QWidget):
        _TYPOGRAPHY_CONTROLLER.apply_widget(widget)


def apply_text_render_hints(painter: QtGui.QPainter) -> None:
    painter.setRenderHint(
        QtGui.QPainter.RenderHint.TextAntialiasing,
        bool(_TYPOGRAPHY_CONTROLLER.globals().get("painter_text_antialias", True)),
    )


def _font_path(font_root: str, filename: str) -> str:
    path = os.path.join(font_root, filename)
    if os.path.isfile(path):
        return path
    raise ValueError(
        f"Missing bundled font: {filename}. Place all Nightwatch font files in "
        f"{font_root!r}, then restart."
    )


def _register_font(font_root: str, filename: str) -> str:
    identifier = QtGui.QFontDatabase.addApplicationFont(_font_path(font_root, filename))
    families = QtGui.QFontDatabase.applicationFontFamilies(identifier)
    if not families:
        raise ValueError(f"Qt could not load {filename} from {font_root!r}.")
    return families[0]


def _apply_typography_if_alive(widget: QtWidgets.QWidget) -> None:
    """Apply a deferred font role only while the wrapped Qt object still exists."""
    try:
        import shiboken6

        if not shiboken6.isValid(widget):
            return
        _TYPOGRAPHY_CONTROLLER.apply_widget(widget)
    except RuntimeError as exc:
        message = str(exc)
        if "Internal C++ object" in message and "already deleted" in message:
            return
        raise


class _TypographyRoleFilter(QtCore.QObject):
    """Re-apply semantic roles after Qt/QSS polishes dynamic widgets."""

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if (
            event.type() in (QtCore.QEvent.Type.Polish, QtCore.QEvent.Type.StyleChange)
            and isinstance(watched, QtWidgets.QWidget)
        ):
            if (
                event.type() == QtCore.QEvent.Type.StyleChange
                and not watched.testAttribute(QtCore.Qt.WidgetAttribute.WA_WState_Polished)
            ):
                return super().eventFilter(watched, event)
            QtCore.QTimer.singleShot(
                0,
                watched,
                lambda widget=watched: _apply_typography_if_alive(widget),
            )
        return super().eventFilter(watched, event)


def load_app_fonts(application: QtGui.QGuiApplication, package_root: str) -> None:
    """Register every application font from project-root ``fonts/``."""
    global _UI_FONT_FAMILY, _NUMERIC_FONT_FAMILY
    project_root = os.path.dirname(os.path.abspath(package_root))
    font_root = os.path.join(project_root, "fonts")
    registered = {}
    for key, filename in TYPOGRAPHY_FONT_FILES.items():
        try:
            registered[key] = _register_font(font_root, filename)
        except ValueError:
            # Source checkouts and packaged builds without optional fonts must
            # still start. Resolve resources independently of the launch CWD.
            fallback = QtGui.QFontDatabase.SystemFont.FixedFont if key.startswith("numeric") else QtGui.QFontDatabase.SystemFont.GeneralFont
            registered[key] = QtGui.QFontDatabase.systemFont(fallback).family()
    _UI_FONT_FAMILY = registered["ui_regular"]
    _NUMERIC_FONT_FAMILY = registered["numeric_regular"]
    _NUMERIC_FONT_FAMILIES.clear()
    _NUMERIC_FONT_FAMILIES.update({
        ("normal", 400): registered["numeric_regular"],
        ("normal", 500): registered["numeric_medium"],
        ("normal", 600): registered["numeric_semibold"],
        ("normal", 700): registered["numeric_bold"],
        ("extended", 500): registered["numeric_extended_medium"],
        ("extended", 600): registered["numeric_extended_semibold"],
    })

    # leadership.py is intentionally untouched; keep its legacy constants in
    # sync without allowing constants.py to own typography configuration.
    constants.UI_FONT_FAMILY = _UI_FONT_FAMILY
    constants.NUMERIC_FONT_FAMILY = _NUMERIC_FONT_FAMILY

    # Family identities can change after bundled-font registration. Discard any
    # bootstrap-era templates before the application asks for its first font.
    _TYPOGRAPHY_CONTROLLER.clear_font_cache()
    application.setFont(typography_font(TextRole.UI_BODY))
    font_filter = _TypographyRoleFilter(application)
    application.installEventFilter(font_filter)
    application._nightwatch_typography_filter = font_filter


# ========================================================================
# utilities
# ========================================================================
import math
import os

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt


def device_pixel_value(widget: QtWidgets.QWidget, value: float) -> float:
    """Snap one logical coordinate to the nearest physical device pixel."""
    ratio = max(1.0, float(widget.devicePixelRatioF()))
    return round(float(value) * ratio) / ratio


def device_pixel_rect(
    widget: QtWidgets.QWidget,
    rect: QtCore.QRectF,
) -> QtCore.QRectF:
    """Snap a text rectangle's edges to the physical device-pixel grid."""
    left = device_pixel_value(widget, rect.left())
    top = device_pixel_value(widget, rect.top())
    right = device_pixel_value(widget, rect.right())
    bottom = device_pixel_value(widget, rect.bottom())
    return QtCore.QRectF(left, top, max(0.0, right - left), max(0.0, bottom - top))


class ElidedLabel(QtWidgets.QLabel):
    """Single-line label that never paints beyond its assigned space."""

    def __init__(
        self,
        text: str = "",
        parent: QtWidgets.QWidget | None = None,
    ):
        super().__init__(parent)
        self._full_text = str(text)
        self._auto_tooltip = False
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self._refresh_text()

    def setText(self, text: str) -> None:
        self._full_text = str(text)
        self._refresh_text()

    def full_text(self) -> str:
        return self._full_text

    def setToolTip(self, text: str) -> None:
        self._auto_tooltip = False
        super().setToolTip(text)

    def _refresh_text(self) -> None:
        width = max(0, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(
            self._full_text,
            Qt.TextElideMode.ElideRight,
            width,
        )
        super().setText(shown)
        if shown != self._full_text and (not self.toolTip() or self._auto_tooltip):
            super().setToolTip(self._full_text)
            self._auto_tooltip = True
        elif shown == self._full_text and self._auto_tooltip:
            super().setToolTip("")
            self._auto_tooltip = False

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (
            QtCore.QEvent.Type.FontChange,
            QtCore.QEvent.Type.ApplicationFontChange,
        ):
            self._refresh_text()


def alpha_color(value: str, alpha: int) -> QtGui.QColor:
    color = QtGui.QColor(value)
    color.setAlpha(max(0, min(255, alpha)))
    return color


INSTRUMENT_BAR_HEIGHT = 42


class MainToolbar(QtWidgets.QFrame):
    """Compact global navigation, independent of the chart/right-panel split."""

    HEIGHT = 28

    def __init__(self, navigation: QtWidgets.QWidget, layout_control: QtWidgets.QWidget,
                 settings_control: QtWidgets.QWidget, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("mainToolbar")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        navigation.setFixedHeight(self.HEIGHT - 1)
        for button in navigation.findChildren(QtWidgets.QPushButton):
            button.setFixedHeight(self.HEIGHT - 1)
        row.addWidget(navigation)
        row.addStretch(1)
        for control in (layout_control, settings_control):
            control.setFixedSize(self.HEIGHT, self.HEIGHT - 1)
            row.addWidget(control)
        self.apply_theme({})

    def apply_theme(self, theme: dict[str, str]) -> None:
        text = theme.get("text", "#EDEDED")
        muted = theme.get("muted", "#8E8E96")
        border = theme.get("border", "#262629")
        hover = theme.get("control_hover", "#1E1E22")
        active = theme.get("active_line", text)
        # Scope all rules locally so the instrument cards retain their styling.
        self.setStyleSheet(f"""
            QFrame#mainToolbar {{ background: #000000; border: 0;
                border-bottom: 1px solid {border}; }}
            QFrame#workspaceNav {{ background: #000000; border: 0; padding: 0; }}
            QPushButton#workspaceNavButton {{ background: transparent; color: {muted};
                border: 0; border-bottom: 1px solid transparent;
                padding: 0 8px; margin: 0; border-radius: 0; }}
            QPushButton#workspaceNavButton:hover {{ color: {text}; background: {hover}; }}
            QPushButton#workspaceNavButton:checked {{ color: {text}; border-bottom-color: {active}; }}
            QPushButton#topUtilityButton {{ background: transparent; color: {text};
                border: 0; padding: 0; margin: 0; border-radius: 0; }}
            QPushButton#topUtilityButton:hover {{ background: {hover}; }}
        """)


class InstrumentBar(QtWidgets.QFrame):
    """Chart-only timeframes, ticker identity and existing market-data cards."""

    def __init__(self, stats: QtWidgets.QWidget):
        super().__init__()
        self.setObjectName("instrumentBar")
        self.stats = stats
        self.timeframes = getattr(stats, "timeframe_selector", None)
        self.row = QtWidgets.QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(0)
        self.row.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)
        self._normal_horizontal_margins = (0, 0)
        self._chart_context_visible = True
        self._responsive_layout_active = False
        self._responsive_state = None

        cards = getattr(stats, "cards", {})
        cards = cards if isinstance(cards, dict) else {}
        self.identity_control = cards.get("last")
        self.metric_controls = [cards[name] for name in ("volume", "oi", "long_short", "funding")
                                if name in cards]
        stats_layout = stats.layout()
        for control in (self.timeframes, self.identity_control, *self.metric_controls):
            if control is not None:
                if stats_layout is not None:
                    stats_layout.removeWidget(control)
                control.setParent(self)

        self.context_slot = QtWidgets.QWidget(self)
        self.context_slot.setObjectName("instrumentContextSlot")
        context_layout = QtWidgets.QHBoxLayout(self.context_slot)
        context_layout.setContentsMargins(0, 0, 0, 0)
        context_layout.setSpacing(0)
        if self.timeframes is not None:
            self.timeframes.setFixedHeight(INSTRUMENT_BAR_HEIGHT)
            context_layout.addWidget(self.timeframes)
        self.row.addWidget(self.context_slot, 0)
        if self.identity_control is not None:
            # The identity absorbs spare width; metric cards stay equally sized.
            self.identity_control.setProperty("instrumentFlexOwned", True)
            self.identity_control.setMaximumWidth(16777215)
            self.identity_control.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            self.row.addWidget(self.identity_control, 1)
        for card in self.metric_controls:
            self.row.addWidget(card, 0)
        stats.setParent(self)
        stats.hide()
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(INSTRUMENT_BAR_HEIGHT)
        QtCore.QTimer.singleShot(0, self._apply_responsive_layout)

    @staticmethod
    def _stable_widget_width(widget: QtWidgets.QWidget | None) -> int:
        if widget is None:
            return 0
        return max(0, widget.minimumWidth(), widget.minimumSizeHint().width(), widget.sizeHint().width())

    def _timeframe_width(self, *, collapsed: bool, tight: bool = False) -> int:
        if self.timeframes is None:
            return 0
        method = getattr(self.timeframes, "collapsedWidth" if collapsed else "expandedWidth", None)
        if callable(method):
            if collapsed:
                return max(0, int(method()))
            try:
                return max(0, int(method(tight=tight)))
            except TypeError:
                return max(0, int(method()))
        return self._stable_widget_width(self.timeframes)

    def _apply_responsive_layout(self) -> None:
        if self._responsive_layout_active or not self._chart_context_visible:
            return
        self._responsive_layout_active = True
        try:
            left, right = self._normal_horizontal_margins
            identity_width = self._stable_widget_width(self.identity_control)
            metric_widths = [self._stable_widget_width(card) for card in self.metric_controls]
            normal_width = self._timeframe_width(collapsed=False)
            tight_width = self._timeframe_width(collapsed=False, tight=True)
            collapsed_width = self._timeframe_width(collapsed=True)
            compact = self.width() < left + right + normal_width + identity_width + sum(metric_widths)
            timeframe_width = tight_width if compact else normal_width
            required = (0 if compact else left + right) + timeframe_width + identity_width + sum(metric_widths)
            count = len(self.metric_controls)
            while self.width() < required and count:
                count -= 1
                required -= metric_widths[count]
            identity_visible = self.identity_control is not None
            if self.width() < required and identity_visible:
                identity_visible = False
                required -= identity_width
            collapsed = self.width() < required and timeframe_width > collapsed_width
            if collapsed:
                timeframe_width = collapsed_width
            state = (compact, count, identity_visible, collapsed, timeframe_width)
            if state == self._responsive_state:
                return
            self._responsive_state = state
            self.row.setContentsMargins(0 if compact else left, 0, 0 if compact else right, 0)
            set_tight = getattr(self.timeframes, "setTightSpacing", None)
            if callable(set_tight):
                set_tight(compact)
            set_collapsed = getattr(self.timeframes, "setCollapsed", None)
            if callable(set_collapsed):
                set_collapsed(collapsed)
            self.context_slot.setFixedWidth(timeframe_width)
            if self.identity_control is not None:
                self.identity_control.setVisible(identity_visible)
            for index, card in enumerate(self.metric_controls):
                card.setVisible(index < count)
            self.row.invalidate()
        finally:
            self._responsive_layout_active = False

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def set_chart_context_visible(self, visible: bool) -> None:
        self._chart_context_visible = bool(visible)
        self.setVisible(self._chart_context_visible)
        if visible:
            self._responsive_state = None
            self._apply_responsive_layout()

    def set_developer_geometry(self, *, left=7, top=4, right=7, bottom=4,
                               spacing=4, height=INSTRUMENT_BAR_HEIGHT) -> None:
        # Keep card height and adjacency stable while retaining tuner margins.
        self._normal_horizontal_margins = (max(0, int(left)), max(0, int(right)))
        self._responsive_state = None
        self._apply_responsive_layout()


class ChartSurfaceHost(QtWidgets.QFrame):
    """Border/style host; chart lifecycle remains owned by its content."""

    def __init__(self, content: QtWidgets.QWidget, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chartSurfaceHost")
        self.content = content
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(content)


def ensure_frameless_close_button(window: QtWidgets.QWidget) -> QtWidgets.QAbstractButton | None:
    """Ensure a frameless top-level window has Nightwatch's standard X control.

    Windows that already provide an in-layout button named ``framelessCloseButton``
    keep ownership of its placement. Other frameless windows receive a small
    overlay button positioned by :func:`position_frameless_close_button`.
    """
    existing = window.findChild(QtWidgets.QAbstractButton, "framelessCloseButton")
    if existing is not None:
        return existing
    button = QtWidgets.QToolButton(window)
    button.setObjectName("framelessCloseButton")
    set_text_role(button, TextRole.UI_GLYPH)
    button.setText("×")
    button.setAccessibleName("Close window · Escape")
    button.setToolTip("Close · Esc")
    button.setAutoRaise(True)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setFixedSize(28, 26)
    button.clicked.connect(window.close)
    setattr(window, "_nightwatch_overlay_close_button", button)
    button.show()
    position_frameless_close_button(window)
    return button


def position_frameless_close_button(window: QtWidgets.QWidget) -> None:
    """Keep an injected close button in the top-right chrome corner."""
    button = getattr(window, "_nightwatch_overlay_close_button", None)
    if not isinstance(button, QtWidgets.QAbstractButton):
        return
    button.move(max(4, window.width() - button.width() - 7), 6)
    button.raise_()


class TerminalStatusBar(QtWidgets.QStatusBar):
    """Retain QStatusBar's timed message API alongside persistent market status."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("terminalStatusBar")
        self.setSizeGripEnabled(False)
        surface = QtWidgets.QWidget(self)
        surface.setObjectName("statusContent")
        row = QtWidgets.QHBoxLayout(surface)
        self._content_layout = row
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(10)
        self.connection = QtWidgets.QLabel("● Connecting")
        self.connection.setObjectName("connectionStatus")
        self.connection.setProperty("live", False)
        self.venue = QtWidgets.QLabel("Binance")
        self.venue.setObjectName("statusVenue")
        self.latency = QtWidgets.QLabel("Feed — · App —")
        self.latency.setObjectName("terminalLatency")
        self.fps = QtWidgets.QLabel("FPS idle")
        self.fps.setObjectName("statusFrameRate")
        self.fps.setToolTip(
            "Recent dirty-driven Nightwatch presentation transactions / active display refresh. "
            "Idle means no visual transaction was needed in the last second."
        )
        self.latency.setToolTip(
            "Feed: Binance event → local WebSocket arrival, corrected with the synchronized Binance clock.\n"
            "App: local WebSocket arrival → GUI depth handler. Not ping RTT."
        )
        self.message_label = ElidedLabel()
        self.message_label.setObjectName("statusMessage")
        self.message_label.setMinimumWidth(0)
        self.market = QtWidgets.QLabel("Market: —")
        self.market.setObjectName("statusMarket")
        self._separators: list[QtWidgets.QFrame] = []
        for widget in (self.connection, self.venue, self.latency, self.fps):
            row.addWidget(widget)
            divider = QtWidgets.QFrame()
            divider.setObjectName("statusSeparator")
            divider.setFixedSize(1, 10)
            self._separators.append(divider)
            row.addWidget(divider)
        row.addWidget(self.message_label, 1)
        row.addWidget(self.market)
        self.addPermanentWidget(surface, 1)
        self.messageChanged.connect(self.message_label.setText)
        self._last_latency_update = 0.0
        self._latency_stale = True
        self.latency.setProperty("stale", True)
        set_text_role(self.connection, TextRole.STATUS_TEXT)
        set_text_role(self.venue, TextRole.STATUS_TEXT)
        set_text_role(self.market, TextRole.STATUS_TEXT)
        set_text_role(self.message_label, TextRole.STATUS_MESSAGE)
        set_text_role(self.latency, TextRole.STATUS_LATENCY)
        set_text_role(self.fps, TextRole.STATUS_LATENCY)

    def apply_tuning(self, values: dict[str, object]) -> None:
        """Apply validated developer geometry and semantic font roles live."""
        self.setFixedHeight(int(values["height"]))
        self._content_layout.setContentsMargins(
            int(values["padding_left"]), int(values["padding_top"]),
            int(values["padding_right"]), int(values["padding_bottom"]),
        )
        self._content_layout.setSpacing(int(values["spacing"]))
        separator_height = max(8, min(10, int(values["height"]) - 12))
        for divider in self._separators:
            divider.setFixedHeight(separator_height)
        text_role = str(values["text_font_role"])
        for widget in (self.connection, self.venue, self.market):
            set_text_role(widget, text_role)
        set_text_role(self.message_label, str(values["message_font_role"]))
        set_text_role(self.latency, str(values["latency_font_role"]))
        set_text_role(self.fps, str(values["latency_font_role"]))

    def set_fps(self, actual: float, target: float, *, active: bool) -> None:
        target_text = f"{target:.0f}" if target > 0 else "—"
        if active:
            actual_text = f"{max(0.0, actual):.0f}"
            text = f"FPS {actual_text}/{target_text}"
        else:
            text = f"FPS idle/{target_text}"
        if self.fps.text() != text:
            self.fps.setText(text)

    def _set_latency_stale(self, stale: bool) -> None:
        if stale == self._latency_stale:
            return
        self._latency_stale = stale
        self.latency.setProperty("stale", stale)
        self.latency.style().unpolish(self.latency)
        self.latency.style().polish(self.latency)

    def set_connection(self, live: bool, text: str, market_open: bool) -> None:
        state = "Connected" if live else "Paused" if text == "HISTORY DOWNLOAD" else "Connecting"
        if not live and ("OFFLINE" in text or "RECONNECT" in text):
            state = "Reconnecting"
        connection_text = f"● {state}"
        if self.connection.text() != connection_text:
            self.connection.setText(connection_text)
        live = bool(live)
        if bool(self.connection.property("live")) != live:
            self.connection.setProperty("live", live)
            style = self.connection.style()
            style.unpolish(self.connection)
            style.polish(self.connection)
        market_text = "Market: Open" if market_open else "Market: -"
        if self.market.text() != market_text:
            self.market.setText(market_text)
        if not live:
            self._set_latency_stale(True)
            if self.latency.text() != "Feed — · App —":
                self.latency.setText("Feed — · App —")

    def set_event_latency(
        self,
        event_ms: float,
        server_received_ms: float,
        socket_mono_ms: float,
        parser_done_mono_ms: float,
        gui_mono_ms: float,
        *,
        latency: dict[str, Any] | None = None,
    ) -> None:
        """Show upstream delivery separately from local application delay.

        ``event_ms`` and ``server_received_ms`` are Binance epoch milliseconds.
        Every ``*_mono_ms`` stamp is local monotonic time. The two domains are
        never subtracted from each other. QWebSocket callback time is the
        earliest receipt boundary available to Nightwatch.
        """
        if gui_mono_ms <= 0 or gui_mono_ms - self._last_latency_update < 1000.0:
            return
        self._last_latency_update = gui_mono_ms
        telemetry = latency if isinstance(latency, dict) else {}
        clock = telemetry.get("clock")
        if not isinstance(clock, dict):
            clock = {}
        clock_fresh = bool(clock.get("fresh")) if clock else server_received_ms > 0.0

        feed_delay: float | None = None
        measured_upstream = telemetry.get("upstream_ms")
        if measured_upstream is not None:
            try:
                measured_upstream = float(measured_upstream)
            except (TypeError, ValueError):
                measured_upstream = None
            if measured_upstream is not None and measured_upstream >= 0.0 and clock_fresh:
                feed_delay = measured_upstream
        elif clock_fresh and event_ms > 0.0 and server_received_ms > 0.0:
            candidate = server_received_ms - event_ms
            if -100.0 <= candidate < 60_000.0:
                feed_delay = max(0.0, candidate)

        app_delay: float | None = None
        if socket_mono_ms > 0.0 and gui_mono_ms >= socket_mono_ms:
            app_delay = gui_mono_ms - socket_mono_ms

        feed_text = f"{feed_delay:,.0f} ms" if feed_delay is not None else "—"
        app_text = (
            f"{app_delay:,.0f} ms"
            if app_delay is not None and app_delay < 60_000.0
            else "—"
        )
        latency_text = f"Feed {feed_text} · App {app_text}"
        if self.latency.text() != latency_text:
            self.latency.setText(latency_text)

        self._set_latency_stale(
            feed_delay is None
            or app_delay is None
            or app_delay >= 60_000.0
            or (bool(clock) and not clock_fresh)
        )

        enqueue_mono_ms = float(telemetry.get("enqueue_mono_ms") or 0.0)
        worker_start_mono_ms = float(telemetry.get("worker_start_mono_ms") or 0.0)
        worker_resume_mono_ms = float(telemetry.get("worker_resume_mono_ms") or 0.0)
        gui_dispatch_mono_ms = float(telemetry.get("gui_dispatch_mono_ms") or 0.0)
        callback_to_enqueue: float | None = None
        worker_queue: float | None = None
        worker_processing: float | None = None
        worker_to_gui: float | None = None
        gui_to_status: float | None = None
        if enqueue_mono_ms >= socket_mono_ms > 0.0:
            callback_to_enqueue = enqueue_mono_ms - socket_mono_ms
        if worker_start_mono_ms >= enqueue_mono_ms > 0.0:
            worker_queue = worker_start_mono_ms - enqueue_mono_ms
        processing_start = max(worker_start_mono_ms, worker_resume_mono_ms)
        if parser_done_mono_ms >= processing_start > 0.0:
            worker_processing = parser_done_mono_ms - processing_start
        if gui_dispatch_mono_ms >= parser_done_mono_ms > 0.0:
            worker_to_gui = gui_dispatch_mono_ms - parser_done_mono_ms
        if gui_mono_ms >= gui_dispatch_mono_ms > 0.0:
            gui_to_status = gui_mono_ms - gui_dispatch_mono_ms

        detail = [
            "Feed = Binance event timestamp → Nightwatch's Qt WebSocket callback,",
            "using the cached Binance clock estimate. This is not a raw network RTT.",
            "App = Qt WebSocket callback → status/depth handler, using monotonic time.",
        ]
        for label, value in (
            ("Callback → worker enqueue", callback_to_enqueue),
            ("Worker queue", worker_queue),
            ("Worker processing/publish", worker_processing),
            ("Worker → GUI dispatch", worker_to_gui),
            ("GUI dispatch → status handler", gui_to_status),
        ):
            if value is not None:
                detail.append(f"{label}: {value:,.2f} ms")

        if clock:
            age_s = clock.get("age_s")
            offset_ms = clock.get("offset_ms")
            rtt_ms = clock.get("rtt_ms")
            if offset_ms is not None:
                detail.append(f"Binance clock offset: {float(offset_ms):,.1f} ms")
            if age_s is not None:
                detail.append(f"Clock sync age: {float(age_s):,.1f} s")
            if rtt_ms is not None:
                detail.append(f"Last clock-sync RTT: {float(rtt_ms):,.1f} ms")
            if rtt_ms is not None:
                detail.append("Clock estimate assumes approximately symmetric request/response path delay.")
            last_error = str(clock.get("last_error") or "")
            if last_error:
                detail.append(f"Last clock-sync error: {last_error}")
        if feed_delay is None:
            detail.append("Feed timing is unavailable until the Binance clock estimate is fresh.")
        detail_text = "\n".join(detail)
        if self.latency.toolTip() != detail_text:
            self.latency.setToolTip(detail_text)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        # The message is rendered in our eliding label. Let QStatusBar continue
        # managing its timeout/signals without drawing a second copy over it.
        option = QtWidgets.QStyleOption()
        option.initFrom(self)
        painter = QtGui.QPainter(self)
        self.style().drawPrimitive(QtWidgets.QStyle.PrimitiveElement.PE_Widget, option, painter, self)


def line_icon_pixmap(
    kind: str,
    color: str,
    device_ratio: float = 1.0,
    logical_size: int = 18,
) -> QtGui.QPixmap:
    ratio = max(1.0, device_ratio)
    pixmap = QtGui.QPixmap(
        round(logical_size * ratio),
        round(logical_size * ratio),
    )
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setCosmetic(True)
    pen.setWidthF(1.45)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind == 'filter':
        path = QtGui.QPainterPath(QtCore.QPointF(2.0, 3.0))
        path.lineTo(16.0, 3.0)
        path.lineTo(10.8, 9.1)
        path.lineTo(10.8, 14.0)
        path.lineTo(7.2, 16.0)
        path.lineTo(7.2, 9.1)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == 'panels':
        painter.drawRoundedRect(QtCore.QRectF(2.0, 2.5, 14.0, 13.0), 1.0, 1.0)
        painter.drawLine(QtCore.QPointF(10.5, 2.5), QtCore.QPointF(10.5, 15.5))
        painter.drawLine(QtCore.QPointF(10.5, 9.0), QtCore.QPointF(16.0, 9.0))
    elif kind == 'gear':
        center = QtCore.QPointF(9.0, 9.0)
        painter.drawEllipse(center, 4.7, 4.7)
        painter.drawEllipse(center, 1.7, 1.7)
        for index in range(8):
            angle = math.radians(index * 45.0)
            inner = QtCore.QPointF(
                center.x() + math.cos(angle) * 5.5,
                center.y() + math.sin(angle) * 5.5,
            )
            outer = QtCore.QPointF(
                center.x() + math.cos(angle) * 7.3,
                center.y() + math.sin(angle) * 7.3,
            )
            painter.drawLine(inner, outer)
    painter.end()
    return pixmap


def line_icon(
    kind: str,
    color: str,
    device_ratio: float = 1.0,
    active_color: str | None = None,
    active_kind: str | None = None,
) -> QtGui.QIcon:
    icon = QtGui.QIcon()
    icon.addPixmap(
        line_icon_pixmap(kind, color, device_ratio),
        QtGui.QIcon.Mode.Normal,
        QtGui.QIcon.State.Off,
    )
    icon.addPixmap(
        line_icon_pixmap(active_kind or kind, active_color or color, device_ratio),
        QtGui.QIcon.Mode.Normal,
        QtGui.QIcon.State.On,
    )
    return icon


def application_data_directory() -> str:
    path = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.AppDataLocation
    )
    if not path:
        path = os.path.join(os.path.abspath(os.curdir), "nightwatch_data")
    os.makedirs(path, exist_ok=True)
    return path


import threading
import time
from collections import Counter, defaultdict, deque
from typing import Any


class DiagnosticsHub:
    """Thread-safe counters, gauges and concise event history for the Developer panel."""

    ERROR = 0
    NORMAL = 1
    VERBOSE = 2

    def __init__(self, max_events: int = 300) -> None:
        self._lock = threading.Lock()
        self._events: deque[tuple[float, int, str, str, str]] = deque(maxlen=max_events)
        self._counters: Counter[str] = Counter()
        self._timings: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=120))
        self._gauges: dict[str, Any] = {}

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[str(key)] += int(amount)

    def observe_ms(self, key: str, value: float) -> None:
        value = float(value)
        if value < 0.0:
            return
        with self._lock:
            self._timings[str(key)].append(value)


    def gauges(self, values: dict[str, Any]) -> None:
        """Record several telemetry gauges under one lock acquisition."""
        if not values:
            return
        with self._lock:
            self._gauges.update({str(key): value for key, value in values.items()})

    def event(
        self,
        category: str,
        message: str,
        *,
        verbosity: int = NORMAL,
        kind: str = "INFO",
    ) -> None:
        text = " ".join(str(message).split())
        if not text:
            return
        with self._lock:
            self._events.append(
                (
                    time.time(),
                    int(verbosity),
                    str(kind).upper(),
                    str(category).upper(),
                    text[:320],
                )
            )

    def error(self, category: str, message: str) -> None:
        self.increment("errors.total")
        self.event(category, message, verbosity=self.ERROR, kind="ERROR")

    def warning(self, category: str, message: str) -> None:
        self.increment("warnings.total")
        self.event(category, message, verbosity=self.NORMAL, kind="WARN")

    def info(self, category: str, message: str) -> None:
        self.event(category, message, verbosity=self.NORMAL, kind="INFO")

    def verbose(self, category: str, message: str) -> None:
        self.event(category, message, verbosity=self.VERBOSE, kind="INFO")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            events = list(self._events)
            timings = {key: tuple(values) for key, values in self._timings.items()}
            gauges = dict(self._gauges)
        return {
            "counters": counters,
            "events": events,
            "timings": timings,
            "gauges": gauges,
        }

    def clear_events(self) -> None:
        with self._lock:
            self._events.clear()


_DIAGNOSTICS: DiagnosticsHub | None = None


def get_diagnostics() -> DiagnosticsHub:
    global _DIAGNOSTICS
    if _DIAGNOSTICS is None:
        _DIAGNOSTICS = DiagnosticsHub()
    return _DIAGNOSTICS

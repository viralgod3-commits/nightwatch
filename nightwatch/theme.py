"""Semantic theme tokens, presets, layout presets and stylesheet generation."""
from __future__ import annotations

from typing import Final
from .constants import ORDERBOOK_FIXED_PALETTE

DEFAULT_ORDERBOOK_THEME_NAME = 'Nightwatch'
_ORDERBOOK_BASE = {
    **ORDERBOOK_FIXED_PALETTE,
    'bg': '#000000', 'surface_top': '#000000', 'surface_raised': '#000000',
    'surface_center': '#000000', 'control': '#000000',
    'control_hover': '#111316', 'control_pressed': '#191C20',
    'dim_price': '#515862', 'bid_text': '#C4EAD5', 'ask_text': '#EDC1CD',
    'price_line': '#C89A47', 'last_price_line': '#444444',
    'badge_text': ORDERBOOK_FIXED_PALETTE['text'],
    'orderflow_absorption': ORDERBOOK_FIXED_PALETTE['mid'],
    'orderflow_stacking': ORDERBOOK_FIXED_PALETTE['purple'],
    'orderflow_pulling': ORDERBOOK_FIXED_PALETTE['amber'],
    'orderflow_depletion': ORDERBOOK_FIXED_PALETTE['ask'],
    'orderflow_wall': ORDERBOOK_FIXED_PALETTE['text'],
}
# Resolve book colors only when preferences change, before raster-process IPC.
ORDERBOOK_THEMES: Final[dict[str, dict[str, str]]] = {
    'Nightwatch': {**_ORDERBOOK_BASE},
}
for _name, _palette in ORDERBOOK_THEMES.items():
    _palette['orderbook_style'] = _name
    # Every palette supplies directional gradients for the heatmap/depth view.
    for _side in ('bid', 'ask'):
        _palette.setdefault(f'{_side}_heat_high', _palette[_side])
        for _kind, _start, _end in (
            ('bar', f'{_side}_fill_strong', _side),
            ('background', f'{_side}_fill', f'{_side}_fill_strong'),
            ('line', f'{_side}_fill_strong', _side),
        ):
            _palette.setdefault(f'{_side}_{_kind}_start', _palette[_start])
            _end_color = _palette[_end]
            if _kind == 'background':
                _end_color = '#' + ''.join(
                    f'{round(int(_palette[_start][i:i + 2], 16) * .75 + int(_end_color[i:i + 2], 16) * .25):02X}'
                    for i in (1, 3, 5))
            _palette.setdefault(f'{_side}_{_kind}_end', _end_color)


def orderbook_palette(source: object = None, theme: dict[str, object] | None = None) -> dict[str, str]:
    """Combine app surfaces with book accents; retain resolved colors across IPC."""
    name = source.get('orderbook_style') if isinstance(source, dict) else source
    palette = dict(ORDERBOOK_THEMES.get(str(name), ORDERBOOK_THEMES[DEFAULT_ORDERBOOK_THEME_NAME]))
    # A resolved book palette may arrive from another widget or raster process.
    # Do not reduce it to its preset name and lose the active app colors.
    if isinstance(source, dict) and 'bid_fill' in source:
        for key, fallback in palette.items():
            if key != 'orderbook_style' and key in source:
                palette[key] = _normalize_hex_color(source[key], fallback)
        return palette
    if theme is None and isinstance(source, dict):
        theme = source
    if theme is None:
        return palette
    resolved = ui_palette(theme)
    mapping = {
        'bg': 'orderbook_bg', 'surface_top': 'panel2', 'surface_raised': 'panel2',
        'surface_center': 'panel2', 'control': 'control', 'control_hover': 'control_hover',
        'control_pressed': 'active', 'control_border': 'control_border',
        'control_hover_line': 'active_line', 'grid': 'book_grid', 'grid_strong': 'border',
        'text': 'orderbook_value_text', 'badge_text': 'orderbook_badge_text',
        'muted': 'orderbook_muted', 'dim_price': 'orderbook_muted',
        'amber': 'amber', 'purple': 'purple', 'last_price_line': 'book_mid',
        'orderflow_absorption': 'orderflow_absorption', 'orderflow_stacking': 'orderflow_stacking',
        'orderflow_pulling': 'orderflow_pulling', 'orderflow_depletion': 'orderflow_depletion',
        'orderflow_wall': 'orderflow_wall',
    }
    palette.update({key: str(resolved[token]) for key, token in mapping.items()})
    # Follow the supplied application palette, including its directional tokens.
    if palette['orderbook_style'] == 'Nightwatch':
        def blend(first: str, second: str, amount: float) -> str:
            return '#' + ''.join(f'{round(int(first[i:i + 2], 16) * (1 - amount) + int(second[i:i + 2], 16) * amount):02X}'
                                 for i in (1, 3, 5))

        palette['mid'] = str(resolved['book_mid'])
        palette['price_line'] = palette['mid']
        for side, token in (('bid', 'book_bid'), ('ask', 'book_ask')):
            accent = str(resolved[token])
            palette[side] = accent
            palette[f'{side}_text'] = accent
            palette[f'{side}_fill'] = blend(palette['bg'], accent, .20)
            palette[f'{side}_fill_strong'] = blend(palette['bg'], accent, .55)
            palette[f'{side}_heat_high'] = accent
            for kind, start, end in (
                ('bar', palette[f'{side}_fill_strong'], accent),
                ('background', palette[f'{side}_fill'], blend(palette['bg'], accent, .30)),
                ('line', palette[f'{side}_fill_strong'], accent),
            ):
                palette[f'{side}_{kind}_start'] = start
                palette[f'{side}_{kind}_end'] = end
    return palette

CANDLE_STYLES: Final[dict[str, dict[str, object]]] = {'Inked': {'width': 0.64, 'body_alpha': 248, 'outline': 0.9, 'wick': 0.85, 'close_tick': True, 'pixel_snap': False, 'antialias': True}, 'Hollow': {'width': 0.74, 'body_alpha': 255, 'outline': 1.0, 'wick': 1.0, 'hollow_up': True, 'hollow_down': False, 'pixel_snap': True, 'antialias': False}, 'Luminous': {'width': 0.6, 'body_alpha': 232, 'outline': 0.85, 'wick': 0.85, 'glow_alpha': 34, 'glow_width': 2.2, 'pixel_snap': False, 'antialias': True}}
import colorsys
UI_COLOR_KEYS = ('bg', 'panel', 'panel2', 'shell_gap', 'grid', 'border', 'control', 'control_top', 'control_hover', 'control_border', 'text', 'muted', 'green', 'red', 'depth_green', 'depth_red', 'cyan', 'info', 'amber', 'purple', 'take_profit', 'risk', 'chart_bg', 'chart_grid', 'chart_border', 'chart_panel2', 'chart_control', 'chart_control_border', 'chart_text', 'chart_muted', 'chart_green', 'chart_red', 'chart_cyan', 'chart_amber', 'chart_purple', 'current_price_line', 'current_price_text', 'reference_price', 'series_1', 'series_2', 'series_3', 'series_4', 'candle_up', 'candle_down', 'topbar', 'header', 'raised', 'separator', 'active', 'active_line', 'book_bid', 'book_ask', 'book_mid', 'book_grid', 'orderbook_bg', 'orderbook_muted', 'orderbook_value_text', 'orderbook_badge_text', 'orderflow_absorption', 'orderflow_stacking', 'orderflow_pulling', 'orderflow_depletion', 'orderflow_wall', 'orderflow_rpi', 'orderflow_entry', 'orderflow_take_profit', 'orderflow_stop', 'orderflow_liquidation', 'orderflow_order', 'rail_neutral', 'rail_buy', 'rail_sell', 'rail_take_profit', 'rail_stop', 'rail_secondary', 'rail_warning', 'metric_price', 'metric_volume', 'metric_taker_buy', 'metric_taker_sell', 'metric_taker_neutral', 'metric_funding_positive', 'metric_funding_negative', 'metric_funding_neutral', )
_DEFAULT_SOURCE_COLORS: dict[str, str] = {'bg': '#07090C', 'panel': '#0C1016', 'panel2': '#11161D', 'grid': '#1C2631', 'border': '#1C2631', 'control': '#171E27', 'control_top': '#1D2631', 'control_hover': '#253141', 'control_border': '#2B3947', 'text': '#EAF0F6', 'muted': '#9AA6B2', 'green': '#28D07F', 'red': '#FF5A6E', 'depth_green': '#28D07F', 'depth_red': '#FF5A6E', 'cyan': '#66C2FF', 'info': '#66C2FF', 'amber': '#F0B34A', 'purple': '#9B8CFF', 'take_profit': '#48D6C2', 'risk': '#FF855A', 'chart_bg': '#07090C', 'chart_grid': '#1B2530', 'chart_border': '#24303D', 'chart_panel2': '#0E131A', 'chart_control': '#111923', 'chart_control_border': '#2B3947', 'chart_text': '#C8D2DC', 'chart_muted': '#7D8996', 'current_price_line': '#66C2FF', 'current_price_text': '#EAF0F6', 'reference_price': '#B7C6D4', 'series_1': '#66C2FF', 'series_2': '#9B8CFF', 'series_3': '#F0B34A', 'series_4': '#4ED6C8', 'orderbook_bg': '#090D12', 'book_grid': '#1C2631', 'orderbook_muted': '#7D8996', 'orderbook_value_text': '#D5DEE7', 'orderbook_badge_text': '#EAF0F6', }
_DERIVED_COLOR_SOURCES: dict[str, str] = {'shell_gap': 'panel2', 'topbar': 'bg', 'header': 'panel2', 'raised': 'control_top', 'separator': 'border', 'active': 'control_hover', 'active_line': 'cyan', 'depth_green': 'green', 'depth_red': 'red', 'chart_green': 'green', 'chart_red': 'red', 'chart_cyan': 'cyan', 'chart_amber': 'amber', 'chart_purple': 'purple', 'current_price_line': 'cyan', 'current_price_text': 'chart_text', 'book_bid': 'green', 'book_ask': 'red', 'book_mid': 'cyan', 'orderflow_absorption': 'info', 'orderflow_stacking': 'purple', 'orderflow_pulling': 'amber', 'orderflow_depletion': 'risk', 'orderflow_wall': 'text', 'orderflow_rpi': 'purple', 'orderflow_entry': 'cyan', 'orderflow_take_profit': 'take_profit', 'orderflow_stop': 'risk', 'orderflow_liquidation': 'risk', 'orderflow_order': 'text', 'rail_neutral': 'cyan', 'rail_buy': 'green', 'rail_sell': 'red', 'rail_take_profit': 'take_profit', 'rail_stop': 'risk', 'rail_secondary': 'purple', 'rail_warning': 'amber', 'candle_up': 'green', 'candle_down': 'red', 'metric_price': 'text', 'metric_volume': 'text', 'metric_taker_buy': 'green', 'metric_taker_sell': 'red', 'metric_taker_neutral': 'text', 'metric_funding_positive': 'green', 'metric_funding_negative': 'red', 'metric_funding_neutral': 'text', }

def _normalize_hex_color(value: object, fallback: str) -> str:
    """Return canonical #RRGGBB, falling back deterministically on invalid input."""
    raw = str(value or '').strip()
    if len(raw) == 7 and raw.startswith('#'):
        try:
            int(raw[1:], 16)
        except ValueError:
            pass
        else:
            return raw.upper()
    return fallback.upper()

def resolve_theme(theme: dict[str, object], overrides: dict[str, object] | None=None) -> dict[str, object]:
    """Resolve one flat runtime palette from explicit source colors.

    Precedence:
        canonical fallbacks
        -> explicit base-theme tokens
        -> explicit developer/user overrides
        -> derive only still-non-explicit semantic children
        -> normalize/validate
    """
    allowed = set(UI_COLOR_KEYS)
    base = {key: value for key, value in dict(theme or {}).items() if key in allowed}
    user = {key: value for key, value in dict(overrides or {}).items() if key in allowed}
    explicit = set(base) | set(user)
    resolved: dict[str, object] = dict(_DEFAULT_SOURCE_COLORS)
    resolved.update(base)
    resolved.update(user)
    for key in UI_COLOR_KEYS:
        if key not in resolved:
            continue
        fallback = _DEFAULT_SOURCE_COLORS.get(key)
        if fallback is None:
            parent = _DERIVED_COLOR_SOURCES.get(key)
            fallback = str(resolved.get(parent, '#000000'))
        resolved[key] = _normalize_hex_color(resolved[key], str(fallback))
    for child, parent in _DERIVED_COLOR_SOURCES.items():
        if child not in explicit:
            resolved[child] = str(resolved[parent])
    for key in UI_COLOR_KEYS:
        if key in resolved:
            continue
        parent = _DERIVED_COLOR_SOURCES.get(key)
        fallback = str(resolved.get(parent, _DEFAULT_SOURCE_COLORS.get(key, '#000000')))
        resolved[key] = _normalize_hex_color(fallback, '#000000')
    return resolved
CLASSIC_DIRECTIONAL_GREEN = '#16C784'
CLASSIC_DIRECTIONAL_RED = '#EA3943'

def _hex_rgb(value: object, fallback: str) -> tuple[float, float, float]:
    color = _normalize_hex_color(value, fallback)
    return tuple((int(color[index:index + 2], 16) / 255.0 for index in (1, 3, 5)))

def _relative_luminance_rgb(rgb: tuple[float, float, float]) -> float:
    linear = tuple((channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in rgb))
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

def _contrast_ratio_hex(foreground: object, background: object) -> float:
    fg = _relative_luminance_rgb(_hex_rgb(foreground, '#000000'))
    bg = _relative_luminance_rgb(_hex_rgb(background, '#000000'))
    return (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)

def _ensure_directional_contrast(color: object, background: object, *, fallback: str, target_ratio: float=2.8) -> str:
    """Keep a theme hue intact and change lightness only when necessary.

    Directional bars/candles need strong separation from their surface, but they
    do not need text-level 4.5:1 contrast. Returning an already-sufficient theme
    color unchanged is the key behavior: contrast correction is a guard rail,
    not a recoloring pass.
    """
    source = _normalize_hex_color(color, fallback)
    bg = _normalize_hex_color(background, '#000000')
    if _contrast_ratio_hex(source, bg) >= target_ratio:
        return source
    red, green, blue = _hex_rgb(source, fallback)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    if saturation < 0.08:
        red, green, blue = _hex_rgb(fallback, fallback)
        hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    background_is_dark = _relative_luminance_rgb(_hex_rgb(bg, '#000000')) < 0.45
    destination = 1.0 if background_is_dark else 0.0
    best = source
    best_ratio = _contrast_ratio_hex(source, bg)
    for step in range(1, 101):
        fraction = step / 100.0
        candidate_lightness = lightness + (destination - lightness) * fraction
        candidate_rgb = colorsys.hls_to_rgb(hue, candidate_lightness, saturation)
        candidate = '#' + ''.join((f'{max(0, min(255, int(round(channel * 255.0)))):02X}' for channel in candidate_rgb))
        ratio = _contrast_ratio_hex(candidate, bg)
        if ratio > best_ratio:
            best, best_ratio = (candidate, ratio)
        if ratio >= target_ratio:
            return candidate
    return best

def candle_directional_palette(chart: dict[str, object], mode: str='theme') -> dict[str, object]:
    """Return a chart palette with candle-only directional presentation."""
    output = dict(chart)
    use_classic = str(mode).casefold() == 'classic'
    up_source = CLASSIC_DIRECTIONAL_GREEN if use_classic else output.get('candle_up', output.get('green'))
    down_source = CLASSIC_DIRECTIONAL_RED if use_classic else output.get('candle_down', output.get('red'))
    background = output.get('bg', output.get('chart_bg', '#000000'))
    output['candle_up'] = (_ensure_directional_contrast(up_source, background, fallback=CLASSIC_DIRECTIONAL_GREEN) if use_classic else _normalize_hex_color(up_source, CLASSIC_DIRECTIONAL_GREEN))
    output['candle_down'] = (_ensure_directional_contrast(down_source, background, fallback=CLASSIC_DIRECTIONAL_RED) if use_classic else _normalize_hex_color(down_source, CLASSIC_DIRECTIONAL_RED))
    return output

def ui_palette(theme: dict[str, object], overrides: dict[str, object] | None=None) -> dict[str, object]:
    """Compatibility wrapper for the canonical Nightwatch color resolver."""
    return resolve_theme(theme, overrides)


# The chart is a working surface, not a themed panel: it deliberately stays
# neutral so that price action and indicators retain the same visual context
# while the surrounding application theme changes.
_GRAY_CHART_SURFACE: Final[dict[str, str]] = {
    'bg': '#131722', 'grid': '#2A2E39', 'border': '#363A45',
    'panel2': '#1E222D', 'control': '#2A2E39', 'control_border': '#434651',
    'text': '#D1D4DC', 'muted': '#9598A1',
}
_BLACK_CHART_SURFACE: Final[dict[str, str]] = {
    'bg': '#000000', 'grid': '#252525', 'border': '#3A3A3A',
    'panel2': '#101010', 'control': '#1A1A1A', 'control_border': '#444444',
    'text': '#D1D4DC', 'muted': '#9598A1',
}
_LIGHT_CHART_SURFACE: Final[dict[str, str]] = {
    'bg': '#FFFFFF', 'grid': '#E0E3EB', 'border': '#D1D4DC',
    'panel2': '#F8F9FD', 'control': '#F0F3FA', 'control_border': '#B2B5BE',
    'text': '#131722', 'muted': '#5D606B',
}

def chart_palette(resolved: dict[str, object], theme_name: str | None=None) -> dict[str, object]:
    """Adapt one resolved palette to chart/research legacy key names.

    Chart surfaces are intentionally independent of the application theme.
    They use only a neutral black, classic charcoal, or white treatment. Price,
    indicator, and directional colors still come from the resolved application
    palette.
    """
    chart = dict(resolved)
    mode = chart_surface_mode(theme_name) if theme_name is not None else None
    if mode is None:
        shell_background = _normalize_hex_color(resolved.get('bg'), '#131722')
        mode = ('white' if _relative_luminance_rgb(
            _hex_rgb(shell_background, '#131722')) >= 0.45 else 'gray')
    chart.update({
        'black': _BLACK_CHART_SURFACE,
        'gray': _GRAY_CHART_SURFACE,
        'white': _LIGHT_CHART_SURFACE,
    }[mode])
    for runtime_key, source_key in {
        'green': 'chart_green', 'red': 'chart_red', 'cyan': 'chart_cyan',
        'amber': 'chart_amber', 'purple': 'chart_purple',
    }.items():
        chart[runtime_key] = str(resolved[source_key])
    return chart
DEFAULT_THEME_NAME = 'Nightwatch'
# Presets are static data; semantic children still follow user overrides in resolve_theme.
# Neutral chart canvases and black dark-theme ladders keep market data in focus.
NIGHTWATCH_COLORS: Final[dict[str, str]] = {
    'bg': '#0B0C0F', 'panel': '#121418', 'panel2': '#191B20', 'grid': '#25282E',
    'border': '#30343C', 'control': '#20242B', 'control_top': '#282D35',
    'control_hover': '#303741', 'control_border': '#414A56', 'text': '#EEE9E1', 'muted': '#B6AEA2',
    'green': '#66D49A', 'red': '#F27D86', 'cyan': '#DCAE77', 'info': '#86B8E8', 'amber': '#E7C574',
    'purple': '#B3A0E4', 'take_profit': '#6DD4C7', 'risk': '#F4A17D', 'active': '#302C29',
    'orderbook_bg': '#000000', 'book_grid': '#22262D', 'orderbook_muted': '#98A3B0',
    'orderbook_value_text': '#DDE3EA', 'orderbook_badge_text': '#EEE9E1',
    'reference_price': '#A8B7C9', 'series_1': '#86B8E8', 'series_2': '#B3A0E4',
    'series_3': '#E7C574', 'series_4': '#6DD4C7', 'chart_bg': '#000000', 'chart_grid': '#252525',
    'chart_border': '#3A3A3A', 'chart_panel2': '#101010', 'chart_control': '#1A1A1A',
    'chart_control_border': '#444444', 'chart_text': '#D1D4DC', 'chart_muted': '#9598A1',
}
THEMES: dict[str, dict[str, object]] = {
    'Nightwatch': dict(NIGHTWATCH_COLORS),
    'Neon Reactor': {
        'bg': '#0B0A12', 'panel': '#13111D', 'panel2': '#1A1727', 'grid': '#2A253A',
        'border': '#383148', 'control': '#242033', 'control_top': '#2C263D',
        'control_hover': '#373046', 'control_border': '#514664', 'text': '#F0EDFA',
        'muted': '#BCB1D0', 'green': '#65E4AA', 'red': '#FF86AB', 'cyan': '#75DDED',
        'info': '#98BFFD', 'amber': '#F2CE79', 'purple': '#B49AFF', 'take_profit': '#6DE0D0',
        'risk': '#FFA780', 'active': '#303049', 'orderbook_bg': '#000000', 'book_grid': '#282336',
        'orderbook_muted': '#AA9CBF', 'orderbook_value_text': '#E3DDF0',
        'orderbook_badge_text': '#F5F1FF', 'reference_price': '#C5B9E2', 'series_1': '#75DDED',
        'series_2': '#B49AFF', 'series_3': '#F2CE79', 'series_4': '#98BFFD', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Arctic Terminal': {
        'bg': '#0A1118', 'panel': '#101B25', 'panel2': '#172632', 'grid': '#213746',
        'border': '#304B5D', 'control': '#1D3040', 'control_top': '#263D4E',
        'control_hover': '#304B5E', 'control_border': '#45677C', 'text': '#E5F0F7',
        'muted': '#B1C4D2', 'green': '#63D6A0', 'red': '#FA8995', 'cyan': '#8AD5EE',
        'info': '#9ABFFF', 'amber': '#E8C87B', 'purple': '#B5ACED', 'take_profit': '#79D5CA',
        'risk': '#F2AC83', 'active': '#244252', 'orderbook_bg': '#000000', 'book_grid': '#233542',
        'orderbook_muted': '#96B1C2', 'orderbook_value_text': '#DAE8F1',
        'orderbook_badge_text': '#EFF8FF', 'reference_price': '#B7CFDE', 'series_1': '#8AD5EE',
        'series_2': '#9ABFFF', 'series_3': '#E8C87B', 'series_4': '#B5ACED', 'chart_bg': '#131722',
        'chart_grid': '#2A2E39', 'chart_border': '#363A45', 'chart_panel2': '#1E222D',
        'chart_control': '#2A2E39', 'chart_control_border': '#434651', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Pulse Light': {
        'bg': '#EDF1F6', 'panel': '#FFFFFF', 'panel2': '#F5F7FA', 'grid': '#DAE1E9',
        'border': '#C4CEDB', 'control': '#E9EEF5', 'control_top': '#F2F5F9',
        'control_hover': '#DCE4EE', 'control_border': '#A7B5C7', 'text': '#202B3A',
        'muted': '#4E6075', 'green': '#087344', 'red': '#B52D45', 'cyan': '#215EA8',
        'info': '#215EA8', 'amber': '#805500', 'purple': '#6843AB', 'take_profit': '#086C78',
        'risk': '#A7471D', 'active': '#D7E4F4', 'orderbook_bg': '#FFFFFF', 'book_grid': '#E0E5EC',
        'orderbook_muted': '#596A7E', 'orderbook_value_text': '#263547',
        'orderbook_badge_text': '#202B3A', 'reference_price': '#5C6C80', 'series_1': '#215EA8',
        'series_2': '#6843AB', 'series_3': '#805500', 'series_4': '#086C78', 'chart_bg': '#FFFFFF',
        'chart_grid': '#E0E3EB', 'chart_border': '#D1D4DC', 'chart_panel2': '#F8F9FD',
        'chart_control': '#F0F3FA', 'chart_control_border': '#B2B5BE', 'chart_text': '#131722',
        'chart_muted': '#5D606B',
    },
    'Graphite Pro': {
        'bg': '#0D0F12', 'panel': '#15181C', 'panel2': '#1C2025', 'grid': '#292E35',
        'border': '#383F48', 'control': '#242A31', 'control_top': '#2C333B',
        'control_hover': '#353E48', 'control_border': '#4B5765', 'text': '#E5E9EE',
        'muted': '#B3BEC9', 'green': '#73D6AC', 'red': '#F38A98', 'cyan': '#B4C6DC',
        'info': '#98BDF2', 'amber': '#EAC784', 'purple': '#BBA8EC', 'take_profit': '#7CCED2',
        'risk': '#F2AD88', 'active': '#303B49', 'orderbook_bg': '#000000', 'book_grid': '#272D34',
        'orderbook_muted': '#A0ADBA', 'orderbook_value_text': '#DCE2EA',
        'orderbook_badge_text': '#ECF0F5', 'reference_price': '#BAC7D7', 'series_1': '#98BDF2',
        'series_2': '#BBA8EC', 'series_3': '#EAC784', 'series_4': '#7CCED2', 'chart_bg': '#131722',
        'chart_grid': '#2A2E39', 'chart_border': '#363A45', 'chart_panel2': '#1E222D',
        'chart_control': '#2A2E39', 'chart_control_border': '#434651', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Deep Ocean': {
        'bg': '#071218', 'panel': '#0C1D26', 'panel2': '#112832', 'grid': '#1C3541',
        'border': '#294855', 'control': '#17333F', 'control_top': '#1D3D4A',
        'control_hover': '#254956', 'control_border': '#3C6472', 'text': '#E1F0F3',
        'muted': '#B1CDD4', 'green': '#56DAB3', 'red': '#FF9B93', 'cyan': '#70D6E2',
        'info': '#8DBDF8', 'amber': '#E9C980', 'purple': '#B9ACEE', 'take_profit': '#81DCBC',
        'risk': '#F5B084', 'active': '#183F4B', 'orderbook_bg': '#000000', 'book_grid': '#203A44',
        'orderbook_muted': '#97BAC5', 'orderbook_value_text': '#D7EAF0',
        'orderbook_badge_text': '#EAF8FB', 'reference_price': '#B2D0D9', 'series_1': '#70D6E2',
        'series_2': '#8DBDF8', 'series_3': '#E9C980', 'series_4': '#B9ACEE', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Copper Dusk': {
        'bg': '#14110F', 'panel': '#1E1916', 'panel2': '#28211C', 'grid': '#362E27',
        'border': '#493B31', 'control': '#322A23', 'control_top': '#3B3128',
        'control_hover': '#473B31', 'control_border': '#675443', 'text': '#F0E7DB',
        'muted': '#CDB9A5', 'green': '#9AD293', 'red': '#F89788', 'cyan': '#E0B07B',
        'info': '#9DBFE1', 'amber': '#E9CC85', 'purple': '#C5AFE0', 'take_profit': '#8AD1BE',
        'risk': '#F4B27E', 'active': '#483426', 'orderbook_bg': '#000000', 'book_grid': '#352C25',
        'orderbook_muted': '#BEAB97', 'orderbook_value_text': '#E9DDCE',
        'orderbook_badge_text': '#FFF1E1', 'reference_price': '#D6C2AA', 'series_1': '#9DBFE1',
        'series_2': '#C5AFE0', 'series_3': '#E9CC85', 'series_4': '#8AD1BE', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Paper Desk': {
        'bg': '#EEECE5', 'panel': '#FCFBF7', 'panel2': '#F4F1E9', 'grid': '#DEDAD0',
        'border': '#C8C1B3', 'control': '#EAE5DA', 'control_top': '#F2EEE5',
        'control_hover': '#DED6C7', 'control_border': '#AEA38F', 'text': '#302D27',
        'muted': '#635B4D', 'green': '#286843', 'red': '#AD3947', 'cyan': '#775326',
        'info': '#355F89', 'amber': '#785600', 'purple': '#6D518E', 'take_profit': '#27696C',
        'risk': '#954620', 'active': '#E2D4BD', 'orderbook_bg': '#FCFBF7', 'book_grid': '#E3DED4',
        'orderbook_muted': '#6A6255', 'orderbook_value_text': '#38332C',
        'orderbook_badge_text': '#302D27', 'reference_price': '#6B6357', 'series_1': '#355F89',
        'series_2': '#6D518E', 'series_3': '#785600', 'series_4': '#27696C', 'chart_bg': '#FFFFFF',
        'chart_grid': '#E0E3EB', 'chart_border': '#D1D4DC', 'chart_panel2': '#F8F9FD',
        'chart_control': '#F0F3FA', 'chart_control_border': '#B2B5BE', 'chart_text': '#131722',
        'chart_muted': '#5D606B',
    },
}
THEME_METADATA: dict[str, dict[str, str]] = {
    'Nightwatch': {'style_family': 'dark_terminal',
        'identity': 'nightwatch', 'chart_surface': 'black'},
    'Neon Reactor': {'style_family': 'dark_terminal',
        'identity': 'neon_reactor', 'chart_surface': 'black'},
    'Arctic Terminal': {'style_family': 'dark_terminal',
        'identity': 'arctic_terminal', 'chart_surface': 'gray'},
    'Pulse Light': {'style_family': 'light_terminal',
        'identity': 'pulse_light', 'chart_surface': 'white'},
    'Graphite Pro': {'style_family': 'dark_terminal',
        'identity': 'graphite_pro', 'chart_surface': 'gray'},
    'Deep Ocean': {'style_family': 'dark_terminal',
        'identity': 'deep_ocean', 'chart_surface': 'black'},
    'Copper Dusk': {'style_family': 'dark_terminal',
        'identity': 'copper_dusk', 'chart_surface': 'black'},
    'Paper Desk': {'style_family': 'light_terminal',
        'identity': 'paper_desk', 'chart_surface': 'white'},
}

def theme_style_family(name: str) -> str:
    return THEME_METADATA.get(str(name), THEME_METADATA[DEFAULT_THEME_NAME]).get('style_family', 'dark_terminal')

def chart_surface_mode(name: str | None) -> str:
    """Return the neutral chart canvas assigned to an application theme."""
    metadata = THEME_METADATA.get(str(name), THEME_METADATA[DEFAULT_THEME_NAME])
    return metadata.get('chart_surface', 'gray')

def is_nightwatch_dark_theme(name: str) -> bool:
    """Compatibility helper for the dense dark-terminal shell family."""
    return theme_style_family(name) == 'dark_terminal'
def _desk_layout(name, columns, row_weights, width):
    """Independent column splits; account activity belongs to Trading."""
    titles = {"depth": "Market depth", "trading": "Trading / positions",
              "trades": "Large trades", "watchlist": "Watchlist"}
    children = []
    for index, (panels, weights) in enumerate(zip(columns, row_weights)):
        leaves = [{"kind": "panel", "id": panel} for panel in panels]
        total = sum(weights)
        children.append(leaves[0] if len(leaves) == 1 else {
            "kind": "split", "id": f"preset-{name}-{index}", "axis": "v",
            "children": leaves, "weights": [weight / total for weight in weights],
        })
    tree = children[0] if len(children) == 1 else {
        "kind": "split", "id": f"preset-{name}", "axis": "h",
        "children": children, "weights": [1 / len(children)] * len(children),
    } if children else None
    visible = tuple(titles[panel] for column in columns for panel in column)
    return {"visible": visible, "sections": (300, 440, 260, 220),
            "watch_tab": 0, "tree": tree,
            "column_mode": 2 if len(columns) > 1 else 1, "rail_width": width}


RIGHT_LAYOUT_PRESETS = {
    "Balanced": _desk_layout("balanced", (("depth", "trades"), ("watchlist", "trading")),
                             ((55, 45), (28, 72)), 660),
    "Book focus": _desk_layout("book", (("depth",), ("trades", "trading")),
                               ((1,), (35, 65)), 660),
    "Tape focus": _desk_layout("tape", (("trades",), ("depth", "trading")),
                               ((1,), (40, 60)), 660),
    "Wide desk": _desk_layout("wide", (("depth",), ("trades",), ("watchlist", "trading")),
                              ((1,), (1,), (28, 72)), 960),
    "Compact": _desk_layout("compact", (("depth", "trading"),), ((40, 60),), 360),
    "Chart only": _desk_layout("chart", (), (), 360),
}

from pathlib import Path
from .constants import DEV_UI_SURFACE_DEFAULTS, DEV_UI_STATUS_GEOMETRY_DEFAULTS
from .utilities import alpha_color, tooltip_stylesheet, typography_state_weight

def build_shell_stylesheet(theme_name: str, theme: dict[str, str], surfaces: dict[str, int] | None=None, status: dict[str, object] | None=None) -> str:
    t = theme
    surface_style = surfaces if surfaces is not None else DEV_UI_SURFACE_DEFAULTS
    market_caption_color = "rgba(%d, %d, %d, %d)" % alpha_color(t['text'], 153).getRgb()
    timeframe_controls = f"""
        QFrame#instrumentBar {{
            background: {t['header']}; border: 1px solid {t['border']};
            border-radius: {int(surface_style['block_radius'])}px;
            margin: 0 6px;
        }}
        QFrame#instrumentContextSlot {{
            background: {t['panel2']}; border: 0;
            border-radius: 4px;
        }}
        QFrame#instrumentMarketGroup {{ background: transparent; border: 0; }}
        QFrame#instrumentMarketGroup QFrame#topMarketIdentity,
        QFrame#instrumentMarketGroup QFrame#topMetricChip {{
            background: transparent; border: 0; padding: 0; margin: 0;
        }}
        QFrame#instrumentMarketGroup QFrame#topMetricChip:hover {{
            background: {t['control_hover']}; border: 0; border-radius: 3px;
        }}
        QFrame#marketBarDivider {{ background: {t['border']}; border: 0; }}
        QFrame#instrumentMarketGroup QLabel {{ background: transparent; padding: 0; border: 0; }}
        QFrame#instrumentMarketGroup QLabel#topMetricTitle {{ color: {t['muted']}; }}
        QFrame#instrumentMarketGroup QLabel#topTickerSymbol {{ color: {t['text']}; }}
        QFrame#instrumentBar QFrame#instrumentContextSlot QWidget#topTimeframeStrip QPushButton#timeframeStripButton {{
            background: transparent; color: {t['muted']}; border: 0;
            border-radius: 3px; padding: 0; margin: 0; min-height: 28px; max-height: 28px;
        }}
        QFrame#instrumentBar QFrame#instrumentContextSlot QWidget#topTimeframeStrip QPushButton#timeframeStripButton:hover {{
            background: {t['control_hover']}; color: {t['text']}; border: 0;
        }}
        QFrame#instrumentBar QFrame#instrumentContextSlot QWidget#topTimeframeStrip QPushButton#timeframeStripButton:checked,
        QFrame#instrumentBar QFrame#instrumentContextSlot QWidget#topTimeframeStrip QPushButton#timeframeStripButton:checked:hover {{
            background: {t['active']}; color: {t['cyan']}; border: 0;
        }}
        QFrame#instrumentBar QPushButton#chartFitButton {{
            background: {t['control']}; color: {t['text']};
            border: 1px solid {t['control_border']}; border-radius: 4px;
            min-height: 26px; padding: 0 10px;
        }}
        QFrame#instrumentBar QPushButton#chartFitButton:hover,
        QFrame#instrumentBar QPushButton#chartFitButton:focus {{
            background: {t['control_hover']}; border-color: {t['active_line']};
        }}

        QPushButton#marketTimeframeChoice:checked,
        QPushButton#marketTimeframePreset:checked {{
            color: {t['cyan']}; background: {t['active']};
            border: 1px solid {t['cyan']};
        }}
        QPushButton#marketTimeframeChoice:disabled:!checked {{
            color: {t['muted']}; border-color: {t['separator']};
        }}
    """
    asset_root = Path(__file__).resolve().parent.parent / 'assets'
    spin_up_arrow = str(asset_root / 'spin-up-arrow.svg').replace('\\', '/')
    spin_down_arrow = str(asset_root / 'spin-down-arrow.svg').replace('\\', '/')
    control_bg = t['control']
    control_top = t.get('control_top', t['control_hover'])
    control_hover = t['control_hover']
    control_border = t['control_border']
    separator = t.get('separator', t['border'])
    depth_green = t.get('depth_green', t['green'])
    depth_red = t.get('depth_red', t['red'])
    block_border_width = max(0, int(surface_style['block_border_width']))
    if is_nightwatch_dark_theme(theme_name):
        radius = f"{int(surface_style['element_radius'])}px"
        section_radius = f"{int(surface_style['block_radius'])}px"
    else:
        radius = '2px'
        section_radius = '1px'
    block_border = '0' if block_border_width <= 0 else f"{block_border_width}px solid {t['border']}"
    block_separator = '0' if block_border_width <= 0 else f'{block_border_width}px solid {separator}'
    menu_bar_bg = t['topbar'] if is_nightwatch_dark_theme(theme_name) else t['bg']
    header_bg = t.get('header', t['panel2'])
    raised_bg = t.get('raised', t['control'])
    active_bg = t.get('active', t['control_hover'])
    active_line = t.get('active_line', t['muted'])
    instrument_group_bg = t['panel']
    instrument_hover_bg = control_hover
    instrument_hover_line = control_border
    status_values = status or {}
    status_background = str(status_values.get('background', t['panel']))
    status_border = str(status_values.get('border', separator))
    status_text = str(status_values.get('text', t['text']))
    status_message = str(status_values.get('message', t['text']))
    status_connection = str(status_values.get('connection', t['muted']))
    status_connection_live = str(status_values.get('connection_live', t['green']))
    status_latency_live = str(status_values.get('latency_live', t['text']))
    status_latency_stale = str(status_values.get('latency_stale', t.get('amber', t['muted'])))
    status_venue_dim = "rgba(%d, %d, %d, %d)" % alpha_color(t['muted'], 140).getRgb()
    status_text_dim = "rgba(%d, %d, %d, %d)" % alpha_color(status_text, 140).getRgb()
    status_message_dim = "rgba(%d, %d, %d, %d)" % alpha_color(status_message, 140).getRgb()
    status_latency_dim = "rgba(%d, %d, %d, %d)" % alpha_color(status_latency_live, 140).getRgb()
    status_stale_dim = "rgba(%d, %d, %d, %d)" % alpha_color(status_latency_stale, 140).getRgb()
    sg = {**DEV_UI_STATUS_GEOMETRY_DEFAULTS, **{key: value for key, value in status_values.items() if key in DEV_UI_STATUS_GEOMETRY_DEFAULTS}}
    status_margin = f"{int(sg['outer_top'])}px {int(sg['outer_right'])}px {int(sg['outer_bottom'])}px {int(sg['outer_left'])}px"
    nightwatch_overrides = ''
    if is_nightwatch_dark_theme(theme_name):
        nightwatch_overrides = f'''
        /* Dark-terminal chrome: compact panels, crisp outlines and small
           radii. Widget placement, spacing and chart rendering stay owned
           by the existing layout and chart code. */
        QWidget#sectionHeader {{
            background: transparent;
            border: 0;
        }}

        QFrame#workspaceNav {{ background: transparent; border: 0; }}
        QFrame#instrumentBar {{
            background: {instrument_group_bg};
            border: 0;
            border-radius: 0;
        }}
        QWidget#instrumentContextSlot {{
            background: transparent;
            border: 0;
        }}
        QFrame#instrumentBar QPushButton#workspaceNavButton,
        QFrame#instrumentBar QPushButton#topUtilityButton {{
            background: transparent;
            color: {t['muted']};
            padding: 3px 6px;
            border: 0;
            border-bottom: 2px solid transparent;
            border-radius: 2px;
        }}
        QFrame#instrumentBar QPushButton#topUtilityButton {{
            padding: 0;
            min-width: 42px;
            max-width: 42px;
            min-height: 42px;
            max-height: 42px;
        }}
        QFrame#instrumentBar QPushButton#workspaceNavButton:hover,
        QFrame#instrumentBar QPushButton#topUtilityButton:hover {{
            background: {instrument_hover_bg};
            color: {t['text']};
            border: 0;
            border-bottom: 2px solid {instrument_hover_line};
            border-radius: 2px;
        }}
        QFrame#instrumentBar QPushButton#workspaceNavButton:checked {{
            background: transparent;
            color: {t['cyan']};
            border: 0;
            border-bottom: 2px solid {t['cyan']};
            border-radius: 2px;
        }}
        QFrame#instrumentBar QPushButton#workspaceNavButton:checked:hover {{
            background: {instrument_hover_bg};
            color: {t['cyan']};
            border: 0;
            border-bottom: 2px solid {t['cyan']};
            border-radius: 2px;
        }}
        QWidget#topTimeframeStrip {{
            background: transparent; border: 0;
        }}
        QPushButton#timeframeStripButton {{
            background: transparent;
            color: {t['muted']};
            border: 0;
            border-bottom: 2px solid transparent;
            border-radius: 2px;
            padding: 2px 3px;
        }}
        QPushButton#timeframeStripButton:hover {{
            background: {instrument_hover_bg};
            color: {t['text']};
            border: 0;
            border-bottom: 2px solid {instrument_hover_line};
            border-radius: 2px;
        }}
        QFrame#instrumentBar QWidget#topTimeframeStrip QPushButton#timeframeStripButton:checked {{
            background: transparent;
            color: {t['cyan']};
            border: 0;
            border-bottom: 2px solid {t['cyan']};
            border-radius: 2px;
        }}
        QFrame#instrumentBar QWidget#topTimeframeStrip QPushButton#timeframeStripButton:checked:hover {{
            background: {instrument_hover_bg};
            color: {t['cyan']};
            border: 0;
            border-bottom: 2px solid {t['cyan']};
            border-radius: 2px;
        }}
        QFrame#chartSurfaceHost {{
            background: {t['panel']};
            border: 0;
            border-radius: 0;
            margin: 0;
            padding: 0;
        }}
        /* right_rail.py owns splitter interaction geometry. The splitter handle
           is a real 10 px inter-window gap: it exists only between adjacent
           panes, so outer application edges remain flush. */
        QSplitter#mainChartSplitter::handle:horizontal,
        QSplitter#rightRailGridRow::handle:horizontal {{
            background: #000000;
            border: 0;
            margin: 0;
        }}
        QSplitter#mainChartSplitter::handle:horizontal:hover {{
            background: #000000;
            border: 0;
            margin: 0;
        }}
        QDialog, QMessageBox {{
            background: {t['panel2']}; border: 2px solid {control_border};
            border-radius: {section_radius};
        }}
        QAbstractButton#framelessCloseButton {{
            background: transparent; color: {t['muted']}; border: 0;
            border-radius: 3px; padding: 0;
        }}
        QAbstractButton#framelessCloseButton:hover {{
            background: {control_hover}; color: {t['text']}; border: 0;
        }}
        QAbstractButton#framelessCloseButton:pressed {{
            background: {active_bg}; color: {t['text']}; border: 0;
        }}
        QStatusBar#terminalStatusBar {{
            background: {status_background}; border: 0;
            border-top: 1px solid {status_border}; border-radius: 0;
            padding: 0; margin: {status_margin};
        }}
        QWidget#statusContent {{ background: transparent; border: 0; }}
        QWidget#statusContent QLabel {{ color: {status_text}; }}
        QLabel#statusVenue, QLabel#statusMarket {{ color: {t['muted']}; }}
        QLabel#statusMessage {{ color: {status_message}; }}
        QLabel#connectionStatus {{ color: {status_connection}; }}
        QLabel#connectionStatus[live="true"] {{ color: {status_connection_live}; }}
        QLabel#terminalLatency {{ color: {status_latency_live}; }}
        QLabel#terminalLatency[stale="true"] {{ color: {status_latency_stale}; }}
        QFrame#statusSeparator {{ background: {status_border}; border: 0; }}

        /* Structural blocks stay borderless by default; atomic UI elements keep outlines. */
        QFrame#section,
        QFrame#section[rightRail="true"],
        QFrame#tradingPanelCard {{
            background: {t['panel']};
            border: {block_border};
            border-radius: {section_radius};
        }}
        QFrame#section[rightRail="true"] QFrame#tradingPanelCard,
        QFrame#tradingControlRow {{
            background: {t['panel']};
            border: 0;
            border-radius: {radius};
        }}
        QLabel#tradeAccountSummary {{
            background: transparent;
            border: 0;
            padding: 0 2px;
        }}

        /* Instrument context is one continuous strip, not a row of boxes. */
        QFrame#metricCard {{
            background: {header_bg};
            border: 1px solid {t['border']};
            border-radius: {radius};
        }}
        QFrame#topMetricChip,
        QFrame#topMarketIdentity {{
            background: {instrument_group_bg};
            border: 0;
            border-right: 1px solid {separator};
            border-bottom: 2px solid transparent;
            border-radius: 0;
        }}
        QFrame#topMetricChip[instrumentClickable="true"]:hover,
        QFrame#topMarketIdentity:hover {{
            background: {instrument_hover_bg};
            border: 0;
            border-right: 1px solid {separator};
            border-bottom: 2px solid {instrument_hover_line};
            border-radius: 0;
        }}
        QFrame#microstructureCard {{
            background: transparent;
            border: 0;
            border-left: 1px solid {separator};
            border-right: 1px solid {separator};
            border-radius: 0;
        }}
        QFrame#microstructureCard:hover {{
            background: {active_bg};
        }}
        QWidget#topMarketStats {{
            background: transparent;
            border: 0;
        }}
        QLabel#topTickerSymbol {{
            /* TypographyController owns family/size/weight for this semantic role. */
            color: {t['cyan']};
        }}
        QLabel#topTickerLast {{
            color: {t['text']};
        }}
        QLabel#topMetricTitle {{
            color: {market_caption_color};
        }}
        QLabel#topMetricValue {{
            color: {t['text']};
        }}
        QDialog#metricDetailDialog {{
            background: {t['panel']};
            color: {t['text']};
            border: 1px solid {t['border']};
            border-radius: {section_radius};
        }}
        QFrame#metricDialogHeader {{
            background: {header_bg};
            border: {block_border};
            border-radius: {section_radius};
        }}
        QLabel#metricDialogHeading {{
            color: {t['text']};
        }}
        QLabel#metricDialogSubheading,
        QLabel#metricControlLabel,
        QLabel#metricHoverName,
        QLabel#metricHoverNote {{
            color: {t['muted']};
        }}
        QFrame#metricDialogTabs,
        QFrame#metricDialogControls {{
            background: transparent;
            border: 0;
        }}
        QPushButton#metricDialogTab,
        QPushButton#metricPeriodButton,
        QPushButton#metricPriceToggle {{
            background: {control_bg};
            color: {t['muted']};
            border: 1px solid {control_border};
            border-radius: 3px;
            min-height: 27px;
            padding: 0 8px;
        }}
        QPushButton#metricDialogTab:hover,
        QPushButton#metricPeriodButton:hover,
        QPushButton#metricPriceToggle:hover {{
            background: {control_hover};
            color: {t['text']};
        }}
        QPushButton#metricDialogTab:checked,
        QPushButton#metricPeriodButton:checked,
        QPushButton#metricPriceToggle:checked {{
            background: {active_bg};
            color: {t['text']};
            border-color: {t['cyan']};
        }}
        QWidget#metricHistoryCanvas {{
            background: {t['bg']};
            border: 1px solid {t['border']};
            border-radius: {radius};
        }}
        QFrame#metricHoverReadout {{
            background: {t['panel2']};
            border: 1px solid {control_border};
            border-radius: 4px;
        }}
        QLabel#metricHoverStamp {{
            color: {t['text']};
        }}
        QLabel#metricHoverValue {{
            color: {t['text']};
        }}

        /* Explicit non-chart backgrounds avoid native light-gray surfaces. */
        QStackedWidget#workspaceStack,
        QFrame#rightRailHost,
        QWidget#responsiveOrderTicket,
        QWidget#tradingInlineField,
        QWidget#qt_scrollarea_viewport {{
            background: {t['panel']};
            border: 0;
        }}
        QAbstractScrollArea {{
            background: {t['panel']};
            border: 0;
        }}

        /* Menus are floating surfaces; they keep one outer edge. */
        QMenuBar {{
            background: {t['topbar']};
            color: {t['muted']};
            border: 0;
            border-bottom: 1px solid {separator};
            padding: 1px 8px;
        }}
        QMenuBar::item {{
            background: transparent;
            padding: 3px 6px;
            border: 0;
        }}
        QMenuBar::item:selected {{
            background: {active_bg};
            color: {t['text']};
        }}
        QMenu {{
            background: {t['panel']};
            color: {t['text']};
            border: 1px solid {t['control_border']};
            padding: 4px;
        }}
        QMenu::item {{
            background: transparent;
            padding: 5px 22px 5px 8px;
            border: 0;
        }}
        QMenu::item:selected {{
            background: {active_bg};
            color: {t['text']};
        }}
        QMenu::separator {{
            height: 1px;
            background: {separator};
            margin: 4px 6px;
        }}

        /* Black controls retain a clear outline in every interaction state. */
        QPushButton, QToolButton {{
            background: {raised_bg};
            color: {t['text']};
            border: 1px solid {t['control_border']};
            border-radius: {radius};
            padding: 4px 7px;
        }}
        QPushButton:hover, QToolButton:hover {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {t['muted']};
        }}
        QPushButton:pressed, QToolButton:pressed {{
            background: {t['header']};
            border: 1px solid {active_line};
        }}
        QPushButton:focus, QToolButton:focus {{
            border: 1px solid {active_line};
        }}
        QPushButton:checked, QToolButton:checked {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {active_line};
        }}
        QPushButton:disabled, QToolButton:disabled {{
            background: {t['panel']};
            color: {t['muted']};
            border-color: {t['border']};
        }}

        /* Permanent top chrome uses state and spacing instead of boxes. */
        QPushButton#topUtilityButton {{
            background: transparent;
            color: {t['muted']};
            border: 0;
            border-radius: 2px;
        }}
        QPushButton#topUtilityButton:hover {{
            background: {active_bg};
            color: {t['text']};
            border: 0;
        }}
        QPushButton#topUtilityButton:checked {{
            background: {active_bg};
            color: {t['text']};
            border: 0;
        }}

        /* Inputs share the same neutral outlines as buttons. */
        QLineEdit, QComboBox, QAbstractSpinBox,
        QKeySequenceEdit, QTextEdit, QPlainTextEdit {{
            background: {t['control']};
            color: {t['text']};
            border: 1px solid {t['control_border']};
            border-radius: {radius};
            padding-top: 4px;
            padding-bottom: 4px;
            selection-background-color: {active_bg};
            selection-color: {t['text']};
        }}
        QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover,
        QKeySequenceEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {{
            border: 1px solid {t['muted']};
        }}
        QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus,
        QKeySequenceEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
            background: {raised_bg};
            border: 1px solid {active_line};
        }}
        QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled,
        QKeySequenceEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
            background: {t['panel']};
            color: {t['muted']};
            border-color: {t['border']};
        }}
        QComboBox::drop-down,
        QAbstractSpinBox::up-button,
        QAbstractSpinBox::down-button {{
            background: {raised_bg};
            border: 0;
            border-left: 1px solid {separator};
        }}
        QAbstractSpinBox::up-arrow {{
            image: url("{spin_up_arrow}");
            width: 9px;
            height: 6px;
        }}
        QAbstractSpinBox::down-arrow {{
            image: url("{spin_down_arrow}");
            width: 9px;
            height: 6px;
        }}
        QComboBox QAbstractItemView {{
            background: {t['panel']};
            color: {t['text']};
            border: 1px solid {t['control_border']};
            outline: 0;
            selection-background-color: {active_bg};
            selection-color: {t['text']};
        }}

        /* Continuous data surfaces. */
        QTableView, QListWidget, QTreeView, QListView {{
            background: {t['panel']};
            alternate-background-color: {t['panel2']};
            color: {t['text']};
            border: 0;
            border-radius: {radius};
            gridline-color: {separator};
            outline: 0;
            selection-background-color: {active_bg};
            selection-color: {t['text']};
        }}
        QTableWidget#tradingDataTable,
        QTableView#symbolSearchResults,
        QTableWidget#sidebarWatchlistTable {{
            background: {t['panel']};
            alternate-background-color: {t['panel2']};
            border: 0;
        }}
        QTableView::item, QListWidget::item,
        QTreeView::item, QListView::item {{
            border: 0;
        }}
        QTableView::item:selected, QListWidget::item:selected,
        QTreeView::item:selected, QListView::item:selected {{
            background: {active_bg};
            color: {t['text']};
        }}
        QTableWidget#tradingDataTable::item {{
            border: 0;
            border-bottom: 1px solid {separator};
        }}
        QHeaderView::section,
        QTableCornerButton::section {{
            background: {raised_bg};
            color: {t['muted']};
            border: 0;
            border-right: 1px solid {separator};
            border-bottom: 1px solid {separator};
            padding: 5px;
        }}
        QHeaderView::section:hover {{
            background: {active_bg};
            color: {t['text']};
        }}

        /* Existing tabs use a restrained filled selection and a clear edge. */
        QTabWidget::pane,
        QTabWidget#tradingAccountTabs::pane {{
            background: {t['panel']};
            border: 0;
            top: 0;
        }}
        QTabBar::base {{
            background: {t['panel']};
            border: 0;
            border-bottom: 1px solid {separator};
        }}
        QTabBar::tab {{
            background: transparent;
            color: {t['muted']};
            border: 1px solid {control_border};
            margin: 0;
            padding: 5px 12px;
        }}
        QTabBar::tab:selected {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {active_line};
        }}
        QTabBar::tab:!selected:hover {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {t['muted']};
        }}
        QTabWidget#tradingAccountTabs QTabBar::tab {{
            background: transparent;
            color: {t['muted']};
            border: 1px solid {control_border};
            margin: 0;
        }}
        QTabWidget#tradingAccountTabs QTabBar::tab:selected {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {active_line};
        }}

        /* Direction stays legible in text/edges; control surfaces remain neutral. */
        QPushButton#buySideButton,
        QPushButton#sellSideButton {{
            background: {raised_bg};
            border: 1px solid {control_border};
            border-radius: {radius};
        }}
        QPushButton#buySideButton,
        QPushButton#sellSideButton {{ color: {t['text']}; }}
        QPushButton#buySideButton:hover {{
            background: {active_bg};
            border-color: {t['green']};
        }}
        QPushButton#sellSideButton:hover {{
            background: {active_bg};
            border-color: {t['red']};
        }}
        QPushButton#buySideButton:checked {{
            background: {active_bg};
            color: {t['green']};
            border-color: {t['green']};
        }}
        QPushButton#sellSideButton:checked {{
            background: {active_bg};
            color: {t['red']};
            border-color: {t['red']};
        }}
        QPushButton#timeInForceCycle,
        QPushButton#protectionButton,
        QPushButton#leveragePresetButton {{
            background: transparent;
            color: {t['muted']};
            border: 1px solid {control_border};
            border-radius: {radius};
        }}
        QPushButton#leveragePresetButton:checked,
        QPushButton#sizePresetButton:checked,
        QPushButton#protectionButton[active="true"] {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {active_line};
        }}
        QFrame#tradingControlRow {{
            background: {t['panel2']};
            border: {block_border};
        }}
        QPushButton#leveragePresetButton {{
            background: {t['control']};
            color: {t['muted']};
            border: 1px solid {t['control_border']};
            border-radius: {radius};
        }}
        QPushButton#leveragePresetButton:hover {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {t['muted']};
        }}
        QPushButton#leveragePresetButton:checked {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {active_line};
        }}
        QPushButton#leveragePresetButton:hover,
        QPushButton#timeInForceCycle:hover,
        QPushButton#protectionButton:hover {{
            background: {active_bg};
            color: {t['text']};
            border: 1px solid {t['muted']};
        }}

        /* Account positions/orders are compact semantic cards instead of
           wide tables. PnL/direction are the visual priority at 400px. */
        QListWidget#tradingCardList {{
            background: transparent;
            border: 0;
            outline: 0;
            padding: 0;
        }}
        QListWidget#tradingCardList::item {{
            background: transparent;
            color: {t['muted']};
            border: 0;
            padding: 0;
            margin: 0;
            min-height: 0;
        }}
        QListWidget#tradingCardList::item:selected {{
            background: {active_bg};
            color: {t['text']};
        }}
        QListWidget#tradingCardList::item:hover {{
            background: {active_bg};
        }}
        QFrame#compactPositionActivityCard,
        QFrame#compactOrderActivityCard {{
            background: {t['panel2']};
            border: {block_border};
            border-radius: {section_radius};
        }}
        QFrame#compactPositionActivityCard[direction="long"],
        QFrame#compactOrderActivityCard[side="buy"] {{
            border-left: 2px solid {t['green']};
        }}
        QFrame#compactPositionActivityCard[direction="short"],
        QFrame#compactOrderActivityCard[side="sell"] {{
            border-left: 2px solid {t['red']};
        }}
        QLabel#accountCardSymbol {{
            /* Instrument symbol typography is owned by TextRole.INSTRUMENT_SYMBOL. */
            color: {t['text']};
        }}
        QLabel#accountCardSide {{ }}
        QLabel#accountCardSide[direction="long"] {{ color: {t['green']}; }}
        QLabel#accountCardSide[direction="short"] {{ color: {t['red']}; }}
        QLabel#accountCardPnl {{ }}
        QLabel#accountCardPnl[pnl="positive"],
        QLabel#accountTotalPnl[pnl="positive"] {{ color: {t['green']}; }}
        QLabel#accountCardPnl[pnl="negative"],
        QLabel#accountTotalPnl[pnl="negative"] {{ color: {t['red']}; }}
        QLabel#accountCardPnl[pnl="flat"],
        QLabel#accountTotalPnl[pnl="flat"] {{ color: {t['muted']}; }}
        QLabel#accountTotalPnl {{
            background: transparent;
            border: 0;
        }}
        QLabel#accountCardDetail {{
            color: {t['muted']};
        }}
        QLabel#accountCardRisk {{
            color: {t['muted']};
        }}
        QLabel#accountCardRisk[risk="warning"] {{ color: {t['amber']}; }}
        QLabel#accountCardRisk[risk="critical"] {{
            color: {t['red']};
        }}
        QLabel#accountOrderStatus {{
            color: {t['muted']};
        }}
        QLabel#accountOrderStatus[state="partial"] {{ color: {t['amber']}; }}

        /* Market toggles use the same compact outlined treatment. */
        QPushButton#filterPreset,
        QToolButton#searchFilterButton {{
            background: transparent;
            border: 1px solid {control_border};
        }}
        QPushButton#filterPreset:hover,
        QToolButton#searchFilterButton:hover {{
            background: {active_bg};
            border: 1px solid {t['muted']};
        }}
        QPushButton#filterPreset:checked,
        QToolButton#searchFilterButton[active="true"] {{
            background: transparent;
            color: {t['text']};
            border: 1px solid {active_line};
        }}

        QCheckBox, QRadioButton {{
            color: {t['text']};
            spacing: 6px;
            padding: 2px 0;
        }}
        QCheckBox::indicator, QRadioButton::indicator {{
            width: 12px;
            height: 12px;
            background: {t['control']};
            border: 1px solid {t['control_border']};
            border-radius: {radius};
        }}
        QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
            border: 1px solid {active_line};
        }}
        QCheckBox::indicator:checked {{
            background: {t['text']};
            border-color: {t['text']};
        }}
        QRadioButton::indicator:checked {{
            background: {t['text']};
            border-color: {t['text']};
        }}

        QGroupBox {{
            background: {t['panel']};
            color: {t['muted']};
            border: {block_separator};
            border-radius: {section_radius};
            margin-top: 8px;
            padding: 8px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 7px;
            padding: 0 4px;
            background: {t['panel']};
            color: {t['muted']};
        }}

        QProgressBar {{
            background: {t['control']};
            color: {t['text']};
            border: 1px solid {separator};
            border-radius: {radius};
            text-align: center;
            min-height: 14px;
        }}
        QProgressBar::chunk {{
            background: {t['green']};
            border: 0;
        }}
        QSlider::groove:horizontal {{
            background: {separator};
            height: 3px;
            border: 0;
        }}
        QSlider::handle:horizontal {{
            background: {t['text']};
            width: 9px;
            margin: -4px 0;
            border: 0;
            border-radius: {radius};
        }}

        QSplitter#mainChartSplitter::handle:horizontal {{
            background: #000000;
            margin: 0;
        }}
        QSplitter#mainChartSplitter::handle:horizontal:hover {{
            background: #000000;
            margin: 0;
        }}
        QSplitter::handle {{
            background: {separator};
            margin: 0;
        }}
        QSplitter::handle:hover {{
            background: {active_line};
        }}
        QScrollBar:vertical {{
            background: {t['panel']};
            width: 6px;
            margin: 0;
            border: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {t['border']};
            min-height: 24px;
            border: 0;
            border-radius: {radius};
        }}
        QScrollBar::handle:vertical:hover {{ background: {active_line}; }}
        QScrollBar:horizontal {{
            background: {t['panel']};
            height: 6px;
            margin: 0;
            border: 0;
        }}
        QScrollBar::handle:horizontal {{
            background: {t['border']};
            min-width: 24px;
            border: 0;
            border-radius: {radius};
        }}
        QScrollBar::handle:horizontal:hover {{ background: {active_line}; }}
        QScrollBar::add-line, QScrollBar::sub-line,
        QScrollBar::add-page, QScrollBar::sub-page {{
            background: transparent;
            border: 0;
        }}

        {tooltip_stylesheet(t)}
        QStatusBar {{
            background: {t['topbar']};
            color: {t['muted']};
            border: 0;
            border-top: {block_separator};
            padding: 0 5px;
        }}
        QStatusBar::item {{ border: 0; }}


                    '''
    attention_font_weight = typography_state_weight("attention")
    # Structural gaps follow the shell; the independent order-book canvas keeps
    # its own palette and raster pipeline.
    structural_overrides = """
        QWidget#centralRoot, QWidget#leftWorkspace, QWidget#workspaceStack,
        QWidget#instrumentBarHost,
        QFrame#rightRailHost, QScrollArea#rightRailScroll,
        QWidget#rightRailViewport, QWidget#rightRailCanvas,
        QSplitter#rightRailSplit {
            background: #000000; border: 0; margin: 0; padding: 0;
        }
        QSplitter#mainChartSplitter::handle,
        QSplitter#mainChartSplitter::handle:hover,
        QSplitter#rightRailSplit::handle,
        QSplitter#rightRailSplit::handle:hover {
            background: #000000; border: 0; margin: 0; padding: 0;
        }
    """
    structural_overrides = structural_overrides.replace('#000000', t['bg'])
    return f'''
            QMainWindow, QWidget {{
                background: {t['bg']};
                color: {t['text']};
            }}
            QDialog, QMessageBox {{
                background: {t['panel']}; color: {t['text']};
                border: 2px solid {control_border};
            }}
            QLabel {{ background: transparent; border: 0; }}
            QMenuBar {{ background: {menu_bar_bg}; color: {t['muted']}; padding: 2px 8px; border-bottom: 1px solid {t['border']}; }}
            QMenuBar::item:selected, QMenu::item:selected {{ background: {control_hover}; color: {t['text']}; }}
            QMenu {{ background: {t['panel']}; border: 1px solid {control_border}; padding: 5px; }}
            QMenu::item {{ padding: 6px 24px 6px 9px; border-radius: {radius}; }}
            QMenu::item:checked {{ color: {t['cyan']}; }}
            QMenu::item:disabled {{ color: {t['muted']}; }}
            QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 7px; }}
            QFrame#auxChartPane {{
                background: {t['bg']}; border: {block_border}; border-radius: 0;
            }}
            QFrame#auxChartHeader {{
                background: {t['header']}; border: 0; border-bottom: {block_separator};
            }}
            QLabel#auxChartStatus {{ color: {t['muted']}; padding: 0 3px; }}
            QComboBox#auxChartSymbol, QComboBox#auxChartInterval {{
                min-height: 22px; padding-top: 0; padding-bottom: 0;
            }}
            QFrame#microstructureCard {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {control_top}, stop:0.22 {control_bg}, stop:1 {t['panel2']});
                border: 1px solid {control_border}; border-bottom: 2px solid {control_border};
                border-radius: {radius};
            }}
            QFrame#microstructureCard:hover {{ border-color: {t['amber']}; }}
            QFrame#section {{
                background: {t['panel']}; border: {block_border}; border-radius: {section_radius};
            }}
            QFrame#workspaceNav {{
                background: transparent; border: 0;
            }}
            QPushButton#workspaceNavButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {control_top}, stop:0.18 {control_bg}, stop:1 {t['panel2']});
                border: 1px solid {control_border}; border-bottom: 2px solid {control_border}; border-radius: {radius};
                color: {t['muted']}; padding: 5px 8px;
            }}
            QPushButton#workspaceNavButton:hover {{
                color: {t['text']}; background: {control_hover}; border-color: {control_border};
                border-bottom-color: {t['cyan']};
            }}
            QPushButton#workspaceNavButton:checked {{
                background: {t['panel2']}; color: {t['cyan']}; border: 1px solid {control_border};
                border-bottom: 2px solid {t['cyan']};
            }}
            QLabel#subtleLabel {{ color: {t['muted']}; }}
            QLabel#tradeValidation {{ color: {t['amber']}; }}
            QLabel#tradeValidation[blocked="true"], QLabel#tradeExecutionState[attention="true"] {{ color: {t['red']}; font-weight: {attention_font_weight}; }}
            QLabel#tradeExecutionState {{ color: {t['text']}; }}
            QLabel#dialogHeading {{ color: {t['text']}; }}
            QLabel#workspaceHeading {{ color: {t['text']}; }}
            QLabel#controlSectionTitle {{ color: {t['cyan']}; }}
            QLabel#lastPrice {{
                color: {t['text']}; min-width: 80px;
            }}
            QFrame#metricCard {{ background: {t['panel2']}; border: 1px solid {t['border']}; border-radius: {radius}; }}
            QFrame#metricCard:hover {{ border-color: {control_border}; }}
            QFrame#topMetricChip,
            QFrame#topMarketIdentity,
            QFrame#microstructureCard {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {control_top}, stop:0.18 {control_bg}, stop:1 {t['panel2']});
                border: 1px solid {control_border};
                border-bottom: 2px solid {control_border}; border-radius: {radius};
            }}
            QFrame#topMetricChip:hover,
            QFrame#topMarketIdentity:hover {{
                background: {control_hover}; border: 1px solid {control_border};
                border-bottom: 2px solid {control_border};
            }}
            QFrame#topMetricChip QLabel,
            QFrame#topMarketIdentity QLabel {{ background: transparent; border: 0; }}
            QLabel#topMetricTitle {{ color: {market_caption_color}; }}
            QLabel#topMetricValue {{
                color: {t['text']};
            }}
            QLabel#topTickerSymbol {{
                /* Do not bypass the semantic typography role with QSS font overrides. */
                color: {t['text']};
            }}
            QLabel#topTickerLast {{
                color: {t['text']};
            }}
            QLabel#metricTitle {{ color: {t['muted']}; }}
            QLabel#metricValue {{
                color: {t['text']};
            }}
            QLabel#metricValue[direction="positive"] {{ color: {depth_green}; }}
            QLabel#metricValue[direction="negative"] {{ color: {depth_red}; }}
            QLabel#tradeAccountSummary {{
                background: {t['panel2']}; color: {t['muted']};
                border: 1px solid {t['border']}; border-left: 2px solid {t['cyan']};
                padding: 5px 7px;
            }}
            QFrame#tradingOrdersDrawer {{
                background: {t['panel']}; border: 2px solid {control_border};
                border-radius: {section_radius};
            }}
            QFrame#tradingControlRow, QFrame#tradingPanelCard {{
                background: {t['panel']}; border: 0;
                border-radius: {section_radius};
            }}
            QFrame#tradingAdaptiveActivity {{
                background: transparent; border: 0;
            }}
            QFrame#tradingControlRow QLabel,
            QFrame#tradingPanelCard QLabel {{ background: transparent; border: 0; }}
            QWidget#tradingInlineField {{ background: transparent; border: 0; }}
            QLabel#tradingSectionLabel {{
                color: {t['amber']};
            }}
            QLabel#tradeFieldLabel {{
                color: {t['muted']};
            }}
            QLabel#tradingDeskStatus {{
                color: {t['text']};
            }}
            QWidget#responsiveOrderTicket QLineEdit,
            QWidget#responsiveOrderTicket QComboBox,
            QWidget#responsiveOrderTicket QSpinBox,
            QWidget#responsiveOrderTicket QDoubleSpinBox {{
                min-height: 22px; padding-top: 4px; padding-bottom: 4px;
            }}
            QWidget#responsiveOrderTicket QComboBox#tradeTicketPrimaryCombo,
            QWidget#responsiveOrderTicket QComboBox#timeInForceCycle,
            QWidget#responsiveOrderTicket QComboBox#leverageDropdown {{
                min-height: 20px; padding: 3px 22px 3px 5px;
            }}
            QWidget#responsiveOrderTicket QComboBox#timeInForceCycle {{
                color: {t['muted']};
            }}
            QWidget#responsiveOrderTicket QStackedWidget {{
                background: transparent; border: 0;
            }}
            QLineEdit, QComboBox, QAbstractSpinBox {{
                background: {control_bg}; border: 1px solid {control_border};
                border-bottom: 2px solid {control_border}; border-radius: {radius};
                color: {t['text']}; padding: 4px 8px; selection-background-color: {t['cyan']};
                selection-color: {t['bg']}; min-height: 18px;
            }}
            QComboBox#watchlistGroupSelector {{
                min-height: 18px; padding: 2px 22px 2px 7px;
            }}
            QToolButton#watchlistGroupMenu {{
                min-width: 28px; max-width: 28px; min-height: 22px;
                padding: 0;
            }}
            QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{
                border: 1px solid {control_border}; border-bottom: 2px solid {t['cyan']};
            }}
            QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {{
                background: {t['panel2']}; border-color: {t['border']}; color: {t['muted']};
            }}
            QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
                width: 18px; background: {control_hover}; border: 0; border-left: 1px solid {control_border};
            }}
            QAbstractSpinBox::up-button {{
                subcontrol-origin: border; subcontrol-position: top right;
                border-top-right-radius: {radius};
            }}
            QAbstractSpinBox::down-button {{
                subcontrol-origin: border; subcontrol-position: bottom right;
                border-bottom-right-radius: {radius};
            }}
            QComboBox {{ padding-left: 8px; padding-right: 8px; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding; subcontrol-position: top right; width: 0;
                background: {control_top}; border: 0; border-left: 1px solid {t['border']};
                border-top-right-radius: {radius}; border-bottom-right-radius: {radius};
            }}
            QWidget#responsiveOrderTicket QComboBox#tradeTicketPrimaryCombo::drop-down,
            QWidget#responsiveOrderTicket QComboBox#timeInForceCycle::drop-down,
            QWidget#responsiveOrderTicket QComboBox#leverageDropdown::drop-down {{
                subcontrol-origin: padding; subcontrol-position: top right;
                width: 0; background: transparent; border: 0;
            }}
            QComboBox QAbstractItemView {{
                background: {t['panel']}; color: {t['text']}; border: 1px solid {control_border};
                selection-background-color: {control_hover}; selection-color: {t['cyan']};
                outline: 0; padding: 3px;
            }}
            QDialog#symbolSearchDialog {{ background: {t['panel']}; }}
            QLineEdit#globalSymbolSearch {{
                padding: 5px 11px;
            }}
            QLabel#searchResultCount {{
                color: {t['muted']}; padding: 1px 2px 0 2px;
            }}
            QTableView#symbolSearchResults {{
                background: {t['bg']}; alternate-background-color: {t['panel2']};
                border: 1px solid {control_border}; border-radius: {radius}; outline: 0;
            }}
            QTableView#symbolSearchResults::item {{ padding: 3px 7px; }}
            QTableView#symbolSearchResults::item:selected {{
                background: {control_hover}; color: {t['text']};
            }}
            QPushButton, QToolButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {control_top}, stop:0.18 {control_bg}, stop:1 {t['panel2']});
                border: 1px solid {control_border}; border-bottom: 2px solid {control_border};
                border-radius: {radius};
                color: {t['text']}; padding: 5px 8px;
            }}
            QPushButton:hover, QToolButton:hover {{
                background: {control_hover}; border: 1px solid {control_border};
                border-bottom: 2px solid {t['cyan']}; color: {t['text']};
            }}
            QPushButton:pressed, QToolButton:pressed {{
                background: {t['panel2']}; border: 1px solid {control_border};
                border-bottom: 2px solid {t['cyan']};
            }}
            QPushButton:focus, QToolButton:focus {{ border-bottom: 2px solid {t['cyan']}; }}
            QPushButton:checked, QToolButton:checked {{
                background: {t['panel2']}; border: 1px solid {control_border};
                border-bottom: 2px solid {t['cyan']}; color: {t['cyan']};
            }}
            QPushButton:disabled, QToolButton:disabled {{
                background: {t['panel2']}; border-color: {t['border']}; color: {t['muted']};
            }}
            QPushButton#topUtilityButton {{ padding: 5px 6px; }}
            QToolButton#searchFilterButton {{
                padding: 5px 8px;
            }}
            QToolButton#searchFilterButton[active="true"] {{
                border-bottom: 2px solid {t['cyan']}; color: {t['cyan']};
            }}
            QToolButton#tradeSettingsMini {{
                background: transparent; border: 0; padding: 0;
                color: {t['muted']};
            }}
            QToolButton#tradeSettingsMini:hover {{
                background: {control_hover}; border: 0; color: {t['text']};
            }}
            QToolButton#tradeMarkButton {{
                background: {control_bg}; color: {t['text']};
                border: 1px solid {control_border}; border-bottom: 2px solid {control_border};
                border-radius: {radius}; padding: 0 3px;
            }}
            QToolButton#tradeMarkButton:hover {{
                background: {control_hover}; border-bottom-color: {t['cyan']};
            }}
            QLineEdit#leverageCustomPopup {{
                background: {t['panel']}; color: {t['text']};
                border: 1px solid {t['cyan']}; border-radius: {radius};
                padding: 3px 6px;
            }}
            QPushButton#timeInForceCycle {{
                background: {control_bg}; color: {t['cyan']};
                border: 1px solid {control_border}; border-bottom: 2px solid {t['cyan']};
                padding: 0;
            }}
            QPushButton#leveragePresetButton {{
                padding-left: 4px; padding-right: 4px; color: {t['muted']};
            }}
            QPushButton#leveragePresetButton:hover {{ color: {t['cyan']}; }}
            QPushButton#leveragePresetButton:checked,
            QPushButton#sizePresetButton:checked {{
                background: {t['panel2']}; color: {t['cyan']};
                border-color: {t['cyan']}; border-bottom-color: {t['cyan']};
            }}
            QPushButton#buySideButton {{
                background: {t['panel2']}; color: {t['text']};
                border-color: {t['border']}; border-bottom-color: {t['green']};
                min-height: 26px;
            }}
            QPushButton#sellSideButton {{
                background: {t['panel2']}; color: {t['text']};
                border-color: {t['border']}; border-bottom-color: {t['red']};
                min-height: 26px;
            }}
            QPushButton#buySideButton:checked {{
                background: {t['green']}; border-color: {t['green']}; color: {t['bg']};
            }}
            QPushButton#sellSideButton:checked {{
                background: {t['red']}; border-color: {t['red']}; color: {t['bg']};
            }}
            QPushButton#buySideButton:disabled,
            QPushButton#sellSideButton:disabled {{
                background: {t['panel2']}; color: {t['muted']}; border-color: {t['border']};
            }}
            QWidget#responsiveOrderTicket[dense="true"] {{ }}
            QWidget#responsiveOrderTicket[dense="true"] QLineEdit,
            QWidget#responsiveOrderTicket[dense="true"] QComboBox,
            QWidget#responsiveOrderTicket[dense="true"] QSpinBox,
            QWidget#responsiveOrderTicket[dense="true"] QDoubleSpinBox,
            QWidget#responsiveOrderTicket[dense="true"] QPushButton,
            QWidget#responsiveOrderTicket[dense="true"] QToolButton {{
                padding: 3px 5px; min-height: 20px;
            }}
            QPushButton#dangerButton {{ color: {t['red']}; border-color: {t['red']}; }}
            QPushButton#filterPreset {{ padding: 3px 5px; }}
            QTableView, QListWidget {{
                background: {t['bg']}; alternate-background-color: {t['panel2']};
                border: 1px solid {control_border}; border-radius: {radius}; gridline-color: {t['border']};
                selection-background-color: {control_hover}; selection-color: {t['text']};
                outline: 0;
            }}
            QTableView {{
            }}
            QTableView::item, QListWidget::item {{ padding: 3px 6px; }}
            QTableView::item:selected, QListWidget::item:selected {{
                background: {control_hover}; color: {t['text']};
            }}
            QHeaderView::section {{
                background: {t['panel2']}; color: {t['muted']}; border: 0;
                border-right: 1px solid {t['border']}; border-bottom: 1px solid {control_border};
                padding: 5px;
            }}
            QHeaderView::section:hover {{ background: {control_hover}; color: {t['text']}; }}
            QTableCornerButton::section {{
                background: {t['panel2']}; border: 0; border-right: 1px solid {t['border']};
                border-bottom: 1px solid {control_border};
            }}
            QTabWidget::pane {{ background: {t['panel']}; border: 1px solid {control_border}; top: -1px; }}
            QTabWidget#tradingAccountTabs::pane {{
                background: {t['bg']}; border: 1px solid {control_border};
                border-radius: 2px; top: -1px;
            }}
            QTabWidget#tradingAccountTabs QTabBar::tab {{
                min-height: 22px; padding: 6px 8px; margin-right: 2px;
            }}
            QTableWidget#tradingDataTable {{
                background: {t['bg']}; alternate-background-color: {t['panel2']};
                border: 0; border-radius: 0;
            }}
            QTableWidget#tradingDataTable::item {{
                padding: 4px 5px; border-bottom: 1px solid {t['border']};
            }}
            QTabBar::tab {{
                background: {control_bg}; color: {t['muted']}; border: 1px solid {control_border};
                padding: 5px 18px; margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {control_hover}; color: {t['cyan']}; border-color: {t['cyan']};
            }}
            QTabBar::tab:!selected:hover {{ background: {control_hover}; color: {t['text']}; }}
            QSplitter::handle {{ background: {control_border}; margin: 0; }}
            QSplitter::handle:hover {{ background: {t['cyan']}; }}
            QScrollBar:vertical {{ background: {t['bg']}; width: 8px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {t['border']}; min-height: 24px; border-radius: {radius}; }}
            QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{ background: {t['bg']}; height: 8px; margin: 0; }}
            QScrollBar::handle:horizontal {{ background: {t['border']}; min-width: 24px; border-radius: {radius}; }}
            QScrollBar::handle:horizontal:hover {{ background: {t['muted']}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
            {tooltip_stylesheet(t)}
            QStatusBar {{ background: {t['panel']}; color: {t['muted']}; border-top: 1px solid {t['border']}; padding: 0 5px; }}
            QStatusBar::item {{ border: 0; }}
            QStatusBar#terminalStatusBar {{
                background: {status_background}; color: {status_text};
                border: 0; border-top: 1px solid {status_border};
                margin: {status_margin}; padding: 0;
            }}
            QWidget#statusContent {{ background: transparent; border: 0; }}
            QWidget#statusContent QLabel {{ color: {status_text}; }}
            QLabel#statusVenue, QLabel#statusMarket {{ color: {t['muted']}; }}
        QLabel#statusMessage {{ color: {status_message}; }}
            QLabel#connectionStatus {{ color: {status_connection}; }}
            QLabel#connectionStatus[live="true"] {{ color: {status_connection_live}; }}
            QLabel#terminalLatency {{ color: {status_latency_live}; }}
            QLabel#terminalLatency[stale="true"] {{ color: {status_latency_stale}; }}
            QFrame#statusSeparator {{ background: {status_border}; border: 0; }}
            QScrollArea {{ background: {t['bg']}; border: 0; }}
            QCheckBox {{ color: {t['text']}; spacing: 7px; padding: 2px; }}
            QCheckBox::indicator {{
                width: 13px; height: 13px; background: {control_bg};
                border: 1px solid {control_border}; border-radius: 1px;
            }}
            QCheckBox::indicator:hover {{ border-color: {t['cyan']}; }}
            QCheckBox::indicator:checked {{
                background: {t['cyan']}; border-color: {t['cyan']};
            }}
            QCheckBox::indicator:disabled {{
                background: {t['panel2']}; border-color: {t['border']};
            }}
            QGroupBox {{
                background: {t['panel']}; border: 1px solid {control_border}; border-radius: {radius};
                margin-top: 9px; padding: 10px 7px 6px 7px; color: {t['muted']};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 7px; padding: 0 5px; background: {t['panel']};
            }}
            QDialogButtonBox QPushButton {{ min-width: 82px; }}
            QMessageBox QPushButton {{ min-width: 86px; }}
            QListWidget::item {{ min-height: 23px; padding: 2px 5px; }}
            QListWidget::item:hover {{ background: {control_hover}; }}

            /* Settings: compact task navigation, searchable content, and aligned cards. */
            QDialog#nightwatchSettingsDialog {{
                background: {t['panel']}; color: {t['text']};
            }}
            QFrame#settingsHeader, QFrame#settingsFooter {{
                background: {t['header']}; border: 0;
            }}
            QFrame#settingsHeader {{ border-bottom: 1px solid {separator}; }}
            QFrame#settingsFooter {{ border-top: 1px solid {separator}; }}
            QWidget#settingsHeaderText {{ background: transparent; border: 0; }}
            QLabel#settingsHeading {{ color: {t['text']}; }}
            QLineEdit#settingsSearch {{
                min-width: 210px; max-width: 320px; min-height: 28px;
                background: {t['control']}; color: {t['text']};
                border: 1px solid {t['control_border']}; border-radius: 5px;
                padding: 3px 10px;
            }}
            QLineEdit#settingsSearch:hover {{ border-color: {t['muted']}; }}
            QLineEdit#settingsSearch:focus {{
                background: {raised_bg}; border: 1px solid {active_line};
            }}
            QListWidget#settingsCategories {{
                background: {t['panel2']}; border: 0; border-right: 1px solid {separator};
                padding: 8px 0; outline: 0;
            }}
            QListWidget#settingsCategories::item {{
                min-height: 30px; padding: 4px 12px; color: {t['muted']}; border: 0;
            }}
            QListWidget#settingsCategories::item:hover {{
                background: {control_hover}; color: {t['text']};
            }}
            QListWidget#settingsCategories::item:selected {{
                background: {active_bg}; color: {t['text']};
                border-left: 3px solid {active_line};
            }}
            QStackedWidget#settingsPages, QWidget#settingsPage, QScrollArea#settingsScroll {{
                background: {t['panel']}; border: 0;
            }}
            QScrollArea#settingsScroll > QWidget > QWidget {{
                background: {t['panel']}; border: 0;
            }}
            QLabel#settingsPageHeading {{ color: {t['text']}; }}
            QDialog#nightwatchSettingsDialog QLabel {{ background: transparent; }}
            QLabel#settingsSubheading {{
                color: {t['muted']}; padding-top: 5px;
            }}
            QGroupBox#settingsGroup {{
                background: {t['panel2']}; border: 1px solid {separator};
                border-radius: 7px; margin-top: 9px; padding: 7px 0 0 0;
            }}
            QGroupBox#settingsGroup::title {{
                subcontrol-origin: margin; left: 9px; padding: 0 6px;
                background: {t['panel2']}; color: {t['text']};
            }}
            QWidget#settingsGridHost {{ background: transparent; border: 0; }}
            QFrame#settingsSeparator {{
                background: {separator}; border: 0; min-height: 1px; max-height: 1px;
            }}
            QDialog#nightwatchSettingsDialog QPushButton {{
                min-height: 25px; padding: 3px 10px;
            }}
            QDialog#nightwatchSettingsDialog QToolButton {{
                min-height: 28px; min-width: 28px; padding: 3px 7px;
            }}
            QDialog#nightwatchSettingsDialog QComboBox,
            QDialog#nightwatchSettingsDialog QLineEdit,
            QDialog#nightwatchSettingsDialog QSpinBox,
            QDialog#nightwatchSettingsDialog QDoubleSpinBox {{
                min-height: 25px; padding: 2px 8px;
            }}
            QDialog#nightwatchSettingsDialog QCheckBox,
            QDialog#nightwatchSettingsDialog QRadioButton {{
                min-height: 23px; spacing: 7px; background: transparent;
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsDeveloperTabs::pane {{
                border: 1px solid {separator}; background: {t['panel']};
                top: -1px;
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsDeveloperTabs QTabBar::tab {{
                min-height: 34px; min-width: 150px; padding: 7px 14px;
                background: {t['panel2']}; color: {t['muted']};
                border: 0; border-bottom: 2px solid transparent;
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsDeveloperTabs QTabBar::tab:hover {{
                background: {control_hover}; color: {t['text']};
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsDeveloperTabs QTabBar::tab:selected {{
                background: {t['panel']}; color: {t['text']};
                border-bottom: 2px solid {active_line};
            }}
            QWidget#settingsEmbeddedTool {{
                background: {t['panel']}; border: 0;
            }}
            QFrame#leftWorkspace {{ background: {t['bg']}; border: 0; }}
            {nightwatch_overrides}

            /* Shared account surfaces, independent of native desktop colors. */
            QWidget#accountActivityPanel {{
                background: {t['panel']}; border: 0;
            }}
            QTabWidget#tradingAccountTabs::pane {{
                background: {t['panel']}; border: 1px solid {t['border']};
                border-radius: 3px; top: -1px;
            }}
            QTabWidget#tradingAccountTabs QTabBar::tab {{
                background: {t['panel']}; color: {t['muted']};
                border: 0; border-bottom: 2px solid transparent;
                padding: 7px 6px; margin: 0; min-width: 0;
            }}
            QTabWidget#tradingAccountTabs QTabBar::tab:selected {{
                background: {t['header']}; color: {t['text']};
                border: 0; border-bottom: 2px solid {t['active_line']};
            }}
            QListWidget#tradingCardList {{
                background: {t['panel']}; border: 0; padding: 4px;
            }}
            QFrame#accountEmptyState, QLabel#accountEmptyState {{
                background: {t['panel2']}; color: {t['muted']};
                border: 1px solid {t['border']}; border-radius: 4px;
                padding: 12px;
            }}
            QFrame#accountEmptyState QLabel {{ background: transparent; border: 0; padding: 0; }}
            QFrame#compactPositionActivityCard,
            QFrame#compactOrderActivityCard {{
                background: {t['panel2']}; border: 1px solid {t['border']}; border-radius: 4px;
            }}
            QFrame#compactPositionActivityCard:hover,
            QFrame#compactOrderActivityCard:hover {{
                border-color: {t['control_border']};
            }}
            QFrame#tradingPanelCard, QFrame#tradingControlRow {{
                background: {t['panel']}; border: 0; border-radius: {section_radius};
            }}
            QFrame#tradingMarginRow {{
                background: transparent; border: 0; border-radius: 0;
            }}
            QFrame#tradingMarginRow QLabel {{
                background: transparent; border: 0; padding: 0;
            }}
            QFrame#tradingMarginRow QPushButton#sizePresetButton {{
                min-height: 22px; padding: 2px 5px;
            }}
            QCheckBox#reduceOnlyCheck {{
                background: transparent; border: 0; padding: 0;
            }}
            QLabel#tradingDeskStatus, QLabel#tradeAccountSummary, QLabel#accountTotalPnl {{
                background: {t['panel2']}; border: 1px solid {t['border']};
                border-radius: 3px; padding: 6px;
            }}
            QLabel#tradeAvailableSummary {{
                background: transparent; border: 0; padding: 1px 2px;
                color: {t['muted']};
            }}
            QTableWidget#tradingDataTable {{ background: {t['panel']}; border: 0; }}
            QTableWidget#tradingDataTable::item {{ border-bottom: 1px solid {t['separator']}; padding: 5px 7px; }}
            QStatusBar#terminalStatusBar {{ margin: {status_margin}; padding: 0; }}
            ''' + structural_overrides + tooltip_stylesheet(t) + timeframe_controls + f"""
        QComboBox::down-arrow, QToolButton::menu-indicator, QPushButton::menu-indicator {{
            image: none; width: 0; height: 0;
        }}
        QComboBox::drop-down {{ width: 0; border: 0; background: transparent; }}
        QComboBox {{ padding-left: 8px; padding-right: 8px; }}
        QWidget#statusContent QLabel#statusVenue {{ color: {status_venue_dim}; }}
        QWidget#statusContent QLabel#statusFrameRate {{ color: {status_text_dim}; }}
        QWidget#statusContent QLabel#statusMessage {{ color: {status_message_dim}; }}
        QWidget#statusContent QLabel#terminalLatency,
        QWidget#statusContent QLabel#statusOrderbookLatency {{ color: {status_latency_dim}; }}
        QWidget#statusContent QLabel#terminalLatency[stale="true"],
        QWidget#statusContent QLabel#statusOrderbookLatency[stale="true"] {{ color: {status_stale_dim}; }}
    """


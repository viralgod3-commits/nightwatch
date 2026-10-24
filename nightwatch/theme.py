"""Semantic theme tokens, presets, layout presets and stylesheet generation."""
from __future__ import annotations

from typing import Final
import re
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
NIGHTWATCH_COLORS: Final[dict[str, str]] = {
    'bg': '#0B0C0D', 'panel': '#121316', 'panel2': '#181A1E', 'grid': '#272A30',
    'border': '#30343C', 'control': '#1D2024', 'control_top': '#22252A',
    'control_hover': '#26292F', 'control_border': '#434853', 'text': '#ECEBE7', 'muted': '#A5AEBC',
    'green': '#56CF98', 'red': '#FF5563', 'cyan': '#DEB980', 'info': '#80B1EC', 'amber': '#E5C071',
    'purple': '#B4A1E8', 'take_profit': '#69CABB', 'risk': '#F29D76', 'active': '#22252B',
    'orderbook_bg': '#000000', 'book_grid': '#24272D', 'orderbook_muted': '#A5AEBC',
    'orderbook_value_text': '#ECEBE7', 'orderbook_badge_text': '#ECEBE7',
    'reference_price': '#A5AEBC', 'series_1': '#80B1EC', 'series_2': '#B4A1E8',
    'series_3': '#E5C071', 'series_4': '#69CABB', 'chart_bg': '#000000', 'chart_grid': '#252525',
    'chart_border': '#3A3A3A', 'chart_panel2': '#101010', 'chart_control': '#1A1A1A',
    'chart_control_border': '#444444', 'chart_text': '#D1D4DC', 'chart_muted': '#9598A1',
}
_DEFAULT_SOURCE_COLORS: dict[str, str] = dict(NIGHTWATCH_COLORS)
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
    base = {key: value for key, value in dict(theme or NIGHTWATCH_COLORS).items() if key in allowed}
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
    # Theme mode must follow the active directional tokens exactly. Do not
    # prefer a stale candle-specific value left in a persisted palette.
    up_source = CLASSIC_DIRECTIONAL_GREEN if use_classic else output.get('green')
    down_source = CLASSIC_DIRECTIONAL_RED if use_classic else output.get('red')
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
THEMES: dict[str, dict[str, object]] = {
    'Nightwatch': dict(NIGHTWATCH_COLORS),
    'Neon Reactor': {
        'bg': '#0C0B10', 'panel': '#14121A', 'panel2': '#1C1825', 'grid': '#2D273A',
        'border': '#373048', 'control': '#221D2C', 'control_top': '#272233',
        'control_hover': '#2C2639', 'control_border': '#4D4364', 'text': '#EEEAF7',
        'muted': '#B2A9C6', 'green': '#5DDBAA', 'red': '#FF5275', 'cyan': '#72D4E7',
        'info': '#98B9F5', 'amber': '#E9C47A', 'purple': '#BAA0FA', 'take_profit': '#6CD6C8',
        'risk': '#F3A17D', 'active': '#282234', 'orderbook_bg': '#000000', 'book_grid': '#292436',
        'orderbook_muted': '#B2A9C6', 'orderbook_value_text': '#EEEAF7',
        'orderbook_badge_text': '#EEEAF7', 'reference_price': '#B2A9C6', 'series_1': '#98B9F5',
        'series_2': '#BAA0FA', 'series_3': '#E9C47A', 'series_4': '#6CD6C8', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Arctic Terminal': {
        'bg': '#080C0F', 'panel': '#0E1419', 'panel2': '#131B23', 'grid': '#1E2B38',
        'border': '#253545', 'control': '#17202A', 'control_top': '#1A2631',
        'control_hover': '#1D2A37', 'control_border': '#344A60', 'text': '#E6EFF6',
        'muted': '#A5BDCE', 'green': '#56D0A0', 'red': '#FF5868', 'cyan': '#87D0E9',
        'info': '#90B7F1', 'amber': '#E2C179', 'purple': '#B0A7E5', 'take_profit': '#74CEC6',
        'risk': '#EFA47D', 'active': '#1B2632', 'orderbook_bg': '#000000', 'book_grid': '#1C2834',
        'orderbook_muted': '#A5BDCE', 'orderbook_value_text': '#E6EFF6',
        'orderbook_badge_text': '#E6EFF6', 'reference_price': '#A5BDCE', 'series_1': '#90B7F1',
        'series_2': '#B0A7E5', 'series_3': '#E2C179', 'series_4': '#74CEC6', 'chart_bg': '#131722',
        'chart_grid': '#2A2E39', 'chart_border': '#363A45', 'chart_panel2': '#1E222D',
        'chart_control': '#2A2E39', 'chart_control_border': '#434651', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Pulse Light': {
        'bg': '#EFF2F6', 'panel': '#FFFFFF', 'panel2': '#F6F8FB', 'grid': '#E1E6ED',
        'border': '#CBD4E0', 'control': '#F0F3F8', 'control_top': '#F6F8FB',
        'control_hover': '#E5EBF3', 'control_border': '#9DAEC3', 'text': '#202B3B',
        'muted': '#4C6078', 'green': '#0B7348', 'red': '#B22D43', 'cyan': '#245EA8',
        'info': '#245EA8', 'amber': '#805600', 'purple': '#6946AD', 'take_profit': '#0C6B77',
        'risk': '#A34623', 'active': '#DDE7F5', 'orderbook_bg': '#FFFFFF', 'book_grid': '#E2E7EF',
        'orderbook_muted': '#536780', 'orderbook_value_text': '#243347',
        'orderbook_badge_text': '#202B3B', 'reference_price': '#596D83', 'series_1': '#245EA8',
        'series_2': '#6946AD', 'series_3': '#805600', 'series_4': '#0C6B77', 'chart_bg': '#FFFFFF',
        'chart_grid': '#E0E3EB', 'chart_border': '#D1D4DC', 'chart_panel2': '#F8F9FD',
        'chart_control': '#F0F3FA', 'chart_control_border': '#B2B5BE', 'chart_text': '#131722',
        'chart_muted': '#5D606B',
    },
    'Graphite Pro': {
        'bg': '#0B0C0D', 'panel': '#111316', 'panel2': '#181A1F', 'grid': '#262A31',
        'border': '#2F343C', 'control': '#1D2025', 'control_top': '#21252A',
        'control_hover': '#252930', 'control_border': '#424954', 'text': '#E7ECF3',
        'muted': '#A6B3C3', 'green': '#58D1A0', 'red': '#FF5865', 'cyan': '#A9BED8',
        'info': '#88B4EE', 'amber': '#E2C180', 'purple': '#B7A5E5', 'take_profit': '#74CCD0',
        'risk': '#F1A57D', 'active': '#22252B', 'orderbook_bg': '#000000', 'book_grid': '#23272D',
        'orderbook_muted': '#A6B3C3', 'orderbook_value_text': '#E7ECF3',
        'orderbook_badge_text': '#E7ECF3', 'reference_price': '#A6B3C3', 'series_1': '#88B4EE',
        'series_2': '#B7A5E5', 'series_3': '#E2C180', 'series_4': '#74CCD0', 'chart_bg': '#131722',
        'chart_grid': '#2A2E39', 'chart_border': '#363A45', 'chart_panel2': '#1E222D',
        'chart_control': '#2A2E39', 'chart_control_border': '#434651', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Deep Ocean': {
        'bg': '#080C0D', 'panel': '#0D1416', 'panel2': '#121C1E', 'grid': '#1D2C30',
        'border': '#23373B', 'control': '#152124', 'control_top': '#192729',
        'control_hover': '#1C2B2E', 'control_border': '#314C52', 'text': '#E2EFF1',
        'muted': '#A3BFC8', 'green': '#52D3AE', 'red': '#FF5861', 'cyan': '#77D0DD',
        'info': '#90B7EC', 'amber': '#E2C17C', 'purple': '#B5A8E5', 'take_profit': '#78CDB8',
        'risk': '#EFA37B', 'active': '#19272A', 'orderbook_bg': '#000000', 'book_grid': '#1A292C',
        'orderbook_muted': '#A3BFC8', 'orderbook_value_text': '#E2EFF1',
        'orderbook_badge_text': '#E2EFF1', 'reference_price': '#A3BFC8', 'series_1': '#90B7EC',
        'series_2': '#B5A8E5', 'series_3': '#E2C17C', 'series_4': '#78CDB8', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Copper Dusk': {
        'bg': '#0D0B09', 'panel': '#161210', 'panel2': '#1E1916', 'grid': '#302822',
        'border': '#3C322A', 'control': '#241E1A', 'control_top': '#2A231E',
        'control_hover': '#2F2721', 'control_border': '#53463B', 'text': '#EEE6DC',
        'muted': '#C0AF9D', 'green': '#A0CD8E', 'red': '#FF5B58', 'cyan': '#DEB07D',
        'info': '#9ABBDD', 'amber': '#E4C47E', 'purple': '#C3ACDE', 'take_profit': '#86CCB7',
        'risk': '#F0AA79', 'active': '#2B241E', 'orderbook_bg': '#000000', 'book_grid': '#2D2520',
        'orderbook_muted': '#C0AF9D', 'orderbook_value_text': '#EEE6DC',
        'orderbook_badge_text': '#EEE6DC', 'reference_price': '#C0AF9D', 'series_1': '#9ABBDD',
        'series_2': '#C3ACDE', 'series_3': '#E4C47E', 'series_4': '#86CCB7', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Paper Desk': {
        'bg': '#EFECE5', 'panel': '#FCFBF7', 'panel2': '#F6F2E9', 'grid': '#E0DACE',
        'border': '#CAC1B2', 'control': '#F0EADF', 'control_top': '#F8F4ED',
        'control_hover': '#E5DECF', 'control_border': '#AA9B85', 'text': '#302D27',
        'muted': '#615747', 'green': '#256841', 'red': '#AE273D', 'cyan': '#795529',
        'info': '#365F88', 'amber': '#795700', 'purple': '#6A4E8C', 'take_profit': '#24696D',
        'risk': '#98451F', 'active': '#E6DBC7', 'orderbook_bg': '#FCFBF7', 'book_grid': '#E3DED4',
        'orderbook_muted': '#675D4D', 'orderbook_value_text': '#38332A',
        'orderbook_badge_text': '#302D27', 'reference_price': '#6F6657', 'series_1': '#365F88',
        'series_2': '#6A4E8C', 'series_3': '#795700', 'series_4': '#24696D', 'chart_bg': '#FFFFFF',
        'chart_grid': '#E0E3EB', 'chart_border': '#D1D4DC', 'chart_panel2': '#F8F9FD',
        'chart_control': '#F0F3FA', 'chart_control_border': '#B2B5BE', 'chart_text': '#131722',
        'chart_muted': '#5D606B',
    },
    'OLED Black': {
        'bg': '#000000', 'panel': '#07080A', 'panel2': '#111418', 'grid': '#242B32',
        'border': '#2C353E', 'control': '#1B2026', 'control_top': '#1F252C',
        'control_hover': '#222930', 'control_border': '#3E4957', 'text': '#E9F0F6',
        'muted': '#A3B8C7', 'green': '#56D49E', 'red': '#FF5261', 'cyan': '#80CBF3',
        'info': '#99B7EF', 'amber': '#E5C275', 'purple': '#B6A1ED', 'take_profit': '#71CEC8',
        'risk': '#F2A17A', 'active': '#20262D', 'orderbook_bg': '#000000', 'book_grid': '#21272F',
        'orderbook_muted': '#A3B8C7', 'orderbook_value_text': '#E9F0F6',
        'orderbook_badge_text': '#E9F0F6', 'reference_price': '#A3B8C7', 'series_1': '#99B7EF',
        'series_2': '#B6A1ED', 'series_3': '#E5C275', 'series_4': '#71CEC8', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Amethyst': {
        'bg': '#0D0A0F', 'panel': '#161119', 'panel2': '#1E1823', 'grid': '#302638',
        'border': '#3B2F45', 'control': '#241D2A', 'control_top': '#2A2131',
        'control_hover': '#2F2536', 'control_border': '#534160', 'text': '#EEE8F4',
        'muted': '#BEAECF', 'green': '#79D2A8', 'red': '#FF5673', 'cyan': '#C2A7ED',
        'info': '#9BB8E7', 'amber': '#E1C287', 'purple': '#C9ACE9', 'take_profit': '#80CCC9',
        'risk': '#EFA989', 'active': '#2B2231', 'orderbook_bg': '#000000', 'book_grid': '#2C2334',
        'orderbook_muted': '#BEAECF', 'orderbook_value_text': '#EEE8F4',
        'orderbook_badge_text': '#EEE8F4', 'reference_price': '#BEAECF', 'series_1': '#9BB8E7',
        'series_2': '#C9ACE9', 'series_3': '#E1C287', 'series_4': '#80CCC9', 'chart_bg': '#131722',
        'chart_grid': '#2A2E39', 'chart_border': '#363A45', 'chart_panel2': '#1E222D',
        'chart_control': '#2A2E39', 'chart_control_border': '#434651', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Evergreen': {
        'bg': '#090C0A', 'panel': '#0E1411', 'panel2': '#141C18', 'grid': '#202D26',
        'border': '#27372F', 'control': '#18221C', 'control_top': '#1C2721',
        'control_hover': '#1F2C25', 'control_border': '#374D41', 'text': '#E5EFE6',
        'muted': '#B0C5B3', 'green': '#78CF99', 'red': '#FF5C62', 'cyan': '#A8CDB2',
        'info': '#9ABBD8', 'amber': '#E0C382', 'purple': '#BFADE0', 'take_profit': '#7AD0BE',
        'risk': '#EEA57B', 'active': '#1C2821', 'orderbook_bg': '#000000', 'book_grid': '#1D2923',
        'orderbook_muted': '#B0C5B3', 'orderbook_value_text': '#E5EFE6',
        'orderbook_badge_text': '#E5EFE6', 'reference_price': '#B0C5B3', 'series_1': '#9ABBD8',
        'series_2': '#BFADE0', 'series_3': '#E0C382', 'series_4': '#7AD0BE', 'chart_bg': '#000000',
        'chart_grid': '#252525', 'chart_border': '#3A3A3A', 'chart_panel2': '#101010',
        'chart_control': '#1A1A1A', 'chart_control_border': '#444444', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
    'Cobalt Amber': {
        'bg': '#090C11', 'panel': '#0E131D', 'panel2': '#141A28', 'grid': '#1F2A3F',
        'border': '#26344E', 'control': '#171F30', 'control_top': '#1B2437',
        'control_hover': '#1E293E', 'control_border': '#36486D', 'text': '#E8EFF9',
        'muted': '#A9BDD8', 'green': '#85C6FF', 'red': '#FF5967', 'cyan': '#EBC17D',
        'info': '#85B5F0', 'amber': '#EBC17D', 'purple': '#B8A6E7', 'take_profit': '#78CFC4',
        'risk': '#EFA18A', 'active': '#1C2538', 'orderbook_bg': '#000000', 'book_grid': '#1D273A',
        'orderbook_muted': '#A9BDD8', 'orderbook_value_text': '#E8EFF9',
        'orderbook_badge_text': '#E8EFF9', 'reference_price': '#A9BDD8', 'series_1': '#85B5F0',
        'series_2': '#B8A6E7', 'series_3': '#EBC17D', 'series_4': '#78CFC4', 'chart_bg': '#131722',
        'chart_grid': '#2A2E39', 'chart_border': '#363A45', 'chart_panel2': '#1E222D',
        'chart_control': '#2A2E39', 'chart_control_border': '#434651', 'chart_text': '#D1D4DC',
        'chart_muted': '#9598A1',
    },
}
# Resolve the named/default book once, just like an explicit Nightwatch override.
# Bare constructors keep their cheap preset-copy path and the same semantic colors.
ORDERBOOK_THEMES[DEFAULT_ORDERBOOK_THEME_NAME].update(orderbook_palette(theme=NIGHTWATCH_COLORS))

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
    'OLED Black': {'style_family': 'dark_terminal',
        'identity': 'oled_black', 'chart_surface': 'black'},
    'Amethyst': {'style_family': 'dark_terminal',
        'identity': 'amethyst', 'chart_surface': 'gray'},
    'Evergreen': {'style_family': 'dark_terminal',
        'identity': 'evergreen', 'chart_surface': 'black'},
    'Cobalt Amber': {'style_family': 'dark_terminal',
        'identity': 'cobalt_amber', 'chart_surface': 'gray'},
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

def _compact_shell_stylesheet(stylesheet: str) -> str:
    """Drop declarations superseded by the same selector later in the sheet.

    Keep rule order and distinct selectors intact so specificity, state rules
    and shorthand declarations retain Qt's cascade. This avoids repeatedly
    matching the legacy/base declarations replaced by terminal overrides.
    """
    stylesheet = re.sub(r'/\*.*?\*/', '', stylesheet, flags=re.S)
    rules = re.findall(r'([^{}]+)\{([^{}]*)\}', stylesheet)
    seen: dict[str, set[str]] = {}
    output = []
    for selectors, body in reversed(rules):
        declarations = [tuple(part.strip() for part in declaration.split(':', 1))
                        for declaration in body.split(';') if ':' in declaration]
        groups: dict[tuple[tuple[str, str], ...], list[str]] = {}
        for selector in selectors.split(','):
            selector = ' '.join(selector.split())
            properties = seen.setdefault(selector, set())
            retained = []
            for key, value in reversed(declarations):
                if key not in properties:
                    retained.append((key, value))
                    properties.add(key)
            if retained:
                groups.setdefault(tuple(reversed(retained)), []).append(selector)
        output.append(''.join(','.join(selectors) + '{' +
                             ';'.join(f'{key}:{value}' for key, value in declarations) + ';}'
                             for declarations, selectors in groups.items()))
    return '\n'.join(reversed(output))

def build_shell_stylesheet(theme_name: str, theme: dict[str, str], surfaces: dict[str, int] | None=None, status: dict[str, object] | None=None) -> str:
    t = theme
    terminal_style = theme_style_family(theme_name) in {'dark_terminal', 'light_terminal'}
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
    if terminal_style:
        radius = f"{int(surface_style['element_radius'])}px"
        section_radius = f"{int(surface_style['block_radius'])}px"
    else:
        radius = '2px'
        section_radius = '1px'
    block_border = '0' if block_border_width <= 0 else f"{block_border_width}px solid {t['border']}"
    block_separator = '0' if block_border_width <= 0 else f'{block_border_width}px solid {separator}'
    menu_bar_bg = t['topbar'] if terminal_style else t['bg']
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
    if terminal_style:
        nightwatch_overrides = f'''
        /* Terminal chrome: compact panels, crisp outlines and small
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
            background: none;
            border: 0;
            spacing: 6px;
            padding: 2px 0;
        }}
        QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled {{
            color: {t['muted']};
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
            background: {active_line};
            border-color: {active_line};
        }}
        QRadioButton::indicator:checked {{
            background: {active_line};
            border-color: {active_line};
        }}
        QRadioButton::indicator {{ border-radius: 7px; }}
        QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
            background: {t['panel2']};
            border: 1px solid {t['border']};
        }}
        QCheckBox::indicator:checked:disabled, QRadioButton::indicator:checked:disabled {{
            background: {t['control_border']};
            border-color: {t['control_border']};
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
    stylesheet = f'''
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
            QDialog#nightwatchSettingsDialog QTabWidget#settingsSectionTabs::pane {{
                border: 0; background: {t['panel']};
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsSectionTabs QTabBar::tab {{
                min-height: 26px; min-width: 0; padding: 5px 12px; margin: 0;
                background: {t['panel2']}; color: {t['muted']};
                border: 0; border-bottom: 2px solid transparent;
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsSectionTabs QTabBar::tab:hover {{
                background: {control_hover}; color: {t['text']};
            }}
            QDialog#nightwatchSettingsDialog QTabWidget#settingsSectionTabs QTabBar::tab:selected {{
                background: {t['panel']}; color: {t['text']}; border-bottom-color: {active_line};
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
    return _compact_shell_stylesheet(stylesheet)


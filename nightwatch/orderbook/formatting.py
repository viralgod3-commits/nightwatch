"""Numeric formatting shared by the GUI and headless market workers."""
from ..models import safe_float


def _general_to_fixed(text: str) -> str:
    """Convert a short general-format float string to fixed notation."""
    lowered = text.lower()
    if 'e' not in lowered:
        if '.' in lowered:
            lowered = lowered.rstrip('0').rstrip('.')
        return '0' if lowered in {'', '-0'} else lowered
    mantissa, exponent_text = lowered.split('e', 1)
    exponent = int(exponent_text)
    negative = mantissa.startswith('-')
    if negative:
        mantissa = mantissa[1:]
    whole, _dot, fraction = mantissa.partition('.')
    digits = whole + fraction
    decimal_position = len(whole) + exponent
    if decimal_position <= 0:
        output = '0.' + '0' * -decimal_position + digits
    elif decimal_position >= len(digits):
        output = digits + '0' * (decimal_position - len(digits))
    else:
        output = digits[:decimal_position] + '.' + digits[decimal_position:]
    if '.' in output:
        output = output.rstrip('0').rstrip('.')
    if negative and output != '0':
        output = '-' + output
    return output


def format_book_price(value: float, decimals: int | None = None) -> str:
    """Fixed-point price text without arbitrary-precision number allocation."""
    value = safe_float(value)
    if not value:
        precision = max(0, min(16, int(decimals or 0)))
        return f'{0.0:.{precision}f}' if decimals is not None else '0'
    if decimals is not None:
        precision = max(0, min(16, int(decimals)))
        return f'{value:.{precision}f}'
    return _general_to_fixed(format(value, '.12g'))

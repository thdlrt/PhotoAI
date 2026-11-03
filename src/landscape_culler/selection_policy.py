"""Shared, model-independent fixed-retention policy."""
from decimal import Decimal, ROUND_CEILING
from numbers import Integral, Real


def retention_budget(count: int, retain_ratio: float) -> int:
    if isinstance(count, bool) or not isinstance(count, Integral) or count < 0:
        raise ValueError("照片数量必须是非负整数。")
    if isinstance(retain_ratio, bool) or not isinstance(retain_ratio, Real):
        raise ValueError("retain_ratio 必须在 0.05 到 1.0 之间。")
    ratio = Decimal(str(retain_ratio))
    if not ratio.is_finite() or not Decimal("0.05") <= ratio <= Decimal("1"):
        raise ValueError("retain_ratio 必须在 0.05 到 1.0 之间。")
    # str(float) recovers the entered decimal value. Binary multiplication can
    # turn 100 * .07 into 7.000000000000001 and incorrectly admit an eighth photo.
    return int((Decimal(int(count)) * ratio).to_integral_value(rounding=ROUND_CEILING))

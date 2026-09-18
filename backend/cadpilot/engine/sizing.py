"""Deterministic engineering calculations (never delegated to an LLM)."""

from __future__ import annotations

import math

from ..model.engineering import Quantity

_TO_M3H = {"KLPH": 1.0, "LPH": 0.001, "m3/h": 1.0}


def flow_m3h(q: Quantity | None) -> float | None:
    if q is None:
        return None
    factor = _TO_M3H.get(q.unit)
    return q.value * factor if factor is not None else None


def velocity(flow_m3_h: float, dn: int) -> float:
    area = math.pi * (dn / 1000) ** 2 / 4
    return flow_m3_h / 3600 / area


def select_dn(flow_m3_h: float, target_velocity: float, series: list[int]) -> int:
    """Smallest standard DN whose velocity at design flow is at or below the target."""
    for dn in series:
        if velocity(flow_m3_h, dn) <= target_velocity:
            return dn
    return series[-1]

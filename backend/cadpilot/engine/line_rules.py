"""Where a line rule applies, and how a line is sized — one definition shared by the rules
topology agent, the LLM-plan compiler and the validator, so they can never disagree.

`applies_to` in a line rule is either a named case:
    pump_suction      the line into a centrifugal pump
    pump_discharge    the line out of a centrifugal pump
    header_branch     a line to or from a header
    bypass            a bypass line
or a filter written in the rule itself (all given keys must match):
    {from_type: [boiler], to_type: [...], service: [ST], not_service: [...], header_branch: true, kind: process}
"""

from __future__ import annotations

from .sizing import select_dn, velocity

NAMED = {
    "pump_suction": lambda fk, tk, kind, svc: tk == "centrifugal_pump" and kind != "bypass",
    "pump_discharge": lambda fk, tk, kind, svc: fk == "centrifugal_pump" and kind != "bypass",
    "header_branch": lambda fk, tk, kind, svc: "header" in (fk, tk) and kind != "bypass",
    "bypass": lambda fk, tk, kind, svc: kind == "bypass",
}


def _one_of(value: str | None, allowed) -> bool:
    return value in (allowed if isinstance(allowed, list) else [allowed])


def applies(applies_to, from_kind: str | None, to_kind: str | None, line_kind: str, service: str | None) -> bool:
    if isinstance(applies_to, str):
        pred = NAMED.get(applies_to)
        return bool(pred and pred(from_kind, to_kind, line_kind, service))
    if line_kind != applies_to.get("kind", "process"):
        return False
    if applies_to.get("header_branch") and "header" not in (from_kind, to_kind):
        return False
    if "not_service" in applies_to and _one_of(service, applies_to["not_service"]):
        return False
    checks = (("from_type", from_kind), ("to_type", to_kind), ("service", service))
    return all(_one_of(v, applies_to[k]) for k, v in checks if k in applies_to)


def in_scope(rule: dict, area: str | None, module_of_area: dict[str, str | None]) -> bool:
    """A module's own rule applies only to lines of that module (a shared rule applies everywhere).
    Every module has its own area number, so the line's area says which module it belongs to."""
    return not rule.get("module") or module_of_area.get(area or "") == rule["module"]


def module_of_area(process) -> dict[str, str | None]:
    return {st.area: st.module for st in process.stages}


def describe(applies_to) -> str:
    if isinstance(applies_to, str):
        return f"{applies_to.replace('_', ' ')} connections"
    parts = [f"{k.replace('_', ' ')} {'/'.join(v) if isinstance(v, list) else v}" for k, v in applies_to.items()
             if k not in ("kind", "header_branch")]
    if applies_to.get("header_branch"):
        parts.insert(0, "a header")
    return "connections with " + ", ".join(parts)


def owner(applies_to, u: str, d: str) -> str:
    """What a valve serves (its tag identity), so re-routing a line keeps the valve's tag."""
    if applies_to == "pump_suction":
        return d
    if applies_to == "pump_discharge":
        return u
    return f"{u}>{d}"


# ---- sizing -----------------------------------------------------------------------------------


def service_sizing(rules: dict, service: str | None) -> tuple[float, float, float, str]:
    """(target velocity, max velocity, volume factor, flow unit) for a line service.

    Flows are carried internally as water-equivalent m³/h (1 m³/h = 1000 kg/h). A gas service such
    as steam declares its density: its real volume is larger by 1000 / density."""
    base = rules["sizing"]
    svc = base.get("services", {}).get(service or "", {})
    density = svc.get("density_kg_m3", 1000)
    return (
        svc.get("target_velocity_m_s", base["target_velocity_m_s"]),
        svc.get("max_velocity_m_s", base.get("max_velocity_m_s", 2.5)),
        1000 / density,
        svc.get("flow_unit", "KLPH"),
    )


def sized(rules: dict, service: str | None) -> bool:
    """False for services that are not pipes (packed product on a conveyor)."""
    return rules["sizing"].get("services", {}).get(service or "", {}).get("size", True)


def size(rules: dict, service: str | None, flow_m3h: float) -> int | None:
    if not sized(rules, service):
        return None
    target, _vmax, factor, _unit = service_sizing(rules, service)
    return select_dn(flow_m3h * factor, target, rules["sizing"]["dn_series"])


def flow_quantity(rules: dict, service: str | None, flow_m3h: float) -> tuple[float, str]:
    unit = service_sizing(rules, service)[3]
    return (round(flow_m3h * 1000), "kg/h") if unit == "kg/h" else (round(flow_m3h, 2), "KLPH")


def line_velocity(rules: dict, service: str | None, flow_m3h: float, dn: int) -> tuple[float, float]:
    """(velocity, allowed maximum) of a line."""
    _t, vmax, factor, _u = service_sizing(rules, service)
    return velocity(flow_m3h * factor, dn), vmax

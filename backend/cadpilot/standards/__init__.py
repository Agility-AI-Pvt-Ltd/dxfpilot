"""Configurable standards layer.

Engineering modules (standards/modules/*.yaml) are self-contained building blocks of a dairy plant:
each brings its process stages, train groups, symbols, tag prefixes, services and its own line and
instrumentation rules. A project draws one module or a composition of several; the composition is
addressed as the template id "modules:<id>+<id>+..." so the rest of the engine sees one template.

Standards are layered: the bundled international baseline, then optional overlays (e.g. an
organisation's standards, then a project configuration) listed in CADPILOT_STANDARDS_OVERLAYS
(os.pathsep-separated directories). Later layers deep-merge over earlier ones.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BASELINE_DIR = Path(__file__).parent
FILES = ("process_templates", "tagging", "line_rules", "instrumentation", "validation", "layout", "symbols", "ingest")
MODULE_PREFIX = "modules:"


def _deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return over


@dataclass(frozen=True)
class Standards:
    process_templates: dict
    tagging: dict
    line_rules: dict
    instrumentation: dict
    validation: dict
    layout: dict
    symbols: dict
    ingest: dict
    layers: tuple[str, ...]
    modules: dict = field(default_factory=dict)  # module id -> module definition

    def template(self, template_id: str) -> dict:
        if template_id.startswith(MODULE_PREFIX):
            return self.compose(module_ids(template_id))
        if template_id in self.modules:
            return self.compose([template_id])
        return self.process_templates["templates"][template_id]

    def compose(self, ids: list[str]) -> dict:
        """One template made of several modules: their stages in module order, each tagged with its module."""
        unknown = [i for i in ids if i not in self.modules]
        if unknown:
            raise KeyError(f"Unknown engineering module(s): {', '.join(unknown)}")
        mods = sorted((self.modules[i] for i in dict.fromkeys(ids)), key=lambda m: m.get("order", 99))
        stages, groups, areas = [], {}, {}
        for m in mods:
            areas[str(m["area"])] = m["area_name"]
            groups |= copy.deepcopy(m["groups"])
            for st in m["stages"]:
                stages.append({**copy.deepcopy(st), "module": m["id"], "area": str(st.get("area", m["area"])),
                               "sections": list(m.get("sections", []))})
        return {
            "name": " + ".join(m["name"] for m in mods),
            "modules": [m["id"] for m in mods],
            "keywords": [],
            "areas": areas,
            "groups": groups,
            "stages": stages,
        }

    def equipment_symbol(self, type_: str) -> dict:
        return self.symbols["equipment"][type_]

    def ports_for(self, type_: str) -> dict[str, dict]:
        if type_ in self.symbols["equipment"]:
            return self.symbols["equipment"][type_]["ports"]
        if type_ in self.symbols["piping_nodes"]:
            return self.symbols["piping_nodes"][type_]["ports"]
        return {}


def module_ids(template_id: str) -> list[str]:
    return [m for m in template_id.removeprefix(MODULE_PREFIX).split("+") if m]


def module_template_id(ids: list[str], standards: "Standards") -> str:
    order = sorted(dict.fromkeys(ids), key=lambda i: standards.modules[i].get("order", 99))
    return MODULE_PREFIX + "+".join(order)


def _load_modules(dirs: tuple[str, ...]) -> dict:
    modules: dict[str, dict] = {}
    for d in dirs:
        for path in sorted((Path(d) / "modules").glob("*.yaml")):
            m = yaml.safe_load(path.read_text()) or {}
            modules[m["id"]] = _deep_merge(modules.get(m["id"], {}), m)
    # stage and group ids are global names (rules and corrections refer to them): no clashes
    seen: dict[str, str] = {}
    for m in modules.values():
        for kind, ids in (("stage", [s["id"] for s in m["stages"]]), ("group", list(m["groups"]))):
            for i in ids:
                other = seen.setdefault(f"{kind}:{i}", m["id"])
                if other != m["id"]:
                    raise ValueError(f"Engineering modules {other} and {m['id']} both define {kind} '{i}'")
    return dict(sorted(modules.items(), key=lambda kv: kv[1].get("order", 99)))  # plant order


def _merge_modules(data: dict[str, dict], modules: dict) -> None:
    """A module's symbols, tag prefixes, services and rules join the shared libraries."""
    for m in modules.values():
        data["symbols"].setdefault("equipment", {}).update(copy.deepcopy(m.get("symbols", {})))
        data["tagging"]["equipment"]["prefixes"].update(m.get("tag_prefixes", {}))
        data["process_templates"].setdefault("services", {}).update(m.get("services", {}))
        data["line_rules"]["valves"] = data["line_rules"]["valves"] + [
            {**r, "module": m["id"]} for r in m.get("line_rules", [])
        ]
        data["instrumentation"]["rules"] = data["instrumentation"]["rules"] + [
            {**r, "module": m["id"]} for r in m.get("instrumentation_rules", [])
        ]
    # symbol aliases: `same_as: <symbol>` reuses the geometry of another symbol
    eq = data["symbols"]["equipment"]
    for name, sym in list(eq.items()):
        if "same_as" in sym:
            base = copy.deepcopy(eq[sym["same_as"]])
            eq[name] = {**base, **{k: v for k, v in sym.items() if k != "same_as"}}


def _load(dirs: tuple[str, ...]) -> Standards:
    data: dict[str, dict] = {}
    for name in FILES:
        merged: dict = {}
        for d in dirs:
            path = Path(d) / f"{name}.yaml"
            if path.exists():
                merged = _deep_merge(merged, yaml.safe_load(path.read_text()) or {})
        data[name] = merged
    modules = _load_modules(dirs)
    _merge_modules(data, modules)
    return Standards(**data, layers=dirs, modules=modules)


@lru_cache(maxsize=8)
def _cached(dirs: tuple[str, ...]) -> Standards:
    return _load(dirs)


def get_standards() -> Standards:
    overlays = [p for p in os.environ.get("CADPILOT_STANDARDS_OVERLAYS", "").split(os.pathsep) if p]
    return _cached((str(BASELINE_DIR), *overlays))

"""Configurable standards layer.

Standards are layered: the bundled international baseline, then optional overlays (e.g. an
organisation's standards, then a project configuration) listed in CADPILOT_STANDARDS_OVERLAYS
(os.pathsep-separated directories). Later layers deep-merge over earlier ones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BASELINE_DIR = Path(__file__).parent
FILES = ("process_templates", "tagging", "line_rules", "instrumentation", "validation", "layout", "symbols")


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
    layers: tuple[str, ...]

    def template(self, template_id: str) -> dict:
        return self.process_templates["templates"][template_id]

    def equipment_symbol(self, type_: str) -> dict:
        return self.symbols["equipment"][type_]

    def ports_for(self, type_: str) -> dict[str, dict]:
        if type_ in self.symbols["equipment"]:
            return self.symbols["equipment"][type_]["ports"]
        if type_ in self.symbols["piping_nodes"]:
            return self.symbols["piping_nodes"][type_]["ports"]
        return {}


def _load(dirs: tuple[str, ...]) -> Standards:
    data: dict[str, dict] = {}
    for name in FILES:
        merged: dict = {}
        for d in dirs:
            path = Path(d) / f"{name}.yaml"
            if path.exists():
                merged = _deep_merge(merged, yaml.safe_load(path.read_text()) or {})
        data[name] = merged
    return Standards(**data, layers=dirs)


@lru_cache(maxsize=8)
def _cached(dirs: tuple[str, ...]) -> Standards:
    return _load(dirs)


def get_standards() -> Standards:
    overlays = [p for p in os.environ.get("CADPILOT_STANDARDS_OVERLAYS", "").split(os.pathsep) if p]
    return _cached((str(BASELINE_DIR), *overlays))

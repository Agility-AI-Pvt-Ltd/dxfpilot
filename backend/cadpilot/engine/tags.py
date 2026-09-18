"""Deterministic, stable tag allocation.

Tags are keyed by the *identity* of an object (e.g. "equipment:pasteurizer#1"), not by its
position in a list. The registry persists with the project, so regenerating after a correction
keeps every unchanged object's tag; only genuinely new objects get new numbers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..standards import Standards


class TagRegistry(BaseModel):
    assigned: dict[str, str] = Field(default_factory=dict)  # identity key -> tag
    counters: dict[str, int] = Field(default_factory=dict)  # namespace -> last sequence

    def _next(self, namespace: str) -> int:
        self.counters[namespace] = self.counters.get(namespace, 0) + 1
        return self.counters[namespace]

    def allocate(self, key: str, namespace: str, fmt: str, **fields: object) -> str:
        if key in self.assigned:
            return self.assigned[key]
        taken = set(self.assigned.values())
        while True:
            tag = fmt.format(seq=self._next(namespace), **fields)
            if tag not in taken:
                break
        self.assigned[key] = tag
        return tag


class Tagger:
    def __init__(self, standards: Standards, registry: TagRegistry):
        self.cfg = standards.tagging
        self.registry = registry

    def equipment(self, key: str, type_: str, area: str) -> str:
        prefix = self.cfg["equipment"]["prefixes"].get(type_, "X")
        return self.registry.allocate(
            f"equipment:{key}", f"eq:{prefix}:{area}", self.cfg["equipment"]["format"], prefix=prefix, area=area
        )

    def piping_node(self, key: str, kind: str, area: str) -> str:
        prefix = self.cfg["piping_nodes"]["prefixes"][kind]
        return self.registry.allocate(
            f"node:{key}", f"node:{prefix}:{area}", self.cfg["piping_nodes"]["format"], prefix=prefix, area=area
        )

    def line(self, key: str, service: str, area: str) -> str:
        return self.registry.allocate(
            f"line:{key}", f"line:{area}", self.cfg["lines"]["format"], service=service, area=area
        )

    def loop_number(self, key: str, area: str) -> str:
        """A shared loop number for all instruments of one measurement/control point."""
        return self.registry.allocate(f"loop:{key}", f"loop:{area}", "{area}{seq:02d}", area=area)

    def instrument(self, function: str, loop_no: str) -> str:
        return f"{function}-{loop_no}"

    def valve(self, key: str, klass: str, area: str) -> str:
        fmt = self.cfg["valves"][klass]["format"]
        return self.registry.allocate(f"valve:{key}", f"valve:{klass}:{area}", fmt, area=area)

    def line_label(self, tag: str, dn: int | None, spec: str) -> str:
        return self.cfg["lines"]["label_format"].format(dn=dn or "?", tag=tag, spec=spec)

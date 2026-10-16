"""Which target connection each source connection points at (pure logic, no I/O).

A promotion can carry tables from more than one source connection, so the remap is a mapping
rather than one source name and one target name. The names come from the exported content and
from the target cluster's own connection list, so nobody has to type them.
"""
import json

import yaml


def _doc(edoc):
    edoc = edoc or "{}"
    return json.loads(edoc) if edoc.strip().startswith("{") else (yaml.safe_load(edoc) or {})


def connections_in(items) -> list:
    """Every connection name the exported content actually uses (tables, and the model table
    entries that carry one), sorted. Variable tokens (${...}) are left to the variable system."""
    names = set()
    for it in items or []:
        d = _doc(it.get("edoc"))
        refs = [d.get("table") or {}]
        for key in ("model", "worksheet"):
            obj = d.get(key) or {}
            refs += list(obj.get("model_tables") or []) + list(obj.get("tables") or [])
        for r in refs:
            c = r.get("connection") if isinstance(r, dict) else None
            n = (c or {}).get("name") if isinstance(c, dict) else None
            if n and not n.strip().startswith("${"):
                names.add(n)
    return sorted(names)


def saved_map(team_cfg: dict, found=()) -> dict:
    """The mapping the team config holds. Older configs carry one source name and one target
    name instead; read those the way the transform always did: a blank source meant every
    connection goes to the target."""
    m = {k: v for k, v in (team_cfg.get("connection_map") or {}).items() if k and v}
    if m:
        return m
    src = (team_cfg.get("source_connection") or "").strip()
    tgt = (team_cfg.get("target_connection") or "").strip()
    if not tgt:
        return {}
    if src:
        return {src: tgt}
    return {n: tgt for n in found}


def effective_map(team_cfg: dict, found, target_names=None) -> dict:
    """The mapping the transform applies: the saved choices, plus any found connection that has
    no choice but exists on the target under the same name. `target_names` is None when the
    target's list could not be read, in which case nothing is assumed about it."""
    m = saved_map(team_cfg, found)
    if target_names is not None:
        have = set(target_names)
        for n in found:
            if n not in m and n in have:
                m[n] = n
    return m


def unmapped(found, eff_map: dict) -> list:
    """Found connections with no target connection to point at."""
    return [n for n in found if not eff_map.get(n)]


def target_for(source_name: str, eff_map: dict) -> str:
    """The target connection a table on `source_name` lands on (its own name when unmapped)."""
    return eff_map.get(source_name) or source_name

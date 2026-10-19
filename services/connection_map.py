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


# ── database.schema per source connection ──────────────────────────────────────────────────

def saved_locations(team_cfg: dict) -> dict:
    """{(source_db, source_schema): (target_db, target_schema)} saved for the team."""
    out = {}
    for e in team_cfg.get("location_map") or []:
        s = (e.get("source_db") or "", e.get("source_schema") or "")
        t = (e.get("target_db") or "", e.get("target_schema") or "")
        if any(s) and any(t):
            out[s] = t
    return out


def locations_by_connection(sel_tables: dict) -> dict:
    """{source_connection: {(db, schema): [table names]}} for the selected tables."""
    out = {}
    for t in (sel_tables or {}).values():
        conn = t.get("connection") or ""
        out.setdefault(conn, {}).setdefault((t.get("db") or "", t.get("schema") or ""), []).append(t["name"])
    return out


def target_copies(sel_tables: dict, source_connection: str, pair=None) -> dict:
    """Where the selected tables on `source_connection` (optionally only those in one source
    database.schema `pair`) already live on the target:
    {"connections": {name, ...}, "locations": {(db, schema), ...}, "on_target": [names],
     "missing": [names]}."""
    conns, locs, on, missing = set(), set(), [], []
    for t in (sel_tables or {}).values():
        if (t.get("connection") or "") != source_connection:
            continue
        if pair is not None and (t.get("db") or "", t.get("schema") or "") != pair:
            continue
        tg = t.get("target")
        if tg:
            on.append(t["name"])
            if tg.get("connection"):
                conns.add(tg["connection"])
            locs.add((tg.get("db") or "", tg.get("schema") or ""))
        else:
            missing.append(t["name"])
    return {"connections": conns, "locations": locs, "on_target": on, "missing": missing}


def effective_locations(saved: dict, sel_tables: dict) -> dict:
    """The database.schema mapping the transform applies: the saved choices, plus, for a source
    pair whose selected tables all already live under one database.schema on the target, that
    one. Shipping the source's coordinates instead would re-point those existing tables."""
    out = dict(saved)
    for conn, pairs in locations_by_connection(sel_tables).items():
        for pair in pairs:
            if pair in out:
                continue
            locs = target_copies(sel_tables, conn, pair)["locations"]
            if len(locs) == 1:
                t = next(iter(locs))
                if t != pair:
                    out[pair] = t
    return out

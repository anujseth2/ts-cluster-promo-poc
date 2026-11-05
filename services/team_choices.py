"""What a team decided in earlier runs, so a repeat run of the same content doesn't ask again.

Per team: columns dropped, tables pruned, column types realigned, and columns recased (to the
source's casing on Source Audit, or the target's on TML Validation). Every entry is scoped to a
table and column ("table::col", lowercase table), so it only takes effect where that same column
turns up again. Kept in a local, gitignored JSON file next to the team config.
"""
import json
import os
from pathlib import Path


def empty() -> dict:
    return {"drop_columns": [], "prune_tables": [], "realign_types": {},
            "recase_source": {}, "recase_target": {}}


def _normalise(rec) -> dict:
    rec = rec or {}
    out = empty()
    out["drop_columns"] = sorted({str(x) for x in rec.get("drop_columns") or [] if x})
    out["prune_tables"] = sorted({str(x) for x in rec.get("prune_tables") or [] if x})
    for key in ("realign_types", "recase_source", "recase_target"):
        out[key] = {str(k): str(v) for k, v in sorted((rec.get(key) or {}).items()) if k and v}
    return out


def is_empty(choices: dict) -> bool:
    return not any(_normalise(choices).values())


def load_all(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return {team: _normalise(rec) for team, rec in (data or {}).items()}


def load(path, team: str) -> dict:
    return load_all(path).get(team, empty())


def save(path, team: str, choices: dict) -> None:
    """Replace one team's record (dropping it when empty) and write the file atomically."""
    data = load_all(path)
    rec = _normalise(choices)
    if any(rec.values()):
        data[team] = rec
    else:
        data.pop(team, None)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)


def _scoped(key):
    t, _, c = (key or "").partition("::")
    return t.strip().lower(), c.strip().lower()


# The source audit compares the TML with the source warehouse, so a column deliberately set to the
# target's type or casing reads as source drift there. Offering to undo it would only bring the
# target failure back, so the audit leaves these out and says why.

def realigned_for_target(choices: dict, table: str, column: str, tml_type: str) -> bool:
    """True when the column's TML type is a realignment the team chose (so not drift to undo)."""
    want = ((table or "").strip().lower(), (column or "").strip().lower())
    return any(_scoped(k) == want and str(v).strip().lower() == (tml_type or "").strip().lower()
               for k, v in (choices.get("realign_types") or {}).items())


def recased_for_target(choices: dict, table: str, db_column: str) -> bool:
    """True when the column was recased to the target's casing and still carries it."""
    want = ((table or "").strip().lower(), (db_column or "").strip().lower())
    return any(_scoped(k) == want and v == (db_column or "").strip()
               for k, v in (choices.get("recase_target") or {}).items())


def describe(choices: dict) -> list:
    """One plain line per remembered choice, for showing what will be applied."""
    c = _normalise(choices)
    lines = [f"Drop `{x.replace('::', '.')}`" for x in c["drop_columns"]]
    lines += [f"Leave table `{x}` out" for x in c["prune_tables"]]
    lines += [f"Realign `{k.replace('::', '.')}` to {v}" for k, v in c["realign_types"].items()]
    lines += [f"Recase `{k.replace('::', '.')}` to `{v}` (source casing)"
              for k, v in c["recase_source"].items()]
    lines += [f"Recase `{k.replace('::', '.')}` to `{v}` (target casing)"
              for k, v in c["recase_target"].items()]
    return lines

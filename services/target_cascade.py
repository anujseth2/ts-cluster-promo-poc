"""Plan the full cascade of changes a dropped column forces on the TARGET.

A column does not go quietly. Anything on the target that references it blocks the import, and
"anything" is a tree: a model uses the column, that model has answers and liveboards, and those
may feed further objects. Deleting the top of the tree to unblock an import is the blunt answer
and takes work nobody asked to lose, so each kind is handled the way that costs least:

    model      strip the column out of it, then walk ITS dependents
    liveboard  remove only the tiles that use the column, leaving the rest of the board
    answer     delete it, because an answer IS a single visualisation

Planning is separated from doing on purpose. The plan is built first, in full, with no writes:
it can be shown to the operator, dry-run against the cluster, and refused as a whole. A cascade
applied halfway leaves the target inconsistent AND the import still blocked, which is worse than
not starting.

IO is injected (fetch_tml, fetch_deps) so the walk is testable without a cluster.
"""

from services.import_diagnostics import (
    _parse_edoc, dependents_using_columns, drop_columns, strip_vizzes_from_tml,
)

MODEL_KINDS = ("model", "worksheet")


def _kind_of(doc):
    if not isinstance(doc, dict):
        return "?"
    for k in ("liveboard", "answer", "model", "worksheet", "table"):
        if k in doc:
            return k
    return "?"


def plan_cascade(seeds, column_names, fetch_tml, fetch_deps, skip_ids=()):
    """Walk the dependency tree and decide what happens to each object.

    seeds:        [{"id", "name", "type"}] objects already known to reference the column
    column_names: every spelling of the dropped column(s) (see scan_names_for_drops)
    fetch_tml:    id -> edoc string, or None when it cannot be read
    fetch_deps:   id -> [{"id", "name", "type"}] dependents of that object
    skip_ids:     objects the promotion itself is updating — never touched here

    Returns (actions, blocked).

    actions: [{"id","name","type","action","detail","new_edoc"}] where action is one of
             "strip_columns" | "remove_tiles" | "delete"; new_edoc is None for a delete.
    blocked: [{"id","name","reason"}] anything that cannot be planned — unreadable, or a
             liveboard where EVERY tile uses the column so there is nothing surgical to do.
             Any entry here means the cascade must not be applied.
    """
    want = {str(c).strip().lower() for c in (column_names or ()) if str(c).strip()}
    skip = {str(i) for i in (skip_ids or ())}
    actions, blocked, seen = [], [], set()
    queue = list(seeds or [])

    while queue:
        obj = queue.pop(0)
        oid = str(obj.get("id") or "")
        if not oid or oid in seen or oid in skip:
            continue
        seen.add(oid)

        edoc = fetch_tml(oid)
        if not edoc:
            blocked.append({"id": oid, "name": obj.get("name"),
                            "reason": "its TML could not be read from the target — the account "
                                      "probably cannot see it"})
            continue
        try:
            doc = _parse_edoc({"edoc": edoc})
        except Exception:
            blocked.append({"id": oid, "name": obj.get("name"),
                            "reason": "its TML could not be parsed"})
            continue

        kind = _kind_of(doc)

        if kind == "answer":
            actions.append({"id": oid, "name": obj.get("name"), "type": "ANSWER",
                            "action": "delete", "new_edoc": None,
                            "detail": "deleted — an answer is a single visualisation, so there "
                                      "is nothing to strip"})
            continue

        if kind == "liveboard":
            hits = dependents_using_columns(
                [{"id": oid, "name": obj.get("name"), "type": "LIVEBOARD", "tml": edoc}], want)
            vz = (hits[0].get("vizzes") if hits else []) or []
            total = (hits[0].get("viz_total") if hits else 0) or 0
            if not vz:
                continue                      # nothing on this board uses the column after all
            if len(vz) >= total:
                blocked.append({"id": oid, "name": obj.get("name"),
                                "reason": f"all {total} tile(s) use the column, so removing them "
                                          "would leave an empty board — decide on that one "
                                          "deliberately"})
                continue
            ids = [v["id"] for v in vz]
            new_edoc, removed, remaining = strip_vizzes_from_tml(edoc, ids)
            actions.append({"id": oid, "name": obj.get("name"), "type": "LIVEBOARD",
                            "action": "remove_tiles", "new_edoc": new_edoc,
                            "detail": f"remove {removed} tile(s) ({', '.join(ids)}); "
                                      f"{remaining} left on the board"})
            continue

        if kind in MODEL_KINDS:
            node = doc.get(kind) or {}
            tname = (node.get("model_tables") or [{}])
            scoped = set()
            for c in node.get("columns") or []:
                cid = (c.get("column_id") or "").strip()
                nm = (c.get("name") or "").strip()
                if cid.split("::")[-1].strip().lower() in want or nm.lower() in want:
                    scoped.add(cid or nm)
            if not scoped:
                continue                      # this model does not surface the column
            new_items, man = drop_columns([{"edoc": edoc}], scoped)
            actions.append({"id": oid, "name": obj.get("name"), "type": "LOGICAL_TABLE",
                            "action": "strip_columns", "new_edoc": new_items[0]["edoc"],
                            "detail": "remove " + ", ".join(sorted(scoped))
                                      + (f"; cascades {len(man.get('formulas') or [])} formula(s)"
                                         if man.get("formulas") else "")})
            # its own dependents now have to be walked too
            for d in (fetch_deps(oid) or []):
                if str(d.get("id")) not in seen:
                    queue.append(d)
            continue

        blocked.append({"id": oid, "name": obj.get("name"),
                        "reason": f"unsupported object kind '{kind}' — resolve it by hand"})

    return actions, blocked


def plan_summary(actions, blocked):
    """One line per object, for the confirmation screen."""
    lines = [f"**{a['name'] or a['id']}** ({a['type'].lower()}) — {a['detail']}" for a in actions]
    lines += [f"**{b['name'] or b['id']}** — CANNOT PROCEED: {b['reason']}" for b in blocked]
    return lines


def dry_run_plan(actions, validate):
    """VALIDATE_ONLY every edited object BEFORE anything is written.

    `validate(edoc) -> (ok, message)`. Returns [] when the whole plan is clean, otherwise a list
    of {"id","name","error"}. A cascade applied halfway leaves the target inconsistent and the
    import still blocked, so the plan is all-or-nothing and this is the gate.
    """
    problems = []
    for a in actions:
        if not a.get("new_edoc"):
            continue                      # a delete has nothing to validate
        try:
            ok, msg = validate(a["new_edoc"])
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            problems.append({"id": a["id"], "name": a.get("name"), "error": msg})
    return problems


def snapshot_plan(actions, fetch_tml, write_file):
    """Save each object's CURRENT TML before it is changed.

    There is no undo for any of this, so the pre-change TML is the only way back: re-import the
    saved file and the object returns. `write_file(name, text)` decides where it lands. Returns
    the list of paths written. A snapshot that cannot be taken is itself a reason to stop, so
    failures are raised rather than swallowed.
    """
    written = []
    for a in actions:
        edoc = fetch_tml(a["id"])
        if not edoc:
            raise RuntimeError(f"could not read {a.get('name') or a['id']} to snapshot it; "
                               "nothing has been changed")
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                       for ch in str(a.get("name") or a["id"]))[:80]
        written.append(write_file(f"{a['id']}__{safe}.tml", edoc))
    return written

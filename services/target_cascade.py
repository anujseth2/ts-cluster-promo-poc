"""Plan the full cascade of changes a dropped column forces on the TARGET.

A column does not go quietly. Anything on the target that references it blocks the import, and
"anything" is a tree: a model uses the column, that model has answers and liveboards, and those
may feed further objects. Deleting the top of the tree to unblock an import is the blunt answer
and takes work nobody asked to lose, so each kind is handled the way that costs least:

    model      strip the column out of it, then walk ITS dependents
    liveboard  remove only the tiles that use the column, leaving the rest of the board,
               OR delete it outright when every tile on it uses the column
    answer     delete it, because an answer IS a single visualisation

One rule sits under the last two: delete the object when EVERY visualisation in it is impacted,
trim it when only some are. An answer is simply the case that always has exactly one.

Planning is separated from doing on purpose. The plan is built first, in full, with no writes:
it can be shown to the operator, dry-run against the cluster, and refused as a whole. A cascade
applied halfway leaves the target inconsistent AND the import still blocked, which is worse than
not starting.

IO is injected (fetch_tml, fetch_deps) so the walk is testable without a cluster.
"""

from services.import_diagnostics import (
    _parse_edoc, classify_import_errors, dependents_using_columns, drop_columns,
    strip_vizzes_from_tml,
)

MODEL_KINDS = ("model", "worksheet")


def _kind_of(doc):
    if not isinstance(doc, dict):
        return "?"
    for k in ("liveboard", "answer", "model", "worksheet", "table"):
        if k in doc:
            return k
    return "?"


def plan_cascade(seeds, column_names, fetch_tml, fetch_deps, skip_ids=(), skip_names=()):
    """Walk the dependency tree and decide what happens to each object.

    seeds:        [{"id", "name", "type"}] objects already known to reference the column
    column_names: every spelling of the dropped column(s) (see scan_names_for_drops)
    fetch_tml:    id -> edoc string, or None when it cannot be read
    fetch_deps:   id -> [{"id", "name", "type"}] dependents of that object
    skip_ids:     objects the promotion itself is updating — never touched here
    skip_names:   the same exclusion by NAME, for objects the walk discovers mid-flight and
                  whose target guid the caller therefore does not know up front. The model
                  being promoted depends on its own table, so it turns up as a dependent of
                  its own drop; stripping or deleting it would undo the promotion itself.

    Returns (actions, blocked).

    actions: [{"id","name","type","action","detail","removed","verify","new_edoc"}] where action
             is one of "strip_columns" | "remove_tiles" | "delete". new_edoc is None for a delete,
             "removed" is the audit record of what the edit took out, and "verify" is what must be
             true on a fresh re-read for the write to count — pass it straight to
             TSClient.apply_tml_verified as keyword arguments.
    blocked: [{"id","name","reason"}] anything that cannot be planned — unreadable, or a
             liveboard where EVERY tile uses the column so there is nothing surgical to do.
             Any entry here means the cascade must not be applied.

    Actions come back in walk order, parents before children. Applying runs the other way —
    see apply_order(). Use plan_tree() when the caller needs the SHAPE of the result rather than
    a flat list.
    """
    return _walk(seeds, column_names, fetch_tml, fetch_deps, skip_ids, skip_names)[:2]


def _walk(seeds, column_names, fetch_tml, fetch_deps, skip_ids=(), skip_names=()):
    """The single walk behind plan_cascade and plan_tree.

    Returns (actions, blocked, reached, no_ref). `reached` is {parent_id: [child_id, ...]} for every
    dependency edge the walk OBSERVED, whether or not the child was new. Recording an edge to an
    already-seen child matters: a child that the operator happened to tick before its parent
    would otherwise look parentless and be offered as a root of its own.

    Children keep the order the platform listed them in. A set here was wrong twice over: the
    plan rendered in a different order on every run, and the confirmation the operator reads
    would not match the one they read a moment earlier.

    `no_ref` holds everything read cleanly whose TML shows NO reference to the column. For an
    object found by recursion that is ordinary and means leave it alone. For one ThoughtSpot
    NAMED as blocking it is a disagreement with the platform, and the caller should say so
    rather than let it vanish from the plan.
    """
    want = {str(c).strip().lower() for c in (column_names or ()) if str(c).strip()}
    skip = {str(i) for i in (skip_ids or ())}
    skipn = {str(n).strip().lower() for n in (skip_names or ()) if str(n).strip()}
    actions, blocked, seen, reached, no_ref = [], [], set(), {}, []
    queue = list(seeds or [])

    while queue:
        obj = queue.pop(0)
        oid = str(obj.get("id") or "")
        if not oid or oid in seen or oid in skip:
            continue
        seen.add(oid)
        if (obj.get("name") or "").strip().lower() in skipn:
            continue

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
            # Check FIRST, like the other two branches do. This used to delete any answer the
            # walk reached, so stripping a model took out every answer hanging off it including
            # ones built on completely different columns. "An answer is a single visualisation"
            # justifies deleting it WHOLE once it is implicated; it never justified implicating
            # it in the first place.
            if not dependents_using_columns(
                    [{"id": oid, "name": obj.get("name"), "type": "ANSWER", "tml": edoc}], want):
                no_ref.append({"id": oid, "name": obj.get("name"), "kind": "answer"})
                continue
            actions.append({"id": oid, "name": obj.get("name"), "type": "ANSWER",
                            "action": "delete", "new_edoc": None, "removed": [], "verify": {},
                            "detail": "deleted — an answer is a single visualisation, so every "
                                      "part of it uses the column"})
            continue

        if kind == "liveboard":
            hits = dependents_using_columns(
                [{"id": oid, "name": obj.get("name"), "type": "LIVEBOARD", "tml": edoc}], want)
            vz = (hits[0].get("vizzes") if hits else []) or []
            total = (hits[0].get("viz_total") if hits else 0) or 0
            if not vz:
                no_ref.append({"id": oid, "name": obj.get("name"), "kind": "liveboard"})
                continue                      # nothing on this board uses the column after all
            if len(vz) >= total:
                # One rule, not two: delete the object when EVERY visualisation in it is
                # impacted, trim it when only some are. Trimming here would leave an empty page
                # rather than a board, and the platform will not even keep a gap to mark why
                # (ps-internal 2026-09-23), so there is nothing left to be proportionate about.
                # An answer is the degenerate case of the same rule, never more than one viz.
                actions.append({"id": oid, "name": obj.get("name"), "type": "LIVEBOARD",
                                "action": "delete", "new_edoc": None, "removed": [], "verify": {},
                                "detail": f"DELETE THE WHOLE BOARD — all {total} tile(s) use the "
                                          "column, so there is nothing left to keep"})
                continue
            ids = [v["id"] for v in vz]
            new_edoc, removed, remaining = strip_vizzes_from_tml(edoc, ids)
            actions.append({"id": oid, "name": obj.get("name"), "type": "LIVEBOARD",
                            "action": "remove_tiles", "new_edoc": new_edoc, "removed": list(ids),
                            # NOT the tile ids: ThoughtSpot renumbers them on import, so the
                            # survivor of a two-tile board comes back wearing the removed one's
                            # id. What is checked is what actually matters and survives that.
                            "verify": {"columns": sorted(want), "viz_count": remaining},
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
                no_ref.append({"id": oid, "name": obj.get("name"), "kind": "model"})
                continue                      # this model does not surface the column
            new_items, man = drop_columns([{"edoc": edoc}], scoped)
            actions.append({"id": oid, "name": obj.get("name"), "type": "LOGICAL_TABLE",
                            "action": "strip_columns", "new_edoc": new_items[0]["edoc"],
                            "removed": sorted(scoped), "verify": {"columns": sorted(scoped)},
                            "detail": "remove " + ", ".join(sorted(scoped))
                                      + (f"; cascades {len(man.get('formulas') or [])} formula(s)"
                                         if man.get("formulas") else "")})
            # its own dependents now have to be walked too
            for d in (fetch_deps(oid) or []):
                did = str(d.get("id") or "")
                if not did:
                    continue
                kids = reached.setdefault(oid, [])
                if did not in kids:
                    kids.append(did)
                if did not in seen:
                    queue.append(d)
            continue

        blocked.append({"id": oid, "name": obj.get("name"),
                        "reason": f"unsupported object kind \'{kind}\' — resolve it by hand"})

    return actions, blocked, reached, no_ref


def plan_summary(actions, blocked):
    """One line per object, for the confirmation screen."""
    lines = [f"**{a['name'] or a['id']}** ({a['type'].lower()}) — {a['detail']}" for a in actions]
    lines += [f"**{b['name'] or b['id']}** — CANNOT PROCEED: {b['reason']}" for b in blocked]
    return lines


def plan_tree(candidates, column_names, fetch_tml, fetch_deps, skip_ids=(), skip_names=()):
    """The same walk, kept in its TREE shape so the operator picks roots rather than rows.

    ThoughtSpot's 14544 message lists every dependent flat, at every level, which reads as a set
    of peers and is not one. A board and an answer hanging off a model are reached BY that model:
    they are its consequences, not separate decisions. Offering all three as independent ticks
    invites a selection that cannot work — tick the model but not its board and stripping the
    model is blocked by the board it left behind — so the subtree is the only granularity that
    can actually succeed, and the list should say so.

    Returns (roots, nodes, blocked, disputed):
      nodes    {id: <action dict> + "children": [id, ...]} for everything the walk decided on
      roots    the node ids that NOTHING else in the walk reached, in candidate order — the rows
               worth ticking. Everything else is shown underneath the root that reaches it.
      blocked  as plan_cascade
      disputed candidates ThoughtSpot NAMED as blocking whose TML shows no reference to the
               column. One side is wrong and it is likelier to be our scan than the platform,
               so these are reported rather than quietly dropped from the plan. An object found
               by RECURSION with no reference is not disputed, just untouched — nobody claimed
               it was blocking.

    A dependency cycle would leave every node with a parent and so produce no roots at all; the
    seeded candidates are used as roots in that case rather than showing an empty list.
    """
    actions, blocked, reached, no_ref = _walk(candidates, column_names, fetch_tml, fetch_deps,
                                              skip_ids, skip_names)
    named = {str(c.get("id")) for c in (candidates or [])}
    disputed = [n for n in no_ref if n["id"] in named]
    nodes = {a["id"]: dict(a, children=[]) for a in actions}
    for parent, kids in reached.items():
        if parent in nodes:
            nodes[parent]["children"] = [k for k in kids if k in nodes]
    has_parent = {k for p, kids in reached.items() if p in nodes
                  for k in kids if k in nodes}
    order = [str(c.get("id")) for c in (candidates or [])]
    rank = {oid: i for i, oid in enumerate(order)}
    roots = sorted((i for i in nodes if i not in has_parent),
                   key=lambda i: rank.get(i, len(rank)))
    if not roots and nodes:
        roots = [i for i in order if i in nodes]
    return roots, nodes, blocked, disputed


def subtree_actions(nodes, selected_ids):
    """Every action under the selected roots, parents before children, deduped.

    Selecting a root means taking everything it reaches, because that is the only selection the
    platform will accept: the parent cannot lose the column while a child still references it.
    """
    out, seen = [], set()
    queue = [str(i) for i in (selected_ids or [])]
    while queue:
        oid = queue.pop(0)
        if oid in seen or oid not in nodes:
            continue
        seen.add(oid)
        node = nodes[oid]
        out.append({k: v for k, v in node.items() if k != "children"})
        queue.extend(node.get("children") or [])
    return out


def tree_lines(roots, nodes, indent="    "):
    """The forest as display lines, each {"id", "depth", "text", "tickable"}.

    Only a root is tickable. A child is shown so the blast radius is visible BEFORE anything is
    ticked, rather than appearing for the first time in the confirmation.
    """
    lines, seen = [], set()

    def _walk_out(oid, depth):
        if oid in seen or oid not in nodes:
            return
        seen.add(oid)
        n = nodes[oid]
        lines.append({"id": oid, "depth": depth, "tickable": depth == 0,
                      "text": f"**{n.get('name') or oid}** ({n['type'].lower()}) — {n['detail']}"})
        for kid in n.get("children") or []:
            _walk_out(kid, depth + 1)

    for r in roots:
        _walk_out(r, 0)
    return lines


def apply_order(actions):
    """Leaves first.

    plan_cascade walks DOWN the tree, so a model appears before the answers and boards hanging
    off it. Writing in that order fails: stripping a column from a model while its own dependents
    still reference that column is exactly the 14544 block this cascade exists to clear. So the
    walk order is reversed to apply — children go first, and each parent is edited only once
    nothing below it still points at the column.
    """
    return list(reversed(list(actions or [])))


def planned_names(actions):
    """Lowercased names of every object the plan changes — what dry_run_plan forgives."""
    return {(a.get("name") or "").strip().lower() for a in (actions or [])
            if (a.get("name") or "").strip()}


def _only_blocked_by(msg, planned):
    """True when the ONLY thing standing in this object's way is something the plan removes."""
    findings = classify_import_errors(
        [{"name": "?", "type": "", "status": "ERROR", "error": msg or ""}])
    named = set()
    for f in findings:
        if f.get("kind") != "drop_blocked_by_dependents":
            return False                  # a different problem entirely; that is a real blocker
        for d in f.get("dependents") or []:
            if str(d).strip():
                named.add(str(d).strip().lower())
    return bool(named) and named <= planned


def dry_run_plan(actions, validate, planned=None):
    """VALIDATE_ONLY every edited object BEFORE anything is written.

    `validate(edoc) -> (ok, message)`. Returns [] when the whole plan is clean, otherwise a list
    of {"id","name","error"}. A cascade applied halfway leaves the target inconsistent and the
    import still blocked, so the plan is all-or-nothing and this is the gate.

    One expected failure is forgiven. A model that strips a column CANNOT validate cleanly while
    its own answers and boards still reference that column — the platform answers 14544 and names
    them. That is the plan working, not a problem, so a failure whose named dependents are ALL
    objects this same plan removes passes. A failure naming anything else, or failing for any
    other reason, is a genuine blocker and stops the whole cascade before a single write.

    `planned` is the set from planned_names(actions); pass it or nothing is forgiven.
    """
    forgive = {str(n).strip().lower() for n in (planned or ()) if str(n).strip()}
    problems = []
    for a in actions:
        if not a.get("new_edoc"):
            continue                      # a delete has nothing to validate
        try:
            ok, msg = validate(a["new_edoc"])
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            continue
        if forgive and _only_blocked_by(msg, forgive):
            continue
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

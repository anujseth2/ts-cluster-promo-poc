"""Joins as things a column can be needed by.

Two questions come up around a dropped column, and both are about joins:

* ThoughtSpot's "Deleted columns have dependents" error lists a model's joins next to real objects,
  by an internal name the TML never shows: `<table>_to_<table>_<suffix>`, one per join (verified on
  ps-internal: `jk_franchise_to_jk_sales_IsUlmmh`). Looked up as an answer, liveboard or table,
  they are never found, so `match_join_names` recognises them from the joins the TML does define.
* A column used in a join condition is a join key. Dropping it removes the whole join (see
  `drop_columns`), which can leave the joined tables with no path to the rest of the model.
  `join_key_impact` says what a set of drops would do before anyone applies it.
"""
import re

from services.import_diagnostics import _parse_edoc, _refs_any


def model_joins(doc) -> list:
    """Every join a TML document defines: {"model", "from", "with", "from_table", "with_table",
    "on", "name"}. `from`/`with` are what the model calls the tables (an alias where it has one);
    `*_table` are the underlying table names, which is what ThoughtSpot builds join names from."""
    doc = doc or {}
    out = []
    for key in ("model", "worksheet"):
        node = doc.get(key)
        if not node:
            continue
        entries = node.get("model_tables") or node.get("tables") or []
        alias_to_table = {(mt.get("alias") or mt.get("name") or ""): (mt.get("name") or "")
                          for mt in entries}
        for mt in entries:
            frm = mt.get("alias") or mt.get("name") or ""
            for j in mt.get("joins") or []:
                w = j.get("with") or ""
                out.append({"model": node.get("name") or "", "from": frm, "with": w,
                            "from_table": mt.get("name") or "",
                            "with_table": alias_to_table.get(w, w),
                            "on": j.get("on") or "", "name": j.get("name") or ""})
        # Older worksheets keep their joins in one list, named, with source and destination.
        for j in node.get("joins") or []:
            src, dst = j.get("source") or "", j.get("destination") or ""
            out.append({"model": node.get("name") or "", "from": src, "with": dst,
                        "from_table": alias_to_table.get(src, src),
                        "with_table": alias_to_table.get(dst, dst),
                        "on": j.get("on") or "", "name": j.get("name") or ""})
    t = doc.get("table")
    if t:
        for j in t.get("joins_with") or []:
            dst = (j.get("destination") or {}).get("name") or ""
            out.append({"model": "", "from": t.get("name") or "", "with": dst,
                        "from_table": t.get("name") or "", "with_table": dst,
                        "on": j.get("on") or "", "name": j.get("name") or ""})
    return out


def _named_like(name: str, j: dict) -> bool:
    key = (name or "").strip().lower()
    if j["name"] and j["name"].strip().lower() == key:
        return True
    a, b = j["from_table"].strip().lower(), j["with_table"].strip().lower()
    if not a or not b:
        return False
    for x, y in ((a, b), (b, a)):   # the generated name doesn't follow the TML's direction
        stem = f"{x}_to_{y}"
        if key == stem or (key.startswith(stem + "_") and key[len(stem) + 1:].isalnum()):
            return True
    return False


def joins_using(joins: list, column: str) -> list:
    """The joins whose condition references this column (on either side, any table)."""
    col = (column or "").strip().lower()
    return [j for j in joins if col and f"::{col}]" in (j.get("on") or "").lower()]


def match_join_names(names, docs, column: str = "") -> dict:
    """{name: [join, ...]} for the names in a dependents list that are joins, not objects.

    Several joins can share a generated stem (two aliases of the same table), and the suffix doesn't
    say which is which, so a name maps to every join it could be. With `column`, joins that use the
    column win when any do."""
    joins, seen = [], set()
    for d in docs or []:
        for j in model_joins(d):
            k = (j["model"], j["from"], j["with"], j["on"])
            if k not in seen:
                seen.add(k)
                joins.append(j)
    out = {}
    for n in names or []:
        hits = [j for j in joins if _named_like(n, j)]
        if column:
            hits = joins_using(hits, column) or hits
        if hits:
            out[n] = hits
    return out


def _components(nodes, edges) -> list:
    adj = {n: set() for n in nodes}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    seen, comps = set(), []
    for n in nodes:
        if n in seen:
            continue
        comp, stack = set(), [n]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack.extend(adj[x] - comp)
        seen |= comp
        comps.append(comp)
    return comps


def join_key_impact(items, drops) -> list:
    """What dropping `drops` ("table::col", or a bare name) does to each model's joins, before it is
    applied: [{"model", "columns": [the drops that are join keys], "joins": [{"from", "with", "on"}],
    "cut_off": [tables left with no join to the rest of the model], "lost_columns": [model columns
    those tables carry]}]. Uses the same reference test as drop_columns, so it names exactly the
    joins a drop would remove."""
    targets, qual = set(), set()
    for c in drops or []:
        cl = (c or "").strip().lower()
        if "::" in cl:
            t, col = cl.split("::", 1)
            qual.add((t.strip(), col.strip()))
        elif cl:
            targets.add(cl)
    if not (targets or qual):
        return []
    out = []
    for it in items or []:
        doc = _parse_edoc(it) or {}
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            entries = node.get("model_tables") or node.get("tables") or []
            ids = [mt.get("alias") or mt.get("name") or "" for mt in entries]
            all_edges, kept_edges, gone, keys = [], [], [], set()
            for mt in entries:
                frm = mt.get("alias") or mt.get("name") or ""
                for j in mt.get("joins") or []:
                    edge = (frm, j.get("with") or "")
                    all_edges.append(edge)
                    if _refs_any(j, targets, qual):
                        gone.append({"from": edge[0], "with": edge[1], "on": j.get("on") or ""})
                        keys |= {_as_written(c, j.get("on") or "") for c in drops
                                 if _refs_any(j, *_split_one(c))}
                    else:
                        kept_edges.append(edge)
            if not gone:
                continue
            before = _components(ids, all_edges)
            after = _components(ids, kept_edges)
            main = max(after, key=len) if after else set()
            cut = sorted(n for n in ids if n not in main
                         and any(n in comp and comp & main for comp in before))
            lost = [c.get("name") or "" for c in node.get("columns") or []
                    if (c.get("column_id") or "").split("::")[0] in cut]
            table_of = {(mt.get("alias") or mt.get("name") or ""): (mt.get("name") or "")
                        for mt in entries}
            out.append({"model": node.get("name") or "", "columns": sorted(keys), "joins": gone,
                        "cut_off": cut, "lost_columns": [x for x in lost if x],
                        "cut_off_tables": sorted({table_of.get(a, a) for a in cut})})
    return out


_REF = re.compile(r"\[([^\]:]+)::([^\]]+)\]")


def _as_written(drop: str, on: str) -> str:
    """The drop as the join condition spells it ("table::Col"), since drop keys can be lowercased."""
    t, _, c = (drop or "").partition("::")
    want = (t.strip().lower(), c.strip().lower()) if c else (None, t.strip().lower())
    for tbl, col in _REF.findall(on or ""):
        if col.strip().lower() == want[1] and want[0] in (None, tbl.strip().lower()):
            return f"{tbl.strip()}::{col.strip()}"
    return drop


def _split_one(c):
    cl = (c or "").strip().lower()
    if "::" in cl:
        t, col = cl.split("::", 1)
        return set(), {(t.strip(), col.strip())}
    return {cl}, set()


def describe_join_block(column: str, model: str, names: list, joins: list,
                        promoted, dropped_here: bool) -> str:
    """Explain a 14544 whose dependents include a model's joins. `promoted`: None when the model
    isn't in this promotion, else whether the promoted version still joins on the column."""
    nm = " and ".join(f"**`{n}`**" for n in names)
    js = " and ".join(f"`{j['from']}` → `{j['with']}`" for j in joins)
    where = f" in **{model}**" if model else ""
    s = (f"{nm} {'are joins' if len(names) > 1 else 'is a join'}{where}, not separate objects: "
         f"{js} join on `{column}`. Removing `{column}` would take "
         f"{'these joins' if len(joins) > 1 else 'this join'} with it, so ThoughtSpot refuses.")
    if promoted is None and model:
        s += f" **{model}** isn't in this promotion, so its joins on the target stay as they are."
    elif promoted is False:
        s += (f" The promoted **{model}** no longer joins on it, but the version on the target "
              "still does.")
    elif promoted:
        s += f" The promoted **{model}** still joins on it too, so the column has to stay."
    if dropped_here:
        s += (" This run is dropping the column: keep it instead (recase it, realign it, or add it "
              "to the warehouse) unless the model really should lose these joins.")
    elif promoted is None and model:
        s += (" The promoted table no longer has the column. Either it goes back into the source "
              f"table, or **{model}** loses these joins on the target: tick it under **What the "
              "target holds** to see everything that removes before deciding.")
    else:
        s += (" The promoted table no longer has the column. Either it goes back into the source "
              "table, or whoever owns the model on the target changes these joins first.")
    return s


def describe_impact(impact: list) -> list:
    """One plain sentence per model, for showing beside a drop before it is applied."""
    lines = []
    for m in impact or []:
        cols = ", ".join(f"`{c.replace('::', '.')}`" for c in m["columns"]) or "this column"
        js = ", ".join(f"`{j['from']}` → `{j['with']}`" for j in m["joins"])
        s = (f"{cols} is a join key in **{m['model']}**: dropping it removes "
             f"{len(m['joins'])} join(s) ({js})")
        if m["cut_off"]:
            s += (", which leaves " + ", ".join(f"`{t}`" for t in m["cut_off"])
                  + " with no join to the rest of the model")
            if m["lost_columns"]:
                s += (", along with the model columns that come from them: "
                      + ", ".join(f"`{c}`" for c in m["lost_columns"][:8])
                      + (", …" if len(m["lost_columns"]) > 8 else ""))
        lines.append(s + ".")
    return lines

"""
Post-import reconciliation — VERIFY the promotion's claims against the live target instead of
inferring them from a pre-import snapshot.

The report's created / updated / DUPLICATE label is inferred from (pre-import snapshot guid vs
returned guid); that inference is fragile (e.g. a Replace-rebuilt model looks like a duplicate).
This re-queries the target after import and derives the TRUTH from what is actually there, so the
report self-corrects and surfaces allied mismatches: import said OK but the object is missing;
feedback said synced but its entries are absent; a "duplicate" that is really a single in-place
object; a real duplicate (two same-named objects).

Reused by app.py Step 4. Cluster-verified on ps-internal.
"""
from typing import Dict, List

_TYPE_META = {"Table": "LOGICAL_TABLE", "Model": "LOGICAL_TABLE",
              "Liveboard": "LIVEBOARD", "Answer": "ANSWER"}


def _search(client, mtype: str) -> List[Dict]:
    d = client._post("/api/rest/2.0/metadata/search",
                     {"metadata": [{"type": mtype}], "record_size": 5000})
    rows = d if isinstance(d, list) else d.get("metadata", [])
    return [{"name": o.get("metadata_name"), "id": o.get("metadata_id"),
             "obj_id": o.get("metadata_obj_id")} for o in rows]


def reconcile(target_client, promoted: List[Dict],
              expected_feedback: Dict[str, List[str]] = None) -> List[Dict]:
    """
    promoted: [{name, obj_id, type}] with type in Table/Model/Liveboard/Answer.
    expected_feedback: {model_name: [phrases]} — the feedback we promoted (to confirm it landed).
    Returns [{object, type, verified, ok}] where `verified` is the reality-derived state and `ok`
    is whether it matches a healthy expectation (single present / feedback all present).
    """
    expected_feedback = expected_feedback or {}
    types = {_TYPE_META.get(p["type"], "LOGICAL_TABLE") for p in promoted}
    rows: List[Dict] = []
    for t in types:
        rows += _search(target_client, t)

    by_name: Dict[str, List[Dict]] = {}
    by_obj:  Dict[str, List[Dict]] = {}
    for r in rows:
        if r["name"]:
            by_name.setdefault(r["name"], []).append(r)
        if r["obj_id"]:
            by_obj.setdefault(r["obj_id"], []).append(r)

    out: List[Dict] = []
    for p in promoted:
        name, oid = p["name"], p.get("obj_id")
        same_name    = by_name.get(name, [])
        by_this_obj  = by_obj.get(oid, []) if oid else []
        if not by_this_obj and not same_name:
            verified, ok = "MISSING (not found on target)", False
        elif len(same_name) >= 2:
            verified, ok = f"DUPLICATE — {len(same_name)} objects named '{name}'", False
        elif oid and len(by_this_obj) == 1:
            verified, ok = "present in place (obj_id matches)", True
        else:
            verified, ok = "present", True
        out.append({"object": name, "type": p["type"], "verified": verified, "ok": ok})

    for model_name, phrases in expected_feedback.items():
        m = by_name.get(model_name, [])
        if not m:
            out.append({"object": model_name, "type": "Feedback",
                        "verified": "model not found — cannot confirm feedback", "ok": False})
            continue
        actual = {e.get("feedback_phrase") for e in target_client.export_feedback_entries(m[0]["id"])}
        missing = [p for p in phrases if p not in actual]
        out.append({"object": model_name, "type": "Feedback",
                    "verified": (f"all {len(phrases)} present" if not missing
                                 else f"MISSING: {', '.join(missing)}"),
                    "ok": not missing})
    return out

"""
Promote Spotter NL (natural-language) instructions — model-level coaching text that guides how
Spotter interprets queries. This is a SEPARATE artifact from feedback and is NOT carried in TML;
it lives only behind ai/instructions get/set (Beta 10.15.0.cl+).

`set` is a FULL REPLACE, so:
  * Merge   = union(source, target)  — add the source's instructions, keep the target's own.
  * Replace = source only            — target ends with exactly the source's instructions.

Scope: only GLOBAL exists today (the API enum is GLOBAL-only; data-model-user scope is a
documented future extension). We promote GLOBAL only, and — because `set` is a full replace of
the whole model — we read the target's blocks and pass any non-GLOBAL block back unchanged so a
future user-scoped block is never clobbered. We deliberately do NOT promote the source's own
non-GLOBAL blocks.
  TODO(user-scope): when data-model-user scope ships, offer promoting it as an explicit opt-in
  choice (default off), carried over with the same scope — do not fold it into GLOBAL.

Instructions are plain strings (deduped exactly). Verified inter-org on ps-internal.
"""
from typing import Dict, List


def preview(source_client, target_client, source_model_guid: str, target_obj_id: str,
            source_instructions=None) -> Dict:
    """Diff source vs target NL instructions for one model. If source_instructions is provided
    (operator-edited on the select page), it is used verbatim instead of re-fetching the source."""
    src = list(source_instructions) if source_instructions is not None \
        else source_client.get_nl_instructions(source_model_guid)
    tgt_guid = target_client.find_by_obj_id(target_obj_id)
    tgt = target_client.get_nl_instructions(tgt_guid) if tgt_guid else []
    return {
        "source":         src,
        "target":         tgt,
        "target_present": bool(tgt_guid),
        "add":            [i for i in src if i not in tgt],       # source-only (added on either mode)
        "shared":         [i for i in src if i in tgt],           # already identical
        "target_only":    [i for i in tgt if i not in src],       # kept on Merge, dropped on Replace
    }


def promote(source_client, target_client, models: List[Dict], mode: str = "merge",
            source_map=None) -> List[Dict]:
    """models: [{name, obj_id, source_guid}]. mode: 'merge' (union) | 'replace' (source only).
    source_map: optional {source_guid: [instructions]} of operator-edited instructions to promote
    instead of the source's current ones. Promotes GLOBAL instructions only; preserves any
    non-GLOBAL target block unchanged. Returns a per-model report."""
    report = []
    for m in models:
        if source_map is not None and m["source_guid"] in source_map:
            src = list(source_map[m["source_guid"]])                       # operator-edited
        else:
            src = source_client.get_nl_instructions(m["source_guid"])      # GLOBAL only
        tgt_guid = target_client.find_by_obj_id(m["obj_id"])
        if not tgt_guid:
            report.append({"model": m["name"], "status": "target model not found",
                           "added": [], "kept": [], "dropped": [], "count": 0})
            continue
        # Split the target's blocks: GLOBAL is what we edit; anything else is passed back as-is.
        tgt: List[str] = []
        other_blocks: List[Dict] = []
        for b in target_client.get_nl_instruction_blocks(tgt_guid):
            if (b.get("scope") or "GLOBAL") == "GLOBAL":
                tgt.extend(b.get("instructions") or [])
            else:
                other_blocks.append(b)                                     # preserve user-/other-scope
        if not src and mode != "replace":
            # nothing to promote and not replacing -> leave the target untouched
            report.append({"model": m["name"], "status": "no source instructions",
                           "added": [], "kept": tgt, "dropped": [], "count": len(tgt)})
            continue
        if mode == "replace":
            final = list(src)
        else:  # merge: source first, then target-only GLOBAL (dedup exact)
            final = list(src) + [i for i in tgt if i not in src]
        blocks = ([{"instructions": final, "scope": "GLOBAL"}] if final else []) + other_blocks
        ok = target_client.set_nl_instruction_blocks(tgt_guid, blocks)
        report.append({"model": m["name"], "status": "ok" if ok else "failed",
                       "added": [i for i in src if i not in tgt],
                       "kept":  [i for i in tgt if i not in src] if mode != "replace" else [],
                       "dropped": [i for i in tgt if i not in src] if mode == "replace" else [],
                       "count": len(final)})
    return report

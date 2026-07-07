"""
Spotter feedback: merge-preview + optional Replace.

The platform has NO API to delete/clear feedback entries (verified live: metadata/delete
rejects FEEDBACK; no ai/feedback endpoint; an empty-array import does not clear). Feedback
import only MERGES (add + replace-by-phrase; target-only entries are kept). So the default is
a safe merge, and this module adds:

  * feedback_preview  — diff source vs target feedback by (type, phrase): add / replace / keep.
  * Replace (opt-in)  — make the target end with ONLY the source's feedback, by REBUILDING the
    model: rename the target model's obj_id to free it, let the normal import create a fresh
    model carrying the aligned obj_id + source feedback (clean), re-point the old model's REAL
    (non-feedback) dependents onto the fresh model, then delete the old model IFF it has no real
    dependents left. Verified inter-org on ps-internal 2026-07-07.

Replace is a heavy, destructive rebuild — the app gates it behind an explicit acknowledgment.
"""
from typing import Dict, List

_REPLACED_SUFFIX = "__replaced"


def _key(e: dict):
    return (e.get("type", ""), (e.get("feedback_phrase") or "").strip())


def feedback_preview(target_client, model_name: str, model_obj_id: str,
                     source_entries: List[dict]) -> Dict:
    """Diff source feedback vs the target model's current feedback, keyed by (type, phrase).
    keep = target-only entries (preserved on Merge, dropped on Replace)."""
    guid = target_client.find_by_obj_id(model_obj_id)
    tgt_entries = target_client.export_feedback_entries(guid) if guid else []

    def _tok(e):
        return (e.get("search_tokens") or "").strip()
    src_tok = {_key(e): _tok(e) for e in source_entries}   # (type,phrase) -> columns it maps to
    tgt_tok = {_key(e): _tok(e) for e in tgt_entries}
    src, tgt = set(src_tok), set(tgt_tok)

    def _label(t, p):
        kind = "biz term" if t == "BUSINESS_TERM" else ("ref Q" if t == "REFERENCE_QUESTION" else t.lower())
        return f"{p} ({kind})"

    def _grouped(pairs, tokmap):
        # {label: [{phrase, tokens}]} — tokens drives the "?" tooltip in the picker/preview.
        out = {"Reference questions": [], "Business terms": [], "Other": []}
        for (t, p) in sorted(pairs):
            item = {"phrase": p if t in ("REFERENCE_QUESTION", "BUSINESS_TERM") else f"{p} ({t})",
                    "tokens": tokmap.get((t, p), "")}
            key = ("Reference questions" if t == "REFERENCE_QUESTION"
                   else "Business terms" if t == "BUSINESS_TERM" else "Other")
            out[key].append(item)
        return out

    return {
        "model":          model_name,
        "target_present": bool(guid),
        "target_guid":    guid,
        "source":  sorted(_label(t, p) for (t, p) in src),   # everything on the source
        "target":  sorted(_label(t, p) for (t, p) in tgt),   # everything on the target now
        "source_grouped": _grouped(src, src_tok),   # source entries grouped by kind (for the dropdown)
        "target_grouped": _grouped(tgt, tgt_tok),   # target entries grouped by kind
        "add":     sorted(_label(t, p) for (t, p) in src if (t, p) not in tgt),
        "replace": sorted(_label(t, p) for (t, p) in src if (t, p) in tgt),
        "keep":    sorted(_label(t, p) for (t, p) in tgt if (t, p) not in src),
    }


def replace_prep(target_client, models: List[Dict]) -> List[Dict]:
    """BEFORE import: for each promoted model {name, obj_id} that already exists on the target,
    capture its real (non-feedback) dependents and rename its obj_id to free it, so the import
    creates a FRESH model with the aligned obj_id. Returns [{name, obj_id, old_guid, real_deps}].
    Models absent on the target (first promotion) are skipped — normal create already gives a
    clean feedback set."""
    prepped = []
    for m in models:
        obj_id = m["obj_id"]
        guid = target_client.find_by_obj_id(obj_id)
        if not guid:
            continue
        real_deps = target_client.real_dependents(guid)
        target_client.update_obj_ids(
            [{"identifier": guid, "new_obj_id": obj_id + _REPLACED_SUFFIX}])
        prepped.append({"name": m["name"], "obj_id": obj_id,
                        "old_guid": guid, "real_deps": real_deps})
    return prepped


def replace_finalize(target_client, prepped: List[Dict]) -> List[Dict]:
    """AFTER import (the fresh model + source feedback now hold the aligned obj_id): re-point the
    old model's real dependents onto the fresh model, then delete the old model IFF no real
    (non-feedback) dependents remain. Returns a per-model report."""
    report = []
    for p in prepped:
        obj_id, old_guid = p["obj_id"], p["old_guid"]
        new_guid = target_client.find_by_obj_id(obj_id)          # freshly-imported model
        new_name = _name_of(target_client, new_guid) if new_guid else p["name"]
        repointed, failed = [], []
        for d in p["real_deps"]:
            r = target_client.repoint_dependent(
                d.get("id"), obj_id + _REPLACED_SUFFIX, obj_id, new_name)
            (repointed if r.get("status") == "OK" else failed).append(r.get("name") or d.get("name"))
        remaining = target_client.real_dependents(old_guid)
        deleted = False
        if not remaining:
            deleted = target_client.delete_metadata("LOGICAL_TABLE", old_guid) in (200, 204)
        report.append({
            "model":             p["name"],
            "repointed":         repointed,
            "failed":            failed,
            "old_model_deleted": deleted,
            "kept_deps":         [x.get("name") for x in remaining],
        })
    return report


def _name_of(client, guid: str) -> str:
    data = client._post("/api/rest/2.0/metadata/search",
                        {"metadata": [{"type": "LOGICAL_TABLE", "identifier": guid}],
                         "record_size": 5})
    rows = data if isinstance(data, list) else data.get("metadata", [])
    for o in rows:
        if o.get("metadata_id") == guid:
            return o.get("metadata_name")
    return guid

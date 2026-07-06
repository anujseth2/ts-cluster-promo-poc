# Cross-Cluster Promotion — Design Map

The one place to reason about this tool. It exists because the tool is a **pipeline**
applied across **object types** under several **scenarios**, and almost every bug is a
cell in that grid that was handled for one type but not the others. Check the whole
column before calling any change done.

## Core invariants (the constitution)

1. **`obj_id` is identity.** Import matches each object `obj_id → guid → else create`.
   A wrong/absent match creates a **duplicate**, which imports as a clean "Succeeded".
2. **Auto `obj_id` (`Name-<guidprefix>`) never matches cross-cluster** — it embeds the
   source cluster's guid. So "has an obj_id" is not "aligned". Set explicit obj_ids.
3. **In-place update on the target requires the target object to already carry the same
   `obj_id` as the source** — for *every* type that can pre-exist (table, model,
   liveboard, answer). First promotion creates with the source obj_id; re-promotion then
   updates in place. A pre-existing target object with a different obj_id must be aligned
   first (Step 2 → "Fix target obj_ids") or it duplicates.
4. **Reference resolution is split by layer.** Liveboard/answer → model refs carry the
   model's `obj_id`. Model → table refs are **by name** (rewrite the name everywhere on a
   rename: `model_tables[].name`, joins `[table::col]`, formulas). Object identity is
   still `obj_id`.
5. **`db_table` is load-bearing** (physical binding). The **warehouse is the type
   authority** — a Table TML cannot override a column's warehouse type (cross-family
   change hard-fails; DATE↔DATE_TIME warns).
6. **Import order:** tables → models → feedback → liveboards/answers.
7. **FEEDBACK is exported by the model's guid** (from `info.id`, not the edoc), and is
   **not independently searchable** (`metadata/search type=FEEDBACK` → HTTP 400).

## Coverage matrix — object type × pipeline stage

Legend: ✓ handled · ✗ gap · ⚠ handled but unverified live · — n/a

| Type       | Resolve | Src obj_id (Step 2A) | Tgt align (Step 2B) | Export            | Transform                         | Commit path   | Import order | Report status            |
|------------|---------|----------------------|---------------------|-------------------|-----------------------------------|---------------|--------------|--------------------------|
| Table      | ✓ dep   | ✓                    | ✓ create/update/dup | ✓ export_tml      | conn/db/schema remap · matcher repoint | `tables/`     | 1st          | ✓ created/updated/DUP    |
| Model      | ✓       | ✓                    | ✓ create/update/dup | ✓ export_tml      | strip fqn · conn/db/schema remap  | `models/`     | 2nd          | ✓ created/updated/DUP    |
| Liveboard  | ✓ leaf  | ✓                    | ⚠ create/update/dup | ✓ export_tml      | strip model-ref fqn (obj_id resolve) | `liveboards/` | last      | ✓ created/updated/DUP    |
| Answer     | ✓ leaf  | ✓                    | ⚠ create/update/dup | ✓ export_tml      | strip model-ref fqn (obj_id resolve) | `answers/`    | last      | ✓ created/updated/DUP    |
| Feedback   | via model toggle (granular pick) | — auto, preserved | ✗ **not searchable → target obj_id can't be aligned** | ✓ export_feedback(model guid) | keep obj_id · strip guid · no data remap | `feedback/`   | after model  | "synced" only (**no dup detection**) |
| Connection | — (referenced, not promoted) | — | — | — | remap by name; warn if target lacks it | — | — | — |

### Open cells (the current "missing pointers")

- **Feedback duplicate on re-promotion — UNVERIFIED.** Because FEEDBACK isn't searchable,
  Step 2B can't align its target obj_id like the other types. It relies on the shared
  model guid + preserved obj_id to match in place. Whether a second promotion updates it
  in place or creates a duplicate is **not yet confirmed on-cluster**. The report also
  can't flag a feedback duplicate (nothing to search). → verify on the VM (smoke test T6).
- **`update-obj-id` on liveboards/answers — UNVERIFIED.** Proven for tables/models; leaves
  carry a top-level obj_id so it should work, but a live run hasn't confirmed it. Failure
  surfaces as the red "Failed to set obj_id…" error, not silently.

## Scenarios (smoke tests to run before "done")

- **T1 First promote** — target empty → every object **created**, 0 duplicates.
- **T2 Re-promote unchanged** — every object **updated in place**, **0 duplicates**. The
  core regression guard; a duplicate here means an obj_id alignment gap.
- **T3 Pre-existing target object, mismatched obj_id** — Step 2B shows **mismatch** →
  "Fix target obj_ids" → re-run → **updated in place**. (Covers a manual copy on target.)
- **T4 Column divergence** — source-extra (14536 hard-fail → drop/add), target-extra with
  dependents (blocked), target-extra silent (pre-diff + ack), type drift (align warehouse).
- **T5 Prune** — a not-on-target table excluded → pruned from the model with the cascade
  shown; the report lists it under "dropped / pruned".
- **T6 Feedback** — toggle on, pick a granular subset → lands as `N ref Q / M biz terms`;
  **re-promote → confirm in place vs duplicate** (open cell above).

## Rule of thumb

When you touch any stage, run down its **column** for all six types. When you hit a bug,
find its **cell** — the fix usually belongs to the whole column, not just the row you saw.

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
| Liveboard  | ✓ leaf  | ✓                    | ✓ create/update/dup | ✓ export_tml      | strip model-ref fqn (obj_id resolve) | `liveboards/` | last      | ✓ created/updated/DUP    |
| Answer     | ✓ leaf  | ✓                    | ✓ create/update/dup | ✓ export_tml      | strip model-ref fqn (obj_id resolve) | `answers/`    | last      | ✓ created/updated/DUP    |
| Feedback (ref Q + biz terms) | via model toggle (granular pick) | — obj_id = model obj_id, preserved | ✓ matches target model by obj_id (needs Spotter-enabled model, committed first) | ✓ export_feedback per-model, 400-tolerant | keep obj_id · strip guid · no data remap | `feedback/` | **SEPARATE call AFTER model commits** | N ref Q / M biz terms · "synced" |
| NL instructions (Spotter coaching) | (not yet wired) | — | — | ✗ **not in TML — `ai/instructions/get`** | n/a (plain text) | not in git bundle | `ai/instructions/set` (full replace) | — | ✗ **not built** |
| Connection | — (referenced, not promoted) | — | — | — | remap by name; warn if target lacks it | — | — | — |

### Verified live on ps-internal (2026-07-07)

- **Leaf → model reference carries `obj_id`** ✓ — exported liveboards show `obj_id` on every
  viz's model ref (and NO `fqn` on this cluster version). So the fqn-strip is safe: obj_id is
  always there to resolve by; the strip is defensive for clusters that do emit an fqn.
- **`update-obj-id` on a leaf WORKS** ✓ — set/read-back/restore on an anuj-owned liveboard
  returned HTTP 204. Caveat: it needs **edit access** to the object — a system-owned
  liveboard returns HTTP 400 `AUTHORIZATION_FAILURE` (same edit-access rule as tables/models).
- **Feedback promotion end-to-end** ✓ — full inter-org run (AnujSeth → Anuj Git Dev, both on
  the same Databricks under different connection names). Findings:
  - Target model must be **Spotter-enabled** or feedback ops fail `14531`; the tool carries
    `model.properties.spotter_config.is_spotter_enabled` through, so it lands enabled.
  - Feedback resolves the target model by **obj_id** (feedback obj_id == model obj_id), so the
    strip-guid/keep-obj_id transform is correct once the model's obj_id is aligned.
  - Import is **additive + phrase-dedup** (platform-side): same-phrase entries replace, others
    are kept — so re-promote does **not** duplicate, and target-only feedback survives.
  - **MUST import feedback in a SEPARATE call after tables+models commit.** A first-time
    model+feedback in ONE batch fails `14500` (feedback can't resolve the not-yet-committed
    model); under ALL_OR_NONE that rolls the model back too. Fixed: `core` = tables+models,
    feedback imported after.
  - Feedback is **capture-and-replay only** — editing an entry's tokens breaks its
    system-managed `nl_context` (`EDOC_FEEDBACK_TML_INVALID`). Export → import; never author.
  - `type=FEEDBACK` export returns HTTP 400 for a model with **no** feedback; `export_feedback`
    is per-model + 400-tolerant so that no longer aborts the run.
- **Feedback merge vs Replace** ✓ — import is additive/phrase-dedup, so default **Merge** keeps
  the target's own feedback. There is **no API to delete feedback** (metadata/delete rejects
  FEEDBACK; no ai/feedback endpoint; empty-array import doesn't clear). So opt-in **Replace**
  (target ends with only source's feedback) is done by **rebuilding the model**: rename the old
  model's obj_id to free it → normal import creates a fresh model + source feedback → re-point
  the old model's REAL dependents (exclude `type=FEEDBACK`) onto the fresh model → delete the old
  model iff no real deps remain (else keep + flag). Verified inter-org; behind an explicit ack.
  `services/feedback_replace.py` + `ts_client` primitives (`find_by_obj_id`, `real_dependents`,
  `repoint_dependent`, `delete_metadata`, `export_feedback_entries`).
- **obj_id mechanics** ✓ — dependencies are **GUID-bound**; obj_id is a portable label.
  Changing a model's OWN obj_id in place is safe: dependents auto-resolve to the new obj_id, no
  edits (verified). MOVING an obj_id to a different model does NOT move dependents — re-import
  each dependent (model ref → new obj_id) to shift them (verified). `update-obj-id` can move an
  obj_id between objects (free, then assign).

### Open cells (the "missing pointers")

- **NL instructions (Spotter coaching) NOT promoted — gap.** Model-level coaching text is a
  distinct object, **not in TML**, managed via `ai/instructions/get` + `set` (Beta, 10.15.0.cl+;
  `CAN_USE_SPOTTER` + edit/`SPOTTER_COACHING_PRIVILEGE`; org-scoped token). Recipe **verified**
  (get source → set target round-trips). `set` is a **full replace** (no auto-merge, unlike
  feedback), so a merge = get target + union + set. Not yet wired into the tool.
- ~~Feedback merge/replace preview~~ — **BUILT** (Step-2 gate: "add X / replace Y / keep Z
  target-only" + Merge/Replace radio; Replace rebuilds the model per above). Service-layer
  verified inter-org; Streamlit UI wiring pending a live VM run.

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

# Cross-Cluster Promotion Tool — Architecture

A technical walkthrough of what the tool does, how data flows through it, and exactly
which API calls fire at each stage. Diagrams render inline on GitHub.

> **Audience:** engineers extending the tool and stakeholders who need the shape of the
> flow. Each diagram opens with a plain-English summary; an *Under the hood* note names the
> real functions/endpoints for anyone reading the code alongside it.

---

## Contents

1. [What the tool is](#1-what-the-tool-is)
2. [System context](#2-system-context)
3. [The four-stage pipeline](#3-the-four-stage-pipeline)
4. [Stage 0 — Select](#4-stage-0--select)
5. [Stage 1 — Align (obj_id identity)](#5-stage-1--align-obj_id-identity)
6. [Stage 2 — Git Operations](#6-stage-2--git-operations)
7. [Deep dive: the discover / validate / drop engine](#7-deep-dive-the-discover--validate--drop-engine)
8. [Deep dive: the cascade drop](#8-deep-dive-the-cascade-drop)
9. [The Merge & Import choreography](#9-the-merge--import-choreography)
10. [Stage 3 — Import Results](#10-stage-3--import-results)
11. [Error taxonomy](#11-error-taxonomy)
12. [REST API reference](#12-rest-api-reference)
13. [Key design decisions](#13-key-design-decisions)

---

## 1. What the tool is

A Streamlit app that promotes ThoughtSpot content (liveboards, answers, models, tables,
Spotter feedback, and NL instructions) **from a source cluster to a target cluster** using
**Git as the transport bridge** and **`obj_id` as the stable cross-cluster identity**.

The core problem it solves: a naïve TML import duplicates objects and breaks
warehouse bindings. This tool instead:

- **aligns identity** so the target object is *updated in place*, not duplicated (`obj_id`),
- **rebinds the data layer** to the target connection / database / schema / warehouse casing,
- **discovers every import blocker up front** (missing warehouse columns, type drift, broken
  formulas, blocked drops) instead of failing one error at a time,
- **routes each blocker to a reviewer-resolvable action** (drop a column, drop a viz, skip an
  object) with the full dependency blast-radius cascaded automatically.

**Three actors, two clusters, one bridge:**

- **Source cluster** — where content is authored (`source_client()`).
- **GitHub bridge repo** — a `dev → main` PR is the promotion transaction (`git_client()`).
- **Target cluster** — where content lands, plus its **CDW / warehouse** which is the
  authoritative source of truth for what columns actually exist (`target_client()`).

---

## 2. System context

Everything the tool talks to, and over which protocol.

```mermaid
flowchart LR
    User(["Reviewer / operator"])

    subgraph App["Streamlit app  ·  app.py"]
        UI["4-stage wizard UI"]
        SC["source_client()<br/>TSClient"]
        TC["target_client()<br/>TSClient"]
        GC["git_client()<br/>GitClient"]
    end

    subgraph Source["SOURCE ThoughtSpot cluster"]
        SRest["REST v2 API"]
    end

    subgraph Bridge["GitHub bridge repo"]
        Dev["dev branch"]
        Main["main branch"]
    end

    subgraph Target["TARGET ThoughtSpot cluster"]
        TRest["REST v2 API"]
        CDW[("Target CDW /<br/>warehouse<br/>(Databricks, Snowflake…)")]
    end

    User --> UI
    UI --> SC & TC & GC
    SC -->|"export TML, read obj_ids,<br/>read feedback / NL"| SRest
    GC -->|"commit → PR → squash-merge<br/>(PyGithub)"| Dev
    Dev --> Main
    GC -->|"read merged TML files"| Main
    TC -->|"VALIDATE_ONLY, import,<br/>set obj_ids, delete, NL"| TRest
    TC -.->|"connection/search COLUMN<br/>(authoritative column read)"| CDW
    TRest --- CDW
```

**Under the hood.** One `TSClient` == one cluster (`services/ts_client.py`); the app builds
two. Auth is either an org-scoped **session login** (`auth/session/login` with `org_identifier`)
or a **Bearer token**. The `GitClient` (`services/git_client.py`) wraps PyGithub against
`GITHUB_REPO`. The target's warehouse is reached *through* ThoughtSpot's stored connection
credential — the tool never holds a warehouse secret.

---

## 3. The four-stage pipeline

The wizard is a linear stage machine. Each stage's gate must pass before the next unlocks.

```mermaid
flowchart TD
    subgraph S0["Stage 0 · SELECT"]
        A0["Search source by tag/type"] --> B0["Pick leaves / models / tables"]
        B0 --> C0["Resolve full dependency stack<br/>leaves → models → tables"]
    end
    subgraph S1["Stage 1 · ALIGN"]
        A1["obj_id health check<br/>(source & target)"] --> B1["Set / align obj_ids"]
        B1 --> C1["Table matcher<br/>(renamed / drifted pairs)"]
    end
    subgraph S2["Stage 2 · GIT OPERATIONS"]
        A2["Export + data-layer transform"] --> B2["Discover ALL issues (probe)"]
        B2 --> C2["Resolve findings<br/>(drop cols / vizzes / skip)"]
        C2 --> D2["Merge PR & Import to target"]
    end
    subgraph S3["Stage 3 · RESULTS"]
        A3["Created / Updated / Duplicate report"] --> B3["Reconcile vs target"]
    end

    C0 ==> A1
    C1 ==> A2
    D2 ==> A3

    B2 -. "re-probe after each resolution" .-> C2
    C2 -. "loops until clean" .-> B2
```

| Stage | Page title | Purpose | Primary calls |
|------|------------|---------|---------------|
| 0 | *Source-cluster assets* | Choose what to promote + resolve its dependency stack | `search_by_tags`, `resolve_promotion` |
| 1 | *obj_id Health Check* | Make target objects share identity with source (update-in-place, not duplicate) | `search_obj_ids`, `update_obj_ids`, `match_tables` |
| 2 | *Git Operations* | Export, transform to target data layer, discover+resolve every blocker, merge & import | `export_tml`, `import_tml` (VALIDATE_ONLY), `commit_tml`/`create_pr`/`merge_pr`, `import_tml` |
| 3 | *Import Results* | Report what was created vs updated-in-place vs duplicated; reconcile | `metadata/search` snapshots |

---

## 4. Stage 0 — Select

**What happens:** the operator searches the source cluster for taggable leaf objects
(liveboards/answers), picks any mix of leaves, models, and tables, and the tool walks the
full dependency graph downward so the *entire promotable stack* is assembled.

```mermaid
sequenceDiagram
    autonumber
    participant U as Reviewer
    participant App
    participant Src as Source cluster

    U->>App: enter tag(s) + type filter
    App->>Src: metadata/search (by tag, paged 200)
    Src-->>App: leaf rows (liveboards, answers)
    U->>App: pick leaves / models / tables

    Note over App,Src: resolve_promotion — walk the stack downward
    App->>Src: export_tml(leaf_ids)
    Src-->>App: leaf TML → extract model refs
    App->>Src: metadata/search (resolve model names → ids)
    App->>Src: export_tml(model_ids)
    Src-->>App: model TML → extract table refs
    App->>Src: metadata/search (resolve table names → ids)
    App-->>U: full set (leaves + models + tables)<br/>+ missing_models / missing_tables warnings
```

**Under the hood.** `search_by_tags` returns leaf types by default; empty tags ⇒ all
accessible leaves (so untaggable assets can still be picked). `resolve_promotion` accepts
mixed roots — a bare liveboard pulls its whole stack, a bare model pulls its tables, bare
tables pass through. Name→id resolution is exact-match via `metadata/search`. Tables the
model references but that can't be resolved on the source come back as `missing_tables`
(a warning, and a candidate for the *prune / drop table from model* path in Stage 2).

---

## 5. Stage 1 — Align (obj_id identity)

**Why this stage exists:** `obj_id` is the cross-cluster primary key. If the target object
carries the **same `obj_id`** as the source object, import *updates it in place*. If not,
import *creates a duplicate*. This stage guarantees the former.

```mermaid
flowchart TD
    Start["Selected objects"] --> H{"obj_id health check"}
    H -->|"source missing obj_id"| SetSrc["Apply obj_id on source<br/>update_obj_ids (source)"]
    H -->|"target missing / mismatched"| AlignTgt

    subgraph AlignTgt["Target alignment"]
        Lookup["search_obj_ids(names) on target<br/>→ {guid, obj_id}"]
        direction TB
        Lookup --> Decide{"target has this<br/>obj_id already?"}
        Decide -->|"yes, matches"| OK["aligned ✓"]
        Decide -->|"no / different"| Fix["Fix target obj_ids<br/>update_obj_ids (target)"]
    end

    SetSrc --> Matcher
    AlignTgt --> Matcher

    subgraph Matcher["Table matcher (renamed / drifted)"]
        M1["match_tables(source, target)"] --> M2["pair source ↔ target<br/>by name + column signature"]
        M2 --> M3["Align matched pair →<br/>set shared obj_id on both<br/>update_obj_ids ×2"]
    end
```

**Under the hood.** `search_obj_ids` maps each object name to `{guid, obj_id}` via
`metadata/search`; a `None` obj_id means the object predates obj_id being enabled.
`update_obj_ids` POSTs to `metadata/update-obj-id` (needs `DATAMANAGEMENT`/`ADMINISTRATION`).
The **table matcher** (`services/table_matcher.py::match_tables`) handles the case where a
target table was renamed or drifted: it pairs by name *and* column signature so a shared
`obj_id` can be stamped on both sides, making a renamed target update-in-place instead of
duplicating.

---

## 6. Stage 2 — Git Operations

The heart of the tool. Four sub-phases run on this one page:
**Export+Transform → Discover → Resolve → Merge & Import.**

```mermaid
flowchart TD
    subgraph P1["① Export + data-layer transform  (on page entry)"]
        E1["export_tml(selected_ids)<br/>include_obj_id + obj_id_ref"] --> E2{"include feedback?"}
        E2 -->|yes| E3["export_feedback(model_guids)<br/>type=FEEDBACK, per-model"]
        E2 --> E4["read target column casing<br/>table_column_cases (fast, modeled)"]
        E3 --> E4
        E4 --> E5["transform_items:<br/>connection remap · db/schema map ·<br/>obj_ids · column casing"]
        E5 --> E6{"prune tables /<br/>skip columns?"}
        E6 -->|prune| E7["drop_tables(...)"]
        E6 -->|skip| E8["drop_columns(...)"]
        E7 --> Bundle["transformed_items<br/>(the promotion bundle)"]
        E8 --> Bundle
    end

    Bundle --> D0

    subgraph P2["② Discover all issues (primary action)"]
        D0["🔎 Export & discover all issues"] --> D1["commit_tml + create_pr"]
        D1 --> D2["_discover_all_issues probe<br/>(see §7)"]
        D2 --> D3["discovered_findings<br/>grouped by kind"]
    end

    D3 --> R0

    subgraph P3["③ Resolve findings"]
        R0{"finding kind"}
        R0 -->|missing warehouse col| RA["tick columns / Select all<br/>→ drop_columns"]
        R0 -->|type mismatch| RB["retype / drop"]
        R0 -->|invalid formula ids| RC["drop by formula name"]
        R0 -->|blocked by dependents| RD["preserve / remove deps"]
        R0 -->|opaque 'other'| RE["Find which object fails<br/>_isolate_failures → classify"]
        RA & RB & RC & RD & RE --> RF["Apply all resolutions<br/>& re-validate"]
        RF -. "re-probe" .-> D2
    end

    RF --> MI["④ Merge & Import (see §9)"]
```

**Under the hood.** Export uses `include_obj_id`/`include_obj_id_ref` so the aligned identity
travels with the TML. Column casing is aligned in two tiers: a **fast** read of tables already
modeled on the target (`table_column_cases`, no warehouse round-trip, runs on entry) and an
**opt-in slow** authoritative read straight from the warehouse
(`connection_column_cases` → `connection/search` with `data_warehouse_object_type=COLUMN`).
The re-export is guarded so plain navigation (Home/breadcrumb) preserves the last stage, but a
feedback-choice change or an obj_id edit forces a fresh export.

---

## 7. Deep dive: the discover / validate / drop engine

This is the mechanism that turns "import fails one error at a time" into "here is the complete,
resolvable list." It runs against a **throwaway copy** of the bundle — the real promotion set
is never mutated by discovery.

### The probe loop

```mermaid
flowchart TD
    Start(["_discover_all_issues(items)"]) --> Copy["work = deep copy of bundle<br/>seen = {} · passes = 0"]
    Copy --> Loop{"pass < SAFETY (40)?"}
    Loop -->|no| StopSafety["stop (backstop)"]
    Loop -->|yes| Val["import_tml(work, VALIDATE_ONLY)<br/>tables first, then models"]

    Val -->|"transient reset / timeout"| Req["reason = request_failed<br/>(keep prior discovery)"]
    Val --> Errs{"any errors?"}
    Errs -->|none| Clean["reason = clean ✓"]
    Errs -->|yes| Classify["classify_import_errors(errs)"]

    Classify --> Opaque{"ALL findings == 'other'?<br/>(opaque batch blob)"}
    Opaque -->|yes| Isolate["_isolate_failures(work):<br/>validate each file alone,<br/>classify per-file → itemized findings"]
    Opaque -->|no| Union
    Isolate --> Union["union findings into seen{}<br/>(dedup by finding_key)"]

    Union --> Neutralize["neutralize this pass on the COPY:<br/>drop_columns(missing/type/formula/dep cols)<br/>drop_vizzes(viz errors)"]
    Neutralize --> Progress{"anything removed?"}
    Progress -->|yes| Loop
    Progress -->|no| NoProg["reason = no_progress<br/>(remaining can't auto-resolve)"]

    Clean --> Return(["return findings, clean, passes, reason"])
    NoProg --> Return
    Req --> Return
    StopSafety --> Return
```

**Why a loop?** ThoughtSpot's `VALIDATE_ONLY` stops at the *first* missing column per table.
Neutralizing each pass's findings on the copy and re-validating surfaces the *next* layer,
until the copy validates clean or a pass can't neutralize anything (`no_progress`). Findings
are unioned across passes and deduped by `finding_key`, so the reviewer sees the complete set.

**The opaque-error escape hatch.** Sometimes the batch validate returns an unactionable blob
(bare `"Schema validation failed"`, no object named). When *every* finding is `kind="other"`,
the probe calls `_isolate_failures` to validate each file **on its own** and re-classify — so
a hidden missing column surfaces as a proper column-level finding instead of forcing a
whole-table skip.

### Per-file isolation

```mermaid
flowchart LR
    In(["opaque failure"]) --> Split["split into tables + models"]
    Split --> T["validate each TABLE alone<br/>(no cross-file deps)"]
    T --> Tbad{"a table failed?"}
    Tbad -->|yes| Attr1["attribute error to that table"]
    Tbad -->|no| M["validate each MODEL<br/>+ ALL tables present"]
    M --> Mbad["attribute error to that model"]
    Attr1 --> Out(["[{name, type, error}]"])
    Mbad --> Out
```

Tables are validated in isolation (no cross-file dependencies); models are validated **with
all tables present** so table refs resolve and only the model varies — a table fault is never
misattributed to a model.

### CDW as the single source of truth

For *missing columns* specifically, the tool prefers the warehouse over trial-and-error:

```mermaid
flowchart TD
    Cols["promoted table columns"] --> Q{"CDW column map<br/>available?<br/>(connection/search COLUMN)"}
    Q -->|yes| Verified["diff vs warehouse columns<br/>→ verified=True, no caveat"]
    Q -->|"no (warehouse unreachable)"| Fallback["diff vs target's MODELED columns<br/>→ verified=False + caveat"]
    Verified --> F["warehouse_missing_findings"]
    Fallback --> F
    F --> UI["missing-columns section<br/>(the whole set, up front)"]
```

`warehouse_missing_findings` diffs every promoted column against the authoritative warehouse
set in one shot — no whack-a-mole. When the warehouse can't be read, it falls back to the
target's modeled columns and flags each finding `verified=False` with a caveat (the modeled set
can be a subset of the warehouse, so a modeled-but-present column could look falsely missing).

---

## 8. Deep dive: the cascade drop

When a reviewer drops a column, everything that depended on it must go too — or the import
fails with dangling references. `drop_columns` cascades to a **fixpoint**.

```mermaid
flowchart TD
    Seed["targets = columns to drop<br/>(+ their model DISPLAY names)"] --> Fix{"fixpoint loop<br/>(re-scan until stable)"}

    Fix --> Fm["remove formulas that reference a removed name<br/>OR are themselves a target<br/>→ formula name joins 'removed'"]
    Fm --> Cm["remove columns that:<br/>• are a target, OR<br/>• have column_id formula_&lt;removed&gt;, OR<br/>• have a NAME matching a removed formula"]
    Cm --> Changed{"did removed{} grow?"}
    Changed -->|yes| Fix
    Changed -->|no| Joins["drop joins whose 'on' references a removed name"]

    Joins --> Viz["drop liveboard vizzes referencing a removed<br/>name (answer_columns or any inner expr)"]
    Viz --> Tiles["prune layout tiles / tabbed tiles<br/>for removed vizzes"]
    Tiles --> Tbl["remove physical columns from table docs"]
    Tbl --> Out(["new_items + manifest<br/>{columns, joins, formulas[], vizzes}"])
```

**The subtle bit that took the longest to get right:** a model column that *surfaces a formula*
may link to it by `column_id = formula_<name>` **or by name only** (`column_id` null). Both
must be dropped when the formula goes, otherwise the leftover column dangles as an
*"invalid formula IDs"* error on the next import. The fixpoint re-scans because removing a
formula-surfacing column can in turn orphan another formula.

`column_drop_cascade` is the **dry-run** twin — same logic, mutates nothing — used to preview
the blast radius before the reviewer confirms.

---

## 9. The Merge & Import choreography

Once findings are resolved, this is the ordered sequence that actually lands content on the
target. The ordering is load-bearing — several steps exist to work around platform behaviours.

```mermaid
sequenceDiagram
    autonumber
    participant App
    participant Git as GitHub bridge
    participant Tgt as Target cluster

    App->>Git: merge_pr() — squash dev → main
    Note right of Git: if already merged →<br/>re-commit, re-PR, re-validate, merge again

    opt Feedback REPLACE mode
        App->>Tgt: replace_prep — free existing model obj_ids
        Note right of Tgt: so import creates a FRESH model (clean feedback)
    end

    App->>Tgt: snapshot pre-import name index<br/>(Created vs Updated vs Duplicate)
    App->>Git: read merged TML (filter to THIS run's files)

    App->>Tgt: import_tml(core = tables + models)  [PARTIAL]
    App->>Tgt: import_tml(feedback)  — SEPARATE call
    Note right of Tgt: feedback batched with a first-time model fails<br/>(err 14500: can't resolve model by obj_id yet)

    opt Feedback REPLACE finalize
        App->>Tgt: replace_finalize — repoint deps to fresh model,<br/>delete old model (if no non-feedback deps)
    end

    opt NL instructions
        App->>Tgt: nl_promote (ai/instructions/set, merge|replace)
    end

    App->>Tgt: import_tml(leaves, VALIDATE_ONLY)
    alt leaves validate clean
        App->>Tgt: import_tml(leaves)  — liveboards / answers
        App->>App: import_phase = complete
    else leaf errors
        App->>App: import_phase = leaves_pending<br/>(surface viz/formula errors for review)
    end
```

**Under the hood.** `merge_pr` squash-merges; if the PR was already merged it re-commits and
opens a fresh one. Only *this run's* files are imported (`cur_paths` filter) — the team folder
accumulates TML across promotions, so an unfiltered import would re-import unrelated tables (the
"10 tables for a 3-table model" bug). **Feedback imports in its own call after tables+models
commit**, because a first-time model+feedback in one batch fails (error 14500) and under
`ALL_OR_NONE` would roll the model back too. **Leaves are validated before import** so viz/formula
errors surface here rather than silently at leaf import.

---

## 10. Stage 3 — Import Results

**What happens:** the tool reports, per object, whether it was **Created** (new on target),
**Updated in place** (obj_id matched — the goal), or a **Duplicate** (obj_id alignment missed).
The pre-import name-index snapshot taken in Stage 2 is what makes that distinction possible.

```mermaid
flowchart LR
    Pre["pre_import_index<br/>(names before import)"] --> Cmp{"compare each<br/>result vs snapshot"}
    Results["import_results<br/>(core + leaf)"] --> Cmp
    Cmp -->|"was absent, now present"| Created["🟢 Created"]
    Cmp -->|"obj_id matched existing"| Updated["🔵 Updated in place"]
    Cmp -->|"new guid, name already existed"| Dup["🟠 Duplicate (alignment gap)"]
    Created & Updated & Dup --> Report["from → to state report<br/>+ reconcile vs target"]
```

A **Duplicate** verdict is the signal that Stage 1 alignment should be revisited for that object.

---

## 11. Error taxonomy

Every blocker the tool recognises, how it's detected, and the reviewer action offered.
Classification lives in `classify_import_errors`; friendly translations in `friendly_error`.

| Kind | Trigger (verbatim from ThoughtSpot) | Code | Resolution offered |
|------|--------------------------------------|------|--------------------|
| `missing_in_target_warehouse` | *"External column with name: … does not exist in connection …"* | 14536 | Add column to warehouse, **or** drop it + dependents. Enumerated up front from the CDW. |
| `type_mismatch` | *"DataType … does not match CDW DataType for column …"* | — | Retype to target's type, align warehouse, or drop + dependents. |
| `drop_blocked_by_dependents` | *"Deleted columns have dependents. …"* | — | Preserve the column, or remove the dependents on target first. |
| `invalid_formula_ids` | *"…columns use invalid formula IDs. …"* | — | Drop the orphaned formula(s) by name (takes their surfacing columns). |
| `viz_error` | *"Visualization … has following errors …"* | — | Drop the failing viz (`drop_vizzes`), prune its layout tile. |
| `other` | anything unrecognised (e.g. bare *"Schema validation failed"*) | — | Per-file isolation to pin the culprit, then skip/fix that object. |

`friendly_error` additionally humanises operational failures: suspended/paused warehouse,
permission (10086), timeout/504, and connection reset (**10054**, which the client also
auto-retries — see §13).

---

## 12. REST API reference

All endpoints are ThoughtSpot REST **v2.0** (`/api/rest/2.0/…`), plus PyGithub for the bridge.

| Endpoint | Purpose | Where |
|----------|---------|-------|
| `auth/session/login` | org-scoped session login (or Bearer token) | `TSClient.__init__` |
| `metadata/search` | search by tag, resolve name→id, obj_id lookup, dependents, list-all | `search_by_tags`, `search_obj_ids`, `list_dependents`, `find_by_obj_id` |
| `metadata/tml/export` | export TML (`include_obj_id`, `include_obj_id_ref`); `type=FEEDBACK` per model | `export_tml`, `export_feedback` |
| `metadata/tml/import` | `VALIDATE_ONLY` (probe/isolate), `PARTIAL` / `ALL_OR_NONE` (real import) | `import_tml` |
| `metadata/update-obj-id` | stamp `obj_id` on source/target for update-in-place | `update_obj_ids` |
| `metadata/delete` | delete old model in feedback-Replace finalize | `delete_metadata` |
| `connection/search` | infer connection auth (`include_details`); read CDW columns (`data_warehouse_object_type=COLUMN`) | `_connection_meta`, `connection_column_cases` |
| `ai/instructions/get` / `set` | read/write Spotter NL instructions (full-replace semantics) | `get/set_nl_instruction_blocks` |
| GitHub: commit / PR / squash-merge | the promotion transaction (`dev → main`) | `GitClient.commit_tml`, `create_pr`, `merge_pr` |

---

## 13. Key design decisions

- **`obj_id` is identity, not name.** Update-in-place vs duplicate hinges entirely on matching
  `obj_id`. Stage 1 exists solely to guarantee alignment before any import.

- **The CDW is the source of truth for columns.** Rather than discover missing columns one
  import-failure at a time, the tool reads the warehouse's real column set
  (`connection/search COLUMN`) and diffs the whole promotion against it up front. Falls back to
  the target's modeled columns (flagged unverified) only when the warehouse can't be read.

- **Discovery is a fixpoint on a throwaway copy.** The real bundle is never mutated by
  discovery. The probe loops validate→classify→neutralize until `clean`, `no_progress`, or
  `request_failed`, unioning every finding so nothing is missed.

- **Cascades run to a fixpoint too.** Dropping a column transitively removes formulas,
  formula-surfacing columns (even name-linked ones with null `column_id`), joins, vizzes, and
  layout tiles — re-scanning until stable, so imports never fail on dangling references.

- **Idempotent calls retry through transient resets.** `VALIDATE_ONLY` and obj_id-keyed
  update-in-place imports are safe to retry, so `_retry_post` backs off (3s, 8s, 20s) on
  connection resets / timeouts (e.g. WinError 10054 from a gateway dropping a slow warehouse
  validate).

- **Only this run's files import.** The bridge team folder accumulates TML across promotions;
  every import/validate filters to the current run's file set to avoid re-importing stale
  objects.

- **Ordering works around platform quirks.** Feedback imports separately *after* models commit
  (error 14500); leaves validate before import so viz errors surface for review rather than
  silently.

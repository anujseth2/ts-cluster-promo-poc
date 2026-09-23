"""
ThoughtSpot Cross-Cluster TML Promotion Tool
Streamlit POC
"""

import json
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

import streamlit as st

from services.ts_client import TSClient
from services.git_client import GitClient
from services.tml_transformer import (
    detect_issues,
    transform_items,
    extract_model_refs,
    items_to_files,
    files_to_tml_strings,
    parse_feedback_items,
    feedback_key,
    filter_feedback,
)
from services.import_diagnostics import (
    classify_import_errors, drop_columns, silent_drop_findings, column_dependents, column_usage,
    drop_vizzes, table_drop_preview, drop_tables, warehouse_missing_findings, friendly_error,
    column_drop_cascade, finding_key, dangling_reference_findings, table_cleanup_findings,
    realign_column_types, warehouse_type_to_ts, warehouse_type_findings, type_family, type_class,
    recase_columns, model_tables_without_columns, prune_tables_whole, prune_stale_realignments,
    restore_unneeded_type_changes, promotion_plan, scan_names_for_drops,
    dependents_using_columns,
    blocking, warnings_only, is_blocking_result, drop_column_properties,
)
from services.target_cascade import (
    plan_cascade, plan_summary, dry_run_plan, snapshot_plan, apply_order, planned_names,
)
from services.table_matcher import column_signature
from services.feedback_replace import feedback_preview, replace_prep, replace_finalize
from services.reconcile import reconcile
from services.nl_instructions import preview as nl_preview, promote as nl_promote
from ui_feedback import render_feedback_panel, render_nl_panel

load_dotenv(Path(__file__).parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

TEAMS_FILE = Path(__file__).parent / "config" / "teams.json"

STEPS = [
    "1 · Select Assets",
    "2a · obj_id Setup",
    "2b · Source Audit",
    "2c · TML Validation",
    "3 · Git Operations",
    "4 · Import Results",
]


def load_teams() -> dict:
    return json.loads(TEAMS_FILE.read_text())


def save_teams(teams: dict):
    TEAMS_FILE.write_text(json.dumps(teams, indent=2))


def get_env(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        st.error(f"Missing environment variable: `{key}`. Check your `.env` file.")
        st.stop()
    return val


def opt_env(key: str) -> str:
    """Optional env var — empty string if unset."""
    return os.environ.get(key, "")


def _parse_edoc(edoc: str) -> dict:
    return json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)


def _target_name_index(client, types) -> dict:
    """{object_name: [guid, ...]} for the given metadata types on the target, captured
    BEFORE import so the results page can distinguish Created vs Updated-in-place vs
    DUPLICATE (a new guid appearing for a name that already existed)."""
    idx = {}
    for t in types:
        try:
            resp = client._post("/api/rest/2.0/metadata/search",
                                 {"metadata": [{"type": t}], "record_size": 5000})
        except Exception:
            continue
        rows = resp if isinstance(resp, list) else resp.get("metadata", [])
        for o in rows:
            nm = o.get("metadata_name")
            if nm:
                idx.setdefault(nm, []).append(o.get("metadata_id"))
    return idx


def _feedback_specs(items) -> list:
    """Promoted models that carry feedback -> [{name, obj_id, entries}] (obj_id = model obj_id)."""
    out = []
    for it in items:
        d = _parse_edoc(it.get("edoc", "{}"))
        if "nls_feedback" in d:
            out.append({"name":   it.get("info", {}).get("name", ""),
                        "obj_id": d.get("obj_id"),
                        "entries": (d.get("nls_feedback", {}) or {}).get("feedback", []) or []})
    return out


def _nl_models(items) -> list:
    """Promoted models -> [{name, obj_id, source_guid}] for NL-instruction promotion (info.id is
    the source model guid; obj_id resolves the target model)."""
    out = []
    for it in items:
        d = _parse_edoc(it.get("edoc", "{}"))
        for k in ("model", "worksheet"):
            node = d.get(k)
            if isinstance(node, dict) and node.get("name"):
                out.append({"name": node["name"], "obj_id": d.get("obj_id", ""),
                            "source_guid": (it.get("info") or {}).get("id", "")})
                break
    return out


# ── Validation + import helpers (module-level so BOTH step 2b and step 3 can call them) ──
def _run_validation(items, step=None):
    """Commit items to dev, create/update PR, validate models from dev. Returns (pr_url, errors, ok).
    step: optional callable(str) to report progress to the UI."""
    _tick = step or (lambda _m: None)
    # Any re-export invalidates a partial import in progress — reset the import phase.
    for _k in ("import_phase", "import_core_results", "import_leaf_files", "import_leaf_errors"):
        st.session_state.pop(_k, None)
    _tick("① Writing TML files to the dev branch…")
    files  = items_to_files(items)
    gc     = git_client()
    sha    = gc.commit_tml(team_name, files)
    _tick("② Opening / updating the pull request…")
    pr_url = gc.create_pr(team_name, sha)
    # Validate ONLY this run's files (tables first, then models) — the team folder accumulates
    # TML across promotions, and reading the whole folder would re-validate unrelated tables.
    val_strings = ([c for p, c in files.items() if p.startswith("tables/")]
                   + [c for p, c in files.items() if p.startswith("models/")])
    if not val_strings:
        return pr_url, [], []
    _tick(f"③ Validating {len(val_strings)} table/model file(s) against the target…")
    results = target_client().import_tml(val_strings, policy="VALIDATE_ONLY")
    st.session_state._last_validate = _log_validate(files, results)
    ok  = [r for r in results if r["status"] == "OK"]
    err = [r for r in results if is_blocking_result(r)]
    return pr_url, err, ok


def _is_github_error(exc) -> bool:
    """True when the exception came from the GITHUB side (PyGithub / the GitHub API) rather than
    the target cluster. Without this a bad GITHUB_TOKEN reports as 'couldn't reach the target',
    which sends you debugging the wrong system entirely."""
    if (type(exc).__module__ or "").split(".")[0] == "github":
        return True
    _m = str(exc).lower()
    return "api.github.com" in _m or "bad credentials" in _m


def _git_error_hint(exc) -> str:
    """A plain-English cause for a GitHub failure, so the fix is obvious from the message."""
    _m = str(exc).lower()
    if "bad credentials" in _m or "401" in _m:
        return ("GitHub rejected the credentials (401). The token is invalid, revoked, or not the "
                "one you think it is.")
    if "403" in _m:
        return ("GitHub refused the request (403). The token lacks permission on this repo, or you "
                "hit a rate limit.")
    if "404" in _m:
        return ("GitHub returned 404. GITHUB_REPO may be wrong, or the token can't see that repo.")
    return str(exc)[:200]


def _safe_validate(items, step=None):
    """_run_validation, but a hard failure becomes a friendly message + a logged run, not a raw
    traceback. Distinguishes a GITHUB failure (commit / PR, usually a bad GITHUB_TOKEN) from a
    TARGET-cluster failure so an auth problem never masquerades as 'couldn't reach the target'.
    Returns (pr_url, err, ok) on success, or None on failure (caller stops)."""
    try:
        return _run_validation(items, step=step)
    except Exception as _e:
        _msg = str(_e)
        st.session_state._last_validate = {
            "ts": "(request failed)", "files": [],
            "results": [{"name": "(validation request)", "status": "ERROR",
                         "error": _msg}]}
        if _is_github_error(_e):
            st.error("**The GitHub step failed** (commit / pull request), not the target cluster. "
                     + _git_error_hint(_e))
            st.caption("Check `GITHUB_TOKEN` and `GITHUB_REPO` in `.env`, and make sure no stale "
                       "`GITHUB_TOKEN` is exported in your shell (it would shadow `.env`). "
                       "Nothing was committed or promoted.")
        else:
            st.error("Validation couldn't reach the target. The client already auto-retries "
                     "transient resets, so try again; if it persists the message below is the "
                     "platform's own.")
            st.code(friendly_error(_msg)[2], language=None)
        return None


def _detect_silent_drops(items):
    """Target columns absent from the source -> dropped on import, SILENTLY when they have no
    dependents. Diff source tables against their current target versions before the final import."""
    tgt = target_client()
    src_docs, names = [], []
    for i in items:
        d = _parse_edoc(i.get("edoc", "{}"))
        if "table" in d and d["table"].get("name"):
            src_docs.append(d)
            names.append(d["table"]["name"])
    if not names:
        return []
    name_to_id = tgt._resolve_names_to_ids(names, "LOGICAL_TABLE")
    target_docs = {}
    if name_to_id:
        raw    = tgt.export_tml(list(name_to_id.values()))
        titems = raw if isinstance(raw, list) else raw.get("object", [])
        for it in titems:
            td = _parse_edoc(it.get("edoc", "{}"))
            if "table" in td and td["table"].get("name"):
                target_docs[td["table"]["name"]] = td
    return silent_drop_findings(src_docs, target_docs)


def _humanize(msg: str) -> str:
    """Turn ThoughtSpot's HTML-flecked error strings into plain text: <br/> becomes a line
    break, <b>..</b> becomes markdown bold. Returns the cleaned string."""
    s = str(msg or "")
    for br in ("<br/>", "<br />", "<br>"):
        s = s.replace(br, "\n")
    s = s.replace("<b>", "**").replace("</b>", "**")
    return s.strip()


def _sno(df):
    """Return a copy of df with a 1-based 'S.No' column inserted first — for display tables."""
    out = df.copy()
    out.insert(0, "S.No", range(1, len(out) + 1))
    return out


def _display_cols(t):
    """Columns of a table AS THE OPERATOR SEES THEM: the display name (falling back to the
    physical db_column_name), named-only, de-duplicated, order-preserved. Used for BOTH the
    'Source cols' count and the skip/drop lists so the two can never disagree — the historical
    24-vs-25 mismatch came from counting raw `columns` in one place and a filtered/deduped set
    in the other."""
    out, seen = [], set()
    for c in (t.get("columns") or []):
        nm = (c.get("name") or c.get("db_column_name") or "").strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower())
            out.append(nm)
    return out


def _gap_label(promoted_n, modeled_n):
    """Human-readable column delta between what the promotion carries and the target's MODELED
    table. Not the physical warehouse size. Avoids a bare negative number (a '-3' reads as a bug)."""
    if modeled_n is None:
        return "new on target"
    if promoted_n > modeled_n:
        return f"+{promoted_n - modeled_n} added to target model"
    if modeled_n > promoted_n:
        return f"{modeled_n - promoted_n} in target model, not promoted"
    return "match"


def _show_errors_verbatim(rows, key, title="Full error text (verbatim, copyable)"):
    """Render each row's error IN FULL, wrapped and copyable, under whatever table showed it.

    st.dataframe clips a long cell and does not wrap, so a platform message shown only in a table
    is a message the operator cannot read or paste. Since we cannot reliably interpret every error
    ThoughtSpot emits, the least we owe is its exact words — losing the tail of the one string
    that explains a failure is the worst possible place to save space.

    rows: iterable of dicts with at least an "error"; "name"/"object" and "status" are used as the
    heading when present."""
    items = [r for r in (rows or []) if str(r.get("error") or "").strip()]
    if not items:
        return
    with st.expander(f"{title} — {len(items)} message(s)", expanded=False):
        for i, r in enumerate(items, 1):
            who = r.get("name") or r.get("object") or r.get("Object") or "(object not named)"
            st_ = r.get("status") or r.get("Status") or ""
            st.markdown(f"**{i}. {who}**" + (f" · `{st_}`" if st_ else ""))
            # st.code wraps, keeps every character, and gives a copy button.
            st.code(str(r.get("error")), language=None)


def _select_editor(view, checkbox_cols, sel_keys, editor_base, column_config, disabled,
                   exclusive=False):
    """Render a data_editor whose checkbox column(s) persist in session sets, with SINGLE-CLICK
    behaviour. `view` must carry a hidden '_scoped' column. For each (checkbox_col, sel_key) pair,
    ticks land in st.session_state[sel_key] (a set of the row's _scoped value).

    Single-click works because an on_change callback consumes the edit BEFORE the rerun body redraws
    (a stashed position->scoped list maps the edit, since callbacks run before this run's locals
    exist). `exclusive=True` makes the FIRST checkbox win over the second per row (used for
    realign-vs-drop). A generation counter in the key lets bulk buttons reset the editor cleanly.

    checkbox_cols/sel_keys are parallel lists (1 or 2 entries)."""
    gen = st.session_state.get(f"_gen_{editor_base}", 0)
    ekey = f"{editor_base}::{gen}"
    st.session_state[f"_posmap_{editor_base}"] = list(view["_scoped"])
    for _k in sel_keys:
        st.session_state.setdefault(_k, set())

    def _cb(ekey=ekey, cols=tuple(checkbox_cols), keys=tuple(sel_keys),
            pk=f"_posmap_{editor_base}", excl=exclusive):
        pm = st.session_state.get(pk, [])
        state = st.session_state.get(ekey, {}) or {}
        for _pos, _ch in (state.get("edited_rows") or {}).items():
            p = int(_pos)
            if not (0 <= p < len(pm)):
                continue
            sk = pm[p]
            for _i, _col in enumerate(cols):
                if _col not in _ch:
                    continue
                _set = st.session_state.setdefault(keys[_i], set())
                if _ch[_col]:
                    _set.add(sk)
                    if excl and _i == 0:   # first checkbox wins: clear the other for this row
                        for _j, _ok in enumerate(keys):
                            if _j != _i:
                                st.session_state.setdefault(_ok, set()).discard(sk)
                else:
                    _set.discard(sk)

    st.data_editor(view.drop(columns=["_scoped"]), column_config=column_config, disabled=disabled,
                   hide_index=True, use_container_width=True, key=ekey, on_change=_cb)


def _bump_editor(editor_base):
    """Force a fresh data_editor next run (after a bulk select-all/clear) so stored per-cell edits
    don't fight the new seed."""
    st.session_state[f"_gen_{editor_base}"] = st.session_state.get(f"_gen_{editor_base}", 0) + 1


def _record_drop(man):
    """Accumulate a drop_columns manifest (columns/vizzes/joins/formulas) into session counters
    for the Import Results report."""
    st.session_state.dropped_cols_count = st.session_state.get("dropped_cols_count", 0) + man.get("columns", 0)
    st.session_state.dropped_vizs_count = st.session_state.get("dropped_vizs_count", 0) + man.get("vizzes", 0)
    st.session_state.dropped_joins_count = st.session_state.get("dropped_joins_count", 0) + man.get("joins", 0)
    # Every name the drop actually removed — the physical table columns AND the model columns /
    # formulas that came out with them. A set, because a re-export re-applies the durable skip set
    # and would otherwise pile up duplicates. Drives the cascade rows in the column-detail table.
    if man.get("column_names"):
        st.session_state.setdefault("dropped_cascade_names", set()).update(man["column_names"])
    if man.get("formulas"):
        st.session_state.setdefault("dropped_formula_names", []).extend(man["formulas"])


def _log_validate(files, results):
    """Append one VALIDATE_ONLY run to logs/validate_runs.jsonl so runs are diffable —
    which files were validated + each file's status/error. Never raises (logging must not
    break validation). Returns the record so the UI can also show it inline."""
    import datetime
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "files": sorted(files.keys()),
        "results": [{"name": r.get("name"), "type": r.get("type", ""),
                     "status": r.get("status"), "error": (r.get("error") or "")}
                    for r in results],
    }
    try:
        logdir = Path(__file__).parent / "logs"
        logdir.mkdir(exist_ok=True)
        with open(logdir / "validate_runs.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


def _prune_tables_whole(items, table_names):
    """Drop whole tables from the promotion: prune them from the model(s) AND remove each table's
    own TML item so it is not committed/validated/imported.

    The implementation now lives in services.import_diagnostics.prune_tables_whole, so the same
    contract is used by the app and covered by the property tests. Kept as a thin alias because
    every call site in this page refers to it by this name."""
    return prune_tables_whole(items, table_names)


def _log_discovery_pass(passes, errs, found, drop_set, viz_set, man, removed):
    """Append one discovery pass to logs/discovery.jsonl AS IT HAPPENS — so what each pass drops
    (the "N dependents") is itemized on disk, no one-shot re-capture needed. Never raises."""
    import datetime
    from collections import Counter
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "pass": passes,
        "errors": len(errs),
        "finding_kinds": dict(Counter(f.get("kind") for f in found)),
        "targeted_names": sorted(drop_set),
        "targeted_vizzes": sorted(str(v) for v in viz_set),
        "removed_total": removed,
        "dropped": {
            "columns": (man or {}).get("column_names", []),
            "joins":   (man or {}).get("join_names", []),
            "formulas": (man or {}).get("formulas", []),
            "vizzes":  (man or {}).get("vizzes", 0),
        },
    }
    try:
        logdir = Path(__file__).parent / "logs"
        logdir.mkdir(exist_ok=True)
        with open(logdir / "discovery.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


def _log_target_delete(host, team, rows, results):
    """Append an audit record of every DESTRUCTIVE CHANGE ON THE TARGET to
    logs/target_deletes.jsonl — a whole object deleted, a column stripped out of a model, or
    tiles removed from a liveboard.

    This is the only part of the tool that destroys customer content on another cluster, so it
    records what was changed and how, who authored it, which dropped column implicated it, and
    the outcome per object — before anyone has to reconstruct it from memory. Never raises."""
    import datetime
    rec = {
        "ts":   datetime.datetime.now().isoformat(timespec="seconds"),
        "host": host, "team": team,
        "deleted": [{"id": r.get("id"), "name": r.get("name"), "type": r.get("type"),
                     "action": r.get("action", "delete"),
                     "author": r.get("author"), "columns": r.get("columns"),
                     "status": results.get(r.get("id"))} for r in rows],
    }
    try:
        logdir = Path(__file__).parent / "logs"
        logdir.mkdir(exist_ok=True)
        with open(logdir / "target_deletes.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


def _log_apply_detail(tag, drop_set, man, pruned, items):
    """Append a detailed record of a manual drop/apply to logs/apply_detail.jsonl: what was
    targeted, what drop_columns ACTUALLY removed (columns + joins by name), what tables were
    pruned, and the FULL post-drop state — every table's remaining columns and every model's
    remaining joins (with ON conditions). Lets us see, from the log, whether a dropped column and
    its join actually left the bundle — no guessing. Never raises."""
    import datetime
    post = {}
    for it in items or []:
        try:
            d = _parse_edoc(it.get("edoc", "{}"))
        except Exception:
            continue
        t = d.get("table")
        if t and t.get("name"):
            post["table:" + t["name"]] = [
                (c.get("db_column_name") or c.get("name")) for c in (t.get("columns") or [])]
        mn = d.get("model") or d.get("worksheet")
        if mn:
            js = []
            for mt in (mn.get("model_tables") or mn.get("tables") or []):
                for j in (mt.get("joins") or []):
                    js.append(f"{mt.get('name')} -> {j.get('with')} ON {j.get('on')}")
            post["model:" + (mn.get("name") or "?")] = js
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "drop_set": sorted(drop_set or []),
        "removed": {
            "columns": (man or {}).get("column_names", []),
            "joins":   (man or {}).get("join_names", []),
            "formulas": (man or {}).get("formulas", []),
        },
        "pruned": sorted(pruned or []),
        "post_state": post,
    }
    try:
        logdir = Path(__file__).parent / "logs"
        logdir.mkdir(exist_ok=True)
        with open(logdir / "apply_detail.jsonl", "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass
    return rec


# ────────────────────────────────────────────────────────────────────────────
# Source-warehouse reads — promoted to module level so BOTH Source Audit (2b) and any
# other page can diff the promotion against the source CDW. Pure: session_state + globals.
# ────────────────────────────────────────────────────────────────────────────
def _read_source_col_map():
    """The SOURCE warehouse's column SET (names) for every promoted table, so the TML can be
    diffed against it (columns present in the TML but gone from the source CDW). hive SHOW
    COLUMNS via the source DBX creds (falling back to target for now), else source
    connection/search. Source coordinates come from the raw source export, pre-remap.
    Returns {table_lower: {col_lower: actual_case}} — same shape as the target map."""
    coord_by_name = {}
    for it in st.session_state.get("_source_raw_items", []):
        t = _parse_edoc(it.get("edoc", "{}")).get("table") or {}
        if t.get("name"):
            coord_by_name[t["name"].strip().lower()] = {
                "name": t["name"], "database": t.get("db", ""),
                "schema": t.get("schema", ""), "table": t.get("db_table", "")}
    tbls = list(coord_by_name.values())
    out = {}
    if not tbls:
        return out
    _host = opt_env("TS_SOURCE_DBX_HOST") or opt_env("TS_TARGET_DBX_HOST")
    _whid = opt_env("TS_SOURCE_DBX_WAREHOUSE") or opt_env("TS_TARGET_DBX_WAREHOUSE")
    _tok  = opt_env("TS_SOURCE_DBX_TOKEN") or opt_env("TS_TARGET_DBX_TOKEN")
    if _host and _whid and _tok:
        try:
            from services.databricks_direct import hive_column_cases
            out.update(hive_column_cases(_host, _whid, _tok, tbls, opt_env("TS_PROXY")))
        except Exception:
            pass
    _conn = (teams[team_name].get("source_connection", "")
             or teams[team_name].get("target_connection", ""))
    _rest = [t for t in tbls if (t.get("name") or "").strip().lower() not in out]
    if _rest and _conn:
        try:
            out.update(source_client().connection_column_cases(_conn, _rest))
        except Exception:
            pass
    return out

def _read_source_type_map():
    """The SOURCE warehouse's TYPE per column for every promoted table (DESCRIBE), so the
    promotion can be diffed against source types + casing in one pass. Same creds/coords as
    _read_source_col_map. Returns {table_lower: {col_lower: type_string}}."""
    coord_by_name = {}
    for it in st.session_state.get("_source_raw_items", []):
        t = _parse_edoc(it.get("edoc", "{}")).get("table") or {}
        if t.get("name"):
            coord_by_name[t["name"].strip().lower()] = {
                "name": t["name"], "database": t.get("db", ""),
                "schema": t.get("schema", ""), "table": t.get("db_table", "")}
    tbls = list(coord_by_name.values())
    out = {}
    if not tbls:
        return out
    _host = opt_env("TS_SOURCE_DBX_HOST") or opt_env("TS_TARGET_DBX_HOST")
    _whid = opt_env("TS_SOURCE_DBX_WAREHOUSE") or opt_env("TS_TARGET_DBX_WAREHOUSE")
    _tok  = opt_env("TS_SOURCE_DBX_TOKEN") or opt_env("TS_TARGET_DBX_TOKEN")
    if _host and _whid and _tok:
        try:
            from services.databricks_direct import hive_column_types
            out.update(hive_column_types(_host, _whid, _tok, tbls, opt_env("TS_PROXY")))
        except Exception:
            pass
    _conn = (teams[team_name].get("source_connection", "")
             or teams[team_name].get("target_connection", ""))
    _rest = [t for t in tbls if (t.get("name") or "").strip().lower() not in out]
    if _rest and _conn:
        try:
            out.update(source_client().connection_column_types(_conn, _rest))
        except Exception:
            pass
    return out






def _name_slug(name: str) -> str:
    """A stable obj_id slug derived from an object's name (used to pre-fill obj_id suggestions
    for objects that have none). Non-alphanumerics become underscores, repeats collapse."""
    s = "".join(c if (c.isalnum() or c == "_") else "_" for c in (name or "").strip())
    s = "_".join(p for p in s.split("_") if p)
    return s.lower() or "obj"


# ── Clients (cached per session) ──────────────────────────────────────────────

def _make_client(prefix: str) -> TSClient:
    """Build a cluster client from TS_<prefix>_* env vars. Token wins over user/pass."""
    host  = get_env(f"TS_{prefix}_HOST")
    proxy = opt_env("TS_PROXY")
    token = opt_env(f"TS_{prefix}_TOKEN")
    if token:
        return TSClient(host, token=token,
                        org_id=opt_env(f"TS_{prefix}_ORG"), proxy=proxy)
    return TSClient(host,
                    username=get_env(f"TS_{prefix}_USERNAME"),
                    password=get_env(f"TS_{prefix}_PASSWORD"),
                    org_id=opt_env(f"TS_{prefix}_ORG"), proxy=proxy)


@st.cache_resource
def source_client() -> TSClient:
    return _make_client("SOURCE")


@st.cache_resource
def target_client() -> TSClient:
    c = _make_client("TARGET")
    # Capture raw error responses as they happen — a failure is on disk the moment it occurs, so
    # debugging is "read the log", not "re-run every validate". Errors only, so it stays small.
    c.debug_raw_log = str(Path(__file__).parent / "logs" / "validate_raw.jsonl")
    return c


@st.cache_resource
def git_client() -> GitClient:
    return GitClient(get_env("GITHUB_TOKEN"), get_env("GITHUB_REPO"))


# ── Navigation helpers ────────────────────────────────────────────────────────

def _go(step: int):
    st.session_state.step = step
    # Remember the furthest step reached so the breadcrumb can navigate FORWARD
    # to already-completed stages (not just backward). Home returns to step 0 but
    # keeps this frontier, so it stays distinct from Reset (which clears it).
    st.session_state.max_step = max(st.session_state.get("max_step", 0), step)
    st.rerun()


def _nav(step: int, can_next: bool = True, next_hint: str = "",
         next_label: str = "Next →", next_reexport: bool = False):
    st.divider()
    col_back, col_mid, col_next = st.columns([1, 6, 1])
    with col_back:
        if step > 0 and st.button("← Back", key=f"back_{step}"):
            _go(step - 1)
    with col_next:
        if step < len(STEPS) - 1:
            if can_next:
                if st.button(next_label, type="primary", key=f"next_{step}"):
                    # next_reexport: force ONE clean re-export on advance when this page made changes
                    # (the durable intent — skip/realign/recase — is re-applied from a fresh source
                    # pull). Only when something actually changed, so a pass-through stays fast.
                    if next_reexport and st.session_state.pop("_source_audit_dirty", False):
                        for _k in ("transformed_items", "pr_url", "validation_errors", "validation_ok"):
                            st.session_state.pop(_k, None)
                    _go(step + 1)
            else:
                # Show a disabled Next so the control never just vanishes, and
                # explain WHY it's blocked instead of leaving the user stuck.
                st.button(next_label, key=f"next_{step}", disabled=True)
    if step < len(STEPS) - 1 and not can_next and next_hint:
        with col_mid:
            st.caption(f"⛔ {next_hint}")


# ── Page setup ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TS Cross-Cluster Promotion",
    page_icon="🔄",
    layout="wide",
)

st.title("ThoughtSpot Cross-Cluster Promotion")

# ── Sidebar ───────────────────────────────────────────────────────────────────

teams = load_teams()

with st.sidebar:
    _hc1, _hc2 = st.columns(2)
    with _hc1:
        if st.button("🏠 Home", use_container_width=True,
                     help="Back to the first page — keeps your current work"):
            _go(0)
    with _hc2:
        if st.button("↺ Reset", use_container_width=True,
                     help="Clear everything and start a fresh promotion"):
            for _k in list(st.session_state.keys()):
                del st.session_state[_k]
            st.session_state.step = 0
            st.rerun()
    st.divider()
    st.header("Team")
    team_name = st.selectbox("Select team", list(teams.keys()))
    team_cfg  = teams[team_name]
    team_tags = team_cfg.get("tags", [])
    st.caption("Scope tag(s): " + (", ".join(f"`{t}`" for t in team_tags) or "_none set_"))

    st.divider()
    st.subheader("Connections")
    st.caption("Remap connection references from the source to the target cluster.")

    src_conn = st.text_input("Source connection name",
                             value=team_cfg.get("source_connection", ""),
                             key="src_conn")
    tgt_conn = st.text_input("Target connection name",
                             value=team_cfg.get("target_connection", ""),
                             key="tgt_conn")

    st.caption("Remap the database / schema names too (leave blank to keep them as-is).")
    _dbm0 = team_cfg.get("db_map", {}) or {}
    _scm0 = team_cfg.get("schema_map", {}) or {}
    _src_db0 = next(iter(_dbm0), ""); _tgt_db0 = _dbm0.get(_src_db0, "")
    _src_sc0 = next(iter(_scm0), ""); _tgt_sc0 = _scm0.get(_src_sc0, "")
    _dc1, _dc2 = st.columns(2)
    with _dc1:
        src_db = st.text_input("Source database", value=_src_db0, key="src_db")
        src_sc = st.text_input("Source schema",   value=_src_sc0, key="src_sc")
    with _dc2:
        tgt_db = st.text_input("Target database", value=_tgt_db0, key="tgt_db")
        tgt_sc = st.text_input("Target schema",   value=_tgt_sc0, key="tgt_sc")

    if st.button("Save connection config"):
        teams[team_name]["source_connection"] = src_conn
        teams[team_name]["target_connection"] = tgt_conn
        teams[team_name]["db_map"]     = {src_db.strip(): tgt_db.strip()} if src_db.strip() and tgt_db.strip() else {}
        teams[team_name]["schema_map"] = {src_sc.strip(): tgt_sc.strip()} if src_sc.strip() and tgt_sc.strip() else {}
        save_teams(teams)
        st.success("Saved.")

    st.divider()
    st.caption("Names are preserved across clusters; identity is by obj_id.")

# ── Step indicator ─────────────────────────────────────────────────────────────

step = st.session_state.get("step", 0)
# The furthest stage reached — every step up to here is navigable in BOTH
# directions from the breadcrumb (keep it at least at the current step).
max_step = max(st.session_state.get("max_step", 0), step)
st.session_state.max_step = max_step

cols = st.columns(len(STEPS))
for i, (col, label) in enumerate(zip(cols, STEPS)):
    with col:
        if i == step:
            st.markdown(
                f"<div style='text-align:center;padding:6px 0;border-bottom:3px solid #4a6fa5;"
                f"font-weight:700;font-size:13px'>{label}</div>",
                unsafe_allow_html=True,
            )
        elif i <= max_step:
            # Reached before (behind or ahead of the current step) → clickable.
            if st.button(label, key=f"step_{i}", use_container_width=True):
                _go(i)
        else:
            st.markdown(
                f"<div style='text-align:center;padding:6px 0;color:#94a3b8;font-size:13px'>{label}</div>",
                unsafe_allow_html=True,
            )

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# Shared bundle prep — export + transform (Source Audit and TML Validation both call this)
# ══════════════════════════════════════════════════════════════════════════════

def _prepare_bundle():
    """Export the selected assets from source, transform (connection remap, obj_ids, column
    casing), apply the approved recasings + drops, and stash the promotion bundle in session
    state. Idempotent: guarded by _need_export, so plain navigation between the Source Audit
    and TML Validation pages does not re-export. Shared by both pages so the bundle exists
    before either one audits or validates it."""
    selected_ids = st.session_state.get("selected_ids", [])

    # Export + transform on entry. This runs AFTER obj_id Setup, so the exported TML carries
    # the aligned obj_ids. (This replaced the standalone Review page.)
    # Re-export when the FEEDBACK choice changed since the last export — otherwise a bundle
    # cached before "Include feedback" was ticked would silently omit feedback (and the commit
    # would too). Changing the choice also invalidates the already-committed PR/validation.
    _fb_state = (bool(st.session_state.get("_include_feedback")),
                 frozenset(st.session_state.get("feedback_selected") or []))
    # Re-export if we have no bundle yet, the feedback choice changed, or obj_ids/alignment were
    # changed since the last export (consume the dirty flag). Plain navigation back to this page
    # (Home / breadcrumb) does none of these, so the last stage is preserved.
    _objids_dirty = st.session_state.pop("_objids_dirty", False)
    _need_export = ("transformed_items" not in st.session_state
                    or st.session_state.get("_export_fb_state") != _fb_state
                    or _objids_dirty)
    if selected_ids and _need_export:
        if "transformed_items" in st.session_state:
            for _k in ("pr_url", "validation_errors", "validation_ok", "import_phase",
                       "import_core_results", "import_leaf_files", "import_leaf_errors",
                       "silent_drops", "_fb_previews", "_nl_previews", "nl_report",
                       "fb_replace_report", "_casing_diag"):
                st.session_state.pop(_k, None)
        with st.status("Preparing the promotion bundle…", expanded=True) as _exp_status:
            st.write("① Exporting TML from the source cluster…")
            try:
                raw = source_client().export_tml(selected_ids)
            except Exception as _ex:
                _exp_status.update(label="Export failed — source connection reset", state="error")
                st.error("Couldn't export from the source cluster. This is usually a transient "
                         "network reset from a proxy or gateway, not anything to do with dropped "
                         "columns, and the client already retried with backoff. Retry below.")
                st.code(friendly_error(str(_ex))[2], language=None)
                if st.button("↻ Retry export"):
                    st.rerun()
                st.stop()
            items = raw if isinstance(raw, list) else raw.get("object", [])
            # Opt-in: also pull each model's Spotter feedback (reference questions + business
            # terms) and promote it alongside the model.
            if st.session_state.get("_include_feedback"):
                # The model GUID lives in the export wrapper's info.id — the edoc itself does NOT
                # carry a top-level `guid` under include_obj_id export (Step 1 reads info.id too).
                model_guids = []
                for it in items:
                    d = _parse_edoc(it.get("edoc", "{}"))
                    if "model" in d or "worksheet" in d:
                        gid = (it.get("info") or {}).get("id") or d.get("guid")
                        if gid:
                            model_guids.append(gid)
                if model_guids:
                    fb_items = source_client().export_feedback(model_guids)
                    # Keep only the reference questions / business terms the operator ticked
                    # on the Select page (None -> promote all, back-compat).
                    fb_items = filter_feedback(
                        fb_items, st.session_state.get("feedback_selected"))
                    items = items + fb_items
            # Align promoted table columns to the TARGET warehouse's casing. Some warehouses bind
            # external columns case-sensitively (e.g. Databricks), so a source column CID cannot
            # import against a target column cid. Primary source of truth is the TARGET connection
            # (ThoughtSpot reads the warehouse with its stored credential — no secret needed, works
            # even when the table isn't a logical table on the target yet). Fall back to reading an
            # existing target logical table's casing if the connection can't be queried.
            _dbm = teams[team_name].get("db_map", {})
            _scm = teams[team_name].get("schema_map", {})
            _trm = st.session_state.get("table_remap", {})
            promoted, names = [], []
            for it in items:
                t = (_parse_edoc(it.get("edoc", "{}")).get("table") or {})
                if not t.get("name"):
                    continue
                nm = t["name"]; names.append(nm)
                tr = _trm.get(nm.strip().lower(), {})
                promoted.append({
                    "name":     nm,
                    "database": _dbm.get(t.get("db", ""), t.get("db", "")),
                    "schema":   _scm.get(t.get("schema", ""), t.get("schema", "")),
                    "table":    tr.get("db_table") or t.get("db_table", ""),
                    "connection": (t.get("connection") or {}).get("name", ""),
                })
            column_case_map = {}
            tgt_conn = teams[team_name].get("target_connection", "")
            st.write("② Reading column casing from tables already on the target (fast)…")
            # FAST PATH ONLY during export — a TML metadata read of tables already modeled on the
            # target, no warehouse round-trip. The authoritative CDW column read (via connection/
            # search) is OPT-IN on this page, because COLUMN introspection can be very slow or time
            # out on some warehouses (the GSK 504) and must NEVER block the export.
            try:
                column_case_map = target_client().table_column_cases(names)
            except Exception:
                column_case_map = {}
            # Snapshot the target's MODELED columns (the logical table as it exists on test right now)
            # BEFORE the hive merge widens the map to the full physical set. The shape display compares
            # the promotion to THIS — "what got promoted vs what the target model has" — not the
            # physical warehouse count (which is far larger and reads like the column count exploded).
            st.session_state._target_modeled_map = {_t: dict(_c) for _t, _c in column_case_map.items()}
            # Hive_metastore casing (authoritative, direct). ThoughtSpot's connection/search 504s on
            # hive_metastore because it introspects columns via <catalog>.information_schema, which
            # hive lacks. So for a hive target we read the true casing straight from Databricks via
            # SHOW COLUMNS (works on hive AND Unity Catalog). Gated on target DBX creds in .env; when
            # present this fills/overrides the map for tables the fast path can't see (not yet on the
            # target) — e.g. a promoted model whose `CID` must bind to the warehouse's `cid`.
            _dbx_host = opt_env("TS_TARGET_DBX_HOST")
            _dbx_wh   = opt_env("TS_TARGET_DBX_WAREHOUSE")
            _dbx_tok  = opt_env("TS_TARGET_DBX_TOKEN")
            _hive = {}   # authoritative physical read of the TARGET warehouse (hive/UC), if creds set
            if _dbx_host and _dbx_wh and _dbx_tok:
                st.write("②b Reading hive_metastore casing directly from Databricks…")
                try:
                    from services.databricks_direct import hive_column_cases
                    _dbg = []
                    _hive = hive_column_cases(_dbx_host, _dbx_wh, _dbx_tok, promoted,
                                              opt_env("TS_PROXY"), debug=_dbg)
                    for _t, _cols in _hive.items():
                        column_case_map.setdefault(_t, {}).update(_cols)
                    _ok = sum(1 for d in _dbg if d.get("state") == "SUCCEEDED")
                    st.write(f"   warehouse casing resolved for {_ok}/{len(_dbg)} table(s).")
                except Exception as _e:
                    st.write(f"   ⚠ direct warehouse casing skipped: {str(_e)[:150]}")
            st.session_state._column_case_map = column_case_map
            # The TARGET warehouse is read HERE, at export (the direct hive read that actually works
            # on GSK — the old connection/search "Verify" button 504'd on hive and was redundant with
            # this). So the missing-column check treats these as warehouse-verified with no extra
            # click. Empty when no target DBX creds → the check falls back to modeled columns.
            st.session_state._warehouse_col_map = _hive
            # Persist coords + connection so the opt-in "verify against the warehouse" button can
            # issue the (slow) connection read without re-exporting.
            st.session_state._promoted_coords = promoted
            st.session_state._promoted_tgt_conn = tgt_conn
            # Stash the RAW source export (pre-transform, pre-drop) so a debug bundle carries the
            # original model+tables — the true joins/columns before any remap or cascade. Captured
            # here, as it happens; edocs are strings so a shallow per-item copy is enough.
            # Record the physical columns the transform will recase to the warehouse casing, so the
            # Import Results report can show it (the recase is otherwise silent). Mirrors the
            # transformer rule: db_column_name is recased when it differs from the map's casing.
            _recase_events = []
            for _it in items:
                _rt = (_parse_edoc(_it.get("edoc", "{}")).get("table") or {})
                _rtn = _rt.get("name")
                _rcc = column_case_map.get((_rtn or "").strip().lower()) if _rtn else None
                if not _rcc:
                    continue
                for _rcol in (_rt.get("columns") or []):
                    _rdbn = _rcol.get("db_column_name")
                    if _rdbn:
                        _rtgt = _rcc.get(_rdbn.strip().lower())
                        if _rtgt and _rtgt != _rdbn:
                            _recase_events.append({"table": _rtn, "from": _rdbn, "to": _rtgt})
            st.session_state._recase_events = _recase_events
            st.session_state._source_raw_items = [dict(it) for it in items]
            # APPROVE-FIRST recasing (Anuj 2026-08-18: no silent mutations). Nothing is recased until
            # the operator approves it in the "Column recasing" panel — the transform sees only the
            # APPROVED subset of the (full) warehouse casing map. Empty approval set → no recasing, so
            # unapproved columns keep their source casing. `_column_case_map` stays the FULL map (other
            # consumers — the missing-column fallback — need it).
            _rapproved = st.session_state.get("recase_approved", set())
            applied_case_map = {
                _t: {_c: _case for _c, _case in (_cols or {}).items()
                     if f"{_t}::{_c}" in _rapproved}
                for _t, _cols in column_case_map.items()}
            st.session_state._recase_applied_set = set(_rapproved)
            st.write("③ Applying the data-layer transform (connection remap, obj_ids, column casing)…")
            transformed_items, warnings = transform_items(
                items,
                source_connection=teams[team_name].get("source_connection", ""),
                target_connection=teams[team_name].get("target_connection", ""),
                db_map=teams[team_name].get("db_map", {}),
                schema_map=teams[team_name].get("schema_map", {}),
                table_remap=st.session_state.get("table_remap", {}),
                column_case_map=applied_case_map,
            )
            # Prune any tables the user chose to drop out of the model (not-on-target excludes).
            prune = st.session_state.get("prune_tables", set())
            if prune:
                transformed_items, prune_summary = drop_tables(transformed_items, prune)
                st.session_state.prune_summary = prune_summary
            # Skip individual columns the user chose to leave out (persisted across re-exports).
            skip_cols = st.session_state.get("skip_columns", set())
            if skip_cols:
                transformed_items, _man = drop_columns(transformed_items, skip_cols)
                _record_drop(_man)
                st.session_state.setdefault("dropped_col_names", set()).update(skip_cols)
            # Realign stale TML types the operator approved on Source Audit — persisted across
            # re-exports (a fresh export would otherwise re-import the stale type). Scoped
            # `table::col` -> TS token; already-dropped columns are gone, so this is a no-op for them.
            realign_map = st.session_state.get("realign_types", {})
            if realign_map:
                # Drop approvals that no longer change the storage class BEFORE applying, so a
                # rule change can't leave the bundle carrying rewrites nobody asked for, and so
                # the "realignments applied" list on the validation page tells the truth.
                realign_map, _stale = prune_stale_realignments(transformed_items, realign_map)
                if _stale:
                    st.session_state.realign_types = realign_map
                    st.session_state._realign_pruned = sorted(_stale)
                if realign_map:
                    transformed_items, _rn = realign_column_types(transformed_items, realign_map)
            st.session_state.transformed_items = transformed_items
            st.session_state.warnings          = warnings
            st.session_state._export_fb_state  = _fb_state   # what feedback choice this export reflects
            st.session_state.pop("_fb_previews", None)   # recompute feedback preview vs the fresh export
            # Flag if a configured source_connection matches NO connection in the exported
            # tables (the remap would silently skip -> import failure on the target).
            src_conn   = teams[team_name].get("source_connection", "")
            conn_names = set()
            for it in items:
                c = (_parse_edoc(it.get("edoc", "{}")).get("table", {}) or {}).get("connection", {})
                if isinstance(c, dict) and c.get("name"):
                    conn_names.add(c["name"])
            st.session_state.conn_mismatch = (
                {"configured": src_conn, "found": sorted(conn_names)}
                if src_conn and conn_names and src_conn not in conn_names else None)
            _exp_status.update(
                label=f"Bundle ready — {len(transformed_items)} object(s) prepared.",
                state="complete", expanded=False)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 0 — Select Assets
# ══════════════════════════════════════════════════════════════════════════════

if step == 0:
    st.subheader(f"Source-cluster assets — team: {team_name}")

    if team_tags:
        st.caption("Fetching content tagged: " + ", ".join(f"`{t}`" for t in team_tags))
        fetch_label = "Fetch tagged content"
    else:
        st.info("No tag set for this team — fetching **all** liveboards, answers, models & tables "
                "you can access. Tick anything to promote: a liveboard pulls its model + tables, a "
                "model pulls its tables, a table promotes on its own.")
        fetch_label = "Fetch assets"

    if st.button(fetch_label, type="primary"):
        with st.spinner("Searching the source cluster…"):
            st.session_state.assets = source_client().search_by_tags(
                team_tags, types=["LIVEBOARD", "ANSWER", "LOGICAL_TABLE"])
            for key in ("picks", "selected_ids", "selected_asset_ids", "dep_info",
                        "_resolved_key", "excluded",
                        "_promo_id2name", "_promo_present", "obj_id_status",
                        "table_alignment", "transformed_items", "import_results", "recon_report",
                        "pre_import_index", "dropped_col_names", "dropped_cols_count",
                        "_landed_col_map", "dropped_cascade_names",
                        "dropped_vizs_count", "prune_summary",
                        "_fb_previews", "feedback_mode", "ack_replace", "fb_replace_report",
                        "_include_feedback", "_export_fb_state",
                        # source-CDW validation state (Increments 2 & 3) — reset for new content
                        "recase_approved", "_recase_applied_set", "_recase_events",
                        "_source_col_map", "_source_type_map", "src_drop_selected",
                        "src_type_drop_selected", "src_type_realign_selected", "_src_realign_to",
                        "realign_types", "_source_audit_dirty", "skip_selected", "skip_expanded",
                        "_skip_gen", "wh_drop_selected",
                        "tm_drop_selected", "tm_realign_selected", "_tm_realign_to",
                        "_tm_target_full", "_tm_source_full", "_target_modeled_map"):
                st.session_state.pop(key, None)

    assets = st.session_state.get("assets", [])
    unsafe = []   # local: models/tables excluded but absent on target (blocks Next)

    if not assets:
        if "assets" in st.session_state:
            st.info("No assets found.")
    else:
        import pandas as pd

        df = pd.DataFrame(assets)[["name", "type", "author", "modified",
                                   "created", "tags", "obj_id", "id"]]

        c1, c2 = st.columns([3, 1])
        with c1:
            flt = st.text_input("Search name or tags", "", key="asset_filter",
                                placeholder="type to narrow the list").strip()
        with c2:
            type_opts = ["All"] + sorted(t for t in df["type"].unique() if t)
            type_sel  = st.selectbox("Type", type_opts, key="asset_type")

        if flt:
            df = df[df["name"].str.contains(flt, case=False, na=False)
                    | df["tags"].str.contains(flt, case=False, na=False)]
        if type_sel != "All":
            df = df[df["type"] == type_sel]

        st.caption(f"{len(df)} object(s) — click any column header to sort.")

        # Persistent selection across filters (#9): this set is the source of truth. It's seeded
        # into the checkbox column each rerun and reconciled from the VISIBLE rows, so narrowing the
        # filter never loses a tick and Clear reliably unticks. Covers EVERY listed type — tables,
        # models, liveboards, answers — since the list holds all of them.
        _asel = st.session_state.setdefault("selected_asset_ids", set())
        _ids_shown = df["id"].tolist()
        _sa, _sc, _sac, _ = st.columns([1, 1, 1, 3])
        with _sa:
            if st.button(f"Select all shown ({len(_ids_shown)})", use_container_width=True,
                         disabled=not _ids_shown):
                _asel.update(_ids_shown); st.rerun()
        with _sc:
            if st.button("Clear shown", use_container_width=True, disabled=not _ids_shown):
                for _i in _ids_shown:
                    _asel.discard(_i)
                st.rerun()
        with _sac:
            if st.button("Clear all", use_container_width=True, disabled=not _asel):
                _asel.clear(); st.rerun()

        df.insert(0, "select", df["id"].isin(list(_asel)))
        df.insert(0, "S.No", range(1, len(df) + 1))

        edited = st.data_editor(
            df,
            column_config={
                "S.No":     st.column_config.NumberColumn("S.No", width="small"),
                "select":   st.column_config.CheckboxColumn("Promote?", default=False),
                "name":     st.column_config.TextColumn("Name",     width="large"),
                "type":     st.column_config.TextColumn("Type",     width="small"),
                "author":   st.column_config.TextColumn("Author",   width="medium"),
                "modified": st.column_config.TextColumn("Modified", width="small"),
                "created":  st.column_config.TextColumn("Created",  width="small"),
                "tags":     st.column_config.TextColumn("Tags",     width="medium"),
                "obj_id":   st.column_config.TextColumn("obj_id",   width="medium"),
                "id":       st.column_config.TextColumn("GUID",     width="small"),
            },
            disabled=["S.No", "name", "type", "author", "modified", "created", "tags", "obj_id", "id"],
            use_container_width=True,
            hide_index=True,
            # Query-scoped key: a fresh editor per filtered view (seeded from _asel) so a stored
            # edit-delta can't mis-apply to a shifted row after the filter changes.
            key=f"asset_editor::{flt}::{type_sel}",
        )

        # Reconcile ONLY the visible rows into the persistent set; the selection is the whole set
        # across filters (not just what's currently shown).
        for _i in df.index:
            _rid = df.at[_i, "id"]
            if bool(edited.at[_i, "select"]):
                _asel.add(_rid)
            else:
                _asel.discard(_rid)
        picks = sorted(_asel)
        _sel_shown = sum(1 for i in _ids_shown if i in _asel)
        _hidden = len(_asel) - _sel_shown
        if _asel:
            _cap = f"**{len(_asel)}** object(s) selected"
            if _hidden > 0:
                _cap += f" · {_hidden} not shown under the current filter"
            st.caption(_cap + ".")
        type_by_id  = {a["id"]: a["type"] for a in assets}
        name_by_id  = {a["id"]: a["name"] for a in assets}
        leaf_picks  = [i for i in picks if type_by_id.get(i) in ("LIVEBOARD", "ANSWER")]
        model_picks = [i for i in picks if type_by_id.get(i) == "MODEL"]
        table_picks = [i for i in picks if type_by_id.get(i) == "TABLE"]

        # Resolve the full stack from the mixed roots, cached by the pick set so it only
        # re-calls when the picks actually change (not on every filter/sort rerun).
        sel_key = tuple(sorted(picks))
        if picks and st.session_state.get("_resolved_key") != sel_key:
            with st.spinner("Resolving dependencies (models + tables)…"):
                dep = source_client().resolve_promotion(leaf_picks, model_picks, table_picks)
            id2name = dict(name_by_id)
            for nm, i in dep["model_map"].items():
                id2name[i] = nm
            for nm, i in dep["table_map"].items():
                id2name[i] = nm
            # Target presence by name (cross-cluster names are preserved) — guards exclusion.
            mt_names = sorted({id2name.get(i, "") for i in dep["model_ids"] + dep["table_ids"]
                               if id2name.get(i)})
            present = set()
            if mt_names:
                try:
                    present = set(target_client()._resolve_names_to_ids(mt_names, "LOGICAL_TABLE").keys())
                except Exception:
                    present = set()
            st.session_state.dep_info        = dep
            st.session_state._promo_id2name  = id2name
            st.session_state._promo_present  = present
            st.session_state._promo_items    = (dep.get("model_items") or []) + (dep.get("leaf_items") or [])
            st.session_state._resolved_key   = sel_key
            st.session_state.pop("excluded", None)
            st.session_state.pop("promo_selected", None)
            st.session_state.pop("_promo_seed_sig", None)
            st.session_state.pop("prune_tables", None)
            st.session_state.pop("prune_ack_sig", None)
            for _k in ("obj_id_status", "_raw_items", "table_alignment", "prod_by_name",
                       "prod_leaf", "dev_table_refs", "dev_model_refs", "dev_leaf_refs",
                       "transformed_items", "match_results", "table_remap"):
                st.session_state.pop(_k, None)
        elif not picks:
            for _k in ("dep_info", "selected_ids"):
                st.session_state.pop(_k, None)
            st.session_state._resolved_key = None

        dep = st.session_state.get("dep_info")
        if dep:
            id2name     = st.session_state.get("_promo_id2name", {})
            present     = st.session_state.get("_promo_present", set())
            promo_items = st.session_state.get("_promo_items", [])
            excluded    = st.session_state.setdefault("excluded", set())
            prune       = st.session_state.setdefault("prune_tables", set())

            st.divider()
            st.markdown("**Promotion set** — leaves always promote. Untick a model or table to "
                        "leave it out. If it already exists on the target the model just binds to "
                        "that copy; if it does not, you can prune it out of the model, and you will "
                        "see exactly what gets dropped first.")

            if dep["model_ids"]:
                # Persist the toggle across steps. Streamlit drops a widget's state once the widget
                # stops rendering (this checkbox lives only on the Select page), so navigating to
                # Step 2/3 wiped `include_feedback` and the export silently skipped feedback. Mirror
                # it into a normal key `_include_feedback` that the later steps read.
                if "include_feedback" not in st.session_state:
                    st.session_state["include_feedback"] = st.session_state.get("_include_feedback", False)
                inc_fb = st.checkbox(
                    "Include Spotter feedback (reference questions + business terms) for the model(s)",
                    key="include_feedback",
                    help="Also promote each model's Spotter feedback — its reference questions and "
                         "business terms — exported as FEEDBACK TML and imported after the model.")
                st.session_state["_include_feedback"] = inc_fb
                if inc_fb:
                    # Load the models' feedback once per model set so the operator can pick
                    # individual reference questions / business terms to promote.
                    fb_set_key = tuple(dep["model_ids"])
                    if st.session_state.get("_fb_loaded_key") != fb_set_key:
                        # New model set: drop stale per-item checkbox widget state so the
                        # picker rebuilds against the current feedback list.
                        for _wk in [k for k in list(st.session_state.keys())
                                    if k.startswith("fbchk_") or k == "fb_picker"]:
                            del st.session_state[_wk]
                        with st.spinner("Loading Spotter feedback…"):
                            st.session_state._fb_items = \
                                source_client().export_feedback(list(dep["model_ids"]))
                        st.session_state._fb_loaded_key = fb_set_key
                        st.session_state.feedback_selected = {
                            feedback_key(e["model"], e["type"], e["phrase"])
                            for e in parse_feedback_items(st.session_state._fb_items)}

                    fb_entries = parse_feedback_items(st.session_state.get("_fb_items", []))
                    if not fb_entries:
                        st.caption("No Spotter feedback found on the selected model(s).")
                        st.session_state.feedback_selected = set()
                    else:
                        prev_sel = st.session_state.get("feedback_selected", set())
                        multi_model = len({e["model"] for e in fb_entries}) > 1
                        type_label  = {"REFERENCE_QUESTION": "Reference question",
                                       "BUSINESS_TERM": "Business term"}
                        # Tabular picker (like the NL box). Select-only: phrases/tokens are read-only
                        # because editing feedback tokens breaks the system-managed nl_context.
                        # Every entry, with its stable key and display fields.
                        all_entries = []
                        for e in fb_entries:
                            all_entries.append({
                                "key":  feedback_key(e["model"], e["type"], e["phrase"]),
                                "Type": type_label.get(e["type"], e["type"] or "Other"),
                                "Feedback": e["phrase"] or "(unnamed)",
                                "Maps to columns": e.get("tokens") or "",
                                "Model": e["model"]})
                        col_order = (["Promote", "Type", "Feedback", "Maps to columns"]
                                     + (["Model"] if multi_model else []))
                        cfg = {
                            "Promote": st.column_config.CheckboxColumn("Promote", width="small"),
                            "Type": st.column_config.TextColumn("Type", disabled=True, width="small"),
                            "Feedback": st.column_config.TextColumn("Feedback", disabled=True, width="large"),
                            "Maps to columns": st.column_config.TextColumn("Maps to columns", disabled=True),
                        }
                        if multi_model:
                            cfg["Model"] = st.column_config.TextColumn("Model", disabled=True)
                        # expanded=True so ticking a row (which reruns) no longer collapses the picker.
                        with st.expander(
                                f"Choose feedback to promote "
                                f"({len(prev_sel)} of {len(fb_entries)} selected)", expanded=True):
                            # ── search + type filter (narrow a long list) ──
                            _fc1, _fc2 = st.columns([3, 1])
                            with _fc1:
                                fb_q = st.text_input(
                                    "Search feedback", key="fb_search",
                                    placeholder="filter by phrase or mapped column").strip().lower()
                            with _fc2:
                                fb_type = st.selectbox(
                                    "Type", ["All", "Reference question", "Business term"],
                                    key="fb_type_filter")
                            st.caption("One row = one reference question / business term. Tick "
                                       "**Promote** to carry it over; **Maps to columns** shows the "
                                       "columns each one references. Filtering never changes rows you "
                                       "can't see — their selection is kept.")

                            def _match(r):
                                if fb_type != "All" and r["Type"] != fb_type:
                                    return False
                                if fb_q and fb_q not in (str(r["Feedback"]) + " "
                                                         + str(r["Maps to columns"])).lower():
                                    return False
                                return True
                            shown = [r for r in all_entries if _match(r)]
                            shown_keys = [r["key"] for r in shown]
                            if not shown:
                                st.caption("No feedback matches the filter.")
                                shown_sel = set()
                            else:
                                grid_rows = [{"Promote": (r["key"] in prev_sel), "Type": r["Type"],
                                              "Feedback": r["Feedback"],
                                              "Maps to columns": r["Maps to columns"],
                                              **({"Model": r["Model"]} if multi_model else {})}
                                             for r in shown]
                                # Widget key includes the filter so state resets cleanly when the
                                # filter changes (no stale edits mapped to the wrong rows).
                                _fsig = f"{fb_q}|{fb_type}|{len(shown)}"
                                grid = st.data_editor(
                                    pd.DataFrame(grid_rows)[col_order], key=f"fb_picker_{_fsig}",
                                    hide_index=True, use_container_width=True, num_rows="fixed",
                                    column_config=cfg)
                                shown_sel = {shown_keys[i] for i, p in
                                             enumerate(grid["Promote"].tolist()) if bool(p)}
                        # Replace selection only for the rows currently shown; keep the rest as-is.
                        st.session_state.feedback_selected = (prev_sel - set(shown_keys)) | shown_sel

                # NL (Spotter coaching) instructions — separate artifact, promoted via the
                # ai/instructions API at import (not TML). Persist the toggle like feedback.
                if "include_nl" not in st.session_state:
                    st.session_state["include_nl"] = st.session_state.get("_include_nl", False)
                inc_nl = st.checkbox(
                    "Include Spotter instructions (model coaching)", key="include_nl",
                    help="Also promote each model's NL instructions (model-level Spotter coaching), "
                         "via the ai/instructions API. Separate from feedback; needs Spotter 10.15+.")
                st.session_state["_include_nl"] = inc_nl
                if inc_nl:
                    # Load the source models' instructions once per model set, then show an editable
                    # box (like the feedback picker) so the operator can edit/add/remove before
                    # promoting. The edited text is what gets promoted at the import gate.
                    nl_set_key = tuple(dep["model_ids"])
                    if st.session_state.get("_nl_loaded_key") != nl_set_key:
                        with st.spinner("Loading Spotter instructions…"):
                            st.session_state._nl_src = {
                                g: source_client().get_nl_instructions(g) for g in dep["model_ids"]}
                        st.session_state._nl_loaded_key = nl_set_key
                        for _wk in [k for k in list(st.session_state.keys()) if k.startswith("nl_edit_")]:
                            del st.session_state[_wk]   # drop stale editors for a new model set
                    nl_src = st.session_state.get("_nl_src", {})
                    total  = sum(len(v) for v in nl_src.values())
                    if not total:
                        st.caption("No Spotter instructions found on the selected model(s).")
                        st.session_state._nl_edited = {}
                    else:
                        # READ-ONLY: instructions are promoted exactly as they are on the source.
                        # Editing them here was risky (drift from the cluster's own coaching); to
                        # change them, edit the model's Spotter instructions back in the source
                        # cluster and re-fetch. expanded=True so it doesn't collapse on rerun.
                        edited = {}
                        with st.expander(f"Spotter instructions ({total} found) — read-only",
                                         expanded=True):
                            st.caption("These are promoted **exactly as they appear on the source** "
                                       "(Merge or Replace at the import gate). To change them, edit "
                                       "the model's Spotter instructions in the **source cluster**, "
                                       "then re-fetch.")
                            models_with = [g for g in dep["model_ids"] if nl_src.get(g)]
                            for g in models_with:
                                if len(models_with) > 1:      # label only when several models (like feedback)
                                    st.markdown(f"**{id2name.get(g, g)}**")
                                st.dataframe(
                                    _sno(pd.DataFrame({"Instruction": nl_src.get(g, [])})),
                                    hide_index=True, use_container_width=True,
                                    column_config={"Instruction": st.column_config.TextColumn(
                                        "Instruction", width="large")})
                                edited[g] = [s for s in (str(v).strip() for v in nl_src.get(g, [])) if s]
                        st.session_state._nl_edited = edited
                        st.session_state.pop("_nl_previews", None)   # reflect edits at the gate

            OPT_CREATE   = "Promote tables (create / update on target)"
            OPT_EXISTING = "Use existing target tables only (don't create)"
            tmode = st.radio(
                "Table handling", [OPT_CREATE, OPT_EXISTING], key="tables_mode", horizontal=True,
                help="Existing-only ships no table TML — the model binds to tables already on the "
                     "target. Any referenced table missing from the target must then be pruned out "
                     "of the model (you'll be shown what that drops).")
            # Mode change flips the default include state of every table, so reset their widgets.
            if st.session_state.get("_tables_mode_prev") != tmode:
                # Re-seed the promotion table (its defaults depend on the mode) and clear the
                # per-table prune acknowledgements.
                st.session_state.pop("_promo_seed_sig", None)
                for _i in dep["table_ids"]:
                    st.session_state.pop(f"ackprune_{_i}", None)
                st.session_state._tables_mode_prev = tmode
            tables_default_include = (tmode == OPT_CREATE)

            import pandas as pd
            if dep["leaf_ids"]:
                st.caption("Always promoted: "
                           + ", ".join(f"`{id2name.get(i, i)}`" for i in dep["leaf_ids"]))

            # ONE table for models + tables instead of 30-odd stacked checkboxes. Streamlit's
            # data_editor cannot style cells, so "colour" is a coloured marker in the text — which
            # also survives copy/paste and screen readers. Semantics are unchanged from the
            # checkbox list: ticked = promote; the meaning of LEAVING one out is spelled out per
            # row rather than left to be remembered.
            _promo_ids = list(dep["model_ids"]) + list(dep["table_ids"])
            _seed_sig = (tuple(_promo_ids), tables_default_include)
            if st.session_state.get("_promo_seed_sig") != _seed_sig:
                # Default: models in, tables per the handling mode. Matches the old widget defaults.
                st.session_state.promo_selected = (
                    set(dep["model_ids"])
                    | (set(dep["table_ids"]) if tables_default_include else set()))
                st.session_state._promo_seed_sig = _seed_sig
                _bump_editor("promoset")
            _psel = st.session_state.setdefault("promo_selected", set(_promo_ids))

            _rows_ps = []
            for _kind, _ids in (("model", dep["model_ids"]), ("table", dep["table_ids"])):
                for i in _ids:
                    nm = id2name.get(i, i)
                    on_tgt = nm in present
                    if _kind == "model":
                        _out = ("binds to the target's copy" if on_tgt
                                else "⚠ can't be left out — not on target")
                    else:
                        _out = ("binds to the target's copy, nothing dropped" if on_tgt
                                else "pruned out of the model (you'll see what drops)")
                    _rows_ps.append({
                        "#": len(_rows_ps) + 1,
                        "Object": nm,
                        "Kind": _kind,
                        "On target": "🟢 yes" if on_tgt else "🔴 no",
                        "Promote?": i in _psel,
                        "If left out": _out,
                        "_scoped": i})
            _psdf = pd.DataFrame(_rows_ps, columns=["#", "Object", "Kind", "On target",
                                                    "Promote?", "If left out", "_scoped"])
            _n_on  = sum(1 for r in _rows_ps if r["On target"].endswith("yes"))
            st.markdown(f"**{len(_rows_ps)} object(s)** · 🟢 **{_n_on}** already on target · "
                        f"🔴 **{len(_rows_ps) - _n_on}** not on target")
            _pq = st.text_input("Filter promotion set", key="promoset_search",
                                label_visibility="collapsed",
                                placeholder="🔎 Filter by name").strip().lower()
            _pv = _psdf
            if _pq and not _psdf.empty:
                _pv = _psdf[_psdf["Object"].str.lower().str.contains(_pq, regex=False)]
            _pb1, _pb2, _pb3, _ = st.columns([1.1, 1.1, 1.3, 2])
            _eb_ps = f"promoset::{_pq}"
            with _pb1:
                if st.button(f"Tick shown ({len(_pv)})", key="ps_all", use_container_width=True,
                             disabled=_pv.empty):
                    _psel.update(_pv["_scoped"].tolist()); _bump_editor(_eb_ps); st.rerun()
            with _pb2:
                if st.button("Untick shown", key="ps_none", use_container_width=True,
                             disabled=_pv.empty):
                    for _s in _pv["_scoped"].tolist():
                        _psel.discard(_s)
                    _bump_editor(_eb_ps); st.rerun()
            with _pb3:
                if st.button("Only what's missing", key="ps_missing", use_container_width=True,
                             help="Promote just the objects that are NOT on the target yet."):
                    _psel.clear()
                    _psel.update(r["_scoped"] for r in _rows_ps if r["On target"].endswith("no"))
                    _bump_editor(_eb_ps); st.rerun()
            _select_editor(
                _pv, ["Promote?"], ["promo_selected"], _eb_ps,
                column_config={
                    "#":          st.column_config.TextColumn("#", width="small"),
                    "Object":     st.column_config.TextColumn("Object", width="large"),
                    "Kind":       st.column_config.TextColumn("Kind", width="small"),
                    "On target":  st.column_config.TextColumn(
                                    "On target", width="small",
                                    help="🟢 already exists on the target (names are preserved "
                                         "across clusters) · 🔴 not there yet"),
                    "Promote?":   st.column_config.CheckboxColumn(
                                    "Promote?", width="small",
                                    help="Ticked = included in this promotion."),
                    "If left out": st.column_config.TextColumn("If left out", width="large"),
                },
                disabled=["#", "Object", "Kind", "On target", "If left out"])
            if _pq:
                st.caption(f"{len(_pv)} of {len(_psdf)} shown. Filtering never changes rows you "
                           "can't see.")

            # Derive the same state the checkbox list produced. Unchanged rules: a model can only
            # be left out when it is already on the target; an unticked table either binds to the
            # target's copy (safe) or must be pruned from the model (gated below).
            _plan = promotion_plan(dep["model_ids"], dep["table_ids"], id2name, present, _psel)
            excluded.clear(); excluded.update(_plan["excluded"])
            unsafe.extend(_plan["unsafe"])
            safe_skips = _plan["safe_skips"]     # on target -> model binds to that copy, no drops
            for _nm in _plan["safe_skips"] + [n for n in _plan["prune"]]:
                prune.discard(_nm)               # the gate below re-adds only what gets acked
            # not-on-target tables left out -> pruned via the ONE acknowledgement gate below
            pending_prune = [(nm, table_drop_preview(promo_items, nm)) for nm in _plan["prune"]]

            # ONE gate for every not-on-target table being pruned: list all removals at once,
            # then a single explicit acknowledgement BUTTON (deliberately not a checkbox, so it
            # does not look like the selection ticks above).
            if pending_prune:
                sig   = frozenset(nm for nm, _ in pending_prune)
                acked = st.session_state.get("prune_ack_sig") == sig
                st.divider()
                st.markdown(f"##### Dropping {len(pending_prune)} table(s) from the model")
                st.caption("These tables are not on the target, so they will be pruned out of the "
                           "model on promotion. Expand a table to see exactly what is removed, then "
                           "acknowledge once.")
                for nm, pv in pending_prune:
                    counts = []
                    if pv["columns"]:  counts.append(f"{len(pv['columns'])} column(s)")
                    if pv["joins"]:    counts.append(f"{len(pv['joins'])} join(s)")
                    if pv["formulas"]: counts.append(f"{len(pv['formulas'])} formula(s)")
                    if pv["vizzes"]:   counts.append(f"{len(pv['vizzes'])} viz(s)")
                    head = f"`{nm}` — removes " + (", ".join(counts) if counts else "nothing else (clean)")
                    with st.expander(head, expanded=False):
                        if pv["columns"]:
                            st.markdown("**Columns:** " + ", ".join(f"`{c}`" for c in pv["columns"]))
                        if pv["joins"]:
                            st.markdown("**Joins:** " + ", ".join(pv["joins"]))
                        if pv["formulas"]:
                            st.markdown("**Formulas:** " + ", ".join(pv["formulas"]))
                        if pv["vizzes"]:
                            st.markdown("**Visualizations:** " + ", ".join(str(v) for v in pv["vizzes"]))
                        if not counts:
                            st.caption("Nothing else in the promotion depends on it — clean removal.")
                if acked:
                    for nm, _ in pending_prune:
                        prune.add(nm)
                    st.success(f"Acknowledged — {len(pending_prune)} table(s) will be dropped from the model.")
                else:
                    for nm, _ in pending_prune:
                        unsafe.append(nm)
                    if st.button(f"Acknowledge and drop {len(pending_prune)} table(s) from the model",
                                 type="primary", key="ack_prune_all"):
                        st.session_state.prune_ack_sig = sig
                        st.rerun()
            else:
                st.session_state.pop("prune_ack_sig", None)

            if safe_skips:
                st.caption("Left out but already on the target — the model binds to the target's "
                           "copy, nothing is dropped: " + ", ".join(f"`{n}`" for n in safe_skips))

            st.session_state.excluded     = excluded
            st.session_state.prune_tables = prune

            order = dep["leaf_ids"] + dep["model_ids"] + dep["table_ids"]
            included = [i for i in order if i not in excluded]
            st.session_state.selected_ids = included

            missing = dep["missing_models"] + dep["missing_tables"]
            if missing:
                st.warning("Unresolved on the source cluster: "
                           + ", ".join(f"`{n}`" for n in missing))
            if unsafe:
                st.error("Left out but not on the target: "
                         + ", ".join(f"`{n}`" for n in unsafe)
                         + ". Re-include each, acknowledge the drop above (tables), or add it to the target first.")
            n_mod = len([i for i in dep["model_ids"] if i not in excluded])
            n_tbl = len([i for i in dep["table_ids"] if i not in excluded])
            msg = (f"Promoting {len(included)} object(s): {len(dep['leaf_ids'])} leaf, "
                   f"{n_mod} model(s), {n_tbl} table(s).")
            if prune:
                msg += f" Pruning {len(prune)} table(s) out of the model."
            st.success(msg)
        else:
            st.session_state.selected_ids = []

    can_next = bool(st.session_state.get("selected_ids")) and not unsafe
    if unsafe:
        hint = ("Some excluded objects are missing on the target — re-include them, "
                "acknowledge the drop, or add them to the target first.")
    elif not st.session_state.get("selected_ids"):
        hint = "Select at least one asset to promote."
    else:
        hint = ""
    _nav(0, can_next=can_next, next_hint=hint)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — obj_id Setup
# ══════════════════════════════════════════════════════════════════════════════

elif step == 1:
    st.subheader("obj_id Health Check")
    st.caption("⏱ Matching tables against the target reads the target cluster; the first run after "
               "a cold Databricks warehouse can take a minute or two — a spinner means it's working.")
    # NOTE: we do NOT clear the export here just for visiting this page — that would throw away
    # Git Operations progress on mere navigation (Home / breadcrumb). Instead, the obj_id/align
    # actions below set `_objids_dirty`, and Git Operations re-exports only when that flag is set
    # (so the exported TML still picks up any real obj_id change).
    st.markdown(
        "Every object being promoted needs `obj_id` set on the **source**. Tables that already "
        "exist on the **target** must share the same `obj_id` (otherwise import duplicates them); "
        "tables absent on the target are created on import with the source `obj_id`."
    )

    selected_ids = st.session_state.get("selected_ids", [])

    if not selected_ids:
        st.info("Select assets in Step 1 first.")
    else:
        if "obj_id_status" not in st.session_state:
            with st.spinner("Exporting TML from the source cluster…"):
                raw   = source_client().export_tml(selected_ids)
                items = raw if isinstance(raw, list) else raw.get("object", [])

                # Part A: object-level obj_ids
                status_rows = []
                for item in items:
                    info     = item.get("info", {})
                    obj_name = info.get("name", "unknown")
                    obj_type = info.get("type", "")
                    doc      = _parse_edoc(item.get("edoc", "{}"))
                    oid      = doc.get("obj_id", "")
                    status_rows.append({
                        "object": obj_name,
                        "type":   obj_type,
                        # No obj_id yet -> pre-fill a suggested name slug (still needs Apply).
                        "obj_id": oid or _name_slug(obj_name),
                        "ok":     bool(oid),   # real state: a suggestion is not yet applied
                    })

                # Part B: obj_id of every logical object (table, model, liveboard, answer) —
                # source value vs target. ANY object that already exists on the target under a
                # DIFFERENT obj_id is DUPLICATED on import (match order obj_id->guid->create), so
                # it must be aligned first. Read obj_id from the object's OWN TML (a model's
                # table-reference does not carry one). Auto obj_ids (Name-<guid>) never match
                # cross-cluster, so this catches auto-vs-auto too, not just missing.
                dev_table_refs = {}   # {table_name: obj_id}
                dev_model_refs = {}   # {model_name: obj_id}
                dev_leaf_refs  = {}   # {leaf_name: (kind, obj_id)}  kind in liveboard|answer
                for item in items:
                    doc = _parse_edoc(item.get("edoc", "{}"))
                    if "table" in doc:
                        tname = doc["table"].get("name")
                        if tname:
                            dev_table_refs[tname] = doc.get("obj_id", "")
                    for mk in ("model", "worksheet"):
                        if mk in doc:
                            mname = doc[mk].get("name")
                            if mname:
                                dev_model_refs[mname] = doc.get("obj_id", "")
                    for lk in ("liveboard", "answer"):
                        if lk in doc:
                            lname = doc[lk].get("name")
                            if lname:
                                dev_leaf_refs[lname] = (lk, doc.get("obj_id", ""))

                def _search_target(mtype):
                    r = target_client()._post(
                        "/api/rest/2.0/metadata/search",
                        {"metadata": [{"type": mtype}], "record_size": 5000})
                    return r if isinstance(r, list) else r.get("metadata", [])

                # metadata/search on LOGICAL_TABLE returns physical tables AND models/worksheets;
                # leaves live under LIVEBOARD / ANSWER. Keep leaf snapshot separate so a table and
                # a liveboard that happen to share a name don't collide.
                prod_by_name = {o.get("metadata_name"): o for o in _search_target("LOGICAL_TABLE")}
                prod_leaf    = {}
                if any(k == "liveboard" for k, _ in dev_leaf_refs.values()):
                    prod_leaf.update({o.get("metadata_name"): o for o in _search_target("LIVEBOARD")})
                if any(k == "answer" for k, _ in dev_leaf_refs.values()):
                    prod_leaf.update({o.get("metadata_name"): o for o in _search_target("ANSWER")})

                # state: aligned | mismatch (exists on target with a different obj_id ->
                # import would duplicate) | create (absent on target -> created on import).
                def _align_state(dev_oid, prod_obj):
                    if prod_obj is None:
                        return "create"
                    if dev_oid and prod_obj.get("metadata_obj_id", "") == dev_oid:
                        return "aligned"
                    return "mismatch"

                def _row(oname, kind, dev_oid, prod_obj):
                    prod_oid = prod_obj.get("metadata_obj_id", "") if prod_obj else None
                    return {
                        "object":        oname,
                        "kind":          kind,
                        "source_obj_id": dev_oid or "NOT SET",
                        "target_obj_id": (prod_oid or "NOT SET") if prod_obj else "WILL CREATE",
                        "state":         _align_state(dev_oid, prod_obj),
                    }

                table_rows = []
                for kind, refs in (("table", dev_table_refs), ("model", dev_model_refs)):
                    for oname, dev_oid in refs.items():
                        table_rows.append(_row(oname, kind, dev_oid, prod_by_name.get(oname)))
                for lname, (lk, dev_oid) in dev_leaf_refs.items():
                    table_rows.append(_row(lname, lk, dev_oid, prod_leaf.get(lname)))

                st.session_state.obj_id_status   = status_rows
                st.session_state._raw_items      = items
                st.session_state.table_alignment = table_rows
                st.session_state.prod_by_name    = prod_by_name
                st.session_state.prod_leaf       = prod_leaf
                st.session_state.dev_table_refs  = dev_table_refs
                st.session_state.dev_model_refs  = dev_model_refs
                st.session_state.dev_leaf_refs   = dev_leaf_refs

        if st.button("Re-check obj_id status"):
            for _k in ("obj_id_status", "_raw_items", "table_alignment", "prod_by_name",
                       "prod_leaf", "dev_table_refs", "dev_model_refs", "dev_leaf_refs"):
                st.session_state.pop(_k, None)
            st.rerun()

    import pandas as pd

    status = st.session_state.get("obj_id_status", [])
    if status:
        st.markdown("#### Selected objects")
        missing_objs = [r for r in status if not r["ok"]]
        if not missing_objs:
            st.success("All selected objects have `obj_id` set.")
        else:
            st.warning(f"{len(missing_objs)} object(s) missing `obj_id` — pre-filled with a suggested "
                       "slug from the name. Edit if needed, then click **Apply** to set them.")

        df_obj = pd.DataFrame(status)[["object", "type", "obj_id", "ok"]]
        df_obj.insert(0, "S.No", range(1, len(df_obj) + 1))
        edited_status = st.data_editor(
            df_obj,
            column_config={
                "S.No":   st.column_config.NumberColumn("S.No", width="small"),
                "object": st.column_config.TextColumn("Object", width="large"),
                "type":   st.column_config.TextColumn("Type",   width="medium"),
                "obj_id": st.column_config.TextColumn("obj_id (edit to set)", width="large"),
                "ok":     st.column_config.CheckboxColumn("Has obj_id", disabled=True),
            },
            disabled=["S.No", "object", "type", "ok"],
            use_container_width=True,
            hide_index=True,
        )

        if st.button("Apply obj_id on the source cluster", type="primary"):
            raw_items = st.session_state._raw_items
            # obj_id on an EXISTING object MUST go through the update-obj-id API — a TML
            # re-import keeps the existing obj_id ("...will be used. Use update API...").
            mappings = []
            for row, item in zip(edited_status.itertuples(), raw_items):
                new_id = str(row.obj_id).strip()
                guid   = (item.get("info") or {}).get("id")
                cur    = _parse_edoc(item.get("edoc", "{}")).get("obj_id", "") or ""
                if new_id and guid and new_id != cur:
                    mappings.append({"identifier": guid, "new_obj_id": new_id})
            if not mappings:
                st.info("No obj_id changes to apply.")
            else:
                try:
                    with st.spinner(f"Setting obj_id on {len(mappings)} source object(s)…"):
                        source_client().update_obj_ids(mappings)
                    st.success(f"obj_id set on {len(mappings)} source object(s).")
                    st.session_state._objids_dirty = True   # export is now stale -> Git Ops re-exports
                    for _k in ("obj_id_status", "_raw_items", "table_alignment", "prod_by_name",
                               "prod_leaf", "dev_table_refs", "dev_model_refs", "dev_leaf_refs"):
                        st.session_state.pop(_k, None)
                    st.rerun()
                except Exception as e:
                    # Same correction as the target-side button: the client already says what
                    # actually went wrong, and a duplicate obj_id is a 500, not a rights problem.
                    st.error(f"**Couldn't set obj_id.**\n\n{e}")

    table_rows = st.session_state.get("table_alignment", [])
    if table_rows:
        st.divider()
        st.markdown("#### obj_id alignment — tables, models, liveboards & answers (source → target)")
        misaligned  = [r for r in table_rows if r["state"] == "mismatch"]
        will_create = [r for r in table_rows if r["state"] == "create"]
        if misaligned:
            st.warning(f"{len(misaligned)} object(s) already exist on the target with a different "
                       "`obj_id` — importing would create DUPLICATES. Fix below before continuing.")
        elif will_create:
            st.info(f"{len(will_create)} object(s) are absent on the target and will be created on "
                    "import with the source `obj_id` (ensure the source `obj_id` is set above).")
        else:
            st.success("All target objects exist and are aligned on `obj_id`.")

        df_tables = pd.DataFrame(table_rows)[["object", "kind", "source_obj_id", "target_obj_id", "state"]]
        st.dataframe(
            _sno(df_tables),
            column_config={"state": st.column_config.TextColumn("State")},
            use_container_width=True,
            hide_index=True,
        )

        if misaligned:
            if st.button("Fix target obj_ids", type="primary"):
                prod_by_name   = st.session_state.get("prod_by_name", {})
                prod_leaf      = st.session_state.get("prod_leaf", {})
                dev_table_refs = st.session_state.get("dev_table_refs", {})
                dev_model_refs = st.session_state.get("dev_model_refs", {})
                dev_leaf_refs  = st.session_state.get("dev_leaf_refs", {})
                to_fix, not_found = [], []
                for r in misaligned:
                    oname, kind = r["object"], r["kind"]
                    if kind == "table":
                        dev_oid, prod_obj = dev_table_refs.get(oname, ""), prod_by_name.get(oname)
                    elif kind == "model":
                        dev_oid, prod_obj = dev_model_refs.get(oname, ""), prod_by_name.get(oname)
                    else:   # liveboard | answer
                        dev_oid  = (dev_leaf_refs.get(oname) or ("", ""))[1]
                        prod_obj = prod_leaf.get(oname)
                    if not dev_oid:
                        continue
                    if not prod_obj:
                        not_found.append(oname)
                        continue
                    to_fix.append({
                        "guid":   prod_obj.get("metadata_id"),
                        "name":   oname,
                        "obj_id": dev_oid,
                    })

                if not_found:
                    st.error(f"Objects not found on the target cluster — import from its connection first: {', '.join(not_found)}")

                if to_fix:
                    # set obj_id via the update-obj-id API (a TML re-import won't change it)
                    mappings = [{"identifier": t["guid"], "new_obj_id": t["obj_id"]} for t in to_fix]
                    try:
                        with st.spinner(f"Setting obj_id on {len(to_fix)} target object(s)…"):
                            target_client().update_obj_ids(mappings)
                        st.success("obj_id set on target: "
                                   + ", ".join(f"`{t['name']}`→`{t['obj_id']}`" for t in to_fix))
                        st.session_state._objids_dirty = True   # export is now stale
                        for _k in ("obj_id_status", "_raw_items", "table_alignment",
                                   "prod_by_name", "prod_leaf", "dev_table_refs",
                                   "dev_model_refs", "dev_leaf_refs"):
                            st.session_state.pop(_k, None)
                        st.rerun()
                    except Exception as e:
                        # The client names the offending object and the real reason. Do not guess
                        # at privileges here: a duplicate obj_id returns 500, not 403, and telling
                        # the operator to go check rights sends them somewhere with nothing wrong.
                        st.error(f"**Couldn't set obj_id.**\n\n{e}")

    all_ok = (
        bool(status) and not [r for r in status if not r["ok"]]
        and not [r for r in table_rows if r["state"] == "mismatch"]
    )
    if not status:
        nav_hint = "Resolve obj_id setup for the selected assets first."
    elif [r for r in status if not r["ok"]]:
        nav_hint = "Some objects still need an obj_id assigned before you can continue."
    elif [r for r in table_rows if r["state"] == "mismatch"]:
        nav_hint = "Resolve the table match/mismatch(es) above before continuing."
    else:
        nav_hint = ""
    _nav(1, can_next=all_ok, next_hint=nav_hint)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2b — Source Audit (fix source drift BEFORE promoting to the target)
# ══════════════════════════════════════════════════════════════════════════════

elif step == 2:
    st.subheader("Source Audit")
    st.caption("Audit the promotion against the **source** warehouse first — drop columns the source "
               "no longer has, realign or drop columns whose type drifted from the source, recase to "
               "the source's casing — so the TML is source-faithful before it's validated against the "
               "target on the next step. (The target check on TML Validation confirms a realigned "
               "type actually binds.)")

    # Build the promotion bundle on entry — the TML has to exist before we can audit it against
    # the source. Idempotent (guarded by _need_export inside), and shared with TML Validation, so
    # walking Source Audit → TML Validation does NOT re-export.
    _prepare_bundle()
    selected_ids = st.session_state.get("selected_ids", [])
    transformed_items = st.session_state.get("transformed_items")

    if not selected_ids:
        st.info("Select assets in Step 1 first.")
    elif transformed_items is None:
        st.info("Preparing the bundle — if this doesn't clear, check the source connection and retry.")
    else:
        import pandas as pd
        _src_conn = (teams[team_name].get("source_connection", "")
                     or teams[team_name].get("target_connection", ""))

        # Apply approved drops to the bundle and make them DURABLE via skip_columns, so a later
        # re-export (e.g. after applying a recasing) re-drops them instead of silently bringing them
        # back. Prune any table the drop emptied. No target validation here — that's the next page.
        def _apply_src_drops(_dropset, _tag):
            _dropset = set(_dropset)
            if not _dropset:
                return
            fixed, _man = drop_columns(st.session_state.transformed_items, _dropset)
            _record_drop(_man)
            st.session_state.setdefault("skip_columns", set()).update(_dropset)
            st.session_state.setdefault("dropped_col_names", set()).update(_dropset)
            _emptied = {ff["table"] for ff in table_cleanup_findings(fixed)}
            if _emptied:
                fixed, _ts = _prune_tables_whole(fixed, _emptied)
                st.session_state.setdefault("prune_tables", set()).update(_emptied)
            st.session_state.transformed_items = fixed
            st.session_state._source_audit_dirty = True   # leaving 2b re-exports a clean bundle
            _log_apply_detail(_tag, _dropset, _man, _emptied, fixed)
            # Keep the source read (_source_col_map/_source_type_map) — it's a read of the WAREHOUSE,
            # not the bundle, so a drop doesn't stale it. The findings below recompute against the
            # updated bundle, so the resolved rows drop off while the section stays visible (no
            # collapse back to the "Read source warehouse" button).

        # ── Read the SOURCE warehouse (columns, casing, types) in one pass ──
        _sh, _sb = st.columns([3, 2])
        with _sh:
            st.markdown("**Source warehouse audit**")
            if st.session_state.get("_source_col_map"):
                st.caption(f"✅ Read {len(st.session_state['_source_col_map'])} source table(s). "
                           "Absent columns and type drift are flagged below — nothing changes until "
                           "you approve and apply. Casing is handled by the recasing panel below.")
            else:
                st.caption("Read the **source** warehouse to check the promoted TML against it: "
                           "columns the source no longer has, type drift, and casing. Everything is "
                           "flagged for approval — nothing changes automatically.")
        with _sb:
            if st.button("🔌 Read source warehouse", disabled=not transformed_items,
                         help="Reads the source warehouse (source DBX creds, falling back to target "
                              "for now) and diffs the promotion: absent columns, casing, types."):
                with st.status("Reading the source warehouse (columns, casing, types)… a cold "
                               "warehouse can take a minute or two.", expanded=True) as _ss:
                    _sm  = _read_source_col_map()     # names + casing (SHOW COLUMNS)
                    _ss.write(f"columns/casing: {len(_sm)} table(s)")
                    _stm = _read_source_type_map()    # types (DESCRIBE)
                    _ss.write(f"types: {len(_stm)} table(s)")
                    # Stay expanded when the read came back empty, so the "0 table(s)" outcome is
                    # visible instead of silently collapsing to nothing.
                    _ss.update(label=f"Source warehouse read — {len(_sm)} table(s).",
                               state=("complete" if _sm else "error"), expanded=not bool(_sm))
                st.session_state._source_col_map  = _sm
                st.session_state._source_type_map = _stm
                st.rerun()

        _src_ran = "_source_col_map" in st.session_state    # a read has happened this session
        _src_map = st.session_state.get("_source_col_map") or {}
        if not _src_map:
            if not _src_ran:
                st.caption("The source read hasn't run yet — click **Read source warehouse** to surface "
                           "columns the source no longer has, type drift, and casing that needs recasing.")
            else:
                # Ran, but came back empty — say WHY instead of reverting to "hasn't run yet".
                _has_dbx = bool((opt_env("TS_SOURCE_DBX_HOST") or opt_env("TS_TARGET_DBX_HOST"))
                                and (opt_env("TS_SOURCE_DBX_WAREHOUSE") or opt_env("TS_TARGET_DBX_WAREHOUSE"))
                                and (opt_env("TS_SOURCE_DBX_TOKEN") or opt_env("TS_TARGET_DBX_TOKEN")))
                if not _has_dbx:
                    st.error("The source read came back with **0 tables** — no direct Databricks creds are "
                             "set, so it fell back to the connection's column read, which times out on "
                             "hive_metastore. Add `TS_TARGET_DBX_HOST`, `TS_TARGET_DBX_WAREHOUSE`, and "
                             "`TS_TARGET_DBX_TOKEN` to `.env` (the same creds the rest of the audit uses), "
                             "then read again.")
                else:
                    st.error("The source read came back with **0 tables** even though Databricks creds are "
                             "set. The SQL warehouse may be cold or stopped (retry), or the promoted "
                             "tables' db / schema / db_table don't resolve in that warehouse. Retry; if it "
                             "persists, confirm the warehouse is running and the table coordinates match.")
        else:
            # (A) columns absent from the SOURCE warehouse → approve-drop (out-of-sync TML) ──
            _src_missing = warehouse_missing_findings(
                st.session_state.get("transformed_items", []), _src_map, connection=_src_conn)
            for _f in _src_missing:
                _f["kind"] = "missing_in_source_warehouse"
            if not _src_missing:
                st.caption("✔ No promoted column is missing from the source warehouse.")
            else:
                st.warning(f"{len(_src_missing)} promoted column(s) are **absent from the source "
                           "warehouse** (out-of-sync TML). Tick to drop — nothing drops until you Apply.")
                _ssel = st.session_state.setdefault("src_drop_selected", set())
                _srows = []
                for _f in sorted(_src_missing, key=lambda x: ((x.get("object") or "").lower(),
                                                              (x.get("column") or "").lower())):
                    _tbl = _f.get("object") or "(table)"
                    _sk  = f"{_tbl}::{_f['column']}" if _f.get("object") else _f["column"]
                    _srows.append({"Table": _tbl, "Column": _f["column"],
                                   "Drop?": _sk in _ssel, "_scoped": _sk})
                _sdf = pd.DataFrame(_srows, columns=["Table", "Column", "Drop?", "_scoped"])
                _ba, _bc, _ = st.columns([1, 1, 3])
                with _ba:
                    if st.button(f"Drop all ({len(_sdf)})", key="srcabsent_all",
                                 use_container_width=True):
                        _ssel.update(_sdf["_scoped"].tolist()); _bump_editor("srcabsent"); st.rerun()
                with _bc:
                    if st.button("Clear all", key="srcabsent_clear", disabled=not _ssel,
                                 use_container_width=True):
                        _ssel.clear(); _bump_editor("srcabsent"); st.rerun()
                _select_editor(
                    _sdf, ["Drop?"], ["src_drop_selected"], "srcabsent",
                    column_config={
                        "Table":  st.column_config.TextColumn("Table", width="medium"),
                        "Column": st.column_config.TextColumn("Column", width="medium"),
                        "Drop?":  st.column_config.CheckboxColumn("Drop?", width="small",
                                    help="Tick to drop this out-of-sync column from the promotion."),
                    },
                    disabled=["Table", "Column"])
                st.caption(f"**{len(_ssel)}** source-absent column(s) approved to drop.")
                if st.button("Apply source drops", disabled=not _ssel, key="srcabsent_apply"):
                    _apply_src_drops(_ssel, "source_missing_apply")
                    _ssel.clear()
                    st.rerun()

            # (B) type differs from the SOURCE warehouse → realign to source, or drop ──
            # Prefer REALIGN: the TML is usually just stale (e.g. an ID column exported as VARCHAR
            # while the warehouse has it as bigint) — rewrite the TML type to the source warehouse's
            # type token (INT64, never the raw 'bigint'). The target check on TML Validation confirms
            # the realigned type binds; if the two warehouses genuinely disagree, it surfaces there.
            _src_type = st.session_state.get("_source_type_map") or {}
            _gone = st.session_state.get("dropped_col_names", set())
            _stfind = [f for f in warehouse_type_findings(
                           st.session_state.get("transformed_items", []), _src_type, connection=_src_conn)
                       if f"{f['object']}::{f['column']}" not in _gone and f["column"] not in _gone]
            if _stfind:
                st.markdown("**Type differs from the source warehouse**")
                st.caption("The TML type disagrees with the source warehouse. **Realign** rewrites the "
                           "TML type to match the source (preferred when the TML is just stale — the "
                           "HCP_ID case); **drop** only as a last resort. Nothing changes until you Apply.")
                _stsel  = st.session_state.setdefault("src_type_drop_selected", set())
                _strsel = st.session_state.setdefault("src_type_realign_selected", set())
                _src_realign_to = {}   # scoped key -> TS token, from the SOURCE warehouse type
                _strows = []
                for _f in sorted(_stfind, key=lambda x: ((x.get("object") or "").lower(),
                                                         (x.get("column") or "").lower())):
                    _tbl = _f["object"]; _sk = f"{_tbl}::{_f['column']}"
                    _scdw = (_src_type.get(_tbl.strip().lower()) or {}).get(_f["column"].lower(), "")
                    _tml  = _f.get("source_type") or ""
                    # Realign only when the source maps to a real token that differs from the TML.
                    # VOID/unknown source (no token) → drop only.
                    _tok  = warehouse_type_to_ts(_scdw) if _scdw else ""
                    _ral  = _tok if (_tok and _tok.strip().lower() != _tml.strip().lower()) else ""
                    if _ral:
                        _src_realign_to[_sk] = _ral
                    _strows.append({"Table": _tbl, "Column": _f["column"],
                                    "Source type": (_scdw.upper() if _scdw else "(?)"),
                                    "TML type": _tml or "(?)",
                                    "Realign to": _ral if _ral else "—",
                                    "Realign?": _sk in _strsel,
                                    "Drop?": _sk in _stsel, "_scoped": _sk})
                st.session_state._src_realign_to = _src_realign_to
                _stdf = pd.DataFrame(_strows, columns=["Table", "Column", "Source type", "TML type",
                                                       "Realign to", "Realign?", "Drop?", "_scoped"])
                _realignable = [r["_scoped"] for _, r in _stdf.iterrows()
                                if r.get("Realign to") not in ("—", "", None)]
                _stb1, _stb2, _stb3, _ = st.columns([1.3, 1, 1, 2])
                with _stb1:
                    if st.button(f"Realign all ({len(_realignable)})", key="srctype_re_all",
                                 use_container_width=True, disabled=not _realignable):
                        _strsel.update(_realignable)
                        for _sk in _realignable:
                            _stsel.discard(_sk)
                        _bump_editor("srctype"); st.rerun()
                with _stb2:
                    if st.button(f"Drop rest ({len(set(_stdf['_scoped']) - set(_realignable))})",
                                 key="srctype_drop_all", use_container_width=True, disabled=_stdf.empty):
                        for _sk in _stdf["_scoped"].tolist():
                            if _sk not in _strsel:
                                _stsel.add(_sk)
                        _bump_editor("srctype"); st.rerun()
                with _stb3:
                    if st.button("Clear all", key="srctype_clear", use_container_width=True,
                                 disabled=not (_stsel or _strsel)):
                        _stsel.clear(); _strsel.clear(); _bump_editor("srctype"); st.rerun()
                _select_editor(
                    _stdf, ["Realign?", "Drop?"], ["src_type_realign_selected", "src_type_drop_selected"],
                    "srctype",
                    column_config={
                        "Table":  st.column_config.TextColumn("Table", width="medium"),
                        "Column": st.column_config.TextColumn("Column", width="medium"),
                        "Source type": st.column_config.TextColumn("Source type", width="small"),
                        "TML type": st.column_config.TextColumn("TML type", width="small"),
                        "Realign to": st.column_config.TextColumn("Realign to", width="small",
                                        help="The TS token a realign writes to the TML, from the source "
                                             "warehouse's type. '—' when the source has no usable type "
                                             "(drop instead)."),
                        "Realign?": st.column_config.CheckboxColumn("Realign?", width="small",
                                      help="Rewrite the TML type to match the source (approve-first)."),
                        "Drop?":  st.column_config.CheckboxColumn("Drop?", width="small",
                                    help="Last resort: drop this column (scoped to this table)."),
                    },
                    disabled=["Table", "Column", "Source type", "TML type", "Realign to"],
                    exclusive=True)   # Realign? wins over Drop? per row
                st.caption(f"**{len(_strsel)}** to realign, **{len(_stsel)}** to drop.")
                if (_stsel or _strsel) and st.button("Apply source type fixes", key="srctype_apply"):
                    _items = st.session_state.transformed_items
                    _re   = {k: _src_realign_to[k] for k in _strsel if _src_realign_to.get(k)}
                    _drop = {k for k in _stsel if k not in _strsel}   # compute before any clearing
                    if _re:
                        _items, _rn = realign_column_types(_items, _re)
                        st.session_state.setdefault("realign_types", {}).update(_re)   # durable
                        st.session_state._source_audit_dirty = True
                    st.session_state.transformed_items = _items
                    _strsel.clear()
                    if _drop:
                        _apply_src_drops(_drop, "source_type_apply")   # durable drop + prune (sets dirty)
                    _stsel.clear()
                    st.rerun()

            # (C) casing differs from the SOURCE warehouse → approve-first recasing ──
            # Same source read as (A)/(B): recasings come from _src_map (the live SHOW COLUMNS), so all
            # three checks are surfaced by one "Read source warehouse". Approve-first (no silent
            # mutations); "Apply recasings" rewrites db_column_name IN PLACE (no re-export) so the row
            # resolves immediately, exactly like the drop/realign buttons. The single re-export happens
            # only when you leave this page (see the nav button below).
            _rapp = st.session_state.setdefault("recase_approved", set())
            _case_rows = []
            for _it in st.session_state.get("transformed_items", []):
                _t = _parse_edoc(_it.get("edoc", "{}")).get("table")
                if not _t or not _t.get("name"):
                    continue
                _cm = _src_map.get(_t["name"].strip().lower())
                if not _cm:
                    continue
                for _c in _t.get("columns", []) or []:
                    _dbn = (_c.get("db_column_name") or "").strip()
                    if _dbn and _cm.get(_dbn.lower()) and _cm[_dbn.lower()] != _dbn:
                        _sk = f"{_t['name'].strip().lower()}::{_dbn.lower()}"
                        _case_rows.append({"Table": _t["name"], "From": _dbn,
                                           "To": _cm[_dbn.lower()],
                                           "Approve?": _sk in _rapp, "_scoped": _sk})
            if not _case_rows:
                st.caption("✔ No promoted column's casing differs from the source warehouse.")
            else:
                # approved rows still showing = approved but not yet applied (in-place apply resolves
                # a row, so it leaves this list once done).
                _approved_here = [r["_scoped"] for r in _case_rows if r["_scoped"] in _rapp]
                st.markdown("**Casing differs from the source warehouse**"
                            + (f"  ·  ⚠ {len(_approved_here)} approved, not yet applied"
                               if _approved_here else ""))
                st.caption("Approve a recasing to align the promoted `db_column_name` to the source "
                           "warehouse's **actual** casing (upper/lower/mixed — read live, never "
                           "assumed). Nothing is recased until you approve and Apply; an unapproved "
                           "column keeps its source casing and may fail to bind on import.")
                _rdf = pd.DataFrame(_case_rows, columns=["Table", "From", "To", "Approve?", "_scoped"])
                _rba, _rbc = st.columns([1, 1])
                with _rba:
                    if st.button(f"Approve all ({len(_rdf)})", key="recase_all",
                                 use_container_width=True):
                        _rapp.update(_rdf["_scoped"].tolist()); _bump_editor("recase"); st.rerun()
                with _rbc:
                    if st.button("Clear all", key="recase_clear", disabled=not _rapp,
                                 use_container_width=True):
                        _rapp.clear(); _bump_editor("recase"); st.rerun()
                _select_editor(
                    _rdf, ["Approve?"], ["recase_approved"], "recase",
                    column_config={
                        "Table":    st.column_config.TextColumn("Table", width="medium"),
                        "From":     st.column_config.TextColumn("From (source TML)", width="medium"),
                        "To":       st.column_config.TextColumn("To (source warehouse)", width="medium"),
                        "Approve?": st.column_config.CheckboxColumn("Approve?", width="small",
                                      help="Tick to recase this column to the source warehouse casing."),
                    },
                    disabled=["Table", "From", "To"])
                if _approved_here:
                    st.warning(f"{len(_approved_here)} recasing(s) approved but not yet applied.")
                    if st.button("Apply recasings", type="primary", key="recase_apply"):
                        # In-place: rewrite db_column_name for the approved subset of the source casing
                        # map. recase_approved stays (durable), so the final re-export reproduces it.
                        _applied_map = {_t: {_c: _case for _c, _case in (_cols or {}).items()
                                             if f"{_t}::{_c}" in _rapp}
                                        for _t, _cols in _src_map.items()}
                        _applied_map = {_t: _sub for _t, _sub in _applied_map.items() if _sub}
                        _fixed, _rn = recase_columns(st.session_state.transformed_items, _applied_map)
                        st.session_state.transformed_items = _fixed
                        # Mark approved recasings as applied so the Import Results report lists them
                        # even before the final re-export (the re-export re-asserts the same set).
                        st.session_state.setdefault("_recase_applied_set", set()).update(_rapp)
                        st.session_state._source_audit_dirty = True
                        st.rerun()

    # The ONE re-export: leaving Source Audit rebuilds a clean bundle from source with every approved
    # drop / realign / recasing applied deterministically, then validates THAT. The section buttons
    # above only preview in place — this is the single authoritative re-export. (At 4-space so the nav
    # renders for every branch: no assets, preparing, or ready.)
    _sa_dirty = st.session_state.get("_source_audit_dirty", False)
    _nav(2, can_next=True,
         next_label=("Re-export & continue →" if _sa_dirty else "Continue →"),
         next_reexport=True)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2c — TML Validation (validate the source-audited TML against the TARGET)
# ══════════════════════════════════════════════════════════════════════════════

elif step == 3:
    st.subheader("TML Validation")
    st.caption("⏱ With source drift handled on **Source Audit**, this step validates the TML against "
               "the **target** warehouse and reads its columns. A **cold Databricks SQL warehouse can "
               "take a minute or two** to wake and respond — warm it first for a faster run; a "
               "spinner/status box means it's working, not hung.")

    _prepare_bundle()
    selected_ids = st.session_state.get("selected_ids", [])

    # SELF-HEAL the bundle in place, every time this page loads. _prepare_bundle only runs on a
    # fresh export, so a type this tool rewrote under an older rule survives in the items and keeps
    # producing "DataType is being changed" with no export to correct it. Comparing against the raw
    # source TML restores anything changed without crossing a storage class; a genuine realignment
    # (VARCHAR -> INT64) crosses one and is left alone.
    _bundle = st.session_state.get("transformed_items")
    if _bundle and st.session_state.get("_source_raw_items"):
        _bundle, _restored = restore_unneeded_type_changes(
            _bundle, st.session_state["_source_raw_items"])
        if _restored:
            st.session_state.transformed_items = _bundle
            st.session_state.realign_types = prune_stale_realignments(
                _bundle, st.session_state.get("realign_types") or {})[0]
            st.info("Restored the source's own data type on **" + str(len(_restored)) +
                    "** column(s) that had been retyped without needing it (integer width only): "
                    + ", ".join(f"`{t}.{c}` {a}→{b}" for t, c, a, b in _restored[:6])
                    + (", …" if len(_restored) > 6 else "") + ".")

    transformed_items = st.session_state.get("transformed_items")
    cm = st.session_state.get("conn_mismatch")

    if not selected_ids:
        st.info("Select assets in Step 1 first.")
    elif cm:
        st.error(
            f"Source connection `{cm['configured']}` matches no connection in the exported tables, "
            "so the remap is skipped and import will fail on the target. Connections present: "
            + ", ".join(f"`{n}`" for n in cm["found"])
            + ". Set the source connection in the sidebar to one of those exactly, or blank it.")
        if st.button("Re-export after fixing the connection"):
            for _k in ("transformed_items", "conn_mismatch", "warnings"):
                st.session_state.pop(_k, None)
            st.rerun()
    elif transformed_items is None:
        st.info("Nothing to promote.")
    else:
        warnings = st.session_state.get("warnings", [])
        if warnings:
            st.warning(f"{len(warnings)} transform warning(s): "
                       + "; ".join(f"{w['object']}: {w['issue']}" for w in warnings))
        ps = st.session_state.get("prune_summary")
        if ps and ps.get("tables"):
            st.info(f"Pruned {ps['tables']} table(s) out of the model — dropped "
                    f"{ps['columns']} column(s), {ps['joins']} join(s), {ps['formulas']} formula(s), "
                    f"{ps['vizzes']} viz(s).")
        skip_objects  = st.session_state.get("skip_objects", set())
        filtered_items = [
            i for i in transformed_items
            if i.get("info", {}).get("name") not in skip_objects
        ]

        # The TARGET warehouse is read automatically at export (the direct hive read above), and the
        # discovery step validates against the target as well, so there is no separate "verify against
        # the target warehouse" button anymore — it used the connection/search path that 504s on GSK
        # and only duplicated what already happens. The one explicit warehouse action left is the
        # SOURCE check further down, which is the only warehouse we cannot see any other way.
        if st.session_state.get("_warehouse_col_map"):
            st.caption(f"✅ Target warehouse columns read at export for "
                       f"{len(st.session_state['_warehouse_col_map'])} table(s) — missing-column "
                       "checks below are warehouse-verified.")

        # Table shape at a glance: how many columns the promotion CARRIES vs how many the target's
        # MODEL currently has (the logical table on test). This is the "what changes in the model"
        # view. It is NOT the physical warehouse column count — that set is far larger (a wide fact
        # table can have 100+ physical columns while the model surfaces 70) and only matters for the
        # bind/missing check, which lives in its own section below.
        _tm = st.session_state.get("_target_modeled_map") or {}
        _shape_rows = []
        for i in filtered_items:
            d = _parse_edoc(i.get("edoc", "{}"))
            t = d.get("table")
            if not t or not t.get("name"):
                continue
            src_n = len(_display_cols(t))   # SAME set the skip/drop lists use → counts always agree
            _key  = t["name"].strip().lower()
            modeled = _tm.get(_key)
            tgt_n = len(modeled) if modeled is not None else None
            _shape_rows.append({
                "Table": t["name"],
                "Promoted cols": src_n,
                "Target model cols": tgt_n if tgt_n is not None else "— (new on target)",
                "Model delta": _gap_label(src_n, tgt_n),
            })
        # Per-table serial number, in the SAME order _sno() numbers the count table below, so the
        # dropper and the skip dialog can prefix each table with the number the operator sees here.
        _tbl_serial = {(r["Table"] or "").strip().lower(): idx + 1
                       for idx, r in enumerate(_shape_rows)}
        if _shape_rows:
            with st.expander(f"Table column counts — promoted vs target model ({len(_shape_rows)} table(s))",
                             expanded=False):
                import pandas as pd
                st.caption("**Promoted** is what this promotion carries (after any drops); **target "
                           "model** is the logical table on the target today. **Model delta**: *added "
                           "to target model* → columns the promotion introduces; *in target model, not "
                           "promoted* → columns the target has that the promotion doesn't carry; *new "
                           "on target* → the table doesn't exist on the target yet. This is the model "
                           "change, not the physical warehouse size; columns that can't bind to the "
                           "warehouse are flagged in the sections below.")
                st.dataframe(_sno(pd.DataFrame(_shape_rows)), use_container_width=True, hide_index=True)

        # ── Skip specific columns (leave a column out without touching the rest) ──
        # Same checkbox-table style as the Source Audit drops, and applied IN PLACE (no re-export):
        # ticked columns are dropped straight from the bundle and recorded in skip_columns (durable),
        # so a later re-export re-applies them. Scoped `table::col`, so skipping a shared name only
        # drops it from that one table. Additive — applying only ADDS the ticked columns, so existing
        # skips (and the Source Audit's source-absent drops) are preserved automatically.
        import pandas as pd
        _tbl_cols = {}   # table name -> [column display names]  (shared _display_cols → counts match)
        for i in filtered_items:
            d = _parse_edoc(i.get("edoc", "{}"))
            t = d.get("table")
            if t and t.get("name"):
                _tbl_cols[t["name"]] = _display_cols(t)
        if _tbl_cols:
            _skip_sel = st.session_state.setdefault("skip_selected", set())
            _skgen = st.session_state.get("_skip_gen", 0)                  # bump = reset all editors
            # The WHOLE section collapses, not just each table — 25+ tables otherwise push the
            # buttons below it off the page. Same checkbox-as-disclosure device as the per-table
            # headers: left-aligned natively (a button label is centred and resists restyling) and
            # its own key holds the open/closed state across the reruns that ticking causes, so it
            # never snaps shut mid-selection the way an expander's `expanded` argument does.
            _sec_hdr = (f"**Skip specific columns**  ·  {len(_tbl_cols)} table(s)"
                        + (f"  ·  {len(_skip_sel)} ticked" if _skip_sel else ""))
            _sec_open = st.checkbox(_sec_hdr, key="skipsec_open")
            st.caption("Leave a column out and keep the rest (optional). Expand a table to tick "
                       "its columns.")
            _skq, _any_shown = "", False
            if _sec_open:
                _skq = st.text_input("Filter columns to skip", key="skip_search",
                                     label_visibility="collapsed",
                                     placeholder="🔎 Filter by table or column name").strip().lower()
            for _tn in (sorted(_tbl_cols, key=lambda n: _tbl_serial.get(n.strip().lower(), 1e9))
                        if _sec_open else []):
                _tn_l = _tn.strip().lower()
                _cols = [c for c in sorted(_tbl_cols[_tn])
                         if (not _skq) or (_skq in _tn_l) or (_skq in c.lower())]
                if not _cols:
                    continue
                _any_shown = True
                _sn = _tbl_serial.get(_tn_l)
                _marks = sum(1 for c in _cols if f"{_tn_l}::{c.strip().lower()}" in _skip_sel)
                _hdr = (f"{(str(_sn) + '. ') if _sn else ''}{_tn}  ·  {len(_cols)} column(s)"
                        + (f"  ·  {_marks} to skip" if _marks else ""))
                # A CHECKBOX, not a button: Streamlit centres a button's label and resists being
                # restyled, whereas a checkbox label is left-aligned natively. Its own key also
                # persists the expanded state across the reruns that ticking a column causes.
                _checked = st.checkbox(_hdr, key=f"skexp_{_tn_l}")
                _open = _checked or bool(_skq)             # a filter force-opens matching tables
                if _open:
                    _rows = [{"Column": c, "Skip?": f"{_tn_l}::{c.strip().lower()}" in _skip_sel,
                              "_scoped": f"{_tn_l}::{c.strip().lower()}"} for c in _cols]
                    _df = pd.DataFrame(_rows, columns=["Column", "Skip?", "_scoped"])
                    _seb = f"skipcol::{_tn_l}::{_skq}::{_skgen}"   # global gen in base → Clear resets all
                    if st.button(f"Skip all in this table ({len(_df)})", key=f"skall_{_tn_l}"):
                        _skip_sel.update(_df["_scoped"].tolist())
                        st.session_state._skip_gen = _skgen + 1
                        st.rerun()
                    _select_editor(
                        _df, ["Skip?"], ["skip_selected"], _seb,
                        column_config={
                            "Column": st.column_config.TextColumn("Column", width="large"),
                            "Skip?":  st.column_config.CheckboxColumn("Skip?", width="small",
                                        help="Leave this column out of the promotion (and any viz using it)."),
                        },
                        disabled=["Column"])
            if _sec_open and not _any_shown:
                st.caption("No columns match the filter.")
            # Apply/Clear stay reachable whenever something is ticked, even with the section shut —
            # collapsing must never strand a selection the operator has already made.
            _apply_skip = False
            if _sec_open or _skip_sel:
                _skf1, _skf2, _ = st.columns([1.4, 1, 3])
                with _skf1:
                    _apply_skip = st.button(f"Apply column skips ({len(_skip_sel)})",
                                            key="skip_apply_cols",
                                            disabled=not _skip_sel, use_container_width=True)
                with _skf2:
                    if st.button("Clear all", key="skip_clear", disabled=not _skip_sel,
                                 use_container_width=True):
                        _skip_sel.clear(); st.session_state._skip_gen = _skgen + 1; st.rerun()
            if _apply_skip and _skip_sel:
                _sdrop = set(_skip_sel)
                _fixed, _sman = drop_columns(st.session_state.transformed_items, _sdrop)
                _record_drop(_sman)
                st.session_state.setdefault("skip_columns", set()).update(_sdrop)       # durable
                st.session_state.setdefault("dropped_col_names", set()).update(_sdrop)
                _semptied = {ff["table"] for ff in table_cleanup_findings(_fixed)}
                if _semptied:
                    _fixed, _sts = _prune_tables_whole(_fixed, _semptied)
                    st.session_state.setdefault("prune_tables", set()).update(_semptied)
                st.session_state.transformed_items = _fixed
                _log_apply_detail("skip_columns_apply", _sdrop, _sman, _semptied, _fixed)
                _skip_sel.clear()
                # the bundle changed — invalidate any current validation / discovery
                for _k in ("pr_url", "validation_errors", "validation_ok",
                           "discovered_findings", "discovered_meta"):
                    st.session_state.pop(_k, None)
                st.rerun()


        def _run_validation(items, step=None):
            """VALIDATE-ONLY against the target — NO Git commit and NO PR (those moved to the Git
            Operations step). Returns (None, errors, ok) so callers keep the same 3-tuple shape.
            step: optional callable(str) to report progress to the UI."""
            _tick = step or (lambda _m: None)
            # Any re-export invalidates a partial import in progress — reset the import phase.
            for _k in ("import_phase", "import_core_results", "import_leaf_files", "import_leaf_errors"):
                st.session_state.pop(_k, None)
            files  = items_to_files(items)
            # Validate ONLY this run's files (not the whole team folder — the repo accumulates TML
            # across promotions). Tables first (a missing column / drop-blocked dep surfaces as 14536),
            # then models. Validation runs on the TML strings directly, so no commit is needed.
            val_strings = ([c for p, c in files.items() if p.startswith("tables/")]
                           + [c for p, c in files.items() if p.startswith("models/")])
            if not val_strings:
                return None, [], []
            _tick(f"Validating {len(val_strings)} table/model file(s) against the target…")
            results = target_client().import_tml(val_strings, policy="VALIDATE_ONLY")
            # Record the raw run so consecutive validates are diffable (why did the finding set
            # change?) — persisted to logs/validate_runs.jsonl and kept for the inline expander.
            st.session_state._last_validate = _log_validate(files, results)
            ok  = [r for r in results if r["status"] == "OK"]
            err = [r for r in results if is_blocking_result(r)]
            return None, err, ok

        def _discover_all_issues(items, progress=None):
            """Probe: VALIDATE_ONLY a throwaway COPY, neutralize each pass's issues (drop the
            reported columns / vizzes / invalid-formula columns), and re-validate — looping UNTIL
            the copy validates clean, or a pass makes no progress (can't neutralize -> stop). No
            git commits; validates TML strings directly. Returns (union_findings, clean, passes,
            reason) where reason is 'clean' | 'no_progress' | 'request_failed'. The real promotion
            bundle is untouched — this only enumerates."""
            _tick = progress or (lambda *_a: None)
            work = [dict(it) for it in items]
            seen, passes, clean, reason = {}, 0, False, "no_progress"
            # Warnings ThoughtSpot accepted, kept apart from the issues to resolve.
            accepted_warnings = {}
            SAFETY = 40   # backstop only; real termination is clean / no-progress
            while passes < SAFETY:
                passes += 1
                files = items_to_files(work)
                strings = ([c for p, c in files.items() if p.startswith("tables/")]
                           + [c for p, c in files.items() if p.startswith("models/")])
                if not strings:
                    clean = True; reason = "clean"
                    break
                _tick(f"Pass {passes} · preparing {len(work)} object(s)…", 0.10)
                _tick(f"Pass {passes} · validating {len(strings)} table/model file(s) against the "
                      f"warehouse — the slow step; a cold warehouse can take a minute…", 0.45)
                try:
                    results = target_client().import_tml(strings, policy="VALIDATE_ONLY")
                except Exception as _e:
                    _tick(f"Pass {passes}: validation request failed — {_e}")
                    reason = "request_failed"
                    break
                _log_validate(files, results)
                # WARNING is not a failure. ThoughtSpot returns it to acknowledge something it
                # accepted — most often "DataType is being changed", which is a realign working as
                # asked. Treating it as an error meant a clean promotion reported issues and never
                # converged, so warnings are collected and surfaced but never gate the loop.
                errs = [r for r in results
                        if (r.get("status") or "").upper() not in ("OK", "WARNING")]
                _warn_found = warnings_only(classify_import_errors(
                    [r for r in results if (r.get("status") or "").upper() == "WARNING"]))
                for _wf in _warn_found:
                    accepted_warnings.setdefault(finding_key(_wf), _wf)
                if not errs:
                    clean = True; reason = "clean"
                    break
                found = blocking(classify_import_errors(errs))
                opaque = bool(found) and all(f["kind"] == "other" for f in found)
                # STATIC detectors first — no server calls. These explain most "opaque" failures:
                # dangling [formula_<name>] refs, and tables emptied/disconnected by earlier drops.
                # ThoughtSpot reports all three only as an unnamed "Schema validation failed".
                _static = dangling_reference_findings(work) + table_cleanup_findings(work)
                if opaque and _static:
                    # Static detection explains the opaque error — use it and SKIP the slow per-file
                    # isolation (which fires one warehouse validate per table).
                    _tick(f"Pass {passes} · opaque error explained statically "
                          f"({len(_static)} issue(s)) — skipping per-file isolation", 0.60)
                    found = _static
                elif opaque:
                    # Nothing static explains it — fall back to per-file isolation (slow: one
                    # validate per file) to name the culprit.
                    _tick(f"Pass {passes} · opaque error, nothing static — isolating each of "
                          f"{len(work)} file(s), one at a time…", 0.60)
                    _itemized = []
                    for _r in _isolate_failures(work, progress=lambda _m: _tick(f"Pass {passes} · {_m}", 0.65)):
                        # Carry the REAL status. Hardcoding "ERROR" here turned every accepted
                        # warning the per-file pass saw back into a blocking issue.
                        for _f in classify_import_errors(
                                [{"name": _r["name"],
                                  "status": _r.get("status") or "ERROR",
                                  "error": _r["error"]}]):
                            _f["object"] = _r["name"]
                            _itemized.append(_f)
                    if _itemized:
                        # ADD the per-file attributions; do not replace. An error that does not
                        # reproduce when files are validated one at a time — a cross-object
                        # permission failure, say — has no per-file counterpart, and replacing the
                        # list dropped it off the screen entirely. GSK 2026-09-15: the real blocker
                        # (AUTHORIZATION_FAILURE on a target logical table) vanished this way,
                        # leaving only two harmless warnings and a count of "2 issues".
                        _ik = {finding_key(x) for x in _itemized}
                        found = _itemized + [f for f in found if finding_key(f) not in _ik]
                else:
                    # Named errors present — merge static findings alongside them (additively).
                    _fk = {finding_key(x) for x in found}
                    found = found + [d for d in _static if finding_key(d) not in _fk]
                for f in found:
                    seen.setdefault(finding_key(f), f)
                _tick(f"Pass {passes} · {len(errs)} error(s) → {len(found)} finding(s) this pass, "
                      f"{len(seen)} unique so far; resolving on a copy…", 0.80)
                # Neutralize this pass's issues on the copy so the NEXT ones surface.
                drop_set, viz_set, tbl_set = set(), set(), set()
                for f in found:
                    if f["kind"] in ("missing_in_target_warehouse", "type_mismatch"):
                        # QUALIFIED drop: the warehouse names the exact table, so scope it to
                        # <table>::<column> — don't drop same-named columns off other tables.
                        _obj = (f.get("object") or "").strip()
                        drop_set.add(f"{_obj}::{f['column']}" if _obj else f["column"])
                    elif f["kind"] == "drop_blocked_by_dependents":
                        # NEVER add these to the drop set. The platform blocks the drop (the column
                        # has dependents) AND the 14544 error names only the TABLE, no column — so
                        # this historically added BARE column names that drop_columns then stripped
                        # off EVERY table, severing shared join keys (HCP_ID/CID) and cascading whole
                        # tables into "drop_table" (one column → seven dropped tables). Surface the
                        # blocked table for manual resolution instead; don't auto-drop anything.
                        pass
                    elif f["kind"] == "viz_error":
                        viz_set.update(f.get("vizzes", []))
                    elif f["kind"] == "invalid_formula_ids":
                        drop_set.update(f.get("formulas", []))   # drop by formula name
                    elif f["kind"] == "formula_broken_ref":
                        # The column this formula computes on is gone, so the formula cannot work.
                        # Drop it by name (cascading anything built on it) — it is reported by name
                        # in the drop summary, never removed silently.
                        if f.get("formula"):
                            drop_set.add(f["formula"])
                    elif f["kind"] == "dangling_ref":
                        drop_set.add(f["name"])   # drop the referrer (formula/column) by name
                    elif f["kind"] == "drop_table":
                        tbl_set.add(f["table"])   # empty / disconnected table -> prune whole
                    # NOTE: deliberately NO <b>…</b> scrape for "other" errors anymore. It grabbed
                    # garbage — ordinals like "1st" (from "translating 1st join") and bare table
                    # names — and dropped them as if they were columns. The static detectors
                    # (dangling refs, empty/disconnected tables) now resolve those opaque errors
                    # precisely; an "other" with no static explanation is surfaced, not guessed at.
                removed = 0
                _m = {}
                if drop_set:
                    work, _m = drop_columns(work, drop_set)
                    removed += _m["columns"] + _m["joins"] + len(_m["formulas"])
                if viz_set:
                    work, _dv = drop_vizzes(work, viz_set)
                    removed += _dv
                # Tables emptied (0 columns left) or orphaned (join key dropped -> unreachable) by
                # those column drops must be pruned WHOLE — else they fail import as "0 columns" /
                # "No matches found for table". Combine any drop_table findings above with a fresh
                # post-drop scan, so the probe converges instead of dead-ending on the opaque error.
                _tf = table_cleanup_findings(work)
                for f in _tf:
                    seen.setdefault(finding_key(f), f)
                    tbl_set.add(f["table"])
                if tbl_set:
                    work, _ts = _prune_tables_whole(work, tbl_set)
                    removed += (_ts["tables"] + _ts["columns"] + _ts["joins"]
                                + _ts["formulas"] + _ts["vizzes"])
                _log_discovery_pass(passes, errs, found, drop_set, viz_set, _m, removed)
                _tick(f"Pass {passes} · dropped {removed} dependent item(s) on the copy "
                      f"(logged to discovery.jsonl); re-validating…", 0.95)
                if removed == 0:
                    reason = "no_progress"
                    break   # nothing could be neutralized -> no progress, stop
            return (list(seen.values()), clean, passes, reason,
                    list(accepted_warnings.values()))

        def _isolate_failures(items, progress=None):
            """Attribute an opaque/unnamed validation error (e.g. bare 'Schema validation failed')
            to a specific object by validating files individually. Tables are validated alone
            (no cross-file deps); each model is validated WITH all tables present (so table refs
            resolve and only the model varies). Returns [{name, type, error}] for files that fail."""
            _tick = progress or (lambda *_a: None)
            tables, models = [], []
            for it in items:
                _e = it.get("edoc", "{}")
                d = _e if isinstance(_e, dict) else _parse_edoc(_e)
                if "table" in d:
                    tables.append(it)
                elif "model" in d or "worksheet" in d:
                    models.append(it)

            def _strings(items_):
                f = items_to_files(items_)
                return [c for p, c in f.items() if p.startswith(("tables/", "models/"))]

            all_table_strings = _strings(tables)
            failures, i, total = [], 0, len(tables) + len(models)
            for it in tables:
                i += 1
                nm = it.get("info", {}).get("name", "?")
                _tick(f"{i}/{total}: table `{nm}`…")
                try:
                    res = target_client().import_tml(_strings([it]), policy="VALIDATE_ONLY")
                    bad = [r for r in res if is_blocking_result(r)]
                    if bad:
                        failures.append({"name": nm, "type": "table",
                                         "status": bad[0].get("status") or "ERROR",
                                         "error": (bad[0].get("error") or "")})
                except Exception as e:
                    failures.append({"name": nm, "type": "table", "error": f"request failed: {e}"})
            # Only isolate models once tables are clean, so a table fault isn't misattributed.
            if not failures:
                for it in models:
                    i += 1
                    nm = it.get("info", {}).get("name", "?")
                    _tick(f"{i}/{total}: model `{nm}`…")
                    try:
                        res = target_client().import_tml(all_table_strings + _strings([it]),
                                                         policy="VALIDATE_ONLY")
                        bad = [r for r in res if is_blocking_result(r)]
                        if bad:
                            failures.append({"name": nm, "type": "model",
                                             "status": bad[0].get("status") or "ERROR",
                                             "error": (bad[0].get("error") or "")})
                    except Exception as e:
                        failures.append({"name": nm, "type": "model", "error": f"request failed: {e}"})
            return failures

        def _run_discover(items, status_ctx):
            """Run the discovery probe and store results; shared by the primary and re-discover
            buttons. A failed request that finds nothing keeps a good prior discovery."""
            # Progress: a bar that shows the sub-phase WITHIN each pass (prepare → validate →
            # isolate → resolve), plus the running log line. The validate itself is one blocking
            # warehouse call, so the bar parks mid-pass while that runs — the label says so.
            _bar = st.progress(0.0, text="Starting discovery…")
            def _prog(msg, frac=None):
                st.write(msg)
                if frac is not None:
                    try:
                        _bar.progress(min(max(float(frac), 0.0), 1.0), text=msg)
                    except Exception:
                        pass
            (_found, _clean, _passes, _reason,
             _accepted) = _discover_all_issues(items, progress=_prog)
            try:
                _bar.empty()
            except Exception:
                pass
            if _reason == "request_failed" and not _found and st.session_state.get("discovered_findings"):
                status_ctx.update(label="Couldn't reach the target — kept the previous discovery.",
                                  state="error", expanded=False)
                return
            st.session_state.discovered_findings = _found
            st.session_state.discovered_meta = {"clean": _clean, "passes": _passes,
                                                "reason": _reason}
            st.session_state.accepted_warnings = _accepted
            if _found:
                if not st.session_state.get("validation_errors"):
                    st.session_state.validation_errors = [{"name": "(probe)", "status": "ERROR", "error": ""}]
            else:
                st.session_state.validation_errors = []   # clean -> Stage-2 hidden, "passed" shows
                st.session_state.validation_ok = ["(discovered clean)"]
            _tail = {"clean": " — validated clean.",
                     "no_progress": " — stopped; remaining errors can't be auto-resolved.",
                     "request_failed": " — stopped: the target connection failed."}[_reason]
            status_ctx.update(label=f"Found {len(_found)} issue(s) over {_passes} pass(es)" + _tail,
                              state=("complete" if _reason != "request_failed" else "error"),
                              expanded=False)

        def _safe_validate(items, step=None):
            """_run_validation, but a hard connection failure (e.g. 10054 after the client's
            auto-retries) becomes a friendly message + a logged run — not a raw traceback.
            Returns (pr_url, err, ok) on success, or None on failure (caller should stop)."""
            try:
                return _run_validation(items, step=step)
            except Exception as _e:
                _msg = str(_e)
                st.session_state._last_validate = {
                    "ts": "(request failed)", "files": [],
                    "results": [{"name": "(validation request)", "status": "ERROR",
                                 "error": _msg}]}
                st.error("Validation couldn't reach the target. The client already auto-retries "
                         "transient resets, so try again; the platform's own message is below.")
                st.code(friendly_error(_msg)[2], language=None)
                return None

        def _detect_silent_drops(items):
            """Target columns absent from the source -> dropped on import, SILENTLY when
            they have no dependents (the platform raises no error). Diff source tables
            against their current target versions before the final import."""
            tgt = target_client()
            src_docs, names = [], []
            for i in items:
                d = _parse_edoc(i.get("edoc", "{}"))
                if "table" in d and d["table"].get("name"):
                    src_docs.append(d)
                    names.append(d["table"]["name"])
            if not names:
                return []
            name_to_id = tgt._resolve_names_to_ids(names, "LOGICAL_TABLE")
            target_docs = {}
            if name_to_id:
                raw    = tgt.export_tml(list(name_to_id.values()))
                titems = raw if isinstance(raw, list) else raw.get("object", [])
                for it in titems:
                    td = _parse_edoc(it.get("edoc", "{}"))
                    if "table" in td and td["table"].get("name"):
                        target_docs[td["table"]["name"]] = td
            return silent_drop_findings(src_docs, target_docs)

        def _modeled_col_types(names):
            """Fallback only: the target LOGICAL table's MODELED type per column (export_tml +
            column_signature). Mirrors the warehouse when the logical table is fresh, but can be
            stale — used just for tables the warehouse itself couldn't answer. {name: {col_lower: type}}."""
            tgt = target_client()
            out = {}
            names = sorted({n for n in names if n})
            if not names:
                return out
            name_to_id = tgt._resolve_names_to_ids(names, "LOGICAL_TABLE")
            if name_to_id:
                raw    = tgt.export_tml(list(name_to_id.values()))
                titems = raw if isinstance(raw, list) else raw.get("object", [])
                for it in titems:
                    td = _parse_edoc(it.get("edoc", "{}"))
                    if "table" in td and td["table"].get("name"):
                        out[td["table"]["name"]] = column_signature(td)
            return out

        def _target_col_types(mismatches):
            """The target WAREHOUSE's actual physical type per mismatched column — the CDW side of a
            14536 DataType mismatch. Reads the warehouse directly: hive_metastore via Databricks
            DESCRIBE when target DBX creds are set (the connection/search path 504s on hive), else the
            connection/search COLUMN path (Unity Catalog / Snowflake / …). Falls back to the target
            logical table's MODELED type for any table the warehouse couldn't answer.
            Returns {(object, column_lower): {"type": str, "source": "warehouse"|"modeled"}}."""
            want = {}   # object -> {col_lower, …}
            for f in mismatches:
                if f.get("object") and f.get("column"):
                    want.setdefault(f["object"], set()).add(f["column"].lower())
            if not want:
                return {}

            # Coordinates (db/schema/db_table) for each mismatched table, captured at export.
            coord_by_name = {(c.get("name") or "").strip().lower(): c
                             for c in (st.session_state.get("_promoted_coords") or [])}
            tbls = [coord_by_name[n.strip().lower()] for n in want
                    if n.strip().lower() in coord_by_name]

            wh = {}   # name.lower() -> {col_lower: warehouse_type}
            # 1) hive_metastore direct (authoritative + fast; skips the connection/search 504).
            _host = opt_env("TS_TARGET_DBX_HOST"); _whid = opt_env("TS_TARGET_DBX_WAREHOUSE")
            _tok  = opt_env("TS_TARGET_DBX_TOKEN")
            if tbls and _host and _whid and _tok:
                try:
                    from services.databricks_direct import hive_column_types
                    wh.update(hive_column_types(_host, _whid, _tok, tbls, opt_env("TS_PROXY")))
                except Exception:
                    pass
            # 2) connection/search COLUMN for any table hive didn't cover (non-hive warehouses).
            _conn = (st.session_state.get("_promoted_tgt_conn")
                     or teams[team_name].get("target_connection", ""))
            _rest = [t for t in tbls if (t.get("name") or "").strip().lower() not in wh]
            if _rest and _conn:
                try:
                    wh.update(target_client().connection_column_types(_conn, _rest))
                except Exception:
                    pass
            # Stash the FULL per-table warehouse type map (all columns of the mismatched tables, as
            # DESCRIBE/connection returns them) so the one-pass type diff can flag every mismatched
            # column in those tables at once — not one per re-validate.
            st.session_state._tm_target_full = wh

            # 3) modeled fallback only for tables no warehouse read could answer.
            _need_modeled = [n for n in want if n.strip().lower() not in wh]
            modeled = _modeled_col_types(_need_modeled) if _need_modeled else {}

            out = {}
            for f in mismatches:
                obj = f.get("object"); col = (f.get("column") or "")
                _wt = (wh.get((obj or "").strip().lower(), {}) or {}).get(col.lower())
                if _wt:
                    out[(obj, col.lower())] = {"type": _wt, "source": "warehouse"}
                else:
                    out[(obj, col.lower())] = {
                        "type": (modeled.get(obj, {}) or {}).get(col.lower(), ""),
                        "source": "modeled"}
            return out

        def _source_col_types(mismatches):
            """The SOURCE CDW's physical type per mismatched column — read straight from the source
            warehouse. hive_metastore via Databricks DESCRIBE when TS_SOURCE_DBX_* creds are set
            (mirrors the target read; hive 504s connection/search), else the source connection/search
            COLUMN path. Source coordinates come from the RAW source export (_source_raw_items),
            pre-remap, so the read hits the source db/schema/db_table — not the target-remapped ones.
            Returns {(object, column_lower): type_str}  (empty string when the warehouse couldn't be read)."""
            want = {}
            for f in mismatches:
                if f.get("object") and f.get("column"):
                    want.setdefault(f["object"], set()).add(f["column"].lower())
            if not want:
                return {}
            # Source coordinates from the raw source TML (db / schema / db_table before any remap).
            coord_by_name = {}
            for it in st.session_state.get("_source_raw_items", []):
                t = _parse_edoc(it.get("edoc", "{}")).get("table") or {}
                if t.get("name"):
                    coord_by_name[t["name"].strip().lower()] = {
                        "name": t["name"], "database": t.get("db", ""),
                        "schema": t.get("schema", ""), "table": t.get("db_table", "")}
            tbls = [coord_by_name[n.strip().lower()] for n in want
                    if n.strip().lower() in coord_by_name]

            wh = {}   # name.lower() -> {col_lower: source_warehouse_type}
            # Source DBX creds — fall back to the TARGET creds for now (Anuj: "source == target,
            # will separate later"). When the source warehouse is genuinely distinct, set the
            # TS_SOURCE_DBX_* vars and this uses them without a code change.
            _host = opt_env("TS_SOURCE_DBX_HOST") or opt_env("TS_TARGET_DBX_HOST")
            _whid = opt_env("TS_SOURCE_DBX_WAREHOUSE") or opt_env("TS_TARGET_DBX_WAREHOUSE")
            _tok  = opt_env("TS_SOURCE_DBX_TOKEN") or opt_env("TS_TARGET_DBX_TOKEN")
            if tbls and _host and _whid and _tok:
                try:
                    from services.databricks_direct import hive_column_types
                    wh.update(hive_column_types(_host, _whid, _tok, tbls, opt_env("TS_PROXY")))
                except Exception:
                    pass
            _conn = (teams[team_name].get("source_connection", "")
                     or teams[team_name].get("target_connection", ""))
            _rest = [t for t in tbls if (t.get("name") or "").strip().lower() not in wh]
            if _rest and _conn:
                try:
                    wh.update(source_client().connection_column_types(_conn, _rest))
                except Exception:
                    pass
            st.session_state._tm_source_full = wh   # full per-table source type map (for the 3-way row)
            out = {}
            for f in mismatches:
                obj = f.get("object"); col = (f.get("column") or "")
                out[(obj, col.lower())] = (wh.get((obj or "").strip().lower(), {})
                                           or {}).get(col.lower(), "")
            return out


        def _resolve_finding_table(f):
            """A table that fails the CDW type check comes back with header name 'unknown',
            but the error's FQN (db.schema.db_table.col) names the physical table. Map that
            db_table to the LOGICAL table name from the promotion bundle so we can resolve it
            on the target (cross-cluster names are preserved). Falls back to the db_table."""
            parts    = (f.get("column_fqn") or "").split(".")
            db_table = parts[-2].lower() if len(parts) >= 2 else ""
            for it in st.session_state.get("transformed_items", []):
                d = _parse_edoc(it.get("edoc", "{}"))
                t = d.get("table")
                if not t:
                    continue
                if (t.get("db_table", "") or "").lower() == db_table or \
                   (t.get("name", "") or "").lower() == db_table:
                    return t.get("name") or db_table
            obj = f.get("object")
            return obj if obj and obj != "unknown" else (db_table or obj)

        st.divider()

        # ── Stage 1: dry-run validate + discover ALL issues (no Git, no import) ─────
        # Validate-only: loops VALIDATE_ONLY (on a throwaway copy) against the target until it stops
        # finding new issues, surfacing every one at once. It does NOT commit or open a PR — the
        # commit, PR, merge and import all live on Git Operations (Step 3). Never touches the
        # connection/search COLUMN path that 504s.
        _dm = st.session_state.get("discovered_meta")
        st.caption("Dry-run validation only: it checks the TML against the target **without importing "
                   "and without touching Git**, surfacing every issue in one pass. Fix what it flags "
                   "and re-validate here; the commit, PR, merge and import all happen on Git Operations.")
        _lbl = "🔎 Re-validate against target" if _dm else "🔎 Validate against target"
        if st.button(_lbl, type="primary", disabled=not filtered_items,
                     help="Dry-run validates against the target repeatedly until it stops finding new "
                          "issues. Nothing is committed, pushed, or imported."):
            with st.status("Validating against the target…", expanded=True) as _disc:
                _run_discover(filtered_items, _disc)
            st.rerun()
        if _dm:
            _rtail = {"clean": " · validated clean",
                      "no_progress": " · stopped before clean (remaining errors can't be auto-resolved)",
                      "request_failed": " · stopped: connection to the target failed — warm the warehouse and retry"}
            _n_block = len(blocking(st.session_state.get("discovered_findings", [])))
            _n_warn  = len(st.session_state.get("accepted_warnings") or [])
            st.caption(f"Validation: {_n_block} issue(s)"
                       + (f" · {_n_warn} accepted with a warning" if _n_warn else "")
                       + f" over {_dm['passes']} pass(es)"
                       + _rtail.get(_dm.get("reason", ""), ""))

        # Type realignments are DURABLE: once approved they are re-applied to every export from
        # then on. So a realignment approved under an older, stricter rule keeps rewriting the TML
        # long after the tool stopped asking for it — which is why an integer-width change can
        # still be in the bundle even though nothing flags it any more. Show what is active and
        # give it an undo; a full Reset was previously the only way out.
        _pruned_re = st.session_state.pop("_realign_pruned", None)
        if _pruned_re:
            st.info("Dropped **" + str(len(_pruned_re)) + "** stale type realignment(s) that no "
                    "longer change anything the platform cares about (integer width only): "
                    + ", ".join(f"`{k.replace('::', '.')}`" for k in _pruned_re[:8])
                    + (", …" if len(_pruned_re) > 8 else "")
                    + ". These columns now keep the source TML's own type.")
        _active_re = st.session_state.get("realign_types") or {}
        if _active_re:
            with st.expander(f"{len(_active_re)} type realignment(s) are applied to every export",
                             expanded=False):
                import pandas as pd
                st.dataframe(_sno(pd.DataFrame(
                    [{"Table": k.split("::")[0], "Column": k.split("::")[-1], "Realigned to": v}
                     for k, v in sorted(_active_re.items())])),
                    use_container_width=True, hide_index=True)
                st.caption("These rewrite the column's declared type on every re-export. Clearing "
                           "them restores the source TML's own types; you will be re-prompted for "
                           "anything that genuinely still conflicts with the warehouse.")
                if st.button("Clear all realignments and re-export", key="clear_realigns"):
                    st.session_state.pop("realign_types", None)
                    # Dropping the bundle is what forces a fresh export (_need_export is derived
                    # from "transformed_items" not being present). The re-export re-applies the
                    # skips and prunes, just without the realignments.
                    st.session_state.pop("transformed_items", None)
                    for _k in ("pr_url", "validation_errors", "validation_ok",
                               "discovered_findings", "discovered_meta", "accepted_warnings"):
                        st.session_state.pop(_k, None)
                    st.rerun()

        # Raw validation run log — so consecutive runs are diffable (which files were validated,
        # each file's status/error). Full history appended to logs/validate_runs.jsonl.
        _lv = st.session_state.get("_last_validate")
        if _lv:
            _n_err = sum(1 for r in _lv["results"]
                         if (r.get("status") or "").upper() not in ("OK", "WARNING"))
            with st.expander(f"Validation run log — {len(_lv['results'])} file(s), {_n_err} error(s) "
                             f"· {_lv['ts']}  (full history in logs/validate_runs.jsonl)"):
                import pandas as pd
                st.dataframe(_sno(pd.DataFrame(_lv["results"])[["name", "status", "error"]]),
                             use_container_width=True, hide_index=True)
                _show_errors_verbatim(_lv["results"], "runlog")

        # ── Stage 2: Column drop (if validation failed) ────────────────────
        val_errors = st.session_state.get("validation_errors", [])
        val_ok     = st.session_state.get("validation_ok", [])

        # Findings come from the discovery probe (the complete union across passes) when it has
        # run; otherwise from the single latest validate. `_discovered` also switches Stage-2 to a
        # single "Apply all" (no per-section re-validate round-trips).
        _discovered = bool(st.session_state.get("discovered_findings"))
        if val_errors or _discovered:
            _all_found   = (st.session_state.get("discovered_findings")
                            or classify_import_errors(val_errors))
            # Split severities BEFORE anything counts, drops or gates on them. A WARNING from
            # ThoughtSpot is an acknowledgement (a realign it applied), not work to do.
            findings     = blocking(_all_found)
            accepted     = (warnings_only(_all_found)
                            + list(st.session_state.get("accepted_warnings") or []))
            _acc_seen, _acc = set(), []
            for _f in accepted:
                if finding_key(_f) not in _acc_seen:
                    _acc_seen.add(finding_key(_f)); _acc.append(_f)
            accepted = _acc
            wh_missing   = [f for f in findings if f["kind"] == "missing_in_target_warehouse"]
            dep_blocked  = [f for f in findings if f["kind"] == "drop_blocked_by_dependents"]
            type_mismatch = [f for f in findings if f["kind"] == "type_mismatch"]
            invalid_formula = [f for f in findings if f["kind"] == "invalid_formula_ids"]
            dangling     = [f for f in findings if f["kind"] == "dangling_ref"]
            drop_table_find = [f for f in findings if f["kind"] == "drop_table"]
            join_unres   = [f for f in findings if f["kind"] == "join_unresolved"]
            model_col_bad = [f for f in findings if f["kind"] == "model_column_unresolved"]
            formula_bad  = [f for f in findings if f["kind"] == "formula_broken_ref"]
            # Static check, not only the platform's complaint: a model table whose last surfaced
            # column was dropped fails on the NEXT validate, so find it now and show it with the
            # drop that caused it. Deduped against whatever validate already reported.
            _nc_seen = {finding_key(f) for f in findings if f["kind"] == "model_table_no_columns"}
            for _f in model_tables_without_columns(st.session_state.get("transformed_items", [])):
                if finding_key(_f) not in _nc_seen:
                    findings.append(_f); _nc_seen.add(finding_key(_f))
            bare_tables  = [f for f in findings if f["kind"] == "model_table_no_columns"]
            # Catch-all by CONSTRUCTION, not by enumeration: anything this page does not render in
            # its own section above falls through to "Other validation errors" below. A new finding
            # kind can then never silently vanish from the screen just because nobody added a
            # section for it — which is a worse failure than showing it raw.
            _handled_kinds = {
                "missing_in_target_warehouse", "drop_blocked_by_dependents", "type_mismatch",
                "invalid_formula_ids", "dangling_ref", "drop_table", "join_unresolved",
                "model_column_unresolved", "formula_broken_ref", "model_table_no_columns",
                "invalid_column_property",
            }
            other        = [f for f in findings if f["kind"] not in _handled_kinds]

            # VALIDATE_ONLY reports only the FIRST missing column per table, so the reviewer
            # otherwise fixes them one-per-round. Diff every promoted table against the TARGET
            # CONNECTION's own column set (the CDW — the source of truth 14536 checks against),
            # fetched at export, to surface EVERY missing column at once. This CDW diff is the
            # complete, authoritative list for any table the connection could read, so it REPLACES
            # the one-per-round validation findings for those tables (no duplicate rows). For a
            # table the warehouse couldn't answer for, we keep the validation-confirmed finding and
            # fall back to the org-modeled column set (flagged 'unverified').
            # ALWAYS diff against the warehouse for missing columns: it returns the COMPLETE set in
            # one pass with REAL table names (object = the table). VALIDATE_ONLY does neither — it
            # reports one missing column per table per round AND names them "unknown", so the drop
            # key becomes `unknown::col`, which matches no table (the drop never clears, discovery
            # stalls) and can't be grouped. The warehouse diff replaces those unknown-named findings.
            if True:
                cdw_map = st.session_state.get("_warehouse_col_map") or {}
                org_map = st.session_state.get("_column_case_map") or {}
                diff_findings = warehouse_missing_findings(
                    st.session_state.get("transformed_items", []), cdw_map, fallback_map=org_map,
                    connection=teams[team_name].get("target_connection", ""))

                def _tbl_of(f):
                    parts = (f.get("column_fqn") or "").split(".")
                    return (parts[-2] if len(parts) >= 2 else f.get("object", "")).strip().lower()

                covered = {k for k in cdw_map} | {k for k in org_map}
                # keep only validation-confirmed missing columns for tables neither map could cover
                confirmed_extra = [f for f in wh_missing if _tbl_of(f) not in covered]
                for f in confirmed_extra:
                    f["verified"] = True
                wh_missing = diff_findings + confirmed_extra

            # The failed table's header name is often "unknown"; recover the real table name
            # from the error FQN + the promotion bundle so target lookups resolve.
            for f in type_mismatch:
                f["object"] = _resolve_finding_table(f)

            _unverified = sum(1 for f in wh_missing if not f.get("verified"))
            if findings:
                _issue_msg = f"Validation found {len(findings)} issue(s) to resolve before import."
                if _unverified:
                    _issue_msg += (f"  {_unverified} column(s) below could not be checked against the "
                                   "warehouse (marked ⚠︎ unverified).")
                st.error(_issue_msg)
            elif accepted:
                st.success(f"Nothing to resolve. ThoughtSpot accepted the promotion with "
                           f"{len(accepted)} warning(s), listed below.")

            # ── accepted with a warning: applied by the platform, no action required ──
            if accepted:
                _tc = [f for f in accepted if f["kind"] == "type_changed_notice"]
                with st.expander(f"Accepted with a warning — {len(accepted)} item(s), no action "
                                 f"needed", expanded=not findings):
                    if _tc:
                        # Show the THREE types. "Why is the tool still changing this?" cannot be
                        # answered by a note saying a change happened — it needs source vs what we
                        # are shipping vs what the target holds today. Promoted == Source means the
                        # tool changed nothing and the target is simply older (often because an
                        # EARLIER run imported a realignment that is now being corrected back).
                        import pandas as pd
                        _src_t = {}
                        for _it in st.session_state.get("_source_raw_items", []):
                            _d = _parse_edoc(_it.get("edoc", "{}")).get("table") or {}
                            if _d.get("name"):
                                for _c in _d.get("columns") or []:
                                    _dt = (_c.get("db_column_properties") or {}).get("data_type", "")
                                    for _k in ((_c.get("name") or "").lower(),
                                               (_c.get("db_column_name") or "").lower()):
                                        if _k:
                                            _src_t[(_d["name"].lower(), _k)] = _dt
                        _bun_t = {}
                        for _it in st.session_state.get("transformed_items", []):
                            _d = _parse_edoc(_it.get("edoc", "{}")).get("table") or {}
                            if _d.get("name"):
                                for _c in _d.get("columns") or []:
                                    _dt = (_c.get("db_column_properties") or {}).get("data_type", "")
                                    for _k in ((_c.get("name") or "").lower(),
                                               (_c.get("db_column_name") or "").lower()):
                                        if _k:
                                            _bun_t[(_d["name"].lower(), _k)] = _dt
                        _tgt_t = {}
                        try:
                            for _tn, _cm2 in _modeled_col_types(
                                    sorted({f.get("object") for f in _tc if f.get("object")})).items():
                                for _cl, _ty in (_cm2 or {}).items():
                                    _tgt_t[(_tn.lower(), _cl.lower())] = _ty
                        except Exception:
                            pass
                        _rows_tc = []
                        for f in _tc:
                            _k = ((f.get("object") or "").lower(), (f.get("column") or "").lower())
                            _s, _b = _src_t.get(_k, ""), _bun_t.get(_k, "")
                            _g = _tgt_t.get(_k, "")
                            _who = ("this tool changed it" if _s and _b and _s != _b
                                    else "not us — source and target differ" if _s and _b
                                    else "couldn't read all three")
                            _rows_tc.append({"Table": f.get("object") or "(not named)",
                                             "Column": f.get("column", ""),
                                             "Source TML": _s or "(?)",
                                             "Promoted": _b or "(?)",
                                             "Target today": _g or "(unread)",
                                             "Who changed it": _who})
                        st.caption("ThoughtSpot is confirming a data type change. **Promoted** is "
                                   "what this run ships; if it equals **Source TML** the tool "
                                   "changed nothing and the target is just older — commonly "
                                   "because an earlier run imported a realignment that this run is "
                                   "now correcting back. Landing it once clears the warning.")
                        st.dataframe(_sno(pd.DataFrame(_rows_tc)),
                                     use_container_width=True, hide_index=True)
                    for f in accepted:
                        if f["kind"] == "type_changed_notice":
                            continue
                        st.markdown(f"**{f.get('object') or '(not named)'}** — "
                                    f"{f.get('error', '')}")

            # Casing diagnostic: if a column is flagged as "missing from warehouse", it usually
            # means the connection-based recasing did not resolve that table. Show what happened.
            diag = st.session_state.get("_casing_diag")
            if diag:
                with st.expander("Column-casing diagnostic (why a column may still be flagged)"):
                    st.markdown(
                        f"- Target connection: `{diag['connection']}`  ·  found on cluster: "
                        f"**{diag['connection_found']}**  ·  auth type: `{diag['auth_type']}`")
                    st.markdown("- Recased from the connection: "
                                + (", ".join(f"`{t}`" for t in diag["resolved"]) or "_none_"))
                    if diag["unresolved"]:
                        st.markdown("- **Not recased** (no warehouse casing returned): "
                                    + ", ".join(f"`{t}`" for t in diag["unresolved"]))
                        st.caption("For each unresolved table, the coordinates the tool queried the "
                                   "connection with are below. If these do not match the table in the "
                                   "target warehouse (wrong database/schema, or the connection name is "
                                   "off), that is why no casing came back.")
                        for t in diag["unresolved"]:
                            st.markdown(f"&nbsp;&nbsp;· `{t}` → queried `{diag['coords'].get(t, '?')}`")
                    trace = diag.get("fetch_trace") or []
                    if trace:
                        st.markdown("- **Connection fetch attempts** (per auth type tried):")
                        for a in trace:
                            bits = [f"auth `{a.get('auth_type')}`", f"HTTP {a.get('status')}",
                                    f"objects: {a.get('has_objects')}", f"columns: {a.get('columns_found')}"]
                            line = "&nbsp;&nbsp;· " + " · ".join(bits)
                            if a.get("error"):
                                line += f" · error: {a['error']}"
                            st.markdown(line)
                        st.caption("If an attempt shows HTTP 200 with objects: False and no error, the "
                                   "fetch ran but the warehouse returned nothing (service-principal / "
                                   "catalog path). An error (e.g. code 10086) means a privilege problem.")

            # ── source-extra: a source column the target warehouse doesn't have ──
            if wh_missing:
                import pandas as pd
                st.markdown("#### Columns missing from the target warehouse")
                st.caption(
                    "Referenced by the source but absent from the target warehouse, so the TML "
                    "cannot import as-is. **Default is to keep them** — add the column to the target "
                    "warehouse, then re-run. Tick **Drop?** on a row only to drop that column from "
                    "this promotion (along with any visualization that uses it).")
                st.caption("Checked against the **target connection** (the warehouse itself), so this "
                           "is the complete set — not one column per re-validate. The **#** column is "
                           "the table's S.No from the count table above.")
                if any(not f.get("verified") for f in wh_missing):
                    st.caption("⚠︎ in the **✓** column means that table's warehouse could not be read; "
                               "the row is inferred from the target's modeled columns and may include a "
                               "column that actually exists in the warehouse. Verify before dropping.")

                _promo_items = st.session_state.get("transformed_items", [])
                _sel = st.session_state.setdefault("wh_drop_selected", set())

                # One row per missing column — a proper table, not a stack of checkboxes-in-expanders.
                # Ticking a row no longer collapses anything (the old per-column st.expander reran and
                # closed on every click); the blast radius is summarised inline in the Dependents
                # column instead. QUALIFIED key `obj::col` scopes each drop to THIS table's column — a
                # bare name would strip the column off EVERY table that has it and collapse shared-key
                # joins (e.g. CID across the bridge tables), gutting the model.
                _rows = []
                for f in sorted(wh_missing, key=lambda x: ((x.get("object") or "").lower(),
                                                           (x.get("column") or "").lower())):
                    _tbl    = f.get("object") or "(unresolved table)"
                    _scoped = f"{_tbl}::{f['column']}" if f.get("object") else f["column"]
                    _deps   = column_dependents(_promo_items, [f["column"]])
                    _dbits  = []
                    if _deps.get("joins"):    _dbits.append(f"{len(_deps['joins'])} join(s)")
                    if _deps.get("formulas"): _dbits.append(f"{len(_deps['formulas'])} formula(s)")
                    if _deps.get("vizzes"):   _dbits.append(f"{len(_deps['vizzes'])} viz(s)")
                    _rows.append({
                        "#":      str(_tbl_serial.get(_tbl.strip().lower(), "")),
                        "Table":  _tbl,
                        "Column": f["column"],
                        "Dependents (source)": ", ".join(_dbits) if _dbits else "none",
                        "✓":      "✓" if f.get("verified") else "⚠︎",
                        "Connection": f.get("connection", ""),
                        "Drop?":  _scoped in _sel,
                        "_scoped": _scoped,
                    })
                _df = pd.DataFrame(_rows, columns=["#", "Table", "Column", "Dependents (source)",
                                                   "✓", "Connection", "Drop?", "_scoped"])

                # Search + tick/clear over the shown rows. Selection persists in wh_drop_selected;
                # single-click ticks + a fresh editor per (query, generation) come from _select_editor.
                _cs, _cb1, _cb2 = st.columns([3, 1, 1])
                with _cs:
                    _q = st.text_input("Filter columns", key="wh_search",
                                       label_visibility="collapsed",
                                       placeholder="🔎 Filter by table or column name").strip().lower()
                if _q and not _df.empty:
                    _view = _df[_df.apply(lambda r: _q in str(r["Table"]).lower()
                                          or _q in str(r["Column"]).lower(), axis=1)]
                else:
                    _view = _df
                _eb = f"wh_drop::{_q}"
                with _cb1:
                    if st.button(f"Tick shown ({len(_view)})", use_container_width=True,
                                 disabled=_view.empty):
                        _sel.update(_view["_scoped"].tolist()); _bump_editor(_eb); st.rerun()
                with _cb2:
                    if st.button("Clear shown", use_container_width=True, disabled=_view.empty):
                        for _sk in _view["_scoped"].tolist():
                            _sel.discard(_sk)
                        _bump_editor(_eb); st.rerun()
                if _q:
                    st.caption(f"{len(_view)} of {len(_df)} column(s) match “{_q}”.")

                _select_editor(
                    _view, ["Drop?"], ["wh_drop_selected"], _eb,
                    column_config={
                        "#":      st.column_config.TextColumn("#", width="small",
                                    help="Matches the S.No in the count table above."),
                        "Table":  st.column_config.TextColumn("Table", width="medium"),
                        "Column": st.column_config.TextColumn("Column", width="medium"),
                        "Dependents (source)": st.column_config.TextColumn(
                                    "Dependents (source)", width="medium",
                                    help="What in this promotion references the column — a drop "
                                         "cascades to these."),
                        "✓":      st.column_config.TextColumn("✓", width="small",
                                    help="✓ warehouse-verified · ⚠︎ inferred from modeled columns"),
                        "Connection": st.column_config.TextColumn("Connection", width="medium"),
                        "Drop?":  st.column_config.CheckboxColumn("Drop?", width="small",
                                    help="Tick to drop this column (and any viz that uses it)."),
                    },
                    disabled=["#", "Table", "Column", "Dependents (source)", "✓", "Connection"])
                drop_set = set(_sel)
                st.caption(f"**{len(drop_set)}** column(s) marked to drop.")

                # #7 — what's already gone this run, shown right here (not only on the next page).
                _done = st.session_state.get("dropped_col_names", set())
                if _done:
                    st.caption("**Already dropped this run:** "
                               + ", ".join(f"`{c.split('::')[-1]}`" for c in sorted(_done)))
                _rep = st.session_state.get("_last_drop_report")
                if _rep:
                    st.success(_rep)

                # Per-section apply only in the single-pass path. When discovery has run, one
                # "Apply all" at the bottom handles every section in a single re-validate.
                if not _discovered and st.button("Apply choices, re-export & re-validate", type="primary"):
                    if drop_set:
                        fixed, _man = drop_columns(st.session_state.transformed_items, drop_set)
                        _record_drop(_man)
                        st.session_state.setdefault("dropped_col_names", set()).update(drop_set)
                        # A drop that leaves a table with 0 columns (or orphans it) must prune the
                        # WHOLE table + cascade its refs, or import hard-fails "0 columns" /
                        # "No matches found for table". The discovery loop already does this on its
                        # copy; the manual-apply path must too, else the emptied table ships as-is.
                        _emptied = {f["table"] for f in table_cleanup_findings(fixed)}
                        if _emptied:
                            fixed, _ts = _prune_tables_whole(fixed, _emptied)
                            st.session_state.setdefault("prune_tables", set()).update(_emptied)
                        st.session_state.transformed_items = fixed
                        _log_apply_detail("manual_apply", drop_set, _man, _emptied, fixed)
                        # #8 — name exactly what left: table.column, plus the cascade actually removed.
                        _names = ", ".join(f"`{s.replace('::', '.')}`" for s in sorted(drop_set))
                        _casc  = []
                        if _man.get("joins"):    _casc.append(f"{_man['joins']} join(s)")
                        if _man.get("formulas"):
                            # NAME the formulas. A formula is something a person wrote; if the tool
                            # removes one because its column went, the operator has to be told which.
                            _fn = sorted({str(x) for x in _man["formulas"]})
                            _casc.append(f"{len(_fn)} formula(s) (" +
                                         ", ".join(f"`{x}`" for x in _fn[:6]) +
                                         (", …" if len(_fn) > 6 else "") + ")")
                        if _man.get("vizzes"):   _casc.append(f"{_man['vizzes']} viz(s)")
                        if _emptied:             _casc.append(f"{len(_emptied)} emptied table(s) pruned")
                        _msg = f"Dropped {len(drop_set)} column(s): {_names}."
                        if _casc:
                            _msg += " Cascade removed " + ", ".join(_casc) + "."
                        st.session_state._last_drop_report = _msg
                        _sel.clear()   # selection consumed; the columns are gone from wh_missing now
                    filtered_fixed = [i for i in st.session_state.transformed_items
                                      if i.get("info", {}).get("name") not in skip_objects]
                    with st.status("Re-validating…", expanded=True) as _rv:
                        _res = _safe_validate(filtered_fixed, step=lambda _m: _rv.write(_m))
                        _rv.update(state=("complete" if _res else "error"), expanded=False)
                        if _res:
                            _, err, ok = _res
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                    if _res:
                        st.rerun()

            # ── target-extra with dependents: the drop is blocked on the target ──
            if dep_blocked:
                st.markdown("#### Blocked — import would delete a column that still has dependents")
                st.caption("Removing these on the target would break something still using them, so "
                           "the platform blocks the whole import (error 14544). Remove the "
                           "dependent(s) on the target (or fix the column), then re-run.")
                # Cross-reference the flagged missing/type columns to each blocked table so the
                # operator knows which column drove a table-only block.
                _flagged_by_tbl = {}
                for _f in (wh_missing + type_mismatch):
                    _flagged_by_tbl.setdefault((_f.get("object") or "").strip().lower(), []) \
                        .append(_f.get("column"))
                # Two shapes, deduped: column-level (platform named the column + its dependents) and
                # table-only (platform named just the table). The same block repeats across 60+ rows.
                _col_deps, _tbl_only = {}, set()
                for f in dep_blocked:
                    if f.get("column"):
                        _col_deps.setdefault(f["column"], set()).update(
                            d for d in f.get("dependents", []) if d)
                    else:
                        _tbl_only.add(f.get("object") or "(table not named)")
                for _col in sorted(_col_deps, key=str.lower):
                    _deps = sorted(_col_deps[_col])
                    _line = f"Column **`{_col}`** is blocked"
                    if _deps:
                        _line += " — used by: " + ", ".join(f"**{d}**" for d in _deps)
                    st.warning(_line + ".")
                for _tbl in sorted(_tbl_only, key=str.lower):
                    _cols = [c for c in _flagged_by_tbl.get(_tbl.strip().lower(), []) if c]
                    _line = f"Table **`{_tbl}`** is blocked"
                    if _cols:
                        _line += " — likely from column(s): " + ", ".join(f"`{c}`" for c in _cols)
                    st.warning(_line + ".")

                # ── remove the blockers, HERE ──────────────────────────────────────────────
                # The remedy belongs where the problem is stated. This used to live only on Git
                # Operations, so the operator read "blocked by X" on this page and had to go
                # looking for the way to act on it.
                _blk_names = sorted({d for ds in _col_deps.values() for d in ds if d})
                # Names to look for inside each dependent. On THIS page the authority is the
                # platform's own message — it already told us which columns are blocked — plus
                # whatever has been dropped so far this run. (_scan_names lives in the Git
                # Operations block and is not in scope here; using it was a NameError.)
                _blk_scan_names = scan_names_for_drops(
                    st.session_state.get("dropped_col_names") or set(),
                    (st.session_state.get("dropped_cascade_names") or set())
                    | {c for c in _col_deps if c})
                if _blk_names:
                    with st.spinner("Looking up the blocking object(s) on the target…"):
                        _tgtc = target_client()
                        _resolved = _tgtc.find_objects_by_name(_blk_names)
                        # Read each one's TML so a LIVEBOARD can name the tile(s) actually using
                        # the column. "Test dev is blocked" is not actionable; "Viz_1 of 2 tiles"
                        # tells the operator how much of the board is really at stake.
                        _cand = []
                        for _n in _blk_names:
                            _h2 = _resolved.get(_n.strip().lower())
                            if not _h2:
                                continue
                            try:
                                _raw2 = _tgtc.export_tml([_h2["id"]])
                                _its2 = _raw2 if isinstance(_raw2, list) else _raw2.get("object", [])
                                _tml2 = _its2[0].get("edoc") if _its2 else None
                            except Exception:
                                _tml2 = None
                            _cand.append({**_h2, "tml": _tml2})
                        _scan = {c["id"]: c for c in
                                 dependents_using_columns(_cand, _blk_scan_names)}
                    _rows_b = []
                    for _n in _blk_names:
                        _hit = _resolved.get(_n.strip().lower())
                        _det = _scan.get((_hit or {}).get("id"), {})
                        _vz = _det.get("vizzes") or []
                        _tot = _det.get("viz_total") or 0
                        _vlabel = ("—" if not _tot else
                                   ", ".join(f"{v['id']}" + (f" ({v['name']})" if v.get("name") else "")
                                             for v in _vz) + f"  · of {_tot} tile(s)"
                                   if _vz else f"(none of {_tot} tile(s) matched)")
                        _rows_b.append({
                            "Object": _n,
                            "Type": (_hit or {}).get("type", "—"),
                            "Author": (_hit or {}).get("author", "—"),
                            "Tile(s) using the column": _vlabel,
                            "Can this account delete it?": "yes" if _hit else "not visible",
                            "_scoped": (_hit or {}).get("id") or f"__unresolved__{_n}",
                            "_deletable": bool(_hit)})
                    _bsel = st.session_state.setdefault("blk_del_selected", set())
                    for _r in _rows_b:
                        _r["Delete?"] = _r["_scoped"] in _bsel
                    import pandas as pd
                    _bdf = pd.DataFrame(_rows_b, columns=[
                        "Object", "Type", "Author", "Tile(s) using the column",
                        "Can this account delete it?", "Delete?",
                        "_scoped", "_deletable"]).drop(columns=["_deletable"])
                    _select_editor(
                        _bdf, ["Delete?"], ["blk_del_selected"], "blkdel",
                        column_config={
                            "Object": st.column_config.TextColumn("Object", width="large"),
                            "Type":   st.column_config.TextColumn("Type", width="small"),
                            "Author": st.column_config.TextColumn("Author", width="medium"),
                            "Tile(s) using the column": st.column_config.TextColumn(
                                "Tile(s) using the column", width="large",
                                help="For a liveboard, which visualisation(s) reference the "
                                     "dropped column, and how many tiles the board has in total."),
                            "Can this account delete it?": st.column_config.TextColumn(
                                "Can this account delete it?", width="medium",
                                help="'not visible' means the object exists but this account "
                                     "cannot see it — its owner or an admin has to remove it."),
                            "Delete?": st.column_config.CheckboxColumn("Delete?", width="small"),
                        },
                        disabled=["Object", "Type", "Author", "Tile(s) using the column",
                                  "Can this account delete it?"])
                    _pick_b = [r for r in _rows_b if r["_scoped"] in _bsel and r["_deletable"]]
                    _pick_x = [r for r in _rows_b if r["_scoped"] in _bsel and not r["_deletable"]]
                    if _pick_x:
                        st.warning("Not visible to this account, so it can't be changed here: "
                                   + ", ".join(f"**{r['Object']}**" for r in _pick_x)
                                   + ". Ask its owner, or run the tool as an admin.")
                    if _pick_b:
                        # What is ticked above is only the FIRST layer. A model dependent is not a
                        # leaf: strip the column out of it and its OWN answers and boards are next
                        # in line, and theirs after that. So the whole tree is walked and planned
                        # in full before anything is shown, let alone written — a cascade applied
                        # halfway leaves the target inconsistent AND the import still blocked.
                        _tc = target_client()
                        _auth_by_id = {r["_scoped"]: r["Author"] for r in _rows_b}

                        def _c_tml(_i):
                            try:
                                _r = _tc.export_tml([_i])
                                _it = _r if isinstance(_r, list) else _r.get("object", [])
                                return _it[0].get("edoc") if _it else None
                            except Exception:
                                return None

                        def _c_deps(_i):
                            try:
                                _m = _tc.list_dependents([_i]) or {}
                            except Exception:
                                return []
                            _o = []
                            for _v in _m.values():
                                _o.extend(_v or [])
                            return _o

                        def _c_validate(_edoc):
                            try:
                                _r = _tc.import_tml([_edoc], policy="VALIDATE_ONLY")
                            except Exception as _e:
                                return False, str(_e)
                            # WARNING is not a failure. Treating it as one is how the tool used to
                            # refuse work that the platform had actually accepted.
                            _bad = [x for x in _r if (x.get("status") or "").upper()
                                    not in ("OK", "WARNING")]
                            if _bad:
                                return False, (_bad[0].get("error") or "the target rejected it")
                            return True, ""

                        _ck = tuple(sorted(r["_scoped"] for r in _pick_b))
                        if st.session_state.get("_casc_key") != _ck:
                            st.session_state.pop("_casc_actions", None)
                            st.session_state.pop("_casc_blocked", None)

                        if "_casc_actions" not in st.session_state:
                            st.caption("The ticked objects are starting points, not the whole "
                                       "list. Planning follows each one down to its own "
                                       "dependents so nothing is left half-fixed.")
                            if st.button(f"Plan the cascade from {len(_pick_b)} object(s)",
                                         key="casc_plan_go"):
                                # The model being promoted depends on its own table, so it turns
                                # up as a dependent of its own drop. It IS the promotion, not a
                                # casualty of it — never plan anything against it.
                                _promo_n = {(_i.get("info", {}).get("name") or "").strip().lower()
                                            for _i in (filtered_items or [])}
                                _promo_n |= {(_n or "").strip().lower() for _n in
                                             (st.session_state.get("_promo_id2name") or {}).values()}
                                _promo_n.discard("")
                                with st.spinner("Walking the target's dependency tree…"):
                                    try:
                                        _acts, _blkc = plan_cascade(
                                            [{"id": r["_scoped"], "name": r["Object"],
                                              "type": r["Type"]} for r in _pick_b],
                                            _blk_scan_names, _c_tml, _c_deps,
                                            skip_names=_promo_n)
                                    except Exception as _e:
                                        _acts, _blkc = None, None
                                        st.error(f"Couldn't plan the cascade: {_e}")
                                if _acts is not None:
                                    st.session_state._casc_key     = _ck
                                    st.session_state._casc_actions = _acts
                                    st.session_state._casc_blocked = _blkc
                                    st.rerun()
                        else:
                            _acts = st.session_state.get("_casc_actions") or []
                            _blkc = st.session_state.get("_casc_blocked") or []
                            st.markdown("##### The full cascade")
                            st.caption(f"{len(_pick_b)} object(s) ticked; {len(_acts)} object(s) "
                                       f"in the plan once their own dependents are followed"
                                       + (f", {len(_blkc)} that cannot be planned" if _blkc else "")
                                       + ".")
                            for _ln in plan_summary(_acts, []):
                                st.markdown("- " + _ln)
                            if st.button("Re-plan", key="casc_replan",
                                         help="Walk the target again — use this if anything on "
                                              "the target changed since the plan was built."):
                                st.session_state.pop("_casc_actions", None)
                                st.session_state.pop("_casc_blocked", None)
                                st.rerun()
                            if _blkc:
                                # All-or-nothing, by design. Applying the reachable part would
                                # destroy content on the target AND leave the import blocked by
                                # whatever could not be reached — strictly worse than not starting.
                                st.error(
                                    f"**The cascade will not run.** {len(_blkc)} object(s) in the "
                                    "tree cannot be planned, so applying the rest would change the "
                                    "target and still leave the import blocked:\n\n"
                                    + "\n".join(f"- **{b['name'] or b['id']}** — {b['reason']}"
                                                for b in _blkc)
                                    + "\n\nResolve these first, or have someone who can see them "
                                      "do it, then re-plan.")
                            elif not _acts:
                                st.info("Nothing to change — none of the ticked objects actually "
                                        "surfaces the dropped column on the target.")
                            else:
                                _ns = sum(1 for a in _acts if a["action"] == "strip_columns")
                                _nt = sum(1 for a in _acts if a["action"] == "remove_tiles")
                                _nd = sum(1 for a in _acts if a["action"] == "delete")
                                st.error(
                                    "**This will change `" + opt_env("TS_TARGET_HOST")
                                    + "`.** Every object's current TML is saved first, and the "
                                      "whole plan is validated against the target before a single "
                                      "write. There is no undo: the saved TML is the way back. "
                                      "Type **DELETE** to confirm.")
                                _tb = st.text_input("Confirm", key="blk_del_confirm",
                                                    label_visibility="collapsed",
                                                    placeholder="type DELETE")
                                if st.button(f"Apply the cascade — {_ns} model(s) stripped, "
                                             f"{_nt} board(s) trimmed, {_nd} answer(s) deleted",
                                             key="blk_del_go",
                                             disabled=_tb.strip().upper() != "DELETE"):
                                    import datetime as _dt
                                    _snapdir = (Path(__file__).parent / "logs" / "snapshots"
                                                / _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))

                                    def _write_snap(_n, _t):
                                        _snapdir.mkdir(parents=True, exist_ok=True)
                                        _p = _snapdir / _n
                                        _p.write_text(_t)
                                        return str(_p)

                                    _res_b, _okn, _go = {}, 0, True
                                    with st.status("Changing the target…", expanded=True) as _bs:
                                        # 1. Save what is there now. A snapshot that cannot be
                                        #    taken is itself a reason to stop — without it there
                                        #    is no way back from any of what follows.
                                        try:
                                            _bs.write("Saving each object's current TML…")
                                            _paths = snapshot_plan(_acts, _c_tml, _write_snap)
                                            _bs.write(f"✓ saved {len(_paths)} file(s) to "
                                                      f"`{_snapdir}`")
                                        except Exception as _e:
                                            _bs.update(label=f"Stopped before changing anything: "
                                                             f"{_e}", state="error")
                                            _go = False
                                        # 2. Validate the WHOLE plan before writing any of it.
                                        if _go:
                                            _bs.write("Validating the whole plan against the "
                                                      "target…")
                                            _probs = dry_run_plan(_acts, _c_validate,
                                                                  planned_names(_acts))
                                            for _p in _probs:
                                                _bs.write(f"✗ {_p.get('name') or _p['id']}: "
                                                          f"{_p['error']}")
                                            if _probs:
                                                _bs.update(
                                                    label="The plan does not validate, so nothing "
                                                          "was changed on the target.",
                                                    state="error")
                                                _go = False
                                            else:
                                                _bs.write("✓ the whole plan validates")
                                        # 3. Write it, leaves first: a model can only lose the
                                        #    column once nothing below it still references it.
                                        if _go:
                                            for _a in apply_order(_acts):
                                                _nm = _a.get("name") or _a["id"]
                                                try:
                                                    if _a["action"] == "delete":
                                                        _okb, _, _det = \
                                                            _tc.delete_metadata_verified(
                                                                _a["type"], _a["id"])
                                                    else:
                                                        _okb, _det = _tc.apply_tml_verified(
                                                            _a["id"], _a["new_edoc"],
                                                            **(_a.get("verify") or {}))
                                                except Exception as _e:
                                                    _okb, _det = False, str(_e)
                                                _res_b[_a["id"]] = _det or ("ok" if _okb
                                                                            else "failed")
                                                _bs.write(("✓ " if _okb else "✗ ")
                                                          + f"{_nm}: {_res_b[_a['id']]}")
                                                if _okb:
                                                    _okn += 1
                                                else:
                                                    # Stop rather than carry on: the rest of the
                                                    # plan assumes this one succeeded.
                                                    _bs.write(
                                                        "Stopping here. What was already applied "
                                                        "stays applied — re-import the TML in "
                                                        f"`{_snapdir}` to put it back.")
                                                    break
                                        _log_target_delete(
                                            opt_env("TS_TARGET_HOST"), team_name,
                                            [{"id": a["id"], "name": a.get("name"),
                                              "type": a["type"], "action": a["action"],
                                              "author": _auth_by_id.get(a["id"], ""),
                                              "columns": a.get("removed") or []}
                                             for a in _acts], _res_b)
                                        if _go:
                                            _bs.update(
                                                label=(f"Applied {_okn} of {len(_acts)} "
                                                       "(logged to logs/target_deletes.jsonl)"),
                                                state="complete" if _okn == len(_acts) else "error")
                                    if _go and _okn == len(_acts):
                                        _bsel.clear()
                                        for _k in ("blk_del_confirm", "_casc_key", "_casc_actions",
                                                   "_casc_blocked", "validation_errors",
                                                   "validation_ok", "discovered_findings",
                                                   "discovered_meta"):
                                            st.session_state.pop(_k, None)
                                        st.rerun()

            # ── type drift: column exists on both sides, types differ ──
            if type_mismatch:
                import pandas as pd
                tm_key = tuple(sorted((f["object"], f["column"]) for f in type_mismatch))
                if st.session_state.get("_tm_key2") != tm_key:
                    with st.spinner("Reading the source & target warehouse column types (CDW)… "
                                    "a cold warehouse can take a minute or two."):
                        # Best-effort warehouse type reads; degrade to "(unread)" on any failure.
                        try:
                            st.session_state._tm_types     = _target_col_types(type_mismatch)
                            st.session_state._tm_src_types = _source_col_types(type_mismatch)
                        except Exception as _e:
                            st.session_state._tm_types     = {}
                            st.session_state._tm_src_types = {}
                            st.caption("⚠ Couldn't read warehouse column types "
                                       f"({str(_e)[:120]}). Showing dev's type only.")
                        st.session_state._tm_key2 = tm_key
                # Full per-table warehouse type maps (all columns of the mismatched tables) — read once
                # by the enrichment above. The row's target/source type reads from these, so the
                # ONE-PASS findings below (columns validate hasn't reported yet) also resolve.
                _tfull = st.session_state.get("_tm_target_full") or {}
                _sfull = st.session_state.get("_tm_source_full") or {}

                # ONE-PASS type check: flag EVERY type-mismatched column in the already-read tables,
                # not just the one VALIDATE_ONLY reported this round. Kills the type whack-a-mole.
                if _tfull:
                    _seen_tm = {(f.get("object"), (f.get("column") or "").lower()) for f in type_mismatch}
                    for _op in warehouse_type_findings(
                            st.session_state.get("transformed_items", []), _tfull,
                            connection=teams[team_name].get("target_connection", "")):
                        _k = (_op.get("object"), (_op.get("column") or "").lower())
                        if _k not in _seen_tm:
                            type_mismatch.append(_op); _seen_tm.add(_k)

                _tnorm = lambda s: "".join((s or "").lower().split())   # loose compare
                _tmsel   = st.session_state.setdefault("tm_drop_selected", set())
                _tmrsel  = st.session_state.setdefault("tm_realign_selected", set())
                _realign_to = {}   # scoped key -> the TS TOKEN a realign would write
                # Columns already dropped this run are NOT candidates — otherwise a column you resolved
                # by dropping keeps reappearing one re-validate at a time.
                _already_gone = st.session_state.get("dropped_col_names", set())
                # Every reason a reported mismatch does NOT become a row, so the table can never
                # sit there empty next to a red type error with nothing to click (the GSK demo).
                _rows, _void_rows, _quiet_agree, _quiet_gone = [], [], [], []
                for f in sorted(type_mismatch, key=lambda x: ((x.get("object") or "").lower(),
                                                              (x.get("column") or "").lower())):
                    obj = f.get("object") or "(table)"
                    col = f.get("column", "")
                    _scoped = f"{obj}::{col}" if f.get("object") else col
                    if _scoped in _already_gone or col in _already_gone:
                        _quiet_gone.append(_scoped)
                        continue
                    tgt_t   = (_tfull.get((obj or "").strip().lower()) or {}).get(col.lower(), "")
                    src_cdw = (_sfull.get((obj or "").strip().lower()) or {}).get(col.lower(), "")
                    src_tml = f.get("source_type", "")
                    # VOID: the warehouse has NO usable type for this column. It can't bind and can't
                    # be realigned — it is NOT a type mismatch. Route it to its own drop table so it
                    # never shows up under "type mismatches" again.
                    if _tnorm(tgt_t) == "void" or _tnorm(src_cdw) == "void":
                        _void_rows.append({
                            "#":          str(_tbl_serial.get(obj.strip().lower(), "")),
                            "Table":      obj,
                            "Column":     col,
                            "Source CDW": src_cdw.upper() if src_cdw else "(unread)",
                            "Target CDW": tgt_t.upper() if tgt_t else "(unread)",
                            "Drop?":      _scoped in _tmsel,
                            "_scoped":    _scoped,
                        })
                        continue
                    # Quiet when the warehouse and the TML agree on storage CLASS. Integer width
                    # (int vs bigint, INT32 vs INT64) is NOT drift: no run in the corpus has ever
                    # hard-failed on it, and realigning it rewrites the customer's TML for nothing
                    # while earning a "may break the dependents" warning. DOUBLE vs INT64 IS drift
                    # (float vs int) — that is PATIENT_AGE, and it hard-fails.
                    # Unread or unmappable on either side => still shown.
                    _cdw_cmp = tgt_t or src_cdw
                    _cc = type_class(_cdw_cmp) if _cdw_cmp else ""
                    _tc = type_class(src_tml)
                    if _cc and _tc and _cc == _tc:
                        _quiet_agree.append(_scoped)
                        continue
                    _agree = bool(src_cdw and tgt_t and _tnorm(src_cdw) == _tnorm(tgt_t))
                    # REALIGN only when both warehouses agree on a real type and the TML is the stale
                    # outlier — written as the TS TOKEN (INT64, not 'bigint'). Disagree → fix warehouse.
                    _ts = warehouse_type_to_ts(tgt_t) if _agree else ""
                    if _ts and _tnorm(_ts) != _tnorm(src_tml):
                        _ralign = _ts
                        _issue = "TML type is stale; both warehouses agree it should be " + _ts
                    elif not _agree and src_cdw and tgt_t:
                        _ralign = ""
                        _issue = "source vs target warehouse types differ — fix the warehouse, or drop"
                    else:
                        _ralign = ""
                        _issue = "types differ — realign only when both warehouses agree; else drop"
                    if _ralign:
                        _realign_to[_scoped] = _ralign
                    _rows.append({
                        "#":          str(_tbl_serial.get(obj.strip().lower(), "")),
                        "Table":      obj,
                        "Column":     col,
                        "Source CDW": src_cdw.upper() if src_cdw else "(unread)",
                        "Source TML": src_tml or "(?)",
                        "Target CDW": tgt_t.upper() if tgt_t else "(unread)",
                        "Issue":      _issue,
                        "Realign to": _ralign if _ralign else "—",
                        "Realign?":   _scoped in _tmrsel,
                        "Drop?":      _scoped in _tmsel,
                        "_scoped":    _scoped,
                    })
                st.session_state._tm_realign_to = _realign_to   # so "Apply all" can read the tokens

                # ── unusable columns (VOID in the warehouse) — drop, NOT a type mismatch ──
                if _void_rows:
                    st.markdown("#### Columns the warehouse can't type (VOID) — drop")
                    st.caption("These exist in the warehouse but with no usable type (VOID), so they "
                               "can't bind and can't be realigned. Drop them from the promotion (or fix "
                               "the column in the warehouse). This is not a type mismatch.")
                    _vdf = pd.DataFrame(_void_rows, columns=["#", "Table", "Column", "Source CDW",
                                                             "Target CDW", "Drop?", "_scoped"])
                    _vb1, _vb2, _ = st.columns([1, 1, 3])
                    with _vb1:
                        if st.button(f"Drop all ({len(_vdf)})", key="tmvoid_all",
                                     use_container_width=True, disabled=_vdf.empty):
                            _tmsel.update(_vdf["_scoped"].tolist()); _bump_editor("tmvoid"); st.rerun()
                    with _vb2:
                        if st.button("Clear all", key="tmvoid_clear", use_container_width=True,
                                     disabled=not any(_s in _tmsel for _s in _vdf["_scoped"])):
                            for _s in _vdf["_scoped"].tolist():
                                _tmsel.discard(_s)
                            _bump_editor("tmvoid"); st.rerun()
                    _select_editor(
                        _vdf, ["Drop?"], ["tm_drop_selected"], "tmvoid",
                        column_config={
                            "#":          st.column_config.TextColumn("#", width="small"),
                            "Table":      st.column_config.TextColumn("Table", width="medium"),
                            "Column":     st.column_config.TextColumn("Column", width="medium"),
                            "Source CDW": st.column_config.TextColumn("Source CDW", width="small"),
                            "Target CDW": st.column_config.TextColumn("Target CDW", width="small"),
                            "Drop?":      st.column_config.CheckboxColumn("Drop?", width="small",
                                            help="Drop this unusable (VOID) column from the promotion."),
                        },
                        disabled=["#", "Table", "Column", "Source CDW", "Target CDW"])

                # ── genuine type mismatches (realignable / real drift) ──
                if _rows:
                    st.markdown("#### Column type mismatches")
                    st.caption(
                        "The data type disagrees across **source CDW · source TML · target CDW**. "
                        "**Flagged, never auto-corrected.** Realign when both warehouses agree the TML "
                        "is stale; when they genuinely differ, fix the warehouse, or drop as a last "
                        "resort (scoped to that one table).")
                elif not _void_rows:
                    # ACCOUNT for the findings, never just render "empty". ThoughtSpot reported a
                    # type problem; if this table has no row for it, say which column and why, or
                    # the screen reads as "nothing to fix" beside a hard failure.
                    if _quiet_gone:
                        st.caption("No type mismatches left to resolve — already dropped this run: "
                                   + ", ".join(f"`{s.replace('::', '.')}`" for s in sorted(_quiet_gone)))
                    elif _quiet_agree:
                        st.info("No action needed here: the warehouse and the TML agree on the type "
                                "for " + ", ".join(f"`{s.replace('::', '.')}`" for s in
                                                   sorted(_quiet_agree)[:8])
                                + (" and others" if len(_quiet_agree) > 8 else "")
                                + ". If ThoughtSpot still rejected a type, the column is listed under "
                                  "**Other validation errors** below — that is a warehouse/TML "
                                  "disagreement this read couldn't see.")
                    else:
                        st.caption("No type mismatches to resolve here "
                                   "(any already dropped are excluded).")
                # Explicit columns so an empty list renders a harmless 0-row table (no crash).
                _df = pd.DataFrame(_rows, columns=["#", "Table", "Column", "Source CDW", "Source TML",
                                                   "Target CDW", "Issue", "Realign to", "Realign?",
                                                   "Drop?", "_scoped"])
                _q = st.text_input("Filter type mismatches", key="tm_search",
                                   label_visibility="collapsed",
                                   placeholder="🔎 Filter by table or column name").strip().lower()
                _view = _df
                if _q and not _df.empty:
                    _view = _df[_df.apply(lambda r: _q in str(r["Table"]).lower()
                                          or _q in str(r["Column"]).lower(), axis=1)]
                _teb = f"tm::{_q}"
                # select-all buttons over shown rows: realign-all only ticks rows that actually HAVE a
                # realign target ('Realign to' != '—'); drop-all ticks the rest.
                _tb1, _tb2, _tb3, _ = st.columns([1.3, 1, 1, 2])
                _shown = list(_view["_scoped"]) if not _view.empty else []
                _realignable = [row["_scoped"] for _, row in _view.iterrows()
                                if row.get("Realign to") not in ("—", "", None)] if not _view.empty else []
                with _tb1:
                    if st.button(f"Realign all ({len(_realignable)})", key="tm_re_all",
                                 use_container_width=True, disabled=not _realignable):
                        _tmrsel.update(_realignable)
                        for _sk in _realignable:
                            _tmsel.discard(_sk)
                        _bump_editor(_teb); st.rerun()
                with _tb2:
                    if st.button(f"Drop rest ({len(set(_shown) - set(_realignable))})", key="tm_drop_all",
                                 use_container_width=True, disabled=not _shown):
                        for _sk in _shown:
                            if _sk not in _tmrsel:
                                _tmsel.add(_sk)
                        _bump_editor(_teb); st.rerun()
                with _tb3:
                    if st.button("Clear all", key="tm_clear", use_container_width=True,
                                 disabled=not (_tmsel or _tmrsel)):
                        _tmsel.clear(); _tmrsel.clear()
                        _bump_editor(_teb); st.rerun()
                _select_editor(
                    _view, ["Realign?", "Drop?"], ["tm_realign_selected", "tm_drop_selected"], _teb,
                    column_config={
                        "#":          st.column_config.TextColumn("#", width="small",
                                        help="Matches the S.No in the count table above."),
                        "Table":      st.column_config.TextColumn("Table", width="medium"),
                        "Column":     st.column_config.TextColumn("Column", width="medium"),
                        "Source CDW": st.column_config.TextColumn("Source CDW", width="small"),
                        "Source TML": st.column_config.TextColumn("Source TML", width="small"),
                        "Target CDW": st.column_config.TextColumn("Target CDW", width="small",
                                        help="The target warehouse's physical type for this column."),
                        "Issue":      st.column_config.TextColumn("Issue", width="medium"),
                        "Realign to": st.column_config.TextColumn("Realign to", width="small",
                                        help="The TS type token a realign would set on the TML. Only "
                                             "offered when BOTH warehouses agree on a real type and the "
                                             "TML is the stale outlier; blank ('—') when they disagree "
                                             "or the column is VOID (drop / fix the warehouse instead)."),
                        "Realign?":   st.column_config.CheckboxColumn("Realign?", width="small",
                                        help="Set the TML's type to 'Realign to' (approve-first). "
                                             "Preferred over dropping when both warehouses agree."),
                        "Drop?":      st.column_config.CheckboxColumn("Drop?", width="small",
                                        help="Last resort: drop this column (scoped to this table) "
                                             "and its dependents."),
                    },
                    disabled=["#", "Table", "Column", "Source CDW", "Source TML", "Target CDW",
                              "Issue", "Realign to"],
                    exclusive=True)   # Realign? wins over Drop? per row
                st.caption(f"**{len(_tmrsel)}** to realign, **{len(_tmsel)}** to drop. Realigning the "
                           "type is preferred when both warehouses agree (your approval, not automatic); "
                           "drop only as a last resort.")

                if not _discovered and st.button("Apply realign / drops, re-export & re-validate",
                                                 key="tm_apply", disabled=not (_tmsel or _tmrsel)):
                    _items = st.session_state.transformed_items
                    _re = {k: _realign_to[k] for k in _tmrsel if _realign_to.get(k)}
                    if _re:
                        _items, _rn = realign_column_types(_items, _re)
                        st.session_state.setdefault("realign_types", {}).update(_re)   # durable
                    _drop = {k for k in _tmsel if k not in _tmrsel}   # SCOPED; never bare
                    if _drop:
                        _items, _man = drop_columns(_items, _drop)
                        _record_drop(_man)
                        st.session_state.setdefault("dropped_col_names", set()).update(_drop)
                    st.session_state.transformed_items = _items
                    _tmsel.clear(); _tmrsel.clear()
                    filtered_fixed = [i for i in st.session_state.transformed_items
                                      if i.get("info", {}).get("name") not in skip_objects]
                    with st.status("Re-validating…", expanded=True) as _rv:
                        _res = _safe_validate(filtered_fixed, step=lambda _m: _rv.write(_m))
                        _rv.update(state=("complete" if _res else "error"), expanded=False)
                        if _res:
                            _, err, ok = _res
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                            st.session_state.pop("_tm_key2", None)
                    if _res:
                        st.rerun()

            # ── invalid formula IDs: model columns pointing at formulas that don't resolve ──
            fml_drop = set()
            if invalid_formula:
                st.markdown("#### Invalid formula references")
                _all_fml = sorted({fm for f in invalid_formula for fm in f.get("formulas", [])})
                st.caption("These model/worksheet columns reference formulas that no longer resolve "
                           "(orphaned or broken in the source). Import can't proceed while they're "
                           "present. Tick to **drop** the column + its formula from the promotion.")
                for _fm in _all_fml:
                    if st.checkbox(f"Drop invalid-formula column  `{_fm}`",
                                   value=True, key=f"dropfml_{_fm}"):
                        fml_drop.add(_fm)
                if not _discovered and st.button("Drop these & re-validate", key="fml_apply"):
                    if fml_drop:
                        fixed, _man = drop_columns(st.session_state.transformed_items, fml_drop)
                        st.session_state.transformed_items = fixed
                        _record_drop(_man)
                        st.session_state.setdefault("dropped_col_names", set()).update(fml_drop)
                    filtered_fixed = [i for i in st.session_state.transformed_items
                                      if i.get("info", {}).get("name") not in skip_objects]
                    with st.status("Re-validating…", expanded=True) as _rv:
                        _res = _safe_validate(filtered_fixed, step=lambda _m: _rv.write(_m))
                        _rv.update(state=("complete" if _res else "error"), expanded=False)
                        if _res:
                            _, err, ok = _res
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                    if _res:
                        st.rerun()

            # ── dangling references: a formula/column points at a formula that was removed ──
            # This is the class ThoughtSpot reports ONLY as an opaque "Schema validation failed"
            # (it never names the object), so the tool detects it statically. Dropping the referrer
            # + its dependents is what clears the dead-end.
            dang_drop = set()
            if dangling:
                st.markdown("#### Broken references (point to something already removed)")
                st.caption("Detected by the tool — ThoughtSpot reports these only as an unnamed "
                           "“Schema validation failed”. Each references a formula that no longer "
                           "exists in the model (usually dropped as invalid earlier). Import can't "
                           "proceed while they're present.")
                for f in dangling:
                    _nm = f.get("name", "?")
                    _miss = ", ".join(f.get("missing", []))
                    _kindlbl = "formula" if f.get("ref_type") == "formula" else "column"
                    if st.checkbox(f"Drop {_kindlbl}  `{_nm}`  — references `{_miss}` (gone)",
                                   value=True, key=f"dropdang_{f.get('object','')}_{_nm}"):
                        dang_drop.add(_nm)

            # ── whole tables to prune: emptied (0 columns) or disconnected (join key dropped) ──
            tbl_drop = set()
            if drop_table_find:
                st.markdown("#### Tables to drop whole (unusable after column drops)")
                st.caption("Detected by the tool — the platform reports these only as “0 columns” "
                           "or “No matches found for table”. Each has no columns left, or lost the "
                           "join that connected it. Dropping removes the table, its joins, and the "
                           "columns it surfaced in the model.")
                for f in sorted(drop_table_find, key=lambda x: x.get("table", "")):
                    _tn = f.get("table", "?")
                    _rz = "empty — 0 columns left" if f.get("reason") == "empty" else "disconnected — join key dropped"
                    if st.checkbox(f"Drop table  `{_tn}`  ·  _{_rz}_", value=True,
                                   key=f"droptbl_{_tn}"):
                        tbl_drop.add(_tn)
                    if f.get("reason") == "disconnected":
                        st.caption(f"   ↳ keep it instead by restoring its join-key column in the target warehouse")

            # ── join cascade: a model join lost the column/table it referenced (downstream) ──
            if join_unres:
                st.markdown("#### Model joins to re-resolve (downstream of the drops)")
                st.caption("These aren't separate problems — a join references a column/table that's "
                           "being dropped, so it can't translate yet. They clear once the drops "
                           "above are applied and the model re-validates.")
                _jt = sorted({t for f in join_unres for t in (f.get("tables") or [])})
                if _jt:
                    st.markdown("Affected join(s) on: " + ", ".join(f"`{t}`" for t in _jt))
                else:
                    st.caption("(ThoughtSpot didn't name the table — apply the drops and re-validate.)")

            # ── model-column cascade: the table column it needs never got created (downstream) ──
            if model_col_bad:
                st.markdown("#### Model columns waiting on a table column (downstream)")
                st.caption("Not separate problems. Each of these model columns points at a table "
                           "column that failed its own check above, so resolve the column there "
                           "(realign the type where the warehouse is right, drop only as a last "
                           "resort) and these clear on the next validate.")
                for _f in sorted(model_col_bad, key=lambda x: ((x.get("object") or "").lower(),
                                                               (x.get("column") or "").lower())):
                    _where = f"`{_f['object']}`" + (f".`{_f['column']}`" if _f.get("column") else "")
                    _in = f" (in model **{_f['model']}**)" if _f.get("model") \
                        and _f["model"] not in (None, "unknown") else ""
                    st.markdown(f"- {_where}{_in}")

            # ── formulas whose expression lost a column (downstream of a drop) ──
            if formula_bad:
                st.markdown("#### Formulas that lost a column they reference")
                st.caption("Each formula below uses a column that is no longer in the promotion, "
                           "usually one dropped earlier. Nothing is removed for you: keep the "
                           "formula by restoring the column, or drop the formula on the Source "
                           "Audit page if it is no longer wanted.")
                for _f in sorted(formula_bad, key=lambda x: (x.get("formula") or "").lower()):
                    st.markdown(f"- **{_f['formula']}** needs `{_f['missing_ref']}`")

            # ── model tables left with no selected columns (needs a decision, not an auto-prune) ──
            if bare_tables:
                st.markdown("#### Model tables with no columns selected")
                st.caption("ThoughtSpot rejects a model that keeps a table on the canvas without "
                           "surfacing any of its columns, because it can produce unintended cross "
                           "joins between fact tables. This is usually the tail of a drop cascade. "
                           "Two ways out, and the tool won't choose for you: keep one column of "
                           "that table (un-drop it on the Source Audit page), or remove the table "
                           "from the model. Removing a bridge table changes the join graph, so it "
                           "changes the numbers.")
                for _f in sorted(bare_tables, key=lambda x: (x.get("model") or "").lower()):
                    _tl = ", ".join(f"`{t}`" for t in (_f.get("tables") or [])) or "(not named)"
                    st.warning(f"**{_f.get('model') or _f.get('object')}** — {_tl}")

            # ── model columns carrying a property the target doesn't have (e.g. a calendar) ──
            _bad_props = [f for f in findings if f["kind"] == "invalid_column_property"]
            if _bad_props:
                st.markdown("#### Model columns with a property the target doesn't have")
                st.caption("A `calendar` here names a CUSTOM CALENDAR object, and custom calendars "
                           "are cluster-local — a promotion never carries them. So this is almost "
                           "always a calendar that exists on the source and was never created on "
                           "the target. (It is not a schema-version gap: other columns in the same "
                           "model keep their calendars fine.) Removing the property lets the "
                           "promotion land and **reverts that column's date grouping to standard "
                           "periods instead of the custom calendar** — no error, just different "
                           "buckets. Creating the calendar on the target preserves the behaviour. "
                           "The column and its data are untouched either way.")
                _prop_map = {}
                for _f in _bad_props:
                    _pl = ", ".join(f"`{p}`" for p in (_f.get("properties") or [])) or "(not named)"
                    for _c in (_f.get("columns") or ["(column not named)"]):
                        st.warning(f"**{_c}** — target won't accept: {_pl}")
                        if _f.get("properties") and "::" in str(_c):
                            _prop_map[_c] = _f["properties"]
                if _prop_map:
                    _n = sum(len(v) for v in _prop_map.values())
                    if st.button(f"Remove {_n} property/properties, keep the column(s)",
                                 key="drop_col_props"):
                        _fixed, _rm = drop_column_properties(
                            st.session_state.transformed_items, _prop_map)
                        st.session_state.transformed_items = _fixed
                        st.session_state.setdefault("dropped_col_props", {}).update(_prop_map)
                        if _rm:
                            st.session_state._last_drop_report = (
                                "Removed " + ", ".join(f"`{p}` from `{c}`" for c, p in _rm)
                                + ". The column(s) and their data are unchanged.")
                        else:
                            st.session_state._last_drop_report = (
                                "No property was found on those columns to remove — the TML may "
                                "nest it differently. Send the debug bundle.")
                        for _k in ("pr_url", "validation_errors", "validation_ok",
                                   "discovered_findings", "discovered_meta"):
                            st.session_state.pop(_k, None)
                        st.rerun()

            # ── anything unrecognised ──
            if other:
                st.markdown("#### Other validation errors")
                for f in other:
                    st.markdown(f"**{f['object']}**")
                    # The platform's own words, in full and copyable. We do not paraphrase them:
                    # classify_import_errors already extracted the names into the sections above,
                    # and ThoughtSpot's own SOLUTION: line is better than anything we'd guess.
                    st.code(friendly_error(f["error"])[2], language=None)

                # These errors are often unattributed (name "unknown"). Validate each file on its
                # own to name the culprit AND itemize its real error — a missing column becomes a
                # column drop (not a whole-table skip); only genuinely unclassifiable failures fall
                # back to skip-object.
                st.caption("ThoughtSpot didn't say which object failed. Isolate it:")
                if st.button("🔬 Find which object fails (validate each file on its own)",
                             key="isolate_btn"):
                    with st.status("Isolating…", expanded=True) as _iso:
                        _fails = _isolate_failures(filtered_items, progress=st.write)
                        _routed, _opaque = [], []
                        for _r in _fails:
                            _cls = classify_import_errors(
                                [{"name": _r["name"], "status": "ERROR", "error": _r["error"]}])
                            _real = [f for f in _cls if f["kind"] != "other"]
                            if _real:
                                for f in _real:
                                    f["object"] = _r["name"]
                                _routed.extend(_real)
                            else:
                                _opaque.append(_r)
                        # Route classified findings (missing cols / type drift / formulas) back into
                        # the normal column-level resolution so the user drops COLUMNS, not tables.
                        if _routed:
                            _ex = st.session_state.get("discovered_findings", []) or []
                            _seen = {finding_key(f) for f in _ex}
                            _ex = _ex + [f for f in _routed if finding_key(f) not in _seen]
                            st.session_state.discovered_findings = _ex
                        st.session_state.isolation = _opaque   # only unclassifiable -> skip-object
                        _iso.update(label=(f"Itemized {len(_routed)} column-level issue(s)"
                                           + (f", {len(_opaque)} unclassifiable" if _opaque else "")
                                           if (_routed or _opaque) else "No single object failed."),
                                    state="complete", expanded=False)
                    st.rerun()

                # Everything is captured AS IT HAPPENS into small logs under logs/ — raw error
                # responses (validate_raw.jsonl) and every discovery pass's drops (discovery.jsonl).
                # Grab them instantly; no re-running anything.
                st.caption("Captured live as the tool ran — download directly, no re-validation:")
                _dlrow = st.columns(3)
                for _i, (_lf, _lbl) in enumerate([
                        ("validate_raw.jsonl", "⬇ Raw validation errors"),
                        ("import_raw.jsonl",   "⬇ Raw IMPORT responses"),
                        ("discovery.jsonl",    "⬇ Per-pass drop log")]):
                    _lp = Path(__file__).parent / "logs" / _lf
                    with _dlrow[_i]:
                        if _lp.exists() and _lp.stat().st_size:
                            st.download_button(f"{_lbl} ({_lf})", data=_lp.read_bytes(),
                                               file_name=_lf, mime="application/x-ndjson",
                                               key=f"log_dl_{_lf}")
                        else:
                            st.caption(f"_{_lf}: none yet_")

                # The bundle just ZIPS those logs + the current TML — instant. Tick 'deep' only for
                # the rare leave-one-out interaction hunt (many slow validate calls).
                _deep = st.checkbox("Also run leave-one-out bisection (slow — many validate calls)",
                                    value=False, key="dbg_deep")
                if st.button("🐞 Capture debug bundle (logs + all TML)", key="dbg_capture"):
                    from services.debug_dump import capture_zip_bytes
                    from datetime import datetime
                    _ts = datetime.now().strftime("%Y%m%dT%H%M%S")
                    _msg = ("Running leave-one-out (slow on a cold warehouse)…" if _deep
                            else "Packaging logs + TML…")
                    with st.status(_msg, expanded=True) as _dbg:
                        try:
                            _fn, _bytes, _sm = capture_zip_bytes(
                                filtered_items, target_client(), _ts, deep=_deep,
                                target_connection=teams[team_name].get("target_connection", ""),
                                source_items=st.session_state.get("_source_raw_items"))
                            st.session_state._dbg_bundle = (_fn, _bytes)
                            st.session_state._dbg_summary = _sm
                            _cul = (f"; leave-one-out culprits: {_sm.get('leave_one_out_culprits')}"
                                    if _deep else "")
                            _dbg.update(label=f"Captured — {_sm.get('files')} file(s){_cul}.",
                                        state="complete", expanded=True)
                        except Exception as _e:
                            _dbg.update(label=f"Capture failed: {str(_e)[:300]}",
                                        state="error", expanded=True)
                if st.session_state.get("_dbg_bundle"):
                    _fn, _bytes = st.session_state._dbg_bundle
                    st.json(st.session_state.get("_dbg_summary", {}), expanded=False)
                    st.download_button("⬇ Download debug bundle", data=_bytes, file_name=_fn,
                                       mime="application/zip", key="dbg_dl")

                _iso_res = st.session_state.get("isolation")
                if _iso_res:
                    st.markdown("**Objects that fail with an unclassifiable error (skip to proceed):**")
                    _skip_pick = set()
                    for r in _iso_res:
                        if st.checkbox(f"Skip **{r['type']} `{r['name']}`** from this promotion",
                                       value=False, key=f"skipobj_{r['name']}"):
                            _skip_pick.add(r["name"])
                        with st.expander(f"error · {r['name']}"):
                            st.code(r["error"])
                    if _skip_pick and st.button("Skip selected & re-validate", key="skip_apply"):
                        st.session_state.setdefault("skip_objects", set()).update(_skip_pick)
                        st.session_state.pop("isolation", None)
                        st.session_state.pop("discovered_findings", None)
                        st.session_state.pop("discovered_meta", None)
                        _ff = [i for i in st.session_state.transformed_items
                               if i.get("info", {}).get("name") not in st.session_state["skip_objects"]]
                        with st.status("Re-validating…", expanded=True) as _rv:
                            _res = _safe_validate(_ff, step=lambda _m: _rv.write(_m))
                            _rv.update(state=("complete" if _res else "error"), expanded=False)
                            if _res:
                                _, err, ok = _res
                                st.session_state.validation_errors = err
                                st.session_state.validation_ok     = ok
                                st.session_state.pop("silent_drops", None)
                        if _res:
                            st.rerun()

            # ── single "Apply all" — only after discovery produced the complete set ──
            if _discovered:
                _all_drop = set(fml_drop) | set(dang_drop)   # invalid-formula cols + dangling refs (by name)
                # Missing-column ticks live in the data_editor's persistent selection (already scoped
                # obj::col), so fold the whole set in.
                _all_drop |= set(st.session_state.get("wh_drop_selected", set()))
                # Source-absent columns approved in the source-warehouse check drop here too.
                _all_drop |= set(st.session_state.get("src_drop_selected", set()))
                _all_drop |= set(st.session_state.get("src_type_drop_selected", set()))
                # Type-mismatch drops now live in the table's persistent selection (already scoped
                # table::col), replacing the old per-row droptm_ checkboxes.
                _all_drop |= set(st.session_state.get("tm_drop_selected", set()))
                # Type-mismatch REALIGNS (set the TML type, don't drop) — applied first; a realigned
                # column is never also dropped.
                _all_realign = {k: st.session_state.get("_tm_realign_to", {}).get(k)
                                for k in st.session_state.get("tm_realign_selected", set())
                                if st.session_state.get("_tm_realign_to", {}).get(k)}
                _all_drop -= set(_all_realign)
                _tbl_now = set(tbl_drop)   # whole tables to prune (empty / disconnected)
                st.divider()
                _lbl_tbl = f" + {len(_tbl_now)} table(s)" if _tbl_now else ""
                _lbl_re = f"{len(_all_realign)} realign · " if _all_realign else ""
                st.caption("All of the above was found by validating repeatedly until clean — "
                           "tick realign or drop, then resolve everything in a single re-validate.")
                if st.button(f"Apply all resolutions & re-validate  ·  {_lbl_re}{len(_all_drop)} column(s){_lbl_tbl} to drop",
                             type="primary"):
                    if _all_realign:
                        fixed, _rn = realign_column_types(st.session_state.transformed_items, _all_realign)
                        st.session_state.transformed_items = fixed
                        st.session_state.setdefault("realign_types", {}).update(_all_realign)   # durable
                    if _all_drop:
                        fixed, _man = drop_columns(st.session_state.transformed_items, _all_drop)
                        st.session_state.transformed_items = fixed
                        _record_drop(_man)
                        st.session_state.setdefault("dropped_col_names", set()).update(_all_drop)
                    if _tbl_now:
                        # Prune whole tables (empty / orphaned) and persist so a re-export keeps them
                        # dropped — mirrors the not-on-target prune path.
                        pruned, _psum = _prune_tables_whole(st.session_state.transformed_items, _tbl_now)
                        st.session_state.transformed_items = pruned
                        st.session_state.setdefault("prune_tables", set()).update(_tbl_now)
                    filtered_fixed = [i for i in st.session_state.transformed_items
                                      if i.get("info", {}).get("name") not in skip_objects]
                    with st.status("Re-validating…", expanded=True) as _rv:
                        _res = _safe_validate(filtered_fixed, step=lambda _m: _rv.write(_m))
                        _rv.update(state=("complete" if _res else "error"), expanded=False)
                        if _res:
                            _, err, ok = _res
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                            # Clear discovery ONLY after a successful validate — a failed
                            # re-validate (connection reset) must not throw away the discovered set.
                            st.session_state.pop("discovered_findings", None)
                            st.session_state.pop("discovered_meta", None)
                            st.session_state.pop("wh_drop_selected", None)   # ticks consumed
                            st.session_state.pop("src_drop_selected", None)
                            st.session_state.pop("src_type_drop_selected", None)
                            st.session_state.pop("tm_drop_selected", None)
                            st.session_state.pop("tm_realign_selected", None)
                            st.session_state.pop("_source_col_map", None)    # stale after drops
                    if _res:
                        st.rerun()

        elif val_ok or val_ok == []:
            tbls = mdls = leaves = 0
            for i in filtered_items:
                d = _parse_edoc(i.get("edoc", "{}"))
                if "table" in d:
                    tbls += 1
                elif "model" in d or "worksheet" in d:
                    mdls += 1
                elif "liveboard" in d or "answer" in d:
                    leaves += 1
            # Count the DISTINCT columns you chose to drop, not the running removal tally.
            # dropped_cols_count sums every removal manifest, so it (a) counts the cascade — the
            # dependent model columns and formulas a drop takes with it — and (b) re-counts the same
            # drops each time a re-export re-applies the durable skip set. That's how 9 dropped
            # columns reported as 40. The cascade is still shown in Import Results.
            dropped_count = len(st.session_state.get("dropped_col_names", set()) or set())
            # Total distinct removals = the columns you chose PLUS the model columns / formulas that
            # came out with them. Showing both makes the two numbers reconcile instead of looking
            # like a discrepancy (the old single figure double-counted on every re-export).
            _total_removed = len(st.session_state.get("dropped_cascade_names", set()) or set())
            dropped_vizs  = st.session_state.get("dropped_vizs_count", 0)
            msg = f"Validation passed — {tbls} table(s) + {mdls} model(s) OK."
            if dropped_count:
                msg += f" {dropped_count} column(s) dropped"
                if _total_removed > dropped_count:
                    msg += (f" ({_total_removed} removed in total, including dependent "
                            "model columns/formulas)")
                msg += "."
            if dropped_vizs:
                msg += f" {dropped_vizs} dependent viz(s) removed from liveboard(s)."
            if leaves:
                msg += f" {leaves} liveboard/answer(s) will import after."
            st.success(msg)

    _nav(3, can_next="discovered_meta" in st.session_state,
          next_hint="Validate against the target to continue")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Git Operations (merge & promote)
# ══════════════════════════════════════════════════════════════════════════════
elif step == 4:
    transformed_items = st.session_state.get("transformed_items")
    skip_objects   = st.session_state.get("skip_objects", set())
    filtered_items = [i for i in (transformed_items or [])
                      if i.get("info", {}).get("name") not in skip_objects]
    if not transformed_items:
        st.info("Finish TML Validation first — validate the promotion there, then return here to "
                "commit, open the PR, and merge & promote.")
    else:
        st.subheader("Git Operations")
        # Diff what we're about to promote against what's already on main BEFORE committing. A no-op
        # promotion then says so plainly instead of silently producing an empty commit + PR (which is
        # indistinguishable from a change that got lost), and a lost change shows up as "0 changed".
        _new_files  = items_to_files(filtered_items)
        _main_files = None
        try:
            _main_files = git_client().get_tml_files(team_name)
        except Exception as _e:
            st.warning("Couldn't read `main` to compare — "
                       + (_git_error_hint(_e) if _is_github_error(_e) else str(_e)[:200]))
        if _main_files is None:
            _added, _changed = sorted(_new_files), []     # can't compare → treat everything as new
        else:
            _added   = sorted(p for p in _new_files if p not in _main_files)
            _changed = sorted(p for p in _new_files
                              if p in _main_files and _main_files[p] != _new_files[p])
        _unchanged = len(_new_files) - len(_added) - len(_changed)
        _nochange  = (_main_files is not None and not _added and not _changed)
        st.session_state._git_nochange = _nochange

        if _nochange:
            # Say WHEN they landed. "Nothing to commit" on its own reads like a failure, when the
            # usual cause is simply that this same content was committed by an earlier run.
            _when = ""
            try:
                _last = git_client()._repo.get_commits(sha="main")[0].commit.author.date
                _when = f" `main` was last updated {_last:%Y-%m-%d %H:%M} UTC."
            except Exception:
                pass
            st.success(f"**Already on `main` — nothing left to commit.** All {len(_new_files)} "
                       f"promoted file(s) match `main` byte for byte, so a commit and PR would be "
                       f"empty.{_when} Normally this means an earlier run today already committed "
                       "this exact content. You can still re-import to the target below.")
            st.caption("If you changed something and expected it here, the change never reached "
                       "the bundle — go back a step and confirm it was applied and re-exported. "
                       "(An unchanged re-promotion is also why earlier runs produced empty PRs.)")
        else:
            st.markdown(f"**{len(_added)} new · {len(_changed)} changed · {_unchanged} unchanged**")
            for _p in _added:
                st.markdown(f"-  🆕 `{_p}`")
            for _p in _changed:
                st.markdown(f"-  ✏️ `{_p}`")
            import hashlib
            _pr_sig = hashlib.md5(
                "\n".join(sorted(it.get("edoc", "") for it in filtered_items)).encode()).hexdigest()
            # Commit only when the bundle CHANGED since the last commit (content signature), so a
            # rerun doesn't re-commit, but going back to fix a column and returning updates the PR.
            if "pr_url" not in st.session_state or st.session_state.get("_pr_bundle_sig") != _pr_sig:
                with st.spinner("Committing the validated TML to the dev branch and opening the PR…"):
                    try:
                        _gc  = git_client()
                        _sha = _gc.commit_tml(team_name, _new_files)
                        st.session_state.pr_url = _gc.create_pr(team_name, _sha)
                        st.session_state._pr_bundle_sig = _pr_sig
                    except Exception as _e:
                        if _is_github_error(_e):
                            st.error("**The GitHub step failed** (commit / pull request). "
                                     + _git_error_hint(_e))
                            st.caption("Check `GITHUB_TOKEN` / `GITHUB_REPO` in `.env`, and that no "
                                       "stale `GITHUB_TOKEN` is exported in your shell. Nothing was "
                                       "committed or promoted.")
                        else:
                            st.error(f"Couldn't commit / open the PR: {str(_e)[:200]}")
                        st.stop()
            st.markdown(f"**PR:** [{st.session_state.pr_url}]({st.session_state.pr_url})")

        # Import proceeds either way: with a fresh PR, or when main already matches (the no-op case).
        validation_passed = _nochange or ("pr_url" in st.session_state)
        if validation_passed:
            st.divider()
            import_phase = st.session_state.get("import_phase")

            if import_phase == "complete":
                st.success("Import complete.")

            elif import_phase == "leaves_pending":
                # Phase 2: tables + models are imported; liveboards/answers were VALIDATE_ONLY'd
                # against the now-present model, so viz-level errors surface BEFORE they import.
                st.markdown("#### Tables and models imported — review liveboards / answers")
                leaf_errors  = st.session_state.get("import_leaf_errors", [])
                findings     = classify_import_errors(leaf_errors)
                viz_findings = [f for f in findings if f["kind"] == "viz_error"]
                other_leaf   = [f for f in findings if f["kind"] != "viz_error"]

                st.warning(
                    "These visualizations fail to load on Test. Tick one to **drop it** so the rest of "
                    "its liveboard imports cleanly, or go Back and fix the source. Leaving them unticked "
                    "imports them anyway, and the platform skips the broken viz.")
                drop_ids = set()
                for f in viz_findings:
                    for vz in f.get("vizzes", []):
                        lbl = f"Drop **{vz}** in {f['object']}"
                        if f.get("formulas"):
                            lbl += f"  ·  formula: {', '.join(f['formulas'])}"
                        if st.checkbox(lbl, value=False, key=f"dropviz_{f['object']}_{vz}"):
                            drop_ids.add(vz)
                    with st.expander(f"error detail — {f['object']}"):
                        st.code(f.get("error", ""))
                for f in other_leaf:
                    st.markdown(f"- **{f.get('object')}**: {f.get('error','')}")

                if st.button("Import liveboards & answers", type="primary"):
                    leaves     = st.session_state.get("import_leaf_files", {})
                    leaf_items = [{"info": {"name": p}, "edoc": c} for p, c in leaves.items()]
                    dropped_v  = 0
                    if drop_ids:
                        leaf_items, dropped_v = drop_vizzes(leaf_items, drop_ids)
                    leaf_strings = [it["edoc"] for it in leaf_items]
                    with st.spinner("Importing liveboards / answers to the target cluster…"):
                        leaf_results = target_client().import_tml(leaf_strings) if leaf_strings else []
                    st.session_state.import_results = st.session_state.get("import_core_results", []) + leaf_results
                    if dropped_v:
                        st.session_state.dropped_vizs_count = st.session_state.get("dropped_vizs_count", 0) + dropped_v
                    st.session_state.pop("recon_report", None)   # re-verify against target for this run
                    st.session_state.import_phase = "complete"
                    st.rerun()

            else:
                # Phase 1: silent-drop safety net, then merge + import tables/models + validate leaves.
                # A target column absent from the source is dropped on import — SILENTLY when it has no
                # dependents (the platform raises no error). Diff first.
                if "silent_drops" not in st.session_state:
                    with st.spinner("Checking the target for columns that would be dropped…"):
                        st.session_state.silent_drops = _detect_silent_drops(filtered_items)
                silent = st.session_state.silent_drops

                proceed = True
                if silent:
                    # Split by CAUSE. A column you dropped in this run is not a risk, it is the
                    # plan, and describing your own decision back as a hazard trains people to
                    # tick past the panel. Only a column that vanished from the SOURCE without
                    # anyone asking is a surprise worth stopping for.
                    _chosen = scan_names_for_drops(
                        st.session_state.get("dropped_col_names") or set(),
                        st.session_state.get("dropped_cascade_names") or set())
                    _expected, _surprise = [], []
                    for _s in silent:
                        _mine = [c for c in _s["columns"] if c.strip().lower() in _chosen]
                        _theirs = [c for c in _s["columns"] if c.strip().lower() not in _chosen]
                        if _mine:
                            _expected.append((_s["table"], _mine))
                        if _theirs:
                            _surprise.append((_s["table"], _theirs))
                    if _expected:
                        st.info("**As requested** — you dropped these in this run, so the import "
                                "will remove them from the target table. Nothing to decide:")
                        for _t, _cs in _expected:
                            st.markdown(f"- **{_t}**: " + ", ".join(f"`{c}`" for c in _cs))
                    if _surprise:
                        st.warning("**Silent-drop risk** — these are on the target and are NOT in "
                                   "this promotion, and you did not drop them. Import will "
                                   "**remove them from the target table**, and the platform "
                                   "raises no error when they have no dependents:")
                        for _t, _cs in _surprise:
                            st.markdown(f"- **{_t}**: " + ", ".join(f"`{c}`" for c in _cs))
                        st.caption("Usually this means the column was removed at source since the "
                                   "last promotion. To keep one, add it back to the source. "
                                   "Otherwise acknowledge to proceed.")
                        proceed = st.checkbox(
                            "I understand these target columns will be removed — proceed.",
                            key="ack_silent")

                # ── Spotter feedback: merge preview + optional Replace ──
                fb_specs = _feedback_specs(filtered_items) if st.session_state.get("_include_feedback") else []
                replace_ack = True
                if fb_specs:
                    if "_fb_previews" not in st.session_state:
                        with st.spinner("Comparing feedback with the target…"):
                            st.session_state._fb_previews = [
                                feedback_preview(target_client(), m["name"], m["obj_id"], m["entries"])
                                for m in fb_specs]
                    replace_ack = render_feedback_panel(st.session_state._fb_previews)

                # ── Spotter NL instructions: preview + Merge/Replace ──
                nl_models = _nl_models(filtered_items) if st.session_state.get("_include_nl") else []
                nl_ack = True
                if nl_models:
                    if "_nl_previews" not in st.session_state:
                        _nl_edited = st.session_state.get("_nl_edited", {})
                        with st.spinner("Comparing Spotter instructions with the target…"):
                            st.session_state._nl_previews = [
                                {**nl_preview(source_client(), target_client(),
                                              m["source_guid"], m["obj_id"],
                                              source_instructions=_nl_edited.get(m["source_guid"])),
                                 "model": m["name"]}
                                for m in nl_models]
                    nl_ack = render_nl_panel(st.session_state._nl_previews)

                # ── Target dependents of the columns being dropped ────────────────────────────
                # Last gate before import. A column leaving the model can break answers and
                # liveboards that already exist ON THE TARGET, and the operator usually cannot see
                # all of them (GSK has no RBAC/OMS, so there is no limited-admin path).
                _scan_names = scan_names_for_drops(
                    st.session_state.get("dropped_col_names") or set(),
                    st.session_state.get("dropped_cascade_names") or set())
                if _scan_names:
                    st.divider()
                    st.markdown("##### Target objects built on the dropped column(s)")
                    _dep_rows = st.session_state.get("_tgt_dep_rows")
                    _c1, _c2 = st.columns([1.6, 3])
                    with _c1:
                        if st.button("Check the target for dependents", key="chk_tgt_deps"):
                            with st.status("Asking the target what depends on these columns…",
                                           expanded=True) as _ds:
                                try:
                                    _tnames = sorted({s.split("::")[0] for s in
                                                      (st.session_state.get("dropped_col_names") or set())
                                                      if "::" in s})
                                    _ds.write(f"Resolving {len(_tnames)} table(s) on the target…")
                                    _tgt = target_client()
                                    _ids = _tgt._resolve_names_to_ids(_tnames, "LOGICAL_TABLE")
                                    _ds.write(f"Listing dependents of {len(_ids)} table(s)…")
                                    _cand = []
                                    for _sid, _deps in _tgt.list_dependents(
                                            list(_ids.values()), "LOGICAL_TABLE").items():
                                        _cand.extend(_deps)
                                    _seen_d, _uniq = set(), []
                                    for _d in _cand:
                                        if _d.get("id") and _d["id"] not in _seen_d:
                                            _seen_d.add(_d["id"]); _uniq.append(_d)
                                    _ds.write(f"{len(_uniq)} object(s) depend on those tables. "
                                              f"Reading each one to see which use the dropped "
                                              f"column(s)…")
                                    for _d in _uniq:
                                        try:
                                            _raw = _tgt.export_tml([_d["id"]])
                                            _its = _raw if isinstance(_raw, list) else _raw.get("object", [])
                                            _d["tml"] = (_its[0].get("edoc") if _its else None)
                                        except Exception:
                                            _d["tml"] = None
                                    _hits = dependents_using_columns(_uniq, _scan_names)
                                    # An object THIS PROMOTION is updating is not a casualty of the
                                    # drop — it IS the drop. The model being promoted depends on
                                    # its own table, so it lands in the dependents list, and
                                    # deleting it would remove the very thing being updated plus
                                    # everything hanging off it, most of which never touched the
                                    # dropped column. Mark those in-promotion and never offer them.
                                    _promo_names = {(_i.get("info", {}).get("name") or "").strip().lower()
                                                    for _i in (filtered_items or [])}
                                    _promo_names |= {(_n or "").strip().lower() for _n in
                                                     (st.session_state.get("_promo_id2name") or {}).values()}
                                    _promo_names.discard("")
                                    for _h in _hits:
                                        _src = next((x for x in _uniq if x["id"] == _h["id"]), {})
                                        _h["author"] = _src.get("author", "")
                                        _h["label"] = _src.get("label") or _h.get("type")
                                        _h["in_promotion"] = (
                                            (_h.get("name") or "").strip().lower() in _promo_names)
                                    st.session_state._tgt_dep_rows = _hits
                                    st.session_state._tgt_dep_scanned = len(_uniq)
                                    _ds.update(label=f"{len(_hits)} of {len(_uniq)} dependent(s) "
                                                     f"use a dropped column.",
                                               state="complete", expanded=False)
                                except Exception as _e:
                                    _ds.update(label="Couldn't read the target's dependents: "
                                                     + str(_e)[:160], state="error")
                            st.rerun()
                    with _c2:
                        st.caption("Only objects **this account can see**. GSK has no RBAC/OMS, so "
                                   "an answer owned by someone else may exist and not appear here. "
                                   "A clean result is not proof that nothing depends on the column.")
                    if _dep_rows is not None:
                        if not _dep_rows:
                            st.success(f"No visible dependent uses these columns "
                                       f"({st.session_state.get('_tgt_dep_scanned', 0)} object(s) "
                                       f"checked).")
                        else:
                            import pandas as pd
                            _dsel = st.session_state.setdefault("tgt_dep_selected", set())
                            # Never offer an object this promotion is updating. It is listed so the
                            # operator can see WHY it is implicated, but it is not a casualty — the
                            # import rewrites it, and deleting it would take its own dependents with
                            # it, most of which never used the dropped column.
                            _inpromo = {_r.get("id") for _r in _dep_rows if _r.get("in_promotion")}
                            for _gid in list(_dsel):
                                if _gid in _inpromo:
                                    _dsel.discard(_gid)
                            _ddf = pd.DataFrame([{
                                "#": _i + 1,
                                "Object": _r.get("name") or "(unnamed)",
                                "Type": _r.get("label") or _r.get("type") or "",
                                "Author": _r.get("author") or "",
                                "Uses": ", ".join(_r.get("columns") or []) or "unreadable TML",
                                "Tile(s)": (", ".join(v["id"] for v in (_r.get("vizzes") or []))
                                            + (f"  · of {_r.get('viz_total')}"
                                               if _r.get("viz_total") else "")) or "—",
                                "Status": ("in this promotion — updated, not deleted"
                                           if _r.get("in_promotion") else "would break"),
                                "Delete?": (False if _r.get("in_promotion")
                                            else _r.get("id") in _dsel),
                                "_scoped": _r.get("id")} for _i, _r in enumerate(_dep_rows)],
                                columns=["#", "Object", "Type", "Author", "Uses", "Tile(s)",
                                         "Status", "Delete?", "_scoped"])
                            _n_break = sum(1 for _r in _dep_rows if not _r.get("in_promotion"))
                            _n_own = len(_dep_rows) - _n_break
                            st.warning(f"**{_n_break}** target object(s) reference a column this "
                                       "promotion removes and would break, or make the import be "
                                       "rejected."
                                       + (f"  A further **{_n_own}** are part of this promotion "
                                          "and are UPDATED in place — never delete those, it would "
                                          "take their own dependents with them." if _n_own else ""))
                            _select_editor(
                                _ddf, ["Delete?"], ["tgt_dep_selected"], "tgtdep",
                                column_config={
                                    "#":       st.column_config.TextColumn("#", width="small"),
                                    "Object":  st.column_config.TextColumn("Object", width="large"),
                                    "Type":    st.column_config.TextColumn("Type", width="small"),
                                    "Author":  st.column_config.TextColumn("Author", width="medium"),
                                    "Uses":    st.column_config.TextColumn("Uses", width="medium"),
                                    "Tile(s)": st.column_config.TextColumn(
                                        "Tile(s)", width="medium",
                                        help="For a liveboard: the visualisation id(s) that use "
                                             "the dropped column, and the board's total tiles."),
                                    "Status":  st.column_config.TextColumn(
                                        "Status", width="medium",
                                        help="Objects in this promotion are rewritten by the "
                                             "import, so they are never deletion candidates."),
                                    "Delete?": st.column_config.CheckboxColumn(
                                        "Delete?", width="small",
                                        help="Permanently delete this object ON THE TARGET. "
                                             "Ignored for rows that are part of this promotion."),
                                },
                                disabled=["#", "Object", "Type", "Author", "Uses", "Tile(s)",
                                          "Status"])
                            _picked = [r for r in _dep_rows
                                       if r.get("id") in _dsel and not r.get("in_promotion")]
                            if _picked:
                                st.error(f"**{len(_picked)} object(s) will be PERMANENTLY DELETED "
                                         f"on `{opt_env('TS_TARGET_HOST')}`.** This cannot be "
                                         "undone, and it removes someone else's work. Type "
                                         "**DELETE** to confirm.")
                                _typed = st.text_input("Confirm deletion", key="tgt_del_confirm",
                                                       label_visibility="collapsed",
                                                       placeholder="type DELETE")
                                if st.button(f"Delete {len(_picked)} object(s) on the target",
                                             key="do_tgt_del", type="secondary",
                                             disabled=_typed.strip().upper() != "DELETE"):
                                    _res = {}
                                    with st.status("Deleting on the target…", expanded=True) as _dl:
                                        for _r in _picked:
                                            # VERIFY, never trust the status. ps-internal
                                            # 2026-09-22: deleting an object you lack rights to
                                            # returns 204 and changes nothing. Reporting that as
                                            # success would tell the operator a blocking dependent
                                            # was cleared and send them into an import that fails
                                            # for the very reason they thought they had fixed.
                                            try:
                                                _okd, _stt, _detail = \
                                                    target_client().delete_metadata_verified(
                                                        _r.get("type") or "ANSWER", _r["id"])
                                            except Exception as _e:
                                                _okd, _detail = False, str(_e)[:120]
                                            _res[_r["id"]] = "deleted" if _okd else _detail
                                            _dl.write(("✓ " if _okd else "✗ ")
                                                      + f"{_r.get('name')}: {_res[_r['id']]}")
                                        _log_target_delete(opt_env("TS_TARGET_HOST"), team_name,
                                                           _picked, _res)
                                        _ok = sum(1 for v in _res.values() if v == "deleted")
                                        _dl.update(label=f"Deleted {_ok} of {len(_picked)} "
                                                         f"(logged to logs/target_deletes.jsonl)",
                                                   state="complete" if _ok == len(_picked) else "error")
                                    if _ok < len(_picked):
                                        st.warning("Some objects were NOT removed. The API accepts "
                                                   "the request either way, so this means the "
                                                   "account lacks rights on them — they have to go "
                                                   "through their owner or an admin. They are still "
                                                   "listed below and will still block the import.")
                                    st.session_state._tgt_dep_rows = [
                                        r for r in _dep_rows
                                        if _res.get(r.get("id")) != "deleted"]
                                    _dsel.clear()
                                    st.session_state.pop("tgt_del_confirm", None)
                                    st.rerun()
                    st.divider()

                if st.button("Merge & Import to Target", type="primary",
                             disabled=not (proceed and replace_ack and nl_ack)):
                    gc = git_client()

                    if st.session_state.get("_git_nochange"):
                        # main already holds this exact TML — there is nothing to merge. Skip the
                        # merge (and don't manufacture an empty PR just to merge it) and import
                        # straight from what main already has.
                        st.info("No Git changes to merge — `main` already has this exact TML. "
                                "Importing from it directly.")
                    else:
                        with st.spinner("Merging PR…"):
                            merged = gc.merge_pr()
                            if not merged:
                                # PR was already merged — re-commit and open a fresh PR
                                _res = _safe_validate(filtered_items)
                                if not _res:
                                    st.stop()
                                pr_url, err, ok = _res
                                st.session_state.pr_url = pr_url
                                if err:
                                    st.session_state.validation_errors = err
                                    st.session_state.validation_ok = ok
                                    st.session_state.pop("silent_drops", None)
                                    st.rerun()
                                merged = gc.merge_pr()
                            if not merged:
                                st.error("Could not find or create a PR to merge.")
                                st.stop()

                    # Feedback REPLACE (opt-in): free each existing target model's obj_id BEFORE import
                    # so the import creates a fresh model (clean feedback). Deps are re-pointed and the
                    # old model deleted AFTER import (replace_finalize below). Verified inter-org.
                    replace_mode = (st.session_state.get("feedback_mode", "").startswith("Replace")
                                    and st.session_state.get("_include_feedback"))
                    fb_prepped = []
                    if replace_mode:
                        with st.spinner("Preparing feedback Replace (freeing target model obj_ids)…"):
                            fb_prepped = replace_prep(
                                target_client(),
                                [{"name": m["name"], "obj_id": m["obj_id"]}
                                 for m in _feedback_specs(filtered_items)])

                    # Import tables + models first, THEN validate the leaves against the live model so
                    # viz/formula errors are caught here instead of surfacing silently at leaf import.
                    # Snapshot the target's object names BEFORE any import so the results page can
                    # tell Created vs Updated-in-place vs DUPLICATE for each promoted object.
                    promo_types = set()
                    for _it in filtered_items:
                        _d = _parse_edoc(_it.get("edoc", "{}"))
                        if "table" in _d or "model" in _d or "worksheet" in _d:
                            promo_types.add("LOGICAL_TABLE")
                        if "liveboard" in _d:
                            promo_types.add("LIVEBOARD")
                        if "answer" in _d:
                            promo_types.add("ANSWER")
                    st.session_state.pre_import_index = _target_name_index(target_client(), promo_types)

                    with st.spinner("Importing tables & models, then validating liveboards/answers…"):
                        # Import ONLY this run's files. The team folder accumulates TML across
                        # promotions; without this filter the import would re-import unrelated
                        # tables/models from earlier runs (the "10 tables for a 3-table model" bug).
                        cur_paths = set(items_to_files(filtered_items).keys())
                        tml_files = {p: c for p, c in gc.get_tml_files(team_name).items() if p in cur_paths}
                        core     = {p: c for p, c in tml_files.items()
                                    if p.startswith(("tables/", "models/"))}
                        feedback = {p: c for p, c in tml_files.items()
                                    if p.startswith("feedback/")}
                        leaves   = {p: c for p, c in tml_files.items()
                                    if not p.startswith(("tables/", "models/", "feedback/"))}
                        core_results = target_client().import_tml(files_to_tml_strings(core)) if core else []
                        # Feedback imports in a SEPARATE call AFTER tables+models commit. A first-time
                        # model+feedback in ONE batch fails (error 14500: feedback can't resolve the
                        # not-yet-committed model by obj_id) — and under ALL_OR_NONE that rolls the
                        # model back too. Verified live on ps-internal 2026-07-07 (inter-org run).
                        feedback_results = (target_client().import_tml(files_to_tml_strings(feedback))
                                            if feedback else [])
                        core_results = core_results + feedback_results
                        # REPLACE finalize: re-point the old models' dependents onto the fresh models,
                        # delete each old model iff it has no non-feedback dependents left.
                        if fb_prepped:
                            st.session_state.fb_replace_report = replace_finalize(target_client(), fb_prepped)
                        # NL instructions (Spotter coaching) — promoted via the ai/instructions API
                        # now that the target model exists (not part of the TML bundle).
                        if st.session_state.get("_include_nl"):
                            nl_mode = ("replace" if st.session_state.get("nl_mode", "").startswith("Replace")
                                       else "merge")
                            st.session_state.nl_report = nl_promote(
                                source_client(), target_client(), _nl_models(filtered_items),
                                mode=nl_mode, source_map=st.session_state.get("_nl_edited"))
                        st.session_state.import_core_results = core_results
                        st.session_state.import_leaf_files   = leaves
                        leaf_errors = []
                        if leaves:
                            leaf_val   = target_client().import_tml(list(leaves.values()), policy="VALIDATE_ONLY")
                            leaf_errors = [r for r in leaf_val if r["status"] != "OK"]

                    if leaf_errors:
                        st.session_state.import_leaf_errors = leaf_errors
                        st.session_state.import_phase = "leaves_pending"
                        st.rerun()
                    else:
                        with st.spinner("Importing liveboards / answers…"):
                            leaf_results = target_client().import_tml(list(leaves.values())) if leaves else []
                        st.session_state.import_results = core_results + leaf_results
                        st.session_state.pop("recon_report", None)   # re-verify against target for this run
                        st.session_state.import_phase = "complete"
                        st.rerun()

    # No next_hint on Git Operations: the page's own buttons (Export & Validate, Merge &
    # Import) are the guidance, and a persistent ⛔ caption through the whole flow just nags.
    _nav(4, can_next="import_results" in st.session_state)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Import Results
# ══════════════════════════════════════════════════════════════════════════════

elif step == 5:
    st.subheader("Import Results")

    results = st.session_state.get("import_results")

    if not results:
        st.info("No import run yet.")
    else:
        import pandas as pd

        # The API reports models AND tables as LOGICAL_TABLE, so relabel each row with the
        # type we know from the promotion bundle (Table / Model / Liveboard / Answer).
        def _friendly(d):
            if "table" in d:                       return "Table"
            if "model" in d or "worksheet" in d:   return "Model"
            if "liveboard" in d:                   return "Liveboard"
            if "answer" in d:                      return "Answer"
            return ""
        type_by_name   = {}
        detail_by_name = {}   # name -> {obj_id, detail (col/viz count)}
        for it in st.session_state.get("transformed_items", []):
            d = _parse_edoc(it.get("edoc", "{}"))
            ft = _friendly(d)
            for k in ("table", "model", "worksheet", "liveboard", "answer"):
                node = d.get(k)
                if isinstance(node, dict) and node.get("name"):
                    type_by_name[node["name"]] = ft
                    if k in ("table", "model", "worksheet"):
                        extra = f"{len(node.get('columns', []) or [])} cols"
                    elif k == "liveboard":
                        extra = f"{len(node.get('visualizations', []) or [])} viz"
                    else:
                        extra = ""
                    detail_by_name[node["name"]] = {"obj_id": d.get("obj_id", ""), "detail": extra}
                    break

        # Feedback that actually landed: reference-question / business-term counts per model.
        fb_counts = {}
        for it in st.session_state.get("transformed_items", []):
            d = _parse_edoc(it.get("edoc", "{}"))
            if "nls_feedback" in d:
                fb = (d.get("nls_feedback", {}) or {}).get("feedback", []) or []
                fb_counts[it.get("info", {}).get("name", "")] = {
                    "rq": sum(1 for e in fb if e.get("type") == "REFERENCE_QUESTION"),
                    "bt": sum(1 for e in fb if e.get("type") == "BUSINESS_TERM"),
                }

        # Post-import reconciliation: re-query the target and VERIFY the claims against reality,
        # rather than inferring duplicate/updated purely from the pre-import snapshot.
        promoted_objs, expected_fb = [], {}
        for it in st.session_state.get("transformed_items", []):
            d = _parse_edoc(it.get("edoc", "{}"))
            if "nls_feedback" in d:
                expected_fb[it.get("info", {}).get("name", "")] = [
                    e.get("feedback_phrase") for e in (d.get("nls_feedback", {}) or {}).get("feedback", []) or []]
                continue
            for k in ("table", "model", "worksheet", "liveboard", "answer"):
                node = d.get(k)
                if isinstance(node, dict) and node.get("name"):
                    promoted_objs.append({"name": node["name"], "obj_id": d.get("obj_id", ""),
                                          "type": _friendly(d)})
                    break
        if "recon_report" not in st.session_state:
            try:
                with st.spinner("Verifying the promotion against the target…"):
                    st.session_state.recon_report = reconcile(target_client(), promoted_objs, expected_fb)
            except Exception as e:
                st.session_state.recon_report = [{"object": "(reconcile failed)", "type": "",
                                                  "verified": str(e)[:150], "ok": False}]
        recon = st.session_state.recon_report
        real_dupes = {r["object"] for r in recon
                      if r["type"] != "Feedback" and r["verified"].startswith("DUPLICATE")}

        _RAW = {"LOGICAL_TABLE": "Table", "PINBOARD_ANSWER_BOOK": "Liveboard",
                "QUESTION_ANSWER_BOOK": "Answer", "ANSWER": "Answer", "LIVEBOARD": "Liveboard",
                "FEEDBACK": "Feedback"}

        def _row_type(row):
            raw = row.get("type", "") or ""
            if raw == "FEEDBACK":     # feedback shares its model's name, so key off the raw type
                return "Feedback"
            nm = row.get("name", "")
            if nm in type_by_name:
                return type_by_name[nm]
            err = str(row.get("error", "") or "")
            if "Visualization" in err or "pinboard" in err.lower():
                return "Liveboard"
            return _RAW.get(raw, raw)

        pre_index = st.session_state.get("pre_import_index", {})
        # Models rebuilt by feedback Replace get a NEW guid on purpose (old one deleted), so the
        # snapshot-based duplicate check would false-flag them — treat them as rebuilt, not dupes.
        _replaced = {r["model"] for r in (st.session_state.get("fb_replace_report") or [])
                     if r.get("old_model_deleted")}

        def _change(row):
            # DUPLICATE is now RECONCILE-authoritative (verified against the live target), not
            # inferred from the snapshot — so a rebuilt/relabeled object that is actually a single
            # object on the target is no longer false-flagged. Created vs updated still uses the
            # pre-import snapshot (reconcile can't distinguish those two on its own).
            # A WARNING object DID land, so it still earns a change label (created / updated /
            # present). Only a real failure has no landing to describe.
            if is_blocking_result(row):
                return ""
            if row["type"] == "Feedback":
                return "synced"
            if row["name"] in real_dupes:
                return "⚠ DUPLICATE"           # reality-confirmed (2+ same-named objects)
            if row["name"] in _replaced and row["type"] == "Model":
                return "rebuilt (Replace)"
            if not pre_index:
                return "present"               # verified present by reconcile; no snapshot to date it
            pre = pre_index.get(row["name"])
            if not pre:
                return "created"
            return "updated in place"

        def _detail(row):
            if row["type"] == "Feedback":
                c = fb_counts.get(row["name"])
                if c:
                    bits = []
                    if c["rq"]: bits.append(f"{c['rq']} ref Q")
                    if c["bt"]: bits.append(f"{c['bt']} biz term(s)")
                    return " · ".join(bits) or "feedback"
                return "feedback"
            return detail_by_name.get(row["name"], {}).get("detail", "")

        df         = pd.DataFrame(results)[["name", "type", "status", "error", "new_id"]]
        df["type"]   = df.apply(_row_type, axis=1)
        df["change"] = df.apply(_change, axis=1)
        df["detail"] = df.apply(_detail, axis=1)
        df["obj_id"] = df["name"].map(lambda n: detail_by_name.get(n, {}).get("obj_id", ""))
        # An import that landed with a WARNING LANDED. ThoughtSpot uses WARNING to acknowledge
        # something it accepted, so counting it under "Failed" told the operator a successful
        # promotion had failed objects in it.
        _sev = df["status"].fillna("").str.upper()
        success    = df[_sev.isin(["OK", "WARNING"])]
        failed     = df[~_sev.isin(["OK", "WARNING"])]
        warned     = df[_sev == "WARNING"]

        dup_ct = int((success["change"] == "⚠ DUPLICATE").sum()) if not success.empty else 0
        # From → To header: which cluster/team this promotion moved between.
        _src_h = opt_env("TS_SOURCE_HOST").replace("https://", "").rstrip("/") or "source"
        _tgt_h = opt_env("TS_TARGET_HOST").replace("https://", "").rstrip("/") or "target"
        st.markdown(f"Promoted **{len(df)}** object(s):  `{_src_h}`  →  `{_tgt_h}`  ·  team **{team_name}**")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Succeeded",  len(success))
        col2.metric("Failed",     len(failed))
        col3.metric("With a warning", len(warned))
        col4.metric("Duplicates", dup_ct)
        # One roll-up line: how each succeeded object landed on the target + what kinds shifted.
        if not success.empty:
            _created = int((success["change"] == "created").sum())
            _updated = int((success["change"] == "updated in place").sum())
            _present = int(success["change"].isin(["present", "synced", "rebuilt (Replace)"]).sum())
            _bits = []
            if _created: _bits.append(f"**{_created}** created")
            if _updated: _bits.append(f"**{_updated}** updated in place")
            if _present: _bits.append(f"**{_present}** already present")
            _by_type = success["type"].value_counts().to_dict()
            _types = ", ".join(f"{v} {k.lower()}(s)" for k, v in _by_type.items() if k)
            _line = "  ·  ".join(_bits)
            if _types:
                _line = f"{_line}  —  {_types}" if _line else _types
            if _line:
                st.caption("On target: " + _line)

        # Loud banner for duplicates — in-place update is the whole point of obj_id.
        if dup_ct:
            dup_names = list(success[success["change"] == "⚠ DUPLICATE"]["name"])
            st.error(
                "**Duplicate(s) on the target (verified)** — the target has 2+ objects sharing a name: "
                + ", ".join(f"`{n}`" for n in dup_names)
                + ".  Fix in Step 2 → **Fix target obj_ids** (align each to the source obj_id), delete "
                "the stale copy on the target, then re-promote — it will then update in place.")

        # Reconciliation: what was VERIFIED against the live target (not inferred).
        recon_bad = [r for r in recon if not r["ok"]]
        if recon_bad:
            st.error("**Verification found issues on the target:**\n"
                     + "\n".join(f"- `{r['object']}` ({r['type']}): {r['verified']}" for r in recon_bad))
        elif recon:
            st.success(f"Verified against the target: {len(recon)} object(s) present as expected "
                       "(no duplicates, feedback confirmed).")
        with st.expander("Verification detail (re-queried from the target)"):
            for r in recon:
                st.markdown(f"- {'✅' if r['ok'] else '⚠️'} `{r['object']}` · {r['type']} — {r['verified']}")

        # What the promotion dropped / pruned, by name.
        ps           = st.session_state.get("prune_summary") or {}
        prune_names  = sorted(st.session_state.get("prune_tables", set()))
        dropped_cols = sorted(st.session_state.get("dropped_col_names", set()))
        dropped_vizs = st.session_state.get("dropped_vizs_count", 0)
        if prune_names or dropped_cols or dropped_vizs:
            with st.expander("What was dropped / pruned from this promotion",
                             expanded=bool(prune_names or dropped_cols)):
                if prune_names:
                    st.markdown("**Tables pruned from the model:** "
                                + ", ".join(f"`{n}`" for n in prune_names))
                    casc = ", ".join(f"{ps.get(k, 0)} {k}"
                                     for k in ("columns", "joins", "formulas", "vizzes") if ps.get(k))
                    if casc:
                        st.caption("cascade removed: " + casc)
                if dropped_cols:
                    # Tabular, not a run-on list: these are scoped `table::col`, so split them so you
                    # can actually scan which table each dropped column came from.
                    st.markdown(f"**Columns dropped ({len(dropped_cols)})**")
                    _dc_rows = []
                    for _c in dropped_cols:
                        _t, _cn = _c.split("::", 1) if "::" in _c else ("(unscoped)", _c)
                        _dc_rows.append({"Table": _t, "Column": _cn})
                    st.dataframe(_sno(pd.DataFrame(_dc_rows, columns=["Table", "Column"])),
                                 use_container_width=True, hide_index=True)
                if dropped_vizs:
                    st.markdown(f"**Visualizations dropped:** {dropped_vizs}")

        # What the promotion recased to match the warehouse — only the APPROVED recasings that were
        # actually applied (approve-first; proposals the operator didn't approve aren't listed here).
        _applied_recase = st.session_state.get("_recase_applied_set", set())
        _recases = [r for r in (st.session_state.get("_recase_events") or [])
                    if f"{r['table'].strip().lower()}::{r['from'].strip().lower()}" in _applied_recase]
        if _recases:
            with st.expander(f"Recased to match the warehouse ({len(_recases)} column(s), approved)",
                             expanded=True):
                st.caption("Physical column names aligned to the warehouse's exact casing (approved "
                           "in the recasing panel) so the TML binds on import. Logical names unchanged.")
                _rc = pd.DataFrame([{"Table": r["table"], "From": r["from"], "To": r["to"]}
                                    for r in _recases])
                st.dataframe(_sno(_rc), use_container_width=True, hide_index=True)

        # ── Column-level detail: source TML → promoted → target (CDW + TML) ──────────────
        # The working-session ask: per column, two target flags (is it in the target CDW, is it in
        # the target TML), with the source TML checked against the source CDW FIRST so a stale source
        # column reads as stale rather than as a failed promotion. Plus the journey Anuj asked for:
        # what the source had, what the promotion carried, what actually landed on the target.
        st.markdown("#### Column detail — source → promoted → target")
        st.caption("For every column the source TML started with: was it still in the **source "
                   "warehouse** (if not, it was stale, not a promotion failure), did the promotion "
                   "**carry** it, and did it land in the **target warehouse** and the **target "
                   "model**. `—` means that side was never read, so nothing is asserted.")
        if st.button("Load column detail (reads the target's modeled columns)", key="coljourney_load"):
            _pi = st.session_state.get("transformed_items") or []
            _nms = sorted({(_parse_edoc(i.get("edoc", "{}")).get("table") or {}).get("name")
                           for i in _pi
                           if (_parse_edoc(i.get("edoc", "{}")).get("table") or {}).get("name")})
            with st.spinner("Reading the target's modeled columns…"):
                try:
                    st.session_state._landed_col_map = target_client().table_column_cases(_nms)
                except Exception as _e:
                    st.session_state._landed_col_map = {}
                    st.warning("Couldn't read the target's modeled columns: " + str(_e)[:200])
            st.rerun()

        if "_landed_col_map" in st.session_state:
            _src_items = st.session_state.get("_source_raw_items") or []
            _src_cdw   = st.session_state.get("_source_col_map") or {}
            _tgt_cdw   = st.session_state.get("_warehouse_col_map") or {}
            _landed    = st.session_state.get("_landed_col_map") or {}
            _dropped_n = {str(d).strip().lower() for d in
                          (st.session_state.get("dropped_col_names") or set())}
            _recased_n = {str(r).strip().lower() for r in
                          (st.session_state.get("_recase_applied_set") or set())}
            _realign_n = {str(k).strip().lower(): v for k, v in
                          (st.session_state.get("realign_types") or {}).items()}
            # what the promotion actually carried, per table
            _promo_cols = {}
            for _it in (st.session_state.get("transformed_items") or []):
                _t = _parse_edoc(_it.get("edoc", "{}")).get("table") or {}
                if _t.get("name"):
                    _promo_cols[_t["name"].strip().lower()] = {
                        (_c.get("db_column_name") or _c.get("name") or "").strip().lower()
                        for _c in (_t.get("columns") or [])}

            def _flag(_map, _tl, _cl):
                """✓/✗ against a read map; — when that table was never read (assert nothing)."""
                if _tl not in _map:
                    return "—"
                return "✓" if _cl in (_map.get(_tl) or {}) else "✗"

            _cj_rows = []
            for _it in _src_items:
                _t = _parse_edoc(_it.get("edoc", "{}")).get("table") or {}
                _nm = (_t.get("name") or "").strip()
                if not _nm:
                    continue
                _tl = _nm.lower()
                for _c in (_t.get("columns") or []):
                    _dbn = (_c.get("db_column_name") or _c.get("name") or "").strip()
                    if not _dbn:
                        continue
                    _cl, _scoped = _dbn.lower(), f"{_tl}::{_dbn.lower()}"
                    _notes = []
                    if _scoped in _dropped_n or _cl in _dropped_n:
                        _notes.append("dropped")
                    if _scoped in _recased_n:
                        _notes.append("recased")
                    if _scoped in _realign_n:
                        _notes.append(f"realigned→{_realign_n[_scoped]}")
                    _cj_rows.append({
                        "Table":        _nm,
                        "Column":       _dbn,
                        "Source TML":   "✓",
                        "Source CDW":   _flag(_src_cdw, _tl, _cl),
                        "Promoted":     "✓" if _cl in _promo_cols.get(_tl, set()) else "✗",
                        "Target CDW":   _flag(_tgt_cdw, _tl, _cl),
                        "Target TML":   _flag(_landed, _tl, _cl),
                        "Note":         ", ".join(_notes),
                    })
            # Cascade rows: the MODEL columns and formulas removed because a source column they
            # depended on was dropped. They aren't columns of any source TABLE, so without this they'd
            # be invisible here — yet they're most of what a drop actually removes (your 9 ticked
            # columns took 11 of these with them).
            _casc = {str(n).strip() for n in (st.session_state.get("dropped_cascade_names") or set())}
            _phys_ids = set()
            # Only the QUALIFIED `table.column` forms count as "already listed above". drop_columns
            # records physical table columns dotted and model/formula columns bare, so matching bare
            # names here would swallow the very cascade rows we want (a model column often shares its
            # display name with the table column it came from).
            for _it in _src_items:
                _t = _parse_edoc(_it.get("edoc", "{}")).get("table") or {}
                _tn = (_t.get("name") or "").strip()
                for _c in (_t.get("columns") or []):
                    for _d in {(_c.get("db_column_name") or "").strip(),
                               (_c.get("name") or "").strip()}:
                        if _d:
                            _phys_ids.add(f"{_tn}.{_d}".lower())
            _model_nm = "(model)"
            for _it in (st.session_state.get("transformed_items") or []):
                _d = _parse_edoc(_it.get("edoc", "{}"))
                _m = _d.get("model") or _d.get("worksheet")
                if _m and _m.get("name"):
                    _model_nm = _m["name"]
                    break
            for _n in sorted(_casc):
                if _n.lower() in _phys_ids:
                    continue                      # a physical table column — already a row above
                _cj_rows.append({
                    "Table": _model_nm, "Column": _n,
                    "Source TML": "✓", "Source CDW": "n/a", "Promoted": "✗",
                    "Target CDW": "n/a", "Target TML": "✗",
                    "Note": "removed — depended on a dropped column",
                })

            if not _cj_rows:
                st.caption("No source columns to report (the raw source export isn't in this session).")
            else:
                # ONE ROW PER TABLE with counts — how many columns the source TML had, how many the
                # promotion carried, and how many landed in the target model. The per-column rows are
                # kept underneath for drill-down rather than being the default view.
                _per_tbl = {}
                for _r in _cj_rows:
                    if _r["Note"].startswith("removed"):
                        continue          # cascade rows belong to the model, not a source table
                    _e = _per_tbl.setdefault(_r["Table"], {"src": 0, "promo": 0, "land": 0})
                    _e["src"] += 1
                    _e["promo"] += 1 if _r["Promoted"] == "✓" else 0
                    _e["land"] += 1 if _r["Target TML"] == "✓" else 0
                # Two counts, named for WHERE the columns are. "Promoted" and "landed" meant
                # different things to each side of the GSK call, so the table says what this run
                # sent and what the target holds, and nothing else. (The source count and the
                # not-promoted count moved into the per-column drill-down below.)
                _tbl_rows = [{"Table": _t,
                              "Promoted columns": _v["promo"],
                              "Target columns":   _v["land"]}
                             for _t, _v in sorted(_per_tbl.items())]
                _n_land  = sum(1 for r in _cj_rows if r["Target TML"] == "✓")
                _n_drop  = sum(1 for r in _cj_rows if r["Note"].startswith("dropped"))
                _n_casc  = sum(1 for r in _cj_rows if r["Note"].startswith("removed"))
                _n_stale = sum(1 for r in _cj_rows if r["Source CDW"] == "✗")
                st.markdown(f"**{len(_tbl_rows)} table(s)** · {_n_drop} column(s) dropped by choice · "
                            f"{_n_casc} removed as dependents · {_n_stale} stale in the source "
                            f"warehouse · {_n_land} landed on target")
                st.dataframe(_sno(pd.DataFrame(_tbl_rows, columns=[
                    "Table", "Promoted columns", "Target columns"])),
                    use_container_width=True, hide_index=True)
                st.caption("**Promoted columns** = what this run sent. **Target columns** = what "
                           "the target model holds now. They differ when a column was dropped, or "
                           "when the target already carried columns this promotion didn't include.")

                with st.expander(f"Per-column detail ({len(_cj_rows)} row(s))", expanded=False):
                    _cjdf = pd.DataFrame(_cj_rows, columns=["Table", "Column", "Source TML",
                                                            "Source CDW", "Promoted", "Target CDW",
                                                            "Target TML", "Note"])
                    _cjq = st.text_input("Filter column detail", key="coljourney_filter",
                                         label_visibility="collapsed",
                                         placeholder="🔎 Filter by table or column name").strip().lower()
                    if _cjq:
                        _cjdf = _cjdf[_cjdf.apply(lambda r: _cjq in str(r["Table"]).lower()
                                                  or _cjq in str(r["Column"]).lower(), axis=1)]
                    st.dataframe(_sno(_cjdf), use_container_width=True, hide_index=True)

        # Feedback Replace report (only when Replace mode rebuilt a model).
        fb_rep = st.session_state.get("fb_replace_report")
        if fb_rep:
            st.markdown("**Feedback Replace**")
            for r in fb_rep:
                line = (f"- `{r['model']}` — target now carries only the source's feedback; "
                        f"re-pointed {len(r['repointed'])} dependent(s)")
                if r["failed"]:
                    line += f"; ⚠ failed to re-point: {', '.join(r['failed'])}"
                line += ("; old model **deleted**" if r["old_model_deleted"]
                         else f"; old model **kept** (still has: {', '.join(r['kept_deps'])})")
                st.markdown(line)

        # NL instructions (Spotter coaching) report.
        nl_rep = st.session_state.get("nl_report")
        if nl_rep:
            st.markdown("**Spotter instructions**")
            for r in nl_rep:
                bits = []
                if r.get("added"):   bits.append(f"added {len(r['added'])}")
                if r.get("kept"):    bits.append(f"kept {len(r['kept'])} target-only")
                if r.get("dropped"): bits.append(f"dropped {len(r['dropped'])} target-only")
                flag = "✅" if r["status"] == "ok" else "⚠️"
                st.markdown(f"- {flag} `{r['model']}` — {r['status']}"
                            + (f" ({', '.join(bits)}); now {r['count']} instruction(s)" if r["status"] == "ok" else ""))

        st.divider()

        if not success.empty:
            st.markdown("**Succeeded**")
            # Columns (promoted vs target model): how many columns the promotion carried, and how that
            # compares to the target's MODEL before import — NOT the physical warehouse size (which is
            # far larger and only matters for binding, flagged during validation). Promoted = the
            # columns that actually went (post-drop transform); target model = the pre-import snapshot.
            _promoted_cols = {}
            for _it in st.session_state.get("transformed_items", []):
                _dt = (_parse_edoc(_it.get("edoc", "{}")).get("table") or {})
                if _dt.get("name"):
                    _promoted_cols[_dt["name"].strip().lower()] = len(_display_cols(_dt))
            _modeled = st.session_state.get("_target_modeled_map") or {}

            def _shape2(row):
                base = detail_by_name.get(row["name"], {}).get("detail", "")
                if row["type"] != "Table":
                    return base
                promoted = _promoted_cols.get(row["name"].strip().lower())
                if promoted is None:
                    return base
                modeled = _modeled.get(row["name"].strip().lower())
                if modeled is None:
                    return f"{promoted} cols promoted (new table on target)"
                return f"{promoted} cols promoted · {_gap_label(promoted, len(modeled))}"

            success = success.copy()
            success["shape2"] = success.apply(_shape2, axis=1)
            _succ = success[["name", "type", "change", "shape2", "obj_id", "new_id"]].rename(columns={
                "name": "Object", "type": "Type", "change": "State on target (from → to)",
                "shape2": "Columns (promoted vs target model)", "obj_id": "obj_id (shared identity)",
                "new_id": "target GUID"})
            st.dataframe(_sno(_succ), use_container_width=True, hide_index=True)

        if not failed.empty:
            st.markdown("**Failed**")
            _fail = failed[["name", "type", "detail", "status", "error"]].rename(columns={
                "name": "Object", "type": "Type", "detail": "Shape",
                "status": "Status", "error": "Error"})
            st.dataframe(_sno(_fail), use_container_width=True, hide_index=True)
            _show_errors_verbatim(failed.to_dict("records"), "importfail")

    _nav(5)

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
    "2 · obj_id Setup",
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


def _record_drop(man):
    """Accumulate a drop_columns manifest (columns/vizzes/joins/formulas) into session counters
    for the Import Results report."""
    st.session_state.dropped_cols_count = st.session_state.get("dropped_cols_count", 0) + man.get("columns", 0)
    st.session_state.dropped_vizs_count = st.session_state.get("dropped_vizs_count", 0) + man.get("vizzes", 0)
    st.session_state.dropped_joins_count = st.session_state.get("dropped_joins_count", 0) + man.get("joins", 0)
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
                     "status": r.get("status"), "error": (r.get("error") or "")[:1500]}
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
    """Drop whole tables from the promotion: prune them from the model(s) (drop_tables cascades the
    model_tables entry, joins, surfaced columns, formulas, vizzes) AND remove each table's own TML
    item so it is not committed/validated/imported. Returns (new_items, summary)."""
    names = {(n or "").strip().lower() for n in table_names if n}
    if not names:
        return items, {"tables": 0, "columns": 0, "joins": 0, "formulas": 0, "vizzes": 0}
    pruned, summary = drop_tables(items, table_names)
    # remove the pruned tables' own TML items (drop_tables only cleans references, not the item)
    out = []
    for it in pruned:
        d = _parse_edoc(it.get("edoc", "{}"))
        t = d.get("table")
        if t and (t.get("name") or "").strip().lower() in names:
            continue   # this IS one of the dropped tables — drop its item entirely
        out.append(it)
    return out, summary


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


def _nav(step: int, can_next: bool = True, next_hint: str = ""):
    st.divider()
    col_back, col_mid, col_next = st.columns([1, 6, 1])
    with col_back:
        if step > 0 and st.button("← Back", key=f"back_{step}"):
            _go(step - 1)
    with col_next:
        if step < len(STEPS) - 1:
            if can_next:
                if st.button("Next →", type="primary", key=f"next_{step}"):
                    _go(step + 1)
            else:
                # Show a disabled Next so the control never just vanishes, and
                # explain WHY it's blocked instead of leaving the user stuck.
                st.button("Next →", key=f"next_{step}", disabled=True)
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
                f"<div style='text-align:center;padding:6px 0;border-bottom:3px solid #ff4b4b;"
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
            for key in ("picks", "selected_ids", "dep_info", "_resolved_key", "excluded",
                        "_promo_id2name", "_promo_present", "obj_id_status",
                        "table_alignment", "transformed_items", "import_results", "recon_report",
                        "pre_import_index", "dropped_col_names", "dropped_cols_count",
                        "dropped_vizs_count", "prune_summary",
                        "_fb_previews", "feedback_mode", "ack_replace", "fb_replace_report",
                        "_include_feedback", "_export_fb_state"):
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
        df.insert(0, "select", False)
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
        )

        picks       = edited[edited["select"] == True]["id"].tolist()
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
                for _i in dep["table_ids"]:
                    st.session_state.pop(f"inc_{_i}", None)
                    st.session_state.pop(f"ackprune_{_i}", None)
                st.session_state._tables_mode_prev = tmode
            tables_default_include = (tmode == OPT_CREATE)

            for i in dep["leaf_ids"]:
                st.markdown(f"-  `{id2name.get(i, i)}`  ·  leaf  ·  _always promoted_")

            # Models: you can only leave one out if it already exists on the target (there is
            # nothing sensible to prune when you are promoting the model itself).
            for i in dep["model_ids"]:
                nm     = id2name.get(i, i)
                on_tgt = nm in present
                mark   = "on target ✓" if on_tgt else "not on target ✗"
                inc = st.checkbox(f"`{nm}`  ·  model  ·  {mark}",
                                  value=(i not in excluded), key=f"inc_{i}")
                if inc:
                    excluded.discard(i)
                else:
                    excluded.add(i)
                    if not on_tgt:
                        unsafe.append(nm)

            # Tables: untick = skip (bind to the target copy) when on target; when NOT on target
            # it must be pruned out of the model — show the blast radius and require an ack.
            pending_prune = []   # not-on-target tables unticked -> pruned via ONE gate below
            safe_skips    = []   # on-target tables unticked -> bind to target's copy (nothing dropped)
            for i in dep["table_ids"]:
                nm     = id2name.get(i, i)
                on_tgt = nm in present
                mark   = "on target ✓" if on_tgt else "not on target ✗"
                inc = st.checkbox(f"`{nm}`  ·  table  ·  {mark}",
                                  value=tables_default_include, key=f"inc_{i}")
                if inc:
                    excluded.discard(i)
                    prune.discard(nm)
                elif on_tgt:
                    excluded.add(i)
                    prune.discard(nm)   # safe skip: the model binds to the target's copy
                    safe_skips.append(nm)
                else:
                    excluded.add(i)
                    prune.discard(nm)
                    pending_prune.append((nm, table_drop_preview(promo_items, nm)))

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
                    st.error(f"Failed to set obj_id (account needs DATAMANAGEMENT or ADMINISTRATION): {e}")

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
                        st.error(f"Failed to set obj_id (account needs DATAMANAGEMENT or ADMINISTRATION): {e}")

    # ── Matcher: pair source tables to their target counterpart (handles renames) ──
    if selected_ids:
        st.divider()
        st.markdown("#### Table matcher — pair source → target (incl. renamed / drifted)")
        st.caption("Finds each source table's counterpart on the target by **column structure + "
                   "physical coordinates** (not just name), with a confidence score and column "
                   "drift. Use it to align tables the name-based step above can't match (e.g. a "
                   "table renamed on the target). Re-run after changing the selection or obj_ids.")

        def _derive_oid(coords):
            base = (str(coords.get("schema", "")) + "_" + str(coords.get("db_table", ""))).lower()
            return "".join(c if (c.isalnum() or c == "_") else "_" for c in base).strip("_") or "obj"

        if st.button("Run table matcher"):
            from services.table_matcher import match_tables
            with st.spinner("Exporting source + target tables and matching…"):
                raw_s   = source_client().export_tml(selected_ids)
                s_items = raw_s if isinstance(raw_s, list) else raw_s.get("object", [])
                src_docs, src_g = [], {}
                for it in s_items:
                    d = _parse_edoc(it.get("edoc", "{}"))
                    if "table" in d and d["table"].get("name"):
                        src_docs.append(d)
                        src_g[d["table"]["name"]] = (it.get("info") or {}).get("id")

                metas     = target_client().list_metadata("LOGICAL_TABLE")
                truncated = len(metas) > 500
                metas     = metas[:500]
                tgt_g     = {m["name"]: m["id"] for m in metas if m.get("name")}
                tgt_docs  = []
                if metas:
                    raw_t   = target_client().export_tml([m["id"] for m in metas])
                    t_items = raw_t if isinstance(raw_t, list) else raw_t.get("object", [])
                    for it in t_items:
                        td = _parse_edoc(it.get("edoc", "{}"))
                        if "table" in td:
                            tgt_docs.append(td)

                cfg = teams[team_name]
                st.session_state.match_results    = match_tables(
                    src_docs, tgt_docs,
                    db_map=cfg.get("db_map", {}), schema_map=cfg.get("schema_map", {}),
                    source_connection=cfg.get("source_connection", ""),
                    target_connection=cfg.get("target_connection", ""))
                st.session_state.src_name_to_guid = src_g
                st.session_state.tgt_name_to_guid = tgt_g
                st.session_state.tgt_truncated    = truncated
                # Per-table physical remap for MATCH pairs: repoint the promoted table to the
                # TARGET's actual db/schema/db_table so a renamed physical table still binds.
                tr_map = {}
                for r in st.session_state.match_results:
                    if r["decision"] == "MATCH" and r.get("best"):
                        s, t = r["source"], r["best"]["target"]
                        nm = (s.get("name") or "").strip().lower()
                        if nm and t.get("db_table"):
                            tr_map[nm] = {"db": t.get("db", ""), "schema": t.get("schema", ""),
                                          "db_table": t.get("db_table", "")}
                st.session_state.table_remap = tr_map
            st.rerun()

        mr = st.session_state.get("match_results")
        if mr is not None:
            if st.session_state.get("tgt_truncated"):
                st.warning("Target has >500 tables — matched against the first 500 only. "
                           "Scope the target connection to be exhaustive.")

            def _drift(c):
                bits = []
                if c.get("missing_on_target"): bits.append(f"missing→{len(c['missing_on_target'])}")
                if c.get("extra_on_target"):   bits.append(f"extra→{len(c['extra_on_target'])}")
                if c.get("type_mismatch"):     bits.append(f"type→{len(c['type_mismatch'])}")
                return ", ".join(bits) or "identical"

            rows = []
            for r in mr:
                s, b = r["source"], r["best"]
                tgt_oid = b["target"].get("obj_id") if b else ""
                aligned = bool(b and s.get("obj_id") and s["obj_id"] == tgt_oid)
                rows.append({
                    "source table":   s["name"],
                    "best target":    b["target"]["name"] if b else "—",
                    "confidence":     (f"{b['confidence']}%" if b else "—"),
                    "decision":       r["decision"],
                    "col drift":      _drift(b["columns"]) if b else "—",
                    "obj_id aligned": "yes" if aligned else ("n/a" if r["decision"] == "NO_MATCH" else "no"),
                })
            st.dataframe(_sno(pd.DataFrame(rows)), use_container_width=True, hide_index=True)

            review   = [r for r in mr if r["decision"] in ("REVIEW", "AMBIGUOUS")]
            no_match = [r for r in mr if r["decision"] == "NO_MATCH"]
            if review:
                st.warning(f"{len(review)} table(s) are REVIEW/AMBIGUOUS — low confidence or multiple "
                           "candidates. Align/rename those manually before promoting.")
            if no_match:
                st.info(f"{len(no_match)} source table(s) have no target counterpart — created on import.")

            repoint = []
            for r in mr:
                if r["decision"] == "MATCH" and r.get("best"):
                    s, t = r["source"], r["best"]["target"]
                    if ((s.get("db_table", "") or "").lower() != (t.get("db_table", "") or "").lower()
                            or (s.get("db", "") or "").lower() != (t.get("db", "") or "").lower()
                            or (s.get("schema", "") or "").lower() != (t.get("schema", "") or "").lower()):
                        repoint.append(f"`{s['name']}` → {t.get('db')}.{t.get('schema')}.{t.get('db_table')}")
            if repoint:
                st.success("Matched tables whose physical binding differs will be **repointed to the "
                           "target's table** on promotion (no need to match names by hand):\n"
                           + "\n".join(f"- {x}" for x in repoint))

            needing = [r for r in mr if r["decision"] == "MATCH" and r["best"]
                       and not (r["source"].get("obj_id")
                                and r["source"]["obj_id"] == r["best"]["target"].get("obj_id"))]
            if needing and st.button(f"Align {len(needing)} matched pair(s) — set shared obj_id",
                                     type="primary"):
                src_g = st.session_state.get("src_name_to_guid", {})
                tgt_g = st.session_state.get("tgt_name_to_guid", {})
                src_up, tgt_up, skipped = [], [], []
                for r in needing:
                    s, t  = r["source"], r["best"]["target"]
                    canon = s.get("obj_id") or _derive_oid(s)
                    sg, tg = src_g.get(s["name"]), tgt_g.get(t["name"])
                    if not sg or not tg:
                        skipped.append(s["name"]); continue
                    if not s.get("obj_id"):
                        src_up.append({"identifier": sg, "new_obj_id": canon})
                    tgt_up.append({"identifier": tg, "new_obj_id": canon})
                try:
                    with st.spinner(f"Aligning {len(tgt_up)} pair(s)…"):
                        if src_up:
                            source_client().update_obj_ids(src_up)
                        if tgt_up:
                            target_client().update_obj_ids(tgt_up)
                    st.success(f"Aligned {len(tgt_up)} pair(s)."
                               + (f"  Skipped (no GUID): {', '.join(skipped)}" if skipped else ""))
                    st.session_state._objids_dirty = True   # export is now stale
                    for _k in ("obj_id_status", "_raw_items", "table_alignment", "prod_by_name",
                               "prod_leaf", "dev_table_refs", "dev_model_refs", "dev_leaf_refs",
                               "match_results"):
                        st.session_state.pop(_k, None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Align failed (account needs DATAMANAGEMENT or ADMINISTRATION): {e}")

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
# STEP 2 — Git Operations
# ══════════════════════════════════════════════════════════════════════════════

elif step == 2:
    st.subheader("Git Operations")

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
            raw   = source_client().export_tml(selected_ids)
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
            # Hive_metastore casing (authoritative, direct). ThoughtSpot's connection/search 504s on
            # hive_metastore because it introspects columns via <catalog>.information_schema, which
            # hive lacks. So for a hive target we read the true casing straight from Databricks via
            # SHOW COLUMNS (works on hive AND Unity Catalog). Gated on target DBX creds in .env; when
            # present this fills/overrides the map for tables the fast path can't see (not yet on the
            # target) — e.g. a promoted model whose `CID` must bind to the warehouse's `cid`.
            _dbx_host = opt_env("TS_TARGET_DBX_HOST")
            _dbx_wh   = opt_env("TS_TARGET_DBX_WAREHOUSE")
            _dbx_tok  = opt_env("TS_TARGET_DBX_TOKEN")
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
            # A fresh export resets any prior warehouse (CDW) read — re-verify on demand below.
            st.session_state._column_case_map = column_case_map
            st.session_state._warehouse_col_map = {}
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
            st.write("③ Applying the data-layer transform (connection remap, obj_ids, column casing)…")
            transformed_items, warnings = transform_items(
                items,
                source_connection=teams[team_name].get("source_connection", ""),
                target_connection=teams[team_name].get("target_connection", ""),
                db_map=teams[team_name].get("db_map", {}),
                schema_map=teams[team_name].get("schema_map", {}),
                table_remap=st.session_state.get("table_remap", {}),
                column_case_map=column_case_map,
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

        # Opt-in authoritative warehouse read. Kept OFF the export critical path because COLUMN
        # introspection can be very slow / 504 on some warehouses (GSK). Missing-column checks work
        # without it (falling back to the target's modeled columns, flagged 'unverified'); click
        # this to upgrade them to warehouse-verified when the connection can answer.
        _coords = st.session_state.get("_promoted_coords") or []
        if _coords:
            _bcol1, _bcol2 = st.columns([3, 2])
            with _bcol1:
                if st.session_state.get("_warehouse_col_map"):
                    st.caption(f"✅ Warehouse-verified columns for "
                               f"{len(st.session_state['_warehouse_col_map'])} table(s).")
                else:
                    st.caption("Column checks use the target's **modeled** columns (fast). Verify "
                               "against the warehouse for authoritative results — may be slow.")
            with _bcol2:
                if st.button("🔌 Verify columns against warehouse", help="Reads columns via the "
                             "connection. Slow / can time out on some warehouses; never blocks export."):
                    _tconn = st.session_state.get("_promoted_tgt_conn") or ""
                    _groups = {}
                    for _p in _coords:
                        _eff = _tconn or _p.get("connection") or ""
                        if _eff:
                            _groups.setdefault(_eff, []).append(_p)
                    _wh_new = {}
                    with st.status("Reading warehouse columns via the connection…",
                                   expanded=True) as _wh_status:
                        for _cn, _tbls in _groups.items():
                            st.write(f"connection `{_cn}` — {len(_tbls)} table(s)…")
                            try:
                                for _k, _v in target_client().connection_column_cases(
                                        _cn, _tbls).items():
                                    _wh_new.setdefault(_k, _v)
                            except Exception as _e:
                                st.write(f"⚠ `{_cn}` failed: {str(_e)[:200]}")
                        _wh_status.update(
                            label=f"Warehouse read done — {len(_wh_new)} table(s) resolved.",
                            state=("complete" if _wh_new else "error"), expanded=False)
                    st.session_state._warehouse_col_map = _wh_new
                    st.rerun()

        # Table shape at a glance: how many columns the SOURCE promotes vs how many the TARGET
        # warehouse actually has. A gap on either side is what drives adds (source>target) or
        # silent drops (target>source), so surface it up front.
        _wh = st.session_state.get("_warehouse_col_map") or {}
        _shape_rows = []
        for i in filtered_items:
            d = _parse_edoc(i.get("edoc", "{}"))
            t = d.get("table")
            if not t or not t.get("name"):
                continue
            src_n = len(t.get("columns", []) or [])
            wh    = _wh.get(t["name"].strip().lower())
            tgt_n = len(wh) if wh is not None else None
            _shape_rows.append({
                "Table": t["name"],
                "Source cols": src_n,
                "Target warehouse cols": tgt_n if tgt_n is not None else "— (not read)",
                "Δ": (src_n - tgt_n) if tgt_n is not None else "",
            })
        if _shape_rows:
            with st.expander(f"Table column counts — source vs target warehouse ({len(_shape_rows)} table(s))",
                             expanded=False):
                import pandas as pd
                st.caption("Δ = source − target warehouse. Positive → source has columns the "
                           "warehouse lacks (must add or drop). Negative → warehouse has extras the "
                           "source omits (dropped on the target unless carried through).")
                st.dataframe(_sno(pd.DataFrame(_shape_rows)), use_container_width=True, hide_index=True)

        # ── Skip specific columns (leave a column out without touching the rest) ──
        _tbl_cols = {}   # table name -> [column display names]
        for i in filtered_items:
            d = _parse_edoc(i.get("edoc", "{}"))
            t = d.get("table")
            if t and t.get("name"):
                _tbl_cols[t["name"]] = [(c.get("name") or c.get("db_column_name") or "")
                                        for c in (t.get("columns") or []) if (c.get("name") or c.get("db_column_name"))]
        if _tbl_cols:
            _skip_now = st.session_state.get("skip_columns", set())
            with st.expander("Skip specific columns (optional) — leave a column out, keep the rest",
                             expanded=bool(_skip_now)):
                st.caption("Pick columns to exclude from this promotion. The rest of the table "
                           "promotes normally; any viz that uses a skipped column is dropped too.")
                _picked = set()
                for _tn, _cols in _tbl_cols.items():
                    _sel = st.multiselect(
                        f"`{_tn}` — columns to skip", options=sorted(_cols),
                        default=sorted(c for c in _cols if c in _skip_now),
                        key=f"skipcols_{_tn}")
                    _picked.update(_sel)
                if st.button("Apply column skips & re-export", disabled=(_picked == _skip_now)):
                    st.session_state.skip_columns = _picked
                    # Force a clean re-export so the skip set is applied from a fresh bundle
                    # (avoids compounding drops on an already-edited bundle).
                    st.session_state.pop("transformed_items", None)
                    for _k in ("pr_url", "validation_errors", "validation_ok", "dropped_col_names",
                               "dropped_cols_count", "dropped_vizs_count"):
                        st.session_state.pop(_k, None)
                    st.rerun()

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

            # Validate ONLY this run's files (what we just committed), not the whole team
            # folder — the repo accumulates TML across promotions, and reading the folder
            # would re-validate/re-import unrelated tables from earlier runs. Tables first:
            # table validation surfaces a missing column (err 14536) / drop-blocked deps;
            # models catch the rest.
            val_strings = ([c for p, c in files.items() if p.startswith("tables/")]
                           + [c for p, c in files.items() if p.startswith("models/")])
            if not val_strings:
                return pr_url, [], []
            _tick(f"③ Validating {len(val_strings)} table/model file(s) against the target…")
            results = target_client().import_tml(val_strings, policy="VALIDATE_ONLY")
            # Record the raw run so consecutive validates are diffable (why did the finding set
            # change?) — persisted to logs/validate_runs.jsonl and kept for the inline expander.
            st.session_state._last_validate = _log_validate(files, results)
            ok  = [r for r in results if r["status"] == "OK"]
            err = [r for r in results if r["status"] != "OK"]
            return pr_url, err, ok

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
                    _tick(f"Pass {passes}: {friendly_error(str(_e))[0] or 'validation failed'}")
                    reason = "request_failed"
                    break
                _log_validate(files, results)
                errs = [r for r in results if r["status"] != "OK"]
                if not errs:
                    clean = True; reason = "clean"
                    break
                found = classify_import_errors(errs)
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
                        for _f in classify_import_errors(
                                [{"name": _r["name"], "status": "ERROR", "error": _r["error"]}]):
                            _f["object"] = _r["name"]
                            _itemized.append(_f)
                    if _itemized:
                        found = _itemized
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
                        drop_set.update(f.get("columns", []))
                    elif f["kind"] == "viz_error":
                        viz_set.update(f.get("vizzes", []))
                    elif f["kind"] == "invalid_formula_ids":
                        drop_set.update(f.get("formulas", []))   # drop by formula name
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
            return list(seen.values()), clean, passes, reason

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
                    bad = [r for r in res if r["status"] != "OK"]
                    if bad:
                        failures.append({"name": nm, "type": "table",
                                         "error": (bad[0].get("error") or "")[:800]})
                except Exception as e:
                    failures.append({"name": nm, "type": "table", "error": f"request failed: {str(e)[:200]}"})
            # Only isolate models once tables are clean, so a table fault isn't misattributed.
            if not failures:
                for it in models:
                    i += 1
                    nm = it.get("info", {}).get("name", "?")
                    _tick(f"{i}/{total}: model `{nm}`…")
                    try:
                        res = target_client().import_tml(all_table_strings + _strings([it]),
                                                         policy="VALIDATE_ONLY")
                        bad = [r for r in res if r["status"] != "OK"]
                        if bad:
                            failures.append({"name": nm, "type": "model",
                                             "error": (bad[0].get("error") or "")[:800]})
                    except Exception as e:
                        failures.append({"name": nm, "type": "model", "error": f"request failed: {str(e)[:200]}"})
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
            _found, _clean, _passes, _reason = _discover_all_issues(items, progress=_prog)
            try:
                _bar.empty()
            except Exception:
                pass
            if _reason == "request_failed" and not _found and st.session_state.get("discovered_findings"):
                status_ctx.update(label="Couldn't reach the target — kept the previous discovery.",
                                  state="error", expanded=False)
                return
            st.session_state.discovered_findings = _found
            st.session_state.discovered_meta = {"clean": _clean, "passes": _passes, "reason": _reason}
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
                                 "error": _msg[:1500]}]}
                _h, _a, _ = friendly_error(_msg)
                st.error("Validation couldn't reach the target — " + (_h or "the connection failed."))
                st.caption("→ " + (_a or "Try again; the client auto-retries transient resets."))
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

        def _target_col_types(mismatches):
            """For each type_mismatch finding, read the target table's ACTUAL type for that
            column (dev's type is in the error). Returns {(object, column_lower): type}.
            The target logical table normally mirrors the warehouse; view access suffices."""
            tgt   = target_client()
            names = sorted({f["object"] for f in mismatches if f.get("object")})
            out   = {}
            if not names:
                return out
            name_to_id = tgt._resolve_names_to_ids(names, "LOGICAL_TABLE")
            sigs = {}
            if name_to_id:
                raw    = tgt.export_tml(list(name_to_id.values()))
                titems = raw if isinstance(raw, list) else raw.get("object", [])
                for it in titems:
                    td = _parse_edoc(it.get("edoc", "{}"))
                    if "table" in td and td["table"].get("name"):
                        sigs[td["table"]["name"]] = column_signature(td)
            for f in mismatches:
                out[(f["object"], f["column"].lower())] = sigs.get(f["object"], {}).get(f["column"].lower(), "")
            return out

        def _target_col_usage(mismatches):
            """Column-PRECISE target impact. Stage 1: table-level dependents (one call).
            Stage 2: export those objects + scan each for the actual column reference.
            Returns {(object, column_lower): {"affected":[{name,kind,where}], "total":int, "missing"?}}."""
            tgt = target_client()
            by_table = {}
            for f in mismatches:
                if f.get("object") and f.get("column"):
                    by_table.setdefault(f["object"], []).append(f["column"])
            name_to_id = tgt._resolve_names_to_ids(list(by_table), "LOGICAL_TABLE")
            out = {}
            for tbl, cols in by_table.items():
                tid = name_to_id.get(tbl)
                if not tid:
                    for c in cols:
                        out[(tbl, c.lower())] = {"affected": [], "total": 0, "missing": True}
                    continue
                deps    = tgt.list_dependents([tid], "LOGICAL_TABLE").get(tid, [])
                dep_ids = [d["id"] for d in deps if d.get("id")]
                items   = []
                if dep_ids:
                    raw    = tgt.export_tml(dep_ids)
                    titems = raw if isinstance(raw, list) else raw.get("object", [])
                    items  = [{"edoc": it.get("edoc", "{}")} for it in titems]
                for c in cols:
                    out[(tbl, c.lower())] = {"affected": column_usage(items, c), "total": len(deps)}
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

        # ── Stage 1: Export → commit/PR → discover ALL issues in one action ─────
        # No separate single-validate step: the primary button commits the TML + opens the PR,
        # then loops VALIDATE_ONLY (on a throwaway copy) until clean, surfacing every issue at
        # once. It never touches the connection/search COLUMN path that 504s.
        _dm = st.session_state.get("discovered_meta")
        _lbl = "🔎 Re-discover all issues" if _dm else "🔎 Export & discover all issues"
        if st.button(_lbl, type="primary", disabled=not filtered_items,
                     help="Commits the TML + opens the PR, then validates a throwaway copy "
                          "repeatedly until clean — surfacing every issue at once."):
            with st.status("Committing & discovering all issues…", expanded=True) as _disc:
                try:
                    st.write("Committing TML + opening the PR…")
                    _gc = git_client()
                    _sha = _gc.commit_tml(team_name, items_to_files(filtered_items))
                    st.session_state.pr_url = _gc.create_pr(team_name, _sha)
                except Exception as _e:
                    _disc.update(label=f"Couldn't open the PR: {str(_e)[:150]}",
                                 state="error", expanded=False)
                    st.stop()
                _run_discover(filtered_items, _disc)
            st.rerun()
        if "pr_url" in st.session_state:
            st.markdown(f"**PR:** [{st.session_state.pr_url}]({st.session_state.pr_url})")
        if _dm:
            _rtail = {"clean": " · validated clean",
                      "no_progress": " · stopped before clean (remaining errors can't be auto-resolved)",
                      "request_failed": " · stopped: connection to the target failed — warm the warehouse and retry"}
            st.caption(f"Discovery: {len(st.session_state.get('discovered_findings', []))} issue(s) "
                       f"over {_dm['passes']} pass(es)" + _rtail.get(_dm.get("reason", ""), ""))

        # Raw validation run log — so consecutive runs are diffable (which files were validated,
        # each file's status/error). Full history appended to logs/validate_runs.jsonl.
        _lv = st.session_state.get("_last_validate")
        if _lv:
            _n_err = sum(1 for r in _lv["results"] if r["status"] != "OK")
            with st.expander(f"Validation run log — {len(_lv['results'])} file(s), {_n_err} error(s) "
                             f"· {_lv['ts']}  (full history in logs/validate_runs.jsonl)"):
                import pandas as pd
                st.dataframe(_sno(pd.DataFrame(_lv["results"])[["name", "status", "error"]]),
                             use_container_width=True, hide_index=True)

        # ── Stage 2: Column drop (if validation failed) ────────────────────
        val_errors = st.session_state.get("validation_errors", [])
        val_ok     = st.session_state.get("validation_ok", [])

        # Findings come from the discovery probe (the complete union across passes) when it has
        # run; otherwise from the single latest validate. `_discovered` also switches Stage-2 to a
        # single "Apply all" (no per-section re-validate round-trips).
        _discovered = bool(st.session_state.get("discovered_findings"))
        if val_errors or _discovered:
            findings     = st.session_state.get("discovered_findings") or classify_import_errors(val_errors)
            wh_missing   = [f for f in findings if f["kind"] == "missing_in_target_warehouse"]
            dep_blocked  = [f for f in findings if f["kind"] == "drop_blocked_by_dependents"]
            type_mismatch = [f for f in findings if f["kind"] == "type_mismatch"]
            invalid_formula = [f for f in findings if f["kind"] == "invalid_formula_ids"]
            dangling     = [f for f in findings if f["kind"] == "dangling_ref"]
            drop_table_find = [f for f in findings if f["kind"] == "drop_table"]
            other        = [f for f in findings if f["kind"] == "other"]

            # VALIDATE_ONLY reports only the FIRST missing column per table, so the reviewer
            # otherwise fixes them one-per-round. Diff every promoted table against the TARGET
            # CONNECTION's own column set (the CDW — the source of truth 14536 checks against),
            # fetched at export, to surface EVERY missing column at once. This CDW diff is the
            # complete, authoritative list for any table the connection could read, so it REPLACES
            # the one-per-round validation findings for those tables (no duplicate rows). For a
            # table the warehouse couldn't answer for, we keep the validation-confirmed finding and
            # fall back to the org-modeled column set (flagged 'unverified').
            # Skip the CDW merge when the discovery probe already produced the complete union.
            if not _discovered:
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
            _issue_msg = f"Validation found {len(findings)} issue(s) to resolve before import."
            if _unverified:
                _issue_msg += (f"  {_unverified} column(s) below could not be checked against the "
                               "warehouse (marked ⚠︎ unverified).")
            st.error(_issue_msg)

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
                st.markdown("#### Columns missing from the target warehouse")
                st.caption(
                    "Referenced by the source but absent from the target warehouse, so the TML "
                    "cannot import as-is. **Default is to keep them** — add the column to the target "
                    "warehouse, then re-run. Tick a column only to **drop** it from this promotion "
                    "(along with any visualization that uses it).")
                st.caption("Checked against the **target connection** (the warehouse itself), so this "
                           "is the complete set — not one column per re-validate.")
                if any(not f.get("verified") for f in wh_missing):
                    st.caption("⚠︎ **unverified** rows are for a table whose warehouse could not be "
                               "read; they are inferred from the target's modeled columns and may "
                               "include a column that actually exists in the warehouse. Verify before "
                               "dropping.")
                # Select-all: tick/untick every missing-column drop at once.
                def _toggle_all_wh():
                    _v = st.session_state.get("selall_wh", False)
                    for _f in wh_missing:
                        st.session_state[f"dropwh_{_f['object']}_{_f['column']}"] = _v
                st.checkbox(f"**Select all** {len(wh_missing)} column(s) to drop",
                            key="selall_wh", on_change=_toggle_all_wh)
                drop_set = set()
                _promo_items = st.session_state.get("transformed_items", [])
                for f in wh_missing:
                    parts = (f.get("column_fqn") or "").split(".")
                    tbl   = parts[-2] if len(parts) >= 2 else f.get("object", "")
                    mark  = "" if f.get("verified") else "⚠︎ unverified · "
                    if st.checkbox(
                            f"{mark}Drop  `{f['column']}`   ·   table `{tbl}`   ·   {f['connection']}",
                            value=False, key=f"dropwh_{f['object']}_{f['column']}"):
                        # QUALIFIED drop: scope to THIS table's column (obj::col). A bare column
                        # name drops that column from EVERY table that has it and cascade-removes
                        # every join keyed on it — e.g. dropping the flagged dim_cid_targets::CID as
                        # bare "CID" also strips fact_subnational_cid_bridge::CID and collapses all
                        # six CID joins, gutting the model. Scoping keeps the drop to the one column.
                        drop_set.add(f"{f['object']}::{f['column']}" if f.get("object") else f["column"])
                    # Blast radius. Distinguish a real cascade (a join / formula / viz that would
                    # BREAK) from the trivial case where a model just exposes the column 1:1 (where
                    # is only "column") — the latter is expected propagation, not extra loss, so it
                    # reads as a calm one-liner instead of an alarming expander.
                    usage = column_usage(_promo_items, f["column"])
                    breaking   = [u for u in usage if any(w != "column" for w in u["where"])]
                    model_maps = [u for u in usage if u not in breaking]
                    if breaking:
                        kinds = {}
                        for u in breaking:
                            kinds[u["kind"]] = kinds.get(u["kind"], 0) + 1
                        summ = ", ".join(f"{n} {k}{'' if n == 1 else 's'}" for k, n in kinds.items())
                        with st.expander(f"↳ dropping `{f['column']}` breaks {len(breaking)} dependent(s) "
                                         f"on the source — {summ}"):
                            for u in breaking:
                                st.markdown(f"- **{u['kind']}** · {u['name']} — {', '.join(u['where'])}")
                    elif model_maps:
                        _nm = ", ".join(u["name"] for u in model_maps)
                        st.caption(f"↳ dropping `{f['column']}` removes the column (and its mapping in "
                                   f"{len(model_maps)} model(s): {_nm}); no join/formula/viz depends on it.")
                    else:
                        st.caption(f"↳ `{f['column']}` has no dependents in the promotion — dropping "
                                   "it removes only the column.")
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
                    filtered_fixed = [i for i in st.session_state.transformed_items
                                      if i.get("info", {}).get("name") not in skip_objects]
                    with st.spinner("Re-committing and re-validating…"):
                        _res = _safe_validate(filtered_fixed)
                        if _res:
                            pr_url, err, ok = _res
                            st.session_state.pr_url            = pr_url
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                    if _res:
                        st.rerun()

            # ── target-extra with dependents: the drop is blocked on the target ──
            if dep_blocked:
                st.markdown("#### Target columns with dependents (drop blocked)")
                for f in dep_blocked:
                    st.warning(
                        f"Promoting **{f['object']}** would remove target column(s) "
                        + ", ".join(f"`{c}`" for c in f["columns"])
                        + " that the target still uses: "
                        + ", ".join(f"**{d}**" for d in f["dependents"]) + ".\n\n"
                        "Resolve by **preserving** the column (add it back to the source) or by "
                        "**removing those dependents** on the target, then re-run.")

            # ── type drift: column exists on both sides, types differ ──
            if type_mismatch:
                st.markdown("#### Column type mismatches (warehouse drift)")
                st.caption(
                    "These columns exist on both clusters but the **target warehouse**'s physical "
                    "type differs from dev's. **Dev is the source of truth**, so the fix is to align "
                    "the target warehouse to dev — not to alter the promoted content. Dropping is a "
                    "last resort; if a join/formula depends on the column, dropping cascade-removes "
                    "those too (previewed before you confirm).")

                tm_key = tuple(sorted((f["object"], f["column"]) for f in type_mismatch))
                if st.session_state.get("_tm_key") != tm_key:
                    with st.spinner("Reading target types and scanning dependents for this column…"):
                        st.session_state._tm_types = _target_col_types(type_mismatch)
                        st.session_state._tm_usage = _target_col_usage(type_mismatch)
                        st.session_state._tm_key    = tm_key
                tgt_types = st.session_state.get("_tm_types", {})
                tgt_usage = st.session_state.get("_tm_usage", {})

                tm_drop = set()
                for f in type_mismatch:
                    tgt_t = tgt_types.get((f["object"], f["column"].lower()), "")
                    test_str = f"`{tgt_t.upper()}`" if tgt_t else "`(differs — see warehouse)`"
                    st.markdown(
                        f"**`{f['column']}`**  ·  {f['object']}  —  dev: `{f['source_type']}`,  test: {test_str}")
                    st.caption(
                        f"Align the target warehouse: set `{f['column_fqn']}` to `{f['source_type']}` "
                        f"on connection **{f['connection']}** to match dev.")

                    deps = column_dependents(st.session_state.transformed_items, [f["column"]])
                    bits = []
                    if deps["joins"]:    bits.append("joins: "    + ", ".join(deps["joins"]))
                    if deps["formulas"]: bits.append("formulas: " + ", ".join(deps["formulas"]))
                    if deps["vizzes"]:   bits.append("vizzes: "   + ", ".join(str(v) for v in deps["vizzes"]))
                    if bits:
                        st.caption("In-promotion dependents — " + "  ·  ".join(bits))

                    # Target-side blast radius, COLUMN-PRECISE: of all objects on the table,
                    # which actually reference THIS column (and where).
                    usage = tgt_usage.get((f["object"], f["column"].lower()))
                    if usage is not None:
                        if usage.get("missing"):
                            st.caption(f"Target-side: `{f['object']}` not found on Test, no dependents to scan.")
                        else:
                            aff, total = usage["affected"], usage["total"]
                            if aff:
                                kinds = {}
                                for a in aff:
                                    kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
                                ksum = ", ".join(f"{n} {k}{'' if n == 1 else 's'}" for k, n in kinds.items())
                                st.caption(
                                    f"Target-side impact (column-precise): **{len(aff)} of {total}** objects on "
                                    f"the table actually use this column — {ksum}.")
                                with st.expander(f"Show the {len(aff)} affected object(s) on Test"):
                                    for a in aff:
                                        st.markdown(f"- **{a['kind']}** · {a['name']} · {', '.join(a['where'])}")
                            else:
                                st.caption(
                                    f"Target-side impact: none of the {total} objects on the table use this "
                                    "column (only the table definition itself).")

                    # Aligning the target warehouse type is still the recommended fix. But dropping is
                    # no longer BLOCKED when a join/formula depends — drop_columns cascade-removes
                    # them. Preview exactly what the drop would take with it.
                    _casc = column_drop_cascade(st.session_state.transformed_items, [f["column"]])
                    if _casc["joins"] or _casc["formulas"] or _casc["vizzes"]:
                        _bits = []
                        if _casc["joins"]:    _bits.append(f"{_casc['joins']} join(s)")
                        if _casc["formulas"]: _bits.append("formulas: " + ", ".join(_casc["formulas"]))
                        if _casc["vizzes"]:   _bits.append(f"{_casc['vizzes']} viz(es)")
                        st.caption("↳ dropping also cascade-removes — " + "  ·  ".join(_bits)
                                   + ".  Prefer aligning the target warehouse type instead.")
                    if st.checkbox(
                            f"Drop `{f['column']}` from this promotion (cascades the above)",
                            value=False, key=f"droptm_{f['object']}_{f['column']}"):
                        tm_drop.add(f["column"])

                if not _discovered and st.button("Apply drops, re-export & re-validate", key="tm_apply"):
                    if tm_drop:
                        fixed, _man = drop_columns(st.session_state.transformed_items, tm_drop)
                        st.session_state.transformed_items  = fixed
                        _record_drop(_man)
                        st.session_state.setdefault("dropped_col_names", set()).update(tm_drop)
                    filtered_fixed = [i for i in st.session_state.transformed_items
                                      if i.get("info", {}).get("name") not in skip_objects]
                    with st.spinner("Re-committing and re-validating…"):
                        _res = _safe_validate(filtered_fixed)
                        if _res:
                            pr_url, err, ok = _res
                            st.session_state.pr_url            = pr_url
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                            st.session_state.pop("_tm_key", None)
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
                    with st.spinner("Re-committing and re-validating…"):
                        _res = _safe_validate(filtered_fixed)
                        if _res:
                            pr_url, err, ok = _res
                            st.session_state.pr_url            = pr_url
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

            # ── anything unrecognised ──
            if other:
                st.markdown("#### Other validation errors")
                for f in other:
                    st.markdown(f"**{f['object']}**")
                    headline, action, raw = friendly_error(f["error"])
                    if headline:
                        st.markdown(f"- {headline}")
                        if action:
                            st.caption(f"→ {action}")
                        with st.expander("Raw error"):
                            st.code(raw)
                    else:
                        for line in raw.split("\n"):
                            line = line.strip()
                            if line:
                                st.markdown(f"- {line}")

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
                _dlrow = st.columns(2)
                for _i, (_lf, _lbl) in enumerate([
                        ("validate_raw.jsonl", "⬇ Raw validation errors"),
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
                        with st.spinner("Re-committing and re-validating…"):
                            _res = _safe_validate(_ff)
                            if _res:
                                pr_url, err, ok = _res
                                st.session_state.pr_url            = pr_url
                                st.session_state.validation_errors = err
                                st.session_state.validation_ok     = ok
                                st.session_state.pop("silent_drops", None)
                        if _res:
                            st.rerun()

            # ── single "Apply all" — only after discovery produced the complete set ──
            if _discovered:
                _all_drop = set(fml_drop) | set(dang_drop)   # invalid-formula cols + dangling refs (by name)
                for f in wh_missing + type_mismatch:
                    _pre = "dropwh_" if f["kind"] == "missing_in_target_warehouse" else "droptm_"
                    if st.session_state.get(f"{_pre}{f['object']}_{f['column']}"):
                        _obj = (f.get("object") or "").strip()
                        _all_drop.add(f"{_obj}::{f['column']}" if _obj else f["column"])
                _tbl_now = set(tbl_drop)   # whole tables to prune (empty / disconnected)
                st.divider()
                _lbl_tbl = f" + {len(_tbl_now)} table(s)" if _tbl_now else ""
                st.caption("All of the above was found by validating repeatedly until clean — "
                           "tick what to drop, then resolve everything in a single re-validate.")
                if st.button(f"Apply all resolutions & re-validate  ·  {len(_all_drop)} column(s){_lbl_tbl} to drop",
                             type="primary"):
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
                    with st.spinner("Re-committing and re-validating…"):
                        _res = _safe_validate(filtered_fixed)
                        if _res:
                            pr_url, err, ok = _res
                            st.session_state.pr_url            = pr_url
                            st.session_state.validation_errors = err
                            st.session_state.validation_ok     = ok
                            st.session_state.pop("silent_drops", None)
                            # Clear discovery ONLY after a successful validate — a failed
                            # re-validate (connection reset) must not throw away the discovered set.
                            st.session_state.pop("discovered_findings", None)
                            st.session_state.pop("discovered_meta", None)
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
            dropped_count = st.session_state.get("dropped_cols_count", 0)
            dropped_vizs  = st.session_state.get("dropped_vizs_count", 0)
            msg = f"Validation passed — {tbls} table(s) + {mdls} model(s) OK."
            if dropped_count:
                msg += f" {dropped_count} column(s) dropped."
            if dropped_vizs:
                msg += f" {dropped_vizs} dependent viz(s) removed from liveboard(s)."
            if leaves:
                msg += f" {leaves} liveboard/answer(s) will import after."
            st.success(msg)

        # ── Stage 3: Merge & Import (only when validation passed) ──────────
        validation_passed = "pr_url" in st.session_state and not val_errors
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
                    st.warning("**Silent-drop risk** — these columns exist on the target but not in the "
                               "source, so import will **remove them from the target table** (no platform "
                               "error when they have no dependents):")
                    for s in silent:
                        st.markdown(f"- **{s['table']}**: " + ", ".join(f"`{c}`" for c in s["columns"]))
                    st.caption("To keep one, add it back to the source. Otherwise acknowledge to proceed.")
                    proceed = st.checkbox("I understand these target columns will be removed — proceed.",
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

                if st.button("Merge & Import to Target", type="primary",
                             disabled=not (proceed and replace_ack and nl_ack)):
                    gc = git_client()

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
    _nav(2, can_next="import_results" in st.session_state)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Import Results
# ══════════════════════════════════════════════════════════════════════════════

elif step == 3:
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
            if row["status"] != "OK":
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
        success    = df[df["status"] == "OK"]
        failed     = df[df["status"] != "OK"]

        dup_ct = int((success["change"] == "⚠ DUPLICATE").sum()) if not success.empty else 0
        # From → To header: which cluster/team this promotion moved between.
        _src_h = opt_env("TS_SOURCE_HOST").replace("https://", "").rstrip("/") or "source"
        _tgt_h = opt_env("TS_TARGET_HOST").replace("https://", "").rstrip("/") or "target"
        st.markdown(f"Promoted **{len(df)}** object(s):  `{_src_h}`  →  `{_tgt_h}`  ·  team **{team_name}**")
        col1, col2, col3 = st.columns(3)
        col1.metric("Succeeded",  len(success))
        col2.metric("Failed",     len(failed))
        col3.metric("Duplicates", dup_ct)
        # From → To rollup: how each succeeded object landed on the target.
        if not success.empty:
            _created = int((success["change"] == "created").sum())
            _updated = int((success["change"] == "updated in place").sum())
            _present = int(success["change"].isin(["present", "synced", "rebuilt (Replace)"]).sum())
            _bits = []
            if _created: _bits.append(f"**{_created}** created (new on target)")
            if _updated: _bits.append(f"**{_updated}** updated in place (same obj_id)")
            if _present: _bits.append(f"**{_present}** already present / synced")
            if _bits:
                st.caption("State on target: " + "  ·  ".join(_bits))

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

        # What kinds of assets shifted.
        if not success.empty:
            by_type = success["type"].value_counts().to_dict()
            st.caption("Shifted: " + ", ".join(f"{v} {k.lower()}(s)" for k, v in by_type.items() if k))

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
                    st.markdown("**Columns dropped:** " + ", ".join(f"`{c}`" for c in dropped_cols))
                if dropped_vizs:
                    st.markdown(f"**Visualizations dropped:** {dropped_vizs}")

        # What the promotion recased to match the target warehouse (otherwise a silent change).
        _recases = st.session_state.get("_recase_events") or []
        if _recases:
            with st.expander(f"Recased to match the target warehouse ({len(_recases)} column(s))",
                             expanded=True):
                st.caption("Physical column names auto-adjusted to the warehouse's exact casing so "
                           "the TML binds on import. Logical column names are unchanged.")
                _rc = pd.DataFrame([{"Table": r["table"], "From": r["from"], "To": r["to"]}
                                    for r in _recases])
                st.dataframe(_sno(_rc), use_container_width=True, hide_index=True)

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
            # Columns (source → warehouse): proves the promoted columns line up with the target
            # warehouse. Source = raw source export; warehouse = read directly at export (the hive /
            # CDW casing read). "warehouse not read" = the target couldn't be introspected.
            _src_cols = {}
            for _it in st.session_state.get("_source_raw_items", []):
                _dt = (_parse_edoc(_it.get("edoc", "{}")).get("table") or {})
                if _dt.get("name"):
                    _src_cols[_dt["name"]] = [(_c.get("db_column_name") or "").strip().lower()
                                              for _c in (_dt.get("columns") or []) if _c.get("db_column_name")]
            _col_map = st.session_state.get("_column_case_map") or {}

            def _shape2(row):
                base = detail_by_name.get(row["name"], {}).get("detail", "")
                if row["type"] != "Table":
                    return base
                src = _src_cols.get(row["name"])
                if src is None:
                    return base
                wh = _col_map.get(row["name"].strip().lower())
                if not wh:
                    return f"{len(src)} cols → warehouse not read"
                missing = [c for c in src if c not in wh]
                return f"{len(src)} → {len(wh)} cols " + ("✓" if not missing else f"⚠ {len(missing)} missing")

            success = success.copy()
            success["shape2"] = success.apply(_shape2, axis=1)
            _succ = success[["name", "type", "change", "shape2", "obj_id", "new_id"]].rename(columns={
                "name": "Object", "type": "Type", "change": "State on target (from → to)",
                "shape2": "Columns (source → warehouse)", "obj_id": "obj_id (shared identity)",
                "new_id": "target GUID"})
            st.dataframe(_sno(_succ), use_container_width=True, hide_index=True)

        if not failed.empty:
            st.markdown("**Failed**")
            _fail = failed[["name", "type", "detail", "status", "error"]].rename(columns={
                "name": "Object", "type": "Type", "detail": "Shape",
                "status": "Status", "error": "Error"})
            st.dataframe(_sno(_fail), use_container_width=True, hide_index=True)

    _nav(3)

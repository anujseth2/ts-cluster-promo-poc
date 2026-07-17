"""
Validation debug capture.

When a promotion fails with an opaque "Schema validation failed" (name "unknown") and per-file
isolation finds no single culprit, the failure is almost certainly a BATCH-LEVEL interaction and
our normalized error surface (import_tml keeps only status.error_message, truncated) has thrown
away the detail the raw API returned.

This module captures EVERYTHING needed to debug that, straight from the wire:

  00_manifest.json        — run metadata: target host, connection, file inventory, counts.
                            NO auth token / secret is ever written.
  01_batch_raw.json       — FULL raw VALIDATE_ONLY response for the tables+models batch
                            (the exact set + order the discover probe validates).
  01b_tables_only_raw.json— raw VALIDATE_ONLY of tables alone (is the fault table-side?).
  01c_models_only_raw.json— raw VALIDATE_ONLY of models alone (is the fault model-side?).
  02_per_file_raw/        — each file validated ALONE, full raw (does anything fail in isolation?).
  03_model_with_tables/   — each model validated WITH all tables, full raw.
  04_leave_one_out.json   — remove each file, validate the rest: the removal that makes the batch
                            PASS names the interaction culprit.
  files/                  — every TML file, full content (so the bundle is self-contained).
  logs/validate_runs.jsonl— copied if present (the pass-by-pass history).

The whole folder is zipped for easy transfer. The zip contains data-model TML and the target
host/connection NAME (no credentials) — review before sharing outside a trusted channel.
"""

import io
import json
import os
import zipfile

from services.tml_transformer import items_to_files

_IMPORT_PATH = "/api/rest/2.0/metadata/tml/import"


def _raw_validate(client, tml_strings, timeout=180):
    """Call VALIDATE_ONLY directly and return the FULL raw response — bypassing import_tml's
    normalization/truncation, which is what hides the real error. Never raises: transport
    failures are captured as a record so the bundle is always complete."""
    if not tml_strings:
        return {"skipped": "no files"}
    url = f"{client.host}{_IMPORT_PATH}"
    payload = {"metadata_tmls": list(tml_strings), "import_policy": "VALIDATE_ONLY"}
    rec = {"file_count": len(tml_strings), "status_code": None}
    try:
        resp = client._session.post(url, json=payload, timeout=timeout)
        rec["status_code"] = resp.status_code
        try:
            rec["json"] = resp.json()
        except ValueError:
            rec["text"] = resp.text[:40000]
    except Exception as e:  # transport-level (reset/timeout) — record, don't crash the capture
        rec["transport_error"] = f"{type(e).__name__}: {str(e)[:400]}"
    return rec


def _has_error(raw):
    """Best-effort: did this raw validate response contain any non-OK object? Used only to label
    the leave-one-out result; the raw JSON is always kept regardless."""
    if not isinstance(raw, dict):
        return None
    if raw.get("status_code") not in (None, 200):
        return True
    if "transport_error" in raw:
        return None
    data = raw.get("json")
    if data is None:
        return None
    rows = data if isinstance(data, list) else data.get("object", data)
    if not isinstance(rows, list):
        return None
    for item in rows:
        if not isinstance(item, dict):
            continue
        resp = item.get("response", item)
        status = (resp.get("status") or {})
        code = status.get("status_code", "OK")
        if code and code != "OK":
            return True
    return False


def _split(files):
    """(tables, models, leaves) as lists of TML strings, keyed by folder prefix."""
    tables = {p: c for p, c in files.items() if p.startswith("tables/")}
    models = {p: c for p, c in files.items() if p.startswith("models/")}
    leaves = {p: c for p, c in files.items()
              if not p.startswith(("tables/", "models/", "feedback/"))}
    return tables, models, leaves


def capture(items, client, out_root, timestamp, target_host="", target_connection="",
            deep=True):
    """Write a full validation-debug bundle for `items` (the promotion set) and zip it.

    items           : the transformed promotion bundle (session_state.transformed_items or the
                      filtered set actually being validated).
    client          : the TARGET TSClient (its _session carries auth; auth is never written out).
    out_root        : directory to create the bundle under (e.g. a "debug" dir).
    timestamp       : caller-supplied timestamp string (keeps this fn deterministic/testable).
    deep            : run the per-file + leave-one-out passes (many validate calls; slow on a cold
                      warehouse). Set False for just the batch/tables/models raw capture.

    Returns {"dir", "zip", "summary"}.
    """
    run_dir = os.path.join(out_root, f"debug_{timestamp}")
    os.makedirs(os.path.join(run_dir, "files"), exist_ok=True)

    files = items_to_files(items)
    tables, models, leaves = _split(files)
    # The exact set + order the discover probe validates: tables first, then models.
    batch = list(tables.values()) + list(models.values())

    # ── write every TML file (self-contained bundle) ──
    for path, content in files.items():
        dest = os.path.join(run_dir, "files", path.replace("/", "__"))
        with open(dest, "w") as fh:
            fh.write(content)

    summary = {"files": len(files), "tables": len(tables), "models": len(models),
               "leaves": len(leaves)}

    # ── 01: raw batch + tables-only + models-only ──
    batch_raw = _raw_validate(client, batch)
    _write(run_dir, "01_batch_raw.json", batch_raw)
    _write(run_dir, "01b_tables_only_raw.json", _raw_validate(client, list(tables.values())))
    _write(run_dir, "01c_models_only_raw.json", _raw_validate(client, list(models.values())))
    summary["batch_has_error"] = _has_error(batch_raw)

    if deep:
        # ── 02: each file ALONE ──
        per_dir = os.path.join(run_dir, "02_per_file_raw")
        os.makedirs(per_dir, exist_ok=True)
        per_fail = []
        for path, content in files.items():
            if path.startswith("feedback/"):
                continue
            raw = _raw_validate(client, [content])
            _write_path(os.path.join(per_dir, path.replace("/", "__") + ".json"), raw)
            if _has_error(raw):
                per_fail.append(path)
        summary["per_file_failures"] = per_fail

        # ── 03: each model WITH all tables ──
        mwt_dir = os.path.join(run_dir, "03_model_with_tables_raw")
        os.makedirs(mwt_dir, exist_ok=True)
        for path, content in models.items():
            raw = _raw_validate(client, list(tables.values()) + [content])
            _write_path(os.path.join(mwt_dir, path.replace("/", "__") + ".json"), raw)

        # ── 04: leave-one-out over the batch — the removal that flips it to PASS is the culprit ──
        loo = []
        batch_pairs = list(tables.items()) + list(models.items())
        for i, (path, _c) in enumerate(batch_pairs):
            subset = [c for j, (_p, c) in enumerate(batch_pairs) if j != i]
            raw = _raw_validate(client, subset)
            err = _has_error(raw)
            loo.append({"removed": path, "remaining_has_error": err,
                        "status_code": raw.get("status_code"),
                        "flips_to_pass": (err is False)})
        _write(run_dir, "04_leave_one_out.json", loo)
        summary["leave_one_out_culprits"] = [x["removed"] for x in loo if x["flips_to_pass"]]

    # ── manifest (no auth) ──
    manifest = {
        "timestamp": timestamp,
        "target_host": target_host or getattr(client, "host", ""),
        "target_connection": target_connection,
        "counts": summary,
        "file_inventory": [{"path": p, "bytes": len(c)} for p, c in sorted(files.items())],
        "note": "Auth token/secret intentionally omitted. Contains data-model TML + host/connection name.",
    }
    _write(run_dir, "00_manifest.json", manifest)

    # ── copy the run history + as-you-go raw error log if present ──
    for cand in ("logs/validate_runs.jsonl", "logs/validate_raw.jsonl",
                 "validate_runs.jsonl", "validate_raw.jsonl"):
        if os.path.exists(cand):
            os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
            with open(cand) as src, open(os.path.join(run_dir, "logs", os.path.basename(cand)), "w") as dst:
                dst.write(src.read())

    zip_path = run_dir + ".zip"
    _zip_dir(run_dir, zip_path)
    return {"dir": run_dir, "zip": zip_path, "summary": summary}


def capture_zip_bytes(items, client, timestamp, **kw):
    """Convenience for Streamlit: build the bundle in a temp dir and return (filename, bytes) for
    st.download_button, plus the summary. Writes under the system temp dir."""
    import tempfile
    root = tempfile.mkdtemp(prefix="tsdebug_")
    res = capture(items, client, root, timestamp, **kw)
    with open(res["zip"], "rb") as fh:
        data = fh.read()
    return os.path.basename(res["zip"]), data, res["summary"]


def _write(run_dir, name, obj):
    _write_path(os.path.join(run_dir, name), obj)


def _write_path(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, default=str)


def _zip_dir(run_dir, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, fnames in os.walk(run_dir):
            for fn in fnames:
                full = os.path.join(root, fn)
                zf.write(full, os.path.relpath(full, os.path.dirname(run_dir)))

"""
ThoughtSpot REST API v2 client.
Wraps metadata search (by tag), dependency resolution, TML export, and TML import.

One TSClient = one cluster. The tool builds two: a source client and a target
client, each with its own host + credentials.
"""

import json
import re
import time
import yaml
from datetime import datetime

import requests
from typing import List, Dict, Optional

# Transient network failures worth retrying — e.g. WinError 10054 (connection reset by a
# gateway/proxy) or a read timeout while a slow warehouse-validate is in flight.
_TRANSIENT = (requests.exceptions.ConnectionError,
              requests.exceptions.Timeout,
              requests.exceptions.ChunkedEncodingError)
_RETRY_BACKOFF = (3, 8, 20)   # seconds between attempts

from services.tml_transformer import extract_model_refs, extract_table_refs


METADATA_TYPES = ["LOGICAL_TABLE", "LIVEBOARD", "ANSWER"]
LEAF_TYPES     = ["LIVEBOARD", "ANSWER"]

# subtype that identifies Models (formerly Worksheets)
MODEL_SUBTYPE = "PRIVATE_WORKSHEET"


class TSClient:
    def __init__(self, host: str, token: str = "",
                 username: str = "", password: str = "", org_id: str = "",
                 proxy: str = ""):
        self.host      = host.rstrip("/")
        self._username = username
        self._password = password
        self._org_id   = org_id
        self._session  = requests.Session()
        # trust_env=True (default) already honours HTTPS_PROXY/HTTP_PROXY. On a
        # corporate Windows box behind an authenticating proxy (e.g. McAfee Web
        # Gateway), set proxy to the gateway URL; Windows integrated auth is then
        # supplied by the OS. See README for the SSPI note.
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if username and password:
            self._session_login()
        elif token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        # Debug breadcrumbs (opt-in): the raw JSON of the most recent import/validate, and an
        # optional rolling log path. When set, import_tml appends the FULL raw response of any
        # call that comes back with an error — so a failure is already captured as it happens,
        # no expensive one-shot re-capture needed. Never carries auth.
        self.last_raw_import = None
        self.debug_raw_log = None

    def _session_login(self):
        """Login via session cookie — correctly scopes to org_id."""
        payload = {
            "username": self._username,
            "password": self._password,
        }
        if self._org_id:
            payload["org_identifier"] = self._org_id
        resp = self._session.post(
            f"{self.host}/api/rest/2.0/auth/session/login",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()

    def refresh_token(self):
        """Re-login to refresh session (call if session expires)."""
        if self._username and self._password:
            self._session_login()

    def _post(self, path: str, payload: dict) -> dict:
        # Retry transient connection resets (WinError 10054 from a proxy/gateway dropping the
        # connection) with backoff — a bare RST means the request almost certainly never reached
        # the server, so a retry is safe. HTTP errors (raise_for_status) are NOT retried.
        url  = f"{self.host}{path}"
        last = None
        for attempt in range(3):
            try:
                resp = self._session.post(url, json=payload, timeout=60)
                if resp.status_code == 401 and self._username and self._password:
                    self._session_login()
                    resp = self._session.post(url, json=payload, timeout=60)
                resp.raise_for_status()
                return resp.json()
            except _TRANSIENT as e:
                last = e
                if attempt < 2:
                    time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
                    continue
                raise
        raise last

    # ── Metadata search by tag ──────────────────────────────────────────────────

    @staticmethod
    def _row(item: dict, obj_type: str) -> dict:
        header = item.get("metadata_header", {}) or {}

        def _fmt(ms):
            try:
                return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else ""
            except (TypeError, ValueError, OSError):
                return ""

        author = (header.get("authorDisplayName") or header.get("authorName")
                  or header.get("author_display_name") or header.get("author_name") or "")
        tags_raw = header.get("tags") or item.get("tags") or []
        tags = ", ".join((t.get("name", "") if isinstance(t, dict) else str(t)) for t in tags_raw) \
            if isinstance(tags_raw, list) else ""
        # LOGICAL_TABLE covers both models (WORKSHEET subtype) and tables — distinguish them
        # so the picker can show MODEL vs TABLE and the dependency walk can treat them right.
        disp_type = obj_type
        if obj_type == "LOGICAL_TABLE":
            sub = (header.get("type") or item.get("metadata_sub_type") or "").upper()
            disp_type = "MODEL" if ("WORKSHEET" in sub or sub == "MODEL") else "TABLE"
        return {
            "id":       item.get("metadata_id", ""),
            "name":     item.get("metadata_name", ""),
            "type":     disp_type,
            "author":   author,
            "modified": _fmt(item.get("metadata_modified_time") or header.get("modified")),
            "created":  _fmt(header.get("created") or item.get("metadata_created_time")),
            "tags":     tags,
            "obj_id":   item.get("metadata_obj_id") or "",
        }

    def search_by_tags(self, tags: List[str], types: Optional[List[str]] = None) -> List[Dict]:
        """
        Return leaf objects (liveboards + answers by default). If `tags` is non-empty,
        only objects carrying those tags are returned; if `tags` is empty, ALL leaves the
        caller can access are returned (so assets that can't be tagged can still be
        selected). Dependencies are resolved separately.
        """
        types = types or LEAF_TYPES
        results = []
        for obj_type in types:
            offset = 0
            while True:
                payload = {
                    "metadata": [{"type": obj_type}],
                    "record_size": 200,
                    "record_offset": offset,
                }
                if tags:
                    payload["tag_identifiers"] = tags
                data  = self._post("/api/rest/2.0/metadata/search", payload)
                items = data if isinstance(data, list) else data.get("metadata", [])
                results.extend(self._row(it, obj_type) for it in items)
                if len(items) < 200:
                    break
                offset += 200
        return results

    def _resolve_names_to_ids(self, names, obj_type: str = "LOGICAL_TABLE") -> Dict[str, str]:
        """Map object names to GUIDs via metadata search (exact-name match)."""
        out = {}
        for name in names:
            payload = {"metadata": [{"type": obj_type, "identifier": name}], "record_size": 5}
            try:
                data = self._post("/api/rest/2.0/metadata/search", payload)
            except requests.HTTPError:
                continue
            items = data if isinstance(data, list) else data.get("metadata", [])
            for it in items:
                if it.get("metadata_name") == name:
                    out[name] = it.get("metadata_id")
                    break
        return out

    # ── Dependency walk: mixed roots (leaves / models / tables) -> full stack ────

    def resolve_promotion(self, leaf_ids: Optional[List[str]] = None,
                          model_ids: Optional[List[str]] = None,
                          table_ids: Optional[List[str]] = None) -> Dict:
        """
        Resolve the full promotion set from mixed roots. Walks leaves -> models -> tables,
        AND directly-selected models -> their tables; directly-selected tables are included
        as-is. So you can promote a liveboard (whole stack), a bare model (+ its tables), or
        bare tables. Returns deduped model_ids/table_ids, the derived name->id maps (so the
        caller can label/target-check by name), and missing_* for warnings.
        """
        leaf_ids   = leaf_ids or []
        sel_models = model_ids or []
        sel_tables = table_ids or []

        def _parse(it):
            e = it.get("edoc", "{}")
            return json.loads(e) if e.strip().startswith("{") else yaml.safe_load(e)

        # 1. leaves -> referenced model names
        leaf_items  = []
        model_items = []
        model_names = set()
        if leaf_ids:
            raw   = self.export_tml(leaf_ids)
            leaf_items = raw if isinstance(raw, list) else raw.get("object", [])
            for it in leaf_items:
                model_names |= set(extract_model_refs(_parse(it)))
        model_map = self._resolve_names_to_ids(model_names) if model_names else {}
        all_model_ids = list(dict.fromkeys(sel_models + list(model_map.values())))

        # 2. all models (selected + leaf-derived) -> referenced table names
        table_names = set()
        if all_model_ids:
            raw_m   = self.export_tml(all_model_ids)
            model_items = raw_m if isinstance(raw_m, list) else raw_m.get("object", [])
            for it in model_items:
                table_names |= set(extract_table_refs(_parse(it)))
        table_map = self._resolve_names_to_ids(table_names) if table_names else {}
        all_table_ids = list(dict.fromkeys(sel_tables + list(table_map.values())))

        return {
            "leaf_ids":       leaf_ids,
            "model_ids":      all_model_ids,
            "table_ids":      all_table_ids,
            "model_map":      model_map,   # derived model name -> id
            "table_map":      table_map,   # derived table name -> id
            "leaf_items":     leaf_items,  # exported leaf TML items (for drop previews)
            "model_items":    model_items, # exported model TML items (for drop previews)
            "missing_models": sorted(model_names - set(model_map)),
            "missing_tables": sorted(table_names - set(table_map)),
        }

    def resolve_dependencies(self, leaf_ids: List[str]) -> Dict:
        """Backward-compat wrapper: resolve the stack from liveboard/answer leaves only."""
        return self.resolve_promotion(leaf_ids=leaf_ids)

    # ── obj_id alignment ────────────────────────────────────────────────────────

    def search_obj_ids(self, names: List[str],
                       obj_type: str = "LOGICAL_TABLE") -> Dict[str, Dict]:
        """
        Map each object name -> {"guid", "obj_id"} via metadata search.
        obj_id is None if the object has not been touched since obj_id was enabled.
        """
        out = {}
        for name in names:
            payload = {"metadata": [{"type": obj_type, "identifier": name}], "record_size": 5}
            try:
                data = self._post("/api/rest/2.0/metadata/search", payload)
            except requests.HTTPError:
                continue
            items = data if isinstance(data, list) else data.get("metadata", [])
            for it in items:
                if it.get("metadata_name") == name:
                    out[name] = {"guid": it.get("metadata_id"),
                                 "obj_id": it.get("metadata_obj_id")}
                    break
        return out

    def list_metadata(self, obj_type: str) -> List[Dict]:
        """Return [{'id','name','obj_id'}] for every object of a type (paged)."""
        out, offset = [], 0
        while True:
            data  = self._post("/api/rest/2.0/metadata/search",
                               {"metadata": [{"type": obj_type}],
                                "record_size": 200, "record_offset": offset})
            items = data if isinstance(data, list) else data.get("metadata", [])
            for it in items:
                out.append({"id":     it.get("metadata_id"),
                            "name":   it.get("metadata_name"),
                            "obj_id": it.get("metadata_obj_id")})
            if len(items) < 200:
                break
            offset += 200
        return out

    # friendly labels for the dependent-object subtypes the platform returns
    DEP_TYPE_LABEL = {
        "PINBOARD_ANSWER_BOOK":  "liveboard",
        "QUESTION_ANSWER_BOOK":  "answer",
        "LOGICAL_TABLE":         "model/table",
    }

    # metadata/search's dependent_objects reports the OLD type names; metadata/delete and the rest
    # of the v2 API only accept the new ones. VERIFIED on ps-internal 2026-09-23: deleting with
    # "QUESTION_ANSWER_BOOK" returns 400 'got invalid value', while "ANSWER" is accepted. Passing
    # the dependency type straight through is why deleting a blocking answer failed.
    DEP_TYPE_TO_METADATA = {
        "PINBOARD_ANSWER_BOOK":  "LIVEBOARD",
        "QUESTION_ANSWER_BOOK":  "ANSWER",
        "ANSWER":                "ANSWER",
        "LIVEBOARD":             "LIVEBOARD",
        "LOGICAL_TABLE":         "LOGICAL_TABLE",
    }

    @classmethod
    def metadata_type_for(cls, obj_type: str) -> str:
        """The v2 metadata type for a type that may have come from a dependency listing."""
        t = (obj_type or "").strip()
        return cls.DEP_TYPE_TO_METADATA.get(t.upper(), t.upper() or "ANSWER")

    def list_dependents(self, object_ids: List[str],
                        obj_type: str = "LOGICAL_TABLE",
                        record_size: int = 500) -> Dict[str, List[Dict]]:
        """
        Cluster-wide dependents of each object, in ONE call, via metadata/search with
        include_dependent_objects. Returns {source_object_id: [{type,label,name,id,author}]}.

        This is TABLE-LEVEL: it returns every object that depends on the table through ANY
        column, so the caller must note that not all dependents necessarily use a specific
        column (column-precision needs a per-dependent export + scan).
        """
        if not object_ids:
            return {}
        payload = {
            "metadata": [{"type": obj_type, "identifier": oid} for oid in object_ids],
            "include_dependent_objects":     True,
            "dependent_objects_record_size": record_size,
            "record_size":                   len(object_ids),
        }
        data  = self._post("/api/rest/2.0/metadata/search", payload)
        items = data if isinstance(data, list) else data.get("metadata", [])
        out: Dict[str, List[Dict]] = {}
        for it in items:
            dep = it.get("dependent_objects") or {}
            if not isinstance(dep, dict):
                continue
            for src_id, by_type in dep.items():
                bucket = out.setdefault(src_id, [])
                if not isinstance(by_type, dict):
                    continue
                for typ, objs in by_type.items():
                    label = self.DEP_TYPE_LABEL.get(typ, typ)
                    for o in (objs or []):
                        hdr = o.get("header", {}) or {}
                        bucket.append({
                            "type":   typ,
                            "label":  label,
                            "name":   o.get("name") or hdr.get("name", ""),
                            "id":     o.get("id") or o.get("metadata_id") or hdr.get("id", ""),
                            "author": hdr.get("authorDisplayName") or hdr.get("authorName", ""),
                        })
        return out

    # ── Spotter-feedback Replace primitives (rebuild model, re-point deps, delete old) ──

    def find_by_obj_id(self, obj_id: str, obj_type: str = "LOGICAL_TABLE") -> Optional[str]:
        """Return the guid of the object currently holding this obj_id (or None)."""
        data = self._post("/api/rest/2.0/metadata/search",
                          {"metadata": [{"type": obj_type}], "record_size": 5000})
        rows = data if isinstance(data, list) else data.get("metadata", [])
        for o in rows:
            if o.get("metadata_obj_id") == obj_id:
                return o.get("metadata_id")
        return None

    def _connection_meta(self, identifier: str):
        """(connection_guid, inferred_auth_type) for a connection, from its stored config.
        The auth type is inferred from the configuration keys; None if unrecognised."""
        try:
            r = self._post("/api/rest/2.0/connection/search",
                           {"connections": [{"identifier": identifier}], "include_details": True,
                            "record_size": -1, "record_offset": 0})
        except requests.HTTPError:
            return None, None
        rows = r if isinstance(r, list) else r.get("connection", [])
        if not rows:
            return None, None
        c   = rows[0]
        cid = c.get("id")
        cfg = (c.get("details") or {}).get("configuration")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        keys = set((cfg or {}).keys())
        if "personal_access_token" in keys:
            auth = "PERSONAL_ACCESS_TOKEN"
        elif keys & {"oauth_client_id", "oauth_client_secret"}:
            auth = "OAUTH_WITH_SERVICE_PRINCIPAL"
        elif "user" in keys and "password" in keys:
            # Databricks "Service Account" auth stores user (often literally "token") + password.
            auth = "SERVICE_ACCOUNT"
        else:
            auth = None
        return cid, auth

    def connection_column_cases(self, connection_identifier: str, tables,
                                 debug=None, timeout: int = 600) -> Dict[str, Dict[str, str]]:
        """Read the WAREHOUSE's true column casing straight from the connection (no logical table
        needed, no warehouse secret — ThoughtSpot uses the connection's stored credential).

        tables: [{"name": <ts table name>, "database", "schema", "table" (db_table)}].
        Returns {name.lower(): {col.lower(): actual_case}}. The connection's own auth type is
        used; if the guess is off we fall back across the valid types until columns come back.

        debug: optional list. If given, one record per auth-type attempt is appended, capturing the
        HTTP status, whether data_warehouse_objects came back, columns found, and any API error —
        so a run can show whether the fetch is erroring (privilege) or genuinely returning empty.

        timeout: per-attempt read timeout in seconds. A cold Databricks SQL warehouse can take
        minutes to wake and enumerate columns, so this is generous by default; warm the warehouse
        first for a fast response."""
        out: Dict[str, Dict[str, str]] = {}
        dwos = [{"database": t.get("database", ""), "schema": t.get("schema", ""),
                 "table": t.get("table", "")} for t in tables if t.get("table")]
        if not dwos:
            return out
        cid, auth = self._connection_meta(connection_identifier)
        if not cid:
            return out
        by_dbtable = {(t.get("table") or "").strip().lower(): t.get("name") for t in tables}
        candidates = [a for a in (auth, "SERVICE_ACCOUNT", "PERSONAL_ACCESS_TOKEN",
                                  "OAUTH_WITH_SERVICE_PRINCIPAL", "OAUTH_WITH_PKCE") if a]
        seen = set(); candidates = [a for a in candidates if not (a in seen or seen.add(a))]
        for auth_try in candidates:
            rec = {"auth_type": auth_try, "status": None, "has_objects": False,
                   "columns_found": 0, "error": None}
            body = {"connections": [{"identifier": cid, "data_warehouse_objects": dwos}],
                    "data_warehouse_object_type": "COLUMN", "authentication_type": auth_try,
                    "record_size": -1, "record_offset": 0}
            try:
                resp = self._session.post(f"{self.host}/api/rest/2.0/connection/search",
                                          json=body, timeout=timeout)
                rec["status"] = resp.status_code
                data = resp.json()
            except (ValueError, requests.RequestException) as e:
                rec["error"] = str(e)[:200]
                if debug is not None:
                    debug.append(rec)
                continue
            if isinstance(data, dict) and data.get("error"):
                rec["error"] = json.dumps(data.get("error"))[:300]
            rows = data if isinstance(data, list) else [data]
            found = {}
            for c in rows:
                dwo = c.get("data_warehouse_objects") if isinstance(c, dict) else None
                if not dwo:
                    continue
                rec["has_objects"] = True
                for db in dwo.get("databases", []) or []:
                    for sch in db.get("schemas", []) or []:
                        for t in sch.get("tables", []) or []:
                            ts_name = by_dbtable.get((t.get("name") or "").strip().lower())
                            if not ts_name:
                                continue
                            cmap = {}
                            for col in t.get("columns", []) or []:
                                nm = col.get("name")
                                if nm:
                                    cmap[nm.strip().lower()] = nm
                            if cmap:
                                found[ts_name.strip().lower()] = cmap
            rec["columns_found"] = sum(len(v) for v in found.values())
            if debug is not None:
                debug.append(rec)
            if found:
                out.update(found)
                return out
        return out

    def connection_column_types(self, connection_identifier: str, tables,
                                 debug=None, timeout: int = 600) -> Dict[str, Dict[str, str]]:
        """The WAREHOUSE's physical column TYPE per column, straight from the connection — the CDW
        side of a 14536 DataType mismatch. Same request as connection_column_cases but captures the
        column's data type instead of its casing. Returns {name.lower(): {col.lower(): type_str}}.

        NOTE: this is the connection/search COLUMN path, which introspects <catalog>.information_schema
        and 504s on legacy hive_metastore. For a hive target, read types directly from Databricks
        (databricks_direct.hive_column_types via DESCRIBE) instead — this method is for warehouses
        that answer connection/search (Unity Catalog, Snowflake, etc.)."""
        out: Dict[str, Dict[str, str]] = {}
        dwos = [{"database": t.get("database", ""), "schema": t.get("schema", ""),
                 "table": t.get("table", "")} for t in tables if t.get("table")]
        if not dwos:
            return out
        cid, auth = self._connection_meta(connection_identifier)
        if not cid:
            return out
        by_dbtable = {(t.get("table") or "").strip().lower(): t.get("name") for t in tables}
        candidates = [a for a in (auth, "SERVICE_ACCOUNT", "PERSONAL_ACCESS_TOKEN",
                                  "OAUTH_WITH_SERVICE_PRINCIPAL", "OAUTH_WITH_PKCE") if a]
        seen = set(); candidates = [a for a in candidates if not (a in seen or seen.add(a))]
        for auth_try in candidates:
            rec = {"auth_type": auth_try, "status": None, "has_objects": False,
                   "columns_found": 0, "error": None}
            body = {"connections": [{"identifier": cid, "data_warehouse_objects": dwos}],
                    "data_warehouse_object_type": "COLUMN", "authentication_type": auth_try,
                    "record_size": -1, "record_offset": 0}
            try:
                resp = self._session.post(f"{self.host}/api/rest/2.0/connection/search",
                                          json=body, timeout=timeout)
                rec["status"] = resp.status_code
                data = resp.json()
            except (ValueError, requests.RequestException) as e:
                rec["error"] = str(e)[:200]
                if debug is not None:
                    debug.append(rec)
                continue
            if isinstance(data, dict) and data.get("error"):
                rec["error"] = json.dumps(data.get("error"))[:300]
            rows = data if isinstance(data, list) else [data]
            found = {}
            for c in rows:
                dwo = c.get("data_warehouse_objects") if isinstance(c, dict) else None
                if not dwo:
                    continue
                rec["has_objects"] = True
                for db in dwo.get("databases", []) or []:
                    for sch in db.get("schemas", []) or []:
                        for t in sch.get("tables", []) or []:
                            ts_name = by_dbtable.get((t.get("name") or "").strip().lower())
                            if not ts_name:
                                continue
                            tmap = {}
                            for col in t.get("columns", []) or []:
                                nm = col.get("name")
                                # field name varies by TS build — coalesce the likely keys
                                ty = (col.get("type") or col.get("data_type")
                                      or col.get("column_type") or col.get("dataType"))
                                if nm and ty:
                                    tmap[nm.strip().lower()] = str(ty)
                            if tmap:
                                found[ts_name.strip().lower()] = tmap
            rec["columns_found"] = sum(len(v) for v in found.values())
            if debug is not None:
                debug.append(rec)
            if found:
                out.update(found)
                return out
        return out

    def table_column_cases(self, table_names) -> Dict[str, Dict[str, str]]:
        """{table_name.lower(): {db_column_name.lower(): actual_db_column_name}} for the named
        tables as they exist on THIS cluster. Used to align a promoted table's column casing to
        the target warehouse (some warehouses bind external columns case-sensitively)."""
        out: Dict[str, Dict[str, str]] = {}
        names = [n for n in (table_names or []) if n]
        if not names:
            return out
        name_to_id = self._resolve_names_to_ids(names, "LOGICAL_TABLE")
        ids = list(name_to_id.values())
        if not ids:
            return out
        raw   = self.export_tml(ids)
        items = raw if isinstance(raw, list) else raw.get("object", [])
        for it in items:
            edoc = it.get("edoc", "") or ""
            try:
                doc = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
            except (ValueError, yaml.YAMLError):
                continue
            t = (doc or {}).get("table")
            if not t or not t.get("name"):
                continue
            cmap = {}
            for c in t.get("columns", []) or []:
                dbn = c.get("db_column_name")
                if dbn:
                    cmap[dbn.strip().lower()] = dbn
            if cmap:
                out[t["name"].strip().lower()] = cmap
        return out

    def remove_vizzes_verified(self, guid: str, viz_ids):
        """Remove visualisations from a liveboard ON THE TARGET, then CHECK.

        Returns (ok, detail). Export the board, strip the named tiles, import it back under the
        same guid, then re-read it and confirm those tiles are actually gone — the same rule as
        deletion, because a 200 from the import API is not proof of anything.

        Refuses to leave an EMPTY board: if every tile uses the dropped column there is nothing
        surgical to do, and the operator should decide to delete the board rather than have this
        quietly hollow it out.
        """
        from services.import_diagnostics import strip_vizzes_from_tml
        want = {str(v).strip() for v in (viz_ids or []) if str(v).strip()}
        if not want:
            return False, "no visualisation ids given"
        try:
            raw = self.export_tml([guid])
            items = raw if isinstance(raw, list) else raw.get("object", [])
            if not items:
                return False, "the liveboard could not be exported from the target"
            edoc = items[0].get("edoc")
        except Exception as e:
            return False, f"export failed: {e}"
        new_edoc, removed, remaining = strip_vizzes_from_tml(edoc, want)
        if removed == 0:
            return False, "none of those visualisations are on the board any more"
        if remaining == 0:
            return False, ("every tile on the board uses the dropped column, so removing them "
                           "would leave an empty liveboard — delete the board instead, "
                           "deliberately")
        try:
            res = self.import_tml([new_edoc], policy="ALL_OR_NONE")
        except Exception as e:
            return False, f"import failed: {e}"
        bad = [r for r in res if r.get("status") != "OK"]
        if bad:
            return False, (bad[0].get("error") or "the target rejected the edited liveboard")
        try:
            raw2 = self.export_tml([guid])
            it2 = raw2 if isinstance(raw2, list) else raw2.get("object", [])
            doc2 = json.loads(it2[0]["edoc"]) if it2 else {}
            left = {str(v.get("id") or v.get("viz_id") or "")
                    for v in ((doc2.get("liveboard") or {}).get("visualizations") or [])}
        except Exception:
            return False, ("the import was accepted but the board could not be re-read, so the "
                           "removal is unconfirmed")
        still = want & left
        if still:
            return False, f"the target still has {', '.join(sorted(still))} — nothing was removed"
        return True, f"removed {removed} tile(s); {remaining} left on the board"

    def find_objects_by_name(self, names: List[str],
                             types: Optional[List[str]] = None) -> Dict[str, Dict]:
        """Resolve object NAMES to {id, type, name, author} across several metadata types.

        A "Deleted columns have dependents" error names the blocking objects but gives no guid and
        no type, and deleting one needs both. Returns {name_lower: {...}} for those found; a name
        that resolves to nothing is simply absent, which is itself the signal that the object is
        not visible to this account.

        The search `identifier` is CASE-SENSITIVE, so the original spelling is sent and the match
        is compared case-insensitively — lowercasing the query returns zero rows."""
        out: Dict[str, Dict] = {}
        originals = [n.strip() for n in (names or []) if (n or "").strip()]
        if not originals:
            return out
        for obj_type in (types or ["ANSWER", "LIVEBOARD", "LOGICAL_TABLE"]):
            for original in originals:
                key = original.lower()
                if key in out:
                    continue
                try:
                    data = self._post("/api/rest/2.0/metadata/search",
                                      {"metadata": [{"type": obj_type, "identifier": original}],
                                       "record_size": 10})
                except Exception:
                    continue
                items = data if isinstance(data, list) else data.get("metadata", [])
                for it in items:
                    hdr = it.get("metadata_header") or {}
                    found = (it.get("metadata_name") or hdr.get("name") or "").strip()
                    if found.lower() != key:
                        continue
                    out[key] = {"id": it.get("metadata_id"),
                                "type": it.get("metadata_type") or obj_type,
                                "name": found,
                                "author": hdr.get("authorDisplayName")
                                or hdr.get("authorName", "")}
                    break
        return out

    def real_dependents(self, model_guid: str) -> List[Dict]:
        """Cluster-wide dependents of a model EXCLUDING its own feedback (type=FEEDBACK).
        Feedback appears as a dependent but dies with the model, so it must not block deletion."""
        deps = self.list_dependents([model_guid]).get(model_guid, [])
        return [d for d in deps if d.get("type") != "FEEDBACK"]

    def delete_metadata(self, obj_type: str, identifier: str) -> int:
        """Delete a metadata object. Returns the HTTP status (204 on success).

        CAUTION: 204 does NOT prove the object is gone. Verified on ps-internal 2026-09-22 — a
        non-admin asked to delete an object they cannot see and got 204 while the object survived
        untouched. Use delete_metadata_verified() for anything the operator is told succeeded."""
        return self._delete_once(self.metadata_type_for(obj_type), identifier).status_code

    def _delete_once(self, metadata_type: str, identifier: str):
        """POST metadata/delete once and return the RESPONSE, so callers can read the body.
        `metadata_type` must already be a v2 type — use metadata_type_for() to normalise."""
        payload = {"metadata": [{"type": metadata_type, "identifier": identifier}]}
        url = f"{self.host}/api/rest/2.0/metadata/delete"
        resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(url, json=payload, timeout=60)
        return resp

    def object_exists(self, obj_type: str, identifier: str) -> bool:
        """Whether this account can still find the object. False also when it was never visible."""
        try:
            data = self._post("/api/rest/2.0/metadata/search",
                              {"metadata": [{"type": obj_type, "identifier": identifier}]})
        except Exception:
            return True          # can't tell -> assume it is still there, never claim success
        items = data if isinstance(data, list) else data.get("metadata", [])
        return bool(items)

    def delete_metadata_verified(self, obj_type: str, identifier: str):
        """Delete, then CHECK. Returns (ok, status, detail).

        The API answers 204 whether or not it removed anything, so a caller that trusts the status
        reports a deletion that did not happen — and in this tool that would tell the operator a
        blocking dependent was cleared when it is still there, sending them into an import that
        fails for the reason they thought they had fixed."""
        mtype = self.metadata_type_for(obj_type)
        resp = self._delete_once(mtype, identifier)
        status = resp.status_code
        if status not in (200, 204):
            body = ""
            try:
                body = json.dumps(resp.json())
            except Exception:
                body = resp.text or ""
            # Distinguish the three very different reasons a delete can fail, because telling the
            # operator "you lack rights" when the request was malformed sends them to an admin for
            # a bug in this tool (VERIFIED: that is exactly what happened on 2026-09-23).
            if "got invalid value" in body:
                detail = (f"the request was malformed — `{mtype}` was rejected as a metadata "
                          "type. That is a bug in this tool, not a permission problem.")
            elif '"code":13003' in body.replace(" ", "") or "13003" in body:
                detail = ("the target says no such object — it may already be gone, or it is not "
                          "visible to this account.")
            elif status == 403:
                detail = "the account lacks rights to delete it; its owner or an admin must."
            else:
                detail = f"HTTP {status}: {body}"
            return False, status, detail
        if self.object_exists(mtype, identifier):
            return False, status, ("the server accepted the request but the object is still "
                                   "there — the account most likely lacks rights on it.")
        return True, status, "deleted"

    def export_feedback_entries(self, model_guid: str) -> List[Dict]:
        """The model's CURRENT feedback entries (list of dicts); [] if none / not exportable."""
        for it in self.export_feedback([model_guid]):
            edoc = it.get("edoc", "") or ""
            doc = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
            if isinstance(doc, dict) and "nls_feedback" in doc:
                return (doc.get("nls_feedback", {}) or {}).get("feedback", []) or []
        return []

    # ── NL (Spotter coaching) instructions — separate from TML, ai/instructions API ──

    def get_nl_instruction_blocks(self, data_source_identifier: str) -> List[Dict]:
        """Raw NL-instruction blocks [{instructions:[...], scope:...}] for a model.
        ai/instructions/get (Beta 10.15.0.cl+). Returns [] if none / endpoint unavailable /
        no access. Scope is GLOBAL-only today; a future data-model-user scope would appear as
        additional blocks here, so callers can inspect/preserve them rather than flattening."""
        try:
            d = self._post("/api/rest/2.0/ai/instructions/get",
                           {"data_source_identifier": data_source_identifier})
        except requests.HTTPError:
            return []
        return [b for b in (d.get("nl_instructions_info") or []) if isinstance(b, dict)]

    def get_nl_instructions(self, data_source_identifier: str, scope: str = "GLOBAL") -> List[str]:
        """A model's NL instructions for ONE scope (default GLOBAL) as a flat list of strings.
        Deliberately scoped: only GLOBAL exists today, and reading per-scope stops a future
        non-GLOBAL (e.g. data-model-user) block from being read/promoted as if it were global."""
        out: List[str] = []
        for blk in self.get_nl_instruction_blocks(data_source_identifier):
            if (blk.get("scope") or "GLOBAL") == scope:
                out.extend(blk.get("instructions") or [])
        return out

    def set_nl_instruction_blocks(self, data_source_identifier: str,
                                  blocks: List[Dict]) -> bool:
        """Set (FULL REPLACE of the whole model) NL-instruction blocks verbatim, preserving each
        block's own scope. ai/instructions/set is a full replace, so callers that want to touch
        only one scope must pass the other scopes' blocks back in unchanged (see
        nl_instructions.promote).

        Clearing gotcha (verified live): the API rejects an empty nl_instructions_info list with
        400 'Empty Scope is not allowed'. To CLEAR a scope you must send a block with a valid
        scope and an empty instructions array. So we keep empty-instruction blocks (they mean
        'clear this scope'), and if the caller passes no blocks at all we express that as clearing
        the GLOBAL scope."""
        info = [{"instructions": list(b.get("instructions") or []),
                 "scope": b.get("scope") or "GLOBAL"}
                for b in blocks]
        if not info:
            info = [{"instructions": [], "scope": "GLOBAL"}]   # clear GLOBAL (empty list would 400)
        payload = {"data_source_identifier": data_source_identifier, "nl_instructions_info": info}
        url = f"{self.host}/api/rest/2.0/ai/instructions/set"
        resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code not in (200, 204):
            return False
        try:
            return bool(resp.json().get("success", True))
        except ValueError:
            return True

    def set_nl_instructions(self, data_source_identifier: str,
                            instructions: List[str], scope: str = "GLOBAL") -> bool:
        """Convenience single-scope setter. WARNING: because set is a full replace, this drops any
        OTHER-scope blocks on the model. To preserve other scopes, read blocks first and use
        set_nl_instruction_blocks. Needs CAN_USE_SPOTTER + edit/SPOTTER_COACHING_PRIVILEGE +
        an org-scoped token."""
        return self.set_nl_instruction_blocks(
            data_source_identifier, [{"instructions": instructions, "scope": scope}])

    def repoint_dependent(self, dep_guid: str, old_obj_id: str,
                          new_obj_id: str, new_name: str) -> Dict:
        """Re-bind a dependent (answer/liveboard) from old_obj_id to new_obj_id by re-importing
        it with its model refs rewritten. Returns {name, status, error}."""
        raw   = self.export_tml([dep_guid])
        items = raw if isinstance(raw, list) else raw.get("object", [])
        if not items:
            return {"name": dep_guid, "status": "ERROR", "error": "export failed"}
        it   = items[0]
        edoc = it.get("edoc", "") or ""
        doc  = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
        name = (it.get("info") or {}).get("name", dep_guid)

        def _fix(tables):
            for t in (tables or []):
                if isinstance(t, dict) and t.get("obj_id") == old_obj_id:
                    t["obj_id"] = new_obj_id
                    t["name"]   = new_name
                    t["id"]     = new_name
                    t.pop("fqn", None)

        if "answer" in doc:
            _fix(doc["answer"].get("tables"))
        if "liveboard" in doc:
            for viz in doc["liveboard"].get("visualizations", []):
                _fix((viz.get("answer", {}) or {}).get("tables"))
        res = self.import_tml([json.dumps(doc)], policy="ALL_OR_NONE")
        row = res[0] if res else {}
        return {"name": name, "status": row.get("status", "ERROR"), "error": row.get("error", "")}

    def _update_obj_id_once(self, mappings: List[Dict]):
        """POST /metadata/update-obj-id for these mappings. Returns the response."""
        payload = {"metadata": [{"metadata_identifier": m["identifier"],
                                 "new_obj_id": m["new_obj_id"]} for m in mappings]}
        url = f"{self.host}/api/rest/2.0/metadata/update-obj-id"
        resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(url, json=payload, timeout=60)
        return resp

    def find_holder_of_obj_id(self, obj_id: str, types: Optional[List[str]] = None):
        """The object currently holding this obj_id in THIS token's org, or None.

        There is no obj_id filter on metadata/search (it 400s), so this pages the org's objects
        and matches. Called only when a write has already failed, so the cost buys an answer the
        operator otherwise has to hunt for by hand."""
        want = (obj_id or "").strip().lower()
        if not want:
            return None
        for obj_type in (types or ["LOGICAL_TABLE", "ANSWER", "LIVEBOARD"]):
            offset = 0
            while True:
                try:
                    data = self._post("/api/rest/2.0/metadata/search",
                                      {"metadata": [{"type": obj_type}], "record_size": 500,
                                       "record_offset": offset})
                except Exception:
                    break
                items = data if isinstance(data, list) else data.get("metadata", [])
                for it in items:
                    if (it.get("metadata_obj_id") or "").strip().lower() == want:
                        hdr = it.get("metadata_header") or {}
                        return {"id": it.get("metadata_id"),
                                "name": it.get("metadata_name") or hdr.get("name", ""),
                                "type": it.get("metadata_type") or obj_type,
                                "obj_id": it.get("metadata_obj_id"),
                                "author": hdr.get("authorDisplayName")
                                or hdr.get("authorName", "")}
                if len(items) < 500:
                    break
                offset += 500
        return None

    @staticmethod
    def _obj_id_error(resp) -> str:
        """Plain reason out of an update-obj-id failure.

        VERIFIED on ps-internal 2026-09-22: asking for an obj_id another object already holds
        returns HTTP 500 with code 14009 / DUPLICATE_CUSTOM_OBJECT_ID. It is not a privilege
        problem, which is what this used to be reported as, sending the operator to check rights
        that were never the issue. A genuine privilege failure comes back 403.

        Scope is per-ORG, not per-cluster: the same obj_id was set successfully on an object in a
        second org while a Primary object held it."""
        body = ""
        try:
            body = json.dumps(resp.json())
        except Exception:
            body = resp.text or ""
        if "DUPLICATE_CUSTOM_OBJECT_ID" in body or '"code":14009' in body.replace(" ", ""):
            # The platform writes "=" as the escaped \u003d, and json.dumps doubles the backslash
            # again, so normalise both before reading the id back out.
            flat = body.replace("\\\\u003d", "=").replace("\\u003d", "=").replace("\u003d", "=")
            m = re.search(r"customObjectId\s*=\s*([^\s\\\"',}\]]+)", flat)
            taken = m.group(1) if m else "that obj_id"
            return (f"obj_id `{taken}` is already held by a different object in this ORG. "
                    "obj_ids are unique per org (VERIFIED on ps-internal 2026-09-22: the same "
                    "obj_id CAN exist in another org), so the clash is with something in the org "
                    "you are pointed at.")
        if resp.status_code == 403:
            return "the account lacks DATAMANAGEMENT or ADMINISTRATION."
        return f"HTTP {resp.status_code}: {body}"

    def _explain_obj_id_failure(self, resp, wanted_obj_id: str) -> str:
        """The reason, plus WHO is holding the obj_id when that is the reason.

        Naming the holder is the difference between an error and a fix: obj_ids are arbitrary
        strings, so "it is taken" leaves the operator with nothing to go on, while "ORDERS
        (605b4cc0) has it" tells them immediately whether they picked the wrong slug or the model
        is pointed at the wrong one of several same-named tables."""
        base = self._obj_id_error(resp)
        if "already held" not in base:
            return base
        try:
            holder = self.find_holder_of_obj_id(wanted_obj_id)
        except Exception:
            holder = None
        if holder:
            who = f"**{holder['name']}** ({holder['type']}, `{holder['id']}`"
            who += f", author {holder['author']})" if holder.get("author") else ")"
            return (base + f" It is held by {who}. Either choose a different obj_id here, or if "
                    "that object is the one you actually meant, point at it instead — several "
                    "objects sharing a NAME is the usual reason the wrong one got picked.")
        return base + (" Nothing visible to this account holds it, so the holder is an object "
                       "you cannot see — its owner or an admin has to free it.")

    def update_obj_ids(self, mappings: List[Dict]) -> bool:
        """
        Set obj_id on existing objects. mappings: [{"identifier": <guid>, "new_obj_id": <str>}].
        POST /metadata/update-obj-id (10.8.0.cl+). Needs DATAMANAGEMENT or ADMINISTRATION.
        Returns True on success (HTTP 204 No Content); raises RuntimeError naming the offending
        object when it fails.

        The API takes the whole batch in one call, so ONE bad mapping fails all of them and the
        response says nothing about which. On failure this retries them individually to name the
        culprit — without that, a 20-object fix reports a single opaque 500.
        """
        if not mappings:
            return True
        resp = self._update_obj_id_once(mappings)
        if resp.status_code in (200, 204):
            return True
        if len(mappings) == 1:
            raise RuntimeError(self._explain_obj_id_failure(resp, mappings[0]["new_obj_id"]))
        bad, ok = [], 0
        for m in mappings:
            r1 = self._update_obj_id_once([m])
            if r1.status_code in (200, 204):
                ok += 1
            else:
                bad.append(f"`{m['new_obj_id']}` — "
                           + self._explain_obj_id_failure(r1, m["new_obj_id"]))
        raise RuntimeError(
            f"{ok} of {len(mappings)} obj_id(s) set; {len(bad)} failed:\n- " + "\n- ".join(bad))

    # ── TML export ────────────────────────────────────────────────────────────

    def export_tml(self, object_ids: List[str]) -> dict:
        """
        Export TML for given object IDs with obj_id and FQN included.
        Returns the raw API response dict.
        """
        payload = {
            "metadata": [{"identifier": oid} for oid in object_ids],
            "export_options": {
                "include_obj_id":     True,
                "include_obj_id_ref": True,
            },
        }
        return self._post("/api/rest/2.0/metadata/tml/export", payload)

    def export_feedback(self, model_ids: List[str]) -> List[Dict]:
        """
        Export the FEEDBACK TML (Spotter reference questions + business terms) for each model.
        FEEDBACK is not independently searchable; it is exported by the model's GUID via
        type=FEEDBACK. Returns the raw item list (feedback objects); empty for models that
        have no feedback.

        Exported PER MODEL and tolerant of non-200: on current clusters a `type=FEEDBACK`
        export for a model that has NO feedback returns HTTP 400 (code 10002), so a single
        batched call would raise and abort the whole promotion (and the Select-page picker).
        Per-model + skip-on-error means a model without feedback is simply skipped while the
        others still export (verified live on ps-internal 2026-07-07).
        """
        out: List[Dict] = []
        url = f"{self.host}/api/rest/2.0/metadata/tml/export"
        for mid in model_ids or []:
            payload = {"metadata": [{"type": "FEEDBACK", "identifier": mid}],
                       "export_options": {"include_obj_id": True}}
            resp = self._session.post(url, json=payload, timeout=60)
            if resp.status_code == 401 and self._username and self._password:
                self._session_login()
                resp = self._session.post(url, json=payload, timeout=60)
            if resp.status_code != 200:
                continue   # model has no feedback (400) or is not exportable — skip it
            try:
                raw = resp.json()
            except ValueError:
                continue
            items = raw if isinstance(raw, list) else raw.get("object", [])
            out.extend(it for it in items if it.get("edoc"))
        return out

    # ── TML import ────────────────────────────────────────────────────────────

    def _retry_post(self, url, payload, timeout, tries=3):
        """POST with retry + backoff on transient connection resets/timeouts (e.g. WinError 10054
        from a gateway/proxy dropping a slow warehouse-validate). Only use for IDEMPOTENT calls
        (VALIDATE_ONLY, or obj_id-keyed update-in-place imports); a bare RST means the request
        almost certainly never completed server-side, so a retry is safe. Re-raises the last
        transient error if every attempt fails."""
        last = None
        for attempt in range(tries):
            try:
                return self._session.post(url, json=payload, timeout=timeout)
            except _TRANSIENT as e:
                last = e
                if attempt < tries - 1:
                    time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
        raise last

    # "Warning: Existing guid <g> corresponding to object Id <o> will be used." is the platform
    # confirming an update-in-place, and on its own it is not a failure. But it is APPENDED to
    # whatever else happened, so a real error arrives with the notice glued on the end. Matching
    # the substring anywhere therefore silences genuine failures: on 2026-09-23 an import that was
    # rejected with 14544 "Deleted columns have dependents" was reported as "Import complete", the
    # objects were listed as succeeded, and the target was of course unchanged.
    _GUID_NOTICE = re.compile(
        r"(?:Warning:\s*)?Existing guid\s+\S+\s+corresponding to object Id\s+\S+\s+will be used\.?",
        re.I)

    @classmethod
    def _is_benign_update_notice(cls, error_message: str) -> bool:
        """True only when the message is the update-in-place notice AND NOTHING ELSE."""
        rest = cls._GUID_NOTICE.sub("", error_message or "")
        for tag in ("<br/>", "<br />", "<br>", "</br>", "<b>", "</b>", "Warning:"):
            rest = rest.replace(tag, " ")
        return not rest.strip()

    @staticmethod
    def _raw_has_error(data) -> bool:
        """True if any object in a raw import/validate response is non-OK (ignoring the benign
        'Existing guid … will be used' update notice)."""
        rows = data if isinstance(data, list) else (data.get("object", []) if isinstance(data, dict) else [])
        for item in rows:
            if not isinstance(item, dict):
                continue
            status = (item.get("response", item).get("status") or {})
            code = status.get("status_code", "OK")
            if code and code != "OK" and not TSClient._is_benign_update_notice(
                    status.get("error_message") or ""):
                return True
        return False

    def _append_raw_log(self, policy, count, status_code, data, files=None):
        """Append the FULL raw response. Best-effort: logging must never break a validate/import.
        No auth is written.

        An IMPORT is always logged, success or failure. A validate is only logged when it carries
        an error. The asymmetry is deliberate: a validate is a dry run you can simply repeat, while
        an import WRITES to the target and is the one call you cannot reconstruct afterwards. On
        2026-09-23 a promotion left the target's columns unchanged and reported its objects as
        "created", and there was no way to tell what the platform had actually done, because nine
        validations were on disk and not one import.

        Imports go to their own file next to the validate log, so a write is never buried in
        dry-run noise."""
        try:
            is_import = (policy or "").upper() != "VALIDATE_ONLY"
            if not is_import and not self._raw_has_error(data):
                return
            rec = {"ts": datetime.now().isoformat(timespec="seconds"), "host": self.host,
                   "policy": policy, "tml_count": count, "http_status": status_code,
                   "had_error": self._raw_has_error(data), "raw": data}
            if files:
                rec["files"] = list(files)
            path = self.debug_raw_log
            if is_import:
                import os.path as _op
                d, base = _op.split(path)
                path = _op.join(d, "import_raw.jsonl") if base.startswith("validate") else path
            import os
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass

    @staticmethod
    def _describe_tmls(tml_strings):
        """A one-line identity for each TML being sent: kind, name and obj_id.

        The obj_id is the whole basis of update-in-place, so a log that omits it cannot answer
        "why was this created instead of updated". Parsing must never break an import."""
        out = []
        for t in tml_strings or []:
            try:
                d = json.loads(t) if str(t).strip().startswith("{") else yaml.safe_load(t)
                kind = next((k for k in ("table", "model", "worksheet", "liveboard", "answer")
                             if isinstance(d, dict) and k in d), "?")
                node = (d.get(kind) or {}) if isinstance(d, dict) else {}
                out.append({"kind": kind,
                            "name": node.get("name"),
                            "obj_id": (d or {}).get("obj_id"),
                            "guid": (d or {}).get("guid"),
                            "columns": len(node.get("columns") or [])})
            except Exception:
                out.append({"kind": "?", "name": None, "obj_id": None,
                            "guid": None, "columns": None})
        return out

    def import_tml(self, tml_strings: List[str],
                   policy: str = "PARTIAL") -> List[Dict]:
        """
        Import a list of TML YAML strings.
        policy: PARTIAL | ALL_OR_NONE | VALIDATE_ONLY
        Returns per-object result list.
        """
        payload = {
            "metadata_tmls": tml_strings,
            "import_policy": policy,
        }
        url = f"{self.host}/api/rest/2.0/metadata/tml/import"
        # Retry transient resets: VALIDATE_ONLY is read-only, and real imports are obj_id-keyed
        # update-in-place, so a retry after a bare connection reset is safe. Longer read window
        # (180s) for slow server-side warehouse validation.
        resp = self._retry_post(url, payload, timeout=180)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._retry_post(url, payload, timeout=180)
        data = resp.json()
        # Breadcrumb the raw response as we go — so an opaque failure is already captured the
        # moment it happens (no need to re-run every validate in a one-shot capture later).
        self.last_raw_import = data
        if self.debug_raw_log:
            # Record WHAT WAS SENT alongside the response. Without it the log says objects were
            # "created" and leaves you unable to check whether the TML carried the obj_id that
            # should have made it an update instead.
            self._append_raw_log(policy, len(tml_strings), resp.status_code, data,
                                 files=self._describe_tmls(tml_strings))

        # Normalise — API may return list or {"object": [...]}
        if isinstance(data, list):
            raw = data
        else:
            raw = data.get("object", [])

        results = []
        for item in raw:
            response = item.get("response", item)
            header   = response.get("header", {})
            status   = response.get("status", {})
            status_code = status.get("status_code", "UNKNOWN")
            error_msg   = status.get("error_message", "")
            # The update-in-place notice on its OWN is not a failure. It is appended to whatever
            # else happened, so only neutralise the status when nothing else is in the message.
            if status_code != "OK" and self._is_benign_update_notice(error_msg):
                status_code = "OK"
                error_msg   = ""
            results.append({
                "name":    header.get("name", "unknown"),
                "type":    header.get("metadata_type", ""),
                "status":  status_code,
                "error":   error_msg,
                "new_id":  header.get("id_guid", header.get("owner_guid", "")),
            })
        return results

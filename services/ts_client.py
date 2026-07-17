"""
ThoughtSpot REST API v2 client.
Wraps metadata search (by tag), dependency resolution, TML export, and TML import.

One TSClient = one cluster. The tool builds two: a source client and a target
client, each with its own host + credentials.
"""

import json
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
        resp = self._session.post(f"{self.host}{path}", json=payload, timeout=60)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(f"{self.host}{path}", json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()

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

    def real_dependents(self, model_guid: str) -> List[Dict]:
        """Cluster-wide dependents of a model EXCLUDING its own feedback (type=FEEDBACK).
        Feedback appears as a dependent but dies with the model, so it must not block deletion."""
        deps = self.list_dependents([model_guid]).get(model_guid, [])
        return [d for d in deps if d.get("type") != "FEEDBACK"]

    def delete_metadata(self, obj_type: str, identifier: str) -> int:
        """Delete a metadata object. Returns the HTTP status (204 on success)."""
        payload = {"metadata": [{"type": obj_type, "identifier": identifier}]}
        url = f"{self.host}/api/rest/2.0/metadata/delete"
        resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(url, json=payload, timeout=60)
        return resp.status_code

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

    def update_obj_ids(self, mappings: List[Dict]) -> bool:
        """
        Set obj_id on existing objects. mappings: [{"identifier": <guid>, "new_obj_id": <str>}].
        POST /metadata/update-obj-id (10.8.0.cl+). Needs DATAMANAGEMENT or ADMINISTRATION.
        Returns True on success (HTTP 204 No Content).
        """
        payload = {"metadata": [{"metadata_identifier": m["identifier"],
                                 "new_obj_id": m["new_obj_id"]} for m in mappings]}
        url = f"{self.host}/api/rest/2.0/metadata/update-obj-id"
        resp = self._session.post(url, json=payload, timeout=60)
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.status_code in (200, 204)

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
            # "Existing guid ... will be used" is an informational update notice, not a failure
            if status_code != "OK" and "will be used" in error_msg:
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

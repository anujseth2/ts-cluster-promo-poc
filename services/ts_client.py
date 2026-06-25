"""
ThoughtSpot REST API v2 client.
Wraps metadata search (by tag), dependency resolution, TML export, and TML import.

One TSClient = one cluster. The tool builds two: a source client and a target
client, each with its own host + credentials.
"""

import json
import yaml
from datetime import datetime

import requests
from typing import List, Dict, Optional

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
        mod_ms = item.get("metadata_modified_time") or header.get("modified")
        try:
            modified = (datetime.fromtimestamp(mod_ms / 1000).strftime("%Y-%m-%d %H:%M")
                        if mod_ms else "")
        except (TypeError, ValueError, OSError):
            modified = ""
        author = (header.get("authorDisplayName") or header.get("authorName")
                  or header.get("author_display_name") or header.get("author_name") or "")
        return {
            "id":       item.get("metadata_id", ""),
            "name":     item.get("metadata_name", ""),
            "type":     obj_type,
            "modified": modified,
            "author":   author,
        }

    def search_by_tags(self, tags: List[str], types: Optional[List[str]] = None) -> List[Dict]:
        """
        Return objects carrying any of the given tags. Defaults to the leaf
        content types (liveboards + answers); dependencies are resolved separately.
        """
        types = types or LEAF_TYPES
        results = []
        for obj_type in types:
            offset = 0
            while True:
                payload = {
                    "metadata": [{"type": obj_type}],
                    "tag_identifiers": tags,
                    "record_size": 200,
                    "record_offset": offset,
                }
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

    # ── Dependency walk: leaves -> models -> tables ─────────────────────────────

    def resolve_dependencies(self, leaf_ids: List[str]) -> Dict:
        """
        Given liveboard/answer GUIDs, walk down the chain and return the model and
        table GUIDs they sit on, so a promotion ships the whole stack. Names that
        cannot be resolved on this cluster are returned in missing_* for warning.
        """
        def _parse(it):
            e = it.get("edoc", "{}")
            return json.loads(e) if e.strip().startswith("{") else yaml.safe_load(e)

        # 1. leaves -> referenced model names
        raw   = self.export_tml(leaf_ids)
        items = raw if isinstance(raw, list) else raw.get("object", [])
        model_names = set()
        for it in items:
            model_names |= set(extract_model_refs(_parse(it)))

        model_map = self._resolve_names_to_ids(model_names) if model_names else {}
        model_ids = list(model_map.values())

        # 2. models -> referenced table names
        table_names = set()
        if model_ids:
            raw_m   = self.export_tml(model_ids)
            items_m = raw_m if isinstance(raw_m, list) else raw_m.get("object", [])
            for it in items_m:
                table_names |= set(extract_table_refs(_parse(it)))

        table_map = self._resolve_names_to_ids(table_names) if table_names else {}
        table_ids = list(table_map.values())

        return {
            "model_ids":      model_ids,
            "table_ids":      table_ids,
            "missing_models": sorted(model_names - set(model_map)),
            "missing_tables": sorted(table_names - set(table_map)),
        }

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

    # ── TML import ────────────────────────────────────────────────────────────

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
        resp = self._session.post(
            f"{self.host}/api/rest/2.0/metadata/tml/import",
            json=payload,
            timeout=120,
        )
        if resp.status_code == 401 and self._username and self._password:
            self._session_login()
            resp = self._session.post(
                f"{self.host}/api/rest/2.0/metadata/tml/import",
                json=payload,
                timeout=120,
            )
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

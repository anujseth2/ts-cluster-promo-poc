"""
Shared fixtures for the L1 pure-logic test suite.

Grounded in reality: the model and liveboard fixtures are the REAL sample TML shipped in
`tml/` (the ps_ecommerce cross-cluster demo content). The `commerce` fact-table fixture is
built to match that same star schema (its physical columns), so the table-level functions
(matcher, column diff, warehouse-missing) are exercised against consistent, realistic shapes.

Everything here is offline: services/* are pure (no network), so these run in milliseconds.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TML_DIR = REPO / "tml"


def _load_tml(name):
    return yaml.safe_load((TML_DIR / name).read_text())


def _item(doc, name):
    """Wrap a TML doc as a promotion 'item' (edoc JSON string + info), as the app passes them."""
    return {"edoc": json.dumps(doc), "info": {"name": name}}


# ── Real sample TML (ps_ecommerce demo) ────────────────────────────────────────

@pytest.fixture
def model_doc():
    return _load_tml("ps_ecommerce.model.tml")


@pytest.fixture
def liveboard_doc():
    return _load_tml("ps_ecommerce.liveboard.tml")


@pytest.fixture
def model_item(model_doc):
    return _item(model_doc, model_doc["model"]["name"])


@pytest.fixture
def liveboard_item(liveboard_doc):
    return _item(liveboard_doc, liveboard_doc["liveboard"]["name"])


# ── Derived table fixtures (same schema as the sample model's `commerce` fact) ──

# Physical columns of the commerce fact, with warehouse types. db_column_name is the physical
# name; the matcher / warehouse diff key on it.
COMMERCE_COLUMNS = [
    ("Visit_ID", "VARCHAR"), ("Date", "DATE"), ("Age_Range", "VARCHAR"),
    ("Gender", "VARCHAR"), ("Condition", "VARCHAR"), ("Quantity", "INT64"),
    ("Cost", "DOUBLE"), ("Revenue", "DOUBLE"), ("Brand_ID", "INT64"),
    ("Category_ID", "INT64"), ("Country_ID", "INT64"),
]


def _table_doc(name="commerce", db="workspace", schema="athoz", db_table="commerce",
               connection="Sisense Migration - Databricks", columns=None, obj_id="commerce_tbl"):
    cols = columns if columns is not None else COMMERCE_COLUMNS
    return {
        "obj_id": obj_id,
        "table": {
            "name": name, "db": db, "schema": schema, "db_table": db_table,
            "connection": {"name": connection},
            "columns": [
                {"name": c, "db_column_name": c,
                 "db_column_properties": {"data_type": t},
                 "properties": {"column_type": "ATTRIBUTE"}}
                for c, t in cols
            ],
        },
    }


@pytest.fixture
def make_table_doc():
    """Factory so tests can build variants (renamed, missing cols, type drift, disjoint)."""
    return _table_doc


@pytest.fixture
def make_item():
    return _item


@pytest.fixture
def commerce_table_doc():
    return _table_doc()


@pytest.fixture
def commerce_table_item(commerce_table_doc):
    return _item(commerce_table_doc, "commerce")

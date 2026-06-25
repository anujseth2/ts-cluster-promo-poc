"""
One-off Databricks seeding helper for the cross-cluster promotion demo.

The two ThoughtSpot clusters point at two SEPARATE Databricks workspaces. The
Sisense demo tables were loaded into the TEST workspace; before the dev (source)
ThoughtSpot cluster can build a model on them, the same tables must exist in the
DEV workspace. This script replicates table structure (and optionally data) from
TEST -> DEV so you can then add them to the dev connection and build the model.

It is deliberately standalone (not wired into the Streamlit app): it talks
Databricks-to-Databricks, not ThoughtSpot.

Credentials are read from environment variables ONLY (never hard-coded). See
.env.seed.example. Because PATs are sensitive, prefer exporting them in the shell
or sourcing a gitignored .env.seed; rotate any token that has been shared.

    TEST_DBX_HOST       dbc-c1944047-1496.cloud.databricks.com   (no scheme)
    TEST_DBX_HTTP_PATH  /sql/1.0/warehouses/b5f30d7c4aa8d54d
    TEST_DBX_TOKEN      <test workspace PAT>
    DEV_DBX_HOST        dbc-ba0cbe77-94ec.cloud.databricks.com
    DEV_DBX_HTTP_PATH   /sql/1.0/warehouses/f6a4e6987957b8fb
    DEV_DBX_TOKEN       <dev workspace PAT>

Usage:
    # 1. See what's where (run against either side to locate the demo tables)
    python scripts/seed_databricks.py list --side test
    python scripts/seed_databricks.py list --side test --catalog main --schema sisense_demo
    python scripts/seed_databricks.py list --side dev                # confirm shared-metastore or empty

    # 2. Replicate structure only (fast), then with data for a real demo
    python scripts/seed_databricks.py replicate \
        --src-catalog main --src-schema sisense_demo \
        --dst-catalog main --dst-schema sisense_demo
    python scripts/seed_databricks.py replicate \
        --src-catalog main --src-schema sisense_demo \
        --dst-catalog main --dst-schema sisense_demo \
        --tables sales,customers,products --with-data

The dst catalog/schema you choose here are exactly what go into
config/teams.json as db_map / schema_map (source dev -> target test) later.
"""

import argparse
import datetime
import os
import sys
from decimal import Decimal

try:
    from databricks import sql
except ImportError:
    sys.exit("Missing dependency: pip install databricks-sql-connector")

# System catalogs/schemas to hide when listing.
_HIDE_CATALOGS = {"system", "samples", "__databricks_internal"}
_HIDE_SCHEMAS = {"information_schema"}


def connect(side):
    """Open a Databricks SQL connection for 'test' or 'dev' from env vars."""
    prefix = side.upper()
    host = os.environ.get(f"{prefix}_DBX_HOST", "")
    path = os.environ.get(f"{prefix}_DBX_HTTP_PATH", "")
    token = os.environ.get(f"{prefix}_DBX_TOKEN", "")
    missing = [f"{prefix}_DBX_{k}" for k, v in
               (("HOST", host), ("HTTP_PATH", path), ("TOKEN", token)) if not v]
    if missing:
        sys.exit(f"Missing env var(s): {', '.join(missing)}")
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    return sql.connect(server_hostname=host, http_path=path, access_token=token)


def _scalar_col(rows, idx=0):
    return [r[idx] for r in rows]


def list_objects(conn, catalog=None, schema=None):
    """Print catalogs -> schemas -> tables, or drill into one schema."""
    cur = conn.cursor()
    if catalog and schema:
        cur.execute(f"SHOW TABLES IN `{catalog}`.`{schema}`")
        tables = [r[1] for r in cur.fetchall()]  # (database, tableName, isTemp)
        print(f"{catalog}.{schema}: {len(tables)} table(s)")
        for t in tables:
            print(f"  - {t}")
        cur.close()
        return

    cur.execute("SHOW CATALOGS")
    catalogs = [c for c in _scalar_col(cur.fetchall())
                if c not in _HIDE_CATALOGS and (not catalog or c == catalog)]
    for cat in catalogs:
        try:
            cur.execute(f"SHOW SCHEMAS IN `{cat}`")
        except Exception as e:  # noqa: BLE001 - some catalogs deny listing
            print(f"{cat}: (cannot list schemas: {e})")
            continue
        schemas = [s for s in _scalar_col(cur.fetchall()) if s not in _HIDE_SCHEMAS]
        print(f"{cat}: {len(schemas)} schema(s)")
        for sch in schemas:
            try:
                cur.execute(f"SHOW TABLES IN `{cat}`.`{sch}`")
                n = len(cur.fetchall())
            except Exception:  # noqa: BLE001
                n = "?"
            print(f"  {cat}.{sch}  ({n} tables)")
    cur.close()


def get_columns(cur, catalog, schema, table):
    """Return [(name, type), ...] via DESCRIBE TABLE (works on UC + hive)."""
    cur.execute(f"DESCRIBE TABLE `{catalog}`.`{schema}`.`{table}`")
    cols = []
    for row in cur.fetchall():
        name = (row[0] or "").strip()
        # DESCRIBE appends a blank line then a '# Partition Information' block.
        if not name or name.startswith("#"):
            break
        cols.append((name, row[1]))
    return cols


def table_names(cur, catalog, schema, only=None):
    cur.execute(f"SHOW TABLES IN `{catalog}`.`{schema}`")
    names = [r[1] for r in cur.fetchall()]
    if only:
        wanted = [t.strip() for t in only.split(",") if t.strip()]
        names = [n for n in names if n in wanted]
        missing = set(wanted) - set(names)
        if missing:
            print(f"  ! not found in source, skipping: {', '.join(sorted(missing))}")
    return names


def _lit(v):
    """Render a Python value as a Databricks SQL literal for INSERT."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, datetime.datetime):
        return f"TIMESTAMP '{v.isoformat(sep=' ')}'"
    if isinstance(v, datetime.date):
        return f"DATE '{v.isoformat()}'"
    if isinstance(v, (bytes, bytearray)):
        return f"X'{v.hex()}'"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def copy_data(src_cur, dst_cur, src_fqtn, dst_fqtn, cols, batch=500):
    """Stream rows from source and bulk-insert into dest in literal batches."""
    collist = ", ".join(f"`{c}`" for c, _ in cols)
    src_cur.execute(f"SELECT {collist} FROM {src_fqtn}")
    total = 0
    while True:
        rows = src_cur.fetchmany(batch)
        if not rows:
            break
        values = ",\n".join("(" + ", ".join(_lit(v) for v in row) + ")" for row in rows)
        dst_cur.execute(f"INSERT INTO {dst_fqtn} ({collist}) VALUES {values}")
        total += len(rows)
        print(f"    copied {total} rows...", end="\r")
    print(f"    copied {total} rows total ")


def replicate(args):
    test = connect("test")
    dev = connect("dev")
    src_cur = test.cursor()
    dst_cur = dev.cursor()

    # Ensure target catalog/schema exist (catalog may require elevated perms).
    try:
        dst_cur.execute(f"CREATE CATALOG IF NOT EXISTS `{args.dst_catalog}`")
    except Exception as e:  # noqa: BLE001
        print(f"(catalog create skipped: {e})")
    dst_cur.execute(f"CREATE SCHEMA IF NOT EXISTS `{args.dst_catalog}`.`{args.dst_schema}`")

    names = table_names(src_cur, args.src_catalog, args.src_schema, args.tables)
    if not names:
        sys.exit("No source tables matched.")

    for t in names:
        cols = get_columns(src_cur, args.src_catalog, args.src_schema, t)
        coldef = ",\n  ".join(f"`{n}` {ty}" for n, ty in cols)
        dst_fqtn = f"`{args.dst_catalog}`.`{args.dst_schema}`.`{t}`"
        src_fqtn = f"`{args.src_catalog}`.`{args.src_schema}`.`{t}`"
        print(f"- {t}: {len(cols)} columns")
        dst_cur.execute(
            f"CREATE TABLE IF NOT EXISTS {dst_fqtn} (\n  {coldef}\n) USING DELTA"
        )
        if args.with_data:
            copy_data(src_cur, dst_cur, src_fqtn, dst_fqtn, cols, args.batch)

    src_cur.close()
    dst_cur.close()
    test.close()
    dev.close()
    print("\nDone. Next: add these tables to the Databricks connection in the DEV "
          "ThoughtSpot cluster, build the model + liveboard, then tag the leaves "
          "with team:commercial-sbu.")


def list_cmd(args):
    conn = connect(args.side)
    list_objects(conn, args.catalog, args.schema)
    conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="enumerate catalogs/schemas/tables")
    pl.add_argument("--side", choices=["test", "dev"], default="test")
    pl.add_argument("--catalog")
    pl.add_argument("--schema")
    pl.set_defaults(func=list_cmd)

    pr = sub.add_parser("replicate", help="copy table structure (and data) test -> dev")
    pr.add_argument("--src-catalog", required=True)
    pr.add_argument("--src-schema", required=True)
    pr.add_argument("--dst-catalog", required=True)
    pr.add_argument("--dst-schema", required=True)
    pr.add_argument("--tables", help="comma-separated subset; default = all in schema")
    pr.add_argument("--with-data", action="store_true", help="also copy rows")
    pr.add_argument("--batch", type=int, default=500)
    pr.set_defaults(func=replicate)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

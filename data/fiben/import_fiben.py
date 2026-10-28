"""
Import the FIBEN benchmark into one DuckDB file.

Expected layout under --fiben-dir:

    <fiben-dir>/
        FIBEN.sql
        data/
            ACCOUNTSPAYABLEANDACCRUEDLIABILITIES.csv
            ACCOUNTSPAYABLEDETAILS.csv
            ...
            WEIGHTEDAVERAGENUMBEROFSHARES.csv

FIBEN.sql contains the table definitions. The CSV files are headerless,
with one CSV corresponding to each CREATE TABLE statement.

Some CSV files in the FIBEN download are completely empty (0 bytes). For
those, the table is still created from FIBEN.sql but no data is loaded, so
the table exists with 0 rows. No rows are dropped or invented.

FIBEN timestamps use the DB2 style 2011-01-16-00.00.00.000000, so the load
passes an explicit timestamp format. If a table still hits a type conversion
error, that one table is reloaded with every column as VARCHAR (text
fallback) and is listed at the end of the run.

Usage:
    python3 import_fiben.py \
        --fiben-dir ~/Desktop/datasets-work/FIBEN \
        --duckdb ~/Desktop/datasets-work/out/decode_join_fiben.duckdb

Outputs:
    - One FIBEN-only .duckdb file
    - data/fiben/schema_mapping.csv

The DuckDB contains one schema for the FIBEN source database.
"""

import argparse
import csv
import hashlib
import re
from pathlib import Path

import duckdb


SCHEMA_PREFIX = "fiben_"


def quote(name):
    """Quote a SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def schema_name(relative_path):
    """Generate a deterministic schema name using the team convention."""
    h = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
    return SCHEMA_PREFIX + h


def read_create_statements(sql_path):
    """
    Read CREATE TABLE statements from FIBEN.sql.

    FIBEN.sql contains one statement per line. ALTER TABLE foreign-key
    statements are intentionally not executed during import because the
    benchmark data itself is what DECODE-JOIN profiles.
    """
    statements = []

    with sql_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            if line.upper().startswith("CREATE TABLE "):
                statements.append(line)

    return statements


def table_name_from_create(statement):
    """Extract the table name from a CREATE TABLE statement."""
    match = re.match(
        r"CREATE\s+TABLE\s+([A-Za-z0-9_]+)",
        statement,
        flags=re.IGNORECASE,
    )

    if not match:
        raise RuntimeError(
            f"Could not determine table name from statement:\n{statement}"
        )

    return match.group(1)


def qualify_create_statement(statement, schema):
    """
    Change:

        CREATE TABLE ASSET (...)

    into:

        CREATE TABLE "schema"."ASSET" (...)
    """
    match = re.match(
        r"(CREATE\s+TABLE\s+)([A-Za-z0-9_]+)(.*)",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        raise RuntimeError(
            f"Could not rewrite CREATE TABLE statement:\n{statement}"
        )

    return (
        match.group(1)
        + quote(schema)
        + "."
        + quote(match.group(2))
        + match.group(3)
    )


def count_csv_rows(csv_path):
    """Count records in a headerless CSV file."""
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.reader(f))


# FIBEN timestamps look like 2011-01-16-00.00.00.000000 (DB2 style), which
# DuckDB does not parse as TIMESTAMP unless it is told the format.
TIMESTAMP_FORMAT = "%Y-%m-%d-%H.%M.%S.%f"


def copy_csv(db, schema, table, csv_path):
    """Load one headerless CSV into an existing table."""
    db.execute(
        f"""
        COPY {quote(schema)}.{quote(table)}
        FROM ?
        (FORMAT CSV, HEADER FALSE, TIMESTAMPFORMAT '{TIMESTAMP_FORMAT}')
        """,
        [str(csv_path)],
    )


def recreate_as_text(db, schema, table):
    """Replace a table with one that has the same columns, all VARCHAR."""
    columns = [
        row[0]
        for row in db.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [schema, table],
        ).fetchall()
    ]

    db.execute(f"DROP TABLE {quote(schema)}.{quote(table)}")

    column_sql = ", ".join(f"{quote(c)} VARCHAR" for c in columns)

    db.execute(
        f"CREATE TABLE {quote(schema)}.{quote(table)} ({column_sql})"
    )


def write_schema_mapping(out_path, source_path, schema):
    """Write the standard DECODE-JOIN schema mapping."""
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["source_path", "duckdb_schema"])
        writer.writerow([source_path, schema])

    print(f"Wrote schema mapping: {out_path}")


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--fiben-dir",
        required=True,
        type=Path,
        help="Folder containing FIBEN.sql and data/*.csv",
    )

    ap.add_argument(
        "--duckdb",
        default="decode_join_fiben.duckdb",
        type=Path,
        help="Output DuckDB file",
    )

    args = ap.parse_args()

    fiben_dir = args.fiben_dir.expanduser().resolve()
    duckdb_path = args.duckdb.expanduser().resolve()

    if not fiben_dir.is_dir():
        raise SystemExit(f"FIBEN folder not found: {fiben_dir}")

    sql_path = fiben_dir / "FIBEN.sql"
    data_dir = fiben_dir / "data"

    if not sql_path.is_file():
        raise SystemExit(f"FIBEN.sql not found: {sql_path}")

    if not data_dir.is_dir():
        raise SystemExit(f"FIBEN data folder not found: {data_dir}")

    create_statements = read_create_statements(sql_path)

    if not create_statements:
        raise SystemExit("No CREATE TABLE statements found in FIBEN.sql")

    csv_files = sorted(data_dir.glob("*.csv"))

    print(f"Found {len(create_statements)} CREATE TABLE statements")
    print(f"Found {len(csv_files)} CSV files")

    sql_tables = {
        table_name_from_create(statement)
        for statement in create_statements
    }

    csv_tables = {path.stem for path in csv_files}

    missing_csv = sorted(sql_tables - csv_tables)
    extra_csv = sorted(csv_tables - sql_tables)

    if missing_csv:
        raise SystemExit(
            "Tables defined in FIBEN.sql without CSV files: "
            + ", ".join(missing_csv)
        )

    if extra_csv:
        raise SystemExit(
            "CSV files without CREATE TABLE definitions: "
            + ", ".join(extra_csv)
        )

    source_path = "FIBEN.sql"
    schema = schema_name(source_path)

    print(f"Target schema: {schema}")

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    if duckdb_path.exists():
        raise SystemExit(
            f"Output DuckDB already exists: {duckdb_path}\n"
            "Refusing to overwrite an existing database."
        )

    db = duckdb.connect(str(duckdb_path))

    try:
        db.execute(f"CREATE SCHEMA {quote(schema)}")

        total_rows = 0
        empty_tables = []
        text_fallback_tables = []

        for i, statement in enumerate(create_statements, 1):
            table = table_name_from_create(statement)
            csv_path = data_dir / f"{table}.csv"

            print(
                f"[{i}/{len(create_statements)}] Importing {table}",
                flush=True,
            )

            qualified_create = qualify_create_statement(statement, schema)
            db.execute(qualified_create)

            if csv_path.stat().st_size == 0:
                empty_tables.append(table)
                source_rows = 0
                print("    empty source CSV (0 bytes), table created with 0 rows")
            else:
                try:
                    copy_csv(db, schema, table, csv_path)
                except duckdb.ConversionException as e:
                    # A value does not fit its declared type. Reload this
                    # one table with every column as VARCHAR so no rows
                    # are dropped and no values are changed.
                    first_line = str(e).splitlines()[0]
                    print(f"    typed load failed: {first_line}")
                    print("    text fallback: reloading with all VARCHAR columns")
                    text_fallback_tables.append(table)
                    recreate_as_text(db, schema, table)
                    copy_csv(db, schema, table, csv_path)
                source_rows = count_csv_rows(csv_path)

            target_rows = db.execute(
                f"SELECT COUNT(*) FROM {quote(schema)}.{quote(table)}"
            ).fetchone()[0]

            if source_rows != target_rows:
                raise RuntimeError(
                    f"Row count mismatch for {table}: "
                    f"CSV={source_rows}, DuckDB={target_rows}"
                )

            print(f"    {target_rows} rows")

            total_rows += target_rows

        table_count = db.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = ?
            """,
            [schema],
        ).fetchone()[0]

        if table_count != len(create_statements):
            raise RuntimeError(
                f"Table count mismatch: expected {len(create_statements)}, "
                f"found {table_count}"
            )

        print()
        print(f"PASS: {table_count} tables")
        print(f"PASS: {total_rows} total rows")
        print(f"PASS: schema {schema}")
        print(
            f"NOTE: {len(empty_tables)} tables have an empty source CSV "
            f"and hold 0 rows; {table_count - len(empty_tables)} tables "
            "have data"
        )
        print(
            f"NOTE: {len(text_fallback_tables)} tables loaded as text "
            "(all VARCHAR) because of a type conversion error"
        )
        for name in text_fallback_tables:
            print(f"    text fallback: {name}")
        print()
        print("IMPORT COMPLETED SUCCESSFULLY")

    except Exception:
        db.close()

        if duckdb_path.exists():
            duckdb_path.unlink()

        raise

    else:
        db.close()

    mapping_path = Path(__file__).resolve().parent / "schema_mapping.csv"

    write_schema_mapping(
        mapping_path,
        source_path,
        schema,
    )


if __name__ == "__main__":
    main()

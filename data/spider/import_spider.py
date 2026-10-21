
import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

import duckdb



ROOT = Path(__file__).resolve().parents[2]

SPIDER_DIR = ROOT / "datasets" / "spider" / "spider_data"
DUCKDB_FILE = ROOT / "data" / "decode_join.duckdb"

META = "spider_metadata"

EXPECTED_DATABASES = 372
EXPECTED_TABLES = 1926
EXPECTED_ROWS = 3454232



def quote(name):
    """Safely quote a SQL table, column, or schema name."""
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    """Safely put a file path or name into SQL."""
    return "'" + str(value).replace("'", "''") + "'"


def file_hash(path):
    """Calculate the SHA-256 hash of a file."""
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def bytes_hash(data):
    return hashlib.sha256(data).hexdigest()


def schema_name(relative_path):
    """Give every SQLite file its own DuckDB schema."""
    h = hashlib.sha256(
        relative_path.encode("utf-8")
    ).hexdigest()[:20]

    return "spider_" + h


def open_sqlite(path):
    """Open the original database without changing it."""
    uri = path.resolve().as_uri() + "?mode=ro"

    db = sqlite3.connect(uri, uri=True)

    def decode_sqlite_text(raw):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw

    db.text_factory = decode_sqlite_text
    return db

def safe_sqlite_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="backslashreplace")
    return value


def get_tables(sqlite_db):
    """Find every normal user table."""
    rows = sqlite_db.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """).fetchall()

    return [row[0] for row in rows]


def get_columns(sqlite_db, table):
    """Read the complete SQLite column definitions."""
    return sqlite_db.execute(
        f"PRAGMA table_xinfo({literal(table)})"
    ).fetchall()


def normalize(value):
    """
    Keep values and their SQLite storage types distinct.
    This catches changes such as integer 1 becoming text '1'.
    """

    if value is None:
        return ("NULL", None)

    if isinstance(value, int):
        return ("INTEGER", value)

    if isinstance(value, float):
        if math.isnan(value):
            return ("REAL_NAN", None)

        return ("REAL", value.hex())

    if isinstance(value, str):
        return ("TEXT", value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("BLOB", bytes(value))

    raise TypeError(
        f"Unexpected value type: {type(value)}"
    )


def row_counts(connection, query):
    """
    Count every distinct row, including duplicates.

    fetchmany() avoids loading the entire table at once.
    The Counter still stores distinct rows in memory.
    """
    cursor = connection.execute(query)
    counts = Counter()

    while True:
        rows = cursor.fetchmany(5000)

        if not rows:
            break

        for row in rows:
            key = tuple(normalize(v) for v in row)
            counts[key] += 1

    return counts

def sqlite_row_counts(sqlite_db, table):
    """Compare SQLite values while preserving storage types."""
    columns = [
        row[1]
        for row in get_columns(sqlite_db, table)
        if row[6] == 0
    ]

    type_expressions = ", ".join(
        f"typeof({quote(column)})"
        for column in columns
    )

    cursor = sqlite_db.execute(
        f"SELECT *, {type_expressions} FROM {quote(table)}"
    )

    counts = Counter()
    column_count = len(columns)

    while True:
        rows = cursor.fetchmany(5000)

        if not rows:
            break

        for row in rows:
            values = row[:column_count]
            types = row[column_count:]

            normalized = []

            for value, storage_type in zip(values, types):
                if storage_type == "text" and isinstance(value, bytes):
                    value = safe_sqlite_text(value)

                normalized.append(normalize(value))

            counts[tuple(normalized)] += 1

    return counts


def pragma_data(sqlite_db, table):
    """Collect the original keys, indexes, and columns."""

    result = {}

    for pragma in [
        "table_xinfo",
        "foreign_key_list",
        "index_list"
    ]:
        cursor = sqlite_db.execute(
            f"PRAGMA {pragma}({literal(table)})"
        )

        names = [item[0] for item in cursor.description]

        result[pragma] = [
            dict(zip(names, row))
            for row in cursor.fetchall()
        ]

    # Also preserve index column definitions.
    result["indexes"] = {}

    for index in result["index_list"]:
        name = index["name"]

        cursor = sqlite_db.execute(
            f"PRAGMA index_xinfo({literal(name)})"
        )

        names = [item[0] for item in cursor.description]

        result["indexes"][name] = [
            dict(zip(names, row))
            for row in cursor.fetchall()
        ]

    return result



def setup_metadata(db):

    db.execute(
        f"CREATE SCHEMA IF NOT EXISTS {quote(META)}"
    )

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.files (
            source_path VARCHAR PRIMARY KEY,
            source_hash VARCHAR,
            file_size BIGINT,
            file_type VARCHAR,
            duckdb_schema VARCHAR,
            original_bytes BLOB
        )
    """)

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.objects (
            source_path VARCHAR,
            object_type VARCHAR,
            object_name VARCHAR,
            parent_table VARCHAR,
            original_sql VARCHAR
        )
    """)

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.table_info (
            source_path VARCHAR,
            table_name VARCHAR,
            metadata_json VARCHAR
        )
    """)

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.checks (
            source_path VARCHAR,
            table_name VARCHAR,
            source_rows BIGINT,
            target_rows BIGINT,
            source_columns BIGINT,
            target_columns BIGINT,
            values_match BOOLEAN,
            status VARCHAR
        )
    """)

    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.run_info (
            setting VARCHAR PRIMARY KEY,
            value VARCHAR
        )
    """)



def save_original_file(db, path, schema=None):
    """
    Store the original file bytes inside DuckDB.

    This is important because DuckDB may interpret
    some SQLite types differently.
    """

    relative = path.relative_to(SPIDER_DIR).as_posix()

    data = path.read_bytes()
    digest = bytes_hash(data)

    if digest != file_hash(path):
        raise RuntimeError(
            f"File changed while reading: {relative}"
        )

    db.execute(f"""
        INSERT INTO {quote(META)}.files
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        relative,
        digest,
        len(data),
        path.suffix.lower(),
        schema,
        data
    ])



def save_schema_metadata(db, sqlite_db, relative):

    objects = sqlite_db.execute("""
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        ORDER BY type, name
    """).fetchall()

    for object_type, name, parent, sql in objects:

        db.execute(f"""
            INSERT INTO {quote(META)}.objects
            VALUES (?, ?, ?, ?, ?)
        """, [
            relative,
            object_type,
            name,
            parent,
            sql
        ])

    for table in get_tables(sqlite_db):

        details = pragma_data(sqlite_db, table)

        db.execute(f"""
            INSERT INTO {quote(META)}.table_info
            VALUES (?, ?, ?)
        """, [
            relative,
            table,
            json.dumps(
                details,
                ensure_ascii=False,
                default=str
            )
        ])


def validate_table(sqlite_db, db, schema, table):

    source_columns = [
        row[1]
        for row in get_columns(sqlite_db, table)
        if row[6] == 0
    ]

    target_columns = [
        row[0]
        for row in db.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = ?
              AND table_name = ?
            ORDER BY ordinal_position
        """, [schema, table]).fetchall()
    ]

    if source_columns != target_columns:
        raise RuntimeError(
            f"Column mismatch: {schema}.{table}\n"
            f"Source: {source_columns}\n"
            f"Target: {target_columns}"
        )

    source_query = f"SELECT * FROM {quote(table)}"

    target_query = (
        f"SELECT * FROM {quote(schema)}.{quote(table)}"
    )

    source_rows = sqlite_row_counts(sqlite_db, table)
    target_rows = row_counts(db, target_query)

    source_count = sum(source_rows.values())
    target_count = sum(target_rows.values())

    if source_count != target_count:
        raise RuntimeError(
            f"Row count mismatch: {schema}.{table}\n"
            f"SQLite: {source_count}\n"
            f"DuckDB: {target_count}"
        )

    if source_rows != target_rows:
        raise RuntimeError(
            f"Value, type, NULL, or duplicate mismatch: "
            f"{schema}.{table}"
        )

    return (
        source_count,
        target_count,
        len(source_columns),
        len(target_columns)
    )


def import_table_preserving_types(sqlite_db, db, schema, table):
    """
    Preserve SQLite storage types while avoiding
    unnecessary UNION columns.
    """

    columns = [
        row[1]
        for row in get_columns(sqlite_db, table)
        if row[6] == 0
    ]

    type_map = {
        "integer": "BIGINT",
        "real": "DOUBLE",
        "text": "VARCHAR",
        "blob": "BLOB",
    }

    union_type = (
        "UNION("
        "integer_value BIGINT, "
        "real_value DOUBLE, "
        "text_value VARCHAR, "
        "blob_value BLOB)"
    )

    definitions = []

    for column in columns:
        quoted_column = quote(column)

        types = {
            row[0]
            for row in sqlite_db.execute(
                f"SELECT DISTINCT typeof({quoted_column}) "
                f"FROM {quote(table)} "
                f"WHERE {quoted_column} IS NOT NULL"
            )
        }

        if len(types) == 1:
            duck_type = type_map[next(iter(types))]
        else:
            duck_type = union_type

        definitions.append(
            f"{quoted_column} {duck_type}"
        )

    db.execute(
        f"CREATE TABLE {quote(schema)}.{quote(table)} "
        f"({', '.join(definitions)})"
    )

    placeholders = ", ".join("?" for _ in columns)

    insert_sql = (
        f"INSERT INTO {quote(schema)}.{quote(table)} "
        f"VALUES ({placeholders})"
    )

    type_expressions = ", ".join(
        f"typeof({quote(column)})"
        for column in columns
    )

    cursor = sqlite_db.execute(
        f"SELECT *, {type_expressions} FROM {quote(table)}"
    )

    while True:
        fetched = cursor.fetchmany(5000)

        if not fetched:
            break

        column_count = len(columns)

        rows = [
            row[:column_count]
            for row in fetched
        ]

        storage_types_per_row = [
            row[column_count:]
            for row in fetched
        ]

        safe_rows = [
            tuple(
                safe_sqlite_text(value)
                if isinstance(value, bytes)
                and types[i] == "text"
                else value
                for i, value in enumerate(row)
            )
            for row, types in zip(rows, storage_types_per_row)
        ]

        db.executemany(insert_sql, safe_rows)
        
def import_database(db, path):

    relative = path.relative_to(SPIDER_DIR).as_posix()
    schema = schema_name(relative)

    existing = db.execute(f"""
        SELECT COUNT(*)
        FROM {quote(META)}.files
        WHERE source_path = ?
    """, [relative]).fetchone()[0]

    if existing:
        print(f"Already imported: {relative}")

        # Verify the existing data before skipping it.
        with open_sqlite(path) as source:
            original_tables = get_tables(source)

            for table in original_tables:
                validate_table(source, db, schema, table)

        # Check that the original file has not changed.
        saved_hash = db.execute(f"""
            SELECT source_hash
            FROM {quote(META)}.files
            WHERE source_path = ?
        """, [relative]).fetchone()[0]

        if file_hash(path) != saved_hash:
            raise RuntimeError(
                f"Original file changed: {relative}"
            )

        print(f"PASS: Existing database verified: {relative}")

        return None

    existing_schema = db.execute("""
        SELECT COUNT(*)
        FROM information_schema.schemata
        WHERE schema_name = ?
    """, [schema]).fetchone()[0]

    if existing_schema:
        raise RuntimeError(
            f"Schema already exists: {schema}"
        )

    sqlite_db = open_sqlite(path)

    # Check the SQLite file itself.
    integrity = sqlite_db.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0]

    if integrity != "ok":
        sqlite_db.close()
        raise RuntimeError(
            f"SQLite integrity check failed: {relative}"
        )

    tables = get_tables(sqlite_db)

    db.execute(
        f"ATTACH {literal(path.resolve())} "
        "AS spider_source (TYPE SQLITE, READ_ONLY)"
    )

    try:
        db.execute("BEGIN TRANSACTION")

        try:
            db.execute(
                f"CREATE SCHEMA {quote(schema)}"
            )

            # Preserve original file bytes and schema.
            save_original_file(db, path, schema)

            save_schema_metadata(
                db, sqlite_db, relative
            )

            total_rows = 0

            for table in tables:

                print(
                    f"  Importing {table}",
                    flush=True
                )

                import_table_preserving_types(
                    sqlite_db, db, schema, table
                )

                # Compare actual values, not just counts.
                (
                    source_count,
                    target_count,
                    source_columns,
                    target_columns
                ) = validate_table(
                    sqlite_db, db, schema, table
                )

                db.execute(f"""
                    INSERT INTO {quote(META)}.checks
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    relative,
                    table,
                    source_count,
                    target_count,
                    source_columns,
                    target_columns,
                    True,
                    "PASS"
                ])

                total_rows += source_count

            # Check the original file again.
            saved_hash = db.execute(f"""
                SELECT source_hash
                FROM {quote(META)}.files
                WHERE source_path = ?
            """, [relative]).fetchone()[0]

            if file_hash(path) != saved_hash:
                raise RuntimeError(
                    f"Original file changed: {relative}"
                )

            db.execute("COMMIT")

            print(
                f"PASS: {relative} | "
                f"{len(tables)} tables | "
                f"{total_rows} rows",
                flush=True
            )

            return len(tables), total_rows

        except Exception:
            db.execute("ROLLBACK")
            raise

    finally:
        db.execute("DETACH spider_source")
        sqlite_db.close()



def import_other_files(db):

    files = sorted(
        p for p in SPIDER_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() != ".sqlite"
    )

    for path in files:

        relative = path.relative_to(SPIDER_DIR).as_posix()

        existing = db.execute(f"""
            SELECT COUNT(*)
            FROM {quote(META)}.files
            WHERE source_path = ?
        """, [relative]).fetchone()[0]

        if existing:
            saved_hash = db.execute(f"""
                SELECT source_hash
                FROM {quote(META)}.files
                WHERE source_path = ?
            """, [relative]).fetchone()[0]

            if file_hash(path) != saved_hash:
                raise RuntimeError(
                    f"Previously saved file changed: {relative}"
                )

            continue

        db.execute("BEGIN TRANSACTION")

        try:
            save_original_file(db, path)
            db.execute("COMMIT")

        except Exception:
            db.execute("ROLLBACK")
            raise

    print(
        f"Preserved {len(files)} additional files.",
        flush=True
    )



def audit(db, full=False):

    print("\nRunning final audit...")

    records = db.execute(f"""
        SELECT source_path, source_hash,
               file_size, duckdb_schema
        FROM {quote(META)}.files
        ORDER BY source_path
    """).fetchall()

    for relative, digest, size, schema in records:

        data = db.execute(f"""
            SELECT original_bytes
            FROM {quote(META)}.files
            WHERE source_path = ?
        """, [relative]).fetchone()[0]
        path = SPIDER_DIR / relative

        if not path.is_file():
            raise RuntimeError(
                f"Missing source file: {relative}"
            )

        if len(data) != size:
            raise RuntimeError(
                f"Stored file size mismatch: {relative}"
            )

        if bytes_hash(data) != digest:
            raise RuntimeError(
                f"Stored file hash mismatch: {relative}"
            )

        if file_hash(path) != digest:
            raise RuntimeError(
                f"Original file hash mismatch: {relative}"
            )

        if schema is not None:

            with open_sqlite(path) as source:

                original_tables = get_tables(source)

                imported_tables = [
                    row[0]
                    for row in db.execute("""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = ?
                          AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                    """, [schema]).fetchall()
                ]

                if original_tables != imported_tables:
                    raise RuntimeError(
                        f"Table list mismatch: {relative}"
                    )

                if full:
                    for table in original_tables:
                        validate_table(
                            source, db, schema, table
                        )

    checks = db.execute(f"""
        SELECT
            COUNT(*),
            COALESCE(SUM(source_rows), 0),
            COALESCE(SUM(target_rows), 0),
            COUNT(*) FILTER (
                WHERE status != 'PASS'
                   OR values_match != TRUE
                   OR source_rows != target_rows
                   OR source_columns != target_columns
            )
        FROM {quote(META)}.checks
    """).fetchone()

    sqlite_count = db.execute(f"""
        SELECT COUNT(*)
        FROM {quote(META)}.files
        WHERE duckdb_schema IS NOT NULL
    """).fetchone()[0]

    table_count, source_rows, target_rows, failures = checks

    if failures:
        raise RuntimeError(
            f"{failures} failed table checks"
        )

    if source_rows != target_rows:
        raise RuntimeError(
            "Total row counts do not match"
        )

    print(f"SQLite databases: {sqlite_count}")
    print(f"Tables: {table_count}")
    print(f"Source rows: {source_rows}")
    print(f"DuckDB rows: {target_rows}")
    print(f"Failed checks: {failures}")

    if full:
        if (
            sqlite_count != EXPECTED_DATABASES
            or table_count != EXPECTED_TABLES
            or source_rows != EXPECTED_ROWS
        ):
            raise RuntimeError(
                "Final totals do not match the Spider inventory"
            )

    print("AUDIT PASSED")




def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--pilot",
        action="store_true",
        help="Import browser_web only"
    )

    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit an existing import"
    )

    args = parser.parse_args()

    if not SPIDER_DIR.is_dir():
        raise SystemExit(
            "Spider source folder was not found."
        )

    if not DUCKDB_FILE.is_file():
        raise SystemExit(
            "DuckDB file was not found."
        )

    sqlite_files = sorted(
        SPIDER_DIR.rglob("*.sqlite")
    )

    if len(sqlite_files) != EXPECTED_DATABASES:
        raise SystemExit(
            f"Expected {EXPECTED_DATABASES} SQLite files, "
            f"found {len(sqlite_files)}."
        )

    db = duckdb.connect(str(DUCKDB_FILE))

    try:
        if args.audit:
            audit(db, full=True)
            return

        db.execute("LOAD sqlite")

        setup_metadata(db)

        if args.pilot:
            sqlite_files = [
                SPIDER_DIR
                / "database"
                / "browser_web"
                / "browser_web.sqlite"
            ]

        total_tables = 0
        total_rows = 0

        for i, path in enumerate(sqlite_files, 1):

            print(
                f"\n[{i}/{len(sqlite_files)}] "
                f"{path.relative_to(SPIDER_DIR)}",
                flush=True
            )

            result = import_database(db, path)

            if result is None:
            # Already imported and verified.
            # Include its tables and rows in our totals.
                with open_sqlite(path) as source:
                    tables = get_tables(source)

                    total_tables += len(tables)

                    for table in tables:
                        row_count = source.execute(
                            f"SELECT COUNT(*) FROM {quote(table)}"
                        ).fetchone()[0]

                        total_rows += row_count
            else:
                tables, rows = result

                total_tables += tables
                total_rows += rows

        if not args.pilot:

            if (
                total_tables != EXPECTED_TABLES
                or total_rows != EXPECTED_ROWS
            ):
                raise RuntimeError(
                    "Imported totals do not match the "
                    "original Spider inventory."
                )

            import_other_files(db)

            db.execute(f"""
                INSERT OR REPLACE INTO {quote(META)}.run_info
                VALUES (?, ?)
            """, [
                "dataset",
                "Spider 1.0"
            ])

            audit(db, full=True)

        else:
            audit(db, full=False)

        print("\nIMPORT COMPLETED SUCCESSFULLY")

    finally:
        db.close()


if __name__ == "__main__":
    main()

"""
Import the BIRD text-to-SQL databases into one DuckDB file, with verification.

Expected layout under --bird-dir (what you get after unzipping the BIRD downloads):

    <bird-dir>/
        dev_databases/<db_name>/<db_name>.sqlite
        dev_databases/<db_name>/database_description/*.csv
        dev.json, dev.sql, ...                      (any other files are preserved too)
        (and/or) train_databases/..., train.json

Usage:
    python import_bird.py --bird-dir path/to/dev_20240627 --duckdb decode_join_bird_dev.duckdb --pilot
    python import_bird.py --bird-dir path/to/dev_20240627 --duckdb decode_join_bird_dev.duckdb
    python import_bird.py --bird-dir path/to/dev_20240627 --duckdb decode_join_bird_dev.duckdb --audit
    python import_bird.py --bird-dir path/to/dev_20240627 --duckdb decode_join_bird_dev.duckdb --audit-full
    python import_bird.py --duckdb decode_join_bird_dev.duckdb --mapping-only
    python import_bird.py --duckdb decode_join_bird_dev.duckdb --mapping-only --rename-schemas

Outputs:
    - the .duckdb file (one bird_<db_name> schema per SQLite file, plus the bird_metadata schema)
    - schema_mapping.csv next to the .duckdb file, one row per column
      (source_database, target_schema, table, ordinal, column, sqlite_type,
       duckdb_type, nullable, attributes)
"""
import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

import duckdb

META = "bird_metadata"
SCHEMA_PREFIX = "bird_"

# Soft sanity checks only (warn, do not fail). Adjust if your release differs.
EXPECTED_DB_COUNTS = {"dev_databases": 11, "train_databases": 69}

SKIP_DIR_NAMES = {"__MACOSX"}
SKIP_FILE_NAMES = {".DS_Store"}
# .duckdb/.wal: never ingest the output database itself if it lives inside --bird-dir
SKIP_SUFFIXES = {".zip", ".duckdb", ".wal"}

# Set in main()
BIRD_DIR = None
STORE_BYTES = False
LARGE_ROWS = 2_000_000


# ----------------------------------------------------------------- helpers
def quote(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def file_hash(path):
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


def schema_name(path):
    """Readable schema per database, e.g. california_schools.sqlite -> bird_california_schools."""
    return SCHEMA_PREFIX + re.sub(r"[^0-9a-z_]+", "_", path.stem.lower())


def is_skipped(path):
    if path.name in SKIP_FILE_NAMES or path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return any(part in SKIP_DIR_NAMES for part in path.relative_to(BIRD_DIR).parts)


def find_sqlite_files():
    return sorted(
        p for p in BIRD_DIR.rglob("*.sqlite") if p.is_file() and not is_skipped(p)
    )


def open_sqlite(path):
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
    rows = sqlite_db.execute("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """).fetchall()
    return [r[0] for r in rows]


def get_columns(sqlite_db, table):
    return sqlite_db.execute(f"PRAGMA table_xinfo({literal(table)})").fetchall()


def visible_columns(sqlite_db, table):
    return [r[1] for r in get_columns(sqlite_db, table) if r[6] == 0]


def normalize(value):
    """Keep values and SQLite storage types distinct (integer 1 != text '1')."""
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
    raise TypeError(f"Unexpected value type: {type(value)}")


# ----------------------------------------------------------- verification
def sqlite_normalized_rows(sqlite_db, table):
    columns = visible_columns(sqlite_db, table)
    type_exprs = ", ".join(f"typeof({quote(c)})" for c in columns)
    cursor = sqlite_db.execute(f"SELECT *, {type_exprs} FROM {quote(table)}")
    n = len(columns)
    while True:
        rows = cursor.fetchmany(5000)
        if not rows:
            break
        for row in rows:
            values, types = row[:n], row[n:]
            out = []
            for v, t in zip(values, types):
                if t == "text" and isinstance(v, bytes):
                    v = safe_sqlite_text(v)
                out.append(normalize(v))
            yield tuple(out)


def duck_normalized_rows(db, schema, table):
    cursor = db.execute(f"SELECT * FROM {quote(schema)}.{quote(table)}")
    while True:
        rows = cursor.fetchmany(5000)
        if not rows:
            break
        for row in rows:
            yield tuple(normalize(v) for v in row)


def summarize(rows, large):
    """
    Small tables: exact multiset (Counter) of rows.
    Large tables: order-independent fingerprint (count, sum, xor of row hashes),
    so memory stays flat. Python's hash() is stable within one process.
    """
    if not large:
        counts = Counter(rows)
        return ("exact", counts, sum(counts.values()))
    count, total, xor = 0, 0, 0
    mask = (1 << 64) - 1
    for r in rows:
        h = hash(r) & mask
        count += 1
        total = (total + h) & mask
        xor ^= h
    return ("fingerprint", (total, xor), count)


def validate_table(sqlite_db, db, schema, table):
    source_cols = visible_columns(sqlite_db, table)
    target_cols = [
        r[0]
        for r in db.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
        """, [schema, table]).fetchall()
    ]
    if source_cols != target_cols:
        raise RuntimeError(
            f"Column mismatch: {schema}.{table}\nSource: {source_cols}\nTarget: {target_cols}"
        )

    src_n = sqlite_db.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
    tgt_n = db.execute(
        f"SELECT COUNT(*) FROM {quote(schema)}.{quote(table)}"
    ).fetchone()[0]
    if src_n != tgt_n:
        raise RuntimeError(
            f"Row count mismatch: {schema}.{table} SQLite={src_n} DuckDB={tgt_n}"
        )

    large = src_n > LARGE_ROWS
    s_kind, s_val, s_cnt = summarize(sqlite_normalized_rows(sqlite_db, table), large)
    t_kind, t_val, t_cnt = summarize(duck_normalized_rows(db, schema, table), large)
    if s_cnt != t_cnt or s_val != t_val:
        raise RuntimeError(
            f"Value, type, NULL, or duplicate mismatch ({s_kind}): {schema}.{table}"
        )
    return src_n, tgt_n, len(source_cols), len(target_cols)


# --------------------------------------------------------------- metadata
def setup_metadata(db):
    db.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(META)}")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.files (
            source_path VARCHAR PRIMARY KEY,
            source_hash VARCHAR,
            file_size BIGINT,
            file_type VARCHAR,
            duckdb_schema VARCHAR,
            original_bytes BLOB
        )""")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.objects (
            source_path VARCHAR, object_type VARCHAR, object_name VARCHAR,
            parent_table VARCHAR, original_sql VARCHAR
        )""")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.table_info (
            source_path VARCHAR, table_name VARCHAR, metadata_json VARCHAR
        )""")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.checks (
            source_path VARCHAR, table_name VARCHAR,
            source_rows BIGINT, target_rows BIGINT,
            source_columns BIGINT, target_columns BIGINT,
            values_match BOOLEAN, status VARCHAR
        )""")
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote(META)}.run_info (
            setting VARCHAR PRIMARY KEY, value VARCHAR
        )""")


def save_file_record(db, path, schema=None):
    """Record hash/size. Store raw bytes for non-SQLite files, and for SQLite
    files only when --store-original-bytes is set (it roughly doubles size)."""
    relative = path.relative_to(BIRD_DIR).as_posix()
    digest = file_hash(path)
    size = path.stat().st_size
    data = None
    if schema is None or STORE_BYTES:
        data = path.read_bytes()
        if bytes_hash(data) != digest:
            raise RuntimeError(f"File changed while reading: {relative}")
    db.execute(
        f"INSERT INTO {quote(META)}.files VALUES (?, ?, ?, ?, ?, ?)",
        [relative, digest, size, path.suffix.lower(), schema, data],
    )


def pragma_data(sqlite_db, table):
    result = {}
    for pragma in ["table_xinfo", "foreign_key_list", "index_list"]:
        cur = sqlite_db.execute(f"PRAGMA {pragma}({literal(table)})")
        names = [d[0] for d in cur.description]
        result[pragma] = [dict(zip(names, r)) for r in cur.fetchall()]
    result["indexes"] = {}
    for index in result["index_list"]:
        cur = sqlite_db.execute(f"PRAGMA index_xinfo({literal(index['name'])})")
        names = [d[0] for d in cur.description]
        result["indexes"][index["name"]] = [dict(zip(names, r)) for r in cur.fetchall()]
    return result


def save_schema_metadata(db, sqlite_db, relative):
    objects = sqlite_db.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    for object_type, name, parent, sql in objects:
        db.execute(
            f"INSERT INTO {quote(META)}.objects VALUES (?, ?, ?, ?, ?)",
            [relative, object_type, name, parent, sql],
        )
    for table in get_tables(sqlite_db):
        details = pragma_data(sqlite_db, table)
        db.execute(
            f"INSERT INTO {quote(META)}.table_info VALUES (?, ?, ?)",
            [relative, table, json.dumps(details, ensure_ascii=False, default=str)],
        )


# ----------------------------------------------------------------- import
def import_table_preserving_types(sqlite_db, db, schema, table):
    """One DuckDB type per column when SQLite storage is uniform; a UNION otherwise."""
    columns = visible_columns(sqlite_db, table)
    type_map = {"integer": "BIGINT", "real": "DOUBLE", "text": "VARCHAR", "blob": "BLOB"}
    union_type = (
        "UNION(integer_value BIGINT, real_value DOUBLE, "
        "text_value VARCHAR, blob_value BLOB)"
    )

    defs = []
    for column in columns:
        qc = quote(column)
        types = {
            r[0]
            for r in sqlite_db.execute(
                f"SELECT DISTINCT typeof({qc}) FROM {quote(table)} WHERE {qc} IS NOT NULL"
            )
        }
        duck_type = type_map[next(iter(types))] if len(types) == 1 else (
            union_type if types else "VARCHAR"  # all-NULL column
        )
        defs.append(f"{qc} {duck_type}")

    db.execute(f"CREATE TABLE {quote(schema)}.{quote(table)} ({', '.join(defs)})")

    insert_sql = (
        f"INSERT INTO {quote(schema)}.{quote(table)} "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    type_exprs = ", ".join(f"typeof({quote(c)})" for c in columns)
    cursor = sqlite_db.execute(f"SELECT *, {type_exprs} FROM {quote(table)}")
    n = len(columns)

    while True:
        fetched = cursor.fetchmany(5000)
        if not fetched:
            break
        safe_rows = [
            tuple(
                safe_sqlite_text(v) if isinstance(v, bytes) and row[n + i] == "text" else v
                for i, v in enumerate(row[:n])
            )
            for row in fetched
        ]
        db.executemany(insert_sql, safe_rows)


def import_database(db, path):
    relative = path.relative_to(BIRD_DIR).as_posix()
    schema = schema_name(path)

    existing = db.execute(
        f"SELECT source_hash, duckdb_schema FROM {quote(META)}.files WHERE source_path = ?",
        [relative],
    ).fetchone()

    if existing:
        print(f"Already imported: {relative}")
        if existing[1] != schema:
            raise RuntimeError(
                f"{relative} was imported as schema {existing[1]}, expected {schema}. "
                "Delete the .duckdb file and re-run the import."
            )
        if file_hash(path) != existing[0]:
            raise RuntimeError(f"Original file changed: {relative}")
        with open_sqlite(path) as source:
            for table in get_tables(source):
                validate_table(source, db, schema, table)
        n_tables, n_rows = db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(source_rows), 0) "
            f"FROM {quote(META)}.checks WHERE source_path = ?",
            [relative],
        ).fetchone()
        print(f"PASS: existing database verified: {relative}")
        return n_tables, n_rows

    if db.execute(
        "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = ?", [schema]
    ).fetchone()[0]:
        raise RuntimeError(f"Schema already exists without a file record: {schema}")

    sqlite_db = open_sqlite(path)
    try:
        if sqlite_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {relative}")

        tables = get_tables(sqlite_db)
        db.execute("BEGIN TRANSACTION")
        try:
            db.execute(f"CREATE SCHEMA {quote(schema)}")
            save_file_record(db, path, schema)
            save_schema_metadata(db, sqlite_db, relative)

            total_rows = 0
            for table in tables:
                print(f"  Importing {table}", flush=True)
                import_table_preserving_types(sqlite_db, db, schema, table)
                src_n, tgt_n, src_c, tgt_c = validate_table(sqlite_db, db, schema, table)
                db.execute(
                    f"INSERT INTO {quote(META)}.checks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [relative, table, src_n, tgt_n, src_c, tgt_c, True, "PASS"],
                )
                total_rows += src_n

            saved = db.execute(
                f"SELECT source_hash FROM {quote(META)}.files WHERE source_path = ?",
                [relative],
            ).fetchone()[0]
            if file_hash(path) != saved:
                raise RuntimeError(f"Original file changed during import: {relative}")

            db.execute("COMMIT")
            print(f"PASS: {relative} | {len(tables)} tables | {total_rows} rows", flush=True)
            return len(tables), total_rows
        except Exception:
            db.execute("ROLLBACK")
            raise
    finally:
        sqlite_db.close()


def import_other_files(db):
    files = sorted(
        p for p in BIRD_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() != ".sqlite" and not is_skipped(p)
    )
    for path in files:
        relative = path.relative_to(BIRD_DIR).as_posix()
        existing = db.execute(
            f"SELECT source_hash FROM {quote(META)}.files WHERE source_path = ?", [relative]
        ).fetchone()
        if existing:
            if file_hash(path) != existing[0]:
                raise RuntimeError(f"Previously saved file changed: {relative}")
            continue
        db.execute("BEGIN TRANSACTION")
        try:
            save_file_record(db, path)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    print(f"Preserved {len(files)} additional files.", flush=True)


# ------------------------------------------------------------------ audit
def audit(db, full=False):
    print("\nRunning final audit...")
    records = db.execute(f"""
        SELECT source_path, source_hash, file_size, duckdb_schema
        FROM {quote(META)}.files ORDER BY source_path
    """).fetchall()

    for relative, digest, size, schema in records:
        path = BIRD_DIR / relative
        if not path.is_file():
            raise RuntimeError(f"Missing source file: {relative}")
        if file_hash(path) != digest:
            raise RuntimeError(f"Original file hash mismatch: {relative}")

        stored = db.execute(
            f"SELECT original_bytes FROM {quote(META)}.files WHERE source_path = ?",
            [relative],
        ).fetchone()[0]
        if stored is not None:
            if len(stored) != size or bytes_hash(stored) != digest:
                raise RuntimeError(f"Stored bytes mismatch: {relative}")

        if schema is not None:
            with open_sqlite(path) as source:
                original = get_tables(source)
                imported = [
                    r[0]
                    for r in db.execute("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = ? AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                    """, [schema]).fetchall()
                ]
                if original != imported:
                    raise RuntimeError(f"Table list mismatch: {relative}")
                if full:
                    for table in original:
                        validate_table(source, db, schema, table)

    n_checks, src_rows, tgt_rows, failures = db.execute(f"""
        SELECT COUNT(*), COALESCE(SUM(source_rows), 0), COALESCE(SUM(target_rows), 0),
               COUNT(*) FILTER (WHERE status != 'PASS' OR values_match != TRUE
                                   OR source_rows != target_rows
                                   OR source_columns != target_columns)
        FROM {quote(META)}.checks
    """).fetchone()
    sqlite_count = db.execute(
        f"SELECT COUNT(*) FROM {quote(META)}.files WHERE duckdb_schema IS NOT NULL"
    ).fetchone()[0]

    if failures:
        raise RuntimeError(f"{failures} failed table checks")
    if src_rows != tgt_rows:
        raise RuntimeError("Total row counts do not match")

    print(f"SQLite databases: {sqlite_count}")
    print(f"Tables: {n_checks}")
    print(f"Source rows: {src_rows}")
    print(f"DuckDB rows: {tgt_rows}")
    print(f"Failed checks: {failures}")
    print("AUDIT PASSED")


def table_columns(db, schema, table):
    return db.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
    """, [schema, table]).fetchall()


def rename_schemas(db):
    """Move each database from its old schema to schema_name() inside DuckDB.
    DuckDB cannot rename schemas, so tables are copied, checked, and the old schema dropped."""
    records = db.execute(f"""
        SELECT source_path, duckdb_schema FROM {quote(META)}.files
        WHERE duckdb_schema IS NOT NULL ORDER BY source_path
    """).fetchall()
    targets = Counter(schema_name(Path(relative)) for relative, _ in records)
    clashes = sorted(s for s, n in targets.items() if n > 1)
    if clashes:
        raise SystemExit(f"Several databases map to the same schema name: {clashes}")

    for relative, old in records:
        new = schema_name(Path(relative))
        if old == new:
            continue
        if db.execute(
            "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = ?", [new]
        ).fetchone()[0]:
            raise RuntimeError(f"Schema already exists: {new}")
        tables = [
            r[0]
            for r in db.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = ? AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """, [old]).fetchall()
        ]
        print(f"{old} -> {new} ({len(tables)} tables)", flush=True)

        db.execute("BEGIN TRANSACTION")
        try:
            db.execute(f"CREATE SCHEMA {quote(new)}")
            for table in tables:
                src, dst = f"{quote(old)}.{quote(table)}", f"{quote(new)}.{quote(table)}"
                db.execute(f"CREATE TABLE {dst} AS SELECT * FROM {src}")
                if table_columns(db, old, table) != table_columns(db, new, table):
                    raise RuntimeError(f"Column/type mismatch after copy: {new}.{table}")
                old_n = db.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
                new_n = db.execute(f"SELECT COUNT(*) FROM {dst}").fetchone()[0]
                if old_n != new_n:
                    raise RuntimeError(f"Row count mismatch after copy: {new}.{table}")
            db.execute(f"DROP SCHEMA {quote(old)} CASCADE")
            db.execute(
                f"UPDATE {quote(META)}.files SET duckdb_schema = ? WHERE source_path = ?",
                [new, relative],
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    db.execute("CHECKPOINT")


def compact(path):
    """DuckDB does not shrink files after DROP; copy into a fresh file and swap it in."""
    tmp = path.with_name(path.name + ".compact")
    if tmp.exists():
        raise SystemExit(f"Remove leftover file first: {tmp}")
    db = duckdb.connect(str(path))
    try:
        db.execute(f"ATTACH {literal(tmp)} AS compacted")
        db.execute(f"COPY FROM DATABASE {quote(path.stem)} TO compacted")
        db.execute("DETACH compacted")
    finally:
        db.close()
    before = path.stat().st_size
    tmp.replace(path)
    print(f"Compacted {path.name}: {before / 1e9:.2f} GB -> {path.stat().st_size / 1e9:.2f} GB")


def column_attributes(col, fks):
    attrs = []
    if col["pk"]:
        attrs.append("PRIMARY KEY")
    if col["notnull"]:
        attrs.append("NOT NULL")
    if col["dflt_value"] is not None:
        attrs.append(f"DEFAULT {col['dflt_value']}")
    for fk in fks.get(col["name"], []):
        target = fk["table"] + (f"({fk['to']})" if fk["to"] else "")
        attrs.append(f"REFERENCES {target}")
    return " ".join(attrs)


def write_schema_mapping(db, out_path):
    """One row per column: SQLite declared type and attributes next to the DuckDB type."""
    tables = db.execute(f"""
        SELECT f.source_path, f.duckdb_schema, t.table_name, t.metadata_json
        FROM {quote(META)}.files f
        JOIN {quote(META)}.table_info t USING (source_path)
        WHERE f.duckdb_schema IS NOT NULL
        ORDER BY f.source_path, t.table_name
    """).fetchall()

    rows = []
    for relative, schema, table, metadata_json in tables:
        meta = json.loads(metadata_json)
        source_cols = [c for c in meta["table_xinfo"] if c["hidden"] == 0]
        duck_cols = table_columns(db, schema, table)
        if [c["name"] for c in source_cols] != [c[0] for c in duck_cols]:
            raise RuntimeError(f"Column mismatch while writing mapping: {schema}.{table}")

        fks = {}
        for fk in meta["foreign_key_list"]:
            fks.setdefault(fk["from"], []).append(fk)

        for ordinal, (col, (_, duck_type)) in enumerate(zip(source_cols, duck_cols), 1):
            rows.append([
                Path(relative).stem, schema, table, ordinal, col["name"],
                col["type"], duck_type, not col["notnull"], column_attributes(col, fks),
            ])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "source_database", "target_schema", "table", "ordinal", "column",
            "sqlite_type", "duckdb_type", "nullable", "attributes",
        ])
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out_path}")


# ------------------------------------------------------------------- main
def main():
    global BIRD_DIR, STORE_BYTES, LARGE_ROWS

    ap = argparse.ArgumentParser()
    ap.add_argument("--bird-dir", type=Path,
                    help="Folder containing dev_databases/ and/or train_databases/")
    ap.add_argument("--duckdb", default="decode_join_bird.duckdb", type=Path)
    ap.add_argument("--pilot", action="store_true", help="Import only the 'financial' database")
    ap.add_argument("--audit", action="store_true", help="Quick audit of an existing import")
    ap.add_argument("--audit-full", action="store_true", help="Audit and re-verify every table's values")
    ap.add_argument("--store-original-bytes", action="store_true",
                    help="Also embed each .sqlite file's raw bytes in DuckDB (roughly doubles size)")
    ap.add_argument("--large-table-rows", type=int, default=2_000_000,
                    help="Above this row count, verify with a low-memory fingerprint")
    ap.add_argument("--mapping-only", action="store_true",
                    help="Only rewrite schema_mapping.csv from an existing .duckdb file")
    ap.add_argument("--rename-schemas", action="store_true",
                    help="With --mapping-only: first move old hashed schemas to bird_<db_name>")
    args = ap.parse_args()

    if args.rename_schemas and not args.mapping_only:
        raise SystemExit("--rename-schemas requires --mapping-only")
    if args.mapping_only:
        if not args.duckdb.is_file():
            raise SystemExit("DuckDB file not found.")
        db = duckdb.connect(str(args.duckdb), read_only=not args.rename_schemas)
        try:
            if args.rename_schemas:
                rename_schemas(db)
            write_schema_mapping(db, args.duckdb.with_name("schema_mapping.csv"))
        finally:
            db.close()
        if args.rename_schemas:
            compact(args.duckdb)
        return

    if args.bird_dir is None:
        raise SystemExit("--bird-dir is required unless --mapping-only is set")
    BIRD_DIR = args.bird_dir.resolve()
    STORE_BYTES = args.store_original_bytes
    LARGE_ROWS = args.large_table_rows

    if not BIRD_DIR.is_dir():
        raise SystemExit(f"BIRD folder not found: {BIRD_DIR}")

    sqlite_files = find_sqlite_files()
    if not sqlite_files:
        raise SystemExit("No .sqlite files found under --bird-dir")

    print(f"Found {len(sqlite_files)} SQLite files")
    schemas = Counter(schema_name(p) for p in sqlite_files)
    clashes = sorted(s for s, n in schemas.items() if n > 1)
    if clashes:
        raise SystemExit(f"Several databases map to the same schema name: {clashes}")
    per_split = Counter(p.relative_to(BIRD_DIR).parts[0] for p in sqlite_files)
    for split, n in per_split.items():
        exp = EXPECTED_DB_COUNTS.get(split)
        if exp is not None and exp != n:
            print(f"WARNING: {split} has {n} databases, expected about {exp}")

    audit_only = args.audit or args.audit_full
    if audit_only and not args.duckdb.is_file():
        raise SystemExit("DuckDB file not found.")

    db = duckdb.connect(str(args.duckdb))
    try:
        if audit_only:
            audit(db, full=args.audit_full)
            return

        setup_metadata(db)

        if args.pilot:
            sqlite_files = [p for p in sqlite_files if p.stem == "financial"]
            if not sqlite_files:
                raise SystemExit("Pilot database 'financial' not found")

        total_tables = total_rows = 0
        for i, path in enumerate(sqlite_files, 1):
            print(f"\n[{i}/{len(sqlite_files)}] {path.relative_to(BIRD_DIR)}", flush=True)
            n_tables, n_rows = import_database(db, path)
            total_tables += n_tables
            total_rows += n_rows

        print(f"\nImported {total_tables} tables, {total_rows} rows")

        if not args.pilot:
            import_other_files(db)
            db.execute(
                f"INSERT OR REPLACE INTO {quote(META)}.run_info VALUES (?, ?)",
                ["dataset", "BIRD"],
            )

        audit(db, full=False)
        write_schema_mapping(db, args.duckdb.with_name("schema_mapping.csv"))
        print("\nIMPORT COMPLETED SUCCESSFULLY")
    finally:
        db.close()


if __name__ == "__main__":
    main()
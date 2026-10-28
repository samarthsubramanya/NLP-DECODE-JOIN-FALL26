#!/usr/bin/env python3
"""Inventory and transactionally load BEAVER dumps without executing MySQL SQL."""
import argparse
import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time
import zipfile
import duckdb
from mysql_dump import statements, parse_create, insert_rows, target_type

ROOT = Path(__file__).resolve().parents[2]
DATABASES = {"dw": 97, "nova": 109, "neutron": 175}


def quote(s):
    return '"' + s.replace('"', '""') + '"'


def digest_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def canonical(row):
    def one(v):
        if isinstance(v, bytes):
            return {"bytes": v.hex()}
        if isinstance(v, (datetime.datetime, datetime.date)):
            return str(v)
        return v

    return (
        json.dumps(
            [one(v) for v in row],
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
        + b"\n"
    )


def convert(row, table):
    if len(row) != len(table["columns"]):
        raise ValueError(f"{table['name']}: column count mismatch")
    out = []
    for v, c in zip(row, table["columns"]):
        typ = c.get("duckdb_type") or target_type(c["mysql_type"])
        if v is None:
            if not c["nullable"]:
                raise ValueError(f"NULL in NOT NULL {table['name']}.{c['name']}")
        elif typ == "TIMESTAMP":
            try:
                v = datetime.datetime.fromisoformat(v)
            except ValueError as e:
                raise ValueError(
                    f"Invalid timestamp {table['name']}.{c['name']}: {v!r}"
                ) from e
        elif typ == "DATE":
            v = datetime.date.fromisoformat(v)
        elif typ in {"FLOAT", "DOUBLE"}:
            v = float(v)
            if not math.isfinite(v):
                raise ValueError("Nonfinite floating point")
            if typ == "FLOAT":
                import struct

                v = struct.unpack("f", struct.pack("f", v))[0]
        elif typ.startswith(("DECIMAL(", "NUMERIC(")):
            from decimal import Decimal, localcontext

            precision, scale = map(int, re.findall(r"\d+", typ))
            with localcontext() as ctx:
                ctx.prec = max(80, precision + scale + 2)
                original = Decimal(v)
                v = original.quantize(Decimal(1).scaleb(-scale))
                if v != original or abs(v) >= Decimal(10) ** (precision - scale):
                    raise ValueError("Decimal overflow or precision loss")
        elif typ == "VARCHAR" and not isinstance(v, str):
            raise ValueError("Nontext value in text column")
        elif typ == "BLOB" and isinstance(v, str):
            v = v.encode("utf8")
        elif typ.endswith("INT") or typ in {"INTEGER", "UINTEGER"}:
            if not isinstance(v, int):
                raise ValueError("Nonintegral integer literal")
            bits = {"TINYINT": 8, "SMALLINT": 16, "INTEGER": 32, "BIGINT": 64}[
                typ.removeprefix("U")
            ]
            low, high = (
                (0, 2**bits - 1)
                if typ.startswith("U")
                else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
            )
            if not low <= v <= high:
                raise ValueError(f'Integer overflow in {table["name"]}.{c["name"]}')
        out.append(v)
    return out


def scan(archive, report, resume=False):
    started = time.monotonic()
    inventory = {
        "archive": str(archive),
        "archive_sha256": digest_file(archive),
        "source": "https://huggingface.co/datasets/beaverbench/beaver-table",
        "source_revision": "NOT VERIFIED (local files have no revision manifest)",
        "databases": {},
        "issues": [],
    }
    if resume and Path(report).exists():
        previous = json.loads(Path(report).read_text())
        if previous["archive_sha256"] != inventory["archive_sha256"]:
            raise ValueError("Cannot resume: archive changed")
        inventory["databases"] = previous["databases"]
        inventory["cached_databases"] = list(previous["databases"])
    with zipfile.ZipFile(archive) as z:
        # Reading every member to EOF verifies CRC for dumps and Apple metadata.
        for info in z.infolist():
            if info.filename not in {
                "beaver_db/" + name + ".sql" for name in DATABASES
            }:
                with z.open(info) as f:
                    for _ in iter(lambda: f.read(1024 * 1024), b""):
                        pass
        for db, expected in DATABASES.items():
            if db in inventory["databases"]:
                print(
                    f"INVENTORY {db}: using hash-matched completed inventory",
                    flush=True,
                )
                continue
            member = "beaver_db/" + db + ".sql"
            info = z.getinfo(member)
            tables = {}
            directives = []
            hashes = {}
            maxstmt = 0
            command_counts = {}
            with z.open(member) as f:
                for sql in statements(f):
                    maxstmt = max(maxstmt, len(sql.encode()))
                    cmd = sql.split(None, 1)[0]
                    command_counts[cmd] = command_counts.get(cmd, 0) + 1
                    if sql.startswith("/*"):
                        directives.append(sql)
                    elif sql.startswith("CREATE TABLE"):
                        t = parse_create(sql)
                        if tables and len(tables) % 10 == 0:
                            print(
                                f"INVENTORY {db}: parsed {len(tables)} table definitions so far",
                                flush=True,
                            )
                        if t["name"].casefold() in {name.casefold() for name in tables}:
                            raise ValueError("Duplicate or case-colliding CREATE")
                        for c in t["columns"]:
                            c["duckdb_type"] = target_type(c["mysql_type"])
                        t.update(
                            rows=0,
                            samples=[],
                            null_counts=[0] * len(t["columns"]),
                            empty_counts=[0] * len(t["columns"]),
                        )
                        tables[t["name"]] = t
                        hashes[t["name"]] = hashlib.sha256()
                    elif sql.startswith("INSERT INTO"):
                        for name, row in insert_rows(sql):
                            if name not in tables:
                                raise ValueError("INSERT before CREATE")
                            t = tables[name]
                            converted = convert(row, t)
                            t["rows"] += 1
                            hashes[name].update(canonical(converted))
                            if len(t["samples"]) < 3:
                                t["samples"].append(json.loads(canonical(converted)))
                            for i, v in enumerate(row):
                                t["null_counts"][i] += v is None
                                t["empty_counts"][i] += v == ""
                    elif not re.match(
                        r"(CREATE DATABASE|USE |DROP TABLE|LOCK TABLES|UNLOCK TABLES|SET )",
                        sql,
                    ):
                        raise ValueError(
                            f"Unsupported command in {member}: {sql[:200]}"
                        )
            if len(tables) != expected:
                raise ValueError(f"{db}: expected {expected}, found {len(tables)}")
            for n, t in tables.items():
                t["ordered_value_sha256"] = hashes[n].hexdigest()
            inventory["databases"][db] = {
                "member": member,
                "uncompressed_bytes": info.file_size,
                "compressed_bytes": info.compress_size,
                "zip_crc32": f"{info.CRC:08x}",
                "table_count": len(tables),
                "row_count": sum(t["rows"] for t in tables.values()),
                "maximum_statement_bytes": maxstmt,
                "commands": command_counts,
                "directives": directives,
                "tables": tables,
            }
            Path(report).write_text(json.dumps(inventory, indent=2, ensure_ascii=False))
            print(
                f"INVENTORY {db}: {len(tables)} tables, {inventory['databases'][db]['row_count']} rows",
                flush=True,
            )
    inventory["seconds"] = time.monotonic() - started
    Path(report).write_text(json.dumps(inventory, indent=2, ensure_ascii=False))
    return inventory


def guard_target(target):
    # The production path is deliberately unavailable before checkpoint approval.
    if Path(target).resolve() == (ROOT / "data/decode_join.duckdb").resolve():
        raise ValueError(
            "Production target blocked pending explicit approval and Phase 7 preparation"
        )
    if Path(target).is_symlink():
        raise ValueError("Symlink target forbidden")
    production = ROOT / "data/decode_join.duckdb"
    if (
        Path(target).exists()
        and production.exists()
        and Path(target).samefile(production)
    ):
        raise ValueError("Production hardlink target forbidden")


def configure_connection(c):
    """Configure an already locked connection; caller must authorize its target."""
    c.execute("SET memory_limit='512MB'")
    c.execute("SET threads=2")
    c.execute("SET max_temp_directory_size='2GB'")
    c.execute("CREATE SCHEMA IF NOT EXISTS beaver_metadata")
    c.execute(
        "CREATE TABLE IF NOT EXISTS beaver_metadata.source_tables (source_db VARCHAR, table_name VARCHAR, definition_json VARCHAR, PRIMARY KEY(source_db,table_name))"
    )
    return c


def connect(target):
    guard_target(target)
    return configure_connection(duckdb.connect(str(target)))


def load(
    archive,
    target,
    inventory,
    selection,
    batch_size=1000,
    connection=None,
    safety_check=None,
    progress_path=None,
):
    if (
        not isinstance(selection, dict)
        or not selection
        or set(selection) - DATABASES.keys()
    ):
        raise ValueError("Selection must name only known source databases")
    if any(
        not isinstance(names, list)
        or not all(isinstance(name, str) for name in names)
        or len(names) != len(set(names))
        for names in selection.values()
    ):
        raise ValueError("Selection must contain unique table-name lists")
    if not any(selection.values()):
        raise ValueError("No selected tables")
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    if digest_file(archive) != inventory["archive_sha256"]:
        raise ValueError("Archive SHA-256 differs from inventory")
    if shutil.disk_usage(Path(target).resolve().parent).free < 4 * 1024**3:
        raise ValueError("Less than 4 GiB free; refusing pilot/import")
    owns_connection = connection is None
    c = connect(target) if owns_connection else configure_connection(connection)
    results = []
    started = time.monotonic()
    try:
        for db in DATABASES:
            selected = set(selection.get(db, []))
            if not selected:
                continue
            definitions = inventory["databases"][db]["tables"]
            if selected - definitions.keys():
                raise ValueError("Unknown selected tables")
            c.execute("CREATE SCHEMA IF NOT EXISTS " + quote("beaver_" + db))
            # Atomic per-database replacement. A failed transaction leaves previous data intact.
            c.execute("BEGIN")
            active_table = None
            try:
                expected_hash = {n: hashlib.sha256() for n in selected}
                counts = {n: 0 for n in selected}
                source_acc = {n: 0 for n in selected}
                for n in sorted(selected):
                    active_table = n
                    t = definitions[n]
                    if any(
                        col["duckdb_type"] != target_type(col["mysql_type"])
                        for col in t["columns"]
                    ):
                        raise ValueError(
                            "Inventory type mapping differs from current loader"
                        )
                    qualified = quote("beaver_" + db) + "." + quote(n)
                    exists = c.execute(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                        ["beaver_" + db, n],
                    ).fetchone()[0]
                    owned = c.execute(
                        "SELECT count(*) FROM beaver_metadata.source_tables WHERE source_db=? AND table_name=?",
                        [db, n],
                    ).fetchone()[0]
                    if exists and not owned:
                        raise ValueError(
                            "Refusing to replace unowned table " + qualified
                        )
                    c.execute("DROP TABLE IF EXISTS " + qualified)
                    ddl = ",".join(
                        quote(col["name"])
                        + " "
                        + col["duckdb_type"]
                        + (" NOT NULL" if not col["nullable"] else "")
                        for col in t["columns"]
                    )
                    c.execute("CREATE TABLE " + qualified + " (" + ddl + ")")
                    c.execute(
                        "INSERT OR REPLACE INTO beaver_metadata.source_tables VALUES (?,?,?)",
                        [db, n, json.dumps(t)],
                    )
                import pyarrow as pa

                def flush(n, rows):
                    if not rows:
                        return
                    if safety_check:
                        safety_check()
                    if (
                        shutil.disk_usage(Path(target).resolve().parent).free
                        < 4 * 1024**3
                    ):
                        raise ValueError("Disk reserve breached; rolling back")
                    t = definitions[n]
                    qualified = quote("beaver_" + db) + "." + quote(n)
                    # Arrow vectors avoid DuckDB executemany's per-row overhead.
                    arrow_types = {
                        "VARCHAR": pa.string(),
                        "TINYINT": pa.int8(),
                        "SMALLINT": pa.int16(),
                        "INTEGER": pa.int32(),
                        "BIGINT": pa.int64(),
                        "UTINYINT": pa.uint8(),
                        "USMALLINT": pa.uint16(),
                        "UINTEGER": pa.uint32(),
                        "UBIGINT": pa.uint64(),
                        "FLOAT": pa.float32(),
                        "DOUBLE": pa.float64(),
                        "TIMESTAMP": pa.timestamp("us"),
                        "DATE": pa.date32(),
                        "BLOB": pa.binary(),
                    }
                    arrays = []
                    for i, col in enumerate(t["columns"]):
                        typ = col["duckdb_type"]
                        at = arrow_types.get(typ)
                        if at is None:
                            m = re.fullmatch(r"(?:DECIMAL|NUMERIC)\((\d+),(\d+)\)", typ)
                            if not m:
                                raise ValueError("Unsupported Arrow mapping " + typ)
                            at = pa.decimal128(int(m[1]), int(m[2]))
                        arrays.append(pa.array([r[i] for r in rows], type=at))
                    batch = pa.Table.from_arrays(
                        arrays, names=[x["name"] for x in t["columns"]]
                    )
                    c.register("_beaver_batch", batch)
                    try:
                        c.execute(
                            "INSERT INTO " + qualified + " SELECT * FROM _beaver_batch"
                        )
                    finally:
                        c.unregister("_beaver_batch")

                with zipfile.ZipFile(archive) as z, z.open(
                    "beaver_db/" + db + ".sql"
                ) as f:
                    for sql in statements(f):
                        if not sql.startswith("INSERT INTO"):
                            continue
                        name = re.match(r"INSERT INTO `((?:[^`]|``)+)`", sql)[
                            1
                        ].replace("``", "`")
                        if name not in selected:
                            continue
                        active_table = name
                        rows = []
                        for n, row in insert_rows(sql):
                            converted = convert(row, definitions[n])
                            rows.append(converted)
                            counts[n] += 1
                            if counts[n] % 100000 == 0:
                                print(
                                    f"PROGRESS {db}.{n}: {counts[n]:,} rows", flush=True
                                )
                            encoded = canonical(converted)
                            expected_hash[n].update(encoded)
                            source_acc[n] = (
                                source_acc[n]
                                + int.from_bytes(
                                    hashlib.sha256(encoded).digest(), "big"
                                )
                            ) % (1 << 256)
                            if len(rows) >= batch_size:
                                flush(n, rows)
                                rows = []
                        flush(name, rows)
                for n in sorted(selected):
                    active_table = n
                    t = definitions[n]
                    qualified = quote("beaver_" + db) + "." + quote(n)
                    actual = c.execute("SELECT count(*) FROM " + qualified).fetchone()[
                        0
                    ]
                    if actual != t["rows"] or actual != counts[n]:
                        raise ValueError("Row count mismatch " + n)
                    if expected_hash[n].hexdigest() != t["ordered_value_sha256"]:
                        raise ValueError("Source changed since inventory " + n)
                    # Validate all imported values as a multiset including duplicate multiplicity.
                    # Compare order-independent SHA-256 accumulators, avoiding sort/spill.
                    modulus = 1 << 256

                    def accumulator(rows):
                        total = 0
                        for r in rows:
                            total = (
                                total
                                + int.from_bytes(
                                    hashlib.sha256(canonical(r)).digest(), "big"
                                )
                            ) % modulus
                        return total

                    actual_acc = 0
                    cursor = c.execute("SELECT * FROM " + qualified)
                    while True:
                        rows = cursor.fetchmany(batch_size)
                        if not rows:
                            break
                        actual_acc = (actual_acc + accumulator(rows)) % modulus
                    if actual_acc != source_acc[n]:
                        raise ValueError("Value checksum mismatch " + n)
                    described = c.execute("DESCRIBE " + qualified).fetchall()
                    if [(r[0], r[1], r[2] == "YES") for r in described] != [
                        (col["name"], col["duckdb_type"], col["nullable"])
                        for col in t["columns"]
                    ]:
                        raise ValueError("Schema mismatch " + n)
                    for i, col in enumerate(t["columns"]):
                        qcol = quote(col["name"])
                        nulls = c.execute(
                            "SELECT count(*) FROM "
                            + qualified
                            + " WHERE "
                            + qcol
                            + " IS NULL"
                        ).fetchone()[0]
                        if nulls != t["null_counts"][i]:
                            raise ValueError("NULL count mismatch " + n)
                        if col["duckdb_type"] == "VARCHAR":
                            empties = c.execute(
                                "SELECT count(*) FROM "
                                + qualified
                                + " WHERE "
                                + qcol
                                + " = ''"
                            ).fetchone()[0]
                            if empties != t["empty_counts"][i]:
                                raise ValueError("Empty string count mismatch " + n)
                    results.append(
                        dict(
                            database=db,
                            table=n,
                            source_rows=t["rows"],
                            target_rows=actual,
                            target_multiset_sha256_sum=f"{actual_acc:064x}",
                            source_multiset_sha256_sum=f"{source_acc[n]:064x}",
                            status="PASS",
                            columns=c.execute("DESCRIBE " + qualified).fetchall(),
                        )
                    )
                    print(f"LOADED {db}.{n}: {actual}", flush=True)
                if safety_check:
                    safety_check()
                c.execute("COMMIT")
                c.execute("CHECKPOINT")
                if progress_path:
                    Path(progress_path).write_text(
                        json.dumps(
                            {
                                "status": "IN_PROGRESS",
                                "completed_databases": sorted(
                                    {row["database"] for row in results}
                                ),
                                "tables": results,
                            },
                            indent=2,
                            default=str,
                        )
                    )
            except Exception as error:
                try:
                    c.execute("ROLLBACK")
                except duckdb.TransactionException:
                    pass
                raise ValueError(
                    f"BEAVER {db}.{active_table}: import stopped: {error}. Uncommitted changes roll back; check completed-database report for committed checkpoints."
                ) from error
        c.execute("CHECKPOINT")
    finally:
        if owns_connection:
            c.close()
    import resource

    return dict(
        tables=results,
        seconds=time.monotonic() - started,
        database_bytes=Path(target).stat().st_size,
        process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--archive", type=Path, default=ROOT / "datasets/beaver/beaver_db.zip"
    )
    p.add_argument(
        "--inventory",
        type=Path,
        default=Path(__file__).parent / "reports/source_inventory.json",
    )
    p.add_argument("--scan", action="store_true")
    p.add_argument("--resume-inventory", action="store_true")
    p.add_argument("--target", type=Path)
    p.add_argument("--selection", type=Path)
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--report", type=Path)
    a = p.parse_args()
    if a.scan:
        scan(a.archive, a.inventory, a.resume_inventory)
    if a.target:
        if not a.selection or not a.report:
            p.error("--target requires --selection and --report")
        inv = json.loads(a.inventory.read_text())
        sel = json.loads(a.selection.read_text())
        try:
            result = load(a.archive, a.target, inv, sel, a.batch_size)
        except Exception as error:
            a.report.write_text(
                json.dumps(
                    {
                        "status": "FAIL",
                        "error": str(error),
                        "target": str(a.target),
                        "recovery": "Failed database transaction rolled back; earlier completed databases may remain. Rerun selected tables safely.",
                    },
                    indent=2,
                )
            )
            raise
        a.report.write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

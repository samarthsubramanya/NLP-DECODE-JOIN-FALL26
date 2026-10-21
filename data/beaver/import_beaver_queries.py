#!/usr/bin/env python3
"""Preserve each BEAVER Parquet split in its own table, with exact multiset audit."""
import argparse
import json
from pathlib import Path
import time
import shutil
import duckdb
from import_beaver import ROOT, guard_target, quote, digest_file

SPLITS = ("dw", "dw_real", "nova", "neutron")


def run(
    source_dir,
    target,
    report,
    source_inventory=None,
    connection=None,
    safety_check=None,
):
    owns_connection = connection is None
    if owns_connection:
        guard_target(target)
    if shutil.disk_usage(Path(target).resolve().parent).free < 4 * 1024**3:
        raise ValueError("Less than 4 GiB free")
    started = time.monotonic()
    c = duckdb.connect(str(target)) if owns_connection else connection
    c.execute("SET memory_limit='512MB'")
    c.execute("SET threads=2")
    c.execute("SET max_temp_directory_size='2GB'")
    results = {}
    known = None
    if source_inventory:
        known = json.loads(Path(source_inventory).read_text())["databases"]
    try:
        c.execute("CREATE SCHEMA IF NOT EXISTS beaver_metadata")
        c.execute(
            "CREATE TABLE IF NOT EXISTS beaver_metadata.query_sources (split VARCHAR PRIMARY KEY, source_path VARCHAR, sha256 VARCHAR, row_count BIGINT)"
        )
        c.execute("BEGIN")
        try:
            for split in SPLITS:
                if safety_check:
                    safety_check()
                path = source_dir / (split + "-00000-of-00001.parquet")
                if not path.is_file():
                    raise FileNotFoundError(path)
                c.execute(
                    "CREATE OR REPLACE TEMP VIEW _query_source AS SELECT * FROM read_parquet("
                    + "'"
                    + str(path).replace("'", "''")
                    + "')"
                )
                schema = c.execute("DESCRIBE _query_source").fetchall()
                columns = [r[0] for r in schema]
                for reserved in ["_beaver_source_file", "_beaver_source_split"]:
                    if reserved.casefold() in {col.casefold() for col in columns}:
                        raise ValueError("Provenance column collision")
                table = "beaver_metadata." + quote("queries_" + split)
                exists = c.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                    ["beaver_metadata", "queries_" + split],
                ).fetchone()[0]
                owned = c.execute(
                    "SELECT count(*) FROM beaver_metadata.query_sources WHERE split=?",
                    [split],
                ).fetchone()[0]
                if exists and not owned:
                    raise ValueError("Refusing to replace unowned query table " + table)
                c.execute(
                    "CREATE OR REPLACE TABLE "
                    + table
                    + " AS SELECT *, ?::VARCHAR AS _beaver_source_file, ?::VARCHAR AS _beaver_source_split FROM _query_source",
                    [path.name, split],
                )
                projection = ",".join(quote(n) for n in columns)
                # EXCEPT ALL catches every field difference and duplicate multiplicity.
                left = c.execute(
                    "SELECT count(*) FROM (SELECT "
                    + projection
                    + " FROM "
                    + table
                    + " EXCEPT ALL SELECT * FROM _query_source)"
                ).fetchone()[0]
                right = c.execute(
                    "SELECT count(*) FROM (SELECT * FROM _query_source EXCEPT ALL SELECT "
                    + projection
                    + " FROM "
                    + table
                    + ")"
                ).fetchone()[0]
                if left or right:
                    raise ValueError("Exact query comparison failed")
                count = c.execute("SELECT count(*) FROM _query_source").fetchone()[0]
                nulls = {
                    n: c.execute(
                        "SELECT count(*) FROM _query_source WHERE "
                        + quote(n)
                        + " IS NULL"
                    ).fetchone()[0]
                    for n in columns
                }
                empty_counts = {
                    name: c.execute(
                        "SELECT count(*) FROM _query_source WHERE "
                        + quote(name)
                        + " = ''"
                    ).fetchone()[0]
                    for name, typ, *_ in schema
                    if typ == "VARCHAR"
                }
                json_annotations = {}
                for name in (
                    "tables",
                    "join_keys",
                    "column_mapping",
                    "domain_knowledge",
                    "sub_questions",
                    "sub_sqls",
                ):
                    if name not in columns:
                        continue
                    typ = dict((row[0], row[1]) for row in schema)[name]
                    if typ != "VARCHAR":
                        continue
                    kinds = {}
                    invalid = []
                    for index, (text,) in enumerate(
                        c.execute(
                            "SELECT " + quote(name) + " FROM _query_source"
                        ).fetchall()
                    ):
                        if text is None:
                            kind = "SQL_NULL"
                        else:
                            try:
                                kind = type(json.loads(text)).__name__
                            except (ValueError, TypeError):
                                kind = "INVALID_JSON"
                                invalid.append(index)
                        kinds[kind] = kinds.get(kind, 0) + 1
                    json_annotations[name] = {
                        "decoded_types": kinds,
                        "invalid_source_row_ordinals": invalid,
                    }
                dups = (
                    c.execute(
                        "SELECT id,count(*) FROM _query_source GROUP BY id HAVING count(*)>1"
                    ).fetchall()
                    if "id" in columns
                    else None
                )
                db_counts = (
                    c.execute(
                        "SELECT db,count(*) FROM _query_source GROUP BY db"
                    ).fetchall()
                    if "db" in columns
                    else None
                )
                unresolved = []
                bad_json = []
                refs = 0
                case_matches = {}
                join_issues = []
                join_refs = 0

                def resolve(db, name):
                    if db not in known:
                        return None
                    tables = known[db]["tables"]
                    if name in tables:
                        return name
                    matches = [n for n in tables if n.casefold() == name.casefold()]
                    return matches[0] if len(matches) == 1 else None

                if known and {"db", "tables"} <= set(columns):
                    id_expr = "id" if "id" in columns else "NULL"
                    joins_expr = "join_keys" if "join_keys" in columns else "NULL"
                    for id_, db, tables, joins in c.execute(
                        "SELECT "
                        + id_expr
                        + ",db,tables,"
                        + joins_expr
                        + " FROM _query_source"
                    ).fetchall():
                        if tables is not None:
                            try:
                                names = (
                                    json.loads(tables)
                                    if isinstance(tables, str)
                                    else tables
                                )
                            except (ValueError, TypeError):
                                bad_json.append(id_)
                                continue
                            if names is None:
                                names = []
                            if not isinstance(names, list) or not all(
                                isinstance(n, str) for n in names
                            ):
                                bad_json.append(id_)
                                continue
                            for name in names:
                                refs += 1
                                resolved = resolve(db, name)
                                if resolved is None:
                                    unresolved.append(dict(id=id_, db=db, table=name))
                                elif resolved != name:
                                    key = db + "." + name + " -> " + resolved
                                    case_matches[key] = case_matches.get(key, 0) + 1
                        if joins is not None:
                            try:
                                pairs = (
                                    json.loads(joins)
                                    if isinstance(joins, str)
                                    else joins
                                )
                            except (ValueError, TypeError):
                                join_issues.append(dict(id=id_, reason="invalid JSON"))
                                continue
                            if pairs is None:
                                pairs = []
                            if not isinstance(pairs, list):
                                join_issues.append(dict(id=id_, reason="expected list"))
                                continue
                            for pair in pairs:
                                if not isinstance(pair, list) or len(pair) != 2:
                                    join_issues.append(
                                        dict(id=id_, reason="expected pair")
                                    )
                                    continue
                                for endpoint in pair:
                                    join_refs += 1
                                    if (
                                        not isinstance(endpoint, str)
                                        or "." not in endpoint
                                    ):
                                        join_issues.append(
                                            dict(
                                                id=id_,
                                                endpoint=endpoint,
                                                reason="unqualified annotation",
                                            )
                                        )
                                        continue
                                    table_name, column = endpoint.rsplit(".", 1)
                                    resolved = resolve(db, table_name)
                                    if resolved is None:
                                        join_issues.append(
                                            dict(
                                                id=id_,
                                                endpoint=endpoint,
                                                reason="unresolved table",
                                            )
                                        )
                                        continue
                                    cols = known[db]["tables"][resolved]["columns"]
                                    if column.casefold() not in {
                                        col["name"].casefold() for col in cols
                                    }:
                                        join_issues.append(
                                            dict(
                                                id=id_,
                                                endpoint=endpoint,
                                                reason="unresolved column",
                                            )
                                        )
                results[split] = dict(
                    path=str(path),
                    bytes=path.stat().st_size,
                    sha256=digest_file(path),
                    rows=count,
                    schema=schema,
                    null_counts=nulls,
                    empty_counts=empty_counts,
                    json_annotations=json_annotations,
                    duplicate_ids=dups,
                    db_counts=db_counts,
                    exact_multiset_comparison="PASS",
                    reference_annotations_checked=refs,
                    unresolved_references=unresolved,
                    case_insensitive_annotation_matches=case_matches,
                    join_annotation_endpoints_checked=join_refs,
                    join_annotation_issues=join_issues,
                    malformed_table_annotations=bad_json,
                    sql_execution="NOT VERIFIED (original MySQL SQL preserved without execution)",
                    nested_fields={
                        name: typ
                        for name, typ, *_ in schema
                        if any(
                            mark in typ for mark in ("[]", "STRUCT(", "MAP(", "UNION(")
                        )
                    },
                    annotation_storage="VARCHAR JSON retained verbatim; native Parquet nested fields remain native",
                )
                c.execute(
                    "INSERT OR REPLACE INTO beaver_metadata.query_sources VALUES (?,?,?,?)",
                    [split, str(path), results[split]["sha256"], count],
                )
                print(f"QUERIES {split}: {count}, exact comparison PASS", flush=True)
            if safety_check:
                safety_check()
            c.execute("COMMIT")
            c.execute("CHECKPOINT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except duckdb.TransactionException:
                pass
            raise
    finally:
        if owns_connection:
            c.close()
    result = dict(
        source="https://huggingface.co/datasets/beaverbench/beaver-query",
        source_revision="NOT VERIFIED (no local revision manifest)",
        splits=results,
        total_rows=sum(x["rows"] for x in results.values()),
        seconds=time.monotonic() - started,
    )
    Path(report).write_text(json.dumps(result, indent=2, default=str))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir", type=Path, default=ROOT / "datasets/beaver/queries")
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--source-inventory", type=Path)
    a = p.parse_args()
    run(a.source_dir, a.target, a.report, a.source_inventory)


if __name__ == "__main__":
    main()

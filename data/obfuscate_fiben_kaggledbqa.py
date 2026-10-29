from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parent

DATASETS = {
    "fiben": (
        ROOT / "fiben/original/decode_join_fiben.duckdb",
        ROOT / "fiben/obfuscated/decode_join_fiben_obfuscated.duckdb",
        152, 374,
    ),
    "kaggledbqa": (
        ROOT / "kaggledbqa/stripped/decode_join_kaggledbqa_stripped.duckdb",
        ROOT / "kaggledbqa/obfuscated/decode_join_kaggledbqa_obfuscated.duckdb",
        17, 179,
    ),
}

def q(value):
    return '"' + value.replace('"', '""') + '"'

def build(name, source, output, expected_tables, expected_columns):
    if not source.is_file():
        raise FileNotFoundError(source)

    if output.exists():
        raise FileExistsError(
            f"Output already exists: {output}. "
            "Remove only after confirming it is incomplete."
        )

    with duckdb.connect(str(source), read_only=True) as src:
        tables = src.execute("""
            SELECT schema_name, table_name
            FROM duckdb_tables()
            WHERE NOT internal
            ORDER BY schema_name, table_name
        """).fetchall()

        columns = src.execute("""
            SELECT schema_name, table_name, column_name, data_type
            FROM duckdb_columns()
            WHERE NOT internal
            ORDER BY schema_name, table_name, column_index
        """).fetchall()

        assert len(tables) == expected_tables
        assert len(columns) == expected_columns

        grouped = {}
        for schema, table, column, typ in columns:
            grouped.setdefault((schema, table), []).append((column, typ))

        output.parent.mkdir(parents=True, exist_ok=True)

        # Create a temporary output so failures do not produce a final file.
        temporary = output.with_name(output.stem + ".building.duckdb")

        if temporary.exists():
            raise FileExistsError(temporary)

        try:
            with duckdb.connect(str(temporary)) as dst:
                path = str(source).replace("'", "''")
                dst.execute(f"ATTACH '{path}' AS source_db (READ_ONLY)")

                for schema in sorted({s for s, _ in tables}):
                    dst.execute(f"CREATE SCHEMA {q(schema)}")

                for index, (schema, table) in enumerate(tables, 1):
                    cols = grouped[(schema, table)]
                    projection = ", ".join(
                        f"{q(col)} AS {q(f'col_{i:03d}')}"
                        for i, (col, _) in enumerate(cols, 1)
                    )

                    target = f"{q(schema)}.{q(table)}"
                    original = f"source_db.{target}"

                    dst.execute(
                        f"CREATE TABLE {target} AS "
                        f"SELECT {projection} FROM {original}"
                    )

                    if index % 25 == 0 or index == len(tables):
                        print(f"{name}: {index}/{len(tables)} tables", flush=True)

                dst.execute("DETACH source_db")
                dst.execute("CHECKPOINT")

            # Validate the completed database before publishing its filename.
            with duckdb.connect(str(temporary), read_only=True) as dst:
                actual_tables = dst.execute(
                    "SELECT schema_name, table_name FROM duckdb_tables() "
                    "WHERE NOT internal ORDER BY 1,2"
                ).fetchall()
                assert actual_tables == tables

                actual_columns = dst.execute("""
                    SELECT schema_name, table_name, column_name, data_type
                    FROM duckdb_columns()
                    WHERE NOT internal
                    ORDER BY schema_name, table_name, column_index
                """).fetchall()

                expected = [
                    (schema, table, f"col_{i:03d}", typ)
                    for (schema, table), cols in grouped.items()
                    for i, (_, typ) in enumerate(cols, 1)
                ]
                assert actual_columns == expected

                assert dst.execute(
                    "SELECT COUNT(*) FROM duckdb_constraints()"
                ).fetchone()[0] == 0

                for schema, table in tables:
                    src_table = f"{q(schema)}.{q(table)}"
                    dst_table = src_table
                    original_cols = grouped[(schema, table)]

                    src_projection = ", ".join(
                        f"{q(col)} AS {q(f'col_{i:03d}')}"
                        for i, (col, _) in enumerate(original_cols, 1)
                    )

                    # Duplicate-sensitive exact data comparison.
                    comparison = f"""
                        WITH a AS (
                            SELECT {src_projection}
                            FROM src_db.{src_table}
                        ),
                        b AS (
                            SELECT * FROM {dst_table}
                        )
                        SELECT
                            EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM b)
                            OR
                            EXISTS(SELECT * FROM b EXCEPT ALL SELECT * FROM a)
                    """

                    # Attach source read-only for cross-database validation.
                    if (schema, table) == tables[0]:
                        path = str(source).replace("'", "''")
                        dst.execute(f"ATTACH '{path}' AS src_db (READ_ONLY)")

                    assert not dst.execute(comparison).fetchone()[0], (
                        f"Data mismatch: {schema}.{table}"
                    )

                dst.execute("DETACH src_db")

            temporary.rename(output)
            print(f"{name}: VALIDATED — {output}", flush=True)

        except Exception:
            print(f"{name}: FAILED — temporary output: {temporary}")
            raise

if __name__ == "__main__":
    for name, args in DATASETS.items():
        build(name, *args)

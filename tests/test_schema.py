import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from duckdb_utils import SCHEMA_SQL, connect  # noqa: E402

EXPECTED = {
    "databases", "columns", "declared_fks", "obfuscation_map", "questions",
    "gold_columns", "column_profiles", "pk_candidates", "fk_candidates",
    "generated_descriptions", "run_log", "eval_results",
}


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.duckdb")
    c.execute(SCHEMA_SQL.read_text())
    yield c
    c.close()


def test_all_meta_tables_created(con):
    got = {r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema='meta'").fetchall()}
    assert EXPECTED <= got


def test_init_is_idempotent(con):
    con.execute(SCHEMA_SQL.read_text())
    con.execute(SCHEMA_SQL.read_text())


def test_database_registry_rejects_duplicates(con):
    q = "insert into meta.databases (dataset, db_id, duck_schema) values ('bird','financial','bird__financial')"
    con.execute(q)
    with pytest.raises(duckdb.ConstraintException):
        con.execute(q)


def test_run_log_ids_autoincrement(con):
    for op in ("a", "b"):
        con.execute("insert into meta.run_log (stage, operation, input) values ('fk', ?, '{}')", [op])
    assert con.execute("select id from meta.run_log order by id").fetchall() == [(1,), (2,)]


def test_profile_roundtrip_with_json(con):
    con.execute("""insert into meta.column_profiles
        (dataset, db_id, setting, table_name, column_name, null_rate, top_values)
        values ('bird','financial','stripped','district','A4',0.0,'[{"value":"1","count":3}]')""")
    n = con.execute("""select json_extract_string(top_values,'$[0].count')
                       from meta.column_profiles""").fetchone()[0]
    assert n == "3"


def test_profile_key_includes_setting(con):
    ins = """insert into meta.column_profiles (dataset, db_id, setting, table_name, column_name)
             values ('bird','financial', ?, 'district','A4')"""
    for s in ("documented", "stripped", "obfuscated"):
        con.execute(ins, [s])
    with pytest.raises(duckdb.ConstraintException):
        con.execute(ins, ["stripped"])

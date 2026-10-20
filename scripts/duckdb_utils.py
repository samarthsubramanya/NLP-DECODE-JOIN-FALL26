"""Shared DuckDB helpers for DECODE-JOIN."""
import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "decode_join.duckdb"
SCHEMA_SQL = ROOT / "db" / "schema.sql"


def connect(path=DEFAULT_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def ident(name):
    """Quote an identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def schema_name(dataset, db_id):
    return re.sub(r"\W", "_", f"{dataset}__{db_id}").lower()

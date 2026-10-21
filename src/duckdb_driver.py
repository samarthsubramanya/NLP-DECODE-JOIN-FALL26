import csv
from functools import lru_cache

import duckdb
import pandas as pd
from huggingface_hub import hf_hub_download

REPO = "SuhxsReddy/decode-join-duckdb"

@lru_cache(maxsize=None)   # one connection per file, shared across calls
def open_db(filename: str) -> duckdb.DuckDBPyConnection:
    path = hf_hub_download(repo_id=REPO, filename=filename, repo_type="dataset")
    return duckdb.connect(path, read_only=True)   # read-only: safe for several processes

def schema_for(mapping_csv: str, database: str) -> str:
    # mapping_csv rows: source_path, duckdb_schema
    with open(mapping_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if f"/{database}/" in row["source_path"]:
                return row["duckdb_schema"]
    raise KeyError(database)

def preview(db_file: str, schema: str, table: str, is_hashed: bool = False,
            mapping_csv: str | None = None, limit: int = 5) -> pd.DataFrame:
    """
    Print and return the first `limit` rows of schema.table from a DuckDB file in the repo.
    is_hashed=False: `schema` is the DuckDB schema name itself (e.g. bird_financial, beaver_dw).
    is_hashed=True:  `schema` is the source database name (e.g. aircraft); the hashed
                     DuckDB schema is looked up in `mapping_csv`.
    """
    if is_hashed:
        if mapping_csv is None:
            raise ValueError("mapping_csv is required when is_hashed=True")
        schema = schema_for(mapping_csv, schema)
    df = open_db(db_file).execute(f'SELECT * FROM "{schema}"."{table}" LIMIT {int(limit)}').df()
    print("=" * 100)
    print(f"{db_file} | schema: {schema} | table: {table}")
    print(df)
    return df

if __name__ == "__main__":
    # BIRD schemas are bird_<db_name>, see data/bird/schema_mapping.csv
    preview("decode_join_bird_dev.duckdb", "bird_financial", "district")
    preview("decode_join.duckdb", "aircraft", "aircraft",
            is_hashed=True, mapping_csv="data/spider/schema_mapping.csv")
    # BEAVER schemas are fixed names: beaver_dw, beaver_nova, beaver_neutron
    preview("decode_join.duckdb", "beaver_dw", "ACADEMIC_TERMS")
    preview("decode_join_kaggledbqa.duckdb", "WhatCDHipHop", "torrents",
            is_hashed=True, mapping_csv="data/kaggledbqa/schema_mapping.csv")
    print("=" * 100)

"""Create an empty DuckDB file at data/decode_join.duckdb."""
from pathlib import Path

import duckdb

path = Path(__file__).resolve().parent.parent / "data" / "decode_join.duckdb"
path.parent.mkdir(exist_ok=True)
duckdb.connect(str(path)).close()
print(f"created {path}")

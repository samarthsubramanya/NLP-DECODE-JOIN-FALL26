"""Download the shared DuckDB file to data/decode_join.duckdb."""
from pathlib import Path

from huggingface_hub import hf_hub_download

REPO = "SuhxsReddy/decode-join-duckdb"
data = Path(__file__).resolve().parent.parent / "data"
data.mkdir(exist_ok=True)
hf_hub_download(REPO, "decode_join.duckdb", repo_type="dataset", local_dir=data, force_download=True)
print(f"pulled {data / 'decode_join.duckdb'}")

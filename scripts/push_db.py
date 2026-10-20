"""Upload data/decode_join.duckdb to the shared repo, overwriting it.
Pull first, tell the group chat before you push, and only one person pushes at a time."""
from pathlib import Path

from huggingface_hub import HfApi

REPO = "SuhxsReddy/decode-join-duckdb"
f = Path(__file__).resolve().parent.parent / "data" / "decode_join.duckdb"
HfApi().upload_file(path_or_fileobj=str(f), path_in_repo="decode_join.duckdb",
                    repo_id=REPO, repo_type="dataset", commit_message="Update database")
print("pushed")

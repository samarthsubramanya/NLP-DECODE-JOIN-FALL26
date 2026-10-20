"""Propose your data/decode_join.duckdb as a pull request on the shared HF repo.

Only the repo owner can write to the master copy directly, so everyone else opens a pull
request and the owner merges it (repo -> Community tab). The owner can pass --direct to
overwrite the master copy without a pull request.

    python scripts/push_db.py "what I changed"
    python scripts/push_db.py --direct "what I changed"
"""
import argparse
from pathlib import Path

from huggingface_hub import HfApi

REPO = "SuhxsReddy/decode-join-duckdb"
f = Path(__file__).resolve().parent.parent / "data" / "decode_join.duckdb"

ap = argparse.ArgumentParser()
ap.add_argument("message", nargs="?", default="Update database")
ap.add_argument("--direct", action="store_true", help="owner only: overwrite without a pull request")
a = ap.parse_args()

info = HfApi().upload_file(path_or_fileobj=str(f), path_in_repo="decode_join.duckdb",
                           repo_id=REPO, repo_type="dataset", commit_message=a.message,
                           create_pr=not a.direct)
print("pushed" if a.direct else f"pull request opened: {info.pr_url}")

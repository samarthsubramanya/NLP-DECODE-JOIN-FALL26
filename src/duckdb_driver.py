import csv
import duckdb
from huggingface_hub import hf_hub_download

REPO = "SuhxsReddy/decode-join-duckdb"

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
print("="*100)
bird_con = open_db("decode_join_bird_dev.duckdb")            # file name as it appears in the repo
bird_schema = schema_for("data/bird/schema_mapping.csv", "financial")
bird_df = bird_con.execute(f'SELECT * FROM "{bird_schema}"."district" LIMIT 5').df()
print(f"Bird schema: {bird_schema}")
print(bird_df)
print("="*100)
spider_bvr_con = open_db("decode_join.duckdb")            # file name as it appears in the repo
spider_schema = schema_for("data/spider/schema_mapping.csv", "aircraft")
spider_df = spider_bvr_con.execute(f'SELECT * FROM "{spider_schema}"."aircraft" LIMIT 5').df()
print(f"Spider BVR schema: {spider_schema}")
print(spider_df)
print("="*100)


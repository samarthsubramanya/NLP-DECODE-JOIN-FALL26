"""End-to-end self-check on a tiny synthetic database, no BIRD and no API key needed.

    .venv/bin/python -m poc.tests.smoke

Builds a documented DuckDB (named columns + PK/FK constraints) and an obfuscated
copy (c1, c2, ... and no constraints), runs every stage on the obfuscated one with a
fake LLM, and checks the evaluator recovers the declared joins through the mapping.
"""

import json
import random
import tempfile
from pathlib import Path

import duckdb

from poc import pipeline
from poc.config import load
from poc.evaluate.run import evaluate

SCHEMA = {
    "cities": [("code", "VARCHAR PRIMARY KEY"), ("label", "VARCHAR")],
    "customers": [("customer_id", "INTEGER PRIMARY KEY"), ("full_name", "VARCHAR"), ("city_code", "VARCHAR REFERENCES cities(code)")],
    "orders": [("order_id", "INTEGER PRIMARY KEY"), ("customer_id", "INTEGER REFERENCES customers(customer_id)"),
               ("amount", "DOUBLE"), ("ordered_on", "DATE"), ("quantity", "INTEGER")],
}


def rows():
    rnd = random.Random(7)
    cities = [("NYC", "New York"), ("SF", "San Francisco"), ("LA", "Los Angeles"), ("CHI", "Chicago"), ("BOS", "Boston")]
    customers = [(i, f"Customer {i}", rnd.choice(cities)[0]) for i in range(1, 51)]
    orders = [(i, rnd.randint(1, 50), round(rnd.uniform(5, 500), 2), f"2024-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}", rnd.randint(1, 5))
              for i in range(1, 201)]
    return {"cities": cities, "customers": customers, "orders": orders}


def build(root):
    data = rows()
    mapping = {}
    for setting in ("documented", "obfuscated"):
        path = root / setting / "shop.duckdb"
        path.parent.mkdir(parents=True)
        con = duckdb.connect(str(path))
        for table in ("cities", "customers", "orders"):
            cols = SCHEMA[table]
            if setting == "documented":
                ddl = ", ".join(f"{c} {t}" for c, t in cols)
            else:
                ddl = ", ".join(f"c{i} {t.split()[0]}" for i, (_, t) in enumerate(cols, 1))
                mapping |= {f"shop.{table}.{c}": f"shop.{table}.c{i}" for i, (c, _) in enumerate(cols, 1)}
            con.execute(f"CREATE TABLE {table} ({ddl})")
            con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})", data[table])
        con.close()
    (root / "mapping.json").write_text(json.dumps(mapping))


def fake_llm(stage, payload):
    p = json.loads(payload)
    if stage == "adjudicate":
        return {"decision": "accept" if p["containment"] >= 0.99 and p["source"]["profile"]["distinct"] > 5 else "reject", "reason": "fake"}
    return {"table": {"name": p["table"], "purpose": "test"},
            "columns": {c: {"role": "category", "name": c, "description": "test column", "status": "resolved"} for c in p["columns"]}}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root)
        overrides = {
            "data.db_path": str(root / "{setting}" / "{db}.duckdb"),
            "output.dir": str(root / "out"),
            "llm.provider": "fake",
            "evaluate.keys_gold": "duckdb",
            "evaluate.mapping": str(root / "mapping.json"),
            "evaluate.reference_descriptions": str(root / "none"),
            "evaluate.description_encoder": "",
        }
        cfg = load(overrides=overrides)
        log = lambda *a: None
        pipeline.run(cfg, "obfuscated", "smoke", pipeline.STAGES, responder=fake_llm, log=log)
        report = evaluate(cfg, "obfuscated", "smoke", retrieval=False, log=log)

        out = root / "out" / "bird" / "obfuscated" / "smoke"
        joins = json.loads((out / "shop" / "joins.json").read_text())
        keys = json.loads((out / "shop" / "keys.json").read_text())

        assert not keys["use_names"], "c1, c2, ... must be detected as opaque names"
        assert report["pk"]["recall"] == 1.0, report["pk"]
        assert report["fk"]["stats"]["recall"] == 1.0, report["fk"]
        assert report["fk"]["llm"]["recall"] == 1.0, report["fk"]
        assert report["description_status"]["status"] == {"resolved": 1.0}
        assert (out / "graphs" / "llm+desc" / "nodes.json").exists()

        decoy = [r for r in joins["candidates"] if r["source"] == {"table": "orders", "column": "c5"}]
        print("pk", {k: report["pk"][k] for k in ("precision", "recall")})
        print("fk (proposal weights)", {g: (round(m["precision"], 2), round(m["recall"], 2)) for g, m in report["fk"].items()})
        print("decoy orders.quantity ->", [(r["target"], r["decision"], round(r.get("score", 0))) for r in decoy])

        # Same data with the optional coverage gate on and PK sources off.
        cov = load(overrides={**overrides, "joins.coverage_min": 0.5, "joins.key_sources": "never"})
        pipeline.run(cov, "obfuscated", "coverage", ["profile", "keys", "joins"], log=log)
        fk = evaluate(cov, "obfuscated", "coverage", metrics=["fk"], retrieval=False, log=log)["fk"]["stats"]
        assert fk["recall"] == 1.0 and fk["precision"] == 1.0, fk
        print("fk (coverage gate 0.5, no key sources)", (fk["precision"], fk["recall"]))
        print("smoke ok")


if __name__ == "__main__":
    main()

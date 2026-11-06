"""
Keep only the text-to-SQL questions whose gold SQL reads from 2+ distinct tables.

Works for any dataset: you say which fields hold the SQL / database id / question,
which SQL dialect it uses, and where the questions and schemas come from. Table names
are taken from the parsed SQL (sqlglot), CTE names are dropped, and what is left is
matched case-insensitively against the real tables of that database in DuckDB.
A self-join counts as one table.

Usage (presets fill in the defaults for our five datasets; any flag overrides them):
    python src/filter_for_multitable_datasets.py --preset bird   --out out/filtered/bird_dev
    python src/filter_for_multitable_datasets.py --preset bird_stripped   --out out/filtered/bird_stripped
    python src/filter_for_multitable_datasets.py --preset bird_obfuscated --out out/filtered/bird_obfuscated
    python src/filter_for_multitable_datasets.py --preset spider --out out/filtered/spider_dev
    python src/filter_for_multitable_datasets.py --preset beaver --out out/filtered/beaver
    python src/filter_for_multitable_datasets.py --preset kaggledbqa \
        --questions KaggleDBQA/examples/*_test.json --out out/filtered/kaggledbqa
    python src/filter_for_multitable_datasets.py --preset fiben  --out out/filtered/fiben

    # any other dataset, no preset
    python src/filter_for_multitable_datasets.py --questions my.jsonl \
        --sql-field gold --db-field db --question-field nl --dialect postgres \
        --db-file my.duckdb --mapping my_mapping.csv --out out/filtered/my

--questions accepts (one or more):
    path/to/file.json | .jsonl | .parquet | .csv    a local file
    duckdb:<schema>.<table>                          a table inside --db-file (BEAVER)
    stored:<metadata_schema>/<source_path>           a JSON file kept in <metadata_schema>.files
                                                     by the importers (BIRD / Spider dev.json)

--schema-source (where the list of real tables per database comes from):
    duckdb         (default) information_schema of the mapped schema in --db-file
    <tables.json>  a Spider/BIRD-style tables.json (db_id, table_names_original)
    none           no check, every table name in the SQL counts

--db-file is a file name in the HF repo (opened through duckdb_driver.open_db) or a local path.
--questions-db-file reads duckdb:/stored: questions from another file (the stripped and
obfuscated BIRD files have no bird_metadata, so their presets read dev.json from the documented one).
--mapping is the dataset's schema_mapping.csv; both formats are understood
(source_path,duckdb_schema for Spider/KaggleDBQA/FIBEN, source_database,target_schema
for BIRD/BEAVER). Without a mapping the database id is used as the schema name.

Outputs, in --out:
    questions.jsonl   kept questions, one per line (input for Stages 1-4 and evaluation)
    databases.json    one entry per database that has kept questions: DuckDB file and schema,
                      all its tables, and the union of gold tables (the Stage 1 work list)
    rejected.jsonl    dropped questions with the reason
    summary.json      counts, settings and parser-vs-annotation agreement
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from duckdb_driver import open_db, schema_for

ROOT = Path(__file__).resolve().parent.parent

PRESETS = {
    "bird": dict(
        db_file="decode_join_bird_dev.duckdb", mapping="data/bird/schema_mapping.csv",
        questions=["stored:bird_metadata/dev.json"], sql_field="SQL", db_field="db_id",
        question_field="question", id_field="question_id", dialect="sqlite",
    ),
    # the same data without bird_metadata or declared keys; dev.json is read from the documented file
    "bird_stripped": dict(
        db_file="decode_join_bird_dev_stripped.duckdb", questions_db_file="decode_join_bird_dev.duckdb",
        mapping="data/bird/schema_mapping.csv",
        questions=["stored:bird_metadata/dev.json"], sql_field="SQL", db_field="db_id",
        question_field="question", id_field="question_id", dialect="sqlite",
    ),
    # stripped, and columns renamed col_NNN by position (table names kept, so the SQL still resolves)
    "bird_obfuscated": dict(
        db_file="bird_dev_obfuscated.duckdb", questions_db_file="decode_join_bird_dev.duckdb",
        mapping="data/bird/schema_mapping.csv",
        questions=["stored:bird_metadata/dev.json"], sql_field="SQL", db_field="db_id",
        question_field="question", id_field="question_id", dialect="sqlite",
    ),
    "spider": dict(
        db_file="decode_join.duckdb", mapping="data/spider/schema_mapping.csv",
        questions=["stored:spider_metadata/dev.json"], sql_field="query", db_field="db_id",
        question_field="question", dialect="sqlite",
    ),
    "beaver": dict(
        db_file="decode_join.duckdb", mapping="data/beaver/schema_mapping.csv",
        questions=[f"duckdb:beaver_metadata.queries_{s}" for s in ("dw", "dw_real", "nova", "neutron")],
        sql_field="sql", db_field="db", question_field="question", id_field="id",
        gold_tables_field="tables", dialect="mysql",
    ),
    # questions are not stored in the DuckDB file: pass --questions KaggleDBQA/examples/*.json
    "kaggledbqa": dict(
        db_file="decode_join_kaggledbqa.duckdb", mapping="data/kaggledbqa/schema_mapping.csv",
        sql_field="query", db_field="db_id", question_field="question", dialect="sqlite",
    ),
    # single database, so no db field; SQL is DB2-style, parsed with the generic dialect
    "fiben": dict(
        db_file="decode_join_fiben.duckdb", mapping="data/fiben/schema_mapping.csv",
        questions=["data/fiben/FIBEN_Queries.json"], sql_field="SQL",
        question_field="question", id_field="uniqueQueryID", default_db="FIBEN", dialect="",
    ),
}

# stripped and obfuscated variants: same questions, other DuckDB file. Stripped drops declared keys;
# obfuscated also renames columns col_NNN by position (table names kept, so the SQL still resolves).
# Spider and BEAVER share one file per variant; questions stay in the documented file's *_metadata.
for name, stripped, obfuscated in (
    ("spider", "decode_join_stripped.duckdb", "decode_join_obfuscated.duckdb"),
    ("beaver", "decode_join_stripped.duckdb", "decode_join_obfuscated.duckdb"),
    ("kaggledbqa", "decode_join_kaggledbqa_stripped.duckdb",
     "kaggledbqa/obfuscated/decode_join_kaggledbqa_obfuscated.duckdb"),
    ("fiben", "decode_join_fiben_stripped.duckdb", "fiben/obfuscated/decode_join_fiben_obfuscated.duckdb"),
):
    for setting, file in (("stripped", stripped), ("obfuscated", obfuscated)):
        PRESETS[f"{name}_{setting}"] = dict(PRESETS[name], db_file=file, questions_db_file=PRESETS[name]["db_file"])

DEFAULTS = dict(
    db_file=None, questions_db_file=None, mapping=None, questions=None, sql_field="sql", db_field=None,
    question_field="question", id_field=None, gold_tables_field=None, dialect="",
    default_db=None, schema_source="duckdb", min_tables=2,
)


# ------------------------------------------------------------------ inputs
def connect(db_file):
    if Path(db_file).is_file():
        return duckdb.connect(db_file, read_only=True)
    return open_db(db_file)


def load_questions(source, con):
    if source.startswith("duckdb:"):
        cur = con.execute(f"SELECT * FROM {source[len('duckdb:'):]}")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    if source.startswith("stored:"):
        meta, path = source[len("stored:"):].split("/", 1)
        row = con.execute(
            f'SELECT original_bytes FROM "{meta}".files WHERE source_path = ?', [path]
        ).fetchone()
        if row is None or row[0] is None:
            raise FileNotFoundError(f"{path} is not stored in {meta}.files")
        return as_records(json.loads(bytes(row[0]).decode("utf-8")), source)
    path = Path(source)
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    if path.suffix == ".json":
        return as_records(json.loads(path.read_text(encoding="utf-8")), source)
    if path.suffix == ".parquet":
        return duckdb.read_parquet(str(path)).df().to_dict("records")
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    raise ValueError(f"Unsupported questions source: {source}")


def as_records(data, source):
    # a JSON list, or a dict holding one list (e.g. {"questions": [...]})
    if isinstance(data, list):
        return data
    lists = [v for v in data.values() if isinstance(v, list)] if isinstance(data, dict) else []
    if len(lists) == 1:
        return lists[0]
    raise ValueError(f"{source}: expected a JSON list of questions")


# ----------------------------------------------------------------- schemas
class Schemas:
    """db id -> (duckdb schema, {lowercased table name: real table name})"""

    def __init__(self, source, con, mapping):
        self.source, self.con = source, con
        self.direct = {}   # BIRD/BEAVER mapping: source_database -> target_schema
        self.mapping = mapping
        if mapping:
            with open(mapping, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if "source_database" in reader.fieldnames:
                    self.direct = {r["source_database"]: r["target_schema"] for r in reader}
                    self.mapping = None
        self.tables_json = {}
        if source not in ("duckdb", "none"):
            for db in json.loads(Path(source).read_text(encoding="utf-8")):
                self.tables_json[db["db_id"]] = db["table_names_original"]

    @lru_cache(maxsize=None)
    def schema(self, db_id):
        if db_id in self.direct:
            return self.direct[db_id]
        if self.mapping:
            return schema_for(self.mapping, db_id)
        return db_id

    @lru_cache(maxsize=None)
    def tables(self, db_id):
        if self.source == "none":
            return None
        if self.source == "duckdb":
            names = [r[0] for r in self.con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
                [self.schema(db_id)],
            ).fetchall()]
        else:
            names = self.tables_json.get(db_id, [])
        if not names:
            raise KeyError(db_id)
        return {n.lower(): n for n in names}


# ------------------------------------------------------------------- parse
def tables_in_sql(sql, dialect):
    """Distinct table names read by the SQL, CTE names removed, original spelling."""
    last_error = None
    for read in dict.fromkeys([dialect or None, None]):   # declared dialect, then generic
        try:
            trees = [t for t in sqlglot.parse(sql, read=read) if t is not None]
            break
        except sqlglot.errors.SqlglotError as e:
            last_error = e
    else:
        raise last_error
    found = {}
    for tree in trees:
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            name = table.name
            if not name or (not table.db and name.lower() in ctes):
                continue
            found.setdefault(name.lower(), name)
    return found


def annotated_tables(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return {str(v).lower() for v in value}


# -------------------------------------------------------------------- main
def run(a):
    con = connect(a.db_file) if a.db_file else None
    questions_con = connect(a.questions_db_file) if a.questions_db_file else con
    if a.schema_source == "duckdb" and con is None:
        raise SystemExit("--schema-source duckdb needs --db-file")
    mapping = a.mapping
    if mapping and not Path(mapping).is_file():   # preset paths are relative to the repo root
        mapping = str(ROOT / mapping)
    schemas = Schemas(a.schema_source, con, mapping)

    kept, rejected = [], []
    reasons = Counter()
    annotation = Counter()
    per_db = defaultdict(lambda: dict(questions=0, gold_tables=set()))

    for source in a.questions:
        for index, row in enumerate(load_questions(source, questions_con)):
            db_id = row.get(a.db_field) if a.db_field else None
            db_id = db_id if db_id is not None else a.default_db
            qid = row.get(a.id_field) if a.id_field else None
            qid = qid if qid is not None else f"{Path(source).name}:{index}"
            sql = row.get(a.sql_field)
            base = dict(uid=f"{a.dataset}:{qid}", dataset=a.dataset, question_id=qid,
                        db_id=db_id, source=source)

            def reject(reason, **extra):
                reasons[reason] += 1
                rejected.append(dict(base, reason=reason, gold_sql=sql, **extra))

            if not sql or db_id is None:
                reject("missing_sql_or_db")
                continue
            try:
                found = tables_in_sql(sql, a.dialect)
            except sqlglot.errors.SqlglotError as e:
                reject("parse_error", error=str(e).splitlines()[0])
                continue
            try:
                real = schemas.tables(db_id)
            except KeyError:
                reject("unknown_db")
                continue

            if real is None:
                gold, unresolved = sorted(found.values()), []
            else:
                gold = sorted(real[k] for k in found if k in real)
                unresolved = sorted(v for k, v in found.items() if k not in real)

            if a.gold_tables_field:
                expected = annotated_tables(row.get(a.gold_tables_field))
                if expected is not None:
                    annotation["agree" if expected == {g.lower() for g in gold} else "disagree"] += 1

            if len(gold) < a.min_tables:
                reject("single_table" if len(gold) == 1 else "no_tables",
                       gold_tables=gold, unresolved_tables=unresolved)
                continue

            extra = {k: v for k, v in row.items()
                     if k not in {a.sql_field, a.db_field, a.question_field, a.id_field}}
            kept.append(dict(
                base,
                duckdb_file=a.db_file,
                duckdb_schema=schemas.schema(db_id) if a.schema_source == "duckdb" else None,
                question=row.get(a.question_field),
                gold_sql=sql,
                dialect=a.dialect or None,
                gold_tables=gold,
                num_gold_tables=len(gold),
                unresolved_tables=unresolved,
                extra=extra,
            ))
            per_db[db_id]["questions"] += 1
            per_db[db_id]["gold_tables"].update(gold)

    databases = []
    for db_id, info in sorted(per_db.items(), key=lambda kv: str(kv[0])):
        real = schemas.tables(db_id)
        databases.append(dict(
            dataset=a.dataset, db_id=db_id, duckdb_file=a.db_file,
            duckdb_schema=schemas.schema(db_id) if a.schema_source == "duckdb" else None,
            tables=sorted(real.values()) if real else None,
            gold_tables=sorted(info["gold_tables"]),
            num_questions=info["questions"],
        ))

    total = len(kept) + len(rejected)
    summary = dict(
        dataset=a.dataset, settings={k: v for k, v in vars(a).items() if k != "preset"},
        total=total, kept=len(kept), rejected=dict(reasons),
        databases=len(databases),
        gold_table_count_histogram=dict(sorted(Counter(q["num_gold_tables"] for q in kept).items())),
        questions_with_unresolved_tables=sum(bool(q["unresolved_tables"]) for q in kept),
        annotation_check=dict(annotation) or None,
    )

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "questions.jsonl", kept)
    write_jsonl(out / "rejected.jsonl", rejected)
    (out / "databases.json").write_text(json.dumps(databases, indent=2, default=str))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"{a.dataset}: kept {len(kept)} / {total} questions "
          f"across {len(databases)} databases -> {out}")
    print(f"  rejected: {dict(reasons)}")
    if annotation:
        print(f"  parser vs '{a.gold_tables_field}' annotation: {dict(annotation)}")
    return summary


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--preset", choices=sorted(PRESETS))
    p.add_argument("--dataset", help="name used in uids and outputs (default: the preset)")
    p.add_argument("--questions", nargs="+")
    p.add_argument("--sql-field")
    p.add_argument("--db-field")
    p.add_argument("--question-field")
    p.add_argument("--id-field")
    p.add_argument("--gold-tables-field", help="optional annotated table list to cross-check the parser")
    p.add_argument("--default-db", help="database id for rows without --db-field (single-database datasets)")
    p.add_argument("--dialect", help="sqlglot dialect of the gold SQL: sqlite, mysql, postgres, ...")
    p.add_argument("--db-file")
    p.add_argument("--questions-db-file", help="DuckDB file for duckdb:/stored: questions (default: --db-file)")
    p.add_argument("--mapping")
    p.add_argument("--schema-source")
    p.add_argument("--min-tables", type=int)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    settings = dict(DEFAULTS, **PRESETS.get(a.preset, {}))
    for key, value in vars(a).items():
        if value is not None:
            settings[key] = value
    settings["dataset"] = settings.get("dataset") or a.preset or "custom"
    if not settings["questions"]:
        p.error("--questions is required for this dataset")
    run(argparse.Namespace(**settings))


if __name__ == "__main__":
    main()

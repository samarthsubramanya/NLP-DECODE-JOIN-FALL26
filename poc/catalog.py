"""The only code that touches a database. Everything downstream sees tables,
columns, types and rows, never which dataset or setting they came from."""

import re
from pathlib import Path

import duckdb

from poc.config import resolve


def q(name):
    return '"' + name.replace('"', '""') + '"'


class Catalog:
    def __init__(self, path, db_id=None):
        self.path = Path(path)
        self.db_id = db_id or self.path.stem
        self.con = duckdb.connect(str(self.path), read_only=True)

    def tables(self):
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name"
        ).fetchall()
        return [r[0] for r in rows]

    def columns(self, table):
        rows = self.con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        return [{"name": n, "type": t, "position": i} for i, (n, t) in enumerate(rows, 1)]

    def row_count(self, table):
        return self.con.execute(f"SELECT count(*) FROM {q(table)}").fetchone()[0]

    def scalar(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchone()

    def rows(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchall()

    def sample_rows(self, table, n, seed):
        cur = self.con.execute(f"SELECT * FROM {q(table)} USING SAMPLE reservoir({int(n)} ROWS) REPEATABLE ({int(seed)})")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, map(_plain, r))) for r in cur.fetchall()]

    def declared_keys(self):
        """PK/FK constraints kept in the database. Evaluation only; the pipeline never calls this."""
        pks, fks = [], []
        for table, ctype, cols, ref_table, ref_cols in self.rows(
            "SELECT table_name, constraint_type, constraint_column_names, referenced_table, referenced_column_names "
            "FROM duckdb_constraints() WHERE constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')"
        ):
            if ctype == "PRIMARY KEY":
                pks.extend((table, c) for c in cols)
            else:
                fks.extend(((table, a), (ref_table, b)) for a, b in zip(cols, ref_cols))
        return pks, fks

    def close(self):
        self.con.close()


def _plain(v):
    return v if v is None or isinstance(v, (int, float, str, bool)) else str(v)


def find_databases(cfg, setting):
    """{db_id: path} for every database file the configured pattern matches."""
    pattern = cfg["data"]["db_path"]
    wanted = set(cfg["data"]["databases"])
    probe = resolve(cfg, pattern, setting=setting, db="*")
    found = {}
    for path in sorted(probe.parent.glob(probe.name)):
        db = re.sub(r"\.duckdb$", "", path.name)
        if not wanted or db in wanted:
            found[db] = path
    return found


def names_carry_meaning(columns, cfg):
    mode = cfg["names"]["use"]
    if mode != "auto":
        return mode == "always"
    if not columns:
        return False
    pattern = re.compile(cfg["names"]["opaque_pattern"])
    opaque = sum(bool(pattern.match(c)) for c in columns)
    return opaque / len(columns) < cfg["names"]["opaque_share"]

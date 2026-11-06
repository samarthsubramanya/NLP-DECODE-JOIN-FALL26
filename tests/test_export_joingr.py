import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import export_joingr as s4  # noqa: E402

E = {"source": {"table": "orders", "column": "c2"}, "target": {"table": "customers", "column": "c1"},
     "cardinality": "many_to_one", "decision": "accepted"}


def desc():
    col = lambda name, d, s: {"role": "", "name": name, "description": d, "status": s}
    return {"tables": {
        "customers": {"table": {"name": "customers", "purpose": "people who buy"},
                      "columns": {"c1": col("customer_id", "customer key", "resolved")}},
        "orders": {"table": {"name": "orders", "purpose": "purchases"},
                   "columns": {"c1": col("order_id", "order key", "resolved"), "c2": col("", "", "unresolved")}}}}


class FakeLLM:
    def __init__(self):
        self.sent = []

    def complete(self, step, tag, system, user, schema):
        self.sent.append((tag, schema["schema"]["required"]))
        return {c: {"name": "buyer", "description": "the customer who placed the order", "status": "resolved"}
                for c in schema["schema"]["required"]}, "low"


def test_refine_only_touches_unresolved_columns_of_joined_tables():
    llm = FakeLLM()
    out, log, n = s4.refine(llm, desc(), [E], {"workers": 1})
    assert n == 1 and llm.sent == [("orders", ["c2"])]          # customers is all resolved: not sent
    assert out["tables"]["orders"]["columns"]["c2"]["status"] == "resolved"
    assert out["tables"]["orders"]["columns"]["c1"] == desc()["tables"]["orders"]["columns"]["c1"]
    assert [r["column"] for r in log] == ["c2"]


def test_refine_never_runs_without_joins():
    llm = FakeLLM()
    out, log, n = s4.refine(llm, desc(), [], {"workers": 1})
    assert n == 0 and not llm.sent and out == desc()


def test_edge_purpose_uses_both_ends():
    text = s4.edge_purpose(E, desc())
    assert "orders" in text and "customer_id (customer key)" in text and "many-to-one" in text


class Fake:
    """Answers batch calls from `tables` and logs every call."""
    def __init__(self, drop=()):
        self.calls, self.drop = [], set(drop)

    def complete(self, step, tag, system, user, schema):
        self.calls.append(tag)
        props = schema["schema"]["properties"]
        col = {"name": "n", "description": "d", "status": "resolved", "role": "key"}
        if "tables" in props:
            names = [t for t in props["tables"]["required"] if t not in self.drop]
            if step == "describe":
                return {"tables": {t: {"table": {"name": t, "purpose": "p"}, "columns": {"a": col}} for t in names}}, "low"
            return {"tables": {t: {"a": col} for t in names}}, "low"
        if "columns" in props:
            return {"table": {"name": "alone", "purpose": "p"}, "columns": {"a": col}}, "low"
        return {"a": col}, "low"


def test_pack_keeps_order_and_budget():
    from describe_columns import pack
    assert pack([3, 3, 3, 9, 1], lambda x: x, 6) == [[3, 3], [3], [9], [1]]
    assert pack([1, 2], lambda x: x, 0) == [[1], [2]]


def test_refine_batch_asks_again_for_a_table_left_out():
    desc = {"tables": {t: {"table": {"purpose": ""}, "columns": {"a": {"role": "", "name": "", "description": "", "status": "tentative"}}}
                       for t in ("x", "y")}}
    llm = Fake(drop=["y"])
    out = s4.refine_batch(llm, ["x", "y"], desc, {}, {})
    assert llm.calls == ["batch of 2 tables", "y"] and out["x"][1]["a"]["name"] == "n" and out["y"][1]["a"]["name"] == "n"


def test_describe_batch_asks_again_for_a_table_left_out():
    import describe_columns as s3
    cfg = {**s3.DEFAULTS}
    prof = {"name": "a", "declared_type": "INT", "family": "int", "role": "int", "null_rate": 0, "distinct": 1, "uniqueness": 1,
            "top_values": [], "sample_values": []}
    profile = {"tables": {t: {"rows": 1, "columns": {"a": prof}, "sample_rows": []} for t in ("x", "y")}}
    keys = {"tables": {t: {"primary_key": None} for t in ("x", "y")}}
    llm = Fake(drop=["y"])
    out = s3.describe_batch(llm, profile, keys, {}, ["x", "y"], cfg, True)
    assert llm.calls == ["batch of 2 tables", "y"]
    assert out["x"]["table"]["name"] == "x" and out["y"]["table"]["name"] == "alone" and out["x"]["ok"] and out["y"]["ok"]

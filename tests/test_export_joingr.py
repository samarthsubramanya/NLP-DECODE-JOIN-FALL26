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

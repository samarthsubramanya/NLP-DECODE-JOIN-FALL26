"""Stage 2d: the frozen LLM judges only the join records in the delta band.

It may accept, reject or abstain; it cannot propose a join. Rejected and
abstained records stay in joins.json with the model's reason but are never exported.
"""

import json

from poc.config import POC
from poc.evidence import column_summary, sample_rows
from poc.joins import assemble

SYSTEM = (POC / "prompts" / "adjudicate.md").read_text()


def payload(rec, profiles, keys, cfg):
    use_names, n = keys["use_names"], cfg["adjudicate"]["sample_rows"]
    side = {}
    for role in ("source", "target"):
        t, c = rec[role]["table"], rec[role]["column"]
        tp = profiles["tables"][t]
        side[role] = {
            "table": t,
            "column": c,
            "profile": column_summary(tp["columns"][c], use_names),
            "other_columns": [x for x in tp["columns"] if x != c],
            "sample_rows": sample_rows(tp, n),
        }
    target_key = next(k for k in keys["tables"][rec["target"]["table"]]["candidate_keys"] if k["column"] == rec["target"]["column"])
    return {
        **side,
        "target_key_score": round(target_key["score"], 3),
        "join_score": round(rec["score"], 1),
        "join_score_parts": {k: round(v, 3) for k, v in rec["parts"].items()},
        "acceptance_cutoff": cfg["joins"]["accept"],
        "containment": round(rec["containment"], 3),
        "cardinality": rec["cardinality"],
    }


def run(joins, profiles, keys, cfg, llm, log=print):
    # Records judged in an earlier run are judged again, so a prompt or model change takes effect.
    band = [r for r in joins["candidates"] if r["decision"] == "band" or r["decision"].startswith("llm_")]
    for rec in band:
        reply = llm.complete_json("adjudicate", SYSTEM, json.dumps(payload(rec, profiles, keys, cfg), default=str)) or {}
        verdict = reply.get("decision") if reply.get("decision") in ("accept", "reject", "abstain") else "abstain"
        rec["llm"] = {"decision": verdict, "reason": reply.get("reason", "unparsable reply")}
        rec["decision"] = f"llm_{verdict}"
    joins["graphs"]["llm"] = assemble(joins["candidates"], {"accepted", "llm_accept"}, cfg)
    counts = {d: sum(r["decision"] == f"llm_{d}" for r in band) for d in ("accept", "reject", "abstain")}
    log(f"  adjudicate: {len(band)} band records -> {counts}, {len(joins['graphs']['llm'])} edges (stats+llm)")
    return joins

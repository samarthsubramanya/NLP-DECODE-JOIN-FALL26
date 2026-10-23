"""Evaluates one pipeline run.

    python -m poc.evaluate.run --setting obfuscated --run full
    python -m poc.evaluate.run --run full --metrics fk pk --no-retrieval

Writes results/poc/<dataset>/<setting>/<run>.json (everything) and <run>.md (summary).
"""

import argparse
import json

from poc.config import DEFAULT, load, parse_override, run_dir, POC
from poc.evaluate.metrics import METRICS, Context

RESULTS = POC.parent / "results" / "poc"
FILES = ("profiles", "keys", "joins", "descriptions")


def load_run(path):
    dbs = {}
    for d in sorted(p for p in path.iterdir() if p.is_dir() and (p / "joins.json").exists()):
        dbs[d.name] = {f: json.loads((d / f"{f}.json").read_text()) if (d / f"{f}.json").exists() else None for f in FILES}
    return dbs


def evaluate(cfg, setting, run_name, metrics=None, retrieval=None, log=print):
    path = run_dir(cfg, setting, run_name)
    dbs = load_run(path)
    if not dbs:
        raise SystemExit(f"no pipeline output under {path}")
    ctx = Context(cfg, setting, path, dbs)

    report = {"dataset": cfg["data"]["dataset"], "setting": setting, "run": run_name, "databases": sorted(dbs)}
    for name in metrics or cfg["evaluate"]["metrics"]:
        report[name] = METRICS[name](ctx)

    if retrieval if retrieval is not None else cfg["evaluate"]["retrieval"]["enabled"]:
        from poc.evaluate import retrieval as ret
        report["retrieval"] = ret.run(ctx, cfg["export"]["variants"], log)

    out = RESULTS / report["dataset"] / setting
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{run_name}.json").write_text(json.dumps(report, indent=1, default=str))
    md = render(report)
    (out / f"{run_name}.md").write_text(md)
    log(md)
    return report


def _f(x):
    return f"{x:.3f}" if isinstance(x, float) else str(x)


def render(r):
    lines = [f"# PoC {r['dataset']} / {r['setting']} / {r['run']}", "", f"Databases: {', '.join(r['databases'])}", ""]
    if "pk" in r:
        p = r["pk"]
        lines += [f"**Primary keys:** P {_f(p['precision'])} R {_f(p['recall'])} F1 {_f(p['f1'])} "
                  f"({p['tp']}/{p['gold']} gold, {p['composite_pks_skipped']} composite skipped)", ""]
    if r.get("fk"):
        lines += ["**Foreign keys vs declared** (undirected)", "", "| graph | P | R | F1 | edges | gold | direction right |", "|---|---|---|---|---|---|---|"]
        for g, m in r["fk"].items():
            lines.append(f"| {g} | {_f(m['precision'])} | {_f(m['recall'])} | {_f(m['f1'])} | {m['pred']} | {m['gold']} | {m['direction_correct']} |")
        lines.append("")
    if r.get("fk_by_decision"):
        lines += ["**Candidates by decision**: " + ", ".join(f"{d} {v['in_gold']}/{v['n']} gold" for d, v in r["fk_by_decision"].items()), ""]
    if r.get("llm_usage"):
        lines += [f"**LLM:** {r['llm_usage']['calls']}, unparsable {r['llm_usage']['unparsable']}, tokens {r['llm_usage']['api_tokens']}", ""]
    if r.get("description_status", {}).get("columns"):
        lines += ["**Descriptions:** " + ", ".join(f"{k} {_f(v)}" for k, v in r["description_status"]["status"].items()), ""]
    if r.get("description_reference", {}).get("columns_with_reference"):
        d = r["description_reference"]
        lines += [f"**Vs reference docs** ({d['columns_with_reference']} columns): token F1 {_f(d['token_f1'])}"
                  + (f", cosine {_f(d['cosine'])}" if "cosine" in d else ""), ""]
    if r.get("retrieval"):
        ret = r["retrieval"]
        cols = ["PR@15", "col_R@15", "col_PR@15"]
        lines += ["**Retrieval (JoinGR)**", "", "| graph | encoder/method | " + " | ".join(cols) + " |", "|---|---|" + "---|" * len(cols)]
        for label, rows in [(f"*{s}*", v) for s, v in ret["baselines"].items()] + [(k, v["scores"]) for k, v in ret["variants"].items()]:
            for em, row in rows.items():
                lines.append(f"| {label} | {em} | " + " | ".join(_f(row.get(c, "")) for c in cols) + " |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(DEFAULT))
    p.add_argument("--setting", default=None)
    p.add_argument("--run", default="default")
    p.add_argument("--metrics", nargs="*", default=None, choices=list(METRICS))
    p.add_argument("--no-retrieval", action="store_true")
    p.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE")
    args = p.parse_args()

    cfg = load(args.config, dict(parse_override(s) for s in args.set))
    evaluate(cfg, args.setting or cfg["data"]["setting"], args.run, args.metrics, False if args.no_retrieval else None)

"""Runs stages 1-3 on every database of one setting and writes their outputs.

    python -m poc.pipeline --setting obfuscated --run stats --stages profile keys joins
    python -m poc.pipeline --setting obfuscated --run full
    python -m poc.pipeline --run tau90 --set joins.containment_min=0.9

Outputs land in artifacts/poc/<dataset>/<setting>/<run>/<db>/{profiles,keys,joins,descriptions}.json,
plus llm_calls.jsonl and the config snapshot. A stage that isn't requested is loaded
from disk if an earlier call wrote it, so later stages can be rerun alone.
"""

import argparse
import json
import time

from poc import adjudicate, describe, export, joins, keys, profile
from poc.catalog import Catalog, find_databases
from poc.config import DEFAULT, load, parse_override, run_dir
from poc.llm import LLM

STAGES = ["profile", "keys", "joins", "adjudicate", "describe", "export"]


def _read(path):
    return json.loads(path.read_text()) if path.exists() else None


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def run_db(db, path, cfg, stages, out, llm, log):
    d = out / db
    cat = Catalog(path, db)
    try:
        prof = profile.run(cat, cfg, log) if "profile" in stages else _read(d / "profiles.json")
        if prof is None:
            raise SystemExit(f"{db}: no profiles yet; include the profile stage")
        _write(d / "profiles.json", prof)

        k = keys.run(prof, cfg, log) if "keys" in stages else _read(d / "keys.json")
        j = joins.run(cat, prof, k, cfg, log) if "joins" in stages else _read(d / "joins.json")
        if "adjudicate" in stages and cfg["adjudicate"]["enabled"]:
            j = adjudicate.run(j, prof, k, cfg, llm, log)
        desc = _read(d / "descriptions.json")
        if "describe" in stages and cfg["describe"]["enabled"]:
            desc = describe.run(cat, prof, k, j, cfg, llm, log)

        for name, obj in (("keys", k), ("joins", j), ("descriptions", desc)):
            if obj is not None:
                _write(d / f"{name}.json", obj)
        return {"profiles": prof, "keys": k, "joins": j, "descriptions": desc}
    finally:
        cat.close()


def run(cfg, setting, run_name, stages, responder=None, log=print):
    dbs = find_databases(cfg, setting)
    if not dbs:
        raise SystemExit(f"no databases found for setting '{setting}' with pattern {cfg['data']['db_path']}")
    out = run_dir(cfg, setting, run_name)
    _write(out / "config.json", {k: v for k, v in cfg.items() if not k.startswith("_")} | {"stages": stages})
    llm = LLM(cfg, out / "llm_calls.jsonl", responder)

    results = {}
    for db, path in dbs.items():
        start = time.perf_counter()
        log(f"[{db}]")
        results[db] = run_db(db, path, cfg, stages, out, llm, log)
        log(f"  done in {time.perf_counter() - start:.1f}s")

    if "export" in stages:
        for variant in cfg["export"]["variants"]:
            if all(export.available(variant, r["joins"], r["descriptions"]) for r in results.values()):
                g = export.build(results, variant, cfg)
                for name, obj in g.items():
                    _write(out / "graphs" / variant / f"{name}.json", obj)
                log(f"export {variant}: {len(g['nodes'])} nodes, {sum(e['edge_type'] == 'join' for e in g['edges'].values())} joins")
    log(f"llm api calls: {llm.calls} -> {out}")
    return out, results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(DEFAULT))
    p.add_argument("--setting", default=None, help="defaults to data.setting in the config")
    p.add_argument("--run", default="default", help="name of this run's output folder")
    p.add_argument("--stages", nargs="+", default=STAGES, choices=STAGES)
    p.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE", help="config overrides, e.g. joins.accept=55")
    args = p.parse_args()

    cfg = load(args.config, dict(parse_override(s) for s in args.set))
    run(cfg, args.setting or cfg["data"]["setting"], args.run, args.stages)

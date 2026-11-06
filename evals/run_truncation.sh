#!/bin/bash
# Truncation test: cut every table to a share of its rows, then compare joins and the walk with and without the repair.
# Works in a copy under <work_dir>, so out/ here is not touched. Run from quartile/. Needs the LLM (Stage 3, joins only).
#   evals/run_truncation.sh <work_dir> <keep> <seed> [dataset ...]        e.g. evals/run_truncation.sh /tmp/trunc 0.5 0 bird spider
# Results: <work_dir>/k<keep>s<seed>/grades.jsonl and retrieval.jsonl
set -u
root=$(pwd); py=${PYTHON:-python}; base=$1; keep=$2; seed=$3; shift 3
datasets=${*:-bird bird_obfuscated spider spider_obfuscated}
tag=k${keep#0.}s$seed; work=$base/$tag; copy=$work/quartile
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1} TOKENIZERS_PARALLELISM=false DECODE_JOIN_DUCKDB_DIR=$work/db
mkdir -p "$copy/out/filtered"
cp -R src poc evals baselines gold "$copy/"; ln -sf "$root/.env" "$copy/.env"; ln -sfn "$root/artifacts" "$copy/artifacts"
ln -sfn "$(dirname "$root")/JoinGR-E656" "$work/JoinGR-E656"
for d in $datasets; do cp -R "out/filtered/$d" "$copy/out/filtered/"; done
$py evals/truncate_tables.py "$work/db" --keep "$keep" --seed "$seed" --datasets $datasets || exit 1
one() {
  ds=$1; flag=""; [[ $ds == *_obfuscated ]] && flag="--no-names"
  cd "$copy" || exit 1
  $py src/profile_columns.py --dataset "$ds" --force && $py src/primary_keys.py --dataset "$ds" --run ddb $flag \
    && $py src/profile_columns.py --dataset "$ds" --overlaps || return 1
  for arm in base rep; do
    run=${arm}_$tag; repair=""; [ "$arm" = rep ] && repair="--repair-isolated 2"
    $py src/foreign_keys.py --dataset "$ds" --keys-run ddb --run "$run" $repair
    $py src/describe_columns.py --dataset "$ds" --joins-run "$run" --run "$run" --no-describe --reasoning-effort low --workers 8
    $py evals/grade_joins_all.py --dataset "$ds" --run "$run" >> "$work/grades.jsonl"
    $py src/export_joingr.py --dataset "$ds" --stage3-run "$run" --run "$run" --no-descriptions
    $py evals/score_joingr.py pipeline "$ds" "$run" | tail -1 >> "$work/retrieval.jsonl"
  done
}
for d in $datasets; do one "$d" > "$work/$d.log" 2>&1 & done
wait

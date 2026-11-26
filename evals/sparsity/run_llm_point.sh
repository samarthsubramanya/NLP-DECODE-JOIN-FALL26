#!/usr/bin/env bash
# LLM verification on ONE sparse point: sparsify -> Stages 1-2b -> Stage 3 (joins only) -> grade + margins -> export
# -> retrieval. Everything runs in a scratch copy of the repo under <work_dir>, so out/ here is never touched
# (same approach as evals/run_truncation.sh). The LLM reply cache is shared, so a rerun costs nothing.
# Needs VOYAGER_API_KEY in .env and the retrieval setup from the README.
#
#   bash evals/sparsity/run_llm_point.sh /tmp/sparse_llm bird_stripped hard 0.5 [seed]
#
# Results: <work_dir>/<dataset>/<tag>/grades.jsonl, margins.jsonl, retrieval.jsonl
set -euo pipefail
root=$(pwd); py=${PYTHON:-python}
base=${1:?work dir}; ds=${2:?dataset}; mode=${3:?mode}; rate=${4:?rate}; seed=${5:-0}
tag=${mode}_r${rate}_s${seed}; work=$base/$ds/$tag; copy=$work/repo
docbase=${ds%_obfuscated}; docbase=${docbase%_stripped}
names=""; [[ $ds == *_obfuscated ]] && names="--no-names"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1} TOKENIZERS_PARALLELISM=false

$py evals/sparsity/sparsify.py "$work/db" --dataset "$ds" --mode "$mode" --rate "$rate" --seed "$seed"
export DECODE_JOIN_DUCKDB_DIR=$work/db     # only the sparse file is here; other files resolve as usual

mkdir -p "$copy/out/filtered" "$root/out/llm_cache"
cp -R src poc evals baselines data "$copy/"
[ -d gold ] && cp -R gold "$copy/"
[ -f .env ] && ln -sf "$root/.env" "$copy/.env"
ln -sfn "$root/artifacts" "$copy/artifacts"
ln -sfn "$root/out/llm_cache" "$copy/out/llm_cache"
ln -sfn "$(dirname "$root")/JoinGR-E656" "$work/JoinGR-E656"
for d in "$ds" "$docbase"; do cp -R "out/filtered/$d" "$copy/out/filtered/"; done

cd "$copy"
run=sp_$tag
$py src/profile_columns.py --dataset "$ds" --force
$py src/primary_keys.py --dataset "$ds" --run "$run" $names
$py src/foreign_keys.py --dataset "$ds" --keys-run "$run" --run "$run"
$py src/describe_columns.py --dataset "$ds" --joins-run "$run" --run "$run" --no-describe --reasoning-effort low --workers 8
$py evals/grade_joins_all.py --dataset "$ds" --run "$run" | tee -a "$work/grades.jsonl"
$py evals/sparsity/margins.py --dataset "$ds" --run "$run" --stage3 | tee -a "$work/margins.jsonl"
$py src/export_joingr.py --dataset "$ds" --stage3-run "$run" --run "$run" --no-descriptions
$py evals/score_joingr.py pipeline "$ds" "$run" | tail -1 | tee -a "$work/retrieval.jsonl" \
  || echo "retrieval skipped (needs the README's retrieval setup for $docbase)"

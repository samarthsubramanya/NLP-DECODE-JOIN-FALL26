#!/usr/bin/env bash
# Component ablation for ONE dataset variant: A0 FULL, A1 -QuantileMD, A2 -LLM verification,
# A3 -column descriptions, A4 heuristics only. Run from the repository root:
#
#     bash evals/ablation/run_ablation.sh fiben_stripped
#
# Reads the team's finished FULL runs (Stage 2a "ddb", Stage 3 "ddb_low", Stage 4 "ddb_full" / "ddb_joins_only");
# everything it writes uses new "abl_*" run names and out/ablation/<dataset>/. Nothing existing is overwritten
# (variants.py refuses an existing folder). Obfuscated datasets: Stage 2a's no-names keys are inherited.
# A1 is the only step that calls the LLM (Stage 3 + Stage 4); unchanged band records hit the reply cache.
set -euo pipefail
DS=${1:?usage: run_ablation.sh <dataset variant, e.g. fiben_stripped>}
KEYS=${KEYS:-ddb}; S3=${S3:-ddb_low}; FULL=${FULL:-ddb_full}; NODESC=${NODESC:-ddb_joins_only}
OUT=out/ablation/$DS
LLM="--reasoning-effort low --workers 8"
mkdir -p "$OUT"

echo "== A1 -QuantileMD: Stage 2b without the distribution term, then Stage 3 and 4"
python evals/ablation/variants.py no-qmd --dataset "$DS" --keys-run "$KEYS" --run abl_noqmd
python src/describe_columns.py --dataset "$DS" --joins-run abl_noqmd --run abl_noqmd_low $LLM
python src/export_joingr.py --dataset "$DS" --stage3-run abl_noqmd_low --run abl_noqmd_full $LLM

echo "== A2 -LLM verification (graphs.stats) and A4 heuristics only (graphs.direct): exports, no new LLM judging"
python evals/ablation/variants.py swap-graph --dataset "$DS" --from-run "$S3" --graph stats --run abl_nollm
python src/export_joingr.py --dataset "$DS" --stage3-run abl_nollm --run abl_nollm_full $LLM
python evals/ablation/variants.py swap-graph --dataset "$DS" --from-run "$S3" --graph direct --run abl_heur
python src/export_joingr.py --dataset "$DS" --stage3-run abl_heur --run abl_heur_full --no-descriptions $LLM

echo "== Join grading with false positives"
python evals/ablation/grade.py --out "$OUT" \
  A0_full="$DS:$S3:llm" A1_noqmd="$DS:abl_noqmd_low:llm" A2_nollm="$DS:$S3:stats" \
  A3_nodesc="$DS:$S3:llm" A4_heuristics="$DS:$S3:direct"

echo "== Retrieval: A0 and A3 reuse the team's saved scores (not re-scored, so their files are untouched)"
python - "$DS" "$FULL" "$NODESC" > "$OUT/retrieval.jsonl" <<'PY'
import json, sys
ds, *runs = sys.argv[1:]
for line in open("out/eval/ddb/retrieval.jsonl"):
    r = json.loads(line)
    if r.get("dataset") == ds and r.get("run") in runs:
        print(line.strip())
PY
for RUN in abl_noqmd_full abl_nollm_full abl_heur_full; do
  python evals/score_joingr.py pipeline "$DS" "$RUN" | tee -a "$OUT/retrieval.jsonl"
done

echo "== Table"
python evals/ablation/table.py --joins "$OUT/joins.jsonl" --retrieval "$OUT/retrieval.jsonl" \
  --export A0_full="$FULL" A1_noqmd=abl_noqmd_full A2_nollm=abl_nollm_full \
           A3_nodesc="$NODESC" A4_heuristics=abl_heur_full | tee "$OUT/table.md"

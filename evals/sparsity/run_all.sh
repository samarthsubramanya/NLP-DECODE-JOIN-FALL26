#!/usr/bin/env bash
# Every no-LLM experiment of the sparsity / threshold / ablation study, in the order to run them. From the repo root:
#
#   bash evals/sparsity/run_all.sh time      # 1. one database, one point: how long does a point take?
#   bash evals/sparsity/run_all.sh sweep     # 2. all 5 benchmarks x stripped/obfuscated, every mode, all arms
#   bash evals/sparsity/run_all.sh report    # 3. tables -> docs/sparsity_hp_results.md, out/sparsity/summary.csv
#
# Override the grid with env vars, e.g.  DATASETS="bird_stripped fiben_stripped" RATES="0.3 0.6" SEEDS="0 1 2"
# Nothing under out/ that already exists is touched; results go to out/sparsity/. Rerunning skips finished points.
set -euo pipefail
PY=${PYTHON:-python}
DATASETS=${DATASETS:-"bird_stripped bird_obfuscated spider_stripped spider_obfuscated fiben_stripped fiben_obfuscated \
kaggledbqa_stripped kaggledbqa_obfuscated beaver_stripped beaver_obfuscated"}
MODES=${MODES:-"hard row_sample cell_null fk_null pk_null parent_delete"}
RATES=${RATES:-"0.1 0.3 0.5 0.7 0.9"}
SEEDS=${SEEDS:-"0"}
# benchmark files not in the Hugging Face cache yet are downloaded on first use (set HF_HUB_OFFLINE=1 to forbid)

case "${1:-}" in
  test)
    $PY -m pytest evals/sparsity -q ;;
  time)
    db=$($PY -c "import json;print(json.load(open('out/filtered/bird_stripped/databases.json'))[0]['db_id'])")
    /usr/bin/time -p $PY evals/sparsity/sweep.py --datasets bird_stripped --modes hard --rates 0.5 --seeds 0 \
      --databases "$db" --arms full ;;
  sweep)
    mkdir -p out/sparsity
    for ds in $DATASETS; do
      $PY evals/sparsity/sweep.py --datasets "$ds" --modes $MODES --rates $RATES --seeds $SEEDS \
        2>&1 | tee -a "out/sparsity/sweep_$ds.log" || echo "!! $ds failed, see out/sparsity/sweep_$ds.log"
    done ;;
  report)
    $PY evals/sparsity/report.py --datasets $DATASETS > docs/sparsity_hp_results.md
    echo "wrote docs/sparsity_hp_results.md and out/sparsity/summary.csv" ;;
  *)
    sed -n '2,9p' "$0"; exit 1 ;;
esac

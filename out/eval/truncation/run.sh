#!/bin/bash
# usage: run.sh <keep> <seed>
T=/private/tmp/claude-501/-Users-samarthms-NLP/2f10857a-a2f9-4566-bf12-38c3dc574873/scratchpad/rq3/trunc
R=/Users/samarthms/NLP/code/quartile; PY=/Users/samarthms/NLP/code/decode-join/.venv/bin/python
keep=$1; seed=$2; tag=k${keep#0.}s$seed; W=$T/$tag; Q=$W/quartile
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false DECODE_JOIN_DUCKDB_DIR=$W/db
mkdir -p $Q/out/filtered; cp -R $R/src $R/poc $R/evals $Q/; ln -sf $R/.env $Q/.env; ln -sfn /Users/samarthms/NLP/code/decode-join $W/decode-join
for d in bird bird_obfuscated spider spider_obfuscated; do cp -R $R/out/filtered/$d $Q/out/filtered/; done
t0=$SECONDS
(cd $R && $PY evals/truncate_tables.py $W/db --keep $keep --seed $seed --datasets bird bird_obfuscated spider spider_obfuscated) > $W/trunc.log 2>&1 || { echo FAIL trunc; exit 1; }
echo "$tag truncated $((SECONDS-t0))s"
one(){ ds=$1; nn=""; case $ds in *_obfuscated) nn="--no-names";; esac; cd $Q; L=$W/$ds.log
  $PY src/profile_columns.py --dataset $ds --force > $L 2>&1 && $PY src/primary_keys.py --dataset $ds --run ddb $nn >> $L 2>&1 && $PY src/profile_columns.py --dataset $ds --overlaps >> $L 2>&1 || { echo "FAIL $ds"; return; }
  for arm in base rep; do r=${arm}_$tag; x=""; [ $arm = rep ] && x="--repair-isolated 2"
    $PY src/foreign_keys.py --dataset $ds --keys-run ddb --run $r $x >> $L 2>&1
    $PY src/describe_columns.py --dataset $ds --joins-run $r --run $r --no-describe --reasoning-effort low --workers 8 >> $L 2>&1
    $PY evals/grade_joins_all.py --dataset $ds --run $r >> $W/grades.jsonl 2>>$L
    $PY src/export_joingr.py --dataset $ds --stage3-run $r --run $r --no-descriptions >> $L 2>&1
    (cd /Users/samarthms/NLP/code/decode-join && $PY $Q/evals/score_joingr.py pipeline $ds $r 2>>$L | tail -1 >> $W/retrieval.jsonl)
  done; echo "$ds done $((SECONDS-t0))s"; }
for d in bird bird_obfuscated spider spider_obfuscated; do one $d & done; wait; echo ALLDONE $tag

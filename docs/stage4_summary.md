# Stage 4: refinement and JoinGR export — summary and results

`src/export_joingr.py` (tests: `tests/test_export_joingr.py`). Reads Stage 3 (`out/descriptions/<dataset>/<run>/`) and writes
`out/joingr/<dataset>/<run>/<db_id>.json` in JoinGR's graph format, plus `<db_id>.refined.json`, `refine_log.jsonl`, `llm_calls.jsonl`.

## What it does

1. **Refinement, one pass** (proposal, Stage 4). One LLM call per table that has an accepted join and a tentative or unresolved
   column. The model sees the table's descriptions and, for each joined column, the documented column on the other end and that
   table's purpose. It may rewrite only the tentative / unresolved columns. It never sees rejected or abstained joins and cannot add
   or drop one. Every change is logged (`refine_log.jsonl`, before / after).
2. **Export.** Same construction as JoinGR (shared code: `poc/export.py`): a node per column with `semantic_table_name`,
   `semantic_column_name`, `column_purpose` from the descriptions (empty when unresolved); keys = the Stage 2a primary key plus both
   ends of each join; intra edges from each non-key column to its table's keys; join edges = `graphs.llm` only (accepted +
   `llm_accept`). Each join edge gets an `edge_purpose` built from its two ends' descriptions (no LLM).

```bash
python src/export_joingr.py --dataset bird --stage3-run dist_low --run full                      # the full system
python src/export_joingr.py --dataset bird --stage3-run dist_low --run no_refine --no-refine     # ablation: no Stage 4 refinement
python src/export_joingr.py --dataset bird --stage3-run dist_low --run joins_only --no-descriptions
python src/export_joingr.py --dataset bird --stage3-run dist_low --run desc_only --no-joins
```

## Results (2026-11-04): DECODE-JOIN + JoinGR vs JoinGR alone, against the gold set

Gold tables and gold columns come from each question's gold SQL (`gold/<dataset>.json`). BIRD: the 1,171 dev questions whose gold
SQL reads 2+ tables; FIBEN: all 300. bge encoder. "dense" = JoinGR's untrained dense retrieval, "walk" = its untrained graph
traversal. Same harness and question set for every row (JoinGR alone was rescored here). Joins: Stage 2b `dist` (current
defaults) + Stage 3 adjudication; descriptions: Stage 3 `dist_low` (glm-5-3, low reasoning).

**PR@15** (share of questions with every gold table in the top 15)

| | BIRD stripped | BIRD obfuscated | FIBEN stripped |
|---|---|---|---|
| JoinGR alone, documented schema (upper bound) | .957 dense / .963 walk | .957 / .963 | .057 / .827 |
| **JoinGR alone** (best of dense / walk; walk = 0 without keys) | .949 | .899 | .053 |
| + our joins | .954 walk | .945 walk | **.860 walk** |
| + our descriptions, no joins | **.962 dense** | **.963 dense** | .313 dense |
| + joins + descriptions, no refinement | .962 / .951 | .962 / .953 | .320 / .850 |
| **+ joins + descriptions + refinement (full)** | .962 / .950 | .962 / .951 | .317 / .850 |

**Gold columns, R@15** (share of gold columns in the top 15) and **column PR@15** (all gold columns retrieved)

| | BIRD stripped R@15 / PR@15 | BIRD obfuscated | FIBEN stripped |
|---|---|---|---|
| JoinGR alone, documented | .614 / .214 | .614 / .214 | .296 / .007 (walk) |
| **JoinGR alone** | .590 / .184 | .476 / .132 | .212 / .000 |
| + our joins (walk) | .596 / .195 | .494 / .122 | .369 / .020 |
| + our descriptions (dense) | .615 / .231 | .583 / .212 | .307 / .020 |
| **full** (best: BIRD dense, FIBEN walk) | .618 / .231 (walk .615 / .238) | .580 / .211 (walk .583 / .221) | **.490 / .023** |

Table R@5 (bge): BIRD stripped .885 → .914, obfuscated .813 → .912, FIBEN .345 → .664 (documented .458).

### What the numbers say

- **BIRD:** our descriptions bring both undocumented settings back to the documented level (.962–.963 vs .957–.963) and above it
  on gold columns (.618 vs .614). Joins alone recover structure (walk works again) but descriptions are what moves BIRD.
  *Caveat:* BIRD is memorized: on obfuscated BIRD the model restores 68% of column names exactly (stage3_summary §5), so the
  obfuscated gain is partly recall, not reading.
- **FIBEN (clean: no name recovery):** joins are what matters. Without keys JoinGR is unusable (.053); with our joins the walk
  reaches .850–.860, above JoinGR on the documented keys (.827). Descriptions then add column recall: .369 → .490 (documented .296).
- **Joins and descriptions overlap on BIRD:** with descriptions, dense retrieval alone (.962) beats the walk (.950); the walk adds
  little once the node text is good. On FIBEN they are complementary (walk for tables, descriptions for columns).
- **Refinement does almost nothing:** it changed 20 / 53 / 6 columns (BIRD / obfuscated / FIBEN) and moved statuses by at most 2
  columns; retrieval changes are ≤ .007 either way. Most unresolved columns sit in tables whose neighbours are not more informative.
  Kept as specified (one pass, cannot promote joins); not a source of gains.

### Not run

FIBEN obfuscated (no Stage 1–3 outputs yet); Spider, BEAVER, KaggleDBQA; JoinGR's trained scorer (not wired into the harness).

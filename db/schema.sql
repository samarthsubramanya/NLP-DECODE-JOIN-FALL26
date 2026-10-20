-- DECODE-JOIN metadata schema. Raw benchmark tables live in their own
-- schema per database (e.g. bird__financial); everything the pipeline
-- produces lives in `meta`, keyed by (dataset, db_id, setting).

CREATE SCHEMA IF NOT EXISTS meta;

-- Registry of loaded databases. setting: documented | stripped | obfuscated
CREATE TABLE IF NOT EXISTS meta.databases (
    dataset     VARCHAR NOT NULL,   -- bird, spider, beaver, kaggledbqa, fiben
    db_id       VARCHAR NOT NULL,
    duck_schema VARCHAR NOT NULL,   -- schema holding the raw tables
    source_path VARCHAR,
    loaded_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (dataset, db_id)
);

-- Original (documented) schema, kept as the upper reference and for gold labels.
CREATE TABLE IF NOT EXISTS meta.columns (
    dataset     VARCHAR NOT NULL,
    db_id       VARCHAR NOT NULL,
    table_name  VARCHAR NOT NULL,
    column_name VARCHAR NOT NULL,
    ordinal     INTEGER,
    data_type   VARCHAR,
    description VARCHAR,            -- original doc, withheld when stripped
    is_pk       BOOLEAN DEFAULT FALSE,  -- declared
    PRIMARY KEY (dataset, db_id, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS meta.declared_fks (
    dataset VARCHAR, db_id VARCHAR,
    src_table VARCHAR, src_column VARCHAR,
    tgt_table VARCHAR, tgt_column VARCHAR
);

-- Opaque-name mapping for the obfuscated setting.
CREATE TABLE IF NOT EXISTS meta.obfuscation_map (
    dataset VARCHAR, db_id VARCHAR,
    table_name VARCHAR, column_name VARCHAR, opaque_name VARCHAR,
    PRIMARY KEY (dataset, db_id, table_name, column_name)
);

-- Gold labels parsed from gold SQL. Never read by profiling or prompts.
CREATE TABLE IF NOT EXISTS meta.questions (
    dataset VARCHAR NOT NULL, db_id VARCHAR NOT NULL,
    question_id VARCHAR NOT NULL, question VARCHAR, gold_sql VARCHAR,
    PRIMARY KEY (dataset, question_id)
);
CREATE TABLE IF NOT EXISTS meta.gold_columns (
    dataset VARCHAR NOT NULL, question_id VARCHAR NOT NULL,
    table_name VARCHAR NOT NULL, column_name VARCHAR NOT NULL
);

-- Stage 1: column profiles (exact, computed over full columns).
CREATE TABLE IF NOT EXISTS meta.column_profiles (
    dataset VARCHAR, db_id VARCHAR, setting VARCHAR,
    table_name VARCHAR, column_name VARCHAR,
    data_type VARCHAR, row_count BIGINT, null_rate DOUBLE,
    empty_rate DOUBLE, placeholder_rate DOUBLE,
    distinct_count BIGINT, uniqueness DOUBLE,
    top_values JSON,        -- [{value, count}]
    type_stats JSON,        -- percentiles / string length / date range
    sample_values JSON,     -- fixed-seed sample
    PRIMARY KEY (dataset, db_id, setting, table_name, column_name)
);

-- Stage 2: primary-key and foreign-key candidates with score components.
CREATE TABLE IF NOT EXISTS meta.pk_candidates (
    dataset VARCHAR, db_id VARCHAR, setting VARCHAR,
    table_name VARCHAR, column_name VARCHAR,
    pk_score DOUBLE, components JSON, is_key BOOLEAN
);
CREATE TABLE IF NOT EXISTS meta.fk_candidates (
    dataset VARCHAR, db_id VARCHAR, setting VARCHAR,
    src_table VARCHAR, src_column VARCHAR,
    tgt_table VARCHAR, tgt_column VARCHAR,
    containment DOUBLE,     -- I(a -> b)
    cardinality VARCHAR,    -- observed, e.g. many-to-one
    fk_score DOUBLE, components JSON,
    gates_passed BOOLEAN,
    decision VARCHAR,       -- accepted | rejected | abstained | ambiguous
    decided_by VARCHAR      -- stats | llm
);

-- Stage 3: column descriptions. status: resolved | tentative | unresolved
CREATE TABLE IF NOT EXISTS meta.generated_descriptions (
    dataset VARCHAR, db_id VARCHAR, setting VARCHAR, variant VARCHAR,
    table_name VARCHAR, column_name VARCHAR,
    semantic_role VARCHAR, description VARCHAR, status VARCHAR,
    PRIMARY KEY (dataset, db_id, setting, variant, table_name, column_name)
);

-- Every LLM call and pipeline operation, for grounding and verification.
CREATE SEQUENCE IF NOT EXISTS meta.run_log_seq;
CREATE TABLE IF NOT EXISTS meta.run_log (
    id BIGINT DEFAULT nextval('meta.run_log_seq') PRIMARY KEY,
    ts TIMESTAMP DEFAULT current_timestamp,
    dataset VARCHAR, db_id VARCHAR, setting VARCHAR,
    stage VARCHAR, operation VARCHAR, model VARCHAR,
    input JSON, output JSON
);

-- Retrieval evaluation results.
CREATE TABLE IF NOT EXISTS meta.eval_results (
    run_id VARCHAR, dataset VARCHAR, setting VARCHAR, system VARCHAR,
    question_id VARCHAR, k INTEGER,
    table_recall DOUBLE, column_recall DOUBLE, full_gold_set BOOLEAN
);

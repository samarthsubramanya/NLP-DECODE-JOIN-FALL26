-- Bare-bones DECODE-JOIN schema. Load each database's raw tables into their own
-- schema (e.g. bird__financial) and register it below.
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.databases (
    dataset     VARCHAR NOT NULL,   -- bird, spider, beaver, kaggledbqa, fiben
    db_id       VARCHAR NOT NULL,
    duck_schema VARCHAR NOT NULL,   -- schema holding the raw tables
    source_path VARCHAR,
    loaded_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (dataset, db_id)
);

-- Original (documented) columns, kept as the upper reference.
CREATE TABLE IF NOT EXISTS meta.columns (
    dataset VARCHAR NOT NULL, db_id VARCHAR NOT NULL,
    table_name VARCHAR NOT NULL, column_name VARCHAR NOT NULL,
    ordinal INTEGER, data_type VARCHAR,
    description VARCHAR, is_pk BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (dataset, db_id, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS meta.declared_fks (
    dataset VARCHAR, db_id VARCHAR,
    src_table VARCHAR, src_column VARCHAR,
    tgt_table VARCHAR, tgt_column VARCHAR
);

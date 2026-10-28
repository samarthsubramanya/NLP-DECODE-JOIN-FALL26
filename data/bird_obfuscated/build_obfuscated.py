#!/usr/bin/env python3
"""Trusted, bounded BIRD preparation. Never use this exporter as a pipeline reader."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import stat
import sys
import threading
import time

import duckdb
import pyarrow

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'datasets/bird/decode_join_bird_dev.duckdb'
SOURCE_SHA = '086712fd122f24c7be36ad62a19698a3163c32cf429f015bf69c9a2506f69460'
PUBLIC = ROOT / 'artifacts/bird_obfuscated/v1'
PRIVATE = ROOT / 'data/.cache/bird_obfuscated/phase3a'
EXPECTED = {
    'bird_california_schools': (3, 89, 29941),
    'bird_card_games': (6, 115, 803445),
    'bird_codebase_community': (8, 71, 740646),
    'bird_debit_card_specializing': (5, 21, 423050),
    'bird_european_football_2': (7, 199, 222796),
    'bird_financial': (8, 55, 1079680),
    'bird_formula_1': (13, 94, 514287),
    'bird_student_club': (8, 48, 42511),
    'bird_superhero': (10, 31, 10614),
    'bird_thrombosis_prediction': (3, 64, 15952),
    'bird_toxicology': (4, 11, 49813),
}
RESERVE = 4 * 1024**3
HEADROOM = 256 * 1024**2


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(8 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    with Path(path).open('x', encoding='utf8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')


def require_space(path):
    free = shutil.disk_usage(path).free
    if free < RESERVE + HEADROOM:
        raise RuntimeError('Stopped before reaching the 4 GiB free-space reserve')
    return free


def configure(connection, scratch):
    connection.execute("SET memory_limit='384MB'")
    connection.execute('SET threads=1')
    connection.execute("SET max_temp_directory_size='512MB'")
    connection.execute('SET preserve_insertion_order=true')
    connection.execute("SET temp_directory='" + str(scratch).replace("'", "''") + "'")


class Monitor:
    """Sample filesystem free space and all owned build bytes; interrupt on a limit."""
    def __init__(self, root, connections):
        self.root = root
        self.connections = connections
        self.minimum_free = require_space(root)
        self.baseline_free = self.minimum_free
        self.peak_bytes = 0
        self.error = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def check(self):
        if self.error:
            raise RuntimeError(self.error)
        free = require_space(self.root)
        self.minimum_free = min(self.minimum_free, free)
        total = 0
        for path in self.root.rglob('*'):
            try:
                info = path.stat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except FileNotFoundError:
                # DuckDB can remove WAL/spill files during checkpoint/cleanup.
                continue
        self.peak_bytes = max(self.peak_bytes, total)
        if total > 3 * 1024**3:
            raise RuntimeError('Private build allocation exceeded 3 GiB')

    def _run(self):
        while not self.done.wait(0.25):
            try:
                self.check()
            except Exception as error:
                self.error = str(error)
                for connection in self.connections:
                    try:
                        connection.interrupt()
                    except Exception:
                        pass
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join()


def inventory(connection):
    tables = []
    for schema, table in connection.execute(
        "SELECT schema_name,table_name FROM duckdb_tables() "
        "WHERE NOT internal AND schema_name<>'bird_metadata' ORDER BY 1,2"
    ).fetchall():
        columns = connection.execute(
            'SELECT column_name,data_type,ordinal_position FROM information_schema.columns '
            'WHERE table_catalog=current_database() AND table_schema=? AND table_name=? '
            'ORDER BY ordinal_position', [schema, table]
        ).fetchall()
        if any(ordinal != i for i, (_, _, ordinal) in enumerate(columns, 1)):
            raise ValueError('Non-contiguous source ordinals')
        aliases = [f'col_{i:03}' for i in range(1, len(columns) + 1)]
        if {a.casefold() for a in aliases} & {name.casefold() for name, _, _ in columns}:
            raise ValueError('Source identifier collides with the approved alias scheme')
        if any(typ not in {'BIGINT', 'DOUBLE', 'VARCHAR'} for _, typ, _ in columns):
            raise ValueError('Unexpected source type; design review required')
        count = connection.execute('SELECT count(*) FROM ' + quote(schema) + '.' + quote(table)).fetchone()[0]
        tables.append(dict(schema=schema, table=table, rows=count, columns=[
            dict(original=name, opaque=alias, ordinal=ordinal, type=typ)
            for (name, typ, ordinal), alias in zip(columns, aliases)
        ]))
    return tables


def check_coverage(tables, expected):
    actual = {}
    for table in tables:
        totals = actual.setdefault(table['schema'], [0, 0, 0])
        totals[0] += 1
        totals[1] += len(table['columns'])
        totals[2] += table['rows']
    if actual != {key: list(value) for key, value in expected.items()}:
        raise ValueError('Source coverage differs from the approved inventory')


def signature(connection, table, originals):
    names = [column['original' if originals else 'opaque'] for column in table['columns']]
    expressions = ['count(*)', 'sum(hash(' + ','.join(map(quote, names)) + ')::HUGEINT)',
                   'bit_xor(hash(' + ','.join(map(quote, names)) + '))']
    labels = ['rows', 'row_hash_sum', 'row_hash_xor']
    for name, column in zip(names, table['columns']):
        q = quote(name)
        expressions.append('count(*) FILTER (WHERE ' + q + ' IS NULL)')
        labels.append(column['opaque'] + ':null')
        if column['type'] == 'VARCHAR':
            expressions.append('count(*) FILTER (WHERE ' + q + "='')")
            labels.append(column['opaque'] + ':empty')
        if column['type'] == 'DOUBLE':
            for function in ['isnan', 'isinf']:
                expressions.append('count(*) FILTER (WHERE ' + function + '(' + q + '))')
                labels.append(column['opaque'] + ':' + function)
    values = connection.execute('SELECT ' + ','.join(expressions) + ' FROM ' +
                                quote(table['schema']) + '.' + quote(table['table'])).fetchone()
    return dict(zip(labels, values))


def audit_public(connection, tables):
    expected_tables = {(t['schema'], t['table']) for t in tables}
    actual = set(connection.execute('SELECT schema_name,table_name FROM duckdb_tables() WHERE NOT internal').fetchall())
    if actual != expected_tables:
        raise ValueError('Public table inventory mismatch')
    user_schemas = {r[0] for r in connection.execute(
        "SELECT schema_name FROM duckdb_schemas() WHERE NOT internal AND schema_name<>'main'"
    ).fetchall()}
    if user_schemas != {t['schema'] for t in tables}:
        raise ValueError('Public schema inventory mismatch')
    checks = {
        'constraints': 'SELECT count(*) FROM duckdb_constraints()',
        'indexes': 'SELECT count(*) FROM duckdb_indexes()',
        'user_views': 'SELECT count(*) FROM duckdb_views() WHERE NOT internal',
        'sequences': 'SELECT count(*) FROM duckdb_sequences()',
        'user_macros': "SELECT count(*) FROM duckdb_functions() WHERE NOT internal AND function_type IN ('macro','table_macro')",
        'table_comments': 'SELECT count(*) FROM duckdb_tables() WHERE NOT internal AND comment IS NOT NULL',
        'column_metadata': 'SELECT count(*) FROM duckdb_columns() WHERE NOT internal AND (comment IS NOT NULL OR column_default IS NOT NULL OR NOT is_nullable)',
        'table_tags': 'SELECT count(*) FROM duckdb_tables() WHERE NOT internal AND cardinality(tags)>0',
        'schema_metadata': 'SELECT count(*) FROM duckdb_schemas() WHERE NOT internal AND (comment IS NOT NULL OR cardinality(tags)>0)',
        'user_types': 'SELECT count(*) FROM duckdb_types() WHERE NOT internal',
        'secrets': 'SELECT count(*) FROM duckdb_secrets()',
        'extra_databases': "SELECT count(*) FROM duckdb_databases() WHERE NOT internal AND database_name<>current_database()",
    }
    results = {name: connection.execute(sql).fetchone()[0] for name, sql in checks.items()}
    if any(results.values()):
        raise ValueError('Prohibited public database metadata found')
    for table in tables:
        columns = connection.execute(
            'SELECT column_name,data_type,ordinal_position FROM information_schema.columns '
            'WHERE table_catalog=current_database() AND table_schema=? AND table_name=? ORDER BY ordinal_position',
            [table['schema'], table['table']]
        ).fetchall()
        if columns != [(c['opaque'], c['type'], c['ordinal']) for c in table['columns']]:
            raise ValueError('Public column identity/type/order mismatch')
    return results


def build(source=SOURCE, public=PUBLIC, private=PRIVATE, expected=EXPECTED, expected_sha=SOURCE_SHA):
    source, public, private = map(lambda p: Path(p).absolute(), (source, public, private))
    if source.is_symlink() or not source.is_file():
        raise ValueError('Source must be an existing regular, non-symlink file')
    if public.exists() or private.exists():
        raise FileExistsError('Refusing to overwrite an existing public or private run')
    if public.resolve().is_relative_to(private.resolve()) or private.resolve().is_relative_to(public.resolve()):
        raise ValueError('Public and private directories must be separate')
    for path in [public, private]:
        if source.resolve().is_relative_to(path.resolve()):
            raise ValueError('Output directory contains source')
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError('Symlinked output ancestors are forbidden')
    require_space(source.parent)
    before = source.stat()
    source_before = sha256(source)
    if source_before != expected_sha:
        raise ValueError('Source SHA-256 differs from approved source')
    old_umask = os.umask(0o077)
    started = time.monotonic()
    report = {'status': 'RUNNING', 'source_sha256_before': source_before,
              'runtime_isolation': 'NOT ESTABLISHED: trusted preparation only', 'tables': []}
    monitor = None
    src = dst = None
    try:
        private.mkdir(parents=True, mode=0o700)
        if stat.S_IMODE(private.stat().st_mode) != 0o700:
            raise ValueError('Private directory must be mode 0700')
        source_scratch = private / 'source_scratch'
        target_scratch = private / 'target_scratch'
        source_scratch.mkdir(mode=0o700)
        target_scratch.mkdir(mode=0o700)
        staged = private / 'bird_dev_obfuscated.duckdb'
        src = duckdb.connect(str(source), read_only=True)
        configure(src, source_scratch)
        tables = inventory(src)
        check_coverage(tables, expected)
        mapping = {'format_version': 1, 'source_sha256': source_before, 'tables': tables}
        write_json(private / 'mapping.json', mapping)
        dst = duckdb.connect(str(staged))
        configure(dst, target_scratch)
        with Monitor(private, [src, dst]) as monitor:
            for schema in sorted(expected):
                monitor.check()
                dst.execute('CREATE SCHEMA ' + quote(schema))
            for table in tables:
                monitor.check()
                qualified = quote(table['schema']) + '.' + quote(table['table'])
                definitions = ','.join(quote(c['opaque']) + ' ' + c['type'] for c in table['columns'])
                dst.execute('BEGIN')
                try:
                    dst.execute('CREATE TABLE ' + qualified + ' (' + definitions + ')')
                    projection = ','.join(quote(c['original']) + ' AS ' + quote(c['opaque']) for c in table['columns'])
                    reader = src.execute('SELECT ' + projection + ' FROM ' + qualified).to_arrow_reader(8192)
                    written = 0
                    for batch in reader:
                        monitor.check()
                        dst.register('_sanitized_batch', batch)
                        try:
                            dst.execute('INSERT INTO ' + qualified + ' SELECT * FROM _sanitized_batch')
                        finally:
                            dst.unregister('_sanitized_batch')
                        written += batch.num_rows
                    if written != table['rows']:
                        raise ValueError('Export row count mismatch')
                    dst.execute('COMMIT')
                except Exception:
                    dst.execute('ROLLBACK')
                    raise
                dst.execute('CHECKPOINT')
                left, right = signature(src, table, True), signature(dst, table, False)
                if left != right:
                    raise ValueError('Basic duplicate-sensitive integrity check failed')
                report['tables'].append({'schema': table['schema'], 'table': table['table'],
                                         'columns': len(table['columns']), 'signature': left, 'status': 'PASS'})
                print('PASS', table['schema'], table['table'], written, flush=True)
            dst.execute('CHECKPOINT')
            monitor.check()
        dst.close()
        dst = None
        with duckdb.connect(str(staged), read_only=True) as fresh:
            configure(fresh, target_scratch)
            report['metadata_audit'] = audit_public(fresh, tables)
            for table, entry in zip(tables, report['tables']):
                require_space(private)
                if signature(fresh, table, False) != entry['signature']:
                    raise ValueError('Fresh read-only signature differs from writer validation')
        source_after = sha256(source)
        after = source.stat()
        if source_after != source_before or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError('Source changed during preparation')
        src.close()
        src = None
        require_space(private)
        if staged.with_suffix('.duckdb.wal').exists():
            raise ValueError('Destination WAL remains after close')
        parent_was_present = public.parent.exists()
        public.parent.mkdir(parents=True, exist_ok=True)
        if not parent_was_present:
            os.chmod(public.parent, 0o755)
        if public.parent.stat().st_dev != private.stat().st_dev:
            raise ValueError('Publication requires the same filesystem to avoid copying')
        sanitized_tables = [{key: value for key, value in t.items() if key != 'columns'} | {
            'columns': [{key: value for key, value in col.items() if key != 'original'} for col in t['columns']]
        } for t in tables]
        database_sha = sha256(staged)
        manifest = {'format_version': 1, 'dataset': 'BIRD 1 development', 'setting': 'obfuscated',
                    'database': staged.name, 'database_sha256': database_sha,
                    'schemas': len(expected), 'tables': sanitized_tables,
                    'table_count': len(tables), 'column_count': sum(len(t['columns']) for t in tables),
                    'row_count': sum(t['rows'] for t in tables),
                    'isolation': 'Trusted preparation; runtime isolation required before pipeline use',
                    'parquet': 'No upfront exports. Dynamic replacement of earlier 11-output contract pending team confirmation.'}
        write_json(private / 'public_manifest.json', manifest)
        # mkdir is exclusive; link is exclusive and does not duplicate database storage.
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        for origin, name in [(staged, staged.name), (private / 'public_manifest.json', 'manifest.json')]:
            os.chmod(origin, 0o444)
            os.link(origin, public / name)
        # Remove only our newly created staging links after successful publication.
        staged.unlink()
        (private / 'public_manifest.json').unlink()
        report.update(status='PASS', source_sha256_after=source_after, public_database=str(public / staged.name),
                      database_sha256=database_sha, database_bytes=(public / staged.name).stat().st_size,
                      schemas=len(expected), tables_count=len(tables), columns=manifest['column_count'], rows=manifest['row_count'],
                      versions={'duckdb': duckdb.__version__, 'pyarrow': pyarrow.__version__, 'python': sys.version.split()[0]},
                      process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
                      seconds=time.monotonic() - started,
                      space={'initial_free_bytes': monitor.baseline_free, 'minimum_sampled_free_bytes': monitor.minimum_free,
                             'peak_sampled_owned_build_bytes': monitor.peak_bytes, 'final_free_bytes': shutil.disk_usage(private).free,
                             'sampling_seconds': 0.25},
                      deferred=['Exact full-value multiset comparison and DOUBLE bit-pattern audit',
                                'Adversarial leakage tests and enforced pipeline runtime isolation',
                                'Phase 3B dynamic evidence and Phase 4 comprehensive validation'])
        write_json(private / 'validation.json', report)
        return report
    except Exception as error:
        if private.exists() and not (private / 'failure.json').exists():
            write_json(private / 'failure.json', {'status': 'FAIL', 'error': str(error), 'report': report})
        raise
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--public', type=Path, default=PUBLIC)
    parser.add_argument('--private', type=Path, default=PRIVATE)
    args = parser.parse_args()
    try:
        result = build(args.source, args.public, args.private)
    except Exception:
        print('FAIL: preparation stopped. Inspect the evaluator-only failure report; existing source data was not opened for writing.')
        raise SystemExit(1)
    print(json.dumps({key: result[key] for key in ['status', 'schemas', 'tables_count', 'columns', 'rows', 'database_bytes']}))


if __name__ == '__main__':
    main()

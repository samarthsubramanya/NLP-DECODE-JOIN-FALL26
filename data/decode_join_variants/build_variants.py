#!/usr/bin/env python3
import sys
sys.dont_write_bytecode = True

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import threading
import time

import duckdb

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'data/decode_join.duckdb'
PRIVATE = ROOT / 'data/.cache/decode_join_variants/v1'
EXCLUDED = {'spider_metadata', 'beaver_metadata'}
RESERVE = 4 * 1024**3 + 256 * 1024**2
MEMBERS = ['integer_value', 'real_value', 'text_value', 'blob_value']


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def qualified(schema, table, database=None):
    return '.'.join(map(quote, ([database] if database else []) + [schema, table]))


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, data):
    with Path(path).open('x', encoding='utf8') as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def require_space(path):
    free = shutil.disk_usage(path).free
    if free < RESERVE:
        raise RuntimeError('Stopped at the 4 GiB reserve plus 256 MiB headroom')
    return free


def configure(c, scratch):
    c.execute("SET memory_limit='256MB'")
    c.execute('SET threads=1')
    c.execute('SET preserve_insertion_order=true')
    c.execute("SET max_temp_directory_size='512MB'")
    c.execute('SET temp_directory=' + literal(scratch))


def inventory(c):
    grouped = defaultdict(list)
    for schema, table, name, typ, ordinal, nullable in c.execute(
            'SELECT table_schema,table_name,column_name,data_type,ordinal_position,is_nullable '
            'FROM information_schema.columns ORDER BY 1,2,5').fetchall():
        if schema not in EXCLUDED:
            grouped[schema, table].append(dict(original=name, opaque=f'col_{ordinal:03}',
                                              type=typ, ordinal=ordinal, original_nullable=nullable == 'YES'))
    tables = []
    for schema, name in c.execute('SELECT schema_name,table_name FROM duckdb_tables() WHERE NOT internal ORDER BY 1,2').fetchall():
        if schema in EXCLUDED:
            continue
        columns = grouped[schema, name]
        if any(col['ordinal'] != i for i, col in enumerate(columns, 1)):
            raise ValueError('Noncontiguous column ordinals')
        if {col['original'].casefold() for col in columns} & {col['opaque'] for col in columns}:
            raise ValueError('Original name collides with approved opaque aliases')
        if any(col['original'].casefold() == 'rowid' for col in columns):
            raise ValueError('A source column shadows the rowid needed for bounded transfer')
        for col in columns:
            if col['type'].startswith('UNION('):
                if re.findall(r'(\w+) (?:BIGINT|DOUBLE|VARCHAR|BLOB)', col['type']) != MEMBERS:
                    raise ValueError('Unexpected UNION members require review')
        rows, low, high = c.execute('SELECT count(*),min(rowid),max(rowid) FROM ' + qualified(schema, name)).fetchone()
        if rows and (low != 0 or high != rows - 1):
            raise ValueError('Source rowids have gaps; bounded transfer needs review')
        tables.append(dict(schema=schema, table=name, rows=rows, columns=columns))
    return tables


def encoded_projection(table):
    fields = []
    for col in table['columns']:
        name = quote(col['original'])
        if col['type'].startswith('UNION('):
            parts = ['tag:=union_tag(' + name + ')::VARCHAR']
            parts += [quote(member) + ':=union_extract(' + name + ',' + literal(member) + ')' for member in MEMBERS]
            name = 'struct_pack(' + ','.join(parts) + ')'
        fields.append(name + ' AS ' + quote(col['opaque']))
    return ','.join(fields)


def restored_expression(col):
    name = quote(col['opaque'])
    if not col['type'].startswith('UNION('):
        return name
    typ = col['type']
    parts = ['CASE WHEN ' + name + '.tag IS NULL THEN NULL::' + typ]
    for member in MEMBERS:
        parts.append('WHEN ' + name + '.tag=' + literal(member) + ' THEN union_value(' +
                     quote(member) + ':=' + name + '.' + quote(member) + ')::' + typ)
    return ' '.join(parts) + " ELSE error('Unexpected UNION tag') END"


def restored_projection(table, setting='obfuscated'):
    key = 'original' if setting == 'stripped' else 'opaque'
    return ','.join(restored_expression(col) + ' AS ' + quote(col[key]) for col in table['columns'])


def audit_projection(table, setting):
    key = 'original' if setting in ('source', 'stripped') else 'opaque'
    fields = []
    for col in table['columns']:
        name = quote(col[key])
        fields.append(name + ' AS ' + quote(col['opaque']))
        if col['type'].startswith('UNION('):
            fields.append('union_tag(' + name + ') AS ' + quote('_tag_' + str(col['ordinal'])))
            name = 'union_extract(' + name + ",'real_value')"
            fields.append('signbit(' + name + ') AS ' + quote('_sign_' + str(col['ordinal'])))
        elif col['type'] in ('FLOAT', 'DOUBLE'):
            fields.append('signbit(' + name + ') AS ' + quote('_sign_' + str(col['ordinal'])))
    return ','.join(fields)


def equal_values(c, left, right):
    sql = ('WITH a AS (' + left + '),b AS (' + right + ') '
           'SELECT EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM b) '
           'OR EXISTS(SELECT * FROM b EXCEPT ALL SELECT * FROM a)')
    if c.execute(sql).fetchone()[0]:
        raise ValueError('Exact duplicate-sensitive value comparison failed')


def check_public(c, tables, setting):
    actual = set(c.execute('SELECT schema_name,table_name FROM duckdb_tables() WHERE NOT internal').fetchall())
    if actual != {(t['schema'], t['table']) for t in tables}:
        raise ValueError('Public table inventory differs')
    schemas = {r[0] for r in c.execute("SELECT schema_name FROM duckdb_schemas() WHERE NOT internal AND schema_name<>'main'").fetchall()}
    if schemas != {t['schema'] for t in tables}:
        raise ValueError('Public schema inventory differs')
    queries = {
        'constraints': 'SELECT count(*) FROM duckdb_constraints()',
        'indexes': 'SELECT count(*) FROM duckdb_indexes()',
        'views': 'SELECT count(*) FROM duckdb_views() WHERE NOT internal',
        'sequences': 'SELECT count(*) FROM duckdb_sequences()',
        'macros': "SELECT count(*) FROM duckdb_functions() WHERE NOT internal AND function_type IN ('macro','table_macro')",
        'table_metadata': 'SELECT count(*) FROM duckdb_tables() WHERE NOT internal AND (comment IS NOT NULL OR cardinality(tags)>0)',
        'column_metadata': 'SELECT count(*) FROM duckdb_columns() WHERE NOT internal AND (comment IS NOT NULL OR column_default IS NOT NULL OR NOT is_nullable)',
        'schema_metadata': 'SELECT count(*) FROM duckdb_schemas() WHERE NOT internal AND (comment IS NOT NULL OR cardinality(tags)>0)',
        'user_types': 'SELECT count(*) FROM duckdb_types() WHERE NOT internal',
        'secrets': 'SELECT count(*) FROM duckdb_secrets()',
        'extra_databases': 'SELECT count(*) FROM duckdb_databases() WHERE NOT internal AND database_name<>current_database()',
    }
    audit = {key: c.execute(sql).fetchone()[0] for key, sql in queries.items()}
    if any(audit.values()):
        raise ValueError('Unexpected public metadata: ' + str(audit))
    actual_columns = defaultdict(list)
    for schema, name, column, typ, ordinal in c.execute(
            'SELECT table_schema,table_name,column_name,data_type,ordinal_position '
            'FROM information_schema.columns ORDER BY 1,2,5').fetchall():
        actual_columns[schema, name].append((column, typ, ordinal))
    key = 'original' if setting == 'stripped' else 'opaque'
    for t in tables:
        if actual_columns[t['schema'], t['table']] != [(col[key], col['type'], col['ordinal']) for col in t['columns']]:
            raise ValueError('Public column names/types/order differ')
    return audit


def key_ground_truth(c, tables):
    by_name = {(t['schema'], t['table']): t for t in tables}
    data = {key: dict(schema=key[0], table=key[1], primary_keys=[], foreign_keys=[], source_constraints=[])
            for key in by_name}
    for schema, table, kind, columns, parent, target in c.execute(
            'SELECT schema_name,table_name,constraint_type,constraint_column_names,referenced_table,referenced_column_names '
            'FROM duckdb_constraints()').fetchall():
        if (schema, table) not in data:
            continue
        row = data[schema, table]
        row['source_constraints'].append(dict(kind=kind, columns=columns, referenced_table=parent, referenced_columns=target))
        if kind == 'PRIMARY KEY':
            row['primary_keys'].append(dict(columns=columns, origin='declared', evidence='source DuckDB catalog'))
        elif kind == 'FOREIGN KEY':
            row['foreign_keys'].append(dict(columns=columns, referenced_table=parent, referenced_columns=target,
                                            origin='declared', evidence='source DuckDB catalog'))
    source_tables = set(c.execute('SELECT schema_name,table_name FROM duckdb_tables() WHERE NOT internal').fetchall())
    if ('spider_metadata', 'table_info') in source_tables:
        paths = dict(c.execute('SELECT source_path,duckdb_schema FROM spider_metadata.files').fetchall())
        for path, table, text in c.execute('SELECT source_path,table_name,metadata_json FROM spider_metadata.table_info').fetchall():
            key = paths[path], table
            if key not in data:
                raise ValueError('Spider metadata table cannot be resolved')
            raw = json.loads(text)
            data[key]['original_metadata'] = raw
            pk = [r['name'] for r in sorted(raw['table_xinfo'], key=lambda r: r['pk']) if r['pk']]
            if pk:
                data[key]['primary_keys'].append(dict(columns=pk, origin='declared', evidence='original SQLite metadata'))
            groups = defaultdict(list)
            for row in raw['foreign_key_list']:
                groups[row['id']].append(row)
            for rows in groups.values():
                rows.sort(key=lambda r: r['seq'])
                data[key]['foreign_keys'].append(dict(columns=[r['from'] for r in rows], referenced_table=rows[0]['table'],
                    referenced_columns=[r['to'] for r in rows], origin='declared', evidence='original SQLite metadata', raw=rows))
    if ('beaver_metadata', 'source_tables') in source_tables:
        def names(text):
            return [x.replace('``','`') for x in re.findall(r'`((?:[^`]|``)+)`', text)]
        for db, name, text in c.execute('SELECT source_db,table_name,definition_json FROM beaver_metadata.source_tables').fetchall():
            key = 'beaver_' + db, name
            if key not in data:
                raise ValueError('BEAVER metadata table cannot be resolved')
            raw = json.loads(text)
            data[key]['original_metadata'] = raw
            for constraint in raw['constraints']:
                pk = re.fullmatch(r'PRIMARY KEY \((.*)\)', constraint)
                fk = re.fullmatch(r'CONSTRAINT `(?:[^`]|``)+` FOREIGN KEY \((.*?)\) REFERENCES `((?:[^`]|``)+)` \((.*?)\)(.*)', constraint)
                if pk:
                    data[key]['primary_keys'].append(dict(columns=names(pk[1]), origin='declared', evidence=constraint))
                elif fk:
                    data[key]['foreign_keys'].append(dict(columns=names(fk[1]), referenced_table=fk[2].replace('``','`'),
                        referenced_columns=names(fk[3]), origin='declared', evidence=constraint))
                elif 'PRIMARY KEY' in constraint or 'FOREIGN KEY' in constraint:
                    raise ValueError('Unparsed BEAVER key declaration')
    # Resolve SQLite case-insensitive identifiers without guessing missing keys.
    table_lookup = {(s, t.casefold()): (s, t) for s, t in by_name}
    for key, row in data.items():
        columns = {col['original'].casefold(): col['original'] for col in by_name[key]['columns']}
        opaque = {col['original']: col['opaque'] for col in by_name[key]['columns']}
        for pk in row['primary_keys']:
            pk['resolved_columns'] = [columns.get(str(name).casefold()) for name in pk['columns']]
            pk['obfuscated_columns'] = [opaque.get(name) for name in pk['resolved_columns']]
        for fk in row['foreign_keys']:
            parent_key = table_lookup.get((key[0], fk['referenced_table'].casefold()))
            source_cols = [columns.get(str(name).casefold()) for name in fk['columns']]
            targets = list(fk['referenced_columns'])
            parent_cols = []
            if parent_key:
                parent_lookup = {col['original'].casefold(): col['original'] for col in by_name[parent_key]['columns']}
                if any(name is None for name in targets):
                    pks = data[parent_key]['primary_keys']
                    if len(pks) == 1 and len(pks[0]['columns']) == len(targets):
                        targets = pks[0]['columns']
                parent_cols = [parent_lookup.get(str(name).casefold()) for name in targets]
            resolved = bool(parent_key and all(source_cols) and parent_cols and all(parent_cols) and len(source_cols) == len(parent_cols))
            fk['resolution'] = 'resolved' if resolved else 'unresolved original declaration'
            if resolved:
                fk['resolved_columns'] = source_cols
                fk['resolved_referenced_table'] = parent_key[1]
                fk['resolved_referenced_columns'] = parent_cols
                fk['obfuscated_columns'] = [opaque[name] for name in source_cols]
                parent_opaque = {col['original']:col['opaque'] for col in by_name[parent_key]['columns']}
                fk['obfuscated_referenced_columns'] = [parent_opaque[name] for name in parent_cols]
    return dict(tables=list(data.values()), note='Original declarations, not inferred candidates. Not enforced in either public variant.')


def join_checks(c, truth, tables):
    inventory_by_name = {(t['schema'],t['table']):t for t in tables}
    candidates = []
    for table in truth['tables']:
        for fk in table['foreign_keys']:
            if fk['resolution'] != 'resolved':
                continue
            child = inventory_by_name[table['schema'],table['table']]
            parent = inventory_by_name[table['schema'],fk['resolved_referenced_table']]
            child_types = {col['original']:col['type'] for col in child['columns']}
            parent_types = {col['original']:col['type'] for col in parent['columns']}
            if not child['rows'] or not parent['rows']:
                continue
            if any(child_types[a] != parent_types[b] for a,b in zip(fk['resolved_columns'],fk['resolved_referenced_columns'])):
                continue
            candidates.append((child['rows']+parent['rows'], table, fk))
    candidates.sort(key=lambda item: (item[0],item[1]['schema'],item[1]['table']))
    selected, used_schemas = [], set()
    # Include both Nova and Neutron, several Spider schemas, and composites.
    ordered = [x for x in candidates if x[1]['schema'].startswith('beaver_')]
    ordered += [x for x in candidates if len(x[2]['resolved_columns']) > 1]
    ordered += candidates
    for _, table, fk in ordered:
        schema = table['schema']
        if schema in used_schemas or len(selected) >= 16:
            continue
        sqls = {}
        for setting in ('source','stripped','obfuscated'):
            child = inventory_by_name[schema,table['table']]
            parent = inventory_by_name[schema,fk['resolved_referenced_table']]
            names = [{col['original']:col['opaque'] if setting=='obfuscated' else col['original'] for col in t['columns']}
                     for t in (child,parent)]
            pairs = [(names[0][a],names[1][b]) for a,b in zip(fk['resolved_columns'],fk['resolved_referenced_columns'])]
            on = ' AND '.join('a.'+quote(a)+'=b.'+quote(b) for a,b in pairs)
            nonnull = ' AND '.join('a.'+quote(a)+' IS NOT NULL' for a,_ in pairs)
            parent_key = 'b.'+quote(pairs[0][1])
            sqls[setting] = ('SELECT count(*) FILTER (WHERE '+parent_key+' IS NOT NULL) AS matches,'
                            'count(*) FILTER (WHERE '+nonnull+' AND '+parent_key+' IS NULL) AS orphan_rows '
                            'FROM '+qualified(schema,child['table'])+' a LEFT JOIN '+qualified(schema,parent['table'])+' b ON '+on)
        expected = list(c.execute(sqls['source']).fetchone())
        if not expected[0]:
            continue
        selected.append(dict(schema=schema,table=table['table'],source_columns=fk['resolved_columns'],
                             referenced_table=fk['resolved_referenced_table'],referenced_columns=fk['resolved_referenced_columns'],
                             sql=sqls,expected=expected))
        used_schemas.add(schema)
    return selected


def build(source=SOURCE, output_dir=ROOT/'data', private=PRIVATE, expected_tables=2307,
          expected_rows=13473416, batch_size=32768):
    source, output_dir, private = [Path(p).absolute() for p in (source,output_dir,private)]
    outputs = {s:output_dir/f'decode_join_{s}.duckdb' for s in ('stripped','obfuscated')}
    manifests = {s:output_dir/f'decode_join_{s}_manifest.json' for s in outputs}
    if source.is_symlink() or not source.is_file():
        raise ValueError('Source must be a regular existing file')
    if private.exists() or any(p.exists() for p in [*outputs.values(),*manifests.values()]):
        raise FileExistsError('Refusing to overwrite an existing run or output')
    if any(p.is_symlink() for path in (output_dir,private) for p in [path,*path.parents]):
        raise ValueError('Symlinked output ancestors are forbidden')
    initial_free = require_space(source.parent)
    before = source.stat()
    source_hash = sha256(source)
    started = time.monotonic()
    old_umask = os.umask(0o077)
    source_c, targets, connections = None, {}, []
    done, errors = threading.Event(), []
    state = dict(initial_free_bytes=initial_free,minimum_free_bytes=initial_free,peak_transfer_bytes=0)
    report = dict(status='RUNNING',source=str(source),source_sha256_before=source_hash,tables=[])
    monitor = None
    try:
        private.mkdir(parents=True,mode=0o700)
        scratch = private/'scratch'
        scratch.mkdir(mode=0o700)
        transfer = private/'transfer.parquet'
        source_c = duckdb.connect(str(source),read_only=True)
        configure(source_c,scratch/'source')
        connections.append(source_c)
        tables = inventory(source_c)
        if len(tables) != expected_tables or sum(t['rows'] for t in tables) != expected_rows:
            raise ValueError('Source coverage differs from the approved scope')
        mapping = dict(format_version=1,source_sha256=source_hash,excluded_schemas=sorted(EXCLUDED),tables=tables)
        write_json(private/'mapping.json',mapping)
        truth = key_ground_truth(source_c,tables)
        truth['representative_queries'] = join_checks(source_c,truth,tables)
        write_json(private/'ground_truth.json',truth)
        staged = {s:private/outputs[s].name for s in outputs}
        for setting,path in staged.items():
            targets[setting] = duckdb.connect(str(path))
            configure(targets[setting],scratch/setting)
            connections.append(targets[setting])
            for schema in sorted({t['schema'] for t in tables}):
                targets[setting].execute('CREATE SCHEMA '+quote(schema))

        def guard():
            free = require_space(private)
            state['minimum_free_bytes'] = min(state['minimum_free_bytes'],free)
            if transfer.exists():
                size = transfer.stat().st_size
                state['peak_transfer_bytes'] = max(state['peak_transfer_bytes'],size)
                if size > 512*1024**2:
                    raise RuntimeError('Single transfer batch exceeded 512 MiB')
            if errors:
                raise RuntimeError(errors[0])

        def watch():
            while not done.wait(.25):
                try:
                    guard()
                except FileNotFoundError:
                    continue
                except Exception as error:
                    errors.append(str(error))
                    for connection in connections:
                        try:
                            connection.interrupt()
                        except Exception:
                            pass
                    return

        monitor = threading.Thread(target=watch,daemon=True)
        monitor.start()
        last_print = time.monotonic()
        for index, table in enumerate(tables,1):
            guard()
            name = qualified(table['schema'],table['table'])
            target_bases = {}
            for setting,target in targets.items():
                key = 'original' if setting=='stripped' else 'opaque'
                definitions = ','.join(quote(col[key])+' '+col['type'] for col in table['columns'])
                target.execute('BEGIN')
                target.execute('CREATE TABLE '+name+' ('+definitions+')')
            checked = 0
            for offset in range(0,table['rows'],batch_size):
                guard()
                upper = min(offset+batch_size,table['rows'])
                where = f' WHERE rowid>={offset} AND rowid<{upper}'
                source_c.execute('COPY (SELECT '+encoded_projection(table)+' FROM '+name+where+') TO '+literal(transfer)+' (FORMAT PARQUET,COMPRESSION ZSTD)')
                restored = '(SELECT '+restored_projection(table)+ ' FROM read_parquet('+literal(transfer)+'))'
                from_file = 'SELECT '+audit_projection(table,'obfuscated')+' FROM '+restored
                original = 'SELECT '+audit_projection(table,'source')+' FROM '+name+where
                equal_values(source_c,original,from_file)
                for setting,target in targets.items():
                    target.execute('INSERT INTO '+name+' SELECT '+restored_projection(table,setting)+' FROM read_parquet('+literal(transfer)+')')
                    if offset == 0:
                        target_bases[setting] = target.execute('SELECT min(rowid) FROM '+name).fetchone()[0]
                    # DuckDB uses temporary rowids until the table transaction commits.
                    base = target_bases[setting]
                    target_where = f' WHERE rowid>={base+offset} AND rowid<{base+upper}'
                    copied = 'SELECT '+audit_projection(table,setting)+' FROM '+name+target_where
                    equal_values(target,copied,from_file)
                transfer.unlink()  # Only this run's bounded transfer file.
                checked += 1
            for target in targets.values():
                target.execute('COMMIT')
                if target.execute('SELECT count(*) FROM '+name).fetchone()[0] != table['rows']:
                    raise ValueError('Copied row count differs')
            report['tables'].append(dict(schema=table['schema'],table=table['table'],rows=table['rows'],batches=checked,exact_values='PASS'))
            if index%50 == 0:
                for target in targets.values():
                    target.execute('CHECKPOINT')
            if index%50 == 0 or time.monotonic()-last_print>15:
                print(f'Copied {index}/{len(tables)} tables; {table["schema"]}.{table["table"]}; free {shutil.disk_usage(private).free/1024**3:.1f} GiB',flush=True)
                last_print = time.monotonic()
        for setting,target in targets.items():
            target.execute('CHECKPOINT')
            target.close()
        targets.clear()
        connections[:] = [source_c]
        report['public_audits'] = {}
        report['joins'] = {}
        for setting,path in staged.items():
            with duckdb.connect(str(path),read_only=True) as c:
                configure(c,scratch/setting)
                report['public_audits'][setting] = check_public(c,tables,setting)
                joins = []
                for query in truth['representative_queries']:
                    result = list(c.execute(query['sql'][setting]).fetchone())
                    if result != query['expected']:
                        raise ValueError('Mapped join results changed')
                    joins.append(dict(schema=query['schema'],table=query['table'],result=result,status='PASS'))
                report['joins'][setting] = joins
        # This evaluator-only connection attaches all inputs READ_ONLY. Neither
        # destination writer ever attaches the original source or metadata.
        verify = duckdb.connect(':memory:')
        configure(verify,scratch/'verify')
        connections.append(verify)
        try:
            verify.execute('ATTACH '+literal(source)+' AS source_data (READ_ONLY)')
            for setting,path in staged.items():
                verify.execute('ATTACH '+literal(path)+' AS '+quote(setting)+' (READ_ONLY)')
            for index,table in enumerate(tables,1):
                guard()
                for offset in range(0,table['rows'],batch_size):
                    upper = min(offset+batch_size,table['rows'])
                    where = f' WHERE rowid>={offset} AND rowid<{upper}'
                    left = 'SELECT '+audit_projection(table,'source')+' FROM '+qualified(table['schema'],table['table'],'source_data')+where
                    for setting in staged:
                        right = 'SELECT '+audit_projection(table,setting)+' FROM '+qualified(table['schema'],table['table'],setting)+where
                        equal_values(verify,left,right)
                if index%200 == 0:
                    print(f'Fresh exact read-only validation {index}/{len(tables)} tables',flush=True)
        finally:
            verify.close()
            connections.remove(verify)
        guard()
        after_hash = sha256(source)
        after = source.stat()
        if after_hash != source_hash or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('Source changed during the build')
        source_c.close()
        source_c = None
        connections.clear()
        output_dir.mkdir(parents=True,exist_ok=True)
        report['outputs'] = {}
        for setting,path in staged.items():
            if path.with_suffix('.duckdb.wal').exists():
                raise ValueError('Destination WAL remains')
            manifest_tables = []
            for table in tables:
                key = 'original' if setting=='stripped' else 'opaque'
                manifest_tables.append(dict(schema=table['schema'],table=table['table'],rows=table['rows'],
                    columns=[dict(name=col[key],type=col['type'],ordinal=col['ordinal']) for col in table['columns']]))
            destination_hash = sha256(path)
            manifest = dict(format_version=1,setting=setting,database=outputs[setting].name,sha256=destination_hash,
                            schemas=len({t['schema'] for t in tables}),table_count=len(tables),row_count=sum(t['rows'] for t in tables),
                            column_count=sum(len(t['columns']) for t in tables),tables=manifest_tables,
                            runtime_isolation='Trusted preparation only; keep private mappings and source inaccessible to evaluation models')
            manifest_path = private/f'{setting}_manifest.json'
            write_json(manifest_path,manifest)
            for origin,destination in [(path,outputs[setting]),(manifest_path,manifests[setting])]:
                os.chmod(origin,0o444)
                os.link(origin,destination)  # Exclusive publication without a database copy.
                origin.unlink()  # Remove only the staging link created by this run.
            report['outputs'][setting] = dict(path=str(outputs[setting]),bytes=outputs[setting].stat().st_size,sha256=destination_hash)
        report.update(status='PASS',source_sha256_after=after_hash,table_count=len(tables),
                      schema_count=len({t['schema'] for t in tables}),row_count=sum(t['rows'] for t in tables),
                      column_count=sum(len(t['columns']) for t in tables),type_counts=dict(Counter(col['type'] for t in tables for col in t['columns'])),
                      fresh_exact_comparisons='PASS',disk=state,final_free_bytes=shutil.disk_usage(private).free,
                      seconds=time.monotonic()-started,duckdb_version=duckdb.__version__,
                      peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                      validation_scope='Exact EXCEPT ALL on bounded batches, including UNION tags and floating zero signs; NaN payload bit patterns are not separately audited')
        write_json(private/'validation.json',report)
        return report
    except Exception as error:
        if private.exists() and not (private/'failure.json').exists():
            write_json(private/'failure.json',dict(error=str(error),report=report))
        raise
    finally:
        done.set()
        if monitor:
            monitor.join()
        for target in targets.values():
            target.close()
        if source_c is not None:
            source_c.close()
        os.umask(old_umask)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source',type=Path,default=SOURCE)
    p.add_argument('--output-dir',type=Path,default=ROOT/'data')
    p.add_argument('--private',type=Path,default=PRIVATE)
    p.add_argument('--batch-size',type=int,default=32768)
    args = p.parse_args()
    if args.batch_size < 1 or args.batch_size > 32768:
        p.error('Batch size must be between 1 and 32768')
    result = build(args.source,args.output_dir,args.private,batch_size=args.batch_size)
    print(json.dumps({key:result[key] for key in ['status','schema_count','table_count','column_count','row_count','outputs','final_free_bytes','seconds']},indent=2))

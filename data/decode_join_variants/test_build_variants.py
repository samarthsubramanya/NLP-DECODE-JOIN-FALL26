import json
from pathlib import Path
import stat
import tempfile
import unittest

import duckdb

from build_variants import build, sha256, inventory, equal_values


class VariantTests(unittest.TestCase):
    def fixture(self, root):
        path = root/'source.duckdb'
        with duckdb.connect(str(path)) as c:
            c.execute('CREATE SCHEMA fixture_case')
            c.execute('CREATE TABLE fixture_case.parents ("Parent Key" INTEGER PRIMARY KEY, caption VARCHAR)')
            c.execute("INSERT INTO fixture_case.parents VALUES (1,'café'),(2,NULL)")
            c.execute('CREATE TABLE fixture_case.children ("Child Key" BIGINT NOT NULL, "Parent Ref" INTEGER REFERENCES fixture_case.parents("Parent Key"), '
                      'mixed UNION(integer_value BIGINT,real_value DOUBLE,text_value VARCHAR,blob_value BLOB), '
                      'single FLOAT, precise DOUBLE, stamp TIMESTAMP, binary_data BLOB, flag BOOLEAN)')
            c.execute("INSERT INTO fixture_case.children VALUES "
                      "(11,1,union_value(integer_value:=8),'-0'::FLOAT,'NaN'::DOUBLE,'2025-01-02 03:04:05.123456',from_hex('00ff'),true),"
                      "(12,1,union_value(real_value:=1.25),'Infinity'::FLOAT,'-Infinity'::DOUBLE,NULL,NULL,false),"
                      "(13,2,union_value(text_value:=''),NULL,0,NULL,NULL,NULL),"
                      "(14,2,union_value(blob_value:=from_hex('00ff')),3.5,1.25,NULL,NULL,true),"
                      "(15,NULL,union_value(text_value:=NULL),NULL,NULL,NULL,NULL,NULL),"
                      "(15,NULL,union_value(text_value:=NULL),NULL,NULL,NULL,NULL,NULL)")
            c.execute('CREATE SCHEMA spider_metadata')
            c.execute('CREATE TABLE spider_metadata.gold (secret VARCHAR)')
            c.execute("INSERT INTO spider_metadata.gold VALUES ('answer-secret')")
            c.execute('CREATE VIEW fixture_case.hidden_view AS SELECT * FROM fixture_case.parents')
            c.execute("COMMENT ON TABLE fixture_case.parents IS 'table-secret'")
            c.execute("COMMENT ON COLUMN fixture_case.children.mixed IS 'column-secret'")
        return path

    def test_both_variants_preserve_native_values_and_strip_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            source = self.fixture(root)
            before = sha256(source)
            result = build(source,root/'public',root/'private',expected_tables=2,expected_rows=8,batch_size=2)
            self.assertEqual(result['status'],'PASS')
            self.assertEqual(sha256(source),before)
            self.assertEqual(result['row_count'],8)
            mapping = json.loads((root/'private/mapping.json').read_text())
            children = next(t for t in mapping['tables'] if t['table']=='children')
            self.assertEqual(children['columns'][0]['opaque'],'col_001')
            for setting in ('stripped','obfuscated'):
                path = root/'public'/f'decode_join_{setting}.duckdb'
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o444)
                self.assertEqual(len(result['joins'][setting]),1)
                self.assertTrue(all(v==0 for v in result['public_audits'][setting].values()))
                with duckdb.connect(str(path),read_only=True) as c:
                    mixed = 'mixed' if setting=='stripped' else 'col_003'
                    values = c.execute(f'SELECT union_tag("{mixed}")::VARCHAR,count(*) FROM fixture_case.children GROUP BY 1 ORDER BY 1').fetchall()
                    self.assertEqual(values,[('blob_value',1),('integer_value',1),('real_value',1),('text_value',3)])
                manifest = (root/'public'/f'decode_join_{setting}_manifest.json').read_text()
                for secret in ('answer-secret','table-secret','column-secret','spider_metadata'):
                    self.assertNotIn(secret,manifest)
                if setting=='obfuscated':
                    self.assertNotIn('Parent Key',manifest)
                    self.assertNotIn('Child Key',manifest)
            self.assertEqual(stat.S_IMODE((root/'private/mapping.json').stat().st_mode),0o600)
            with self.assertRaises(FileExistsError):
                build(source,root/'public',root/'private',expected_tables=2,expected_rows=8)

    def test_exact_multiset_check_detects_duplicate_changes(self):
        with duckdb.connect(':memory:') as c:
            with self.assertRaisesRegex(ValueError,'Exact'):
                equal_values(c,'SELECT * FROM (VALUES (1),(1),(2)) t(v)',
                             'SELECT * FROM (VALUES (1),(2),(2)) t(v)')

    def test_approved_alias_collision_is_not_silently_renamed(self):
        with duckdb.connect(':memory:') as c:
            c.execute('CREATE TABLE t(col_001 INTEGER)')
            with self.assertRaisesRegex(ValueError,'collides'):
                inventory(c)

    def test_sparse_rowids_fail_before_copying(self):
        with duckdb.connect(':memory:') as c:
            c.execute('CREATE TABLE t(v INTEGER)')
            c.execute('INSERT INTO t VALUES (1),(2),(3)')
            c.execute('DELETE FROM t WHERE v=2')
            with self.assertRaisesRegex(ValueError,'gaps'):
                inventory(c)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Preparation safety/integrity tests use only disposable synthetic databases."""
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

import duckdb
from build_obfuscated import build, sha256, require_space, inventory, signature, RESERVE, HEADROOM


class PreparationTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'source.duckdb'
        with duckdb.connect(str(source)) as c:
            c.execute('CREATE SCHEMA bird_example')
            c.execute('CREATE TABLE bird_example."Mixed Table" '
                      '("Original ID" BIGINT NOT NULL, "Original Text" VARCHAR, "Original Float" DOUBLE)')
            c.execute('INSERT INTO bird_example."Mixed Table" VALUES '
                      "(1,'café',1.5),(1,'café',1.5),(2,'',NULL),(3,NULL,'NaN'::DOUBLE),(4,'x','Infinity'::DOUBLE)")
            c.execute('CREATE TABLE bird_example.empty_table (secret_name VARCHAR)')
            c.execute('CREATE SCHEMA bird_metadata')
            c.execute('CREATE TABLE bird_metadata.gold (SQL VARCHAR PRIMARY KEY)')
            c.execute("INSERT INTO bird_metadata.gold VALUES ('gold-secret')")
            c.execute('CREATE VIEW bird_metadata.leaky AS SELECT * FROM bird_example."Mixed Table"')
            c.execute("COMMENT ON TABLE bird_example.\"Mixed Table\" IS 'reference-secret'")
        return source

    def test_separate_sanitized_build_and_refusal_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.fixture(root)
            before = sha256(source)
            public, private = root / 'public', root / 'private'
            result = build(source, public, private, {'bird_example': (2, 4, 5)}, before)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(sha256(source), before)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((private / 'mapping.json').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((public / 'bird_dev_obfuscated.duckdb').stat().st_mode), 0o444)
            manifest = (public / 'manifest.json').read_text()
            for secret in ['Original ID', 'Original Text', 'Original Float', 'secret_name', 'gold-secret', 'reference-secret', 'bird_metadata']:
                self.assertNotIn(secret, manifest)
            with duckdb.connect(str(public / 'bird_dev_obfuscated.duckdb'), read_only=True) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM bird_example."Mixed Table" WHERE col_001=1').fetchone()[0], 2)
                self.assertEqual(c.execute('SELECT count(*) FROM bird_example.empty_table').fetchone()[0], 0)
                self.assertEqual(c.execute('SELECT count(*) FROM duckdb_constraints()').fetchone()[0], 0)
                self.assertEqual(c.execute('SELECT count(*) FROM duckdb_views() WHERE NOT internal').fetchone()[0], 0)
            with self.assertRaises(FileExistsError):
                build(source, public, private, {'bird_example': (2, 4, 5)}, before)

    def test_bad_source_hash_prevents_all_artifact_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                build(source, root / 'public', root / 'private', {}, 'invalid')
            self.assertFalse((root / 'public').exists())
            self.assertFalse((root / 'private').exists())

    def test_unapproved_coverage_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'coverage'):
                build(source, root / 'public', root / 'private', {}, sha256(source))
            self.assertFalse((root / 'public').exists())
            self.assertFalse((root / 'private/mapping.json').exists())

    def test_guard_preserves_reserve_and_headroom(self):
        usage = shutil._ntuple_diskusage(20 * 1024**3, 16 * 1024**3, RESERVE + HEADROOM - 1)
        with patch('build_obfuscated.shutil.disk_usage', return_value=usage):
            with self.assertRaisesRegex(RuntimeError, 'reserve'):
                require_space(Path('/tmp'))

    def test_symlink_output_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.fixture(root)
            (root / 'alias').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'Symlinked'):
                build(source, root / 'alias/public', root / 'private', {}, sha256(source))

    def test_mapping_is_deterministic_across_separate_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.fixture(root)
            for run in [1, 2]:
                build(source, root / f'public{run}', root / f'private{run}',
                      {'bird_example': (2, 4, 5)}, sha256(source))
            self.assertEqual((root / 'private1/mapping.json').read_bytes(),
                             (root / 'private2/mapping.json').read_bytes())

    def test_duplicate_sensitive_signature_detects_changed_multiplicity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = self.fixture(root)
            with duckdb.connect(str(source)) as c:
                table = next(t for t in inventory(c) if t['table'] == 'Mixed Table')
                before = signature(c, table, True)
                c.execute('UPDATE bird_example."Mixed Table" SET "Original Text"=\'changed\' '
                          'WHERE rowid=(SELECT min(rowid) FROM bird_example."Mixed Table")')
                after = signature(c, table, True)
                self.assertEqual(before['rows'], after['rows'])
                self.assertNotEqual(before['row_hash_sum'], after['row_hash_sum'])


if __name__ == '__main__':
    unittest.main()

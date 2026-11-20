import io, json, shutil, tempfile, unittest, zipfile
from pathlib import Path
from studio import update

SRC = Path(__file__).resolve().parents[1]


class UpdateBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = self.tmp / 'install'; self.target.mkdir()
        (self.target / 'studio').mkdir(); (self.target / 'studio' / 'old_only.py').write_text('x=1\n')

    def test_roundtrip_stages_exact_files(self):
        m, data = update.make_bundle(SRC)
        self.assertRegex(m['version'], r'^[0-9a-f]{12}$')
        self.assertIn('tools/Apply-Update.ps1', m['files'])
        self.assertFalse(any(r.startswith(('python/', 'production-config', 'runtime')) for r in m['files']))
        r = update.stage(self.target, data, 'oldversion00')
        self.assertEqual(r['version'], m['version'])
        staged = self.target / 'updates' / 'staging' / 'files'
        files = {p.relative_to(staged).as_posix() for p in staged.rglob('*') if p.is_file()}
        self.assertEqual(files, set(m['files']))
        self.assertEqual(update.version(staged), m['version'])      # 풀린 파일만으로 같은 버전
        cur = (self.target / 'updates' / 'staging' / 'current-files.txt').read_text().split()
        self.assertEqual(cur, ['studio/old_only.py'])
        self.assertEqual(update.last_status(self.target)['status'], 'staged')
        self.assertTrue(update.busy_updating(self.target))

    def _rebuild(self, data, mutate):
        src = zipfile.ZipFile(io.BytesIO(data)); m = json.loads(src.read('manifest.json'))
        entries = {n: src.read(n) for n in src.namelist() if n != 'manifest.json'}
        mutate(m, entries)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('manifest.json', json.dumps(m))
            for n, b in entries.items(): z.writestr(n, b)
        return buf.getvalue()

    def test_rejects_tampered_file(self):
        _, data = update.make_bundle(SRC)
        bad = self._rebuild(data, lambda m, e: e.__setitem__('files/studio/app.py', b'import os\n'))
        with self.assertRaisesRegex(ValueError, '손상'): update.stage(self.target, bad, 'x')
        self.assertFalse((self.target / 'studio' / 'app.py').exists())

    def test_rejects_paths_outside_source(self):
        _, data = update.make_bundle(SRC)
        for rel in ('../evil.py', 'production-config/export.json', 'python/python.exe', 'C:/x.py', 'studio\\x.py'):
            def mutate(m, e, rel=rel):
                m['files'][rel] = __import__('hashlib').sha256(b'x').hexdigest(); m['version'] = update.version_of(m['files'])
                e['files/' + rel] = b'x'
            with self.assertRaises(ValueError, msg=rel): update.stage(self.target, self._rebuild(data, mutate), 'x')

    def test_rejects_wrong_version(self):
        _, data = update.make_bundle(SRC)
        bad = self._rebuild(data, lambda m, e: m.__setitem__('version', 'abcdefabcdef'))
        with self.assertRaisesRegex(ValueError, '버전'): update.stage(self.target, bad, 'x')


class StationNameTest(unittest.TestCase):
    def test_station_must_be_ascii_identifier(self):
        from studio.core import load_config
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, True)
        base = dict(role='export', port=18766, data_dir=str(tmp / 'd'), worker_password='w' * 12, admin_password='a' * 12, transfer_token='t' * 12)
        for bad in ('수집PC_1', 'capture 01', ''):
            (tmp / 'c.json').write_text(json.dumps(dict(base, station=bad)), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'station', msg=bad): load_config(tmp / 'c.json')
        (tmp / 'c.json').write_text(json.dumps(dict(base, station='capture-01')), encoding='utf-8')
        self.assertEqual(load_config(tmp / 'c.json')['station'], 'capture-01')


if __name__ == '__main__':
    unittest.main()

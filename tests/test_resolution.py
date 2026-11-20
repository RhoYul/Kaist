import json, shutil, tempfile, unittest
from pathlib import Path
from studio.core import DEFAULTS, Store, load_config
from studio.capture import Capture


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg_file(self, **kw):
        base = dict(role='capture', port=18765, data_dir=str(self.tmp / 'd'), worker_password='w' * 12, admin_password='a' * 12, transfer_token='t' * 12)
        f = self.tmp / 'c.json'; f.write_text(json.dumps(dict(base, **kw)), encoding='utf-8'); return f

    def test_default_is_848x480_for_mano(self):
        cfg = load_config(self.cfg_file())
        self.assertEqual((cfg['capture_width'], cfg['capture_height'], cfg['capture_fps']), (848, 480, 30))

    def test_rejects_unsupported_resolution(self):
        with self.assertRaisesRegex(ValueError, 'capture_width'): load_config(self.cfg_file(capture_width=800, capture_height=600))
        with self.assertRaisesRegex(ValueError, 'capture_fps'): load_config(self.cfg_file(capture_fps=60))
        self.assertEqual(load_config(self.cfg_file(capture_width=640))['capture_width'], 640)

    def test_old_calibration_must_be_redone(self):
        cfg = dict(DEFAULTS, role='capture', demo=False, data_dir=str(self.tmp / 'cap'))
        cap = Capture(cfg, Store(str(self.tmp / 'cap')))
        intr = lambda w, h: {c: dict(fx=1, fy=1, cx=1, cy=1, width=w, height=h) for c in ('cam_1', 'cam_2')}
        self.assertEqual(cap.calibration_mismatch(dict(intrinsics=intr(640, 480))), '640x480')
        self.assertIsNone(cap.calibration_mismatch(dict(intrinsics=intr(848, 480))))
        self.assertEqual(cap.calibration_mismatch(dict(confirmed=True)), '640x480')      # 해상도 기록 없는 예전 결과
        self.assertIsNone(cap.calibration_mismatch(dict(resolution='848x480')))


if __name__ == '__main__':
    unittest.main()

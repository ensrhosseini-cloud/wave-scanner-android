import json
import tempfile
import threading
import unittest
from pathlib import Path
from dataclasses import asdict
from futures_engine import Config, LABELS
from mobile_core import ScanController, config_from_fields, parse_number, report_payload, save_report


def report():
    return dict(config=asdict(Config()), market='KuCoin Futures', started_at='start',
                finished_at='end', coverage_complete_for_eligible=False, cancelled=True,
                counts={}, sources={}, groups={k: [] for k in LABELS}, rejection_counts={},
                rejected=[], errors=[], excluded=[], candidates=[])


class MobileTests(unittest.TestCase):
    def test_persian_number(self):
        self.assertEqual(parse_number('۱٬۰۰۰٫۵'), 1000.5)
        self.assertEqual(parse_number('٣'), 3)

    def test_config(self):
        values = dict(min_turnover='۱۰۰۰۰۰۰', target_pct='۳', min_rr='۱٫۵',
                      volume_factor='۱٫۲', spread_pct='۰٫۳')
        self.assertEqual(config_from_fields(values).target_return, .03)
        values['target_pct'] = 'nan'
        with self.assertRaises(ValueError):
            config_from_fields(values)

    def test_report_export(self):
        r = report()
        self.assertEqual(json.loads(report_payload(r, 'json'))['timeframes']['signal'], '1h')
        self.assertIn('۱ساعته', report_payload(r, 'txt'))
        with tempfile.TemporaryDirectory() as folder:
            paths = save_report(r, folder)
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(p.exists() for p in paths))
            self.assertFalse(list(Path(folder).glob('*.tmp')))

    def test_cancel_and_no_concurrent_scan(self):
        started = threading.Event()
        def scanner(cfg, stop, emit):
            started.set()
            emit('log', 'شروع')
            stop.wait(2)
            return report()
        ctl = ScanController(scanner)
        self.assertTrue(ctl.start(Config()))
        self.assertTrue(started.wait(1))
        self.assertFalse(ctl.start(Config()))
        ctl.cancel()
        ctl.thread.join(2)
        self.assertFalse(ctl.running)
        self.assertTrue(ctl.report['cancelled'])
        events = list(ctl.events.queue)
        self.assertEqual(events[-1][0], 'done')

    def test_scanner_exception(self):
        def broken(*args):
            raise RuntimeError('network test')
        ctl = ScanController(broken)
        ctl.start(Config())
        ctl.thread.join(2)
        self.assertEqual(ctl.events.get_nowait(), ('error', 'network test'))
        self.assertIsNone(ctl.report)


if __name__ == '__main__':
    unittest.main()

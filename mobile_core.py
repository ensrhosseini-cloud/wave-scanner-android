"""UI-independent Android scan lifecycle and UTF-8 report storage."""
import json
import queue
import threading
from dataclasses import asdict
from pathlib import Path

from futures_engine import Config, scan, report_text

TIMEFRAMES = 'تایم‌فریم سیگنال: ۱ساعته | تأیید روند: ۴ساعته'
DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬', '01234567890123456789.,')


def parse_number(text):
    return float(str(text).translate(DIGITS).replace(',', '').strip())


def config_from_fields(values, direction='both'):
    return Config(min_turnover=parse_number(values['min_turnover']),
                  target_return=parse_number(values['target_pct']) / 100,
                  min_rr=parse_number(values['min_rr']),
                  volume_factor=parse_number(values['volume_factor']),
                  max_spread=parse_number(values['spread_pct']) / 100,
                  direction=direction)


def report_payload(report, kind):
    if kind == 'json':
        data = dict(report, timeframes={'signal': '1h', 'trend': '4h', 'closed_candles_only': True})
        return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    if kind == 'txt':
        return report_text(report)
    raise ValueError('Unsupported export type')


def save_report(report, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind in ('txt', 'json'):
        path = folder / ('latest_report.' + kind)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(report_payload(report, kind), encoding='utf-8')
        temporary.replace(path)
        paths.append(path)
    return paths


class ScanController:
    def __init__(self, scanner=scan):
        self.scanner = scanner
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.report = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, config):
        if self.running:
            return False
        # Remove events from an earlier completed run before starting another.
        while not self.events.empty():
            self.events.get_nowait()
        self.stop_event = threading.Event()
        self.report = None
        self.thread = threading.Thread(target=self._run, args=(config,), daemon=True)
        self.thread.start()
        return True

    def _run(self, config):
        try:
            report = self.scanner(config, self.stop_event,
                                  lambda kind, data: self.events.put((kind, data)))
            self.report = report
            self.events.put(('done', report))
        except Exception as exc:
            self.events.put(('error', str(exc)))

    def cancel(self):
        self.stop_event.set()

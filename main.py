"""Android-first Kivy interface. No trading credentials or order endpoints."""
import json
import queue
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.metrics import dp
from kivy.properties import ObjectProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.utils import platform

from futures_engine import Config, LABELS, price
from mobile_core import ScanController, TIMEFRAMES, config_from_fields, report_payload, save_report

LabelBase.register(name='Persian', fn_regular=str(Path(__file__).parent / 'assets/DejaVuSans.ttf'))


def rtl(text):
    return '\n'.join(get_display(arabic_reshaper.reshape(line)) for line in str(text).split('\n'))


def label(text, height=42, **kwargs):
    widget = Label(text=rtl(text), font_name='Persian', font_size=dp(14),
                   size_hint_y=None, height=dp(height), halign='right', valign='middle', **kwargs)
    widget.bind(size=lambda obj, size: setattr(obj, 'text_size', (size[0] - dp(12), size[1])))
    return widget


def button(text, callback, height=46):
    widget = Button(text=rtl(text), font_name='Persian', font_size=dp(14),
                    size_hint_y=None, height=dp(height), background_normal='',
                    background_color=(.07, .39, .43, 1))
    widget.bind(on_release=callback)
    return widget


class ResultButton(Button):
    """Recycled rows keep the candidate as data, not an event handler."""
    candidate = ObjectProperty(None, allownone=True)

    def on_release(self):
        if self.candidate is not None:
            App.get_running_app().show_candidate(self.candidate)


class WaveScannerApp(App):
    title = 'WaveScanner Futures'

    def build(self):
        self.controller = ScanController()
        self.report = None
        self.active_group = 'long_active'
        self.pending_export = None
        self.root_box = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(5))
        self.root_box.add_widget(label('رادار فیوچرز KuCoin', 36, bold=True))
        self.root_box.add_widget(label('سیگنال: ۱ساعته | روند: ۴ساعته', 36, color=(.3, 1, .8, 1)))
        self.root_box.add_widget(label('فقط کندل بسته‌شده؛ بدون سفارش و کلید API', 32))
        self.body = BoxLayout(orientation='vertical', spacing=dp(5))
        self.root_box.add_widget(self.body)
        self.inputs = {}
        defaults = {'min_turnover': '1000000', 'target_pct': '3', 'min_rr': '1.5',
                    'volume_factor': '1.2', 'spread_pct': '0.3', 'direction': 'both'}
        try:
            saved = json.loads((Path(self.user_data_dir) / 'settings.json').read_text('utf-8'))
            if isinstance(saved, dict):
                defaults.update({k: str(v) for k, v in saved.items() if k in defaults})
        except (OSError, ValueError):
            pass
        self.saved = defaults
        self.group_buttons = {}
        self.show_scan()
        Clock.schedule_interval(self.poll, .15)
        if platform == 'android':
            from android import activity
            activity.bind(on_activity_result=self.on_activity_result)
        return self.root_box

    def show_scan(self, *_):
        self.body.clear_widgets()
        controls = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(5))
        self.start_button = button('شروع اسکن', self.start_scan)
        self.stop_button = button('توقف', self.cancel_scan)
        self.stop_button.disabled = True
        self.settings_button = button('تنظیمات', self.show_settings)
        for item in (self.start_button, self.stop_button, self.settings_button):
            controls.add_widget(item)
        self.body.add_widget(controls)
        exports = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(5))
        for kind in ('TXT', 'JSON'):
            exports.add_widget(button('ذخیره ' + kind, lambda _, k=kind.lower(): self.export(k), 42))
        self.body.add_widget(exports)
        self.status_label = label('برای بررسی بازار، شروع اسکن را بزنید.', 50)
        self.body.add_widget(self.status_label)
        self.progress = ProgressBar(max=1, value=0, size_hint_y=None, height=dp(8))
        self.body.add_widget(self.progress)
        self.body.add_widget(label('فعال ≠ دستور ورود؛ آماده ≠ تأیید ورود', 36, color=(1, .8, .3, 1)))
        self.group_buttons = {}
        keys = list(LABELS)
        for pair in (keys[:2], keys[2:]):
            row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(4))
            for key in pair:
                b = button(LABELS[key], lambda _, k=key: self.select_group(k), 42)
                self.group_buttons[key] = b
                row.add_widget(b)
            self.body.add_widget(row)
        self.results = RecycleView()
        self.results.viewclass = ResultButton
        layout = RecycleBoxLayout(default_size=(None, dp(116)), default_size_hint=(1, None),
                                  size_hint_y=None, orientation='vertical', spacing=dp(5))
        layout.bind(minimum_height=layout.setter('height'))
        self.results.add_widget(layout)
        self.body.add_widget(self.results)
        if self.report is not None:
            self.render_report(self.report)

    def show_settings(self, *_):
        if self.controller.running:
            return
        self.body.clear_widgets()
        scroll = ScrollView()
        form = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(7))
        form.bind(minimum_height=form.setter('height'))
        fields = [('min_turnover', 'حداقل گردش ۲۴ساعته (USDT)'),
                  ('target_pct', 'هدف حرکت قیمت ٪؛ نه سود اهرمی'), ('min_rr', 'حداقل R/R پس از کارمزد'),
                  ('volume_factor', 'ضریب تأیید حجم'), ('spread_pct', 'حداکثر اسپرد ٪')]
        self.inputs = {}
        for key, text in fields:
            form.add_widget(label(text, 34))
            entry = TextInput(text=self.saved[key], multiline=False, font_name='Persian',
                              size_hint_y=None, height=dp(46), input_type='number')
            self.inputs[key] = entry
            form.add_widget(entry)
        choices = {'both': 'هر دو جهت', 'long': 'فقط لانگ', 'short': 'فقط شورت'}
        self.direction_map = {rtl(v): k for k, v in choices.items()}
        self.direction = Spinner(text=rtl(choices.get(self.saved['direction'], choices['both'])),
                                 values=list(self.direction_map), font_name='Persian',
                                 size_hint_y=None, height=dp(48))
        form.add_widget(self.direction)
        form.add_widget(label(TIMEFRAMES + '\nتایم‌فریم‌ها در این نسخه ثابت‌اند.', 64))
        form.add_widget(label('اسکن در پس‌زمینه درخواست توقف می‌گیرد.\nاین قواعد بک‌تست سودآوری نشده‌اند.\nفاندینگ و لغزش در R/R مدل نشده‌اند.', 100))
        form.add_widget(button('ذخیره و بازگشت', self.save_settings))
        form.add_widget(button('بازگشت بدون ذخیره', self.show_scan))
        scroll.add_widget(form)
        self.body.add_widget(scroll)

    def save_settings(self, *_):
        values = {key: entry.text for key, entry in self.inputs.items()}
        direction = self.direction_map[self.direction.text]
        try:
            config_from_fields(values, direction)
            values['direction'] = direction
            path = Path(self.user_data_dir) / 'settings.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
            self.saved = values
            self.show_scan()
        except (ValueError, OSError) as exc:
            self.message('تنظیمات ذخیره نشد: ' + str(exc))

    def start_scan(self, *_):
        try:
            cfg = config_from_fields(self.saved, self.saved['direction'])
        except ValueError as exc:
            self.message(str(exc))
            return
        if self.controller.start(cfg):
            self.report = None
            self.results.data = []
            for key, b in self.group_buttons.items():
                b.text = rtl(LABELS[key])
            self.set_running(True)
            self.progress.value = 0
            self.status_label.text = rtl('دریافت اطلاعات عمومی فیوچرز…')

    def set_running(self, running):
        self.start_button.disabled = running
        self.settings_button.disabled = running
        self.stop_button.disabled = not running

    def cancel_scan(self, *_):
        self.controller.cancel()
        self.status_label.text = rtl('درخواست توقف؛ منتظر پایان درخواست جاری…')

    def poll(self, _):
        for _ in range(200):
            try:
                kind, data = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                done, total, symbol = data
                self.progress.max = max(1, total)
                self.progress.value = done
                self.status_label.text = rtl(f'{done} / {total} — {symbol}')
            elif kind == 'log':
                self.status_label.text = rtl(data)
            elif kind == 'done':
                self.set_running(False)
                self.report = data
                self.render_report(data)
                try:
                    save_report(data, self.user_data_dir)
                except (OSError, ValueError) as exc:
                    self.message('ذخیرهٔ داخلی ناموفق: ' + str(exc))
            elif kind == 'error':
                self.set_running(False)
                self.status_label.text = rtl('اسکن ناموفق؛ نتیجهٔ کامل تولید نشد.')
                self.message('خطای دریافت یا تحلیل: ' + data)

    def render_report(self, report):
        for key, b in self.group_buttons.items():
            b.text = rtl(f"{LABELS[key]} ({len(report['groups'][key])})")
        coverage = 'پوشش کامل' if report['coverage_complete_for_eligible'] else 'پوشش ناقص؛ موارد نامعلوم وجود دارد'
        state = 'متوقف‌شده' if report['cancelled'] else 'پایان اسکن'
        self.status_label.text = rtl(f"{state} | {coverage}\nارزیابی‌شده: {report['counts'].get('evaluated', 0)}")
        self.select_group(self.active_group)

    def select_group(self, key):
        self.active_group = key
        for group, b in self.group_buttons.items():
            b.background_color = (.1, .6, .5, 1) if group == key else (.07, .39, .43, 1)
        rows = [] if self.report is None else self.report['groups'][key]
        if not rows:
            self.results.data = [dict(text=rtl('اسکن نشده یا مورد واجد شرایطی در این گروه نیست.'),
                                      font_name='Persian', font_size=dp(13), candidate=None)]
            return
        self.results.data = [dict(text=rtl(f"{c['symbol']} | {LABELS[key]}\nسیگنال ۱ساعته | روند ۴ساعته\nورود: {price(c['entry'])} | هدف: {price(c['target_price'])}\nR/R: {c['net_rr']:.2f} | جزئیات: لمس کنید"),
                                  font_name='Persian', font_size=dp(13),
                                  candidate=c) for c in rows]

    def show_candidate(self, c):
        funding = 'نامعلوم' if c['funding_rate'] is None else f"{c['funding_rate'] * 100:g}%"
        text = '\n'.join([c['symbol'] + ' — ' + LABELS[c['group']], TIMEFRAMES,
                          'نوع الگو: ' + c['pattern'], 'ورود مرجع/شرطی: ' + price(c['entry']),
                          'ماشه: ' + price(c['trigger']), 'ابطال: ' + price(c['stop']),
                          'هدف: ' + price(c['target_price']), f"R/R پس از کارمزد: {c['net_rr']:.2f}",
                          f"نسبت حجم: {c['volume_ratio']:.2f} | اسپرد: {c['spread_pct']:.4f}%",
                          'فاندینگ ثبت‌شده: ' + funding, c['reason'], c['condition'],
                          'بسته‌شدن ۱ساعته UTC: ' + c['h1_closed_at'],
                          'بسته‌شدن ۴ساعته UTC: ' + c['h4_closed_at'], 'مظنه UTC: ' + c['quote_at'],
                          'هدف، تضمین سود یا زمان وصول نیست.'])
        self.message(text)

    def message(self, text):
        box = BoxLayout(orientation='vertical', spacing=dp(6))
        scroll = ScrollView()
        content = label(text, 50)
        content.bind(width=lambda obj, width: setattr(obj, 'text_size', (width - dp(16), None)))
        content.bind(texture_size=lambda obj, size: setattr(obj, 'height', size[1] + dp(24)))
        scroll.add_widget(content)
        box.add_widget(scroll)
        popup = Popup(title='WaveScanner', content=box, size_hint=(.96, .85))
        box.add_widget(button('بستن', lambda *_: popup.dismiss()))
        popup.open()

    def export(self, kind):
        if self.report is None:
            self.message('ابتدا یک اسکن انجام دهید.')
            return
        content = report_payload(self.report, kind)
        if platform != 'android':
            try:
                paths = save_report(self.report, self.user_data_dir)
                self.message('ذخیره شد:\n' + '\n'.join(map(str, paths)))
            except OSError as exc:
                self.message(str(exc))
            return
        if self.pending_export is not None:
            return
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            intent = Intent(Intent.ACTION_CREATE_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType('application/json' if kind == 'json' else 'text/plain')
            intent.putExtra(Intent.EXTRA_TITLE, 'WaveScanner_report.' + kind)
            self.pending_export = content
            autoclass('org.kivy.android.PythonActivity').mActivity.startActivityForResult(intent, 2401)
        except Exception as exc:
            self.pending_export = None
            self.message('باز کردن ذخیره‌ساز ناموفق: ' + str(exc))

    def on_activity_result(self, request_code, result_code, intent):
        if request_code != 2401:
            return
        content, self.pending_export = self.pending_export, None
        if result_code != -1 or intent is None or content is None:
            return
        try:
            from jnius import autoclass
            activity = autoclass('org.kivy.android.PythonActivity').mActivity
            stream = activity.getContentResolver().openOutputStream(intent.getData())
            if stream is None:
                raise OSError('Could not open output document')
            try:
                stream.write(autoclass('java.lang.String')(content).getBytes('UTF-8'))
                stream.flush()
            finally:
                stream.close()
            Clock.schedule_once(lambda _: self.message('گزارش ذخیره شد.'))
        except Exception as exc:
            message = 'ذخیره ناموفق: ' + str(exc)
            Clock.schedule_once(lambda _, text=message: self.message(text))

    def on_pause(self):
        self.controller.cancel()
        return True

    def on_stop(self):
        self.controller.cancel()
        if platform == 'android':
            from android import activity
            activity.unbind(on_activity_result=self.on_activity_result)


if __name__ == '__main__':
    WaveScannerApp().run()

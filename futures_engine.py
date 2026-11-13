"""KuCoin USDT perpetual radar. Public futures data only; no order endpoints.
Legacy spot engine.py/shorts.py are deliberately not imported.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math
import threading
import time

import numpy as np
import pandas as pd
import requests

BASE = 'https://api-futures.kucoin.com'
SOURCES = {name: BASE + path for name, path in {
    'contracts': '/api/v1/contracts/active', 'candles': '/api/v1/kline/query',
    'quote': '/api/v1/ticker', 'clock': '/api/v1/timestamp',
}.items()}
LABELS = {'long_active': 'لانگ فعال', 'long_ready': 'آمادهٔ لانگ',
          'short_active': 'شورت فعال', 'short_ready': 'آمادهٔ شورت'}


class ScanError(Exception):
    pass


class DataError(ScanError):
    pass


class Cancelled(ScanError):
    pass


@dataclass
class Config:
    min_turnover: float = 1_000_000
    target_return: float = .03
    min_rr: float = 1.5
    volume_factor: float = 1.2
    max_spread: float = .003
    direction: str = 'both'
    request_pause: float = .25

    def __post_init__(self):
        for name in ('min_turnover', 'target_return', 'min_rr', 'volume_factor', 'max_spread', 'request_pause'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('تنظیمات باید اعداد متناهی باشند.')
        if self.direction not in ('both', 'long', 'short'):
            raise ValueError('جهت اسکن نامعتبر است.')
        if not (0 <= self.min_turnover <= 1e15 and .001 <= self.target_return < 1 and
                .1 <= self.min_rr <= 20 and .1 <= self.volume_factor <= 10 and
                0 < self.max_spread <= .05 and .05 <= self.request_pause <= 10):
            raise ValueError('گردش نامنفی؛ هدف ۰٫۱ تا کمتر از ۱۰۰٪؛ R/R از ۰٫۱ تا ۲۰؛ حجم ۰٫۱ تا ۱۰؛ اسپرد تا ۵٪.')


def utc(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat(timespec='seconds')


def price(value):
    return f'{value:.8g}' if value is not None else '—'


def number(value):
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DataError('invalid_numeric_field') from exc
    if not math.isfinite(value):
        raise DataError('nonfinite_numeric_field')
    return value


def eligible_contract(c, cfg):
    """No symbol matching against spot or third-party coin names."""
    if not isinstance(c, dict) or not c.get('symbol'):
        return False, 'invalid_contract'
    if (c.get('status') != 'Open' or c.get('quoteCurrency') != 'USDT' or
            c.get('settleCurrency') != 'USDT' or c.get('isInverse') is not False or
            c.get('expireDate') is not None or c.get('type') != 'FFWCSX' or
            c.get('marketType', 'CRYPTO') != 'CRYPTO' or c.get('marketStage', 'NORMAL') != 'NORMAL'):
        return False, 'not_open_usdt_crypto_perpetual'
    try:
        if number(c.get('turnoverOf24h')) < cfg.min_turnover:
            return False, 'below_turnover'
    except DataError:
        return False, 'invalid_turnover'
    return True, ''


def closed_frame(rows, minutes, now):
    """Futures schema: [milliseconds, open, high, low, close, contracts, turnover].
    Some API versions omit turnover; volume remains contract count, never spot volume.
    """
    if not isinstance(rows, list) or not rows or any(not isinstance(r, (list, tuple)) or len(r) not in (6, 7) for r in rows):
        raise DataError('malformed_futures_candles')
    try:
        d = pd.DataFrame([r[:6] for r in rows], columns=['time', 'open', 'high', 'low', 'close', 'volume']).astype(float)
    except (ValueError, TypeError) as exc:
        raise DataError('malformed_futures_candles') from exc
    if not np.isfinite(d.to_numpy()).all():
        raise DataError('nonfinite_candles')
    d['time'] /= 1000
    seconds = minutes * 60
    d = d[d.time + seconds <= now - 5].sort_values('time').reset_index(drop=True)
    if len(d) < 120:
        raise DataError('insufficient_closed_candles')
    if d.time.duplicated().any() or (d.time.diff().dropna() != seconds).any() or (d.time % seconds != 0).any():
        raise DataError('missing_duplicate_or_unaligned_candles')
    expected = math.floor((now - 5) / seconds) * seconds - seconds
    if d.time.iloc[-1] != expected:
        raise DataError('stale_or_missing_latest_closed_candle')
    if ((d.low <= 0) | (d.volume < 0) | (d.high < d[['open', 'close', 'low']].max(axis=1)) |
            (d.low > d[['open', 'close']].min(axis=1))).any():
        raise DataError('invalid_ohlcv')
    if (d.volume.tail(30) <= 0).any():
        raise DataError('recent_zero_volume')
    d['ema20'] = d.close.ewm(span=20, adjust=False).mean()
    d['ema50'] = d.close.ewm(span=50, adjust=False).mean()
    prev = d.close.shift(1)
    d['atr14'] = pd.concat([d.high - d.low, (d.high - prev).abs(), (d.low - prev).abs()], axis=1).max(axis=1).rolling(14).mean()
    d['volume20_previous'] = d.volume.shift(1).rolling(20).mean()
    return d


def setup(h4, h1, direction, cfg):
    """Directional 4h trend, then 1h breakout or EMA20 pullback. No old wave rules."""
    sign = 1 if direction == 'long' else -1
    trend, bar, prev = h4.iloc[-1], h1.iloc[-1], h1.iloc[-2]
    if not (sign * (trend.close - trend.ema20) > 0 and
            sign * (trend.ema20 - trend.ema50) > 0 and
            sign * (trend.ema20 - h4.ema20.iloc[-4]) > 0):
        return None, 'no_4h_trend'
    atr = number(bar.atr14)
    if atr <= 0 or not (0.001 <= atr / bar.close <= .08):
        return None, 'atr_outside_range'
    if sign * (bar.ema20 - bar.ema50) <= 0:
        return None, 'no_1h_alignment'
    history = h1.iloc[-21:-1]
    trigger = float(history.high.max() if sign == 1 else history.low.min())
    prev_level = float(h1.high.iloc[-22:-2].max() if sign == 1 else h1.low.iloc[-22:-2].min())
    volume_ratio = float(bar.volume / bar.volume20_previous)
    vol_ok = volume_ratio >= cfg.volume_factor
    beyond = sign * (bar.close - trigger)
    crossed = beyond > 0 and sign * (prev.close - prev_level) <= 0
    active_break = crossed and vol_ok and beyond <= .8 * atr
    touched = bool((h1.low.iloc[-3:] <= h1.ema20.iloc[-3:] + .2 * atr).any() if sign == 1 else
                   (h1.high.iloc[-3:] >= h1.ema20.iloc[-3:] - .2 * atr).any())
    resumed = (sign * (bar.close - prev.close) > 0 and sign * (bar.close - bar.open) > 0 and
               sign * (bar.close - bar.ema20) > 0 and abs(bar.close - bar.ema20) <= atr)
    held = sign * (bar.close - bar.ema50) > 0
    active_pull = touched and resumed and held and vol_ok
    ready_break = -.6 * atr <= beyond <= 0
    ready_pull = held and abs(bar.close - bar.ema20) <= .6 * atr and not active_pull
    # A breakout without volume is never silently promoted to active via the pullback path.
    if beyond > 0 and not vol_ok:
        active_pull = False
    if active_break:
        state, pattern, level = 'active', 'breakout', trigger
        reason = 'شکست ۲۰ کندل قبل با حجم تأیید شد'
    elif active_pull:
        state, pattern, level = 'active', 'pullback', float(bar.ema20)
        reason = 'تماس با EMA20 و برگشت هم‌جهت با حجم تأیید شد'
    elif ready_break:
        state, pattern, level = 'ready', 'breakout', trigger
        reason = 'تا شکست ۲۰ کندل فاصله کم است؛ منتظر بسته‌شدن کندل و تأیید حجم'
    elif ready_pull:
        state, pattern, level = 'ready', 'pullback', float(bar.high if sign == 1 else bar.low)
        reason = 'قیمت نزدیک EMA20 است؛ منتظر عبور از ماشه با کندل هم‌جهت و حجم'
    else:
        return None, 'no_recent_trigger_or_near_setup'
    stop = float(h1.low.iloc[-5:].min() - .25 * atr if sign == 1 else h1.high.iloc[-5:].max() + .25 * atr)
    return {'direction': direction, 'state': state, 'pattern': pattern, 'trigger': level,
            'stop': stop, 'atr': atr, 'close': float(bar.close), 'volume_ratio': volume_ratio,
            'h1_closed_at': utc(float(bar.time) + 3600),
            'h4_closed_at': utc(float(trend.time) + 14400), 'reason': reason}, ''


def finalize(s, quote, contract, cfg, now):
    if not isinstance(quote, dict) or quote.get('symbol') != contract['symbol']:
        return None, 'quote_symbol_mismatch'
    try:
        bid, ask = number(quote.get('bestBidPrice')), number(quote.get('bestAskPrice'))
        stamp = number(quote.get('ts')) / 1e9  # Futures ticker timestamp is nanoseconds.
    except DataError:
        return None, 'invalid_quote'
    if not (0 < bid <= ask) or not (-5 <= now - stamp <= 30):
        return None, 'stale_or_crossed_quote'
    spread = (ask - bid) / ((ask + bid) / 2)
    if spread > cfg.max_spread:
        return None, 'spread_too_wide'
    sign = 1 if s['direction'] == 'long' else -1
    live = ask if sign == 1 else bid
    if sign * (live - s['stop']) <= 0:
        return None, 'stop_already_breached'
    if abs(live - s['close']) > .8 * s['atr']:
        return None, 'price_moved_since_closed_signal'
    if s['state'] == 'active' and sign * (live - s['trigger']) <= 0:
        return None, 'active_trigger_lost'
    entry = live if s['state'] == 'active' else (max(ask, s['trigger'] + .1 * s['atr']) if sign == 1 else min(bid, s['trigger'] - .1 * s['atr']))
    risk = sign * (entry - s['stop'])
    if entry <= 0 or not .0025 <= risk / entry <= .08:
        return None, 'stop_distance_outside_range'
    target = entry * (1 + sign * cfg.target_return)
    try:
        # Actual public contract fee, conservative fallback. No VIP assumptions.
        fee = max(0, number(contract.get('takerFeeRate', .0006)))
    except DataError:
        return None, 'invalid_contract_fee'
    net_reward = abs(target - entry) - fee * (entry + target)
    net_risk = risk + fee * (entry + s['stop'])
    rr = net_reward / net_risk
    if rr < cfg.min_rr:
        return None, 'net_rr_below_minimum'
    funding = contract.get('fundingFeeRate')
    try:
        funding = number(funding) if funding is not None else None
    except DataError:
        funding = None
    result = dict(s, symbol=contract['symbol'], group=s['direction'] + '_' + s['state'],
                  entry=entry, live_price=live, target_price=target, net_rr=rr,
                  stop_pct=risk / entry * 100, spread_pct=spread * 100,
                  turnover=number(contract['turnoverOf24h']), funding_rate=funding,
                  taker_fee=fee, quote_at=utc(stamp), evaluated_at=utc(now))
    result['score'] = round(min(100, 40 + min(rr, 4) * 10 + min(s['volume_ratio'], 2) * 10), 1)
    result['condition'] = ('تأیید کندلی موجود است؛ تا از دست رفتن ماشه یا حد ابطال معتبر'
                           if s['state'] == 'active' else
                           'صرفاً زیرنظر: بسته‌شدن کندل ۱ساعته آن‌سوی ماشه و حجم حداقل ' + str(cfg.volume_factor) + ' برابر؛ سپس ارزیابی مجدد')
    return result, ''


class Client:
    def __init__(self, stop, pause=.25):
        self.stop, self.pause = stop, pause
        self.session = requests.Session()
        self.last_request = 0.
        self.server_anchor = None
        self.local_anchor = None

    def check(self):
        if self.stop.is_set():
            raise Cancelled('اسکن متوقف شد.')

    def get(self, name, params=None):
        for attempt in range(3):
            self.check()
            if self.stop.wait(max(0, self.pause - (time.monotonic() - self.last_request))):
                self.check()
            self.last_request = time.monotonic()
            try:
                r = self.session.get(SOURCES[name], params=params, timeout=(8, 20))
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.RequestException('temporary_http_' + str(r.status_code))
                r.raise_for_status()
                j = r.json()
                if j.get('code') != '200000' or j.get('data') is None:
                    raise DataError('exchange_api_error_' + str(j.get('code')))
                self.check()
                return j['data']
            except (requests.RequestException, ValueError, DataError) as exc:
                if attempt == 2:
                    raise DataError(f'{name}: {type(exc).__name__}: {exc}') from exc
                if self.stop.wait(1.5 * (attempt + 1)):
                    self.check()
        raise DataError('request_failed')

    def sync(self):
        start = time.monotonic()
        server = number(self.get('clock')) / 1000
        end = time.monotonic()
        if end - start > 10:
            raise DataError('server_clock_roundtrip_too_long')
        self.server_anchor, self.local_anchor = server, (start + end) / 2

    def now(self):
        if self.server_anchor is None:
            raise DataError('clock_not_synchronized')
        return self.server_anchor + time.monotonic() - self.local_anchor

    def candles(self, symbol, minutes):
        now = self.now()
        seconds = minutes * 60
        # Keep below 200 rows: the live endpoint can truncate larger ranges
        # from the oldest side, silently omitting the most recent candles.
        rows = self.get('candles', {'symbol': symbol, 'granularity': minutes,
                                  'from': int((now - 185 * seconds) * 1000), 'to': int(now * 1000)})
        return closed_frame(rows, minutes, now)


def scan(cfg, stop, emit, client=None):
    own_client = client is None
    client = client or Client(stop, cfg.request_pause)
    report = {'version': '2.0', 'market': 'KuCoin Futures — linear USDT crypto perpetuals',
              'started_at': utc(), 'config': asdict(cfg), 'sources': SOURCES.copy(),
              'candidates': [], 'rejected': [], 'errors': [], 'excluded': [],
              'groups': {key: [] for key in LABELS}, 'cancelled': False,
              'coverage_complete_for_eligible': False}
    counts = Counter()
    try:
        emit('log', 'همگام‌سازی ساعت و دریافت قراردادهای فیوچرز؛ بدون داده اسپات…')
        client.sync()
        contracts = client.get('contracts')
        if not isinstance(contracts, list) or not contracts:
            raise DataError('empty_or_invalid_contract_list')
        report['universe_at'] = utc(client.now())
        eligible, seen = [], set()
        for c in contracts:
            ok, reason = eligible_contract(c, cfg)
            symbol = c.get('symbol', '?') if isinstance(c, dict) else '?'
            if symbol in seen:
                raise DataError('duplicate_contract_symbol')
            seen.add(symbol)
            if ok:
                eligible.append(c)
            else:
                report['excluded'].append({'symbol': symbol, 'reason': reason})
        # Liquidity order only; no slice, top-N, or result quota.
        eligible.sort(key=lambda c: number(c['turnoverOf24h']), reverse=True)
        counts['all_contracts'], counts['eligible_contracts'] = len(contracts), len(eligible)
        directions = ('long', 'short') if cfg.direction == 'both' else (cfg.direction,)
        for index, c in enumerate(eligible, 1):
            symbol = c['symbol']
            client.check()
            emit('progress', (index - 1, len(eligible), symbol))
            counts['attempted'] += 1
            try:
                # Resync periodically; a long full-market scan is not a single snapshot.
                if index > 1 and index % 30 == 0:
                    client.sync()
                h4 = client.candles(symbol, 240)
                h1 = client.candles(symbol, 60)
                counts['valid_data'] += 1
                setups = []
                for direction in directions:
                    s, reason = setup(h4, h1, direction, cfg)
                    if s is not None:
                        setups.append(s)
                    else:
                        report['rejected'].append({'symbol': symbol, 'direction': direction, 'reason': reason})
                if setups:
                    quote = client.get('quote', {'symbol': symbol})
                    now = client.now()
                    # No promotion from an old candle if an hour boundary passed during requests.
                    if h1.time.iloc[-1] != math.floor((now - 5) / 3600) * 3600 - 3600 or h4.time.iloc[-1] != math.floor((now - 5) / 14400) * 14400 - 14400:
                        raise DataError('candle_boundary_changed_retry_next_scan')
                    for s in setups:
                        result, reason = finalize(s, quote, c, cfg, now)
                        if result:
                            report['candidates'].append(result)
                            emit('log', symbol + ' — ' + LABELS[result['group']])
                        else:
                            report['rejected'].append({'symbol': symbol, 'direction': s['direction'], 'reason': reason})
                counts['evaluated'] += 1
            except DataError as exc:
                report['errors'].append({'symbol': symbol, 'reason': str(exc)})
                emit('log', symbol + ' — داده نامعلوم: ' + str(exc))
            emit('progress', (index, len(eligible), symbol))
        report['coverage_complete_for_eligible'] = not report['errors']
    except Cancelled:
        report['cancelled'] = True
    finally:
        if own_client:
            client.session.close()
    report['finished_at'] = utc()
    report['counts'] = dict(counts)
    report['rejection_counts'] = dict(Counter(x['reason'] for x in report['rejected']))
    report['candidates'].sort(key=lambda x: (x['group'], -x['score'], x['symbol']))
    for c in report['candidates']:
        report['groups'][c['group']].append(c)
    return report


REASONS = {
    'no_4h_trend': 'روند ۴ساعته هم‌جهت تأیید نیست',
    'atr_outside_range': 'نوسان ATR خارج از محدودهٔ ۰٫۱ تا ۸٪ است',
    'no_1h_alignment': 'میانگین‌های ۱ساعته هم‌جهت نیستند',
    'no_recent_trigger_or_near_setup': 'شکست/برگشت تازه یا نزدیکی کافی به ماشه وجود ندارد',
    'quote_symbol_mismatch': 'نماد مظنه با قرارداد تطبیق ندارد',
    'invalid_quote': 'مظنه معتبر نیست', 'stale_or_crossed_quote': 'مظنه کهنه یا نامعتبر است',
    'spread_too_wide': 'اسپرد بیش از سقف مجاز است',
    'stop_already_breached': 'حد ابطال قبلاً شکسته شده است',
    'price_moved_since_closed_signal': 'قیمت زنده از سیگنال کندلی فاصله گرفته است',
    'active_trigger_lost': 'ماشهٔ وضعیت فعال در قیمت زنده از دست رفته است',
    'stop_distance_outside_range': 'فاصلهٔ حد ابطال خارج از ۰٫۲۵ تا ۸٪ است',
    'invalid_contract_fee': 'کارمزد قرارداد نامعتبر است',
    'net_rr_below_minimum': 'نسبت هدف به ریسک پس از کارمزد کافی نیست',
}


def report_text(r):
    cfg = r['config']
    lines = ['رادار فیوچرز KuCoin — نسخه ۲٫۰', r['market'],
             'تایم‌فریم سیگنال: ۱ساعته | تأیید روند: ۴ساعته | فقط کندل بسته‌شده',
             'شروع UTC: ' + r['started_at'], 'پایان UTC: ' + r['finished_at'],
             'پوشش: ' + ('کامل برای قراردادهای واجد فیلتر' if r['coverage_complete_for_eligible'] else 'ناقص؛ بخش بررسی‌نشده/خطادار نامعلوم است'),
             'وضعیت توقف: ' + ('متوقف‌شده' if r['cancelled'] else 'پایان‌یافته'),
             f"هدف حرکت قیمت بدون اهرم: {cfg['target_return'] * 100:g}٪؛ بدون پیش‌بینی زمان وصول",
             'بدون سقف تعداد خروجی؛ فیلتر نقدشوندگی و کیفیت همچنان برقرار است.',
             'فعال = تأیید قواعد کندلی؛ نه سفارش یا توصیهٔ قطعی ورود. آماده = منتظر تأیید و ارزیابی مجدد.',
             'قیمت آماده، ورود شرطی/برآوردی است؛ قیمت فعال مظنهٔ همان زمان بررسی است.',
             'اسکن ترتیبی است؛ نتایج هم‌زمان نیستند و باید پیش از اقدام دوباره بررسی شوند.',
             'R/R پس از کارمزد تیکر ورودی/خروجی؛ فاندینگ، لغزش، عمق سفارش و لیکوییدشدن مدل نشده‌اند.',
             'هدف فقط سناریوی قیمت است؛ سود حساب، اهرم و تضمین کیفیت نیست. قواعد هنوز بک‌تست نشده‌اند.', '',
             'آمار: ' + str(r['counts'])]
    for key, label in LABELS.items():
        group = r['groups'][key]
        lines += ['', f'=== {label} — {len(group)} ===']
        if not group:
            lines.append('در داده‌های بررسی‌شده مورد واجد شرایطی پیدا نشد؛ نه اثبات نبود فرصت در کل بازار.')
        for c in group:
            funding = 'نامعلوم' if c['funding_rate'] is None else f"{c['funding_rate'] * 100:.5g}٪"
            lines += [f"{c['symbol']} | {c['pattern']} | امتیاز قواعد {c['score']} (نه احتمال موفقیت)",
                      f"ورود {'شرطی' if c['state'] == 'ready' else 'مرجع'}: {price(c['entry'])} | ماشه: {price(c['trigger'])} | ابطال: {price(c['stop'])} | هدف: {price(c['target_price'])}",
                      f"R/R خالص کارمزد: {c['net_rr']:.2f} | حجم: {c['volume_ratio']:.2f}x | اسپرد: {c['spread_pct']:.4f}٪ | فاندینگ ثبت‌شده: {funding}",
                      c['reason'], c['condition'],
                      f"تایم‌فریم سیگنال: ۱ساعته | تأیید روند: ۴ساعته | بسته‌شدن ۱ساعته: {c['h1_closed_at']} | بسته‌شدن ۴ساعته: {c['h4_closed_at']} | مظنه: {c['quote_at']}"]
    lines += ['', '=== دلایل رد هر جهت ===']
    for x in r['rejected']:
        lines.append(f"{x['symbol']} / {x['direction']}: {REASONS.get(x['reason'], x['reason'])}")
    lines += ['', '=== دادهٔ نامعلوم/خطا ===']
    lines += [f"{x['symbol']}: {x['reason']}" for x in r['errors']]
    lines += ['', '=== قراردادهای خارج از دامنه/فیلتر گردش ===']
    lines += [f"{x['symbol']}: {x['reason']}" for x in r['excluded']]
    lines += ['', '=== منابع (فقط فیوچرز) ==='] + list(r['sources'].values())
    return '\n'.join(lines)

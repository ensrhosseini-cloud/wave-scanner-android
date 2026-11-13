import json
import threading
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import futures_engine as f

NOW = (2_000_000_000 // 14400) * 14400 + 17


def rows(minutes=60):
    step = minutes * 60
    end = NOW // step * step
    out = []
    for i in range(181):
        t = end - (180-i) * step
        p = 100 + .04 * i
        out.append([t * 1000, p-.03, p+.2, p-.2, p, 100, p*100])
    return out


def frame(direction='long', state='active', minutes=60):
    data = rows(minutes)
    # Row -1 is still open. The closed bar at -2 drives the setup.
    prior_roof = data[-3][2]
    close = prior_roof + (.15 if state == 'active' else -.1)
    data[-2][4] = close
    data[-2][2] = max(data[-2][2], close+.03)
    data[-2][5] = 180 if state == 'active' else 90
    if direction == 'short':
        data = [[t, 220-o, 220-low, 220-high, 220-c, v, turn] for t,o,high,low,c,v,turn in data]
    return f.closed_frame(data, minutes, NOW)


def contract(symbol='TESTUSDTM'):
    return dict(symbol=symbol, status='Open', quoteCurrency='USDT', settleCurrency='USDT',
                isInverse=False, expireDate=None, type='FFWCSX', marketType='CRYPTO',
                turnoverOf24h=10_000_000, takerFeeRate=.0006, fundingFeeRate=.0001)


def quote(s, symbol='TESTUSDTM'):
    return dict(symbol=symbol, bestBidPrice=s['close']-.005, bestAskPrice=s['close']+.005, ts=NOW*1e9)


class DataTests(unittest.TestCase):
    def test_futures_ohlc_order_and_ms_conversion(self):
        r = rows()
        d = f.closed_frame(r, 60, NOW)
        self.assertEqual(d.close.iloc[-1], r[-2][4])
        self.assertEqual(d.high.iloc[-1], r[-2][2])
        self.assertEqual(d.time.iloc[-1], r[-2][0]/1000)
        self.assertEqual(len(d), 180)

    def test_optional_turnover_not_spot_volume(self):
        self.assertEqual(len(f.closed_frame([r[:6] for r in rows()], 60, NOW)), 180)

    def test_open_bar_excluded_with_grace(self):
        d = f.closed_frame(rows(), 60, NOW-15)
        self.assertEqual(len(d), 179)

    def test_missing_duplicate_stale_and_invalid_data(self):
        variants = []
        r = rows(); del r[-10]; variants.append(r)
        r = rows(); r[-10] = r[-11]; variants.append(r)
        variants.append(rows()[:-2])
        r = rows(); r[-2][4] = 10000; variants.append(r)
        r = rows(); r[-2][5] = 0; variants.append(r)
        r = rows(); r[-2][1] = float('nan'); variants.append(r)
        r = rows(); r[-2][0] /= 1000; variants.append(r)
        for r in variants:
            with self.subTest(r=r[-2]):
                with self.assertRaises(f.DataError):
                    f.closed_frame(r, 60, NOW)

    def test_contract_universe_is_not_spot_or_inverse_or_delivery(self):
        cfg = f.Config()
        self.assertTrue(f.eligible_contract(contract(), cfg)[0])
        for change in [dict(settleCurrency='BTC'), dict(isInverse=True), dict(expireDate=123),
                       dict(status='Closed'), dict(marketType='STOCK'), dict(marketStage='PRE_MARKET'),
                       dict(turnoverOf24h=5), dict(type='OTHER')]:
            self.assertFalse(f.eligible_contract(dict(contract(), **change), cfg)[0])

    def test_configuration_rejects_bad_input(self):
        for change in [dict(target_return=1), dict(target_return=float('nan')), dict(direction='spot'),
                       dict(min_turnover=-1), dict(max_spread=0), dict(min_rr=True)]:
            with self.assertRaises(ValueError):
                f.Config(**change)


class SetupTests(unittest.TestCase):
    def test_all_four_states_are_independent(self):
        for direction in ('long', 'short'):
            for state in ('active', 'ready'):
                with self.subTest(direction=direction, state=state):
                    s, reason = f.setup(frame(direction, minutes=240), frame(direction, state), direction, f.Config())
                    self.assertIsNotNone(s, reason)
                    self.assertEqual(s['state'], state)
                    self.assertEqual(s['direction'], direction)

    def test_opposing_trend_rejected(self):
        s, reason = f.setup(frame(minutes=240), frame(), 'short', f.Config())
        self.assertIsNone(s)
        self.assertEqual(reason, 'no_4h_trend')

    def test_volume_never_silently_confirms_breakout(self):
        h1 = frame()
        h1.loc[h1.index[-1], 'volume'] = 10
        s, _ = f.setup(frame(minutes=240), h1, 'long', f.Config())
        self.assertTrue(s is None or s['state'] == 'ready')

    def test_targets_stops_and_conditional_entries(self):
        for direction in ('long', 'short'):
            for state in ('active', 'ready'):
                s, _ = f.setup(frame(direction, minutes=240), frame(direction, state), direction, f.Config())
                c, reason = f.finalize(s, quote(s), contract(), f.Config(), NOW)
                self.assertIsNotNone(c, reason)
                if direction == 'long':
                    self.assertLess(c['stop'], c['entry'])
                    self.assertGreater(c['target_price'], c['entry'])
                    if state == 'ready':
                        self.assertGreater(c['entry'], c['trigger'])
                else:
                    self.assertGreater(c['stop'], c['entry'])
                    self.assertLess(c['target_price'], c['entry'])
                    if state == 'ready':
                        self.assertLess(c['entry'], c['trigger'])
                self.assertLess(c['net_rr'], abs(c['target_price']-c['entry'])/abs(c['entry']-c['stop']))

    def test_stale_wide_wrong_symbol_and_drift_rejected(self):
        s, _ = f.setup(frame(minutes=240), frame(), 'long', f.Config())
        for change in [dict(ts=(NOW-60)*1e9), dict(ts=(NOW+60)*1e9), dict(symbol='OTHER'),
                       dict(bestBidPrice=s['close']-2), dict(bestBidPrice=s['close']+3, bestAskPrice=s['close']+3.01)]:
            c, _ = f.finalize(s, dict(quote(s), **change), contract(), f.Config(), NOW)
            self.assertIsNone(c)

    def test_fee_sensitive_rr(self):
        s, _ = f.setup(frame(minutes=240), frame(), 'long', f.Config())
        c, reason = f.finalize(s, quote(s), dict(contract(), takerFeeRate=.02), f.Config(), NOW)
        self.assertIsNone(c)
        self.assertEqual(reason, 'net_rr_below_minimum')


class FakeClient:
    def __init__(self, stop=None, fail=None):
        self.stop = stop or threading.Event()
        self.fail = fail
        self.calls = []

    def sync(self):
        pass

    def now(self):
        return NOW

    def check(self):
        if self.stop.is_set():
            raise f.Cancelled()

    def get(self, name, params=None):
        self.calls.append((name, params))
        if name == 'contracts':
            return [contract(f'{d}{i}USDTM') for d in ('L', 'S') for i in range(5)]
        symbol = params['symbol']
        direction = 'long' if symbol.startswith('L') else 'short'
        s, _ = f.setup(frame(direction, minutes=240), frame(direction), direction, f.Config())
        return quote(s, symbol)

    def candles(self, symbol, minutes):
        if self.fail == symbol:
            raise f.DataError('missing_candles')
        return frame('long' if symbol.startswith('L') else 'short', minutes=minutes)


class ScanTests(unittest.TestCase):
    def test_more_than_three_in_each_direction_and_futures_sources_only(self):
        r = f.scan(f.Config(), threading.Event(), lambda *a: None, FakeClient())
        self.assertEqual(len(r['groups']['long_active']), 5)
        self.assertEqual(len(r['groups']['short_active']), 5)
        self.assertTrue(r['coverage_complete_for_eligible'])
        self.assertEqual(r['counts']['evaluated'], 10)
        self.assertTrue(all(v.startswith(f.BASE) for v in r['sources'].values()))
        self.assertEqual(len(r['rejected']), 10)  # opposite direction
        json.dumps(r, allow_nan=False)
        text = f.report_text(r)
        self.assertIn('لانگ فعال — 5', text)
        self.assertIn('لیکوییدشدن', text)

    def test_data_error_is_unknown_not_negative(self):
        r = f.scan(f.Config(), threading.Event(), lambda *a: None, FakeClient(fail='L0USDTM'))
        self.assertFalse(r['coverage_complete_for_eligible'])
        self.assertEqual(len(r['errors']), 1)
        self.assertEqual(r['counts']['evaluated'], 9)
        self.assertFalse(any(x['symbol'] == 'L0USDTM' for x in r['rejected']))

    def test_cancel_retains_partial_report(self):
        stop = threading.Event()
        def emit(kind, data):
            if kind == 'progress' and data[0] >= 1:
                stop.set()
        r = f.scan(f.Config(), stop, emit, FakeClient(stop=stop))
        self.assertTrue(r['cancelled'])
        self.assertFalse(r['coverage_complete_for_eligible'])
        self.assertEqual(r['counts']['evaluated'], 1)

    def test_single_direction(self):
        r = f.scan(f.Config(direction='long'), threading.Event(), lambda *a: None, FakeClient())
        self.assertEqual(len(r['candidates']), 5)
        self.assertTrue(all(c['direction'] == 'long' for c in r['candidates']))

    def test_candle_request_has_futures_units_and_bounded_range(self):
        client = f.Client(threading.Event())
        with patch.object(client, 'now', return_value=NOW), patch.object(client, 'get', return_value=rows(240)) as get:
            client.candles('XBTUSDTM', 240)
        name, params = get.call_args.args
        self.assertEqual(name, 'candles')
        self.assertEqual(params['granularity'], 240)
        self.assertEqual(params['symbol'], 'XBTUSDTM')
        self.assertLess(params['to']-params['from'], 190*14400*1000)
        self.assertGreater(params['from'], 1e12)
        client.session.close()


if __name__ == '__main__':
    unittest.main()

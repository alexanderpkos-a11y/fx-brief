#!/usr/bin/env python3
"""
Generate AUDUSD-brief-API-sample.html — premium financial terminal brief.
All data hardcoded; no external fetches at generation time.
"""

import os
import re
import subprocess
import json
import csv
import io
import math
import bisect
import urllib.request
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
AV_API_KEY  = os.environ.get('AV_API_KEY', '')   # set via GitHub Secret or local env
FMP_API_KEY = os.environ.get('FMP_API_KEY', '')  # Financial Modeling Prep — US Treasury yields

# ---------------------------------------------------------------------------
# LIVE FETCH HELPERS
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date as _date, timedelta

_UA = 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

def _curl_get(url, timeout=15):
    return subprocess.run(
        ['curl', '-s', '-L', '--max-time', str(timeout), '-H', _UA, url],
        capture_output=True, text=True
    ).stdout

def _yf(ticker, days=7):
    """Return (last_close, prev_close, date_str) from Yahoo Finance v8 chart API."""
    try:
        raw = _curl_get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={days}d'
        )
        d = json.loads(raw)
        res = d['chart']['result'][0]
        pairs = [(ts, c) for ts, c in zip(res['timestamp'],
                  res['indicators']['quote'][0]['close']) if c is not None]
        if len(pairs) >= 2:
            return (pairs[-1][1], pairs[-2][1],
                    datetime.fromtimestamp(pairs[-1][0]).strftime('%d %b %Y'))
        if pairs:
            return (pairs[-1][1], None,
                    datetime.fromtimestamp(pairs[-1][0]).strftime('%d %b %Y'))
    except Exception as e:
        print(f'  [WARN] YF {ticker}: {e}')
    return None, None, None

def _yf_history(ticker, range_str='10y'):
    """Fetch ~4 months of daily close history from Yahoo Finance.
    Returns {date_str: float}. Empty dict on failure."""
    try:
        raw = _curl_get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/'
            f'{ticker}?interval=1d&range={range_str}'
        )
        d = json.loads(raw)
        res = d['chart']['result'][0]
        return {
            datetime.fromtimestamp(ts).strftime('%Y-%m-%d'): c
            for ts, c in zip(res['timestamp'],
                              res['indicators']['quote'][0]['close'])
            if c is not None
        }
    except Exception as e:
        print(f'  [WARN] YF history {ticker}: {e}')
    return {}

def _fetch_us2y_fred():
    """FRED DGS2 – US 2y Treasury yield (daily, no API key required).
    Uses curl with no custom User-Agent (FRED blocks browser UAs).
    Returns (rate_float, date_str, hist_dict)."""
    try:
        raw = subprocess.run(
            ['curl', '-s', '-L', '--max-time', '30',
             'https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2'],
            capture_output=True, text=True
        ).stdout
        hist = {}
        for line in raw.splitlines():
            if not line.strip() or ',' not in line:
                continue
            parts = line.split(',')
            val = parts[1].strip()
            if val in ('.', '', 'DGS2'):
                continue
            try:
                hist[parts[0].strip()] = float(val)
            except ValueError:
                pass
        if hist:
            last_date = max(hist)
            return hist[last_date], last_date, hist
    except Exception as e:
        print(f'  [WARN] FRED DGS2: {e}')
    return None, None, {}

def _fetch_us2y_fmp(api_key, lookback_days=400):
    """FMP treasury-rates — US 2y Treasury yield (daily, free tier).
    Returns (rate_float, date_str, hist_dict). hist_dict is {YYYY-MM-DD: float}.
    Empty on failure / missing key. The API key is never logged."""
    if not api_key:
        return None, None, {}
    try:
        end   = _date.today()
        start = end - timedelta(days=lookback_days)
        url = ('https://financialmodelingprep.com/stable/treasury-rates'
               f'?from={start.isoformat()}&to={end.isoformat()}&apikey={api_key}')
        raw = _curl_get(url, timeout=30)
        recs = json.loads(raw)
        if not isinstance(recs, list):
            # surface the API message (e.g. restricted endpoint) without the URL/key
            msg = recs.get('Error Message') or recs.get('message') or str(recs)
            print(f'  [WARN] FMP treasury: {str(msg)[:120]}')
            return None, None, {}
        hist = {}
        for r in recs:
            d, y2 = r.get('date'), r.get('year2')
            if d and y2 is not None:
                hist[d[:10]] = float(y2)
        if hist:
            last_date = max(hist)
            return hist[last_date], last_date, hist
    except Exception as e:
        print(f'  [WARN] FMP treasury: {e}')
    return None, None, {}

def _fetch_rba_f2():
    """RBA F2 – AU 2y and 10y bond yields.
    Returns (au2y, au2y_prev, au10y, au10y_prev, date_str, au2y_hist).
    au2y_hist is {date_str: float} for the last 120 daily rows."""
    try:
        raw = _curl_get('https://www.rba.gov.au/statistics/tables/csv/f2-data.csv', timeout=25)
        rows = list(csv.reader(io.StringIO(raw)))
        data_rows = [r for r in rows[10:]
                     if r and r[0] and r[0][0:1].isdigit() and len(r) >= 5]
        if len(data_rows) >= 2:
            last, prev = data_rows[-1], data_rows[-2]
            hist = {}
            for r in data_rows:
                try:
                    dt = datetime.strptime(r[0], '%d-%b-%Y').strftime('%Y-%m-%d')
                    hist[dt] = float(r[1])
                except (ValueError, IndexError):
                    pass
            return float(last[1]), float(prev[1]), float(last[4]), float(prev[4]), last[0], hist
    except Exception as e:
        print(f'  [WARN] RBA F2: {e}')
    return None, None, None, None, None, {}

def _fetch_rba_f1():
    """RBA F1 – cash rate + 1m/3m BABs → implied cut probability.
    Returns (cash_rate, babs_1m, babs_3m, cut_prob_pct, date_str).
    Note: BABs carry a ~5-15 bp credit spread over OIS; probability is indicative."""
    try:
        raw = _curl_get('https://www.rba.gov.au/statistics/tables/csv/f1-data.csv', timeout=25)
        rows = list(csv.reader(io.StringIO(raw)))
        data_rows = [r for r in rows[10:] if r and r[0] and r[0][0:1].isdigit()]
        for r in reversed(data_rows):
            if len(r) > 10 and r[1] and r[9] and r[10]:
                cash = float(r[1])
                b1m  = float(r[9])
                b3m  = float(r[10])
                prob = round(max(0, min(100, (cash - b1m) / 0.25 * 100)))
                return cash, b1m, b3m, prob, r[0]
    except Exception as e:
        print(f'  [WARN] RBA F1: {e}')
    return None, None, None, None, None

def _fetch_cot():
    """CFTC TFF – AUD futures positioning (latest report).
    Returns a dict of all COT values, or None on failure."""
    try:
        raw = _curl_get(
            'https://publicreporting.cftc.gov/resource/gpe5-46if.json'
            '?$where=contract_market_name=%27AUSTRALIAN+DOLLAR%27'
            '&$order=report_date_as_yyyy_mm_dd+DESC&$limit=1',
            timeout=30
        )
        recs = json.loads(raw)
        if recs:
            r = recs[0]
            am_long  = int(r['asset_mgr_positions_long'])
            am_short = int(r['asset_mgr_positions_short'])
            am_net   = am_long - am_short
            lev_long  = int(r['lev_money_positions_long'])
            lev_short = int(r['lev_money_positions_short'])
            lev_net   = lev_long - lev_short
            am_chg   = int(r['change_in_asset_mgr_long']) - int(r['change_in_asset_mgr_short'])
            lev_chg  = int(r['change_in_lev_money_long']) - int(r['change_in_lev_money_short'])
            return dict(
                am_long=am_long,   am_short=am_short,  am_net=am_net,
                lev_long=lev_long, lev_short=lev_short, lev_net=lev_net,
                am_net_prev=am_net - am_chg,   am_net_chg=am_chg,
                lev_net_prev=lev_net - lev_chg, lev_net_chg=lev_chg,
                cot_date=r['report_date_as_yyyy_mm_dd'][:10],
            )
    except Exception as e:
        print(f'  [WARN] CFTC COT: {e}')
    return None

# ---------------------------------------------------------------------------
# CORRELATION HELPERS
# ---------------------------------------------------------------------------
def _log_returns(hist_dict):
    """Log returns from sorted {date: price} dict. Returns list of (date, ret)."""
    dates = sorted(hist_dict)
    out = []
    for i in range(1, len(dates)):
        p0, p1 = hist_dict[dates[i-1]], hist_dict[dates[i]]
        if p0 and p1 and p0 > 0 and p1 > 0:
            out.append((dates[i], math.log(p1 / p0)))
    return out

def _first_diffs(hist_dict):
    """First differences from sorted {date: value} dict. Returns list of (date, diff)."""
    dates = sorted(hist_dict)
    out = []
    for i in range(1, len(dates)):
        v0, v1 = hist_dict[dates[i-1]], hist_dict[dates[i]]
        if v0 is not None and v1 is not None:
            out.append((dates[i], v1 - v0))
    return out

def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n;  my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx)**2 for x in xs))
    dy = math.sqrt(sum((y - my)**2 for y in ys))
    return (num / (dx * dy)) if dx and dy else None

def compute_correlations(aud_hist, driver_hists, use_diffs, windows=(20, 60), min_obs_frac=0.75):
    """
    aud_hist: {date: price}
    driver_hists: {name: {date: value}}
    use_diffs: set of driver names that use first differences (not log returns)
    Returns: {name: {window: (corr_or_None, n_obs)}}
    """
    aud_by_date = dict(_log_returns(aud_hist))
    results = {}
    for name, hist in driver_hists.items():
        drv_by_date = dict(_first_diffs(hist) if name in use_diffs else _log_returns(hist))
        common = sorted(set(aud_by_date) & set(drv_by_date))
        results[name] = {}
        for w in windows:
            min_obs = round(w * min_obs_frac)
            tail = common[-w:] if len(common) >= w else common
            n = len(tail)
            if n < min_obs:
                results[name][w] = (None, n)
            else:
                xs = [aud_by_date[d] for d in tail]
                ys = [drv_by_date[d] for d in tail]
                results[name][w] = (_pearson(xs, ys), n)
    return results

def _build_driver_series(aud_hist, driver_hists, sample_every=5):
    """Per-driver chart series: {name: [{date, aud, drv, r20, r60}...]}.
    aud and drv are raw price levels; r20/r60 are rolling Pearson correlations
    of log-returns (sampled every sample_every trading days)."""
    results = {}
    for name, hist in driver_hists.items():
        common = sorted(set(aud_hist) & set(hist))
        if len(common) < 22:
            results[name] = []
            continue
        aud_r = [math.log(aud_hist[common[i]] / aud_hist[common[i-1]])
                 for i in range(1, len(common))]
        drv_r = [math.log(hist[common[i]] / hist[common[i-1]])
                 for i in range(1, len(common))]
        ret_dates = common[1:]
        records = []
        for i in range(len(ret_dates)):
            if i % sample_every != 0 and i < len(ret_dates) - 1:
                continue
            d = ret_dates[i]
            def _r(win, _i=i):
                if _i < win:
                    return None
                r = _pearson(aud_r[_i-win:_i], drv_r[_i-win:_i])
                return round(r, 3) if r is not None else None
            records.append({
                'date': d,
                'aud':  round(aud_hist[d], 4) if d in aud_hist else None,
                'drv':  round(hist[d], 2)     if d in hist      else None,
                'r20':  _r(20),
                'r60':  _r(60),
            })
        results[name] = records
    return results

PRINT_DIAGNOSTICS = os.environ.get('PRINT_DIAGNOSTICS', '').lower() in ('1', 'true', 'yes')


# ---------------------------------------------------------------------------
# FETCH ALL LIVE DATA
# ---------------------------------------------------------------------------
YF_TICKERS = {
    'audusd':   'AUDUSD=X',
    'audcny':   'AUDCNY=X',
    'dxy':      'DX-Y.NYB',
    'usdcnh':   'CNH=X',
    'gold':     'GC=F',
    'copper':   'HG=F',
    'spx':      '^GSPC',
    'hsi':      '^HSI',
    'csi300':   '000300.SS',
    'vix':      '^VIX',
    'us10y':    '^TNX',
    'iron_ore': 'TIO=F',
}

# 4-month daily history for correlation panel (5 drivers + AUD/USD)
# Note: CNH=X (offshore) has no YF history; use USDCNY=X (onshore) as proxy
HIST_TICKERS = {
    'audusd':   'AUDUSD=X',
    'dxy':      'DX-Y.NYB',
    'usdcnh':   'USDCNY=X',
    'spx':      '^GSPC',
    'iron_ore': 'TIO=F',
    'vix':      '^VIX',
}

print('Fetching Yahoo Finance tickers (parallel)…')
_yf_results = {}
_hist_results = {}
with ThreadPoolExecutor(max_workers=10) as _ex:
    _tile_futs = {_ex.submit(_yf, ticker): key for key, ticker in YF_TICKERS.items()}
    _hist_futs = {_ex.submit(_yf_history, ticker): key for key, ticker in HIST_TICKERS.items()}
    for _fut in list(_tile_futs):
        _yf_results[_tile_futs[_fut]] = _fut.result()
    for _fut in list(_hist_futs):
        _hist_results[_hist_futs[_fut]] = _fut.result()

for _k, (_l, _p, _d) in _yf_results.items():
    print(f'  {_k:12} {f"{_l:.4g}" if _l else "FAILED"}')

print('Fetching AU yields (RBA F2)…')
au2y, au2y_prev, au10y, au10y_prev, _f2_date, _au2y_hist = _fetch_rba_f2()
print(f'  AU2y={au2y}  AU10y={au10y}  ({_f2_date})  hist={len(_au2y_hist)}d')

print('Fetching US 2y yield (FMP treasury-rates → FRED fallback)…')
_us2y_raw, _us2y_date_raw, _us2y_hist = _fetch_us2y_fmp(FMP_API_KEY)
_us2y_src = 'FMP'
if _us2y_raw is None:
    _us2y_raw, _us2y_date_raw, _us2y_hist = _fetch_us2y_fred()
    _us2y_src = 'FRED'
us2y      = _us2y_raw
us2y_date = (datetime.strptime(_us2y_date_raw, '%Y-%m-%d').strftime('%d %b')
             if _us2y_date_raw else '?')
print(f'  US2y={us2y}  ({us2y_date})  src={_us2y_src}  hist={len(_us2y_hist)}d')

print('Fetching RBA cash rate & BABs (F1)…')
rba_cash_rate, rba_babs_1m, rba_babs_3m, rba_cut_prob, _rba_f1_date = _fetch_rba_f1()
print(f'  cash={rba_cash_rate}%  1m_BABs={rba_babs_1m}%  cut_prob~{rba_cut_prob}%')

print('Fetching COT positioning (CFTC)…')
cot = _fetch_cot()
print(f'  AM_net={cot["am_net"] if cot else "FAILED"}  '
      f'LF_net={cot["lev_net"] if cot else "FAILED"}  '
      f'({cot["cot_date"] if cot else "?"})')

# ---------------------------------------------------------------------------
# CORRELATION — build spread series, compute all driver correlations
# ---------------------------------------------------------------------------
# Spread in percentage points (AU 2y % - US 2y %), daily, pairwise-complete dates
# Stored in % so OLS betas are per 1 ppt change (not per 1 bp), keeping coefficients readable
_spread_hist = {
    d: (_au2y_hist[d] - _us2y_hist[d])
    for d in sorted(set(_au2y_hist) & set(_us2y_hist))
}

_driver_hists = {
    'spread':   _spread_hist,
    'iron_ore': _hist_results.get('iron_ore', {}),
    'spx':      _hist_results.get('spx', {}),
    'usdcnh':   _hist_results.get('usdcnh', {}),
    'dxy':      _hist_results.get('dxy', {}),
}
print('Computing driver correlations…')
corr_results = compute_correlations(
    _hist_results.get('audusd', {}),
    _driver_hists,
    use_diffs={'spread'},
)
for _name, _res in corr_results.items():
    _r20, _n20 = _res[20];  _r60, _n60 = _res[60]
    print(f'  {_name:12} r20={f"{_r20:.3f}" if _r20 else "n/c":>7} ({_n20}obs)'
          f'  r60={f"{_r60:.3f}" if _r60 else "n/c":>7} ({_n60}obs)')


print('Building per-driver chart series…')
_chart_driver_hists = {
    'spx':      _hist_results.get('spx', {}),
    'usdcnh':   _hist_results.get('usdcnh', {}),
    'iron_ore': _hist_results.get('iron_ore', {}),
    'dxy':      _hist_results.get('dxy', {}),
    'vix':      _hist_results.get('vix', {}),
}
_driver_series = _build_driver_series(_hist_results.get('audusd', {}), _chart_driver_hists)
for _k in _chart_driver_hists:
    print(f'  {_k}: {len(_driver_series.get(_k, []))} records')
_driver_series_j = json.dumps(_driver_series, separators=(',', ':'))
driver_series_script = f'<script>\nvar DRIVER_SERIES={_driver_series_j};\n</script>'


# ---------------------------------------------------------------------------
# ASSEMBLE DATA DICT
# ---------------------------------------------------------------------------
def _g(key, idx=0, fallback=None):
    v = _yf_results.get(key, (None,) * 3)[idx]
    return v if v is not None else fallback

data = {
    'audusd':      _g('audusd',  0, 0.7186),
    'audusd_prev': _g('audusd',  1, 0.7164),
    'audcny':      _g('audcny',  0, 4.640),
    'audcny_prev': _g('audcny',  1, 4.630),
    'dxy':         _g('dxy',     0, 98.942),
    'dxy_prev':    _g('dxy',     1, 99.020),
    'usdcnh':      _g('usdcnh',  0, 6.7628),
    'usdcnh_prev': _g('usdcnh',  1),
    'gold':        _g('gold',    0, 4569.90),
    'gold_prev':   _g('gold',    1, 4499.30),
    'copper':      _g('copper',  0, 6.394),
    'copper_prev': _g('copper',  1, 6.396),
    'spx':         _g('spx',     0, 7580.06),
    'spx_prev':    _g('spx',     1, 7563.63),
    'hsi':         _g('hsi',     0, 25182.4),
    'hsi_prev':    _g('hsi',     1, 25006.2),
    'csi300':      _g('csi300',  0, 4892.1),
    'csi300_prev': _g('csi300',  1, 4914.2),
    'vix':         _g('vix',     0, 15.32),
    'vix_prev':    _g('vix',     1, 15.74),
    'au2y':        au2y    or 4.542,
    'au2y_prev':   au2y_prev,
    'au10y':       au10y   or 4.863,
    'au10y_prev':  au10y_prev or 4.917,
    'us2y':        us2y    or 4.09,
    'us2y_date':   us2y_date or '22 May',
    'us10y':       _g('us10y',  0, 4.453),
    'us10y_prev':  _g('us10y',  1, 4.455),
    'am_long':     cot['am_long']      if cot else 80727,
    'am_short':    cot['am_short']     if cot else 61652,
    'am_net':      cot['am_net']       if cot else 19075,
    'lev_long':    cot['lev_long']     if cot else 89665,
    'lev_short':   cot['lev_short']    if cot else 29431,
    'lev_net':     cot['lev_net']      if cot else 60234,
    'am_net_prev': cot['am_net_prev']  if cot else 40595,
    'am_net_chg':  cot['am_net_chg']   if cot else -21520,
    'lev_net_prev':cot['lev_net_prev'] if cot else 61178,
    'lev_net_chg': cot['lev_net_chg']  if cot else -944,
}

# Derived spreads (recomputed from live yields)
data['spread_2y']  = round((data['au2y']  - data['us2y'])  * 100)
data['spread_10y'] = round((data['au10y'] - data['us10y']) * 100)

# Standalone live variables
iron_ore_last = _g('iron_ore', 0)
iron_ore_prev = _g('iron_ore', 1)
cot_date      = cot['cot_date'] if cot else '2026-05-26'
data_date_str = _yf_results.get('audusd', (None, None, None))[2] or '29 May 2026'

# Date strings for dynamic HTML
today          = _date.today()
today_long     = today.strftime('%A %-d %B %Y')   # e.g. "Friday 30 May 2026"
today_short    = today.strftime('%-d %B %Y')       # e.g. "30 May 2026"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def pct_change(current, prev):
    if prev is None or prev == 0:
        return None
    return (current - prev) / abs(prev) * 100

def bp_change(current, prev):
    if prev is None:
        return None
    return (current - prev) * 100

def fmt_pct(val, decimals=2):
    if val is None:
        return None
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.{decimals}f}%"

def fmt_bp(val):
    if val is None:
        return None
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f} bp"

def change_html(val, invert=False, is_bp=False):
    """Return a styled span for a change value."""
    if val is None:
        return '<span class="dim-text faint-italic">n/a</span>'
    positive = val >= 0
    if invert:
        positive = not positive
    color_cls = "green" if positive else "red"
    arrow = "▲" if val >= 0 else "▼"
    if is_bp:
        text = f"{arrow} {fmt_bp(val)}"
    else:
        text = f"{arrow} {fmt_pct(val)}"
    return f'<span class="{color_cls}">{text}</span>'

def badge(src):
    badges = {
        'YF':   ('badge-yf',   'YF'),
        'RBA':  ('badge-rba',  'RBA'),
        'CFTC': ('badge-cftc', 'CFTC'),
        'AV':   ('badge-av',   'AV'),
        'FRED': ('badge-av',   'FRED'),
        'FMP':  ('badge-av',   'FMP'),
        'SCR':  ('badge-scr',  'SCR'),
    }
    cls, label = badges.get(src, ('badge-scr', src))
    return f'<span class="badge {cls}">{label}</span>'

def data_gap(label="DATA GAP"):
    return f'<span class="data-gap">{label}</span>'

# ---------------------------------------------------------------------------
# PRE-COMPUTE VALUES
# ---------------------------------------------------------------------------
audusd_chg  = pct_change(data['audusd'], data['audusd_prev'])
audcny_chg  = pct_change(data['audcny'], data['audcny_prev'])
dxy_chg     = pct_change(data['dxy'],    data['dxy_prev'])
gold_chg    = pct_change(data['gold'],   data['gold_prev'])
copper_chg  = pct_change(data['copper'], data['copper_prev'])
spx_chg     = pct_change(data['spx'],    data['spx_prev'])
hsi_chg     = pct_change(data['hsi'],    data['hsi_prev'])
csi300_chg  = pct_change(data['csi300'], data['csi300_prev'])
vix_chg     = pct_change(data['vix'],    data['vix_prev'])
au10y_chg   = bp_change(data['au10y'],   data['au10y_prev'])
us10y_chg   = bp_change(data['us10y'],   data['us10y_prev'])
iron_ore_chg = pct_change(iron_ore_last, iron_ore_prev)

# ---------------------------------------------------------------------------
# CHART ASSETS (lifted from fx_dashboard.html)
# ---------------------------------------------------------------------------
# Pull the interactive chart <script> (with its RD_SERIES / COT_SERIES data and
# both IIFE inits) straight out of fx_dashboard.html so the charts stay in sync
# with that source rather than duplicating ~72 KB of data here.
DASHBOARD_SRC = os.path.join(_HERE, 'planning', 'fx_dashboard.html')
with open(DASHBOARD_SRC, encoding="utf-8") as _f:
    _dash = _f.read()

_m = re.search(r"<script>\s*/\* ── shared.*?</script>", _dash, re.S)
if not _m:
    raise RuntimeError("Could not locate the chart <script> block in fx_dashboard.html")
chart_js = _m.group(0)

# ---------------------------------------------------------------------------
# EXTEND RD_SERIES WITH LIVE TAIL
# The chart series baked into fx_dashboard.html is static (last point 29-May-26).
# Splice live points onto the end so the chart advances each run. The long
# 2016→ history stays intact; only dates after the last baked point are added.
# RD_SERIES format: {"date":"YYYY-MM-DD","audusd":<px>,"spread":<bp>}
# (spread in basis points = (AU2y% − US2y%) × 100).
# ---------------------------------------------------------------------------
def _extend_rd_series(js):
    _rm = re.search(r"(RD_SERIES\s*=\s*)(\[[^\]]*\])", js)
    if not _rm:
        print('  [WARN] RD_SERIES: array not found in chart JS — chart left static')
        return js
    try:
        series = json.loads(_rm.group(2))
    except Exception as e:
        print(f'  [WARN] RD_SERIES: parse failed ({e}) — chart left static')
        return js
    if not series:
        return js
    last_static = max(p['date'] for p in series)

    _aud_hist = _hist_results.get('audusd', {})
    if not _spread_hist or not _aud_hist:
        print('  [WARN] RD_SERIES: no live spread/audusd history — chart left static')
        return js
    _aud_dates = sorted(_aud_hist)

    def _aud_on_or_before(d):
        # nearest AUD/USD close at or before date d (handles holiday gaps)
        prior = [x for x in _aud_dates if x <= d]
        return _aud_hist[prior[-1]] if prior else None

    # candidate live dates strictly after the last baked point, sampled ~weekly
    # to match the existing cadence; the final available date is always included
    new_dates = [d for d in sorted(_spread_hist) if d > last_static]
    added = 0
    for i, d in enumerate(new_dates):
        if i % 5 != 0 and d != new_dates[-1]:
            continue
        px = _aud_on_or_before(d)
        if px is None:
            continue
        series.append({
            'date':   d,
            'audusd': round(px, 4),
            'spread': round(_spread_hist[d] * 100),
        })
        added += 1

    if not added:
        print(f'  RD_SERIES: no new points after {last_static} (chart unchanged)')
        return js
    series.sort(key=lambda p: p['date'])
    new_last = series[-1]['date']
    print(f'  RD_SERIES: extended {last_static} → {new_last} (+{added} live points)')
    _new_arr = json.dumps(series, separators=(',', ':'))
    return js[:_rm.start()] + _rm.group(1) + _new_arr + js[_rm.end():]

chart_js = _extend_rd_series(chart_js)

# Chart component CSS (curated subset of fx_dashboard's styles; the brief's
# design tokens already cover the rest, so only chart-specific rules + the
# extra colour variables the chart JS reads via css() are added here).
CHART_CSS = """
/* ── CHART COMPONENTS (merged from fx_dashboard) ── */
:root{
  --aud:#5b9bd5; --spread:#e07856; --am:#5ec2a0; --lev:#e8915b;
  --ink:#0c1320; --ink-2:#111b2e;
  --grid:rgba(142,162,189,.10);
}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.card{background:linear-gradient(180deg,var(--panel),rgba(16,30,54,.6));border:1px solid var(--panel-edge);border-radius:12px;padding:16px 18px}
.card .lbl{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-faint)}
.card .val{font-family:'Fraunces',serif;font-size:28px;font-weight:600;margin-top:8px;letter-spacing:-.01em}
.card .chg{font-size:12px;margin-top:6px;color:var(--text-dim)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
.dot.aud{background:var(--aud)}.dot.spr{background:var(--spread)}.dot.am{background:var(--am)}.dot.lev{background:var(--lev)}
.up{color:#6fcf8e}.down{color:#e8746a}
.panel-box{background:linear-gradient(180deg,var(--panel),rgba(12,21,37,.55));border:1px solid var(--panel-edge);border-radius:16px;padding:22px 22px 10px;box-shadow:0 24px 60px -30px rgba(0,0,0,.8);position:relative}
.panel-title{font-family:'Fraunces',serif;font-size:18px;font-weight:600;margin-bottom:2px}
.legend{display:flex;gap:18px;font-size:12px;color:var(--text-dim);margin:10px 0 16px;flex-wrap:wrap}
.legend span{display:flex;align-items:center}
.chart-holder{position:relative;height:380px;width:100%}
.slider-wrap{margin:26px 4px 14px}
.slider-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px}
.slider-head .k{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-faint)}
.range-readout{font-size:13px}.range-readout b{color:var(--gold)}
.dual{position:relative;height:34px}
.track{position:absolute;top:15px;left:0;right:0;height:4px;background:var(--panel-edge);border-radius:4px}
.track-fill{position:absolute;top:15px;height:4px;background:linear-gradient(90deg,var(--aud),var(--gold));border-radius:4px}
.dual input[type=range]{-webkit-appearance:none;appearance:none;position:absolute;top:0;left:0;width:100%;height:34px;background:none;pointer-events:none;margin:0}
.dual input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;pointer-events:all;width:18px;height:18px;border-radius:50%;background:var(--gold);border:3px solid var(--ink-2);cursor:grab;box-shadow:0 2px 8px rgba(0,0,0,.5)}
.dual input[type=range]::-moz-range-thumb{pointer-events:all;width:18px;height:18px;border-radius:50%;background:var(--gold);border:3px solid var(--ink-2);cursor:grab;box-shadow:0 2px 8px rgba(0,0,0,.5)}
.dual input[type=range]::-webkit-slider-runnable-track{background:none;height:34px}
.dual input[type=range]::-moz-range-track{background:none;height:34px}
.presets{display:flex;gap:8px;margin-top:18px;flex-wrap:wrap}
.preset{font-family:inherit;font-size:11.5px;letter-spacing:.06em;color:var(--text-dim);background:transparent;border:1px solid var(--panel-edge);padding:7px 13px;border-radius:20px;cursor:pointer;transition:.18s}
.preset:hover{border-color:var(--aud);color:var(--text)}
.preset.active{background:var(--aud);border-color:var(--aud);color:var(--ink);font-weight:600}
.source{margin-top:18px;margin-bottom:6px;font-size:11px;color:var(--text-faint);letter-spacing:.03em}

"""

# Section 02 chart markup (replaces the old yield table).
RD_CHART_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD — latest</div>
    <div class="val" id="rd_audVal">—</div>
    <div class="chg" id="rd_audChg">—</div>
  </div>
  <div class="card">
    <div class="lbl"><span class="dot spr"></span>2y ACGB/UST spread</div>
    <div class="val" id="rd_sprVal">—</div>
    <div class="chg" id="rd_sprChg">—</div>
  </div>
  <div class="card">
    <div class="lbl">Window correlation</div>
    <div class="val" id="rd_corrVal">—</div>
    <div class="chg">AUD/USD vs spread, selected range</div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD and 2y yield spread (ACGB minus UST)</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot spr"></span>2y ACGB/UST spread, bp (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="rd_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="rd_rangeStart">—</b> &nbsp;→&nbsp; <b id="rd_rangeEnd">—</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="rd_trackFill"></div>
      <input type="range" id="rd_minR" min="0" max="100" value="0">
      <input type="range" id="rd_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="rd_presets">
      <button class="preset" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset active" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source" id="rd_source">Source: FMP treasury-rates (US 2y) &middot; RBA Table F2 (AU 2y) &middot; spread = (AU 2y &minus; US 2y) &times; 100 bp.</p>
"""

# Section 03 chart markup (the live COT chart; hard-stat cards follow below it).
COT_CHART_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD — latest</div>
    <div class="val" id="cot_audVal">—</div>
    <div class="chg" id="cot_audChg">—</div>
  </div>
  <div class="card">
    <div class="lbl"><span class="dot am"></span>Asset Mgr net</div>
    <div class="val" id="cot_amVal">—</div>
    <div class="chg" id="cot_amChg">—</div>
  </div>
  <div class="card">
    <div class="lbl"><span class="dot lev"></span>Leveraged Funds net</div>
    <div class="val" id="cot_levVal">—</div>
    <div class="chg" id="cot_levChg">—</div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD futures — speculative positioning vs price</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot am"></span>Asset Mgr net, contracts (RHS)</span>
    <span><span class="dot lev"></span>Leveraged Funds net, contracts (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="cot_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="cot_rangeStart">—</b> &nbsp;→&nbsp; <b id="cot_rangeEnd">—</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="cot_trackFill"></div>
      <input type="range" id="cot_minR" min="0" max="100" value="0">
      <input type="range" id="cot_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="cot_presets">
      <button class="preset" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset active" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source">Source: CFTC TFF Futures-Only (positioning) &middot; Yahoo Finance (AUD/USD) &middot; net = long &minus; short contracts. Weekly, Tuesday close.</p>
"""

# ---------------------------------------------------------------------------
# AUD/USD vs S&P 500 individual chart
# ---------------------------------------------------------------------------
DC_SPX_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD &mdash; latest</div>
    <div class="val" id="dc_spx_audVal">&mdash;</div>
    <div class="chg" id="dc_spx_audChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#2fcb9a">S&amp;P 500 &mdash; latest</div>
    <div class="val" id="dc_spx_drvVal">&mdash;</div>
    <div class="chg" id="dc_spx_drvChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl">Window correlation</div>
    <div class="val" id="dc_spx_corrVal">&mdash;</div>
    <div id="dc_spx_corrToggle" class="presets" style="margin-top:8px">
      <button class="preset active" data-w="20">20d</button>
      <button class="preset" data-w="60">60d</button>
    </div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD and S&amp;P 500</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot" style="background:#2fcb9a"></span>S&amp;P 500 (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="dc_spx_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="dc_spx_start">&mdash;</b> &nbsp;&rarr;&nbsp; <b id="dc_spx_end">&mdash;</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="dc_spx_fill"></div>
      <input type="range" id="dc_spx_minR" min="0" max="100" value="0">
      <input type="range" id="dc_spx_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="dc_spx_presets">
      <button class="preset active" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source">Source: Yahoo Finance. Rolling Pearson correlation of log-returns vs AUD/USD, selected date range.</p>
"""

DC_SPX_SCRIPT = """<script>
(function(){
  var SER = DRIVER_SERIES['spx'];
  if (!SER || !SER.length) return;
  var N = SER.length;
  var activeCorrKey = 'r20';

  var audValEl  = document.getElementById('dc_spx_audVal');
  var audChgEl  = document.getElementById('dc_spx_audChg');
  var drvValEl  = document.getElementById('dc_spx_drvVal');
  var drvChgEl  = document.getElementById('dc_spx_drvChg');
  var corrValEl = document.getElementById('dc_spx_corrVal');
  var startEl   = document.getElementById('dc_spx_start');
  var endEl     = document.getElementById('dc_spx_end');
  var fillEl    = document.getElementById('dc_spx_fill');
  var minR      = document.getElementById('dc_spx_minR');
  var maxR      = document.getElementById('dc_spx_maxR');
  minR.max = maxR.max = N - 1;

  var AUD_COLOR = '#5b9bd5';
  var SPX_COLOR = '#2fcb9a';

  var chart = new Chart(document.getElementById('dc_spx_chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'AUD/USD', data: [], borderColor: AUD_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_aud' },
      { label: 'S&P 500', data: [], borderColor: SPX_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_drv' },
    ]},
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: 'rgba(255,255,255,.35)', font: { size: 11 } },
             grid: { color: 'rgba(142,162,189,.08)' } },
        y_aud: { position: 'left',
                 ticks: { color: AUD_COLOR, font: { size: 11 },
                          callback: function(v){ return v.toFixed(4); } },
                 grid: { color: 'rgba(142,162,189,.08)' } },
        y_drv: { position: 'right',
                 grid: { drawOnChartArea: false },
                 ticks: { color: SPX_COLOR, font: { size: 11 },
                          callback: function(v){ return v >= 1000 ? (v/1000).toFixed(1)+'k' : v.toFixed(0); } } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(10,18,35,.92)', borderColor: 'rgba(255,255,255,.1)',
          borderWidth: 1, titleColor: 'rgba(255,255,255,.6)', bodyColor: '#fff',
          callbacks: {
            label: function(ctx) {
              if (ctx.datasetIndex === 0) return ' AUD/USD: ' + (ctx.raw || 0).toFixed(4);
              return ' S&P 500: ' + Math.round(ctx.raw || 0).toLocaleString();
            }
          }
        }
      }
    }
  });

  function fmtPct(val, prev) {
    if (!val || !prev) return '';
    var p = (val - prev) / prev * 100;
    return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  }

  function applySlice() {
    var lo = parseInt(minR.value), hi = parseInt(maxR.value) + 1;
    if (hi > N) hi = N;
    if (lo >= hi - 1) lo = Math.max(0, hi - 2);
    var slice = SER.slice(lo, hi);

    chart.data.labels            = slice.map(function(r){ return r.date; });
    chart.data.datasets[0].data  = slice.map(function(r){ return r.aud; });
    chart.data.datasets[1].data  = slice.map(function(r){ return r.drv; });
    chart.update('none');

    var last = slice[slice.length - 1] || {};
    var prev = slice[slice.length - 2] || {};
    audValEl.textContent = last.aud ? last.aud.toFixed(4) : '\\u2014';
    audChgEl.textContent = fmtPct(last.aud, prev.aud) || '\\u2014';
    drvValEl.textContent = last.drv ? Math.round(last.drv).toLocaleString() : '\\u2014';
    drvChgEl.textContent = fmtPct(last.drv, prev.drv) || '\\u2014';

    var corrs = slice.map(function(r){ return r[activeCorrKey]; })
                     .filter(function(v){ return v != null; });
    var avg = corrs.length ? corrs.reduce(function(a,b){ return a+b; }, 0) / corrs.length : null;
    corrValEl.textContent = avg !== null ? avg.toFixed(2) : '\\u2014';
    corrValEl.style.color = avg === null ? '' : (avg > 0.1 ? '#6fcf8e' : avg < -0.1 ? '#e8746a' : 'var(--text)');

    startEl.textContent = (slice[0] || {}).date || '\\u2014';
    endEl.textContent   = last.date || '\\u2014';

    var pLo = lo / (N - 1) * 100, pHi = parseInt(maxR.value) / (N - 1) * 100;
    fillEl.style.left  = pLo + '%';
    fillEl.style.width = (pHi - pLo) + '%';
  }

  minR.addEventListener('input', function(){
    if (parseInt(minR.value) >= parseInt(maxR.value)) minR.value = parseInt(maxR.value) - 1;
    applySlice();
  });
  maxR.addEventListener('input', function(){
    if (parseInt(maxR.value) <= parseInt(minR.value)) maxR.value = parseInt(minR.value) + 1;
    applySlice();
  });

  document.getElementById('dc_spx_presets').addEventListener('click', function(e){
    var btn = e.target.closest('[data-y]');
    if (!btn) return;
    var yrs = parseInt(btn.dataset.y), lo = 0;
    if (yrs > 0) {
      var cutoff = new Date(SER[N-1].date);
      cutoff.setFullYear(cutoff.getFullYear() - yrs);
      var ct = cutoff.toISOString().slice(0,10);
      for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    }
    minR.value = lo; maxR.value = N - 1;
    document.querySelectorAll('#dc_spx_presets .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  document.getElementById('dc_spx_corrToggle').addEventListener('click', function(e){
    var btn = e.target.closest('[data-w]');
    if (!btn) return;
    activeCorrKey = btn.dataset.w === '60' ? 'r60' : 'r20';
    document.querySelectorAll('#dc_spx_corrToggle .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  (function(){
    var cutoff = new Date(SER[N-1].date);
    cutoff.setFullYear(cutoff.getFullYear() - 1);
    var ct = cutoff.toISOString().slice(0,10), lo = 0;
    for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    minR.value = lo; maxR.value = N - 1;
    applySlice();
  })();
})();
</script>"""

# ---------------------------------------------------------------------------
# AUD/USD vs USD/CNY individual chart
# ---------------------------------------------------------------------------
DC_CNH_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD &mdash; latest</div>
    <div class="val" id="dc_cnh_audVal">&mdash;</div>
    <div class="chg" id="dc_cnh_audChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#c084fc">USD/CNY &mdash; latest</div>
    <div class="val" id="dc_cnh_drvVal">&mdash;</div>
    <div class="chg" id="dc_cnh_drvChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl">Window correlation</div>
    <div class="val" id="dc_cnh_corrVal">&mdash;</div>
    <div id="dc_cnh_corrToggle" class="presets" style="margin-top:8px">
      <button class="preset active" data-w="20">20d</button>
      <button class="preset" data-w="60">60d</button>
    </div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD and USD/CNY</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot" style="background:#c084fc"></span>USD/CNY (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="dc_cnh_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="dc_cnh_start">&mdash;</b> &nbsp;&rarr;&nbsp; <b id="dc_cnh_end">&mdash;</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="dc_cnh_fill"></div>
      <input type="range" id="dc_cnh_minR" min="0" max="100" value="0">
      <input type="range" id="dc_cnh_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="dc_cnh_presets">
      <button class="preset active" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source">Source: Yahoo Finance. AUD/USD tends to move inversely with USD/CNY (higher USD/CNY = weaker yuan = headwind for AUD). Rolling Pearson correlation of log-returns, selected range.</p>
"""

DC_CNH_SCRIPT = """<script>
(function(){
  var SER = DRIVER_SERIES['usdcnh'];
  if (!SER || !SER.length) return;
  var N = SER.length;
  var activeCorrKey = 'r20';

  var audValEl  = document.getElementById('dc_cnh_audVal');
  var audChgEl  = document.getElementById('dc_cnh_audChg');
  var drvValEl  = document.getElementById('dc_cnh_drvVal');
  var drvChgEl  = document.getElementById('dc_cnh_drvChg');
  var corrValEl = document.getElementById('dc_cnh_corrVal');
  var startEl   = document.getElementById('dc_cnh_start');
  var endEl     = document.getElementById('dc_cnh_end');
  var fillEl    = document.getElementById('dc_cnh_fill');
  var minR      = document.getElementById('dc_cnh_minR');
  var maxR      = document.getElementById('dc_cnh_maxR');
  minR.max = maxR.max = N - 1;

  var AUD_COLOR = '#5b9bd5';
  var DRV_COLOR = '#c084fc';

  var chart = new Chart(document.getElementById('dc_cnh_chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'AUD/USD', data: [], borderColor: AUD_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_aud' },
      { label: 'USD/CNY', data: [], borderColor: DRV_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_drv' },
    ]},
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: 'rgba(255,255,255,.35)', font: { size: 11 } },
             grid: { color: 'rgba(142,162,189,.08)' } },
        y_aud: { position: 'left',
                 ticks: { color: AUD_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(4); } },
                 grid: { color: 'rgba(142,162,189,.08)' } },
        y_drv: { position: 'right', grid: { drawOnChartArea: false },
                 ticks: { color: DRV_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(3); } } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(10,18,35,.92)', borderColor: 'rgba(255,255,255,.1)',
          borderWidth: 1, titleColor: 'rgba(255,255,255,.6)', bodyColor: '#fff',
          callbacks: {
            label: function(ctx) {
              if (ctx.datasetIndex === 0) return ' AUD/USD: ' + (ctx.raw || 0).toFixed(4);
              return ' USD/CNY: ' + (ctx.raw || 0).toFixed(4);
            }
          }
        }
      }
    }
  });

  function fmtPct(val, prev) {
    if (!val || !prev) return '';
    var p = (val - prev) / prev * 100;
    return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  }

  function applySlice() {
    var lo = parseInt(minR.value), hi = parseInt(maxR.value) + 1;
    if (hi > N) hi = N;
    if (lo >= hi - 1) lo = Math.max(0, hi - 2);
    var slice = SER.slice(lo, hi);

    chart.data.labels           = slice.map(function(r){ return r.date; });
    chart.data.datasets[0].data = slice.map(function(r){ return r.aud; });
    chart.data.datasets[1].data = slice.map(function(r){ return r.drv; });
    chart.update('none');

    var last = slice[slice.length - 1] || {};
    var prev = slice[slice.length - 2] || {};
    audValEl.textContent = last.aud ? last.aud.toFixed(4) : '\\u2014';
    audChgEl.textContent = fmtPct(last.aud, prev.aud) || '\\u2014';
    drvValEl.textContent = last.drv ? last.drv.toFixed(4) : '\\u2014';
    drvChgEl.textContent = fmtPct(last.drv, prev.drv) || '\\u2014';

    var corrs = slice.map(function(r){ return r[activeCorrKey]; }).filter(function(v){ return v != null; });
    var avg = corrs.length ? corrs.reduce(function(a,b){ return a+b; }, 0) / corrs.length : null;
    corrValEl.textContent = avg !== null ? avg.toFixed(2) : '\\u2014';
    corrValEl.style.color = avg === null ? '' : (avg > 0.1 ? '#6fcf8e' : avg < -0.1 ? '#e8746a' : 'var(--text)');

    startEl.textContent = (slice[0] || {}).date || '\\u2014';
    endEl.textContent   = last.date || '\\u2014';
    var pLo = lo / (N-1) * 100, pHi = parseInt(maxR.value) / (N-1) * 100;
    fillEl.style.left = pLo + '%'; fillEl.style.width = (pHi - pLo) + '%';
  }

  minR.addEventListener('input', function(){
    if (parseInt(minR.value) >= parseInt(maxR.value)) minR.value = parseInt(maxR.value) - 1;
    applySlice();
  });
  maxR.addEventListener('input', function(){
    if (parseInt(maxR.value) <= parseInt(minR.value)) maxR.value = parseInt(minR.value) + 1;
    applySlice();
  });

  document.getElementById('dc_cnh_presets').addEventListener('click', function(e){
    var btn = e.target.closest('[data-y]');
    if (!btn) return;
    var yrs = parseInt(btn.dataset.y), lo = 0;
    if (yrs > 0) {
      var cutoff = new Date(SER[N-1].date);
      cutoff.setFullYear(cutoff.getFullYear() - yrs);
      var ct = cutoff.toISOString().slice(0,10);
      for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    }
    minR.value = lo; maxR.value = N - 1;
    document.querySelectorAll('#dc_cnh_presets .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  document.getElementById('dc_cnh_corrToggle').addEventListener('click', function(e){
    var btn = e.target.closest('[data-w]');
    if (!btn) return;
    activeCorrKey = btn.dataset.w === '60' ? 'r60' : 'r20';
    document.querySelectorAll('#dc_cnh_corrToggle .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  (function(){
    var cutoff = new Date(SER[N-1].date); cutoff.setFullYear(cutoff.getFullYear() - 1);
    var ct = cutoff.toISOString().slice(0,10), lo = 0;
    for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    minR.value = lo; maxR.value = N - 1; applySlice();
  })();
})();
</script>"""

# ---------------------------------------------------------------------------
# AUD/USD vs Iron Ore individual chart
# ---------------------------------------------------------------------------
DC_IRON_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD &mdash; latest</div>
    <div class="val" id="dc_iron_audVal">&mdash;</div>
    <div class="chg" id="dc_iron_audChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#e09438">Iron ore &mdash; latest</div>
    <div class="val" id="dc_iron_drvVal">&mdash;</div>
    <div class="chg" id="dc_iron_drvChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl">Window correlation</div>
    <div class="val" id="dc_iron_corrVal">&mdash;</div>
    <div id="dc_iron_corrToggle" class="presets" style="margin-top:8px">
      <button class="preset active" data-w="20">20d</button>
      <button class="preset" data-w="60">60d</button>
    </div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD and Iron Ore</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot" style="background:#e09438"></span>Iron ore USD/t (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="dc_iron_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="dc_iron_start">&mdash;</b> &nbsp;&rarr;&nbsp; <b id="dc_iron_end">&mdash;</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="dc_iron_fill"></div>
      <input type="range" id="dc_iron_minR" min="0" max="100" value="0">
      <input type="range" id="dc_iron_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="dc_iron_presets">
      <button class="preset active" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source">Source: Yahoo Finance (SGX iron ore futures). Rolling Pearson correlation of log-returns vs AUD/USD, selected range.</p>
"""

DC_IRON_SCRIPT = """<script>
(function(){
  var SER = DRIVER_SERIES['iron_ore'];
  if (!SER || !SER.length) return;
  var N = SER.length;
  var activeCorrKey = 'r20';

  var audValEl  = document.getElementById('dc_iron_audVal');
  var audChgEl  = document.getElementById('dc_iron_audChg');
  var drvValEl  = document.getElementById('dc_iron_drvVal');
  var drvChgEl  = document.getElementById('dc_iron_drvChg');
  var corrValEl = document.getElementById('dc_iron_corrVal');
  var startEl   = document.getElementById('dc_iron_start');
  var endEl     = document.getElementById('dc_iron_end');
  var fillEl    = document.getElementById('dc_iron_fill');
  var minR      = document.getElementById('dc_iron_minR');
  var maxR      = document.getElementById('dc_iron_maxR');
  minR.max = maxR.max = N - 1;

  var AUD_COLOR = '#5b9bd5';
  var DRV_COLOR = '#e09438';

  var chart = new Chart(document.getElementById('dc_iron_chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'AUD/USD', data: [], borderColor: AUD_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_aud' },
      { label: 'Iron ore', data: [], borderColor: DRV_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_drv' },
    ]},
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: 'rgba(255,255,255,.35)', font: { size: 11 } },
             grid: { color: 'rgba(142,162,189,.08)' } },
        y_aud: { position: 'left',
                 ticks: { color: AUD_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(4); } },
                 grid: { color: 'rgba(142,162,189,.08)' } },
        y_drv: { position: 'right', grid: { drawOnChartArea: false },
                 ticks: { color: DRV_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(1); } } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(10,18,35,.92)', borderColor: 'rgba(255,255,255,.1)',
          borderWidth: 1, titleColor: 'rgba(255,255,255,.6)', bodyColor: '#fff',
          callbacks: {
            label: function(ctx) {
              if (ctx.datasetIndex === 0) return ' AUD/USD: ' + (ctx.raw || 0).toFixed(4);
              return ' Iron ore: $' + (ctx.raw || 0).toFixed(2) + '/t';
            }
          }
        }
      }
    }
  });

  function fmtPct(val, prev) {
    if (!val || !prev) return '';
    var p = (val - prev) / prev * 100;
    return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  }

  function applySlice() {
    var lo = parseInt(minR.value), hi = parseInt(maxR.value) + 1;
    if (hi > N) hi = N;
    if (lo >= hi - 1) lo = Math.max(0, hi - 2);
    var slice = SER.slice(lo, hi);

    chart.data.labels           = slice.map(function(r){ return r.date; });
    chart.data.datasets[0].data = slice.map(function(r){ return r.aud; });
    chart.data.datasets[1].data = slice.map(function(r){ return r.drv; });
    chart.update('none');

    var last = slice[slice.length - 1] || {};
    var prev = slice[slice.length - 2] || {};
    audValEl.textContent = last.aud ? last.aud.toFixed(4) : '\\u2014';
    audChgEl.textContent = fmtPct(last.aud, prev.aud) || '\\u2014';
    drvValEl.textContent = last.drv ? '$' + last.drv.toFixed(2) : '\\u2014';
    drvChgEl.textContent = fmtPct(last.drv, prev.drv) || '\\u2014';

    var corrs = slice.map(function(r){ return r[activeCorrKey]; }).filter(function(v){ return v != null; });
    var avg = corrs.length ? corrs.reduce(function(a,b){ return a+b; }, 0) / corrs.length : null;
    corrValEl.textContent = avg !== null ? avg.toFixed(2) : '\\u2014';
    corrValEl.style.color = avg === null ? '' : (avg > 0.1 ? '#6fcf8e' : avg < -0.1 ? '#e8746a' : 'var(--text)');

    startEl.textContent = (slice[0] || {}).date || '\\u2014';
    endEl.textContent   = last.date || '\\u2014';
    var pLo = lo / (N-1) * 100, pHi = parseInt(maxR.value) / (N-1) * 100;
    fillEl.style.left = pLo + '%'; fillEl.style.width = (pHi - pLo) + '%';
  }

  minR.addEventListener('input', function(){
    if (parseInt(minR.value) >= parseInt(maxR.value)) minR.value = parseInt(maxR.value) - 1;
    applySlice();
  });
  maxR.addEventListener('input', function(){
    if (parseInt(maxR.value) <= parseInt(minR.value)) maxR.value = parseInt(minR.value) + 1;
    applySlice();
  });

  document.getElementById('dc_iron_presets').addEventListener('click', function(e){
    var btn = e.target.closest('[data-y]');
    if (!btn) return;
    var yrs = parseInt(btn.dataset.y), lo = 0;
    if (yrs > 0) {
      var cutoff = new Date(SER[N-1].date);
      cutoff.setFullYear(cutoff.getFullYear() - yrs);
      var ct = cutoff.toISOString().slice(0,10);
      for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    }
    minR.value = lo; maxR.value = N - 1;
    document.querySelectorAll('#dc_iron_presets .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  document.getElementById('dc_iron_corrToggle').addEventListener('click', function(e){
    var btn = e.target.closest('[data-w]');
    if (!btn) return;
    activeCorrKey = btn.dataset.w === '60' ? 'r60' : 'r20';
    document.querySelectorAll('#dc_iron_corrToggle .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  (function(){
    var cutoff = new Date(SER[N-1].date); cutoff.setFullYear(cutoff.getFullYear() - 1);
    var ct = cutoff.toISOString().slice(0,10), lo = 0;
    for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    minR.value = lo; maxR.value = N - 1; applySlice();
  })();
})();
</script>"""

# ---------------------------------------------------------------------------
# AUD/USD vs DXY individual chart
# ---------------------------------------------------------------------------
DC_DXY_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD &mdash; latest</div>
    <div class="val" id="dc_dxy_audVal">&mdash;</div>
    <div class="chg" id="dc_dxy_audChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#f05a52">DXY &mdash; latest</div>
    <div class="val" id="dc_dxy_drvVal">&mdash;</div>
    <div class="chg" id="dc_dxy_drvChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl">Window correlation</div>
    <div class="val" id="dc_dxy_corrVal">&mdash;</div>
    <div id="dc_dxy_corrToggle" class="presets" style="margin-top:8px">
      <button class="preset active" data-w="20">20d</button>
      <button class="preset" data-w="60">60d</button>
    </div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD and DXY</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot" style="background:#f05a52"></span>DXY (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="dc_dxy_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="dc_dxy_start">&mdash;</b> &nbsp;&rarr;&nbsp; <b id="dc_dxy_end">&mdash;</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="dc_dxy_fill"></div>
      <input type="range" id="dc_dxy_minR" min="0" max="100" value="0">
      <input type="range" id="dc_dxy_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="dc_dxy_presets">
      <button class="preset active" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source">Source: Yahoo Finance. DXY excluded from OLS regression (collinear with USD/CNY) &mdash; shown for reference. Rolling Pearson correlation of log-returns vs AUD/USD, selected range.</p>
"""

DC_DXY_SCRIPT = """<script>
(function(){
  var SER = DRIVER_SERIES['dxy'];
  if (!SER || !SER.length) return;
  var N = SER.length;
  var activeCorrKey = 'r20';

  var audValEl  = document.getElementById('dc_dxy_audVal');
  var audChgEl  = document.getElementById('dc_dxy_audChg');
  var drvValEl  = document.getElementById('dc_dxy_drvVal');
  var drvChgEl  = document.getElementById('dc_dxy_drvChg');
  var corrValEl = document.getElementById('dc_dxy_corrVal');
  var startEl   = document.getElementById('dc_dxy_start');
  var endEl     = document.getElementById('dc_dxy_end');
  var fillEl    = document.getElementById('dc_dxy_fill');
  var minR      = document.getElementById('dc_dxy_minR');
  var maxR      = document.getElementById('dc_dxy_maxR');
  minR.max = maxR.max = N - 1;

  var AUD_COLOR = '#5b9bd5';
  var DRV_COLOR = '#f05a52';

  var chart = new Chart(document.getElementById('dc_dxy_chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'AUD/USD', data: [], borderColor: AUD_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_aud' },
      { label: 'DXY', data: [], borderColor: DRV_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_drv' },
    ]},
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: 'rgba(255,255,255,.35)', font: { size: 11 } },
             grid: { color: 'rgba(142,162,189,.08)' } },
        y_aud: { position: 'left',
                 ticks: { color: AUD_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(4); } },
                 grid: { color: 'rgba(142,162,189,.08)' } },
        y_drv: { position: 'right', grid: { drawOnChartArea: false },
                 ticks: { color: DRV_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(1); } } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(10,18,35,.92)', borderColor: 'rgba(255,255,255,.1)',
          borderWidth: 1, titleColor: 'rgba(255,255,255,.6)', bodyColor: '#fff',
          callbacks: {
            label: function(ctx) {
              if (ctx.datasetIndex === 0) return ' AUD/USD: ' + (ctx.raw || 0).toFixed(4);
              return ' DXY: ' + (ctx.raw || 0).toFixed(2);
            }
          }
        }
      }
    }
  });

  function fmtPct(val, prev) {
    if (!val || !prev) return '';
    var p = (val - prev) / prev * 100;
    return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  }

  function applySlice() {
    var lo = parseInt(minR.value), hi = parseInt(maxR.value) + 1;
    if (hi > N) hi = N;
    if (lo >= hi - 1) lo = Math.max(0, hi - 2);
    var slice = SER.slice(lo, hi);

    chart.data.labels           = slice.map(function(r){ return r.date; });
    chart.data.datasets[0].data = slice.map(function(r){ return r.aud; });
    chart.data.datasets[1].data = slice.map(function(r){ return r.drv; });
    chart.update('none');

    var last = slice[slice.length - 1] || {};
    var prev = slice[slice.length - 2] || {};
    audValEl.textContent = last.aud ? last.aud.toFixed(4) : '\\u2014';
    audChgEl.textContent = fmtPct(last.aud, prev.aud) || '\\u2014';
    drvValEl.textContent = last.drv ? last.drv.toFixed(2) : '\\u2014';
    drvChgEl.textContent = fmtPct(last.drv, prev.drv) || '\\u2014';

    var corrs = slice.map(function(r){ return r[activeCorrKey]; }).filter(function(v){ return v != null; });
    var avg = corrs.length ? corrs.reduce(function(a,b){ return a+b; }, 0) / corrs.length : null;
    corrValEl.textContent = avg !== null ? avg.toFixed(2) : '\\u2014';
    corrValEl.style.color = avg === null ? '' : (avg > 0.1 ? '#6fcf8e' : avg < -0.1 ? '#e8746a' : 'var(--text)');

    startEl.textContent = (slice[0] || {}).date || '\\u2014';
    endEl.textContent   = last.date || '\\u2014';
    var pLo = lo / (N-1) * 100, pHi = parseInt(maxR.value) / (N-1) * 100;
    fillEl.style.left = pLo + '%'; fillEl.style.width = (pHi - pLo) + '%';
  }

  minR.addEventListener('input', function(){
    if (parseInt(minR.value) >= parseInt(maxR.value)) minR.value = parseInt(maxR.value) - 1;
    applySlice();
  });
  maxR.addEventListener('input', function(){
    if (parseInt(maxR.value) <= parseInt(minR.value)) maxR.value = parseInt(minR.value) + 1;
    applySlice();
  });

  document.getElementById('dc_dxy_presets').addEventListener('click', function(e){
    var btn = e.target.closest('[data-y]');
    if (!btn) return;
    var yrs = parseInt(btn.dataset.y), lo = 0;
    if (yrs > 0) {
      var cutoff = new Date(SER[N-1].date);
      cutoff.setFullYear(cutoff.getFullYear() - yrs);
      var ct = cutoff.toISOString().slice(0,10);
      for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    }
    minR.value = lo; maxR.value = N - 1;
    document.querySelectorAll('#dc_dxy_presets .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  document.getElementById('dc_dxy_corrToggle').addEventListener('click', function(e){
    var btn = e.target.closest('[data-w]');
    if (!btn) return;
    activeCorrKey = btn.dataset.w === '60' ? 'r60' : 'r20';
    document.querySelectorAll('#dc_dxy_corrToggle .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  (function(){
    var cutoff = new Date(SER[N-1].date); cutoff.setFullYear(cutoff.getFullYear() - 1);
    var ct = cutoff.toISOString().slice(0,10), lo = 0;
    for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    minR.value = lo; maxR.value = N - 1; applySlice();
  })();
})();
</script>"""

DC_VIX_HTML = """
<div class="stats">
  <div class="card">
    <div class="lbl"><span class="dot aud"></span>AUD/USD &mdash; latest</div>
    <div class="val" id="dc_vix_audVal">&mdash;</div>
    <div class="chg" id="dc_vix_audChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl" style="color:#facc15">VIX &mdash; latest</div>
    <div class="val" id="dc_vix_drvVal">&mdash;</div>
    <div class="chg" id="dc_vix_drvChg">&mdash;</div>
  </div>
  <div class="card">
    <div class="lbl">Window correlation</div>
    <div class="val" id="dc_vix_corrVal">&mdash;</div>
    <div id="dc_vix_corrToggle" class="presets" style="margin-top:8px">
      <button class="preset active" data-w="20">20d</button>
      <button class="preset" data-w="60">60d</button>
    </div>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD and VIX</div>
  <div class="legend">
    <span><span class="dot aud"></span>AUD/USD (LHS)</span>
    <span><span class="dot" style="background:#facc15"></span>VIX (RHS)</span>
  </div>
  <div class="chart-holder"><canvas id="dc_vix_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="dc_vix_start">&mdash;</b> &nbsp;&rarr;&nbsp; <b id="dc_vix_end">&mdash;</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="dc_vix_fill"></div>
      <input type="range" id="dc_vix_minR" min="0" max="100" value="0">
      <input type="range" id="dc_vix_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="dc_vix_presets">
      <button class="preset active" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset" data-y="0">Max</button>
    </div>
  </div>
</div>
<p class="source">Source: Yahoo Finance (^VIX). VIX is the equity-market volatility (&ldquo;fear&rdquo;) gauge &mdash; risk-off spikes typically coincide with AUD/USD weakness (negative correlation). Rolling Pearson correlation of log-returns vs AUD/USD, selected range.</p>
"""

DC_VIX_SCRIPT = """<script>
(function(){
  var SER = DRIVER_SERIES['vix'];
  if (!SER || !SER.length) return;
  var N = SER.length;
  var activeCorrKey = 'r20';

  var audValEl  = document.getElementById('dc_vix_audVal');
  var audChgEl  = document.getElementById('dc_vix_audChg');
  var drvValEl  = document.getElementById('dc_vix_drvVal');
  var drvChgEl  = document.getElementById('dc_vix_drvChg');
  var corrValEl = document.getElementById('dc_vix_corrVal');
  var startEl   = document.getElementById('dc_vix_start');
  var endEl     = document.getElementById('dc_vix_end');
  var fillEl    = document.getElementById('dc_vix_fill');
  var minR      = document.getElementById('dc_vix_minR');
  var maxR      = document.getElementById('dc_vix_maxR');
  minR.max = maxR.max = N - 1;

  var AUD_COLOR = '#5b9bd5';
  var DRV_COLOR = '#facc15';

  var chart = new Chart(document.getElementById('dc_vix_chart').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'AUD/USD', data: [], borderColor: AUD_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_aud' },
      { label: 'VIX', data: [], borderColor: DRV_COLOR, borderWidth: 1.5,
        pointRadius: 0, tension: 0.2, spanGaps: true, yAxisID: 'y_drv' },
    ]},
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: 'rgba(255,255,255,.35)', font: { size: 11 } },
             grid: { color: 'rgba(142,162,189,.08)' } },
        y_aud: { position: 'left',
                 ticks: { color: AUD_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(4); } },
                 grid: { color: 'rgba(142,162,189,.08)' } },
        y_drv: { position: 'right', grid: { drawOnChartArea: false },
                 ticks: { color: DRV_COLOR, font: { size: 11 }, callback: function(v){ return v.toFixed(1); } } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(10,18,35,.92)', borderColor: 'rgba(255,255,255,.1)',
          borderWidth: 1, titleColor: 'rgba(255,255,255,.6)', bodyColor: '#fff',
          callbacks: {
            label: function(ctx) {
              if (ctx.datasetIndex === 0) return ' AUD/USD: ' + (ctx.raw || 0).toFixed(4);
              return ' VIX: ' + (ctx.raw || 0).toFixed(2);
            }
          }
        }
      }
    }
  });

  function fmtPct(val, prev) {
    if (!val || !prev) return '';
    var p = (val - prev) / prev * 100;
    return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  }

  function applySlice() {
    var lo = parseInt(minR.value), hi = parseInt(maxR.value) + 1;
    if (hi > N) hi = N;
    if (lo >= hi - 1) lo = Math.max(0, hi - 2);
    var slice = SER.slice(lo, hi);

    chart.data.labels           = slice.map(function(r){ return r.date; });
    chart.data.datasets[0].data = slice.map(function(r){ return r.aud; });
    chart.data.datasets[1].data = slice.map(function(r){ return r.drv; });
    chart.update('none');

    var last = slice[slice.length - 1] || {};
    var prev = slice[slice.length - 2] || {};
    audValEl.textContent = last.aud ? last.aud.toFixed(4) : '\\u2014';
    audChgEl.textContent = fmtPct(last.aud, prev.aud) || '\\u2014';
    drvValEl.textContent = last.drv ? last.drv.toFixed(2) : '\\u2014';
    drvChgEl.textContent = fmtPct(last.drv, prev.drv) || '\\u2014';

    var corrs = slice.map(function(r){ return r[activeCorrKey]; }).filter(function(v){ return v != null; });
    var avg = corrs.length ? corrs.reduce(function(a,b){ return a+b; }, 0) / corrs.length : null;
    corrValEl.textContent = avg !== null ? avg.toFixed(2) : '\\u2014';
    corrValEl.style.color = avg === null ? '' : (avg > 0.1 ? '#6fcf8e' : avg < -0.1 ? '#e8746a' : 'var(--text)');

    startEl.textContent = (slice[0] || {}).date || '\\u2014';
    endEl.textContent   = last.date || '\\u2014';
    var pLo = lo / (N-1) * 100, pHi = parseInt(maxR.value) / (N-1) * 100;
    fillEl.style.left = pLo + '%'; fillEl.style.width = (pHi - pLo) + '%';
  }

  minR.addEventListener('input', function(){
    if (parseInt(minR.value) >= parseInt(maxR.value)) minR.value = parseInt(maxR.value) - 1;
    applySlice();
  });
  maxR.addEventListener('input', function(){
    if (parseInt(maxR.value) <= parseInt(minR.value)) maxR.value = parseInt(minR.value) + 1;
    applySlice();
  });

  document.getElementById('dc_vix_presets').addEventListener('click', function(e){
    var btn = e.target.closest('[data-y]');
    if (!btn) return;
    var yrs = parseInt(btn.dataset.y), lo = 0;
    if (yrs > 0) {
      var cutoff = new Date(SER[N-1].date);
      cutoff.setFullYear(cutoff.getFullYear() - yrs);
      var ct = cutoff.toISOString().slice(0,10);
      for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    }
    minR.value = lo; maxR.value = N - 1;
    document.querySelectorAll('#dc_vix_presets .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  document.getElementById('dc_vix_corrToggle').addEventListener('click', function(e){
    var btn = e.target.closest('[data-w]');
    if (!btn) return;
    activeCorrKey = btn.dataset.w === '60' ? 'r60' : 'r20';
    document.querySelectorAll('#dc_vix_corrToggle .preset').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    applySlice();
  });

  (function(){
    var cutoff = new Date(SER[N-1].date); cutoff.setFullYear(cutoff.getFullYear() - 1);
    var ct = cutoff.toISOString().slice(0,10), lo = 0;
    for (var i = 0; i < N; i++) { if (SER[i].date >= ct) { lo = i; break; } }
    minR.value = lo; maxR.value = N - 1; applySlice();
  })();
})();
</script>"""

# ---------------------------------------------------------------------------
# DRIVER ATTRIBUTION PANEL  (replaces slider-based beta sensitivity panel)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# EVENT CALENDAR  (single source of truth — auto-rolls by today's date)
# ---------------------------------------------------------------------------
# One list drives BOTH the forward "Event Risk Calendar" and the trailing
# "Previous Calendar Events" table. Classification is automatic:
#   • end >= today            → upcoming  (forward table, shows `consensus`)
#   • cutoff <= end < today    → previous  (last-30-days table, shows `outcome`)
# To maintain: add new events with a `consensus`; once an event has happened,
# fill its `outcome`. No need to move rows or touch dates by hand — the date
# math below sorts everything into the right table on each run.
#   date      = ISO start date (used for ordering + the display label)
#   end       = ISO end date for multi-day events (optional; defaults to date)
#   label     = display override, e.g. "16–17 Jun" or "~26 Jun" (optional)
#   impact    = 'high' | 'med' | 'low'
#   outcome   = recap once it has occurred; if missing once past, shows a
#               "recap pending" placeholder so it's obvious it needs filling.
EVENTS = [
    # ── already occurred ──
    {'date': '2026-05-13', 'name': 'US CPI (Apr)', 'impact': 'high', 'sens': 'Via USD &amp; risk sentiment',
     'consensus': 'Core CPI key for FOMC dot-plot guidance.',
     'outcome': 'Core CPI modestly below consensus; USD softened on print, AUD and risk assets recovered. FOMC cut expectations edged forward'},
    {'date': '2026-05-15', 'name': 'AU Employment (Apr)', 'impact': 'high', 'sens': 'Direct — RBA reaction function',
     'consensus': 'Labour force survey; key RBA input.',
     'outcome': 'Labour market softened; unemployment nudged higher, reinforcing RBA cut optionality. Lifted Jun cut probability to ~24%'},
    {'date': '2026-05-16', 'name': 'CN Industrial Output / Retail (Apr)', 'impact': 'med', 'sens': 'Via China demand channel',
     'consensus': 'April activity data.',
     'outcome': 'Industrial output in line with estimates; retail sales below consensus. Soft domestic demand weighed on iron ore and CNH'},
    {'date': '2026-05-20', 'name': 'RBA Meeting Minutes', 'impact': 'med', 'sens': 'Direct — RBA guidance',
     'consensus': 'Minutes of the May board meeting.',
     'outcome': 'Minutes showed board discussed case for a cut; dovish lean confirmed. AUD slipped on release as markets firmed Jun cut bets'},
    {'date': '2026-05-23', 'name': 'CFTC COT Release (May-20 snapshot)', 'impact': 'med', 'sens': 'Positioning signal',
     'consensus': 'Weekly positioning snapshot.',
     'outcome': 'Positioning showed AM net longs rebuilding; LF net positioning continued to extend, broadly supportive backdrop for AUD'},
    {'date': '2026-05-27', 'name': 'US PCE (Apr)', 'impact': 'high', 'sens': 'Via Fed pricing &amp; USD',
     'consensus': "Fed's preferred inflation measure.",
     'outcome': 'Core PCE in line with expectations; no fresh hawkish catalyst. Fed cut timing little changed, AUD steady into month-end'},
    {'date': '2026-05-29', 'end': '2026-05-30', 'label': '29–30 May', 'name': 'China NBS PMI (May)', 'impact': 'med', 'sens': 'Via China demand &amp; CNH',
     'consensus': 'Manufacturing + services PMI.',
     'outcome': 'Manufacturing PMI held in expansion; services solid. Positive signal for commodity demand and AUD via China growth channel'},
    {'date': '2026-06-02', 'name': 'AU GDP (Q1 2026)', 'impact': 'high', 'sens': 'Direct — growth / RBA outlook',
     'consensus': 'Q1 national accounts.',
     'outcome': 'Growth modest, confirming subdued domestic demand. Consistent with RBA cutting — AUD offered on release before stabilising'},
    {'date': '2026-06-03', 'name': 'China Caixin Services PMI (May)', 'impact': 'med', 'sens': 'Via China demand channel',
     'consensus': 'Private-survey services gauge.',
     'outcome': 'Services PMI above 52, confirming expansion in the services sector. Positive sentiment for risk assets and AUD near-term'},
    {'date': '2026-06-05', 'name': 'AU Trade Balance (Apr)', 'impact': 'med', 'sens': 'Terms of trade signal',
     'consensus': 'Goods + services trade balance.',
     'outcome': 'Trade surplus narrowed as commodity export values softened with iron ore prices; import demand subdued domestically'},
    {'date': '2026-06-06', 'name': 'US Nonfarm Payrolls (May)', 'impact': 'high', 'sens': 'Via USD &amp; risk sentiment',
     'consensus': 'May payrolls + wage growth.',
     'outcome': 'Payrolls broadly in line, wage growth easing at the margin. USD softened modestly; AUD/USD held firm near the top of its recent range'},
    {'date': '2026-06-07', 'end': '2026-06-09', 'label': '7–9 Jun', 'name': 'China Trade Balance (May)', 'impact': 'high', 'sens': 'Via CNH &amp; commodity demand',
     'consensus': 'Exports + imports gauge.',
     'outcome': 'Exports steady; imports still soft, pointing to subdued domestic demand. Mixed for iron ore and the commodity-AUD channel'},
    {'date': '2026-06-10', 'name': 'China CPI / PPI (May)', 'impact': 'med', 'sens': 'Via commodity channel',
     'consensus': 'Inflation + factory-gate prices.',
     'outcome': 'PPI deflation persisted but showed tentative stabilisation; CPI subdued. Limited fresh impulse for the iron ore demand story'},
    {'date': '2026-06-11', 'name': 'US CPI (May)', 'impact': 'high', 'sens': 'Via USD &amp; risk sentiment',
     'consensus': 'Core CPI ahead of the FOMC.',
     'outcome': 'Core CPI in line to slightly soft; reinforced market pricing for Fed cuts later in the year. USD eased, AUD supported into the FOMC'},
    # ── upcoming (fill `outcome` once each has occurred) ──
    {'date': '2026-06-12', 'name': 'AU Employment (May)', 'impact': 'high', 'sens': 'Direct — RBA reaction function',
     'consensus': 'Labour force survey — last major domestic input before the RBA. Weak jobs → Jul/Aug cut probability rises sharply'},
    {'date': '2026-06-13', 'name': 'CFTC COT Release', 'impact': 'med', 'sens': 'Positioning signal',
     'consensus': 'Jun-09 snapshot; track AM net vs prior ~+1k and LF net vs ~+59k. Rebuild in net longs = positioning tailwind for AUD'},
    {'date': '2026-06-16', 'name': 'CN Industrial Output / Retail (May)', 'impact': 'med', 'sens': 'Via China demand channel',
     'consensus': 'May activity data; industrial output drives iron ore demand, retail a proxy for domestic stimulus traction'},
    {'date': '2026-06-16', 'end': '2026-06-17', 'label': '16–17 Jun', 'name': 'RBA Board Meeting', 'impact': 'high', 'sens': 'Primary AUD driver',
     'consensus': 'Rate decision + press conference. ~20% cut probability; a cut takes cash to 4.10%. Cut = AUD downside; hold = relief rally'},
    {'date': '2026-06-17', 'end': '2026-06-18', 'label': '17–18 Jun', 'name': 'FOMC Meeting', 'impact': 'high', 'sens': 'Via USD &amp; rate differential',
     'consensus': 'Rate decision + SEP + dot plot. Hold at 4.25–4.50% expected; dot-plot cut timing key for the AU-US rate diff and AUD'},
    {'date': '2026-06-17', 'label': '~17 Jun', 'name': 'US Retail Sales (May)', 'impact': 'med', 'sens': 'Via USD &amp; risk',
     'consensus': 'Consumer spending resilience gauge; a strong print supports USD and compresses AUD/USD'},
    {'date': '2026-06-26', 'label': '~26 Jun', 'name': 'US PCE (May)', 'impact': 'high', 'sens': 'Via Fed pricing &amp; USD',
     'consensus': "Fed's preferred inflation measure; sticky core PCE = fewer cuts priced, USD bid into month-end"},
    {'date': '2026-06-30', 'name': 'China NBS PMI (Jun)', 'impact': 'med', 'sens': 'Via China demand &amp; CNH',
     'consensus': 'Manufacturing + services PMI; early read on Q3 China demand — an AUD proxy for the commodity outlook'},
    {'date': '2026-07-03', 'name': 'US Nonfarm Payrolls (Jun)', 'impact': 'high', 'sens': 'Via USD &amp; risk sentiment',
     'consensus': 'First major US labour read post-FOMC; soft print firms cut expectations, pressures USD and supports AUD'},
    {'date': '2026-07-10', 'label': '~10 Jul', 'name': 'China CPI / PPI (Jun)', 'impact': 'med', 'sens': 'Via commodity channel',
     'consensus': 'Watch for PPI moving out of deflation — a stabilisation signal would support iron ore and the commodity-AUD channel'},
    {'date': '2026-07-15', 'name': 'China Q2 GDP + Activity', 'impact': 'high', 'sens': 'Via China demand &amp; CNH',
     'consensus': 'Q2 GDP with June industrial output and retail sales; primary read on China demand momentum into H2'},
    {'date': '2026-07-17', 'name': 'AU Employment (Jun)', 'impact': 'high', 'sens': 'Direct — RBA reaction function',
     'consensus': 'Labour force survey; sustained softening builds the case for an August RBA cut and weighs on AUD'},
]

_PREV_WINDOW_DAYS = 30  # how far back the "Previous Calendar Events" table reaches

def _ev_label(e):
    """Display label: explicit override, else '12 Jun' from the ISO start date."""
    if e.get('label'):
        return e['label']
    return datetime.strptime(e['date'], '%Y-%m-%d').strftime('%-d %b')

def _ev_rows(events, field):
    """Render <tr> rows for a list of events using `field` ('consensus' or 'outcome')."""
    cls = {'high': 'impact-high', 'med': 'impact-med', 'low': 'impact-low'}
    txt = {'high': 'HIGH', 'med': 'MED', 'low': 'LOW'}
    rows = []
    for e in events:
        imp = e.get('impact', 'med')
        detail = e.get(field)
        if not detail:
            detail = '<span class="dim-text faint-italic">recap pending</span>'
        rows.append(
            '    <tr>\n'
            f'      <td><span class="event-date">{_ev_label(e)}</span></td>\n'
            f'      <td><span class="event-name">{e["name"]}</span></td>\n'
            f'      <td class="event-detail">{detail}</td>\n'
            f'      <td><span class="impact-badge {cls[imp]}">{txt[imp]}</span></td>\n'
            f'      <td class="dim-text">{e["sens"]}</td>\n'
            '    </tr>'
        )
    return '\n'.join(rows)

# Auto-classify against today's date (an event leaves the forward table only
# once its END date has passed; multi-day events stay until fully done).
_today_iso  = today.isoformat()
_cutoff_iso = (today - timedelta(days=_PREV_WINDOW_DAYS)).isoformat()
def _ev_end(e): return e.get('end', e['date'])

_upcoming_events = sorted([e for e in EVENTS if _ev_end(e) >= _today_iso], key=lambda e: e['date'])
_previous_events = sorted([e for e in EVENTS if _cutoff_iso <= _ev_end(e) < _today_iso], key=lambda e: e['date'])

EVENTS_FWD_ROWS  = _ev_rows(_upcoming_events, 'consensus')
EVENTS_PREV_ROWS = _ev_rows(_previous_events, 'outcome')
print(f'Event calendar: {len(_upcoming_events)} upcoming, {len(_previous_events)} in last {_PREV_WINDOW_DAYS}d')

PREV_EVENTS_HTML = f"""
<table class="events-table">
  <thead>
    <tr>
      <th>Date</th>
      <th>Event</th>
      <th>Detail / Outcome</th>
      <th>Impact</th>
      <th>AUD Sensitivity</th>
    </tr>
  </thead>
  <tbody>
{EVENTS_PREV_ROWS}
  </tbody>
</table>
"""

# ---------------------------------------------------------------------------
# HTML TEMPLATE
# ---------------------------------------------------------------------------
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUD/USD · API Brief · {today_short}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>

<style>
/* ── RESET & BASE ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ font-size: 15px; scroll-behavior: smooth; }}

/* ── DESIGN TOKENS ── */
:root {{
  --bg:         #070d18;
  --surface:    #0c1525;
  --panel:      #101e36;
  --panel-edge: #1b2d4f;
  --text:       #e2eaf6;
  --text-dim:   #7a92b4;
  --text-faint: #3d5270;
  --blue:       #4b8ef0;
  --green:      #2fcb9a;
  --red:        #f05a52;
  --gold:       #f0b329;
  --amber:      #e09438;
  --header-h:   64px;
}}

/* ── BODY ── */
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'IBM Plex Mono', monospace;
  line-height: 1.6;
  min-height: 100vh;
}}

/* ── STICKY HEADER ── */
.site-header {{
  position: sticky;
  top: 0;
  z-index: 100;
  height: var(--header-h);
  background: rgba(7,13,24,0.92);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--panel-edge);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 32px;
}}
.header-left h1 {{
  font-family: 'Fraunces', serif;
  font-weight: 700;
  font-size: 1.25rem;
  letter-spacing: -0.01em;
  color: var(--text);
  line-height: 1.2;
}}
.header-left .header-date {{
  font-size: 0.7rem;
  color: var(--text-dim);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-top: 1px;
}}
.header-right {{
  display: flex;
  align-items: center;
  gap: 16px;
}}
.regime-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: rgba(47,203,154,0.12);
  border: 1px solid rgba(47,203,154,0.35);
  color: var(--green);
  font-size: 0.72rem;
  font-weight: 500;
  letter-spacing: 0.1em;
  padding: 5px 12px;
  border-radius: 20px;
  text-transform: uppercase;
}}
.regime-pill::before {{
  content: '';
  width: 6px; height: 6px;
  background: var(--green);
  border-radius: 50%;
  animation: pulse 2s ease-in-out infinite;
}}
@keyframes pulse {{
  0%,100% {{ opacity:1; transform:scale(1); }}
  50%      {{ opacity:0.5; transform:scale(0.85); }}
}}
.api-note {{
  font-size: 0.62rem;
  color: var(--text-faint);
  letter-spacing: 0.04em;
  border-left: 1px solid var(--panel-edge);
  padding-left: 16px;
}}

/* ── MAIN LAYOUT ── */
.main {{
  max-width: 1120px;
  margin: 0 auto;
  padding: 40px 24px 80px;
}}

/* ── SECTION HEADER ── */
.section-header {{
  display: flex;
  align-items: center;
  gap: 14px;
  margin: 52px 0 24px;
}}
.section-header:first-child {{ margin-top: 0; }}
.sec-num {{
  font-family: 'Fraunces', serif;
  font-weight: 600;
  font-size: 1.05rem;
  color: var(--gold);
  flex-shrink: 0;
  line-height: 1;
}}
.sec-title {{
  font-size: 0.72rem;
  font-weight: 500;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-dim);
  flex-shrink: 0;
}}
.sec-line {{
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--panel-edge) 0%, transparent 100%);
}}

/* ── BADGES ── */
.badge {{
  display: inline-block;
  font-size: 0.58rem;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 2px 6px;
  border-radius: 4px;
  vertical-align: middle;
  line-height: 1.4;
}}
.badge-yf   {{ background: rgba(224,148,56,0.18); color: var(--amber); border: 1px solid rgba(224,148,56,0.3); }}
.badge-rba  {{ background: rgba(47,203,154,0.14); color: var(--green); border: 1px solid rgba(47,203,154,0.3); }}
.badge-cftc {{ background: rgba(240,179,41,0.14); color: var(--gold);  border: 1px solid rgba(240,179,41,0.3); }}
.badge-av   {{ background: rgba(75,142,240,0.14); color: var(--blue);  border: 1px solid rgba(75,142,240,0.3); }}
.badge-scr  {{ background: rgba(122,146,180,0.12); color: var(--text-dim); border: 1px solid rgba(122,146,180,0.2); }}

/* ── STAT TILES ── */
.tiles-grid {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}}
@media (max-width: 900px) {{ .tiles-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 480px) {{ .tiles-grid {{ grid-template-columns: 1fr; }} }}

.tile {{
  background: var(--panel);
  border: 1px solid var(--panel-edge);
  border-radius: 10px;
  padding: 16px 18px 14px;
  position: relative;
  overflow: hidden;
  transition: border-color 0.2s, box-shadow 0.2s;
}}
.tile::before {{
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 10px;
  pointer-events: none;
  background: linear-gradient(135deg, rgba(255,255,255,0.025) 0%, transparent 60%);
}}
.tile:hover {{
  border-color: rgba(75,142,240,0.4);
  box-shadow: 0 4px 24px rgba(0,0,0,0.5);
}}

.tile-top {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}}
.tile-label {{
  font-size: 0.62rem;
  font-weight: 500;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-dim);
}}
.tile-value {{
  font-family: 'Fraunces', serif;
  font-weight: 700;
  font-size: 2.1rem;
  line-height: 1;
  letter-spacing: -0.02em;
  margin-bottom: 8px;
}}
.tile-value.mono {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: 1.75rem;
  font-weight: 500;
}}
.tile-bottom {{
  font-size: 0.7rem;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}}
.tile-ref {{
  color: var(--text-faint);
  font-size: 0.62rem;
}}

/* ── COLOR HELPERS ── */
.green  {{ color: var(--green); }}
.red    {{ color: var(--red); }}
.gold   {{ color: var(--gold); }}
.blue   {{ color: var(--blue); }}
.amber  {{ color: var(--amber); }}
.dim-text     {{ color: var(--text-dim); }}
.faint-italic {{ color: var(--text-faint); font-style: italic; }}

/* ── CARDS SIDE BY SIDE ── */
.cards-row {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 16px;
}}
@media (max-width: 600px) {{ .cards-row {{ grid-template-columns: 1fr; }} }}

.mini-card {{
  background: var(--panel);
  border: 1px solid var(--panel-edge);
  border-radius: 10px;
  padding: 16px 20px;
}}
.mini-card-title {{
  font-size: 0.62rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-faint);
  margin-bottom: 10px;
}}
.mini-card-status {{
  font-size: 0.85rem;
  font-weight: 500;
  margin-bottom: 6px;
}}
.mini-card-detail {{
  font-size: 0.72rem;
  color: var(--text-dim);
  line-height: 1.6;
}}
.pending-field {{
  display: inline-block;
  border: 1px dashed var(--panel-edge);
  color: var(--text-faint);
  font-style: italic;
  font-size: 0.7rem;
  padding: 1px 8px;
  border-radius: 4px;
  background: rgba(61,82,112,0.08);
}}

/* ── COT CARDS ── */
.cot-big-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 16px;
}}
@media (max-width: 600px) {{ .cot-big-grid {{ grid-template-columns: 1fr; }} }}

.cot-card {{
  background: var(--panel);
  border: 1px solid var(--panel-edge);
  border-radius: 10px;
  padding: 20px 22px 16px;
  position: relative;
  overflow: hidden;
}}
.cot-card::after {{
  content: '';
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 3px;
  border-radius: 0 0 10px 10px;
}}
.cot-card.am-card::after  {{ background: linear-gradient(90deg, var(--red), transparent); }}
.cot-card.lev-card::after {{ background: linear-gradient(90deg, var(--green), transparent); }}

.cot-card-label {{
  font-size: 0.62rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 8px;
}}
.cot-net-value {{
  font-family: 'Fraunces', serif;
  font-weight: 700;
  font-size: 2.8rem;
  letter-spacing: -0.03em;
  line-height: 1;
  margin-bottom: 10px;
}}
.cot-wow-row {{
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.75rem;
}}
.cot-prev {{
  color: var(--text-faint);
  font-size: 0.68rem;
}}

.cot-compare-row {{
  background: var(--surface);
  border: 1px solid var(--panel-edge);
  border-radius: 8px;
  padding: 12px 18px;
  display: flex;
  gap: 32px;
  flex-wrap: wrap;
  margin-bottom: 14px;
  font-size: 0.78rem;
}}
.cot-compare-item {{
  display: flex;
  flex-direction: column;
  gap: 2px;
}}
.cot-compare-label {{
  font-size: 0.6rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-faint);
}}

.cot-note {{
  font-size: 0.72rem;
  color: var(--text-dim);
  background: rgba(240,179,41,0.06);
  border: 1px solid rgba(240,179,41,0.15);
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 16px;
  line-height: 1.55;
}}
.cot-note strong {{ color: var(--gold); }}

.cot-raw-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.78rem;
}}
.cot-raw-table th {{
  font-size: 0.6rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-faint);
  font-weight: 500;
  padding: 6px 12px;
  text-align: right;
  border-bottom: 1px solid var(--panel-edge);
}}
.cot-raw-table th:first-child {{ text-align: left; }}
.cot-raw-table td {{
  padding: 10px 12px;
  text-align: right;
  font-family: 'IBM Plex Mono', monospace;
  border-bottom: 1px solid rgba(27,45,79,0.4);
}}
.cot-raw-table td:first-child {{ text-align: left; color: var(--text-dim); font-family: inherit; }}
.cot-raw-table tr:last-child td {{ border-bottom: none; }}

/* ── COMMODITIES / MARKETS TABLE ── */
.markets-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}}
.markets-table th {{
  font-size: 0.6rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-faint);
  font-weight: 500;
  padding: 8px 14px;
  text-align: right;
  border-bottom: 1px solid var(--panel-edge);
}}
.markets-table th:first-child, .markets-table th:nth-child(2) {{ text-align: left; }}
.markets-table td {{
  padding: 11px 14px;
  text-align: right;
  font-family: 'IBM Plex Mono', monospace;
  border-bottom: 1px solid rgba(27,45,79,0.4);
  vertical-align: middle;
}}
.markets-table td:first-child {{ text-align: left; font-family: inherit; font-weight: 500; }}
.markets-table td:nth-child(2) {{ text-align: left; font-family: inherit; color: var(--text-dim); font-size: 0.7rem; }}
.markets-table tr:last-child td {{ border-bottom: none; }}
.markets-table tr:hover td {{ background: rgba(16,30,54,0.5); }}

.chg-positive {{ color: var(--green); }}
.chg-negative {{ color: var(--red); }}

/* ── DATA GAP ── */
.data-gap {{
  display: inline-block;
  border: 1px dashed var(--panel-edge);
  color: var(--text-faint);
  font-style: italic;
  font-size: 0.72rem;
  padding: 1px 10px;
  border-radius: 4px;
  background: rgba(61,82,112,0.07);
}}

/* ── EVENTS TABLE ── */
.events-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}}
.events-table th {{
  font-size: 0.6rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-faint);
  font-weight: 500;
  padding: 8px 14px;
  text-align: left;
  border-bottom: 1px solid var(--panel-edge);
}}
.events-table td {{
  padding: 10px 14px;
  border-bottom: 1px solid rgba(27,45,79,0.4);
  vertical-align: middle;
  line-height: 1.5;
}}
.events-table tr:last-child td {{ border-bottom: none; }}
.events-table tr:hover td {{ background: rgba(16,30,54,0.5); }}

.impact-badge {{
  display: inline-block;
  font-size: 0.58rem;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 4px;
}}
.impact-high {{
  background: rgba(240,90,82,0.15);
  color: var(--red);
  border: 1px solid rgba(240,90,82,0.3);
}}
.impact-med {{
  background: rgba(224,148,56,0.14);
  color: var(--amber);
  border: 1px solid rgba(224,148,56,0.3);
}}
.impact-low {{
  background: rgba(122,146,180,0.1);
  color: var(--text-dim);
  border: 1px solid rgba(122,146,180,0.2);
}}
.event-date {{ color: var(--gold); font-family: 'IBM Plex Mono', monospace; font-size: 0.75rem; }}
.event-name {{ font-weight: 500; }}
.event-detail {{ color: var(--text-dim); font-size: 0.72rem; }}

/* ── SURFACE WRAPPER ── */
.section-surface {{
  background: var(--surface);
  border: 1px solid var(--panel-edge);
  border-radius: 12px;
  overflow: hidden;
}}

/* ── FOOTER ── */
.site-footer {{
  border-top: 1px solid var(--panel-edge);
  margin-top: 60px;
  padding-top: 24px;
  font-size: 0.65rem;
  color: var(--text-faint);
  line-height: 1.8;
}}
.footer-sources {{ margin-top: 6px; }}
.footer-sources a {{
  color: var(--text-faint);
  text-decoration: none;
  margin-right: 16px;
  transition: color 0.15s;
}}
.footer-sources a:hover {{ color: var(--text-dim); }}
.footer-row {{
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}}
{CHART_CSS}
</style>
</head>
<body>

<!-- ═══════════════════ STICKY HEADER ═══════════════════ -->
<header class="site-header">
  <div class="header-left">
    <h1>AUD/USD &middot; API Brief</h1>
    <div class="header-date">{today_long} &nbsp;&bull;&nbsp; Data as of {data_date_str}</div>
  </div>
  <div class="header-right">
    <div class="regime-pill">{'AUD BID' if (audusd_chg or 0) >= 0 else 'AUD OFFERED'} &nbsp;&bull;&nbsp; Daily {fmt_pct(audusd_chg) or '—'}</div>
    <div class="api-note">All data via live APIs &middot; No screenshots</div>
  </div>
</header>

<!-- ═══════════════════ MAIN ═══════════════════ -->
<main class="main">

<!-- ─── 01 REGIME SNAPSHOT ─── -->
<div class="section-header">
  <span class="sec-num">01</span>
  <span class="sec-title">Regime Snapshot</span>
  <div class="sec-line"></div>
</div>

<div class="tiles-grid">

  <!-- AUD/USD -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">AUD / USD</span>
      {badge('YF')}
    </div>
    <div class="tile-value green">{data['audusd']:.4f}</div>
    <div class="tile-bottom">
      {change_html(audusd_chg)}
      <span class="tile-ref">prev {data['audusd_prev']:.4f}</span>
    </div>
  </div>

  <!-- DXY -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">DXY Index</span>
      {badge('YF')}
    </div>
    <div class="tile-value red">{data['dxy']:.3f}</div>
    <div class="tile-bottom">
      {change_html(dxy_chg, invert=True)}
      <span class="tile-ref">prev {data['dxy_prev']:.3f}</span>
    </div>
  </div>

  <!-- AU-US 2y Spread -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">AU&minus;US 2y Spread</span>
      {badge('RBA')}{badge(_us2y_src)}
    </div>
    <div class="tile-value gold">{data['spread_2y']} bp</div>
    <div class="tile-bottom">
      <span class="dim-text">AU {data['au2y']:.3f}% &nbsp;|&nbsp; US {data['us2y']:.2f}%</span>
    </div>
  </div>

  <!-- Gold -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">Gold (GC=F)</span>
      {badge('YF')}
    </div>
    <div class="tile-value gold">{data['gold']:,.2f}</div>
    <div class="tile-bottom">
      {change_html(gold_chg)}
      <span class="tile-ref">prev {data['gold_prev']:,.2f}</span>
    </div>
  </div>

  <!-- AUD/CNY -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">AUD / CNY</span>
      {badge('YF')}
    </div>
    <div class="tile-value green">{data['audcny']:.3f}</div>
    <div class="tile-bottom">
      {change_html(audcny_chg)}
      <span class="tile-ref">prev {data['audcny_prev']:.3f}</span>
    </div>
  </div>

  <!-- USD/CNH -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">USD / CNH</span>
      {badge('YF')}
    </div>
    <div class="tile-value mono dim-text">{data['usdcnh']:.4f}</div>
    <div class="tile-bottom">
      <span class="data-gap">prev close n/a</span>
    </div>
  </div>

  <!-- VIX -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">VIX</span>
      {badge('YF')}
    </div>
    <div class="tile-value green">{data['vix']:.2f}</div>
    <div class="tile-bottom">
      {change_html(vix_chg, invert=True)}
      <span class="tile-ref">prev {data['vix_prev']:.2f}</span>
    </div>
  </div>

  <!-- S&P 500 -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">S&amp;P 500</span>
      {badge('YF')}
    </div>
    <div class="tile-value green">{data['spx']:,.2f}</div>
    <div class="tile-bottom">
      {change_html(spx_chg)}
      <span class="tile-ref">prev {data['spx_prev']:,.2f}</span>
    </div>
  </div>

</div><!-- /tiles-grid -->


<!-- ─── AUD/USD vs S&P 500 ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-num">02</span>
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">AUD/USD &amp; S&amp;P 500</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {DC_SPX_HTML}
</div>


<!-- ─── AUD/USD vs USD/CNY ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-num">03</span>
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">AUD/USD &amp; USD/CNY</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {DC_CNH_HTML}
</div>


<!-- ─── AUD/USD vs Iron Ore ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-num">04</span>
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">AUD/USD &amp; IRON ORE</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {DC_IRON_HTML}
</div>


<!-- ─── AUD/USD vs DXY ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-num">05</span>
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">AUD/USD &amp; DXY</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {DC_DXY_HTML}
</div>


<!-- ─── AUD/USD vs VIX ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-num">06</span>
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">AUD/USD &amp; VIX</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {DC_VIX_HTML}
</div>



<!-- ─── 07 RATE DIFFERENTIALS ─── -->
<div class="section-header">
  <span class="sec-num">07</span>
  <span class="sec-title">Rate Differentials &amp; Yield Curve</span>
  <div class="sec-line"></div>
</div>

{RD_CHART_HTML}



<!-- ─── 08 COT POSITIONING ─── -->
<div class="section-header">
  <span class="sec-num">08</span>
  <span class="sec-title">COT Positioning — CFTC TFF (AUD Futures)</span>
  <div class="sec-line"></div>
</div>

{COT_CHART_HTML}

<div class="cot-big-grid">
  <!-- Asset Manager -->
  <div class="cot-card am-card">
    <div class="cot-card-label">Asset Manager Net {badge('CFTC')}</div>
    <div class="cot-net-value {'red' if data['am_net'] < 0 else 'green'}">{'+'if data['am_net']>=0 else ''}{data['am_net']:,}</div>
    <div class="cot-wow-row">
      <span class="{'green' if data['am_net_chg'] >= 0 else 'red'}">{'▲' if data['am_net_chg'] >= 0 else '▼'} {abs(data['am_net_chg']):,} WoW</span>
      <span class="cot-prev">prev {'+' if data['am_net_prev'] >= 0 else ''}{data['am_net_prev']:,}</span>
    </div>
  </div>
  <!-- Leveraged Funds -->
  <div class="cot-card lev-card">
    <div class="cot-card-label">Leveraged Funds Net {badge('CFTC')}</div>
    <div class="cot-net-value {'red' if data['lev_net'] < 0 else 'green'}">{'+'if data['lev_net']>=0 else ''}{data['lev_net']:,}</div>
    <div class="cot-wow-row">
      <span class="{'green' if data['lev_net_chg'] >= 0 else 'red'}">{'▲' if data['lev_net_chg'] >= 0 else '▼'} {abs(data['lev_net_chg']):,} WoW</span>
      <span class="cot-prev">prev {'+' if data['lev_net_prev'] >= 0 else ''}{data['lev_net_prev']:,}</span>
    </div>
  </div>
</div>

<div class="cot-compare-row">
  <div class="cot-compare-item">
    <span class="cot-compare-label">AM Net (current)</span>
    <span class="gold">{'+'if data['am_net']>=0 else ''}{data['am_net']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">AM Net (prior week)</span>
    <span class="dim-text">{'+'if data['am_net_prev']>=0 else ''}{data['am_net_prev']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">AM WoW Δ</span>
    <span class="{'green' if data['am_net_chg'] >= 0 else 'red'}">{'▲' if data['am_net_chg'] >= 0 else '▼'} {abs(data['am_net_chg']):,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">LF Net (current)</span>
    <span class="{'red' if data['lev_net'] < 0 else 'green'}">{'+'if data['lev_net']>=0 else ''}{data['lev_net']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">LF Net (prior week)</span>
    <span class="dim-text">{'+'if data['lev_net_prev']>=0 else ''}{data['lev_net_prev']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">LF WoW Δ</span>
    <span class="{'green' if data['lev_net_chg'] >= 0 else 'red'}">{'▲' if data['lev_net_chg'] >= 0 else '▼'} {abs(data['lev_net_chg']):,}</span>
  </div>
</div>

<div class="cot-note">
  {'<strong>Asset Manager trim note:</strong>' if data['am_net_chg'] < 0 else '<strong>Asset Manager build note:</strong>'}
  The AM net {'fell' if data['am_net_chg'] < 0 else 'rose'} from
  {("+" if data['am_net_prev'] >= 0 else "") + f"{data['am_net_prev']:,}"} to
  {("+" if data['am_net'] >= 0 else "") + f"{data['am_net']:,}"}
  ({("+" if data['am_net_chg'] >= 0 else "&minus;")}{abs(data['am_net_chg']):,} contracts WoW).
  The position remains {'net-long' if data['am_net'] > 0 else 'net-short'}, suggesting
  {'no outright reversal, only a partial unwind' if data['am_net_chg'] < 0 and data['am_net'] > 0 else 'continued directional conviction' if data['am_net_chg'] > 0 else 'an outright reversal in allocation bias'}.
  Leveraged Funds {'trimmed' if data['lev_net_chg'] < 0 else 'added'} {abs(data['lev_net_chg']):,} contracts WoW
  to {("+" if data['lev_net'] >= 0 else "") + f"{data['lev_net']:,}"} net.
</div>

<div class="section-surface">
<table class="cot-raw-table">
  <thead>
    <tr>
      <th>Category</th>
      <th>Long</th>
      <th>Short</th>
      <th>Net</th>
      <th>Reference week</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Asset Manager</td>
      <td class="green">{data['am_long']:,}</td>
      <td class="red">{data['am_short']:,}</td>
      <td class="gold">+{data['am_net']:,}</td>
      <td class="dim-text" style="font-size:0.68rem">{cot_date} {badge('CFTC')}</td>
    </tr>
    <tr>
      <td>Leveraged Funds</td>
      <td class="green">{data['lev_long']:,}</td>
      <td class="red">{data['lev_short']:,}</td>
      <td class="gold">+{data['lev_net']:,}</td>
      <td class="dim-text" style="font-size:0.68rem">{cot_date} {badge('CFTC')}</td>
    </tr>
  </tbody>
</table>
</div>


<!-- ─── 09 COMMODITIES & MARKETS ─── -->
<div class="section-header">
  <span class="sec-num">09</span>
  <span class="sec-title">Commodities &amp; Related Markets</span>
  <div class="sec-line"></div>
</div>

<div class="section-surface">
<table class="markets-table">
  <thead>
    <tr>
      <th>Instrument</th>
      <th>Ticker / Note</th>
      <th>Last</th>
      <th>Prev Close</th>
      <th>1D Chg %</th>
      <th>Source</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Gold</td>
      <td>GC=F (front month)</td>
      <td>{data['gold']:,.2f}</td>
      <td>{data['gold_prev']:,.2f}</td>
      <td class="{'chg-positive' if (gold_chg or 0) >= 0 else 'chg-negative'}">{'▲' if (gold_chg or 0) >= 0 else '▼'} {fmt_pct(gold_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>Copper</td>
      <td>HG=F (front month)</td>
      <td>{data['copper']:.3f}</td>
      <td>{data['copper_prev']:.3f}</td>
      <td class="{'chg-positive' if (copper_chg or 0) >= 0 else 'chg-negative'}">{'▲' if (copper_chg or 0) >= 0 else '▼'} {fmt_pct(copper_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>Iron Ore (62% Fe)</td>
      <td>TIO=F · CFR China TSI</td>
      <td>{f"{iron_ore_last:.2f}" if iron_ore_last else data_gap("no feed")}</td>
      <td>{f"{iron_ore_prev:.2f}" if iron_ore_prev else data_gap("n/a")}</td>
      <td>{"" if iron_ore_chg is None else (f'<span class="chg-positive">▲ {fmt_pct(iron_ore_chg)}</span>' if iron_ore_chg >= 0 else f'<span class="chg-negative">▼ {fmt_pct(iron_ore_chg)}</span>')}</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>USD / CNH</td>
      <td>Offshore RMB</td>
      <td>{data['usdcnh']:.4f}</td>
      <td class="data-gap">prev n/a</td>
      <td class="data-gap">n/a</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>CSI 300</td>
      <td>A-shares benchmark</td>
      <td>{data['csi300']:,.1f}</td>
      <td>{data['csi300_prev']:,.1f}</td>
      <td class="{'chg-positive' if (csi300_chg or 0) >= 0 else 'chg-negative'}">{'▲' if (csi300_chg or 0) >= 0 else '▼'} {fmt_pct(csi300_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>Hang Seng Index</td>
      <td>HSI — H-shares proxy</td>
      <td>{data['hsi']:,.1f}</td>
      <td>{data['hsi_prev']:,.1f}</td>
      <td class="{'chg-positive' if (hsi_chg or 0) >= 0 else 'chg-negative'}">{'▲' if (hsi_chg or 0) >= 0 else '▼'} {fmt_pct(hsi_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
  </tbody>
</table>
</div>


<!-- ─── 10 EVENT RISK CALENDAR ─── -->
<div class="section-header">
  <span class="sec-num">10</span>
  <span class="sec-title">Event Risk Calendar</span>
  <div class="sec-line"></div>
</div>

<div class="section-surface">
<table class="events-table">
  <thead>
    <tr>
      <th>Date</th>
      <th>Event</th>
      <th>Detail / Consensus</th>
      <th>Impact</th>
      <th>AUD Sensitivity</th>
    </tr>
  </thead>
  <tbody>
{EVENTS_FWD_ROWS}
  </tbody>
</table>
</div>


<!-- ─── 11 PREVIOUS CALENDAR EVENTS ─── -->
<div class="section-header">
  <span class="sec-num">11</span>
  <span class="sec-title">Previous Calendar Events — Last 30 Days</span>
  <div class="sec-line"></div>
</div>

<div class="section-surface">
{PREV_EVENTS_HTML}
</div>


<!-- ─── FOOTER ─── -->
<footer class="site-footer">
  <div class="footer-row">
    <div>
      Generated {today_long} &nbsp;&bull;&nbsp; AUD/USD API Brief &nbsp;&bull;&nbsp;
      Market data as of {data_date_str} &nbsp;&bull;&nbsp; AU yields: {_f2_date or data_date_str} &nbsp;&bull;&nbsp; COT: {cot_date}
    </div>
    <div>No screenshots &mdash; 100% API-sourced data</div>
  </div>
  <div class="footer-sources">
    <a href="https://finance.yahoo.com" target="_blank">[YF] Yahoo Finance — FX, equities, commodities, VIX</a>
    <a href="https://www.rba.gov.au/statistics/tables/xls/f02hist.xls" target="_blank">[RBA] Reserve Bank of Australia — AU bond yields (F2 table)</a>
    <a href="https://site.financialmodelingprep.com" target="_blank">[FMP] Financial Modeling Prep — US 2-year Treasury yield</a>
    <a href="https://www.cftc.gov/dea/futures/deacmesf.htm" target="_blank">[CFTC] CFTC TFF Report — AUD futures positioning ({cot_date})</a>
  </div>
</footer>

</main>

{chart_js}

{driver_series_script}
{DC_SPX_SCRIPT}
{DC_CNH_SCRIPT}
{DC_IRON_SCRIPT}
{DC_DXY_SCRIPT}
{DC_VIX_SCRIPT}


</body>
</html>
"""

# ---------------------------------------------------------------------------
# WRITE FILE
# ---------------------------------------------------------------------------
out_path = os.path.join(_HERE, 'docs', 'index.html')
os.makedirs(os.path.dirname(out_path), exist_ok=True)

with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)

size_bytes = os.path.getsize(out_path)
size_kb = size_bytes / 1024
line_count = html.count("\n")

print(f"Written: {out_path}")
print(f"Size:    {size_bytes:,} bytes ({size_kb:.1f} KB)")
print(f"Lines:   {line_count:,}")

# ---------------------------------------------------------------------------
# EMAIL SUMMARY  (runs only when GMAIL_USER + GMAIL_APP_PASSWORD are set)
# ---------------------------------------------------------------------------
_gmail_user = os.environ.get('GMAIL_USER', '')
_gmail_pass = os.environ.get('GMAIL_APP_PASSWORD', '')
_to_email   = os.environ.get('TO_EMAIL', _gmail_user)

if _gmail_user and _gmail_pass:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Build link to full brief from GitHub Actions env (no extra secret needed)
    _repo = os.environ.get('GITHUB_REPOSITORY', '')
    if _repo and '/' in _repo:
        _gh_user, _gh_repo = _repo.split('/', 1)
        _brief_url = f'https://{_gh_user}.github.io/{_gh_repo}/'
    else:
        _brief_url = '#'

    def _ec(val, invert=False):
        """Inline-styled change span for email (no CSS vars)."""
        if val is None:
            return '<span style="color:#7a92b4">—</span>'
        pos = (val >= 0) if not invert else (val < 0)
        col = '#2fcb9a' if pos else '#f05a52'
        arrow = '▲' if val >= 0 else '▼'
        sign  = '+' if val >= 0 else ''
        return f'<span style="color:{col}">{arrow} {sign}{val:.2f}%</span>'

    def _tile(label, value, chg_html, extra=''):
        return f"""
        <td style="padding:12px 14px;border-right:1px solid #1b2d4f;vertical-align:top">
          <div style="font-size:10px;color:#7a92b4;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">{label}</div>
          <div style="font-size:20px;font-weight:700;margin-bottom:4px">{value}</div>
          <div style="font-size:12px">{chg_html}</div>
          {f'<div style="font-size:11px;color:#7a92b4;margin-top:2px">{extra}</div>' if extra else ''}
        </td>"""

    _io_val  = f'{iron_ore_last:.2f}' if iron_ore_last else '—'
    _am_sign = '+' if data['am_net']  >= 0 else ''
    _lf_sign = '+' if data['lev_net'] >= 0 else ''
    _am_col  = '#f05a52' if data['am_net_chg'] < 0 else '#2fcb9a'
    _lf_col  = '#f05a52' if data['lev_net_chg'] < 0 else '#2fcb9a'
    _am_arr  = '▼' if data['am_net_chg'] < 0 else '▲'
    _lf_arr  = '▼' if data['lev_net_chg'] < 0 else '▲'

    _email_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#070d18;font-family:Arial,Helvetica,sans-serif;color:#e2eaf6">
<div style="max-width:620px;margin:0 auto;padding:24px 16px">

  <!-- Header -->
  <div style="border-bottom:2px solid #f0b329;padding-bottom:12px;margin-bottom:20px">
    <div style="font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#7a92b4;margin-bottom:4px">AUD/USD Daily Brief</div>
    <div style="font-size:22px;font-weight:700;color:#f0b329">{today_long}</div>
    <div style="font-size:11px;color:#7a92b4;margin-top:3px">Data as of {data_date_str}</div>
  </div>

  <!-- FX row -->
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#3d5270;margin-bottom:6px">FX &amp; Rates</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#101e36;border:1px solid #1b2d4f;margin-bottom:2px">
    <tr>
      {_tile('AUD / USD', f'{data["audusd"]:.4f}', _ec(audusd_chg))}
      {_tile('DXY Index', f'{data["dxy"]:.3f}', _ec(dxy_chg, invert=True))}
      {_tile('AU−US 2y Spread', f'{data["spread_2y"]} bp',
             f'<span style="color:#7a92b4">AU {data["au2y"]:.3f}% / US {data["us2y"]:.2f}%</span>')}
    </tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#0c1525;border:1px solid #1b2d4f;border-top:none;margin-bottom:16px">
    <tr>
      {_tile('AUD / CNY', f'{data["audcny"]:.3f}', _ec(audcny_chg))}
      {_tile('USD / CNH', f'{data["usdcnh"]:.4f}', '<span style="color:#7a92b4">offshore RMB</span>')}
      {_tile('VIX', f'{data["vix"]:.2f}', _ec(vix_chg, invert=True))}
    </tr>
  </table>

  <!-- Commodities -->
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#3d5270;margin-bottom:6px">Commodities &amp; Markets</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#101e36;border:1px solid #1b2d4f;margin-bottom:2px">
    <tr>
      {_tile('Gold (GC=F)', f'{data["gold"]:,.1f}', _ec(gold_chg))}
      {_tile('Iron Ore (TIO=F)', _io_val, _ec(iron_ore_chg))}
      {_tile('Copper (HG=F)', f'{data["copper"]:.3f}', _ec(copper_chg))}
    </tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#0c1525;border:1px solid #1b2d4f;border-top:none;margin-bottom:16px">
    <tr>
      {_tile('S&amp;P 500', f'{data["spx"]:,.0f}', _ec(spx_chg))}
      {_tile('Hang Seng', f'{data["hsi"]:,.0f}', _ec(hsi_chg))}
      {_tile('CSI 300', f'{data["csi300"]:,.0f}', _ec(csi300_chg))}
    </tr>
  </table>

  <!-- COT -->
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#3d5270;margin-bottom:6px">COT Positioning — CFTC TFF ({cot_date})</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#101e36;border:1px solid #1b2d4f;margin-bottom:20px">
    <tr>
      <td style="padding:14px 16px;border-right:1px solid #1b2d4f;vertical-align:top">
        <div style="font-size:10px;color:#7a92b4;margin-bottom:4px">Asset Manager Net</div>
        <div style="font-size:22px;font-weight:700">{_am_sign}{data['am_net']:,}</div>
        <div style="font-size:12px;color:{_am_col};margin-top:4px">{_am_arr} {abs(data['am_net_chg']):,} WoW</div>
      </td>
      <td style="padding:14px 16px;vertical-align:top">
        <div style="font-size:10px;color:#7a92b4;margin-bottom:4px">Leveraged Funds Net</div>
        <div style="font-size:22px;font-weight:700">{_lf_sign}{data['lev_net']:,}</div>
        <div style="font-size:12px;color:{_lf_col};margin-top:4px">{_lf_arr} {abs(data['lev_net_chg']):,} WoW</div>
      </td>
    </tr>
  </table>

  <!-- CTA -->
  <div style="text-align:center;padding:16px 0;border-top:1px solid #1b2d4f;margin-bottom:16px">
    <a href="{_brief_url}" style="display:inline-block;background:#f0b329;color:#070d18;font-weight:700;font-size:13px;padding:12px 32px;border-radius:6px;text-decoration:none;letter-spacing:.06em">VIEW FULL BRIEF &amp; CHARTS →</a>
    <div style="font-size:11px;color:#3d5270;margin-top:10px">Interactive rate differential and COT charts</div>
  </div>

  <div style="font-size:10px;color:#3d5270;border-top:1px solid #1b2d4f;padding-top:12px">
    Yahoo Finance · RBA · Alpha Vantage · CFTC · Auto-generated via GitHub Actions
  </div>
</div>
</body></html>"""

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'AUD/USD Brief — {today_long}'
        msg['From']    = _gmail_user
        msg['To']      = _to_email
        msg.attach(MIMEText(_email_html, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as _srv:
            _srv.login(_gmail_user, _gmail_pass)
            _srv.sendmail(_gmail_user, _to_email, msg.as_string())
        print(f'Email sent → {_to_email}')
    except Exception as e:
        print(f'Warning – email failed: {e}')
else:
    print('Email skipped (GMAIL_USER not set)')

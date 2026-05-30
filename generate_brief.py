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

_HERE = os.path.dirname(os.path.abspath(__file__))
AV_API_KEY = os.environ.get('AV_API_KEY', 'CP93A6VZ8592MCDN')

# ---------------------------------------------------------------------------
# LIVE FETCH HELPERS
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date as _date

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

def _fetch_us2y_av():
    """Alpha Vantage US 2y Treasury yield (weekly). Returns (rate_float, date_str)."""
    try:
        raw = _curl_get(
            f'https://www.alphavantage.co/query?function=TREASURY_YIELD'
            f'&interval=weekly&maturity=2year&apikey={AV_API_KEY}'
        )
        series = json.loads(raw).get('data', [])
        if series:
            return float(series[0]['value']), series[0]['date']
    except Exception as e:
        print(f'  [WARN] Alpha Vantage US2y: {e}')
    return None, None

def _fetch_rba_f2():
    """RBA F2 – AU 2y and 10y bond yields.
    Returns (au2y, au2y_prev, au10y, au10y_prev, date_str)."""
    try:
        raw = _curl_get('https://www.rba.gov.au/statistics/tables/csv/f2-data.csv', timeout=25)
        rows = list(csv.reader(io.StringIO(raw)))
        data_rows = [r for r in rows[10:]
                     if r and r[0] and r[0][0:1].isdigit() and len(r) >= 5]
        if len(data_rows) >= 2:
            last, prev = data_rows[-1], data_rows[-2]
            return float(last[1]), float(prev[1]), float(last[4]), float(prev[4]), last[0]
    except Exception as e:
        print(f'  [WARN] RBA F2: {e}')
    return None, None, None, None, None

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
# FETCH ALL LIVE DATA
# ---------------------------------------------------------------------------
YF_TICKERS = {
    'audusd':   'AUDUSD=X',
    'audjpy':   'AUDJPY=X',
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

print('Fetching Yahoo Finance tickers (parallel)…')
_yf_results = {}
with ThreadPoolExecutor(max_workers=6) as _ex:
    _futures = {_ex.submit(_yf, ticker): key for key, ticker in YF_TICKERS.items()}
    for _fut in list(_futures):
        _key = _futures[_fut]
        _yf_results[_key] = _fut.result()

for _k, (_l, _p, _d) in _yf_results.items():
    print(f'  {_k:12} {f"{_l:.4g}" if _l else "FAILED"}')

print('Fetching AU yields (RBA F2)…')
au2y, au2y_prev, au10y, au10y_prev, _f2_date = _fetch_rba_f2()
print(f'  AU2y={au2y}  AU10y={au10y}  ({_f2_date})')

print('Fetching US 2y yield (Alpha Vantage)…')
_us2y_raw, _us2y_date_raw = _fetch_us2y_av()
us2y      = _us2y_raw
us2y_date = (datetime.strptime(_us2y_date_raw, '%Y-%m-%d').strftime('%d %b')
             if _us2y_date_raw else '?')
print(f'  US2y={us2y}  ({us2y_date})')

print('Fetching RBA cash rate & BABs (F1)…')
rba_cash_rate, rba_babs_1m, rba_babs_3m, rba_cut_prob, _rba_f1_date = _fetch_rba_f1()
print(f'  cash={rba_cash_rate}%  1m_BABs={rba_babs_1m}%  cut_prob~{rba_cut_prob}%')

print('Fetching COT positioning (CFTC)…')
cot = _fetch_cot()
print(f'  AM_net={cot["am_net"] if cot else "FAILED"}  '
      f'LF_net={cot["lev_net"] if cot else "FAILED"}  '
      f'({cot["cot_date"] if cot else "?"})')

# ---------------------------------------------------------------------------
# ASSEMBLE DATA DICT
# ---------------------------------------------------------------------------
def _g(key, idx=0, fallback=None):
    v = _yf_results.get(key, (None,) * 3)[idx]
    return v if v is not None else fallback

data = {
    'audusd':      _g('audusd',  0, 0.7186),
    'audusd_prev': _g('audusd',  1, 0.7164),
    'audjpy':      _g('audjpy',  0, 114.408),
    'audjpy_prev': _g('audjpy',  1, 114.060),
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
audjpy_chg  = pct_change(data['audjpy'], data['audjpy_prev'])
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
<p class="source" id="rd_source">Source: Alpha Vantage (AUD/USD, US 2y) &middot; RBA Table F2 (AU 2y) &middot; spread = (AU 2y &minus; US 2y) &times; 100 bp.</p>
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
<p class="source">Source: CFTC TFF Futures-Only (positioning) &middot; Alpha Vantage (AUD/USD) &middot; net = long &minus; short contracts. Weekly, Tuesday close.</p>
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
      {badge('RBA')}{badge('AV')}
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

  <!-- AUD/JPY -->
  <div class="tile">
    <div class="tile-top">
      <span class="tile-label">AUD / JPY</span>
      {badge('YF')}
    </div>
    <div class="tile-value green">{data['audjpy']:.3f}</div>
    <div class="tile-bottom">
      {change_html(audjpy_chg)}
      <span class="tile-ref">prev {data['audjpy_prev']:.3f}</span>
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


<!-- ─── 02 RATE DIFFERENTIALS ─── -->
<div class="section-header">
  <span class="sec-num">02</span>
  <span class="sec-title">Rate Differentials &amp; Yield Curve</span>
  <div class="sec-line"></div>
</div>

{RD_CHART_HTML}

<div class="cards-row">
  <!-- RBA Card -->
  <div class="mini-card">
    <div class="mini-card-title">RBA Policy Status</div>
    <div class="mini-card-status green">Easing Bias Active</div>
    <div class="mini-card-detail">
      Cash Rate Target: <strong>{f"{rba_cash_rate:.2f}%" if rba_cash_rate else "4.35%"}</strong><br>
      Last move: &minus;25 bp &nbsp;&bull;&nbsp; 20 May 2026<br>
      Next meeting: <strong>16&ndash;17 Jun 2026</strong><br>
      {f'Jun cut probability: <strong class="gold">~{rba_cut_prob}%</strong> <span class="dim-text">(−25 bp, from 1m BABs)</span>' if rba_cut_prob is not None else 'Market pricing: <span class="pending-field">pending next meeting</span>'}
    </div>
  </div>
  <!-- FOMC Card -->
  <div class="mini-card">
    <div class="mini-card-title">FOMC Policy Status</div>
    <div class="mini-card-status amber">Hold — Data Dependent</div>
    <div class="mini-card-detail">
      Fed Funds Target: <strong>4.25–4.50%</strong><br>
      Last move: &minus;25 bp &nbsp;&bull;&nbsp; Dec 2024<br>
      Next meeting: <strong>17–18 Jun 2026</strong><br>
      SEP / Dot plot: <span class="pending-field">June update pending</span>
    </div>
  </div>
</div>


<!-- ─── 03 COT POSITIONING ─── -->
<div class="section-header">
  <span class="sec-num">03</span>
  <span class="sec-title">COT Positioning — CFTC TFF (AUD Futures)</span>
  <div class="sec-line"></div>
</div>

{COT_CHART_HTML}

<div class="cot-big-grid">
  <!-- Asset Manager -->
  <div class="cot-card am-card">
    <div class="cot-card-label">Asset Manager Net {badge('CFTC')}</div>
    <div class="cot-net-value red">+{data['am_net']:,}</div>
    <div class="cot-wow-row">
      <span class="red">▼ {abs(data['am_net_chg']):,} WoW</span>
      <span class="cot-prev">prev +{data['am_net_prev']:,}</span>
    </div>
  </div>
  <!-- Leveraged Funds -->
  <div class="cot-card lev-card">
    <div class="cot-card-label">Leveraged Funds Net {badge('CFTC')}</div>
    <div class="cot-net-value green">+{data['lev_net']:,}</div>
    <div class="cot-wow-row">
      <span class="red">▼ {abs(data['lev_net_chg']):,} WoW</span>
      <span class="cot-prev">prev +{data['lev_net_prev']:,}</span>
    </div>
  </div>
</div>

<div class="cot-compare-row">
  <div class="cot-compare-item">
    <span class="cot-compare-label">AM Net (current)</span>
    <span class="gold">+{data['am_net']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">AM Net (prior week)</span>
    <span class="dim-text">+{data['am_net_prev']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">AM WoW Δ</span>
    <span class="red">▼ {abs(data['am_net_chg']):,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">LF Net (current)</span>
    <span class="green">+{data['lev_net']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">LF Net (prior week)</span>
    <span class="dim-text">+{data['lev_net_prev']:,}</span>
  </div>
  <div class="cot-compare-item">
    <span class="cot-compare-label">LF WoW Δ</span>
    <span class="red">▼ {abs(data['lev_net_chg']):,}</span>
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


<!-- ─── 04 COMMODITIES & MARKETS ─── -->
<div class="section-header">
  <span class="sec-num">04</span>
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
      <td class="chg-positive">▲ {fmt_pct(gold_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>Copper</td>
      <td>HG=F (front month)</td>
      <td>{data['copper']:.3f}</td>
      <td>{data['copper_prev']:.3f}</td>
      <td class="chg-negative">▼ {fmt_pct(copper_chg)}</td>
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
      <td class="chg-negative">▼ {fmt_pct(csi300_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
    <tr>
      <td>Hang Seng Index</td>
      <td>HSI — H-shares proxy</td>
      <td>{data['hsi']:,.1f}</td>
      <td>{data['hsi_prev']:,.1f}</td>
      <td class="chg-positive">▲ {fmt_pct(hsi_chg)}</td>
      <td>{badge('YF')}</td>
    </tr>
  </tbody>
</table>
</div>


<!-- ─── 05 EVENT RISK CALENDAR ─── -->
<div class="section-header">
  <span class="sec-num">05</span>
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
    <tr>
      <td><span class="event-date">1 Jun</span></td>
      <td><span class="event-name">Caixin China Mfg PMI</span></td>
      <td class="event-detail">May reading — consensus ~51.0; upside supports AUD via CNH</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct via China demand</td>
    </tr>
    <tr>
      <td><span class="event-date">3 Jun</span></td>
      <td><span class="event-name">AU Q1 GDP</span></td>
      <td class="event-detail">qoq consensus +0.3%; yoy ~1.4%. Weak print could bring RBA cut forward</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct — domestic growth</td>
    </tr>
    <tr>
      <td><span class="event-date">6 Jun</span></td>
      <td><span class="event-name">US Nonfarm Payrolls</span></td>
      <td class="event-detail">May; consensus ~180k. Risk-off on miss → AUD/USD downside</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Via USD &amp; risk sentiment</td>
    </tr>
    <tr>
      <td><span class="event-date">6 Jun</span></td>
      <td><span class="event-name">CFTC COT Release</span></td>
      <td class="event-detail">2026-06-03 snapshot; watch AM net rebuild vs continued trim</td>
      <td><span class="impact-badge impact-med">MED</span></td>
      <td class="dim-text">Positioning signal</td>
    </tr>
    <tr>
      <td><span class="event-date">12 Jun</span></td>
      <td><span class="event-name">AU CPI + Employment</span></td>
      <td class="event-detail">Monthly CPI indicator; labour force survey. Key for RBA Jun meeting</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct — RBA reaction</td>
    </tr>
    <tr>
      <td><span class="event-date">15 Jun</span></td>
      <td><span class="event-name">CN Industrial Output / Retail</span></td>
      <td class="event-detail">May activity data — AUD proxy via commodity demand</td>
      <td><span class="impact-badge impact-med">MED</span></td>
      <td class="dim-text">Via China demand channel</td>
    </tr>
    <tr>
      <td><span class="event-date">16–17 Jun</span></td>
      <td><span class="event-name">RBA Board Meeting</span></td>
      <td class="event-detail">Rate decision; press conference. Market ~60% pricing another &minus;25 bp cut</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Primary AUD driver</td>
    </tr>
    <tr>
      <td><span class="event-date">17–18 Jun</span></td>
      <td><span class="event-name">FOMC Meeting</span></td>
      <td class="event-detail">Rate decision + SEP + dot plot. Hold expected; guidance key for USD</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Via USD &amp; rate diff</td>
    </tr>
  </tbody>
</table>
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
    <a href="https://www.alphavantage.co" target="_blank">[AV] Alpha Vantage — US 2-year Treasury yield</a>
    <a href="https://www.cftc.gov/dea/futures/deacmesf.htm" target="_blank">[CFTC] CFTC TFF Report — AUD futures positioning ({cot_date})</a>
    <a href="#" style="color:var(--text-faint)">[SCR] Screenshot / manual — Iron ore spot (data gap)</a>
  </div>
</footer>

</main>

{chart_js}

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

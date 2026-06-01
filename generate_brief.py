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
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
AV_API_KEY = os.environ.get('AV_API_KEY', '')  # set via GitHub Secret or local env

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

def _fetch_us2y_av():
    """Alpha Vantage US 2y Treasury yield (daily). Returns (rate_float, date_str, hist_dict)."""
    try:
        raw = _curl_get(
            f'https://www.alphavantage.co/query?function=TREASURY_YIELD'
            f'&interval=daily&maturity=2year&apikey={AV_API_KEY}'
        )
        series = json.loads(raw).get('data', [])
        if series:
            rate = float(series[0]['value'])
            date_str = series[0]['date']
            hist = {}
            for pt in series:
                try:
                    hist[pt['date']] = float(pt['value'])
                except (ValueError, KeyError):
                    pass
            return rate, date_str, hist
    except Exception as e:
        print(f'  [WARN] Alpha Vantage US2y: {e}')
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

def _compute_rolling_corr_series(aud_hist, driver_hists, use_diffs, window=20, sample_every=5):
    """Rolling Pearson for each driver vs AUD/USD over the full history.
    Also computes 252-day trailing mean/SD bands for AUD/USD.
    Returns list of records with date, audusd, aud_mean, aud_u1/l1/u2/l2,
    and per-driver rolling correlations. Sampled every sample_every trading days."""
    aud_rets = dict(_log_returns(aud_hist))
    drv_rets = {
        name: dict(_first_diffs(hist) if name in use_diffs else _log_returns(hist))
        for name, hist in driver_hists.items()
    }
    all_price_dates = sorted(aud_hist.keys())
    all_dates = sorted(aud_rets.keys())
    min_obs = round(window * 0.75)
    SD_WIN = 252
    records = []
    for i in range(window, len(all_dates)):
        if i % sample_every != 0 and i < len(all_dates) - 1:
            continue
        date = all_dates[i]
        # 252-day trailing SD bands for AUD/USD
        pi = bisect.bisect_right(all_price_dates, date)
        recent = [aud_hist[d] for d in all_price_dates[max(0, pi - SD_WIN):pi]]
        aud_price = round(aud_hist.get(date, 0), 4)
        if len(recent) >= 20:
            mu = sum(recent) / len(recent)
            sd = math.sqrt(sum((p - mu)**2 for p in recent) / (len(recent) - 1))
            rec = {
                'date': date, 'audusd': aud_price,
                'aud_mean': round(mu, 4),
                'aud_u1': round(mu + sd, 4),   'aud_l1': round(mu - sd, 4),
                'aud_u2': round(mu + 2*sd, 4), 'aud_l2': round(mu - 2*sd, 4),
            }
        else:
            rec = {'date': date, 'audusd': aud_price,
                   'aud_mean': None, 'aud_u1': None, 'aud_l1': None,
                   'aud_u2': None, 'aud_l2': None}
        # Pairwise rolling correlations
        win_dates = all_dates[i - window:i]
        for name, drets in drv_rets.items():
            pairs = [(aud_rets[d], drets[d]) for d in win_dates if d in drets]
            if len(pairs) < min_obs:
                rec[name] = None
            else:
                xs, ys = zip(*pairs)
                r = _pearson(list(xs), list(ys))
                rec[name] = round(r, 3) if r is not None else None
        records.append(rec)
    return records

# ---------------------------------------------------------------------------
# ROLLING OLS BETA PIPELINE
# ---------------------------------------------------------------------------
PRINT_DIAGNOSTICS = os.environ.get('PRINT_DIAGNOSTICS', '').lower() in ('1', 'true', 'yes')

def _to_returns_df(aud_hist, driver_hists, spread_lag=1):
    """Align all series, compute log returns / first diffs, return (dates, y, X).
    X columns (in order): intercept, dxy, spread, spx, usdcnh, iron.
    spread uses first differences; all others use log returns.
    spread_lag shifts spread forward by N days so today's regression uses the
    prior day's spread value (conservative: AU yields close ~4h before NYSE)."""
    spread_hist = driver_hists.get('spread', {})
    dxy_hist    = driver_hists.get('dxy', {})
    spx_hist    = driver_hists.get('spx', {})
    usdcnh_hist = driver_hists.get('usdcnh', {})
    iron_hist   = driver_hists.get('iron_ore', {})

    # Build return series as dicts
    aud_r   = dict(_log_returns(aud_hist))
    dxy_r   = dict(_log_returns(dxy_hist))
    spx_r   = dict(_log_returns(spx_hist))
    usdcnh_r = dict(_log_returns(usdcnh_hist))
    iron_r  = dict(_log_returns(iron_hist))
    spread_d = dict(_first_diffs(spread_hist))   # first diff in bp

    # Collect all return dates and apply spread lag
    all_dates = sorted(set(aud_r) & set(dxy_r) & set(spx_r) & set(usdcnh_r))
    # iron is allowed to be missing (starts 2013) — use NaN when absent
    spread_dates = sorted(spread_d)
    # lagged spread: for each date, use the spread diff from `spread_lag` earlier trading days
    spread_lagged = {}
    for i, d in enumerate(spread_dates):
        if spread_lag == 0:
            spread_lagged[d] = spread_d[d]
        elif i >= spread_lag:
            spread_lagged[d] = spread_d[spread_dates[i - spread_lag]]

    dates_out, y_out, X_rows = [], [], []
    for d in all_dates:
        y = aud_r.get(d)
        xd = dxy_r.get(d)
        xs = spread_lagged.get(d)   # may be None if spread missing
        xp = spx_r.get(d)
        xc = usdcnh_r.get(d)
        xi = iron_r.get(d)          # may be None pre-2013
        if y is None or xd is None or xp is None or xc is None:
            continue  # core series must be present
        dates_out.append(d)
        y_out.append(y)
        X_rows.append([1.0,
                        xd,
                        xs if xs is not None else float('nan'),
                        xp,
                        xc,
                        xi if xi is not None else float('nan')])

    return (
        dates_out,
        np.array(y_out, dtype=float),
        np.array(X_rows, dtype=float),
    )

def _rolling_ols_betas(y_arr, X_arr, window=252, min_obs_frac=0.75):
    """Rolling OLS via np.linalg.lstsq. Returns (betas, r2) both shape (n, k) / (n,).
    Rows where window has fewer than min_obs finite pairs are NaN."""
    n, k = X_arr.shape
    min_obs = round(window * min_obs_frac)
    betas = np.full((n, k), np.nan)
    r2    = np.full(n, np.nan)
    for i in range(window - 1, n):
        yi = y_arr[i - window + 1:i + 1]
        Xi = X_arr[i - window + 1:i + 1]
        mask = np.isfinite(yi) & np.all(np.isfinite(Xi), axis=1)
        if mask.sum() < min_obs:
            continue
        b, _, _, _ = np.linalg.lstsq(Xi[mask], yi[mask], rcond=None)
        betas[i] = b
        yhat  = Xi[mask] @ b
        ss_res = np.sum((yi[mask] - yhat) ** 2)
        ss_tot = np.sum((yi[mask] - yi[mask].mean()) ** 2)
        r2[i]  = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return betas, r2

def _add_beta_bands(dates, betas, r2_arr, col_names, sample_every=5, band_window=252):
    """Compute trailing mean/std bands on each beta series.
    Returns list of dicts (weekly sampled) with beta values + band columns."""
    n = len(dates)
    records = []
    band_half = band_window // 2  # minimum obs to compute std (avoid very early noise)
    for i in range(n):
        if i % sample_every != 0 and i < n - 1:
            continue
        rec = {'date': dates[i], 'r2': round(float(r2_arr[i]), 4) if np.isfinite(r2_arr[i]) else None}
        lo = max(0, i - band_window + 1)
        for j, name in enumerate(col_names):
            b = betas[i, j]
            window_vals = betas[lo:i + 1, j]
            valid = window_vals[np.isfinite(window_vals)]
            rec[name] = round(float(b), 5) if np.isfinite(b) else None
            if len(valid) >= band_half:
                mu  = float(np.mean(valid))
                sig = float(np.std(valid, ddof=1))
                rec[f'{name}_mean'] = round(mu, 5)
                rec[f'{name}_u1']   = round(mu + sig, 5)
                rec[f'{name}_l1']   = round(mu - sig, 5)
                rec[f'{name}_u2']   = round(mu + 2*sig, 5)
                rec[f'{name}_l2']   = round(mu - 2*sig, 5)
            else:
                for sfx in ('_mean','_u1','_l1','_u2','_l2'):
                    rec[name + sfx] = None
        records.append(rec)
    return records

def _static_ols_diagnostics(y_arr, X_arr, col_names):
    """Full-sample OLS diagnostics: betas, approximate t-stats, R², VIF.
    col_names should match columns of X_arr (including 'intercept')."""
    mask = np.isfinite(y_arr) & np.all(np.isfinite(X_arr), axis=1)
    y = y_arr[mask]; X = X_arr[mask]
    b, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    yhat  = X @ b
    resid = y - yhat
    n, k  = X.shape
    s2    = np.sum(resid**2) / max(n - k, 1)
    try:
        cov = s2 * np.linalg.inv(X.T @ X)
        se  = np.sqrt(np.diag(cov))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
    ss_tot = np.sum((y - y.mean())**2)
    ss_res = np.sum(resid**2)
    r2_full = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan

    EXPECTED_SIGN = {'dxy': -1, 'spread': 1, 'spx': 1, 'usdcnh': -1, 'iron': 1}
    print('\n── Static OLS (full sample) ──────────────────────────')
    print(f'  n={mask.sum()} obs  R²={r2_full:.4f}')
    for j, name in enumerate(col_names):
        t = b[j] / se[j] if se[j] > 0 else float('nan')
        stars = '***' if abs(t) > 2.576 else ('**' if abs(t) > 1.960 else ('*' if abs(t) > 1.645 else ''))
        exp = EXPECTED_SIGN.get(name)
        sign_ok = '' if exp is None else (' ✓' if (b[j] * exp > 0) else ' ✗ SIGN MISMATCH!')
        # VIF for non-intercept columns
        vif_str = ''
        if name != 'intercept':
            other_cols = [c for c in range(k) if c != j]
            Xj = X[:, j]; Xo = X[:, other_cols]
            bv, _, _, _ = np.linalg.lstsq(Xo, Xj, rcond=None)
            xjhat = Xo @ bv
            ss_tot_j = np.sum((Xj - Xj.mean())**2)
            ss_res_j = np.sum((Xj - xjhat)**2)
            r2j = 1 - ss_res_j / ss_tot_j if ss_tot_j > 0 else 0
            vif = 1 / (1 - r2j) if r2j < 1 else float('inf')
            vif_str = f'  VIF={vif:.1f}{"  ⚠ collinear" if vif > 5 else ""}'
        print(f'  {name:12}  β={b[j]:+.4f}  t={t:+.1f} {stars:3}{sign_ok}{vif_str}')
    print('──────────────────────────────────────────────────────\n')

# ---------------------------------------------------------------------------
# DRIVER ATTRIBUTION RENDERER
# ---------------------------------------------------------------------------
_DRIVER_LABELS = {
    'spread':   'AU–US 2y spread',
    'iron_ore': 'Iron ore (TIO=F)',
    'spx':      'S&P 500',
    'usdcnh':   'USD/CNY (CNH proxy)',
    'dxy':      'DXY',
}
_DRIVER_EXPECTED_SIGN = {'spread': 1, 'iron_ore': 1, 'spx': 1, 'usdcnh': -1, 'dxy': -1}
_DRIVER_HEADLINE = {
    'spread':   'RATES-DRIVEN',
    'iron_ore': 'CHINA / COMMODITIES-DRIVEN',
    'spx':      'RISK-DRIVEN',
    'usdcnh':   'CNH-DRIVEN',
    'dxy':      'USD-DRIVEN',
}

def _render_attribution_panel(corr):
    """Return HTML string for the driver attribution panel."""
    def r_str(r):
        return f'{r:+.2f}' if r is not None else 'n/c'

    # Sort by |r_20| descending
    ranked = sorted(corr, key=lambda n: abs(corr[n][20][0] or 0), reverse=True)

    # Headline
    top = ranked[0] if ranked else None
    top_r = corr[top][20][0] if top else None
    if top_r is not None and abs(top_r) >= 0.25:
        headline = f'{_DRIVER_HEADLINE[top]} — {_DRIVER_LABELS[top]} leads at r&nbsp;=&nbsp;{r_str(top_r)}&nbsp;(20d)'
    else:
        headline = 'DRIVER UNCLEAR — low correlation across all channels'

    # S&P regime flag
    spx_r20 = corr.get('spx', {}).get(20, (None, 0))[0]
    spx_r60 = corr.get('spx', {}).get(60, (None, 0))[0]
    if spx_r20 is not None:
        if spx_r20 > 0.45:
            flag_label, flag_txt = 'RISK COUPLING', f'S&amp;P r&nbsp;=&nbsp;{r_str(spx_r20)}&nbsp;(20d) — equity sentiment co-moving with AUD'
        elif spx_r20 < 0.25 and spx_r60 is not None and spx_r60 > 0.40:
            flag_label, flag_txt = 'RISK DECOUPLING', f'S&amp;P r&nbsp;=&nbsp;{r_str(spx_r20)}&nbsp;(20d) vs {r_str(spx_r60)}&nbsp;(60d) — fading; idiosyncratic driver emerging'
        elif spx_r20 < 0.25:
            flag_label, flag_txt = 'RISK DECOUPLED', f'S&amp;P r&nbsp;=&nbsp;{r_str(spx_r20)}&nbsp;(20d) — rates, China, or USD driving instead'
        else:
            flag_label, flag_txt = 'MODERATE RISK COUPLING', f'S&amp;P r&nbsp;=&nbsp;{r_str(spx_r20)}&nbsp;(20d)'
    else:
        flag_label, flag_txt = 'RISK — INSUFFICIENT DATA', 'Fewer than 15 overlapping daily observations in 20d window'

    # Bar rows
    rows = ''
    for name in ranked:
        r20, n20 = corr[name][20]
        r60, n60 = corr[name][60]
        label = _DRIVER_LABELS[name]
        exp = _DRIVER_EXPECTED_SIGN[name]
        low = r20 is None
        if low:
            fill_cls, w20 = 'attr-fill positive', '0%'
            val20 = 'n/c*'
        else:
            anomaly = abs(r20) > 0.3 and ((1 if r20 > 0 else -1) != exp)
            fill_cls = 'attr-fill anomaly' if anomaly else ('attr-fill positive' if r20 >= 0 else 'attr-fill negative')
            w20  = f'{abs(r20)*100:.1f}%'
            val20 = r_str(r20)
        m60_left = f'{abs(r60)*100:.1f}%' if r60 is not None else '0%'
        val60    = r_str(r60)
        caveat   = ' <span class="attr-caveat">⚠&nbsp;DXY≈58%&nbsp;EUR</span>' if name == 'dxy' else ''
        row_cls  = 'attr-row attr-low-conf' if low else 'attr-row'
        rows += f"""
    <div class="{row_cls}">
      <span class="attr-label">{label}</span>
      <div class="attr-track">
        <div class="{fill_cls}" style="width:{w20}"></div>
        <span class="attr-marker-60d" style="left:{m60_left}" title="60d: {val60}"></span>
      </div>
      <span class="attr-val">{val20}</span>
      <span class="attr-val-60d">{val60}&nbsp;(60d)</span>{caveat}
    </div>"""

    return f"""<div class="attr-headline">{headline}</div>
  <div class="attr-section">{rows}
  </div>
  <div class="attr-flag"><strong>&#x26A1; {flag_label}</strong> &nbsp;&mdash;&nbsp; {flag_txt}</div>
  <p class="attr-note">Daily log&nbsp;returns / spread&nbsp;&Delta;bp &middot; 20-trading-day window &middot; &#9670;&nbsp;=&nbsp;60d marker &middot; *&nbsp;=&nbsp;low&nbsp;confidence (&lt;{round(20*0.75)}&nbsp;obs).<br>
  Caveats: AU yields close ~4h before NYC (half-day skew on spread) &middot; DXY is 58%&nbsp;EUR-weighted, not a pure AUD/USD&nbsp;inverse &middot; TIO=F liquidity thins near contract&nbsp;roll.</p>"""

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

# 4-month daily history for correlation panel (5 drivers + AUD/USD)
# Note: CNH=X (offshore) has no YF history; use USDCNY=X (onshore) as proxy
HIST_TICKERS = {
    'audusd':   'AUDUSD=X',
    'dxy':      'DX-Y.NYB',
    'usdcnh':   'USDCNY=X',
    'spx':      '^GSPC',
    'iron_ore': 'TIO=F',
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

print('Fetching US 2y yield (Alpha Vantage)…')
_us2y_raw, _us2y_date_raw, _us2y_hist = _fetch_us2y_av()
us2y      = _us2y_raw
us2y_date = (datetime.strptime(_us2y_date_raw, '%Y-%m-%d').strftime('%d %b')
             if _us2y_date_raw else '?')
print(f'  US2y={us2y}  ({us2y_date})  hist={len(_us2y_hist)}d')

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
# Spread (bp) = (AU 2y % - US 2y %) * 100, daily, pairwise-complete dates
_spread_hist = {
    d: (_au2y_hist[d] - _us2y_hist[d]) * 100
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

attribution_html = _render_attribution_panel(corr_results)

print('Computing rolling correlation series (10y backdate)…')
_corr_series_20 = _compute_rolling_corr_series(
    _hist_results.get('audusd', {}), _driver_hists, use_diffs={'spread'}, window=20)
_corr_series_60 = _compute_rolling_corr_series(
    _hist_results.get('audusd', {}), _driver_hists, use_diffs={'spread'}, window=60)
print(f'  20d series: {len(_corr_series_20)} records  |  60d series: {len(_corr_series_60)} records')
_c20j = json.dumps(_corr_series_20, separators=(',', ':'))
_c60j = json.dumps(_corr_series_60, separators=(',', ':'))
corr_data_script = f'<script>\nvar CORR20_SERIES={_c20j};\nvar CORR60_SERIES={_c60j};\n</script>'

# ---------------------------------------------------------------------------
# ROLLING OLS BETAS
# ---------------------------------------------------------------------------
_BETA_DRIVERS = ['dxy', 'spread', 'spx', 'usdcnh', 'iron']
_BETA_COL_NAMES = ['intercept'] + _BETA_DRIVERS

print('Computing rolling OLS betas (252d window)…')
_beta_dates, _beta_y, _beta_X = _to_returns_df(
    _hist_results.get('audusd', {}), _driver_hists, spread_lag=1)
print(f'  {len(_beta_dates)} aligned return-days after differencing')

# Drop columns that are entirely NaN (e.g. spread when AV key absent locally)
_col_finite = [np.any(np.isfinite(_beta_X[:, j])) for j in range(_beta_X.shape[1])]
_active_col_names = [n for n, ok in zip(_BETA_COL_NAMES, _col_finite) if ok]
_active_driver_names = [n for n in _BETA_DRIVERS if n in _active_col_names]
_beta_X_active = _beta_X[:, _col_finite]
_dropped = [n for n, ok in zip(_BETA_COL_NAMES, _col_finite) if not ok]
if _dropped:
    print(f'  Dropped all-NaN predictors: {_dropped} (missing data source)')

if PRINT_DIAGNOSTICS:
    _static_ols_diagnostics(_beta_y, _beta_X_active, _active_col_names)

_betas_252_raw, _r2_252 = _rolling_ols_betas(_beta_y, _beta_X_active, window=252)
_betas_60_raw,  _r2_60  = _rolling_ols_betas(_beta_y, _beta_X_active, window=60)
print(f'  252d window: {int(np.isfinite(_r2_252).sum())} valid rows  |  60d: {int(np.isfinite(_r2_60).sum())} valid rows')

# Reconstruct full-width beta arrays (NaN for dropped columns) so downstream code is consistent
def _expand_betas(betas_raw, active_names, all_names):
    """Re-insert NaN columns for drivers that were dropped."""
    n = betas_raw.shape[0]
    out = np.full((n, len(all_names)), np.nan)
    active_idx = {name: i for i, name in enumerate(active_names)}
    for j, name in enumerate(all_names):
        if name in active_idx:
            out[:, j] = betas_raw[:, active_idx[name]]
    return out

_betas_252 = _expand_betas(_betas_252_raw, _active_col_names, _BETA_COL_NAMES)
_betas_60  = _expand_betas(_betas_60_raw,  _active_col_names, _BETA_COL_NAMES)

# Slice to driver columns only (exclude intercept at index 0)
_driver_col_idx = {n: i+1 for i, n in enumerate(_BETA_DRIVERS)}  # +1 for intercept offset
_betas_drv_252 = _betas_252[:, 1:]  # columns 1-5 = drivers
_betas_drv_60  = _betas_60[:, 1:]

_beta_series_252 = _add_beta_bands(_beta_dates, _betas_drv_252, _r2_252, _BETA_DRIVERS, sample_every=5)
_beta_series_60  = _add_beta_bands(_beta_dates, _betas_drv_60,  _r2_60,  _BETA_DRIVERS, sample_every=5)
print(f'  Beta series: {len(_beta_series_252)} weekly records (252d)  |  {len(_beta_series_60)} (60d)')

# Latest betas (most recent row with at least one non-NaN driver) — used by sensitivity panel
def _latest_valid(series_list, names):
    for rec in reversed(series_list):
        if any(rec.get(n) is not None for n in names):
            return rec
    return None

_latest_252 = _latest_valid(_beta_series_252, _BETA_DRIVERS)
_latest_60  = _latest_valid(_beta_series_60,  _BETA_DRIVERS)

def _build_latest_betas(rec, names):
    if rec is None:
        return {}
    out = {'date': rec['date'], 'r2': rec.get('r2')}
    for n in names:
        out[n] = {
            'beta': rec.get(n),
            'mean': rec.get(n + '_mean'),
            'u1':   rec.get(n + '_u1'),
            'l1':   rec.get(n + '_l1'),
            'u2':   rec.get(n + '_u2'),
            'l2':   rec.get(n + '_l2'),
        }
    return out

_latest_betas_252 = _build_latest_betas(_latest_252, _BETA_DRIVERS)
_latest_betas_60  = _build_latest_betas(_latest_60,  _BETA_DRIVERS)

_lb252j = json.dumps(_latest_betas_252, separators=(',', ':'))
_lb60j  = json.dumps(_latest_betas_60,  separators=(',', ':'))
beta_data_script = (
    f'<script>\n'
    f'var LATEST_BETAS_252={_lb252j};\n'
    f'var LATEST_BETAS_60={_lb60j};\n'
    f'</script>'
)

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

/* ── DRIVER ATTRIBUTION PANEL ── */
.attr-headline{font-family:'Fraunces',serif;font-size:1.05rem;font-weight:600;color:var(--gold);margin-bottom:20px;letter-spacing:-.01em}
.attr-section{margin-bottom:12px}
.attr-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;font-size:0.78rem}
.attr-low-conf{opacity:.42}
.attr-label{width:155px;color:var(--text-dim);flex-shrink:0;font-size:0.75rem}
.attr-track{flex:1;height:8px;background:var(--panel-edge);border-radius:4px;position:relative;min-width:80px}
.attr-fill{height:100%;border-radius:4px;position:absolute;top:0;left:0}
.attr-fill.positive{background:var(--green)}
.attr-fill.negative{background:var(--red)}
.attr-fill.anomaly{background:var(--amber)}
.attr-marker-60d{position:absolute;top:-4px;width:2px;height:16px;background:var(--gold);border-radius:1px;opacity:.6}
.attr-val{width:42px;text-align:right;font-family:'IBM Plex Mono',monospace;font-size:0.78rem;color:var(--text)}
.attr-val-60d{width:76px;color:var(--text-faint);font-size:0.68rem}
.attr-caveat{font-size:0.65rem;color:var(--amber);margin-left:4px;opacity:.85}
.attr-flag{background:rgba(240,179,41,.07);border:1px solid rgba(240,179,41,.18);border-radius:8px;padding:10px 16px;font-size:0.75rem;color:var(--text-dim);margin-top:16px;line-height:1.55}
.attr-flag strong{color:var(--gold)}
.attr-note{margin-top:14px;font-size:0.66rem;color:var(--text-faint);line-height:1.65}
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

# Section: Correlation history chart (10-year rolling Pearson vs AUD/USD)
CORR_CHART_HTML = """
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px">
  <div class="legend" style="flex-wrap:wrap">
    <span><span class="dot" style="background:#e2eaf6;width:11px;height:11px"></span>AUD/USD (RHS)</span>
    <span style="color:rgba(240,179,41,0.7);font-size:0.7rem">&plusmn;1&sigma;&thinsp;/&thinsp;2&sigma;</span>
    <span style="width:1px;height:12px;background:var(--panel-edge);margin:0 2px;display:inline-block"></span>
    <span id="corr_leg_spread"   style="cursor:pointer" title="Click to hide"><span class="dot" style="background:#4b8ef0"></span>Spread</span>
    <span id="corr_leg_iron_ore" style="cursor:pointer" title="Click to hide"><span class="dot" style="background:#e09438"></span>Iron ore</span>
    <span id="corr_leg_spx"      style="cursor:pointer" title="Click to hide"><span class="dot" style="background:#2fcb9a"></span>S&amp;P</span>
    <span id="corr_leg_usdcnh"   style="cursor:pointer" title="Click to hide"><span class="dot" style="background:#c084fc"></span>USD/CNY</span>
    <span id="corr_leg_dxy"      style="cursor:pointer" title="Click to hide"><span class="dot" style="background:#f05a52"></span>DXY</span>
  </div>
  <div id="corr_toggleBtns" class="presets" style="margin-top:0">
    <button class="preset active" data-w="20">20d</button>
    <button class="preset" data-w="60">60d</button>
  </div>
</div>

<div class="panel-box">
  <div class="panel-title">AUD/USD price &amp; rolling factor correlations</div>
  <div class="chart-holder" style="height:380px"><canvas id="corr_chart"></canvas></div>

  <div class="slider-wrap">
    <div class="slider-head">
      <div class="k">Date range</div>
      <div class="range-readout"><b id="corr_rangeStart">&mdash;</b> &nbsp;&rarr;&nbsp; <b id="corr_rangeEnd">&mdash;</b></div>
    </div>
    <div class="dual">
      <div class="track"></div>
      <div class="track-fill" id="corr_trackFill"></div>
      <input type="range" id="corr_minR" min="0" max="100" value="0">
      <input type="range" id="corr_maxR" min="0" max="100" value="100">
    </div>
    <div class="presets" id="corr_presets">
      <button class="preset" data-y="1">1Y</button>
      <button class="preset" data-y="2">2Y</button>
      <button class="preset" data-y="5">5Y</button>
      <button class="preset active" data-y="0">Max</button>
    </div>
  </div>
</div>

<div style="margin-top:16px;font-size:0.6rem;letter-spacing:.12em;text-transform:uppercase;color:var(--text-faint);margin-bottom:8px">Avg correlation &mdash; selected period</div>
<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px">
  <div style="background:var(--panel);border:1px solid var(--panel-edge);border-radius:8px;padding:12px 14px;border-top:2px solid #4b8ef0">
    <div style="font-size:0.58rem;color:#4b8ef0;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">AU&ndash;US spread</div>
    <div id="corr_val_spread" style="font-family:'Fraunces',serif;font-size:1.55rem;font-weight:600;letter-spacing:-.02em;line-height:1;color:var(--text)">—</div>
    <div id="corr_tag_spread" style="font-size:0.6rem;color:var(--text-faint);margin-top:5px">&nbsp;</div>
  </div>
  <div style="background:var(--panel);border:1px solid var(--panel-edge);border-radius:8px;padding:12px 14px;border-top:2px solid #e09438">
    <div style="font-size:0.58rem;color:#e09438;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">Iron ore</div>
    <div id="corr_val_iron_ore" style="font-family:'Fraunces',serif;font-size:1.55rem;font-weight:600;letter-spacing:-.02em;line-height:1;color:var(--text)">—</div>
    <div id="corr_tag_iron_ore" style="font-size:0.6rem;color:var(--text-faint);margin-top:5px">&nbsp;</div>
  </div>
  <div style="background:var(--panel);border:1px solid var(--panel-edge);border-radius:8px;padding:12px 14px;border-top:2px solid #2fcb9a">
    <div style="font-size:0.58rem;color:#2fcb9a;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">S&amp;P 500</div>
    <div id="corr_val_spx" style="font-family:'Fraunces',serif;font-size:1.55rem;font-weight:600;letter-spacing:-.02em;line-height:1;color:var(--text)">—</div>
    <div id="corr_tag_spx" style="font-size:0.6rem;color:var(--text-faint);margin-top:5px">&nbsp;</div>
  </div>
  <div style="background:var(--panel);border:1px solid var(--panel-edge);border-radius:8px;padding:12px 14px;border-top:2px solid #c084fc">
    <div style="font-size:0.58rem;color:#c084fc;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">USD/CNY</div>
    <div id="corr_val_usdcnh" style="font-family:'Fraunces',serif;font-size:1.55rem;font-weight:600;letter-spacing:-.02em;line-height:1;color:var(--text)">—</div>
    <div id="corr_tag_usdcnh" style="font-size:0.6rem;color:var(--text-faint);margin-top:5px">&nbsp;</div>
  </div>
  <div style="background:var(--panel);border:1px solid var(--panel-edge);border-radius:8px;padding:12px 14px;border-top:2px solid #f05a52">
    <div style="font-size:0.58rem;color:#f05a52;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">DXY</div>
    <div id="corr_val_dxy" style="font-family:'Fraunces',serif;font-size:1.55rem;font-weight:600;letter-spacing:-.02em;line-height:1;color:var(--text)">—</div>
    <div id="corr_tag_dxy" style="font-size:0.6rem;color:var(--text-faint);margin-top:5px">&nbsp;</div>
  </div>
</div>
<p class="source" style="margin-top:12px">AUD/USD: 252-day trailing mean &plusmn;1&sigma;&thinsp;/&thinsp;2&sigma; bands (RHS) &middot; Correlations: rolling Pearson vs AUD/USD log-returns or spread &Delta;bp (LHS) &middot; weekly sampled &middot; Yahoo Finance, RBA F2, Alpha Vantage.</p>
"""

# Chart init script for the correlation history chart (regular string — no f-string escaping needed)
CORR_INIT_SCRIPT = """<script>
(function() {
  var activeSeries = CORR20_SERIES;
  var DRIVER_COLORS = {spread:'#4b8ef0',iron_ore:'#e09438',spx:'#2fcb9a',usdcnh:'#c084fc',dxy:'#f05a52'};
  var DRIVER_LABELS_MAP = {spread:'AU–US spread',iron_ore:'Iron ore',spx:'S&P 500',usdcnh:'USD/CNY',dxy:'DXY'};
  var DRIVER_SIGN   = {spread:1,iron_ore:1,spx:1,usdcnh:-1,dxy:-1};
  var DRIVERS = ['spread','iron_ore','spx','usdcnh','dxy'];
  var DRIVER_IDX    = {spread:6,iron_ore:7,spx:8,usdcnh:9,dxy:10};
  var hiddenDrivers = new Set();

  // Band datasets at fixed indices 0-5, AUD/USD at 5, correlations at 6-10
  function buildDatasets(series) {
    var labels = series.map(function(r){return r.date;});
    var get = function(key){return series.map(function(r){return r[key]!==undefined?r[key]:null;});};
    var bOpts = {borderWidth:0,pointRadius:0,spanGaps:true,yAxisID:'y_aud',tension:0.15};
    return {
      labels: labels,
      datasets: [
        // 0: outer lower (-2σ)
        Object.assign({},bOpts,{label:'',data:get('aud_l2'),fill:false,borderColor:'transparent',backgroundColor:'transparent'}),
        // 1: outer upper (+2σ) — fill to index 0
        Object.assign({},bOpts,{label:'\xb12σ',data:get('aud_u2'),fill:0,backgroundColor:'rgba(240,179,41,0.06)',borderColor:'transparent'}),
        // 2: inner lower (-1σ)
        Object.assign({},bOpts,{label:'',data:get('aud_l1'),fill:false,borderColor:'transparent',backgroundColor:'transparent'}),
        // 3: inner upper (+1σ) — fill to index 2
        Object.assign({},bOpts,{label:'\xb11σ',data:get('aud_u1'),fill:2,backgroundColor:'rgba(240,179,41,0.11)',borderColor:'transparent'}),
        // 4: rolling mean (subtle dashed)
        Object.assign({},bOpts,{label:'Mean (252d)',data:get('aud_mean'),fill:false,borderColor:'rgba(240,179,41,0.38)',borderWidth:1,borderDash:[4,4]}),
        // 5: AUD/USD main (thick, prominent)
        {label:'AUD/USD',data:get('audusd'),yAxisID:'y_aud',fill:false,borderColor:'#e2eaf6',borderWidth:2.5,pointRadius:0,spanGaps:true,tension:0.15},
        // 6-10: correlations
        {label:DRIVER_LABELS_MAP.spread,  data:get('spread'),  yAxisID:'y_corr',fill:false,borderColor:'#4b8ef0',borderWidth:1.5,pointRadius:0,spanGaps:true,tension:0.2},
        {label:DRIVER_LABELS_MAP.iron_ore,data:get('iron_ore'),yAxisID:'y_corr',fill:false,borderColor:'#e09438',borderWidth:1.5,pointRadius:0,spanGaps:true,tension:0.2},
        {label:DRIVER_LABELS_MAP.spx,     data:get('spx'),     yAxisID:'y_corr',fill:false,borderColor:'#2fcb9a',borderWidth:1.5,pointRadius:0,spanGaps:true,tension:0.2},
        {label:DRIVER_LABELS_MAP.usdcnh,  data:get('usdcnh'),  yAxisID:'y_corr',fill:false,borderColor:'#c084fc',borderWidth:1.5,pointRadius:0,spanGaps:true,tension:0.2},
        {label:DRIVER_LABELS_MAP.dxy,     data:get('dxy'),     yAxisID:'y_corr',fill:false,borderColor:'#f05a52',borderWidth:1.5,pointRadius:0,spanGaps:true,tension:0.2},
      ]
    };
  }

  var zeroLinePlug = {
    id:'zeroLine',
    afterDraw:function(chart){
      var sc=chart.scales.y_corr; if(!sc) return;
      var y=sc.getPixelForValue(0),ctx=chart.ctx;
      ctx.save(); ctx.beginPath();
      ctx.moveTo(chart.chartArea.left,y); ctx.lineTo(chart.chartArea.right,y);
      ctx.strokeStyle='rgba(142,162,189,0.28)'; ctx.setLineDash([4,4]); ctx.lineWidth=1;
      ctx.stroke(); ctx.restore();
    }
  };

  var corrMin=document.getElementById('corr_minR');
  var corrMax=document.getElementById('corr_maxR');
  var N=activeSeries.length-1;
  corrMin.max=corrMax.max=N; corrMax.value=N;

  var initData=buildDatasets(activeSeries);
  var corrChart=new Chart(document.getElementById('corr_chart'),{
    type:'line', plugins:[zeroLinePlug],
    data:{labels:initData.labels,datasets:initData.datasets},
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:300},
      interaction:{mode:'index',intersect:false},
      scales:{
        x:{grid:{color:'rgba(142,162,189,0.10)'},ticks:{color:'#7a92b4',maxTicksLimit:10,
          callback:function(val){var d=this.getLabelForValue(val);return d?d.slice(0,4):'';}}},
        y_corr:{type:'linear',position:'left',min:-1,max:1,
          grid:{color:'rgba(142,162,189,0.10)'},
          ticks:{color:'#7a92b4',callback:function(v){return v===0?'0':(v>0?'+':'')+v.toFixed(1);}},
          title:{display:true,text:'Correlation',color:'#7a92b4',font:{size:10}}},
        y_aud:{type:'linear',position:'right',grid:{drawOnChartArea:false},
          ticks:{color:'#e2eaf6',callback:function(v){return v.toFixed(3);}},
          title:{display:true,text:'AUD/USD',color:'#e2eaf6',font:{size:10}}}
      },
      plugins:{
        legend:{display:false},
        tooltip:{backgroundColor:'#101e36',borderColor:'#1b2d4f',borderWidth:1,
          titleColor:'#e2eaf6',bodyColor:'#7a92b4',
          filter:function(item){return !!item.dataset.label;},
          callbacks:{
            title:function(items){return items[0]?items[0].label:'';},
            label:function(item){
              var v=item.raw,lbl=item.dataset.label;
              if(v===null||v===undefined) return null;
              if(item.dataset.yAxisID==='y_aud'){
                if(!lbl||lbl==='\xb12σ'||lbl==='\xb11σ') return null;
                return lbl+': '+v.toFixed(4);
              }
              return lbl+': r='+(v>=0?'+':'')+v.toFixed(2);
            }
          }
        }
      }
    }
  });

  DRIVERS.forEach(function(name){
    var el=document.getElementById('corr_leg_'+name);
    if(!el) return;
    el.addEventListener('click',function(){
      var idx=DRIVER_IDX[name];
      if(hiddenDrivers.has(name)){
        hiddenDrivers.delete(name);
        corrChart.setDatasetVisibility(idx,true);
        el.style.opacity='1';
        el.title='Click to hide';
      } else {
        hiddenDrivers.add(name);
        corrChart.setDatasetVisibility(idx,false);
        el.style.opacity='0.3';
        el.title='Click to show';
      }
      corrChart.update();
    });
  });

  function fmtDate(d){
    return new Date(d+'T00:00:00').toLocaleDateString('en-AU',{day:'numeric',month:'short',year:'numeric'});
  }

  function corrUpdateStats(slice){
    DRIVERS.forEach(function(name){
      var vals=slice.map(function(r){return r[name];}).filter(function(v){return v!==null&&v!==undefined;});
      var elVal=document.getElementById('corr_val_'+name);
      var elTag=document.getElementById('corr_tag_'+name);
      if(!elVal) return;
      if(vals.length===0){
        elVal.textContent='n/c'; elVal.style.color='#3d5270';
        elTag.textContent='insufficient data'; elTag.style.color='#3d5270'; return;
      }
      var avg=vals.reduce(function(a,b){return a+b;},0)/vals.length;
      elVal.textContent=(avg>=0?'+':'')+avg.toFixed(2);
      var exp=DRIVER_SIGN[name];
      var isAnomaly=Math.abs(avg)>0.25&&(avg>0?1:-1)!==exp;
      if(isAnomaly){
        elVal.style.color='#f05a52'; elTag.textContent='⚠ sign anomaly'; elTag.style.color='#f05a52';
      } else if(Math.abs(avg)<0.15){
        elVal.style.color='#e09438'; elTag.textContent='weak / mixed'; elTag.style.color='#7a92b4';
      } else {
        elVal.style.color='#2fcb9a';
        elTag.textContent=(exp>0?'expected +ve':'expected −ve'); elTag.style.color='#7a92b4';
      }
    });
  }

  function corrApply(){
    var lo=+corrMin.value,hi=+corrMax.value,n=activeSeries.length-1;
    var slice=activeSeries.slice(lo,hi+1);
    var rebuilt=buildDatasets(slice);
    corrChart.data.labels=rebuilt.labels;
    corrChart.data.datasets.forEach(function(ds,i){ds.data=rebuilt.datasets[i].data;});
    corrChart.update();
    var fill=document.getElementById('corr_trackFill');
    fill.style.left=(lo/n*100)+'%'; fill.style.right=((n-hi)/n*100)+'%';
    var start=activeSeries[lo]&&activeSeries[lo].date;
    var end=activeSeries[hi]&&activeSeries[hi].date;
    if(start) document.getElementById('corr_rangeStart').textContent=fmtDate(start);
    if(end)   document.getElementById('corr_rangeEnd').textContent=fmtDate(end);
    corrUpdateStats(slice);
  }

  corrMin.addEventListener('input',function(){
    if(+corrMin.value>+corrMax.value-2) corrMin.value=+corrMax.value-2; corrApply();});
  corrMax.addEventListener('input',function(){
    if(+corrMax.value<+corrMin.value+2) corrMax.value=+corrMin.value+2; corrApply();});

  document.getElementById('corr_presets').addEventListener('click',function(e){
    var btn=e.target.closest('[data-y]'); if(!btn) return;
    this.querySelectorAll('.preset').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    var yrs=+btn.dataset.y,n=activeSeries.length-1;
    if(yrs===0){corrMin.value=0;}
    else{var cutoff=Date.now()-yrs*365*864e5;
      var idx=activeSeries.findIndex(function(r){return new Date(r.date).getTime()>=cutoff;});
      corrMin.value=idx<0?0:idx;}
    corrMax.value=n; corrApply();
  });

  document.getElementById('corr_toggleBtns').addEventListener('click',function(e){
    var btn=e.target.closest('[data-w]'); if(!btn) return;
    this.querySelectorAll('.preset').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    activeSeries=btn.dataset.w==='20'?CORR20_SERIES:CORR60_SERIES;
    var n=activeSeries.length-1;
    corrMin.value=0; corrMax.value=n; corrMin.max=corrMax.max=n; corrApply();
  });

  corrApply();
})();
</script>"""

# ---------------------------------------------------------------------------
# BETA SENSITIVITY PANEL
# ---------------------------------------------------------------------------
BETA_PANEL_HTML = """
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px">
  <div style="font-size:0.72rem;color:var(--text-dim)">
    Rolling OLS betas &nbsp;&middot;&nbsp; adjust driver moves &rarr; implied AUD/USD impact
  </div>
  <div id="beta_toggleBtns" class="presets" style="margin-top:0">
    <button class="preset active" data-w="252">252d</button>
    <button class="preset" data-w="60">60d</button>
  </div>
</div>

<div class="panel-box" style="padding:18px 22px 16px">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:18px;flex-wrap:wrap;gap:6px">
    <div class="panel-title" style="font-size:15px">Beta sensitivity tool</div>
    <div style="font-size:0.68rem;color:var(--text-faint)">
      R&sup2;&nbsp;=&nbsp;<span id="beta_r2">—</span> &nbsp;&middot;&nbsp;
      model explains <span id="beta_r2_pct">—</span> of daily variance
    </div>
  </div>

  <!-- Driver rows -->
  <div id="beta_rows" style="display:flex;flex-direction:column;gap:14px;margin-bottom:22px">

    <!-- DXY -->
    <div class="beta-row" data-driver="dxy" data-unit="pct" data-sign="-1">
      <div class="beta-label"><span class="dot" style="background:#f05a52"></span>DXY</div>
      <div class="beta-stat">
        <span class="beta-val" id="beta_val_dxy">—</span>
        <span class="beta-badge" id="beta_badge_dxy"></span>
      </div>
      <div class="beta-slider-wrap">
        <input type="range" class="beta-slider" id="beta_sl_dxy"
               min="-30" max="30" value="0" step="1">
        <span class="beta-slider-label" id="beta_sllbl_dxy">0.0%</span>
      </div>
      <div class="beta-implied">
        <span class="beta-arrow" id="beta_arr_dxy">→</span>
        <span class="beta-impl-val" id="beta_impl_dxy">—</span>
      </div>
    </div>

    <!-- AU-US Spread -->
    <div class="beta-row" data-driver="spread" data-unit="bp" data-sign="1">
      <div class="beta-label"><span class="dot" style="background:#4b8ef0"></span>AU&ndash;US 2y</div>
      <div class="beta-stat">
        <span class="beta-val" id="beta_val_spread">—</span>
        <span class="beta-badge" id="beta_badge_spread"></span>
      </div>
      <div class="beta-slider-wrap">
        <input type="range" class="beta-slider" id="beta_sl_spread"
               min="-20" max="20" value="0" step="1">
        <span class="beta-slider-label" id="beta_sllbl_spread">0 bp</span>
      </div>
      <div class="beta-implied">
        <span class="beta-arrow" id="beta_arr_spread">→</span>
        <span class="beta-impl-val" id="beta_impl_spread">—</span>
      </div>
    </div>

    <!-- S&P 500 -->
    <div class="beta-row" data-driver="spx" data-unit="pct" data-sign="1">
      <div class="beta-label"><span class="dot" style="background:#2fcb9a"></span>S&amp;P 500</div>
      <div class="beta-stat">
        <span class="beta-val" id="beta_val_spx">—</span>
        <span class="beta-badge" id="beta_badge_spx"></span>
      </div>
      <div class="beta-slider-wrap">
        <input type="range" class="beta-slider" id="beta_sl_spx"
               min="-50" max="50" value="0" step="1">
        <span class="beta-slider-label" id="beta_sllbl_spx">0.0%</span>
      </div>
      <div class="beta-implied">
        <span class="beta-arrow" id="beta_arr_spx">→</span>
        <span class="beta-impl-val" id="beta_impl_spx">—</span>
      </div>
    </div>

    <!-- USD/CNY -->
    <div class="beta-row" data-driver="usdcnh" data-unit="pct" data-sign="-1">
      <div class="beta-label"><span class="dot" style="background:#c084fc"></span>USD/CNY</div>
      <div class="beta-stat">
        <span class="beta-val" id="beta_val_usdcnh">—</span>
        <span class="beta-badge" id="beta_badge_usdcnh"></span>
      </div>
      <div class="beta-slider-wrap">
        <input type="range" class="beta-slider" id="beta_sl_usdcnh"
               min="-30" max="30" value="0" step="1">
        <span class="beta-slider-label" id="beta_sllbl_usdcnh">0.0%</span>
      </div>
      <div class="beta-implied">
        <span class="beta-arrow" id="beta_arr_usdcnh">→</span>
        <span class="beta-impl-val" id="beta_impl_usdcnh">—</span>
      </div>
    </div>

    <!-- Iron ore -->
    <div class="beta-row" data-driver="iron" data-unit="pct" data-sign="1">
      <div class="beta-label"><span class="dot" style="background:#e09438"></span>Iron ore</div>
      <div class="beta-stat">
        <span class="beta-val" id="beta_val_iron">—</span>
        <span class="beta-badge" id="beta_badge_iron"></span>
      </div>
      <div class="beta-slider-wrap">
        <input type="range" class="beta-slider" id="beta_sl_iron"
               min="-50" max="50" value="0" step="1">
        <span class="beta-slider-label" id="beta_sllbl_iron">0.0%</span>
      </div>
      <div class="beta-implied">
        <span class="beta-arrow" id="beta_arr_iron">→</span>
        <span class="beta-impl-val" id="beta_impl_iron">—</span>
      </div>
    </div>

  </div><!-- /beta_rows -->

  <!-- Summary bar -->
  <div id="beta_summary" style="background:rgba(240,179,41,0.05);border:1px solid rgba(240,179,41,0.18);border-radius:10px;padding:14px 18px;display:flex;align-items:center;gap:24px;flex-wrap:wrap">
    <div>
      <div style="font-size:0.6rem;color:var(--text-faint);letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px">Current AUD/USD</div>
      <div id="beta_current" style="font-family:'Fraunces',serif;font-size:1.4rem;font-weight:600;color:var(--text)">—</div>
    </div>
    <div style="font-size:1.4rem;color:var(--text-faint)">→</div>
    <div>
      <div style="font-size:0.6rem;color:var(--text-faint);letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px">Net move</div>
      <div id="beta_net" style="font-family:'Fraunces',serif;font-size:1.4rem;font-weight:600;color:var(--text)">0.00%</div>
    </div>
    <div style="font-size:1.4rem;color:var(--text-faint)">→</div>
    <div>
      <div style="font-size:0.6rem;color:var(--text-faint);letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px">Implied AUD/USD</div>
      <div id="beta_implied_total" style="font-family:'Fraunces',serif;font-size:1.4rem;font-weight:600;color:var(--gold)">—</div>
    </div>
    <button id="beta_resetBtn" style="margin-left:auto;font-family:inherit;font-size:11px;letter-spacing:.06em;color:var(--text-faint);background:transparent;border:1px solid var(--panel-edge);padding:6px 14px;border-radius:16px;cursor:pointer">Reset</button>
  </div>

</div>
<p class="source" style="margin-top:12px">Rolling OLS: AUD/USD log-returns ~ DXY + AU&ndash;US 2y spread (&Delta;bp, lagged 1d) + S&amp;P 500 + USD/CNY + iron ore · all log-returns except spread · 252d / 60d window · daily data · Yahoo Finance, RBA F2, Alpha Vantage. &sigma; bands are trailing 252d mean &plusmn; std of rolling betas. Fat-tail caveat: &plusmn;2&sigma; breaches occur more often than 1-in-20 for financial data.</p>
"""

BETA_CSS = """
/* ── BETA SENSITIVITY PANEL ── */
.beta-row{display:grid;grid-template-columns:130px 120px 1fr 100px;align-items:center;gap:10px}
@media(max-width:640px){.beta-row{grid-template-columns:100px 100px 1fr 80px;gap:6px}}
.beta-label{display:flex;align-items:center;font-size:0.78rem;color:var(--text-dim)}
.beta-stat{display:flex;align-items:center;gap:7px}
.beta-val{font-family:'IBM Plex Mono',monospace;font-size:0.85rem;color:var(--text);min-width:58px}
.beta-badge{font-size:0.58rem;letter-spacing:.06em;padding:2px 7px;border-radius:10px;white-space:nowrap}
.beta-badge.sig2{background:rgba(240,148,56,0.18);color:var(--amber);border:1px solid rgba(240,148,56,0.35)}
.beta-badge.sig1{background:rgba(122,146,180,0.12);color:var(--text-faint);border:1px solid rgba(122,146,180,0.25)}
.beta-badge.nodta{background:rgba(61,82,112,0.15);color:var(--text-faint);font-style:italic}
.beta-slider-wrap{display:flex;align-items:center;gap:10px}
.beta-slider{-webkit-appearance:none;appearance:none;width:100%;height:4px;background:var(--panel-edge);border-radius:4px;outline:none;cursor:pointer}
.beta-slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--gold);border:2px solid var(--ink-2);cursor:grab;box-shadow:0 2px 6px rgba(0,0,0,.5)}
.beta-slider::-moz-range-thumb{width:16px;height:16px;border-radius:50%;background:var(--gold);border:2px solid var(--ink-2);cursor:grab}
.beta-slider-label{font-family:'IBM Plex Mono',monospace;font-size:0.72rem;color:var(--gold);min-width:52px;text-align:right}
.beta-implied{text-align:right}
.beta-arrow{color:var(--text-faint);margin-right:4px;font-size:0.75rem}
.beta-impl-val{font-family:'IBM Plex Mono',monospace;font-size:0.82rem}
.beta-impl-pos{color:#2fcb9a}.beta-impl-neg{color:#f05a52}.beta-impl-zero{color:var(--text-faint)}
.beta-slider:disabled{opacity:0.3;cursor:not-allowed}
"""

BETA_INIT_SCRIPT = """<script>
(function() {
  var DRIVERS = ['dxy','spread','spx','usdcnh','iron'];
  var UNITS   = {dxy:'pct',spread:'bp',spx:'pct',usdcnh:'pct',iron:'pct'};
  var activeLatest = LATEST_BETAS_252;
  var currentAUD   = null;

  function setCurrentAUD(v) {
    currentAUD = v;
    var el = document.getElementById('beta_current');
    if (el) el.textContent = v ? v.toFixed(4) : '—';
  }

  function betaFlagBadge(name, latest) {
    var d = latest[name]; if (!d) return null;
    var b = d.beta, mu = d.mean, u2 = d.u2, l2 = d.l2, u1 = d.u1, l1 = d.l1;
    if (b === null || b === undefined) return null;
    if (u2 !== null && (b > u2 || b < l2)) return {cls:'sig2', txt:'outside ±2σ'};
    if (u1 !== null && (b > u1 || b < l1)) return {cls:'sig1', txt:'near ±1σ'};
    return null;
  }

  function renderBetas(latest) {
    if (!latest || !latest.dxy) return;
    var r2 = latest.r2;
    document.getElementById('beta_r2').textContent = r2 !== null && r2 !== undefined ? r2.toFixed(3) : '—';
    document.getElementById('beta_r2_pct').textContent = r2 !== null && r2 !== undefined ? (r2*100).toFixed(1)+'%' : '—';

    DRIVERS.forEach(function(name) {
      var d = latest[name];
      var valEl   = document.getElementById('beta_val_'+name);
      var badgeEl = document.getElementById('beta_badge_'+name);
      var slEl    = document.getElementById('beta_sl_'+name);
      if (!valEl) return;

      if (!d || d.beta === null || d.beta === undefined) {
        valEl.textContent = 'n/a';
        valEl.style.color = 'var(--text-faint)';
        badgeEl.textContent = 'no data';
        badgeEl.className = 'beta-badge nodta';
        if (slEl) slEl.disabled = true;
        return;
      }
      slEl && (slEl.disabled = false);
      valEl.textContent = (d.beta >= 0 ? '+' : '') + d.beta.toFixed(4);
      valEl.style.color = 'var(--text)';
      var flag = betaFlagBadge(name, latest);
      if (flag) {
        badgeEl.textContent = flag.txt;
        badgeEl.className = 'beta-badge ' + flag.cls;
      } else {
        badgeEl.textContent = '';
        badgeEl.className = 'beta-badge';
      }
    });
    recalc();
  }

  function recalc() {
    var latest = activeLatest;
    if (!latest) return;
    var totalLR = 0;
    DRIVERS.forEach(function(name) {
      var slEl  = document.getElementById('beta_sl_'+name);
      var lblEl = document.getElementById('beta_sllbl_'+name);
      var arrEl = document.getElementById('beta_arr_'+name);
      var impEl = document.getElementById('beta_impl_'+name);
      if (!slEl || slEl.disabled) { if(impEl){impEl.textContent='—';impEl.className='beta-impl-val beta-impl-zero';} return; }

      var raw = +slEl.value;
      var unit = UNITS[name];
      var labelTxt = unit === 'bp' ? raw+' bp' : (raw/10).toFixed(1)+'%';
      if (lblEl) lblEl.textContent = labelTxt;

      var d = latest[name];
      if (!d || d.beta === null || d.beta === undefined) { if(impEl){impEl.textContent='—';impEl.className='beta-impl-val beta-impl-zero';} return; }

      // beta is AUD/USD log-return per unit of driver log-return (or per 1bp for spread)
      var driverMove = unit === 'bp' ? raw : raw / 10 / 100;  // convert slider units
      var lr = d.beta * driverMove;
      totalLR += lr;

      var pctMove = (Math.exp(lr) - 1) * 100;
      if (impEl) {
        if (Math.abs(pctMove) < 0.001) {
          impEl.textContent = '—'; impEl.className = 'beta-impl-val beta-impl-zero';
        } else {
          impEl.textContent = (pctMove >= 0 ? '+' : '') + pctMove.toFixed(3) + '%';
          impEl.className = 'beta-impl-val ' + (pctMove >= 0 ? 'beta-impl-pos' : 'beta-impl-neg');
        }
      }
      if (arrEl) arrEl.style.color = Math.abs(pctMove) < 0.001 ? 'var(--text-faint)' : (pctMove >= 0 ? '#2fcb9a' : '#f05a52');
    });

    var netPct = (Math.exp(totalLR) - 1) * 100;
    var netEl = document.getElementById('beta_net');
    if (netEl) {
      netEl.textContent = (netPct >= 0 ? '+' : '') + netPct.toFixed(3) + '%';
      netEl.style.color = Math.abs(netPct) < 0.001 ? 'var(--text)' : (netPct >= 0 ? '#2fcb9a' : '#f05a52');
    }
    var impTot = document.getElementById('beta_implied_total');
    if (impTot && currentAUD) {
      var implied = currentAUD * Math.exp(totalLR);
      impTot.textContent = implied.toFixed(4);
      impTot.style.color = Math.abs(netPct) < 0.001 ? 'var(--gold)' :
        (netPct >= 0 ? '#2fcb9a' : '#f05a52');
    } else if (impTot) { impTot.textContent = '—'; }
  }

  // Wire sliders
  DRIVERS.forEach(function(name) {
    var slEl = document.getElementById('beta_sl_'+name);
    if (slEl) slEl.addEventListener('input', recalc);
  });

  // Reset button
  var resetBtn = document.getElementById('beta_resetBtn');
  if (resetBtn) resetBtn.addEventListener('click', function() {
    DRIVERS.forEach(function(name) {
      var sl = document.getElementById('beta_sl_'+name);
      if (sl) { sl.value = 0; sl.disabled = false; }
      var lbl = document.getElementById('beta_sllbl_'+name);
      if (lbl) lbl.textContent = UNITS[name]==='bp' ? '0 bp' : '0.0%';
      var imp = document.getElementById('beta_impl_'+name);
      if (imp) { imp.textContent='—'; imp.className='beta-impl-val beta-impl-zero'; }
      var arr = document.getElementById('beta_arr_'+name);
      if (arr) arr.style.color='var(--text-faint)';
    });
    var netEl = document.getElementById('beta_net');
    if (netEl) { netEl.textContent='0.00%'; netEl.style.color='var(--text)'; }
    var imp = document.getElementById('beta_implied_total');
    if (imp && currentAUD) { imp.textContent=currentAUD.toFixed(4); imp.style.color='var(--gold)'; }
  });

  // Window toggle
  document.getElementById('beta_toggleBtns').addEventListener('click', function(e) {
    var btn = e.target.closest('[data-w]'); if (!btn) return;
    this.querySelectorAll('.preset').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    activeLatest = btn.dataset.w === '252' ? LATEST_BETAS_252 : LATEST_BETAS_60;
    renderBetas(activeLatest);
  });

  // Init — expose setCurrentAUD for inline call after AUD/USD value is known
  window._betaSetAUD = setCurrentAUD;
  renderBetas(activeLatest);
})();
</script>"""

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
{BETA_CSS}
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


<!-- ─── DRIVER ATTRIBUTION ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">DRIVER ATTRIBUTION</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {attribution_html}
</div>


<!-- ─── CORRELATION HISTORY ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">CORRELATION HISTORY</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {CORR_CHART_HTML}
</div>


<!-- ─── BETA SENSITIVITY ─── -->
<div class="section-header" style="margin-top:36px">
  <span class="sec-title" style="font-size:.65rem;letter-spacing:.14em">BETA SENSITIVITY</span>
  <div class="sec-line"></div>
</div>

<div style="background:var(--surface);border:1px solid var(--panel-edge);border-radius:12px;padding:22px 26px 18px">
  {BETA_PANEL_HTML}
</div>
<script>if(window._betaSetAUD) _betaSetAUD({data['audusd']:.4f});</script>


<!-- ─── 02 RATE DIFFERENTIALS ─── -->
<div class="section-header">
  <span class="sec-num">02</span>
  <span class="sec-title">Rate Differentials &amp; Yield Curve</span>
  <div class="sec-line"></div>
</div>

{RD_CHART_HTML}



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
      <td class="event-detail">May reading — releasing today (9:45am Beijing / 11:45am AEST); consensus ~51.0; upside supports AUD via CNH</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct via China demand</td>
    </tr>
    <tr>
      <td><span class="event-date">3 Jun</span></td>
      <td><span class="event-name">AU Q1 GDP</span></td>
      <td class="event-detail">qoq consensus +0.3%; yoy ~1.4%. Weak print would reinforce pressure for RBA to cut at Jun meeting</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct — domestic growth</td>
    </tr>
    <tr>
      <td><span class="event-date">4 Jun</span></td>
      <td><span class="event-name">AU RBA Governor Speech</span></td>
      <td class="event-detail">Any guidance on Jun 16–17 meeting likely to move AUD sharply</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct — rate guidance</td>
    </tr>
    <tr>
      <td><span class="event-date">6 Jun</span></td>
      <td><span class="event-name">US Nonfarm Payrolls</span></td>
      <td class="event-detail">May; consensus ~180k. Miss → risk-off / AUD downside; beat → USD strength</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Via USD &amp; risk sentiment</td>
    </tr>
    <tr>
      <td><span class="event-date">6 Jun</span></td>
      <td><span class="event-name">CFTC COT Release</span></td>
      <td class="event-detail">Jun-03 snapshot; watch AM net rebuild vs continued unwind from +40k prev</td>
      <td><span class="impact-badge impact-med">MED</span></td>
      <td class="dim-text">Positioning signal</td>
    </tr>
    <tr>
      <td><span class="event-date">11 Jun</span></td>
      <td><span class="event-name">US CPI (May)</span></td>
      <td class="event-detail">Core CPI key for FOMC guidance; hot print → hold narrative hardens, USD bid</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Via USD &amp; risk sentiment</td>
    </tr>
    <tr>
      <td><span class="event-date">12 Jun</span></td>
      <td><span class="event-name">AU CPI + Employment</span></td>
      <td class="event-detail">Monthly CPI indicator + labour force survey. Final data inputs before RBA Jun 16–17 meeting</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Direct — RBA reaction</td>
    </tr>
    <tr>
      <td><span class="event-date">15 Jun</span></td>
      <td><span class="event-name">CN Industrial Output / Retail</span></td>
      <td class="event-detail">May activity data — AUD proxy via commodity demand channel</td>
      <td><span class="impact-badge impact-med">MED</span></td>
      <td class="dim-text">Via China demand channel</td>
    </tr>
    <tr>
      <td><span class="event-date">16–17 Jun</span></td>
      <td><span class="event-name">RBA Board Meeting</span></td>
      <td class="event-detail">Rate decision + press conference. On hold at 4.35%. Cut scenario conditional on soft Q1 GDP + CPI</td>
      <td><span class="impact-badge impact-high">HIGH</span></td>
      <td class="dim-text">Primary AUD driver</td>
    </tr>
    <tr>
      <td><span class="event-date">17–18 Jun</span></td>
      <td><span class="event-name">FOMC Meeting</span></td>
      <td class="event-detail">Rate decision + SEP + dot plot update. Hold at 4.25–4.50% expected; guidance on cut timeline key for USD</td>
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
  </div>
</footer>

</main>

{chart_js}

{corr_data_script}
{CORR_INIT_SCRIPT}

{beta_data_script}
{BETA_INIT_SCRIPT}

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
      {_tile('AUD / JPY', f'{data["audjpy"]:.3f}', _ec(audjpy_chg))}
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

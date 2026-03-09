"""
orderflow_analyzer.py
=====================
קורא את ה-CSV שמייצר OrderFlowExporter.cpp בזמן אמת,
מריץ מודלי Poisson ו-Negative Binomial,
ומציג dashboard חי.

התקנה (פעם אחת):
    pip install pandas scipy matplotlib numpy watchdog

הרצה:
    python orderflow_analyzer.py
    
    או עם נתיב מותאם:
    python orderflow_analyzer.py --path "C:/SierraChart/Data/orderflow_export.csv"
"""

import argparse
import time
import os
import sys
from collections import deque
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.animation import FuncAnimation
from scipy import stats
from scipy.stats import nbinom, poisson

# ============================================================
# הגדרות
# ============================================================
DEFAULT_CSV_PATH    = r"C:\SierraChart\Data\orderflow_export.csv"
REFRESH_INTERVAL_MS = 500        # עדכון תצוגה כל 500ms
BASELINE_MINUTES    = 3          # חלון λ בסיסי
SHORT_SECONDS       = 30         # חלון λ קצר
ALERT_MULTIPLIER    = 2.0        # פי כמה = alert
PRICE_LEVEL_WINDOW  = 10         # כמה טיקים להגדיר "רמת מחיר"
MIN_TRADES_FOR_FIT  = 20         # מינימום trades לפני שמריצים NegBinom fit

# ============================================================
# מחלקת ניתוח Order Flow
# ============================================================
class OrderFlowAnalyzer:
    def __init__(self, csv_path: str):
        self.csv_path      = csv_path
        self.last_pos      = 0          # מיקום קריאה בקובץ
        self.trades        = deque()    # כל ה-trades בזיכרון
        self.price_levels  = {}         # price -> {'ask': [], 'bid': []}

        # NegBinom calibration
        self.negbinom_r    = None       # פרמטר r (מספר הצלחות)
        self.negbinom_p    = None       # פרמטר p

        # היסטוריית alerts
        self.alerts        = deque(maxlen=10)

    # --------------------------------------------------------
    # קריאת שורות חדשות בלבד מה-CSV
    # --------------------------------------------------------
    def read_new_trades(self):
        if not os.path.exists(self.csv_path):
            return []

        new_rows = []
        try:
            with open(self.csv_path, 'r') as f:
                f.seek(self.last_pos)
                lines = f.readlines()
                self.last_pos = f.tell()

            for line in lines:
                line = line.strip()
                if not line or line.startswith("Timestamp"):
                    continue
                parts = line.split(',')
                if len(parts) < 9:
                    continue
                try:
                    ts_str = parts[0].strip()
                    t = None
                    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
                        try:
                            t = datetime.strptime(ts_str, fmt)
                            break
                        except ValueError:
                            continue
                    if t is None:
                        continue
                    row = {
                        'time':   t,
                        'price':  float(parts[1]),
                        'volume': int(parts[2]),
                        'side':   int(parts[3]),
                        'label':  parts[4],
                    }
                    new_rows.append(row)
                    self.trades.append(row)
                except (ValueError, IndexError):
                    continue

        except Exception as e:
            print(f"[read error] {e}")

        # ניקוי trades ישנים (מעל 15 דקות) — לפי זמן הדאטא עצמו
        if self.trades:
            latest_time = self.trades[-1]['time']
            cutoff = latest_time - timedelta(minutes=15)
            while self.trades and self.trades[0]['time'] < cutoff:
                self.trades.popleft()

        return new_rows

    # --------------------------------------------------------
    # Poisson: חישוב λ בחלון נתון
    # --------------------------------------------------------
    def now_time(self) -> datetime:
        """מחזיר את הזמן הנוכחי לפי הדאטא עצמו (לא שעון המחשב)"""
        if self.trades:
            return self.trades[-1]['time']
        return datetime.now()

    def calc_lambda(self, window_seconds: int, side_filter=None) -> float:
        if not self.trades:
            return 0.0
        now = self.now_time()
        cutoff = now - timedelta(seconds=window_seconds)
        count = sum(
            1 for t in self.trades
            if t['time'] >= cutoff
            and (side_filter is None or t['side'] == side_filter)
        )
        return count / (window_seconds / 60.0)

    # --------------------------------------------------------
    # Poisson test: האם הקצב הנוכחי חריג?
    # מחזיר p-value (נמוך = חריג)
    # --------------------------------------------------------
    def poisson_test(self) -> dict:
        baseline_sec = BASELINE_MINUTES * 60

        lambda_base_ask  = self.calc_lambda(baseline_sec, side_filter=1)
        lambda_base_bid  = self.calc_lambda(baseline_sec, side_filter=-1)
        lambda_short_ask = self.calc_lambda(SHORT_SECONDS, side_filter=1)
        lambda_short_bid = self.calc_lambda(SHORT_SECONDS, side_filter=-1)

        now    = self.now_time()
        cutoff = now - timedelta(seconds=SHORT_SECONDS)
        ask_observed = sum(1 for t in self.trades if t['time'] >= cutoff and t['side'] == 1)
        bid_observed = sum(1 for t in self.trades if t['time'] >= cutoff and t['side'] == -1)

        ratio        = SHORT_SECONDS / baseline_sec
        ask_expected = lambda_base_ask * ratio * (baseline_sec / 60.0)
        bid_expected = lambda_base_bid * ratio * (baseline_sec / 60.0)

        ask_pval = 1 - poisson.cdf(ask_observed - 1, max(ask_expected, 0.001))
        bid_pval = 1 - poisson.cdf(bid_observed - 1, max(bid_expected, 0.001))

        return {
            'lambda_base_ask':  lambda_base_ask,
            'lambda_base_bid':  lambda_base_bid,
            'lambda_short_ask': lambda_short_ask,
            'lambda_short_bid': lambda_short_bid,
            'ask_observed':     ask_observed,
            'bid_observed':     bid_observed,
            'ask_pval':         ask_pval,
            'bid_pval':         bid_pval,
            'ask_spike':        ask_pval < 0.05,
            'bid_spike':        bid_pval < 0.05,
        }

    # --------------------------------------------------------
    # Negative Binomial: כיול
    # כמה מרקטים נדרשים לפרוץ רמת מחיר אחת?
    #
    # לוגיקה:
    # - מקבצים trades לפי רמת מחיר (טיק)
    # - סופרים כמה מרקטים נדרשו לפרוץ כל רמה (הכיוון הדומיננטי נצח)
    # - מכיילים NegBinom על ההיסטוגרמה של הספירות
    # --------------------------------------------------------
    def fit_negbinom(self) -> dict | None:
        if len(self.trades) < MIN_TRADES_FOR_FIT:
            return None

        # קיבוץ לפי טיק (ES טיק = 0.25)
        tick_size   = 0.25
        level_counts = {}  # price_level -> {'ask': int, 'bid': int}

        for t in self.trades:
            lvl = round(t['price'] / tick_size) * tick_size
            if lvl not in level_counts:
                level_counts[lvl] = {'ask': 0, 'bid': 0}
            if t['side'] == 1:
                level_counts[lvl]['ask'] += t['volume']
            else:
                level_counts[lvl]['bid'] += t['volume']

        # ספירת מרקטים עד "פריצה" (הצד הדומיננטי > פי 2 מהצד הנגדי)
        breakout_counts = []
        for lvl, counts in level_counts.items():
            total     = counts['ask'] + counts['bid']
            dominant  = max(counts['ask'], counts['bid'])
            if total > 0 and dominant > total * 0.65:  # 65%+ = פריצה
                breakout_counts.append(total)

        if len(breakout_counts) < 5:
            return None

        # Negative Binomial fit
        data = np.array(breakout_counts)
        mean = np.mean(data)
        var  = np.var(data)

        if var <= mean:
            # שונות <= תוחלת = לא מתאים ל-NegBinom, נחזיר Poisson
            return {'model': 'poisson', 'mu': mean, 'n_levels': len(breakout_counts)}

        # MOM estimation: r = mean^2 / (var - mean), p = mean / var
        r_est = (mean ** 2) / (var - mean)
        p_est = mean / var

        # log-likelihood
        ll = np.sum(nbinom.logpmf(data.astype(int), r_est, 1 - p_est))

        return {
            'model':        'negbinom',
            'r':            r_est,
            'p':            p_est,
            'mean':         mean,
            'std':          np.std(data),
            'log_likelihood': ll,
            'n_levels':     len(breakout_counts),
            # סיכוי שפריצה תקרה בפחות מ-X מרקטים
            'prob_lt_mean':  nbinom.cdf(int(mean), r_est, 1 - p_est),
        }

    # --------------------------------------------------------
    # זיהוי אסימטריה ברמה הנוכחית
    # --------------------------------------------------------
    def current_level_imbalance(self) -> dict:
        if not self.trades:
            return {}

        current_price = self.trades[-1]['price']
        tick_size     = 0.25
        # רמות קרובות: ±PRICE_LEVEL_WINDOW טיקים
        nearby_range  = PRICE_LEVEL_WINDOW * tick_size

        cutoff = self.now_time() - timedelta(seconds=60)
        ask_vol = sum(
            t['volume'] for t in self.trades
            if t['time'] >= cutoff
            and abs(t['price'] - current_price) <= nearby_range
            and t['side'] == 1
        )
        bid_vol = sum(
            t['volume'] for t in self.trades
            if t['time'] >= cutoff
            and abs(t['price'] - current_price) <= nearby_range
            and t['side'] == -1
        )
        total = ask_vol + bid_vol
        delta = ask_vol - bid_vol

        return {
            'ask_vol':    ask_vol,
            'bid_vol':    bid_vol,
            'delta':      delta,
            'imbalance':  delta / total if total > 0 else 0.0,
        }


# ============================================================
# Dashboard (Matplotlib)
# ============================================================
class Dashboard:
    def __init__(self, analyzer: OrderFlowAnalyzer):
        self.analyzer = analyzer

        plt.style.use('dark_background')
        self.fig = plt.figure(figsize=(14, 9))
        self.fig.suptitle('ES Order Flow — Poisson + NegBinom Monitor', 
                          fontsize=14, color='white')

        gs = gridspec.GridSpec(3, 3, figure=self.fig, hspace=0.45, wspace=0.4)

        # שורה 1: מטרים של λ
        self.ax_lambda_ask = self.fig.add_subplot(gs[0, 0])
        self.ax_lambda_bid = self.fig.add_subplot(gs[0, 1])
        self.ax_pval       = self.fig.add_subplot(gs[0, 2])

        # שורה 2: היסטוגרמת volume + NegBinom fit
        self.ax_hist       = self.fig.add_subplot(gs[1, :2])
        self.ax_imbalance  = self.fig.add_subplot(gs[1, 2])

        # שורה 3: log alerts
        self.ax_alerts     = self.fig.add_subplot(gs[2, :])
        self.ax_alerts.axis('off')

    # --------------------------------------------------------
    def update(self, frame):
        self.analyzer.read_new_trades()
        poisson_res = self.analyzer.poisson_test()
        nb_res      = self.analyzer.fit_negbinom()
        imb         = self.analyzer.current_level_imbalance()

        # ---- ניקוי ----
        for ax in [self.ax_lambda_ask, self.ax_lambda_bid,
                   self.ax_pval, self.ax_hist,
                   self.ax_imbalance, self.ax_alerts]:
            ax.cla()

        # ---- λ ASK ----
        self._draw_lambda_bar(
            self.ax_lambda_ask,
            poisson_res['lambda_base_ask'],
            poisson_res['lambda_short_ask'],
            "ASK Lifts (Buyers)",
            poisson_res['ask_spike']
        )

        # ---- λ BID ----
        self._draw_lambda_bar(
            self.ax_lambda_bid,
            poisson_res['lambda_base_bid'],
            poisson_res['lambda_short_bid'],
            "BID Hits (Sellers)",
            poisson_res['bid_spike']
        )

        # ---- p-values ----
        self._draw_pval(self.ax_pval, poisson_res)

        # ---- NegBinom histogram ----
        self._draw_negbinom(self.ax_hist, nb_res)

        # ---- Imbalance gauge ----
        self._draw_imbalance(self.ax_imbalance, imb)

        # ---- Alerts log ----
        self._check_and_draw_alerts(poisson_res, nb_res, imb)

    # --------------------------------------------------------
    def _draw_lambda_bar(self, ax, base, current, title, spike):
        colors   = ['#4a9eff', '#ff6b35' if spike else '#2ecc71']
        values   = [base, current]
        labels   = [f'Baseline\n{BASELINE_MINUTES}min', f'Current\n{SHORT_SECONDS}s']
        bars     = ax.bar(labels, values, color=colors, width=0.5)
        ax.set_title(title, fontsize=9, color='white')
        ax.set_ylabel('Trades/min', fontsize=8)

        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
                    f'{val:.1f}', ha='center', va='bottom', fontsize=9, color='white')

        if spike:
            ax.set_facecolor('#3d1a0a')
            ax.set_title(f'⚡ {title} — SPIKE!', fontsize=9, color='#ff6b35')

    # --------------------------------------------------------
    def _draw_pval(self, ax, res):
        ask_color = '#ff4444' if res['ask_spike'] else '#2ecc71'
        bid_color = '#ff4444' if res['bid_spike'] else '#2ecc71'

        ax.barh(['ASK p-val', 'BID p-val'],
                [res['ask_pval'], res['bid_pval']],
                color=[ask_color, bid_color])
        ax.axvline(0.05, color='yellow', linestyle='--', linewidth=1, label='α=0.05')
        ax.set_xlim(0, 1)
        ax.set_title('Poisson p-value\n(< 0.05 = spike)', fontsize=9, color='white')
        ax.legend(fontsize=7)

        ax.text(0.5, -0.25,
                f"Observed: ASK={res['ask_observed']}  BID={res['bid_observed']}",
                transform=ax.transAxes, ha='center', fontsize=8, color='#aaaaaa')

    # --------------------------------------------------------
    def _draw_negbinom(self, ax, nb_res):
        ax.set_title('NegBinom: Markets needed to break price level', fontsize=9, color='white')
        if nb_res is None:
            ax.text(0.5, 0.5, f'Collecting data...\n(need {MIN_TRADES_FOR_FIT}+ trades)',
                    transform=ax.transAxes, ha='center', va='center',
                    fontsize=10, color='#888888')
            return

        if nb_res['model'] == 'poisson':
            mu = nb_res['mu']
            x  = np.arange(0, int(mu * 3) + 1)
            ax.bar(x, poisson.pmf(x, mu), color='#4a9eff', alpha=0.7, label=f'Poisson μ={mu:.1f}')
            ax.axvline(mu, color='yellow', linestyle='--', linewidth=1.5, label=f'Mean={mu:.1f}')
        else:
            r, p = nb_res['r'], nb_res['p']
            mean = nb_res['mean']
            x    = np.arange(0, int(mean * 3) + 1)
            ax.bar(x, nbinom.pmf(x, r, 1 - p), color='#4a9eff', alpha=0.7,
                   label=f'NegBinom r={r:.1f}')
            ax.axvline(mean, color='yellow', linestyle='--', linewidth=1.5,
                       label=f'Mean={mean:.1f} ± {nb_res["std"]:.1f}')
            ax.text(0.97, 0.95,
                    f'P(break < mean)={nb_res["prob_lt_mean"]:.1%}\nn={nb_res["n_levels"]} levels',
                    transform=ax.transAxes, ha='right', va='top',
                    fontsize=8, color='#cccccc',
                    bbox=dict(boxstyle='round', facecolor='#1a1a2e', alpha=0.7))

        ax.set_xlabel('Number of aggressive markets', fontsize=8)
        ax.set_ylabel('Probability', fontsize=8)
        ax.legend(fontsize=8)

    # --------------------------------------------------------
    def _draw_imbalance(self, ax, imb):
        ax.set_title('Current Level\nImbalance (last 1min)', fontsize=9, color='white')
        if not imb:
            return

        delta = imb.get('imbalance', 0)
        color = '#2ecc71' if delta > 0.2 else '#ff4444' if delta < -0.2 else '#ffcc00'

        ax.barh(['Delta'], [delta], color=color, height=0.5)
        ax.set_xlim(-1, 1)
        ax.axvline(0, color='white', linewidth=0.5)
        ax.axvline(0.3, color='green', linewidth=0.5, linestyle='--')
        ax.axvline(-0.3, color='red', linewidth=0.5, linestyle='--')

        ax.text(0.5, 0.2,
                f"ASK: {imb.get('ask_vol', 0)}  BID: {imb.get('bid_vol', 0)}\n"
                f"Δ = {imb.get('delta', 0):+d}",
                transform=ax.transAxes, ha='center', fontsize=9, color='white')

    # --------------------------------------------------------
    def _check_and_draw_alerts(self, poisson_res, nb_res, imb):
        now_str = self.analyzer.now_time().strftime("%H:%M:%S")

        if poisson_res['ask_spike']:
            msg = f"{now_str} ⚡ ASK SPIKE: λ={poisson_res['lambda_short_ask']:.1f} vs base={poisson_res['lambda_base_ask']:.1f} (p={poisson_res['ask_pval']:.3f})"
            if not self.analyzer.alerts or self.analyzer.alerts[-1] != msg:
                self.analyzer.alerts.append(msg)

        if poisson_res['bid_spike']:
            msg = f"{now_str} ⚡ BID SPIKE: λ={poisson_res['lambda_short_bid']:.1f} vs base={poisson_res['lambda_base_bid']:.1f} (p={poisson_res['bid_pval']:.3f})"
            if not self.analyzer.alerts or self.analyzer.alerts[-1] != msg:
                self.analyzer.alerts.append(msg)

        if imb and abs(imb.get('imbalance', 0)) > 0.5:
            direction = "BUYERS" if imb['imbalance'] > 0 else "SELLERS"
            msg = f"{now_str} 🔥 IMBALANCE: {direction} dominating ({imb['imbalance']:.0%})"
            if not self.analyzer.alerts or self.analyzer.alerts[-1] != msg:
                self.analyzer.alerts.append(msg)

        self.ax_alerts.axis('off')
        self.ax_alerts.set_title('Recent Alerts', fontsize=9, color='white', loc='left')

        alerts_list = list(self.analyzer.alerts)[-8:]
        for i, alert in enumerate(reversed(alerts_list)):
            color = '#ff6b35' if 'SPIKE' in alert else '#ff4444' if 'IMBALANCE' in alert else '#cccccc'
            self.ax_alerts.text(0.01, 0.85 - i * 0.12, alert,
                                transform=self.ax_alerts.transAxes,
                                fontsize=8, color=color, va='top')

    # --------------------------------------------------------
    def run(self):
        ani = FuncAnimation(self.fig, self.update,
                            interval=REFRESH_INTERVAL_MS, cache_frame_data=False)
        plt.show()


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='ES Order Flow Analyzer')
    parser.add_argument('--path', default=DEFAULT_CSV_PATH,
                        help='Path to CSV exported by Sierra Chart ACSIL study')
    args = parser.parse_args()

    print(f"[*] Reading from: {args.path}")
    print(f"[*] Baseline window: {BASELINE_MINUTES} minutes")
    print(f"[*] Short window: {SHORT_SECONDS} seconds")
    print(f"[*] Alert multiplier: {ALERT_MULTIPLIER}x")
    print("[*] Waiting for data...")

    analyzer  = OrderFlowAnalyzer(args.path)
    dashboard = Dashboard(analyzer)
    dashboard.run()

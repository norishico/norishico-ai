"""analyze_f1_2024.py - F1未勝利 2024年単年不振の深掘り分析

btv6_2024.json の F1_未勝利主流accel bet_records を対象に:
  1. 月別ROI (前半/後半/特定月集中?)
  2. オッズ帯別ROI (15-20 / 20-25 / 25-33)
  3. 血統別ROI (SS系 vs KK系、サイヤ別)
  4. 騎手別ROI (上位騎手)
  5. 会場別ROI
  6. 人気別ROI (1-3人気 / 4-6人気 / 7-8人気)
  7. 他年との構造比較 (2022/2023/2025)
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_f1(year):
    p = ROOT / f"btv6_{year}.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return [b for b in data["bet_records"] if b.get("rule") == "F1_未勝利主流accel"]


def agg(bets, keyfn):
    d = defaultdict(lambda: {"n": 0, "cost": 0, "ret": 0, "win": 0})
    for b in bets:
        k = keyfn(b)
        s = d[k]
        s["n"] += 1
        s["cost"] += b.get("cost", 1000)
        s["ret"] += b.get("ret", 0)
        if b.get("honmei_finish") == 1:
            s["win"] += 1
    out = []
    for k, s in sorted(d.items(), key=lambda x: -x[1]["ret"] + x[1]["cost"]):
        roi = s["ret"] / s["cost"] * 100 if s["cost"] else 0
        winrate = s["win"] / s["n"] * 100 if s["n"] else 0
        out.append((k, s["n"], s["cost"], s["ret"], s["ret"] - s["cost"], roi, winrate))
    return out


def _fmt(rows, title):
    print(f"\n--- {title} ---")
    print(f"{'key':<22} {'N':>4} {'cost':>8} {'ret':>8} {'profit':>10} {'ROI':>7} {'win%':>6}")
    for k, n, c, r, p, roi, w in rows:
        print(f"{str(k)[:22]:<22} {n:>4} {c:>8,} {r:>8,} {p:>+10,} {roi:>6.1f}% {w:>5.1f}%")


def month_key(b):
    d = b.get("date", "")
    return d[:7] if d else "?"


def odds_band(b):
    o = b.get("honmei_odds") or 0
    if o < 18: return "15-18"
    if o < 22: return "18-22"
    if o < 26: return "22-26"
    if o < 30: return "26-30"
    return "30-33"


def main():
    target_year = 2024
    f1 = load_f1(target_year)
    if not f1:
        print(f"btv6_{target_year}.json not found or empty")
        return 1

    cost = sum(b.get("cost", 1000) for b in f1)
    ret = sum(b.get("ret", 0) for b in f1)
    wins = sum(1 for b in f1 if b.get("honmei_finish") == 1)
    print(f"=== F1未勝利 {target_year}年 ({len(f1)}R) ===")
    print(f"投資 {cost:,} / 回収 {ret:,} / 損益 {ret-cost:+,} / ROI {ret/cost*100:.1f}% / 勝率 {wins/len(f1)*100:.1f}%")

    _fmt(agg(f1, month_key), "月別")
    _fmt(agg(f1, odds_band), "オッズ帯別")
    _fmt(agg(f1, lambda b: b.get("venue", "?")), "会場別")

    # 人気帯 — bet_records に popularity は無いので odds → 人気推定は不可。
    # 代わりに honmei_odds の分布は上で出てる。

    # 他年比較
    print("\n--- 年次比較 (F1) ---")
    print(f"{'year':<6} {'N':>4} {'profit':>10} {'ROI':>7} {'win%':>6}")
    for y in (2021, 2022, 2023, 2024, 2025):
        arr = load_f1(y)
        if not arr:
            continue
        c = sum(b.get("cost", 1000) for b in arr)
        r = sum(b.get("ret", 0) for b in arr)
        w = sum(1 for b in arr if b.get("honmei_finish") == 1)
        roi = r / c * 100 if c else 0
        print(f"{y:<6} {len(arr):>4} {r-c:>+10,} {roi:>6.1f}% {w/len(arr)*100:>5.1f}%")

    # 当たったレース一覧 (2024)
    hits = [b for b in f1 if b.get("honmei_finish") == 1]
    print(f"\n--- 2024 的中レース {len(hits)}件 ---")
    for b in hits[:20]:
        print(f"  {b.get('date')} {b.get('venue')}{b.get('race_num')}R "
              f"{b.get('honmei_name')} {b.get('honmei_odds')}倍 ret={b.get('ret')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""MLセカンドオピニオン — XGBoostで勝率予測

v6のスコアリングは変えず、MLモデルを並走させて「v6とMLの両方が推す馬」を特定。
区間ラップなしでも動く特徴量設計。

Usage:
  python ml_second_opinion.py              # 学習+評価
  python ml_second_opinion.py --shap       # SHAP重要度も出力
"""

import sys, json, sqlite3, numpy as np
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = Path(__file__).parent / 'keiba.db'


def extract_features(conn):
    """DBから特徴量を抽出"""
    print('Extracting features...')

    q = '''
    SELECT r.race_id, r.date, r.venue, r.surface, r.distance, r.track_cond,
           r.horse_name, r.horse_num, r.num_horses, r.finish, r.odds, r.popularity,
           r.pos4, r.last3f, r.time_sec, r.week_num, r.kai, r.age, r.sex,
           r.weight_kg, r.horse_weight, r.prev_finish, r.prev_distance,
           r.sire, r.dam_sire, r.race_name, r.margin, r.jockey
    FROM results r
    WHERE r.finish > 0 AND r.finish < 90 AND r.num_horses >= 8
      AND r.odds > 0 AND r.odds < 200
      AND r.date BETWEEN '2020-01-01' AND '2026-12-31'
    ORDER BY r.date, r.venue, r.race_num, r.horse_num
    '''
    rows = conn.execute(q).fetchall()
    print(f'  Raw rows: {len(rows)}')

    # Venue/surface encoding
    venue_map = {'札幌':1,'函館':2,'福島':3,'新潟':4,'東京':5,'中山':6,'中京':7,'京都':8,'阪神':9,'小倉':10}
    sex_map = {'牡':0, '牝':1, '騸':2}

    features = []
    labels = []
    meta = []

    for r in rows:
        venue = r[2] or ''
        surface = r[3] or ''
        sf = 1 if '芝' in str(surface) else 0
        dist = r[4] or 1600
        cond_map = {'良':0, '稍':1, '重':2, '不':3}
        cond = cond_map.get(str(r[5] or '')[:1], 0)
        hnum = r[7] or 0
        nhorses = r[8] or 12
        odds = r[10] or 10
        pop = r[11] or 5
        pos4 = r[12] or 0
        last3f = r[13] or 0
        time_sec = r[14] or 0
        wn = r[15] or 1
        age = r[17] or 3
        sex = sex_map.get(r[18], 0)
        wkg = r[19] or 55
        hwt = r[20] or 480
        prev_fin = r[21] or 5
        prev_dist = r[22] or dist

        # Derived features
        gate_ratio = hnum / nhorses if nhorses > 0 else 0.5
        phase = 0 if wn <= 3 else (1 if wn <= 5 else 2)
        dist_change = dist - (prev_dist or dist)
        log_odds = np.log(odds + 1)

        feat = [
            venue_map.get(venue, 0),  # 0: venue
            sf,                        # 1: surface (芝=1, ダ=0)
            dist,                      # 2: distance
            cond,                      # 3: track condition
            nhorses,                   # 4: num_horses
            gate_ratio,                # 5: gate_ratio
            log_odds,                  # 6: log_odds
            pop,                       # 7: popularity
            phase,                     # 8: opening phase
            age,                       # 9: age
            sex,                       # 10: sex
            wkg,                       # 11: weight_kg
            hwt,                       # 12: horse_weight
            prev_fin,                  # 13: prev_finish
            dist_change,               # 14: distance_change
        ]

        features.append(feat)
        labels.append(1 if r[9] == 1 else 0)  # win or not
        meta.append({
            'race_id': r[0], 'date': r[1], 'venue': venue,
            'horse_name': (r[6] or '').strip(), 'odds': odds,
            'finish': r[9], 'race_name': r[25] or '',
            'popularity': pop,
        })

    feature_names = [
        'venue', 'surface', 'distance', 'track_cond', 'num_horses',
        'gate_ratio', 'log_odds', 'popularity', 'phase',
        'age', 'sex', 'weight_kg', 'horse_weight',
        'prev_finish', 'dist_change',
    ]

    return np.array(features), np.array(labels), meta, feature_names


def train_and_evaluate(X, y, meta, feature_names, show_shap=False):
    """Walk-forward: 2020-2023学習 → 2024-2026評価"""
    from xgboost import XGBClassifier
    from sklearn.metrics import roc_auc_score, log_loss

    # Split by year
    dates = [m['date'] for m in meta]
    train_mask = np.array([d < '2024-01-01' for d in dates])
    test_mask = ~train_mask

    X_train, y_train = X[train_mask], y[train_mask]
    X_test, y_test = X[test_mask], y[test_mask]
    meta_test = [m for m, mask in zip(meta, test_mask) if mask]

    print(f'\nTrain: {len(X_train)} rows ({y_train.sum()} wins)')
    print(f'Test:  {len(X_test)} rows ({y_test.sum()} wins)')

    # Train XGBoost
    model = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric='logloss',
        random_state=42,
        verbosity=0,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    # Evaluate
    probs_test = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, probs_test)
    ll = log_loss(y_test, probs_test)
    print(f'\nTest AUC: {auc:.4f}')
    print(f'Test LogLoss: {ll:.4f}')

    # Feature importance
    print('\nFeature Importance (gain):')
    imp = model.feature_importances_
    for idx in np.argsort(imp)[::-1]:
        print(f'  {feature_names[idx]:>15s}: {imp[idx]:.4f}')

    # Simulate ROI: bet on top-predicted horse per race if prob > threshold
    # Group test data by race
    races = defaultdict(list)
    for i, m in enumerate(meta_test):
        races[m['race_id']].append((probs_test[i], m, y_test[i]))

    print('\n=== MLセカンドオピニオン ROIシミュレーション (2024-2026) ===\n')

    for threshold_pct in [90, 95, 97]:
        total_cost = 0; total_ret = 0; n_bets = 0; n_wins = 0
        for rid, horses in races.items():
            # Find horse with highest ML probability
            horses.sort(key=lambda x: -x[0])
            top_prob, top_meta, top_win = horses[0]

            # Only bet if ML confidence is above threshold
            prob_rank = top_prob  # absolute probability
            if prob_rank < (threshold_pct / 1000):  # rough threshold
                continue

            odds = top_meta['odds']
            if odds < 2 or odds > 100:
                continue

            n_bets += 1
            total_cost += 1000
            if top_win:
                total_ret += odds * 1000
                n_wins += 1

        if n_bets > 0:
            roi = total_ret / total_cost * 100
            wr = n_wins / n_bets * 100
            print(f'  Top{100-threshold_pct}% conf: {n_bets}R  WR={wr:.1f}%  ROI={roi:.0f}%  P&L={total_ret-total_cost:+,.0f}')

    # SHAP analysis
    if show_shap:
        try:
            import shap
            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X_test[:1000])
            print('\nSHAP mean |value| (top features):')
            mean_shap = np.abs(shap_values).mean(axis=0)
            for idx in np.argsort(mean_shap)[::-1][:10]:
                print(f'  {feature_names[idx]:>15s}: {mean_shap[idx]:.4f}')
        except ImportError:
            print('\nshap not installed, skipping SHAP analysis')

    return model, probs_test, meta_test


def main():
    show_shap = '--shap' in sys.argv

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")

    X, y, meta, feature_names = extract_features(conn)
    model, probs, meta_test = train_and_evaluate(X, y, meta, feature_names, show_shap)

    conn.close()
    print('\nDone.')


if __name__ == '__main__':
    main()

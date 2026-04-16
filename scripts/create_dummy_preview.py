"""ダミー予想データで全新機能(U1/A1/nicks)の表示確認用"""
import json, os, time
from datetime import datetime, timedelta

sat = datetime(2026, 4, 18)
sat_str = sat.strftime('%Y%m%d')
date_key = sat.strftime('%Y-%m-%d')

races = [
    {'race_id': f'{sat_str}06020511', 'venue': '中山', 'race_num': 11,
     'race_name': '皐月賞トライアル', 'surface': '芝', 'distance': 2000,
     'track_cond': '良', 'start_time': '15:35', 'num_horses': 16,
     'horses': [{'name': f'ダミー{i}', 'odds': str(3+i*2), 'waku': (i%8)+1, 'horse_num': i+1} for i in range(16)]},
    {'race_id': f'{sat_str}08020509', 'venue': '阪神', 'race_num': 9,
     'race_name': '淡路特別', 'surface': '芝', 'distance': 1800,
     'track_cond': '稍重', 'start_time': '13:50', 'num_horses': 12,
     'horses': [{'name': f'テスト{i}', 'odds': str(5+i*3), 'waku': (i%8)+1, 'horse_num': i+1} for i in range(12)]},
    {'race_id': f'{sat_str}03020505', 'venue': '福島', 'race_num': 5,
     'race_name': '3歳未勝利', 'surface': 'ダ', 'distance': 1700,
     'track_cond': '良', 'start_time': '11:35', 'num_horses': 14,
     'horses': [{'name': f'フク{i}', 'odds': str(8+i*4), 'waku': (i%8)+1, 'horse_num': i+1} for i in range(14)]},
]

preds = [
    # Race 1: ★★★ cushion+nicks+momentum強力支持
    {
        'race': {'race_id': races[0]['race_id'], 'venue': '中山', 'race_num': 11,
                 'race_name': '皐月賞トライアル', 'surface': '芝', 'distance': 2000,
                 'track_cond': '良', 'start_time': '15:35', 'num_horses': 16},
        '_date_key': date_key, 'grade': 'G2',
        'buy_type': 'v6_star3',
        'honmei': {
            'horse_name': 'ゴールデンスター', 'horse_num': 5, 'jockey': 'C.ルメール',
            'odds': 8.2, 'popularity': 3, 'total_score': 82.5, 'waku': 3,
            '_sire': 'エピファネイア', '_dam_sire': 'ディープインパクト',
            '_blood_score': 72, '_prev_pos4': 3, '_running_style': '先行',
            'accel_lap': True, 'has_good_train': True, 'trainer': '国枝栄',
            'bias_overcome': False, 'bias_close_loss': False, 'surface_switch': '',
            '_score_breakdown': {
                'base': 71.2, 'course_blood': 3.5, 'gate_cond_blood': 2.1,
                'track_bias': 0.0, 'venue_sire': 2.5, 'venue_damsire': 1.2,
                'cushion_sire': 1.0, 'nicks': 1.5
            },
        },
        'ni': {'horse_name': 'シルバーウインド', 'horse_num': 8, 'jockey': '川田将雅',
               'odds': 5.1, 'popularity': 2, 'total_score': 77.8, 'waku': 4},
        'momentum': {'initial_odds': 10.5, 'current_odds': 8.2,
                     'change_pct': 21.9, 'label': '🔥強力支持'},
        'reasons': ['コース適性◎', '調教好仕上がり', 'スコア突出'],
        'special_horse': None, 'nige_candidates': 1,
    },
    # Race 2: ★★ venue_sire+nicks+momentum支持上昇
    {
        'race': {'race_id': races[1]['race_id'], 'venue': '阪神', 'race_num': 9,
                 'race_name': '淡路特別', 'surface': '芝', 'distance': 1800,
                 'track_cond': '稍重', 'start_time': '13:50', 'num_horses': 12},
        '_date_key': date_key, 'grade': '3勝',
        'buy_type': 'v6_star2',
        'honmei': {
            'horse_name': 'サクラフェアリー', 'horse_num': 3, 'jockey': '武豊',
            'odds': 9.8, 'popularity': 4, 'total_score': 76.3, 'waku': 2,
            '_sire': 'キズナ', '_dam_sire': 'ボストンハーバー',
            '_blood_score': 68, '_prev_pos4': 2, '_running_style': '差し',
            'accel_lap': True, 'has_good_train': True, 'trainer': '友道康夫',
            'bias_overcome': True, 'bias_close_loss': False, 'surface_switch': '',
            '_score_breakdown': {
                'base': 68.5, 'course_blood': 0.0, 'gate_cond_blood': 1.8,
                'track_bias': 1.5, 'venue_sire': 3.0, 'venue_damsire': 0.0,
                'cushion_sire': 0.0, 'nicks': 1.5
            },
        },
        'ni': {'horse_name': 'ミヤコブレイブ', 'horse_num': 7, 'jockey': '横山武史',
               'odds': 6.5, 'popularity': 2, 'total_score': 72.1, 'waku': 4},
        'momentum': {'initial_odds': 11.2, 'current_odds': 9.8,
                     'change_pct': 12.5, 'label': '↑支持上昇'},
        'reasons': ['前走不利克服', '配当妙味◎'],
        'special_horse': None,
    },
    # Race 3: F1別枠 momentum支持低下
    {
        'race': {'race_id': races[2]['race_id'], 'venue': '福島', 'race_num': 5,
                 'race_name': '3歳未勝利', 'surface': 'ダ', 'distance': 1700,
                 'track_cond': '良', 'start_time': '11:35', 'num_horses': 14},
        '_date_key': date_key, 'grade': '未勝利',
        'buy_type': None,
        'honmei': {
            'horse_name': 'フクノマジック', 'horse_num': 2, 'jockey': '石橋脩',
            'odds': 22.5, 'popularity': 7, 'total_score': 65.2, 'waku': 1,
            '_sire': 'ロードカナロア', '_dam_sire': 'サクラバクシンオー',
            '_blood_score': 55, '_prev_pos4': 5, '_running_style': '追込',
            'accel_lap': True, 'has_good_train': True, 'trainer': '萩原清',
            'bias_overcome': False, 'bias_close_loss': False, 'surface_switch': '',
            '_score_breakdown': {
                'base': 62.0, 'course_blood': 0.0, 'gate_cond_blood': 0.0,
                'track_bias': 0.0, 'venue_sire': 0.0, 'venue_damsire': 0.0,
                'cushion_sire': 0.0, 'nicks': 0.0
            },
        },
        'ni': {'horse_name': 'フクノスピード', 'horse_num': 9, 'jockey': '田辺裕信',
               'odds': 15.0, 'popularity': 5, 'total_score': 61.8, 'waku': 5},
        'momentum': {'initial_odds': 18.0, 'current_odds': 22.5,
                     'change_pct': -25.0, 'label': '⚠️支持低下'},
        'reasons': [],
        'special_horse': {
            'horse_name': 'フクノドリーム', 'horse_num': 11, 'jockey': '横山和生',
            'odds': 25.0, 'waku': 6, 'rule': 'F1_未勝利主流accel'
        },
    },
]

with open('weekend_predictions.json', 'w', encoding='utf-8') as f:
    json.dump(preds, f, ensure_ascii=False, indent=2)
with open('this_week_races.json', 'w', encoding='utf-8') as f:
    json.dump(races, f, ensure_ascii=False, indent=2)
os.utime('this_week_races.json', (time.time(), time.time()))

print("Dummy data created: 3 races")
print("  1. ★★★ cushion+nicks+🔥強力支持")
print("  2. ★★  venue_sire+nicks+↑支持上昇")
print("  3. F1   ⚠️支持低下")

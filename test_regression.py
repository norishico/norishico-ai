"""回帰テスト — 日付判定・hotspotマッチング・horse_name照合
Usage: py -m pytest test_regression.py -v
"""
import json
import sqlite3
from pathlib import Path

PROJ_DIR = Path(__file__).parent


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 日付判定: race_idから正しい曜日が出るか
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestDateDetection:
    def test_odd_day_is_saturday(self):
        """奇数日目（01, 03, 05）は土曜"""
        from generate_weekend_prediction import get_date_info
        # 中山: day_code=05 → 土曜
        short, key, label = get_date_info('202606030501')
        assert '土' in short, f"day_code=05 should be Saturday, got {short}"

        # 福島: day_code=01 → 土曜
        short, key, label = get_date_info('202603010101')
        assert '土' in short, f"day_code=01 should be Saturday, got {short}"

    def test_even_day_is_sunday(self):
        """偶数日目（02, 04, 06）は日曜"""
        from generate_weekend_prediction import get_date_info
        # 中山: day_code=06 → 日曜
        short, key, label = get_date_info('202606030601')
        assert '日' in short, f"day_code=06 should be Sunday, got {short}"

        # 福島: day_code=02 → 日曜
        short, key, label = get_date_info('202603010201')
        assert '日' in short, f"day_code=02 should be Sunday, got {short}"

    def test_same_venue_different_days(self):
        """同会場でも日目が違えば異なる曜日になる"""
        from generate_weekend_prediction import get_date_info
        sat_short, sat_key, _ = get_date_info('202609020511')  # 阪神 day=05
        sun_short, sun_key, _ = get_date_info('202609020611')  # 阪神 day=06
        assert sat_key != sun_key, f"Same venue different days should have different keys: {sat_key} vs {sun_key}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. hotspotマッチング: 同venue同race_numの別日レースが混在しないか
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestHotspotMatching:
    def test_hotspot_uses_race_id_not_venue_rnum(self):
        """hotspot_picksはrace_idでマッチングされていること"""
        preds_file = PROJ_DIR / 'weekend_predictions.json'
        if not preds_file.exists():
            return  # skip if no predictions
        preds = json.load(open(preds_file, encoding='utf-8'))

        for p in preds:
            rid = p['race'].get('race_id', '')
            for hp in p.get('hotspot_picks', []):
                hp_rid = hp.get('race_id', '')
                if hp_rid:
                    assert hp_rid == rid, (
                        f"Hotspot race_id mismatch: pred={rid} hotspot={hp_rid} "
                        f"horse={hp.get('horse_name','')}"
                    )

    def test_no_cross_day_contamination(self):
        """土曜レースのhotspotに日曜の馬が混入していないこと"""
        preds_file = PROJ_DIR / 'weekend_predictions.json'
        if not preds_file.exists():
            return
        preds = json.load(open(preds_file, encoding='utf-8'))
        races_file = PROJ_DIR / 'this_week_races.json'
        if not races_file.exists():
            return
        races = json.load(open(races_file, encoding='utf-8'))

        # Build horse -> race_id map from raw data
        horse_race_map = {}
        for r in races:
            for h in r.get('horses', []):
                horse_race_map.setdefault(h['name'].strip(), set()).add(r['race_id'])

        for p in preds:
            rid = p['race'].get('race_id', '')
            for hp in p.get('hotspot_picks', []):
                hname = hp.get('horse_name', '').strip()
                valid_rids = horse_race_map.get(hname, set())
                if valid_rids:
                    assert rid in valid_rids, (
                        f"{hname} in hotspot of {rid} but only registered in {valid_rids}"
                    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. horse_name照合: DBのhorse_nameが正規化されているか
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestHorseNameLookup:
    def test_no_trailing_spaces(self):
        """resultsテーブルのhorse_nameに末尾スペースがないこと"""
        db_path = PROJ_DIR / 'keiba.db'
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM results WHERE horse_name != TRIM(horse_name)"
        ).fetchone()[0]
        conn.close()
        assert count == 0, f"{count} rows have trailing spaces in horse_name"

    def test_exact_match_works(self):
        """TRIM不要で完全一致検索できること"""
        db_path = PROJ_DIR / 'keiba.db'
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        # 適当な馬名を1つ取得
        row = conn.execute(
            "SELECT horse_name FROM results LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            return
        name = row[0]
        # 完全一致で引けることを確認
        count = conn.execute(
            "SELECT COUNT(*) FROM results WHERE horse_name = ?", (name,)
        ).fetchone()[0]
        count_trim = conn.execute(
            "SELECT COUNT(*) FROM results WHERE TRIM(horse_name) = ?", (name.strip(),)
        ).fetchone()[0]
        conn.close()
        assert count == count_trim, (
            f"Exact match ({count}) != TRIM match ({count_trim}) for '{name}'"
        )


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])

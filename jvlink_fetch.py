"""jvlink_fetch.py - 32bit Python script.

Fetch RACE dataspec via JV-Link, decode RA/SE/HR records using offsets
extracted from JVData_Struct.cs, and write to a JSON file consumable by
the 64-bit pipeline.

Run with the 32-bit Python that has pywin32 installed:
    C:\\Users\\westr\\AppData\\Local\\Programs\\Python\\Python312-32\\python.exe jvlink_fetch.py [fromtime]
"""

import csv
import json
import os
import sys
from collections import Counter

import win32com.client


JYO_NAME = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}

BABA_COND = {"1": "良", "2": "稍", "3": "重", "4": "不良"}

SEX_NAME = {"1": "牡", "2": "牝", "3": "セ"}
TOZAI_STABLE = {"1": "美", "2": "栗", "3": "他", "4": "他"}


def p(msg):
    print(msg, flush=True)


# Offsets are 1-indexed (matches C# JVData_Struct.cs MidB2S(ref bBuff, start, len)).
# Python slice = bytes[start-1 : start-1+len].
def slc(b, start, length):
    return b[start - 1 : start - 1 + length]


def decode(b, start, length):
    return slc(b, start, length).decode("cp932", "replace").rstrip(" \u3000")


def parse_race_id(b, base):
    """16-byte race id starting at offset `base`."""
    return {
        "year": decode(b, base, 4),
        "monthday": decode(b, base + 4, 4),
        "jyo": decode(b, base + 8, 2),
        "kaiji": decode(b, base + 10, 2),
        "nichiji": decode(b, base + 12, 2),
        "race_num": decode(b, base + 14, 2),
    }


def race_id_key(rid):
    return (
        f"{rid['year']}{rid['monthday']}{rid['jyo']}"
        f"{rid['kaiji']}{rid['nichiji']}{rid['race_num']}"
    )


# JV record type codes mapped to record byte length (per SetDataB byte[N] declarations).
RECORD_LENGTH = {
    "RA": 1272,
    "SE": 555,
    "HR": 719,
    "UM": 1609,
}


KEIRO_NAME = {
    "01": "栗毛", "02": "栃栗毛", "03": "鹿毛", "04": "黒鹿毛", "05": "青鹿毛",
    "06": "青毛", "07": "芦毛", "08": "栗粕毛", "09": "鹿粕毛", "10": "青粕毛",
    "11": "白毛",
}


def parse_se(b):
    """馬毎レース情報 (SE) - 555 bytes."""
    rid = parse_race_id(b, 12)
    return {
        "race_id": race_id_key(rid),
        **rid,
        "wakuban": decode(b, 28, 1),
        "umaban": decode(b, 29, 2),
        "ketto_num": decode(b, 31, 10),
        "bamei": decode(b, 41, 36),
        "sex_cd": decode(b, 79, 1),
        "barei": decode(b, 83, 2),
        "tozai_cd": decode(b, 85, 1),
        "chokyosi_code": decode(b, 86, 5),
        "chokyosi": decode(b, 91, 8),
        "futan": decode(b, 289, 3),
        "kisyu_code": decode(b, 297, 5),
        "kisyu": decode(b, 307, 8),
        "ba_taijyu": decode(b, 325, 3),
        "zogen_fugo": decode(b, 328, 1),
        "zogen_sa": decode(b, 329, 3),
        "ijyo_cd": decode(b, 332, 1),
        "nyusen": decode(b, 333, 2),
        "finish": decode(b, 335, 2),
        "time": decode(b, 339, 4),
        "pos1c": decode(b, 352, 2),
        "pos2c": decode(b, 354, 2),
        "pos3c": decode(b, 356, 2),
        "pos4c": decode(b, 358, 2),
        "odds": decode(b, 360, 4),
        "ninki": decode(b, 364, 2),
        "haron_l4": decode(b, 388, 3),
        "haron_l3": decode(b, 391, 3),
    }


def parse_ra(b):
    """レース詳細 (RA) - 1272 bytes."""
    rid = parse_race_id(b, 12)
    return {
        "race_id": race_id_key(rid),
        **rid,
        # RaceInfo substruct starts at offset 28
        "youbi_cd": decode(b, 28, 1),
        "toku_num": decode(b, 29, 4),
        "hondai": decode(b, 33, 60),  # race_name
        "ryakusyo10": decode(b, 573, 20),
        "kubun": decode(b, 611, 1),
        "grade_cd": decode(b, 615, 1),
        "kyori": decode(b, 698, 4),  # distance
        "track_cd": decode(b, 706, 2),
        "course_kubun": decode(b, 710, 2),
        "hasso_time": decode(b, 874, 4),
        "syusso_tosu": decode(b, 884, 2),  # 出走頭数
        "tenko_cd": decode(b, 888, 1),
        "siba_baba_cd": decode(b, 889, 1),
        "dirt_baba_cd": decode(b, 890, 1),
    }


def parse_um(b):
    """馬マスタ (UM) - JV_UM_UMA 1609 bytes. 血統+基本情報のみ抽出."""
    ketto_num = decode(b, 12, 10)
    # KETTO3_INFO is 46 bytes: HansyokuNum(10) + Bamei(36)
    # [0]=父, [3]=母, [4]=母父 (3代血統14エントリ)
    def ketto_name(idx):
        return decode(b, 205 + 46 * idx + 10, 36)
    def ketto_id(idx):
        return decode(b, 205 + 46 * idx, 10)
    birth = decode(b, 39, 8)                   # YYYYMMDD
    prize_heichi = decode(b, 1089, 9)          # 平地収得賞金累計(百円単位)
    return {
        "ketto_num": ketto_num,
        "bamei": decode(b, 47, 36),
        "birthday": birth,
        "keiro_cd": decode(b, 203, 2),
        "sire_id": ketto_id(0),
        "sire": ketto_name(0),
        "dam_id": ketto_id(3),
        "dam": ketto_name(3),
        "dam_sire": ketto_name(4),
        "breeder": decode(b, 891, 72),
        "owner": decode(b, 989, 64),
        "prize_heichi": prize_heichi,
    }


def _pay_info1(b, base):
    return {
        "umaban": decode(b, base, 2),
        "pay": decode(b, base + 2, 9),
        "ninki": decode(b, base + 11, 2),
    }


def _pay_info2(b, base, kumi_len=4):
    return {
        "kumi": decode(b, base, kumi_len),
        "pay": decode(b, base + kumi_len, 9),
        "ninki": decode(b, base + kumi_len + 9, 3),
    }


def _pay_info4(b, base):
    return {
        "kumi": decode(b, base, 6),
        "pay": decode(b, base + 6, 9),
        "ninki": decode(b, base + 15, 4),
    }


def parse_hr(b):
    """払戻 (HR) - JV_HR_PAY 719 bytes full decode."""
    rid = parse_race_id(b, 12)
    out = {"race_id": race_id_key(rid), **rid}
    out["toroku_tosu"] = decode(b, 28, 2)
    out["syusso_tosu"] = decode(b, 30, 2)
    out["tansho"]     = [_pay_info1(b, 103 + 13 * i) for i in range(3)]
    out["fukusho"]    = [_pay_info1(b, 142 + 13 * i) for i in range(5)]
    out["wakuren"]    = [_pay_info1(b, 207 + 13 * i) for i in range(3)]
    out["umaren"]     = [_pay_info2(b, 246 + 16 * i, 4) for i in range(3)]
    out["wide"]       = [_pay_info2(b, 294 + 16 * i, 4) for i in range(7)]
    out["umatan"]     = [_pay_info2(b, 454 + 16 * i, 4) for i in range(6)]
    out["sanrenpuku"] = [_pay_info2(b, 550 + 18 * i, 6) for i in range(3)]
    out["sanrentan"]  = [_pay_info4(b, 604 + 19 * i) for i in range(6)]
    return out


def _surface_from_track_cd(cd):
    s = (cd or "").strip()
    if not s.isdigit():
        return "", ""
    n = int(s)
    if 10 <= n <= 22:
        turf_type = "1" if n in (11, 13, 15, 17, 19, 21) else "0"
        return "芝", turf_type
    if 23 <= n <= 29:
        return "ダ", ""
    if 50 <= n <= 59:
        return "障", ""
    return "", ""


def _track_cond(ra):
    # 障/ダは dirt_baba_cd、それ以外は siba_baba_cd
    cd = ra.get("siba_baba_cd", "").strip() or ra.get("dirt_baba_cd", "").strip()
    return BABA_COND.get(cd, "")


def _time_raw_msstt(se_time):
    """SE.time 4桁(MMSSF, 分・秒・1/10秒) → RESULTS time_raw(MSSTT)にそのまま流し込める形へ."""
    s = (se_time or "").strip()
    return s if s.isdigit() and int(s) > 0 else ""


def _se_row(ra, se, um=None):
    """RESULTS_COL 52列の位置付きリストを返す."""
    row = [""] * 52
    year = ra.get("year", "")
    row[0]  = year[-2:] if len(year) >= 2 else ""
    md = ra.get("monthday", "")
    row[1]  = md[:2].lstrip("0") or "0"
    row[2]  = md[2:].lstrip("0") or "0"
    row[3]  = ra.get("kaiji", "").lstrip("0") or "0"
    row[4]  = JYO_NAME.get(ra.get("jyo", ""), "")
    row[5]  = ra.get("nichiji", "").lstrip("0") or "0"
    row[6]  = ra.get("race_num", "").lstrip("0") or "0"
    row[7]  = ra.get("hondai", "")
    row[8]  = (ra.get("syusso_tosu", "") or "").lstrip("0") or "0"
    surface, turf_type = _surface_from_track_cd(ra.get("track_cd", ""))
    row[9]  = surface
    row[10] = turf_type
    row[11] = (ra.get("kyori", "") or "").lstrip("0") or "0"
    row[12] = _track_cond(ra)
    row[13] = se.get("bamei", "")
    row[14] = SEX_NAME.get(se.get("sex_cd", ""), "")
    row[15] = (se.get("barei", "") or "").lstrip("0") or "0"
    row[16] = se.get("kisyu", "")
    futan = (se.get("futan", "") or "").lstrip("0")
    row[17] = f"{int(futan)/10:.1f}" if futan.isdigit() else ""
    row[18] = (se.get("ninki", "") or "").lstrip("0")
    row[19] = (se.get("finish", "") or "").lstrip("0")
    row[20] = (se.get("umaban", "") or "").lstrip("0")
    row[21] = se.get("wakuban", "")
    # 22 jockey_change / 23 margin / 24 prev_popularity 未取得
    row[25] = ""  # time_sec_raw は下流で time_raw から再計算される
    row[26] = _time_raw_msstt(se.get("time", ""))
    row[28] = (se.get("pos1c", "") or "").lstrip("0")
    row[29] = (se.get("pos2c", "") or "").lstrip("0")
    row[30] = (se.get("pos3c", "") or "").lstrip("0")
    row[31] = (se.get("pos4c", "") or "").lstrip("0")
    l3 = (se.get("haron_l3", "") or "").lstrip("0")
    row[32] = f"{int(l3)/10:.1f}" if l3.isdigit() else ""
    bw = (se.get("ba_taijyu", "") or "").lstrip("0")
    row[33] = bw if bw.isdigit() else ""
    row[34] = se.get("chokyosi", "")
    row[35] = TOZAI_STABLE.get(se.get("tozai_cd", ""), "")
    # 36 prize_won 未取得
    row[37] = se.get("ketto_num", "")
    odds = (se.get("odds", "") or "").lstrip("0")
    row[48] = f"{int(odds)/10:.1f}" if odds.isdigit() else ""
    # UM enrichment
    if um:
        row[36] = (um.get("prize_heichi", "") or "").lstrip("0")
        row[38] = um.get("sire_id", "")
        row[39] = um.get("dam_id", "")
        row[41] = um.get("owner", "")
        row[42] = um.get("breeder", "")
        row[43] = um.get("sire", "")
        row[44] = um.get("dam", "")
        row[45] = um.get("dam_sire", "")
        row[46] = KEIRO_NAME.get(um.get("keiro_cd", ""), "")
        b = um.get("birthday", "")
        row[47] = b[2:] if len(b) == 8 else ""  # YYMMDD
    return row


def _pay_int(s):
    s = (s or "").lstrip("0")
    return s if s.isdigit() else ""


def _hr_row(ra, hr):
    """DIVIDENDS_COL 224列."""
    row = [""] * 224
    year = ra.get("year", "") if ra else ""
    md = ra.get("monthday", "") if ra else ""
    row[0] = year[-2:] if len(year) >= 2 else hr.get("year", "")[-2:]
    row[1] = (md[:2] if md else hr.get("monthday", "")[:2]).lstrip("0") or "0"
    row[2] = (md[2:] if md else hr.get("monthday", "")[2:]).lstrip("0") or "0"
    row[3] = (ra or hr).get("kaiji", "").lstrip("0") or "0"
    row[4] = JYO_NAME.get((ra or hr).get("jyo", ""), "")
    row[5] = (ra or hr).get("nichiji", "").lstrip("0") or "0"
    row[6] = (ra or hr).get("race_num", "").lstrip("0") or "0"
    row[7] = ra.get("hondai", "") if ra else ""
    row[8] = ((ra.get("syusso_tosu", "") if ra else hr.get("syusso_tosu", "")) or "").lstrip("0") or "0"
    surface, turf_type = _surface_from_track_cd(ra.get("track_cd", "") if ra else "")
    row[9]  = surface
    row[10] = turf_type
    row[11] = ((ra.get("kyori", "") if ra else "") or "").lstrip("0")
    row[12] = _track_cond(ra) if ra else ""
    row[13] = (hr.get("syusso_tosu", "") or "").lstrip("0") or "0"
    # 14 race_id_raw 未取得
    # --- 単勝（1件目） ---
    t = hr["tansho"][0]
    row[87] = (t["umaban"] or "").lstrip("0")
    row[88] = _pay_int(t["pay"])
    # --- 複勝 3頭 ---
    for i, off in enumerate(((93, 94), (95, 96), (97, 98))):
        f = hr["fukusho"][i]
        row[off[0]] = (f["umaban"] or "").lstrip("0")
        row[off[1]] = _pay_int(f["pay"])
    # --- 馬連 ---
    u = hr["umaren"][0]
    kumi = u["kumi"]
    if len(kumi) == 4:
        row[115] = kumi[:2].lstrip("0")
        row[116] = kumi[2:].lstrip("0")
    row[117] = _pay_int(u["pay"])
    row[118] = (u["ninki"] or "").lstrip("0")
    # --- ワイド 3組 ---
    wide_bases = [(127, 128, 129, 130), (131, 132, 133, 134), (135, 136, 137, 138)]
    for i, b in enumerate(wide_bases):
        w = hr["wide"][i]
        k = w["kumi"]
        if len(k) == 4:
            row[b[0]] = k[:2].lstrip("0")
            row[b[1]] = k[2:].lstrip("0")
        row[b[2]] = _pay_int(w["pay"])
        row[b[3]] = (w["ninki"] or "").lstrip("0")
    # --- 馬単 ---
    ut = hr["umatan"][0]
    k = ut["kumi"]
    if len(k) == 4:
        row[155] = k[:2].lstrip("0")
        row[156] = k[2:].lstrip("0")
    row[157] = _pay_int(ut["pay"])
    row[158] = (ut["ninki"] or "").lstrip("0")
    # --- 3連複 ---
    sp = hr["sanrenpuku"][0]
    k = sp["kumi"]
    if len(k) == 6:
        row[179] = k[:2].lstrip("0")
        row[180] = k[2:4].lstrip("0")
        row[181] = k[4:].lstrip("0")
    row[182] = _pay_int(sp["pay"])
    row[183] = (sp["ninki"] or "").lstrip("0")
    # --- 3連単 ---
    st = hr["sanrentan"][0]
    k = st["kumi"]
    if len(k) == 6:
        row[194] = k[:2].lstrip("0")
        row[195] = k[2:4].lstrip("0")
        row[196] = k[4:].lstrip("0")
    row[197] = _pay_int(st["pay"])
    row[198] = (st["ninki"] or "").lstrip("0")
    return row


def write_target_csv(ra_records, se_records, hr_records, um_records, base_path):
    results_path = base_path + "_results.csv"
    dividends_path = base_path + "_dividends.csv"
    with open(results_path, "w", encoding="cp932", errors="replace", newline="") as f:
        w = csv.writer(f)
        for se in se_records:
            ra = ra_records.get(se["race_id"])
            if not ra:
                continue
            um = um_records.get(se.get("ketto_num", ""))
            w.writerow(_se_row(ra, se, um))
    with open(dividends_path, "w", encoding="cp932", errors="replace", newline="") as f:
        w = csv.writer(f)
        for rid, hr in hr_records.items():
            ra = ra_records.get(rid)
            w.writerow(_hr_row(ra, hr))
    p(f"written: {results_path} ({len(se_records)} rows)")
    p(f"written: {dividends_path} ({len(hr_records)} rows)")


UM_CACHE_PATH = "jvlink_um_cache.json"


def load_um_cache():
    if os.path.exists(UM_CACHE_PATH):
        try:
            with open(UM_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_um_cache(um):
    with open(UM_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(um, f, ensure_ascii=False)


def jv_read_all(jv, counts, handlers):
    """JVRead loop. handlers: dict[rec_type] -> callable(bytes)."""
    BUF = 200000
    total = 0
    while True:
        size, buf, _, filename = jv.JVRead(" " * BUF, BUF, " " * 256)
        if size == 0:
            break
        if size in (-1, -3):
            continue
        if size < 0:
            p(f"error size={size}")
            break
        b = buf[:size].encode("cp932", "replace")
        rec_type = b[0:2].decode("ascii", "replace")
        counts[rec_type] += 1
        total += 1
        fn = handlers.get(rec_type)
        if fn:
            try:
                fn(b)
            except Exception as e:
                p(f"parse err {rec_type}: {e}")
        if total % 1000 == 0:
            p(f"  progress: {total} records")
    return total


def main():
    fromtime = sys.argv[1] if len(sys.argv) > 1 else "20260320000000"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "jvlink_dump.json"

    jv = win32com.client.Dispatch("JVDTLab.JVLink")
    p("dispatch ok")
    rc = jv.JVInit("NORISHIKO/1.0.0.0")
    p(f"JVInit: {rc}")
    try:
        jv.JVClose()
    except Exception:
        pass

    se_records = []
    ra_records = {}
    hr_records = {}
    um_records = load_um_cache()
    p(f"UM cache loaded: {len(um_records)}")
    counts = Counter()

    # === RACE dataspec: RA/SE/HR ===
    rc_open = jv.JVOpen("RACE", fromtime, 1)
    p(f"JVOpen RACE: {rc_open}")
    if rc_open[0] != 0:
        p(f"FAIL: JVOpen RACE rc={rc_open[0]}")
        return

    def h_se(b):
        se_records.append(parse_se(b))

    def h_ra(b):
        r = parse_ra(b)
        ra_records[r["race_id"]] = r

    def h_hr(b):
        r = parse_hr(b)
        hr_records[r["race_id"]] = r

    def h_um(b):
        r = parse_um(b)
        if r["ketto_num"]:
            um_records[r["ketto_num"]] = r

    total = jv_read_all(jv, counts, {"SE": h_se, "RA": h_ra, "HR": h_hr})
    jv.JVClose()

    # === BLOD dataspec: UM (馬マスタ差分) ===
    # ※蓄積系dataspecは契約プラン要件で -111 (RACEのみ対応の速報系契約の可能性)
    # netkeibaスクレイパで代替中 (scripts/fetch_blood_netkeiba.py)
    try:
        rc_b = jv.JVOpen("BLOD", fromtime, 2)
        p(f"JVOpen BLOD: {rc_b}")
        if rc_b[0] == 0:
            total += jv_read_all(jv, counts, {"UM": h_um})
            jv.JVClose()
        else:
            p(f"BLOD open skipped rc={rc_b[0]}")
    except Exception as e:
        p(f"BLOD fetch skipped: {e}")

    # === 調教 dataspec: WOOD + SLOP ===
    # WOOD → WC レコード (ウッドチップ調教)
    # SLOP → HC レコード (坂路調教)
    # 両方とも (0, ファイル数, 0, ...) で取得可能、TFJV DAT と同形式
    hc_records = []  # 坂路調教レコード (bytes)
    wc_records = []  # ウッドチップ調教レコード (bytes)

    def h_hc(b):
        hc_records.append(b[2:].decode("cp932", "replace").rstrip())

    def h_wc(b):
        wc_records.append(b[2:].decode("cp932", "replace").rstrip())

    for spec, rec_type, handler in [("SLOP", "HC", h_hc), ("WOOD", "WC", h_wc)]:
        # 前回の状態をクリア
        try:
            jv.JVClose()
        except Exception:
            pass
        try:
            rc_tr = jv.JVOpen(spec, fromtime, 1)
            p(f"JVOpen {spec}: {rc_tr}")
            if rc_tr[0] == 0:
                tr_counts = Counter()
                total += jv_read_all(jv, tr_counts, {rec_type: handler})
                jv.JVClose()
                p(f"  {spec} counts: {dict(tr_counts)}")
            else:
                p(f"  {spec} open failed rc={rc_tr[0]}, retrying...")
                # -303 対策: 少し待って再試行
                import time
                time.sleep(1)
                try:
                    jv.JVClose()
                except Exception:
                    pass
                rc_tr2 = jv.JVOpen(spec, fromtime, 1)
                p(f"  {spec} retry: {rc_tr2}")
                if rc_tr2[0] == 0:
                    tr_counts = Counter()
                    total += jv_read_all(jv, tr_counts, {rec_type: handler})
                    jv.JVClose()
                    p(f"  {spec} counts (retry): {dict(tr_counts)}")
        except Exception as e:
            p(f"  {spec} fetch error: {e}")
    p(f"training fetched: HC={len(hc_records)} WC={len(wc_records)}")

    # JV-Link record は 9-byte 配信ヘッダ + TFJV DAT形式 (HC=47, WC=92 chars)
    # パースして training用JSONにダンプ
    def parse_training_line(line, source):
        """47 or 92 char inner record → dict"""
        if len(line) < 23:
            return None
        try:
            venue_flag = line[0]  # 0=栗東, 1=美浦
            year = line[1:5]
            mmdd = line[5:9]
            date = f"{year}-{mmdd[:2]}-{mmdd[2:]}"
            horse_id = line[13:23]
            lap2 = int(line[-6:-3]) / 10.0
            lap1 = int(line[-3:]) / 10.0
            return {
                "date": date,
                "venue": "栗東" if venue_flag == "0" else "美浦",
                "horse_id": horse_id,
                "source": source,
                "lap1": lap1,
                "lap2": lap2,
            }
        except (ValueError, IndexError):
            return None

    training_rows = []
    for raw in hc_records:
        # Strip 9-byte delivery header
        if len(raw) < 9:
            continue
        inner = raw[9:]
        rec = parse_training_line(inner, "sakuro")
        if rec:
            training_rows.append(rec)
    for raw in wc_records:
        if len(raw) < 9:
            continue
        inner = raw[9:]
        rec = parse_training_line(inner, "woodc")
        if rec:
            training_rows.append(rec)
    p(f"training parsed: {len(training_rows)} rows")

    import pathlib
    tr_dump = pathlib.Path("jvlink_dump_training.json")
    with open(tr_dump, "w", encoding="utf-8") as f:
        import json as _j
        _j.dump(training_rows, f, ensure_ascii=False)
    p(f"written: {tr_dump} ({len(training_rows)} rows)")


    save_um_cache(um_records)
    p(f"UM cache saved: {len(um_records)}")
    p(f"total={total} SE={len(se_records)} RA={len(ra_records)} HR={len(hr_records)} UM={len(um_records)}")
    for rt, n in sorted(counts.items(), key=lambda x: -x[1]):
        p(f"  {rt}: {n}")

    out = {
        "fromtime": fromtime,
        "ra": list(ra_records.values()),
        "se": se_records,
        "hr": list(hr_records.values()),
        "counts": dict(counts),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    p(f"written: {out_path}")

    base, _ = os.path.splitext(out_path)
    write_target_csv(ra_records, se_records, hr_records, um_records, base)


if __name__ == "__main__":
    main()

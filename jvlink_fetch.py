"""jvlink_fetch.py - 32bit Python script.

Fetch RACE dataspec via JV-Link, decode RA/SE/HR records using offsets
extracted from JVData_Struct.cs, and write to a JSON file consumable by
the 64-bit pipeline.

Run with the 32-bit Python that has pywin32 installed:
    C:\\Users\\westr\\AppData\\Local\\Programs\\Python\\Python312-32\\python.exe jvlink_fetch.py [fromtime]
"""

import json
import sys
from collections import Counter

import win32com.client


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
    "HR": 1369,  # not strictly needed, kept for reference
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


def parse_hr(b):
    """払戻 (HR) - extract main payouts only."""
    rid = parse_race_id(b, 12)
    out = {"race_id": race_id_key(rid), **rid}
    # Per JV_HR_PAY: tansho 3 entries (each 12 bytes: 2 umaban + 9 payout + 1 ninki) etc.
    # For simplicity, copy the raw record so 64-bit side can re-decode if needed.
    out["raw_b64"] = None  # placeholder - actual HR parsing left for next step
    return out


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

    rc_open = jv.JVOpen("RACE", fromtime, 1)
    p(f"JVOpen: {rc_open}")
    if rc_open[0] != 0:
        p(f"FAIL: JVOpen rc={rc_open[0]}")
        return

    BUF = 200000
    se_records = []
    ra_records = {}
    counts = Counter()
    total = 0
    while True:
        size, buf, _, filename = jv.JVRead(" " * BUF, BUF, " " * 256)
        if size == 0:
            break
        if size == -1:
            continue
        if size == -3:
            continue
        if size < 0:
            p(f"error size={size}")
            break
        b = buf[:size].encode("cp932", "replace")
        rec_type = b[0:2].decode("ascii", "replace")
        counts[rec_type] += 1
        total += 1
        try:
            if rec_type == "SE":
                se_records.append(parse_se(b))
            elif rec_type == "RA":
                rec = parse_ra(b)
                ra_records[rec["race_id"]] = rec
        except Exception as e:
            p(f"parse err {rec_type}: {e}")
        if total % 1000 == 0:
            p(f"  progress: {total} records")

    jv.JVClose()
    p(f"total={total} SE={len(se_records)} RA={len(ra_records)}")
    for rt, n in sorted(counts.items(), key=lambda x: -x[1]):
        p(f"  {rt}: {n}")

    out = {
        "fromtime": fromtime,
        "ra": list(ra_records.values()),
        "se": se_records,
        "counts": dict(counts),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    p(f"written: {out_path}")


if __name__ == "__main__":
    main()

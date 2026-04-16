"""data_source.py - Phase 2 データソース選択層

目的:
  - 本番scoring/予想パイプは keiba.db (netkeiba主系)を読む
  - JV-Link 並行検証時のみ keiba_staging.db を読むように一点で切替可能にする
  - auto_refresh の ±20%ロックで「どちらのDBを真と見るか」を明示

使い方:
  from data_source import get_active_db_path, get_data_source_info
  db_path = get_active_db_path()              # 通常は keiba.db
  info    = get_data_source_info()            # ダッシュボード表示用メタ

設定ファイル: data_source.json (無ければデフォルト=netkeiba/keiba.db)
  {
    "active": "netkeiba",            # "netkeiba" or "jvlink"
    "primary_db": "keiba.db",        # netkeiba 主系
    "parallel_db": "keiba_staging.db", # JV-Link 並行検証系
    "lock_source_of_truth": "netkeiba" # ±20%ロック判定で正とするソース
  }

環境変数 NORISHIKO_DATA_SOURCE=jvlink で一時切替可 (デバッグ用)
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "data_source.json"

DEFAULT_CONFIG = {
    "active": "netkeiba",
    "primary_db": "keiba.db",
    "parallel_db": "keiba_staging.db",
    "lock_source_of_truth": "netkeiba",
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def get_active_source():
    """環境変数 > 設定ファイル > デフォルト."""
    env = os.environ.get("NORISHIKO_DATA_SOURCE", "").strip().lower()
    if env in ("netkeiba", "jvlink"):
        return env
    return load_config()["active"]


def get_active_db_path():
    cfg = load_config()
    src = get_active_source()
    name = cfg["primary_db"] if src == "netkeiba" else cfg["parallel_db"]
    return str(ROOT / name)


def get_lock_source_of_truth():
    """auto_refresh ±20%ロックの判定基準となるデータソース名."""
    return load_config()["lock_source_of_truth"]


def get_data_source_info():
    """ダッシュボード/ログ表示用の状態スナップショット."""
    import datetime as dt
    cfg = load_config()
    active = get_active_source()
    info = {
        "active": active,
        "active_db_path": get_active_db_path(),
        "lock_source_of_truth": cfg["lock_source_of_truth"],
        "available": {},
    }
    for name, key in (("netkeiba", "primary_db"), ("jvlink", "parallel_db")):
        p = ROOT / cfg[key]
        if p.exists():
            st = p.stat()
            info["available"][name] = {
                "path": str(p),
                "size_bytes": st.st_size,
                "mtime": dt.datetime.fromtimestamp(st.st_mtime).isoformat(),
            }
        else:
            info["available"][name] = {"path": str(p), "exists": False}
    # last_fetch タイムスタンプを併記
    for tag, fname in (("netkeiba_last_fetch", "last_fetch.txt"),
                       ("jvlink_last_fetch",   "last_fetch_parallel.txt")):
        f = ROOT / fname
        info[tag] = f.read_text(encoding="utf-8").strip() if f.exists() else None
    return info


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(get_data_source_info(), ensure_ascii=False, indent=2))

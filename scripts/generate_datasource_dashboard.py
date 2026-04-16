"""generate_datasource_dashboard.py - Phase 2 両系統ステータスダッシュボード

出力: docs/datasource_status.html

内容:
  - netkeiba / JV-Link それぞれの最終取得時刻・DB サイズ・更新時刻
  - 直近の並行検証レポート (logs/diff_report_latest.json)
  - 合意基準の達成状況 (race_set/value/blood/prize_null)
  - 現在アクティブなデータソース (data_source.py)
"""

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_source import get_data_source_info  # noqa: E402

OUT = ROOT / "docs" / "datasource_status.html"
DIFF_LATEST = ROOT / "logs" / "diff_report_latest.json"
MONITORING_LATEST = ROOT / "logs" / "monitoring_latest.json"


def _fmt_bytes(n):
    if n is None:
        return "-"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def _fmt_dt(s):
    if not s:
        return "<span class='na'>未取得</span>"
    return s


def _badge(ok, label_ok="OK", label_ng="NG"):
    cls = "ok" if ok else "ng"
    txt = label_ok if ok else label_ng
    return f"<span class='badge {cls}'>{txt}</span>"


def build_html():
    info = get_data_source_info()
    diff = None
    if DIFF_LATEST.exists():
        try:
            diff = json.loads(DIFF_LATEST.read_text(encoding="utf-8"))
        except Exception as e:
            diff = {"error": f"parse error: {e}"}

    ns = info["available"].get("netkeiba", {})
    js = info["available"].get("jvlink", {})

    active = info["active"]

    rows = []
    def row(k, netkeiba, jvlink):
        rows.append(f"<tr><th>{k}</th><td>{netkeiba}</td><td>{jvlink}</td></tr>")

    row("データソース",
        "netkeiba (主系)" + (" ★active" if active == "netkeiba" else ""),
        "JV-Link (並行検証)" + (" ★active" if active == "jvlink" else ""))
    row("DBパス", ns.get("path", "-"), js.get("path", "-"))
    row("DBサイズ", _fmt_bytes(ns.get("size_bytes")), _fmt_bytes(js.get("size_bytes")))
    row("DB最終更新", _fmt_dt(ns.get("mtime")), _fmt_dt(js.get("mtime")))
    row("last_fetch", _fmt_dt(info.get("netkeiba_last_fetch")), _fmt_dt(info.get("jvlink_last_fetch")))

    diff_block = ""
    if diff and "error" not in diff:
        rs = diff["race_set"]; rc = diff["row_compare"]; j = diff["judgement"]
        diff_block = f"""
        <h2>並行検証レポート ({diff.get('since_date','?')} 以降)</h2>
        <p class='small'>生成: {diff.get('generated_at','?')}</p>
        <table>
          <tr><th>レース集合差</th><td>
            prod専有 {rs['only_in_prod_total']} / staging専有 {rs['only_in_staging_total']}
            {_badge(j['race_set_ok'])}
          </td></tr>
          <tr><th>値一致率</th><td>
            {rc['value_match_rate']*100:.2f}% (基準 99%) {_badge(j['value_ok'])}
          </td></tr>
          <tr><th>血統一致率</th><td>
            {rc['blood_match_rate']*100:.2f}% (基準 95%) {_badge(j['blood_ok'])}
          </td></tr>
          <tr><th>staging prize_won NULL率</th><td>
            {rc['staging_prize_null_rate']*100:.2f}% (基準 20%以下) {_badge(j['prize_null_ok'])}
          </td></tr>
          <tr><th>総合判定</th><td>{_badge(j['overall_ok'], '合意達成', '未達')}</td></tr>
        </table>
        """
        if rc["value_mismatch_samples"]:
            diff_block += "<h3>値ズレサンプル</h3><table><tr><th>race_id</th><th>horse</th><th>col</th><th>prod</th><th>staging</th></tr>"
            for s in rc["value_mismatch_samples"][:10]:
                diff_block += f"<tr><td>{s['race_id']}</td><td>{s['horse']}</td><td>{s['col']}</td><td>{s['prod']}</td><td>{s['staging']}</td></tr>"
            diff_block += "</table>"
    elif diff and "error" in diff:
        diff_block = f"<h2>並行検証レポート</h2><p class='ng'>エラー: {diff['error']}</p>"
    else:
        diff_block = "<h2>並行検証レポート</h2><p class='na'>まだレポートがありません (scripts/diff_sources.py を実行してください)</p>"

    # 実運用モニタリング
    monit_block = "<h2>実運用モニタリング</h2>"
    if MONITORING_LATEST.exists():
        try:
            ms = json.loads(MONITORING_LATEST.read_text(encoding="utf-8"))
            parsed = ms.get("parsed") or {}
            eff = ms.get("effective_verdict", "?")
            verdict_cls = {
                "GREEN+": "ok", "GREEN": "ok",
                "YELLOW": "", "RED": "ng",
                "INSUFFICIENT": "", "NO_DATA": "",
            }.get(eff, "")
            monit_block += f"""
            <p class='small'>生成: {ms.get('generated_at','?')}</p>
            <table>
              <tr><th>判定</th><td><span class='badge {verdict_cls}'>{eff}</span></td></tr>
              <tr><th>累積ROI</th><td>{parsed.get('roi','-')}%</td></tr>
              <tr><th>買い件数</th><td>{parsed.get('n','-')}R (INSUFFICIENT閾値 N&lt;{ms.get('min_sample_threshold','?')})</td></tr>
              <tr><th>累計損益</th><td>{parsed.get('profit','-')}円</td></tr>
              <tr><th>詳細</th><td><a href='monitoring_report.md'>monitoring_report.md</a></td></tr>
            </table>
            """
        except Exception as e:
            monit_block += f"<p class='ng'>parse error: {e}</p>"
    else:
        monit_block += "<p class='na'>まだレポートがありません (scripts/run_monitoring.py を実行してください)</p>"

    html = f"""<!DOCTYPE html>
<html lang='ja'><head><meta charset='utf-8'>
<title>norishiko_ai データソース状態</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 900px; margin: 20px auto; padding: 0 16px; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #f4f4f4; width: 22%; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.85em; margin-left: 6px; }}
.badge.ok {{ background: #d4edda; color: #155724; }}
.badge.ng {{ background: #f8d7da; color: #721c24; }}
.small {{ color: #666; font-size: 0.85em; }}
.na {{ color: #888; }}
.ng {{ color: #c0392b; }}
</style></head><body>
<h1>📊 データソース状態 (Phase 2)</h1>
<p class='small'>生成: {dt.datetime.now().isoformat()} / lock source of truth: <b>{info['lock_source_of_truth']}</b></p>
<table>
{''.join(rows)}
</table>
{diff_block}
{monit_block}
<hr><p class='small'>このページは scripts/generate_datasource_dashboard.py が生成します。
netkeiba が主系、JV-Link は並行検証系です。合意基準 (値一致率99%+) を満たしたら主系切替を判断します。</p>
</body></html>
"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    build_html()

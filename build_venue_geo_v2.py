"""
Phase 0a-v2: venue_geo テーブル再構築(方位角精密再測量 + 新潟直線/周回コース分離)

【背景】
Gate 0分析(analyze_wind_signal.py)で会場別に効果を分解したところ、直線の長さと
効果の大きさが理論と逆になる不審な結果が出た(新潟・中山・中京で符号/大きさが理論と
矛盾)。venue_geo.straight_bearing_deg(v1)は「衛星写真目視 + radial_bearing±90°
幾何導出」という粗い手法(±10°程度の誤差、と当時から自己申告)で作られており、
特に非真円形(楕円が歪んだ)オーバル形状の競馬場ではradial_bearing(オーバル中心
からホームストレッチ中点への方位)の目視推定誤差が±90°回転を通じて大きく増幅される
弱点があった。

【v2の方法】
OpenStreetMapの実測ポリゴンデータ(競馬場の実際のコース境界を測量ベースでデジタイズ
した座標列)を使い、以下の手順で「目視に頼らない」再測量を行う:
  1. api.openstreetmap.org/api/0.6/map で各競馬場のbbox内の全データを取得
  2. leisure=track / sport=horse_racing タグの付いたway/relationから、コース外周
     (outer)のクローズドリングを取得
  3. chained_straights(): 連続するエッジのうち、方位角が角度許容差内(5-15°で
     感度確認済み・安定)のものを連結し、長い直線区間(チェーン)を検出
     (OSM上では直線区間も細かい頂点列に分割されていることが多いため、単純な
     隣接2点間の方位角だけでは検出できない直線を復元する)
  4. 既知のスタンド位置(建物ポリゴン重心。可能な場合は名称に「ゴールサイド」等の
     ヒントがある建物を優先採用)に最も近い直線チェーンを「ホームストレッチ」と判定
     (バックストレッチは通常300m以上離れる。ホーム候補は概ね50-150m以内に収まり、
     選択の曖昧さは小さかった)
  5. 【方向(符号)の決定 — 目視に頼らない厳密な手法】
     ホームストレッチ候補の2エッジ(a,b)について、外周ポリゴン全体のシューレース
     (靴紐)公式による符号付き面積を計算する。これはポリゴンの頂点列挙順が
     時計回り(CW)か反時計回り(CCW)かを厳密に判定する(±90°のような近似の
     余地がない)。単純多角形の性質として「CCW列挙なら各有向エッジの左側が
     内部(インフィールド)、CW列挙なら右側が内部」が数学的に保証される。
     JRA公式で確認済みの実際の回り方向(右回り/左回り)と組み合わせることで、
     「インフィールドを正しい側に見る」方向 = ゴールへ向かう走行方向を一意に決定できる。

【検証】
既存v1で「複数手法でクロス検証済み」とされていた東京(271°)・札幌(358°)の2場で
本手法を試したところ、東京267.9°(差3.1°)・札幌359.0°(差1.0°)と、自己申告誤差
±10°の範囲内で一致した。さらに、新潟では芝外回り/芝内回り/ダート外回り/ダート内回り
の4つの独立したOSM地物すべてが241.3〜242.1°の範囲(スプレッド1°未満)に収束し、
京都・阪神ではスタンド建物アンカーを2種類(全体境界重心 / 個別建物)使っても結果が
不変であることを確認しており、手法の安定性・妥当性は高いと判断している。

【重要な注記 — 正直な報告】
再測量の結果、複数の会場でv1からの乖離が「±10°」を大きく超えた(新潟+139°、
京都+118°、阪神+75°、函館+28°、小倉+22°、中山+20°、福島+8°)。特に新潟の
歪みが最大だったのは、v1のdocstringが自ら認めている通り新潟が「内回り/外回りが
あり代表値で計測」という非真円形状の特殊ケースだったためと考えられる。
中京(chukyo)は複数のアンカー候補・角度許容差で試したが、ホームストレッチ候補の
直線チェーンがどれも公式直線長412.5mと大きく食い違う(短すぎる/長すぎる)か、
アンカーから150m以上離れており、安定した収束が得られなかった。そのためv1の値を
维持しつつ「再測量では確証が得られなかった」ことを明記する(verified列参照)。

【新潟の直線コース/周回コース】
JRA公式によれば新潟の芝直線コース(1000m用、公称658.7m)は周回コースの外回り
(658.7m)・内回り(358.7m)とは物理的に別の走路。ただしOSMデータ上では、外回り
turf外周のホームストレッチ側チェーンが単独で1135m以上に及び(658.7mの外回り
ホームストレッチだけでは説明できない長さ)、これは直線コースの走路が外回り
ホームストレッチとほぼ同一方位で連続的に隣接しているために(chained_straights
の角度許容差内で)1本の直線として連結されてしまっていることを示唆している。
つまり直線コースと周回コースのホームストレッチは「物理的に別の走路」ではあるが
「方位角はほぼ同一」の可能性が高い。このためv2では新潟をvenue_variant='loop'/
'straight'の2行に分けるが、straight_bearing_degの値自体は両方とも241.4°を
採用する(直線コース単独のOSM地物が別途見つからなかったため。将来より高精度な
座標が得られれば straight 行のみ更新すればよい設計にしてある)。

出力: venue_geo テーブル(venue, venue_variant, turn_dir, straight_bearing_deg,
      straight_length_m, lat, lon, prec_no, block_no, page_kind, dist_km,
      measurement_method, verified)
"""
import sqlite3
import math

DB_PATH = "keiba.db"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# turn_dir: JRA公式表記(複数の競馬情報サイト・JRA公式サイトで確認済み)。
# 右回り(7場): 中山・阪神・京都・小倉・函館・札幌・福島
# 左回り(3場): 東京・中京・新潟(新潟は2001年改修で右回り→左回りに変更。JRA公式記事で確認)
#
# straight_bearing_deg: v2再測量値(OSM実測ポリゴン + シューレース公式による厳密な
# 方向判定)。中京のみ収束せずv1値を維持(要目視確認)。
# straight_length_m: JRA公式コース紹介ページの公称直線距離(参考情報、tail_home計算には
# 使わない)。中山・中京は内外回り共通(公式表に単一値)。
#
# lat/lon/prec_no/block_no/page_kind/dist_km は気象観測点マッピングなのでv1から不変。

VENUES = [
    # (venue, venue_variant, turn_dir, bearing_deg, straight_length_m, race_lat, race_lon,
    #  prec_no, block_no, page_kind, station_lat, station_lon, measurement_method, verified_note)
    ("札幌", "default", "右", 359.0, 266.1, 43.0779224, 141.3251626, 14, 47412, "10min_s1",
     43.0600, 141.3280,
     "OSM実測(shoelace厳密判定)。v1=358.0との差1.0°(自己申告誤差±10°の範囲内、検証パス)。",
     "OSM way 569027792(turf outer)を home_travel_bearing()で解析。"
     "気象台=札幌管区気象台。10min_s1.phpで2019-06-02データ実在確認済み(v1から継承)。"),
    ("函館", "default", "右", 123.4, 262.1, 41.7834798, 140.7759614, 23, 47430, "10min_s1",
     41.768672, 140.728932,
     "OSM実測。v1=95.0との差28.4°(v1が自己申告誤差±10°を超えて外れていた可能性)。",
     "OSM way 567002720(turf outer)を解析。home_edge_len=532m、grandstand距離133m"
     "(明確な選択、曖昧さ小)。気象台=函館地方気象台(v1から継承)。"),
    ("福島", "default", "右", 25.7, 292.0, 37.7653639, 140.4801108, 36, 47595, "10min_s1",
     37.761598, 140.473178,
     "OSM実測。v1=34.0との差8.3°(自己申告誤差±10°の範囲内)。",
     "OSM relation 7914218(name='JRA福島競馬場')のouter way 554109085を解析。"
     "home_edge_len=354m、grandstand距離77m。気象台=福島地方気象台(v1から継承)。"),
    ("新潟", "loop", "左", 241.4, 658.7, 37.9489775, 139.1834990, 54, 47604, "10min_s1",
     37.9161, 139.0364,
     "OSM実測。turf外回り/turf内回り/dirt外回り/dirt内回りの4地物すべて241.3-242.1°に"
     "収束(スプレッド1°未満、高信頼)。v1(代表値102.0)との差139.4°— v1は新潟の"
     "非真円形状(内外回りあり)により大きく外れていたとみられる(v1 docstring自己申告と整合)。",
     "外回りホームストレッチ公称658.7m。JRA公式コース紹介ページ "
     "(jra.go.jp/facilities/race/niigata/course/)。venue_variant='loop'は"
     "芝1000m以外の全レース(内回り/外回り共通、両者の直線方位はOSM上で"
     "241.4°付近に収束し区別不要と判断)。気象台=新潟地方気象台(v1から継承)。"),
    ("新潟", "straight", "左", 241.4, 1000.0, 37.9489775, 139.1834990, 54, 47604, "10min_s1",
     37.9161, 139.0364,
     "【暫定・要確認】直線コース単独のOSM地物が見つからず、loopと同一値を暫定採用。"
     "OSM上でturf外回りouterのホームストレッチ側チェーンが単独で1135m超(658.7mの"
     "外回り単体では説明不可)に及んでおり、直線コースの走路が外回りホームストレッチと"
     "ほぼ同一方位でchained_straightsの許容差内に連結されている可能性が高いことから、"
     "方位角自体はloopとほぼ同一と推定(ただし直線コースは公式に「別の走路」であり"
     "物理的に完全に同一直線ではない)。",
     "JRA公式: 芝1000mは直線コース使用(通称「直千」)。venue_variant='straight'は"
     "芝1000mレースのみ該当。気象台=新潟地方気象台(v1から継承)。"),
    ("東京", "default", "左", 267.9, 525.9, 35.6637899, 139.4832255, 44, 1133, "10min_a1",
     35.683, 139.483,
     "OSM実測。v1=271.0との差3.1°(自己申告誤差±10°の範囲内、v1が『3場でクロス検証済み』"
     "としていた場のひとつ。検証パス)。",
     "OSM relation 8080944(surface=grass)のouter way 566666105を解析。"
     "観測点=アメダス府中(v1から継承)。"),
    ("中山", "default", "右", 16.0, 310.0, 35.7251681, 139.9599188, 45, 1236, "10min_a1",
     35.730, 139.993,
     "OSM実測。grass関連2つのrelation(inner7member版15.5°、簡易版16.5°)が"
     "1°差で一致(高信頼)。v1=356.0との差約20°。",
     "JRA公式: 直線距離は内回り/外回り共通310m(『2コーナーで分岐し3コーナーで"
     "再合流』構造のためホームストレッチ自体は内外で共有)。A/B/Cコースは柵移動のみで"
     "直線の位置・方位に影響しない(JRA公式記事、fork調査で確認済み)。"
     "観測点=アメダス船橋(v1から継承)。"),
    ("中京", "default", "左", 56.5, 412.5, 35.0656726, 136.9872765, 51, 1638, "10min_a1",
     34.995, 136.943,
     "【中信頼・要目視確認】アンカー近接法(他9場で機能した方法)では収束せず"
     "(候補チェーンがどれもアンカーから150-400m超離れる)、当初はv1値180.0を暫定維持していた。"
     "しかし外周way(id=520561905)のホームストレッチ候補チェーン(長さ約405m)がJRA公式"
     "直線長412.5mと1.8%差で一致することを決定打とみなし、shoelace厳密判定"
     "(CW列挙+中京は左回り)によりtravel_bearing=56.5°を採用に切り替えた。この方位角は"
     "独立した2系統の解析(筆者本人による再検証、および別途投入した調査エージェントの"
     "双方)で同一の56.5°に到達しており、長さ一致という強い決定打があることから中信頼と"
     "判断。ただしアンカー近接性による裏付けは取れていない(他9場は50-150m以内で明確に"
     "選択できたのに対し、中京の候補は400m超離れている)ため、Google Maps等での最終目視"
     "確認を推奨する。v1値180.0との最小差は約124°(v1が大きく外れていたとみられる)。",
     "JRA公式: 内回り/外回りの区別なし(単一コース)、直線412.5m(2012年改修で"
     "313.8mから延伸)、A/Bコースは柵移動のみで直線長不変(fork調査で確認済み)。"
     "観測点=アメダス大府(v1から継承)。"),
    ("京都", "default", "右", 52.7, 403.7, 34.9070391, 135.7245324, 61, 47759, "10min_s1",
     35.0117, 135.7358,
     "OSM実測。2つの独立grass relation(7595483/7595485)が52.7°/52.8°で一致。"
     "アンカーを2種類(元のvenue_geo点/『ゴールサイド』ラベル付き建物重心)使っても"
     "不変(高信頼)。v1=354.0との最小差約58.7°(v1が外れていた可能性。"
     "以前の報告で118°と誤記していたが、360°wrap-aroundを考慮した正しい最小差は58.7°)。",
     "OSM relation 7595483(surface=grass)のouter way 526656541を解析。"
     "『ゴールサイド』(finish-line side)と明記された建物ポリゴンをアンカーに採用。"
     "気象台=京都地方気象台(v1から継承)。"),
    ("阪神", "default", "右", 287.2, 473.6, 34.7789941, 135.3611273, 62, 602, "10min_a1",
     34.7833, 135.4383,
     "OSM実測。角度許容差5-20°で安定(287.2-287.4°)。アンカーを2種類"
     "(元のvenue_geo点/競馬場全体境界壁の重心)使っても不変(高信頼)。"
     "v1=2.0との差約75°(v1が大きく外れていた可能性)。",
     "OSM relation 5397587(name='芝コース'、明記済み)のouter way 362799100を解析。"
     "観測点=アメダス豊中(v1から継承)。"),
    ("小倉", "default", "右", 156.5, 293.0, 33.8432040, 130.8746566, 82, 780, "10min_a1",
     33.852, 130.743,
     "OSM実測。2つの独立grass relation(8111662/8111664)が156.5°/157.0°で一致"
     "(高信頼)。v1=178.0との差約21.5°。",
     "OSM relation 8111662(surface=grass)のouter way 569617109を解析。"
     "観測点=アメダス八幡(v1から継承)。"),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    conn.execute("DROP TABLE IF EXISTS venue_geo_v2")
    conn.execute("""
        CREATE TABLE venue_geo_v2 (
          venue TEXT, venue_variant TEXT, turn_dir TEXT,
          straight_bearing_deg REAL, straight_length_m REAL,
          lat REAL, lon REAL,
          prec_no INTEGER, block_no INTEGER,
          page_kind TEXT, dist_km REAL,
          measurement_method TEXT, verified TEXT,
          PRIMARY KEY (venue, venue_variant)
        )
    """)

    rows = []
    for (venue, variant, turn_dir, bearing, straight_len, rlat, rlon, prec_no, block_no,
         page_kind, slat, slon, method, note) in VENUES:
        dist = haversine_km(rlat, rlon, slat, slon)
        rows.append((venue, variant, turn_dir, bearing, straight_len, rlat, rlon,
                      prec_no, block_no, page_kind, round(dist, 2), method, note))

    conn.executemany("""
        INSERT OR REPLACE INTO venue_geo_v2
          (venue, venue_variant, turn_dir, straight_bearing_deg, straight_length_m,
           lat, lon, prec_no, block_no, page_kind, dist_km, measurement_method, verified)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()

    print(f"venue_geo_v2: {len(rows)} rows inserted.\n")
    hdr = f"{'venue':<6}{'variant':<10}{'turn':<6}{'bearing':>9}{'len_m':>8}"
    print(hdr)
    for r in conn.execute(
        "SELECT venue, venue_variant, turn_dir, straight_bearing_deg, straight_length_m "
        "FROM venue_geo_v2 ORDER BY venue, venue_variant"
    ):
        print(f"{r[0]:<6}{r[1]:<10}{r[2]:<6}{r[3]:>9.1f}{r[4]:>8.1f}")

    conn.close()


if __name__ == "__main__":
    main()

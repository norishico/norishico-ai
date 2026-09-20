// api/odds.js — 注目レースタブ「オッズ取得」ボタン用(2026-09-21新設)。
// netkeibaの非公開API(api_get_jra_odds.html)から単勝・複勝オッズを取得し、
// MC123のp1(1着率)と組み合わせて表示するためのデータを返す。買い目ではなく
// 参考情報(のりお個人の予想参考用)。詳細は .claude/rules/ayokeiba-mc.md 参照。
//
// GET /api/odds?nk_id=202609040701
//  -> 200 { nk_id, status:"before|middle|result", official_datetime, fetched_at,
//           tansho:{"1":{odds,pop}|null,...}, fukusho:{"1":{min,max,pop}|null,...},
//           source:"netkeiba" }
//  -> 400 { error:"invalid nk_id" }            nk_idが12桁数値でない
//  -> 502 { error:"upstream", detail:"..." }   netkeiba接続失敗・非JSON応答・想定外status

const CACHE_TTL_MS = 15000;
// 同一nk_idへの連打防止用キャッシュ(モジュールスコープ、warmインスタンス内でのみ有効)
const oddsCache = new Map(); // nk_id -> { fetchedAtMs, payload }

function jstIsoNow() {
  const jst = new Date(Date.now() + 9 * 60 * 60 * 1000);
  return jst.toISOString().replace('Z', '+09:00');
}

// "" / "---" / 0 は取消・未発売扱いでnull
function toOddsNum(v) {
  if (v === undefined || v === null) return null;
  const s = String(v).trim();
  if (s === '' || s === '---') return null;
  const n = parseFloat(s);
  if (!Number.isFinite(n) || n === 0) return null;
  return n;
}

function toPop(v) {
  if (v === undefined || v === null) return null;
  const s = String(v).trim();
  if (s === '' || s === '---') return null;
  const n = parseInt(s, 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

// 単勝: {"01":["20.1","","6"], ...} -> {"1":{odds,pop}|null, ...}
function buildTansho(raw) {
  const out = {};
  for (const [numStr, arr] of Object.entries(raw || {})) {
    const num = String(parseInt(numStr, 10));
    const odds = toOddsNum(arr && arr[0]);
    out[num] = odds === null ? null : { odds, pop: toPop(arr && arr[2]) };
  }
  return out;
}

// 複勝: {"01":["2.8","11.4","6"], ...} -> {"1":{min,max,pop}|null, ...}
function buildFukusho(raw) {
  const out = {};
  for (const [numStr, arr] of Object.entries(raw || {})) {
    const num = String(parseInt(numStr, 10));
    const min = toOddsNum(arr && arr[0]);
    const max = toOddsNum(arr && arr[1]);
    out[num] = (min === null || max === null) ? null : { min, max, pop: toPop(arr && arr[2]) };
  }
  return out;
}

export default async function handler(req, res) {
  const { searchParams } = new URL(
    req.url.startsWith('http') ? req.url : `http://localhost${req.url}`
  );
  const nk_id = searchParams.get('nk_id') || '';

  if (!/^\d{12}$/.test(nk_id)) {
    res.status(400).json({ error: 'invalid nk_id' });
    return;
  }

  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Access-Control-Allow-Origin', '*');

  const cached = oddsCache.get(nk_id);
  if (cached && (Date.now() - cached.fetchedAtMs) < CACHE_TTL_MS) {
    res.status(200).json(cached.payload);
    return;
  }

  const url = `https://race.netkeiba.com/api/api_get_jra_odds.html?race_id=${nk_id}&type=1&action=update`;
  const hdrs = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': `https://race.netkeiba.com/odds/index.html?race_id=${nk_id}`,
    'Accept': 'application/json, */*',
  };

  let data;
  try {
    const r = await fetch(url, { headers: hdrs, signal: AbortSignal.timeout(8000) });
    if (!r.ok) {
      res.status(502).json({ error: 'upstream', detail: `HTTP ${r.status}` });
      return;
    }
    data = await r.json();
  } catch (e) {
    res.status(502).json({ error: 'upstream', detail: String((e && e.message) || e) });
    return;
  }

  const status = data && data.status;
  if (status !== 'before' && status !== 'middle' && status !== 'result') {
    // 12桁形式は正しいがnetkeiba側に該当レースが無い等、想定外status(例: "NG")。
    // JSON自体は正常応答だが本APIの契約(before/middle/result)を満たさないため、
    // 接続失敗系と同じ502で扱う。
    res.status(502).json({ error: 'upstream', detail: (data && data.reason) || `unexpected status: ${status}` });
    return;
  }

  const d = (data && data.data) || {};
  const oddsRaw = d.odds || {};
  const payload = {
    nk_id,
    status,
    official_datetime: d.official_datetime || null,
    fetched_at: jstIsoNow(),
    tansho: buildTansho(oddsRaw['1']),
    fukusho: buildFukusho(oddsRaw['2']),
    source: 'netkeiba',
  };

  // キャッシュ肥大化防止の簡易掃除(多数のnk_idを長時間warmインスタンスで扱うケース対策)
  if (oddsCache.size > 200) {
    const now = Date.now();
    for (const [k, v] of oddsCache) {
      if (now - v.fetchedAtMs >= CACHE_TTL_MS) oddsCache.delete(k);
    }
  }
  oddsCache.set(nk_id, { fetchedAtMs: Date.now(), payload });

  res.status(200).json(payload);
}

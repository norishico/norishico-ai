import iconv from 'iconv-lite';

function key2(a, b) {
  const [x, y] = [a, b].map(Number).sort((p, q) => p - q);
  return `${String(x).padStart(2,'0')}${String(y).padStart(2,'0')}`;
}

async function fetchNK(url, headers) {
  try {
    const r = await fetch(url, { headers });
    if (!r.ok) return null;
    return await r.json();
  } catch(e) { return null; }
}

/**
 * SP版結果ページ(UTF-8)を解析
 * - tr.Umaren / tr.Wide で馬連・ワイドを取得
 * - 着順は li>span パターンではなく td テキストから
 */
function parseSpResultPage(html) {
  const out = {
    actual_track: null,
    result_1st: null, result_2nd: null, result_3rd: null,
    wide_payouts: {},
    umaren_payout: 0,
    umaren_uma1: null, umaren_uma2: null,
  };

  // ── 馬場状態 (SP版: Item04クラスに1文字で格納) ──
  const item04M = html.match(/class="Item04"[^>]*>([^<]+)<\/span>/);
  if (item04M) {
    const c = item04M[1].trim();
    if (c === '稍') out.actual_track = '稍重';
    else if (c === '不') out.actual_track = '不良';
    else if (c === '重') out.actual_track = '重';
    else if (c === '良') out.actual_track = '良';
  }

  // ── 馬連 (tr.Umaren) ──
  const umarenM = html.match(/<tr[^>]*class="[^"]*Umaren[^"]*"[^>]*>([\s\S]*?)<\/tr>/i);
  if (umarenM) {
    const horseNums = [...umarenM[0].matchAll(/<li><span>(\d{1,2})<\/span><\/li>/g)]
      .map(m => parseInt(m[1])).filter(n => n >= 1 && n <= 18);
    const payM = umarenM[0].match(/([\d,]+)円/);
    if (horseNums.length >= 2 && payM) {
      out.umaren_uma1 = horseNums[0];
      out.umaren_uma2 = horseNums[1];
      out.umaren_payout = parseInt(payM[1].replace(/,/g, ''));
    }
  }

  // ── ワイド (tr.Wide) ──
  const wideM = html.match(/<tr[^>]*class="[^"]*Wide[^"]*"[^>]*>([\s\S]*?)<\/tr>/i);
  if (wideM) {
    const horseNums = [...wideM[0].matchAll(/<li><span>(\d{1,2})<\/span><\/li>/g)]
      .map(m => parseInt(m[1])).filter(n => n >= 1 && n <= 18);
    const payouts = [...wideM[0].matchAll(/([\d,]+)円/g)]
      .map(m => parseInt(m[1].replace(/,/g, '')));
    for (let k = 0; k < 3; k++) {
      const h1 = horseNums[k * 2], h2 = horseNums[k * 2 + 1];
      if (h1 && h2 && payouts[k]) {
        out.wide_payouts[key2(h1, h2)] = payouts[k];
      }
    }
  }

  // ── 着順: tr.HorseList or generic tr パターン ──
  // SP page: <td>1着</td> <td>枠</td> <td>馬番</td>
  const trRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
  let tm;
  const found = {};
  while ((tm = trRe.exec(html)) !== null) {
    const cells = [...tm[0].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)]
      .map(m => m[1].replace(/<[^>]+>/g, '').replace(/\s+/g, '').trim());
    if (cells.length < 3) continue;
    const finM = cells[0].match(/^([123])着?$/);
    if (!finM) continue;
    const umaM = cells[2].match(/^(\d{1,2})$/);
    if (!umaM) continue;
    const uma = parseInt(umaM[1]);
    if (uma < 1 || uma > 18) continue;
    found[parseInt(finM[1])] = uma;
  }
  if (found[1] && found[2] && found[3]) {
    out.result_1st = found[1];
    out.result_2nd = found[2];
    out.result_3rd = found[3];
  }

  return out;
}

/**
 * PC版結果ページ(EUC-JP)を解析（フォールバック）
 */
function parsePcResultPage(html) {
  const out = {
    actual_track: null,
    result_1st: null, result_2nd: null, result_3rd: null,
    wide_payouts: {},
    umaren_payout: 0,
    umaren_uma1: null, umaren_uma2: null,
  };

  // 馬場状態
  outer:
  for (const cls of ['Item04', 'RaceData02', 'RaceData01']) {
    const attrRe = new RegExp(`class=["'][^"'\\n]*\\b${cls}\\b[^"'\\n]*["'][^>]*>`, 'gi');
    let m;
    while ((m = attrRe.exec(html)) !== null) {
      const end = m.index + m[0].length;
      const snippet = html.slice(end, end + 300).replace(/<[^>]+>/g, '').replace(/\s+/g, ' ');
      for (const tc of ['稍重', '不良', '重', '良']) {
        if (snippet.includes(tc)) { out.actual_track = tc; break outer; }
      }
    }
  }
  if (!out.actual_track) {
    for (const tc of ['稍重', '不良', '重']) {
      if (html.includes(tc)) { out.actual_track = tc; break; }
    }
    if (!out.actual_track && html.includes('良')) out.actual_track = '良';
  }

  // 払戻テーブル（ワイド・馬連）
  const payTableRe = /<table[^>]*>([\s\S]*?)<\/table>/gi;
  let tm;
  payTableRe.lastIndex = 0;
  while ((tm = payTableRe.exec(html)) !== null) {
    const tText = tm[0].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
    if (!tText.includes('ワイド') && !tText.includes('馬連')) continue;

    const cellsAll = [...tm[0].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)]
      .map(c => c[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());

    let wideMode = false;
    let widePairs = [];
    let widePayouts = [];

    for (let i = 0; i < cellsAll.length; i++) {
      const cell = cellsAll[i];
      if (cell.includes('ワイド')) { wideMode = true; }
      if (cell.includes('馬連') && !wideMode) {
        const umMat = tText.match(/馬連.{0,5}?(\d{1,2}).{0,5}?(\d{1,2}).{0,10}?([\d,]+)円/);
        if (umMat) {
          out.umaren_uma1 = parseInt(umMat[1]);
          out.umaren_uma2 = parseInt(umMat[2]);
          out.umaren_payout = parseInt(umMat[3].replace(/,/g, ''));
        }
      }
      if (!wideMode) continue;
      if (cell.includes('馬単') || cell.includes('3連')) { wideMode = false; break; }

      const pairM = cell.match(/^(\d{1,2})\s+(\d{1,2})$/);
      if (pairM) widePairs.push([parseInt(pairM[1]), parseInt(pairM[2])]);

      const payM = cell.match(/^([\d,]+)円$/);
      if (payM) widePayouts.push(parseInt(payM[1].replace(/,/g, '')));
    }

    for (let k = 0; k < Math.min(widePairs.length, widePayouts.length); k++) {
      out.wide_payouts[key2(...widePairs[k])] = widePayouts[k];
    }
    break;
  }

  // 着順
  const tableRe = /<table[^>]*>([\s\S]*?)<\/table>/gi;
  tableRe.lastIndex = 0;
  while ((tm = tableRe.exec(html)) !== null) {
    const rowRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
    let rm;
    const found = {};
    while ((rm = rowRe.exec(tm[0])) !== null) {
      const cells = [...rm[0].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)]
        .map(m => m[1].replace(/<[^>]+>/g, '').replace(/\s+/g, '').trim());
      if (cells.length < 3) continue;
      const finM = cells[0].match(/^([123])着?$/);
      if (!finM) continue;
      const umaM = cells[2].match(/^(\d{1,2})$/);
      if (!umaM) continue;
      const uma = parseInt(umaM[1]);
      if (uma < 1 || uma > 18) continue;
      found[parseInt(finM[1])] = uma;
    }
    if (found[1] && found[2] && found[3]) {
      out.result_1st = found[1];
      out.result_2nd = found[2];
      out.result_3rd = found[3];
      break;
    }
  }

  return out;
}

export default async function handler(req, res) {
  const { searchParams } = new URL(
    req.url.startsWith('http') ? req.url : `http://localhost${req.url}`
  );
  const nk_id = searchParams.get('nk_id');
  const jiku  = parseInt(searchParams.get('jiku') || '0');
  const aite  = (searchParams.get('aite') || '').split(',').map(Number).filter(Boolean);

  if (!nk_id || !jiku || aite.length < 3) {
    res.status(400).json({ error: 'invalid params' });
    return;
  }

  const base = `https://race.netkeiba.com/api/api_get_jra_odds.html?race_id=${nk_id}&action=update`;
  const hdrs = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': `https://race.netkeiba.com/race/shutuba.html?race_id=${nk_id}`,
    'Accept': 'application/json, */*',
  };

  const result = {
    status: 'before',
    umaren: 0, wide: [0, 0, 0], total_payout: 0,
    result_1st: null, result_2nd: null, result_3rd: null,
    actual_track: null,
  };

  // ── Step1: ステータス確認（単勝 type=1）──
  const d1 = await fetchNK(base + '&type=1', hdrs);
  const rawStatus = d1?.status || 'before';
  result.status = rawStatus === 'result' ? 'final' : rawStatus;

  if (result.status !== 'final') {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.json(result);
    return;
  }

  // ── Step2: 結果HTML取得 (SP版 UTF-8 優先 → PC版 EUC-JP フォールバック) ──
  let parsed = null;

  // SP版(UTF-8)を試す
  try {
    const spHdrs = {
      'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
      'Referer': 'https://race.sp.netkeiba.com/',
    };
    const rSp = await fetch(
      `https://race.sp.netkeiba.com/?pid=race_result&race_id=${nk_id}`,
      { headers: spHdrs }
    );
    if (rSp.ok) {
      const html = await rSp.text();
      parsed = parseSpResultPage(html);
    }
  } catch(e) { /* fallthrough */ }

  // PC版(EUC-JP)フォールバック
  if (!parsed || (!parsed.result_1st && !parsed.umaren_payout)) {
    try {
      const rPc = await fetch(
        `https://race.netkeiba.com/race/result.html?race_id=${nk_id}`,
        { headers: { ...hdrs, 'Accept': 'text/html' } }
      );
      if (rPc.ok) {
        const buf = await rPc.arrayBuffer();
        const html = iconv.decode(Buffer.from(buf), 'euc-jp');
        parsed = parsePcResultPage(html);
      }
    } catch(e) { /* ignore */ }
  }

  // パース結果を反映
  if (parsed) {
    if (parsed.actual_track) result.actual_track = parsed.actual_track;
    if (parsed.result_1st)   result.result_1st   = parsed.result_1st;
    if (parsed.result_2nd)   result.result_2nd   = parsed.result_2nd;
    if (parsed.result_3rd)   result.result_3rd   = parsed.result_3rd;
  }

  // ── Step3: ワイド払戻（HTML優先 → type=4 API フォールバック）──
  // ※ netkeiba API: type=4=馬連, type=5=ワイド(min/max範囲のみ)
  const widePayoutsFromHtml = parsed?.wide_payouts || {};

  if (Object.keys(widePayoutsFromHtml).length > 0) {
    result.wide = aite.map(an => widePayoutsFromHtml[key2(jiku, an)] || 0);
  } else {
    // フォールバック: type=5 API (min値を使用 — 近似)
    const d5w = await fetchNK(base + '&type=5', hdrs);
    if (d5w) {
      const om = d5w?.data?.odds?.['5'] || {};
      result.wide = aite.map(an => {
        const k = key2(jiku, an);
        const v = om[k];
        if (!v) return 0;
        const vals = (Array.isArray(v) ? v : [v]).filter(x => x && String(x) !== '0');
        return vals.length ? Math.round(parseFloat(vals[0]) * 100) : 0;
      });
    }
  }

  // ── Step4: 馬連払戻（HTML優先 → type=4 API フォールバック）──
  // ※ netkeiba API: type=4 = 馬連
  if (parsed?.umaren_payout > 0) {
    result.umaren = parsed.umaren_payout;
  } else {
    const d4m = await fetchNK(base + '&type=4', hdrs);
    if (d4m) {
      const om = d4m?.data?.odds?.['4'] || {};
      const k = key2(jiku, aite[0]);
      const v = om[k];
      if (v) {
        const vals = (Array.isArray(v) ? v : [v]).filter(x => x && String(x) !== '0');
        if (vals.length) result.umaren = Math.round(parseFloat(vals[0]) * 100);
      }
    }
  }

  // ── Step5: 当たり判定 ──
  if (result.result_1st && result.result_2nd && result.result_3rd) {
    const t3 = new Set([result.result_1st, result.result_2nd, result.result_3rd]);
    const t2 = new Set([result.result_1st, result.result_2nd]);

    // 馬連: HTMLから取得した場合はumaren_uma1/uma2で当たり判定
    // APIから取得した場合は jiku × aite[0] で判定
    if (parsed?.umaren_uma1) {
      const umarenKey = key2(parsed.umaren_uma1, parsed.umaren_uma2);
      const betKey = key2(jiku, aite[0]);
      if (umarenKey !== betKey) result.umaren = 0;
    } else {
      if (!(result.umaren > 0 && t2.has(jiku) && t2.has(aite[0]))) {
        result.umaren = 0;
      }
    }

    // ワイド: 軸とaite[i]が両方3着以内
    result.wide = aite.map((an, i) => {
      const w = result.wide[i] || 0;
      return (w > 0 && t3.has(jiku) && t3.has(an)) ? w : 0;
    });
  }

  result.total_payout = (result.umaren || 0) + result.wide.reduce((s, w) => s + w, 0);

  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.json(result);
}

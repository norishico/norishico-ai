// AYO keiba Widget — Scriptable用
// widget_data.jsonは既にサーバー側(generate_mc_record.py)で「注目レース」基準
// (MC123信頼度tier=高 かつ 展開予想精度tier=高)に絞り込み済み(2026-09-07〜)。
// このスクリプト側では追加の絞り込みは行わず、発走時刻順の抽出のみ行う。
// 設定: OPEN_URL を自分のPCのローカルIPに変更してください
// 例: http://192.168.1.10:8765/
const OPEN_URL = "https://mckeibapublic.vercel.app/";
const DATA_URL = "https://mckeibapublic.vercel.app/widget_data.json";

// --- 色定義 ---
const C_BG     = new Color("#0f0f1a");
const C_CORAL  = new Color("#E8865A");
const C_PURPLE = new Color("#8B5CF6");
const C_GREEN  = new Color("#4ADE80");
const C_DIM    = new Color("#64748b");
const C_TEXT   = new Color("#e2e8f0");
const C_SUB    = new Color("#94a3b8");

// --- データ取得 ---
async function fetchData() {
  try {
    const req = new Request(DATA_URL);
    req.timeoutInterval = 8;
    return await req.loadJSON();
  } catch (e) {
    return null;
  }
}

// --- 直近レースを抽出 (発走10分前まで含む) ---
function getUpcomingRaces(races, maxCount, dataDate) {
  const now = new Date();
  const todayStr = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");

  // データが今日のものでなければ空を返す
  if (dataDate && dataDate !== todayStr) return [];

  return races
    .filter((r) => {
      const [h, m] = r.start_time.split(":").map(Number);
      const t = new Date(now);
      t.setHours(h, m, 0, 0);
      // 発走10分前以降のレースを対象
      return t.getTime() >= now.getTime() - 10 * 60 * 1000;
    })
    .sort((a, b) => a.start_time.localeCompare(b.start_time))
    .slice(0, maxCount);
}

// --- 残り時間テキスト ---
function minutesUntil(startTime) {
  const now = new Date();
  const [h, m] = startTime.split(":").map(Number);
  const t = new Date(now);
  t.setHours(h, m, 0, 0);
  const diff = Math.round((t - now) / 60000);
  if (diff < 0) return "発走済";
  if (diff === 0) return "まもなく";
  if (diff > 60) return "";
  return `${diff}分後`;
}

// --- Medium ウィジェット (最大3レース) ---
function buildMedium(widget, races, date) {
  widget.backgroundColor = C_BG;
  widget.url = OPEN_URL;

  // ヘッダー
  const header = widget.addStack();
  header.layoutHorizontally();
  const icon = header.addText("🎲");
  icon.font = Font.boldSystemFont(13);
  header.addSpacer(4);
  const title = header.addText("AYO keiba");
  title.textColor = C_CORAL;
  title.font = Font.boldSystemFont(13);
  header.addSpacer();
  const dateText = header.addText(date ? date.slice(5).replace("-", "/") : "--/--");
  dateText.textColor = C_DIM;
  dateText.font = Font.systemFont(11);

  widget.addSpacer(6);

  if (!races || races.length === 0) {
    const empty = widget.addText("本日は注目レースなし");
    empty.textColor = C_DIM;
    empty.font = Font.systemFont(12);
    widget.addSpacer();
    return;
  }

  for (const r of races) {
    const row = widget.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();

    // 発走時間バッジ
    const timeBadge = row.addStack();
    timeBadge.backgroundColor = new Color("#1e293b");
    timeBadge.cornerRadius = 5;
    timeBadge.setPadding(2, 6, 2, 6);
    const timeText = timeBadge.addText(r.start_time);
    timeText.textColor = C_CORAL;
    timeText.font = Font.boldSystemFont(12);

    row.addSpacer(6);

    // 会場 + R番
    const venueText = row.addText(`${r.venue}${r.rno}R`);
    venueText.textColor = C_TEXT;
    venueText.font = Font.boldSystemFont(13);

    row.addSpacer();

    // 残り時間
    const until = row.addText(minutesUntil(r.start_time));
    until.textColor = C_GREEN;
    until.font = Font.systemFont(11);

    widget.addSpacer(5);
  }
  widget.addSpacer();
}

// --- Small ウィジェット (次の1レース) ---
function buildSmall(widget, race, date) {
  widget.backgroundColor = C_BG;
  widget.url = OPEN_URL;

  const icon = widget.addText("🎲 AYO keiba");
  icon.textColor = C_CORAL;
  icon.font = Font.boldSystemFont(11);

  widget.addSpacer(8);

  if (!race) {
    const t = widget.addText("本日は\n注目レースなし");
    t.textColor = C_DIM;
    t.font = Font.systemFont(13);
    widget.addSpacer();
    return;
  }

  // 発走時間 (大きく)
  const startText = widget.addText(race.start_time);
  startText.textColor = C_CORAL;
  startText.font = Font.boldSystemFont(28);

  widget.addSpacer(2);

  // 会場+R番
  const venueText = widget.addText(`${race.venue} ${race.rno}R`);
  venueText.textColor = C_TEXT;
  venueText.font = Font.boldSystemFont(16);

  widget.addSpacer(4);

  widget.addSpacer();

  // 残り時間
  const until = widget.addText(minutesUntil(race.start_time));
  until.textColor = C_GREEN;
  until.font = Font.systemFont(11);
}

// --- メイン ---
const data = await fetchData();
const widget = new ListWidget();
widget.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000); // 5分ごと更新

const family = config.widgetFamily;

// --- ロック画面ウィジェット (accessoryRectangular / accessoryInline) ---
function buildAccessoryRect(widget, races) {
  widget.url = OPEN_URL;
  if (!races || races.length === 0) {
    const t = widget.addText("本日は注目レースなし");
    t.font = Font.systemFont(12);
    return;
  }
  for (const r of races.slice(0, 3)) {
    const row = widget.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();

    const timeCol = row.addStack();
    timeCol.size = new Size(38, 0);
    const tTime = timeCol.addText(r.start_time);
    tTime.font = Font.boldSystemFont(11);

    row.addSpacer(4);

    const venueCol = row.addStack();
    venueCol.size = new Size(56, 0);
    const tVenue = venueCol.addText(r.venue + r.rno + "R");
    tVenue.font = Font.boldSystemFont(11);

    row.addSpacer();
    const tRight = row.addText(minutesUntil(r.start_time));
    tRight.font = Font.systemFont(11);
  }
}
function buildAccessoryInline(widget, race) {
  widget.url = OPEN_URL;
  if (!race) {
    widget.addText("🎲 本日は注目レースなし");
    return;
  }
  const _u = minutesUntil(race.start_time);
  widget.addText(`🎲 ${race.start_time} ${race.venue}${race.rno}R${_u ? '  ' + _u : ''}`);
}

// --- 描画分岐 ---
if (!data) {
  widget.backgroundColor = C_BG;
  const t = widget.addText("データ取得失敗");
  t.textColor = C_DIM;
  t.font = Font.systemFont(12);
} else {
  const upcoming = getUpcomingRaces(data.races || [], 3, data.date);
  if (family === "accessoryRectangular") {
    buildAccessoryRect(widget, upcoming);
  } else if (family === "accessoryInline") {
    buildAccessoryInline(widget, upcoming[0] || null);
  } else if (family === "small") {
    buildSmall(widget, upcoming[0] || null, data.date);
  } else {
    buildMedium(widget, upcoming, data.date);
  }
}

Script.setWidget(widget);
if (!config.runsInWidget) widget.presentMedium();
Script.complete();

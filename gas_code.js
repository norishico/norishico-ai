function doPost(e) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var data = JSON.parse(e.postData.contents);
  var type = data.type || 'results';

  if (type === 'results') {
    return handleResults(ss, data);
  } else if (type === 'commands') {
    return handleCommands(ss, data);
  }

  return ContentService.createTextOutput(JSON.stringify({status: 'error', message: 'unknown type'}))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleResults(ss, data) {
  var sheet = ss.getSheetByName('結果') || ss.getActiveSheet();
  if (sheet.getName() !== '結果') sheet.setName('結果');

  if (sheet.getLastRow() === 0) {
    var headers = [
      '日付', '会場', 'R', 'レース名', 'グレード', '買い目',
      '◎馬名', '◎着順', '◎オッズ', '○馬名', '○着順',
      '1着馬', '1着オッズ', '馬場', '投資', '回収', '損益', '累計損益'
    ];
    sheet.appendRow(headers);
    sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold').setBackground('#E8D5C0');
    sheet.setFrozenRows(1);
  }

  var rows = data.rows;
  var cumulative = 0;
  var lastRow = sheet.getLastRow();
  if (lastRow > 1) {
    cumulative = Number(sheet.getRange(lastRow, 18).getValue()) || 0;
  }

  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    cumulative += r.profit;
    sheet.appendRow([
      r.date, r.venue, r.race_num, r.race_name, r.grade, r.buy_type,
      r.honmei, r.honmei_finish, r.honmei_odds,
      r.ni || '', r.ni_finish || '',
      r.winner, r.winner_odds, r.track_cond,
      r.cost, r.ret, r.profit, cumulative
    ]);

    var newRow = sheet.getLastRow();
    var pnlCell = sheet.getRange(newRow, 17);
    if (r.profit > 0) pnlCell.setFontColor('#2E7D32');
    else if (r.profit < 0) pnlCell.setFontColor('#C62828');

    var cumCell = sheet.getRange(newRow, 18);
    if (cumulative > 0) cumCell.setFontColor('#2E7D32');
    else if (cumulative < 0) cumCell.setFontColor('#C62828');
  }

  updateSummary(ss, sheet);

  return ContentService.createTextOutput(JSON.stringify({status: 'ok', rows: rows.length}))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleCommands(ss, data) {
  var sheet = ss.getSheetByName('コマンド一覧');
  if (!sheet) {
    sheet = ss.insertSheet('コマンド一覧');
  }
  sheet.clear();

  var headers = ['カテゴリ', 'コマンド / キーワード', '説明', 'タイミング'];
  sheet.appendRow(headers);
  sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold').setBackground('#E8D5C0');
  sheet.setFrozenRows(1);

  var rows = data.rows;
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    sheet.appendRow([r.category, r.command, r.description, r.timing]);
  }

  sheet.autoResizeColumns(1, 4);
  sheet.setColumnWidth(2, 350);
  sheet.setColumnWidth(3, 400);

  return ContentService.createTextOutput(JSON.stringify({status: 'ok', rows: rows.length}))
    .setMimeType(ContentService.MimeType.JSON);
}

function updateSummary(ss, resultSheet) {
  var sumSheet = ss.getSheetByName('サマリー');
  if (!sumSheet) {
    sumSheet = ss.insertSheet('サマリー');
  }
  sumSheet.clear();

  sumSheet.getRange('A1').setValue('月間サマリー').setFontWeight('bold').setFontSize(14);
  sumSheet.getRange('A3:F3').setValues([['年月', 'レース数', '投資', '回収', '損益', 'ROI']]);
  sumSheet.getRange('A3:F3').setFontWeight('bold').setBackground('#E8D5C0');

  var lastRow = resultSheet.getLastRow();
  if (lastRow <= 1) return;

  var data = resultSheet.getRange(2, 1, lastRow - 1, 18).getValues();
  var monthly = {};
  var yearly = {};

  for (var i = 0; i < data.length; i++) {
    var date = String(data[i][0]);
    var cost = Number(data[i][14]) || 0;
    var ret = Number(data[i][15]) || 0;
    var profit = Number(data[i][16]) || 0;

    var ym = date.substring(0, 7);
    var y = date.substring(0, 4);

    if (!monthly[ym]) monthly[ym] = {races: 0, cost: 0, ret: 0, profit: 0};
    monthly[ym].races++;
    monthly[ym].cost += cost;
    monthly[ym].ret += ret;
    monthly[ym].profit += profit;

    if (!yearly[y]) yearly[y] = {races: 0, cost: 0, ret: 0, profit: 0};
    yearly[y].races++;
    yearly[y].cost += cost;
    yearly[y].ret += ret;
    yearly[y].profit += profit;
  }

  var row = 4;
  var months = Object.keys(monthly).sort();
  for (var m = 0; m < months.length; m++) {
    var d = monthly[months[m]];
    var roi = d.cost > 0 ? Math.round(d.ret / d.cost * 100) : 0;
    sumSheet.getRange(row, 1, 1, 6).setValues([[months[m], d.races, d.cost, d.ret, d.profit, roi + '%']]);
    var pnl = sumSheet.getRange(row, 5);
    if (d.profit > 0) pnl.setFontColor('#2E7D32');
    else if (d.profit < 0) pnl.setFontColor('#C62828');
    row++;
  }

  row += 1;
  sumSheet.getRange(row, 1).setValue('年間サマリー').setFontWeight('bold').setFontSize(14);
  row += 1;
  sumSheet.getRange(row, 1, 1, 6).setValues([['年', 'レース数', '投資', '回収', '損益', 'ROI']]);
  sumSheet.getRange(row, 1, 1, 6).setFontWeight('bold').setBackground('#E8D5C0');
  row++;

  var years = Object.keys(yearly).sort();
  for (var y = 0; y < years.length; y++) {
    var d = yearly[years[y]];
    var roi = d.cost > 0 ? Math.round(d.ret / d.cost * 100) : 0;
    sumSheet.getRange(row, 1, 1, 6).setValues([[years[y], d.races, d.cost, d.ret, d.profit, roi + '%']]);
    var pnl = sumSheet.getRange(row, 5);
    if (d.profit > 0) pnl.setFontColor('#2E7D32');
    else if (d.profit < 0) pnl.setFontColor('#C62828');
    row++;
  }

  sumSheet.autoResizeColumns(1, 6);
}

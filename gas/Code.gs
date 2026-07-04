/**
 * PDF個別メール送信システム(GAS版)
 * Google Apps Script のウェブアプリとして動作する。
 * 送信はアクセスしている先生自身のGoogleアカウントから行われる
 * (デプロイ設定「次のユーザーとして実行: ウェブアプリにアクセスしているユーザー」が前提)。
 */

var HISTORY_FILENAME = 'PDF個別メール送信システム_送信履歴';

function doGet() {
  return HtmlService.createHtmlOutputFromFile('index')
    .setTitle('PDF個別メール送信システム')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

/** 画面表示に必要な初期情報(ログイン中のアドレス・テンプレート・残り送信可能数) */
function getInitialData() {
  return {
    email: Session.getActiveUser().getEmail(),
    templates: getTemplates_(),
    remainingQuota: MailApp.getRemainingDailyQuota(),
  };
}

// ---------- テンプレート(Googleアカウントごとに保存される) ----------

function getTemplates_() {
  var raw = PropertiesService.getUserProperties().getProperty('templates');
  return raw ? JSON.parse(raw) : [];
}

function saveTemplate(tpl) {
  var list = getTemplates_().filter(function (t) { return t.name !== tpl.name; });
  list.push({
    name: String(tpl.name),
    subject: String(tpl.subject || ''),
    body: String(tpl.body || ''),
    prefix: String(tpl.prefix || ''),
    honorific: String(tpl.honorific || ''),
  });
  PropertiesService.getUserProperties().setProperty('templates', JSON.stringify(list));
  return list;
}

function deleteTemplate(name) {
  var list = getTemplates_().filter(function (t) { return t.name !== name; });
  PropertiesService.getUserProperties().setProperty('templates', JSON.stringify(list));
  return list;
}

// ---------- 送信 ----------

/**
 * 1バッチ(数名分)を送信する。ブラウザ側が分割済みPDF(Base64)を渡してくる。
 * items: [{serial, name, email, filename, pdfB64}]
 *   pdfB64 が空のときは添付なし(本文のみ)で送信する。
 * meta:  {subject, body, honorific}
 */
function sendBatch(items, meta) {
  var results = [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var r = { serial: it.serial, name: it.name, email: it.email, status: '成功', error: '' };
    try {
      var opts = {};
      if (it.pdfB64) {
        opts.attachments = [Utilities.newBlob(
          Utilities.base64Decode(it.pdfB64), 'application/pdf', it.filename)];
      }
      var body = it.name + meta.honorific + '\n\n' + meta.body;
      GmailApp.sendEmail(it.email, meta.subject, body, opts);
    } catch (e) {
      r.status = '失敗';
      r.error = String(e.message || e);
    }
    results.push(r);
    Utilities.sleep(300);
  }
  return results;
}

/** 自分宛てのテスト送信(1通) */
function testSend(item, meta) {
  var me = Session.getActiveUser().getEmail();
  try {
    var opts = {};
    if (item.pdfB64) {
      opts.attachments = [Utilities.newBlob(
        Utilities.base64Decode(item.pdfB64), 'application/pdf', item.filename)];
    }
    var body = '※これはテスト送信です。実際には各生徒(保護者)のアドレスへ送信されます。\n' +
      '※以下は 通し番号 ' + item.serial + '(' + item.name + ')さん宛ての内容の例です。\n' +
      (item.pdfB64 ? '' : '※添付なし(本文のみ)の送信です。\n') +
      '--------------------\n' +
      item.name + meta.honorific + '\n\n' + meta.body;
    GmailApp.sendEmail(me, '【テスト送信】' + meta.subject, body, opts);
    appendHistory({
      mode: 'テスト送信', subject: meta.subject,
      results: [{ serial: item.serial, name: item.name, email: me, status: '成功', error: '' }],
    });
    return { ok: true, message: '自分宛て(' + me + ')にテスト送信しました。受信内容を確認してください。' };
  } catch (e) {
    return { ok: false, message: 'テスト送信に失敗しました: ' + String(e.message || e) };
  }
}

// ---------- 送信履歴(先生ごとのGoogleスプレッドシートに記録) ----------

function historySheet_() {
  var props = PropertiesService.getUserProperties();
  var id = props.getProperty('historySheetId');
  var ss = null;
  if (id) {
    try { ss = SpreadsheetApp.openById(id); } catch (e) { ss = null; }
  }
  if (!ss) {
    ss = SpreadsheetApp.create(HISTORY_FILENAME);
    ss.getSheets()[0].appendRow(
      ['日時', '種別', '件名', '通し番号', '生徒氏名', 'メールアドレス', '結果', 'エラー']);
    props.setProperty('historySheetId', ss.getId());
  }
  return ss.getSheets()[0];
}

function appendHistory(record) {
  var sh = historySheet_();
  var now = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd HH:mm:ss');
  var rows = (record.results || []).map(function (r) {
    return [now, record.mode, record.subject, r.serial, r.name, r.email, r.status, r.error];
  });
  if (rows.length) {
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, 8).setValues(rows);
  }
  return { url: sh.getParent().getUrl() };
}

function getHistory() {
  var sh = historySheet_();
  var last = sh.getLastRow();
  var n = Math.min(500, last - 1);
  var rows = n > 0 ? sh.getRange(last - n + 1, 1, n, 8).getValues() : [];
  // Dateオブジェクトはブラウザへ返せないため、全セルを文字列化する
  rows = rows.map(function (row) { return row.map(function (c) { return String(c); }); });
  return { rows: rows.reverse(), url: sh.getParent().getUrl() };
}

# -*- coding: utf-8 -*-
"""
PDF個別メール送信システム
CSVの名簿とPDFを読み込み、通し番号ごとにPDFを分割して
各生徒(保護者)へGmail経由で個別送信するWebアプリ。
"""
import csv
import io
import json
import os
import re
import smtplib
import socket
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from email.message import EmailMessage

from flask import Flask, jsonify, render_template, request, send_file
from pypdf import PdfReader, PdfWriter

PORT = 8787
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SEND_INTERVAL_SEC = 1.0
REQUIRED_HEADERS = ["通し番号", "クラス", "出席番号", "生徒氏名", "メールアドレス"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEMPLATES_FILE = os.path.join(DATA_DIR, "templates.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

# アップロードされたCSV+PDFのセット(ジョブ)をメモリに保持
JOBS = {}
JOBS_ORDER = []
MAX_JOBS = 5
jobs_lock = threading.Lock()

# 送信の進行状況(サーバー全体で同時に1件のみ)
send_state = {
    "running": False,
    "cancel": False,
    "mode": "",
    "sender": "",
    "total": 0,
    "done": 0,
    "results": [],
    "message": "",
}
state_lock = threading.Lock()
file_lock = threading.Lock()


# ---------- 共通ユーティリティ ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with file_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def decode_csv_bytes(raw):
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSVファイルの文字コードを判別できませんでした。")


def is_valid_email(addr):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr))


def make_filename(prefix, name):
    base = "{}_{}.pdf".format(prefix, re.sub(r"\s+", "", name))
    return re.sub(r'[\\/:*?"<>|]', "", base)


def make_greeting(name, honorific):
    return "{}{}".format(name, honorific)


# ---------- CSV / PDF の読み込み ----------

def parse_csv(text):
    """CSVを解析して生徒リストとエラー/警告を返す。"""
    students, errors, warnings = [], [], []
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], ["CSVファイルが空です。"], []

    header = [h.strip() for h in rows[0]]
    idx = {}
    for col in REQUIRED_HEADERS:
        # 「通し番号(〜を自分で入力)」のような説明付きヘッダーも許容する
        i = next((j for j, h in enumerate(header) if h == col), None)
        if i is None:
            i = next((j for j, h in enumerate(header) if h.startswith(col)), None)
        if i is None:
            errors.append("CSVの1行目に「{}」の列が見つかりません。".format(col))
        else:
            idx[col] = i
    if errors:
        return [], errors, []

    seen_serials = set()
    for line_no, row in enumerate(rows[1:], start=2):
        def cell(col):
            i = idx[col]
            return row[i].strip() if i < len(row) else ""

        serial_raw = cell("通し番号")
        try:
            serial = int(serial_raw)
        except ValueError:
            errors.append("{}行目: 通し番号「{}」が数字ではありません。".format(line_no, serial_raw))
            continue
        if serial in seen_serials:
            errors.append("{}行目: 通し番号 {} が重複しています。".format(line_no, serial))
            continue
        seen_serials.add(serial)

        name = cell("生徒氏名")
        email = cell("メールアドレス")
        skip_reason = ""
        if not email:
            skip_reason = "メールアドレスが空欄"
        elif not is_valid_email(email):
            skip_reason = "メールアドレスの形式が不正"
        if skip_reason:
            warnings.append("通し番号 {}({}): {} のためスキップされます。".format(
                serial, name or "氏名なし", skip_reason))

        students.append({
            "serial": serial,
            "class": cell("クラス"),
            "number": cell("出席番号"),
            "name": name,
            "email": email,
            "skip": bool(skip_reason),
            "skip_reason": skip_reason,
        })

    if not students:
        errors.append("CSVに生徒のデータ行がありません。")
    return students, errors, warnings


def extract_pages(pdf_bytes, start, end):
    """PDFの start〜end ページ(1始まり・両端含む)を抜き出したバイト列を返す。"""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for i in range(start - 1, end):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------- メール送信 ----------

def build_message(sender, to, subject, body, pdf_bytes, filename):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=filename)
    return msg


def smtp_login(sender, password):
    smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    smtp.login(sender, password)
    return smtp


AUTH_ERROR_MSG = ("Gmailへのログインに失敗しました。メールアドレスと"
                  "アプリパスワード(16文字)が正しいか確認してください。")


def append_history(record):
    history = load_json(HISTORY_FILE, [])
    history.append(record)
    save_json(HISTORY_FILE, history[-200:])


def send_worker(job, params, targets):
    """バックグラウンドで1通ずつ順次送信する。"""
    sender = params["sender"]
    password = params["password"]
    smtp = None
    try:
        for st in targets:
            with state_lock:
                if send_state["cancel"]:
                    send_state["message"] = "中断されました。"
                    break
                send_state["message"] = "{} へ送信中…".format(st["name"] or st["email"])

            result = {"serial": st["serial"], "name": st["name"],
                      "email": st["email"], "status": "", "error": ""}
            try:
                pdf_part = extract_pages(job["pdf"], st["page_start"], st["page_end"])
                body = "{}\n\n{}".format(
                    make_greeting(st["name"], params["honorific"]), params["body"])
                msg = build_message(sender, st["email"], params["subject"], body,
                                    pdf_part, make_filename(params["prefix"], st["name"]))
                last_err = None
                for attempt in range(2):
                    try:
                        if smtp is None:
                            smtp = smtp_login(sender, password)
                        smtp.send_message(msg)
                        last_err = None
                        break
                    except smtplib.SMTPAuthenticationError:
                        raise
                    except Exception as e:  # 接続切れ等は1回だけ再接続して再試行
                        last_err = e
                        try:
                            if smtp is not None:
                                smtp.quit()
                        except Exception:
                            pass
                        smtp = None
                if last_err is not None:
                    raise last_err
                result["status"] = "成功"
            except smtplib.SMTPAuthenticationError:
                result["status"] = "失敗"
                result["error"] = AUTH_ERROR_MSG
                with state_lock:
                    send_state["results"].append(result)
                    send_state["done"] += 1
                    send_state["message"] = AUTH_ERROR_MSG
                break
            except Exception as e:
                result["status"] = "失敗"
                result["error"] = str(e)

            with state_lock:
                send_state["results"].append(result)
                send_state["done"] += 1
            time.sleep(SEND_INTERVAL_SEC)
    finally:
        try:
            if smtp is not None:
                smtp.quit()
        except Exception:
            pass
        with state_lock:
            results = list(send_state["results"])
            mode = send_state["mode"]
            if not send_state["message"].startswith(("中断", "Gmail")):
                send_state["message"] = "送信が完了しました。"
            send_state["running"] = False
        ok = sum(1 for r in results if r["status"] == "成功")
        append_history({
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sender": sender,
            "mode": mode,
            "subject": params["subject"],
            "ok": ok,
            "fail": len(results) - ok,
            "results": results,
        })


# ---------- ルーティング ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    csv_file = request.files.get("csv")
    pdf_file = request.files.get("pdf")
    pages_per_raw = request.form.get("pages_per", "").strip()
    errors, warnings = [], []

    if not csv_file:
        errors.append("CSVファイルを選択してください。")
    if not pdf_file:
        errors.append("PDFファイルを選択してください。")
    try:
        pages_per = int(pages_per_raw)
        if pages_per < 1:
            raise ValueError
    except ValueError:
        pages_per = 0
        errors.append("「1人あたりのページ数」は1以上の数字で入力してください。")
    if errors:
        return jsonify({"valid": False, "errors": errors, "warnings": [], "students": []})

    try:
        students, csv_errors, warnings = parse_csv(decode_csv_bytes(csv_file.read()))
        errors.extend(csv_errors)
    except ValueError as e:
        return jsonify({"valid": False, "errors": [str(e)], "warnings": [], "students": []})

    pdf_bytes = pdf_file.read()
    try:
        total_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return jsonify({"valid": False, "warnings": warnings, "students": [],
                        "errors": ["PDFファイルを読み込めませんでした。ファイルが壊れていないか確認してください。"]})

    if students:
        expected = len(students) * pages_per
        if total_pages != expected:
            errors.append(
                "PDFのページ数({}ページ)が「人数 {}名 × {}ページ = {}ページ」と一致しません。"
                "送信できません。".format(total_pages, len(students), pages_per, expected))
        for st in students:
            st["page_start"] = (st["serial"] - 1) * pages_per + 1
            st["page_end"] = st["serial"] * pages_per
            if st["page_end"] > total_pages:
                errors.append("通し番号 {}({}): 割り当てページ({}〜{})がPDFの範囲外です。".format(
                    st["serial"], st["name"] or "氏名なし", st["page_start"], st["page_end"]))

    valid = not errors
    job_id = ""
    if valid:
        job_id = uuid.uuid4().hex
        with jobs_lock:
            JOBS[job_id] = {"students": students, "pdf": pdf_bytes,
                            "pages_per": pages_per}
            JOBS_ORDER.append(job_id)
            while len(JOBS_ORDER) > MAX_JOBS:
                JOBS.pop(JOBS_ORDER.pop(0), None)

    return jsonify({
        "valid": valid,
        "job_id": job_id,
        "errors": errors,
        "warnings": warnings,
        "total_pages": total_pages,
        "students": [{k: v for k, v in st.items() if k != "pdf"} for st in students],
    })


def get_send_params(data, require_password=True):
    """送信系APIの共通パラメータを検証して返す。エラー時は (None, メッセージ)。"""
    job = JOBS.get(data.get("job_id", ""))
    if job is None:
        return None, "ファイルの情報が見つかりません。もう一度「読み込んでチェック」からやり直してください。"
    params = {
        "sender": data.get("sender", "").strip(),
        "password": data.get("password", "").replace(" ", ""),
        "subject": data.get("subject", "").strip(),
        "body": data.get("body", ""),
        "honorific": data.get("honorific", "").strip(),
        "prefix": data.get("prefix", "").strip(),
    }
    if not is_valid_email(params["sender"]):
        return None, "送信元メールアドレスを正しく入力してください。"
    if require_password and not params["password"]:
        return None, "アプリパスワードを入力してください。"
    if not params["subject"]:
        return None, "件名を入力してください。"
    if not params["body"].strip():
        return None, "本文を入力してください。"
    if params["honorific"] not in ("君", "保護者様"):
        return None, "宛名(君/保護者様)を選択してください。"
    if not params["prefix"]:
        return None, "添付ファイル名の頭(例: 面談案内)を入力してください。"
    return (job, params), ""


@app.route("/api/test_send", methods=["POST"])
def api_test_send():
    data = request.get_json(force=True)
    checked, err = get_send_params(data)
    if err:
        return jsonify({"ok": False, "message": err})
    job, params = checked

    with state_lock:
        if send_state["running"]:
            return jsonify({"ok": False, "message": "現在ほかの送信が実行中です。完了までお待ちください。"})

    sample = next((s for s in job["students"] if not s["skip"]), None)
    if sample is None:
        return jsonify({"ok": False, "message": "送信可能な生徒(メールアドレスあり)が1人もいません。"})

    try:
        pdf_part = extract_pages(job["pdf"], sample["page_start"], sample["page_end"])
        body = ("※これはテスト送信です。実際には各生徒(保護者)のアドレスへ送信されます。\n"
                "※以下は 通し番号 {}({})さん宛ての内容の例です。\n"
                "--------------------\n{}\n\n{}").format(
                    sample["serial"], sample["name"],
                    make_greeting(sample["name"], params["honorific"]), params["body"])
        msg = build_message(params["sender"], params["sender"],
                            "【テスト送信】" + params["subject"], body,
                            pdf_part, make_filename(params["prefix"], sample["name"]))
        smtp = smtp_login(params["sender"], params["password"])
        try:
            smtp.send_message(msg)
        finally:
            smtp.quit()
    except smtplib.SMTPAuthenticationError:
        return jsonify({"ok": False, "message": AUTH_ERROR_MSG})
    except Exception as e:
        return jsonify({"ok": False, "message": "テスト送信に失敗しました: {}".format(e)})

    append_history({
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sender": params["sender"],
        "mode": "テスト送信",
        "subject": params["subject"],
        "ok": 1, "fail": 0,
        "results": [{"serial": sample["serial"], "name": sample["name"],
                     "email": params["sender"], "status": "成功", "error": ""}],
    })
    return jsonify({"ok": True,
                    "message": "自分宛て({})にテスト送信しました。受信内容を確認してください。".format(params["sender"])})


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True)
    checked, err = get_send_params(data)
    if err:
        return jsonify({"ok": False, "message": err})
    job, params = checked

    serials = data.get("serials")  # 再送信時: 対象の通し番号リスト
    targets = [s for s in job["students"] if not s["skip"]]
    if serials:
        serial_set = set(serials)
        targets = [s for s in targets if s["serial"] in serial_set]
    if not targets:
        return jsonify({"ok": False, "message": "送信対象がいません。"})

    with state_lock:
        if send_state["running"]:
            return jsonify({"ok": False, "message": "現在ほかの送信が実行中です。完了までお待ちください。"})
        send_state.update({
            "running": True, "cancel": False,
            "mode": "再送信" if serials else "本送信",
            "sender": params["sender"],
            "total": len(targets), "done": 0,
            "results": [], "message": "送信を開始しています…",
        })

    # ログイン情報の誤りは開始前にその場で伝える
    try:
        smtp = smtp_login(params["sender"], params["password"])
        smtp.quit()
    except smtplib.SMTPAuthenticationError:
        with state_lock:
            send_state["running"] = False
        return jsonify({"ok": False, "message": AUTH_ERROR_MSG})
    except Exception as e:
        with state_lock:
            send_state["running"] = False
        return jsonify({"ok": False, "message": "Gmailに接続できませんでした: {}".format(e)})

    threading.Thread(target=send_worker, args=(job, params, targets),
                     daemon=True).start()
    return jsonify({"ok": True, "total": len(targets)})


@app.route("/api/progress")
def api_progress():
    with state_lock:
        return jsonify({k: v for k, v in send_state.items() if k != "cancel"})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    with state_lock:
        if send_state["running"]:
            send_state["cancel"] = True
    return jsonify({"ok": True})


@app.route("/api/templates", methods=["GET", "POST", "DELETE"])
def api_templates():
    all_templates = load_json(TEMPLATES_FILE, {})
    if request.method == "GET":
        email = request.args.get("email", "").strip().lower()
        return jsonify({"templates": all_templates.get(email, [])})

    data = request.get_json(force=True)
    email = data.get("email", "").strip().lower()
    name = data.get("name", "").strip()
    if not email or not name:
        return jsonify({"ok": False, "message": "メールアドレスとテンプレート名が必要です。"})

    items = all_templates.get(email, [])
    items = [t for t in items if t["name"] != name]
    if request.method == "POST":
        items.append({
            "name": name,
            "subject": data.get("subject", ""),
            "body": data.get("body", ""),
            "prefix": data.get("prefix", ""),
            "honorific": data.get("honorific", ""),
        })
    all_templates[email] = items
    save_json(TEMPLATES_FILE, all_templates)
    return jsonify({"ok": True, "templates": items})


CSV_TEMPLATE = ("通し番号(2以降は送りたい人数分を自分で入力),"
                "クラス,出席番号,生徒氏名,メールアドレス\r\n"
                "1,,,,\r\n")


@app.route("/api/csv_template")
def api_csv_template():
    # Excelで開いても文字化けしないよう BOM付きUTF-8 で返す
    return send_file(
        io.BytesIO(CSV_TEMPLATE.encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="メールアドレスリスト_テンプレート.csv",
    )


@app.route("/api/history")
def api_history():
    history = load_json(HISTORY_FILE, [])
    return jsonify({"history": list(reversed(history[-100:]))})


# ---------- 起動 ----------

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    url_self = "http://127.0.0.1:{}".format(PORT)
    url_lan = "http://{}:{}".format(lan_ip(), PORT)
    print("=" * 60)
    print(" PDF個別メール送信システム を起動しました")
    print("   このPCから使う場合   : {}".format(url_self))
    print("   他のPCから使う場合   : {}".format(url_lan))
    print(" 終了するにはこのウィンドウを閉じてください")
    print("=" * 60)
    threading.Timer(1.0, lambda: webbrowser.open(url_self)).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)

# -*- coding: utf-8 -*-
import sqlite3, re, os, math, html, logging, sys, smtplib
from functools import wraps
from flask import Flask, request, Response, redirect, url_for, session, render_template_string, flash, get_flashed_messages, jsonify
from email import message_from_bytes
from email.header import decode_header, Header
from email.utils import parseaddr
from email.mime.text import MIMEText
from markupsafe import escape
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from werkzeug.security import check_password_hash, generate_password_hash
import asyncio
from aiosmtpd.controller import Controller

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.getenv("DB_FILE", os.path.join(BASE_DIR, "emails.db"))
EMAILS_PER_PAGE = int(os.getenv("EMAILS_PER_PAGE", 50))
LAST_CLEANUP_FILE = os.getenv("LAST_CLEANUP_FILE", os.path.join(BASE_DIR, "last_cleanup.txt"))
CLEANUP_INTERVAL_DAYS = int(os.getenv("CLEANUP_INTERVAL_DAYS", 1))
EMAILS_TO_KEEP = int(os.getenv("EMAILS_TO_KEEP", 1000))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
SYSTEM_TITLE = os.getenv("SYSTEM_TITLE", "Mail API Service")
SPECIAL_VIEW_TOKEN = "2088"
SERVER_PUBLIC_IP = os.getenv("SERVER_PUBLIC_IP", "127.0.0.1")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "change-this-secret-key")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.sendgrid.net")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "apikey")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_SENDER = os.getenv("DEFAULT_SENDER", "")


handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s'))
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)


def get_db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS received_emails (id INTEGER PRIMARY KEY, recipient TEXT, sender TEXT, subject TEXT, body TEXT, body_type TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, is_read BOOLEAN DEFAULT 0)')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(received_emails)")
    columns = [row['name'] for row in cursor.fetchall()]
    if 'is_read' not in columns:
        app.logger.info("Schema update: Adding 'is_read' column to 'received_emails' table.")
        cursor.execute("ALTER TABLE received_emails ADD COLUMN is_read BOOLEAN DEFAULT 0")
        conn.commit()
    conn.close()


def run_cleanup_if_needed():
    now = datetime.now()
    if os.path.exists(LAST_CLEANUP_FILE):
        try:
            with open(LAST_CLEANUP_FILE, 'r') as f:
                last_cleanup_time = datetime.fromisoformat(f.read().strip())
            if now - last_cleanup_time < timedelta(days=CLEANUP_INTERVAL_DAYS):
                return
        except Exception:
            pass
    app.logger.info("开始执行定时邮件清理任务...")
    conn = get_db_conn()
    deleted_count = conn.execute(
        f"DELETE FROM received_emails WHERE id NOT IN (SELECT id FROM received_emails ORDER BY id DESC LIMIT {EMAILS_TO_KEEP})"
    ).rowcount
    conn.commit()
    conn.close()
    if deleted_count > 0:
        app.logger.info(f"清理完成，成功删除了 {deleted_count} 封旧邮件。")
    with open(LAST_CLEANUP_FILE, 'w') as f:
        f.write(now.isoformat())


def process_email_data(to_address, raw_email_data):
    msg = message_from_bytes(raw_email_data)

    subject = ""
    if msg['Subject']:
        subject_raw, encoding = decode_header(msg['Subject'])[0]
        if isinstance(subject_raw, bytes):
            subject = subject_raw.decode(encoding or 'utf-8', errors='ignore')
        else:
            subject = str(subject_raw)
    subject = subject.strip()

    # --- 反垃圾邮件的核心判断 ---
    spam_keywords = ["email tester !", "smtp test"]
    subject_lower = subject.lower()

    if SERVER_PUBLIC_IP and SERVER_PUBLIC_IP != "127.0.0.1":
        if SERVER_PUBLIC_IP in subject:
            app.logger.warning(f"SPAM REJECTED: Subject contains server IP. From: {msg.get('From')}, Subject: '{subject}'")
            return

    for keyword in spam_keywords:
        if keyword in subject_lower:
            app.logger.warning(f"SPAM REJECTED: Subject contains keyword '{keyword}'. From: {msg.get('From')}, Subject: '{subject}'")
            return

    app.logger.info("=" * 20 + " 开始处理一封新邮件 " + "=" * 20)
    app.logger.info(f"SMTP信封接收地址: {to_address}")

    final_recipient = None
    recipient_headers_to_check = ['Delivered-To', 'X-Original-To', 'X-Forwarded-To', 'To']
    for header_name in recipient_headers_to_check:
        header_value = msg.get(header_name)
        if header_value:
            _, recipient_addr = parseaddr(header_value)
            if recipient_addr and '@' in recipient_addr:
                final_recipient = recipient_addr.strip()
                break
    if not final_recipient:
        final_recipient = str(to_address).strip()

    final_sender = None
    icloud_hme_header = msg.get('X-ICLOUD-HME')
    if icloud_hme_header:
        match = re.search(r's=([^;]+)', icloud_hme_header)
        if match:
            final_sender = match.group(1)
            app.logger.info(f"在 'X-ICLOUD-HME' 头中找到真实发件人: {final_sender}")

    if not final_sender:
        reply_to_header = msg.get('Reply-To', '')
        from_header = msg.get('From', '')
        _, reply_to_addr = parseaddr(reply_to_header)
        _, from_addr = parseaddr(from_header)
        if reply_to_addr and '@' in reply_to_addr:
            final_sender = reply_to_addr
        elif from_addr and '@' in from_addr:
            final_sender = from_addr

    if not final_sender:
        final_sender = "unknown@sender.com"

    app.logger.info(f"最终解析结果: 发件人 -> {final_sender}, 收件人 -> {final_recipient}")

    body, body_type = "", "text/plain"
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
                body_type = "text/html"
                break
            elif part.get_content_type() == 'text/plain':
                body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='ignore')
                body_type = "text/plain"
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='ignore')

    conn = get_db_conn()
    conn.execute(
        "INSERT INTO received_emails (recipient, sender, subject, body, body_type) VALUES (?, ?, ?, ?, ?)",
        (final_recipient, final_sender, subject, body, body_type)
    )
    conn.commit()
    conn.close()
    app.logger.info("邮件已存入数据库")
    run_cleanup_if_needed()


def extract_code_from_body(body_text):
    if not body_text:
        return None

    body_text = str(body_text)
    body_lower = body_text.lower()

    code_keywords = [
        'verification code', '验证码', '驗證碼', '検証コード',
        'authentication code', 'your code is',
        'chatgpt code', 'temporary verification code',
        'enter this temporary verification code',
        'log-in code', 'login code', 'one-time password', 'otp'
    ]

    if any(keyword in body_lower for keyword in code_keywords):
        semantic_patterns = [
            r'(?:your\s+chatgpt\s+code\s+is|your\s+code\s+is|verification\s+code|temporary\s+verification\s+code|authentication\s+code|log-?in\s+code|login\s+code|otp)[^\d]{0,30}(\d{6})',
        ]
        for pat in semantic_patterns:
            m = re.search(pat, body_text, re.IGNORECASE)
            if m:
                return m.group(1)

    m = re.search(r'(?<!\d)(\d{6})(?!\d)', body_text)
    if m:
        return m.group(1)

    m = re.search(r'\b(\d{4,8})\b', body_text)
    if m:
        return m.group(1)

    return None


def strip_tags_for_preview(html_content):
    if not html_content:
        return ""
    text_content = re.sub(r'<style.*?</style>|<script.*?</script>|<[^>]+>', ' ', html_content, flags=re.S)
    return re.sub(r'\s+', ' ', text_content).strip()


def parse_request_timestamp(value):
    """
    支持：
    - 秒级时间戳: 1711111111
    - 毫秒级时间戳: 1711111111000
    - ISO 时间字符串
    返回 UTC datetime，失败返回 None
    """
    if value is None or value == "":
        return None

    try:
        if isinstance(value, (int, float)) or str(value).strip().replace('.', '', 1).isdigit():
            ts = float(value)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def row_timestamp_to_utc(row_ts):
    if not row_ts:
        return None
    try:
        text = str(row_ts).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_email' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/api/unread_count')
@login_required
def unread_count():
    conn = get_db_conn()
    if session.get('is_admin'):
        count = conn.execute("SELECT COUNT(*) FROM received_emails WHERE is_read = 0").fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM received_emails WHERE recipient = ? AND is_read = 0",
            (session['user_email'],)
        ).fetchone()[0]
    conn.close()
    return jsonify({'unread_count': count})


@app.route('/')
@login_required
def index():
    return redirect(url_for('admin_view') if session.get('is_admin') else url_for('view_emails'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        conn = get_db_conn()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()

        if email == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['user_email'], session['is_admin'] = ADMIN_USERNAME, True
            return redirect(request.args.get('next') or url_for('admin_view'))
        elif user and check_password_hash(user['password_hash'], password):
            session['user_email'] = user['email']
            session.pop('is_admin', None)
            return redirect(request.args.get('next') or url_for('view_emails'))
        else:
            flash('邮箱或密码错误', 'error')

    return render_template_string('''
        <!DOCTYPE html><html><head><title>登录 - {{ SYSTEM_TITLE }}</title><style>
        body{display:flex;flex-direction:column;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;margin:0;background-color:#f4f4f4;}
        .main-title{font-size:2em;color:#333;margin-bottom:1em;font-weight:bold;}
        .login-box{padding:2em;border:1px solid #ddd;border-radius:8px;background-color:#fff;box-shadow:0 4px 6px rgba(0,0,0,0.1);width:300px;}
        h2 {text-align:center;color:#333;margin-top:0;margin-bottom:1.5em;}
        form {display:flex;flex-direction:column;}
        label {margin-bottom:0.5em;color:#555;}
        input[type="text"], input[type="password"] {padding:0.8em;margin-bottom:1em;border:1px solid #ccc;border-radius:4px;font-size:1em;}
        input[type="submit"] {padding:0.8em;border:none;border-radius:4px;background-color:#007bff;color:white;cursor:pointer;font-size:1em;transition:background-color 0.2s;}
        input[type="submit"]:hover {background-color:#0056b3;}
        .error{color:red;text-align:center;margin-bottom:1em;}
        {% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<p class="error">{{msg}}</p>{% endfor %}{% endwith %}
        </style></head><body>
        <h1 class="main-title">{{ SYSTEM_TITLE }}</h1>
        <div class="login-box"><h2>邮箱登录</h2>
        <form method="post">
        <label for="email">邮箱地址 (或管理员账户):</label><input type="text" name="email" required>
        <label for="password">密码:</label><input type="password" name="password" required>
        <input type="submit" value="登录"></form></div></body></html>
    ''', SYSTEM_TITLE=SYSTEM_TITLE)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def send_email_via_smtp(to_address, subject, body):
    if not SMTP_PASSWORD or not DEFAULT_SENDER:
        return False, "发件功能未配置(缺少API密钥或发件人地址)。"

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = DEFAULT_SENDER
    msg['To'] = to_address

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, f"邮件已成功发送至 {to_address}"
    except Exception as e:
        app.logger.error(f"通过 SMTP 发送邮件失败: {e}")
        return False, f"邮件发送失败: {e}"


@app.route('/compose', methods=['GET', 'POST'])
@login_required
def compose_email():
    if not SMTP_PASSWORD or not DEFAULT_SENDER:
        flash('发件功能未配置。请在安装脚本中提供SendGrid API密钥和已验证的发件人邮箱。', 'error')
        return redirect(url_for('index'))

    form_data = {}
    if request.method == 'POST':
        to_address = request.form.get('to')
        subject = request.form.get('subject')
        body = request.form.get('body')

        if not to_address or not subject:
            flash('收件人和主题不能为空！', 'error')
            form_data = {'to': to_address, 'subject': subject, 'body': body}
        else:
            success, message = send_email_via_smtp(to_address, subject, body)
            flash(message, 'success' if success else 'error')
            if success:
                return redirect(url_for('index'))
            else:
                form_data = {'to': to_address, 'subject': subject, 'body': body}

    reply_to_id = request.args.get('reply_to_id')
    if reply_to_id and not form_data:
        try:
            conn = get_db_conn()
            query = "SELECT * FROM received_emails WHERE id = ?"
            params = [reply_to_id]
            if not session.get('is_admin'):
                query += " AND recipient = ?"
                params.append(session['user_email'])

            original_email = conn.execute(query, params).fetchone()
            conn.close()

            if original_email:
                _, parsed_sender = parseaddr(original_email['sender'])
                form_data['to'] = parsed_sender or ''

                original_subject = original_email['subject'] or ""
                if not original_subject.lower().startswith('re:'):
                    form_data['subject'] = "Re: " + original_subject
                else:
                    form_data['subject'] = original_subject

                beijing_tz = ZoneInfo("Asia/Shanghai")
                utc_dt = datetime.strptime(original_email['timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                bjt_str = utc_dt.astimezone(beijing_tz).strftime('%Y-%m-%d %H:%M:%S')

                body_content = strip_tags_for_preview(original_email['body'] or '')
                quoted_text = "\n".join([f"> {line}" for line in body_content.splitlines()])
                form_data['body'] = f"\n\n\n--- On {bjt_str}, {original_email['sender']} wrote: ---\n{quoted_text}"
        except Exception as e:
            app.logger.error(f"加载回复邮件时出错: {e}")
            flash("加载原始邮件以供回复时出错。", 'error')

    return render_template_string('''
        <!DOCTYPE html><html><head><title>写新邮件 - {{SYSTEM_TITLE}}</title><style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; background-color: #f8f9fa; display: flex; justify-content: center; padding-top: 4em; }
            .container { width: 100%; max-width: 800px; background: #fff; padding: 2em; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h2 { color: #333; } a { color: #007bff; text-decoration: none; } a:hover { text-decoration: underline; }
            form { margin-top: 1.5em; } .form-group { margin-bottom: 1em; } label { display: block; margin-bottom: .5em; color: #555; }
            input[type="text"], input[type="email"], textarea { width: calc(100% - 22px); padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 1em; }
            input[readonly] { background-color: #e9ecef; } textarea { height: 250px; resize: vertical; }
            button { padding: 10px 20px; border: none; border-radius: 4px; color: white; cursor: pointer; background-color: #007bff; font-size: 1em; }
            button:hover { background-color: #0056b3; }
            .flash-success { padding: 1em; margin-bottom: 1em; border-radius: 4px; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .flash-error { padding: 1em; margin-bottom: 1em; border-radius: 4px; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .nav-link { font-size: 1.2em; }
        </style></head><body><div class="container">
        <h2><a href="{{url_for('index')}}" class="nav-link">&larr; 返回收件箱</a> | 写新邮件</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="flash-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        <form method="post">
            <div class="form-group">
                <label for="from_address">发件人:</label>
                <input type="text" id="from_address" name="from_address" value="{{ from_email }}" readonly>
            </div>
            <div class="form-group">
                <label for="to">收件人:</label>
                <input type="email" id="to" name="to" value="{{ form_data.get('to', '') }}" required>
            </div>
            <div class="form-group">
                <label for="subject">主题:</label>
                <input type="text" id="subject" name="subject" value="{{ form_data.get('subject', '') }}" required>
            </div>
            <div class="form-group">
                <label for="body">正文:</label>
                <textarea id="body" name="body" required>{{ form_data.get('body', '') }}</textarea>
            </div>
            <button type="submit">发送邮件</button>
        </form>
        </div></body></html>
    ''', SYSTEM_TITLE=SYSTEM_TITLE, from_email=DEFAULT_SENDER, form_data=form_data)


def render_email_list_page(emails_data, page, total_pages, total_emails, search_query, is_admin_view, token_view_context=None):
    if token_view_context:
        endpoint = 'view_mail_by_token'
        title_text = f"收件箱 ({token_view_context['mail']}) - 共 {total_emails} 封"
    else:
        endpoint = 'admin_view' if is_admin_view else 'view_emails'
        title_text = f"管理员视图 (共 {total_emails} 封)" if is_admin_view else f"收件箱 ({session.get('user_email', '')} - 共 {total_emails} 封)"

    processed_emails = []
    beijing_tz = ZoneInfo("Asia/Shanghai")
    sending_enabled = bool(SMTP_PASSWORD and DEFAULT_SENDER)

    for item in emails_data:
        utc_dt = datetime.strptime(item['timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        bjt_str = utc_dt.astimezone(beijing_tz).strftime('%Y-%m-%d %H:%M:%S')
        body_for_preview = strip_tags_for_preview(item['body']) if item['body_type'] and 'html' in item['body_type'] else (item['body'] or "")
        code = extract_code_from_body(body_for_preview)
        processed_emails.append({
            'id': item['id'], 'bjt_str': bjt_str, 'subject': item['subject'], 'is_read': item['is_read'],
            'preview_text': code if code else body_for_preview, 'is_code': bool(code),
            'recipient': item['recipient'], 'sender': parseaddr(item['sender'] or "")[1]
        })

    return render_template_string('''
        <!DOCTYPE html><html><head><title>{{title}} - {{SYSTEM_TITLE}}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; background-color: #f8f9fa; font-size: 14px; }
            .container { max-width: 95%; margin: 0 auto; padding: 1em; }
            .flash-success, .flash-error { padding: 1em; margin-bottom: 1em; border-radius: 4px; border: 1px solid transparent; transition: opacity 0.5s ease; }
            .flash-success { background-color: #d4edda; color: #155724; border-color: #c3e6cb; }
            .flash-error { background-color: #f8d7da; color: #721c24; border-color: #f5c6cb; }
            table { border-collapse: collapse; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.05); background-color: #fff; margin-top: 1.5em; border: 1px solid #dee2e6; }
            th, td { padding: 12px 15px; vertical-align: middle; border-bottom: 1px solid #dee2e6; border-right: 1px solid #dee2e6; word-break: break-all; }
            th:last-child, td:last-child { border-right: none; }
            tr.unread { font-weight: bold; background-color: #fffaf0; }
            tr:hover { background-color: #f1f3f5; }
            th { background-color: #4CAF50; color: white; text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.05em; text-align: center; }
            .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5em; flex-wrap: wrap; gap: 1em;}
            .top-bar h2 { margin: 0; color: #333; font-size: 1.5em; }
            .top-bar .user-actions { display: flex; gap: 10px; }
            .btn { text-decoration: none; display: inline-block; padding: 8px 15px; border: 1px solid transparent; border-radius: 4px; color: white; cursor: pointer; font-size: 0.9em; transition: background-color 0.2s; white-space: nowrap; }
            .btn-primary { background-color: #007bff; border-color: #007bff; } .btn-primary:hover { background-color: #0056b3; }
            .btn-secondary { background-color: #6c757d; border-color: #6c757d; } .btn-danger { background-color: #dc3545; border-color: #dc3545; }
            .controls { display: flex; justify-content: space-between; align-items: center; padding-bottom: 1.5em; border-bottom: 1px solid #dee2e6; flex-wrap: wrap; gap: 1em;}
            .controls .bulk-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
            .search-form { display: flex; gap: 5px; }
            .search-form input[type="text"] { padding: 8px; border: 1px solid #ccc; border-radius: 4px; min-width: 200px;}
            .pagination { margin-top: 1.5em; text-align: center; }
            .pagination a { color: #007bff; padding: 8px 12px; text-decoration: none; border: 1px solid #ddd; margin: 0 4px; border-radius: 4px; }
            .pagination a:hover { background-color: #e9ecef; }
            .preview-code { color: #e83e8c; font-weight: bold; font-family: monospace; }
            a.view-link { color: #007bff; text-decoration: none; } a.view-link:hover { text-decoration: underline; }
            td { text-align: left; } .preview-text { overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
        </style></head><body>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endwith %}
            <div class="top-bar">
                <h2>{{title}}</h2>
                <div class="user-actions">
                    {% if sending_enabled %}
                    <a href="{{url_for('compose_email')}}" class="btn btn-primary">写邮件</a>
                    {% endif %}
                    {% if not token_view_context and is_admin_view %}
                        <a href="{{url_for('manage_users')}}" class="btn btn-secondary">管理用户</a>
                    {% endif %}
                    {% if not token_view_context %}
                         <a href="{{url_for('logout')}}" class="btn btn-danger">登出</a>
                    {% endif %}
                </div>
            </div>
            <div class="controls">
                <div class="bulk-actions">
                    {% if is_admin_view %}
                        <button onclick="window.location.reload();" class="btn btn-secondary">刷新</button>
                        <button type="submit" form="delete-selected-form" class="btn btn-secondary">删除选中</button>
                        <form id="delete-all-form" method="POST" action="{{url_for('delete_all_emails')}}" style="display: inline;" onsubmit="return confirm('您确定要删除所有邮件吗？这将无法恢复！');">
                            <button type="submit" class="btn btn-danger">删除所有</button>
                        </form>
                    {% endif %}
                </div>
                <form method="get" class="search-form" action="{{ url_for(endpoint) }}">
                    <input type="text" name="search" value="{{search_query|e}}" placeholder="搜索...">
                    {% if token_view_context %}
                    <input type="hidden" name="token" value="{{ token_view_context.token }}">
                    <input type="hidden" name="mail" value="{{ token_view_context.mail }}">
                    {% endif %}
                    <button type="submit" class="btn btn-primary">搜索</button>
                </form>
            </div>
            <form id="delete-selected-form" method="POST" action="{{url_for('delete_selected_emails')}}">
            <table>
                <thead><tr>
                    <th style="width: 3%; min-width: 40px;"><input type="checkbox" onclick="toggleAllCheckboxes(this);" {% if not is_admin_view %}style="display:none;"{% endif %}></th>
                    <th style="width: 15%; min-width: 160px;">时间 (北京)</th><th style="width: 20%; min-width: 150px;">主题</th>
                    <th style="width: 35%; min-width: 200px;">内容预览</th><th style="width: 13%; min-width: 120px;">收件人</th><th style="width: 14%; min-width: 120px;">发件人</th>
                </tr></thead>
                <tbody>
                {% for mail in mails %}
                <tr class="{{'unread' if not mail.is_read else ''}}">
                    <td style="text-align: center;"><input type="checkbox" name="selected_ids" value="{{mail.id}}" {% if not is_admin_view %}style="display:none;"{% endif %}></td>
                    <td>{{mail.bjt_str}}</td>
                    <td>{{mail.subject|e}} <a href="{{ url_for('view_email_detail', email_id=mail.id) }}" target="_blank" class="view-link" title="新窗口打开">↳</a></td>
                    <td>
                        {% if mail.is_code %}<span class="preview-code">{{mail.preview_text|e}}</span>
                        {% else %}<div class="preview-text" title="{{mail.preview_text|e}}">{{mail.preview_text|e}}</div>{% endif %}
                    </td>
                    <td>{{mail.recipient|e}}</td><td>{{mail.sender|e}}</td>
                </tr>
                {% else %}<tr><td colspan="6" style="text-align:center;padding:2em;">无邮件</td></tr>{% endfor %}
                </tbody>
            </table>
            </form>
            <div class="pagination">
                {% if page > 1 %}
                    <a href="{{url_for(endpoint, page=page-1, search=search_query)}}">&laquo; 上一页</a>
                {% endif %}
                <span> Page {{page}} / {{total_pages}} </span>
                {% if page < total_pages %}
                    <a href="{{url_for(endpoint, page=page+1, search=search_query)}}">下一页 &raquo;</a>
                {% endif %}
            </div>
        </div>
        <script>
            function toggleAllCheckboxes(source) {
                var checkboxes = document.getElementsByName('selected_ids');
                for(var i=0; i < checkboxes.length; i++) { checkboxes[i].checked = source.checked; }
            }
            document.addEventListener('DOMContentLoaded', function() {
                const flashMessages = document.querySelectorAll('.flash-success, .flash-error');
                flashMessages.forEach(function(message) {
                    setTimeout(function() {
                        message.style.opacity = '0';
                        setTimeout(function() { message.style.display = 'none'; }, 500);
                    }, 5000);
                });
            });
        </script>
        </body></html>
    ''', title=title_text, mails=processed_emails, page=page, total_pages=total_pages, search_query=search_query, is_admin_view=is_admin_view, endpoint=endpoint, SYSTEM_TITLE=SYSTEM_TITLE, token_view_context=token_view_context, sending_enabled=sending_enabled)


@app.route('/view')
@login_required
def view_emails():
    return base_view_logic(is_admin_view=False)


@app.route('/admin')
@login_required
@admin_required
def admin_view():
    return base_view_logic(is_admin_view=True)


def base_view_logic(is_admin_view, mark_as_read=True, recipient_override=None):
    search_query = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    conn = get_db_conn()
    where_clauses, params = [], []
    token_context = None

    if recipient_override:
        is_admin_view = False
        where_clauses.append("recipient = ?")
        params.append(recipient_override)
        if search_query:
            where_clauses.append("(subject LIKE ? OR sender LIKE ?)")
            params.extend([f"%{search_query}%"] * 2)
        token_context = {'token': request.args.get('token'), 'mail': recipient_override}
    elif is_admin_view:
        if search_query:
            where_clauses.append("(subject LIKE ? OR recipient LIKE ? OR sender LIKE ?)")
            params.extend([f"%{search_query}%"] * 3)
    else:
        where_clauses.append("recipient = ?")
        params.append(session['user_email'])
        if search_query:
            where_clauses.append("(subject LIKE ? OR sender LIKE ?)")
            params.extend([f"%{search_query}%"] * 2)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    total_emails = conn.execute(f"SELECT COUNT(*) FROM received_emails {where_sql}", params).fetchone()[0]
    total_pages = math.ceil(total_emails / EMAILS_PER_PAGE) if total_emails > 0 else 1
    offset = (page - 1) * EMAILS_PER_PAGE
    emails_data = conn.execute(
        f"SELECT * FROM received_emails {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [EMAILS_PER_PAGE, offset]
    ).fetchall()

    if mark_as_read:
        ids_to_mark = [str(e['id']) for e in emails_data if not e['is_read']]
        if ids_to_mark:
            conn.execute(f"UPDATE received_emails SET is_read=1 WHERE id IN ({','.join(ids_to_mark)})")
            conn.commit()

    conn.close()
    return render_email_list_page(emails_data, page, total_pages, total_emails, search_query, is_admin_view, token_view_context=token_context)


# ----------------- 兼容旧脚本：原 /Mail 接口 -----------------
@app.route('/Mail')
def view_mail_by_token():
    token = request.args.get('token')
    recipient_mail = request.args.get('mail')

    if not token or token != SPECIAL_VIEW_TOKEN:
        return jsonify({"error": "Invalid token"}), 401
    if not recipient_mail:
        return jsonify({"error": "mail parameter is missing"}), 400

    old_keywords = ["verify your email address", "验证您的电子邮件地址", "e メールアドレスを検証してください", "verification code"]
    new_keywords = ["chatgpt", "openai"]

    conn = get_db_conn()
    try:
        messages = conn.execute("""
            SELECT id, subject, body, body_type
            FROM received_emails
            WHERE lower(trim(recipient)) = lower(trim(?))
            ORDER BY id DESC
            LIMIT 50
        """, (recipient_mail,)).fetchall()

        for msg in messages:
            subject = (msg['subject'] or "").lower().strip()
            body = (msg['body'] or "").lower()

            match_old = any(subject.startswith(k) for k in old_keywords)
            match_new = any(k in subject for k in new_keywords) or any(k in body for k in new_keywords)

            if match_old or match_new:
                return Response(msg['body'], mimetype=f"{msg['body_type'] or 'text/html'}; charset=utf-8")

        return jsonify({"error": "Verification email not found"}), 404
    finally:
        if conn:
            conn.close()
# ----------------- /Mail 结束 -----------------


# ----------------- 新接口：返回最新验证码 JSON，支持时间过滤 -----------------
@app.route('/MailCode')
def view_mail_code_by_token():
    token = request.args.get('token')
    recipient_mail = request.args.get('mail')
    after = request.args.get('after') or request.args.get('min_ts') or request.args.get('otp_sent_at')

    if not token or token != SPECIAL_VIEW_TOKEN:
        return jsonify({"error": "Invalid token"}), 401
    if not recipient_mail:
        return jsonify({"error": "mail parameter is missing"}), 400

    after_dt = parse_request_timestamp(after)

    conn = get_db_conn()
    try:
        messages = conn.execute("""
            SELECT id, recipient, sender, subject, body, body_type, timestamp
            FROM received_emails
            WHERE lower(trim(recipient)) = lower(trim(?))
            ORDER BY id DESC
            LIMIT 100
        """, (recipient_mail,)).fetchall()

        for msg in messages:
            sender = (msg['sender'] or "").lower().strip()
            subject = (msg['subject'] or "").strip()
            body = msg['body'] or ""
            body_type = msg['body_type'] or "text/plain"
            ts = row_timestamp_to_utc(msg['timestamp'])

            # 只接受 after 之后的新邮件
            if after_dt and ts:
                if ts < (after_dt - timedelta(seconds=1)):
                    continue

            preview_text = strip_tags_for_preview(body)
            combined_text = f"{subject}\n{preview_text}"

            looks_like_openai = (
                "openai" in sender
                or "chatgpt" in combined_text.lower()
                or "verification code" in combined_text.lower()
                or "temporary verification code" in combined_text.lower()
                or "log-in code" in combined_text.lower()
                or "login code" in combined_text.lower()
                or "your code is" in combined_text.lower()
            )
            if not looks_like_openai:
                continue

            code = extract_code_from_body(subject) or extract_code_from_body(preview_text)
            if not code:
                continue

            return jsonify({
                "id": msg["id"],
                "recipient": msg["recipient"],
                "sender": msg["sender"],
                "subject": msg["subject"],
                "timestamp": msg["timestamp"],
                "body_type": body_type,
                "code": code
            })

        return jsonify({"error": "Verification email not found"}), 404
    finally:
        if conn:
            conn.close()
# ----------------- /MailCode 结束 -----------------


@app.route('/delete_selected_emails', methods=['POST'])
@login_required
@admin_required
def delete_selected_emails():
    selected_ids = request.form.getlist('selected_ids')
    if selected_ids:
        conn = get_db_conn()
        try:
            placeholders = ','.join('?' for _ in selected_ids)
            query = f"DELETE FROM received_emails WHERE id IN ({placeholders})"
            conn.execute(query, selected_ids)
            conn.commit()
        finally:
            if conn:
                conn.close()
    return redirect(request.referrer or url_for('admin_view'))


@app.route('/delete_all_emails', methods=['POST'])
@login_required
@admin_required
def delete_all_emails():
    conn = get_db_conn()
    try:
        conn.execute("DELETE FROM received_emails")
        conn.commit()
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_view'))


@app.route('/view_email/<int:email_id>')
@login_required
def view_email_detail(email_id):
    conn = get_db_conn()
    if session.get('is_admin'):
        email = conn.execute("SELECT * FROM received_emails WHERE id = ?", (email_id,)).fetchone()
    else:
        email = conn.execute("SELECT * FROM received_emails WHERE id = ? AND recipient = ?", (email_id, session['user_email'])).fetchone()

    if not email:
        conn.close()
        return "邮件未找到或无权查看", 404

    if not email['is_read']:
        conn.execute("UPDATE received_emails SET is_read = 1 WHERE id = ?", (email_id,))
        conn.commit()
    conn.close()

    sending_enabled = bool(SMTP_PASSWORD and DEFAULT_SENDER)
    _, sender_address = parseaddr(email['sender'])
    is_replyable_address = '@' in (sender_address or '')

    reply_button_html = ''
    if not sending_enabled:
        reply_button_html = '<a href="#" class="btn disabled" title="发件功能未配置，无法回复">回复</a>'
    elif not is_replyable_address:
        reply_button_html = '<a href="#" class="btn disabled" title="无法识别有效的发件人地址">无法回复</a>'
    else:
        reply_button_html = f'<a href="{url_for("compose_email", reply_to_id=email_id)}" class="btn">回复</a>'

    body_content = email['body'] or ''
    if 'text/html' in (email['body_type'] or ''):
        email_display = f'<iframe srcdoc="{html.escape(body_content)}" style="width:100%;height:calc(100vh - 50px);border:none;"></iframe>'
    else:
        email_display = f'<pre style="white-space:pre-wrap;word-wrap:break-word;padding:1em;">{escape(body_content)}</pre>'

    return render_template_string(f'''
        <!DOCTYPE html><html><head><title>邮件详情</title>
        <style>
            body {{ margin: 0; font-family: sans-serif; }}
            .top-bar {{ display: flex; align-items: center; justify-content: flex-start; padding: 8px 15px; background-color: #f8f9fa; border-bottom: 1px solid #dee2e6; }}
            .btn {{ text-decoration: none; display: inline-block; padding: 8px 15px; border-radius: 4px; color: white; background-color: #007bff; transition: background-color 0.2s; }}
            .btn:hover {{ background-color: #0056b3; }}
            .btn.disabled {{ background-color: #6c757d; cursor: not-allowed; }}
        </style></head><body>
            <div class="top-bar">{reply_button_html}</div>
            {email_display}
        </body></html>
    ''')


@app.route('/view_email_token/<int:email_id>')
def view_email_token_detail(email_id):
    token = request.args.get('token')
    if token != SPECIAL_VIEW_TOKEN:
        return "无效的Token", 403

    conn = get_db_conn()
    email = conn.execute("SELECT * FROM received_emails WHERE id = ?", (email_id,)).fetchone()
    conn.close()
    if not email:
        return "邮件未找到", 404

    body_content = email['body'] or ''
    if 'text/html' in (email['body_type'] or ''):
        email_display = f'<iframe srcdoc="{html.escape(body_content)}" style="width:100%;height:calc(100vh - 20px);border:none;"></iframe>'
    else:
        email_display = f'<pre style="white-space:pre-wrap;word-wrap:break-word;">{escape(body_content)}</pre>'

    return Response(email_display, mimetype="text/html; charset=utf-8")


@app.route('/manage_users', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_users():
    conn = get_db_conn()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            email, password = request.form.get('email'), request.form.get('password')
            if email and password:
                try:
                    conn.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, generate_password_hash(password)))
                    conn.commit()
                    flash(f"用户 {email} 添加成功", 'success')
                except sqlite3.IntegrityError:
                    flash(f"用户 {email} 已存在", 'error')
        elif action == 'delete':
            user_id = request.form.get('user_id')
            conn.execute("DELETE FROM users WHERE id = ? AND email != ?", (user_id, ADMIN_USERNAME))
            conn.commit()
            flash("用户已删除", 'success')

    users = conn.execute("SELECT id, email FROM users WHERE email != ?", (ADMIN_USERNAME,)).fetchall()
    conn.close()

    return render_template_string('''
        <!DOCTYPE html><html><head><title>管理用户 - {{SYSTEM_TITLE}}</title><style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; background-color: #f8f9fa; display: flex; justify-content: center; padding-top: 4em; }
            .container { width: 100%; max-width: 800px; background: #fff; padding: 2em; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h2, h3 { color: #333; } a { color: #007bff; text-decoration: none; } a:hover { text-decoration: underline; }
            form { margin-bottom: 2em; padding: 1.5em; border: 1px solid #ddd; border-radius: 5px; background: #fdfdfd; }
            form.inline-form { display: inline; border: none; padding: 0; margin: 0; background: none; }
            input[type="email"], input[type="password"] { width: calc(100% - 22px); padding: 10px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; }
            button { padding: 10px 15px; border: none; border-radius: 4px; color: white; cursor: pointer; transition: background-color 0.2s; }
            button.add { background-color: #28a745; } button.add:hover { background-color: #218838; }
            button.delete { background-color: #dc3545; } button.delete:hover { background-color: #c82333; }
            ul { list-style: none; padding: 0; }
            li { background: #f8f9fa; padding: 15px; border-bottom: 1px solid #ddd; display: flex; justify-content: space-between; align-items: center; }
            li:last-child { border-bottom: none; }
            .flash-success { color: green; font-weight: bold; margin-bottom: 1em; }
            .flash-error { color: red; font-weight: bold; margin-bottom: 1em; } .nav-link { font-size: 1.2em; }
        </style></head><body><div class="container">
        <h2><a href="{{url_for('admin_view')}}" class="nav-link">&larr; 返回收件箱</a> | 管理用户</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <p class="flash-{{ category }}">{{ message }}</p>
            {% endfor %}
        {% endwith %}
        <h3>添加新用户</h3>
        <form method="post">
            <input type="hidden" name="action" value="add">
            <input type="email" name="email" placeholder="新用户邮箱地址" required>
            <input type="password" name="password" placeholder="新用户密码" required>
            <button type="submit" class="add">添加用户</button>
        </form>
        <h3>现有用户</h3>
        <ul>
            {% for user in users %}
            <li>
                <span>{{user.email}}</span>
                <form method="post" class="inline-form" onsubmit="return confirm('确定要删除该用户吗？');">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="user_id" value="{{user.id}}">
                    <button type="submit" class="delete">删除</button>
                </form>
            </li>
            {% else %}<li>无普通用户</li>{% endfor %}
        </ul>
        </div></body></html>
    ''', users=users, SYSTEM_TITLE=SYSTEM_TITLE)


class CustomSMTPHandler:
    async def handle_DATA(self, server, session, envelope):
        try:
            process_email_data(','.join(envelope.rcpt_tos), envelope.content)
            return '250 OK'
        except Exception as e:
            app.logger.error(f"处理邮件时发生严重错误: {e}")
            return '500 Error processing message'


if __name__ == '__main__':
    init_db()
    controller = Controller(CustomSMTPHandler(), hostname='0.0.0.0', port=25)
    controller.start()
    app.logger.info("SMTP 服务器启动，监听端口 25...")
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        app.logger.info("SMTP 服务器已关闭。")

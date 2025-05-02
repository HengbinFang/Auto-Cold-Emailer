# cold_email_tool.py

import os
import csv
import sqlite3
import imaplib
import smtplib
import base64
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, redirect, render_template_string, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
import uuid
import asyncio
import time
import atexit
import select
import threading

UPLOAD_FOLDER = 'uploads'
DB_PATH = 'email_tool.db'
TRACKING_PIXEL_PATH = 'pixel.png'
SEND_INTERVAL_MINUTES = 10  # will stagger every 10 min, rotating accounts

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
scheduler = BackgroundScheduler()
scheduler.start()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Templates

UPLOAD_TEMPLATE = '''
<h2>Upload Recipient CSV</h2>
<form method="post" enctype="multipart/form-data">
  <input type="file" name="file">
  <input type="submit" value="Upload Recipients CSV">
</form>
<br>
<h2>Upload Account CSV</h2>
<form method="post" enctype="multipart/form-data" action="/accounts_upload">
  <input type="file" name="file">
  <input type="submit" value="Upload Accounts CSV">
</form>
'''
SELECT_TEMPLATE = '''
<h2>Select Columns</h2>
<form method="post" action="/select">
  <input type="hidden" name="filename" value="{{ filename }}">
  <label for="email_col">Email Column:</label>
  <select name="email_col">{% for col in cols %}<option>{{ col }}</option>{% endfor %}</select><br>
  <label for="msg_col">Message Column:</label>
  <select name="msg_col">{% for col in cols %}<option>{{ col }}</option>{% endfor %}</select><br>
  <input type="submit" value="Start Sending">
</form>
'''

INBOX_TEMPLATE = '''
<h2>Inbox</h2>
{% for msg in messages %}
  <div style="border:1px solid #ccc; padding:10px; margin-bottom:10px;">
    <p><b>From:</b> {{ msg['from'] }}</p>
    <p><b>Subject:</b> {{ msg['subject'] }}</p>
    <p><b>Body:</b> {{ msg['body'] }}</p>
    <form method="post" action="/reply">
      <input type="hidden" name="to" value="{{ msg['from'] }}">
      <input type="hidden" name="subject" value="{{ msg['subject'] }}">
      <input type="hidden" name="message_id" value="{{ msg['message_id'] }}">
      <input type="hidden" name="references" value="{{ msg['references'] }}">
      <input type="hidden" name="original_body" value="{{ msg['body'] }}">
      <textarea name="body" placeholder="Your reply..." style="width: 100%; height: 100px;"></textarea>
      <input type="submit" value="Reply">
    </form>
  </div>
{% endfor %}
'''

# Create the scheduler
scheduler = BackgroundScheduler()

all_messages = []
imap_blacklist = {}
BLACKLIST_DURATION = 3600

# Stores active message lists per inbox
per_account_messages = {}

# Database setup
with sqlite3.connect(DB_PATH) as conn:
    conn.execute('''CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY,
        uid TEXT,
        email TEXT,
        message TEXT,
        sent_at TIMESTAMP,
        opened INTEGER DEFAULT 0,
        opened_at TIMESTAMP,
        replied INTEGER DEFAULT 0,
        replied_at TIMESTAMP,
        account_email TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY,
        email TEXT UNIQUE,
        smtp_host TEXT,
        smtp_port INTEGER,
        smtp_user TEXT,
        smtp_pass TEXT,
        imap_host TEXT,
        imap_port INTEGER,
        imap_user TEXT,
        imap_pass TEXT,
        daily_limit INTEGER,
        last_sent DATE,
        sent_today INTEGER DEFAULT 0
    )''')
    conn.commit()

@app.route('/', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'file' not in request.files or request.files['file'].filename == '':
            return "No file uploaded", 400
        file = request.files['file']
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        with open(filepath, newline='', encoding='utf-8-sig') as csvfile:  # handles BOM
            reader = csv.reader(csvfile)
            headers = next(reader)
        return render_template_string(SELECT_TEMPLATE, cols=headers, filename=filename)
    return render_template_string(UPLOAD_TEMPLATE)

@app.route('/select', methods=['POST'])
def select_columns():
    email_col = request.form['email_col']
    msg_col = request.form['msg_col']
    filename = request.form['filename']
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    inserted = 0
    with open(filepath, newline='', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            uid = str(uuid.uuid4())
            email = row.get(email_col)
            msg = row.get(msg_col)
            if not email or not msg:
                continue
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT INTO emails (uid, email, message) VALUES (?, ?, ?)", (uid, email, msg))
                conn.commit()
                inserted += 1

    print(f"[SELECT] Inserted {inserted} emails into queue")

    if inserted > 0:
        print("[SCHEDULER] Starting staggered sending task")
        scheduler.add_job(send_next_email, 'interval', minutes=SEND_INTERVAL_MINUTES, id='send_task', replace_existing=True)
    else:
        print("[SELECT] No emails to insert. Scheduler not started.")

    return redirect('/dashboard')

@app.route('/accounts_upload', methods=['POST'])
def upload_accounts():
    file = request.files['file']
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
    file.save(filepath)
    with open(filepath, newline='', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        with sqlite3.connect(DB_PATH) as conn:
            for row in reader:
                conn.execute('''INSERT OR REPLACE INTO accounts (
                    email, smtp_host, smtp_port, smtp_user, smtp_pass,
                    imap_host, imap_port, imap_user, imap_pass, daily_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                    row['Email'],
                    row['SMTP Host'],
                    int(row['SMTP Port']),
                    row['SMTP Username'],
                    row['SMTP Password'],
                    row['IMAP Host'],
                    int(row['IMAP Port']),
                    row['IMAP Username'],
                    row['IMAP Password'],
                    int(row['Daily Limit'])
                ))
            conn.commit()
    return "Accounts uploaded."


def get_available_account():
    today = date.today()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('''SELECT * FROM accounts
                               WHERE (last_sent IS NULL OR last_sent < ? OR sent_today < daily_limit)
                               ORDER BY sent_today ASC''', (today,)).fetchall()
        if not rows:
            return None
        account = rows[0]
        if account[11] != today:
            conn.execute("UPDATE accounts SET last_sent=?, sent_today=0 WHERE id=?", (today, account[0]))
            conn.commit()
        return account
    
# inside send_next_email()
def send_next_email():
    print("[SCHEDULER] Checking for unsent emails...")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id, uid, email, message FROM emails WHERE sent_at IS NULL LIMIT 1").fetchone()
        if not row:
            print("[SCHEDULER] No unsent emails found.")
            return
        id_, uid, to_email, message = row

        account = get_available_account()
        if not account:
            print("[SCHEDULER] No available account with quota.")
            return
        
        print("[DEBUG] Using account:", account)

        smtp_host = account[2]
        smtp_port = account[3]
        smtp_user = account[4]
        smtp_pass = account[5]


        full_msg = MIMEMultipart("alternative")
        full_msg['Subject'] = "Hello from Hengbin"
        full_msg['From'] = account[1]
        full_msg['To'] = to_email
        html_msg = f"{message}<img src='http://localhost:5000/pixel.gif?uid={uid}' width='1' height='1'>"
        full_msg.attach(MIMEText(html_msg, 'html'))

        try:
            print(f"[SEND] Attempting to send to {to_email} using {account[1]} ({smtp_host}:{smtp_port})")
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                server.set_debuglevel(1)  # ← this will show raw SMTP handshake
                print("Connected. Logging in...")
                server.login(smtp_user, smtp_pass)
                print("Logged in.")
                server.send_message(full_msg)

            conn.execute(
                "UPDATE emails SET sent_at=?, account_email=? WHERE id=?",
                (datetime.utcnow(), account[1], id_)
            )
            conn.execute("UPDATE accounts SET sent_today = sent_today + 1 WHERE id=?", (account[0],))
            conn.commit()
            print(f"[SUCCESS] Sent to {to_email}")
        except Exception as e:
            print(f"[ERROR] Failed to send to {to_email} via {account[1]}: {e}")

# (You can add 'account_email' field in the inbox/reply tracking if needed)
@app.route('/pixel.gif')
def tracking_pixel():
    uid = request.args.get('uid')
    if uid:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE emails SET opened=1, opened_at=? WHERE uid=?", (datetime.utcnow(), uid))
            conn.commit()
    return send_file(TRACKING_PIXEL_PATH, mimetype='image/gif')

@app.route('/dashboard')
def dashboard():
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM emails WHERE sent_at IS NOT NULL").fetchone()[0]
        opened = conn.execute("SELECT COUNT(*) FROM emails WHERE opened=1").fetchone()[0]
        replied = conn.execute("SELECT COUNT(*) FROM emails WHERE replied=1").fetchone()[0]
    return f"""
    <h2>Dashboard</h2>
    <p>Total: {total}</p>
    <p>Sent: {sent}</p>
    <p>Opened: {opened} ({(opened/sent*100 if sent else 0):.2f}%)</p>
    <p>Replied: {replied} ({(replied/sent*100 if sent else 0):.2f}%)</p>
    <a href='/inbox'>View Inbox</a>
    """

def background_inbox_fetch_parallel():
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        accounts = conn.execute("SELECT imap_host, imap_port, imap_user, imap_pass FROM accounts").fetchall()
    
    def launch_all(accounts):
        for host, port, user, pwd in accounts:
            threading.Thread(target=persistent_check_loop, args=(host, port, user, pwd), daemon=True).start()
    
    threading.Thread(target=launch_all, args=(accounts,), daemon=True).start()

def persistent_check_loop(host, port, user, pwd):
    key = f"{user}@{host}:{port}"
    print(f"[IDLE LOOP STARTED] {key} is now running in persistent IDLE mode.")

    while True:
        try:
            with imaplib.IMAP4_SSL(host, port, timeout=30) as mail:
                mail.debug = 4
                mail.login(user, pwd)
                mail.select("inbox")

                try:
                    while True:
                        print(f"[IDLE] {key} entering IDLE...")
                        tag = mail._new_tag()
                        mail.send(f"{tag} IDLE\r\n".encode())
                        if not mail.readline().startswith(b'+'):
                            raise Exception("IDLE not acknowledged")

                        # Wait for change or timeout
                        if select.select([mail.sock], [], [], 1740)[0]:
                            print(f"[IDLE] {key} change detected!")
                            mail.send(b"DONE\r\n")

                            # Flush full DONE response
                            while True:
                                line = mail.readline()
                                if line.startswith(tag.encode()):
                                    break

                            time.sleep(0.2)

                            typ, data = mail.search(None, 'UNSEEN')
                            messages = []
                            for num in data[0].split():
                                typ, msg_data = mail.fetch(num, '(BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID REFERENCES)] BODY[TEXT])')
                                from_, subject, message_id, references, body = '', '', '', '', ''
                                for part in msg_data:
                                    if isinstance(part, tuple):
                                        raw = part[1].decode(errors='ignore')
                                        if 'From:' in raw:
                                            from_ = raw.split('From:')[-1].strip()
                                        elif 'Subject:' in raw:
                                            subject = raw.split('Subject:')[-1].strip()
                                        elif 'Message-ID:' in raw:
                                            message_id = raw.split('Message-ID:')[-1].strip()
                                        elif 'References:' in raw:
                                            references = raw.split('References:')[-1].strip()
                                        else:
                                            body = raw.strip()
                                messages.append({
                                    'from': from_,
                                    'subject': subject,
                                    'message_id': message_id,
                                    'references': references,
                                    'body': body
                                })
                            per_account_messages[key] = messages
                            print(f"[IDLE] {key} → {len(messages)} messages")

                        else:
                            # No new mail, IDLE timeout
                            mail.send(b"DONE\r\n")
                            while True:
                                if mail.readline().startswith(tag.encode()):
                                    break

                except Exception as e_inner:
                    print(f"[IDLE INNER ERROR] {key} loop error: {e_inner}")
                    # Gracefully continue to reconnect

        except Exception as e_outer:
            print(f"[IDLE ERROR] {key} failed to connect or login: {e_outer}")

        print(f"[IDLE] {key} sleeping before retry...")
        time.sleep(60)

@app.route('/inbox')
def inbox():
    print("[INBOX] Merging messages from all inboxes...")
    merged = []
    print(per_account_messages)
    for batch in per_account_messages.values():
        merged.extend(batch)
    print(f"[INBOX] Returning {len(merged)} cached messages")
    return render_template_string(INBOX_TEMPLATE, messages=merged)

@app.route('/reply', methods=['POST'])
def reply():
    try:
        to_email = request.form['to']
        body = request.form['body']
        original_subject = request.form.get('subject', '')  # Get original subject if available
        
        # Get an available account for sending
        account = get_available_account()
        if not account:
            return "No available email accounts. Please try again later.", 503
            
        # Create the email message
        msg = MIMEMultipart()
        msg['From'] = account[1]  # email
        msg['To'] = to_email
        
        # Set proper threading headers
        msg_id = f"<{uuid.uuid4()}@{account[1].split('@')[1]}>"
        msg['Message-ID'] = msg_id
        msg['In-Reply-To'] = request.form.get('message_id', '')  # Get original message ID if available
        msg['References'] = request.form.get('references', '')  # Get original references if available
        
        # Set subject with proper threading
        if original_subject:
            if not original_subject.startswith('Re:'):
                msg['Subject'] = f"Re: {original_subject}"
            else:
                msg['Subject'] = original_subject
        else:
            msg['Subject'] = 'Re: Follow-up'
        
        # Create the reply body with original message quoted
        original_body = request.form.get('original_body', '')
        if original_body:
            quoted_body = f"\n\nOn {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, {to_email} wrote:\n> " + original_body.replace('\n', '\n> ')
            full_body = body + quoted_body
        else:
            full_body = body
            
        msg.attach(MIMEText(full_body, 'plain'))
        
        # Send the email
        with smtplib.SMTP_SSL(account[2], account[3], timeout=10) as server:  # smtp_host, smtp_port
            server.login(account[4], account[5])  # smtp_user, smtp_pass
            server.send_message(msg)
            
        # Update account usage and mark email as replied
        with sqlite3.connect(DB_PATH) as conn:
            # Update account usage
            conn.execute("UPDATE accounts SET sent_today = sent_today + 1 WHERE id=?", (account[0],))
            
            # Mark the original email as replied
            conn.execute("UPDATE emails SET replied=1, replied_at=? WHERE email=?", 
                        (datetime.utcnow(), to_email))
            conn.commit()
            
        return redirect('/inbox')
    except Exception as e:
        print(f"[ERROR] Failed to send reply: {e}")
        return f"Failed to send reply: {str(e)}", 500 

if __name__ == '__main__':
    # Start scheduler job
    background_inbox_fetch_parallel()  # Only run once

    # send_next_email()
    app.run()
    # Clean shutdown
    atexit.register(lambda: scheduler.shutdown(wait=False))


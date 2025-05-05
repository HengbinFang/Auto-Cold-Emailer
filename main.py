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
from flask import Flask, request, redirect, render_template, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
import uuid
import asyncio
import time
import atexit
import select
import threading
import email.utils
import random
import re

UPLOAD_FOLDER = 'uploads'
DB_PATH = 'email_tool.db'
TRACKING_PIXEL_PATH = 'pixel.png'
SEND_INTERVAL_MINUTES = 10  # base interval for each email
MIN_WAIT_MINUTES = 5  # minimum wait time
MAX_WAIT_MINUTES = 15  # maximum wait time

# Global variable for storing per-account messages
per_account_messages = {}

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
scheduler = BackgroundScheduler()
scheduler.start()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Database setup
with sqlite3.connect(DB_PATH) as conn:
    # Create campaigns table
    conn.execute('''CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY,
        name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Create emails table with campaign_id
    conn.execute('''CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY,
        uid TEXT,
        email TEXT,
        subject TEXT,
        message TEXT,
        sent_at TIMESTAMP,
        opened INTEGER DEFAULT 0,
        opened_at TIMESTAMP,
        replied INTEGER DEFAULT 0,
        replied_at TIMESTAMP,
        account_email TEXT,
        next_send_time TIMESTAMP,
        is_sending INTEGER DEFAULT 0,
        campaign_id INTEGER,
        message_id TEXT,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
    )''')
    

    # Create accounts table
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
    
    # Add campaign_id column if it doesn't exist
    try:
        conn.execute("ALTER TABLE emails ADD COLUMN campaign_id INTEGER")
        
        # Create a default campaign for existing emails
        cursor = conn.execute("INSERT INTO campaigns (name) VALUES (?)", 
                            ("Default Campaign",))
        default_campaign_id = cursor.lastrowid
        
        # Update existing emails to belong to the default campaign
        conn.execute("UPDATE emails SET campaign_id = ? WHERE campaign_id IS NULL", 
                    (default_campaign_id,))
    except sqlite3.OperationalError:
        # Column already exists, ignore error
        pass
    
    conn.commit()

@app.route('/', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        print("[DEBUG] Request files:", request.files)
        print("[DEBUG] Request form:", request.form)
        if 'file' not in request.files:
            print("[DEBUG] No file in request.files")
            return "No file uploaded", 400
        file = request.files['file']
        if file.filename == '':
            print("[DEBUG] Empty filename")
            return "No file uploaded", 400
        print("[DEBUG] File:", file)
        print("[DEBUG] Filename:", file.filename)
        
        # Add timestamp to filename to prevent duplicates
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_name, ext = os.path.splitext(secure_filename(file.filename))
        filename = f"{base_name}_{timestamp}{ext}"
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)  # Save the file immediately
        with open(filepath, newline='', encoding='utf-8-sig') as csvfile:  # handles BOM
            reader = csv.reader(csvfile)
            headers = next(reader)
        return render_template('select.html', cols=headers, filename=filename)
    return render_template('upload.html')

@app.route('/select', methods=['POST'])
def select_columns():
    try:
        email_col = request.form['email_col']
        subject_col = request.form['subject_col']
        msg_col = request.form['msg_col']
        filename = request.form['filename']
        campaign_name = request.form.get('campaign_name', f"Campaign {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        enable_tracking = request.form.get('enable_tracking', 'on') == 'on'  # Default to on if not specified
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        # Create new campaign
        with sqlite3.connect(DB_PATH) as conn: 
            cursor = conn.execute("INSERT INTO campaigns (name) VALUES (?)", (campaign_name,))
            campaign_id = cursor.lastrowid
            conn.commit()

        inserted = 0
        with open(filepath, newline='', encoding='utf-8-sig') as csvfile:
            reader = csv.DictReader(csvfile)
            rows = list(reader)  # Read all rows first
            
            # Get number of available accounts
            with sqlite3.connect(DB_PATH) as conn:
                available_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            
            for i, row in enumerate(rows):
                uid = str(uuid.uuid4())
                email = row.get(email_col)
                subject = row.get(subject_col) if subject_col else None  # Make subject optional
                msg = row.get(msg_col)
                if not email or not msg:
                    continue
                
                # Add tracking pixel if enabled
                if enable_tracking:
                    tracking_pixel = f'<img src=".../pixel.gif?uid={uid}" width="1" height="1">'
                    msg = f"{msg}\n{tracking_pixel}"
                
                # Calculate which batch this email is in
                batch_number = i // available_accounts
                next_send_time = datetime.utcnow() + timedelta(minutes=batch_number * SEND_INTERVAL_MINUTES)
                
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("INSERT INTO emails (uid, email, subject, message, next_send_time, campaign_id) VALUES (?, ?, ?, ?, ?, ?)", 
                               (uid, email, subject, msg, next_send_time, campaign_id))
                    conn.commit()
                    inserted += 1

        print(f"[SELECT] Inserted {inserted} emails into queue for campaign {campaign_name}")
        return redirect('/dashboard')
    except Exception as e:
        print(f"[ERROR] Failed to process file: {e}")
        return f"Error processing file: {str(e)}", 500

@app.route('/accounts_upload', methods=['POST'])
def upload_accounts():
    file = request.files['file']
    # Add timestamp to filename to prevent duplicates
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_name, ext = os.path.splitext(secure_filename(file.filename))
    filename = f"{base_name}_{timestamp}{ext}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
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

def get_available_accounts():
    today = date.today()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('''SELECT * FROM accounts
                               WHERE (last_sent IS NULL OR last_sent < ? OR sent_today < daily_limit)
                               ORDER BY sent_today ASC''', (today,)).fetchall()
        if not rows:
            return None
        
        # Update last_sent date for accounts that need it
        for account in rows:
            if account[11] != today:
                conn.execute("UPDATE accounts SET last_sent=?, sent_today=0 WHERE id=?", (today, account[0]))
        conn.commit()
        
        return rows

def send_next_email():
    print("[SCHEDULER] Checking for unsent emails...")
    with sqlite3.connect(DB_PATH) as conn:
        # Get all available accounts
        accounts = get_available_accounts()
        if not accounts:
            print("[SCHEDULER] No available accounts with quota.")
            return

        # For each available account, try to send one email
        accounts = [accounts[2]]
        print(accounts)
        for account in accounts:
            # Get the next email that's ready to send
            now = datetime.utcnow()
            row = conn.execute('''SELECT id, uid, email, subject, message FROM emails 
                                WHERE sent_at IS NULL 
                                AND next_send_time <= ? 
                                AND is_sending = 0
                                LIMIT 1''', (now,)).fetchone()
            
            if not row:
                continue

            id_, uid, to_email, subject, message = row
            
            # Mark email as being sent to prevent duplicate sends
            conn.execute("UPDATE emails SET is_sending = 1 WHERE id = ?", (id_,))
            conn.commit()

            smtp_host = account[2]
            smtp_port = account[3]
            smtp_user = account[4]
            smtp_pass = account[5]

            full_msg = MIMEMultipart("alternative")
            full_msg['Subject'] = subject or "Hello from Hengbin"
            full_msg['From'] = account[1]
            full_msg['To'] = to_email
            
            # Generate a proper Message-ID
            msg_id = f"{uuid.uuid4()}@{account[1].split('@')[1]}"
            full_msg['Message-ID'] = f"<{msg_id}>"
            
            html_msg = message
            full_msg.attach(MIMEText(html_msg, 'html'))

            try:
                print(f"[SEND] Attempting to send to {to_email} using {account[1]} ({smtp_host}:{smtp_port})")
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                    server.set_debuglevel(1)
                    print("Connected. Logging in...")
                    server.login(smtp_user, smtp_pass)
                    print("Logged in.")
                    server.send_message(full_msg)

                conn.execute(
                    "UPDATE emails SET sent_at=?, account_email=?, is_sending=0, message_id=? WHERE id=?",
                    (now, account[1], msg_id, id_)
                )
                conn.execute("UPDATE accounts SET sent_today = sent_today + 1 WHERE id=?", (account[0],))
                conn.commit()
                print(f"[SUCCESS] Sent to {to_email}")
            except Exception as e:
                print(f"[ERROR] Failed to send to {to_email} via {account[1]}: {e}")
                # Reset is_sending flag on error
                conn.execute("UPDATE emails SET is_sending = 0 WHERE id = ?", (id_,))
                conn.commit()

# (You can add 'account_email' field in the inbox/reply tracking if needed)
@app.route('/pixel.gif')
def tracking_pixel():
    uid = request.args.get('uid')
    if uid:
        with sqlite3.connect(DB_PATH) as conn:
            # Check if this email belongs to a campaign and hasn't been opened yet
            cursor = conn.execute('''SELECT id, campaign_id FROM emails 
                                  WHERE uid=? AND sent_at IS NOT NULL 
                                  AND opened=0''', (uid,))
            result = cursor.fetchone()
            
            if result:
                email_id, campaign_id = result
                # Only mark as opened if it belongs to a campaign
                if campaign_id:
                    conn.execute("UPDATE emails SET opened=1, opened_at=? WHERE id=?", 
                               (datetime.utcnow(), email_id))
                    conn.commit()
                    print(f"[OPEN DETECTED] Email {email_id} from campaign {campaign_id} was opened")
    return send_file(TRACKING_PIXEL_PATH, mimetype='image/gif')

@app.route('/dashboard')
def dashboard():
    with sqlite3.connect(DB_PATH) as conn:
        # Get all campaigns with their stats
        campaigns = conn.execute('''SELECT 
            c.id, c.name, c.created_at,
            COUNT(e.id) as total,
            SUM(CASE WHEN e.sent_at IS NOT NULL THEN 1 ELSE 0 END) as sent,
            SUM(e.opened) as opened,
            SUM(e.replied) as replied
            FROM campaigns c
            LEFT JOIN emails e ON c.id = e.campaign_id
            GROUP BY c.id
            ORDER BY c.created_at DESC''').fetchall()
        
        # Get overall stats
        total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM emails WHERE sent_at IS NOT NULL").fetchone()[0]
        opened = conn.execute("SELECT COUNT(*) FROM emails WHERE opened=1").fetchone()[0]
        replied = conn.execute("SELECT COUNT(*) FROM emails WHERE replied=1").fetchone()[0]
    
    return render_template('dashboard.html', 
                         campaigns=campaigns,
                         total=total,
                         sent=sent,
                         opened=opened,
                         replied=replied)

def background_inbox_fetch_parallel():
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        accounts = conn.execute("SELECT imap_host, imap_port, imap_user, imap_pass FROM accounts").fetchall()
    
    def launch_all(accounts):
        for host, port, user, pwd in accounts:
            if user == "...":
                threading.Thread(target=persistent_check_loop, args=(host, port, user, pwd), daemon=True).start()
                break # just to try 1
    
    threading.Thread(target=launch_all, args=(accounts,), daemon=True).start()

def parse_email_message(msg_data):
    """Helper function to parse email message data into a structured format."""
    from_, subject, message_id, references, body = '', '', '', '', ''
    print(f"[EMAIL PARSING] Raw message data: {msg_data}")
    
    for part in msg_data:
        if isinstance(part, tuple):
            raw = part[1].decode(errors='ignore')
            print(f"[EMAIL PARSING] Raw header part: {raw}")
            
            # Check each header separately
            if 'From:' in raw:
                from_ = parse_email_address(raw.split('From:')[-1].strip())
                print(f"[EMAIL PARSING] Found From: {from_}")
            
            if 'Subject:' in raw:
                subject = raw.split('Subject:')[-1].strip()
                print(f"[EMAIL PARSING] Found Subject: {subject}")
            
            if 'Message-ID:' in raw:
                message_id = raw.split('Message-ID:')[-1].strip()
                if message_id:
                    message_id = message_id.strip()
                    if not message_id.startswith('<'):
                        message_id = f"<{message_id}>"
                    if not message_id.endswith('>'):
                        message_id = f"{message_id}>"
                print(f"[EMAIL PARSING] Found Message-ID: {message_id}")
            
            if 'References:' in raw:
                references = raw.split('References:')[-1].strip()
                print(f"[EMAIL PARSING] Found References header: {references}")
                if references:
                    # Extract only message IDs (text between < and >)
                    refs = re.findall(r'<([^>]+)>', references)
                    # Clean up each reference
                    refs = [f"<{ref}>" if not ref.startswith('<') else ref for ref in refs]
                    refs = [ref if ref.endswith('>') else f"{ref}>" for ref in refs]
                    references = ' '.join(refs)
                    print(f"[EMAIL PARSING] Processed References: {references}")
                    print(f"[EMAIL PARSING] References list: {refs}")
            
            if not any(header in raw for header in ['From:', 'Subject:', 'Message-ID:', 'References:']):
                body = raw.strip()
                print(f"[EMAIL PARSING] Found body content")
    
    return {
        'from': from_,
        'subject': subject,
        'message_id': message_id,
        'references': references,
        'body': body
    }

def check_reply_tracking(references, from_):
    """Helper function to check and update reply tracking."""
    if not references:  # If no references, not a reply
        return
        
    print(f"[REPLY TRACKING] Found references: {references}")
    with sqlite3.connect(DB_PATH) as conn:
        # Look up the original email by its message ID in the references
        for ref in references.split():
            ref = ref.strip('<>')  # Remove angle brackets
            print(f"[REPLY TRACKING] Checking reference: {ref}")
            print(f"[REPLY TRACKING] SQL Query: SELECT id, campaign_id FROM emails WHERE message_id=? AND sent_at IS NOT NULL AND replied=0")
            print(f"[REPLY TRACKING] Query parameters: message_id={ref}")
            cursor = conn.execute('''SELECT id, campaign_id FROM emails 
                                  WHERE message_id=? AND sent_at IS NOT NULL 
                                  AND replied=0''', (ref,))
            result = cursor.fetchone()
            if result:
                email_id, campaign_id = result
                print(f"[REPLY TRACKING] Found matching email: id={email_id}, campaign_id={campaign_id}")
                # Mark the original email as replied
                conn.execute('''UPDATE emails SET replied=1, replied_at=? 
                              WHERE id=?''', (datetime.utcnow(), email_id))
                conn.commit()
                print(f"[REPLY DETECTED] Email {email_id} from campaign {campaign_id} was replied to by {from_}")
                break
            else:
                print(f"[REPLY TRACKING] No matching email found for reference: {ref}")
                # Debug: Check if the email exists at all
                cursor = conn.execute("SELECT id, message_id, sent_at FROM emails WHERE message_id=?", (ref,))
                debug_result = cursor.fetchone()
                if debug_result:
                    print(f"[REPLY TRACKING DEBUG] Found email but not matching criteria: id={debug_result[0]}, message_id={debug_result[1]}, sent_at={debug_result[2]}")
                else:
                    print(f"[REPLY TRACKING DEBUG] No email found with message_id={ref}")

def persistent_check_loop(host, port, user, pwd):
    key = f"{user}@{host}:{port}"
    print(f"[IDLE LOOP STARTED] {key} is now running in persistent IDLE mode.")

    retry_delay = 10  # Initial retry delay in seconds
    max_delay = 3600  # Max delay of 1 hour

    while True:
        try:
            with imaplib.IMAP4_SSL(host, port) as mail:
                mail.debug = 4
                mail.login(user, pwd)
                mail.select("inbox")
                status, folders = mail.list()
                for folder in folders:
                    print(folder.decode())
                # Reset retry delay on successful connection
                retry_delay = 60

                # Initially load all messages
                typ, data = mail.search(None, 'ALL')
                messages = []
                for num in data[0].split():
                    typ, msg_data = mail.fetch(num, '(BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID REFERENCES)] BODY[TEXT])')
                    message = parse_email_message(msg_data)
                    check_reply_tracking(message['references'], message['from'])
                    messages.append(message)
                    
                per_account_messages[key] = messages
                print(f"[INITIAL LOAD] {key} → {len(messages)} messages")

                try:
                    while True:
                        print(f"[IDLE] {key} entering IDLE...")
                        tag = mail._new_tag()
                        mail.send(f"{tag} IDLE\r\n".encode())

                        if not mail.readline().startswith(b'+'):
                            raise Exception("IDLE not acknowledged")

                        if select.select([mail.sock], [], [], 1740)[0]:
                            print(f"[IDLE] {key} change detected!")
                            mail.send(b"DONE\r\n")

                            # Read until DONE acknowledged
                            while True:
                                line = mail.readline()
                                if b"Idle completed" in line:
                                    break

                            time.sleep(0.2)

                            # Reload all messages when changes detected
                            typ, data = mail.search(None, 'UNSEEN')
                            new_messages = []
                            for num in data[0].split():
                                typ, msg_data = mail.fetch(num, '(BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID REFERENCES)] BODY[TEXT])')
                                message = parse_email_message(msg_data)
                                check_reply_tracking(message['references'], message['from'])
                                new_messages.append(message)

                            # Only add messages that aren't already in the list
                            existing_message_ids = {msg['message_id'] for msg in per_account_messages[key]}
                            for msg in new_messages:
                                if msg['message_id'] not in existing_message_ids:
                                    per_account_messages[key].append(msg)
                            print(f"[IDLE] {key} → Added {len(new_messages)} new messages")

                        else:
                            # IDLE timed out (29 mins), flush DONE
                            mail.send(b"DONE\r\n")
                            while True:
                                line = mail.readline()
                                if b"Idle completed" in line:
                                    break

                except Exception as e_inner:
                    print(f"[IDLE INNER ERROR] {key} loop error: {e_inner}")
                    raise

        except Exception as e_outer:
            print(f"[IDLE ERROR] {key} failed to connect or login: {e_outer}")
            print(f"[IDLE] {key} sleeping for {retry_delay} seconds before retry...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)

@app.route('/inbox')
def inbox():
    print("[INBOX] Merging messages from all inboxes...")
    merged = []
    print(per_account_messages)
    for batch in per_account_messages.values():
        merged.extend(batch)
    print(f"[INBOX] Returning {len(merged)} cached messages")
    return render_template('inbox.html', messages=merged)

    
@app.route('/reply', methods=['POST'])
def reply():
    try:
        print("[REPLY] Starting reply process...")
        to_email = parse_email_address(request.form['to'])  # Use the helper function
        body = request.form['body']
        original_subject = request.form.get('subject', '').split('\r\n')[0].strip()  # Get first line only
        
        print(f"[REPLY] Sending to: {to_email}")
        print(f"[REPLY] Subject: {original_subject}")
        
        # Get an available account for sending
        account = get_available_account()
        if not account:
            print("[REPLY] No available accounts found")
            return "No available email accounts. Please try again later.", 503
            
        print(f"[REPLY] Using account: {account[1]}")
            
        # Create the email message
        msg = MIMEMultipart()
        msg['From'] = account[1]  # email
        msg['To'] = to_email
        
        # Set proper threading headers
        msg_id = f"{uuid.uuid4()}@{account[1].split('@')[1]}"
        msg['Message-ID'] = f"<{msg_id}>"
        
        # Clean up message ID and references
        in_reply_to = request.form.get('message_id', '').split('\r\n')[0].strip()
        references = request.form.get('references', '').split('\r\n')[0].strip()
        
        # Ensure Message-ID is properly formatted
        if in_reply_to and not in_reply_to.startswith('<'):
            in_reply_to = f"<{in_reply_to}>"
        
        # Build References header
        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
            print(f"[REPLY] In-Reply-To: {in_reply_to}")
            
            # Combine References with In-Reply-To
            if references:
                # Split references into individual message IDs
                refs = [ref.strip() for ref in references.split()]
                # Clean up each reference
                refs = [f"<{ref}>" if not ref.startswith('<') else ref for ref in refs]
                refs = [ref if ref.endswith('>') else f"{ref}>" for ref in refs]
                # Remove any non-message-id content
                refs = [ref for ref in refs if ref.startswith('<') and ref.endswith('>')]
                # Add the current In-Reply-To if not already in references
                if in_reply_to not in refs:
                    refs.append(in_reply_to)
                msg['References'] = ' '.join(refs)
            else:
                msg['References'] = in_reply_to
            print(f"[REPLY] References: {msg['References']}")
        
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
        
        print("[REPLY] Attempting to send email...")
        # Send the email
        with smtplib.SMTP_SSL(account[2], account[3], timeout=10) as server:  # smtp_host, smtp_port
            print(f"[REPLY] Connected to SMTP server {account[2]}:{account[3]}")
            server.login(account[4], account[5])  # smtp_user, smtp_pass
            print("[REPLY] Successfully logged in to SMTP server")
            server.send_message(msg)
            print("[REPLY] Email sent successfully")
            
        print("[REPLY] Updating database...")
        # Update account usage
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE accounts SET sent_today = sent_today + 1 WHERE id=?", (account[0],))
            conn.commit()
            print(f"[REPLY] Updated sent_today count for account {account[0]}")
            
        print("[REPLY] Reply process completed successfully")
        return redirect('/inbox')
    except Exception as e:
        print(f"[REPLY ERROR] Failed to send reply: {e}")
        print(f"[REPLY ERROR] Full error details: {traceback.format_exc()}")
        return f"Failed to send reply: {str(e)}", 500

def parse_email_address(addr_str):
    """Parse an email address string into a clean email address."""
    if not addr_str:
        return ''
    try:
        # Try to parse using email.utils
        name, addr = email.utils.parseaddr(addr_str)
        if addr:
            return addr
        # If that fails, try to extract from common formats
        if '<' in addr_str and '>' in addr_str:
            return addr_str.split('<')[-1].split('>')[0].strip()
        return addr_str.strip()
    except:
        return addr_str.strip()

if __name__ == '__main__':
    # Start scheduler job
    scheduler.add_job(send_next_email, 'interval', minutes=1, id='send_task')
    send_next_email()
    background_inbox_fetch_parallel()  # Only run once
    app.run()
    # Clean shutdown
    atexit.register(lambda: scheduler.shutdown(wait=False))


import imaplib
import base64

imaplib.Commands['AUTH'] = ('AUTH', 'AUTHENTICATE', 'SELECT')  # override if needed

host = "..."
port = 993
user = "..."
pwd = "..."


with imaplib.IMAP4_SSL(host, port) as mail:
    mail.debug = 4
    tag = mail._new_tag()
    mail.send(f"{tag} AUTHENTICATE PLAIN\r\n".encode("utf-8"))
    # ✅ Wait for `+` from server
    line = mail.readline()
    print("SERVER SENT:", line)
    if not line.startswith(b'+'):
        raise Exception(f"Expected '+', got: {line}")
    # ✅ Send base64 encoded creds
    auth_string = f'\0{user}\0{pwd}'.encode('utf-8')
    encoded = base64.b64encode(auth_string).decode('utf-8')
    print("Sending base64:", encoded)
    mail.send(f"{encoded}\r\n".encode("utf-8"))
    # ✅ Wait for final IMAP response
    typ, data = mail._get_tagged_response(tag)
    print("FINAL:", typ, data)
    if typ == 'OK':
        print("✅ Logged in.")
    else:
        print("❌ Login failed:", data)


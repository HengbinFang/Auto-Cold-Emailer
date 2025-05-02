import imaplib
import base64

imaplib.Commands['AUTH'] = ('AUTH', 'AUTHENTICATE', 'SELECT')  # override if needed

host = "..."
port = ...
user = "..."
pwd = "..."


try:
    with imaplib.IMAP4_SSL(host, port) as mail:
        mail.debug = 4  # Show IMAP communication

        tag = mail._new_tag()
        mail.send(f"{tag} AUTHENTICATE PLAIN\r\n".encode("utf-8"))

        # Wait for '+'
        resp = mail._get_response()
        plus_prompt = resp[1][0]
        print("SERVER SENT:", plus_prompt)
        if not plus_prompt.startswith(b'+'):
            raise Exception("Expected '+', got:", plus_prompt)

        # Send base64
        auth_string = f'\0{user}\0{pwd}'.encode('utf-8')
        encoded = base64.b64encode(auth_string).decode('utf-8')
        print("Sending base64:", encoded)
        mail.send(f"{encoded}\r\n".encode("utf-8"))

        # Final response
        typ, data = mail._get_tagged_response(tag)
        print("FINAL:", typ, data)
        if typ == 'OK':
            print("✅ Logged in.")
        else:
            print("❌ Login failed:", data)

except Exception as e:
    print("❌ Exception:", e)
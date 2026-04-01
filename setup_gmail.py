"""
Setup Gmail OAuth untuk S&P 500 Recommender.

Jika sudah punya token.json dari project saham-recommender (IDX),
cukup copy file tersebut ke folder ini — tidak perlu setup ulang.

Jika belum punya, jalankan script ini sekali:
    python setup_gmail.py
"""

import os
import sys

TOKEN_FILE       = os.path.join(os.path.dirname(__file__), "token.json")
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def check_dependencies():
    missing = []
    try: import google.auth
    except ImportError: missing.append("google-auth")
    try: import google_auth_oauthlib
    except ImportError: missing.append("google-auth-oauthlib")
    try: import googleapiclient
    except ImportError: missing.append("google-api-python-client")
    if missing:
        print(f"[ERROR] Missing packages: {', '.join(missing)}")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)


def setup_oauth():
    check_dependencies()
    from google_auth_oauthlib.flow import InstalledAppFlow

    # Cek apakah token dari project IDX bisa dipakai
    if os.path.exists(TOKEN_FILE):
        print(f"[INFO] token.json sudah ada di folder ini.")
        print("[INFO] Jika ini adalah copy dari project saham-recommender, langsung jalankan main.py.")
        ans = input("Re-generate token baru? (y/N): ").strip().lower()
        if ans != "y":
            print("[OK] Menggunakan token yang sudah ada.")
            return

    if not os.path.exists(CREDENTIALS_FILE):
        print("\n[ERROR] credentials.json tidak ditemukan!")
        print("\nOpsi 1 — Copy dari project saham-recommender:")
        print("  copy ../saham-recommender/credentials.json .")
        print("  copy ../saham-recommender/token.json .")
        print("\nOpsi 2 — Buat baru dari Google Cloud Console:")
        print("  1. https://console.cloud.google.com/")
        print("  2. APIs & Services > Library > Gmail API > Enable")
        print("  3. Credentials > OAuth client ID > Desktop app > Download JSON")
        print("  4. Simpan sebagai credentials.json di folder ini")
        sys.exit(1)

    print("[INFO] Opening browser for Gmail OAuth login...")
    try:
        flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as e:
        print(f"[ERROR] OAuth failed: {e}")
        sys.exit(1)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\n[OK] Token saved: {TOKEN_FILE}")

    # Verify
    try:
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email   = profile.get("emailAddress", "unknown")
        print(f"[OK] Connected as: {email}")
        print(f'\n[ACTION] Update main.py:\n  SENDER_EMAIL    = "{email}"')
        print(f'  RECIPIENT_EMAIL = "your_recipient@email.com"')
    except Exception as e:
        print(f"[WARNING] Verification failed: {e}")


if __name__ == "__main__":
    setup_oauth()

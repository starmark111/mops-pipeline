"""一次性 Google Drive OAuth 授權腳本
執行後會開啟瀏覽器，用 lkyee1219@gmail.com 登入授權，
完成後產生 token.json，之後 Daily_Important_Event_v3.py 就能自動上傳。
"""
import os, sys

SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:
    print("請先執行：pip3 install google-api-python-client google-auth-oauthlib --break-system-packages")
    sys.exit(1)

if not os.path.exists(CREDS_PATH):
    print(f"❌ 找不到 {CREDS_PATH}")
    sys.exit(1)

print("🌐 開啟瀏覽器進行 Google 授權，請用 lkyee1219@gmail.com 登入...")
flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_PATH, "w") as f:
    f.write(creds.to_json())

print(f"✅ 授權完成！token.json 已儲存到 {TOKEN_PATH}")

# 測試：列出 Drive 根目錄檔案
service = build("drive", "v3", credentials=creds)
results = service.files().list(pageSize=5, fields="files(name)").execute()
files = results.get("files", [])
print(f"✅ Drive 連線測試成功，帳號下有 {len(files)} 個檔案（前5筆）")
for f in files:
    print(f"   - {f['name']}")

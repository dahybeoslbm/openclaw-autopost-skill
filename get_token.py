from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = ['https://www.googleapis.com/auth/business.manage']

flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json', SCOPES,
    # Bắt buộc có access_type=offline mới lấy được refresh_token
)

# Mở browser để login 1 lần
creds = flow.run_local_server(
    port=0,
    access_type='offline',
    prompt='consent'   # Buộc Google trả refresh_token dù đã từng login
)

print("✅ Access token:", creds.token)
print("✅ Refresh token:", creds.refresh_token)

# Lưu lại
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print("✅ Đã lưu token.json")
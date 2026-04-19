import os
import json
import datetime
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# 從環境變數讀取資料
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")
CHAT_ID = os.environ.get("CHAT_ID")  # 由 GitHub Actions 傳入
TEXT = os.environ.get("TEXT")        # 由 GitHub Actions 傳入

SCOPES = ['https://www.googleapis.com/auth/calendar']

def send_telegram_message(text):
    """將結果傳回 Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def get_calendar_service():
    """取得 Google Calendar 授權"""
    if GOOGLE_TOKEN_JSON:
        token_info = json.loads(GOOGLE_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        return build('calendar', 'v3', credentials=creds)
    return None

def main():
    if not TEXT or not CHAT_ID:
        print("沒有收到文字或 CHAT_ID，結束程式。")
        return

    try:
        # 切割字串
        parts = TEXT.split(' ', 2)
        if len(parts) < 3:
            send_telegram_message("⚠️ 格式錯誤！請輸入例如：\n5/11 17:30 小孩看牙醫")
            return

        date_str, time_str, summary = parts
        month, day = map(int, date_str.split('/'))
        hour, minute = map(int, time_str.split(':'))
        year = datetime.datetime.now().year
        
        start_time = datetime.datetime(year, month, day, hour, minute)
        end_time = start_time + datetime.timedelta(hours=1)
        
        # 寫入日曆
        service = get_calendar_service()
        event = {
            'summary': summary,
            'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Taipei'},
            'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Taipei'},
        }
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        event_link = created_event.get('htmlLink')
        
        # 回報成功
        success_msg = f"✅ 已成功加入行事曆！\n📅 標題：{summary}\n⏰ 時間：{start_time.strftime('%Y-%m-%d %H:%M')}\n🔗 [點擊查看日曆]({event_link})"
        send_telegram_message(success_msg)

    except Exception as e:
        send_telegram_message(f"❌ 發生錯誤：{str(e)}")

if __name__ == '__main__':
    main()
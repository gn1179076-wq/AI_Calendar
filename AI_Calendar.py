# -*- coding: utf-8 -*-
import os
import json
import re
import datetime
import requests
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 環境變數 ──
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
CHAT_ID           = os.environ.get("CHAT_ID")
ACTION            = os.environ.get("ACTION", "create")
TEXT              = os.environ.get("TEXT", "")
EVENT_ID          = os.environ.get("EVENT_ID", "")
FAMILY_CAL_ID     = os.environ.get("FAMILY_CAL_ID")
LINE_TOKEN        = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
SOURCE = os.environ.get("SOURCE", "discord")

TZ = ZoneInfo("Asia/Taipei")
SCOPES = ['https://www.googleapis.com/auth/calendar']

# 格式 1：M/D HH:MM
RE_STRICT = re.compile(
    r'^\s*(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$'
)
# 格式 2：今天/明天/後天 HH:MM
RE_RELATIVE = re.compile(
    r'^\s*(?P<rel>今天|明天|後天)\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$'
)

def log(msg):
    print(f"[AI_Calendar] {msg}", flush=True)

# ==========================================
# 推播函式庫 (Discord / Telegram / LINE)
# ==========================================
def send_discord(title, embed_fields=None, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        log("❌ 找不到 DISCORD_WEBHOOK_URL")
        return
    
    payload = {
        "username": "AI 行程管家",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2693/2693507.png",
        "embeds": [{
            "title": title,
            "color": color,
            "fields": embed_fields or [],
            "footer": {"text": f"來源分支: {os.getenv('GITHUB_REF_NAME', 'main')}"},
            "timestamp": datetime.datetime.now(TZ).isoformat()
        }]
    }
    
    # 如果是刪除操作，把顏色換成紅色
    if color == 3066993 and ("刪除" in title or "❌" in title):
        payload["embeds"][0]["color"] = 15158332

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        log(f"Discord 狀態: {res.status_code}")
    except Exception as e:
        log(f"Discord 連線異常: {e}")

def send_telegram(text, reply_markup=None):
    if not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload, timeout=15)

def send_line(text):
    if not CHAT_ID or not LINE_TOKEN: return
    url = "https://api.line.me/v2/bot/message/push"
    body = {"to": CHAT_ID, "messages": [{"type": "text", "text": text}]}
    requests.post(url, json=body, headers={
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json"
    }, timeout=15)

def notify(msg_title, embed_fields=None, raw_text=None):
    # Discord 優先顯示卡片，其餘平台顯示純文字
    if SOURCE == "discord":
        send_discord(msg_title, embed_fields)
    elif SOURCE == "line":
        send_line(raw_text or msg_title)
    else:
        send_telegram(raw_text or msg_title)

# ==========================================
# 日曆邏輯
# ==========================================
def get_calendar():
    token_info = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    return build('calendar', 'v3', credentials=creds)

def try_parse_strict(text):
    now = datetime.datetime.now(TZ)
    rel_m = RE_RELATIVE.match(text)
    if rel_m:
        g = rel_m.groupdict()
        offset = {"今天": 0, "明天": 1, "後天": 2}
        base = now + datetime.timedelta(days=offset[g["rel"]])
        start = base.replace(hour=int(g["sh"]), minute=int(g["sm"]), second=0, microsecond=0)
        end = start.replace(hour=int(g["eh"]), minute=int(g["em"])) if g["eh"] else start + datetime.timedelta(hours=1)
        return {"summary": g["title"], "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M")}
    m = RE_STRICT.match(text)
    if m:
        g = m.groupdict()
        year = int(g["year"]) if g["year"] else now.year
        start = datetime.datetime(year, int(g["month"]), int(g["day"]), int(g["sh"]), int(g["sm"]))
        if not g["year"] and start < now.replace(tzinfo=None): start = start.replace(year=year + 1)
        end = start.replace(hour=int(g["eh"]), minute=int(g["em"])) if g["eh"] else start + datetime.timedelta(hours=1)
        return {"summary": g["title"], "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M")}
    return None

def parse_with_gemini(text):
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    now = datetime.datetime.now(TZ)
    prompt = f"現在是 {now.strftime('%Y-%m-%d %H:%M %A')}。將訊息解析為 JSON: {{\"summary\": \"標題\", \"start\": \"YYYY-MM-DDTHH:MM\", \"end\": \"YYYY-MM-DDTHH:MM\"}}。訊息：{text}"
    resp = model.generate_content(prompt)
    raw = resp.text.strip().strip("`").lstrip("json").strip()
    return json.loads(raw)

def do_create():
    data = try_parse_strict(TEXT)
    mode = "⚡️ 高速"
    if data is None:
        try:
            data = parse_with_gemini(TEXT)
            mode = "🤖 AI"
        except Exception as e:
            notify(f"❌ 解析失敗: {e}"); return

    target_cal = 'primary'
    cal_label = "👤 個人日曆"
    family_keywords = ["老婆", "家", "我們", "家庭", "小孩", "晚餐", "產檢", "接送"]
    if FAMILY_CAL_ID and any(k in TEXT for k in family_keywords):
        target_cal = FAMILY_CAL_ID
        cal_label = "🏠 家庭日曆"

    start = datetime.datetime.fromisoformat(data["start"]).replace(tzinfo=TZ)
    end   = datetime.datetime.fromisoformat(data["end"]).replace(tzinfo=TZ)
    
    event = {
        'summary': data["summary"],
        'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'},
        'end':   {'dateTime': end.isoformat(),   'timeZone': 'Asia/Taipei'},
    }
    
    try:
        created = get_calendar().events().insert(calendarId=target_cal, body=event).execute()
        
        fields = [
            {"name": "📌 行程內容", "value": f"**{data['summary']}**", "inline": False},
            {"name": "⏰ 開始時間", "value": start.strftime('%m/%d (%a) %H:%M'), "inline": True},
            {"name": "⏳ 結束時間", "value": end.strftime('%m/%d (%a) %H:%M'), "inline": True},
            {"name": "📂 存入位置", "value": f"{cal_label} ({mode}解析)", "inline": False},
            {"name": "🔗 連結", "value": f"[在 Google 日曆中查看](<{created.get('htmlLink')}>)", "inline": False}
        ]
        
        notify(f"✅ 行程已存入日曆", embed_fields=fields, raw_text=f"✅ 已存入 {cal_label}\n📅 {data['summary']}\n⏰ {start.strftime('%m/%d %H:%M')}")
    except Exception as e:
        notify(f"❌ 存入失敗: {e}")

def do_list():
    now = datetime.datetime.now(TZ)
    arg = TEXT.strip().lower()
    if arg == "today":
        sd = now.replace(hour=0, minute=0, second=0); ed = sd + datetime.timedelta(days=1); label = "今天"
    else:
        try: days = int(arg) if arg else 7
        except: days = 7
        sd = now; ed = now + datetime.timedelta(days=days); label = f"未來 {days} 天"

    try:
        service = get_calendar()
        cals = [('primary', '👤', 'p')]
        if FAMILY_CAL_ID: cals.append((FAMILY_CAL_ID, '🏠', 'f'))
        
        event_list_text = []
        fields = []

        for cid, icon, short_code in cals:
            events = service.events().list(calendarId=cid, timeMin=sd.isoformat(), timeMax=ed.isoformat(),
                                           singleEvents=True, orderBy='startTime').execute().get('items', [])
            
            for ev in events:
                t = ev['start'].get('dateTime') or ev['start'].get('date')
                dt = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone(TZ)
                summary = ev.get('summary', '(無標題)')
                event_list_text.append(f"{icon} {dt.strftime('%m/%d %H:%M')} {summary}")
                fields.append({"name": f"{icon} {dt.strftime('%m/%d %H:%M')}", "value": summary, "inline": True})

        if not event_list_text:
            notify(f"📭 {label}沒有行程")
        else:
            notify(f"📋 {label}行程預覽", embed_fields=fields, raw_text=f"📋 {label}行程預覽\n\n" + "\n".join(event_list_text))
            
    except Exception as e:
        notify(f"❌ 無法讀取行程: {str(e)}")

def do_del():
    try:
        raw_data = EVENT_ID
        if "|" in raw_data:
            short_code, eid = raw_data.split("|", 1)
            cid = 'primary' if short_code == 'p' else FAMILY_CAL_ID
        else:
            cid, eid = 'primary', raw_data
            
        get_calendar().events().delete(calendarId=cid, eventId=eid).execute()
        notify("🗑 行程已成功刪除", color=15158332)
    except Exception as e:
        notify(f"❌ 刪除失敗: {e}")

def main():
    if not CHAT_ID and not DISCORD_WEBHOOK_URL: return
    try:
        if ACTION == "list": do_list()
        elif ACTION == "del": do_del()
        else: do_create()
    except Exception as e: log(f"Error: {e}")

if __name__ == '__main__':
    main()

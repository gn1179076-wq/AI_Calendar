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
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
# 你的 Cloudflare Worker 網址 (用來處理點擊刪除)
CF_WORKER_URL     = os.environ.get("CF_WORKER_URL", "https://your-worker.your-name.workers.dev")

ACTION            = os.environ.get("ACTION", "create")
TEXT              = os.environ.get("TEXT", "")
EVENT_ID          = os.environ.get("EVENT_ID", "")
FAMILY_CAL_ID     = os.environ.get("FAMILY_CAL_ID")
SOURCE            = "discord"  # 強制鎖定 Discord

TZ = ZoneInfo("Asia/Taipei")
SCOPES = ['https://www.googleapis.com/auth/calendar']

# 格式解析正則
RE_STRICT = re.compile(r'^\s*(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$')
RE_RELATIVE = re.compile(r'^\s*(?P<rel>今天|明天|後天)\s+(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$')

def log(msg):
    print(f"[AI_Calendar] {msg}", flush=True)

# ==========================================
# Discord 推播核心 (Embed 卡片版)
# ==========================================
def send_discord(title, embed_fields=None, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        log("❌ 找不到 DISCORD_WEBHOOK_URL")
        return
    
    payload = {
        "username": "Fiona 行程管家",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2693/2693507.png",
        "embeds": [{
            "title": title,
            "color": color,
            "fields": embed_fields or [],
            "footer": {"text": f"系統分支: {os.getenv('GITHUB_REF_NAME', 'main')} • 自動化同步"},
            "timestamp": datetime.datetime.now(TZ).isoformat()
        }]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        log(f"Discord 發送結果: {res.status_code}")
    except Exception as e:
        log(f"Discord 連線異常: {e}")

# ==========================================
# 日曆核心邏輯
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
            send_discord(f"❌ 解析失敗: {e}", color=15158332); return

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
            {"name": "📌 內容", "value": f"**{data['summary']}**", "inline": False},
            {"name": "⏰ 時間", "value": f"{start.strftime('%m/%d %H:%M')} - {end.strftime('%H:%M')}", "inline": True},
            {"name": "📂 位置", "value": f"{cal_label} ({mode})", "inline": True},
            {"name": "🔗 連結", "value": f"[在日曆中查看](<{created.get('htmlLink')}>)", "inline": False}
        ]
        send_discord("✅ 行程存入成功", embed_fields=fields)
    except Exception as e:
        send_discord(f"❌ 存入失敗: {e}", color=15158332)

def do_list():
    now = datetime.datetime.now(TZ)
    arg = TEXT.strip().lower()
    days = 1 if arg == "today" else (int(arg) if arg.isdigit() else 7)
    sd = now.replace(hour=0, minute=0, second=0) if days == 1 else now
    ed = sd + datetime.timedelta(days=days)

    try:
        service = get_calendar()
        cals = [('primary', '👤', 'p')]
        if FAMILY_CAL_ID: cals.append((FAMILY_CAL_ID, '🏠', 'f'))
        
        fields = []
        for cid, icon, short_code in cals:
            events = service.events().list(calendarId=cid, timeMin=sd.isoformat(), timeMax=ed.isoformat(),
                                           singleEvents=True, orderBy='startTime').execute().get('items', [])
            for ev in events:
                t = ev['start'].get('dateTime') or ev['start'].get('date')
                dt = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone(TZ)
                summary = ev.get('summary', '(無標題)')
                
                # 產生 Cloudflare Worker 刪除連結
                del_url = f"{CF_WORKER_URL}/del?sc={short_code}&eid={ev['id']}"
                
                fields.append({
                    "name": f"{icon} {dt.strftime('%m/%d %H:%M')}",
                    "value": f"{summary} **[[❌]](<{del_url}>)**",
                    "inline": True
                })

        if not fields:
            send_discord(f"📭 未來 {days} 天沒有行程")
        else:
            send_discord(f"📋 未來 {days} 天行程預覽", embed_fields=fields)
    except Exception as e:
        send_discord(f"❌ 讀取清單失敗: {e}", color=15158332)

def do_del():
    try:
        raw_data = EVENT_ID
        if "|" in raw_data:
            short_code, eid = raw_data.split("|", 1)
            cid = FAMILY_CAL_ID if short_code == 'f' else 'primary'
        else:
            cid, eid = 'primary', raw_data
            
        get_calendar().events().delete(calendarId=cid, eventId=eid).execute()
        send_discord("🗑 行程刪除成功", color=15158332)
    except Exception as e:
        send_discord(f"❌ 刪除失敗: {e}", color=15158332)

def main():
    if not DISCORD_WEBHOOK_URL: return
    if ACTION == "list": do_list()
    elif ACTION == "del": do_del()
    else: do_create()

if __name__ == '__main__':
    main()

import os
import json
import re
import datetime
import requests
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import google.generativeai as genai

# ── 環境變數 ──
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
CHAT_ID           = os.environ.get("CHAT_ID")
ACTION            = os.environ.get("ACTION", "create")
TEXT              = os.environ.get("TEXT", "")
EVENT_ID          = os.environ.get("EVENT_ID", "")
FAMILY_CAL_ID     = os.environ.get("FAMILY_CAL_ID") # 記得在 GitHub Secrets 設定

TZ = ZoneInfo("Asia/Taipei")
SCOPES = ['https://www.googleapis.com/auth/calendar']

# 格式 1：[YYYY/]M/D HH:MM[-HH:MM] 標題
RE_STRICT = re.compile(
    r'^\s*(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$'
)

# 格式 2：今天/明天/後天 HH:MM[-HH:MM] 標題
RE_RELATIVE = re.compile(
    r'^\s*(?P<rel>今天|明天|後天)\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$'
)

def log(msg):
    print(f"[AI_Calendar] {msg}", flush=True)

def send_telegram(text, reply_markup=None):
    if not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload, timeout=15)

def get_calendar():
    token_info = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    return build('calendar', 'v3', credentials=creds)

def try_parse_strict(text):
    """嘗試用固定格式解析（支援相對與絕對日期），成功回傳 dict"""
    now = datetime.datetime.now(TZ)
    
    # --- 嘗試相對日期 (今天/明天/後天) ---
    rel_m = RE_RELATIVE.match(text)
    if rel_m:
        g = rel_m.groupdict()
        offset = {"今天": 0, "明天": 1, "後天": 2}
        base_date = now + datetime.timedelta(days=offset[g["rel"]])
        try:
            start = base_date.replace(hour=int(g["sh"]), minute=int(g["sm"]), second=0, microsecond=0)
            if g["eh"]:
                end = start.replace(hour=int(g["eh"]), minute=int(g["em"]))
            else:
                end = start + datetime.timedelta(hours=1)
            return {"summary": g["title"], "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M")}
        except: return None

    # --- 嘗試絕對日期 (M/D) ---
    m = RE_STRICT.match(text)
    if m:
        g = m.groupdict()
        year = int(g["year"]) if g["year"] else now.year
        try:
            start = datetime.datetime(year, int(g["month"]), int(g["day"]), int(g["sh"]), int(g["sm"]))
            if not g["year"] and start < now.replace(tzinfo=None):
                start = start.replace(year=year + 1)
            if g["eh"]:
                end = start.replace(hour=int(g["eh"]), minute=int(g["em"]))
            else:
                end = start + datetime.timedelta(hours=1)
            return {"summary": g["title"], "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M")}
        except: return None
        
    return None

def parse_with_gemini(text):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    now = datetime.datetime.now(TZ)
    prompt = f"現在是 {now.strftime('%Y-%m-%d %H:%M %A')}。將訊息解析為 JSON: {{\"summary\": \"標題\", \"start\": \"YYYY-MM-DDTHH:MM\", \"end\": \"YYYY-MM-DDTHH:MM\"}}。訊息：{text}"
    resp = model.generate_content(prompt)
    raw = resp.text.strip().strip("`").lstrip("json").strip()
    return json.loads(raw)

def do_create():
    # 1. 優先用固定格式（省錢、快速）
    data = try_parse_strict(TEXT)
    mode = "快速解析"
    
    # 2. 不符合格式才呼叫 AI
    if data is None:
        try:
            data = parse_with_gemini(TEXT)
            mode = "AI 解析"
        except Exception as e:
            send_telegram(f"❌ 解析失敗：{TEXT}\n錯誤：{e}")
            return

    # ── 決定日曆 ID ──
    target_cal = 'primary'
    cal_label = "👤 個人"
    family_keywords = ["老婆", "家", "我們", "家庭", "小孩", "晚餐", "產檢", "接送"]
    
    if FAMILY_CAL_ID and any(k in TEXT for k in family_keywords):
        target_cal = FAMILY_CAL_ID
        cal_label = "🏠 家庭"

    start = datetime.datetime.fromisoformat(data["start"]).replace(tzinfo=TZ)
    end   = datetime.datetime.fromisoformat(data["end"]).replace(tzinfo=TZ)

    event = {
        'summary': data["summary"],
        'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'},
        'end':   {'dateTime': end.isoformat(),   'timeZone': 'Asia/Taipei'},
    }
    
    created = get_calendar().events().insert(calendarId=target_cal, body=event).execute()
    msg = (
        f"✅ 已加入{cal_label}日曆 ({mode})\n"
        f"📅 {data['summary']}\n"
        f"⏰ {start.strftime('%m/%d (%a) %H:%M')} – {end.strftime('%H:%M')}\n"
        f"🔗 [點此查看]({created.get('htmlLink')})"
    )
    send_telegram(msg)

def do_list():
    now = datetime.datetime.now(TZ)
    arg = TEXT.strip().lower()
    if arg == "today":
        sd = now.replace(hour=0, minute=0, second=0); ed = sd + datetime.timedelta(days=1); label = "今天"
    else:
        try: days = int(arg) if arg else 7
        except: days = 7
        sd = now; ed = now + datetime.timedelta(days=days); label = f"未來 {days} 天"

    service = get_calendar()
    cals = [('primary', '👤')]
    if FAMILY_CAL_ID: cals.append((FAMILY_CAL_ID, '🏠'))

    lines = [f"📋 {label}行程\n"]; keyboard = []
    for cid, icon in cals:
        events = service.events().list(calendarId=cid, timeMin=sd.isoformat(), timeMax=ed.isoformat(),
                                      singleEvents=True, orderBy='startTime').execute().get('items', [])
        for ev in events:
            t = ev['start'].get('dateTime') or ev['start'].get('date')
            dt = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone(TZ)
            lines.append(f"{icon} {dt.strftime('%m/%d %H:%M')} {ev.get('summary')}")
            keyboard.append([{"text": f"🗑 {icon} {ev.get('summary')[:12]}", "callback_data": f"del:{cid}|{ev['id']}"}])

    if len(lines) <= 1: send_telegram(f"📭 {label}沒有行程")
    else: send_telegram("\n".join(lines), reply_markup={"inline_keyboard": keyboard})

def do_del():
    if not EVENT_ID: return
    try:
        cid, eid = EVENT_ID.split("|", 1) if "|" in EVENT_ID else ('primary', EVENT_ID)
        get_calendar().events().delete(calendarId=cid, eventId=eid).execute()
        send_telegram("🗑 已刪除行程")
    except Exception as e: send_telegram(f"❌ 刪除失敗：{e}")

def main():
    if not CHAT_ID: return
    try:
        if ACTION == "list": do_list()
        elif ACTION == "del": do_del()
        else: do_create()
    except Exception as e: log(f"Error: {e}"); send_telegram(f"❌ 錯誤：{e}")

if __name__ == '__main__':
    main()

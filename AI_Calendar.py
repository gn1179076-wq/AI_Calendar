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
FAMILY_CAL_ID     = os.environ.get("FAMILY_CAL_ID") # 家庭日曆 ID

TZ = ZoneInfo("Asia/Taipei")
SCOPES = ['https://www.googleapis.com/auth/calendar']

# 固定格式：[YYYY/]M/D HH:MM[-HH:MM] 標題
RE_STRICT = re.compile(
    r'^\s*'
    r'(?:(?P<year>\d{4})/)?'
    r'(?P<month>\d{1,2})/(?P<day>\d{1,2})'
    r'\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})'
    r'(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?'
    r'\s+(?P<title>.+?)\s*$'
)

def log(msg):
    print(f"[AI_Calendar] {msg}", flush=True)

def send_telegram(text, reply_markup=None):
    if not CHAT_ID:
        log("no CHAT_ID, skip telegram")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(url, json=payload, timeout=15)
    log(f"telegram status={r.status_code}")

def get_calendar():
    token_info = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    return build('calendar', 'v3', credentials=creds)

def try_parse_strict(text):
    m = RE_STRICT.match(text)
    if not m: return None
    g = m.groupdict()
    now = datetime.datetime.now(TZ)
    year = int(g["year"]) if g["year"] else now.year
    try:
        start = datetime.datetime(year, int(g["month"]), int(g["day"]), int(g["sh"]), int(g["sm"]))
    except ValueError: return None
    if not g["year"] and start < now.replace(tzinfo=None):
        start = start.replace(year=year + 1)
    if g["eh"]:
        end = start.replace(hour=int(g["eh"]), minute=int(g["em"]))
        if end <= start: end = end + datetime.timedelta(days=1)
    else:
        end = start + datetime.timedelta(hours=1)
    return {
        "summary": g["title"],
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end":   end.strftime("%Y-%m-%dT%H:%M"),
    }

def parse_with_gemini(text):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    now = datetime.datetime.now(TZ)
    prompt = f"""你是行事曆助手。現在時間是 {now.strftime('%Y-%m-%d %H:%M %A')} (Asia/Taipei)。
把使用者的訊息解析成 JSON，格式：
{{"summary": "事件標題", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}}
規則：只輸出 JSON，不要任何其他文字。
使用者訊息：{text}"""
    resp = model.generate_content(prompt)
    raw = resp.text.strip().strip("`").lstrip("json").strip()
    return json.loads(raw)

def do_create():
    data = try_parse_strict(TEXT)
    if data is None:
        try:
            data = parse_with_gemini(TEXT)
        except Exception as e:
            send_telegram(f"❌ 解析失敗：{e}")
            return

    # ── 判斷要存入哪一個日曆 ──
    target_cal = 'primary'
    cal_label = "個人"
    
    # 判斷關鍵字，決定是否存入家庭日曆
    family_keywords = ["老婆", "家", "我們", "家庭", "小孩", "晚餐"]
    if FAMILY_CAL_ID and any(k in TEXT for k in family_keywords):
        target_cal = FAMILY_CAL_ID
        cal_label = "家庭"

    start = datetime.datetime.fromisoformat(data["start"]).replace(tzinfo=TZ)
    end   = datetime.datetime.fromisoformat(data["end"]).replace(tzinfo=TZ)

    event = {
        'summary': data["summary"],
        'start':   {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'},
        'end':     {'dateTime': end.isoformat(),   'timeZone': 'Asia/Taipei'},
    }
    
    created = get_calendar().events().insert(calendarId=target_cal, body=event).execute()
    msg = (
        f"✅ 已加入【{cal_label}】日曆\n"
        f"📅 {data['summary']}\n"
        f"⏰ {start.strftime('%m/%d (%a) %H:%M')} – {end.strftime('%H:%M')}\n"
        f"🔗 {created.get('htmlLink')}"
    )
    send_telegram(msg)

def do_list():
    now = datetime.datetime.now(TZ)
    arg = TEXT.strip().lower()
    if arg == "today":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt   = start_dt + datetime.timedelta(days=1)
        label = "今天"
    else:
        try: days = int(arg) if arg else 7
        except ValueError: days = 7
        start_dt = now
        end_dt   = now + datetime.timedelta(days=days)
        label = f"未來 {days} 天"

    service = get_calendar()
    # 要檢查的日曆清單
    cals = [('primary', '👤')]
    if FAMILY_CAL_ID:
        cals.append((FAMILY_CAL_ID, '🏠'))

    lines = [f"📋 {label}行程\n"]
    keyboard = []

    for cid, icon in cals:
        events = service.events().list(
            calendarId=cid, timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(),
            singleEvents=True, orderBy='startTime', maxResults=20
        ).execute().get('items', [])

        for ev in events:
            s = ev['start'].get('dateTime') or ev['start'].get('date')
            dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(TZ)
            title = ev.get('summary', '(無標題)')
            lines.append(f"{icon} {dt.strftime('%m/%d %H:%M')} {title}")
            # 將日曆 ID 與 事件 ID 封裝在 callback_data (格式: cid|eid)
            keyboard.append([{
                "text": f"🗑 {icon} {title[:12]}",
                "callback_data": f"del:{cid}|{ev['id']}"
            }])

    if len(lines) <= 1:
        send_telegram(f"📭 {label}沒有行程")
    else:
        send_telegram("\n".join(lines), reply_markup={"inline_keyboard": keyboard})

def do_del():
    if not EVENT_ID:
        send_telegram("❌ 沒收到事件 ID")
        return
    try:
        # 拆解傳回來的 cid|eid
        if "|" in EVENT_ID:
            cid, eid = EVENT_ID.split("|", 1)
        else:
            cid, eid = 'primary', EVENT_ID
            
        get_calendar().events().delete(calendarId=cid, eventId=eid).execute()
        send_telegram("🗑 已從日曆刪除該行程")
    except Exception as e:
        send_telegram(f"❌ 刪除失敗：{e}")

def main():
    if not CHAT_ID: return
    try:
        if ACTION == "list": do_list()
        elif ACTION == "del": do_del()
        else: do_create()
    except Exception as e:
        log(f"error: {e}")
        send_telegram(f"❌ 發生錯誤：{e}")

if __name__ == '__main__':
    main()

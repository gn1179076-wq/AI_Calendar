import os
import json
import re
import datetime
import requests
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── 環境變數 ──
# TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")   # ← Telegram 已停用
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY")
CHAT_ID           = os.environ.get("CHAT_ID")
ACTION            = os.environ.get("ACTION", "create")
TEXT              = os.environ.get("TEXT", "")
EVENT_ID          = os.environ.get("EVENT_ID", "")
FAMILY_CAL_ID     = os.environ.get("FAMILY_CAL_ID")
LINE_TOKEN        = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
DISCORD_WEBHOOK   = os.environ.get("DISCORD_WEBHOOK_URL")   # ← Discord Webhook URL
SOURCE            = "discord"

TZ = ZoneInfo("Asia/Taipei")
SCOPES = ['https://www.googleapis.com/auth/calendar']
WD = ["一","二","三","四","五","六","日"]  # 星期對照表（Monday=0）

# ── 支援格式說明 ──
FORMAT_HELP = """❓ 格式不符，請用以下格式輸入：

📌 固定日期：
  4/25 14:00 看牙醫
  4/25 14:00-15:30 會議
  2026/4/25 14:00 健檢

📌 相對日期：
  今天 20:00 家庭晚餐
  明天 09:00-10:00 產檢
  後天 18:30 接送"""

# 格式 1：(YYYY/)M/D HH:MM(-HH:MM) 標題
RE_STRICT = re.compile(
    r'^\s*(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$'
)
# 格式 2：今天/明天/後天 HH:MM(-HH:MM) 標題
RE_RELATIVE = re.compile(
    r'^\s*(?P<rel>今天|明天|後天)\s+'
    r'(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$'
)

def log(msg):
    print(f"[AI_Calendar] {msg}", flush=True)

# ── Telegram 相關（已停用） ──
# def send_telegram(text, reply_markup=None):
#     if not CHAT_ID: return
#     url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
#     payload = {"chat_id": CHAT_ID, "text": text}
#     if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
#     requests.post(url, json=payload, timeout=15)

def send_discord(text):
    if not DISCORD_WEBHOOK:
        log("send_discord skipped: DISCORD_WEBHOOK not set")
        return
    payload = {"content": text}
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    log(f"send_discord status={r.status_code}")

def send_line(text):
    if not CHAT_ID or not LINE_TOKEN:
        log(f"send_line skipped: CHAT_ID={bool(CHAT_ID)}, LINE_TOKEN={bool(LINE_TOKEN)}")
        return
    url = "https://api.line.me/v2/bot/message/push"
    body = {"to": CHAT_ID, "messages": [{"type": "text", "text": text}]}
    log(f"send_line to={CHAT_ID}")
    r = requests.post(url, json=body, headers={
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json"
    }, timeout=15)
    log(f"send_line status={r.status_code} body={r.text[:200]}")

def notify(text):
    if SOURCE == "line":
        send_line(text)
    elif SOURCE == "discord":
        send_discord(text)
    # else:                          # ← Telegram 已停用
    #     send_telegram(text)

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

# ── Gemini 解析（已停用，固定格式不需 AI） ──
# def parse_with_gemini(text):
#     import google.generativeai as genai
#     genai.configure(api_key=GEMINI_API_KEY)
#     model = genai.GenerativeModel("gemini-2.0-flash")
#     now = datetime.datetime.now(TZ)
#     prompt = f'現在是 {now.strftime("%Y-%m-%d %H:%M %A")}。將訊息解析為 JSON: {{"summary": "標題", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}}。訊息：{text}'
#     resp = model.generate_content(prompt)
#     raw = resp.text.strip().strip("`").lstrip("json").strip()
#     return json.loads(raw)

def do_create():
    data = try_parse_strict(TEXT)
    if data is None:
        # 格式不符，直接提示使用者，不呼叫 AI
        notify(FORMAT_HELP)
        return
    mode = "⚡️"
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
    notify(f"✅ 已存入 {cal_label} ({mode})\n📅 {data['summary']}\n⏰ {start.strftime('%m/%d')} ({WD[start.weekday()]}) {start.strftime('%H:%M')}\n🔗 查看行程：{created.get('htmlLink')}")

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
        lines = []
        for cid, icon, short_code in cals:
            events = service.events().list(calendarId=cid, timeMin=sd.isoformat(), timeMax=ed.isoformat(),
                                           singleEvents=True, orderBy='startTime').execute().get('items', [])
            for ev in events:
                t  = ev['start'].get('dateTime') or ev['start'].get('date')
                dt = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone(TZ)
                summary = ev.get('summary', '(無標題)')
                lines.append(f"{icon} {dt.strftime('%m/%d')}({WD[dt.weekday()]}) {dt.strftime('%H:%M')} {summary}")
        today_str = f"{now.strftime('%m/%d')}({WD[now.weekday()]})"
        if not lines:
            notify(f"📆 今天是 {today_str}\n\n📭 {label}沒有行程")
        else:
            notify(f"📆 今天是 {today_str}\n📋 {label}行程預覽\n\n" + "\n".join(lines))
        # ── Telegram inline keyboard 已停用（Discord 不支援）──
        # if SOURCE != "line":
        #     send_telegram(f"📋 {label}行程預覽", reply_markup={"inline_keyboard": keyboard})
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
        notify("🗑 行程已成功刪除")
    except Exception as e:
        notify(f"❌ 刪除失敗: {e}")

def main():
    if not CHAT_ID: return
    try:
        if ACTION == "list":    do_list()
        elif ACTION == "del":   do_del()
        else:                   do_create()
    except Exception as e:
        log(f"Error: {e}")

if __name__ == '__main__':
    main()

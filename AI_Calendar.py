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
CHAT_ID           = os.environ.get("CHAT_ID")      # 由 Worker 傳入
ACTION            = os.environ.get("ACTION", "create")  # create / list / del
TEXT              = os.environ.get("TEXT", "")
EVENT_ID          = os.environ.get("EVENT_ID", "")

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
    """輸出到 GitHub Actions log，方便排錯"""
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
    log(f"telegram status={r.status_code} body={r.text[:200]}")


def get_calendar():
    token_info = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    return build('calendar', 'v3', credentials=creds)


def try_parse_strict(text):
    """嘗試用固定格式解析，成功回傳 dict，失敗回傳 None"""
    m = RE_STRICT.match(text)
    if not m:
        return None
    g = m.groupdict()
    now = datetime.datetime.now(TZ)
    year = int(g["year"]) if g["year"] else now.year

    try:
        start = datetime.datetime(year, int(g["month"]), int(g["day"]),
                                  int(g["sh"]), int(g["sm"]))
    except ValueError:
        return None

    # 沒給年份且日期已過 → 推到明年
    if not g["year"] and start < now.replace(tzinfo=None):
        start = start.replace(year=year + 1)

    if g["eh"]:
        end = start.replace(hour=int(g["eh"]), minute=int(g["em"]))
        if end <= start:
            end = end + datetime.timedelta(days=1)
    else:
        end = start + datetime.timedelta(hours=1)

    return {
        "summary": g["title"],
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end":   end.strftime("%Y-%m-%dT%H:%M"),
    }


def parse_with_gemini(text):
    """用 Gemini 把自然語言解析成事件資料"""
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    now = datetime.datetime.now(TZ)
    prompt = f"""你是行事曆助手。現在時間是 {now.strftime('%Y-%m-%d %H:%M %A')} (Asia/Taipei)。
把使用者的訊息解析成 JSON，格式：
{{"summary": "事件標題", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}}
規則：
- 只輸出 JSON，不要任何其他文字或 markdown code fence
- 如果沒指定結束時間，預設事件長 1 小時
- 如果沒指定年份，用最接近的未來日期
- 時區一律 Asia/Taipei
- 時間不帶時區資訊

使用者訊息：{text}"""

    resp = model.generate_content(prompt)
    raw = resp.text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    log(f"gemini raw: {raw}")
    return json.loads(raw)


def do_create():
    # 1. 先試固定格式
    data = try_parse_strict(TEXT)
    source = "strict"

    # 2. 固定格式不吻合才呼叫 Gemini
    if data is None:
        try:
            data = parse_with_gemini(TEXT)
            source = "gemini"
        except Exception as e:
            send_telegram(f"❌ 我沒看懂這句話：{TEXT}\n錯誤：{e}")
            return

    log(f"parsed via {source}: {data}")

    start = datetime.datetime.fromisoformat(data["start"]).replace(tzinfo=TZ)
    end   = datetime.datetime.fromisoformat(data["end"]).replace(tzinfo=TZ)

    event = {
        'summary': data["summary"],
        'start':   {'dateTime': start.isoformat(), 'timeZone': 'Asia/Taipei'},
        'end':     {'dateTime': end.isoformat(),   'timeZone': 'Asia/Taipei'},
    }
    created = get_calendar().events().insert(calendarId='primary', body=event).execute()
    msg = (
        f"✅ 已加入行事曆\n"
        f"📅 {data['summary']}\n"
        f"⏰ {start.strftime('%m/%d (%a) %H:%M')} – {end.strftime('%H:%M')}\n"
        f"🔗 {created.get('htmlLink')}"
    )
    send_telegram(msg)


def do_list():
    """列出未來 N 天的事件。TEXT 可以是 'today' / '7' / '30' / 空"""
    now = datetime.datetime.now(TZ)
    arg = TEXT.strip().lower()
    if arg == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = start + datetime.timedelta(days=1)
        label = "今天"
    else:
        try:
            days = int(arg) if arg else 7
        except ValueError:
            days = 7
        start = now
        end   = now + datetime.timedelta(days=days)
        label = f"未來 {days} 天"

    events = get_calendar().events().list(
        calendarId='primary',
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True, orderBy='startTime', maxResults=50
    ).execute().get('items', [])

    if not events:
        send_telegram(f"📭 {label}沒有行程")
        return

    lines = [f"📋 {label}行程 ({len(events)} 筆)\n"]
    keyboard = []
    for ev in events:
        s = ev['start'].get('dateTime') or ev['start'].get('date')
        dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(TZ)
        title = ev.get('summary', '(無標題)')
        lines.append(f"• {dt.strftime('%m/%d %H:%M')}  {title}")
        keyboard.append([{
            "text": f"🗑 {dt.strftime('%m/%d %H:%M')} {title[:15]}",
            "callback_data": f"del:{ev['id']}"
        }])

    send_telegram("\n".join(lines), reply_markup={"inline_keyboard": keyboard})


def do_del():
    if not EVENT_ID:
        send_telegram("❌ 沒收到事件 ID")
        return
    try:
        get_calendar().events().delete(calendarId='primary', eventId=EVENT_ID).execute()
        send_telegram("🗑 已刪除該事件")
    except Exception as e:
        send_telegram(f"❌ 刪除失敗：{e}")


def main():
    log(f"action={ACTION} chat_id={CHAT_ID} text={TEXT!r} event_id={EVENT_ID!r}")
    if not CHAT_ID:
        log("no CHAT_ID, abort")
        return
    try:
        if ACTION == "list":
            do_list()
        elif ACTION == "del":
            do_del()
        else:
            do_create()
    except Exception as e:
        log(f"unhandled error: {e}")
        send_telegram(f"❌ 發生錯誤：{e}")


if __name__ == '__main__':
    main()

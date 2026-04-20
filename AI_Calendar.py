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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "https://your-worker.workers.dev")

ACTION = os.environ.get("ACTION", "create")
TEXT = os.environ.get("TEXT", "")
EVENT_ID = os.environ.get("EVENT_ID", "")
FAMILY_CAL_ID = os.environ.get("FAMILY_CAL_ID")
SOURCE = "discord"

TZ = ZoneInfo("Asia/Taipei")
SCOPES = ['https://www.googleapis.com/auth/calendar']

RE_STRICT = re.compile(r'^\s*(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$')
RE_RELATIVE = re.compile(r'^\s*(?P<rel>今天|明天|後天)\s+(?P<sh>\d{1,2}):(?P<sm>\d{2})(?:\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2}))?\s+(?P<title>.+?)\s*$')

def log(msg):
    print(f"[AI_Calendar] {msg}", flush=True)

# ==========================================
# Discord 推播核心 (分批防爆版)
# ==========================================
def send_discord(title, embed_fields=None, color=3066993):
    if not DISCORD_WEBHOOK_URL:
        log("❌ 找不到 DISCORD_WEBHOOK_URL")
        return
    
    fields = embed_fields or []
    for f in fields:
        if not f.get("value") or str(f["value"]).strip() == "":
            f["value"] = "-"

    chunk_size = 20 
    chunks = [fields[i:i + chunk_size] for i in range(0, len(fields), chunk_size)] if fields else [[]]
    now_iso = datetime.datetime.now(TZ).isoformat()

    for i, chunk in enumerate(chunks):
        display_title = title if i == 0 else f"{title} (續)"
        payload = {
            "username": "Fiona 行程管家",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/2693/2693507.png",
            "embeds": [{
                "title": str(display_title),
                "color": int(color),
                "fields": chunk,
                "footer": {"text": f"系統分支: {os.getenv('GITHUB_REF_NAME', 'main')}"},
                "timestamp": now_iso
            }]
        }

        try:
            res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            if res.status_code != 204:
                log(f"❌ Discord 發送失敗 ({res.status_code}): {res.text}")
            else:
                log(f"✅ Discord 發送成功: {res.status_code}")
        except Exception as e:
            log(f"Discord 連線異常: {e}")

# ==========================================
# 日曆核心邏輯
# ==========================================
def get_calendar():
    token_info = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    return build('calendar', 'v3', credentials=creds)

def parse_with_gemini(text):
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        now = datetime.datetime.now(TZ)
        prompt = f"現在是 {now.strftime('%Y-%m-%d %H:%M %A')}。將訊息解析為 JSON: {{\"summary\": \"標題\", \"start\": \"YYYY-MM-DDTHH:MM\", \"end\": \"YYYY-MM-DDTHH:MM\"}}。訊息：{text}"
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        raw = response.text.strip().strip("`").lstrip("json").strip()
        return json.loads(raw)
    except Exception:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
        now = datetime.datetime.now(TZ)
        prompt = f"現在是 {now.strftime('%Y-%m-%d %H:%M %A')}。將訊息解析為 JSON: {{\"summary\": \"標題\", \"start\": \"YYYY-MM-DDTHH:MM\", \"end\": \"YYYY-MM-DDTHH:MM\"}}。訊息：{text}"
        resp = model.generate_content(prompt)
        raw = resp.text.strip().strip("`").lstrip("json").strip()
        return json.loads(raw)

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
    family_keywords = ["老婆", "家", "我們", "家庭", "小孩", "晚餐", "產檢", "接送"]
    is_family = FAMILY_CAL_ID and any(k in TEXT for k in family_keywords)
    target_cal = FAMILY_CAL_ID if is_family else 'primary'
    try:
        start = datetime.datetime.fromisoformat(data["start"]).replace(tzinfo=TZ)
        end = datetime.datetime.fromisoformat(data["end"]).replace(tzinfo=TZ)
        event = {'summary': data["summary"], 'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': end.isoformat()}}
        created = get_calendar().events().insert(calendarId=target_cal, body=event).execute()
        fields = [
            {"name": "📌 內容", "value": f"**{data['summary']}**", "inline": False},
            {"name": "⏰ 時間", "value": f"{start.strftime('%m/%d %H:%M')} - {end.strftime('%H:%M')}", "inline": True},
            {"name": "📂 位置", "value": f"{'🏠 家庭' if is_family else '👤 個人'} ({mode})", "inline": True},
            {"name": "🔗 連結", "value": f"[日曆連結](<{created.get('htmlLink')}>)", "inline": False}
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
            items = service.events().list(calendarId=cid, timeMin=sd.isoformat(), timeMax=ed.isoformat(), singleEvents=True, orderBy='startTime').execute().get('items', [])
            for ev in items:
                t = ev['start'].get('dateTime') or ev['start'].get('date')
                dt = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone(TZ)
                del_url = f"{CF_WORKER_URL}/del?sc={short_code}&eid={ev['id']}"
                fields.append({
                    "name": f"{icon} {dt.strftime('%m/%d %H:%M')}",
                    "value": f"{ev.get('summary','(無標題)')} **[[❌]](<{del_url}>)**",
                    "inline": True
                })
        if not fields:
            send_discord(f"📭 未來 {days} 天沒有行程")
        else:
            send_discord(f"📋 未來 {days} 天行程預覽", embed_fields=fields)
    except Exception as e:
        send_discord(f"❌ 列表失敗: {e}", color=15158332)

def do_del():
    try:
        sc, eid = EVENT_ID.split("|", 1) if "|" in EVENT_ID else ('p', EVENT_ID)
        cid = FAMILY_CAL_ID if sc == 'f' else 'primary'
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

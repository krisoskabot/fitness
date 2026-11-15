import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen

from products import decode_image, lookup, save_label, scanner_library

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER = os.getenv("OWNER_ID", "")
DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
DB = Path(os.getenv("DATA_DIR", "/data")) / "diary.sqlite"
PAGE = Path(__file__).with_name("index.html")


def authorized(raw):
    try:
        fields = dict(parse_qsl(raw, strict_parsing=True))
        supplied = fields.pop("hash")
        key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
        message = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        return bool(TOKEN and OWNER and hmac.compare_digest(
            hmac.new(key, message.encode(), hashlib.sha256).hexdigest(), supplied
        ) and 0 <= time.time() - int(fields["auth_date"]) < 86400
            and str(json.loads(fields["user"])["id"]) == OWNER)
    except (KeyError, TypeError, ValueError):
        return False


METRICS = {"weight", "height", "chest", "waist", "hips", "thigh", "arm"}


def validate(item):
    kind = item["kind"]
    if kind == "water" and (type(item.get("ml")) is not int or not 0 <= item["ml"] <= 5000):
        raise ValueError()
    if kind == "meal" and "mealType" in item and item["mealType"] not in ("breakfast","lunch","dinner","snack"):
        raise ValueError()
    if kind in ("measure", "goals"):
        values = item["values"]
        if item["id"] != kind or not isinstance(values, dict) or (kind == "measure" and not values) or not values.keys() <= METRICS:
            raise ValueError()
        if not all(type(v) in (int, float) and isfinite(v) and 0 < v <= 1000 for v in values.values()):
            raise ValueError()
    if kind in ("rhythm", "plan"):
        if item["id"] != kind or not isinstance(item["name"], str) or not 0 < len(item["name"].strip()) <= 120:
            raise ValueError()
        if item["time"] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item["time"]):
            raise ValueError()
        if type(item["enabled"]) is not bool:
            raise ValueError()


def overview(conn):
    rows = conn.execute("""
        SELECT day,kind,body FROM items
        WHERE kind IN ('rhythm','plan','measure') ORDER BY day
    """)
    events = [{"day": day, **json.loads(body)} for day, _, body in rows]
    workouts = [r[0] for r in conn.execute(
        "SELECT DISTINCT day FROM items WHERE kind='work' ORDER BY day")]
    meals = []
    seen = set()
    rows = conn.execute("""
        SELECT json_extract(body,'$.name'), json_extract(body,'$.kcal'),
               json_extract(body,'$.p'), json_extract(body,'$.f'), json_extract(body,'$.c')
        FROM items WHERE kind='meal' ORDER BY day DESC,rowid DESC LIMIT 100
    """)
    for name, kcal, protein, fat, carbs in rows:
        normalized = (name or "").strip().casefold()
        if normalized and normalized not in seen:
            meals.append(dict(name=name, kcal=kcal, p=protein, f=fat, c=carbs))
            seen.add(normalized)
        if len(meals) == 6:
            break
    stored = conn.execute("SELECT body FROM preferences WHERE name='goals'").fetchone()
    return {"events": events, "workdays": workouts, "meals": meals,
            "goals": json.loads(stored[0]) if stored else {}}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/":
            self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/health":
            self.reply(200, b'{"ok":true}')
        else:
            self.reply(404, b"{}")

    def do_POST(self):
        if not authorized(self.headers.get("X-Telegram-Init", "")):
            return self.reply(403, b"{}")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= (3500000 if self.path == "/scan" else 1100000):
                return self.reply(413, b"{}")
            entry = json.loads(self.rfile.read(size))
            day = date.fromisoformat(entry["day"]).isoformat()
            with closing(sqlite3.connect(DB, timeout=8)) as conn, conn:
                if self.path == "/overview":
                    result = overview(conn)
                elif self.path == "/scan":
                    result = {"code": decode_image(entry)}
                elif self.path == "/product":
                    result = lookup(conn, entry["code"])
                elif self.path == "/draft":
                    row = conn.execute("SELECT description FROM meal_drafts WHERE id=? AND expires>?",
                                       (str(entry["id"]), int(time.time()))).fetchone()
                    result = {"description": row[0] if row else None}
                elif self.path == "/goals":
                    values = entry["values"]
                    validate({"id": "goals", "kind": "goals", "values": values})
                    conn.execute("INSERT INTO preferences(name,body) VALUES ('goals',?) "
                                 "ON CONFLICT(name) DO UPDATE SET body=excluded.body", (json.dumps(values),))
                    result = {"goals": values}
                elif self.path == "/read":
                    rows = conn.execute("SELECT body FROM items WHERE day=? ORDER BY rowid", (day,))
                    items = [json.loads(r[0]) for r in rows]
                    rows = conn.execute("SELECT day,body FROM items WHERE kind='work' AND day<? ORDER BY day DESC,rowid DESC", (day,))
                    history = {}
                    for when, body in rows:
                        for s in json.loads(body)["sets"]:
                            key = s["name"].strip().casefold()
                            previous = history.setdefault(key, {"day": when, "sets": []})
                            if previous["day"] == when:
                                previous["sets"].append(s)
                    result = {"items": items, "history": history}
                elif self.path == "/save":
                    item = entry["item"]
                    ident, kind = item["id"], item["kind"]
                    if kind not in ("meal","work","habit","measure","plan","rhythm","water") or not isinstance(ident,str) or len(ident)>100:
                        raise ValueError()
                    if kind == "work" and not all(isinstance(s["name"],str) for s in item["sets"]):
                        raise ValueError()
                    validate(item)
                    if kind == "meal" and entry.get("label") is not None:
                        save_label(conn, item["barcode"], entry["label"])
                    if kind == "rhythm" and item["enabled"]:
                        conn.execute("DELETE FROM items WHERE day=? AND kind='plan'", (day,))
                    conn.execute("INSERT INTO items VALUES (?,?,?,?) ON CONFLICT(day,id) DO UPDATE SET body=excluded.body WHERE items.kind=excluded.kind", (day,ident,kind,json.dumps(item,ensure_ascii=False)))
                    result = {}
                else:
                    return self.reply(404, b"{}")
            self.reply(200, json.dumps(result, ensure_ascii=False).encode())
        except (KeyError, ValueError, TypeError):
            self.reply(400, b"{}")


def telegram(method, payload):
    req = Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=json.dumps(payload).encode(),
                  headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=45) as response:
        return json.load(response)["result"]


def bot():
    offset = 0
    connected = False
    while True:
        try:
            if not connected:
                telegram("getMe", {})
                print("Telegram credentials accepted", flush=True)
                connected = True
            for update in telegram("getUpdates", {"timeout":25,"offset":offset}):
                msg = update.get("message", {})
                chat = msg.get("chat", {})
                if chat.get("type") == "private":
                    if msg.get("text") in ("/start", "/id"):
                        payload = {"chat_id":chat["id"], "text":f"Твой Telegram ID — {chat['id']}"}
                        if str(chat["id"]) == OWNER and DOMAIN:
                            payload.update(text="Твой дневник готов.", reply_markup={"inline_keyboard":[[{
                                "text":"Открыть мой день","web_app":{"url":"https://" + DOMAIN}}]]})
                        telegram("sendMessage", payload)
                    elif str(chat["id"]) == OWNER and DOMAIN:
                        description = msg.get("text", "").strip()
                        if description and not description.startswith("/"):
                            draft_id = str(update["update_id"])
                            if len(description) > 600:
                                telegram("sendMessage", {"chat_id": chat["id"], "text": "Сократи описание блюда до 600 символов."})
                            else:
                                with closing(sqlite3.connect(DB, timeout=8)) as conn, conn:
                                    now = int(time.time())
                                    conn.execute("DELETE FROM meal_drafts WHERE expires<=?", (now,))
                                    conn.execute("INSERT OR REPLACE INTO meal_drafts VALUES (?,?,?)",
                                                 (draft_id, description, now + 86400))
                                telegram("sendMessage", {"chat_id": chat["id"], "text": "Открой расчёт, проверь порцию и сохрани блюдо.",
                                    "reply_markup": {"inline_keyboard": [[{"text": "Посмотреть КБЖУ",
                                    "web_app": {"url": f"https://{DOMAIN}/?draft={draft_id}"}}]]}})
                offset = update["update_id"] + 1
        except HTTPError as error:
            print(f"Telegram HTTP {error.code}; retrying", flush=True)
            time.sleep(5)
        except URLError as error:
            print(f"Telegram network error: {type(error.reason).__name__}", flush=True)
            time.sleep(5)
        except Exception as error:
            print(f"Telegram error: {type(error).__name__}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    scanner_library()
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS items(day TEXT,id TEXT,kind TEXT,body TEXT,PRIMARY KEY(day,id))")
        conn.execute("CREATE TABLE IF NOT EXISTS meal_drafts(id TEXT PRIMARY KEY,description TEXT NOT NULL,expires INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS preferences(name TEXT PRIMARY KEY,body TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS product_labels(code TEXT PRIMARY KEY,body TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS by_kind_day ON items(kind,day)")
    if TOKEN:
        threading.Thread(target=bot, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT","8080"))), Handler).serve_forever()




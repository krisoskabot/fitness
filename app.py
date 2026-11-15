import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

TOKEN = os.getenv("BOT_TOKEN", "")
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


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
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
            if not 0 < size <= 1100000:
                return self.reply(413, b"{}")
            entry = json.loads(self.rfile.read(size))
            day = date.fromisoformat(entry["day"]).isoformat()
            with closing(sqlite3.connect(DB, timeout=8)) as conn, conn:
                if self.path == "/read":
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
                    if kind not in ("meal","work","habit") or not isinstance(ident,str) or len(ident)>100:
                        raise ValueError()
                    if kind == "work" and not all(isinstance(s["name"],str) for s in item["sets"]):
                        raise ValueError()
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
    while True:
        try:
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
                offset = update["update_id"] + 1
        except Exception:
            print("Telegram temporarily unavailable; retrying", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS items(day TEXT,id TEXT,kind TEXT,body TEXT,PRIMARY KEY(day,id))")
    if TOKEN:
        threading.Thread(target=bot, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT","8080"))), Handler).serve_forever()

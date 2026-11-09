#!/usr/bin/env python3
"""
andropic GO — сервер для компьютера.
Только стандартная библиотека Python 3.8+ (psutil необязателен, но даст точные CPU/RAM/батарею).

Запуск:   python server.py
С действиями (блокировка/сон):   python server.py --actions
Arduino через ПК:   python server.py --arduino 192.168.1.20
"""
import argparse
import hmac
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.txt")
CONFIG_FILE = os.path.join(HERE, "config.json")
STARTED = time.time()

try:
    import psutil  # type: ignore
except Exception:  # psutil не установлен — используем запасные варианты
    psutil = None

state = {
    "token": "",
    "actions": False,
    "arduino_ip": "",
    "arduino_token": "andropic",
    "messages": [],
    "next_id": 1,
}
lock = threading.Lock()


# ---------------------------------------------------------------- утилиты
def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    t = secrets.token_hex(3)  # 6 символов, легко ввести на телефоне
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t)
    return t


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            state["arduino_ip"] = cfg.get("arduino_ip", state["arduino_ip"])
            state["arduino_token"] = cfg.get("arduino_token", state["arduino_token"])
    except Exception:
        pass


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"arduino_ip": state["arduino_ip"], "arduino_token": state["arduino_token"]}, f)
    except Exception:
        pass


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # пакет не отправляется, просто выбираем интерфейс
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def fmt_uptime(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d} д " if d else "") + f"{h} ч {m} мин"


def system_status():
    data = {
        "name": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": None,
        "ram": None,
        "battery": None,
        "uptime": None,
    }
    if psutil:
        try:
            data["cpu"] = psutil.cpu_percent(interval=0.2)
            data["ram"] = psutil.virtual_memory().percent
            b = psutil.sensors_battery()
            if b is not None:
                data["battery"] = b.percent
            data["uptime"] = fmt_uptime(time.time() - psutil.boot_time())
        except Exception:
            pass
    else:
        # запасной вариант для Linux
        try:
            if hasattr(os, "getloadavg"):
                data["cpu"] = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":")
                    mem[k] = int(v.split()[0])
            data["ram"] = 100.0 * (1 - mem["MemAvailable"] / mem["MemTotal"])
            with open("/proc/uptime") as f:
                data["uptime"] = fmt_uptime(float(f.read().split()[0]))
        except Exception:
            data["uptime"] = fmt_uptime(time.time() - STARTED) + " (сервер)"
    return data


def run_action(action):
    sysname = platform.system()
    cmds = {
        ("Windows", "lock"): ["rundll32.exe", "user32.dll,LockWorkStation"],
        ("Windows", "sleep"): ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        ("Darwin", "lock"): ["pmset", "displaysleepnow"],
        ("Darwin", "sleep"): ["pmset", "sleepnow"],
        ("Linux", "lock"): ["loginctl", "lock-session"],
        ("Linux", "sleep"): ["systemctl", "suspend"],
    }
    cmd = cmds.get((sysname, action))
    if not cmd:
        return False
    subprocess.Popen(cmd)
    return True


def add_message(sender, text):
    with lock:
        m = {"id": state["next_id"], "from": sender, "text": text, "t": time.strftime("%H:%M")}
        state["next_id"] += 1
        state["messages"].append(m)
        state["messages"] = state["messages"][-200:]
    return m


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "andropic-go/1.0"

    def log_message(self, fmt, *args):
        pass  # тишина в консоли, чтобы не мешать чату

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        q = parse_qs(urlparse(self.path).query)
        given = self.headers.get("X-Token") or (q.get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), state["token"].encode())

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        u = urlparse(self.path)
        path = u.path.rstrip("/")

        if path == "/api/ping":  # без авторизации — нужен для поиска в сети
            return self._send(200, {
                "app": "andropic", "type": "pc", "name": socket.gethostname(),
                "os": platform.system(), "auth": True,
            })

        if not self._auth():
            return self._send(401, {"error": "неверный код доступа"})

        if path == "/api/status" and method == "GET":
            return self._send(200, system_status())

        if path == "/api/message" and method == "POST":
            text = str(self._json_body().get("text", "")).strip()[:2000]
            if not text:
                return self._send(400, {"error": "пустое сообщение"})
            m = add_message("phone", text)
            print(f"\r📱 {m['t']}  {text}\n> ", end="", flush=True)
            return self._send(200, {"ok": True, "id": m["id"]})

        if path == "/api/messages" and method == "GET":
            since = int((parse_qs(u.query).get("since") or ["0"])[0] or 0)
            with lock:
                msgs = [m for m in state["messages"] if m["id"] > since]
            return self._send(200, {"messages": msgs})

        if path == "/api/action" and method == "POST":
            if not state["actions"]:
                return self._send(403, {"error": "запусти сервер с флагом --actions"})
            action = self._json_body().get("action", "")
            ok = action in ("lock", "sleep") and run_action(action)
            return self._send(200 if ok else 400, {"ok": bool(ok)} if ok else {"error": "неизвестное действие"})

        if path == "/api/config" and method == "POST":
            b = self._json_body()
            if "arduino_ip" in b:
                state["arduino_ip"] = str(b["arduino_ip"]).strip()
            if b.get("arduino_token"):
                state["arduino_token"] = str(b["arduino_token"]).strip()
            save_config()
            return self._send(200, {"ok": True})

        if path.startswith("/api/arduino/"):
            return self._relay(path[len("/api/arduino/"):], u.query)

        self._send(404, {"error": "не найдено"})

    def _relay(self, sub, query):
        ip = state["arduino_ip"]
        if not ip:
            return self._send(503, {"error": "IP Arduino не задан (--arduino или в приложении)"})
        qs = "&".join(p for p in query.split("&") if p and not p.startswith("token="))
        url = f"http://{ip}/api/{sub}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, headers={"X-Token": state["arduino_token"]})
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                data = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            data, code = e.read(), e.code
        except Exception as e:
            return self._send(502, {"error": f"Arduino недоступна: {e}"})
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def console_loop():
    """Всё, что напечатаешь в консоли ПК, уйдёт на телефон."""
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if line:
            add_message("pc", line)


def main():
    ap = argparse.ArgumentParser(description="andropic GO — сервер для ПК")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--actions", action="store_true", help="разрешить блокировку и сон по команде с телефона")
    ap.add_argument("--arduino", default="", help="IP платы ESP для управления через ПК")
    ap.add_argument("--arduino-token", default="", help="код доступа платы (по умолчанию andropic)")
    args = ap.parse_args()

    load_config()
    state["token"] = load_token()
    state["actions"] = args.actions
    if args.arduino:
        state["arduino_ip"] = args.arduino
    if args.arduino_token:
        state["arduino_token"] = args.arduino_token
    save_config()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("=" * 46)
    print("  andropic GO — сервер запущен")
    print("=" * 46)
    for ip in local_ips():
        print(f"  Адрес:  {ip}:{args.port}")
    print(f"  Код доступа:  {state['token']}")
    print(f"  Действия (блокировка/сон):  {'включены' if state['actions'] else 'выключены (--actions)'}")
    if not psutil:
        print("  Совет: pip install psutil — точнее CPU/RAM/батарея")
    print("  Пиши сюда сообщения — они придут на телефон. Ctrl+C — выход.")
    print("=" * 46)

    threading.Thread(target=console_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())

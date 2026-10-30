# growbox_widget.pyw - GrowBox widget for the Windows 11 desktop. A port of
# the Mac widget (growbox-widget-macos): the same cards, colours, icons and rules.
#
# - polls the main ESP: GET /api/config every 10 s (not more often: the ESP
#   once reset from running out of memory with many clients at once), the
#   climate log /api/environment every 5 min,
# - after 3 failed requests looks for the ESP (esp32.local, then the /24
#   network, nearest addresses first) and remembers the new address,
# - borderless window, below other windows like on the Mac; dragged with
#   the mouse, right click = menu,
# - day 4 without feeding: a notification every 2 h between 8:00 and 22:00,
# - temperature and humidity in the tray (like the menu bar on the Mac); left
#   click shows/hides the panel, right = menu.
#
# Requires Python 3.10+, Pillow and pystray (pip install pillow pystray;
# without pystray the widget works, just without tray icons). Settings
# (address, token, window position) in %APPDATA%\GrowBoxWidget\settings.json.
#
# python growbox_widget.pyw --snapshot file.png  -> one snapshot and quit

import concurrent.futures
import datetime as dt
import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

APP = "GrowBox Widget"
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "GrowBoxWidget")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
ICON_FONT = os.path.join(HERE, "assets", "Ionicons.ttf")

DEFAULT_URL = "http://192.168.18.100"
MDNS_HOST = "esp32.local"
POLL_S = 10
HISTORY_S = 5 * 60
SPAN = dt.timedelta(hours=24)
HTTP_TIMEOUT_S = 10
FAILURES_BEFORE_SEARCH = 3
SEARCH_COOLDOWN_S = 60
ALERT_INTERVAL = dt.timedelta(hours=2)

# target bands like data/growTargets.ts: (temp min, temp max, hum min, hum max)
VEG = (24, 27, 52, 62)
FLOWER_ON = (22, 26, 40, 55)
FLOWER_OFF = (18, 22, 40, 55)
TEMP_SCALE = (15, 30)
HUM_SCALE = (30, 90)


def rgb(h):
    return (h >> 16 & 255, h >> 8 & 255, h & 255)


# palette as in the app (constants/Colors.ts)
BACKGROUND = rgb(0x011515)
SURFACE = rgb(0x041C1B)
TEXT = rgb(0xF2F5F4)
MUTED = rgb(0x8A9B9B)
GREEN = rgb(0x00E95A)
BLUE = rgb(0x78BFFF)
YELLOW = rgb(0xFFD21C)
PURPLE = rgb(0xB18CFF)
RED = rgb(0xFF6070)
BORDER = rgb(0x123333)
OFF = rgb(0x2A4A3E)
KEY = (1, 2, 3)  # window transparency colour (rounded corners)

# app icons (Ionicons)
SUNNY_OUTLINE = "\uF5AE"
MOON_OUTLINE = "\uF461"
WATER_OUTLINE = "\uF62F"
WATER = "\uF62E"

W, H = 340, 438
SS = 3  # supersampling - smooth edges


def mix(color, alpha, under=SURFACE):
    return tuple(round(c * alpha + u * (1 - alpha)) for c, u in zip(color, under))


# --- settings ---

def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    os.replace(tmp, SETTINGS_FILE)


# --- ESP data (like BoxModel.swift) ---

def parse_local(s):
    """The ESP logs local time 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def parse_iso(s):
    """ISO from the app ('2026-10-08T17:12:33.123Z') -> local datetime."""
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None

    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


class Box:
    """Derived values for the view; response = JSON from /api/config."""

    def __init__(self, response, now):
        self.response = response
        self.now = now
        r = response or {}
        self.config = r.get("config") or {}
        self.sensor = r.get("sensor") or {}
        self.status = r.get("status") or {}

    @property
    def flowering(self):
        return bool((self.config.get("auto") or {}).get("enabled"))

    @property
    def light_on(self):
        return (self.config.get("relayLight") or {}).get("state") == 1

    @property
    def fan_on(self):
        return (self.config.get("relayFan") or {}).get("state") == 1

    # the dimmer keeps its level even with the relay off
    @property
    def fan_set_level(self):
        return max(0, min(100, self.status.get("fanLevel") or 0))

    @property
    def fan_level(self):
        return self.fan_set_level if self.fan_on else 0

    @property
    def humidifier_band(self):
        config = self.config.get("humidifier")

        if not config:
            return None

        return config.get("auto" if self.flowering else "manual")

    # --- feeding, like hooks/useFeedingStatus.ts in the app ---

    @property
    def feedings(self):
        history = (self.config.get("feeding") or {}).get("history") or []
        return sorted(t for t in map(parse_iso, history) if t)

    @property
    def feeding_count(self):
        return (self.config.get("feeding") or {}).get("count") or 0

    def _days_since(self, t):
        return (self.now.date() - t.date()).days

    @property
    def feeding_status(self):
        """Day 3 without feeding late (yellow), day 4 overdue (red)."""
        feedings = self.feedings

        if not feedings:
            return "overdue"

        since = self._days_since(feedings[-1])
        return "overdue" if since >= 4 else "late" if since >= 3 else "ok"

    @property
    def fed_recently(self):
        feedings = self.feedings
        return bool(feedings) and self.now - feedings[-1] < dt.timedelta(hours=24)

    @property
    def last_fed_label(self):
        feedings = self.feedings

        if not feedings:
            return "Never"

        n = self._days_since(feedings[-1])
        return "Today" if n <= 0 else "Yesterday" if n == 1 else "%d days ago" % n

    # exact start when the ESP knows it, otherwise midnight of the start day
    @property
    def flowering_start(self):
        auto = self.config.get("auto") or {}

        if not auto.get("enabled"):
            return None

        exact = parse_local(auto.get("floweringStartedAt"))

        if exact:
            return exact

        day = auto.get("floweringStartDate")
        return parse_local(day + " 00:00:00") if day else None

    @property
    def flowering_week(self):
        """(week 1-10, progress 0-1) from midnight of the start day."""
        day = (self.config.get("auto") or {}).get("floweringStartDate")
        start = parse_local(day + " 00:00:00") if day and self.flowering else None

        if start is None:
            return None

        week_s = 7 * 86400
        elapsed = max(0.0, (self.now - start).total_seconds())
        week = min(int(elapsed // week_s) + 1, 10)
        progress = 1.0 if elapsed >= 10 * week_s else (elapsed % week_s) / week_s
        return week, progress

    def light_scheduled(self, t):
        items = self.config.get("lightSchedule") or []

        if not items:
            return True

        def minutes(v):
            parts = [int(p) for p in (v or "0:0").split(":") if p.isdigit()]
            return (parts[0] if parts else 0) * 60 + (parts[1] if len(parts) > 1 else 0)

        now, on, off = t.hour * 60 + t.minute, minutes(items[0].get("on")), minutes(items[0].get("off"))
        return on <= now < off if on < off else now >= on or now < off

    def targets(self, t):
        start = self.flowering_start

        if start is None or t < start:
            return VEG

        return FLOWER_ON if self.light_scheduled(t) else FLOWER_OFF


# --- network ---

def get_json(url, path, token, timeout=HTTP_TIMEOUT_S):
    req = urllib.request.Request(url + path, headers={"Authorization": "Bearer " + token})

    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def looks_like_esp(url, token):
    try:
        r = get_json(url, "/api/config", token, 1.5)
        return isinstance(r, dict) and "config" in r and "status" in r
    except Exception:
        return False


def port_open(host):
    try:
        with socket.create_connection((host, 80), timeout=0.4):
            return True
    except OSError:
        return False


def my_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # sends nothing, only picks the interface
            return s.getsockname()[0]
    except OSError:
        return None


def find_esp(url, token):
    """After a router reset the ESP may get a new address (DHCP): esp32.local,
    then the /24 of the known address (and of the computer's own network),
    nearest first."""
    try:
        found = "http://" + socket.gethostbyname(MDNS_HOST)

        if found != url and looks_like_esp(found, token):
            return found
    except OSError:
        pass

    host = urllib.parse.urlsplit(url).hostname or ""
    octets = host.split(".")

    try:
        last = int(octets[3])
        prefixes = [".".join(octets[:3])]
    except (ValueError, IndexError):
        last, prefixes = 100, []

    mine = my_ip()

    if mine and mine.rsplit(".", 1)[0] not in prefixes:
        prefixes.append(mine.rsplit(".", 1)[0])

    for prefix in prefixes:
        hosts = ["%s.%d" % (prefix, h) for h in sorted(range(1, 255), key=lambda h: abs(h - last))]
        hosts = [h for h in hosts if h not in (host, mine)]

        with concurrent.futures.ThreadPoolExecutor(64) as pool:
            alive = [h for h, ok in zip(hosts, pool.map(port_open, hosts)) if ok]

        for h in alive:
            if looks_like_esp("http://" + h, token):
                return "http://" + h

    return None


class Poller(threading.Thread):
    """One request to the ESP at a time, in the background. The window reads the state."""

    def __init__(self, url, token, on_url_change):
        super().__init__(daemon=True)
        self.url, self.token = url, token
        self.on_url_change = on_url_change
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.response = None
        self.readings = []  # [(datetime, temp, hum)] ascending
        self.error = None
        self.searching = False
        self.syncing_history = False
        self.version = 0
        self.failures = 0
        self.last_history = None
        self.last_search = 0.0

    def changed(self):
        with self.lock:
            self.version += 1

    def refresh(self):
        self.last_history = None
        self.wake.set()

    def set_token(self, token):
        self.token = token
        self.refresh()

    def run(self):
        while True:
            self.poll()
            self.wake.wait(POLL_S)
            self.wake.clear()

    def poll(self):
        try:
            response = get_json(self.url, "/api/config", self.token)

            with self.lock:
                self.response, self.error, self.failures = response, None, 0
        except urllib.error.HTTPError as e:
            self.error = "Wrong token" if e.code in (401, 403) else "ESP offline"
            self.failures += 1
        except Exception:
            self.error = "ESP offline"
            self.failures += 1

        self.changed()

        if self.error:
            if self.failures >= FAILURES_BEFORE_SEARCH and self.error == "ESP offline":
                self.search()
            return

        now = dt.datetime.now()

        if self.last_history is None or now - self.last_history >= dt.timedelta(seconds=HISTORY_S):
            self.sync_history(now)

    def search(self):
        if time.monotonic() - self.last_search < SEARCH_COOLDOWN_S:
            return

        self.last_search = time.monotonic()
        self.searching = True
        self.changed()

        try:
            found = find_esp(self.url, self.token)
        finally:
            self.searching = False
            self.changed()

        if found and found != self.url:
            self.url = found
            self.failures = 0
            self.on_url_change(found)
            self.wake.set()

    def sync_history(self, now):
        start = now - SPAN
        since = self.readings[-1][0] if self.readings else start
        cursor = since.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        self.syncing_history = True
        self.changed()

        try:
            # pages of 100 rows; the first time 24 h = 3 pages
            for _ in range(20):
                query = urllib.parse.urlencode({"since": cursor, "limit": 100})
                page = get_json(self.url, "/api/environment?" + query, self.token)

                for row in page.get("data") or ():
                    t = parse_local(row.get("datetime"))

                    if t and row.get("temperature") is not None and row.get("humidity") is not None:
                        rows.append((t, float(row["temperature"]), float(row["humidity"])))

                nxt = page.get("next_since")

                if not page.get("has_more") or not nxt or nxt == cursor:
                    break

                cursor = nxt
                time.sleep(1)  # a pause between pages - the ESP reads them from the SD card
        except Exception:
            return  # the old curve stays, we try again on the next request
        finally:
            self.syncing_history = False

        by_time = {r[0]: r for r in self.readings}
        by_time.update({r[0]: r for r in rows})

        with self.lock:
            self.readings = sorted(r for r in by_time.values() if r[0] >= start)
            self.last_history = now

        self.changed()


# --- feeding notification ---

POWERSHELL_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode($env:GB_TITLE)) > $null
$t.Item(1).AppendChild($xml.CreateTextNode($env:GB_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def notify(title, body):
    if sys.platform != "win32":
        return

    env = dict(os.environ, GB_TITLE=title, GB_BODY=body)

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_TOAST],
            env=env, creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except OSError:
        pass


def alert_if_feeding_missed(box, settings, save):
    """Red: a notification every 2 h between 8:00 and 22:00, until fed."""
    if box.response is None or box.feeding_status != "overdue" or not box.feedings:
        return

    last, now = box.feedings[-1], box.now

    if not 8 <= now.hour <= 22:
        return

    sent = parse_local(settings.get("feedingAlertAt"))

    if sent and sent > last and now - sent < ALERT_INTERVAL - dt.timedelta(minutes=1):
        return

    settings["feedingAlertAt"] = now.strftime("%Y-%m-%d %H:%M:%S")
    save()
    days = (now.date() - last.date()).days
    notify("Watering missed", "The plant hasn't been watered for %d days. Feed it now!" % days)


# --- drawing ---

def _fonts():
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    inter = "/usr/share/fonts/opentype/inter/Inter-%s.otf"
    return {
        "regular": [os.path.join(win, "segoeui.ttf"), inter % "Regular"],
        "semibold": [os.path.join(win, "seguisb.ttf"), inter % "SemiBold"],
        "bold": [os.path.join(win, "segoeuib.ttf"), inter % "Bold"],
        "icons": [ICON_FONT],
    }


def fan_blade(scale):
    """Fan blade from the app (components/FanGauge.tsx, viewBox 48)."""
    def bezier(p0, p1, p2, p3, n=16):
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            yield tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d
                        for a, b, c, d in zip(p0, p1, p2, p3))

    pts = [(24, 24)]
    pts += bezier((24, 24), (19, 17), (18, 7), (24, 4))
    pts += bezier((24, 4), (31, 3), (32, 14), (24, 24))
    return [(x * scale, y * scale) for x, y in pts]


class Painter:
    def __init__(self, scale):
        self.scale = scale
        self.k = scale * SS
        self.size = (round(W * scale), round(H * scale))
        self.cache = {}
        self.paths = _fonts()

    def font(self, weight, size):
        key = (weight, size)

        if key not in self.cache:
            path = next((p for p in self.paths[weight] if os.path.exists(p)), None)
            px = round(size * self.k)
            self.cache[key] = ImageFont.truetype(path, px) if path else ImageFont.load_default(px)

        return self.cache[key]

    def p(self, *v):
        return [x * self.k for x in v]

    def rrect(self, d, box, r, fill, outline=None, width=1.0):
        d.rounded_rectangle(self.p(*box), radius=r * self.k, fill=fill, outline=outline,
                            width=max(1, round(width * self.k)))

    def text(self, d, xy, s, weight, size, fill, anchor="ls"):
        d.text(self.p(*xy), s, font=self.font(weight, size), fill=fill, anchor=anchor)

    def text_w(self, s, weight, size):
        return self.font(weight, size).getlength(s) / self.k

    def line(self, d, pts, fill, width):
        d.line([tuple(self.p(*pt)) for pt in pts], fill=fill, width=max(1, round(width * self.k)), joint="curve")

    def icon(self, d, cx, cy, glyph, size, color):
        self.text(d, (cx, cy), glyph, "icons", size, color, "mm")

    def ring(self, d, cx, cy, progress, color, thin=False):
        """Thin full circle + thick progress arc (like Ring in Views.swift)."""
        r = 22
        track_w, arc_w = (2, 6) if thin else (4, 4)
        track = mix(color, 0.5) if thin else BORDER
        box = self.p(cx - r, cy - r, cx + r, cy + r)
        d.ellipse(self.p(cx - r - track_w / 2, cy - r - track_w / 2, cx + r + track_w / 2, cy + r + track_w / 2),
                  outline=track, width=round(track_w * self.k))
        progress = max(0.0, min(1.0, progress))

        if progress <= 0:
            return

        ob = self.p(cx - r - arc_w / 2, cy - r - arc_w / 2, cx + r + arc_w / 2, cy + r + arc_w / 2)

        if progress >= 0.999:
            d.ellipse(ob, outline=color, width=round(arc_w * self.k))
            return

        start, end = -90, -90 + 360 * progress
        d.arc(ob, start, end, fill=color, width=round(arc_w * self.k))

        for a in (start, end):  # rounded ends
            x, y = cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
            d.ellipse(self.p(x - arc_w / 2, y - arc_w / 2, x + arc_w / 2, y + arc_w / 2), fill=color)

    def fan(self, d, cx, cy, color, angle=0.0, size=22):
        s = size / 48
        blade = fan_blade(s)

        for b in (0, 120, 240):
            a = math.radians(b + angle)
            ca, sa = math.cos(a), math.sin(a)
            pts = [(cx + (x - size / 2) * ca - (y - size / 2) * sa, cy + (x - size / 2) * sa + (y - size / 2) * ca)
                   for x, y in blade]
            d.polygon([tuple(self.p(*pt)) for pt in pts], fill=color)

        r1, r2 = 5 * s, 2 * s
        d.ellipse(self.p(cx - r1, cy - r1, cx + r1, cy + r1), fill=color)
        d.ellipse(self.p(cx - r2, cy - r2, cx + r2, cy + r2), fill=SURFACE)

    def fan_frame(self, color, angle, box=26):
        """Just the fan animation frame (box x box pt, card background)."""
        img = Image.new("RGB", (round(box * self.k),) * 2, SURFACE)
        self.fan(ImageDraw.Draw(img), box / 2, box / 2, color, angle)
        n = round(box * self.scale)
        return img.resize((n, n), Image.LANCZOS)

    # --- whole widget ---

    def render(self, s):
        img = Image.new("RGB", (round(W * self.k), round(H * self.k)), KEY)
        d = ImageDraw.Draw(img, "RGBA")
        self.rrect(d, (0, 0, W, H), 20, BACKGROUND, BORDER, 1)

        self.header(d, s)
        self.sensor(d, s)
        self.chart(img, s)
        d = ImageDraw.Draw(img, "RGBA")
        fan_center = self.status(d, s)
        self.feeding(d, s)

        return img.resize(self.size, Image.LANCZOS), fan_center

    def header(self, d, s):
        self.text(d, (16, 26), "Box Panel", "bold", 15, TEXT)

        if s["searching"]:
            label, color, weight = "Searching for the box…", MUTED, "regular"
        elif s["box"].response is None and s["error"] is None:
            label, color, weight = "Connecting…", MUTED, "regular"
        elif s["error"]:
            label, color, weight = s["error"], RED, "regular"
        else:
            label, color, weight = "AUTO" if s["box"].flowering else "MANUAL", GREEN, "bold"

        self.text(d, (324, 26), label, weight, 11, color, "rs")

    def sensor(self, d, s):
        box = s["box"]
        self.rrect(d, (12, 40, 328, 102), 14, SURFACE, BORDER, 1)
        temp = box.sensor.get("temperature")
        hum = box.sensor.get("humidity")
        light = box.light_on
        items = (
            (24, SUNNY_OUTLINE if light else MOON_OUTLINE, YELLOW if light else TEXT,
             "%.1f°C" % temp if temp is not None else "--", "Temperature"),
            (183, WATER_OUTLINE, BLUE, "%.1f%%" % hum if hum is not None else "--", "Humidity"),
        )

        for x, glyph, color, value, label in items:
            self.icon(d, x + 14, 71, glyph, 28, color)
            self.text(d, (x + 38, 74), value, "semibold", 20, TEXT)
            self.text(d, (x + 38, 89), label, "regular", 11, MUTED if color == TEXT else color)

        self.line(d, [(170, 54), (170, 88)], BORDER, 1)

    def chart(self, img, s):
        box = s["box"]
        d = ImageDraw.Draw(img, "RGBA")
        self.rrect(d, (12, 112, 328, 246), 14, SURFACE, BORDER, 1)

        x = 24
        legend = [("Temp", YELLOW), ("Humidity", BLUE)]

        if box.flowering_start:
            legend.append(("Flower", PURPLE))

        for name, color in legend:
            d.rounded_rectangle(self.p(x, 130, x + 10, 132), radius=self.k, fill=color)
            self.text(d, (x + 14, 135), name, "regular", 10, MUTED)
            x += 14 + self.text_w(name, "regular", 10) + 10

        self.text(d, (316, 135), "24 h", "regular", 10, MUTED, "rs")

        x0, x1, y0, y1 = 24, 316, 144, 234
        readings = s["readings"]
        end = readings[-1][0] if readings else box.now
        start = end - SPAN

        def xt(t):
            return x0 + (x1 - x0) * (t - start).total_seconds() / SPAN.total_seconds()

        def yv(v, scale):
            lo, hi = scale
            return y1 - (v - lo) / (hi - lo) * (y1 - y0)

        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer, "RGBA")

        if box.response is not None:
            # target bands in 5 min steps, merged into segments
            stretches = []
            t = start

            while t < end:
                nxt = min(t + dt.timedelta(minutes=5), end)
                band = box.targets(t)

                if stretches and stretches[-1][2] == band:
                    stretches[-1][1] = nxt
                else:
                    stretches.append([t, nxt, band])
                t = nxt

            for a, b, (tmin, tmax, hmin, hmax) in stretches:
                ld.rectangle(self.p(xt(a), yv(hmax, HUM_SCALE), xt(b), yv(hmin, HUM_SCALE)), fill=BLUE + (26,))
                ld.rectangle(self.p(xt(a), yv(tmax, TEMP_SCALE), xt(b), yv(tmin, TEMP_SCALE)), fill=YELLOW + (36,))

        # grid every 6 h
        mark = start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)

        while mark < end:
            if mark.hour % 6 == 0:
                ld.line(self.p(xt(mark), y0, xt(mark), y1), fill=BORDER + (255,), width=round(self.k))
            mark += dt.timedelta(hours=1)

        # flowering start
        flower = box.flowering_start

        if flower and start < flower <= end:
            y = y0

            while y < y1:
                ld.line(self.p(xt(flower), y, xt(flower), min(y + 4, y1)), fill=PURPLE + (255,), width=round(2 * self.k))
                y += 7

        # every 4th reading (20 min) counting from the newest, like in the app
        rows = [r for r in readings if r[0] >= start]
        points = [r for i, r in enumerate(rows) if (len(rows) - 1 - i) % 4 == 0]

        for idx, color, scale in ((2, BLUE, HUM_SCALE), (1, YELLOW, TEMP_SCALE)):
            pts = [tuple(self.p(xt(r[0]), yv(r[idx], scale))) for r in points]

            if len(pts) >= 2:
                ld.line(pts, fill=color + (255,), width=round(1.3 * self.k), joint="curve")

        # chart clipped to its own area
        clip = Image.new("L", img.size, 0)
        ImageDraw.Draw(clip).rectangle(self.p(x0, y0, x1, y1), fill=255)
        layer.putalpha(Image.composite(layer.getchannel("A"), clip, clip))
        img.paste(layer, (0, 0), layer)

        d = ImageDraw.Draw(img, "RGBA")
        self.text(d, (x0 + 2, y0 + 11), "30°", "regular", 9, YELLOW + (204,))
        self.text(d, (x0 + 2, y1 - 3), "15°", "regular", 9, YELLOW + (204,))
        self.text(d, (x1 - 2, y0 + 11), "90%", "regular", 9, BLUE + (204,), "rs")
        self.text(d, (x1 - 2, y1 - 3), "30%", "regular", 9, BLUE + (204,), "rs")

        if not readings and (s["loading"] or s["syncing"]):
            self.text(d, ((x0 + x1) / 2, (y0 + y1) / 2 + 4), "Loading 24 h history…", "regular", 11, MUTED, "ms")

    def status(self, d, s):
        """Week / Fan / Humidifier card; returns the fan centre."""
        box = s["box"]
        self.rrect(d, (12, 256, 328, 361), 14, SURFACE, BORDER, 1)

        if box.response is None:
            self.text(d, (170, 312), "--", "regular", 13, MUTED, "mm")
            return None

        columns = []
        week = box.flowering_week

        if week:
            columns.append("week")

        columns.append("fan")
        humidifier = box.status.get("humidifier")

        if humidifier:
            columns.append("humidifier")

        left, right = 24, 316
        width = (right - left) / len(columns)
        cy = 309
        fan_center = None

        for i, col in enumerate(columns):
            cx = left + width * (i + 0.5)

            if i:
                self.line(d, [(left + width * i, 268), (left + width * i, 349)], BORDER, 1)

            if col == "week":
                n, progress = week
                label, detail, detail_color = "Week", "%d%% of week %d" % (round(progress * 100), n), MUTED
                self.ring(d, cx, cy, progress, GREEN, thin=True)
                self.text(d, (cx, cy), str(n), "bold", 17, GREEN, "mm")
            elif col == "fan":
                label, detail_color = "Fan", MUTED
                detail = "%d%%" % box.fan_level if box.fan_on else "off · %d%%" % box.fan_set_level
                self.ring(d, cx, cy, box.fan_level / 100, GREEN)
                # static (snapshot); in the window the animation covers it
                self.fan(d, cx, cy, GREEN if box.fan_level else OFF)
                fan_center = (cx, cy)
            else:
                online = humidifier.get("online", False)
                on = online and humidifier.get("on", False)
                band = box.humidifier_band
                label = "Humidifier"

                if not online:
                    detail = "offline"
                elif band:
                    detail = "%d–%d%%" % (band.get("min", 0), band.get("max", 0)) if band.get("enabled") else "disabled"
                else:
                    detail = ""

                detail_color = MUTED if online else RED
                self.ring(d, cx, cy, 1 if on else 0, BLUE)
                self.icon(d, cx, cy, WATER if on else WATER_OUTLINE, 20, BLUE if on else OFF)

            self.text(d, (cx, 278), label, "regular", 11, MUTED, "ms")
            self.text(d, (cx, 349), detail, "regular", 10, detail_color, "ms")

        return fan_center

    def feeding(self, d, s):
        box = s["box"]
        loaded = box.response is not None
        status = box.feeding_status
        accent = None if not loaded else RED if status == "overdue" else YELLOW if status == "late" else None
        top, bottom = 371, 425
        self.rrect(d, (12, top, 328, bottom), 14, SURFACE, accent or BORDER, 1)
        self.icon(d, 35, 398, WATER_OUTLINE, 22, accent or BLUE)
        self.text(d, (56, 392), "Last fed", "regular", 11, MUTED)

        if not loaded:
            self.text(d, (56, 412), "--", "semibold", 16, TEXT)
            return

        label = box.last_fed_label
        self.text(d, (56, 412), label, "semibold", 16, accent or TEXT)
        n = box.feeding_count
        self.text(d, (56 + self.text_w(label, "semibold", 16), 412),
                  " · %d %s" % (n, "feed" if n == 1 else "feeds"), "regular", 11, MUTED)

        if box.fed_recently:
            w = self.text_w("Feeded", "bold", 13)
            self.text(d, (316, 403), "Feeded", "bold", 13, GREEN, "rs")
            cx, cy, r = 316 - w - 11, 398.5, 6.5
            d.ellipse(self.p(cx - r, cy - r, cx + r, cy + r), fill=GREEN)
            self.line(d, [(cx - 3, cy), (cx - 0.8, cy + 2.4), (cx + 3.2, cy - 2.4)], SURFACE, 1.6)
        elif accent:
            self.text(d, (316, 403), "Feed me now!" if status == "overdue" else "Time to feed", "bold", 12, accent, "rs")


def build_state(poller):
    with poller.lock:
        response, readings = poller.response, list(poller.readings)

    box = Box(response, dt.datetime.now())
    return {
        "box": box,
        "readings": readings,
        "error": poller.error,
        "searching": poller.searching,
        "syncing": poller.syncing_history,
        "loading": response is None and (poller.error is None or poller.searching),
    }


# --- tray ---

def tray_image(text, color, size=64):
    """Tray icon: just the number, like the menu bar text on the Mac."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    path = next((p for p in _fonts()["bold"] if os.path.exists(p)), None)
    px = big

    # the largest size at which the number fits in the icon
    while True:
        font = ImageFont.truetype(path, px) if path else ImageFont.load_default(px)
        left, top, right, bottom = d.textbbox((0, 0), text, font=font)

        if px <= 16 or (right - left <= big * 0.98 and bottom - top <= big * 0.72):
            break

        px -= 8

    d.text(((big - (right - left)) / 2 - left, (big - (bottom - top)) / 2 - top), text, font=font, fill=color)
    return img.resize((size, size), Image.LANCZOS)


class Tray:
    """Two tray icons: temperature and humidity. The menu calls
    post(action) - the window thread runs the actions."""

    def __init__(self, post, checked):
        import pystray

        Item = pystray.MenuItem
        menu = pystray.Menu(
            Item("Show / hide panel", lambda: post("toggle"), default=True),
            Item("Refresh now", lambda: post("refresh")),
            pystray.Menu.SEPARATOR,
            Item("Numbers on the taskbar", lambda: post("bar"), checked=lambda _: checked("bar")),
            Item("Tray icons", lambda: post("tray"), checked=lambda _: checked("tray")),
            Item("Panel always on top", lambda: post("topmost"), checked=lambda _: checked("topmost")),
            Item("Start with Windows", lambda: post("autostart"), checked=lambda _: checked("autostart"),
                 visible=sys.platform == "win32"),
            pystray.Menu.SEPARATOR,
            Item("Quit", lambda: post("quit")),
        )
        self.icons = {}
        self.shown = {}

        for key in ("hum", "temp"):  # Windows adds new icons on the left
            icon = pystray.Icon("growbox-" + key, tray_image("--", MUTED), "GrowBox", menu)
            icon.run_detached()
            self.icons[key] = icon

    def update(self, box, error):
        temp = box.sensor.get("temperature")
        hum = box.sensor.get("humidity")
        offline = box.response is None or error

        values = {
            "temp": ("--" if offline or temp is None else "%d" % round(temp), MUTED if offline else YELLOW),
            "hum": ("--" if offline or hum is None else "%d" % round(hum), MUTED if offline else BLUE),
        }

        if box.response is None:
            tip = "GrowBox: " + (error or "connecting…")
        else:
            tip = "GrowBox: %s · %s · %s" % (
                "%.1f°C" % temp if temp is not None else "--",
                "%.1f%%" % hum if hum is not None else "--",
                error or ("AUTO" if box.flowering else "MANUAL"),
            )

        for key, icon in self.icons.items():
            if self.shown.get(key) != values[key]:
                self.shown[key] = values[key]
                icon.icon = tray_image(*values[key])

            if icon.title != tip:
                icon.title = tip

    def refresh_menu(self):
        for icon in self.icons.values():
            icon.update_menu()

    def stop(self):
        for icon in self.icons.values():
            try:
                icon.stop()
            except Exception:
                pass


# --- numbers on the taskbar (next to the Windows weather) ---

def light_taskbar():
    """Light taskbar theme (Settings > Personalization > Colors)."""
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def taskbar_rect():
    """(left, top, right, bottom) of the taskbar in pixels, or None."""
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
    rect = wintypes.RECT()

    if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    return rect.left, rect.top, rect.right, rect.bottom


def fullscreen_app():
    """Whether the foreground window is full screen (game, video)."""
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()

    if not hwnd:
        return False

    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)

    if name.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left <= 0 and rect.top <= 0 and rect.right >= user32.GetSystemMetrics(0)
            and rect.bottom >= user32.GetSystemMetrics(1))


def set_z_order(window, after):
    """Moves a Tk window in the window stack: after=-1 to the top (HWND_TOPMOST),
    1 to the bottom (HWND_BOTTOM). Without argtypes ctypes passes -1 as a 32-bit
    0xFFFFFFFF and on 64-bit Windows the call silently does nothing."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
    hwnd = user32.GetParent(window.winfo_id())
    # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x13)


class TaskbarLabel:
    """Temperature and humidity on the taskbar, in two lines like the Windows
    weather next to it. A small always-on-top window laid on the taskbar; dragged
    sideways with the mouse, left click shows/hides the panel, right = menu."""

    OFFSET = 170  # default (pt from the taskbar's left edge): right after the weather

    def __init__(self, widget):
        tk = widget.tk
        self.widget = widget
        self.painter = widget.painter
        self.light = light_taskbar()
        key = "#%02x%02x%02x" % KEY

        self.top = tk.Toplevel(widget.root)
        self.top.overrideredirect(True)
        self.top.configure(bg=key)

        if sys.platform == "win32":
            self.top.attributes("-transparentcolor", key)

        self.top.attributes("-topmost", True)
        self.label = tk.Label(self.top, bd=0, bg=key)
        self.label.pack()
        self.photo = None
        self.size = (1, 1)
        self.shown = None
        self.hidden = False
        self._drag = None

        self.label.bind("<ButtonPress-1>", self.press)
        self.label.bind("<B1-Motion>", self.move)
        self.label.bind("<ButtonRelease-1>", self.release)
        self.label.bind("<Button-3>", widget.menu)
        self.keep()

    def render(self, box, error):
        p = self.painter
        temp = box.sensor.get("temperature")
        hum = box.sensor.get("humidity")
        offline = box.response is None or error
        line1 = "--" if offline or temp is None else "%.1f°C" % temp
        line2 = (error or "connecting…") if offline else "--" if hum is None else "%.1f%%" % hum
        text = (30, 30, 30) if self.light else (255, 255, 255)
        yellow, blue = ((176, 120, 0), (0, 95, 184)) if self.light else (YELLOW, BLUE)  # readable on light
        icon = MUTED if offline else yellow if box.light_on else text
        glyph = SUNNY_OUTLINE if offline or box.light_on else MOON_OUTLINE
        h = 40
        w = 34 + max(p.text_w(line1, "regular", 11.5), p.text_w(line2, "regular", 11.5)) + 6

        img = Image.new("RGBA", (round(w * p.k), round(h * p.k)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        p.icon(d, 14, h / 2, glyph, 24, icon)
        p.text(d, (32, 18), line1, "regular", 11.5, text)
        p.text(d, (32, 34), line2, "regular", 11.5, MUTED if offline else blue)
        img = img.resize((round(w * p.scale), round(h * p.scale)), Image.LANCZOS)

        # letter edges blended with the taskbar colour, the rest transparent
        under = Image.new("RGB", img.size, (243, 243, 243) if self.light else (28, 28, 28))
        out = Image.new("RGB", img.size, KEY)
        out.paste(Image.composite(img.convert("RGB"), under, img.getchannel("A")), (0, 0),
                  img.getchannel("A").point(lambda a: 255 if a > 8 else 0))
        return out

    def update(self, box, error):
        from PIL import ImageTk

        key = (box.sensor.get("temperature"), box.sensor.get("humidity"), box.response is None, error,
               box.light_on)

        if key == self.shown:
            return

        self.shown = key
        img = self.render(box, error)
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)
        self.size = img.size
        self.place()

    def place(self):
        rect = taskbar_rect()
        w, h = self.size
        offset = round(self.widget.settings.get("barOffset", self.OFFSET) * self.painter.scale)

        if rect:
            left, top, right, bottom = rect
            x = max(left, min(left + offset, right - w))
            y = top + max(0, (bottom - top - h) // 2)
        else:
            x, y = offset, self.top.winfo_screenheight() - h - 4

        self.top.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def keep(self):
        """The taskbar covers the small window after a click on it - every half
        second we go back on top; during a full-screen game/video we hide."""
        if fullscreen_app():
            if not self.hidden:
                self.top.withdraw()
                self.hidden = True
        else:
            if self.hidden:
                self.top.deiconify()
                self.hidden = False
                self.place()

            if sys.platform == "win32":
                set_z_order(self.top, -1)

        self.after = self.top.after(500, self.keep)

    def press(self, e):
        self._drag = (e.x_root, self.top.winfo_x(), False)

    def move(self, e):
        start, x0, _ = self._drag

        if abs(e.x_root - start) > 3:
            self._drag = (start, x0, True)
            self.top.geometry("+%d+%d" % (x0 + e.x_root - start, self.top.winfo_y()))

    def release(self, _e):
        if not self._drag:
            return

        _, _, moved = self._drag
        self._drag = None

        if not moved:
            self.widget.toggle_panel()
            return

        rect = taskbar_rect()
        left = rect[0] if rect else 0
        self.widget.settings["barOffset"] = round((self.top.winfo_x() - left) / self.painter.scale)
        self.widget.save()

    def destroy(self):
        try:
            self.top.after_cancel(self.after)
        except Exception:
            pass

        self.top.destroy()


# --- Windows ---

def set_dpi_awareness():
    if sys.platform != "win32":
        return

    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command():
    exe = sys.executable

    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"

    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def autostart_enabled():
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    if sys.platform != "win32":
        return

    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP)
            except OSError:
                pass


# --- window ---

class Widget:
    def __init__(self, snapshot=None):
        import tkinter as tk
        from tkinter import simpledialog

        self.tk = tk
        self.snapshot_path = snapshot
        self.settings = load_settings()
        set_dpi_awareness()

        self.root = tk.Tk()
        self.root.title(APP)
        self.root.withdraw()

        if not self.settings.get("token") and not snapshot:
            token = simpledialog.askstring(
                APP, "Paste the API token of the main ESP\n(the same one the app and the macOS widget use):",
                parent=self.root)

            if not token:
                self.root.destroy()
                sys.exit(0)

            self.settings["token"] = token.strip()
            self.save()

        scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.painter = Painter(scale)
        key = "#%02x%02x%02x" % KEY

        self.root.overrideredirect(True)
        self.root.configure(bg=key)

        if sys.platform == "win32":
            self.root.attributes("-transparentcolor", key)

        w, h = self.painter.size
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = max(0, min(self.settings.get("x", sw - w - 24), sw - 40))
        y = max(0, min(self.settings.get("y", 24), sh - 40))
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

        self.label = tk.Label(self.root, bd=0, bg=key)
        self.label.place(x=0, y=0)
        self.fan_label = tk.Label(self.root, bd=0, highlightthickness=0)
        self.photo = None
        self.fan_frames = {}
        self.fan_color = None
        self.fan_level = 0

        for widget in (self.label, self.fan_label):
            widget.bind("<ButtonPress-1>", self.drag_start)
            widget.bind("<B1-Motion>", self.drag_move)
            widget.bind("<ButtonRelease-1>", self.drag_end)
            widget.bind("<Button-3>", self.menu)

        self.poller = Poller(self.settings.get("url") or DEFAULT_URL, self.settings.get("token", ""), self.url_changed)
        self.poller.start()
        self.seen = None
        self.actions = queue.Queue()
        self.tray = None
        self.bar = None
        self.state = None

        if not snapshot:
            if self.settings.get("taskbarLabel", True):
                self.set_bar(True)

            if self.settings.get("trayIcons", False):
                self.set_tray(True)

        # a hidden panel stays hidden after a restart (if there is a way to show it)
        if not (self.can_hide() and self.settings.get("panelHidden")):
            self.root.deiconify()
            self.apply_level()

        self.tick()
        self.animate()
        self.pump()

    def save(self):
        save_settings(self.settings)

    def url_changed(self, url):
        self.settings["url"] = url
        self.save()

    # below other windows, like the Mac widget (unless "always on top")
    def apply_level(self):
        top = bool(self.settings.get("topmost"))
        self.root.attributes("-topmost", top)

        if not top and sys.platform == "win32":
            set_z_order(self.root, 1)

    def tick(self):
        version = (self.poller.version, dt.datetime.now().strftime("%H:%M"))

        if version != self.seen:
            self.seen = version
            self.draw()

        if self.snapshot_path and self.poller.response is not None and self.poller.readings:
            self.painter.render(build_state(self.poller))[0].save(self.snapshot_path)
            self.root.destroy()
            return

        self.root.after(500, self.tick)

    def draw(self):
        from PIL import ImageTk

        state = build_state(self.poller)
        img, fan_center = self.painter.render(state)
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)
        alert_if_feeding_missed(state["box"], self.settings, self.save)

        self.state = state

        if self.tray:
            self.tray.update(state["box"], state["error"])

        if self.bar:
            self.bar.update(state["box"], state["error"])

        if fan_center is None:
            self.fan_label.place_forget()
            return

        box = state["box"]
        self.fan_level = box.fan_level
        color = GREEN if self.fan_level else OFF

        if color != self.fan_color:
            self.fan_color = color
            self.fan_frames = {}

        size = 26
        s = self.painter.scale
        self.fan_label.place(x=round((fan_center[0] - size / 2) * s), y=round((fan_center[1] - size / 2) * s))
        self.show_fan_frame()

    FAN_STEPS = 24  # frames per 120° (the blades are symmetric)

    def show_fan_frame(self):
        from PIL import ImageTk

        if self.fan_level:
            per_turn = 2.4 - 1.32 * self.fan_level / 100
            i = int((time.monotonic() % (per_turn / 3)) / (per_turn / 3) * self.FAN_STEPS)
        else:
            i = 0

        if i not in self.fan_frames:
            self.fan_frames[i] = ImageTk.PhotoImage(self.painter.fan_frame(self.fan_color, i * 120 / self.FAN_STEPS))

        self.fan_label.configure(image=self.fan_frames[i])

    def animate(self):
        # the fan spins faster at higher power: 2.4 s per turn
        # at 0 %, 1.08 s at 100 %
        if self.fan_level and self.fan_color:
            self.show_fan_frame()

        self.root.after(33, self.animate)

    def drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry("+%d+%d" % (e.x_root - dx, e.y_root - dy))

    def drag_end(self, _e):
        self.settings["x"], self.settings["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.save()
        self.apply_level()

    def menu(self, e):
        tk = self.tk
        m = tk.Menu(self.root, tearoff=0)
        top = tk.BooleanVar(value=bool(self.settings.get("topmost")))
        auto = tk.BooleanVar(value=autostart_enabled())
        bar = tk.BooleanVar(value=bool(self.bar))
        tray = tk.BooleanVar(value=bool(self.tray))
        hidden = self.root.state() == "withdrawn"

        m.add_command(label="Refresh now", command=self.poller.refresh)

        if self.can_hide() or hidden:
            m.add_command(label="Show panel" if hidden else "Hide panel", command=self.toggle_panel)

        m.add_separator()
        m.add_checkbutton(label="Numbers on the taskbar", variable=bar, command=lambda: self.set_bar(bar.get()))
        m.add_checkbutton(label="Tray icons", variable=tray, command=lambda: self.set_tray(tray.get()))
        m.add_checkbutton(label="Always on top", variable=top, command=lambda: self.set_topmost(top.get()))

        if sys.platform == "win32":
            m.add_checkbutton(label="Start with Windows", variable=auto,
                              command=lambda: self.set_autostart(auto.get()))

        m.add_command(label="Change token…", command=self.change_token)
        m.add_command(label="ESP: %s" % self.poller.url, state="disabled")
        m.add_separator()
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def set_topmost(self, value):
        self.settings["topmost"] = value
        self.save()
        self.apply_level()

        if self.tray:
            self.tray.refresh_menu()

    def set_autostart(self, value):
        set_autostart(value)

        if self.tray:
            self.tray.refresh_menu()

    # --- tray: actions from the icon menus, run in the window thread ---

    def can_hide(self):
        return bool(self.bar or self.tray)

    def set_bar(self, value):
        if value and not self.bar:
            self.bar = TaskbarLabel(self)

            if self.state:
                self.bar.update(self.state["box"], self.state["error"])
        elif not value and self.bar:
            self.bar.destroy()
            self.bar = None

        self.settings["taskbarLabel"] = bool(self.bar)
        self.after_switch()

    def set_tray(self, value):
        if value and not self.tray:
            try:
                self.tray = Tray(self.actions.put, self.tray_checked)
            except Exception as e:  # no pystray
                print("Tray:", e)
            else:
                if self.state:
                    self.tray.update(self.state["box"], self.state["error"])
        elif not value and self.tray:
            self.tray.stop()
            self.tray = None

        self.settings["trayIcons"] = bool(self.tray)
        self.after_switch()

    def after_switch(self):
        # without the taskbar and tray icons a hidden panel would have no way back
        if not self.can_hide() and self.root.state() == "withdrawn":
            self.toggle_panel()

        self.save()

        if self.tray:
            self.tray.refresh_menu()

    def tray_checked(self, name):
        if name == "topmost":
            return bool(self.settings.get("topmost"))

        if name == "bar":
            return bool(self.bar)

        if name == "tray":
            return bool(self.tray)

        return autostart_enabled()

    def pump(self):
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                break

            if action == "toggle":
                self.toggle_panel()
            elif action == "refresh":
                self.poller.refresh()
            elif action == "topmost":
                self.set_topmost(not self.settings.get("topmost"))
            elif action == "autostart":
                self.set_autostart(not autostart_enabled())
            elif action == "bar":
                self.set_bar(not self.bar)
            elif action == "tray":
                self.set_tray(not self.tray)
            elif action == "quit":
                self.quit()
                return

        self.root.after(100, self.pump)

    def toggle_panel(self):
        hidden = self.root.state() == "withdrawn"

        if hidden:
            self.root.deiconify()
            self.apply_level()
            self.root.lift()
        else:
            self.root.withdraw()

        self.settings["panelHidden"] = not hidden
        self.save()

    def quit(self):
        if self.tray:
            self.tray.stop()

        if self.bar:
            self.bar.destroy()

        self.root.destroy()

    def change_token(self):
        from tkinter import simpledialog

        token = simpledialog.askstring(APP, "New API token of the main ESP:", parent=self.root)

        if token:
            self.settings["token"] = token.strip()
            self.save()
            self.poller.set_token(self.settings["token"])

    def run(self):
        self.root.mainloop()


def main():
    snapshot = sys.argv[sys.argv.index("--snapshot") + 1] if "--snapshot" in sys.argv else None

    # one instance at a time - a second one would only add requests to the ESP
    lock = socket.socket()

    try:
        lock.bind(("127.0.0.1", 47811))
    except OSError:
        if not snapshot:
            return

    Widget(snapshot).run()


if __name__ == "__main__":
    main()

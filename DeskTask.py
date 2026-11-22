"""
DeskTask - minimalist task cards that live on your desktop.

Tray menu (right-click the icon):  Add task, Tasks, Customize, Start with Windows, Exit
Hotkey:                            Ctrl+Alt+N  adds a task from anywhere
Cards:                             drag anywhere; drop on the green check to complete;
                                   right-click a card to delete it (restorable from Tasks)
"""
import tkinter as tk
import tkinter.font as tkfont
import atexit, collections, ctypes, faulthandler, json, shutil, threading, math, os, queue, sys, time, traceback, uuid
from ctypes import wintypes as W_
from datetime import date, datetime, timedelta
import winreg
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
import pystray



def set_dpi_aware():
    """Opt in to DPI awareness so Windows never bitmap-scales (blurs) our windows."""
    for fn in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2)),
               lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
               lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            fn()
            return
        except Exception:
            continue


set_dpi_aware()

APP_NAME = "DeskTask"
VERSION = "1.0.4"
APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
DATA_DIR = os.path.join(APPDATA, "DeskTask")
FILE = os.path.join(DATA_DIR, "data.json")
LEGACY_FILES = [os.path.join(APPDATA, "DesktopTasks", "data.json"),
                os.path.join(os.path.expanduser("~"), "desktop_tasks_v2.json")]
MSG_TITLE = "DeskTaskMessageWindow"

ACCENTS = {"Blue": "#0A84FF", "Green": "#30D158", "Orange": "#FF9F0A",
           "Pink": "#FF375F", "Purple": "#BF5AF2"}
THEMES = {
    "light": dict(card="#FFFFFF", text="#1C1C1E", border="#E5E5EA", bg="#F2F2F7", row="#FFFFFF",
                  sub="#8E8E93", sep="#E5E5EA", seg="#E3E3E8", seg_on="#FFFFFF", track="#E5E5EA"),
    "dark":  dict(card="#2C2C2E", text="#F2F2F7", border="#3A3A3C", bg="#1C1C1E", row="#2C2C2E",
                  sub="#98989D", sep="#3A3A3C", seg="#2C2C2E", seg_on="#636366", track="#48484A"),
}
GREEN, RED = "#30D158", "#FF453A"
SS = 4          # supersampling for cards (smooth edges and text)
PANEL_SS = 3    # supersampling for the Tasks / Customize windows
PAD = 30        # shadow margin around windows (logical px)

WEIGHTS = ("regular", "semibold", "bold", "extra")
state = {"settings": {"theme": "light", "accent": "Blue", "size": 15,
                      "opacity": 95, "behind": True, "weight": "semibold",
                      "capitalize": False},
         "tasks": [],
         "history": {},                      # completed-per-day counts (survives deleting tasks)
         "meta": {"tutorial_seen": False}}

# runtime globals (set in main)
root = None
S = 1.0
TK_FONT = "Segoe UI"
icon = None
target = None
hotkey_win = None
HINST = None
cards, panels, q = {}, {}, queue.Queue()
add_ui = None
_crash_file = None
_mutex = None
menu_state = {"left": 0, "startup": False}


# ================================================================== helpers
def log_text(text):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except Exception:
        pass


def log_exc():
    log_text(traceback.format_exc())


def px(n):
    return int(n * S)


def display_text(text):
    """Task text as shown on screen (optionally Capitalizing Each Word). Stored text is never changed."""
    text = str(text)
    if not state["settings"].get("capitalize"):
        return text
    return "\n".join(" ".join(w[:1].upper() + w[1:] for w in line.split(" ")) for line in text.split("\n"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def theme():
    return THEMES[state["settings"]["theme"]]


def accent():
    return ACCENTS[state["settings"]["accent"]]


def mix(c1, c2, t):
    """Blend hex colours; t is the weight of c1."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] * t + b[i] * (1 - t)) for i in range(3))


def when(iso):
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return ""
    d = dt.date()
    if d == date.today():
        return f"today {dt:%H:%M}"
    if d == date.today() - timedelta(days=1):
        return f"yesterday {dt:%H:%M}"
    return f"{dt:%b} {dt.day}, {dt:%H:%M}"


# ================================================================== data
def load_state():
    """Loads saved data. Returns True if an existing data file was found."""
    for path in [FILE, FILE + ".bak"] + LEGACY_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        st = data.get("settings", {})
        for k, v in st.items():
            if k in state["settings"]:
                state["settings"][k] = v
        if "weight" not in st and "bold" in st:               # older versions
            state["settings"]["weight"] = "semibold" if st["bold"] else "regular"
        if state["settings"]["weight"] not in WEIGHTS:
            state["settings"]["weight"] = "semibold"
        tasks = []
        for t in data.get("tasks", []):
            if isinstance(t, dict) and "id" in t and "text" in t:
                t.setdefault("status", "active")
                t.setdefault("created", now_iso())
                t.setdefault("x", None)
                t.setdefault("y", None)
                tasks.append(t)
        state["tasks"] = tasks
        hist = data.get("history")
        if not isinstance(hist, dict):                         # rebuild from completed tasks
            hist = {}
            for t in tasks:
                if t["status"] == "done" and t.get("done_at"):
                    hist[t["done_at"][:10]] = hist.get(t["done_at"][:10], 0) + 1
        state["history"] = {str(k): int(v) for k, v in hist.items() if isinstance(v, (int, float))}
        state["meta"]["tutorial_seen"] = bool(data.get("meta", {}).get("tutorial_seen", False))
        return True
    return False


def save():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(FILE):
            shutil.copyfile(FILE, FILE + ".bak")               # keep the last good copy
        os.replace(tmp, FILE)
    except Exception:
        log_exc()


def tasks_with(status):
    return [t for t in state["tasks"] if t["status"] == status]


def bump_history(iso, delta):
    day = str(iso)[:10]
    n = max(0, state["history"].get(day, 0) + delta)
    if n:
        state["history"][day] = n
    else:
        state["history"].pop(day, None)


def week_start(d=None):
    """The most recent Saturday (weeks run Saturday to Friday)."""
    d = d or date.today()
    return d - timedelta(days=(d.weekday() - 5) % 7)


def week_counts():
    start = week_start()
    days = [start + timedelta(days=i) for i in range(7)]
    return [(d, int(state["history"].get(d.isoformat(), 0))) for d in days]


def today_count():
    return int(state["history"].get(date.today().isoformat(), 0))


def card_rect(task):
    c = cards.get(task["id"])
    w, h = (c.w - 2 * c.pad, c.h - 2 * c.pad) if c else (px(300), px(60))
    return (task["x"], task["y"], w, h)


def overlap_fraction(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return (ix * iy) / max(1, min(aw * ah, bw * bh))


def free_slot(taken, size=None):
    """First position on the desktop where a card would not cover any rectangle in `taken`."""
    w, h = size or (px(300), px(60))
    left, top, right, bottom = work_area(0, 0)
    step, col_w = px(72), px(320)
    rows = max(1, (bottom - top - px(160) - px(40)) // step)
    cols = max(1, (right - left - px(350) - w) // col_w + 1)
    for col in range(cols):
        for row in range(rows):
            x, y = left + px(350) + col * col_w, top + px(160) + row * step
            if all(overlap_fraction((x, y, w, h), r) == 0 for r in taken):
                return x, y
    n = len(taken)                                              # screen is full: cascade
    return left + px(350) + (n % 10) * px(24), top + px(160) + (n % 10) * px(24)


def next_pos(exclude=None, size=None):
    taken = [card_rect(t) for t in tasks_with("active")
             if t.get("x") is not None and t["id"] != exclude]
    return free_slot(taken, size)


def place_card(task, x, y):
    c = cards.get(task["id"])
    task["x"], task["y"] = x, y
    if c:
        c.move(x - c.pad, y - c.pad)
        if state["settings"]["behind"]:
            c.to_bottom()


def arrange_cards():
    """Neatly re-lays out every card so none covers another."""
    taken = []
    for t in sorted(tasks_with("active"), key=lambda t: t.get("created", "")):
        c = cards.get(t["id"])
        size = (c.w - 2 * c.pad, c.h - 2 * c.pad) if c else None
        x, y = free_slot(taken, size)
        place_card(t, x, y)
        taken.append(card_rect(t))
    save()


def separate_overlaps():
    """Fixes cards that were stacked on top of each other (including by older versions)."""
    actives = [t for t in tasks_with("active") if t["id"] in cards]
    done = []
    for t in actives:
        r = card_rect(t)
        if any(overlap_fraction(r, p) > 0.3 for p in done):
            others = [card_rect(o) for o in actives if o is not t]
            x, y = free_slot(others, (r[2], r[3]))
            place_card(t, x, y)
            r = card_rect(t)
        done.append(r)
    save()


def add_task(text):
    text = text.strip()[:300]
    x, y = next_pos()
    task = {"id": uuid.uuid4().hex, "text": text, "status": "active",
            "created": now_iso(), "x": x, "y": y}
    state["tasks"].append(task)
    cards[task["id"]] = Card(task)
    save()
    refresh_all()


def complete_task(task):
    task["status"] = "done"
    task["done_at"] = now_iso()
    bump_history(task["done_at"], 1)
    card = cards.pop(task["id"], None)
    if card:
        card.fade_out()
    save()
    refresh_all()
    if not tasks_with("active"):
        try:
            icon.notify("Everything on your list is done.", "All clear")
        except Exception:
            pass


def delete_task(task):
    task["status"] = "deleted"
    task["deleted_at"] = now_iso()
    card = cards.pop(task["id"], None)
    if card:
        card.fade_out()
    deleted = sorted(tasks_with("deleted"), key=lambda t: t.get("deleted_at", ""))
    for old in deleted[:-100]:                 # keep the last 100 deleted tasks
        state["tasks"].remove(old)
    save()
    refresh_all()


def restore_task(task):
    if task.get("done_at"):
        bump_history(task["done_at"], -1)                      # it will be completed again later
    task["status"] = "active"
    task.pop("done_at", None)
    task.pop("deleted_at", None)
    if task.get("x") is None or any(
            overlap_fraction(card_rect(task), card_rect(o)) > 0.3
            for o in tasks_with("active") if o is not task and o.get("x") is not None):
        task["x"], task["y"] = next_pos(exclude=task["id"])
    cards[task["id"]] = Card(task)
    save()
    refresh_all()


def clear_deleted():
    state["tasks"] = [t for t in state["tasks"] if t["status"] != "deleted"]
    save()
    refresh_all()


def refresh_all():
    for p in list(panels.values()):
        p.render()
    update_tray()


def refresh_menu_state():
    menu_state["left"] = len(tasks_with("active"))
    menu_state["startup"] = startup_enabled()


def update_tray():
    refresh_menu_state()
    if icon is None:
        return
    try:
        left = len(tasks_with("active"))
        today = today_count()
        icon.title = f"{APP_NAME}: {left} left, {today} done today"
    except Exception:
        pass


# ================================================================== fonts
FONT_FILES = {
    "regular": ["SF-Pro-Display-Regular.otf", "SF-Pro-Text-Regular.otf", "SegUIVar.ttf",
                "segoeui.ttf", "arial.ttf"],
    "semibold": ["SF-Pro-Display-Semibold.otf", "SF-Pro-Text-Semibold.otf", "seguisb.ttf",
                 "segoeuib.ttf", "arialbd.ttf"],
    "bold": ["SF-Pro-Display-Bold.otf", "SF-Pro-Text-Bold.otf", "segoeuib.ttf", "arialbd.ttf"],
    "extra": ["SF-Pro-Display-Heavy.otf", "SF-Pro-Display-Black.otf", "SF-Pro-Text-Heavy.otf",
              "seguibl.ttf", "segoeuib.ttf", "ariblk.ttf", "arialbd.ttf"],
}


def find_font(weight):
    dirs = [os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]
    for n in FONT_FILES[weight]:
        for d in dirs:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


FONT_PATH = {}
_font_cache = {}


def load_font(size, weight="regular"):
    w = weight if isinstance(weight, str) else ("semibold" if weight else "regular")
    key = (int(size), w)
    if key not in _font_cache:
        if w not in FONT_PATH:
            FONT_PATH[w] = find_font(w)
        p = FONT_PATH[w]
        _font_cache[key] = (ImageFont.truetype(p, int(size)) if p
                            else ImageFont.load_default(int(size)))
    return _font_cache[key]


def wrap(text, font, maxw):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            t = word if not cur else cur + " " + word
            if not cur or font.getlength(t) <= maxw:
                cur = t
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


# ================================================================== smooth drawing
class Gfx:
    """Draws in logical units onto a supersampled image; records clickable regions."""

    def __init__(self, lw, lh, ss, origin=0):
        self.ss, self.k, self.ox = ss, S * ss, origin
        self.W, self.H = int(lw * S), int(lh * S)
        self.img = Image.new("RGBA", (self.W * ss, self.H * ss), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.hits, self.texts = [], []

    def _p(self, v):
        return (v + self.ox) * self.k

    def rrect(self, x, y, w, h, r, fill=None, outline=None, bw=1):
        self.d.rounded_rectangle((self._p(x), self._p(y), self._p(x + w) - 1, self._p(y + h) - 1),
                                 radius=r * self.k, fill=fill, outline=outline,
                                 width=max(1, round(bw * self.k)))

    def poly(self, pts, fill=None, outline=None, bw=1):
        big = [(self._p(x), self._p(y)) for x, y in pts]
        self.d.polygon(big, fill=fill)
        if outline:
            self.d.line(big + [big[0]], fill=outline, width=max(1, round(bw * self.k)), joint="curve")

    def circle(self, cx, cy, r, fill=None, outline=None, bw=1):
        self.d.ellipse((self._p(cx - r), self._p(cy - r), self._p(cx + r), self._p(cy + r)),
                       fill=fill, outline=outline, width=max(1, round(bw * self.k)))

    def line(self, pts, w, fill):
        big = [(self._p(x), self._p(y)) for x, y in pts]
        wd = max(1, round(w * self.k))
        self.d.line(big, fill=fill, width=wd, joint="curve")
        for x, y in (big[0], big[-1]):
            self.d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill=fill)

    def font(self, size, bold=False):
        return load_font(round(size * S), bold)

    def text_w(self, s, size, bold=False):
        return self.font(size, bold).getlength(s) / S

    def text(self, x, y, s, size, fill, bold=False, anchor="lm"):
        self.texts.append((x, y, s, size, fill, bold, anchor))     # drawn crisp at the end

    def fit(self, s, size, maxw, bold=False):
        if self.text_w(s, size, bold) <= maxw:
            return s
        while s and self.text_w(s + "\u2026", size, bold) > maxw:
            s = s[:-1]
        return s + "\u2026"

    def hit(self, x, y, w, h, kind, cb):
        self.hits.append((x + self.ox, y + self.ox, w, h, kind, cb))

    def result(self):
        img = self.img.resize((self.W, self.H), Image.LANCZOS)
        d = ImageDraw.Draw(img)
        for x, y, s, size, fill, bold, anchor in self.texts:
            d.text((round((x + self.ox) * S), round((y + self.ox) * S)), s,
                   font=self.font(size, bold), fill=fill, anchor=anchor)
        return img


def compose_shadow(final, pad, cw, ch, radius, offset=4, blur=9, alpha=55):
    W, H = final.size
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle(
        (pad, pad + px(offset), pad + cw - 1, pad + px(offset) + ch - 1), radius=radius, fill=alpha)
    sh = sh.filter(ImageFilter.GaussianBlur(px(blur)))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow.putalpha(sh)
    return Image.alpha_composite(shadow, final)


def make_card_base(cw, ch, radius, th):
    pad = px(PAD)
    W, H = cw + 2 * pad, ch + 2 * pad
    big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle(
        (pad * SS, pad * SS, (pad + cw) * SS - 1, (pad + ch) * SS - 1),
        radius=radius * SS, fill=th["card"], outline=th["border"], width=SS)
    return big, pad, W, H


def render_dialog(th):
    cw, ch, radius = px(400), px(64), px(22)
    big, pad, W, H = make_card_base(cw, ch, radius, th)
    return compose_shadow(big.resize((W, H), Image.LANCZOS), pad, cw, ch, radius), pad


def ease_out(t):
    return 1 - (1 - t) ** 3


def ease_out_back(t):
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def partial_line(pts, t):
    """First fraction t of a polyline (used to 'draw on' the check mark)."""
    if t >= 1:
        return pts
    if t <= 0:
        return []
    segs = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    want, out = sum(segs) * t, [pts[0]]
    for i, length in enumerate(segs):
        if want >= length:
            out.append(pts[i + 1])
            want -= length
        else:
            f = want / length if length else 0
            out.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f,
                        pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f))
            break
    return out


_target_cache = {}


def target_frame(scale=1.0, fill_t=0.0, ring_t=None, check_t=1.0):
    """One animation frame of the green check button (cached)."""
    key = (round(scale, 2), round(fill_t, 2), None if ring_t is None else round(ring_t, 2), round(check_t, 2))
    if key in _target_cache:
        return _target_cache[key]
    T, ss = px(230), 3
    m = T * ss // 2
    big = Image.new("RGBA", (T * ss, T * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    r = max(2, int(px(36) * scale * ss))
    if ring_t is not None:                                       # expanding, fading ring
        rr = int(r * (1.0 + 0.8 * ring_t))
        d.ellipse((m - rr, m - rr, m + rr, m + rr), outline=(48, 209, 88, int(200 * (1 - ring_t))),
                  width=max(1, int(px(3) * ss * (1 - 0.6 * ring_t))))
    d.ellipse((m - r, m - r, m + r, m + r), fill=mix(GREEN, "#FFFFFF", fill_t),
              outline=mix(GREEN, "#E5E5EA", fill_t), width=max(1, int(px(1 + 2 * fill_t) * ss)))
    k = r / 36.0
    pts = [(m - 14 * k, m + 1 * k), (m - 4 * k, m + 11 * k), (m + 15 * k, m - 9 * k)]
    pts = partial_line(pts, check_t)
    if len(pts) > 1:
        col, wd = mix("#FFFFFF", GREEN, fill_t), max(2, int(6 * k))
        d.line(pts, fill=col, width=wd, joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill=col)
    sh = Image.new("L", (T, T), 0)
    c, r0 = T // 2, r // ss
    ImageDraw.Draw(sh).ellipse((c - r0, c - r0 + px(3), c + r0, c + r0 + px(3)), fill=70)
    sh = sh.filter(ImageFilter.GaussianBlur(px(8)))
    shadow = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    shadow.putalpha(sh)
    img = Image.alpha_composite(shadow, big.resize((T, T), Image.LANCZOS))
    if len(_target_cache) > 120:
        _target_cache.clear()
    _target_cache[key] = img
    return img


def _build_sequences():
    pop = [((0.55 + 0.45 * ease_out_back((i + 1) / 9), 0.0, None, 1.0), int(255 * min(1.0, (i + 1) / 9 * 1.8)))
           for i in range(9)]                                    # pops in with a little overshoot
    hover_in = []                                                # swells, fills green, ripple
    for i in range(8):
        t = (i + 1) / 8
        hover_in.append(((1.0 + 0.22 * ease_out_back(t), ease_out(t), t if t < 1 else None, 1.0), 255))
    hover_out = [((1.22 - 0.22 * ease_out((i + 1) / 6), 1 - ease_out((i + 1) / 6), None, 1.0), 255)
                 for i in range(6)]
    drop = []                                                    # success: burst, check redraws, fade
    for i in range(14):
        t = (i + 1) / 14
        if t <= 0.5:
            u = t / 0.5
            drop.append(((1.22 + 0.18 * math.sin(math.pi * u), 1.0, u, min(1.0, u / 0.7)), 255))
        else:
            u = (t - 0.5) / 0.5
            drop.append(((1.22 * (1 - 0.3 * ease_out(u)), 1.0, None, 1.0), int(255 * (1 - ease_out(u)))))
    out = [((1.0 - 0.3 * ease_out((i + 1) / 6), 0.0, None, 1.0), int(255 * (1 - (i + 1) / 6)))
           for i in range(6)]                                    # dismissed without dropping
    return pop, hover_in, hover_out, drop, out


SEQ_IN, SEQ_HOVER_IN, SEQ_HOVER_OUT, SEQ_DROP, SEQ_OUT = _build_sequences()


# ---- icons (drawn with lines so they stay crisp)
def icon_check(g, cx, cy, s, color, w=2.2):
    g.line([(cx - .42 * s, cy + .02 * s), (cx - .12 * s, cy + .32 * s), (cx + .42 * s, cy - .3 * s)], w, color)


def icon_trash(g, cx, cy, s, color, w=1.8):
    g.line([(cx - .42 * s, cy - .28 * s), (cx + .42 * s, cy - .28 * s)], w, color)
    g.line([(cx - .14 * s, cy - .28 * s), (cx - .14 * s, cy - .42 * s),
            (cx + .14 * s, cy - .42 * s), (cx + .14 * s, cy - .28 * s)], w, color)
    g.rrect(cx - .3 * s, cy - .28 * s, .6 * s, .76 * s, 2, outline=color, bw=w)
    for dx in (-.1, .1):
        g.line([(cx + dx * s, cy - .06 * s), (cx + dx * s, cy + .3 * s)], w * .8, color)


def icon_undo(g, cx, cy, s, color, w=2.0):
    r, c = .34 * s, (cx, cy + .04 * s)
    pts = [(c[0] + r * math.cos(math.radians(a)), c[1] + r * math.sin(math.radians(a)))
           for a in range(-130, 141, 10)]
    g.line(pts, w, color)
    tip, a0 = pts[0], math.radians(-130)
    dx, dy = math.sin(a0), -math.cos(a0)
    h = .26 * s
    for sg in (1, -1):
        ang = math.radians(38 * sg)
        rx = dx * math.cos(ang) - dy * math.sin(ang)
        ry = dx * math.sin(ang) + dy * math.cos(ang)
        g.line([tip, (tip[0] - rx * h, tip[1] - ry * h)], w, color)


# ---- UI building blocks
def seg_control(g, th, x, y, w, h, labels, sel, cbs):
    g.rrect(x, y, w, h, 10, fill=th["seg"])
    n = len(labels)
    sw = (w - 4) / n
    for i, label in enumerate(labels):
        sx = x + 2 + i * sw
        if i == sel:
            g.rrect(sx, y + 2, sw, h - 4, 8, fill=th["seg_on"],
                    outline=th["sep"] if state["settings"]["theme"] == "light" else None)
        g.text(sx + sw / 2, y + h / 2, label, 13 if n <= 3 else 12, th["text"] if i == sel else th["sub"],
               bold=(i == sel), anchor="mm")
        g.hit(sx, y, sw, h, "click", cbs[i])


def switch(g, th, x, y, on, cb):
    g.rrect(x, y, 46, 28, 14, fill=GREEN if on else th["track"])
    g.circle(x + (32 if on else 14), y + 14, 11.5, fill="#FFFFFF")
    g.hit(x - 4, y - 4, 54, 36, "click", cb)


def slider(g, th, x, y, w, value, lo, hi, key):
    frac = (value - lo) / (hi - lo)
    g.rrect(x, y - 2, w, 4, 2, fill=th["track"])
    g.rrect(x, y - 2, max(4, w * frac), 4, 2, fill=accent())
    g.circle(x + w * frac, y, 11, fill="#FFFFFF", outline=th["sep"] if th is THEMES["light"] else None)
    ox = g.ox

    def cb(lx, final):
        v = round(lo + min(max(lx - ox - x, 0), w) / w * (hi - lo))
        if v == state["settings"][key] and not final:
            return False
        set_setting(key, v, final)
        return True
    g.hit(x - 12, y - 16, w + 24, 32, "drag", cb)


def section_label(g, th, x, y, s):
    g.text(x, y, s.upper(), 11, th["sub"], bold=True)


def set_setting(key, value, final=True):
    state["settings"][key] = value
    if key == "opacity":
        for c in cards.values():
            c.apply_opacity()
    elif final:
        for c in cards.values():
            c.draw()
        if key == "behind":
            for c in cards.values():
                c.to_bottom() if value else c.to_normal()
    if final:
        save()
        if key == "accent" and icon is not None:
            icon.icon = tray_image()
        for p in list(panels.values()):
            p.render()


# ================================================================== native windows
user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W_.DWORD), ("biWidth", W_.LONG), ("biHeight", W_.LONG),
                ("biPlanes", W_.WORD), ("biBitCount", W_.WORD), ("biCompression", W_.DWORD),
                ("biSizeImage", W_.DWORD), ("biXPelsPerMeter", W_.LONG),
                ("biYPelsPerMeter", W_.LONG), ("biClrUsed", W_.DWORD),
                ("biClrImportant", W_.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class RECT(ctypes.Structure):
    _fields_ = [("left", W_.LONG), ("top", W_.LONG), ("right", W_.LONG), ("bottom", W_.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W_.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", W_.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W_.HWND, W_.UINT, W_.WPARAM, W_.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W_.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W_.HINSTANCE), ("hIcon", W_.HICON),
                ("hCursor", W_.HANDLE), ("hbrBackground", W_.HBRUSH),
                ("lpszMenuName", W_.LPCWSTR), ("lpszClassName", W_.LPCWSTR)]


user32.GetParent.argtypes = [W_.HWND]
user32.GetParent.restype = W_.HWND
user32.SetWindowPos.argtypes = [W_.HWND, W_.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, W_.UINT]
user32.ShowWindow.argtypes = [W_.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [W_.HWND]
user32.SetForegroundWindow.argtypes = [W_.HWND]
user32.GetDC.argtypes = [W_.HWND]
user32.GetDC.restype = W_.HDC
user32.ReleaseDC.argtypes = [W_.HWND, W_.HDC]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(W_.POINT)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.MonitorFromPoint.argtypes = [W_.POINT, W_.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.FindWindowW.argtypes = [W_.LPCWSTR, W_.LPCWSTR]
user32.FindWindowW.restype = W_.HWND
user32.PostMessageW.argtypes = [W_.HWND, W_.UINT, W_.WPARAM, W_.LPARAM]
user32.IsWindowVisible.argtypes = [W_.HWND]
user32.IsIconic.argtypes = [W_.HWND]
user32.GetWindowRect.argtypes = [W_.HWND, ctypes.POINTER(RECT)]
kernel32.RegisterApplicationRestart.argtypes = [W_.LPCWSTR, W_.DWORD]
WNDENUMPROC = ctypes.WINFUNCTYPE(W_.BOOL, W_.HWND, W_.LPARAM)
dwmapi = ctypes.windll.dwmapi
user32.EnumWindows.argtypes = [WNDENUMPROC, W_.LPARAM]
user32.GetClassNameW.argtypes = [W_.HWND, W_.LPWSTR, ctypes.c_int]
user32.GetWindowLongW.argtypes = [W_.HWND, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [W_.HWND]
dwmapi.DwmGetWindowAttribute.argtypes = [W_.HWND, W_.DWORD, ctypes.c_void_p, W_.DWORD]
dwmapi.DwmSetWindowAttribute.argtypes = [W_.HWND, W_.DWORD, ctypes.c_void_p, W_.DWORD]
user32.RegisterHotKey.argtypes = [W_.HWND, ctypes.c_int, W_.UINT, W_.UINT]
user32.UpdateLayeredWindow.argtypes = [W_.HWND, W_.HDC, ctypes.POINTER(W_.POINT),
                                       ctypes.POINTER(W_.SIZE), W_.HDC,
                                       ctypes.POINTER(W_.POINT), W_.DWORD,
                                       ctypes.POINTER(BLENDFUNCTION), W_.DWORD]
user32.DefWindowProcW.argtypes = [W_.HWND, W_.UINT, W_.WPARAM, W_.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.LoadCursorW.argtypes = [W_.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = W_.HANDLE
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [W_.DWORD, W_.LPCWSTR, W_.LPCWSTR, W_.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, W_.HWND,
                                   W_.HMENU, W_.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = W_.HWND
kernel32.GetModuleHandleW.argtypes = [W_.LPCWSTR]
kernel32.GetModuleHandleW.restype = W_.HINSTANCE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, W_.BOOL, W_.LPCWSTR]
kernel32.CreateMutexW.restype = W_.HANDLE
gdi32.CreateCompatibleDC.argtypes = [W_.HDC]
gdi32.CreateCompatibleDC.restype = W_.HDC
gdi32.CreateDIBSection.argtypes = [W_.HDC, ctypes.c_void_p, W_.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), W_.HANDLE, W_.DWORD]
gdi32.CreateDIBSection.restype = W_.HBITMAP
gdi32.SelectObject.argtypes = [W_.HDC, W_.HGDIOBJ]
gdi32.SelectObject.restype = W_.HGDIOBJ
gdi32.DeleteObject.argtypes = [W_.HGDIOBJ]
gdi32.DeleteDC.argtypes = [W_.HDC]

windows = {}
CLS_CARD, CLS_PANEL = "DesktopTasksCard", "DesktopTasksPanel"
NOACT = 0x0001 | 0x0002 | 0x0010        # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE


def dispatch(hwnd, msg, delta):
    obj = windows.get(hwnd)
    if obj is None:
        return
    try:
        if msg == 0x0201:                       # WM_LBUTTONDOWN
            obj.on_press()
        elif msg == 0x0205:                     # WM_RBUTTONUP
            obj.on_rpress()
        elif msg == 0x020A:                     # WM_MOUSEWHEEL
            obj.on_wheel(delta)
        elif msg == 0x0312:                     # WM_HOTKEY
            obj.on_hotkey()
        elif msg == 0x8001:                     # WM_APP+1: second launch asked us to show ourselves
            obj.on_wake()
    except Exception:
        log_exc()


pending = collections.deque()


def _wndproc(hwnd, msg, wp, lp):
    if msg in (0x0011, 0x0016):                 # WM_QUERYENDSESSION / WM_ENDSESSION: Windows is shutting down
        save()
        return 1 if msg == 0x0011 else 0
    # The native callback does nothing but queue the event (no Tk or Python-heavy work here).
    if msg in (0x0201, 0x0205, 0x020A, 0x0312, 0x8001) and hwnd in windows:
        delta = ((wp >> 16) & 0xFFFF)
        if delta >= 0x8000:
            delta -= 0x10000
        pending.append((hwnd, msg, delta if msg == 0x020A else 0))
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def pump():
    try:
        while pending:
            dispatch(*pending.popleft())
    except Exception:
        log_exc()
    root.after(4, pump)


_wndproc_ref = WNDPROC(_wndproc)


def register_classes():
    for name, cur in ((CLS_CARD, 32649), (CLS_PANEL, 32512)):    # hand / arrow cursor
        wc = WNDCLASSW()
        wc.lpfnWndProc = _wndproc_ref
        wc.hInstance = HINST
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(cur))
        wc.lpszClassName = name
        user32.RegisterClassW(ctypes.byref(wc))


def cursor():
    p = W_.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def work_area(x, y):
    try:
        hm = user32.MonitorFromPoint(W_.POINT(x, y), 2)       # nearest monitor
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                 "Windows.UI.Core.CoreWindow", "NotifyIconOverflowWindow",
                 "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow",
                 "TaskListThumbnailWnd", "Windows.UI.Composition.DesktopWindowContentBridge"}


def desktop_has_app_windows():
    """True if at least one normal app window is showing. False means the desktop is
    being shown ('Show desktop' button or Win+D), so our cards must stay visible."""
    found = [False]

    def check(hwnd, _lp):
        try:
            if hwnd in windows or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            ex = user32.GetWindowLongW(hwnd, -20)
            if ex & (0x80 | 0x8 | 0x08000000 | 0x20):   # tool, topmost, no-activate, transparent
                return True
            cloaked = W_.DWORD(0)
            dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
            buf = ctypes.create_unicode_buffer(80)
            user32.GetClassNameW(hwnd, buf, 80)
            if buf.value in SHELL_CLASSES:
                return True
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if (r.right - r.left) < 120 or (r.bottom - r.top) < 80:
                return True
            found[0] = True
            return False                                # stop enumerating
        except Exception:
            return True

    callback = WNDENUMPROC(check)
    user32.EnumWindows(callback, 0)
    return found[0]


def virtual_screen():
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # x, y, w, h


class Layered:
    """Borderless, non-activating window with per-pixel alpha (smooth edges, soft shadow)."""

    def __init__(self, cls=CLS_CARD, title=""):
        ex = 0x00080000 | 0x00000080 | 0x08000000     # LAYERED | TOOLWINDOW | NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, cls, title, 0x80000000, 0, 0, 1, 1,
                                           None, None, HINST, None)
        windows[self.hwnd] = self
        self.x = self.y = self.w = self.h = 0
        self.alpha = 255
        self.mem = self.bmp = self.old = None

    def exclude_from_peek(self):
        try:                                               # DWMWA_EXCLUDED_FROM_PEEK
            dwmapi.DwmSetWindowAttribute(self.hwnd, 12, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    def on_press(self): pass
    def on_rpress(self): pass
    def on_wheel(self, delta): pass
    def on_hotkey(self): pass
    def on_wake(self): pass

    def _free(self):
        if self.mem:
            gdi32.SelectObject(self.mem, self.old)
            gdi32.DeleteObject(self.bmp)
            gdi32.DeleteDC(self.mem)
            self.mem = self.bmp = self.old = None

    def set_image(self, img):
        w, h = img.size
        r, g, b, a = img.split()
        data = Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a),
                                    ImageChops.multiply(r, a), a)).tobytes()    # premultiplied BGRA
        bmi = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        screen = user32.GetDC(None)
        if not screen:
            return
        dc = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not dc or not bmp or not bits.value:            # out of GDI memory: skip, never crash
            if bmp:
                gdi32.DeleteObject(bmp)
            if dc:
                gdi32.DeleteDC(dc)
            user32.ReleaseDC(None, screen)
            return
        ctypes.memmove(bits.value, data, len(data))
        old = gdi32.SelectObject(dc, bmp)
        user32.ReleaseDC(None, screen)
        self._free()
        self.mem, self.bmp, self.old, self.w, self.h = dc, bmp, old, w, h
        self.push()

    def push(self):
        if not self.mem:
            return
        size, src = W_.SIZE(self.w, self.h), W_.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, self.alpha, 1)             # AC_SRC_OVER, AC_SRC_ALPHA
        user32.UpdateLayeredWindow(self.hwnd, None, None, ctypes.byref(size), self.mem,
                                   ctypes.byref(src), 0, ctypes.byref(blend), 2)

    def set_alpha(self, a):
        self.alpha = max(0, min(255, int(a)))
        self.push()

    def move(self, x, y):
        self.x, self.y = int(x), int(y)
        user32.SetWindowPos(self.hwnd, None, self.x, self.y, 0, 0, 0x0001 | 0x0004 | 0x0010)

    def show(self): user32.ShowWindow(self.hwnd, 4)            # SW_SHOWNOACTIVATE
    def hide(self): user32.ShowWindow(self.hwnd, 0)
    def heal(self):
        """Self-repair: re-show and re-position the window if Windows hid or moved it."""
        try:
            if not user32.IsWindowVisible(self.hwnd) or user32.IsIconic(self.hwnd):
                self.show()
            r = RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(r))
            if (r.left, r.top) != (self.x, self.y):
                self.move(self.x, self.y)
        except Exception:
            log_exc()

    def to_top(self): user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, NOACT)
    def to_normal(self): user32.SetWindowPos(self.hwnd, -2, 0, 0, 0, 0, NOACT)
    def to_bottom(self): user32.SetWindowPos(self.hwnd, 1, 0, 0, 0, 0, NOACT)

    def destroy(self):
        windows.pop(self.hwnd, None)
        self._free()
        user32.DestroyWindow(self.hwnd)


# ================================================================== drop target
class Target(Layered):
    def __init__(self):
        super().__init__()
        self.hover, self.seq_id = False, 0
        self.cx = self.cy = None
        self.set_frame((1.0, 0.0, None, 1.0), 255)
        self.place(*cursor())
        root.after(3000, self.prewarm)

    def set_frame(self, params, alpha):
        self.alpha = alpha
        self.set_image(target_frame(*params))

    def prewarm(self):                                           # render frames in the background
        frames = [p for seq in (SEQ_IN, SEQ_HOVER_IN, SEQ_HOVER_OUT, SEQ_DROP, SEQ_OUT) for p, _ in seq]

        def warm(i):
            if i < len(frames):
                target_frame(*frames[i])
                root.after(30, lambda: warm(i + 1))
        warm(0)

    def play(self, seq, on_done=None):
        self.seq_id += 1
        sid = self.seq_id

        def step(i):
            if sid != self.seq_id:                               # a newer animation took over
                return
            if i >= len(seq):
                if on_done:
                    on_done()
                return
            params, alpha = seq[i]
            self.set_frame(params, alpha)
            root.after(16, lambda: step(i + 1))
        step(0)

    def place(self, x, y):
        left, top, right, bottom = work_area(x, y)
        cx, cy = (left + right) // 2, bottom - px(120)
        if (cx, cy) != (self.cx, self.cy):
            self.cx, self.cy = cx, cy
            self.move(cx - self.w // 2, cy - self.h // 2)

    def show(self):
        self.hover = False
        self.seq_id += 1
        self.set_frame((0.55, 0.0, None, 1.0), 0)
        super().show()
        self.to_top()
        self.play(SEQ_IN)

    def hide(self):
        self.seq_id += 1
        self.hover = False
        super().hide()

    def drop(self):                                              # task released on the check
        self.hover = False
        self.play(SEQ_DROP, self.hide)

    def dismiss(self):                                           # task released elsewhere
        self.hover = False
        self.play(SEQ_OUT, self.hide)

    def contains(self, x, y):
        return (x - self.cx) ** 2 + (y - self.cy) ** 2 <= px(64) ** 2

    def set_hover(self, hov):
        if hov != self.hover:
            self.hover = hov
            self.play(SEQ_HOVER_IN if hov else SEQ_HOVER_OUT)


# ================================================================== task cards
class Card(Layered):
    def __init__(self, task):
        super().__init__()
        self.task, self.dragging, self.pad = task, False, px(PAD)
        self.exclude_from_peek()
        self.draw()
        if task.get("x") is None:
            task["x"], task["y"] = next_pos()
        x, y = self.clamp(task["x"], task["y"])
        task["x"], task["y"] = x, y
        self.move(x - self.pad, y - self.pad)
        self.show()
        if state["settings"]["behind"]:
            self.to_bottom()

    def clamp(self, x, y):
        vx, vy, vw, vh = virtual_screen()
        cw, ch = self.w - 2 * self.pad, self.h - 2 * self.pad
        return (min(max(x, vx), vx + vw - cw), min(max(y, vy), vy + vh - ch))

    def draw(self):
        s = state["settings"]
        th, acc = theme(), accent()
        f = load_font(round(s["size"] * S), s["weight"])
        asc, desc = f.getmetrics()
        lh = int((asc + desc) * 1.12)
        cw, hp, vp, dot, radius = px(300), px(18), px(16), px(8), px(18)
        tx = hp + dot + px(12)
        lines = wrap(display_text(self.task["text"]), f, cw - tx - hp)
        if len(lines) > 12:
            lines = lines[:12]
            lines[-1] = lines[-1].rstrip() + "\u2026"
        ch = max(px(54), 2 * vp + lh * len(lines))
        big, pad, W, H = make_card_base(cw, ch, radius, th)
        y0 = pad + (ch - lh * len(lines)) // 2
        dc = y0 + int(asc * 0.62)
        ImageDraw.Draw(big).ellipse(((pad + hp) * SS, (dc - dot // 2) * SS,
                                     (pad + hp + dot) * SS, (dc + dot // 2 + dot % 2) * SS), fill=acc)
        body = big.resize((W, H), Image.LANCZOS)
        d = ImageDraw.Draw(body)
        for i, line in enumerate(lines):
            d.text((pad + tx, y0 + i * lh), line, font=f, fill=th["text"])
        self.alpha = int(255 * s["opacity"] / 100)
        self.set_image(compose_shadow(body, pad, cw, ch, radius))

    def apply_opacity(self):
        self.set_alpha(255 * state["settings"]["opacity"] / 100)

    # dragging is driven by polling the mouse, so nothing can interrupt it
    def on_press(self):
        self.sx, self.sy = cursor()
        self.ox, self.oy = self.x, self.y
        self.dragging = False
        self.tick()

    def tick(self):
        if not (user32.GetAsyncKeyState(0x01) & 0x8000):        # left button released
            self.release()
            return
        cx, cy = cursor()
        if not self.dragging and abs(cx - self.sx) + abs(cy - self.sy) > px(4):
            self.dragging = True
            self.to_top()
            target.show()
        if self.dragging:
            self.move(self.ox + cx - self.sx, self.oy + cy - self.sy)
            target.place(cx, cy)
            target.set_hover(target.contains(cx, cy))
        root.after(8, self.tick)

    def release(self):
        if not self.dragging:
            return
        self.dragging = False
        inside = target.contains(*cursor())
        self.to_normal()
        if inside:
            target.drop()
            complete_task(self.task)
        else:
            target.dismiss()
            x, y = self.clamp(self.x + self.pad, self.y + self.pad)
            self.move(x - self.pad, y - self.pad)
            self.task["x"], self.task["y"] = x, y
            save()
            if state["settings"]["behind"]:
                self.to_bottom()

    def on_rpress(self):                                       # right-click asks before deleting
        if not self.dragging and self.task.get("status") == "active":
            ConfirmPanel(self.task)

    def fade_out(self):
        def fade(a):
            if a <= 12:
                self.destroy()
                return
            self.set_alpha(a)
            root.after(16, lambda: fade(a - 30))
        fade(self.alpha)


# ================================================================== windows (panels)
class Panel(Layered):
    key, bw, bh = "", 360, 400

    def __init__(self):
        super().__init__(cls=CLS_PANEL)
        self.closed, self.hits, self._gen = False, [], 0
        if self.key in panels:
            panels[self.key].close()
        panels[self.key] = self
        self.move(*self.initial_pos(px(self.bw + 2 * PAD), px(self.bh + 2 * PAD)))
        self.render()
        self.show()
        self.to_top()

    def initial_pos(self, W, H):
        left, top, right, bottom = work_area(*cursor())
        return left + (right - left - W) // 2, max(top, top + (bottom - top - H) // 2 - px(20))

    def draw(self, g, th):
        pass

    def render(self, ss=None):
        if self.closed:
            return
        self._gen += 1
        th = theme()
        g = Gfx(self.bw + 2 * PAD, self.bh + 2 * PAD, ss or PANEL_SS, origin=PAD)
        g.rrect(0, 0, self.bw, self.bh, 22, fill=th["bg"], outline=th["sep"])
        self.draw(g, th)
        self.hits = g.hits
        self.set_image(compose_shadow(g.result(), px(PAD), px(self.bw), px(self.bh), px(22)))

    def header(self, g, th, title):
        g.hit(0, 0, self.bw, 56, "move", None)
        g.text(24, 30, title, 19, th["text"], bold=True)
        cx, cy = self.bw - 34, 30
        g.circle(cx, cy, 12, fill=mix(th["sub"], th["bg"], .18))
        g.line([(cx - 4, cy - 4), (cx + 4, cy + 4)], 1.6, th["sub"])
        g.line([(cx - 4, cy + 4), (cx + 4, cy - 4)], 1.6, th["sub"])
        g.hit(cx - 16, cy - 16, 32, 32, "click", self.close)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if panels.get(self.key) is self:
            panels.pop(self.key)
        self.destroy()

    def on_press(self):
        cx, cy = cursor()
        lx, ly = (cx - self.x) / S, (cy - self.y) / S
        for x, y, w, h, kind, cb in reversed(self.hits):
            if x <= lx <= x + w and y <= ly <= y + h:
                if kind == "click":
                    gen = self._gen
                    cb()
                    if not self.closed and self._gen == gen:
                        self.render()
                elif kind == "drag":
                    self.drag_loop(cb)
                elif kind == "move":
                    self.move_loop(cx, cy, self.x, self.y)
                return

    def move_loop(self, sx, sy, ox, oy):
        if self.closed or not (user32.GetAsyncKeyState(0x01) & 0x8000):
            return
        cx, cy = cursor()
        self.move(ox + cx - sx, oy + cy - sy)
        root.after(8, lambda: self.move_loop(sx, sy, ox, oy))

    def drag_loop(self, cb):
        if self.closed:
            return
        down = user32.GetAsyncKeyState(0x01) & 0x8000
        lx = (cursor()[0] - self.x) / S
        changed = cb(lx, not down)
        if not down:
            self.render()
            return
        if changed:
            self.render(2)                       # cheaper render while dragging
        root.after(16, lambda: self.drag_loop(cb))


class CustomizePanel(Panel):
    key, bw, bh = "customize", 360, 596

    def draw(self, g, th):
        s = state["settings"]
        self.header(g, th, "Customize")

        section_label(g, th, 26, 76, "Appearance")
        seg_control(g, th, 20, 94, 320, 36, ["Light", "Dark"], 0 if s["theme"] == "light" else 1,
                    [lambda: set_setting("theme", "light"), lambda: set_setting("theme", "dark")])

        section_label(g, th, 26, 158, "Accent")
        for i, (name, col) in enumerate(ACCENTS.items()):
            cx, cy = 52 + i * 64, 194
            if s["accent"] == name:
                g.circle(cx, cy, 19, outline=col, bw=2)
            g.circle(cx, cy, 14, fill=col)
            g.hit(cx - 20, cy - 20, 40, 40, "click", lambda n=name: set_setting("accent", n))

        section_label(g, th, 26, 238, "Text size")
        g.text(340, 238, str(s["size"]), 12, th["sub"], anchor="rm")
        slider(g, th, 24, 264, 312, s["size"], 12, 24, "size")

        section_label(g, th, 26, 296, "Opacity")
        g.text(340, 296, f"{s['opacity']}%", 12, th["sub"], anchor="rm")
        slider(g, th, 24, 322, 312, s["opacity"], 50, 100, "opacity")

        section_label(g, th, 26, 358, "Font weight")
        seg_control(g, th, 20, 376, 320, 34, ["Regular", "Semibold", "Bold", "Extra Bold"],
                    WEIGHTS.index(s["weight"]), [lambda w=w: set_setting("weight", w) for w in WEIGHTS])

        g.rrect(20, 428, 320, 144, 16, fill=th["row"])
        g.line([(36, 476), (324, 476)], 1, th["sep"])
        g.line([(36, 524), (324, 524)], 1, th["sep"])
        g.text(36, 452, "Capitalize Each Word", 14.5, th["text"])
        switch(g, th, 260, 438, s["capitalize"], lambda: set_setting("capitalize", not s["capitalize"]))
        g.text(36, 500, "Keep cards behind windows", 14.5, th["text"])
        switch(g, th, 260, 486, s["behind"], lambda: set_setting("behind", not s["behind"]))
        g.text(36, 548, "Start with Windows", 14.5, th["text"])
        switch(g, th, 260, 534, startup_enabled(), toggle_startup)


class TasksPanel(Panel):
    key, bw, bh = "tasks", 380, 562
    RH, GAP, ROWS, LY = 52, 6, 5, 256

    def __init__(self):
        self.tab, self.offset = "active", 0
        super().__init__()

    def items(self):
        ts = tasks_with(self.tab)
        field = {"active": "created", "done": "done_at", "deleted": "deleted_at"}[self.tab]
        return sorted(ts, key=lambda t: t.get(field, ""), reverse=self.tab != "active")

    def set_tab(self, tab):
        self.tab, self.offset = tab, 0

    def on_wheel(self, delta):
        n = len(self.items())
        if n > self.ROWS:
            self.offset = max(0, min(n - self.ROWS, self.offset + (-1 if delta > 0 else 1)))
            self.render()

    def draw(self, g, th):
        acc = accent()
        self.header(g, th, "Tasks")
        if self.tab == "deleted" and tasks_with("deleted"):
            g.text(self.bw - 62, 30, "Clear all", 13, RED, bold=True, anchor="rm")
            g.hit(self.bw - 130, 14, 70, 32, "click", clear_deleted)

        # ---- progress chart
        g.rrect(20, 64, 340, 132, 16, fill=th["row"])
        counts = week_counts()                                   # Saturday .. Friday
        today = date.today()
        ti = next(i for i, (d, _) in enumerate(counts) if d == today)
        today_n, week = counts[ti][1], sum(c for _, c in counts)
        g.text(38, 86, "This week", 12, th["sub"], bold=True)
        g.text(342, 86, f"{today_n} today \u00b7 {week} this week", 12, th["sub"], anchor="rm")
        top = max(max(c for _, c in counts), 1)
        slot = 304 / 7
        for i, (d, c) in enumerate(counts):
            future = d > today
            cx = 38 + slot * i + slot / 2
            h = 6 if c == 0 else 8 + (c / top) * 44
            if c == 0:
                col = mix(th["track"], th["row"], .5) if future else th["track"]
            else:
                col = acc if i == ti else mix(acc, th["row"], .5)
            g.rrect(cx - 12, 162 - h, 24, h, 6, fill=col)
            if c:
                g.text(cx, 162 - h - 9, str(c), 11, th["sub"], bold=True, anchor="mm")
            g.text(cx, 180, "SSMTWTF"[i], 11.5,
                   th["text"] if i == ti else (mix(th["sub"], th["row"], .6) if future else th["sub"]),
                   bold=(i == ti), anchor="mm")

        # ---- tabs
        labels = [("active", "Current"), ("done", "Done"), ("deleted", "Deleted")]
        seg_control(g, th, 20, 210, 340, 34,
                    [f"{name} {len(tasks_with(k))}" for k, name in labels],
                    [k for k, _ in labels].index(self.tab),
                    [lambda k=k: self.set_tab(k) for k, _ in labels])

        # ---- list
        items = self.items()
        self.offset = max(0, min(self.offset, max(0, len(items) - self.ROWS)))
        if not items:
            msg = {"active": ("All clear", "Add a task from the tray menu"),
                   "done": ("Nothing completed yet", "Drop a card on the green check"),
                   "deleted": ("Nothing deleted", "")}[self.tab]
            g.text(self.bw / 2, self.LY + 90, msg[0], 16, th["text"], bold=True, anchor="mm")
            g.text(self.bw / 2, self.LY + 116, msg[1], 12.5, th["sub"], anchor="mm")
        for i, t in enumerate(items[self.offset:self.offset + self.ROWS]):
            y = self.LY + i * (self.RH + self.GAP)
            g.rrect(20, y, 340, self.RH, 14, fill=th["row"])
            dot = {"active": acc, "done": GREEN, "deleted": th["sub"]}[self.tab]
            g.circle(40, y + self.RH / 2, 4.5, fill=dot)
            btns = {"active": [("trash", RED), ("check", GREEN)],
                    "done": [("trash", RED), ("undo", acc)],
                    "deleted": [("undo", acc)]}[self.tab]
            verb, stamp = {"active": ("Added", "created"), "done": ("Completed", "done_at"),
                           "deleted": ("Deleted", "deleted_at")}[self.tab]
            title = g.fit(display_text(t["text"]).replace("\n", " "), 14.5, 340 - 56 - len(btns) * 38 - 6)
            g.text(56, y + 20, title, 14.5, th["text"])
            g.text(56, y + 38, f"{verb} {when(t.get(stamp, ''))}", 11, th["sub"])
            for j, (kind, col) in enumerate(btns):
                cx, cy = 360 - 26 - j * 38, y + self.RH / 2
                g.circle(cx, cy, 15, fill=mix(col, th["row"], .14))
                {"check": icon_check, "trash": icon_trash, "undo": icon_undo}[kind](g, cx, cy, 16, col)
                action = {"check": complete_task, "trash": delete_task, "undo": restore_task}[kind]
                g.hit(cx - 16, cy - 16, 32, 32, "click", lambda t=t, a=action: a(t))
        n = len(items)
        if n > self.ROWS:
            track = self.ROWS * (self.RH + self.GAP) - self.GAP
            thumb = max(24, track * self.ROWS / n)
            ty = self.LY + (track - thumb) * self.offset / (n - self.ROWS)
            g.rrect(366, ty, 4, thumb, 2, fill=th["track"])


# ================================================================== first-run tutorial
def card_fill(th):
    return th["card"] if state["settings"]["theme"] == "light" else "#3A3A3C"


def illu_bg(th):
    return mix(accent(), th["row"], .08)


def mini_card(g, th, x, y, w, label, ghost=False, lift=False):
    bg = illu_bg(th)
    if lift:
        g.rrect(x + 1, y + 5, w, 44, 14, fill=mix("#000000", bg, .12))
    fill = mix(card_fill(th), bg, .4) if ghost else card_fill(th)
    g.rrect(x, y, w, 44, 14, fill=fill, outline=th["sep"])
    g.circle(x + 18, y + 22, 4.5, fill=mix(accent(), fill, .4) if ghost else accent())
    g.text(x + 32, y + 22, label, 14, th["sub"] if ghost else th["text"], bold=not ghost)


def pointer(g, x, y, s=1.0):
    pts = [(0, 0), (0, 17), (4.2, 13.2), (7, 19.5), (9.6, 18.4), (6.9, 12.2), (12.5, 12.2)]
    g.poly([(x + a * s, y + b * s) for a, b in pts], fill="#FFFFFF", outline="#1C1C1E", bw=1.3)


def dashed_arrow(g, p0, p1, p2, color, w=2.2, dash=7, gap=6):
    pts = []
    for i in range(61):
        t = i / 60
        pts.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]))
    acc_len, cur, on = 0.0, [pts[0]], True
    for a, b in zip(pts, pts[1:]):
        acc_len += math.dist(a, b)
        if on:
            cur.append(b)
        if acc_len >= (dash if on else gap):
            if on and len(cur) > 1:
                g.line(cur, w, color)
            on, acc_len, cur = not on, 0.0, [b]
    if on and len(cur) > 1:
        g.line(cur, w, color)
    a, b = pts[-3], pts[-1]
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    for sg in (1, -1):
        g.line([b, (b[0] - 9 * math.cos(ang + sg * .5), b[1] - 9 * math.sin(ang + sg * .5))], w, color)


def ill_complete(g, th):
    bg = illu_bg(th)
    mini_card(g, th, 38, 36, 190, "Drink a glass of water", ghost=True)
    dashed_arrow(g, (140, 88), (118, 134), (132, 150), accent())
    mini_card(g, th, 84, 140, 190, "Drink a glass of water", lift=True)
    cx, cy = 312, 196
    g.circle(cx, cy + 3, 38, fill=mix("#000000", bg, .10))
    g.circle(cx, cy, 36, fill="#FFFFFF", outline=GREEN, bw=3)
    icon_check(g, cx, cy, 52, GREEN, 5)
    pointer(g, 150, 168, 1.3)


def ill_delete(g, th):
    bg = illu_bg(th)
    mini_card(g, th, 36, 50, 196, "Stretch for five minutes")
    mx, my = 280, 44
    g.rrect(mx, my, 42, 64, 20, fill=card_fill(th), outline=th["sub"], bw=2)
    g.line([(mx + 2, my + 27), (mx + 40, my + 27)], 1.6, th["sub"])
    g.line([(mx + 21, my + 2), (mx + 21, my + 27)], 1.6, th["sub"])
    g.rrect(mx + 22.5, my + 3.5, 16, 22, 8, fill=RED)
    g.circle(mx + 31, my + 15, 17, outline=mix(RED, bg, .55), bw=1.6)
    g.circle(mx + 31, my + 15, 25, outline=mix(RED, bg, .25), bw=1.4)
    dashed_arrow(g, (134, 100), (134, 118), (134, 142), RED)
    g.rrect(36, 150, 292, 38, 19, fill=mix(RED, bg, .14))
    icon_trash(g, 58, 169, 17, RED, 1.8)
    g.text(76, 169, "Moves to Deleted. Restore anytime.", 12.5, RED, bold=True)


def ill_add(g, th):
    keys, x, y = [("Ctrl", 72), ("Alt", 60), ("N", 52)], 0, 52
    x = 200 - (sum(w for _, w in keys) + 68) / 2
    for i, (label, w) in enumerate(keys):
        g.rrect(x, y + 4, w, 52, 12, fill=mix("#000000", illu_bg(th), .16))
        g.rrect(x, y, w, 52, 12, fill=card_fill(th), outline=th["sep"])
        g.text(x + w / 2, y + 26, label, 17, th["text"], bold=True, anchor="mm")
        x += w
        if i < 2:
            g.text(x + 17, y + 26, "+", 20, th["sub"], anchor="mm")
            x += 34
    g.rrect(52, 148, 296, 56, 20, fill=card_fill(th), outline=th["sep"])
    g.line([(76, 164), (76, 188)], 2, accent())
    g.text(88, 176, "New task", 16, th["sub"])


def ill_tray(g, th):
    acc, bar = accent(), "#26262A"
    g.rrect(224, 92, 140, 36, 18, fill=acc)
    g.text(294, 110, "Double-click", 13.5, "#FFFFFF", bold=True, anchor="mm")
    g.poly([(286, 128), (302, 128), (294, 138)], fill=acc)
    g.rrect(30, 140, 340, 54, 14, fill=bar)
    g.line([(176, 164), (183, 171), (190, 164)], 2, "#9A9AA0")
    g.circle(228, 167, 9, fill="#5A5A60")
    g.circle(338, 167, 9, fill="#5A5A60")
    g.rrect(278, 149, 32, 32, 9, fill=acc)
    g.line([(286, 165), (292, 171), (303, 159)], 2.6, "#FFFFFF")
    g.rrect(274, 145, 40, 40, 12, outline=mix(acc, bar, .55), bw=2)
    g.text(200, 222, "Can't see it? Click the ^ arrow.", 12, th["sub"], anchor="mm")


class TutorialPanel(Panel):
    key, bw, bh = "tutorial", 400, 500
    PAGES = [
        ("Complete a task", "Drag a card onto the green check circle that appears at the bottom of your screen.", ill_complete),
        ("Delete a task", "Right-click a card to delete it. Deleted tasks wait in your Tasks list, so you can always restore them.", ill_delete),
        ("Add your own", "Press Ctrl+Alt+N from anywhere, or choose Add task from the tray menu.", ill_add),
        ("Find it in the tray", "Double-click the DeskTask icon to see your tasks and progress. Right-click it for settings.", ill_tray),
    ]

    def __init__(self):
        self.page = 0
        super().__init__()
        left, top, right, bottom = work_area(*cursor())
        W = px(self.bw + 2 * PAD)
        self.move(left + int((right - left) * 0.64) - W // 2, self.y)

    def close(self):
        if not self.closed:
            state["meta"]["tutorial_seen"] = True
            save()
        super().close()

    def go(self, delta):
        self.page = max(0, min(len(self.PAGES) - 1, self.page + delta))

    def draw(self, g, th):
        acc = accent()
        title, desc, illustration = self.PAGES[self.page]
        g.hit(0, 0, self.bw, 252, "move", None)
        g.rrect(20, 20, 360, 232, 18, fill=illu_bg(th), outline=th["sep"])
        illustration(g, th)
        g.text(self.bw / 2, 284, title, 21, th["text"], bold=True, anchor="mm")
        for i, line in enumerate(wrap(desc, g.font(14.5), 316 * S)[:4]):
            g.text(self.bw / 2, 320 + i * 22, line, 14.5, th["sub"], anchor="mm")
        n = len(self.PAGES)
        for i in range(n):
            g.circle(self.bw / 2 + (i - (n - 1) / 2) * 16, 414, 4, fill=acc if i == self.page else th["track"])
        last = self.page == n - 1
        left_label, left_cb = ("Skip", self.close) if self.page == 0 else ("Back", lambda: self.go(-1))
        g.text(44, 456, left_label, 14.5, th["sub"], bold=True)
        g.hit(24, 436, 80, 40, "click", left_cb)
        g.rrect(self.bw - 20 - 128, 436, 128, 40, 20, fill=acc)
        g.text(self.bw - 20 - 64, 456, "Get started" if last else "Next", 14.5, "#FFFFFF", bold=True, anchor="mm")
        g.hit(self.bw - 148, 436, 128, 40, "click", self.close if last else (lambda: self.go(1)))


class ConfirmPanel(Panel):
    key, bw, bh = "confirm", 340, 184

    def __init__(self, task):
        self.task = task
        super().__init__()

    def initial_pos(self, W, H):                                 # appears right where you clicked
        cx, cy = cursor()
        left, top, right, bottom = work_area(cx, cy)
        return min(max(left, cx - W // 2), right - W), min(max(top, cy - H // 2), bottom - H)

    def confirm(self):
        if self.task.get("status") == "active":
            delete_task(self.task)
        self.close()

    def draw(self, g, th):
        g.hit(0, 0, self.bw, self.bh, "move", None)
        g.text(26, 40, "Delete this task?", 18, th["text"], bold=True)
        g.text(26, 72, g.fit(display_text(self.task["text"]).replace("\n", " "), 14, self.bw - 52), 14, th["sub"])
        g.text(26, 96, "You can restore it later from Tasks.", 12, th["sub"])
        w = (self.bw - 52 - 12) / 2
        g.rrect(26, 124, w, 40, 20, fill=th["seg"])
        g.text(26 + w / 2, 144, "Cancel", 14.5, th["text"], bold=True, anchor="mm")
        g.hit(26, 124, w, 40, "click", self.close)
        g.rrect(38 + w, 124, w, 40, 20, fill=RED)
        g.text(38 + w + w / 2, 144, "Delete", 14.5, "#FFFFFF", bold=True, anchor="mm")
        g.hit(38 + w, 124, w, 40, "click", self.confirm)


# ================================================================== add-task bar
def add_dialog():
    global add_ui
    if add_ui is not None:                       # already open: just bring the focus back
        try:
            add_ui["focus"]()
            return
        except Exception:
            add_ui = None
    s = state["settings"]
    th = theme()
    img, pad = render_dialog(th)
    bg = Layered(cls=CLS_PANEL)
    bg.set_image(img)
    cw, ch = px(400), px(64)
    left, top, right, bottom = work_area(*cursor())
    X, Y = left + (right - left - cw) // 2, top + (bottom - top) // 4
    bg.move(X - pad, Y - pad)
    bg.show()
    bg.to_top()

    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.configure(bg=th["card"])
    ew, eh = cw - px(48), px(34)
    win.geometry(f"{ew}x{eh}+{X + px(24)}+{Y + (ch - eh) // 2}")
    win.attributes("-topmost", True)
    entry = tk.Entry(win, relief="flat", bd=0, font=(TK_FONT, -px(17)), bg=th["card"], fg=th["sub"],
                     insertbackground=ACCENTS[s["accent"]], highlightthickness=0)
    entry.pack(fill="both", expand=True)
    entry.insert(0, "New task")
    ph = {"on": True}
    done = {"v": False}
    opened = time.time()

    def close(_=None):
        global add_ui
        if done["v"]:
            return
        done["v"] = True
        add_ui = None
        for w in (bg, win):
            try:
                w.destroy()
            except Exception:
                log_exc()

    def submit(_=None):
        text = "" if ph["on"] else entry.get().strip()
        close()
        if text:
            add_task(text)

    def on_key(e):
        if ph["on"] and e.keysym not in ("Return", "Escape", "Shift_L", "Shift_R", "Control_L",
                                         "Control_R", "Alt_L", "Alt_R", "Caps_Lock"):
            entry.delete(0, "end")
            entry.config(fg=th["text"])
            ph["on"] = False

    def grab_focus(first=False):
        if done["v"]:
            return
        try:                                      # allow stealing focus when opened by hotkey
            win.update_idletasks()
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        entry.focus_force()
        if first:
            entry.icursor(0)

    def watch():
        """Closes the bar on Esc, on a click anywhere outside it, or after a minute of nothing."""
        if done["v"]:
            return
        try:
            age = time.time() - opened
            esc = user32.GetAsyncKeyState(0x1B) & 0x8000
            down = user32.GetAsyncKeyState(0x01) & 0x8000
            cx, cy = cursor()
            outside = not (X - px(10) <= cx <= X + cw + px(10) and Y - px(10) <= cy <= Y + ch + px(10))
            if (esc and age > 0.3) or (down and outside and age > 0.5) or (ph["on"] and age > 60):
                close()
                return
        except Exception:
            log_exc()
        root.after(40, watch)

    entry.bind("<Key>", on_key)
    entry.bind("<Return>", submit)
    entry.bind("<Escape>", close)
    entry.bind("<Button-1>", lambda e: entry.focus_force())
    entry.bind("<FocusOut>", lambda e: close() if (ph["on"] and time.time() - opened > 0.8) else None)
    add_ui = {"focus": grab_focus, "close": close}
    for delay in (60, 300, 800):                  # retry: focus can be refused the first time
        win.after(delay, lambda first=(delay == 60): grab_focus(first))
    watch()


class HotkeyWin(Layered):
    def __init__(self):
        super().__init__(cls=CLS_PANEL, title=MSG_TITLE)
        # Ctrl+Alt+N  (MOD_ALT | MOD_CONTROL | MOD_NOREPEAT)
        user32.RegisterHotKey(self.hwnd, 1, 0x0001 | 0x0002 | 0x4000, 0x4E)

    def on_hotkey(self):
        add_dialog()

    def on_wake(self):                                  # launched a second time
        TasksPanel()
        notify("DeskTask is already running", "Your tasks are open. DeskTask lives in the system tray.")


# ================================================================== startup / tray
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_NAME = "DeskTask"
LEGACY_RUN_NAME = "DesktopTasks"


def startup_command():
    if getattr(sys, "frozen", False):                          # packaged .exe
        return f'"{sys.executable}" --autostart'
    py = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{py}" "{os.path.abspath(__file__)}" --autostart'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
    except OSError:
        return False
    try:                                                       # disabled in Task Manager > Startup?
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data, _ = winreg.QueryValueEx(k, RUN_NAME)
            if data and data[0] & 1:
                return False
    except OSError:
        pass
    return True


def set_startup(on):
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except OSError:
                    pass
        try:                                                   # clear any "disabled" flag
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, RUN_NAME)
        except OSError:
            pass
    except Exception:
        log_exc()


def migrate_startup():
    """Old builds registered as 'DesktopTasks'; move that over, and repair stale paths."""
    try:
        legacy = False
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            try:
                winreg.QueryValueEx(k, LEGACY_RUN_NAME)
                winreg.DeleteValue(k, LEGACY_RUN_NAME)
                legacy = True
            except OSError:
                pass
            try:
                current, _ = winreg.QueryValueEx(k, RUN_NAME)
            except OSError:
                current = None
        if legacy and current is None:
            set_startup(True)
        elif current is not None and current != startup_command():
            set_startup(True)                           # exe was moved or renamed
    except Exception:
        log_exc()


def toggle_startup():
    set_startup(not startup_enabled())
    update_tray()
    for p in list(panels.values()):
        p.render()


def tray_image():
    try:
        n = max(16, user32.GetSystemMetrics(49))        # SM_CXSMICON: exact tray icon size
    except Exception:
        n = 32
    f = n * 8 / 256.0
    big = Image.new("RGBA", (n * 8, n * 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle((16 * f, 16 * f, 240 * f, 240 * f), radius=60 * f, fill=accent())
    pts = [(76 * f, 134 * f), (112 * f, 170 * f), (184 * f, 90 * f)]
    wd = 28 * f
    d.line(pts, fill="white", width=int(wd), joint="curve")
    for x, y in pts:
        d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill="white")
    return big.resize((n, n), Image.LANCZOS)


def notify(title_msg, message):
    try:
        icon.notify(message, title_msg)
    except Exception:
        pass


def quit_app():
    save()
    if icon is not None:
        icon.stop()
    for w in list(windows.values()):
        w.destroy()
    root.destroy()


def post(fn):
    q.put(fn)


def poll():
    try:
        while True:
            q.get_nowait()()
    except queue.Empty:
        pass
    except Exception:
        log_exc()
    root.after(100, poll)


_desktop = {"shown": None}


def keep_bottom():
    """Watchdog (runs several times a second).
    - normal: cards sit behind your windows, on the wallpaper
    - 'Show desktop' / Win+D (no app windows left): cards go on top so they stay visible
    - any card Windows hid or moved is re-shown and put back"""
    try:
        shown = not desktop_has_app_windows()
        _desktop["shown"] = shown
        for card in list(cards.values()):
            if card.dragging:
                continue
            card.heal()
            if shown:
                card.to_top()
            elif state["settings"]["behind"]:
                card.to_bottom()
            else:
                card.to_normal()
    except Exception:
        log_exc()
    root.after(400, keep_bottom)


def already_running():
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "DeskTask.SingleInstance")
    return kernel32.GetLastError() == 183                      # ERROR_ALREADY_EXISTS


def setup_crash_log():
    global _crash_file
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        for name in ("error.log", "crash.log"):
            p = os.path.join(DATA_DIR, name)
            if os.path.exists(p) and os.path.getsize(p) > 200_000:
                os.remove(p)
        _crash_file = open(os.path.join(DATA_DIR, "crash.log"), "a")
        faulthandler.enable(_crash_file)                       # records hard crashes
    except Exception:
        pass


def run():
    global root, S, TK_FONT, icon, target, hotkey_win, HINST
    setup_crash_log()
    sys.excepthook = lambda t, v, tb: log_text("".join(traceback.format_exception(t, v, tb)))
    threading.excepthook = lambda a: log_text("".join(
        traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    if already_running():
        wake = user32.FindWindowW(None, MSG_TITLE)
        if wake:
            user32.PostMessageW(wake, 0x8001, 0, 0)            # tell the running copy to show itself
        else:
            user32.MessageBoxW(None, "DeskTask is already running.\nLook for its icon in the "
                               "system tray (click the ^ arrow).", APP_NAME, 0x40)
        return
    autostart = "--autostart" in sys.argv
    restarted = "--restarted" in sys.argv
    if autostart:
        time.sleep(4)                       # let the desktop finish loading after sign-in
    try:                                    # Windows relaunches us if we ever crash or hang
        kernel32.RegisterApplicationRestart("--restarted", 0)
    except Exception:
        pass
    fresh = not load_state()                # True only the very first time DeskTask ever runs
    atexit.register(save)
    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda *a: log_exc()
    try:
        S = max(1.0, user32.GetDpiForSystem() / 96.0)
    except Exception:
        S = max(1.0, root.winfo_fpixels("1i") / 96.0)
    fams = set(tkfont.families())
    TK_FONT = next((f for f in ["SF Pro Display", "SF Pro Text", "Segoe UI Variable Display",
                                "Segoe UI"] if f in fams), "Segoe UI")
    HINST = kernel32.GetModuleHandleW(None)
    register_classes()
    migrate_startup()

    target = Target()
    for t in tasks_with("active"):
        try:
            cards[t["id"]] = Card(t)
        except Exception:
            log_exc()
    hotkey_win = HotkeyWin()
    try:
        separate_overlaps()
    except Exception:
        log_exc()
    if fresh:                               # starter tasks for the tutorial
        for text in ("Drink a glass of water", "Stretch for five minutes", "Plan tomorrow's top three"):
            try:
                add_task(text)
            except Exception:
                log_exc()
    refresh_menu_state()

    menu = pystray.Menu(
        pystray.MenuItem("Add task\tCtrl+Alt+N", lambda: post(add_dialog)),
        pystray.MenuItem(lambda item: f"Tasks ({menu_state['left']} left)",
                         lambda: post(TasksPanel)),
        pystray.MenuItem("Customize", lambda: post(CustomizePanel)),
        pystray.MenuItem("Arrange cards", lambda: post(arrange_cards)),
        pystray.MenuItem("How to use", lambda: post(TutorialPanel)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", lambda: post(toggle_startup),
                         checked=lambda item: menu_state["startup"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda: post(quit_app)),
    )
    class TrayIcon(pystray.Icon):
        def _on_notify(self, wparam, lparam):
            if lparam == 0x0203:                    # double-click: show the tasks list
                post(TasksPanel)
                return
            if lparam == 0x0205:                    # right click: refresh the menu, on this thread,
                try:                                # before it is displayed (never while it is open)
                    refresh_menu_state()
                    self._update_menu()
                except Exception:
                    log_exc()
            return super()._on_notify(wparam, lparam)

    # no default item: left click does nothing, right click opens the menu
    icon = TrayIcon("DeskTask", tray_image(), APP_NAME, menu)
    icon.run_detached()
    root.after(1500, update_tray)
    if not state["meta"]["tutorial_seen"]:
        root.after(1500, TutorialPanel)     # first run: show the visual tutorial
    elif not autostart:                     # manual launch (or crash-restart): confirm it's running
        root.after(2000, lambda: notify(
            "DeskTask restarted" if restarted else "DeskTask is running",
            "Right-click the tray icon for tasks and settings. Press Ctrl+Alt+N to add a task."))
    pump()
    poll()
    keep_bottom()
    root.mainloop()


def main():
    try:
        run()
    except Exception:
        log_exc()
        try:
            user32.MessageBoxW(None, "DeskTask ran into a problem and has to close.\n\nDetails were "
                               "saved to:\n" + os.path.join(DATA_DIR, "error.log"), APP_NAME, 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
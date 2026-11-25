"""
DeskTask - minimalist task cards that live on your desktop.

Tray menu (right-click the icon):  Add task, Tasks, Customize, Start with Windows, Exit
Hotkey:                            Ctrl+Alt+N  adds a task from anywhere
Cards:                             drag anywhere; drop on the green check to complete;
                                   right-click a card to delete it (restorable from Tasks)
"""
import tkinter as tk
import tkinter.font as tkfont
import atexit, collections, ctypes, faulthandler, json, re, shutil, threading, math, os, queue, sys, time, traceback, uuid
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
VERSION = "1.0.10"
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
                      "opacity": 95, "above": False, "weight": "semibold",
                      "capitalize": False},
         "tasks": [],
         "history": {},                      # completed-per-day counts (survives deleting tasks)
         "removed": {},                      # deleted-per-day counts (tasks given up on)
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
card_z = []                   # task ids, front-most card first
card_menu = None
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


def task_items(task):
    """The items of a bullet / numbered list (the task's text is its headline)."""
    return [str(s) for s in task.get("items", []) if str(s).strip()]


def task_plain(task):
    """One-line version of a task, for the Tasks list and dialogs."""
    head = display_text(task["text"]).replace("\n", " ")
    if task.get("kind") in ("bullets", "numbers"):
        items = [display_text(i) for i in task_items(task)]
        if items:
            return head + " \u00b7 " + " \u00b7 ".join(items)
    return head


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
                if t.get("kind") in ("bullets", "numbers") and "items" not in t:     # 1.0.7 lists
                    lines = [ln for ln in str(t["text"]).split("\n") if ln.strip()]
                    t["text"], t["items"] = (lines[0] if lines else "List"), lines[1:]
                tasks.append(t)
        state["tasks"] = tasks
        hist = data.get("history")
        if not isinstance(hist, dict):                         # rebuild from completed tasks
            hist = {}
            for t in tasks:
                if t["status"] == "done" and t.get("done_at"):
                    hist[t["done_at"][:10]] = hist.get(t["done_at"][:10], 0) + 1
        state["history"] = {str(k): int(v) for k, v in hist.items() if isinstance(v, (int, float))}
        rem = data.get("removed")
        state["removed"] = ({str(k): int(v) for k, v in rem.items() if isinstance(v, (int, float))}
                            if isinstance(rem, dict) else {})
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


def bump_counts(counts, iso, delta):
    day = str(iso)[:10]
    n = max(0, counts.get(day, 0) + delta)
    if n:
        counts[day] = n
    else:
        counts.pop(day, None)


def bump_history(iso, delta):
    bump_counts(state["history"], iso, delta)


def week_start(d=None):
    """The most recent Saturday (weeks run Saturday to Friday)."""
    d = d or date.today()
    return d - timedelta(days=(d.weekday() - 5) % 7)


def week_counts():
    """Saturday..Friday: (day, tasks completed, tasks completed + deleted = that day's tasks)."""
    start = week_start()
    out = []
    for i in range(7):
        d = start + timedelta(days=i)
        done = int(state["history"].get(d.isoformat(), 0))
        out.append((d, done, done + int(state["removed"].get(d.isoformat(), 0))))
    return out


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


def bring_front(card):
    tid = card.task["id"]
    if tid in card_z:
        card_z.remove(tid)
    card_z.insert(0, tid)


_layer = {"sig": None, "lifted": set(), "hold": 0.0, "reassert": 0.0}
DESKTOPISH = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
              "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}


def ordered_cards():
    return [cards[i] for i in card_z if i in cards] + [c for i, c in cards.items() if i not in card_z]


def layer_sig():
    return (state["settings"]["above"], tuple(c.task["id"] for c in ordered_cards()),
            frozenset(_layer["lifted"]))


def sync_layers():
    """Tell the layer system the current stacking is already right (new / removed card)."""
    _layer["sig"] = layer_sig()


def apply_layers():
    """Re-applies the stacking of every card. It is only called when something really changed:
    every call briefly swaps overlapping cards, and that swap is what used to flicker."""
    order = ordered_cards()
    lifted = {c.task["id"] for c in order} & _layer["lifted"]
    if state["settings"]["above"]:
        for c in reversed(order):                         # back first, so the front card ends on top
            c.lifted = False
            c.to_top()
    else:
        for c in order:                                   # front first: the front card stays above
            c.to_bottom()
        for c in reversed([c for c in order if c.task["id"] in lifted]):
            c.to_top()
        for c in order:
            c.lifted = c.task["id"] in lifted
    sync_layers()


def foreground_is_desktop():
    """True if the desktop / taskbar has the focus, False if an app does, None if unknown."""
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            return None
        top = user32.GetAncestor(fg, 2) or fg
        if top in windows:
            return None
        buf = ctypes.create_unicode_buffer(60)
        user32.GetClassNameW(top, buf, 60)
        return buf.value in DESKTOPISH
    except Exception:
        return None


def foreign_covers(card):
    """True if some window that is not ours is drawn over the card's centre."""
    try:
        hwnd = user32.WindowFromPoint(W_.POINT(card.x + card.w // 2, card.y + card.h // 2))
        if not hwnd:
            return False
        top = user32.GetAncestor(hwnd, 2) or hwnd
        if top in windows:
            return False
        buf = ctypes.create_unicode_buffer(40)
        user32.GetClassNameW(top, buf, 40)
        return buf.value not in DESKTOP_CLASSES
    except Exception:
        return False


def check_layers():
    """Looks (without touching any window) for a reason to change the stacking."""
    now = time.time()
    order = [c for c in ordered_cards() if not c.editing]
    if state["settings"]["above"]:
        if now - _layer["reassert"] > 1.5 and any(foreign_covers(c) for c in order):
            _layer["reassert"] = now                      # something else got on top of the cards
            apply_layers()
        elif layer_sig() != _layer["sig"]:
            apply_layers()
        return
    lifted = _layer["lifted"]
    changed = False
    relower = bool(lifted) and now >= _layer["hold"] and foreground_is_desktop() is False
    if relower:                                           # an app has the focus again: back down
        lifted.clear()
        apply_layers()
    for c in order:                                       # desktop shown over a card: lift it
        if c.task["id"] not in lifted and desktop_covers(c):
            lifted.add(c.task["id"])
            changed = True
            if relower:
                _layer["hold"] = now + 2.0                # don't ping-pong
    if changed or layer_sig() != _layer["sig"]:
        apply_layers()


def place_card(task, x, y):
    c = cards.get(task["id"])
    task["x"], task["y"] = x, y
    if c:
        c.move(x - c.pad, y - c.pad)


def refit_card(task):
    """After a card changed size (e.g. became a list): keep it on screen and off other cards."""
    c = cards.get(task["id"])
    if not c:
        return
    c.draw()
    x, y = c.clamp(task["x"], task["y"])
    size = (c.w - 2 * c.pad, c.h - 2 * c.pad)
    others = [card_rect(o) for o in tasks_with("active") if o is not task and o["id"] in cards]
    if any(overlap_fraction((x, y) + size, r) > 0.3 for r in others):
        x, y = free_slot(others, size)
    place_card(task, x, y)


def set_kind(task, kind):
    """Right-click menu. On a normal task: it becomes a list whose headline is its text, and you
    type the items right on the card. On a list: switch bullets <-> numbers, or click the active
    type again to turn it back into a normal task."""
    old = task.get("kind", "task")
    if old == kind:                                        # back to a normal task: the items are removed
        task.pop("kind", None)
        task.pop("items", None)
        refit_card(task)
        save()
        refresh_all()
        return
    task["kind"] = kind
    task.setdefault("items", [])
    refit_card(task)
    save()
    refresh_all()
    if old == "task":
        ListEditor(task)


def rename_task(task, text):
    text = text.strip()[:300]
    if text:
        task["text"] = text
        refit_card(task)
        save()
        refresh_all()


def arrange_cards():
    """Keeps the highest card where it is and glides all the others, one after another, into a neat
    column right under it (ordered by their current height). A full column continues to its right."""
    actives = [(t, cards[t["id"]]) for t in tasks_with("active") if t["id"] in cards and not cards[t["id"]].editing]
    if not actives:
        return
    actives.sort(key=lambda tc: (tc[0]["y"], tc[0]["x"]))
    ax, ay = actives[0][0]["x"], actives[0][0]["y"]
    left, top, right, bottom = work_area(ax, ay)
    gap, x, y, plan = px(14), ax, ay, []
    for t, c in actives:
        w, h = c.w - 2 * c.pad, c.h - 2 * c.pad
        if y > ay and y + h > bottom - px(24):               # column is full: start the next one
            x, y = x + w + px(20), ay
        plan.append((t, c, min(x, max(left, right - w - px(10))), y))
        y += h + gap
    run_glides(plan)


_anim = {"running": False, "glides": []}


def run_glides(plan, duration=0.42, stagger=0.065):
    """One shared, time-based animator for all cards (same look as before, but smooth):
    positions follow the clock instead of counting frames, so a late frame never slows the
    animation down, and every card moves in a single batched call per frame."""
    if _anim["running"]:                                   # pressed again: finish the previous run first
        for g in _anim["glides"]:
            if not g["done"]:
                g["done"] = True
                g["t"]["x"], g["t"]["y"] = g["x1"], g["y1"]
                if g["c"].hwnd in windows:
                    g["c"].move(g["x1"] - g["c"].pad, g["y1"] - g["c"].pad)
    glides = [{"t": t, "c": c, "x0": c.x + c.pad, "y0": c.y + c.pad, "x1": x, "y1": y,
               "start": i * stagger, "dur": duration, "done": False}
              for i, (t, c, x, y) in enumerate(plan)]
    _anim["running"], _anim["glides"] = True, glides
    timer_resolution(True)
    t0 = time.perf_counter()

    def frame():
        el = time.perf_counter() - t0
        batch, waiting = [], False
        for g in glides:
            c = g["c"]
            if g["done"]:
                continue
            if c.hwnd not in windows or c.dragging:        # completed, deleted or grabbed meanwhile
                g["done"] = True
                continue
            k = (el - g["start"]) / g["dur"]
            if k <= 0:
                waiting = True
            elif k >= 1:
                g["done"] = True
                g["t"]["x"], g["t"]["y"] = g["x1"], g["y1"]
                batch.append((c, g["x1"] - c.pad, g["y1"] - c.pad))
            else:
                e = ease_out_back(k)                       # smooth glide with a tiny overshoot
                batch.append((c, g["x0"] + (g["x1"] - g["x0"]) * e - c.pad,
                              g["y0"] + (g["y1"] - g["y0"]) * e - c.pad))
                waiting = True
        move_many(batch)
        if waiting:
            root.after(8, frame)
        else:
            _anim["running"] = False
            timer_resolution(False)
            save()
    frame()


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
    sync_layers()
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
    was_open = task["status"] == "active"
    task["status"] = "deleted"
    task["deleted_at"] = now_iso()
    if was_open:                                           # gave up on it: counts toward that day's total
        task["del_counted"] = True
        bump_counts(state["removed"], task["deleted_at"], 1)
    card = cards.pop(task["id"], None)
    sync_layers()
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
    if task.pop("del_counted", False) and task.get("deleted_at"):
        bump_counts(state["removed"], task["deleted_at"], -1)
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


def clear_progress():
    """Resets the weekly chart and empties the Done list. Current tasks stay."""
    state["history"], state["removed"] = {}, {}
    state["tasks"] = [t for t in state["tasks"] if t["status"] != "done"]
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


def ui_weight(bold=False):
    """Weight for window text. Follows the Font weight chosen in Customize; headings are one step heavier."""
    i = WEIGHTS.index(state["settings"].get("weight", "semibold"))
    return WEIGHTS[min(len(WEIGHTS) - 1, i + (1 if bold else 0))]


def tk_weight():
    return "normal" if state["settings"].get("weight") == "regular" else "bold"


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
        return load_font(round(size * S), ui_weight(bold))

    def text_w(self, s, size, bold=False):
        return self.font(size, bold).getlength(s) / S

    def text(self, x, y, s, size, fill, bold=False, anchor="lm"):
        self.texts.append((x, y, s, size, fill, bold, anchor))     # drawn crisp at the end

    def text_fit(self, x, y, s, size, fill, bold=False, anchor="lm", maxw=None):
        """Like text(), but shrinks the text a little if a heavy font weight would not fit."""
        while maxw and size > 8 and self.text_w(s, size, bold) > maxw:
            size -= 0.5
        self.text(x, y, s, size, fill, bold, anchor)

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


def compose_shadow_multi(final, rects, offset=4, blur=9, alpha=55):
    """Soft shadow under several rounded rectangles (x, y, w, h, radius, all in final pixels)."""
    W, H = final.size
    sh = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(sh)
    for x, y, w, h, r in rects:
        d.rounded_rectangle((x, y + px(offset), x + w - 1, y + px(offset) + h - 1), radius=r, fill=alpha)
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


def icon_bullets(g, cx, cy, s, color, w=1.8):
    for k in (-1, 0, 1):
        y = cy + k * .34 * s
        g.circle(cx - .38 * s, y, .1 * s, fill=color)
        g.line([(cx - .12 * s, y), (cx + .42 * s, y)], w, color)


def icon_numbers(g, cx, cy, s, color, w=1.8):
    for k in (-1, 0, 1):
        y = cy + k * .34 * s
        g.text(cx - .38 * s, y, str(k + 2), s * .46, color, bold=True, anchor="mm")
        g.line([(cx - .08 * s, y), (cx + .42 * s, y)], w, color)


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
        g.text_fit(sx + sw / 2, y + h / 2, label, 13 if n <= 3 else 12, th["text"] if i == sel else th["sub"],
                   bold=(i == sel), anchor="mm", maxw=sw - 8)
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
        if key == "above":
            _layer["lifted"].clear()
            apply_layers()
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
dwmapi = ctypes.windll.dwmapi
user32.GetClassNameW.argtypes = [W_.HWND, W_.LPWSTR, ctypes.c_int]
user32.WindowFromPoint.argtypes = [W_.POINT]
user32.WindowFromPoint.restype = W_.HWND
user32.GetAncestor.argtypes = [W_.HWND, W_.UINT]
user32.GetAncestor.restype = W_.HWND
user32.GetForegroundWindow.restype = W_.HWND
dwmapi.DwmGetWindowAttribute.argtypes = [W_.HWND, W_.DWORD, ctypes.c_void_p, W_.DWORD]
dwmapi.DwmSetWindowAttribute.argtypes = [W_.HWND, W_.DWORD, ctypes.c_void_p, W_.DWORD]
user32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
user32.BeginDeferWindowPos.restype = ctypes.c_void_p
user32.DeferWindowPos.argtypes = [ctypes.c_void_p, W_.HWND, W_.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, W_.UINT]
user32.DeferWindowPos.restype = ctypes.c_void_p
user32.EndDeferWindowPos.argtypes = [ctypes.c_void_p]
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


DESKTOP_CLASSES = {"Progman", "WorkerW"}


def desktop_covers(card):
    """True if, right now, the desktop itself is what you see where this card should be
    (Windows raises the desktop above everything when you 'show desktop').
    A real app window on top of the card does NOT count: that is exactly where the card belongs."""
    try:
        hwnd = user32.WindowFromPoint(W_.POINT(card.x + card.w // 2, card.y + card.h // 2))
        if not hwnd:
            return False
        top = user32.GetAncestor(hwnd, 2) or hwnd              # GA_ROOT
        if top in windows:                                     # one of our own windows: card is visible
            return False
        buf = ctypes.create_unicode_buffer(40)
        user32.GetClassNameW(top, buf, 40)
        return buf.value in DESKTOP_CLASSES
    except Exception:
        return False


def virtual_screen():
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # x, y, w, h


def timer_resolution(fine):
    """Windows timers normally tick every ~15 ms; ask for 1 ms while an animation runs."""
    try:
        (ctypes.windll.winmm.timeBeginPeriod if fine else ctypes.windll.winmm.timeEndPeriod)(1)
    except Exception:
        pass


def move_many(items):
    """Moves several windows (window, x, y) in ONE call, so Windows redraws them together."""
    items = [(w, int(x), int(y)) for w, x, y in items]
    if not items:
        return
    try:
        h = user32.BeginDeferWindowPos(len(items))
        if not h:
            raise OSError("BeginDeferWindowPos failed")
        for w, x, y in items:
            h = user32.DeferWindowPos(h, w.hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010 | 0x0200 | 0x0400)
            if not h:
                raise OSError("DeferWindowPos failed")
        user32.EndDeferWindowPos(h)
        for w, x, y in items:
            w.x, w.y = x, y
    except Exception:                                      # never lose a move: fall back to one by one
        for w, x, y in items:
            w.move(x, y)


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
        if getattr(self, "editing", False):
            return
        try:
            if not user32.IsWindowVisible(self.hwnd) or user32.IsIconic(self.hwnd):
                self.show()
            r = RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(r))
            if abs(r.left - self.x) > 2 or abs(r.top - self.y) > 2:
                self._misses = getattr(self, "_misses", 0) + 1
                if self._misses <= 2:
                    self.move(self.x, self.y)
                else:                                     # the system insists: accept where it is
                    self.x, self.y = r.left, r.top
            else:
                self._misses = 0
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
        self.task, self.dragging, self.pad, self.lifted = task, False, px(PAD), False
        self.editing, self.last_click = False, 0.0
        self.exclude_from_peek()
        self.draw()
        if task.get("x") is None:
            task["x"], task["y"] = next_pos()
        x, y = self.clamp(task["x"], task["y"])
        task["x"], task["y"] = x, y
        self.move(x - self.pad, y - self.pad)
        self.show()
        tid = task["id"]                                   # join the stack without disturbing the others
        cards[tid] = self
        if tid in card_z:
            card_z.remove(tid)
        if state["settings"]["above"]:
            card_z.insert(0, tid)
            self.to_top()
        else:
            card_z.append(tid)
            self.to_bottom()
        sync_layers()

    def destroy(self):
        super().destroy()
        sync_layers()

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
        kind = self.task.get("kind", "task")
        is_list = kind in ("bullets", "numbers")
        tx = hp if is_list else hp + dot + px(12)
        rows, marks = [], []             # rows: (indent, text, font, line height, ascent, gap before)
        if is_list:
            hf = load_font(round(s["size"] * 1.32 * S), ui_weight(True))
            ha, hd = hf.getmetrics()
            hlh = int((ha + hd) * 1.1)
            for line in wrap(display_text(self.task["text"]), hf, cw - tx - hp) or [""]:
                rows.append((0, line, hf, hlh, ha, 0))
            items = task_items(self.task)
            prefix_w = px(20) if kind == "bullets" else int(f.getlength(f"{len(items)}.")) + px(10)
            for n, item in enumerate(items, 1):
                marks.append((len(rows), n))
                for k, line in enumerate(wrap(display_text(item), f, cw - tx - hp - prefix_w) or [""]):
                    rows.append((prefix_w, line, f, lh, asc, px(9) if (n == 1 and k == 0) else 0))
        else:
            rows = [(0, line, f, lh, asc, 0) for line in wrap(display_text(self.task["text"]), f, cw - tx - hp)]
        cap = 18 if is_list else 12
        if len(rows) > cap:
            rows = rows[:cap]
            last = rows[-1]
            rows[-1] = (last[0], last[1].rstrip() + "\u2026") + last[2:]
            marks = [m for m in marks if m[0] < cap]
        total = sum(r[3] + r[5] for r in rows)
        ch = max(px(54), 2 * vp + total)
        big, pad, W, H = make_card_base(cw, ch, radius, th)
        y, ys = pad + (ch - total) // 2, []
        for r in rows:
            y += r[5]
            ys.append(y)
            y += r[3]
        bd = ImageDraw.Draw(big)
        if not is_list:
            dc = ys[0] + int(asc * 0.62)
            bd.ellipse(((pad + hp) * SS, (dc - dot // 2) * SS, (pad + hp + dot) * SS,
                        (dc + dot // 2 + dot % 2) * SS), fill=acc)
        elif kind == "bullets":
            rad = max(2, px(3))
            for ri, _n in marks:
                cx, cy = pad + tx + px(5), ys[ri] + int(asc * 0.62)
                bd.ellipse(((cx - rad) * SS, (cy - rad) * SS, (cx + rad) * SS, (cy + rad) * SS), fill=acc)
        body = big.resize((W, H), Image.LANCZOS)
        d = ImageDraw.Draw(body)
        if marks:                                            # thin rule between headline and items
            ry = ys[marks[0][0]] - px(5)
            d.line((pad + tx, ry, pad + cw - hp, ry), fill=th["border"], width=1)
        for i, (indent, line, font, _lh, _asc, _gap) in enumerate(rows):
            d.text((pad + tx + indent, ys[i]), line, font=font, fill=th["text"])
        if kind == "numbers":
            for ri, n in marks:
                d.text((pad + tx, ys[ri]), f"{n}.", font=f, fill=acc)
        self.alpha = int(255 * s["opacity"] / 100)
        self.set_image(compose_shadow(body, pad, cw, ch, radius))

    def apply_opacity(self):
        self.set_alpha(255 * state["settings"]["opacity"] / 100)

    # dragging is driven by polling the mouse, so nothing can interrupt it
    def on_press(self):
        if card_menu:
            card_menu.close()
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
            bring_front(self)
            self.to_top()
            target.show()
        if self.dragging:
            self.move(self.ox + cx - self.sx, self.oy + cy - self.sy)
            target.place(cx, cy)
            target.set_hover(target.contains(cx, cy))
        root.after(8, self.tick)

    def release(self):
        if not self.dragging:                              # a plain click: two of them edit the card
            now = time.time()
            if now - self.last_click < 0.45 and not card_menu:
                self.last_click = 0.0
                self.edit()
            else:
                self.last_click = now
            return
        self.dragging = False
        inside = target.contains(*cursor())
        apply_layers()                                     # the dragged card goes back to its layer
        if inside:
            target.drop()
            complete_task(self.task)
        else:
            target.dismiss()
            x, y = self.clamp(self.x + self.pad, self.y + self.pad)
            self.move(x - self.pad, y - self.pad)
            self.task["x"], self.task["y"] = x, y
            save()

    def edit(self):
        if self.task.get("kind") in ("bullets", "numbers"):
            ListEditor(self.task)
        else:
            add_dialog(edit_task=self.task)

    def on_rpress(self):                                       # right-click opens the card menu
        if not self.dragging and self.task.get("status") == "active":
            CardMenu(self)

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
    key, bw, bh = "customize", 360, 632

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

        g.rrect(20, 428, 320, 132, 16, fill=th["row"])
        for ly in (472, 516):
            g.line([(36, ly), (324, ly)], 1, th["sep"])
        g.text_fit(36, 450, "Capitalize Each Word", 14.5, th["text"], maxw=212)
        switch(g, th, 260, 436, s["capitalize"], lambda: set_setting("capitalize", not s["capitalize"]))
        g.text_fit(36, 494, "Keep cards above windows", 14.5, th["text"], maxw=212)
        switch(g, th, 260, 480, s["above"], lambda: set_setting("above", not s["above"]))
        g.text_fit(36, 538, "Start with Windows", 14.5, th["text"], maxw=212)
        switch(g, th, 260, 524, startup_enabled(), toggle_startup)

        g.rrect(20, 572, 320, 40, 14, fill=mix(RED, th["bg"], .14))
        g.text_fit(180, 592, "Clear progress\u2026", 14.5, RED, bold=True, anchor="mm", maxw=290)
        g.hit(20, 572, 320, 40, "click", lambda: ConfirmPanel(
            title="Clear all progress?",
            lines=["Resets your weekly chart and empties the Done list.", "Your current tasks are kept."],
            label="Clear", action=clear_progress))


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
        ti = next(i for i, (d, _, _) in enumerate(counts) if d == today)
        today_done, today_all, week = counts[ti][1], counts[ti][2], sum(c[1] for c in counts)
        g.text(38, 86, "This week", 12, th["sub"], bold=True)
        g.text(342, 86, (f"{today_done}/{today_all}" if today_all > today_done else f"{today_done}")
               + f" today \u00b7 {week} this week", 12, th["sub"], anchor="rm")
        top = max(max(c[2] for c in counts), 1)
        slot = 304 / 7
        grey = mix(th["sub"], th["row"], .3)                     # the day's tasks (done + deleted)
        for i, (d, done, total) in enumerate(counts):
            future = d > today
            cx = 38 + slot * i + slot / 2
            if total == 0:
                g.rrect(cx - 12, 156, 24, 6, 3, fill=mix(th["track"], th["row"], .5) if future else th["track"])
            else:
                ht = 8 + (total / top) * 44
                g.rrect(cx - 12, 162 - ht, 24, ht, 6, fill=grey)
                if done:
                    hd = 8 + (done / top) * 44
                    g.rrect(cx - 12, 162 - hd, 24, hd, 6, fill=acc if i == ti else mix(acc, th["row"], .5))
                label = f"{done}/{total}" if total > done else str(done)
                g.text(cx, 162 - ht - 9, label, 10.5 if total > done else 11, th["sub"], bold=True, anchor="mm")
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
            title = g.fit(task_plain(t), 14.5, 340 - 56 - len(btns) * 38 - 6)
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
    """'Are you sure?' box. By default it asks about deleting a task; other actions pass their own text."""
    key, bw, bh = "confirm", 340, 184

    def __init__(self, task=None, title="Delete this task?", lines=None, label="Delete", action=None):
        self.task, self.title, self.label = task, title, label
        self.lines = lines or ([task_plain(task), "You can restore it later from Tasks."] if task else [])
        self.action = action
        super().__init__()

    def initial_pos(self, W, H):                                 # appears right where you clicked
        cx, cy = cursor()
        left, top, right, bottom = work_area(cx, cy)
        return min(max(left, cx - W // 2), right - W), min(max(top, cy - H // 2), bottom - H)

    def confirm(self):
        if self.action:
            self.action()
        elif self.task is not None and self.task.get("status") == "active":
            delete_task(self.task)
        self.close()

    def draw(self, g, th):
        g.hit(0, 0, self.bw, self.bh, "move", None)
        g.text(26, 40, self.title, 18, th["text"], bold=True)
        for i, line in enumerate(self.lines[:2]):
            size = 14 if i == 0 else 12
            g.text(26, 72 + i * 24, g.fit(line, size, self.bw - 52), size, th["sub"])
        w = (self.bw - 52 - 12) / 2
        g.rrect(26, 124, w, 40, 20, fill=th["seg"])
        g.text_fit(26 + w / 2, 144, "Cancel", 14.5, th["text"], bold=True, anchor="mm", maxw=w - 12)
        g.hit(26, 124, w, 40, "click", self.close)
        g.rrect(38 + w, 124, w, 40, 20, fill=RED)
        g.text_fit(38 + w + w / 2, 144, self.label, 14.5, "#FFFFFF", bold=True, anchor="mm", maxw=w - 12)
        g.hit(38 + w, 124, w, 40, "click", self.confirm)


class CardMenu(Layered):
    """Right-click menu beside a card: two list types and Delete, as equal rounded buttons in a
    rounded container. Hovering a button inverts its colours and shows an info card for it."""
    BW, BH, GAP, PADC, SPACE, IW, IH = 150, 42, 8, 10, 10, 244, 118
    CW = BW + 2 * PADC
    CH = 3 * BH + 2 * GAP + 2 * PADC
    ITEMS = (
        ("bullets", "Bullet list", "Your text becomes the headline. Then type the list items right on the card."),
        ("numbers", "Numbered list", "Your text becomes the headline. Then type the numbered items right on the card."),
        ("delete", "Delete", "Removes this task. You will be asked to confirm, and you can restore it from Tasks."),
    )

    def __init__(self, card):
        global card_menu
        if card_menu is not None:
            card_menu.close()
        super().__init__(cls=CLS_PANEL)
        card_menu = self
        self.card, self.task = card, card.task
        self.hover, self.closed, self.born = None, False, time.time()
        content_w = self.CW + self.SPACE + self.IW
        self.lw, self.lh = content_w + 2 * PAD, self.CH + 2 * PAD
        left, top, right, bottom = work_area(card.x, card.y)
        card_l, card_r, gap = card.x + card.pad, card.x + card.w - card.pad, px(12)
        self.side = "right" if card_r + gap + px(content_w) <= right else "left"
        self.x0 = card_r + gap if self.side == "right" else card_l - gap - px(content_w)
        mid = card.y + card.h // 2
        self.y0 = min(max(mid - px(self.CH) // 2, top + px(8)), bottom - px(self.CH) - px(8))
        self.cont_x = 0 if self.side == "right" else self.IW + self.SPACE
        self.info_x = self.CW + self.SPACE if self.side == "right" else 0
        self.render()
        self.move(self.x0 - px(PAD), self.y0 - px(PAD))
        self.show()
        self.to_top()
        self.tick()

    # --- geometry (logical units, relative to the content box)
    def local(self, cx, cy):
        return (cx - self.x0) / S, (cy - self.y0) / S

    def btn_rect(self, i):
        return self.cont_x + self.PADC, self.PADC + i * (self.BH + self.GAP)

    def info_y(self, i):
        mid = self.PADC + i * (self.BH + self.GAP) + self.BH / 2
        return min(max(mid - self.IH / 2, 0), self.CH - self.IH)

    def item_at(self, cx, cy):
        lx, ly = self.local(cx, cy)
        for i in range(len(self.ITEMS)):
            bx, by = self.btn_rect(i)
            if bx <= lx <= bx + self.BW and by <= ly <= by + self.BH:
                return i
        return None

    def inside(self, cx, cy):
        lx, ly = self.local(cx, cy)
        if self.cont_x <= lx <= self.cont_x + self.CW and 0 <= ly <= self.CH:
            return True
        if self.hover is not None:
            iy = self.info_y(self.hover)
            return self.info_x <= lx <= self.info_x + self.IW and iy <= ly <= iy + self.IH
        return False

    def render(self):
        if self.closed:
            return
        th, acc = theme(), accent()
        kind = self.task.get("kind", "task")
        g = Gfx(self.lw, self.lh, 3, origin=PAD)
        g.rrect(self.cont_x, 0, self.CW, self.CH, 18, fill=th["bg"], outline=th["sep"])
        rects = [(px(PAD + self.cont_x), px(PAD), px(self.CW), px(self.CH), px(18))]
        for i, (key, label, _desc) in enumerate(self.ITEMS):
            bx, by = self.btn_rect(i)
            if key == "delete":
                fill, fg, hfill, hfg = mix(RED, th["bg"], .15), RED, RED, "#FFFFFF"
            elif key == kind:                                    # the type this task already is
                fill, fg, hfill, hfg = mix(acc, th["bg"], .2), acc, acc, "#FFFFFF"
            else:
                fill, fg, hfill, hfg = th["seg"], th["text"], th["text"], th["bg"]
            if self.hover == i:                                  # hovered: colours inverted
                fill, fg = hfill, hfg
            g.rrect(bx, by, self.BW, self.BH, 12, fill=fill)
            icon = {"bullets": icon_bullets, "numbers": icon_numbers, "delete": icon_trash}[key]
            icon(g, bx + 22, by + self.BH / 2, 17, fg)
            g.text_fit(bx + 42, by + self.BH / 2, label, 13, fg, bold=True, maxw=self.BW - 50)
        if self.hover is not None:
            key, label, desc = self.ITEMS[self.hover]
            iy = self.info_y(self.hover)
            g.rrect(self.info_x, iy, self.IW, self.IH, 16, fill=th["card"], outline=th["sep"])
            if key == kind:
                label = f"{label} (on)"
                desc = "Double-click the card to edit its items. Click here for a normal task (the items are removed)."
            g.text(self.info_x + 16, iy + 26, label, 14.5, th["text"], bold=True)
            for k, line in enumerate(wrap(desc, g.font(12.5), (self.IW - 32) * S)[:4]):
                g.text(self.info_x + 16, iy + 54 + k * 19, line, 12.5, th["sub"])
            rects.append((px(PAD + self.info_x), px(PAD + iy), px(self.IW), px(self.IH), px(16)))
        self.set_image(compose_shadow_multi(g.result(), rects))

    def on_press(self):
        i = self.item_at(*cursor())
        if i is not None:
            self.activate(i)

    def activate(self, i):
        key, task = self.ITEMS[i][0], self.task
        self.close()
        if key == "delete":
            ConfirmPanel(task)
        else:
            set_kind(task, key)

    def tick(self):
        if self.closed:
            return
        try:
            cx, cy = cursor()
            hov = self.item_at(cx, cy)
            if hov != self.hover:
                self.hover = hov
                self.render()
            esc = user32.GetAsyncKeyState(0x1B) & 0x8000
            down = (user32.GetAsyncKeyState(0x01) | user32.GetAsyncKeyState(0x02)) & 0x8000
            gone = self.task.get("status") != "active" or self.task["id"] not in cards
            if gone or esc or (down and time.time() - self.born > 0.25 and not self.inside(cx, cy)):
                self.close()
                return
            self.to_top()
        except Exception:
            log_exc()
        root.after(30, self.tick)

    def close(self):
        global card_menu
        if self.closed:
            return
        self.closed = True
        if card_menu is self:
            card_menu = None
        self.destroy()


# ================================================================== list editor (typed right on the card)
list_editor = None


def force_focus(win, widget):
    try:                                       # lets us take the keyboard even when opened by a hotkey
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    widget.focus_force()


class EditorBG(Layered):
    """The rounded card (with Save button) behind the text boxes."""

    def __init__(self, editor):
        super().__init__(cls=CLS_PANEL)
        self.editor = editor

    def on_press(self):
        self.editor.bg_press()


class _Row:
    pass


class ListEditor:
    """Edit a list on the card itself: a headline, then one text box per item. Typing in the last
    box adds a fresh empty one below it. Enter moves to the next line; Enter on an empty line,
    the Save button or a click outside saves; Esc cancels."""
    MAX_ITEMS = 20

    def __init__(self, task):
        global list_editor
        if list_editor is not None:
            list_editor.save()
        list_editor = self
        s, th = state["settings"], theme()
        self.task, self.card = task, cards.get(task["id"])
        self.kind = task.get("kind", "bullets")
        self.th, self.acc, self.closed, self.born = th, accent(), False, time.time()
        self.cw, self.hp, self.vp = px(300), px(18), px(14)
        self.ew = self.cw - 2 * self.hp
        self.f_head = tkfont.Font(family=TK_FONT, size=-round(s["size"] * 1.32 * S), weight="bold")
        self.f_item = tkfont.Font(family=TK_FONT, size=-round(s["size"] * S), weight=tk_weight())
        self.f_pre = tkfont.Font(family=TK_FONT, size=-round(s["size"] * S), weight="bold")
        self.head_h = self.f_head.metrics("linespace") + px(12)
        self.row_h = self.f_item.metrics("linespace") + px(12)
        self.pw = px(24) if self.kind == "bullets" else max(px(24), self.f_item.measure("00.") + px(8))
        c = self.card
        self.bx, self.by = (c.x + c.pad, c.y + c.pad) if c else (px(350), px(160))
        if c:                                       # the card is replaced by the editor while editing
            c.editing = True
            c.hide()
        self.bg = EditorBG(self)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg=th["card"])
        self.win.attributes("-topmost", True)
        self.rows = []
        # headline
        self.head_var = tk.StringVar(value=str(task["text"]))
        self.head = tk.Entry(self.win, textvariable=self.head_var, font=self.f_head, bd=0, relief="flat",
                             bg=th["card"], fg=th["text"], insertbackground=self.acc, highlightthickness=0)
        self.head_ph = tk.Label(self.win, text="Headline", font=self.f_head, bg=th["card"], fg=th["sub"], anchor="w")
        self.head_ph.bind("<Button-1>", lambda e: self.head.focus_force())
        self.head_var.trace_add("write", lambda *a: self.update_ph(None))
        self.head.bind("<Return>", lambda e: self.focus_row(0))
        self.head.bind("<Down>", lambda e: self.focus_row(0))
        self.head.bind("<Escape>", lambda e: self.cancel())
        for text in task_items(task):
            self.add_row(text, layout=False)
        self.add_row("", layout=False)
        self.pill = (0, 0, 0, 0)
        self.relayout(first=True)
        self.bg.show()
        self.bg.to_top()
        self.win.lift()
        self.refresh_prefixes()
        first_empty = next((r for r in self.rows if not r.var.get()), self.rows[-1])
        self.win.after(60, lambda: force_focus(self.win, first_empty.entry))
        self.win.after(300, lambda: force_focus(self.win, first_empty.entry) if not self.closed else None)
        self.watch()

    # ---- rows
    def add_row(self, text="", layout=True):
        th = self.th
        r = _Row()
        r.var = tk.StringVar(value=text)
        r.label = tk.Label(self.win, font=self.f_pre, bg=th["card"], fg=self.acc, anchor="w")
        r.entry = tk.Entry(self.win, textvariable=r.var, font=self.f_item, bd=0, relief="flat", bg=th["card"],
                           fg=th["text"], insertbackground=self.acc, highlightthickness=0)
        r.ph = tk.Label(self.win, text="Add an item", font=self.f_item, bg=th["card"], fg=th["sub"], anchor="w")
        r.ph.bind("<Button-1>", lambda e, r=r: r.entry.focus_force())
        r.entry.bind("<Return>", lambda e, r=r: self.on_enter(r))
        r.entry.bind("<BackSpace>", lambda e, r=r: self.on_backspace(r))
        r.entry.bind("<Up>", lambda e, r=r: self.step(r, -1))
        r.entry.bind("<Down>", lambda e, r=r: self.step(r, 1))
        r.entry.bind("<Escape>", lambda e: self.cancel())
        self.rows.append(r)
        r.var.trace_add("write", lambda *a, r=r: self.changed(r))
        if layout:
            self.refresh_prefixes()
            self.relayout()
        return r

    def remove_row(self, r):
        had_focus = self.win.focus_get() is r.entry
        idx = self.rows.index(r)
        self.rows.remove(r)
        for w in (r.label, r.entry, r.ph):
            w.destroy()
        self.refresh_prefixes()
        self.relayout()
        if had_focus:
            target_entry = self.rows[idx - 1].entry if idx > 0 else self.head
            target_entry.focus_force()
            target_entry.icursor("end")

    def changed(self, r):
        self.update_ph(r)
        self.normalize()

    def normalize(self):
        """Always exactly one empty box at the end, never two."""
        while len(self.rows) > 1 and not self.rows[-1].var.get().strip() and not self.rows[-2].var.get().strip():
            self.remove_row(self.rows[-1])
        if self.rows[-1].var.get().strip() and len(self.rows) < self.MAX_ITEMS:
            self.add_row()

    def update_ph(self, r):
        if r is None:
            if self.head_var.get():
                self.head_ph.place_forget()
            else:
                self.head_ph.place(x=0, y=0, width=self.ew, height=self.head_h)
            return
        if r.var.get():
            r.ph.place_forget()
        else:
            i = self.rows.index(r)
            r.ph.place(x=self.pw + px(2), y=self.head_h + px(6) + i * self.row_h, height=self.row_h)

    def refresh_prefixes(self):
        for i, r in enumerate(self.rows, 1):
            r.label.config(text="\u2022" if self.kind == "bullets" else f"{i}.")

    # ---- keys
    def entries(self):
        return [self.head] + [r.entry for r in self.rows]

    def focus_row(self, i):
        if 0 <= i < len(self.rows):
            self.rows[i].entry.focus_force()
            self.rows[i].entry.icursor("end")
        return "break"

    def step(self, r, d):
        es = self.entries()
        j = es.index(r.entry) + d
        if 0 <= j < len(es):
            es[j].focus_force()
            es[j].icursor("end")
        return "break"

    def on_enter(self, r):
        if not r.var.get().strip():
            self.save()
        else:
            self.focus_row(self.rows.index(r) + 1)
        return "break"

    def on_backspace(self, r):
        if r.var.get() == "":
            i = self.rows.index(r)
            if len(self.rows) > 1:
                self.remove_row(r)
            else:
                self.head.focus_force()
                self.head.icursor("end")
            return "break"

    # ---- layout and drawing
    def relayout(self, first=False):
        n = len(self.rows)
        inner = self.head_h + px(6) + n * self.row_h
        self.body_h = self.vp + inner + px(10) + px(34) + px(14)
        self.win.geometry(f"{self.ew}x{inner}+{self.bx + self.hp}+{self.by + self.vp}")
        self.head.place(x=0, y=0, width=self.ew, height=self.head_h)
        self.update_ph(None)
        for i, r in enumerate(self.rows):
            y = self.head_h + px(6) + i * self.row_h
            r.label.place(x=0, y=y, width=self.pw, height=self.row_h)
            r.entry.place(x=self.pw, y=y, width=self.ew - self.pw, height=self.row_h)
            self.update_ph(r)
        self.render_bg()
        if not first:
            self.win.lift()

    def render_bg(self):
        th, acc = self.th, self.acc
        lw, lh = 300, self.body_h / S
        g = Gfx(lw + 2 * PAD, lh + 2 * PAD, 3, origin=PAD)
        g.rrect(0, 0, lw, lh, 18, fill=th["card"], outline=th["border"])
        g.line([(18, 14 + self.head_h / S + 1), (282, 14 + self.head_h / S + 1)], 1, th["border"])
        g.text(18, lh - 14 - 17, "Enter = next line", 11, th["sub"])
        pw_, ph_ = 92, 34
        px_, py_ = lw - 18 - pw_, lh - 14 - ph_
        g.rrect(px_, py_, pw_, ph_, 17, fill=acc)
        g.text(px_ + pw_ / 2, py_ + ph_ / 2, "Save", 14, "#FFFFFF", bold=True, anchor="mm")
        self.pill = (px_, py_, pw_, ph_)
        pad, cwf, chf = px(PAD), px(lw), int(self.body_h)
        self.bg.set_image(compose_shadow(g.result(), pad, cwf, chf, px(18)))
        self.bg.move(self.bx - pad, self.by - pad)

    def bg_press(self):
        cx, cy = cursor()
        lx, ly = (cx - self.bx) / S, (cy - self.by) / S
        x, y, w, h = self.pill
        if x <= lx <= x + w and y <= ly <= y + h:
            self.save()

    # ---- finishing
    def watch(self):
        if self.closed:
            return
        try:
            age = time.time() - self.born
            cx, cy = cursor()
            outside = not (self.bx - px(8) <= cx <= self.bx + self.cw + px(8) and
                           self.by - px(8) <= cy <= self.by + self.body_h + px(8))
            down = user32.GetAsyncKeyState(0x01) & 0x8000
            esc = user32.GetAsyncKeyState(0x1B) & 0x8000
            if self.task.get("status") != "active":
                self.finish()
                return
            if esc and age > 0.4:
                self.cancel()
                return
            if down and outside and age > 0.5:
                self.save()                                  # clicking away keeps what you typed
                return
        except Exception:
            log_exc()
        root.after(40, self.watch)

    def finish(self):
        global list_editor
        if self.closed:
            return
        self.closed = True
        if list_editor is self:
            list_editor = None
        for w in (self.bg, self.win):
            try:
                w.destroy()
            except Exception:
                log_exc()
        c = self.card
        if c is not None and c.hwnd in windows:
            c.editing = False
            c.show()

    def cancel(self):
        t = self.task
        self.finish()
        if not task_items(t) and t.get("kind"):            # a list nobody filled in: back to a normal task
            t.pop("kind", None)
            t.pop("items", None)
            refit_card(t)
            save()
            refresh_all()
        return "break"

    def save(self):
        if self.closed:
            return
        t = self.task
        head = self.head_var.get().strip()
        items = [r.var.get().strip() for r in self.rows if r.var.get().strip()]
        t["text"] = (head or str(t["text"]))[:300]
        if items:
            t["items"] = items[: self.MAX_ITEMS]
        else:                                              # no items typed: it is just a normal task again
            t.pop("kind", None)
            t.pop("items", None)
        self.finish()
        refit_card(t)
        save()
        refresh_all()


# ================================================================== add-task bar
def add_dialog(edit_task=None):
    global add_ui
    if add_ui is not None:
        try:
            if edit_task is None:                # already open: just bring the focus back
                add_ui["focus"]()
                return
            add_ui["close"]()
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
    entry = tk.Entry(win, relief="flat", bd=0, font=(TK_FONT, -px(17), tk_weight()), bg=th["card"], fg=th["sub"],
                     insertbackground=ACCENTS[s["accent"]], highlightthickness=0)
    entry.pack(fill="both", expand=True)
    ph = {"on": edit_task is None}
    if edit_task is None:
        entry.insert(0, "New task")
    else:                                         # renaming a task: show its text
        entry.config(fg=th["text"])
        entry.insert(0, str(edit_task["text"]))
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
        if edit_task is not None:
            if text:
                rename_task(edit_task, text)
        elif text:
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
            entry.icursor(0 if edit_task is None else "end")

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


_watch = {"fg": None, "n": 0}


def keep_bottom():
    """Runs every 30 ms but only LOOKS: it re-stacks the cards when the focus changes or the desktop
    covers them, and does nothing at all while you drag a card. About every 0.4 s it re-shows or
    re-positions a card Windows hid or moved. In steady state it changes no window, so nothing flickers."""
    try:
        if not _anim["running"] and not any(c.dragging for c in cards.values()):
            fg = user32.GetForegroundWindow()
            _watch["n"] += 1
            n, changed = _watch["n"], fg != _watch["fg"]
            _watch["fg"] = fg
            if changed or n % 4 == 0:
                check_layers()
            if n % 13 == 0:
                for card in list(cards.values()):
                    card.heal()
    except Exception:
        log_exc()
    root.after(30, keep_bottom)


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
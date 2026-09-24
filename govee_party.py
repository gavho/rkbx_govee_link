"""
Govee Party Lights  —  Rekordbox -> rkbx_link (OSC) -> Govee Bluetooth bulbs

  python govee_party.py          start the lights (normal use)
  python govee_party.py test     flash each bulb red/green/blue to check they work
  python govee_party.py scan     find bulb addresses (only needed for new bulbs)
  python govee_party.py demo     run everything WITHOUT bulbs (to test the remote / rkbx_link)

Phone remote: open the address printed at startup on a phone on the same Wi-Fi.
Hotkeys (work while Rekordbox is open):
  Ctrl+Alt+1..9, 0   pick a look            Ctrl+Alt+B   blackout on/off
  Ctrl+Alt+C         next colors            Ctrl+Alt+M   next movement
  Ctrl+Alt+K         cycle punch            Ctrl+Alt+A   follow song sections on/off
  Ctrl+Alt+L         cycle bulb layout (together / mirror / alternate)
  Ctrl+Alt+] / [     lights earlier / later (10 ms steps)
  Ctrl+Alt+Up/Down   brightness
"""
import asyncio
import json
import math
import socket
import statistics
import struct
import sys
import time
from urllib.parse import parse_qs, urlparse

# =====================================================================
#  SETTINGS  —  the only part you normally need to touch
# =====================================================================
BULBS = [                      # left-to-right order matters for chase / kick & clap
    "D0:C9:07:C4:57:DF",
    "D0:C9:07:C3:B6:77",
    "D0:C9:07:3F:C4:C1",
]
USE_ALT = True                # set True if your test only worked with --alt
START_LOOK = "Warm groove"     # which look is on when you start
START_BRIGHTNESS = 0.75        # 0.25, 0.5, 0.75 or 1.0
START_LAYOUT = "alternate"     # "together" (all bulbs the same), "mirror" (outer bulbs match), or "alternate"
OSC_PORT = 4460                # must match osc.destination in rkbx_link's config
REMOTE_PORT = 8080             # phone remote web page port
RELIABLE_WRITES = True         # wait for each bulb to confirm a command (steadier connection, ~20-50 ms later)
MIN_GAP_MS = 90                # never start commands to one bulb closer together than this
LIGHT_OFFSET_MS = 40           # default timing: lights fire this many ms BEFORE the beat (adjust live on the remote)

# ---------------------------------------------------------------------
#  COLORS  —  (red, green, blue), 0-255. Add or edit freely.
# ---------------------------------------------------------------------
PALETTES = [
    ("Warm",   [(255, 120, 20), (255, 70, 30), (230, 40, 110), (255, 165, 40)]),
    ("Ocean",  [(0, 90, 255), (0, 185, 210), (95, 45, 255), (0, 140, 255)]),
    ("Sunset", [(255, 95, 30), (255, 45, 90), (165, 45, 205), (255, 140, 55)]),
    ("Neon",   [(255, 40, 150), (0, 190, 255), (150, 50, 255)]),
    ("Ember",  [(205, 0, 35), (125, 0, 95), (175, 20, 60)]),
    ("Citrus", [(255, 200, 0), (120, 255, 20), (255, 120, 0), (255, 235, 60)]),
    ("Ice",    [(140, 200, 255), (200, 170, 255), (90, 140, 255), (220, 235, 255)]),
    ("Jungle", [(0, 220, 90), (0, 170, 150), (120, 230, 20), (0, 200, 200)]),
    ("Miami",  [(255, 60, 140), (0, 210, 190), (255, 150, 90)]),
    ("Candy",  [(255, 90, 200), (170, 110, 255), (255, 140, 170), (110, 200, 255)]),
]

# ---------------------------------------------------------------------
#  PUNCH  —  how hard each beat hits. Never goes fully dark (no strobes).
#    dip  = how far the light drops after the hit (0.55 = down to 45%)
#    tail = how soon it drops, as a fraction of a beat (smaller = snappier)
# ---------------------------------------------------------------------
PUNCH = {
    "soft":   dict(dip=0.18, tail=0.50),
    "medium": dict(dip=0.35, tail=0.32),
    "punchy": dict(dip=0.55, tail=0.18),
}
MAX_DIP = 0.60                 # safety cap: lights never drop below 40% of their level

# How each part of a song feels when "Follow song sections" is on.
#   level  = overall brightness     energy = multiplies the punch
#   every  = change colors every N beats when Speed is on Auto (1 = each beat, 4 = each bar)
SECTIONS = {
    "calm":      dict(level=0.60, energy=0.35, every=2),   # intro / outro
    "groove":    dict(level=0.85, energy=1.00, every=1),   # verse / main groove
    "peak":      dict(level=1.00, energy=1.20, every=1),   # chorus / drop
    "breakdown": dict(level=0.45, energy=0.15, every=4),   # bridge / breakdown
}
# =====================================================================

CHAR = "00010203-0405-0607-0809-0a0b0c0d2b11"
if "--alt" in sys.argv:
    USE_ALT = True


# ---------- color helpers ----------
def lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def scale(c, k):
    return tuple(v * k for v in c)


def cyc(pal, x):
    i = math.floor(x)
    return lerp(pal[i % len(pal)], pal[(i + 1) % len(pal)], x - i)


def bounce(n):
    return [0] if n <= 1 else list(range(n)) + list(range(n - 2, 0, -1))


# ---------- movement patterns ----------
# ctx.step  = counts up each time colors should change (set by Speed)
# ctx.k     = beat brightness: 1.0 right on the beat, lower after the punch drops
# ctx.tail  = True after the drop;  ctx.bar_beat = 0-3 position in the bar
def p_rotate(ctx, pal):
    return [scale(pal[(ctx.step + i) % len(pal)], ctx.k) for i in range(ctx.n)]


def p_chase(ctx, pal):
    seq = bounce(ctx.n)
    pos = seq[ctx.step % len(seq)]
    base = pal[(ctx.bar // 4) % len(pal)]
    accent = pal[(ctx.bar // 4 + 1) % len(pal)]
    return [scale(accent, ctx.k) if i == pos else scale(base, 0.3) for i in range(ctx.n)]


def p_drift(ctx, pal):
    x = ctx.step / 4
    return [scale(cyc(pal, x + i * 0.75), ctx.k) for i in range(ctx.n)]


def p_trade(ctx, pal):
    a = pal[(ctx.bar // 2) % len(pal)]
    b = pal[(ctx.bar // 2 + 1) % len(pal)]
    flip = ctx.step % 2
    return [scale(a if (i + flip) % 2 == 0 else b, ctx.k) for i in range(ctx.n)]


BREATH = [0.40, 0.50, 0.62, 0.75, 0.85, 0.75, 0.62, 0.50]   # one breath = 8 beats


def p_breathe(ctx, pal):
    # slow breathing, one gentle step per beat; each bulb is a little out of phase
    out = []
    for i in range(ctx.n):
        k = BREATH[(ctx.beat + i * 2) % len(BREATH)]
        c = cyc(pal, ctx.bar / 2 + i * 0.5)
        out.append(scale(c, k * (1 - (1 - ctx.k) * 0.3)))
    return out


def p_unison(ctx, pal):
    c = pal[ctx.step % len(pal)]
    return [scale(c, ctx.k) for _ in range(ctx.n)]


def p_pump(ctx, pal):
    # colors hold for the whole bar; every beat pumps the brightness
    return [scale(pal[(ctx.bar + i) % len(pal)], ctx.k) for i in range(ctx.n)]


def p_kickclap(ctx, pal):
    # outer bulbs hit on the kick-ish beats 1 & 3, middle bulb on the clap beats 2 & 4
    clap = ctx.bar_beat % 2 == 1
    out = []
    for i in range(ctx.n):
        c = pal[(ctx.bar + i) % len(pal)]
        mine = (i % 2 == 1) == clap
        out.append(scale(c, ctx.k) if mine else scale(c, 0.25))
    return out


# (id, name, function, limits)
#   min_every = never change faster than this many beats   dip_cap = max punch for this pattern
PATTERNS = [
    ("rotate",   "Rotate",       p_rotate,   {}),
    ("chase",    "Chase",        p_chase,    {}),
    ("drift",    "Step fade",    p_drift,    dict(dip_cap=0.25)),
    ("trade",    "Trade",        p_trade,    {}),
    ("breathe",  "Breathe",      p_breathe,  dict(min_every=2, dip_cap=0.10)),
    ("unison",   "Unison",       p_unison,   {}),
    ("pump",     "Pump",         p_pump,     {}),
    ("kickclap", "Kick & clap",  p_kickclap, {}),
]
PATTERN_IDS = [p[0] for p in PATTERNS]
PALETTE_NAMES = [p[0] for p in PALETTES]

# ---------------------------------------------------------------------
#  LOOKS  —  one-tap combos of movement + colors + punch + speed.
#  speed: "auto" (follows the song) or 1 / 2 / 4 beats per change.
# ---------------------------------------------------------------------
LOOKS = [
    # chill
    dict(name="Sunset step",  group="Chill",  pattern="drift",    palette="Sunset", punch="soft",   speed="auto"),
    dict(name="Deep water",   group="Chill",  pattern="chase",    palette="Ocean",  punch="soft",   speed="auto"),
    dict(name="After hours",  group="Chill",  pattern="breathe",  palette="Ember",  punch="soft",   speed="auto"),
    dict(name="Ice lounge",   group="Chill",  pattern="drift",    palette="Ice",    punch="soft",   speed=2),
    dict(name="Jungle sway",  group="Chill",  pattern="trade",    palette="Jungle", punch="soft",   speed=2),
    # punchy
    dict(name="Warm groove",  group="Punchy", pattern="rotate",   palette="Warm",   punch="medium", speed="auto"),
    dict(name="Neon pocket",  group="Punchy", pattern="trade",    palette="Neon",   punch="medium", speed="auto"),
    dict(name="Kick & clap",  group="Punchy", pattern="kickclap", palette="Miami",  punch="punchy", speed=1),
    dict(name="Citrus pump",  group="Punchy", pattern="pump",     palette="Citrus", punch="punchy", speed="auto"),
    dict(name="Candy hit",    group="Punchy", pattern="unison",   palette="Candy",  punch="punchy", speed=1),
]


# ---------- Govee Bluetooth ----------
def packet(cmd, payload):
    p = bytearray([0x33, cmd] + list(payload))
    p += bytes(19 - len(p))
    chk = 0
    for b in p:
        chk ^= b
    p.append(chk)
    return bytes(p)


def power_pkt(on):
    return packet(0x01, [1 if on else 0])


def color_pkt(r, g, b):
    return packet(0x05, [0x0D if USE_ALT else 0x02, r, g, b])


def keepalive_pkt():
    p = bytearray([0xAA, 0x01]) + bytes(17)
    chk = 0
    for b in p:
        chk ^= b
    p.append(chk)
    return bytes(p)


CONNECT_LOCK = None


class Bulb:
    """One bulb. Commands are sent one at a time and paced so the Bluetooth link isn't flooded.
    If new colors arrive while a command is still sending, only the newest one is sent."""

    def __init__(self, address):
        self.address = address
        self.client = None
        self.device = None
        self.reconnecting = False
        self.last_rgb = None
        self.last_send = 0.0
        self.pending = None
        self.writing = False
        self.drops = 0
        self.closing = False
        self.use_response = RELIABLE_WRITES

    @property
    def ok(self):
        return self.client is not None and self.client.is_connected

    async def _write(self, data):
        """Send one command. Some bulbs refuse confirmed writes - switch those to plain writes."""
        try:
            await self.client.write_gatt_char(CHAR, data, response=self.use_response)
        except Exception as e:
            if self.use_response and "not permitted" in str(e).lower():
                self.use_response = False
                print(f"  {self.address}: doesn't accept confirmed writes - using plain writes for this bulb")
                await self.client.write_gatt_char(CHAR, data, response=False)
            else:
                raise

    def _on_disconnect(self, _client):
        if self.closing:
            return
        self.drops += 1
        print(f"  {self.address}: disconnected at {time.strftime('%H:%M:%S')} (drop #{self.drops})")

    async def _try_connect(self, target):
        from bleak import BleakClient
        self.client = BleakClient(target, timeout=15, disconnected_callback=self._on_disconnect)
        await self.client.connect()
        await asyncio.sleep(0.5)
        if not self.ok:
            return False
        await self._write(power_pkt(True))
        self.last_rgb, self.last_send = None, time.monotonic()
        return True

    async def connect(self, tries=3):
        from bleak import BleakScanner
        async with CONNECT_LOCK:              # one at a time - Windows Bluetooth prefers it
            for attempt in range(1, tries + 1):
                try:
                    # reconnect directly first; scanning while other bulbs are connected can upset them
                    if self.device is not None and await self._try_connect(self.device):
                        print(f"  {self.address}: connected")
                        return True
                    dev = await BleakScanner.find_device_by_address(self.address, timeout=8)
                    if dev is None:
                        print(f"  {self.address}: not found (in use elsewhere or out of range)")
                        await asyncio.sleep(2)
                        continue
                    self.device = dev
                    if await self._try_connect(dev):
                        print(f"  {self.address}: connected")
                        return True
                except Exception as e:
                    print(f"  {self.address}: try {attempt} failed ({e})")
                    await asyncio.sleep(2)
        return False

    async def _reconnect_loop(self):
        self.reconnecting = True
        try:
            while not self.ok:
                try:
                    if self.client:
                        await self.client.disconnect()
                except Exception:
                    pass
                if await self.connect(tries=1):
                    break
                await asyncio.sleep(3)
        finally:
            self.reconnecting = False

    def _need_reconnect(self):
        if not self.reconnecting:
            asyncio.create_task(self._reconnect_loop())

    async def send_rgb(self, rgb):
        if self.last_rgb is not None and self.pending is None:
            if max(abs(a - b) for a, b in zip(rgb, self.last_rgb)) < 3:
                return                         # no visible change
        self.pending = rgb
        if not self.writing:
            asyncio.create_task(self._flush())

    async def _flush(self):
        self.writing = True
        try:
            while self.pending is not None:
                if not self.ok:
                    self.pending = None
                    self._need_reconnect()
                    return
                wait = MIN_GAP_MS / 1000 - (time.monotonic() - self.last_send)
                if wait > 0:
                    await asyncio.sleep(wait)
                rgb, self.pending = self.pending, None
                started = time.monotonic()
                try:
                    await self._write(color_pkt(*rgb))
                    self.last_rgb, self.last_send = rgb, started
                except Exception:
                    self._need_reconnect()
                    return
        finally:
            self.writing = False

    async def keepalive(self):
        """Nudge the bulb when nothing has been sent for a while so it doesn't drop the link."""
        if self.ok and not self.writing and time.monotonic() - self.last_send > 2.0:
            try:
                await self._write(keepalive_pkt())
                self.last_send = time.monotonic()
            except Exception:
                self._need_reconnect()
        elif not self.ok:
            self._need_reconnect()

    async def disconnect(self):
        try:
            if self.client:
                self.closing = True
                await self.client.disconnect()
        except Exception:
            pass


class FakeBulb(Bulb):
    ok = True

    async def connect(self, tries=3):
        return True

    async def send_rgb(self, rgb):
        self.last_rgb = rgb

    async def keepalive(self):
        pass

    async def disconnect(self):
        pass


# ---------- saved settings (timing is remembered between sessions) ----------
import os
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "govee_party_settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(updates):
    data = load_settings()
    data.update(updates)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ---------- the lighting engine ----------
class Ctx:
    pass


def to_section(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return {1: "calm", 2: "groove", 3: "peak", 4: "breakdown", 5: "calm"}.get(int(round(val)))
    s = str(val).lower()
    if "intro" in s or "outro" in s:
        return "calm"
    if "chorus" in s:
        return "peak"
    if "bridge" in s or "down" in s:
        return "breakdown"
    if "verse" in s or "up" in s:
        return "groove"
    return None


SECTION_LABELS = {"calm": "Intro / outro", "groove": "Groove", "peak": "Drop", "breakdown": "Breakdown"}
SPEEDS = ["auto", 1, 2, 4]
LAYOUTS = ["together", "mirror", "alternate"]


class Engine:
    def __init__(self, bulbs):
        self.bulbs = bulbs
        self.pattern = 0
        self.palette = 0
        self.punch = "medium"
        self.layout = START_LAYOUT if START_LAYOUT in LAYOUTS else "alternate"
        self.speed = "auto"
        names = [l["name"].lower() for l in LOOKS]
        self.apply_look(names.index(START_LOOK.lower()) if START_LOOK.lower() in names else 0, kick=False)
        self.master = START_BRIGHTNESS
        self.blackout = False
        self.auto = True
        self.section = "groove"
        self.next_section = None
        self.countin = None
        self.beat = 0
        self.bar = 0
        self.last_beat = 0.0
        self.beat_len = 60 / 124
        self.intervals = []
        self.bpm_msg = None
        self.downbeat = 0.0
        self.last_bar_trigger = 0.0
        self.bar_predicted_at = 0.0
        self.count_bar_beat = 0
        self.last_osc_beat = 0.0
        self.source = "none"
        self.kick = asyncio.Event()
        self.tail = False
        self._tail_handle = None
        self._pred_handle = None
        self._pred_pos = None
        self._pred_time = 0.0
        self.vis = (0, 0, 0)
        self.vis_time = 0.0
        self.offset_ms = int(load_settings().get("offset_ms", LIGHT_OFFSET_MS))
        self._last_status = None

    # ----- controls -----
    def apply_look(self, i, kick=True):
        look = LOOKS[max(0, min(len(LOOKS) - 1, int(i)))]
        self.pattern = PATTERN_IDS.index(look["pattern"])
        self.palette = PALETTE_NAMES.index(look["palette"])
        self.punch = look["punch"]
        self.speed = look["speed"]
        if kick:
            self.blackout = False
            self.kick.set()

    def current_look(self):
        for i, l in enumerate(LOOKS):
            if (PATTERN_IDS.index(l["pattern"]) == self.pattern and PALETTE_NAMES.index(l["palette"]) == self.palette
                    and l["punch"] == self.punch and l["speed"] == self.speed):
                return i
        return -1

    def set_palette(self, i):
        self.palette = int(i) % len(PALETTES)
        self.blackout = False
        self.kick.set()

    def set_pattern(self, i):
        self.pattern = int(i) % len(PATTERNS)
        self.blackout = False
        self.kick.set()

    def set_punch(self, v):
        if v in PUNCH:
            self.punch = v
            self.kick.set()

    def set_layout(self, v):
        if v in LAYOUTS:
            self.layout = v
            self.kick.set()

    def cycle_layout(self):
        self.set_layout(LAYOUTS[(LAYOUTS.index(self.layout) + 1) % len(LAYOUTS)])

    def cycle_punch(self):
        keys = list(PUNCH)
        self.set_punch(keys[(keys.index(self.punch) + 1) % len(keys)])

    def set_speed(self, v):
        v = "auto" if str(v) == "auto" else int(v)
        if v in SPEEDS:
            self.speed = v
            self.kick.set()

    def set_master(self, v):
        self.master = max(0.25, min(1.0, float(v)))
        self.kick.set()

    def nudge_master(self, d):
        self.set_master(round((self.master + d) * 4) / 4)

    def toggle_blackout(self):
        self.blackout = not self.blackout
        self.kick.set()

    def toggle_auto(self):
        self.auto = not self.auto
        self.kick.set()

    # ----- timing input -----
    def bar_triggers_active(self, now):
        return now - self.last_bar_trigger < self.beat_len * 4.5

    def on_beat(self, source, link_bar_beat=None):
        """A real beat arrived from rkbx_link (or Link). Update the clock, then light it."""
        now = time.monotonic()
        dt = now - self.last_beat
        if 0.25 < dt < 1.5:
            self.intervals = (self.intervals + [dt])[-8:]
            self.beat_len = statistics.median(self.intervals)
        if self.bpm_msg and now - self.bpm_msg[1] < 2 and 40 < self.bpm_msg[0] < 250:
            self.beat_len = 60 / self.bpm_msg[0]
        steady = len(self.intervals) >= 3
        self.last_beat = now
        self.beat += 1
        self.source = source

        if link_bar_beat is not None:
            self.count_bar_beat = link_bar_beat
            if link_bar_beat == 0:
                self.bar += 1
        elif self.bar_triggers_active(now):
            if round((now - self.downbeat) / self.beat_len) >= 4:
                self.bar += 1
                self.downbeat = now
                self.bar_predicted_at = now
        else:
            self.count_bar_beat = (self.count_bar_beat + 1) % 4
            if self.count_bar_beat == 0:
                self.bar += 1

        actual = self.position(now)
        loop = asyncio.get_running_loop()
        offset = self.offset_ms / 1000
        if self._pred_handle:
            self._pred_handle.cancel()
            self._pred_handle = None

        # Did we already light this beat early (prediction)? If it matched, don't hit twice.
        already = (self._pred_pos == actual and now - self._pred_time < max(offset, 0) + 0.15)
        self._pred_pos = None
        if not already:
            if offset < 0:
                loop.call_later(-offset, self._hit, actual)       # lights were early: delay them
            else:
                self._hit(actual)

        # Light the NEXT beat slightly early to cancel out the Bluetooth delay.
        if offset > 0 and steady:
            nxt = self.advance(actual)
            self._pred_handle = loop.call_later(max(0.0, self.beat_len - offset), self._predicted_hit, nxt)

    def position(self, now):
        if self.bar_triggers_active(now):
            bb = max(0, round((self.last_beat - self.downbeat) / self.beat_len)) % 4
        else:
            bb = self.count_bar_beat
        return (self.beat, self.bar, bb)

    @staticmethod
    def advance(pos):
        beat, bar, bb = pos
        bb = (bb + 1) % 4
        return (beat + 1, bar + (1 if bb == 0 else 0), bb)

    def _predicted_hit(self, pos):
        self._pred_handle = None
        self._pred_pos, self._pred_time = pos, time.monotonic()
        self._hit(pos)

    def _hit(self, pos):
        """Show a beat: full brightness now, punch drop a moment later."""
        self.vis = pos
        self.vis_time = time.monotonic()
        self.tail = False
        if self._tail_handle:
            self._tail_handle.cancel()
        delay = self.beat_len * PUNCH[self.punch]["tail"]
        self._tail_handle = asyncio.get_running_loop().call_later(delay, self._on_tail)
        self.kick.set()

    def _on_tail(self):
        self.tail = True
        self.kick.set()

    def set_offset(self, ms):
        self.offset_ms = max(-150, min(200, int(round(float(ms)))))
        save_settings(dict(offset_ms=self.offset_ms))
        self.kick.set()

    def nudge_offset(self, d):
        self.set_offset(self.offset_ms + d)

    def on_bar(self):
        now = time.monotonic()
        if now - self.bar_predicted_at > 0.25:
            self.bar += 1
        self.downbeat = now
        self.last_bar_trigger = now
        self.count_bar_beat = 0

    def on_osc(self, address, *args):
        parts = [p for p in address.lower().split("/") if p]
        if "master" not in parts:
            return
        val = args[0] if args else None
        try:
            if "trigger" in parts:
                if isinstance(val, (int, float)) and val <= 0:
                    return
                interval = float(parts[-1])
                if abs(interval - 1) < 1e-6:
                    self.last_osc_beat = time.monotonic()
                    self.on_beat("rkbx_link")
                elif abs(interval - 4) < 1e-6:
                    self.on_bar()
            elif "phrase" in parts:
                if "current" in parts:
                    sec = to_section(val)
                    if sec:
                        self.section = sec
                elif "next" in parts:
                    self.next_section = to_section(val)
                elif "countin" in parts:
                    self.countin = float(val)
            elif "bpm" in parts and "current" in parts:
                self.bpm_msg = (float(val), time.monotonic())
        except (TypeError, ValueError):
            pass

    # ----- rendering -----
    def idle(self):
        return time.monotonic() - self.last_beat > 2.5

    def section_params(self):
        base = dict(SECTIONS[self.section] if self.auto else SECTIONS["groove"])
        building = False
        if (self.auto and self.next_section == "peak" and self.section != "peak"
                and self.countin is not None and 0 <= self.countin <= 16):
            t = 1 - self.countin / 16
            peak = SECTIONS["peak"]
            base["level"] += (peak["level"] - base["level"]) * t
            base["energy"] += (peak["energy"] - base["energy"]) * t
            if t > 0.5:
                base["every"] = 1
            building = True
        if self.speed != "auto":
            base["every"] = self.speed
        return base, building

    def section_label(self):
        if self.blackout:
            return "Blackout"
        if self.idle():
            return "Waiting for music"
        _, building = self.section_params()
        if building:
            return "Building"
        return SECTION_LABELS[self.section] if self.auto else "Groove"

    def frame(self):
        now = time.monotonic()
        sec, _ = self.section_params()
        ctx = Ctx()
        n = len(self.bulbs)
        # layout: work out how many "different" bulbs the pattern should draw
        if self.layout == "together":
            ctx.n = 1
        elif self.layout == "mirror":
            ctx.n = (n + 1) // 2
        else:
            ctx.n = n
        vbeat, vbar, vbb = self.vis
        ctx.bar = vbar
        limits = PATTERNS[self.pattern][3]
        every = max(int(sec["every"]), limits.get("min_every", 1))
        ctx.beat = vbeat
        ctx.step = vbeat // max(1, every)
        ctx.tail = self.tail and not self.idle()
        dip = min(MAX_DIP, limits.get("dip_cap", 1.0), PUNCH[self.punch]["dip"] * sec["energy"])
        ctx.k = 1.0 - dip if ctx.tail else 1.0
        ctx.bar_beat = vbb
        drawn = PATTERNS[self.pattern][2](ctx, PALETTES[self.palette][1])
        if self.layout == "together":
            colors = [drawn[0]] * n
        elif self.layout == "mirror":
            colors = [drawn[min(i, n - 1 - i)] for i in range(n)]
        else:
            colors = drawn
        k = 0.0 if self.blackout else sec["level"] * self.master
        return [tuple(max(0, min(255, int(v * k))) for v in c) for c in colors]

    async def render_loop(self):
        # Sends only on the beat, the punch drop, or when you press a control.
        while True:
            try:
                await asyncio.wait_for(self.kick.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                await asyncio.gather(*(b.keepalive() for b in self.bulbs))
            self.kick.clear()
            cols = self.frame()
            await asyncio.gather(*(b.send_rgb(c) for b, c in zip(self.bulbs, cols)))
            self.print_status()

    def look_label(self):
        i = self.current_look()
        if i >= 0:
            return LOOKS[i]["name"]
        return f"Custom: {PATTERNS[self.pattern][1]} + {PALETTES[self.palette][0]}"

    def print_status(self):
        status = (self.look_label(), self.punch, self.section_label(),
                  round(60 / self.beat_len) if not self.idle() else None,
                  sum(b.ok for b in self.bulbs))
        if status != self._last_status:
            self._last_status = status
            bpm = f"{status[3]} BPM" if status[3] else "-"
            print(f"  [{status[0]} / {status[1]}]  {status[2]}  |  {bpm}  |  bulbs {status[4]}/{len(self.bulbs)}")

    def state(self):
        hexs = lambda cols: ["#%02x%02x%02x" % c for c in cols]
        return dict(
            looks=[dict(name=l["name"], group=l["group"],
                        colors=hexs(PALETTES[PALETTE_NAMES.index(l["palette"])][1])) for l in LOOKS],
            palettes=[dict(name=n, colors=hexs(c)) for n, c in PALETTES],
            patterns=[p[1] for p in PATTERNS],
            pattern_note=("Breathe always moves slowly and softly, whatever the speed and punch settings."
                          if PATTERNS[self.pattern][0] == "breathe" else
                          "Step fade keeps the punch gentle so the fades stay smooth."
                          if PATTERNS[self.pattern][0] == "drift" else ""),
            punches=list(PUNCH), layouts=LAYOUTS, layout=self.layout, offset_ms=self.offset_ms,
            look=self.current_look(), look_label=self.look_label(),
            palette=self.palette, pattern=self.pattern, punch=self.punch, speed=str(self.speed),
            master=self.master, blackout=self.blackout, auto=self.auto,
            section=self.section_label(),
            bpm=None if self.idle() else round(60 / self.beat_len, 1),
            bulbs=[bool(b.ok) for b in self.bulbs],
        )


# ---------- built-in OSC receiver (no extra install needed) ----------
def _osc_str(d, i):
    end = d.index(b"\0", i)
    return d[i:end].decode(errors="ignore"), (end + 4) & ~3


def osc_parse(d):
    """Decode an OSC packet (message or bundle) into [(address, [args])]."""
    if d.startswith(b"#bundle\0"):
        out, i = [], 16
        while i + 4 <= len(d):
            size = int.from_bytes(d[i:i + 4], "big")
            i += 4
            out += osc_parse(d[i:i + size])
            i += size
        return out
    addr, i = _osc_str(d, 0)
    if i >= len(d):
        return [(addr, [])]
    tags, i = _osc_str(d, i)
    args = []
    for t in tags[1:]:
        if t == "f":
            args.append(struct.unpack(">f", d[i:i + 4])[0]); i += 4
        elif t == "i":
            args.append(struct.unpack(">i", d[i:i + 4])[0]); i += 4
        elif t == "d":
            args.append(struct.unpack(">d", d[i:i + 8])[0]); i += 8
        elif t == "h":
            args.append(struct.unpack(">q", d[i:i + 8])[0]); i += 8
        elif t == "s":
            s, i = _osc_str(d, i); args.append(s)
        elif t in "TF":
            args.append(t == "T")
        elif t == "N":
            args.append(None)
        else:
            break
    return [(addr, args)]


class OSCReceiver(asyncio.DatagramProtocol):
    def __init__(self, engine):
        self.engine = engine

    def datagram_received(self, data, addr):
        try:
            for address, args in osc_parse(data):
                self.engine.on_osc(address, *args)
        except Exception:
            pass


# ---------- Ableton Link backup (used only if OSC beats stop arriving) ----------
async def link_backup(engine):
    try:
        from aalink import Link
    except ImportError:
        return
    link = Link(120)
    link.enabled = True
    while True:
        await link.sync(1)
        if getattr(link, "num_peers", 0) == 0:
            continue
        if time.monotonic() - engine.last_osc_beat < 3:
            continue
        engine.bpm_msg = (link.tempo, time.monotonic())
        engine.on_beat("Ableton Link", link_bar_beat=int(round(link.beat)) % 4)


# ---------- phone remote (tiny web server) ----------
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1a1320">
<title>Lights</title>
<style>
:root{--bg:#1a1320;--surface:#251c2e;--line:#3d3048;--text:#f5ebf7;--muted:#ab98b5;--ok:#86e3a8;
--font:"Avenir Next","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font)}
body{max-width:560px;margin:0 auto;padding:calc(env(safe-area-inset-top,0px) + 14px) 16px calc(env(safe-area-inset-bottom,0px) + 28px)}
header{position:sticky;top:calc(env(safe-area-inset-top,0px) + 6px);z-index:5;overflow:hidden;border-radius:24px;background:var(--surface);padding:16px 18px 14px;box-shadow:0 10px 30px rgba(10,5,14,.55)}
header::before{content:"";position:absolute;inset:-60% -20% 20% -20%;background:var(--glow,#3d3048);filter:blur(42px);opacity:.45}
.now{position:relative;font-size:2.1rem;font-weight:700;letter-spacing:-.025em;line-height:1.05}
.look{position:relative;margin-top:4px;font-weight:600}
.meta{position:relative;display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;margin-top:8px;color:var(--muted);font-size:.92rem}
.dots{display:inline-flex;gap:6px;align-items:center}
.dot{width:10px;height:10px;border-radius:50%;background:var(--line)}
.dot.on{background:var(--ok)}
h2{font-size:1rem;font-weight:600;color:var(--muted);margin:24px 4px 10px}
h3{font-size:.9rem;font-weight:600;color:var(--muted);margin:14px 4px 8px}
.looks{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.lk{position:relative;border:0;border-radius:18px;min-height:86px;padding:12px;text-align:left;color:#fff;font:inherit;font-weight:700;font-size:1.02rem;
background:var(--g);display:flex;align-items:flex-end;outline:3px solid transparent;outline-offset:3px;cursor:pointer}
.lk:last-child:nth-child(odd){grid-column:span 2;min-height:70px}
.lk::after{content:"";position:absolute;inset:0;border-radius:inherit;background:linear-gradient(to top,rgba(22,12,28,.78),rgba(22,12,28,0) 70%)}
.lk span{position:relative;z-index:1}
.lk.active{outline-color:var(--text)}
.chips{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.chip{border:0;background:none;color:var(--muted);font:inherit;font-size:.78rem;padding:0;cursor:pointer;text-align:center}
.chip i{display:block;height:44px;border-radius:14px;background:var(--g);margin-bottom:5px;outline:3px solid transparent;outline-offset:2px}
.chip.active{color:var(--text);font-weight:600}
.chip.active i{outline-color:var(--text)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.opt{border:1.5px solid var(--line);border-radius:14px;background:transparent;color:var(--text);font:inherit;font-weight:600;padding:13px 12px;text-align:left;cursor:pointer}
.opt.active{background:var(--line);border-color:var(--text)}
.seg{display:grid;gap:4px;padding:4px;border-radius:16px;background:var(--surface)}
.seg button{border:0;border-radius:12px;padding:14px 0;background:transparent;color:var(--muted);font:inherit;font-weight:600;cursor:pointer}
.seg button.active{background:var(--line);color:var(--text)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:24px}
.toggle{border:1.5px solid var(--line);border-radius:16px;background:transparent;color:var(--text);font:inherit;font-weight:600;padding:14px;text-align:left;cursor:pointer}
.toggle small{display:block;font-weight:400;color:var(--muted);font-size:.82rem;margin-top:3px}
.toggle.on{background:var(--surface);border-color:var(--text)}
.toggle.black.on{background:#000;border-color:#ff6b81}
button:focus-visible{outline:3px solid #fff !important;outline-offset:3px}
.timing{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;background:var(--surface);border-radius:16px;padding:6px}
.timing button{border:0;border-radius:12px;background:var(--line);color:var(--text);font:inherit;font-weight:600;padding:14px 16px;cursor:pointer}
.timing div{text-align:center}
.timing strong{display:block;font-size:1.15rem}
.timing small{display:block;color:var(--muted);font-size:.78rem;margin-top:2px}
.note{margin:10px 4px 0;color:var(--muted);font-size:.88rem;line-height:1.4}
.err{margin:20px 4px 0;color:#ffb8ab;font-size:.95rem;line-height:1.45}
@media (prefers-reduced-motion:no-preference){.lk,.chip i,.opt,.seg button,.toggle{transition:outline-color .15s,background-color .15s}}
</style></head>
<body>
<header>
  <div class="now" id="now">Connecting</div>
  <div class="look" id="look"></div>
  <div class="meta"><span id="bpm">No beat yet</span><span class="dots" id="dots" aria-label="Bulbs connected"></span></div>
</header>

<h2>Looks</h2>
<h3>Chill</h3><div class="looks" id="chill"></div>
<h3>Punchy</h3><div class="looks" id="punchy"></div>

<h2>Colors</h2><div class="chips" id="palettes"></div>
<h2>Movement</h2><div class="grid2" id="patterns"></div><p class="note" id="pnote" hidden></p>
<h2>Bulbs</h2><div class="seg" id="layout" style="grid-template-columns:repeat(3,1fr)"></div>
<h2>Punch</h2><div class="seg" id="punch" style="grid-template-columns:repeat(3,1fr)"></div>
<h2>Color changes</h2><div class="seg" id="speed" style="grid-template-columns:repeat(4,1fr)"></div>
<h2>Timing</h2>
<div class="timing">
  <button id="tMinus" aria-label="Lights later">Later</button>
  <div><strong id="tVal"></strong><small>Tap Earlier if the lights feel behind the kick</small></div>
  <button id="tPlus" aria-label="Lights earlier">Earlier</button>
</div>
<h2>Brightness</h2><div class="seg" id="bright" style="grid-template-columns:repeat(4,1fr)"></div>

<div class="row">
  <button class="toggle" id="auto">Follow song sections<small id="autoS"></small></button>
  <button class="toggle black" id="black">Blackout<small id="blackS"></small></button>
</div>
<p class="err" id="err" hidden>Can't reach the laptop. Make sure the lights script is running and this phone is on the same Wi-Fi.</p>

<script>
const $ = id => document.getElementById(id);
const LEVELS = [0.25, 0.5, 0.75, 1];
const SPEEDS = [['auto','Auto'],['1','Beat'],['2','2 beats'],['4','Bar']];
const LAYOUT_LABELS = {together:'Together', mirror:'Mirror', alternate:'Alternate'};
const PUNCH_LABELS = {soft:'Soft', medium:'Medium', punchy:'Punchy'};
let S = null, built = false;
const grad = c => `linear-gradient(135deg, ${c.join(', ')})`;

async function call(url){
  try{
    const r = await fetch(url, {cache:'no-store'});
    S = await r.json(); $('err').hidden = true; render();
  }catch(e){ $('err').hidden = false; }
}
function btn(parent, cls, html, onclick){
  const b = document.createElement('button');
  b.className = cls; b.innerHTML = html; b.onclick = onclick; parent.appendChild(b); return b;
}
function build(){
  S.looks.forEach((l, i) => {
    const b = btn($(l.group === 'Chill' ? 'chill' : 'punchy'), 'lk', '<span></span>', () => call('/api/look?i=' + i));
    b.style.setProperty('--g', grad(l.colors));
    b.querySelector('span').textContent = l.name;
    b.dataset.i = i;
  });
  S.palettes.forEach((p, i) => {
    const b = btn($('palettes'), 'chip', '<i></i><span></span>', () => call('/api/palette?i=' + i));
    b.querySelector('i').style.setProperty('--g', grad(p.colors));
    b.querySelector('span').textContent = p.name;
  });
  S.patterns.forEach((name, i) => { btn($('patterns'), 'opt', '', () => call('/api/pattern?i=' + i)).textContent = name; });
  S.layouts.forEach(v => { btn($('layout'), '', '', () => call('/api/layout?v=' + v)).textContent = LAYOUT_LABELS[v] || v; });
  S.punches.forEach(v => { btn($('punch'), '', '', () => call('/api/punch?v=' + v)).textContent = PUNCH_LABELS[v] || v; });
  SPEEDS.forEach(([v, label]) => { btn($('speed'), '', '', () => call('/api/speed?v=' + v)).textContent = label; });
  LEVELS.forEach(v => { btn($('bright'), '', '', () => call('/api/bright?v=' + v)).textContent = Math.round(v * 100) + '%'; });
  $('tMinus').onclick = () => call('/api/offset?d=-10');
  $('tPlus').onclick = () => call('/api/offset?d=10');
  $('auto').onclick = () => call('/api/auto');
  $('black').onclick = () => call('/api/blackout');
  built = true;
}
function mark(parent, test){
  [...parent.children].forEach((b, i) => { const on = test(b, i); b.classList.toggle('active', on); b.setAttribute('aria-pressed', on); });
}
function render(){
  if(!built) build();
  [$('chill'), $('punchy')].forEach(box => mark(box, b => +b.dataset.i === S.look));
  mark($('palettes'), (b, i) => i === S.palette);
  mark($('patterns'), (b, i) => i === S.pattern);
  $('pnote').hidden = !S.pattern_note; $('pnote').textContent = S.pattern_note || '';
  mark($('layout'), (b, i) => S.layouts[i] === S.layout);
  mark($('punch'), (b, i) => S.punches[i] === S.punch);
  mark($('speed'), (b, i) => SPEEDS[i][0] === S.speed);
  mark($('bright'), (b, i) => Math.abs(LEVELS[i] - S.master) < 0.01);
  document.documentElement.style.setProperty('--glow', `linear-gradient(90deg, ${S.palettes[S.palette].colors.join(', ')})`);
  $('now').textContent = S.section;
  $('tVal').textContent = S.offset_ms === 0 ? 'On the beat' : (S.offset_ms > 0 ? `${S.offset_ms} ms early` : `${-S.offset_ms} ms late`);
  $('look').textContent = S.look_label;
  $('bpm').textContent = S.bpm ? `${Math.round(S.bpm)} BPM` : 'No beat yet';
  $('dots').innerHTML = S.bulbs.map(on => `<span class="dot${on ? ' on' : ''}"></span>`).join('');
  $('auto').classList.toggle('on', S.auto);
  $('autoS').textContent = S.auto ? 'On: harder on drops, softer in breakdowns' : 'Off: same energy all song';
  $('black').classList.toggle('on', S.blackout);
  $('blackS').textContent = S.blackout ? 'Lights off. Tap to bring them back' : 'Turn all bulbs off';
}
call('/api/state');
setInterval(() => call('/api/state'), 1000);
</script>
</body></html>
"""


def make_http_handler(engine):
    routes = {
        "/api/look":     lambda q: engine.apply_look(q["i"][0]),
        "/api/palette":  lambda q: engine.set_palette(q["i"][0]),
        "/api/pattern":  lambda q: engine.set_pattern(q["i"][0]),
        "/api/punch":    lambda q: engine.set_punch(q["v"][0]),
        "/api/layout":   lambda q: engine.set_layout(q["v"][0]),
        "/api/offset":   lambda q: engine.nudge_offset(int(q["d"][0])),
        "/api/speed":    lambda q: engine.set_speed(q["v"][0]),
        "/api/bright":   lambda q: engine.set_master(q["v"][0]),
        "/api/blackout": lambda q: engine.toggle_blackout(),
        "/api/auto":     lambda q: engine.toggle_auto(),
        "/api/state":    lambda q: None,
    }

    async def handle(reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            line = data.split(b"\r\n", 1)[0].decode(errors="ignore").split(" ")
            url = urlparse(line[1] if len(line) > 1 else "/")
            q = parse_qs(url.query)
            status, ctype = "200 OK", "application/json"
            if url.path == "/":
                ctype, body = "text/html; charset=utf-8", PAGE.encode()
            elif url.path in routes:
                try:
                    routes[url.path](q)
                except (KeyError, ValueError, IndexError):
                    pass
                body = json.dumps(engine.state()).encode()
            else:
                status, ctype, body = "404 Not Found", "text/plain", b"Not found"
            head = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
            writer.write(head.encode() + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    return handle


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def setup_hotkeys(engine, loop):
    try:
        import keyboard
    except ImportError:
        print("  (hotkeys off - run: pip install keyboard)")
        return

    def on(fn, *a):
        return lambda: loop.call_soon_threadsafe(fn, *a)
    try:
        for i in range(min(len(LOOKS), 10)):
            keyboard.add_hotkey(f"ctrl+alt+{(i + 1) % 10}", on(engine.apply_look, i))
        keyboard.add_hotkey("ctrl+alt+c", on(lambda: engine.set_palette(engine.palette + 1)))
        keyboard.add_hotkey("ctrl+alt+m", on(lambda: engine.set_pattern(engine.pattern + 1)))
        keyboard.add_hotkey("ctrl+alt+k", on(engine.cycle_punch))
        keyboard.add_hotkey("ctrl+alt+l", on(engine.cycle_layout))
        keyboard.add_hotkey("ctrl+alt+]", on(engine.nudge_offset, 10))
        keyboard.add_hotkey("ctrl+alt+[", on(engine.nudge_offset, -10))
        keyboard.add_hotkey("ctrl+alt+b", on(engine.toggle_blackout))
        keyboard.add_hotkey("ctrl+alt+a", on(engine.toggle_auto))
        keyboard.add_hotkey("ctrl+alt+up", on(engine.nudge_master, 0.25))
        keyboard.add_hotkey("ctrl+alt+down", on(engine.nudge_master, -0.25))
        print("  Hotkeys: Ctrl+Alt+1-0 looks | C colors | M movement | K punch | L layout | [ ] timing | "
              "B blackout | A sections | Up/Down brightness")
    except Exception as e:
        print(f"  (hotkeys unavailable: {e})")


# ---------- main modes ----------
async def run(demo=False):
    global CONNECT_LOCK
    CONNECT_LOCK = asyncio.Lock()
    loop = asyncio.get_running_loop()
    bulbs = [(FakeBulb if demo else Bulb)(a) for a in BULBS]
    engine = Engine(bulbs)

    osc_transport, _ = await loop.create_datagram_endpoint(
        lambda: OSCReceiver(engine), local_addr=("127.0.0.1", OSC_PORT))
    http = await asyncio.start_server(make_http_handler(engine), "0.0.0.0", REMOTE_PORT)

    print("=" * 60)
    print(f"  Phone remote:  http://{lan_ip()}:{REMOTE_PORT}")
    print(f"  (on this laptop: http://localhost:{REMOTE_PORT})")
    print(f"  Listening for rkbx_link on port {OSC_PORT}")
    setup_hotkeys(engine, loop)
    print("=" * 60)

    if not demo:
        print("Connecting bulbs...")
        for b in bulbs:
            await b.connect()
    print("Running. Press Ctrl+C to stop.")
    try:
        await asyncio.gather(engine.render_loop(), link_backup(engine))
    finally:
        osc_transport.close()
        http.close()
        for b in bulbs:
            await b.disconnect()
        print("Bulbs released.")


async def scan():
    from bleak import BleakScanner
    print("Scanning for 10 seconds... (close the Govee app first)")
    devices = await BleakScanner.discover(timeout=10)
    found = [d for d in devices if d.name and any(k in d.name.lower() for k in ("govee", "ihoment", "minger", "h6"))]
    for d in found:
        print(f"FOUND:  {d.address}   ({d.name})")
    if not found:
        print("No Govee bulbs found. Close the Govee app, turn off phone Bluetooth, and try again.")


async def test():
    global CONNECT_LOCK
    CONNECT_LOCK = asyncio.Lock()
    bulbs = [Bulb(a) for a in BULBS]
    for b in bulbs:
        await b.connect()
    try:
        for name, rgb in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]:
            print(f"Sending {name}")
            for b in bulbs:
                b.last_rgb = None
            await asyncio.gather(*(b.send_rgb(rgb) for b in bulbs))
            await asyncio.sleep(1.5)
    finally:
        for b in bulbs:
            await b.disconnect()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = args[0] if args else "run"
    try:
        if mode == "scan":
            asyncio.run(scan())
        elif mode == "test":
            asyncio.run(test())
        elif mode == "demo":
            asyncio.run(run(demo=True))
        elif mode == "run":
            asyncio.run(run())
        else:
            print(__doc__)
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        print(f"\nCouldn't start: {e}\nIs another copy of the lights script already running?")


if __name__ == "__main__":
    main()

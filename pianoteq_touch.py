#!/usr/bin/env python3
"""
pianoteq_touch.py - big-button touchscreen remote for a headless Pianoteq.

Designed for a Raspberry Pi with a 480x320 3.5" SPI LCD (e.g. MHS-3.5")
and Pianoteq running with its JSON-RPC server enabled:

    "Pianoteq 9" --headless --serve ""        # listens on 127.0.0.1:8081

Controls (same JSON-RPC calls as github.com/shuhaowu/remote-for-pianoteq):
  * main volume (-/+ buttons, or tap the bar to jump)
  * metronome on/off, tempo, time signature, click volume, accent

Two display back-ends:
  * "fb"     : draws straight to the Linux framebuffer (/dev/fbN) and reads
               the touch panel with evdev.  No X/desktop needed.
  * "window" : an ordinary pygame window (use under X/Wayland, or on a
               desktop machine to try out the layout).
  * "auto"   : (default) "window" if $DISPLAY/$WAYLAND_DISPLAY is set,
               otherwise "fb".

First run on the Pi console:   python3 pianoteq_touch.py --calibrate
"""

import argparse
import glob
import json
import os
import queue
import sys
import threading
import time
import urllib.request

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

W, H = 480, 320
CONFIG_PATH = os.path.expanduser("~/.config/pianoteq-touch.json")

# Option lists copied from remote-for-pianoteq (which mirrors Pianoteq's menus).
BPM_STEPS = (list(range(40, 60, 4)) + list(range(60, 72, 3)) +
             list(range(72, 120, 4)) + list(range(120, 144, 6)) +
             list(range(144, 209, 8)))
TIMESIGS = ["1/1", "1/2", "2/4", "3/4", "4/4", "5/4", "3/8", "6/8", "7/8", "9/8"]
CLICK_DB = [-96, -35, -30, -25, -20, -15, -10, -7, -5, -3, -2, -1,
            0, 1, 2, 3, 5, 7, 10, 15, 20]
VOLUME_STEP = 0.05

# Colours
BG = (18, 18, 24)
PANEL = (44, 46, 58)
PANEL_DOWN = (90, 94, 120)
TEXT = (235, 235, 240)
DIM = (140, 140, 155)
ACCENT = (70, 150, 255)
GREEN = (40, 170, 80)
RED = (200, 60, 60)


# --------------------------------------------------------------------------
# Pianoteq JSON-RPC client + background worker
# --------------------------------------------------------------------------

class Pianoteq:
    def __init__(self, host, port, timeout=2.0):
        self.url = "http://{}:{}/jsonrpc".format(host, port)
        self.timeout = timeout

    def rpc(self, method, params=None):
        payload = {"jsonrpc": "2.0", "id": 0, "method": method,
                   "params": params if params is not None else []}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        if "error" in data and data["error"]:
            raise RuntimeError("{}: {}".format(method, data["error"]))
        return data.get("result")

    def get_volume(self):
        for p in self.rpc("getParameters"):
            if p.get("id") == "volume":
                return float(p["normalized_value"])
        return None

    def set_volume(self, level):
        self.rpc("setParameters", {"list": [
            {"id": "volume", "name": "Volume",
             "normalized_value": float(level)}]})

    def get_metronome(self):
        m = self.rpc("getMetronome")
        return m[0] if isinstance(m, list) else m

    def set_metronome(self, **kw):
        # Accepts any subset of: enabled, bpm, timesig, volume_db, accentuate
        self.rpc("setMetronome", kw)

    def get_preset_name(self):
        info = self.rpc("getInfo")
        if isinstance(info, list):
            info = info[0]
        return info.get("current_preset", {}).get("name", "")


class State:
    """Shared UI state.  The UI thread edits it optimistically; the worker
    pushes edits to Pianoteq and refreshes from Pianoteq when idle."""

    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.preset = ""
        self.volume = 0.5
        self.met = {"enabled": False, "bpm": 120, "timesig": "4/4",
                    "volume_db": 0, "accentuate": True}
        self.last_local_change = 0.0
        self.version = 0  # bumps whenever something changes -> redraw


class Worker(threading.Thread):
    POLL_INTERVAL = 2.0
    SETTLE = 1.5   # ignore polled values this long after a local edit

    def __init__(self, pt, state):
        super().__init__(daemon=True)
        self.pt, self.state = pt, state
        self.q = queue.Queue()

    def send(self, kind, **kw):
        with self.state.lock:
            self.state.last_local_change = time.time()
        self.q.put((kind, kw))

    def _drain(self, first):
        """Collapse a burst of queued commands to the latest per setting."""
        pending = {}
        items = [first]
        while True:
            try:
                items.append(self.q.get_nowait())
            except queue.Empty:
                break
        for kind, kw in items:
            if kind == "metronome":
                pending.setdefault("metronome", {}).update(kw)
            else:
                pending[kind] = kw
        return pending

    def run(self):
        next_poll = 0.0
        while True:
            try:
                item = self.q.get(timeout=max(0.05, next_poll - time.time()))
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    for kind, kw in self._drain(item).items():
                        if kind == "volume":
                            self.pt.set_volume(kw["level"])
                        elif kind == "metronome":
                            self.pt.set_metronome(**kw)
                    self._set_connected(True)
                    next_poll = time.time() + self.SETTLE
                elif time.time() >= next_poll:
                    self._poll()
                    next_poll = time.time() + self.POLL_INTERVAL
            except Exception as e:  # network error, Pianoteq not up yet, ...
                if self.state.connected:
                    print("Pianoteq connection problem:", e, file=sys.stderr)
                self._set_connected(False)
                next_poll = time.time() + self.POLL_INTERVAL

    def _set_connected(self, ok):
        with self.state.lock:
            if self.state.connected != ok:
                self.state.connected = ok
                self.state.version += 1

    def _poll(self):
        vol = self.pt.get_volume()
        met = self.pt.get_metronome()
        preset = self.pt.get_preset_name()
        s = self.state
        with s.lock:
            s.connected = True
            if time.time() - s.last_local_change > self.SETTLE:
                if vol is not None:
                    s.volume = vol
                for k in s.met:
                    if k in met:
                        s.met[k] = met[k]
            s.preset = preset
            s.version += 1


# --------------------------------------------------------------------------
# Display back-ends
# --------------------------------------------------------------------------

class WindowBackend:
    """Regular pygame window; mouse/touch arrive as mouse events."""

    def __init__(self, fullscreen):
        pygame.display.init()
        flags = pygame.FULLSCREEN if fullscreen else 0
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("Pianoteq Touch")
        if fullscreen:
            pygame.mouse.set_visible(False)
        self.surface = pygame.Surface((W, H))

    def flip(self):
        self.screen.blit(self.surface, (0, 0))
        pygame.display.flip()

    def events(self):
        out = []
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                out.append(("quit",))
            elif ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q):
                out.append(("quit",))
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                out.append(("down", ev.pos))
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                out.append(("up", ev.pos))
            elif ev.type == pygame.MOUSEMOTION and ev.buttons[0]:
                out.append(("move", ev.pos))
        return out

    def close(self):
        pygame.display.quit()


def find_framebuffer():
    """Pick the framebuffer whose size is 480x320 / 320x480 (the SPI LCD)."""
    for path in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            with open(os.path.join(path, "virtual_size")) as f:
                w, h = (int(v) for v in f.read().strip().split(","))
        except OSError:
            continue
        if (w, h) in ((W, H), (H, W)):
            return "/dev/" + os.path.basename(path)
    return "/dev/fb1" if os.path.exists("/dev/fb1") else "/dev/fb0"


def find_touch_device():
    import evdev
    from evdev import ecodes
    candidates = []
    for p in evdev.list_devices():
        d = evdev.InputDevice(p)
        caps = d.capabilities()
        abs_codes = [c[0] if isinstance(c, tuple) else c
                     for c in caps.get(ecodes.EV_ABS, [])]
        if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes:
            score = 0
            name = d.name.lower()
            if "ads7846" in name or "xpt2046" in name:
                score = 2
            elif "touch" in name:
                score = 1
            candidates.append((score, d))
        else:
            d.close()
    if not candidates:
        raise RuntimeError("No touchscreen input device found (is the ads7846 overlay loaded?)")
    candidates.sort(key=lambda t: -t[0])
    for _, d in candidates[1:]:
        d.close()
    return candidates[0][1]


class FramebufferBackend:
    """Render offscreen with pygame, blit to /dev/fbN, read touch via evdev."""

    def __init__(self, fbdev, touchdev, rotate, calib):
        import numpy as np
        from evdev import ecodes
        self.np, self.ec = np, ecodes
        self.surface = pygame.Surface((W, H))
        self.rotate = rotate % 360

        self.fbdev = fbdev or find_framebuffer()
        name = os.path.basename(self.fbdev)
        sysfs = "/sys/class/graphics/" + name

        def rd(attr):
            with open(os.path.join(sysfs, attr)) as f:
                return f.read().strip()
        self.fb_w, self.fb_h = (int(v) for v in rd("virtual_size").split(","))
        self.bpp = int(rd("bits_per_pixel"))
        try:
            self.stride = int(rd("stride"))
        except OSError:
            self.stride = self.fb_w * self.bpp // 8
        self.fb = open(self.fbdev, "r+b", buffering=0)
        print("Framebuffer {}: {}x{} {}bpp".format(self.fbdev, self.fb_w,
              self.fb_h, self.bpp), file=sys.stderr)
        hide_console_cursor()

        import evdev
        self.touch = evdev.InputDevice(touchdev) if touchdev else find_touch_device()
        print("Touch device {}: {}".format(self.touch.path, self.touch.name), file=sys.stderr)
        os.set_blocking(self.touch.fd, False)
        self.calib = calib
        self.raw_x = self.raw_y = None
        self.touching = False
        self.was_touching = False

    # -- display --------------------------------------------------------
    def flip(self):
        np = self.np
        surf = self.surface
        if self.rotate:
            surf = pygame.transform.rotate(surf, -self.rotate)
        px = pygame.surfarray.array3d(surf).swapaxes(0, 1)  # (h, w, 3)
        h, w = px.shape[:2]
        if (w, h) != (self.fb_w, self.fb_h):
            # Size mismatch: centre/crop into the framebuffer.
            canvas = np.zeros((self.fb_h, self.fb_w, 3), np.uint8)
            ch, cw = min(h, self.fb_h), min(w, self.fb_w)
            canvas[:ch, :cw] = px[:ch, :cw]
            px = canvas
        if self.bpp == 16:
            r = px[..., 0].astype(np.uint16)
            g = px[..., 1].astype(np.uint16)
            b = px[..., 2].astype(np.uint16)
            out = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out = out.astype("<u2")
        elif self.bpp == 32:
            out = np.zeros((self.fb_h, self.fb_w, 4), np.uint8)
            out[..., 0] = px[..., 2]
            out[..., 1] = px[..., 1]
            out[..., 2] = px[..., 0]
            out[..., 3] = 255
        elif self.bpp == 24:
            out = px[..., ::-1].copy()
        else:
            raise RuntimeError("Unsupported framebuffer depth %d" % self.bpp)
        rows = np.ascontiguousarray(out).reshape(self.fb_h, -1).view(np.uint8)
        row_bytes = rows.shape[1]
        self.fb.seek(0)
        if row_bytes == self.stride:
            self.fb.write(rows.tobytes())
        else:
            for y in range(self.fb_h):
                self.fb.seek(y * self.stride)
                self.fb.write(rows[y].tobytes())

    # -- touch ----------------------------------------------------------
    def raw_events(self):
        """Yield ("down"|"move"|"up", raw_x, raw_y) from the touch device."""
        ec = self.ec
        out = []
        try:
            for ev in self.touch.read():
                if ev.type == ec.EV_ABS:
                    if ev.code == ec.ABS_X:
                        self.raw_x = ev.value
                    elif ev.code == ec.ABS_Y:
                        self.raw_y = ev.value
                    elif ev.code == ec.ABS_PRESSURE:
                        self.touching = ev.value > 0
                elif ev.type == ec.EV_KEY and ev.code == ec.BTN_TOUCH:
                    self.touching = bool(ev.value)
                elif ev.type == ec.EV_SYN and ev.code == ec.SYN_REPORT:
                    if self.raw_x is None or self.raw_y is None:
                        continue
                    if self.touching and not self.was_touching:
                        out.append(("down", self.raw_x, self.raw_y))
                    elif self.touching:
                        out.append(("move", self.raw_x, self.raw_y))
                    elif self.was_touching:
                        out.append(("up", self.raw_x, self.raw_y))
                    self.was_touching = self.touching
        except BlockingIOError:
            pass
        return out

    def to_screen(self, rx, ry):
        c = self.calib
        if c.get("swap_xy"):
            rx, ry = ry, rx
        # Linear map using two reference points.
        x = c["sx0"] + (rx - c["rx0"]) * (c["sx1"] - c["sx0"]) / float(c["rx1"] - c["rx0"])
        y = c["sy0"] + (ry - c["ry0"]) * (c["sy1"] - c["sy0"]) / float(c["ry1"] - c["ry0"])
        return (int(min(max(x, 0), W - 1)), int(min(max(y, 0), H - 1)))

    def events(self):
        return [(kind, self.to_screen(rx, ry)) for kind, rx, ry in self.raw_events()]

    def close(self):
        try:
            self.surface.fill((0, 0, 0))
            self.flip()
        except Exception:
            pass
        self.fb.close()
        self.touch.close()


def hide_console_cursor():
    for tty in ("/dev/tty1", "/dev/tty0"):
        try:
            with open(tty, "w") as f:
                f.write("\033[?25l\033[9;0]")  # cursor off, no console blanking
            return
        except OSError:
            continue


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print("Saved", CONFIG_PATH, file=sys.stderr)


# --------------------------------------------------------------------------
# Calibration (framebuffer mode)
# --------------------------------------------------------------------------

def run_calibration(backend, fonts):
    """Ask for taps on two crosshairs near opposite corners and work out the
    raw->screen mapping, including whether the axes are swapped."""
    m = 30
    targets = [(m, m), (W - 1 - m, H - 1 - m), (W - 1 - m, m)]
    raws = []
    for i, (tx, ty) in enumerate(targets):
        s = backend.surface
        s.fill(BG)
        pygame.draw.line(s, TEXT, (tx - 20, ty), (tx + 20, ty), 2)
        pygame.draw.line(s, TEXT, (tx, ty - 20), (tx, ty + 20), 2)
        pygame.draw.circle(s, ACCENT, (tx, ty), 8, 2)
        draw_text(s, "Calibration: tap the cross ({}/3)".format(i + 1),
                  fonts["med"], TEXT, (W // 2, H // 2))
        backend.flip()
        samples = []
        done = False
        while not done:
            for kind, rx, ry in backend.raw_events():
                if kind in ("down", "move"):
                    samples.append((rx, ry))
                elif kind == "up" and samples:
                    done = True
            time.sleep(0.01)
        samples = samples[len(samples) // 4:] or samples  # skip initial jitter
        raws.append((sum(p[0] for p in samples) / len(samples),
                     sum(p[1] for p in samples) / len(samples)))
        time.sleep(0.3)

    (ax, ay), (bx, by), (cx, cy) = raws
    # Third point is top-right: moving along screen X between point 1 and 3.
    # If raw Y changed more than raw X, the axes are swapped.
    swap = abs(cy - ay) > abs(cx - ax)
    if swap:
        ax, ay, bx, by = ay, ax, by, bx
    (sx0, sy0), (sx1, sy1) = targets[0], targets[1]
    calib = {"swap_xy": swap, "rx0": ax, "rx1": bx, "ry0": ay, "ry1": by,
             "sx0": sx0, "sx1": sx1, "sy0": sy0, "sy1": sy1}
    return calib


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def draw_text(surf, text, font, color, center):
    img = font.render(text, True, color)
    surf.blit(img, img.get_rect(center=center))


def draw_plus_minus(surf, rect, plus, color):
    cx, cy = rect.center
    n = min(rect.w, rect.h) // 4
    t = max(4, n // 4)
    pygame.draw.rect(surf, color, (cx - n, cy - t // 2, 2 * n, t))
    if plus:
        pygame.draw.rect(surf, color, (cx - t // 2, cy - n, t, 2 * n))


class Button:
    def __init__(self, rect, on_press, repeat=False):
        self.rect = pygame.Rect(rect)
        self.on_press = on_press
        self.repeat = repeat


class App:
    REPEAT_DELAY = 0.45
    REPEAT_RATE = 0.12

    def __init__(self, backend, state, worker):
        self.b, self.s, self.w = backend, state, worker
        pygame.font.init()
        self.fonts = make_fonts()
        self.pressed = None
        self.press_time = self.next_repeat = 0.0
        self.dirty = True
        self.last_version = -1
        self.last_full_flush = 0.0

        # Layout (480 x 320)
        R = pygame.Rect
        self.r_status = R(0, 0, W, 30)
        self.r_vol_minus = R(4, 34, 80, 80)
        self.r_vol_bar = R(90, 34, 300, 80)
        self.r_vol_plus = R(396, 34, 80, 80)
        self.r_met = R(4, 120, 150, 90)
        self.r_bpm_minus = R(160, 120, 80, 90)
        self.r_bpm = R(244, 120, 148, 90)
        self.r_bpm_plus = R(396, 120, 80, 90)
        self.r_sig = R(4, 216, 110, 100)
        self.r_acc = R(118, 216, 110, 100)
        self.r_clk_minus = R(232, 216, 70, 100)
        self.r_clk = R(306, 216, 86, 100)
        self.r_clk_plus = R(396, 216, 80, 100)

        self.buttons = [
            Button(self.r_vol_minus, lambda: self.nudge_volume(-VOLUME_STEP), True),
            Button(self.r_vol_plus, lambda: self.nudge_volume(+VOLUME_STEP), True),
            Button(self.r_vol_bar, self.tap_volume_bar),
            Button(self.r_met, self.toggle_metronome),
            Button(self.r_bpm_minus, lambda: self.step_bpm(-1), True),
            Button(self.r_bpm_plus, lambda: self.step_bpm(+1), True),
            Button(self.r_sig, self.cycle_timesig),
            Button(self.r_acc, self.toggle_accent),
            Button(self.r_clk_minus, lambda: self.step_click(-1), True),
            Button(self.r_clk_plus, lambda: self.step_click(+1), True),
        ]
        self.touch_pos = (0, 0)

    # -- actions --------------------------------------------------------
    def changed(self):
        with self.s.lock:
            self.s.version += 1

    def nudge_volume(self, d):
        with self.s.lock:
            v = round(min(1.0, max(0.0, self.s.volume + d)) / VOLUME_STEP) * VOLUME_STEP
            self.s.volume = v
        self.w.send("volume", level=v)
        self.changed()

    def tap_volume_bar(self):
        r = self.r_vol_bar.inflate(-20, 0)
        v = min(1.0, max(0.0, (self.touch_pos[0] - r.x) / float(r.w)))
        with self.s.lock:
            self.s.volume = v
        self.w.send("volume", level=v)
        self.changed()

    def toggle_metronome(self):
        with self.s.lock:
            on = not self.s.met["enabled"]
            self.s.met["enabled"] = on
        self.w.send("metronome", enabled=on)
        self.changed()

    def step_bpm(self, d):
        with self.s.lock:
            bpm = float(self.s.met["bpm"])
            if d > 0:
                nxt = next((b for b in BPM_STEPS if b > bpm + 0.01), BPM_STEPS[-1])
            else:
                nxt = next((b for b in reversed(BPM_STEPS) if b < bpm - 0.01), BPM_STEPS[0])
            self.s.met["bpm"] = nxt
        self.w.send("metronome", bpm=nxt)
        self.changed()

    def cycle_timesig(self):
        with self.s.lock:
            cur = self.s.met["timesig"]
            i = TIMESIGS.index(cur) if cur in TIMESIGS else -1
            sig = TIMESIGS[(i + 1) % len(TIMESIGS)]
            self.s.met["timesig"] = sig
        self.w.send("metronome", timesig=sig)
        self.changed()

    def toggle_accent(self):
        with self.s.lock:
            acc = not self.s.met["accentuate"]
            self.s.met["accentuate"] = acc
        self.w.send("metronome", accentuate=acc)
        self.changed()

    def step_click(self, d):
        with self.s.lock:
            db = float(self.s.met["volume_db"])
            if d > 0:
                nxt = next((v for v in CLICK_DB if v > db + 0.01), CLICK_DB[-1])
            else:
                nxt = next((v for v in reversed(CLICK_DB) if v < db - 0.01), CLICK_DB[0])
            self.s.met["volume_db"] = nxt
        self.w.send("metronome", volume_db=nxt)
        self.changed()

    # -- input ----------------------------------------------------------
    def handle(self, ev):
        kind = ev[0]
        if kind == "quit":
            return False
        pos = ev[1]
        self.touch_pos = pos
        now = time.time()
        if kind == "down":
            for btn in self.buttons:
                if btn.rect.collidepoint(pos):
                    self.pressed = btn
                    self.press_time = now
                    self.next_repeat = now + self.REPEAT_DELAY
                    btn.on_press()
                    break
            self.dirty = True
        elif kind == "move":
            if self.pressed is self.buttons[2] and self.r_vol_bar.collidepoint(pos):
                self.tap_volume_bar()  # drag along the volume bar
            elif self.pressed and not self.pressed.rect.inflate(30, 30).collidepoint(pos):
                self.pressed = None
                self.dirty = True
        elif kind == "up":
            self.pressed = None
            self.dirty = True
        return True

    def tick_repeat(self):
        now = time.time()
        if self.pressed and self.pressed.repeat and now >= self.next_repeat:
            self.pressed.on_press()
            self.next_repeat = now + self.REPEAT_RATE

    # -- drawing --------------------------------------------------------
    def panel(self, rect, color=None, label=None, font=None, text_color=TEXT):
        surf = self.b.surface
        down = self.pressed is not None and self.pressed.rect == rect
        pygame.draw.rect(surf, PANEL_DOWN if down else (color or PANEL), rect,
                         border_radius=10)
        if label is not None:
            draw_text(surf, label, font or self.fonts["med"], text_color, rect.center)

    def pm_button(self, rect, plus):
        self.panel(rect)
        draw_plus_minus(self.b.surface, rect, plus, TEXT)

    def draw(self):
        s = self.b.surface
        f = self.fonts
        with self.s.lock:
            connected = self.s.connected
            preset = self.s.preset
            vol = self.s.volume
            met = dict(self.s.met)
        s.fill(BG)

        # Status bar
        name = preset if connected else "Waiting for Pianoteq..."
        img = f["small"].render(name, True, TEXT if connected else DIM)
        s.blit(img, (8, (self.r_status.h - img.get_height()) // 2))
        pygame.draw.circle(s, GREEN if connected else RED, (W - 14, 15), 6)

        # Volume
        self.pm_button(self.r_vol_minus, False)
        self.pm_button(self.r_vol_plus, True)
        self.panel(self.r_vol_bar)
        inner = self.r_vol_bar.inflate(-20, -44).move(0, 10)
        pygame.draw.rect(s, BG, inner, border_radius=6)
        fill = inner.copy()
        fill.w = int(inner.w * vol)
        if fill.w > 0:
            pygame.draw.rect(s, ACCENT, fill, border_radius=6)
        draw_text(s, "Volume  {:d}%".format(int(round(vol * 100))), f["small"],
                  TEXT, (self.r_vol_bar.centerx, self.r_vol_bar.y + 16))

        # Metronome row
        on = bool(met["enabled"])
        self.panel(self.r_met, GREEN if on else PANEL)
        draw_text(s, "Metronome", f["small"], TEXT,
                  (self.r_met.centerx, self.r_met.y + 24))
        draw_text(s, "ON" if on else "OFF", f["big"], TEXT,
                  (self.r_met.centerx, self.r_met.y + 60))
        self.pm_button(self.r_bpm_minus, False)
        self.pm_button(self.r_bpm_plus, True)
        self.panel(self.r_bpm)
        bpm = float(met["bpm"])
        draw_text(s, "{:g}".format(round(bpm, 1)), f["big"], TEXT,
                  (self.r_bpm.centerx, self.r_bpm.y + 38))
        draw_text(s, "BPM", f["small"], DIM, (self.r_bpm.centerx, self.r_bpm.y + 72))

        # Bottom row
        self.panel(self.r_sig)
        draw_text(s, "Time sig", f["small"], DIM, (self.r_sig.centerx, self.r_sig.y + 22))
        draw_text(s, str(met["timesig"]), f["big"], TEXT,
                  (self.r_sig.centerx, self.r_sig.y + 62))

        acc = bool(met["accentuate"])
        self.panel(self.r_acc, ACCENT if acc else PANEL)
        draw_text(s, "Accent", f["small"], TEXT if acc else DIM,
                  (self.r_acc.centerx, self.r_acc.y + 22))
        draw_text(s, "ON" if acc else "OFF", f["big"], TEXT,
                  (self.r_acc.centerx, self.r_acc.y + 62))

        self.pm_button(self.r_clk_minus, False)
        self.pm_button(self.r_clk_plus, True)
        self.panel(self.r_clk)
        db = float(met["volume_db"])
        draw_text(s, "Click", f["small"], DIM, (self.r_clk.centerx, self.r_clk.y + 22))
        label = "mute" if db <= -96 else "{:+g}dB".format(db)
        draw_text(s, label, f["med"], TEXT, (self.r_clk.centerx, self.r_clk.y + 62))

        self.b.flip()

    # -- main loop -------------------------------------------------------
    def run(self):
        while True:
            for ev in self.b.events():
                if not self.handle(ev):
                    return
            self.tick_repeat()
            with self.s.lock:
                v = self.s.version
            now = time.time()
            # Redraw on changes; also every few seconds in case the console
            # scribbled over the framebuffer.
            if self.dirty or v != self.last_version or now - self.last_full_flush > 5:
                self.draw()
                self.dirty = False
                self.last_version = v
                self.last_full_flush = now
            time.sleep(0.01)


def make_fonts():
    # Prefer a clean bold system font if present; fall back to pygame's default.
    path = None
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(cand):
            path = cand
            break
    if path:
        return {"small": pygame.font.Font(path, 16),
                "med": pygame.font.Font(path, 22),
                "big": pygame.font.Font(path, 34)}
    return {"small": pygame.font.Font(None, 22),
            "med": pygame.font.Font(None, 30),
            "big": pygame.font.Font(None, 46)}


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pianoteq-host", default="127.0.0.1")
    ap.add_argument("--pianoteq-port", type=int, default=8081)
    ap.add_argument("--backend", choices=["auto", "fb", "window"], default="auto")
    ap.add_argument("--fbdev", help="framebuffer device (default: auto-detect 480x320 one)")
    ap.add_argument("--touchdev", help="evdev touch device, e.g. /dev/input/event0 (default: auto)")
    ap.add_argument("--rotate", type=int, default=None, choices=[0, 90, 180, 270],
                    help="rotate output in fb mode (e.g. if the framebuffer is 320x480)")
    ap.add_argument("--calibrate", action="store_true",
                    help="run touch calibration (fb mode) and save it")
    ap.add_argument("--fullscreen", action="store_true", help="fullscreen in window mode")
    args = ap.parse_args()

    backend_name = args.backend
    if backend_name == "auto":
        backend_name = "window" if (os.environ.get("DISPLAY") or
                                    os.environ.get("WAYLAND_DISPLAY")) else "fb"

    cfg = load_config()
    rotate = args.rotate if args.rotate is not None else cfg.get("rotate", 0)

    pygame.font.init()
    if backend_name == "window":
        backend = WindowBackend(args.fullscreen)
    else:
        backend = FramebufferBackend(args.fbdev or cfg.get("fbdev"),
                                     args.touchdev or cfg.get("touchdev"),
                                     rotate, cfg.get("calibration") or {})
        if args.calibrate or not cfg.get("calibration"):
            if not args.calibrate:
                print("No touch calibration saved yet - running calibration.", file=sys.stderr)
            backend.calib = run_calibration(backend, make_fonts())
            cfg["calibration"] = backend.calib
            cfg["rotate"] = rotate
            save_config(cfg)

    state = State()
    worker = Worker(Pianoteq(args.pianoteq_host, args.pianoteq_port), state)
    worker.start()
    try:
        App(backend, state, worker).run()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()


if __name__ == "__main__":
    main()

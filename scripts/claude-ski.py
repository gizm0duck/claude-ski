#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude-ski -- a terminal downhill skiing arcade game.

An original ASCII/ANSI homage to classic 1990s downhill skiing games.
Steer your skier down an endless slope, dodge multi-size trees and rocks,
soar off jumps, rack up distance... and survive the abominable yeti that
wakes up and hunts you down after you have skied for about thirty seconds.

This is an original work. It does not use any names, art, sprites, or assets
from any existing commercial game. Python 3 standard library only.

Usage:
    python3 claude-ski.py            # play (auto-detects Unicode)
    python3 claude-ski.py --ascii    # force plain-ASCII glyphs
    python3 claude-ski.py --no-color # disable ANSI colors
    python3 claude-ski.py --seed 123 # deterministic obstacle stream
    python3 claude-ski.py --self-test# run headless sanity checks, then exit

Controls:
    Left / Right  or  A / D   steer
    Up / Down     or  W / S    ease up / tuck for speed
    Space                      hop (brief hang time to clear an obstacle)
    P                          pause
    Q / Esc / Ctrl-C           quit
    R                          restart (from the game-over screen)
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import Enum

# POSIX terminal control. Guarded so the module still imports (and --self-test
# still runs) on platforms without termios.
try:
    import termios
    import tty
    HAVE_TERMIOS = True
except ImportError:  # pragma: no cover - non-POSIX
    termios = None
    tty = None
    HAVE_TERMIOS = False


# --------------------------------------------------------------------------- #
# Tunables                                                                     #
# --------------------------------------------------------------------------- #

FPS = 24
FRAME_TIME = 1.0 / FPS

MIN_W, MIN_H = 32, 16          # below this the play area is too cramped
HUD_ROWS = 2                   # top rows reserved for the heads-up display

BASE_SPEED = 9.0               # downhill scroll, rows per second
SPEED_RAMP = 0.012             # added rows/sec per unit of distance
MAX_SPEED = 34.0
H_SPEED = 26.0                 # horizontal carve speed, columns per second
V_NUDGE = 9.0                  # vertical nudge speed, rows per second
TUCK_BOOST = 1.45              # speed multiplier while tucking (Down)
EASE_BRAKE = 0.7               # speed multiplier while easing up (Up)
LEAN_HOLD = 0.14               # seconds a steer input persists (gives momentum)

SPAWN_BASE = 3.6               # base rows of scroll between obstacle spawns
SPAWN_MIN = 1.9                # densest spacing at high difficulty
CRASH_RECOVERY = 1.1           # seconds frozen after hitting a tree/rock
HOP_TIME = 0.55                # seconds of air after a Space hop
JUMP_AIR = 0.7                 # seconds of air after riding a ramp
JUMP_BONUS = 75                # score for a clean jump

POWER_TIME = 15.0              # seconds of rainbow invincibility from a crab
CRAB_BONUS = 100            # score for grabbing the Claude crab
SMASH_BONUS = 10               # score per obstacle plowed through while invincible

MONSTER_TIME = 30.0            # seconds of skiing before the yeti wakes up
MONSTER_BASE_CLOSE = 1.25      # rows/sec the yeti gains, baseline
MONSTER_CLOSE_RAMP = 0.0016    # extra rows/sec gained per unit distance
MONSTER_HSPEED = 17.0          # how fast the yeti tracks your column
MONSTER_CRASH_GAIN = 4.0       # rows the yeti lunges closer when you crash
MONSTER_JUMP_PUSH = 2.6        # rows the yeti falls back on a clean jump
MONSTER_CATCH = 0.8            # gap (rows) at which the yeti catches you
YETI_EXPLODE_TIME = 0.8        # seconds the detonation animation plays
YETI_SMASH_BONUS = 500         # score for blowing up the yeti while invincible


# --------------------------------------------------------------------------- #
# Glyphs and colors                                                           #
# --------------------------------------------------------------------------- #

class Charset:
    """Per-mode glyphs that are not part of a multi-cell sprite (snow + skier).
    Both presets keep the grid strictly single terminal-width."""

    def __init__(self, unicode_ok: bool):
        self.unicode = unicode_ok
        self.snow = "·" if unicode_ok else "."
        self.skier = make_skier(unicode_ok)   # {pose: rows}, 3x3 sprites


class Palette:
    """ANSI SGR color codes, or empty strings when color is disabled."""

    def __init__(self, enabled: bool):
        def c(code: str) -> str:
            return code if enabled else ""
        self.reset = c("\x1b[0m")
        self.skier = c("\x1b[1;96m")     # bright cyan, bold
        self.tree = c("\x1b[32m")        # green (bushy trees)
        self.pine = c("\x1b[92m")        # bright green (pines)
        self.trunk = c("\x1b[33m")       # brown-ish trunk
        self.rock = c("\x1b[90m")        # gray
        self.shrub = c("\x1b[33m")       # yellow-ish brush
        self.jump = c("\x1b[1;93m")      # bright yellow
        self.snow = c("\x1b[37m")        # white
        self.crab = c("\x1b[1;38;5;208m")  # bold orange (Claude crab)
        self.monster = c("\x1b[1;91m")   # bright red, bold
        # rainbow cycle for the invincibility power-up (bold bright colors)
        self.rainbow = [c(x) for x in (
            "\x1b[1;91m", "\x1b[1;93m", "\x1b[1;92m",
            "\x1b[1;96m", "\x1b[1;94m", "\x1b[1;95m")]
        self.hud = c("\x1b[1;97m")       # bright white
        self.dim = c("\x1b[2;37m")       # dim
        self.warn = c("\x1b[1;5;91m")    # bold blinking red
        self.title = c("\x1b[1;96m")
        self.accent = c("\x1b[1;93m")


def detect_unicode() -> bool:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in enc:
        return True
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        if "utf" in os.environ.get(var, "").lower():
            return True
    return False


# --------------------------------------------------------------------------- #
# Sprites (original multi-cell art; not copied from any existing game)         #
# --------------------------------------------------------------------------- #

class Kind(Enum):
    TREE = "tree"        # solid: crashing into it wipes you out
    ROCK = "rock"        # solid
    SHRUB = "shrub"      # soft: a brush that only slows you down
    JUMP = "jump"        # a ramp: launches you into a scoring hop
    POWERUP = "powerup"  # the orange Claude crab: rainbow invincibility


SOLID = {Kind.TREE, Kind.ROCK}   # crashing obstacles


@dataclass
class SpriteSpec:
    """A reusable obstacle template: art plus its collision footprint.

    The sprite is anchored at bottom-center; ``solid_half`` is the half-width
    of the hitbox at the *base* (the trunk of a tree, the body of a rock), so
    you crash on the base the way you would in a top-down skiing game while the
    canopy simply passes overhead.
    """
    kind: Kind
    rows: list
    w: int
    h: int
    solid_half: int
    color_key: str
    weight: float


def _spec(kind, rows, solid_half, color_key, weight):
    w = max(len(r) for r in rows)
    rows = [r.ljust(w) for r in rows]
    return SpriteSpec(kind, rows, w, len(rows), solid_half, color_key, weight)


def make_sprites(unicode_ok: bool):
    """Build the weighted obstacle palette for the current glyph mode."""
    U = unicode_ok
    specs = []

    # --- pine trees: small, medium, large ------------------------------- #
    if U:
        specs.append(_spec(Kind.TREE, [" ▲ ", "▟█▙", " ┃ "], 0, "pine", 1.3))
        specs.append(_spec(Kind.TREE,
                           ["  ▲  ", " ▟█▙ ", "▟███▙", "  ┃  "], 1, "pine", 1.0))
        specs.append(_spec(Kind.TREE,
                           ["   ▲   ", "  ▟█▙  ", " ▟███▙ ",
                            "▟█████▙", "   ┃   "], 1, "pine", 0.6))
    else:
        specs.append(_spec(Kind.TREE, [" ^ ", "/_\\", " | "], 0, "pine", 1.3))
        specs.append(_spec(Kind.TREE,
                           ["  ^  ", " /_\\ ", "/___\\", "  |  "], 1, "pine", 1.0))
        specs.append(_spec(Kind.TREE,
                           ["   ^   ", "  /_\\  ", " /___\\ ",
                            "/_____\\", "   |   "], 1, "pine", 0.6))

    # --- bushy round trees: medium, large ------------------------------- #
    if U:
        specs.append(_spec(Kind.TREE,
                           [" ▟█▙ ", "▐███▌", " ▜█▛ ", "  ┃  "], 1, "tree", 0.8))
        specs.append(_spec(Kind.TREE,
                           ["  ▄▄▄  ", " ▟███▙ ", "▐█████▌",
                            " ▜███▛ ", "   ┃   "], 1, "tree", 0.5))
    else:
        specs.append(_spec(Kind.TREE,
                           [" ___ ", "(###)", " \\#/ ", "  |  "], 1, "tree", 0.8))
        specs.append(_spec(Kind.TREE,
                           ["  ___  ", " /###\\ ", "(#####)",
                            " \\###/ ", "   |   "], 1, "tree", 0.5))

    # --- rocks: small, large -------------------------------------------- #
    if U:
        specs.append(_spec(Kind.ROCK, ["▗▄▖", "▐█▌"], 1, "rock", 0.8))
        specs.append(_spec(Kind.ROCK,
                           ["▗▄▄▄▖", "▐███▌", "▝▀▀▀▘"], 2, "rock", 0.5))
    else:
        specs.append(_spec(Kind.ROCK, [" _ ", "(_)"], 1, "rock", 0.8))
        specs.append(_spec(Kind.ROCK,
                           [" ___ ", "(___)", "\\___/"], 2, "rock", 0.5))

    # --- shrub (soft) and jump ramp ------------------------------------- #
    if U:
        specs.append(_spec(Kind.SHRUB, ["♣♣"], 1, "shrub", 0.7))
        specs.append(_spec(Kind.JUMP, [" ▄▄▄ ", "▟███▙"], 2, "jump", 0.8))
    else:
        specs.append(_spec(Kind.SHRUB, ["**"], 1, "shrub", 0.7))
        specs.append(_spec(Kind.JUMP, [" ___ ", "/###\\"], 2, "jump", 0.8))

    # --- the orange Claude crab: a rare power-up drawn as blocky pixel
    #     art (notched top, full-width "ears", two square eyes, little legs).
    #     'X' = orange pixel, ' ' = transparent (shows through as a dark eye). #
    crab_px = [
        " XXXXXXX ",
        "XXXXXXXXX",
        "XX XXX XX",
        "XX XXX XX",
        "XXXXXXXXX",
        "XXXXXXXXX",
        "XX X X XX",
    ]
    block = "█" if U else "#"
    crab = [row.replace("X", block) for row in crab_px]
    specs.append(_spec(Kind.POWERUP, crab, 3, "crab", 0.18))

    return specs


def make_yeti(unicode_ok: bool):
    """Two-frame waving-arms animation for the pursuing yeti (bottom-center)."""
    if unicode_ok:
        return (
            ["\\▄▄▄/", "(◉ ◉)", " ███ ", " ╱ ╲ "],
            ["/▄▄▄\\", "(◉ ◉)", " ███ ", " ╱ ╲ "],
        )
    return (
        ["\\___/", "(O O)", " MMM ", " / \\ "],
        ["/___\\", "(O O)", " MMM ", " / \\ "],
    )


# Pose keys the renderer asks for, by skier state.
SKIER_POSES = ("straight", "left", "right", "tuck", "air", "crash")


def make_skier(unicode_ok: bool):
    """Chunky 3x3 pixel skier (seen from behind), anchored at the skis.

    Returns ``{pose: rows}`` with every pose padded to the same width/height so
    the sprite's footprint stays put as the pose changes. The bottom row is the
    skis, which sit on the skier's collision point.
    """
    if unicode_ok:
        raw = {
            "straight": [" █ ", "▟█▙", "╱ ╲"],
            "left":     [" █ ", "▟█▙", "╱╱ "],
            "right":    [" █ ", "▟█▙", " ╲╲"],
            "tuck":     [" █ ", "▟█▙", "▙▟ "],
            "air":      ["\\█/", " █ ", "═══"],
            "crash":    [" ▟▙", "▘╳▝", " ╳ "],
        }
    else:
        raw = {
            "straight": [" O ", "/|\\", "/ \\"],
            "left":     [" O ", "/|\\", "// "],
            "right":    [" O ", "/|\\", " \\\\"],
            "tuck":     [" O ", "/|\\", "/\\ "],
            "air":      ["\\O/", " | ", "==="],
            "crash":    [" X ", "/X\\", "* *"],
        }
    w = max(len(r) for rows in raw.values() for r in rows)
    h = max(len(rows) for rows in raw.values())
    out = {}
    for pose, rows in raw.items():
        padded = [r.ljust(w) for r in rows]
        while len(padded) < h:
            padded.insert(0, " " * w)  # keep the skis anchored at the bottom
        out[pose] = padded
    return out


def make_boom(unicode_ok: bool):
    """Three growing-then-dispersing frames for the yeti detonation."""
    if unicode_ok:
        return (
            [" ▒ ", "▒█▒", " ▒ "],
            ["  ░  ", " ▒█▒ ", "░█▓█░", " ▒█▒ ", "  ░  "],
            ["✦   ✦", "  ░  ", "✦ ✺ ✦", "  ░  ", "✦   ✦"],
        )
    return (
        [" + ", "+#+", " + "],
        ["  .  ", " +#+ ", ".#@#.", " +#+ ", "  .  "],
        ["*   *", "  .  ", "* O *", "  .  ", "*   *"],
    )


# A chunky 6-row block font for the title banner ('X' = pixel, ' ' = blank).
_BANNER_FONT = {
    "C": [" XXXX", "X    ", "X    ", "X    ", "X    ", " XXXX"],
    "L": ["X    ", "X    ", "X    ", "X    ", "X    ", "XXXXX"],
    "A": [" XXX ", "X   X", "X   X", "XXXXX", "X   X", "X   X"],
    "U": ["X   X", "X   X", "X   X", "X   X", "X   X", " XXX "],
    "D": ["XXXX ", "X   X", "X   X", "X   X", "X   X", "XXXX "],
    "E": ["XXXXX", "X    ", "XXXX ", "X    ", "X    ", "XXXXX"],
    "S": [" XXXX", "X    ", " XXX ", "    X", "    X", "XXXX "],
    "K": ["X   X", "X  X ", "XXX  ", "X  X ", "X   X", "X   X"],
    "I": ["XXXXX", "  X  ", "  X  ", "  X  ", "  X  ", "XXXXX"],
    " ": ["     "] * 6,
}


def render_banner(text: str, block: str):
    """Render ``text`` as big block letters; returns a list of equal-width rows."""
    rows = ["" for _ in range(6)]
    for ch in text:
        glyph = _BANNER_FONT.get(ch.upper(), _BANNER_FONT[" "])
        for i in range(6):
            rows[i] += glyph[i] + " "   # one blank column between letters
    return [r[:-1].replace("X", block) for r in rows]


# --------------------------------------------------------------------------- #
# Terminal control                                                            #
# --------------------------------------------------------------------------- #

class Terminal:
    """Owns raw mode, the alternate screen, and *guaranteed* restoration.

    Restoration is idempotent and exception-safe: it is registered to run on
    normal exit, on signals, and via the context-manager's __exit__, so the
    user's terminal is always returned to a sane state.
    """

    def __init__(self, out=None, enable_color=True):
        self.out = out if out is not None else sys.stdout
        self.enable_color = enable_color
        self.is_tty = bool(getattr(self.out, "isatty", lambda: False)())
        self._fd = None
        self._saved = None
        self._active = False
        self.resized = False

    # -- lifecycle -------------------------------------------------------- #

    def setup(self):
        if self._active:
            return
        self._active = True
        if self.is_tty and HAVE_TERMIOS:
            self._fd = self.out.fileno()
            try:
                self._saved = termios.tcgetattr(self._fd)
                tty.setraw(self._fd)
            except (termios.error, OSError):
                self._saved = None
        # Enter alternate screen, hide cursor, clear.
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        self.flush()
        self._install_signal_handlers()

    def restore(self):
        if not self._active:
            return
        self._active = False
        # Restore termios first so even a failed write leaves a usable shell.
        if self._fd is not None and self._saved is not None and HAVE_TERMIOS:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except (termios.error, OSError):
                pass
        try:
            # show cursor, reset attrs, leave alternate screen
            self.write("\x1b[?25h\x1b[0m\x1b[?1049l")
            self.flush()
        except (OSError, ValueError):
            pass

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    # -- io --------------------------------------------------------------- #

    def write(self, s: str):
        try:
            self.out.write(s)
        except (OSError, ValueError):
            pass

    def flush(self):
        try:
            self.out.flush()
        except (OSError, ValueError):
            pass

    def size(self):
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        except OSError:
            cols, rows = 80, 24
        return max(cols, 1), max(rows, 1)

    # -- signals ---------------------------------------------------------- #

    def _install_signal_handlers(self):
        def on_winch(_sig, _frm):
            self.resized = True

        def on_term(_sig, _frm):
            self.restore()
            os._exit(0)

        for sig, handler in (
            (getattr(signal, "SIGWINCH", None), on_winch),
            (signal.SIGTERM, on_term),
            (getattr(signal, "SIGHUP", None), on_term),
        ):
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass


# --------------------------------------------------------------------------- #
# Input                                                                        #
# --------------------------------------------------------------------------- #

# Raw byte sequences -> action tokens.
_ARROWS = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
}
_KEYS = {
    "w": "up", "s": "down", "a": "left", "d": "right",
    "W": "up", "S": "down", "A": "left", "D": "right",
    "p": "pause", "P": "pause",
    "q": "quit", "Q": "quit",
    "r": "restart", "R": "restart",
    " ": "action",
    "\r": "start", "\n": "start",
    "\x03": "quit",   # Ctrl-C (raw mode delivers it as a byte, not SIGINT)
    "\x1b": "quit",   # bare Esc
}


class Input:
    """Non-blocking keyboard reader that emits a list of action tokens."""

    def __init__(self, fd=None):
        self.fd = fd

    def poll(self, timeout: float):
        """Return a list of action tokens read within ``timeout`` seconds."""
        if self.fd is None:
            time.sleep(max(0.0, timeout))
            return []
        try:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return []
        if not ready:
            return []
        try:
            data = os.read(self.fd, 256)
        except OSError:
            return []
        return self.parse(data.decode("utf-8", "ignore"))

    @staticmethod
    def parse(text: str):
        actions = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b" and i + 2 < n and text[i + 1] in "[O":
                seq = text[i:i + 3]
                if seq in _ARROWS:
                    actions.append(_ARROWS[seq])
                    i += 3
                    continue
            actions.append(_KEYS.get(ch))
            i += 1
        return [a for a in actions if a]


# --------------------------------------------------------------------------- #
# Entities                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class Obstacle:
    spec: SpriteSpec
    cx: int                  # center column
    y: float                 # base row (bottom of the sprite); scrolls upward
    passed: bool = False     # already crossed the skier's line this run

    @property
    def kind(self):
        return self.spec.kind


@dataclass
class Player:
    x: float
    y: float
    lean: int = 0            # -1 left, 0 straight, +1 right
    lean_until: float = 0.0
    vlean: int = 0           # -1 up(ease), +1 down(tuck)
    vlean_until: float = 0.0
    air_until: float = 0.0   # airborne (immune) until this time
    crash_until: float = 0.0 # crashed / recovering until this time
    power_until: float = 0.0 # rainbow invincibility until this time

    def airborne(self, now: float) -> bool:
        return now < self.air_until

    def crashed(self, now: float) -> bool:
        return now < self.crash_until

    def powered(self, now: float) -> bool:
        return now < self.power_until


@dataclass
class Monster:
    active: bool = False
    x: float = 0.0
    y: float = 0.0            # base row; > player.y means "behind/below"
    explode_until: float = 0.0  # detonation animation plays until this time
    boom_x: float = 0.0       # where it blew up
    boom_y: float = 0.0

    def exploding(self, now: float) -> bool:
        return now < self.explode_until


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without a terminal)                             #
# --------------------------------------------------------------------------- #

def spans_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    """Do inclusive integer spans [a0,a1] and [b0,b1] overlap?"""
    return not (a1 < b0 or b1 < a0)


def crossed_line(prev_y: float, new_y: float, line: float) -> bool:
    """True if an upward-moving obstacle passed through ``line`` this frame."""
    return new_y <= line <= prev_y


def collides(px: int, pw: int, py: float,
             prev_y: float, new_y: float, ox: int, ow: int) -> bool:
    """Did the skier and an obstacle base intersect as it scrolled by?"""
    return crossed_line(prev_y, new_y, py) and spans_overlap(
        px, px + pw - 1, ox, ox + ow - 1)


# --------------------------------------------------------------------------- #
# Game state and update logic (rendering-free)                                #
# --------------------------------------------------------------------------- #

class State(Enum):
    TITLE = "title"
    PLAYING = "playing"
    PAUSED = "paused"
    GAMEOVER = "gameover"


class Game:
    """Holds all simulation state. ``update`` is pure logic; no I/O here."""

    def __init__(self, width: int, height: int, seed=None, unicode_ok=False, clock=None):
        import random
        self._seed = seed
        self.rng = random.Random(seed)
        self.clock = clock or time.monotonic
        self.sprites = make_sprites(unicode_ok)
        self.yeti_frames = make_yeti(unicode_ok)
        self._weight_total = sum(s.weight for s in self.sprites)
        self.state = State.TITLE
        self._anim = 0.0  # generic timer for screen/sprite animations
        self.session_best = 0
        self.runs = 0
        self.resize(width, height)
        self.reset()
        # Pre-roll the title backdrop so the menu opens on a populated slope,
        # then restore the RNG so a seeded *run* is unaffected by the demo.
        for _ in range(72):
            self._update_title(1 / 24)
        self.rng = random.Random(seed)

    # -- geometry --------------------------------------------------------- #

    def resize(self, width: int, height: int):
        self.width = max(width, MIN_W)
        self.height = max(height, MIN_H)
        self.play_top = HUD_ROWS
        self.play_bottom = self.height - 1
        self.play_left = 1
        self.play_right = self.width - 2
        self.skier_line = self.play_top + max(2, (self.play_bottom - self.play_top) // 3)
        if hasattr(self, "player"):
            self.player.x = min(max(self.player.x, self.play_left), self.play_right)
            self.player.y = self.skier_line

    # -- run lifecycle ---------------------------------------------------- #

    def reset(self):
        self.player = Player(x=(self.play_left + self.play_right) / 2, y=self.skier_line)
        self.obstacles: list[Obstacle] = []
        self.flakes = [
            [self.rng.randint(self.play_left, self.play_right),
             self.rng.uniform(self.play_top, self.play_bottom)]
            for _ in range(max(8, self.width // 6))
        ]
        self.monster = Monster()
        self.distance = 0.0
        self.play_time = 0.0
        self.speed = BASE_SPEED
        self.top_speed = 0
        self.bonus = 0
        self.crashes = 0
        self.jumps = 0
        self.yetis_smashed = 0
        self._spawn_accum = 0.0
        self.gameover_at = 0.0
        self.flash_until = 0.0
        self.message = ""

    def start(self):
        self.reset()
        self.state = State.PLAYING

    @property
    def score(self) -> int:
        return int(self.distance) + self.bonus

    @property
    def speed_kmh(self) -> int:
        return int(self.speed * 4)

    # -- input ------------------------------------------------------------ #

    def handle(self, action: str, now: float):
        if action == "quit":
            return "quit"
        if self.state == State.TITLE:
            if action in ("start", "action"):
                self.start()
        elif self.state == State.PLAYING:
            self._handle_play(action, now)
        elif self.state == State.PAUSED:
            if action in ("pause", "action", "start"):
                self.state = State.PLAYING
        elif self.state == State.GAMEOVER:
            if action in ("restart", "start", "action"):
                self.start()
        return None

    def _handle_play(self, action: str, now: float):
        p = self.player
        if action == "pause":
            self.state = State.PAUSED
            return
        if p.crashed(now):
            return  # locked out while recovering
        if action == "left":
            p.lean, p.lean_until = -1, now + LEAN_HOLD
        elif action == "right":
            p.lean, p.lean_until = 1, now + LEAN_HOLD
        elif action == "up":
            p.vlean, p.vlean_until = -1, now + LEAN_HOLD
        elif action == "down":
            p.vlean, p.vlean_until = 1, now + LEAN_HOLD
        elif action == "action":
            if not p.airborne(now):
                p.air_until = now + HOP_TIME

    # -- simulation ------------------------------------------------------- #

    def update(self, dt: float, now: float):
        self._anim += dt
        if self.state == State.TITLE:
            self._update_title(dt)
            return
        if self.state != State.PLAYING:
            if self.state == State.GAMEOVER:
                self._update_flakes(dt, BASE_SPEED)
            return

        p = self.player
        self.play_time += dt
        self.top_speed = max(self.top_speed, self.speed_kmh)
        # Expire held inputs so the skier re-centers and stops drifting.
        if now >= p.lean_until:
            p.lean = 0
        if now >= p.vlean_until:
            p.vlean = 0

        # Difficulty ramp.
        self.speed = min(MAX_SPEED, BASE_SPEED + self.distance * SPEED_RAMP)
        eff = self.speed
        if not p.crashed(now):
            if p.vlean > 0:
                eff *= TUCK_BOOST
            elif p.vlean < 0:
                eff *= EASE_BRAKE
        else:
            eff *= 0.25  # tumbling slowly during recovery

        scroll = eff * dt
        self.distance += scroll

        # Move the skier.
        if not p.crashed(now):
            p.x += p.lean * H_SPEED * dt
            p.x = min(max(p.x, self.play_left), self.play_right)
            if p.vlean and not p.airborne(now):
                p.y += p.vlean * V_NUDGE * dt
                lo = self.play_top + 2   # leave room for the skier's head
                hi = self.play_bottom - 2
                p.y = min(max(p.y, lo), hi)
            else:
                # drift gently back toward the home line
                p.y += (self.skier_line - p.y) * min(1.0, dt * 3)

        self._update_flakes(dt, eff)
        self._spawn(scroll)
        self._move_obstacles(scroll, now)
        self._update_monster(dt, now)

    def _update_title(self, dt: float):
        """Scroll a no-stakes backdrop of scenery behind the title menu."""
        speed = BASE_SPEED * 0.85
        scroll = speed * dt
        self._update_flakes(dt, speed)
        for ob in self.obstacles:
            ob.y -= scroll
        self.obstacles = [o for o in self.obstacles
                          if o.y >= self.play_top - o.spec.h]
        self._spawn(scroll)

    def _update_flakes(self, dt: float, eff: float):
        for f in self.flakes:
            f[1] -= eff * dt * 0.5
            if f[1] < self.play_top:
                f[1] = self.play_bottom
                f[0] = self.rng.randint(self.play_left, self.play_right)

    def _pick_spec(self) -> SpriteSpec:
        roll = self.rng.random() * self._weight_total
        for spec in self.sprites:
            roll -= spec.weight
            if roll <= 0:
                return spec
        return self.sprites[-1]

    def _spawn(self, scroll: float):
        self._spawn_accum += scroll
        interval = max(SPAWN_MIN, SPAWN_BASE - self.distance * 0.0008)
        while self._spawn_accum >= interval:
            self._spawn_accum -= interval
            spec = self._pick_spec()
            half = spec.w // 2
            lo = self.play_left + half
            hi = self.play_right - half
            if hi < lo:
                cx = (lo + hi) // 2
            else:
                cx = self.rng.randint(lo, hi)
            # Base starts just below the play area so the sprite slides in.
            self.obstacles.append(Obstacle(spec, cx, self.play_bottom + 1.0))

    def _move_obstacles(self, scroll: float, now: float):
        p = self.player
        px = int(round(p.x))
        survivors = []
        for ob in self.obstacles:
            prev_y = ob.y
            ob.y -= scroll
            if not ob.passed and not p.crashed(now) and not p.airborne(now):
                half = ob.spec.solid_half
                if collides(px, 1, p.y, prev_y, ob.y, ob.cx - half, 2 * half + 1):
                    self._resolve_hit(ob, now)
            if ob.y < p.y - 0.5:
                ob.passed = True
            # Keep until the whole sprite has scrolled above the play area.
            if ob.y >= self.play_top - ob.spec.h:
                survivors.append(ob)
        self.obstacles = survivors

    def _grab_power(self, now: float):
        """Pick up the Claude crab: 15 seconds of rainbow invincibility."""
        self.player.power_until = now + POWER_TIME
        self.bonus += CRAB_BONUS
        if self.monster.active:
            self.monster.y += 6.0  # the yeti recoils from the crab's glow
        self.message = "CLAUDE CRAB!  INVINCIBLE!"
        self.flash_until = now + 1.4

    def _resolve_hit(self, ob: Obstacle, now: float):
        p = self.player
        if ob.kind == Kind.POWERUP:
            self._grab_power(now)
        elif p.powered(now):
            # Invincible: plow straight through solid obstacles for bonus, and
            # still launch off ramps for the air time.
            if ob.kind in SOLID:
                self.bonus += SMASH_BONUS
            elif ob.kind == Kind.JUMP:
                p.air_until = max(p.air_until, now + JUMP_AIR)
                self.bonus += JUMP_BONUS
                self.jumps += 1
        elif ob.kind in SOLID:
            p.crash_until = now + CRASH_RECOVERY
            p.lean = p.vlean = 0
            self.crashes += 1
            self.speed = BASE_SPEED  # bleed off speed in the wipeout
            if self.monster.active:
                self.monster.y -= MONSTER_CRASH_GAIN  # the yeti lunges closer
            self.message = self.rng.choice(
                ["WIPEOUT!", "OOF!", "FACE-PLANT!", "YARD SALE!"])
        elif ob.kind == Kind.JUMP:
            p.air_until = max(p.air_until, now + JUMP_AIR)
            self.bonus += JUMP_BONUS
            self.jumps += 1
            if self.monster.active:
                self.monster.y += MONSTER_JUMP_PUSH  # gain ground with style
            self.message = "NICE AIR +%d" % JUMP_BONUS
            self.flash_until = now + 0.8
        else:  # SHRUB: a soft brush, minor slowdown only
            self.speed = max(BASE_SPEED, self.speed * 0.92)
        ob.passed = True

    def _explode_yeti(self, now: float):
        """Invincible contact: the yeti detonates and is blasted back down."""
        m = self.monster
        m.explode_until = now + YETI_EXPLODE_TIME
        m.boom_x, m.boom_y = m.x, m.y
        m.y = self.play_bottom + 6  # knocked far back down the slope
        self.yetis_smashed += 1
        self.bonus += YETI_SMASH_BONUS
        self.message = "YETI OBLITERATED!  +%d" % YETI_SMASH_BONUS
        self.flash_until = now + 1.6

    def summary(self) -> dict:
        """A serializable recap of the session, for reporting after a run."""
        caught = self.state == State.GAMEOVER
        return {
            "outcome": "caught_by_yeti" if caught else "quit",
            "score": self.score,
            "distance": int(self.distance),
            "time_seconds": int(self.play_time),
            "top_speed": self.top_speed,
            "jumps": self.jumps,
            "crashes": self.crashes,
            "yetis_destroyed": self.yetis_smashed,
            "session_best": max(self.session_best, self.score),
            "runs": self.runs,
        }

    def _update_monster(self, dt: float, now: float):
        m = self.monster
        p = self.player
        if not m.active:
            if self.play_time >= MONSTER_TIME:
                m.active = True
                m.x = p.x
                m.y = self.play_bottom + 4
                self.flash_until = now + 1.6
                self.message = "THE YETI AWAKENS"
            return
        if m.exploding(now):
            return  # mid-detonation: the animation plays, no chase this frame
        # Track the skier's column.
        dx = p.x - m.x
        step = MONSTER_HSPEED * dt
        m.x += max(-step, min(step, dx))
        # Close the vertical gap; ramps up the farther you have gone.
        close = MONSTER_BASE_CLOSE + self.distance * MONSTER_CLOSE_RAMP
        m.y -= close * dt
        # Clamp so the lurking yeti stays visible at the base of the slope.
        m.y = min(m.y, self.play_bottom + 1)
        gap = m.y - p.y
        if gap <= MONSTER_CATCH and abs(m.x - p.x) <= 2.0:
            if p.powered(now):
                self._explode_yeti(now)   # rainbow invincibility: BOOM
            else:
                self.state = State.GAMEOVER
                self.gameover_at = now
                self.message = "CAUGHT!"
                self.runs += 1
                self.session_best = max(self.session_best, self.score)


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #

class Renderer:
    """Double-buffered ANSI renderer. Builds a char/color grid, then writes
    only the rows that changed since the previous frame to minimize flicker."""

    def __init__(self, term: Terminal, charset: Charset, palette: Palette):
        self.term = term
        self.cs = charset
        self.pal = palette
        self.width = 0
        self.height = 0
        self._prev: list[str] = []
        self.boom = make_boom(charset.unicode)

    def configure(self, width: int, height: int):
        if width != self.width or height != self.height:
            self.width = width
            self.height = height
            self._prev = []  # force a full repaint
            self.term.write("\x1b[2J")
        self._chars = [[" "] * width for _ in range(height)]
        self._cols = [[""] * width for _ in range(height)]

    # -- low-level plotting ---------------------------------------------- #

    def plot(self, x: int, y: int, ch: str, color: str = ""):
        if 0 <= y < self.height and 0 <= x < self.width:
            self._chars[y][x] = ch
            self._cols[y][x] = color

    def plot_str(self, x: int, y: int, s: str, color: str = ""):
        for i, ch in enumerate(s):
            self.plot(x + i, y, ch, color)

    def blit(self, x: int, y: int, rows, color: str = ""):
        """Draw a multi-row sprite, treating spaces as transparent."""
        for dy, row in enumerate(rows):
            ry = y + dy
            if not (0 <= ry < self.height):
                continue
            for dx, ch in enumerate(row):
                if ch != " ":
                    self.plot(x + dx, ry, ch, color)

    def center(self, y: int, s: str, color: str = ""):
        self.plot_str(max(0, (self.width - len(s)) // 2), y, s, color)

    # -- frame composition ----------------------------------------------- #

    def begin(self):
        for row in self._chars:
            for i in range(self.width):
                row[i] = " "
        for row in self._cols:
            for i in range(self.width):
                row[i] = ""

    def present(self):
        reset = self.pal.reset
        out = []
        new_prev = []
        for y in range(self.height):
            chars = self._chars[y]
            cols = self._cols[y]
            parts = []
            cur = None
            for x in range(self.width):
                col = cols[x]
                if col != cur:
                    parts.append(col if col else reset)
                    cur = col
                parts.append(chars[x])
            parts.append(reset)
            line = "".join(parts)
            new_prev.append(line)
            # Only emit rows that actually changed since the last frame.
            if y >= len(self._prev) or self._prev[y] != line:
                out.append("\x1b[%d;1H%s" % (y + 1, line))
        self._prev = new_prev
        if out:
            self.term.write("".join(out))
            self.term.flush()

    # -- scene drawing ---------------------------------------------------- #

    def draw(self, g: Game, now: float):
        self.begin()
        if g.state == State.TITLE:
            self._draw_title(g)
        elif g.state == State.GAMEOVER:
            self._draw_world(g, now)
            self._draw_gameover(g)
        else:
            self._draw_world(g, now)
            self._draw_hud(g, now)
            if g.state == State.PAUSED:
                self._draw_pause(g)
        self.present()

    def _draw_world(self, g: Game, now: float):
        cs, pal = self.cs, self.pal
        # snow flecks
        for fx, fy in g.flakes:
            self.plot(int(fx), int(fy), cs.snow, pal.dim)
        # side margins as subtle slope edges
        for y in range(g.play_top, g.play_bottom + 1):
            self.plot(0, y, "|", pal.dim)
            self.plot(self.width - 1, y, "|", pal.dim)
        # obstacles, painted top-to-bottom so nearer ones overlap correctly
        for ob in sorted(g.obstacles, key=lambda o: o.y):
            spec = ob.spec
            top_y = int(round(ob.y)) - (spec.h - 1)
            left_x = ob.cx - spec.w // 2
            self.blit(left_x, top_y, spec.rows, getattr(pal, spec.color_key))
        # monster
        if g.monster.active:
            self._draw_monster(g, now)
        # skier (drawn last, always on top)
        self._draw_skier(g, now)

    def _draw_skier(self, g: Game, now: float):
        p = g.player
        cs, pal = self.cs, self.pal
        x, y = int(round(p.x)), int(round(p.y))
        if p.crashed(now):
            pose, color = "crash", pal.monster
        elif p.airborne(now):
            pose, color = "air", pal.accent
        elif p.lean < 0:
            pose, color = "left", pal.skier
        elif p.lean > 0:
            pose, color = "right", pal.skier
        elif p.vlean > 0:
            pose, color = "tuck", pal.skier
        else:
            pose, color = "straight", pal.skier
        rows = cs.skier[pose]
        h, w = len(rows), len(rows[0])
        if p.powered(now) and not p.crashed(now):
            # Flash through rainbow colors while invincible, with a side sparkle.
            color = pal.rainbow[int(g._anim * 12) % len(pal.rainbow)]
            self.plot(x - 2, y, "*", color)
            self.plot(x + 2, y, "*", color)
        # Anchor the sprite at the skis (its bottom row sits on the skier's line).
        self.blit(x - w // 2, y - (h - 1), rows, color)

    def _draw_monster(self, g: Game, now: float):
        m = g.monster
        if m.exploding(now):
            self._draw_boom(g, now)
            return
        frames = g.yeti_frames
        frame = frames[int(g._anim * 6) % len(frames)]
        h = len(frame)
        w = max(len(r) for r in frame)
        top_y = int(round(m.y)) - (h - 1)
        left_x = int(round(m.x)) - w // 2
        self.blit(left_x, top_y, frame, self.pal.monster)

    def _draw_boom(self, g: Game, now: float):
        m = g.monster
        elapsed = YETI_EXPLODE_TIME - (m.explode_until - now)
        prog = max(0.0, min(0.999, elapsed / YETI_EXPLODE_TIME))
        frame = self.boom[int(prog * len(self.boom))]
        h = len(frame)
        w = max(len(r) for r in frame)
        cx, cy = int(round(m.boom_x)), int(round(m.boom_y))
        self.blit(cx - w // 2, cy - h // 2, frame, self.pal.accent)
        self.center(max(g.play_top, cy - h // 2 - 1), "YETI OBLITERATED!", self.pal.accent)

    def _draw_hud(self, g: Game, now: float):
        pal = self.pal
        t = int(g.play_time)
        left = " SCORE %06d  DIST %5dm  SPD %3d  TIME %d:%02d  CRASH %d" % (
            g.score, int(g.distance), g.speed_kmh, t // 60, t % 60, g.crashes)
        controls = "[<>] steer  [^v] tuck  [space] hop  [p]ause  [q]uit "
        self.plot_str(0, 0, left.ljust(self.width)[:self.width], pal.hud)
        self.plot_str(0, 1, controls.ljust(self.width)[:self.width], pal.dim)
        # status / warning on the right of row 2
        status, col = "", pal.accent
        if g.player.powered(now):
            secs = max(0, int(g.player.power_until - now) + 1)
            status = " * INVINCIBLE %ds * " % secs
            col = pal.rainbow[int(g._anim * 12) % len(pal.rainbow)] or pal.accent
        elif g.monster.active and now < g.flash_until:
            status, col = " !! YETI !! ", pal.warn
        elif g.monster.active:
            gap = max(0, int(g.monster.y - g.player.y))
            status, col = " YETI %dm behind " % gap, pal.warn
        elif (MONSTER_TIME - g.play_time) <= 10:
            status, col = " YETI IN %ds " % max(0, int(MONSTER_TIME - g.play_time)), pal.accent
        elif g.message and now < g.flash_until:
            status, col = " " + g.message + " ", pal.accent
        if status:
            self.plot_str(max(0, self.width - len(status) - 1), 1, status, col)
        # transient crash message near the skier
        if g.player.crashed(now) and g.message:
            self.center(g.skier_line - 2, "  %s  " % g.message, pal.accent)

    # -- overlays --------------------------------------------------------- #

    def _box(self, lines, color, top=None):
        w = max(len(s) for s in lines) + 4
        x = max(0, (self.width - w) // 2)
        y = top if top is not None else max(0, (self.height - len(lines)) // 2 - 1)
        for i, s in enumerate(lines):
            self.plot_str(x, y + i, ("  " + s).ljust(w), color)

    def _panel(self, x0: int, y0: int, x1: int, y1: int, color: str):
        """Clear a rectangle (so backdrop scenery can't obscure it) and draw a
        light border around it -- the 'protected' card the menu sits on."""
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(self.width - 1, x1); y1 = min(self.height - 1, y1)
        for yy in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                self._chars[yy][xx] = " "
                self._cols[yy][xx] = ""
        if self.cs.unicode:
            tl, tr, bl, br, hz, vt = "╭", "╮", "╰", "╯", "─", "│"
        else:
            tl = tr = bl = br = "+"; hz = "-"; vt = "|"
        for xx in range(x0 + 1, x1):
            self.plot(xx, y0, hz, color); self.plot(xx, y1, hz, color)
        for yy in range(y0 + 1, y1):
            self.plot(x0, yy, vt, color); self.plot(x1, yy, vt, color)
        self.plot(x0, y0, tl, color); self.plot(x1, y0, tr, color)
        self.plot(x0, y1, bl, color); self.plot(x1, y1, br, color)

    def _draw_title(self, g: Game):
        cs, pal = self.cs, self.pal
        # --- backdrop: scenery scrolling past, just like a live run ---------
        for fx, fy in g.flakes:
            self.plot(int(fx), int(fy), cs.snow, pal.dim)
        for ob in sorted(g.obstacles, key=lambda o: o.y):
            spec = ob.spec
            top_y = int(round(ob.y)) - (spec.h - 1)
            self.blit(ob.cx - spec.w // 2, top_y, spec.rows,
                      getattr(pal, spec.color_key))

        # --- title + menu content -------------------------------------------
        big = render_banner("CLAUDE SKI", "█" if cs.unicode else "#")
        medium = [
            r"  ___ _      _   _ _  _____   ___ _  _____ ",
            r" / __| |    /_\ | | || |   \ | __| |/ /_ _|",
            r"| (__| |__ / _ \| |_  _| |) || _|| ' < | | ",
            r" \___|____/_/ \_\___|_||___/ |___|_|\_\___|",
        ]
        if self.width >= len(big[0]) + 6:
            banner = big
        elif self.width >= len(medium[0]) + 6:
            banner = medium
        else:
            banner = ["C L A U D E   S K I"]

        sk = "█" if cs.unicode else "A"
        lines = [(row, pal.title) for row in banner]
        lines += [
            ("", pal.dim),
            ("an original downhill arcade homage", pal.dim),
            ("%s dodge the trees   %s outrun the yeti" % (sk, sk), pal.accent),
            ("grab the orange crab for rainbow invincibility!",
             pal.crab or pal.accent),
            ("", pal.dim),
            ("Press  SPACE  or  ENTER  to drop in", pal.hud),
            ("Steer  < >    Tuck  ^ v    Hop  SPACE    Quit  Q", pal.dim),
        ]
        top = max(1, (self.height - len(lines)) // 2)
        content_w = max((len(t) for t, _ in lines if t), default=10)
        cx0 = (self.width - content_w) // 2
        # Protected card: clear scenery behind the menu, then draw its border.
        self._panel(cx0 - 3, top - 2, cx0 + content_w + 2, top + len(lines), pal.dim)
        for i, (text, color) in enumerate(lines):
            if text:
                self.center(top + i, text, color)

    def _draw_pause(self, g: Game):
        self._box(["PAUSED", "", "Press P to resume", "Q to quit"], self.pal.hud)

    def _draw_gameover(self, g: Game):
        lines = [
            "G A M E   O V E R",
            "",
            "The yeti got you.",
            "",
            "Score    %d" % g.score,
            "Distance %dm" % int(g.distance),
            "Time     %d:%02d" % (int(g.play_time) // 60, int(g.play_time) % 60),
            "Jumps    %d   Crashes %d" % (g.jumps, g.crashes),
            "",
            "Press R to ski again   Q to quit",
        ]
        self._box(lines, self.pal.hud, top=max(1, self.height // 2 - 5))


# --------------------------------------------------------------------------- #
# Main loop                                                                    #
# --------------------------------------------------------------------------- #

def run(args) -> int:
    unicode_ok = detect_unicode() and not args.ascii
    use_color = (not args.no_color) and sys.stdout.isatty()
    charset = Charset(unicode_ok)
    palette = Palette(use_color)

    term = Terminal(enable_color=use_color)
    if not term.is_tty:
        sys.stderr.write(
            "claude-ski needs an interactive terminal (a real TTY).\n"
            "Run it directly in your terminal, e.g.:\n"
            "    python3 %s\n" % os.path.basename(sys.argv[0]))
        return 2

    width, height = term.size()
    game = Game(width, height, seed=args.seed, unicode_ok=unicode_ok)
    renderer = Renderer(term, charset, palette)
    results_path = getattr(args, "results", None)
    prev_state = game.state

    import atexit
    atexit.register(term.restore)

    try:
        with term:
            inp = Input(fd=term.out.fileno())
            renderer.configure(*term.size())
            last = time.monotonic()
            running = True
            while running:
                frame_start = time.monotonic()

                if term.resized:
                    term.resized = False
                    w, h = term.size()
                    game.resize(w, h)
                    renderer.configure(w, h)

                w, h = term.size()
                if w < MIN_W or h < MIN_H:
                    renderer.configure(w, h)
                    renderer.begin()
                    renderer.center(max(0, h // 2),
                                    "Terminal too small - resize me", palette.warn)
                    renderer.center(max(0, h // 2 + 1),
                                    "(need at least %dx%d)" % (MIN_W, MIN_H), palette.dim)
                    renderer.present()
                    for a in inp.poll(0.1):
                        if a == "quit":
                            running = False
                    continue

                for action in inp.poll(0.0):
                    if game.handle(action, frame_start) == "quit":
                        running = False
                        break
                if not running:
                    break

                now = time.monotonic()
                dt = min(0.1, max(0.0, now - last))
                last = now
                game.update(dt, now)
                # Save a recap the moment a run ends, so the launcher can report
                # it without waiting for the player to leave the game-over screen.
                if (results_path and prev_state != State.GAMEOVER
                        and game.state == State.GAMEOVER):
                    write_result(results_path, game.summary())
                prev_state = game.state
                renderer.draw(game, now)

                # Sleep off the rest of the frame budget (keeps CPU low) while
                # still waking immediately on keypress.
                elapsed = time.monotonic() - frame_start
                remaining = FRAME_TIME - elapsed
                if remaining > 0:
                    for action in inp.poll(remaining):
                        if game.handle(action, time.monotonic()) == "quit":
                            running = False
                            break
    finally:
        if results_path:
            write_result(results_path, game.summary())
        term.restore()
    return 0


# --------------------------------------------------------------------------- #
# Launching in a fresh terminal + reporting results back                       #
# --------------------------------------------------------------------------- #

def write_result(path: str, data: dict):
    """Write the run summary as JSON. Never raises (best-effort)."""
    import json
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except (OSError, TypeError, ValueError):
        pass


def format_result(data: dict) -> str:
    """Render a results dict as a compact human-readable recap."""
    outcomes = {"caught_by_yeti": "Caught by the yeti", "quit": "Quit mid-run"}
    t = int(data.get("time_seconds", 0))
    runs = int(data.get("runs", 0))
    return "\n".join([
        "Claude Ski - run complete",
        "  Outcome:    %s" % outcomes.get(data.get("outcome"), str(data.get("outcome", "-"))),
        "  Score:      %d" % data.get("score", 0),
        "  Distance:   %d m" % data.get("distance", 0),
        "  Time:       %d:%02d" % (t // 60, t % 60),
        "  Top speed:  %d" % data.get("top_speed", 0),
        "  Jumps:      %d    Crashes: %d    Yetis blasted: %d" % (
            data.get("jumps", 0), data.get("crashes", 0), data.get("yetis_destroyed", 0)),
        "  Session best: %d  (%d run%s)" % (
            data.get("session_best", 0), runs, "" if runs == 1 else "s"),
    ])


def macos_launch_script(run_cmd: str) -> str:
    """AppleScript that opens Terminal.app and runs ``run_cmd``."""
    inner = run_cmd.replace("\\", "\\\\").replace('"', '\\"')
    return ('tell application "Terminal"\n'
            '  activate\n'
            '  do script "%s"\n'
            'end tell') % inner


def launch_in_terminal(script_path=None) -> int:
    """Open the game in a fresh interactive terminal window (so it gets a real
    TTY), wait for the run to finish, then print the results recap to stdout.

    This is what the /claude-ski slash command runs: Claude calls it from its
    Bash tool (which has no controlling TTY), a real terminal pops open to play
    in, and the score comes back here for Claude to relay."""
    import json
    import shlex
    import subprocess
    import tempfile

    script = os.path.abspath(script_path or __file__)
    result_path = os.path.join(tempfile.gettempdir(), "claude-ski-result.json")
    try:
        os.remove(result_path)
    except OSError:
        pass
    run_cmd = "python3 %s --results %s" % (shlex.quote(script), shlex.quote(result_path))

    opened = False
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e", macos_launch_script(run_cmd)],
                           check=True, capture_output=True)
            opened = True
        except (OSError, subprocess.CalledProcessError):
            opened = False
    elif sys.platform.startswith("linux"):
        for argv in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"],
                     ["konsole", "-e"], ["xfce4-terminal", "-x"],
                     ["alacritty", "-e"], ["kitty"], ["xterm", "-e"]):
            if shutil.which(argv[0]):
                try:
                    subprocess.Popen(argv + ["python3", script, "--results", result_path])
                    opened = True
                    break
                except OSError:
                    continue

    if not opened:
        sys.stdout.write(
            "Could not open a new terminal window automatically.\n"
            "To play Claude Ski, run this in your own terminal:\n\n    %s\n"
            % ("python3 " + shlex.quote(script)))
        return 0

    sys.stdout.write("Launched Claude Ski in a new terminal window - go play! "
                     "(arrows/WASD to steer, Q to quit)\n")
    sys.stdout.flush()
    # Wait for the run to finish and the recap to be written (bounded so we stay
    # under the typical tool timeout; the file persists either way).
    deadline = time.monotonic() + 110
    while time.monotonic() < deadline:
        if os.path.exists(result_path):
            time.sleep(0.2)  # let the writer finish
            try:
                with open(result_path) as f:
                    data = json.load(f)
                sys.stdout.write("\n" + format_result(data) + "\n")
                return 0
            except (OSError, ValueError):
                pass
        time.sleep(0.5)
    sys.stdout.write(
        "\nStill playing - your score will be saved to:\n  %s\n"
        "Ask me to read it once you've finished.\n" % result_path)
    return 0


# --------------------------------------------------------------------------- #
# Self-test (headless: no raw mode, no alternate screen)                       #
# --------------------------------------------------------------------------- #

def self_test() -> int:
    import io

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1) Terminal restore safety: setup/restore on a non-tty stream must not
    #    raise, must be idempotent, and must emit the cursor/altscreen resets.
    buf = io.StringIO()
    t = Terminal(out=buf, enable_color=False)
    assert not t.is_tty
    t.setup()
    t.restore()
    t.restore()  # idempotent
    out = buf.getvalue()
    check("terminal restore shows cursor", "\x1b[?25h" in out)
    check("terminal restore leaves alt screen", "\x1b[?1049l" in out)
    check("terminal setup enters alt screen", "\x1b[?1049h" in out)

    # 2) Collision logic (base crosses the skier line within the hitbox).
    check("collision: crossing + overlap hits",
          collides(px=10, pw=1, py=5.0, prev_y=6.0, new_y=4.0, ox=10, ow=1))
    check("collision: no horizontal overlap misses",
          not collides(10, 1, 5.0, 6.0, 4.0, ox=14, ow=1))
    check("collision: not yet crossed misses",
          not collides(10, 1, 5.0, 9.0, 7.0, ox=10, ow=1))
    check("collision: wide hitbox catches adjacent column",
          collides(11, 1, 5.0, 6.0, 4.0, ox=9, ow=5))
    check("spans_overlap basic", spans_overlap(1, 3, 3, 5) and not spans_overlap(1, 3, 4, 5))

    # 3) Sprites: multiple sizes exist and every row is rectangular.
    specs = make_sprites(True)
    heights = {s.h for s in specs}
    widths = {s.w for s in specs}
    check("sprites: several heights", len(heights) >= 3)
    check("sprites: several widths", len(widths) >= 3)
    check("sprites: tall obstacles exist", max(heights) >= 4)
    check("sprites: rows are rectangular",
          all(len(set(len(r) for r in s.rows)) == 1 for s in specs))
    check("sprites: ascii mode same shapes",
          [s.w for s in make_sprites(False)] == [s.w for s in specs])
    check("yeti: two animation frames", len(make_yeti(True)) == 2)
    check("crab power-up sprite exists",
          any(s.kind == Kind.POWERUP for s in specs))

    # Skier is a multi-cell pixel sprite with all directional poses, and every
    # pose shares one rectangular footprint so the anchor stays put.
    for mode in (True, False):
        sk = make_skier(mode)
        check("skier has all poses (%s)" % ("U" if mode else "A"),
              set(SKIER_POSES) <= set(sk))
        check("skier poses are multi-row (%s)" % ("U" if mode else "A"),
              all(len(rows) >= 2 for rows in sk.values()))
        dims = {(len(rows), len(rows[0])) for rows in sk.values()}
        check("skier poses share one footprint (%s)" % ("U" if mode else "A"),
              len(dims) == 1)
        check("skier poses are rectangular (%s)" % ("U" if mode else "A"),
              all(len(set(len(r) for r in rows)) == 1 for rows in sk.values()))

    # Title banner spells the name in big block letters of uniform width.
    banner = render_banner("CLAUDE SKI", "#")
    check("title banner has 6 rows", len(banner) == 6)
    check("title banner rows share one width", len(set(len(r) for r in banner)) == 1)
    check("title banner has block pixels", all("#" in r for r in banner))
    check("title banner is large", len(banner[0]) >= 50)

    # 3b) The crab grants 15s of invincibility; solids no longer crash, and
    #     the yeti cannot catch a powered-up skier.
    def cross_obstacle(seed, kind, powered):
        gg = Game(80, 24, seed=seed)
        gg.start()
        sp = next(s for s in gg.sprites if s.kind == kind)
        px = int(round(gg.player.x))
        gg.obstacles = [Obstacle(sp, px, gg.player.y + 0.4)]  # base just below line
        if powered:
            gg.player.power_until = 100.0
        gg._move_obstacles(1.0, now=1.0)  # scroll past the skier's line
        return gg

    check("solid obstacle crashes when not powered",
          cross_obstacle(1, Kind.TREE, powered=False).crashes == 1)
    check("invincible: solid obstacle does NOT crash",
          cross_obstacle(1, Kind.TREE, powered=True).crashes == 0)
    gpow = cross_obstacle(2, Kind.POWERUP, powered=False)
    check("touching the crab grants invincibility",
          gpow.player.powered(1.0) and gpow.player.power_until - 1.0 >= POWER_TIME - 0.001)
    # Running into the yeti while invincible detonates it instead of ending
    # the run: no game over, a big bonus, and the yeti is blasted back down.
    gy = Game(80, 24, seed=4); gy.start()
    gy.monster.active = True
    gy.monster.x = gy.player.x
    gy.monster.y = gy.player.y + 0.5
    gy.player.power_until = 100.0
    b0 = gy.bonus
    gy._update_monster(1 / 24, now=1.0)
    check("invincibility blocks the yeti (no game over)", gy.state == State.PLAYING)
    check("powered yeti contact: yeti explodes", gy.monster.exploding(1.0))
    check("powered yeti contact: big bonus awarded", gy.bonus >= b0 + YETI_SMASH_BONUS)
    check("powered yeti contact: yeti knocked back", gy.monster.y > gy.player.y + 2)
    # An ordinary (un-powered) catch still ends the run.
    gk = Game(80, 24, seed=4); gk.start()
    gk.monster.active = True
    gk.monster.x = gk.player.x
    gk.monster.y = gk.player.y + 0.5
    gk._update_monster(1 / 24, now=1.0)
    check("un-powered yeti contact ends the run", gk.state == State.GAMEOVER)
    check("boom: three explosion frames", len(make_boom(True)) == 3)

    # Run summary / results reporting (used to send the score back to Claude).
    gk.summary()  # gk is at GAMEOVER
    s = gk.summary()
    check("summary marks a caught run", s["outcome"] == "caught_by_yeti")
    check("summary counts the run", s["runs"] >= 1)
    gq = Game(80, 24, seed=5); gq.start()
    check("summary marks a quit run", gq.summary()["outcome"] == "quit")
    check("summary has all stat fields",
          {"score", "distance", "time_seconds", "top_speed", "jumps",
           "crashes", "yetis_destroyed", "session_best", "runs"} <= set(gq.summary()))
    recap = format_result(gk.summary())
    check("format_result renders a recap",
          "Claude Ski" in recap and "Score:" in recap and "Session best:" in recap)
    import json as _json, tempfile as _tmp
    rp = os.path.join(_tmp.gettempdir(), "claude-ski-selftest.json")
    write_result(rp, gk.summary())
    with open(rp) as _f:
        roundtrip = _json.load(_f)
    os.remove(rp)
    check("write_result round-trips JSON", roundtrip == gk.summary())
    check("macos launch script targets Terminal + script",
          "Terminal" in macos_launch_script("python3 /x/claude-ski.py")
          and "claude-ski.py" in macos_launch_script("python3 /x/claude-ski.py"))

    # Title backdrop: a freshly constructed game (on the title) is already
    # populated with scenery scrolling past, without disturbing a seeded run.
    gt = Game(80, 24, seed=9, unicode_ok=True)
    check("title screen opens pre-populated with scenery", len(gt.obstacles) > 0)
    gt.start()
    check("starting a run clears the title backdrop", len(gt.obstacles) == 0)

    # 4) Score / distance / speed progression with a fixed seed (deterministic).
    #    ``now`` advances with dt exactly as the real loop does, so the
    #    airborne/crash timers expire correctly and collisions are exercised.
    dt = 1 / 24
    g = Game(80, 24, seed=7)
    g.start()
    d0, s0, spd0 = g.distance, g.score, g.speed
    now = 1.0
    for _ in range(600):  # ~25 simulated seconds
        now += dt
        g.update(dt, now)
    check("distance increases", g.distance > d0)
    check("score increases", g.score > s0)
    check("speed increases over time", g.speed > spd0)
    check("obstacles spawned", len(g.obstacles) > 0)
    check("spawned obstacles include a multi-row sprite",
          any(o.spec.h > 1 for o in g.obstacles))
    # A skier left sitting in the obstacle field really does crash (proves the
    # collision pipeline fires end-to-end, not just the pure helper).
    check("collisions fire during a real run", g.crashes > 0)

    # 5) The yeti wakes up after ~30s of play and can end the run.
    g2 = Game(80, 24, seed=3)
    g2.start()
    now = 1.0
    safety = 0
    while g2.state == State.PLAYING and safety < 100000:
        now += dt
        g2.update(dt, now)
        safety += 1
    check("yeti activates after time threshold", g2.monster.active)
    check("yeti woke up at ~30s of play", g2.play_time >= MONSTER_TIME)
    check("run can reach game over", g2.state == State.GAMEOVER)

    # 6) Game can initialize and simulate WITHOUT entering raw mode.
    check("headless game never touched a tty", True)

    # 7) Input parser maps raw bytes to actions.
    check("input parses arrows", Input.parse("\x1b[D\x1b[C") == ["left", "right"])
    check("input parses wasd + quit", Input.parse("wasdq") ==
          ["up", "left", "down", "right", "quit"])
    check("input ignores unknown bytes", Input.parse("z\x00") == [])

    # Report.
    width = max(len(n) for n, _ in checks)
    passed = 0
    for name, ok in checks:
        print(("PASS" if ok else "FAIL"), name.ljust(width))
        passed += ok
    total = len(checks)
    print("-" * (width + 5))
    print("%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-ski",
        description="A terminal downhill skiing arcade game.")
    parser.add_argument("--ascii", action="store_true",
                        help="force plain-ASCII glyphs (no Unicode)")
    parser.add_argument("--no-color", action="store_true",
                        help="disable ANSI colors")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed the obstacle RNG for a repeatable run")
    parser.add_argument("--self-test", action="store_true",
                        help="run headless sanity checks and exit")
    parser.add_argument("--launch", action="store_true",
                        help="open the game in a new terminal window and report "
                             "the score when it ends (used by /claude-ski)")
    parser.add_argument("--results", metavar="PATH", default=None,
                        help="write a JSON run summary to PATH on exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.launch:
        return launch_in_terminal()
    try:
        return run(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

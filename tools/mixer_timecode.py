#!/usr/bin/env python3
"""
The timecode display (docs/timecode_proposal_2026-11-18.md; OSC standard
"Timecode"): from the PL's decoded LTC to the UDP stream the controllers show.
No OSC parsing and no TCP here: the server validates `timecode/subscribe` and
calls TimecodeHub.

  decode_ltc / encode_ltc   the 64 data bits of an LTC frame <-> a time
  count_from_label /        a time <-> its frame number in the day, drop-frame
    label_from_count        included
  RateMeter                 the frame rate, from the samples between sync words
  Tracker                   readings -> what the controllers are told (state,
                            time, rate). No clock, no sockets
  TimecodeSource            select(source); read() -> Reading
    HardwareTimecodeSource  the PL's window (mixer_hw.TimecodeHW)
    SyntheticTimecodeSource for tests and the simulator only (server flag
                            --timecode-source synthetic, never with --hw)
  TimecodeHub               subscriptions, the poll, the stream

Two rules protect the rest of the mixer:
  - the time is never a parameter: it would be saved to the SD card, and SD
    writes have disturbed USB audio before (hardening, H-F8);
  - the stream never takes the TCP ordering lock: it has its own thread, its
    own lock and UDP, so control traffic can't wait behind it.

Stream format (standard): /<name>/timecode <seq:i> <state:i> <hours:i>
<minutes:i> <seconds:i> <frames:i> <fps:f> <dropFrame:i>; once per decoded
frame, otherwise twice a second, and at once when the state changes.

The clock and the send function are injected, so tests drive tick() with a
fake clock and capture datagrams without sockets or sleeps.
"""

import collections
import threading
import time

from osc_codec import encode_message

STATE_OFF, STATE_NO_TIMECODE, STATE_RUNNING = 0, 1, 2

POLL_HZ = 60.0           # twice the fastest rate: a 30 fps source is seen frame by frame
LEASE_S = 5.0
KEEPALIVE_S = 0.5
RATE_WINDOW = 16         # frames averaged (about half a second)
RATE_JUMP = 0.03         # a frame this far from the mean starts the measurement again
SEQ_MOD = 1 << 31

FS = 48000

Rate = collections.namedtuple("Rate", "samples fps nominal")
RATES = (Rate(2002.0, 23.976, 24), Rate(2000.0, 24.0, 24), Rate(1920.0, 25.0, 25),
         Rate(1601.6, 29.97, 30), Rate(1600.0, 30.0, 30))
DROP_FPS = 29.97

Time = collections.namedtuple("Time", "hours minutes seconds frames drop")
Reading = collections.namedtuple("Reading", "frames low high frame_samples signal locked")
Status = collections.namedtuple("Status", "state hours minutes seconds frames fps drop")

DAY_DROP = 2_589_408     # frames in a drop-frame day


# ----- the LTC frame's data bits -----
def decode_ltc(low, high):
    """LTC bits 31:0 and 63:32 -> a Time, or None if the digits are impossible."""
    word = (high << 32) | low

    def bits(pos, width):
        return (word >> pos) & ((1 << width) - 1)

    units = (bits(0, 4), bits(16, 4), bits(32, 4), bits(48, 4))
    if any(u > 9 for u in units):
        return None
    frames = bits(8, 2) * 10 + units[0]
    seconds = bits(24, 3) * 10 + units[1]
    minutes = bits(40, 3) * 10 + units[2]
    hours = bits(56, 2) * 10 + units[3]
    if frames > 29 or seconds > 59 or minutes > 59 or hours > 23:
        return None
    return Time(hours, minutes, seconds, frames, bool(bits(10, 1)))


def encode_ltc(hours, minutes, seconds, frames, drop=False):
    """A time -> (LTC bits 31:0, LTC bits 63:32); the user bits are 0."""
    word = ((frames % 10) | (frames // 10) << 8 | int(drop) << 10
            | (seconds % 10) << 16 | (seconds // 10) << 24
            | (minutes % 10) << 32 | (minutes // 10) << 40
            | (hours % 10) << 48 | (hours // 10) << 56)
    return word & 0xFFFF_FFFF, word >> 32


# ----- a label <-> its frame number in the day -----
def count_from_label(hours, minutes, seconds, frames, nominal, drop=False):
    n = (hours * 3600 + minutes * 60 + seconds) * nominal + frames
    if drop:
        total_minutes = 60 * hours + minutes
        n -= 2 * (total_minutes - total_minutes // 10)
    return n


def label_from_count(n, nominal, drop=False):
    """(hours, minutes, seconds, frames); n wraps into the day."""
    n %= DAY_DROP if drop else 86400 * nominal
    if drop:
        d, r = divmod(n, 17982)
        n += 18 * d
        if r >= 2:
            n += 2 * ((r - 2) // 1798)
    return (n // (3600 * nominal)) % 24, (n // (60 * nominal)) % 60, (n // nominal) % 60, n % nominal


# ----- the rate -----
def nearest_rate(samples):
    return min(RATES, key=lambda r: abs(r.samples - samples))


class RateMeter:
    """The mean of the last RATE_WINDOW frame lengths. 1,600 and 1,601.6 samples
    differ by 0.1 %, so one frame can't tell 30 from 29.97."""

    def __init__(self):
        self.history = collections.deque(maxlen=RATE_WINDOW)

    def reset(self):
        self.history.clear()

    def add(self, samples):
        if self.history:
            mean = sum(self.history) / len(self.history)
            if abs(samples - mean) > RATE_JUMP * mean:      # another rate: measure afresh
                self.history.clear()
        self.history.append(samples)

    def rate(self):
        if len(self.history) < RATE_WINDOW:
            return None
        return nearest_rate(sum(self.history) / len(self.history))


# ----- readings -> what the controllers are told -----
class Tracker:
    def __init__(self):
        self.meter = RateMeter()
        self.reset(0)

    def reset(self, source):
        """A new source: nothing of the previous one carries over."""
        self.source = source
        self.meter.reset()
        self.last_frames = None
        self.time = (0, 0, 0, 0)
        self.fps = 0.0
        self.drop = 0

    def feed(self, reading):
        """(Status, True if this reading brought a new frame)."""
        if self.source == 0 or reading is None:
            return Status(STATE_OFF, 0, 0, 0, 0, 0.0, 0), False
        new = False
        moved = self.last_frames is not None and reading.frames != self.last_frames
        self.last_frames = reading.frames
        if moved and reading.locked:
            t = decode_ltc(reading.low, reading.high)
            if t is not None:
                self.meter.add(reading.frame_samples)
                rate = self.meter.rate()
                if rate is not None and t.frames < rate.nominal:
                    self.time = (t.hours, t.minutes, t.seconds, t.frames)
                    self.fps = rate.fps
                    self.drop = int(t.drop and rate.fps == DROP_FPS)
                    new = True
        running = reading.locked and self.fps != 0.0 and self.meter.rate() is not None
        return Status(STATE_RUNNING if running else STATE_NO_TIMECODE, *self.time,
                      self.fps, self.drop), new


# ----- sources -----
class TimecodeSource:
    def select(self, source):
        raise NotImplementedError

    def read(self):
        raise NotImplementedError


class HardwareTimecodeSource(TimecodeSource):
    """The PL's window: {mixer_hw.TimecodeHW}."""

    def __init__(self, window):
        self.window = window

    def select(self, source):
        self.window.set_source(source)

    def read(self):
        return Reading(*self.window.read_frame())


class SyntheticTimecodeSource(TimecodeSource):
    """Deterministic, for tests and the simulator (the controller spec, section
    8): each source behaves differently, so every display state can be reached
    from the picker. source = port + 1."""

    # source: (fps, nominal, drop, start label, seconds on and off, or None)
    PROFILES = {
        1: (30.0, 30, False, (1, 0, 0, 0), None),           # Analog 1
        2: (30000 / 1001, 30, True, (0, 59, 50, 0), None),  # Analog 2, across a dropped minute
        3: (25.0, 25, False, (1, 0, 0, 0), None),           # Analog 3
        4: (24000 / 1001, 24, False, (1, 0, 0, 0), None),   # Analog 4
        5: None,                                            # USB 1: never any timecode
        6: (30.0, 30, False, (2, 0, 0, 0), (5.0, 3.0)),     # USB 2: runs 5 s, stops 3 s
    }
    OTHER = (24.0, 24, False, (1, 0, 0, 0), None)

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.source = 0
        self.t0 = clock()
        self.total = 0                  # the FRAMES register: never goes back

    def select(self, source):
        self.total += 1000              # a counter that moved on, as the PL's may have
        self.source = source
        self.t0 = self.clock()

    def read(self):
        profile = self.PROFILES.get(self.source, self.OTHER) if self.source else None
        if profile is None:
            return Reading(self.total, 0, 0, 0, False, False)
        fps, nominal, drop, start, duty = profile
        elapsed = self.clock() - self.t0
        locked = True
        if duty is None:
            count = int(elapsed * fps)
        else:
            cycles, phase = divmod(elapsed, duty[0] + duty[1])
            locked = phase < duty[0]
            count = int(cycles) * int(duty[0] * fps) + int(min(phase, duty[0]) * fps)
        label = label_from_count(count_from_label(*start, nominal, drop) + count, nominal, drop)
        length = FS / fps
        samples = int((count + 1) * length) - int(count * length)
        return Reading(self.total + count, *encode_ltc(*label, drop), samples, locked, locked)


# ----- the stream -----
class _Sub:
    def __init__(self, ip, port, now):
        self.ip, self.port = ip, port
        self.expires = now + LEASE_S
        self.next_due = now             # the first tick sends
        self.seq = 0
        self.last_state = None


class TimecodeHub:
    def __init__(self, source, send, name, clock=time.monotonic, log=lambda m: None):
        """send(packet, (ip, port)); name() -> the mixer's current name."""
        self.source = source
        self.send = send
        self.name = name
        self.clock = clock
        self.log = log
        self.tracker = Tracker()
        self.generation = 0             # a read from before a select is thrown away
        self._subs = {}
        self._cond = threading.Condition()

    def select(self, source):
        """The user picked a source (0 = off)."""
        with self._cond:
            self.generation += 1
            self.source.select(source)
            self.tracker.reset(source)
            self._cond.notify()

    # ----- subscriptions (called from the TCP handlers) -----
    def subscribe(self, key, ip, port):
        """key = the TCP connection. A new key starts a subscription; the same
        key renews the lease, and a new port moves the stream. Port 0 ends it."""
        now = self.clock()
        with self._cond:
            if port == 0:
                if self._subs.pop(key, None) is not None:
                    self.log(f"timecode: {ip} unsubscribed")
                return
            sub = self._subs.get(key)
            if sub is None:
                self._subs[key] = _Sub(ip, port, now)
                self.log(f"timecode: {ip}:{port} subscribed")
            else:
                sub.ip, sub.port, sub.expires = ip, port, now + LEASE_S
            self._cond.notify()

    def drop(self, key):
        with self._cond:
            if self._subs.pop(key, None) is not None:
                self.log("timecode: subscription ended with its connection")

    def active(self):
        with self._cond:
            return len(self._subs)

    # ----- the poll -----
    def tick(self):
        now = self.clock()
        with self._cond:
            for key in [k for k, s in self._subs.items() if now >= s.expires]:
                sub = self._subs.pop(key)
                self.log(f"timecode: {sub.ip}:{sub.port} lease expired")
            if not self._subs:
                return 0
            generation = self.generation
            off = self.tracker.source == 0
        reading = None if off else self.source.read()      # outside the lock
        out = []
        with self._cond:
            if generation != self.generation:              # the source changed meanwhile
                return 0
            status, new = self.tracker.feed(reading)
            for sub in self._subs.values():
                if new or now >= sub.next_due or status.state != sub.last_state:
                    out.append(((sub.ip, sub.port), sub.seq))
                    sub.seq = (sub.seq + 1) % SEQ_MOD
                    sub.next_due = now + KEEPALIVE_S
                    sub.last_state = status.state
        name = self.name()
        for addr, seq in out:
            packet = encode_message(f"/{name}/timecode",
                                    [seq, status.state, status.hours, status.minutes, status.seconds,
                                     status.frames, float(status.fps), status.drop])
            try:
                self.send(packet, addr)
            except OSError as e:                           # display-only: never stop the poll
                self.log(f"timecode: send to {addr[0]}:{addr[1]} failed: {e}")
        return len(out)

    def run(self, stop=None):
        """The poll's thread. Sleeps while nobody subscribes."""
        period = 1.0 / POLL_HZ
        next_t = None
        while stop is None or not stop.is_set():
            with self._cond:
                while not self._subs and (stop is None or not stop.is_set()):
                    next_t = None
                    self._cond.wait(timeout=1.0)
            now = time.monotonic()
            next_t = now if next_t is None else next_t
            if next_t > now:
                time.sleep(next_t - now)
            try:
                self.tick()
            except Exception as e:                         # a display must never take the server down
                self.log(f"timecode: poll failed: {e}")
                time.sleep(0.5)
            next_t += period
            if next_t < time.monotonic():
                next_t = time.monotonic()

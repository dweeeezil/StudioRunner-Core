#!/usr/bin/env python3
"""
Unit tests for mixer_timecode.py (the timecode display): the LTC digits against
the testbenches' independent encoder (src/sim/ltc_gen.py), the frame arithmetic
against the controller spec's vectors, the rate, the tracker, the synthetic
source, and TimecodeHub with a fake clock and captured datagrams (no sockets, no
sleeps). Runs anywhere.

    python3 -m unittest -v test_mixer_timecode        (from tools/)
"""

import os
import sys
import threading
import time
import unittest

import mixer_hw
import mixer_timecode as tc
from osc_codec import decode_packet

# The encoder must be there: a test that skips itself proves nothing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "sim"))
import ltc_gen  # noqa: E402


def word_parts(bits):
    word = ltc_gen.bits_to_word(bits)
    return word & 0xFFFF_FFFF, word >> 32


class Digits(unittest.TestCase):
    def test_decodes_the_independent_encoders_frames(self):
        for nominal, drop in ((24, False), (25, False), (30, False), (30, True)):
            for label in ((0, 0, 0, 0), (1, 2, 3, 4), (23, 59, 59, nominal - 1), (10, 20, 30, 11),
                          (19, 45, 37, nominal - 2), (9, 9, 9, 9)):
                for user in (0, 0xFFFFFFFF, 0x5A3CC3A5):
                    low, high = word_parts(ltc_gen.frame_bits(*label, nominal, drop, user))
                    self.assertEqual(tc.decode_ltc(low, high), tc.Time(*label, drop), (label, user))

    def test_encode_matches_the_independent_encoder(self):
        for label in ((0, 0, 0, 0), (1, 2, 3, 4), (23, 59, 59, 29), (10, 20, 30, 11)):
            for drop in (False, True):
                low, high = tc.encode_ltc(*label, drop)
                self.assertEqual(tc.decode_ltc(low, high), tc.Time(*label, drop))

    def test_impossible_digits_are_refused(self):
        base = tc.encode_ltc(0, 0, 0, 0)
        def with_bits(pos, value):
            word = (base[1] << 32 | base[0]) | value << pos
            return word & 0xFFFF_FFFF, word >> 32
        for pos, value, what in ((0, 10, "frame units"), (16, 10, "second units"), (32, 15, "minute units"),
                                 (48, 10, "hour units"), (24, 6, "second tens"), (40, 6, "minute tens"),
                                 (8, 3, "frame tens")):
            self.assertIsNone(tc.decode_ltc(*with_bits(pos, value)), what)
        low, high = tc.encode_ltc(0, 0, 0, 0)
        self.assertIsNone(tc.decode_ltc(low, high | 2 << 24 | 4 << 16), "24 hours")

    def test_user_bits_and_flags_are_ignored(self):
        low, high = tc.encode_ltc(1, 2, 3, 4)
        flags = 1 << 11 | 1 << 27 | 1 << 43 | 1 << 58 | 1 << 59     # colour, polarity, binary group, clock
        users = sum(0xF << p for p in (4, 12, 20, 28, 36, 44, 52, 60))
        word = (high << 32 | low) | flags | users
        self.assertEqual(tc.decode_ltc(word & 0xFFFF_FFFF, word >> 32), tc.Time(1, 2, 3, 4, False))


class Labels(unittest.TestCase):
    def test_counts_walk_the_labels_in_order(self):
        for nominal, drop in ((24, False), (25, False), (30, False), (30, True)):
            t = (0, 0, 0, 0)
            for n in range(40000):
                self.assertEqual(tc.count_from_label(*t, nominal, drop), n, t)
                self.assertEqual(tc.label_from_count(n, nominal, drop), t, n)
                t = ltc_gen.next_label(*t, nominal, drop)

    def test_the_controller_specs_offset_vectors(self):
        for nominal, drop, start, offset, want in (
                (30, False, (1, 0, 0, 0), 2, (1, 0, 0, 2)),
                (30, False, (1, 0, 0, 0), -1, (0, 59, 59, 29)),
                (30, False, (23, 59, 59, 29), 1, (0, 0, 0, 0)),
                (30, False, (0, 0, 0, 0), -1, (23, 59, 59, 29)),
                (25, False, (0, 0, 0, 24), 1, (0, 0, 1, 0)),
                (24, False, (0, 0, 0, 23), 1, (0, 0, 1, 0)),
                (30, True, (0, 0, 59, 29), 1, (0, 1, 0, 2)),
                (30, True, (0, 1, 0, 2), -1, (0, 0, 59, 29)),
                (30, True, (0, 9, 59, 29), 1, (0, 10, 0, 0)),
                (30, True, (23, 59, 59, 29), 1, (0, 0, 0, 0)),
                (30, True, (0, 0, 0, 0), -1, (23, 59, 59, 29))):
            n = tc.count_from_label(*start, nominal, drop) + offset
            self.assertEqual(tc.label_from_count(n, nominal, drop), want, (start, offset))


class Rates(unittest.TestCase):
    def measured(self, lengths):
        m = tc.RateMeter()
        for n in lengths:
            m.add(n)
        return m.rate()

    def test_each_rate(self):
        for samples, fps in ((2002, 23.976), (2000, 24.0), (1920, 25.0), (1600, 30.0)):
            self.assertEqual(self.measured([samples] * tc.RATE_WINDOW).fps, fps)

    def test_2997_and_30_differ_by_a_fraction_of_a_sample(self):
        self.assertEqual(self.measured([1602, 1601, 1602, 1602, 1601] * 4).fps, 29.97)
        self.assertEqual(self.measured([1601, 1599] * 8).fps, 30.0)     # one sample of jitter

    def test_needs_a_full_window(self):
        self.assertIsNone(self.measured([1600] * (tc.RATE_WINDOW - 1)))

    def test_a_new_rate_measures_afresh(self):
        m = tc.RateMeter()
        for _ in range(tc.RATE_WINDOW):
            m.add(1600)
        m.add(1920)
        self.assertIsNone(m.rate())
        for _ in range(tc.RATE_WINDOW - 1):
            m.add(1920)
        self.assertEqual(m.rate().fps, 25.0)


def reading(n, label=(1, 0, 0, 0), samples=1600, locked=True, drop=False):
    return tc.Reading(n, *tc.encode_ltc(*label, drop), samples, locked, locked)


class TrackerCase(unittest.TestCase):
    def setUp(self):
        self.t = tc.Tracker()
        self.t.reset(3)
        self.n = 100
        self.t.feed(reading(self.n))             # the counter's starting point

    def frames(self, count, nominal=30, drop=False, samples=1600, start=(1, 0, 0, 0)):
        out = None
        c = tc.count_from_label(*start, nominal, drop)
        for i in range(count):
            self.n += 1
            out = self.t.feed(reading(self.n, tc.label_from_count(c + i, nominal, drop), samples, drop=drop))
        return out

    def test_off(self):
        self.t.reset(0)
        self.assertEqual(self.t.feed(reading(5)), (tc.Status(0, 0, 0, 0, 0, 0.0, 0), False))
        self.assertEqual(self.t.feed(None)[0].state, tc.STATE_OFF)

    def test_no_timecode_until_the_rate_is_known(self):
        st, new = self.frames(tc.RATE_WINDOW - 1)
        self.assertEqual(st, tc.Status(tc.STATE_NO_TIMECODE, 0, 0, 0, 0, 0.0, 0))
        self.assertFalse(new)

    def test_running(self):
        st, new = self.frames(tc.RATE_WINDOW)
        self.assertEqual(st, tc.Status(tc.STATE_RUNNING, 1, 0, 0, tc.RATE_WINDOW - 1, 30.0, 0))
        self.assertTrue(new)

    def test_a_reading_without_a_new_frame_is_not_new(self):
        self.frames(tc.RATE_WINDOW)
        st, new = self.t.feed(reading(self.n, (9, 9, 9, 9)))
        self.assertFalse(new)
        self.assertEqual(st.hours, 1)            # the time didn't move either

    def test_the_counter_at_selection_is_not_a_frame(self):
        self.t.reset(3)
        self.assertFalse(self.t.feed(reading(5000))[1])
        self.assertEqual(self.t.meter.history, type(self.t.meter.history)([], tc.RATE_WINDOW))

    def test_lost_lock_holds_the_time(self):
        self.frames(tc.RATE_WINDOW)
        st, new = self.t.feed(reading(self.n, locked=False))
        self.assertEqual(st, tc.Status(tc.STATE_NO_TIMECODE, 1, 0, 0, tc.RATE_WINDOW - 1, 30.0, 0))
        self.assertFalse(new)

    def test_the_same_timecode_resumes_at_once(self):
        self.frames(tc.RATE_WINDOW)
        self.t.feed(reading(self.n, locked=False))
        st, new = self.frames(1, start=(5, 0, 0, 0))
        self.assertEqual((st.state, st.hours), (tc.STATE_RUNNING, 5))
        self.assertTrue(new)

    def test_a_new_rate_goes_through_no_timecode(self):
        self.frames(tc.RATE_WINDOW)
        st, _ = self.frames(1, nominal=25, samples=1920, start=(6, 0, 0, 0))
        self.assertEqual((st.state, st.hours), (tc.STATE_NO_TIMECODE, 1))  # held
        st, _ = self.frames(tc.RATE_WINDOW, nominal=25, samples=1920, start=(6, 0, 0, 0))
        self.assertEqual((st.state, st.hours, st.fps), (tc.STATE_RUNNING, 6, 25.0))

    def test_drop_frame_only_at_2997(self):
        st, _ = self.frames(tc.RATE_WINDOW, drop=True)          # 30 fps with the flag set
        self.assertEqual((st.fps, st.drop), (30.0, 0))
        self.t.reset(3)
        self.t.feed(reading(self.n))
        for i in range(tc.RATE_WINDOW):
            self.n += 1
            st, _ = self.t.feed(reading(self.n, (1, 0, 0, 2), [1602, 1601, 1602, 1602, 1601][i % 5], drop=True))
        self.assertEqual((st.fps, st.drop), (29.97, 1))

    def test_a_frame_number_the_rate_cant_have_is_ignored(self):
        self.frames(tc.RATE_WINDOW, nominal=25, samples=1920)
        self.n += 1
        st, new = self.t.feed(reading(self.n, (7, 0, 0, 27), 1920))
        self.assertFalse(new)
        self.assertEqual(st.hours, 1)

    def test_impossible_digits_are_ignored(self):
        self.frames(tc.RATE_WINDOW)
        self.n += 1
        st, new = self.t.feed(tc.Reading(self.n, 0xF, 0, 1600, True, True))
        self.assertFalse(new)
        self.assertEqual(st.state, tc.STATE_RUNNING)

    def test_a_new_source_forgets_the_old_one(self):
        self.frames(tc.RATE_WINDOW)
        self.t.reset(4)
        st, _ = self.t.feed(reading(self.n))
        self.assertEqual(st, tc.Status(tc.STATE_NO_TIMECODE, 0, 0, 0, 0, 0.0, 0))

    def test_a_new_source_measures_its_own_rate(self):
        self.frames(tc.RATE_WINDOW)
        self.t.reset(4)
        self.t.feed(reading(self.n))
        st, new = self.frames(1)
        self.assertEqual((st.state, st.fps, new), (tc.STATE_NO_TIMECODE, 0.0, False))


class Synthetic(unittest.TestCase):
    """Each source as the controller spec's table says, through the tracker."""

    def run_source(self, source, seconds):
        self.now = 10.0
        src = tc.SyntheticTimecodeSource(clock=lambda: self.now)
        t = tc.Tracker()
        src.select(source)
        t.reset(source)
        st, states = None, []
        while self.now < 10.0 + seconds:
            st, _new = t.feed(src.read() if source else None)
            states.append(st)
            self.now += 1.0 / tc.POLL_HZ
        return st, states

    def test_table(self):
        for source, fps, drop, hour in ((1, 30.0, 0, 1), (2, 29.97, 1, 0), (3, 25.0, 0, 1), (4, 23.976, 0, 1),
                                        (7, 24.0, 0, 1), (20, 24.0, 0, 1)):
            st, _ = self.run_source(source, 2.0)
            self.assertEqual((st.state, st.fps, st.drop, st.hours), (tc.STATE_RUNNING, fps, drop, hour), source)

    def test_off(self):
        self.assertEqual(self.run_source(0, 0.5)[0].state, tc.STATE_OFF)

    def test_usb1_never_has_timecode(self):
        st, states = self.run_source(5, 2.0)
        self.assertEqual(st, tc.Status(tc.STATE_NO_TIMECODE, 0, 0, 0, 0, 0.0, 0))
        self.assertEqual({s.state for s in states}, {tc.STATE_NO_TIMECODE})

    def test_usb2_runs_and_stops(self):
        running = self.run_source(6, 4.5)[0]
        self.assertEqual(running.state, tc.STATE_RUNNING)
        stopped = self.run_source(6, 6.5)[0]
        self.assertEqual(stopped.state, tc.STATE_NO_TIMECODE)
        self.assertEqual((stopped.hours, stopped.seconds), (2, 4))       # held
        self.assertEqual(self.run_source(6, 9.0)[0].state, tc.STATE_RUNNING)

    def test_analog2_crosses_a_dropped_minute(self):
        st, states = self.run_source(2, 11.0)
        labels = {(s.minutes, s.seconds, s.frames) for s in states if s.state == tc.STATE_RUNNING}
        self.assertIn((59, 59, 29), labels)
        self.assertEqual((st.hours, st.minutes), (1, 0))                 # 01:00 keeps ;00 and ;01: tens of minutes
        self.assertIn((0, 0, 0), labels)
        odd = [s for s in states if s.state == tc.STATE_RUNNING and (s.minutes, s.seconds) == (1, 0)]
        self.assertEqual(odd, [])


class FakeSource(tc.TimecodeSource):
    def __init__(self):
        self.reading = reading(100)
        self.selected = []
        self.reads = 0
        self.during_read = None

    def select(self, source):
        self.selected.append(source)

    def read(self):
        self.reads += 1
        if self.during_read:
            self.during_read()
        return self.reading


class Hub(unittest.TestCase):
    A = ("10.0.0.5", 9000)

    def setUp(self):
        self.now = 100.0
        self.sent = []
        self.src = FakeSource()
        self.name = "FOH"
        self.hub = tc.TimecodeHub(self.src, lambda pkt, addr: self.sent.append((pkt, addr)),
                                  lambda: self.name, clock=lambda: self.now)
        self.n = 100

    def msgs(self, addr=None):
        out = []
        for pkt, to in self.sent:
            if addr is None or to == addr:
                out.append(decode_packet(pkt)[0])
        return out

    def play(self, count, dt=1 / 30, start=(1, 0, 0, 0)):
        c = tc.count_from_label(*start, 30)
        for i in range(count):
            self.n += 1
            self.src.reading = reading(self.n, tc.label_from_count(c + i, 30))
            self.hub.tick()
            self.now += dt

    def running(self, addr=A):
        self.hub.select(1)
        self.hub.subscribe("k", *addr)
        self.hub.tick()
        self.play(tc.RATE_WINDOW)
        self.sent.clear()

    def test_the_message(self):
        self.running()
        self.play(1, start=(1, 2, 3, 4))
        m = self.msgs()[0]
        self.assertEqual(m.address, "/FOH/timecode")
        self.assertEqual(m.args[1:6], [tc.STATE_RUNNING, 1, 2, 3, 4])
        self.assertAlmostEqual(m.args[6], 30.0)
        self.assertEqual(m.args[7], 0)
        self.assertEqual(len(m.args), 8)
        self.assertTrue(all(isinstance(v, int) for v in m.args[:6] + m.args[7:]))
        self.assertIsInstance(m.args[6], float)

    def test_the_address_follows_a_rename(self):
        self.running()
        self.name = "Stage"
        self.play(1)
        self.assertEqual(self.msgs()[0].address, "/Stage/timecode")

    def test_one_message_per_frame_however_often_it_polls(self):
        self.running()
        for _ in range(5):
            self.play(1, dt=1 / 60)
            self.hub.tick()
            self.now += 1 / 60
        self.assertEqual(len(self.sent), 5)

    def test_keepalive_when_nothing_changes(self):
        self.hub.select(1)
        self.hub.subscribe("k", *self.A)
        self.hub.tick()
        self.assertEqual(len(self.sent), 1)                  # the first tick sends
        self.now += tc.KEEPALIVE_S - 0.01
        self.hub.tick()
        self.assertEqual(len(self.sent), 1)
        self.now += 0.02
        self.hub.tick()
        self.assertEqual(len(self.sent), 2)

    def test_a_state_change_goes_out_at_once(self):
        self.running()
        self.src.reading = reading(self.n, locked=False)
        self.now += 0.001
        self.hub.tick()
        self.assertEqual(self.msgs()[0].args[1], tc.STATE_NO_TIMECODE)

    def test_off_never_touches_the_source(self):
        self.hub.subscribe("k", *self.A)
        self.hub.tick()
        self.assertEqual(self.src.reads, 0)
        self.assertEqual(self.msgs()[0].args[1:], [tc.STATE_OFF, 0, 0, 0, 0, 0.0, 0])

    def test_nobody_subscribed_nothing_read(self):
        self.hub.select(1)
        self.assertEqual(self.hub.tick(), 0)
        self.assertEqual(self.src.reads, 0)

    def test_sequences_are_per_subscriber_and_wrap(self):
        self.hub.select(1)
        self.hub.subscribe("a", "10.0.0.5", 9000)
        self.hub.tick()
        self.now += tc.KEEPALIVE_S
        self.hub.tick()
        self.hub.subscribe("b", "10.0.0.6", 9001)
        self.now += tc.KEEPALIVE_S
        self.hub.tick()
        self.assertEqual([m.args[0] for m in self.msgs(("10.0.0.5", 9000))], [0, 1, 2])
        self.assertEqual([m.args[0] for m in self.msgs(("10.0.0.6", 9001))], [0])
        self.hub._subs["a"].seq = tc.SEQ_MOD - 1
        self.sent.clear()
        for _ in range(2):
            self.now += tc.KEEPALIVE_S
            self.hub.tick()
        self.assertEqual([m.args[0] for m in self.msgs(("10.0.0.5", 9000))], [tc.SEQ_MOD - 1, 0])

    def test_the_lease(self):
        self.hub.subscribe("k", *self.A)
        self.now += tc.LEASE_S - 1
        self.hub.subscribe("k", *self.A)                     # renewed
        self.now += tc.LEASE_S - 1
        self.hub.tick()
        self.assertEqual(self.hub.active(), 1)
        self.now += 1.01
        self.hub.tick()
        self.assertEqual(self.hub.active(), 0)

    def test_a_new_port_moves_the_stream_and_the_sequence_goes_on(self):
        self.hub.subscribe("k", *self.A)
        self.hub.tick()
        self.hub.subscribe("k", "10.0.0.5", 9500)
        self.now += tc.KEEPALIVE_S
        self.hub.tick()
        self.assertEqual(self.hub.active(), 1)
        self.assertEqual(self.sent[-1][1], ("10.0.0.5", 9500))
        self.assertEqual(self.msgs()[-1].args[0], 1)

    def test_port_0_and_drop_end_it(self):
        self.hub.subscribe("k", *self.A)
        self.hub.subscribe("k", "10.0.0.5", 0)
        self.assertEqual(self.hub.active(), 0)
        self.hub.subscribe("k", *self.A)
        self.hub.drop("k")
        self.assertEqual(self.hub.active(), 0)

    def test_select_reaches_the_source_and_forgets_the_time(self):
        self.running()
        self.hub.select(4)
        self.assertEqual(self.src.selected[-1], 4)
        self.now += 0.001
        self.hub.tick()
        self.assertEqual(self.msgs()[-1].args[1:6], [tc.STATE_NO_TIMECODE, 0, 0, 0, 0])

    def test_a_read_from_before_a_select_is_thrown_away(self):
        self.running()
        self.n += 1
        self.src.reading = reading(self.n, (9, 0, 0, 0))
        self.src.during_read = lambda: self.hub.select(2)
        self.assertEqual(self.hub.tick(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.hub.tracker.time, (0, 0, 0, 0))

    def test_a_failed_send_doesnt_stop_the_others(self):
        calls = []
        def send(pkt, addr):
            calls.append(addr)
            if len(calls) == 1:
                raise OSError("no route")
        self.hub.send = send
        self.hub.subscribe("a", "10.0.0.5", 9000)
        self.hub.subscribe("b", "10.0.0.6", 9001)
        self.assertEqual(self.hub.tick(), 2)
        self.assertEqual(len(calls), 2)

    def test_a_failed_read_doesnt_end_the_thread(self):
        class Flaky(FakeSource):
            def read(inner):
                inner.reads += 1
                if inner.reads == 1:
                    raise RuntimeError("the PL stopped answering")
                return inner.reading
        src = Flaky()
        got = threading.Event()
        hub = tc.TimecodeHub(src, lambda pkt, addr: got.set(), lambda: "FOH")
        hub.select(1)
        hub.subscribe("k", *self.A)
        stop = threading.Event()
        t = threading.Thread(target=hub.run, args=(stop,), daemon=True)
        t.start()
        self.assertTrue(got.wait(3.0))
        stop.set()
        hub.drop("k")
        t.join(3.0)
        self.assertGreaterEqual(src.reads, 2)


class FakeWindow(mixer_hw.TimecodeHW):
    """The register logic without /dev/mem."""

    def __init__(self):
        self.regs = {self.REG_FRAMES: 7, self.REG_LOW: 0x11, self.REG_HIGH: 0x22,
                     self.REG_FRAME_SAMPLES: 1600, self.REG_FLAGS: 3, self.REG_SOURCE: 0}
        self.on_read = None

    def rd(self, off):
        if self.on_read:
            self.on_read(off)
        return self.regs[off]

    def wr(self, off, val):
        self.regs[off] = val


class HardwareWindow(unittest.TestCase):
    def test_the_map(self):
        base, cls = mixer_hw.WINDOWS["timecode"]
        self.assertEqual((base, cls), (0x8001_1000, mixer_hw.TimecodeHW))
        self.assertEqual(cls.ID, 0x5443_5001)
        self.assertEqual([cls.REG_SOURCE, cls.REG_LOW, cls.REG_HIGH, cls.REG_FRAMES, cls.REG_FRAME_SAMPLES,
                          cls.REG_FLAGS, cls.REG_SNAPSHOTS],
                         [0x100, 0x104, 0x108, 0x10C, 0x110, 0x114, 0x118])

    def test_a_reading(self):
        src = tc.HardwareTimecodeSource(FakeWindow())
        self.assertEqual(src.read(), tc.Reading(7, 0x11, 0x22, 1600, True, True))

    def test_flags(self):
        w = FakeWindow()
        w.regs[w.REG_FLAGS] = 1
        self.assertEqual(w.read_frame()[4:], (True, False))
        w.regs[w.REG_FLAGS] = 2
        self.assertEqual(w.read_frame()[4:], (False, True))

    def test_select_writes_source(self):
        w = FakeWindow()
        tc.HardwareTimecodeSource(w).select(13)
        self.assertEqual(w.regs[w.REG_SOURCE], 13)

    def test_a_frame_landing_mid_read_is_read_again(self):
        w = FakeWindow()
        def land(off):
            if off == w.REG_HIGH and w.regs[w.REG_FRAMES] == 7:      # after LOW, before the second FRAMES
                w.regs.update({w.REG_FRAMES: 8, w.REG_LOW: 0x33, w.REG_HIGH: 0x44})
        w.on_read = land
        self.assertEqual(w.read_frame()[:3], (8, 0x33, 0x44))


if __name__ == "__main__":
    unittest.main()

import threading
import time

import pytest

from a3plot.grbl import GrblStreamer
from fake_grbl import FakeGrbl


def make_lines(n):
    out = ["G21", "G90", "M3S25"]
    for i in range(n):
        out.append(f"G1X{i % 200}.{i % 97:02d}Y{(i * 7) % 150}.5F400")
    out.append("M5")
    return out


class Recorder:
    def __init__(self):
        self.events = []
        self.done = threading.Event()
        self.connected = threading.Event()

    def __call__(self, name, data):
        self.events.append((name, data))
        if name == "connected":
            self.connected.set()
        if name == "job_state" and data in ("finished", "aborted", "failed"):
            self.done.set()

    def states(self):
        return [d for n, d in self.events if n == "job_state"]


def run_job(fake, lines, timeout=20, **kw):
    rec = Recorder()
    s = GrblStreamer(lambda: fake, rec, poll_interval=0.05, **kw)
    s.start()
    assert rec.connected.wait(5)
    s.start_job(lines)
    assert rec.done.wait(timeout), rec.events[-10:]
    s.stop()
    return rec


@pytest.mark.parametrize("mode,budget", [("safe", 64), ("count", 64), ("count", 127)])
def test_all_lines_delivered_in_order_without_overflow(mode, budget):
    fake = FakeGrbl(block_time=0.0005)
    lines = make_lines(600)
    rec = run_job(fake, lines, mode=mode, budget=budget)
    assert rec.states()[-1] == "finished"
    assert fake.overflow == 0
    job_received = [l for l in fake.received if l != "$$"]
    assert job_received == lines
    assert fake.executed == lines


def test_count_mode_never_exceeds_budget():
    fake = FakeGrbl(block_time=0.001)
    max_seen = 0
    orig_write = fake.write

    def write(data):
        nonlocal max_seen
        r = orig_write(data)
        max_seen = max(max_seen, len(fake.rx))
        return r

    fake.write = write
    run_job(fake, make_lines(300), mode="count", budget=64)
    assert max_seen <= 64
    assert fake.overflow == 0


def test_error_line_is_resent_in_safe_mode():
    fake = FakeGrbl(block_time=0.0005, corrupt={10})
    lines = make_lines(50)
    rec = run_job(fake, lines, mode="safe")
    assert rec.states()[-1] == "finished"
    # Zeile 10 kam zweimal an, ausgeführt wurde jede Zeile genau einmal, in Reihenfolge
    assert fake.executed == lines
    assert ("repeats", 1) in rec.events


def test_error_in_count_mode_holds_and_asks():
    fake = FakeGrbl(block_time=0.0005, corrupt={10})
    rec = Recorder()
    s = GrblStreamer(lambda: fake, rec, mode="count", poll_interval=0.05)
    s.start()
    assert rec.connected.wait(5)
    s.start_job(make_lines(50))
    t0 = time.time()
    while "holding" not in rec.states() and time.time() - t0 < 5:
        time.sleep(0.01)
    assert "holding" in rec.states()
    assert any(n == "job_problem" for n, _ in rec.events)
    s.skip_failed()
    assert rec.done.wait(10)
    assert rec.states()[-1] == "finished"
    s.stop()


def test_lost_ok_is_detected_and_line_resent():
    fake = FakeGrbl(block_time=0.0005, drop_ok={20})
    lines = make_lines(40)
    rec = run_job(fake, lines, mode="safe", idle_timeout=0.5)
    assert rec.states()[-1] == "finished"
    assert any(n == "repeats" for n, _ in rec.events)
    # Zeile 20 wurde doppelt gesendet; wegen absoluter Koordinaten harmlos
    assert fake.received.count(lines[20]) == 2


def test_unexpected_reset_fails_job():
    fake = FakeGrbl(block_time=0.01)
    rec = Recorder()
    s = GrblStreamer(lambda: fake, rec, mode="safe", poll_interval=0.05)
    s.start()
    assert rec.connected.wait(5)
    s.start_job(make_lines(500))
    time.sleep(0.3)
    with fake.lock:
        fake.planner.clear()
        fake.tx += b"\r\nGrbl 0.9i ['$' for help]\r\n"
    assert rec.done.wait(5)
    assert rec.states()[-1] == "failed"
    s.stop()


def test_soft_pause_stops_at_pause_point_and_resumes():
    fake = FakeGrbl(block_time=0.002)
    lines = make_lines(200)
    rec = Recorder()
    s = GrblStreamer(lambda: fake, rec, mode="safe", poll_interval=0.05)
    s.start()
    assert rec.connected.wait(5)
    s.start_job(lines, pause_points={100})
    s.pause_soft()
    t0 = time.time()
    while "soft_paused" not in rec.states() and time.time() - t0 < 5:
        time.sleep(0.01)
    time.sleep(0.3)
    assert [l for l in fake.received if l != "$$"] == lines[:101]
    s.resume(["G90"])
    assert rec.done.wait(10)
    assert [l for l in fake.received if l not in ("$$", "G90")][-1] == "M5"
    s.stop()


def test_abort_holds_then_resets():
    fake = FakeGrbl(block_time=0.02)
    rec = Recorder()
    s = GrblStreamer(lambda: fake, rec, mode="safe", poll_interval=0.05)
    s.start()
    assert rec.connected.wait(5)
    s.start_job(make_lines(500))
    time.sleep(0.3)
    s.abort()
    assert rec.done.wait(5)
    assert rec.states()[-1] == "aborted"
    assert len(fake.executed) < 500
    s.stop()

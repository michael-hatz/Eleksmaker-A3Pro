import threading
import time

import pytest

from a3plot.grbl import GrblStreamer, Inflight
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
    job_received = [l for l in fake.received if l not in ("$$", "$#")]
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
    assert [l for l in fake.received if l not in ("$$", "$#")] == lines[:101]
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


def _offline_streamer():
    rec = Recorder()
    return GrblStreamer(lambda: None, rec), rec


def test_job_finishes_when_last_ok_belongs_to_manual_command():
    # Letzte Plot-Zeile bestätigt, während ein Konsolenbefehl noch unterwegs ist:
    # das ok des Befehls muss den Plot beenden.
    s, rec = _offline_streamer()
    s.job, s.job_next, s.job_active = ["G1X1Y1"], 1, True
    s.inflight.extend([Inflight("G1X1Y1", 7, "job", 0, 0.0), Inflight("G4P0", 5, "cmd", -1, 0.0)])
    s._handle("ok")
    assert rec.states() == []
    s._handle("ok")
    assert rec.states() == ["finished"]
    assert not s.job_active


def test_watchdog_resends_job_lines_queued_behind_lost_settings_query():
    s, rec = _offline_streamer()
    s.job, s.job_next, s.job_active = ["G1X1Y1"] * 10, 8, True
    s.state, s.last_status_at, s.last_rx_at = "Idle", 100.0, time.monotonic()
    s.inflight.extend([Inflight("$$", 3, "cmd", -1, 0.0), Inflight("G1X1Y1", 7, "job", 6, 0.0),
                       Inflight("G1X1Y1", 7, "job", 7, 0.0)])
    s._watchdog()
    assert s.job_next == 6
    assert ("repeats", 2) in rec.events
    assert not s.inflight


def test_status_prefers_work_position():
    s, rec = _offline_streamer()
    s._handle("<Idle,MPos:10.000,20.000,0.000,WPos:7.000,8.000,0.000>")
    assert s.mpos == (7.0, 8.0)
    s._handle("<Idle,MPos:10.000,20.000,0.000>")       # $10=1: nur MPos
    assert s.mpos == (10.0, 20.0)


@pytest.mark.parametrize("g54,warned", [((0.0, 0.0), False), ((5.0, 0.0), True)])
def test_connect_warns_about_work_offset(g54, warned):
    fake = FakeGrbl(block_time=0.0005, g54=g54)
    rec = Recorder()
    s = GrblStreamer(lambda: fake, rec, poll_interval=0.05)
    s.start()
    assert rec.connected.wait(5)
    t0 = time.time()
    while "$#" not in fake.received and time.time() - t0 < 2:
        time.sleep(0.01)
    time.sleep(0.2)
    logs = [t for n, (k, t) in ((n, d) for n, d in rec.events if n == "log")]
    # die automatische Abfrage beim Verbinden erscheint nicht im Log …
    assert not any("[G55" in t for t in logs)
    # … ein selbst getipptes $# dagegen vollständig
    s.command("$#")
    t0 = time.time()
    while not any("$# -> ok" in t for n, (k, t) in ((n, d) for n, d in rec.events if n == "log")) \
            and time.time() - t0 < 2:
        time.sleep(0.01)
    s.stop()
    logs = [t for n, (k, t) in ((n, d) for n, d in rec.events if n == "log")]
    assert "[G55:0.000,0.000,0.000]" in logs and "$# -> ok" in logs
    warns = [t for n, (k, t) in ((n, d) for n, d in rec.events if n == "log") if k == "warn"]
    assert any("G54" in t and "G10 L2 P1 X0 Y0" in t for t in warns) == warned

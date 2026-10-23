"""Robot library for tcp-cc-lab: thin keywords over tools/cclab.py (ssh to the VMs, the WAN emulator, iperf3 flows)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import cclab as C  # noqa: E402

ROBOT_LIBRARY_SCOPE = "GLOBAL"


def lab_inventory(): return C.inventory()


def node_names(role=None): return [n["name"] for n in C.inventory()["nodes"] if role in (None, n["role"])]


def node_info(name): return C.node(name)


def run_on(node, cmd, expect_rc="0", timeout=60):
    """Run a command on a node; fails unless the exit code is expect_rc ("any" accepts all). Returns stdout, stripped."""
    rc, out = C.run(node, cmd, timeout=int(timeout))
    if expect_rc != "any" and rc != int(expect_rc): raise AssertionError(f"{node}: '{cmd}' exited {rc}: {out[-300:]}")
    return out.strip()


def wan_set(rtt_ms=None, rate_mbit=None, loss_pct=None, buffer_bdp=None):
    f = lambda v: None if v in (None, "", "None") else float(v)   # noqa: E731
    return C.wan_apply(f(rtt_ms), f(rate_mbit), f(loss_pct), f(buffer_bdp))


def wan_reset(): return C.wan_reset()


def wan_stats(): return C.wan_stats()


def ping_rtt(src="a-cubic", dst="b-cubic", count=10): return C.ping_rtt(src, dst, int(count))


def ping_loss(src, dst, count=200, interval=0.05):
    """Percent of pings lost (every ping crosses the emulator twice)."""
    out = run_on(src, f"ping -c {int(count)} -i {interval} -W 2 -q {C.node(dst)['ip']}", expect_rc="any", timeout=int(count) * float(interval) + 30)
    for l in out.splitlines():
        if "packet loss" in l: return float(l.split("%")[0].split()[-1])
    raise AssertionError(f"no ping summary: {out[-200:]}")


def run_flows(mode, duration=20, direction="a2b", algos="cubic,bbr,reno", stagger=0):
    """compete | solo -> the cclab summary (flows: [{cc, cc_reported, mbps, retransmits, rtt_ms {mean..}, error}], total_mbps, ...)."""
    return C.flows(mode, int(duration), direction, [a.strip() for a in algos.split(",")], float(stagger))


def flow_of(result, cc):
    return next(f for f in result["flows"] if f["cc"] == cc)


def flow_with_rtt_change(cc="cubic", duration=20, at=6, new_rtt=120):
    """One flow; at `at` s the emulator's RTT changes in place. Returns (summary, samples) — the flow must survive it."""
    import threading, time
    h = next(n for n in C.inventory()["nodes"] if n["role"] == "host" and n["site"] == "a" and n["cc"] == cc)
    t0 = time.time(); f = C.Flow(h, int(duration), t0); f.start()
    time.sleep(float(at)); C.wan_apply(rtt_ms=float(new_rtt)); f.join(int(duration) + 60)
    return {"summary": f.summary(), "samples": f.samples, "changed_at": float(at)}

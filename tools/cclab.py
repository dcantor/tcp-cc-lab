#!/usr/bin/env python3
"""tcp-cc-lab measurements: TCP flows from the hosts of one site to their peers across the emulated WAN, one host per
congestion control algorithm (CUBIC / BBR / Reno), and the WAN emulator's settings. Stdlib only (ssh to the VMs).

    cclab.py compete [--duration 30] [--direction a2b|b2a] [--algos cubic,bbr,reno] [--stagger 0]
                     all flows at once through the shared bottleneck: who gets what
    cclab.py solo    [--duration 20] [--direction a2b|b2a] [--algos ...]   one algorithm at a time, the link to itself
    cclab.py sweep   [--rtts 0,10,30,60,100,150] [--rate 500] [--duration 20] [--bitrate 500M] [--direction a2b|b2a] [--algos ...]
                     each algorithm alone at each RTT, the bottleneck at --rate; the WAN goes back to its settings after
    cclab.py rtt     [--count 10]      ping RTT a-cubic -> b-cubic (the emulated RTT + the VMs' own ~1 ms)
    cclab.py wan     [--rtt MS] [--rate MBIT] [--loss PCT] [--buffer BDP] [--reset]   show / change the WAN emulator
    cclab.py mtu     [--set 1500|9000]   the hosts' MTU (all six), and r1 / r2 / wanem's links (always 9000)

A flow run prints one line per flow per second on stderr while it runs,
    SAMPLE {"t": 3, "ts": 1792720728.4, "cc": "bbr", "host": "a-bbr", "mbps": 41.2, "rtt_ms": 33.1, "cwnd": 412000, "retr": 0}
(the portal draws its live chart from them), and the summary as JSON on stdout."""
import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5"]
ALGOS = ("cubic", "bbr", "reno")
_inv = None


def inventory():
    global _inv
    if _inv is None:
        out = subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout
        _inv = json.loads(out); _inv["by_name"] = {n["name"]: n for n in _inv["nodes"]}
    return _inv


def node(name): return inventory()["by_name"][name]


def run(name, cmd, timeout=60):
    """Run a shell command on a node; returns (rc, stdout)."""
    n = node(name)
    r = subprocess.run([*SSH, f"{n['user']}@{n['mgmt_ip']}", cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout


def log(*a): print(*a, file=sys.stderr, flush=True)


# ---- WAN emulator ----------------------------------------------------------------------------------------------------
LIMITS = {"rtt_ms": (0, 300), "rate_mbit": (1, 1000), "loss_pct": (0, 20), "buffer_bdp": (0.05, 16)}


def wan_stats():
    """The emulator's settings and both directions' qdisc counters (eth2 = towards r2 = site A -> B traffic)."""
    rc, out = run("wanem", "doas wanem stats", timeout=15)
    if rc: raise RuntimeError("wanem stats failed")
    d = json.loads(out); s = d.get("settings", {})
    res = {"settings": {"rtt_ms": s.get("RTT_MS"), "rate_mbit": s.get("RATE_MBIT"), "loss_pct": s.get("LOSS_PCT"), "buffer_bdp": s.get("BUFFER_BDP"),
                        "one_way_us": s.get("ONE_WAY_US"), "queue_bytes": s.get("QUEUE_BYTES"), "bdp_bytes": s.get("BDP_BYTES"), "applied": s.get("APPLIED")}}
    for dev, direction in (("eth2", "a2b"), ("eth1", "b2a")):
        q = {x.get("kind"): x for x in d.get(dev, [])}
        t, ne = q.get("tbf", {}), q.get("netem", {})
        res[direction] = {"bytes": t.get("bytes", 0), "packets": t.get("packets", 0), "drops": t.get("drops", 0),
                          "backlog_bytes": t.get("backlog", 0), "backlog_packets": t.get("qlen", 0),
                          "netem_drops": max(0, ne.get("drops", 0) - t.get("drops", 0)), "delayed_packets": ne.get("qlen", 0)}
    return res


def wan_apply(rtt_ms=None, rate_mbit=None, loss_pct=None, buffer_bdp=None):
    """Change any of the four settings (the others stay as they are); returns the new stats."""
    cur = wan_stats()["settings"]; new = {}
    for k, v in (("rtt_ms", rtt_ms), ("rate_mbit", rate_mbit), ("loss_pct", loss_pct), ("buffer_bdp", buffer_bdp)):
        v = cur[k] if v is None else v
        lo, hi = LIMITS[k]
        if v is None or not (lo <= float(v) <= hi): raise ValueError(f"{k} must be between {lo} and {hi}")
        new[k] = float(v)
    fmt = lambda x: f"{x:g}"   # noqa: E731
    rc, out = run("wanem", f"doas wanem apply {fmt(new['rtt_ms'])} {fmt(new['rate_mbit'])} {fmt(new['loss_pct'])} {fmt(new['buffer_bdp'])}", timeout=15)
    if rc: raise RuntimeError(f"wanem apply failed: {out[-300:]}")
    return wan_stats()


def wan_reset():
    rc, out = run("wanem", "doas wanem reset", timeout=15)
    if rc: raise RuntimeError("wanem reset failed")
    return wan_stats()


# ---- MTU -------------------------------------------------------------------------------------------------------------
HOST_MTUS = (1500, 9000)
INFRA_MTU = 9000
INFRA_LINKS = {"r1": ("eth1", "eth2"), "r2": ("eth1", "eth2"), "wanem": ("eth1", "eth2", "br0")}


def _parallel(fn, items):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max(1, len(items))) as ex: return dict(zip(items, ex.map(fn, items)))


def mtu_state():
    """{"hosts": {host: mtu}, "infra": {node: {dev: mtu}}, "host_mtu": the common value or None if mixed, "infra_ok": bool}."""
    hosts = [n["name"] for n in inventory()["nodes"] if n["role"] == "host"]
    def host(h):
        rc, out = run(h, "cat /sys/class/net/eth1/mtu", timeout=10); return int(out) if rc == 0 and out.strip().isdigit() else None
    def infra(n):
        rc, out = run(n, " ; ".join(f"cat /sys/class/net/{d}/mtu" for d in INFRA_LINKS[n]), timeout=10)
        vals = [int(x) if x.isdigit() else None for x in out.split()]
        return dict(zip(INFRA_LINKS[n], vals + [None] * (len(INFRA_LINKS[n]) - len(vals))))
    hs = _parallel(host, hosts); inf = _parallel(infra, list(INFRA_LINKS))
    vals = set(hs.values())
    return {"hosts": hs, "host_mtu": vals.pop() if len(vals) == 1 else None, "choices": list(HOST_MTUS),
            "infra": inf, "infra_mtu": INFRA_MTU, "infra_ok": all(v == INFRA_MTU for d in inf.values() for v in d.values())}


def mtu_set(mtu):
    mtu = int(mtu)
    if mtu not in HOST_MTUS: raise ValueError(f"MTU must be one of {HOST_MTUS}")
    r = subprocess.run([str(LAB / "lab.sh"), "mtu", "set", str(mtu)], capture_output=True, text=True, timeout=60)
    st = mtu_state()
    if r.returncode or st["host_mtu"] != mtu: raise RuntimeError(f"not every host took MTU {mtu}: {st['hosts']} {r.stdout[-200:]}")
    return st


# ---- measurements ----------------------------------------------------------------------------------------------------
def ping_rtt(src="a-cubic", dst="b-cubic", count=10):
    rc, out = run(src, f"ping -c {count} -i 0.2 -W 2 -q {node(dst)['ip']}", timeout=count + 15)
    for l in out.splitlines():
        if "min/avg/max" in l:
            mn, avg, mx = (float(x) for x in l.split("=")[1].split()[0].split("/")[:3])
            return {"src": src, "dst": dst, "min_ms": mn, "avg_ms": avg, "max_ms": mx}
    raise RuntimeError(f"ping {src} -> {dst} failed")


def senders(direction, algos):
    site = "a" if direction == "a2b" else "b"
    hs = [n for n in inventory()["nodes"] if n["role"] == "host" and n["site"] == site and n["cc"] in algos]
    return sorted(hs, key=lambda n: algos.index(n["cc"]))


class Flow(threading.Thread):
    """One iperf3 client on a sender host, to its peer's server, --json-stream parsed as it arrives."""

    def __init__(self, sender, duration, t0, delay=0.0, bitrate=None):
        super().__init__(daemon=True)
        self.h, self.duration, self.t0, self.delay, self.bitrate = sender, duration, t0, delay, bitrate
        self.samples, self.end, self.error, self.actual_cc, self.mss = [], None, None, None, None

    def run(self):
        time.sleep(self.delay)
        peer = node(self.h["peer"])["ip"]
        cmd = f"iperf3 -c {peer} -p 5201 -t {self.duration} -i 1 --json-stream -f m" + (f" -b {self.bitrate}" if self.bitrate else "")
        p = subprocess.Popen([*SSH, f"lab@{self.h['mgmt_ip']}", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for l in p.stdout:
                try: ev = json.loads(l)
                except ValueError: continue
                kind, data = ev.get("event"), ev.get("data") or {}
                if kind == "start":
                    self.actual_cc = (data.get("test_start") or {}).get("congestion") or data.get("sender_tcp_congestion")
                    self.mss = data.get("tcp_mss_default")
                elif kind == "interval":
                    st = (data.get("streams") or [{}])[0]; su = data.get("sum") or {}
                    now = time.time(); s = {"t": round(now - self.t0), "ts": round(now, 2), "cc": self.h["cc"], "host": self.h["name"],
                         "mbps": round(su.get("bits_per_second", st.get("bits_per_second", 0)) / 1e6, 2),
                         "rtt_ms": round(st.get("rtt", 0) / 1000, 2), "cwnd": st.get("snd_cwnd", 0), "retr": st.get("retransmits", 0)}
                    self.samples.append(s); log("SAMPLE " + json.dumps(s))
                elif kind == "end":
                    self.end = data
                elif kind == "error":
                    self.error = str(data)
        finally:
            p.wait(timeout=self.duration + 30)
            if p.returncode and not self.error: self.error = (p.stderr.read() or f"iperf3 rc={p.returncode}").strip()[-300:]

    def summary(self):
        e = self.end or {}; snt = (e.get("sum_sent") or {}); rcv = (e.get("sum_received") or {})
        st = ((e.get("streams") or [{}])[0].get("sender") or {})
        rtts = [s["rtt_ms"] for s in self.samples if s["rtt_ms"]]
        return {"host": self.h["name"], "cc": self.h["cc"], "bitrate": self.bitrate, "cc_reported": (e.get("sender_tcp_congestion") or self.actual_cc),
                "mss": self.mss, "mbps": round(rcv.get("bits_per_second", 0) / 1e6, 2), "sent_mbps": round(snt.get("bits_per_second", 0) / 1e6, 2),
                "retransmits": snt.get("retransmits", 0), "rtt_ms": {"mean": round(st.get("mean_rtt", 0) / 1000, 2), "min": round(st.get("min_rtt", 0) / 1000, 2),
                "max": round(st.get("max_rtt", 0) / 1000, 2), "p50_samples": sorted(rtts)[len(rtts) // 2] if rtts else None},
                "error": self.error}


def jain(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)), 3) if xs and any(xs) else None


def flows(mode, duration, direction, algos, stagger=0.0, bitrate=None):
    wan = wan_stats(); hs = senders(direction, algos); mtu = mtu_state()
    if not hs: raise SystemExit("no senders for those algorithms")
    log(f"{mode}: {', '.join(h['name'] + ' -> ' + h['peer'] for h in hs)}; WAN RTT {wan['settings']['rtt_ms']} ms, "
        f"{wan['settings']['rate_mbit']} Mbit/s, loss {wan['settings']['loss_pct']} %, buffer {wan['settings']['buffer_bdp']} x BDP; host MTU {mtu['host_mtu'] or 'mixed'}; {duration} s each")
    before = wan_stats()[direction]; results = []
    t0 = time.time()
    if mode == "compete":
        fs = [Flow(h, duration, t0, i * stagger, bitrate) for i, h in enumerate(hs)]
        for f in fs: f.start()
        for f in fs: f.join(duration + stagger * len(fs) + 60)
        results = [f.summary() for f in fs]
    else:
        for h in hs:
            log(f"--- {h['cc']} alone ({h['name']})"); f = Flow(h, duration, t0, bitrate=bitrate); f.start(); f.join(duration + 60); results.append(f.summary()); time.sleep(2)
    after = wan_stats()
    bott = {k: after[direction][k] - before[k] for k in ("bytes", "packets", "drops")}
    out = {"mode": mode, "direction": direction, "duration": duration, "stagger": stagger, "bitrate": bitrate, "wan": wan["settings"], "host_mtu": mtu["host_mtu"], "flows": results,
           "total_mbps": round(sum(r["mbps"] for r in results), 2) if mode == "compete" else None,
           "jain_fairness": jain([r["mbps"] for r in results]) if mode == "compete" else None,
           "bottleneck": {**bott, "wan_after": after["settings"]},
           "samples": [s for f in (fs if mode == "compete" else []) for s in f.samples] if mode == "compete" else None}
    for r in results:
        log(f"{r['cc']:>6} {r['host']:<8} {r['mbps']:>7} Mbit/s  retr {r['retransmits']:>5}  rtt mean {r['rtt_ms']['mean']} ms  mss {r['mss']}" + (f"  ERROR {r['error']}" if r["error"] else ""))
    if mode == "compete": log(f" total {out['total_mbps']} Mbit/s, Jain fairness {out['jain_fairness']}, bottleneck drops {bott['drops']}")
    return out


SWEEP_RTTS = (0, 10, 30, 60, 100, 150)


def sweep(rtts=SWEEP_RTTS, rate_mbit=500, duration=20, direction="a2b", algos=ALGOS, bitrate="500M"):
    """For each RTT: set the WAN (RTT, rate; loss and buffer as they are), ping it, then run each algorithm alone.
    The WAN's previous settings are put back at the end, also on error or Ctrl-C."""
    hs = senders(direction, list(algos)); prev = wan_stats()["settings"]; mtu = mtu_state()
    if not hs: raise SystemExit("no senders for those algorithms")
    log(f"sweep: RTT {', '.join(f'{r:g}' for r in rtts)} ms at {rate_mbit:g} Mbit/s, iperf3 -b {bitrate or 'unlimited'}; {', '.join(h['cc'] for h in hs)} alone, {duration} s each; host MTU {mtu['host_mtu'] or 'mixed'}")
    points = []
    try:
        for rtt in rtts:
            wan = wan_apply(rtt, rate_mbit)["settings"]; time.sleep(1)
            ping = ping_rtt(hs[0]["name"], hs[0]["peer"])
            log(f"=== RTT {rtt:g} ms (ping avg {ping['avg_ms']} ms), BDP {wan['bdp_bytes']} B, queue {wan['queue_bytes']} B")
            res = []
            for h in hs:
                before = wan_stats()[direction]; t0 = time.time()
                f = Flow(h, duration, t0, bitrate=bitrate); f.start(); f.join(duration + 60)
                after = wan_stats()[direction]; r = f.summary()
                r["drops"] = after["drops"] - before["drops"]; r["samples"] = [{k: x[k] for k in ("t", "mbps", "rtt_ms", "cwnd", "retr")} for x in f.samples]
                res.append(r)
                log(f"{r['cc']:>6} {r['mbps']:>7} Mbit/s  retr {r['retransmits']:>6}  drops {r['drops']:>6}  rtt mean {r['rtt_ms']['mean']} ms" + (f"  ERROR {r['error']}" if r["error"] else ""))
                time.sleep(2)
            points.append({"rtt_ms": rtt, "ping": ping, "wan": wan, "flows": res})
    finally:
        wan_apply(prev["rtt_ms"], prev["rate_mbit"], prev["loss_pct"], prev["buffer_bdp"])
        log(f"WAN back to RTT {prev['rtt_ms']} ms, {prev['rate_mbit']} Mbit/s")
    return {"mode": "sweep", "direction": direction, "duration": duration, "rate_mbit": rate_mbit, "bitrate": bitrate, "rtts": list(rtts),
            "algos": [h["cc"] for h in hs], "host_mtu": mtu["host_mtu"], "wan_restored": prev, "points": points}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for m, d in (("compete", 30), ("solo", 20)):
        p = sp.add_parser(m); p.add_argument("--duration", type=int, default=d); p.add_argument("--direction", choices=("a2b", "b2a"), default="a2b")
        p.add_argument("--algos", default=",".join(ALGOS)); p.add_argument("--stagger", type=float, default=0.0)
        p.add_argument("--bitrate", default=None, help="iperf3 -b per flow, e.g. 500M (default: unlimited)")
    p = sp.add_parser("sweep"); p.add_argument("--rtts", default=",".join(map(str, SWEEP_RTTS))); p.add_argument("--rate", type=float, default=500)
    p.add_argument("--duration", type=int, default=20); p.add_argument("--direction", choices=("a2b", "b2a"), default="a2b"); p.add_argument("--algos", default=",".join(ALGOS))
    p.add_argument("--samples", action="store_true", help="keep the per-second samples in the JSON")
    p.add_argument("--bitrate", default="500M", help="iperf3 -b (default 500M; 0 = unlimited)")
    p = sp.add_parser("rtt"); p.add_argument("--count", type=int, default=10); p.add_argument("--src", default="a-cubic"); p.add_argument("--dst", default="b-cubic")
    p = sp.add_parser("wan"); p.add_argument("--rtt", type=float); p.add_argument("--rate", type=float); p.add_argument("--loss", type=float)
    p.add_argument("--buffer", type=float); p.add_argument("--reset", action="store_true")
    p = sp.add_parser("mtu"); p.add_argument("--set", type=int, choices=HOST_MTUS)
    a = ap.parse_args()
    if a.cmd in ("compete", "solo"):
        algos = [x.strip() for x in a.algos.split(",") if x.strip()]
        bad = [x for x in algos if x not in ALGOS]
        if bad: ap.error(f"unknown algorithm(s): {', '.join(bad)}")
        if not 5 <= a.duration <= 300: ap.error("--duration: 5 .. 300 s")
        res = flows(a.cmd, a.duration, a.direction, algos, a.stagger, a.bitrate)
        res.pop("samples", None) if a.cmd == "solo" else None
    elif a.cmd == "sweep":
        algos = [x.strip() for x in a.algos.split(",") if x.strip()]
        if [x for x in algos if x not in ALGOS]: ap.error("unknown algorithm(s)")
        try: rtts = [int(v) if float(v).is_integer() else float(v) for v in (x.strip() for x in a.rtts.split(",")) if v]
        except ValueError: ap.error("--rtts: comma-separated numbers")
        if not rtts or not all(LIMITS["rtt_ms"][0] <= r <= LIMITS["rtt_ms"][1] for r in rtts): ap.error("--rtts: 0 .. 300 ms")
        if not LIMITS["rate_mbit"][0] <= a.rate <= LIMITS["rate_mbit"][1]: ap.error("--rate: 1 .. 1000 Mbit/s")
        if not 5 <= a.duration <= 300: ap.error("--duration: 5 .. 300 s")
        res = sweep(rtts, a.rate, a.duration, a.direction, algos, None if a.bitrate in ("0", "") else a.bitrate)
        if not a.samples:
            for pt in res["points"]:
                for r in pt["flows"]: r.pop("samples", None)
    elif a.cmd == "rtt": res = ping_rtt(a.src, a.dst, a.count)
    elif a.cmd == "mtu": res = mtu_set(a.set) if a.set else mtu_state()
    elif a.reset: res = wan_reset()
    elif any(v is not None for v in (a.rtt, a.rate, a.loss, a.buffer)): res = wan_apply(a.rtt, a.rate, a.loss, a.buffer)
    else: res = wan_stats()
    print(json.dumps(res, indent=1))
    if a.cmd in ("compete", "solo") and any(r["error"] for r in res["flows"]): sys.exit(1)
    if a.cmd == "sweep" and any(r["error"] for pt in res["points"] for r in pt["flows"]): sys.exit(1)


if __name__ == "__main__": main()

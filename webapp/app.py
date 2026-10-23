#!/usr/bin/env python3
"""The tcp-cc-lab portal: the WAN emulator's controls (RTT slider, bottleneck rate, loss, buffer) and its live queue,
the hosts and their congestion control algorithms, flow runs drawn live (throughput and RTT per algorithm), and tests.

Runs go through the shared run engine (labportal): one at a time, streamed, kept in runs/<id>.json, resumable:

    compete    all three algorithms at once through the shared bottleneck (tools/cclab.py compete)
    solo       each algorithm alone, one after the other (tools/cclab.py solo)
    verify     ./lab.sh verify: algorithms, WAN settings, routes, RTT across the WAN
    test       the Robot suites (all, or one), results in tests/results_<date>_<time>/ (results/latest links the newest)

The WAN can be changed while a compete / solo run is going — that is the point: the change is written into the run's
log as a MARK line, and the chart shows where it happened. Not while the tests run (they set the WAN themselves).
Sign-in is the Lab Hub's (labportal.gate). Start with ./lab.sh webapp (port 8112)."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from labportal import RunBase, RunRegistry, install_runs_api, gate, run_metrics, exposition, metrics_generated, metric_line as line
from pydantic import BaseModel, Field

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
import cclab as C  # noqa: E402

NAME = "tcp-cc-lab"
PORT = int(os.environ.get("WEBAPP_PORT", "8112"))
RUNS_DIR = Path(__file__).resolve().parent / "runs"; RUNS_DIR.mkdir(exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"
VERSION = (LAB / "VERSION").read_text().strip()
SUITES = sorted(p.stem for p in (LAB / "tests" / "suites").glob("*.robot"))
FLOW_MODES = ("compete", "solo")

app = FastAPI(title="tcp-cc-lab portal API", version="1.0", docs_url="/docs",
              description="TCP congestion control (CUBIC / BBR / Reno) over an emulated WAN: the emulator, flows, tests. UI: [/](/)")
gate.install(app, open_paths={"/metrics", "/api/sd"}, open_from=["10.0.0.0/24", "192.168.122.0/24"])
registry = RunRegistry(RUNS_DIR)


def lab_sh(*args, timeout=60):
    env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
    r = subprocess.run([str(LAB / "lab.sh"), *args], capture_output=True, text=True, timeout=timeout, env=env)
    return r.returncode, r.stdout


def nodes():
    """lab.sh status's node table (it stops at the first blank line)."""
    _, out = lab_sh("status", timeout=30); rows = []
    for l in out.splitlines()[1:]:
        if not l.strip(): break
        c = l.split()
        if len(c) < 7: continue
        st, rest = ("shut off", c[4:]) if c[2] == "shut" else (c[2], c[3:])
        rows.append({"node": c[0], "role": c[1], "state": st, "mgmt": rest[0], "data": rest[1], "cc": rest[2] if rest[2] != "-" else None, "vm": rest[-1]})
    return rows


_wan = {"t": 0, "v": None}; _lock = threading.Lock()


def wan(fresh=False):
    """The emulator's settings and counters, cached for 1 s (several viewers polling)."""
    with _lock:
        if not fresh and _wan["v"] and time.time() - _wan["t"] < 1.0: return _wan["v"]
        try: v = C.wan_stats()
        except Exception as e: v = {"error": str(e)[:200]}   # noqa: BLE001
        _wan.update(t=time.time(), v={"at": time.time(), **v}); return _wan["v"]


HOST_CMD = "sysctl -n net.ipv4.tcp_congestion_control; tc qdisc show dev eth1 | awk 'NR==1{print $2}'; pgrep -x iperf3 >/dev/null && echo up || echo down; cat /sys/class/net/eth1/mtu"
_hosts = {"t": 0, "v": None}


def hosts_health():
    """Each host's algorithm, qdisc and iperf3 server over SSH, in parallel; cached for 20 s (the scrape and the page)."""
    from concurrent.futures import ThreadPoolExecutor
    if _hosts["v"] and time.time() - _hosts["t"] < 20: return _hosts["v"]
    hs = [n for n in C.inventory()["nodes"] if n["role"] == "host"]
    def one(h):
        try:
            rc, out = C.run(h["name"], HOST_CMD, timeout=10); l = out.split()
            return {"node": h["name"], "expected": h["cc"], "up": rc == 0 and len(l) == 4, "cc": l[0] if l else None,
                    "qdisc": l[1] if len(l) > 1 else None, "iperf3": len(l) > 2 and l[2] == "up", "mtu": int(l[3]) if len(l) > 3 and l[3].isdigit() else None}
        except Exception: return {"node": h["name"], "expected": h["cc"], "up": False, "cc": None, "qdisc": None, "iperf3": False, "mtu": None}   # noqa: BLE001
    with ThreadPoolExecutor(6) as ex: v = list(ex.map(one, hs))
    _hosts.update(t=time.time(), v=v); return v


_mtu = {"t": 0, "v": None}


def mtu(fresh=False):
    """The hosts' MTU and r1 / r2 / wanem's links (tools/cclab.py mtu_state), cached for 20 s."""
    if fresh or not _mtu["v"] or time.time() - _mtu["t"] > 20:
        try: v = C.mtu_state()
        except Exception as e: v = {"error": str(e)[:200]}   # noqa: BLE001
        _mtu.update(t=time.time(), v=v)
    return _mtu["v"]


def live_flows():
    """While a compete / solo run goes: each algorithm's last per-second sample (from the run's SAMPLE lines, at most 5 s old)."""
    a = active_run()
    if not a or a.mode not in FLOW_MODES: return []
    last = {}
    for l in a.log[-200:]:
        if l["line"].startswith("SAMPLE "):
            try: s = json.loads(l["line"][7:]); last[s["cc"]] = s
            except ValueError: pass
    return [s for s in last.values() if time.time() - s.get("ts", 0) < 5]


def all_running(ns=None): return all(n["state"] == "running" for n in (ns or nodes()))


def tests_summary():
    latest = LAB / "results" / "latest" / "output.xml"
    if not latest.exists(): return None
    try:
        from labportal.runs import parse_robot
        r = parse_robot(latest); r["when"] = latest.resolve().parent.name; return r
    except Exception: return None   # noqa: BLE001


def latest_flows():
    out = {}
    for r in registry.list(limit=200):
        if r["mode"] in FLOW_MODES and r["mode"] not in out and r["status"] == "success" and r.get("result"): out[r["mode"]] = {"id": r["id"], "finished": r["finished"], "result": r["result"]}
    return out


def active_run():
    a = registry.active(); return a


# ---- API -------------------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index(): return FileResponse(STATIC / "index.html")


@app.get("/api/lab", tags=["lab"], summary="What the lab is: nodes, the WAN defaults and limits, the suites")
def lab():
    inv = C.inventory()
    return {"name": NAME, "version": VERSION, "inventory": {k: v for k, v in inv.items() if k != "by_name"}, "limits": C.LIMITS, "suites": SUITES}


@app.get("/api/state", tags=["lab"], summary="Nodes, the WAN emulator, the last tests, the last compete / solo result, the run going")
def state():
    ns = nodes(); up = any(n["state"] == "running" for n in ns); a = active_run()
    return {"at": time.time(), "nodes": ns, "up": up, "all_up": up and all_running(ns), "wan": wan() if up else None, "mtu": mtu() if up else None, "tests": tests_summary(),
            "flows": latest_flows(), "busy": registry.busy(), "active": a.id if a else None, "active_mode": a.mode if a else None}


@app.get("/api/wan", tags=["wan"], summary="The WAN emulator: settings, and both directions' bottleneck counters (poll this for rates)")
def get_wan(): return wan()


class Wan(BaseModel):
    rtt_ms: float | None = Field(None, description="round-trip time added by the emulator, ms (split evenly between the directions); 0..300")
    rate_mbit: float | None = Field(None, description="bottleneck rate, Mbit/s; 1..1000")
    loss_pct: float | None = Field(None, description="random loss per direction, %; 0..20")
    buffer_bdp: float | None = Field(None, description="bottleneck buffer in multiples of the BDP (rate x RTT); 0.05..16")
    reset: bool = Field(False, description="back to the lab defaults (lab.conf): 30 ms, 100 Mbit/s, 0 %, 1 x BDP")


@app.post("/api/wan", tags=["wan"], summary="Change the WAN emulator in place (running flows see it at once); any field left out stays")
def set_wan(w: Wan):
    a = active_run()
    if a and a.mode == "test": raise HTTPException(409, "the tests are running; they set the WAN themselves")
    try: v = C.wan_reset() if w.reset else C.wan_apply(w.rtt_ms, w.rate_mbit, w.loss_pct, w.buffer_bdp)
    except ValueError as e: raise HTTPException(422, str(e))
    except Exception as e: raise HTTPException(502, str(e)[:300])   # noqa: BLE001
    _wan["t"] = 0
    if a and a.mode in FLOW_MODES: a.say("MARK " + json.dumps({"ts": round(time.time(), 2), **{k: v["settings"][k] for k in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp")}}))
    return v


@app.get("/api/mtu", tags=["mtu"], summary="The hosts' MTU (1500 or 9000, all six together) and r1 / r2 / wanem's links (always 9000)")
def get_mtu(): return mtu(fresh=True)


class Mtu(BaseModel):
    mtu: int = Field(..., description="the hosts' eth1 MTU: 1500 or 9000 (the routers' and wanem's links stay 9000)")


@app.post("/api/mtu", tags=["mtu"], summary="Set the hosts' MTU (all six); not while a run goes (a flow keeps the MSS it started with)")
def set_mtu(m: Mtu):
    if m.mtu not in C.HOST_MTUS: raise HTTPException(422, f"mtu: one of {', '.join(map(str, C.HOST_MTUS))}")
    if registry.busy(): raise HTTPException(409, "a run is going: change the MTU between runs (a TCP flow keeps the MSS it started with)")
    try: v = C.mtu_set(m.mtu)
    except Exception as e: raise HTTPException(502, str(e)[:300])   # noqa: BLE001
    _mtu.update(t=time.time(), v=v); _hosts["t"] = 0
    return v


@app.post("/api/rtt", tags=["wan"], summary="Measure the RTT across the WAN now (ping a-cubic -> b-cubic, 10 x 0.2 s)")
def rtt():
    try: return C.ping_rtt()
    except Exception as e: raise HTTPException(502, str(e)[:300])   # noqa: BLE001


@app.get("/report/{name}", tags=["tests"], summary="The last Robot run's report.html / log.html")
def report(name: str):
    if name not in ("report.html", "log.html"): raise HTTPException(404)
    p = LAB / "results" / "latest" / name
    if not p.exists(): raise HTTPException(404, "no test run yet")
    return FileResponse(p)


# ---- runs ------------------------------------------------------------------------------------------------------------
class RunRequest(BaseModel):
    mode: str = Field(..., description="compete | solo | verify | test")
    options: dict = Field(default_factory=dict, description="compete/solo: {duration: s, direction: a2b|b2a, algos: [cubic, bbr, reno], stagger: s}; test: {suite}")


class Run(RunBase):
    LAB = NAME
    EXTRA = {"result": "result"}
    STEP_TITLES = {"compete": "All algorithms at once through the bottleneck", "solo": "Each algorithm alone",
                   "verify": "Verify: algorithms, WAN settings, routes, RTT across the WAN", "test": "Robot Framework suites"}

    def __init__(self, mode, options=None, resume_of=None):
        self.result = None
        super().__init__(mode, options, resume_of, runs_dir=RUNS_DIR, cwd=LAB)
        o = self.options
        if mode in FLOW_MODES:
            self.steps[0]["title"] = (f"{self.STEP_TITLES[mode]}: {', '.join(o['algos'])}, {o['duration']} s, "
                                      f"site {'A → B' if o['direction'] == 'a2b' else 'B → A'}" + (f", {o['stagger']} s apart" if o.get("stagger") else ""))
        if mode == "test" and o.get("suite"): self.steps[0]["title"] = f"Robot suite {o['suite']}"

    def plan(self): return [self.mode]

    def _flows(self, s):
        o = self.options; out = RUNS_DIR / f"{self.id}.flows.json"
        args = f"{self.mode} --duration {int(o['duration'])} --direction {o['direction']} --algos {','.join(o['algos'])} --stagger {float(o.get('stagger') or 0)}"
        rc = self.sh(["bash", "-c", f'python3 "{LAB}/tools/cclab.py" {args} > "{out}"'], check=False)
        try: self.result = json.loads(out.read_text()); out.unlink()
        except Exception: self.result = None   # noqa: BLE001
        if self.result:
            fl = self.result["flows"]
            s["summary"] = "; ".join(f"{f['cc']} {f['mbps']} Mbit/s" for f in fl) + (f"; Jain {self.result['jain_fairness']}" if self.result.get("jain_fairness") else "")
        if rc: raise RuntimeError(f"flows failed (rc={rc})" + (f": {s['summary']}" if s["summary"] else ""))

    do_compete = _flows
    do_solo = _flows

    def do_verify(self, s):
        rc = self.sh([LAB / "lab.sh", "verify"], check=False)
        s["summary"] = next((l["line"].strip() for l in reversed(self.log) if "pairs ok" in l["line"]), "")
        if rc: raise RuntimeError(s["summary"] or "verify failed")

    def do_test(self, s):
        suite = self.options.get("suite")
        self.record_tests(LAB / "results", s, self.sh([LAB / "tests" / "run.sh", *([f"suites/{suite}.robot"] if suite else [])], check=False))

    def after(self): _wan["t"] = 0


install_runs_api(app, registry, resume_factory=lambda d: Run(d["mode"], d.get("options"), resume_of=d))


@app.post("/api/runs", tags=["runs"], summary="Start a run: compete | solo (flows) | verify | test (all or one suite)")
def start_run(req: RunRequest):
    if req.mode not in (*FLOW_MODES, "verify", "test"): raise HTTPException(422, "mode: compete, solo, verify or test")
    o = dict(req.options)
    if req.mode in FLOW_MODES:
        o["duration"] = int(o.get("duration") or (30 if req.mode == "compete" else 20))
        if not 5 <= o["duration"] <= 300: raise HTTPException(422, "options.duration: 5 .. 300 s")
        o["direction"] = o.get("direction") or "a2b"
        if o["direction"] not in ("a2b", "b2a"): raise HTTPException(422, "options.direction: a2b or b2a")
        o["algos"] = [a for a in C.ALGOS if a in (o.get("algos") or C.ALGOS)]
        if not o["algos"]: raise HTTPException(422, "options.algos: some of cubic, bbr, reno")
        o["stagger"] = float(o.get("stagger") or 0)
        if not 0 <= o["stagger"] <= 60: raise HTTPException(422, "options.stagger: 0 .. 60 s")
    if req.mode == "test" and o.get("suite") not in (None, "", *SUITES): raise HTTPException(422, f"options.suite: one of {', '.join(SUITES)}")
    if not all_running(): raise HTTPException(409, "the lab is not (fully) running: power it on from the Lab Hub")
    return registry.start(Run(req.mode, o))


# ---- monitoring ------------------------------------------------------------------------------------------------------
@app.get("/api/sd", tags=["monitoring"], summary="Prometheus HTTP service discovery: this portal")
def prometheus_sd():
    return [{"targets": [f"10.0.0.1:{PORT}"], "labels": {"lab": NAME, "job": "portal", "role": "portal"}}]


@app.get("/metrics", tags=["monitoring"], summary="Prometheus metrics: nodes, the WAN settings and bottleneck counters, runs", response_class=PlainTextResponse)
def metrics():
    lab = {"lab": NAME}; out = []
    def gauge(name, help_, typ="gauge"): out.extend([f"# HELP {name} {help_}", f"# TYPE {name} {typ}"])
    ns = nodes()
    gauge("lab_vm_running", "1 if the node's VM is running")
    for n in ns: out.append(line("lab_vm_running", {**lab, "node": n["node"], "role": n["role"]}, int(n["state"] == "running")))
    w = wan() if any(n["state"] == "running" for n in ns) else None
    if w and "error" not in w:
        s = w["settings"]
        for k, h in (("rtt_ms", "emulated round-trip time, ms"), ("rate_mbit", "bottleneck rate, Mbit/s"), ("loss_pct", "random loss per direction, %"),
                     ("buffer_bdp", "bottleneck buffer, x BDP"), ("queue_bytes", "bottleneck buffer, bytes")):
            gauge(f"tcpcc_wan_{k}", f"WAN emulator: {h}"); out.append(line(f"tcpcc_wan_{k}", lab, s.get(k) or 0))
        for k, h, t in (("bytes", "bytes through the bottleneck", "counter"), ("drops", "packets dropped at the bottleneck (queue full)", "counter"),
                        ("backlog_bytes", "bytes queued at the bottleneck now", "gauge")):
            gauge(f"tcpcc_bottleneck_{k}" + ("_total" if t == "counter" else ""), f"WAN emulator: {h}", t)
            for d in ("a2b", "b2a"): out.append(line(f"tcpcc_bottleneck_{k}" + ("_total" if t == "counter" else ""), {**lab, "direction": d}, w[d][k]))
    if any(n["state"] == "running" for n in ns):
        gauge("tcpcc_wanem_up", "1 if the portal can read the WAN emulator's settings and counters")
        out.append(line("tcpcc_wanem_up", lab, int(bool(w) and "error" not in w)))
        hh = hosts_health()
        gauge("tcpcc_host_up", "1 if the host answers over SSH"); gauge("tcpcc_host_cc_ok", "1 if the host runs the algorithm its name says")
        gauge("tcpcc_host_mtu", "the host's eth1 MTU (1500 or 9000)")
        for h in hh:
            if h["mtu"]: out.append(line("tcpcc_host_mtu", {**lab, "node": h["node"]}, h["mtu"]))
        mt = mtu()
        if "error" not in mt:
            gauge("tcpcc_link_mtu", "MTU of r1 / r2 / wanem's data links (must always be 9000)")
            for node, devs in mt["infra"].items():
                for dev, v in devs.items(): out.append(line("tcpcc_link_mtu", {**lab, "node": node, "dev": dev}, v or 0))
        gauge("tcpcc_host_fq", "1 if the host's data NIC has the fq qdisc (pacing)"); gauge("tcpcc_host_iperf3_up", "1 if the host's iperf3 server is running")
        for h in hh:
            hl = {**lab, "node": h["node"], "expected": h["expected"], "cc": h["cc"] or ""}
            out.append(line("tcpcc_host_up", hl, int(h["up"]))); out.append(line("tcpcc_host_cc_ok", hl, int(h["cc"] == h["expected"])))
            out.append(line("tcpcc_host_fq", hl, int(h["qdisc"] == "fq"))); out.append(line("tcpcc_host_iperf3_up", hl, int(h["iperf3"])))
    lf = live_flows()
    if lf:
        gauge("tcpcc_flow_mbps", "a running portal flow's throughput (last second), per algorithm")
        gauge("tcpcc_flow_rtt_ms", "a running portal flow's smoothed RTT as its sender sees it, per algorithm")
        gauge("tcpcc_flow_cwnd_bytes", "a running portal flow's congestion window, per algorithm")
        for f in lf:
            fl = {**lab, "cc": f["cc"], "host": f["host"]}
            out.append(line("tcpcc_flow_mbps", fl, f["mbps"])); out.append(line("tcpcc_flow_rtt_ms", fl, f["rtt_ms"])); out.append(line("tcpcc_flow_cwnd_bytes", fl, f["cwnd"]))
    lc = latest_flows().get("compete")
    if lc:
        gauge("tcpcc_last_compete_mbps", "the last compete run's throughput per algorithm"); gauge("tcpcc_last_compete_share", "the last compete run's share of the total, per algorithm")
        tot = sum(f["mbps"] for f in lc["result"]["flows"]) or 1
        for f in lc["result"]["flows"]:
            out.append(line("tcpcc_last_compete_mbps", {**lab, "cc": f["cc"]}, f["mbps"])); out.append(line("tcpcc_last_compete_share", {**lab, "cc": f["cc"]}, round(f["mbps"] / tot, 3)))
        gauge("tcpcc_last_compete_jain", "the last compete run's Jain fairness index (1 = equal shares)")
        out.append(line("tcpcc_last_compete_jain", lab, lc["result"].get("jain_fairness") or 0))
    gauge("tcpcc_lab_run_active", "1 while a portal run is going (flows load the WAN on purpose; the tests change it)")
    out.append(line("tcpcc_lab_run_active", lab, int(registry.busy())))
    out += run_metrics(NAME, registry.list())
    return exposition(out + metrics_generated(NAME))


def main():
    import uvicorn; uvicorn.run(app, host=os.environ.get("WEBAPP_HOST", "0.0.0.0"), port=PORT)


if __name__ == "__main__": main()

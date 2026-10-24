#!/usr/bin/env python3
"""The tcp-cc-lab portal: the WAN emulator's controls (RTT slider, bottleneck rate, loss, buffer) and its live queue,
the hosts and their congestion control algorithms, flow runs drawn live (throughput and RTT per algorithm), and tests.

Runs go through the shared run engine (labportal): one at a time, streamed, kept in runs/<id>.json, resumable:

    compete    all three algorithms at once through the shared bottleneck (tools/cclab.py compete)
    solo       each algorithm alone, one after the other (tools/cclab.py solo)
    live       the chosen algorithms at once, until stopped (or max_s): the sender bit rate (iperf3 -b) can be changed while it
               goes (POST /api/live/bitrate: the flows are stopped and new ones started with the new -b) as can the WAN;
               the bottleneck queue is sampled every second (WAN lines)
    verify     ./lab.sh verify: algorithms, WAN settings, routes, RTT across the WAN
    test       the Robot suites (all, or one), results in tests/results_<date>_<time>/ (results/latest links the newest)

The WAN can be changed while a compete / solo run is going — that is the point: the change is written into the run's
log as a MARK line, and the chart shows where it happened. Not while the tests run (they set the WAN themselves).
Sign-in is the Lab Hub's (labportal.gate). Start with ./lab.sh webapp (port 8112)."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from labportal import RunBase, RunRegistry, install_runs_api, gate, run_metrics, exposition, metrics_generated, metric_line as line
from pydantic import BaseModel, Field

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
import cclab as C  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import report as pdfreport  # noqa: E402  (app.py already has a report() endpoint)
import report_shot  # noqa: E402

NAME = "tcp-cc-lab"
PORT = int(os.environ.get("WEBAPP_PORT", "8112"))
RUNS_DIR = Path(__file__).resolve().parent / "runs"; RUNS_DIR.mkdir(exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"
VERSION = (LAB / "VERSION").read_text().strip()
SUITES = sorted(p.stem for p in (LAB / "tests" / "suites").glob("*.robot"))
FLOW_MODES = ("compete", "solo")
LIVE_MODES = (*FLOW_MODES, "live")
SWEEP_RTTS = (0, 15, 30, 60, 90)   # ms: the portal's RTT sweep (the WAN card's checkbox)   # runs that draw per-second samples
BITRATE_MAX = 5000   # Mbit/s; 0 = iperf3 without -b

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


def path_drops():
    """Packets the physical host's tap devices dropped on their way into each VM on a data network (tx_dropped: the VM did not
    take them off its virtio ring in time). Losses outside the emulator, which tbf's and netem's counters never see. {"<vm> from <net>": n}"""
    out = {}
    for n in pdfreport.NODE_ORDER:
        try: rows = subprocess.run(["virsh", "-c", "qemu:///system", "domiflist", f"tcpcc-{n}"], capture_output=True, text=True, timeout=5).stdout.splitlines()[2:]
        except Exception: continue   # noqa: BLE001
        for r in rows:
            f = r.split()
            if len(f) >= 3 and f[2] != "tcpcc-oob":
                try: out[f"{n} from {f[2].removeprefix('tcpcc-')}"] = int(Path(f"/sys/class/net/{f[0]}/statistics/tx_dropped").read_text())
                except (OSError, ValueError): pass
    return out


def drops_since(d0):
    d1 = path_drops(); return {k: d1[k] - d0[k] for k in d1 if k in d0 and d1[k] >= d0[k]}


def live_flows():
    """While a compete / solo run goes: each algorithm's last per-second sample (from the run's SAMPLE lines, at most 5 s old)."""
    a = active_run()
    if not a or a.mode not in LIVE_MODES: return []
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
        if r["mode"] in LIVE_MODES and r["mode"] not in out and r["status"] == "success" and r.get("result"): out[r["mode"]] = {"id": r["id"], "finished": r["finished"], "result": r["result"]}
    return out


def active_run():
    a = registry.active(); return a


# ---- node CPU and memory: one long-lived SSH per node streams /proc/stat and /proc/meminfo every second ------------------
STATS_KEEP = 1800   # samples per node (30 min at 1 s)
STATS_LOOP = ("while :; do head -1 /proc/stat; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; nproc; echo .; sleep 1; done")
_stats = {}   # node -> {"vcpus", "samples": deque([ts, cpu_pct, mem_used_mb, mem_total_mb])}


def _stats_worker(name):
    """Keeps one SSH session to the node open (reconnecting when the node is off or rebooting) and turns each block into a sample."""
    st = _stats.setdefault(name, {"vcpus": None, "samples": deque(maxlen=STATS_KEEP)})
    while True:
        try:
            n = C.node(name)
            p = subprocess.Popen([*C.SSH, "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", f"{n['user']}@{n['mgmt_ip']}", STATS_LOOP],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            prev, blk = None, []
            for l in p.stdout:
                l = l.strip()
                if l != ".": blk.append(l); continue
                try:
                    cpu = [int(x) for x in blk[0].split()[1:9]]; mem = {k.split(":")[0]: int(k.split()[1]) for k in blk[1:3]}
                    st["vcpus"] = int(blk[3])
                    if prev:
                        tot = sum(cpu) - sum(prev); idle = (cpu[3] + cpu[4]) - (prev[3] + prev[4])
                        st["samples"].append([round(time.time(), 2), round(100 * (tot - idle) / tot, 1) if tot > 0 else 0.0,
                                              round((mem["MemTotal"] - mem["MemAvailable"]) / 1024, 1), round(mem["MemTotal"] / 1024, 1)])
                    prev = cpu
                except (IndexError, ValueError, KeyError): pass
                blk = []
            p.wait()
        except Exception: pass   # noqa: BLE001
        time.sleep(10)


def start_stats():
    for n in C.inventory()["nodes"]: threading.Thread(target=_stats_worker, args=(n["name"],), daemon=True).start()


# ---- device configuration pages (the topology links here) ------------------------------------------------------------
HOST_SECTIONS = [
    ("System", "uname -sr; echo; echo \"$(nproc) vCPU\"; free -m"),
    ("Interfaces", "ip -br addr; echo; ip -d link show eth1 | head -2"),
    ("Routes", "ip route"),
    ("Queueing discipline on eth1 (fq: pacing for the sender)", "tc -s qdisc show dev eth1"),
    ("Congestion control", "echo \"in use:    $(sysctl -n net.ipv4.tcp_congestion_control)\"; echo \"available: $(sysctl -n net.ipv4.tcp_available_congestion_control)\"; echo \"default qdisc: $(sysctl -n net.core.default_qdisc)\""),
    ("TCP and socket settings (sysctl net.ipv4.tcp_* and net.core.*)", "sysctl -a 2>/dev/null | grep -E '^net\\.(ipv4\\.tcp_|core\\.)' | grep -v '_entries'"),
    ("Settings applied at boot (/etc/sysctl.d)", "for f in /etc/sysctl.d/*.conf; do echo \"# $f\"; cat \"$f\"; echo; done"),
    ("iperf3", "iperf3 --version | head -1; echo; ps -o pid,args | awk '$2 == \"iperf3\"'"),
]
NODE_SECTIONS = {
    "host": HOST_SECTIONS,
    "wanem": [
        ("Emulator settings and qdiscs (wanem show)", "doas wanem show"),
        ("Defaults (/etc/wanem.conf)", "cat /etc/wanem.conf"),
        ("Bridge", "ip -br addr; echo; bridge link"),
        ("Offloads (must be off: the qdiscs see wire-size packets)", "doas wanem offloads"),
        ("The control script (/usr/local/bin/wanem)", "cat /usr/local/bin/wanem"),
        ("System", "uname -sr; echo; echo \"$(nproc) vCPU\"; free -m"),
    ],
    "router": [
        ("Configuration (show configuration commands)", "/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands"),
        ("Interfaces", "/opt/vyatta/bin/vyatta-op-cmd-wrapper show interfaces"),
        ("Routes", "/opt/vyatta/bin/vyatta-op-cmd-wrapper show ip route"),
        ("System and version", "echo \"$(nproc) vCPU\"; free -m; echo; /opt/vyatta/bin/vyatta-op-cmd-wrapper show version | head -8"),
    ],
}
CONFIG_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} configuration</title>
<script>try {{ const t = localStorage.getItem('portal-theme'); if (t) document.documentElement.dataset.theme = t; }} catch (e) {{ }}</script>
<style>
:root {{ --bg:#f6f7f9; --panel:#fcfcfb; --ink:#1f2937; --mute:#52514e; --line:#e5e7eb; --code:#f3f4f6; --accent:#2563eb; --bad:#dc2626; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0f141b; --panel:#1a1a19; --ink:#f0efec; --mute:#c3c2b7; --line:#2c2c2a; --code:#121211; --accent:#60a5fa; --bad:#f87171; }} }}
:root[data-theme="dark"] {{ --bg:#0f141b; --panel:#1a1a19; --ink:#f0efec; --mute:#c3c2b7; --line:#2c2c2a; --code:#121211; --accent:#60a5fa; --bad:#f87171; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
header {{ padding:14px 16px; background:var(--panel); border-bottom:1px solid var(--line); display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }}
header h1 {{ font-size:18px; margin:0; }} header .mute {{ color:var(--mute); }} header .right {{ margin-left:auto; display:flex; gap:12px; }}
a {{ color:var(--accent); }} main {{ max-width:1100px; margin:0 auto; padding:16px; }}
nav {{ display:flex; flex-wrap:wrap; gap:6px 14px; margin-bottom:12px; font-size:13px; }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:14px; }}
h2 {{ font-size:14px; margin:0 0 8px; }}
pre {{ background:var(--code); border:1px solid var(--line); border-radius:6px; padding:10px; margin:0; overflow:auto; font:12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space:pre; }}
.bad {{ color:var(--bad); }}
</style></head><body>
<header><h1>{name}</h1><span class="mute">{role} · {mgmt} · collected {when}</span><span class="right"><a href="">Refresh</a><a href="/">Portal</a></span></header>
<main><nav>{nav}</nav>{body}</main></body></html>"""


def node_sections(name):
    """[(title, text)] for one node, collected live over SSH in one go (VyOS password hashes redacted)."""
    role = C.node(name)["role"]
    script = "".join(f"echo '@@{i}'; {{ {cmd} ; }} 2>&1; " for i, (_, cmd) in enumerate(NODE_SECTIONS[role]))
    try: rc, out = C.run(name, script, timeout=30)
    except Exception as e: rc, out = 255, f"@@0\n{e}"   # noqa: BLE001
    out = re.sub(r"(encrypted-password|plaintext-password) '[^']*'", r"\1 '<redacted>'", out)   # VyOS login hash: not for a web page
    parts, cur = {}, None
    for l in out.splitlines():
        if l.startswith("@@") and l[2:].isdigit(): cur = int(l[2:]); parts[cur] = []; continue
        if cur is not None: parts[cur].append(l)
    return [(t, "\n".join(parts.get(i, [])).rstrip()) for i, (t, _) in enumerate(NODE_SECTIONS[role])]


def node_desc(n):
    return {"host": f"host, {({'cubic': 'CUBIC', 'bbr': 'BBR', 'reno': 'Reno'}).get(n['cc'], n['cc'])}, data {n['ip']}",
            "wanem": "WAN emulator (Alpine, netem + tbf on a bridge)", "router": f"router (VyOS), data {n['ip']}"}[n["role"]]


def config_html(name):
    n = C.node(name); secs = node_sections(name)
    body = "".join(f'<section id="s{i}"><h2>{escape(t)}</h2><pre>{escape(txt) or "<span class=bad>no output (is the node running?)</span>"}</pre></section>' for i, (t, txt) in enumerate(secs))
    nav = " ".join(f'<a href="#s{i}">{escape(t.split(" (")[0])}</a>' for i, (t, _) in enumerate(secs))
    return CONFIG_PAGE.format(name=escape(name), role=escape(node_desc(n)), mgmt=escape(f"{n['user']}@{n['mgmt_ip']}"), when=time.strftime("%Y-%m-%d %H:%M:%S"), nav=nav, body=body)


# ---- API -------------------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index(): return FileResponse(STATIC / "index.html")


@app.get("/config/{name}", tags=["lab"], summary="One node's configuration, collected live (HTML page; the topology links here)", response_class=HTMLResponse)
def node_config(name: str):
    if name not in C.inventory()["by_name"]: raise HTTPException(404, "no such node")
    return HTMLResponse(config_html(name))


@app.get("/api/nodestats", tags=["lab"], summary="Each node's CPU (% of its vCPUs) and memory in use (MemTotal - MemAvailable), one sample a second, last 30 min")
def nodestats(since: float = 0):
    return {"now": time.time(), "nodes": {k: {"vcpus": v["vcpus"], "samples": [x for x in v["samples"] if x[0] > since]} for k, v in _stats.items()}}


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
    rate_mbit: float | None = Field(None, description="bottleneck rate, Mbit/s; 1..5000")
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
    if a and a.mode in LIVE_MODES: a.say("MARK " + json.dumps({"ts": round(time.time(), 2), **{k: v["settings"][k] for k in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp")}}))
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
    options: dict = Field(default_factory=dict, description="compete/solo: {duration: s, direction: a2b|b2a, algos: [cubic, bbr, reno], stagger: s, bitrate_mbit}; "
                          "live: {max_s, direction, algos, bitrate_mbit, sequential: one algorithm at a time, slot_s: seconds each}; test: {suite}. bitrate_mbit: iperf3 -b per flow, 0 or missing = unlimited")


class Run(RunBase):
    LAB = NAME
    EXTRA = {"result": "result", "node_stats": "node_stats"}
    STEP_TITLES = {"compete": "All algorithms at once through the bottleneck", "solo": "Each algorithm alone",
                   "verify": "Verify: algorithms, WAN settings, routes, RTT across the WAN", "test": "Robot Framework suites",
                   "live": "Live test"}

    def __init__(self, mode, options=None, resume_of=None):
        self.rtt_restore = None; self.result = None; self.node_stats = None; self.ctl = {"stop": False, "bitrate": None, "next": False}
        super().__init__(mode, options, resume_of, runs_dir=RUNS_DIR, cwd=LAB)
        o = self.options
        if mode in FLOW_MODES:
            self.steps[0]["title"] = (f"{self.STEP_TITLES[mode]}: {', '.join(o['algos'])}, {o['duration']} s, "
                                      f"site {'A → B' if o['direction'] == 'a2b' else 'B → A'}" + (f", {o['stagger']} s apart" if o.get("stagger") else ""))
        if mode == "live":
            if o.get("rtt_sweep"): o_sw = f", RTT sweep {'/'.join(f'{r:g}' for r in o['rtts'])} ms, {o['step_s']} s per RTT"
            else: o_sw = ""
            self.steps[0]["title"] = (f"Live test{o_sw}: {', '.join(o['algos'])} one at a time, {o['slot_s']} s each, site {'A → B' if o['direction'] == 'a2b' else 'B → A'}, until stopped"
                                      if o.get("sequential") else f"Live test{o_sw}: {', '.join(o['algos'])} at once, site {'A → B' if o['direction'] == 'a2b' else 'B → A'}, until stopped (at most {o['max_s']} s)")
        if mode == "test" and o.get("suite"): self.steps[0]["title"] = f"Robot suite {o['suite']}"

    def plan(self): return [self.mode]

    def _flows(self, s):
        o = self.options; out = RUNS_DIR / f"{self.id}.flows.json"
        args = f"{self.mode} --duration {int(o['duration'])} --direction {o['direction']} --algos {','.join(o['algos'])} --stagger {float(o.get('stagger') or 0)}"
        if o.get("bitrate_mbit"): args += f" --bitrate {o['bitrate_mbit']:g}M"
        d0 = path_drops()
        rc = self.sh(["bash", "-c", f'python3 "{LAB}/tools/cclab.py" {args} > "{out}"'], check=False)
        try: self.result = json.loads(out.read_text()); out.unlink()
        except Exception: self.result = None   # noqa: BLE001
        if self.result:
            self.result["path_drops"] = drops_since(d0)
            fl = self.result["flows"]
            s["summary"] = "; ".join(f"{f['cc']} {f['mbps']} Mbit/s" for f in fl) + (f"; Jain {self.result['jain_fairness']}" if self.result.get("jain_fairness") else "")
        if rc: raise RuntimeError(f"flows failed (rc={rc})" + (f": {s['summary']}" if s["summary"] else ""))

    do_compete = _flows
    do_solo = _flows

    def do_live(self, s):
        """Flows run until stopped; a bit-rate change stops them (SIGINT: iperf3 ends its test cleanly) and starts new ones with the new -b."""
        o = self.options; hs_all = C.senders(o["direction"], o["algos"]); t0 = time.time(); end = t0 + o["max_s"]
        seq = bool(o.get("sequential")); k = 0; hs = hs_all[:1] if seq else hs_all   # hs: the flows running now
        slot_end = t0 + (o.get("slot_s") or 0)
        self.ctl["bitrate"] = o.get("bitrate_mbit") or 0; segs, sent, last_w = [], {}, None
        rtts = o.get("rtts") if o.get("rtt_sweep") else None; ri, step_end = 0, None   # RTT sweep: index of the RTT now, and when its step ends
        def set_rtt(r, **extra):   # in place, like the WAN card; logged as a MARK (with extra, e.g. the next algorithm)
            w_ = C.wan_apply(rtt_ms=r)["settings"]; _wan["t"] = 0
            self.say("MARK " + json.dumps({"ts": round(time.time(), 2), **{k_: w_[k_] for k_ in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp")}, **extra}))
        if rtts:
            self.rtt_restore = (wan(fresh=True).get("settings") or {}).get("rtt_ms")   # put back when the run ends (after())
            C.wan_apply(rtt_ms=rtts[0]); _wan["t"] = 0; step_end = time.time() + o["step_s"]
            self.say(f"RTT sweep: {', '.join(f'{r:g}' for r in rtts)} ms, {o['step_s']} s each" + (", within each algorithm's turn" if seq else ""))
        def flow(h, br, tries=0):
            f = C.Flow(h, max(5, int((slot_end if seq else end) - time.time()) + 2), t0, bitrate=f"{br:g}M" if br else None)
            f.started_at, f.br, f.tries = time.time(), br, tries; f.start(); return f
        def start(br):
            fs = [flow(h, br) for h in hs]
            self.say(f"flows started: {', '.join(h['name'] + ' -> ' + h['peer'] for h in hs)}, iperf3 -b {f'{br:g}M' if br else 'unlimited'}"); return fs
        def stop(fs):
            C._parallel(lambda n: C.run(n, "pkill -INT -f '[i]perf3 -c'", timeout=10), [h["name"] for h in hs])
            for f in fs: f.join(30)
            for f in fs:   # a stopped iperf3 reports no receiver total: the segment's figures come from its per-second samples (sender side)
                sm = f.samples; rt = [x["rtt_ms"] for x in sm if x["rtt_ms"]]
                segs.append({**f.summary(), "bitrate_mbit": f.br, "from_s": round(f.started_at - t0, 1), "to_s": round(time.time() - t0, 1),
                             "mbps": round(sum(x["mbps"] for x in sm) / len(sm), 2) if sm else 0, "retransmits": sum(x["retr"] for x in sm),
                             "rtt_ms": {"min": min(rt, default=0), "mean": round(sum(rt) / len(rt), 2) if rt else 0, "max": max(rt, default=0)},
                             "error": None if "interrupt" in (f.error or "") else f.error})
        d0 = path_drops(); w0 = wan(fresh=True); self.say("START " + json.dumps({"ts": round(t0, 2), **{k_: (w0.get("settings") or {}).get(k_) for k_ in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp")}, "bitrate_mbit": self.ctl["bitrate"],
                                                                **({"algo": hs[0]["cc"]} if seq else {})}))
        fs = start(self.ctl["bitrate"]); br = self.ctl["bitrate"]
        def flush(fs):
            for f in fs:
                for smp in f.samples[sent.get(id(f), 0):]: self.say("SAMPLE " + json.dumps({**smp, "bitrate_mbit": f.br}))
                sent[id(f)] = len(f.samples)
        while True:
            flush(fs)
            for i, f in enumerate(fs):   # the peer's iperf3 server can still be closing the previous test: try again
                if not f.is_alive() and "busy" in (f.error or "") and f.tries < 20: time.sleep(0.5); fs[i] = flow(f.h, f.br, f.tries + 1)
            w = wan()
            if "error" not in w and (not last_w or w["at"] > last_w["at"]):
                d = w[o["direction"]]
                if last_w:
                    dt, p = w["at"] - last_w["at"], last_w[o["direction"]]
                    self.say("WAN " + json.dumps({"ts": round(w["at"], 2), "mbps": round(max(0, d["bytes"] - p["bytes"]) * 8 / dt / 1e6, 1), "drops_s": round(max(0, d["drops"] - p["drops"]) / dt, 1),
                                                  "q_ms": round(d["backlog_bytes"] * 8 / (w["settings"]["rate_mbit"] * 1000), 2), "q_kb": round(d["backlog_bytes"] / 1000, 1)}))
                last_w = w
            if seq and (self.ctl["next"] or time.time() >= slot_end) and k + 1 < len(hs_all):   # the next algorithm's turn
                self.ctl["next"] = False; stop(fs); flush(fs); k += 1; hs[:] = [hs_all[k]]
                if rtts: ri = 0; set_rtt(rtts[0], algo=hs[0]["cc"]); step_end = time.time() + o["step_s"]   # its turn starts at the first RTT
                else: self.say("MARK " + json.dumps({"ts": round(time.time(), 2), "algo": hs[0]["cc"]}))
                slot_end = time.time() + o["slot_s"]; fs = start(br); continue
            if rtts and time.time() >= step_end and ri + 1 < len(rtts):   # the sweep's next RTT, in place
                ri += 1; set_rtt(rtts[ri]); step_end = time.time() + o["step_s"]
            if seq and self.ctl["next"]: self.ctl["next"] = False; break   # Next on the last algorithm ends the run
            if rtts and not seq and ri + 1 == len(rtts) and time.time() >= step_end: break   # together: the sweep's last step is over
            if self.ctl["stop"] or time.time() >= end or (seq and time.time() >= slot_end) or all(not f.is_alive() for f in fs): break
            if self.ctl["bitrate"] != br:
                br = self.ctl["bitrate"]; stop(fs); flush(fs)
                self.say("MARK " + json.dumps({"ts": round(time.time(), 2), "bitrate_mbit": br})); fs = start(br)
            time.sleep(0.25)
        stop(fs); flush(fs)
        self.result = {"mode": "live", "direction": o["direction"], "algos": o["algos"], "wan": w0.get("settings"), "host_mtu": (mtu() or {}).get("host_mtu"),
                       "seconds": round(time.time() - t0, 1), "segments": segs, "path_drops": drops_since(d0)}
        self.result["sequential"] = seq
        if rtts: self.result["rtt_sweep"] = rtts
        s["summary"] = f"{self.result['seconds']} s, " + (f"{k + 1} of {len(hs_all)} algorithms in turn" if seq else f"RTT sweep, {ri + 1} of {len(rtts)} steps" if rtts else f"{len(segs) // max(1, len(hs))} bit-rate segment(s)")
        bad = [g for g in segs if g["error"]]
        if bad: raise RuntimeError("; ".join(f"{g['cc']}: {g['error']}" for g in bad)[:300])

    def do_verify(self, s):
        rc = self.sh([LAB / "lab.sh", "verify"], check=False)
        s["summary"] = next((l["line"].strip() for l in reversed(self.log) if "pairs ok" in l["line"]), "")
        if rc: raise RuntimeError(s["summary"] or "verify failed")

    def do_test(self, s):
        suite = self.options.get("suite")
        self.record_tests(LAB / "results", s, self.sh([LAB / "tests" / "run.sh", *([f"suites/{suite}.robot"] if suite else [])], check=False))

    def after(self):
        if getattr(self, "rtt_restore", None) is not None:   # an RTT sweep puts the RTT back as it was, however the run ended
            try: C.wan_apply(rtt_ms=self.rtt_restore); self.say(f"RTT back to {self.rtt_restore:g} ms")
            except Exception as e: self.say(f"could not put the RTT back: {e}")   # noqa: BLE001
        _wan["t"] = 0
        if self.mode in LIVE_MODES:   # keep the nodes' CPU / memory for the run's span with the run (the report needs it after a portal restart)
            t0 = (self.steps[0].get("started") or self.started) - 2
            self.node_stats = {n: [x for x in v["samples"] if t0 <= x[0] <= (self.finished or time.time()) + 2] for n, v in _stats.items()}
            self.persist()


install_runs_api(app, registry, resume_factory=lambda d: Run(d["mode"], d.get("options"), resume_of=d))


@app.post("/api/runs", tags=["runs"], summary="Start a run: compete | solo (flows) | verify | test (all or one suite)")
def start_run(req: RunRequest):
    if req.mode not in (*LIVE_MODES, "verify", "test"): raise HTTPException(422, "mode: compete, solo, live, verify or test")
    o = dict(req.options)
    if req.mode in LIVE_MODES:
        o["bitrate_mbit"] = float(o.get("bitrate_mbit") or 0)
        if not 0 <= o["bitrate_mbit"] <= BITRATE_MAX: raise HTTPException(422, f"options.bitrate_mbit: 0 (unlimited) .. {BITRATE_MAX}")
    if req.mode == "live":
        o["max_s"] = int(o.get("max_s") or 300); o["duration"] = o["max_s"]
        if not 30 <= o["max_s"] <= 1800: raise HTTPException(422, "options.max_s: 30 .. 1800 s")
    if req.mode in LIVE_MODES:
        if req.mode != "live": o["duration"] = int(o.get("duration") or (30 if req.mode == "compete" else 20))
        if req.mode != "live" and not 5 <= o["duration"] <= 300: raise HTTPException(422, "options.duration: 5 .. 300 s")
        o["direction"] = o.get("direction") or "a2b"
        if o["direction"] not in ("a2b", "b2a"): raise HTTPException(422, "options.direction: a2b or b2a")
        o["algos"] = [a for a in C.ALGOS if a in (o.get("algos") or C.ALGOS)]
        if not o["algos"]: raise HTTPException(422, "options.algos: some of cubic, bbr, reno")
        o["stagger"] = float(o.get("stagger") or 0) if req.mode != "live" else 0
        if not 0 <= o["stagger"] <= 60: raise HTTPException(422, "options.stagger: 0 .. 60 s")
        if req.mode == "live":   # sequential: one algorithm at a time, slot_s each (Next moves on early)
            o["sequential"] = bool(o.get("sequential")); o["rtt_sweep"] = bool(o.get("rtt_sweep"))
            if o["rtt_sweep"]:   # the RTT steps through SWEEP_RTTS, step_s each (one at a time: within each algorithm's turn)
                o["rtts"] = list(SWEEP_RTTS); o["step_s"] = int(o.get("step_s") or 30)
                if not 10 <= o["step_s"] <= 600: raise HTTPException(422, "options.step_s: 10 .. 600 s")
                o["slot_s"] = o["step_s"] * len(o["rtts"]) if o["sequential"] else None
                o["max_s"] = o["duration"] = o["step_s"] * len(o["rtts"]) * (len(o["algos"]) if o["sequential"] else 1) + 5 * len(o["algos"])
            elif o["sequential"]:
                o["slot_s"] = int(o.get("slot_s") or 60)
                if not 10 <= o["slot_s"] <= 600: raise HTTPException(422, "options.slot_s: 10 .. 600 s")
                o["max_s"] = o["duration"] = o["slot_s"] * len(o["algos"]) + 5 * len(o["algos"])
    if req.mode == "test" and o.get("suite") not in (None, "", *SUITES): raise HTTPException(422, f"options.suite: one of {', '.join(SUITES)}")
    if not all_running(): raise HTTPException(409, "the lab is not (fully) running: power it on from the Lab Hub")
    return registry.start(Run(req.mode, o))


_report_lock = threading.Lock()


def portal_shot(d, out_png):
    """The portal as it shows run d (finished): for the report's appendix D. None if it could not be made (the report goes on without it)."""
    try:
        import urllib.request
        runs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/runs", timeout=20))
        w = dict(wan()); log = d.get("log") or []
        last = {k: v for k, v in ((d.get("result") or {}).get("wan") or {}).items() if k in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp")}   # the WAN as the run left it: as it started, then each MARK
        for l in log:
            if l["line"].startswith(("START ", "MARK ")):
                try: last.update({k: v for k, v in json.loads(l["line"].split(" ", 1)[1]).items() if k in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp")})
                except ValueError: pass
        if w.get("settings"):   # with what follows from them, as wanem.sh works it out (BDP; buffer = BUFFER x BDP, 36 kB .. 128 MB)
            ws = {**w["settings"], **last}; bdp = ws["rate_mbit"] * 1e6 / 8 * ws["rtt_ms"] / 1000
            ws.update(bdp_bytes=int(bdp), queue_bytes=int(min(134217728, max(36056, bdp * ws["buffer_bdp"]))), one_way_us=int(ws["rtt_ms"] * 500)); w["settings"] = ws
        st = {**state(), "busy": False, "active": None, "active_mode": None, "at": d.get("finished") or time.time()}
        ns = d.get("node_stats") or {}
        stubs = {"/api/lab": lab(), "/api/state": st, "/api/runs": runs, "/api/wan": w,
                 "/api/nodestats": {"now": d.get("finished") or time.time(), "nodes": {k: {"vcpus": (_stats.get(k) or {}).get("vcpus"), "samples": v} for k, v in ns.items()}}}
        return report_shot.screenshot(stubs, {**d, "log_offset": 0}, out_png)
    except Exception as e:   # noqa: BLE001
        print(f"portal screenshot failed: {e}", file=sys.stderr); return None


@app.get("/api/runs/{run_id}/report.pdf", tags=["runs"], summary="PDF report of a finished compete / solo / live run: setup, changes, per-second charts, results, nodes' CPU and memory")
def run_report(run_id: str):
    d = registry.load(run_id)
    if not d: raise HTTPException(404, "no such run")
    if d["mode"] not in LIVE_MODES: raise HTTPException(422, "reports are for compete, solo and live runs")
    if d["status"] in ("queued", "running"): raise HTTPException(409, "the run has not finished yet")
    out = RUNS_DIR / f"{run_id}.report-{VERSION}.pdf"   # rebuilt when the report format changes
    with _report_lock:
        if not out.exists():
            a = _stats.get("a-cubic") or {}; last = a["samples"][-1] if a.get("samples") else None
            info = (f"Alpine Linux VMs, {a.get('vcpus') or '?'} vCPU and {round(last[3] / 1000, 1) if last else '?'} GB usable RAM each; fq on eth1; "
                    "the same sysctls on every host apart from the congestion control")
            inv = C.inventory(); configs = C._parallel(node_sections, [n["name"] for n in inv["nodes"]])   # appendix A: every device, live
            shot = portal_shot(d, RUNS_DIR / f"{run_id}.portal.png")   # appendix D
            try: pdfreport.build_pdf(pdfreport.build_html(d, VERSION, {n: list(v["samples"]) for n, v in _stats.items()}, info,
                                                          {k: v for k, v in inv.items() if k != "by_name"}, configs, portal_png=shot), out)
            except Exception as e: raise HTTPException(500, f"report failed: {str(e)[:300]}")   # noqa: BLE001
            finally:
                if shot: Path(shot).unlink(missing_ok=True)
    return FileResponse(out, media_type="application/pdf", filename=f"tcp-cc-lab-{d['mode']}-{run_id}.pdf")


def live_run():
    a = active_run()
    if not a or a.mode != "live": raise HTTPException(409, "no live test is going")
    return a


class Bitrate(BaseModel):
    bitrate_mbit: float = Field(..., description=f"iperf3 -b per flow, Mbit/s; 0 = unlimited; 0..{BITRATE_MAX}")


@app.post("/api/live/bitrate", tags=["runs"], summary="Change the live test's sender bit rate: its flows are stopped and new ones started with the new iperf3 -b")
def live_bitrate(b: Bitrate):
    if not 0 <= b.bitrate_mbit <= BITRATE_MAX: raise HTTPException(422, f"bitrate_mbit: 0 (unlimited) .. {BITRATE_MAX}")
    a = live_run(); a.ctl["bitrate"] = b.bitrate_mbit; return {"id": a.id, "bitrate_mbit": b.bitrate_mbit}


@app.post("/api/live/next", tags=["runs"], summary="One-at-a-time live test: end the current algorithm's flow and start the next one's (on the last, ends the run)")
def live_next():
    a = live_run()
    if not a.options.get("sequential"): raise HTTPException(409, "the live test runs its algorithms together")
    a.ctl["next"] = True; return {"id": a.id}


@app.post("/api/live/stop", tags=["runs"], summary="Stop the live test (its flows end cleanly and the run finishes)")
def live_stop():
    a = live_run(); a.ctl["stop"] = True; return {"id": a.id}


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
    start_stats()
    import uvicorn; uvicorn.run(app, host=os.environ.get("WEBAPP_HOST", "0.0.0.0"), port=PORT)


if __name__ == "__main__": main()

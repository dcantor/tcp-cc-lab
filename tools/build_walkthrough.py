#!/usr/bin/env python3
"""Build docs/tcp-cc-lab-walkthrough.pdf from docs/walkthrough.src.html.

    build_walkthrough.py --refresh     collect everything from the running lab into docs/data/ and docs/img/ (~12 min:
                                       show commands, the experiments, a portal run and its screenshots), then build
    build_walkthrough.py               build from docs/data/ as collected last time
    build_walkthrough.py --sweep500    re-run only walkthrough J (the 500 Mbit/s RTT series at MTU 1500 and 9000, ~16 min), then build
    build_walkthrough.py --html-only   stop after docs/walkthrough.html

Placeholders in the source: {{text:NAME}} (docs/data/NAME.txt, preformatted), {{val:NAME.path}} (a value from
docs/data/NAME.json), {{file:PATH}} / {{code:PATH:A-B}} (repository files), {{chart:NAME}}, {{table:NAME}}, {{img:NAME}}, {{json:NAME}},
{{results}}, {{version}}, {{date}}, {{tests}}, {{passed}}. Headless Chrome prints the HTML (from file://: Chrome on this
host hangs on http:// pages, so the portal screenshots load a copy of the page fed with recorded API answers).
Run with webapp/.venv/bin/python (websockets, for the screenshots)."""
import asyncio
import base64
import html
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
DOCS, DATA, IMG = LAB / "docs", LAB / "docs" / "data", LAB / "docs" / "img"
SRC, STYLE, OUT_HTML, OUT_PDF = DOCS / "walkthrough.src.html", DOCS / "walkthrough.style.html", DOCS / "walkthrough.html", DOCS / "tcp-cc-lab-walkthrough.pdf"
sys.path.insert(0, str(LAB / "tools"))
E = html.escape
ENV = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
PORTAL = "http://127.0.0.1:8112"
COL = {"cubic": "#2a78d6", "bbr": "#eb6834", "reno": "#1baf7a"}   # the portal's slots: blue / orange / aqua
NAME = {"cubic": "CUBIC", "bbr": "BBR", "reno": "Reno"}
BOTTLE = (100, "bottleneck 100 Mbit/s")


def sh(*args, timeout=600):
    r = subprocess.run([str(LAB / "lab.sh"), *args], capture_output=True, text=True, timeout=timeout, cwd=LAB, env=ENV)
    return re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout + r.stderr)).rstrip()


def put(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    p = DATA / (name + (".txt" if isinstance(obj, str) else ".json"))
    p.write_text(obj.rstrip() + "\n" if isinstance(obj, str) else json.dumps(obj, indent=1))
    print(f"  data/{p.name}", file=sys.stderr, flush=True)


# ---- collection -------------------------------------------------------------------------------------------------------
def solo(C, algos=("cubic", "bbr", "reno"), duration=20):
    """Each algorithm alone, in turn, keeping its per-second samples (cclab's solo summary drops them)."""
    out = []
    for h in C.senders("a2b", list(algos)):
        before = C.wan_stats()["a2b"]
        f = C.Flow(h, duration, time.time()); f.start(); f.join(duration + 60)
        after = C.wan_stats()["a2b"]
        out.append({**f.summary(), "samples": f.samples, "drops": after["drops"] - before["drops"],
                    "bytes_per_packet": round((after["bytes"] - before["bytes"]) / max(1, after["packets"] - before["packets"]))})
        time.sleep(2)
    return out


def rtt_change(C, cc, duration=30, at=10, new_rtt=120):
    h = next(n for n in C.inventory()["nodes"] if n["role"] == "host" and n["site"] == "a" and n["cc"] == cc)
    t0 = time.time(); f = C.Flow(h, duration, t0); f.start()
    time.sleep(at); C.wan_apply(rtt_ms=new_rtt); f.join(duration + 60)
    C.wan_apply(rtt_ms=30)
    return {"summary": f.summary(), "samples": f.samples, "at": at, "new_rtt": new_rtt}


def portal_cookie():
    return subprocess.run([str(Path.home() / "lab-portal/.venv/bin/python"), "-c",
                           "from labportal.hub import auth; import json; print(auth.issue(next(iter(json.load(open(auth.FILE))['users']))))"],
                          capture_output=True, text=True, cwd=Path.home() / "lab-portal").stdout.strip()


def portal(path, body=None, cookie=None, raw=False):
    req = urllib.request.Request(PORTAL + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", "Cookie": f"labhub_session={cookie}"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as r:
        d = r.read().decode()
    return d if raw else json.loads(d)


def portal_run(cookie):
    """A compete run started through the portal, with two RTT changes from the portal's WAN card while it goes: the run's
    log then carries the MARKs the chart draws. Returns the recorded API answers for the screenshot page."""
    rec = {p: portal(p, cookie=cookie) for p in ("/api/lab",)}
    r = portal("/api/runs", {"mode": "compete", "options": {"duration": 45, "direction": "a2b", "algos": ["cubic", "bbr", "reno"], "stagger": 0}}, cookie)
    time.sleep(16); portal("/api/wan", {"rtt_ms": 100}, cookie)
    time.sleep(15); portal("/api/wan", {"rtt_ms": 30}, cookie)
    while portal(f"/api/runs/{r['id']}", cookie=cookie)["status"] == "running": time.sleep(3)
    time.sleep(2)
    for p in ("/api/state", "/api/wan", "/api/mtu", "/api/runs"): rec[p] = portal(p, cookie=cookie)
    rec["RUN"] = portal(f"/api/runs/{r['id']}?since=0", cookie=cookie)
    rec["RUN_ID"] = r["id"]
    return rec


def mock_page(rec, theme):
    src = (LAB / "webapp" / "static" / "index.html").read_text()
    shim = ("<script>const REC=" + json.dumps(rec).replace("</", "<\\/") + ";\n"
            "try { localStorage.setItem('portal-theme', " + json.dumps(theme) + "); } catch (e) { }\n"
            "window.fetch = async (p) => { p = String(p); let body = p.startsWith('/api/runs/') ? REC.RUN : REC[p.split('?')[0]];\n"
            "  if (p.includes('since=') && !p.endsWith('since=0')) body = {...REC.RUN, log: [], log_offset: REC.RUN.log.length};\n"
            "  return {ok: !!body, json: async () => body || {}, statusText: 'x'}; };</script>\n")
    src = src.replace("<head>", "<head>\n<script>try { localStorage.setItem('portal-theme', " + json.dumps(theme) + "); } catch (e) { }</script>", 1)
    i = src.index("<script>\nconst $ = ")
    src = src[:i] + shim + src[i:]
    src = src.replace("</body>", f"<script>setTimeout(() => openRun({json.dumps(rec['RUN_ID'])}), 1500)</script></body>", 1)
    return src


def screenshot(url, out, width=1400, wait=6.0, dark=False):
    import websockets
    prof = tempfile.mkdtemp(prefix="tcpcc-wt-")
    port = 9447
    p = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-debugging-address=127.0.0.1",
                          f"--remote-debugging-port={port}", "--remote-allow-origins=*", f"--user-data-dir={prof}", "--hide-scrollbars", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for _ in range(50):
            try: json.load(op.open(f"http://127.0.0.1:{port}/json", timeout=2)); break
            except Exception: time.sleep(0.2)   # noqa: BLE001
        ws_url = json.load(op.open(urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT"), timeout=5))["webSocketDebuggerUrl"]

        async def main():
            async with websockets.connect(ws_url, max_size=80_000_000, ping_interval=None) as ws:
                n = 0
                async def cmd(m, **pa):
                    nonlocal n; n += 1; await ws.send(json.dumps({"id": n, "method": m, "params": pa}))
                    while True:
                        r = json.loads(await ws.recv())
                        if r.get("id") == n: return r.get("result")
                await cmd("Page.enable")
                await cmd("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=2, mobile=False)
                if dark: await cmd("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
                await cmd("Page.navigate", url=url); await asyncio.sleep(wait)
                h = (await cmd("Runtime.evaluate", expression="document.documentElement.scrollHeight", returnByValue=True))["result"]["value"]
                await cmd("Emulation.setDeviceMetricsOverride", width=width, height=int(h), deviceScaleFactor=2, mobile=False)
                await asyncio.sleep(0.5)
                r = await cmd("Page.captureScreenshot", format="png")
                Path(out).write_bytes(base64.b64decode(r["data"]))
        asyncio.run(main())
    finally:
        os.killpg(p.pid, signal.SIGTERM)
    print(f"  img/{Path(out).name}", file=sys.stderr, flush=True)


def collect():
    import cclab as C
    print("collecting from the lab:", file=sys.stderr)
    C.wan_reset(); C.mtu_set(1500)
    put("status", sh("status"))
    put("usage", sh("help"))
    put("verify", sh("verify"))
    put("cc", sh("cc"))
    put("mtu_show", sh("mtu", "show"))
    put("wan_show", sh("wan", "show"))
    put("wan_stats", C.wan_stats())
    put("ping", C.ping_rtt(count=20))
    put("r1_config", "\n".join(l for l in C.run("r1", "/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands", timeout=60)[1].splitlines()
                               if re.match(r"set (interfaces ethernet eth[12] |protocols static|system host-name)", l) and "hw-id" not in l))
    put("r1_route", C.run("r1", "/opt/vyatta/bin/vyatta-op-cmd-wrapper show ip route", timeout=60)[1].split("IPv4 unicast", 1)[-1].split("\n", 1)[-1].strip())
    put("host_sysctl", C.run("a-bbr", "sysctl net.ipv4.tcp_congestion_control net.ipv4.tcp_available_congestion_control net.core.default_qdisc "
                                      "net.ipv4.tcp_no_metrics_save net.ipv4.tcp_rmem net.ipv4.tcp_wmem net.core.rmem_max net.core.wmem_max; tc qdisc show dev eth1")[1].strip())
    put("wanem_offloads", C.run("wanem", "doas wanem offloads")[1].strip())
    put("wanem_bridge", C.run("wanem", "ip -br link show master br0; ip -br link show br0; bridge link show")[1].strip())

    # A: baselines, each alone at the defaults
    put("solo_default", solo(C))
    # B: all three at once at the defaults
    put("compete_default", C.flows("compete", 40, "a2b", ["cubic", "bbr", "reno"]))
    # C: RTT sweep, all three at once, 1 x BDP
    sweep = []
    for rtt in (10, 30, 80, 160):
        C.wan_apply(rtt_ms=rtt); sweep.append(C.flows("compete", 30, "a2b", ["cubic", "bbr", "reno"]))
    C.wan_reset(); put("sweep_rtt", sweep)
    # D: the buffer — bloat (4 x BDP, alone) and shallow (1/4 x BDP at 100 ms, together)
    C.wan_apply(buffer_bdp=4); put("solo_bloat", solo(C)); C.wan_reset()
    C.wan_apply(rtt_ms=100, buffer_bdp=0.25); put("compete_shallow", C.flows("compete", 30, "a2b", ["cubic", "bbr", "reno"])); C.wan_reset()
    # E: random loss, each alone
    C.wan_apply(loss_pct=1); put("solo_loss", solo(C)); C.wan_reset()
    # F: the RTT moves under a running flow
    put("rttchange", {cc: rtt_change(C, cc) for cc in ("cubic", "bbr")})
    # G: staggered start: CUBIC first, BBR 10 s later, Reno 10 s after that
    put("compete_stagger", C.flows("compete", 40, "a2b", ["cubic", "bbr", "reno"], stagger=10))
    # H: jumbo frames
    C.mtu_set(9000)
    put("mtu_show_9000", sh("mtu", "show"))
    put("ping_df_9000", C.run("a-cubic", f"ping -c 3 -M do -s 8972 {C.node('b-cubic')['ip']}")[1].strip())
    put("solo_9000", solo(C, duration=15))
    C.mtu_set(1500)
    put("ping_df_1500", C.run("a-cubic", f"ping -c 2 -M do -s 1600 {C.node('b-cubic')['ip']} 2>&1; true")[1].strip())
    put("solo_1500", solo(C, duration=15))
    C.wan_reset()
    collect_host_tcp(C)
    collect_sweep500(C)
    collect_tcp_mem_check(C)
    # the portal: metrics, SD, a run with RTT changes, screenshots
    ck = portal_cookie()
    put("metrics", "\n".join(l for l in portal("/metrics", cookie=ck, raw=True).splitlines()
                             if re.match(r"tcpcc_(wan_|bottleneck_|host_cc_ok|host_mtu|link_mtu\{node=\"r1|last_compete|lab_run)", l))[:4000])
    put("sd", portal("/api/sd", cookie=ck))
    rec = portal_run(ck); put("portal_rec", rec)
    C.wan_reset()
    shoot()


def collect_sweep500(C=None, mtus=(1500, 9000)):
    """J: each algorithm alone at RTT 0, 10, 30, 60, 100, 150 ms with the bottleneck at 500 Mbit/s (cclab sweep), once with
    the hosts at MTU 1500 (sweep500) and once at 9000 (sweep500_9000); the hosts end at 1500."""
    if C is None:
        import cclab as C
    try:
        for mtu in mtus:
            C.mtu_set(mtu)
            put("sweep500" if mtu == 1500 else f"sweep500_{mtu}", C.sweep(C.SWEEP_RTTS, 500, 30, "a2b", C.ALGOS, "500M"))
    finally:
        C.mtu_set(1500); C.wan_reset()


def collect_host_tcp(C=None):
    """The hosts' TCP settings: kernel, the tcp_* / net.core sysctls, eth1's MTU, offloads and qdisc, from all six."""
    if C is None:
        import cclab as C
    hosts = [n["name"] for n in C.inventory()["nodes"] if n["role"] == "host"]
    cmd = ("uname -r; echo ---; sysctl -a 2>/dev/null | grep -E '^net[.]ipv4[.]tcp_|^net[.]core[.](rmem_|wmem_|default_qdisc|netdev_max_backlog|optmem_max)'; echo ---; "
           "cat /sys/class/net/eth1/mtu; echo ---; ethtool -k eth1 | grep -E '^(tcp-segmentation-offload|generic-segmentation-offload|generic-receive-offload|tx-checksumming|rx-checksumming|scatter-gather):'; "
           "echo ---; tc qdisc show dev eth1")
    out = {}
    for h in hosts:
        rc, txt = C.run(h, cmd, timeout=20)
        kern, sysc, mtu, off, qd = (x.strip() for x in (txt.split("---") + [""] * 5)[:5])
        out[h] = {"kernel": kern, "mtu": int(mtu) if mtu.isdigit() else mtu, "qdisc": qd,
                  "sysctl": dict(l.split(" = ", 1) for l in sysc.splitlines() if " = " in l),
                  "offloads": dict((a.strip(), b.strip()) for a, b in (l.split(":", 1) for l in off.splitlines() if ":" in l))}
    put("host_tcp", out)


def collect_tcp_mem_check(C=None, rtt=150, rate=500, duration=30):
    """Does tcp_mem (sized from the hosts' small RAM) bite at 500 Mbit/s on a long path? One CUBIC flow alone, counting
    TcpExt TCPMemoryPressures on the sender and the receiver around it."""
    if C is None:
        import cclab as C
    def pressures(h):
        k, v = (l.split() for l in C.run(h, "grep TcpExt: /proc/net/netstat")[1].splitlines()[:2])
        return int(dict(zip(k, v))["TCPMemoryPressures"])
    snd = next(n for n in C.inventory()["nodes"] if n["role"] == "host" and n["site"] == "a" and n["cc"] == "cubic")
    ram = int(C.run(snd["name"], "awk '/MemTotal/{print int($2/1024)}' /proc/meminfo")[1].strip())
    mem = [int(x) for x in C.run(snd["name"], "cat /proc/sys/net/ipv4/tcp_mem")[1].split()]
    try:
        C.mtu_set(1500); wan = C.wan_apply(rtt, rate)["settings"]
        b = (pressures(snd["name"]), pressures(snd["peer"]))
        f = C.Flow(snd, duration, time.time(), bitrate="500M"); f.start(); f.join(duration + 60)
        a = (pressures(snd["name"]), pressures(snd["peer"]))
    finally:
        C.wan_reset()
    put("tcp_mem_check", {"ram_mb": ram, "tcp_mem_pages": mem, "pressure_mb": mem[1] * 4096 / 1e6, "high_mb": mem[2] * 4096 / 1e6,
                          "rtt_ms": rtt, "rate_mbit": rate, "bdp_mb": wan["bdp_bytes"] / 1e6, "duration": duration, "mbps": f.summary()["mbps"],
                          "sender_pressures": a[0] - b[0], "receiver_pressures": a[1] - b[1]})


def shoot():
    rec = json.loads((DATA / "portal_rec.json").read_text())
    IMG.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        page = IMG / f"portal-{theme}.html"
        page.write_text(mock_page(rec, theme))
        screenshot(f"file://{page}", IMG / f"portal-{theme}.png", dark=theme == "dark")
        page.unlink()


# ---- rendering helpers ------------------------------------------------------------------------------------------------
def j(name):
    return json.loads((DATA / f"{name}.json").read_text())


def f1(x): return "–" if x is None else f"{x:.1f}"


def nice_max(v):
    if v <= 0: return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * e: return m * e
    return 10 * e


def svg_lines(series, ylabel, xlabel="seconds", marks=(), ymax=None, h=230, w=680, log=False, legend=True, hline=None, xticks=None, dots=False):
    """series: [(key, label, [(x, y)])] — 2px lines in the algorithm's colour, a legend plus end labels, recessive grid;
    marks: [(x, text)] vertical dashed rules (an RTT change); hline: (y, text) a reference level."""
    left, right, top, bot = 48, 70, 24 if legend else 10, 34
    xs = [x for _, _, pts, *_ in series for x, _ in pts] or [0, 1]
    ys = [y for _, _, pts, *_ in series for _, y in pts] or [0, 1]
    x0, x1 = 0, max(xs)
    y1 = ymax or nice_max(max(ys) * 1.05)
    X = lambda x: left + (x - x0) / max(1e-9, x1 - x0) * (w - left - right)
    Y = lambda y: top + (1 - min(y, y1) / y1) * (h - top - bot)
    s = [f"<svg viewBox='0 0 {w} {h}' width='100%' xmlns='http://www.w3.org/2000/svg' style='font-size:10.5px'>"]
    for i in range(6):
        v = y1 * i / 5; y = Y(v)
        s.append(f"<line x1='{left}' y1='{y:.1f}' x2='{w - right}' y2='{y:.1f}' stroke='{'#d4d4d0' if i == 0 else '#ecebe8'}'/>")
        s.append(f"<text x='{left - 6}' y='{y + 3.5:.1f}' text-anchor='end' fill='#52514e'>{v:g}</text>")
    step = 5 if x1 <= 50 else 10
    for t in (xticks or range(0, int(x1) + 1, step)):
        s.append(f"<text x='{X(t):.1f}' y='{h - bot + 14}' text-anchor='middle' fill='#52514e'>{t}</text>")
    s.append(f"<text x='{(left + w - right) / 2:.0f}' y='{h - 4}' text-anchor='middle' fill='#52514e'>{E(xlabel)}</text>")
    s.append(f"<text x='12' y='{(top + h - bot) / 2:.0f}' transform='rotate(-90 12 {(top + h - bot) / 2:.0f})' text-anchor='middle' fill='#52514e'>{E(ylabel)}</text>")
    if hline:
        y = Y(hline[0])
        s.append(f"<line x1='{left}' y1='{y:.1f}' x2='{w - right}' y2='{y:.1f}' stroke='#8a8984' stroke-dasharray='2 3'/>")
        s.append(f"<text x='{w - right - 4}' y='{y - 4:.1f}' text-anchor='end' fill='#52514e' stroke='#fff' stroke-width='3' paint-order='stroke'>{E(hline[1])}</text>")
    for x, text in marks:
        s.append(f"<line x1='{X(x):.1f}' y1='{top}' x2='{X(x):.1f}' y2='{h - bot}' stroke='#8a8984' stroke-dasharray='4 3'/>")
        s.append(f"<text x='{X(x) + 4:.1f}' y='{top + 10}' fill='#52514e'>{E(text)}</text>")
    ends = []
    for key, label, pts, *dash in series:
        if not pts: continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(pts))
        da = f" stroke-dasharray='{dash[0]}'" if dash and dash[0] else ""
        s.append(f"<path d='{d}' fill='none' stroke='{COL.get(key, '#52514e')}' stroke-width='2' stroke-linejoin='round'{da}/>")
        if dots:
            s.extend(f"<circle cx='{X(x):.1f}' cy='{Y(y):.1f}' r='4' fill='{COL.get(key, '#52514e')}' stroke='#fff' stroke-width='2'/>" for x, y in pts)
        ends.append([Y(pts[-1][1]), label, pts[-1][1]])
    ends.sort()
    for i in range(1, len(ends)):   # keep end labels 12px apart
        if ends[i][0] - ends[i - 1][0] < 12: ends[i][0] = ends[i - 1][0] + 12
    for y, label, v in ends:
        s.append(f"<text x='{w - right + 6}' y='{y + 3.5:.1f}' fill='#0b0b0b'>{E(label)}</text>")
    if legend:
        lx = left
        for key, label, _, *dash in series:
            if dash and dash[0]:
                s.append(f"<line x1='{lx}' y1='7.5' x2='{lx + 14}' y2='7.5' stroke='{COL.get(key, '#52514e')}' stroke-width='2' stroke-dasharray='{dash[0]}'/><text x='{lx + 19}' y='11' fill='#0b0b0b'>{E(label)}</text>")
            else:
                s.append(f"<rect x='{lx}' y='6' width='14' height='3' rx='1.5' fill='{COL.get(key, '#52514e')}'/><text x='{lx + 19}' y='11' fill='#0b0b0b'>{E(label)}</text>")
            lx += 28 + 6.5 * len(label)
    s.append("</svg>")
    return "".join(s)


def by_cc(samples, field, t_of=lambda s: s["t"]):
    out = {}
    for s in samples: out.setdefault(s["cc"], []).append((t_of(s), s[field]))
    return out


def chart_compete(name, field="mbps", ylabel="Mbit/s", ymax=None, marks=(), hline=None):
    r = j(name); per = by_cc(r["samples"], field)
    return svg_lines([(cc, NAME[cc], per.get(cc, [])) for cc in ("cubic", "bbr", "reno")], ylabel, ymax=ymax, marks=marks, hline=hline)


def chart_solo(name, field, ylabel, ymax=None, hline=None):
    rs = j(name); series = []
    for r in rs:
        pts = [(i + 1, s[field]) for i, s in enumerate(r["samples"])]
        series.append((r["cc"], NAME[r["cc"]], pts))
    return svg_lines(series, ylabel, xlabel="seconds since each flow started (run one after the other)", ymax=ymax, hline=hline)


def chart_rttchange(field, ylabel, hline=None):
    d = j("rttchange"); series = []
    at = d["cubic"]["at"]
    for cc in ("cubic", "bbr"):
        series.append((cc, NAME[cc], [(i + 1, s[field]) for i, s in enumerate(d[cc]["samples"])]))
    return svg_lines(series, ylabel, xlabel="seconds since the flow started (each run alone)", marks=[(at, f"RTT 30 → {d['cubic']['new_rtt']} ms")], hline=hline)


def chart_stagger():
    r = j("compete_stagger"); per = by_cc(r["samples"], "mbps")
    return svg_lines([(cc, NAME[cc], per.get(cc, [])) for cc in ("cubic", "bbr", "reno")], "Mbit/s", xlabel="seconds since CUBIC started", hline=BOTTLE,
                     marks=[(10, "BBR joins"), (20, "Reno joins")])


CHARTS = {
    "compete_mbps": lambda: chart_compete("compete_default", hline=BOTTLE),
    "compete_rtt": lambda: chart_compete("compete_default", "rtt_ms", "smoothed RTT, ms"),
    "solo_mbps": lambda: chart_solo("solo_default", "mbps", "Mbit/s", hline=BOTTLE),
    "bloat_rtt": lambda: chart_solo("solo_bloat", "rtt_ms", "smoothed RTT, ms", hline=(30, "the emulated RTT (30 ms)")),
    "loss_mbps": lambda: chart_solo("solo_loss", "mbps", "Mbit/s", hline=BOTTLE),
    "rttchange_mbps": lambda: chart_rttchange("mbps", "Mbit/s", hline=BOTTLE),
    "rttchange_rtt": lambda: chart_rttchange("rtt_ms", "smoothed RTT, ms"),
    "stagger": chart_stagger,
}


def chart_sweep():
    """Share of the bottleneck per algorithm at each RTT: one 100 % stacked bar per RTT, 2px gaps, labelled segments."""
    sw = j("sweep_rtt"); W, left, right, top, rowh = 680, 70, 20, 30, 34
    H = top + rowh * len(sw) + 30
    X = lambda v: left + v / 100 * (W - left - right)
    s = [f"<svg viewBox='0 0 {W} {H}' width='100%' xmlns='http://www.w3.org/2000/svg' style='font-size:10.5px'>"]
    lx = left
    for cc in ("cubic", "bbr", "reno"):
        s.append(f"<rect x='{lx}' y='6' width='10' height='10' rx='2' fill='{COL[cc]}'/><text x='{lx + 15}' y='15' fill='#0b0b0b'>{NAME[cc]}</text>"); lx += 70
    s.append(f"<text x='{W - right}' y='15' text-anchor='end' fill='#52514e'>share of 100 Mbit/s, 30 s, all three at once, buffer 1 × BDP</text>")
    for i, r in enumerate(sw):
        y = top + rowh * i
        s.append(f"<text x='{left - 8}' y='{y + rowh / 2:.0f}' text-anchor='end' fill='#0b0b0b'>{r['wan']['rtt_ms']:g} ms</text>")
        x = 0.0
        for cc in ("cubic", "bbr", "reno"):
            v = next((f["mbps"] for f in r["flows"] if f["cc"] == cc), 0)
            if v <= 0: continue
            a, b = X(x), X(x + v)
            s.append(f"<rect x='{a + 1:.1f}' y='{y + 4}' width='{max(0.5, b - a - 2):.1f}' height='{rowh - 12}' rx='3' fill='{COL[cc]}'/>")
            if b - a > 34: s.append(f"<text x='{(a + b) / 2:.1f}' y='{y + rowh / 2 + 1:.0f}' text-anchor='middle' fill='#fff' font-weight='600'>{v:.0f}</text>")
            x += v
    for v in (0, 25, 50, 75, 100):
        s.append(f"<text x='{X(v):.1f}' y='{H - 8}' text-anchor='middle' fill='#52514e'>{v}</text>")
    s.append("</svg>")
    return "".join(s)


CHARTS["sweep"] = chart_sweep


def s500(): return j("sweep500")


def chart_s500(field):
    d = s500(); rtts = [p["rtt_ms"] for p in d["points"]]; series = []
    for cc in d["algos"]:
        pts = []
        for p in d["points"]:
            f = next(f for f in p["flows"] if f["cc"] == cc)
            if field == "mbps": y = f["mbps"]
            else: y = max(0.0, f["rtt_ms"]["mean"] - p["ping"]["min_ms"])   # queueing the flow added on top of the idle path
            pts.append((p["rtt_ms"], y))
        series.append((cc, NAME[cc], pts))
    if field == "mbps":
        return svg_lines(series, "Mbit/s (whole 30 s run)", xlabel="emulated RTT, ms", xticks=rtts, dots=True, ymax=nice_max(d["rate_mbit"] * 1.05),
                         hline=(d["rate_mbit"], f"bottleneck {d['rate_mbit']:g} Mbit/s"))
    return svg_lines(series, "queueing delay added, ms", xlabel="emulated RTT, ms", xticks=rtts, dots=True)


def chart_s500_ramp():
    d = s500(); p = d["points"][-1]
    series = [(f["cc"], NAME[f["cc"]], [(s["t"] if s["t"] else i + 1, s["mbps"]) for i, s in enumerate(f["samples"])]) for f in p["flows"]]
    series = [(k, l, [(i + 1, y) for i, (_, y) in enumerate(pts)]) for k, l, pts in series]
    return svg_lines(series, "Mbit/s", xlabel=f"seconds since each flow started (RTT {p['rtt_ms']:g} ms, each alone)",
                     hline=(d["rate_mbit"], f"bottleneck {d['rate_mbit']:g} Mbit/s"), ymax=nice_max(d["rate_mbit"] * 1.15))


def chart_s500_mtu():
    a, b = j("sweep500"), j("sweep500_9000"); rtts = [p["rtt_ms"] for p in a["points"]]; series = []
    for cc in a["algos"]:
        for d, tag, dash in ((b, "9000", None), (a, "1500", "5 4")):
            series.append((cc, f"{NAME[cc]} {tag}", [(p["rtt_ms"], next(f for f in p["flows"] if f["cc"] == cc)["mbps"]) for p in d["points"]], dash))
    return svg_lines(series, "Mbit/s (whole 30 s run)", xlabel="emulated RTT, ms", xticks=rtts, dots=True, ymax=nice_max(a["rate_mbit"] * 1.05), h=260,
                     hline=(a["rate_mbit"], f"bottleneck {a['rate_mbit']:g} Mbit/s"))


def table_s500_mtu():
    a, b = j("sweep500"), j("sweep500_9000"); rows = []
    for pa, pb in zip(a["points"], b["points"]):
        va, vb = {f["cc"]: f for f in pa["flows"]}, {f["cc"]: f for f in pb["flows"]}
        cells = ""
        for c in a["algos"]:
            gain = (vb[c]["mbps"] / va[c]["mbps"] - 1) * 100 if va[c]["mbps"] else 0
            cells += (f"<td class='num'>{va[c]['mbps']:.0f}</td><td class='num'>{vb[c]['mbps']:.0f}</td><td class='num'>{gain:+.0f} %</td>"
                      f"<td class='num'>{va[c]['retransmits']} / {vb[c]['retransmits']}</td>")
        rows.append(f"<tr><td class='num'>{pa['rtt_ms']:g}</td>{cells}</tr>")
    head = "".join(f"<th colspan=4><span class='sw' style='background:{COL[c]}'></span>{NAME[c]}</th>" for c in a["algos"])
    sub = "<th class='num'>1500</th><th class='num'>9000</th><th class='num'>gain</th><th class='num'>retr 1500/9000</th>" * len(a["algos"])
    return ("<table class='tight'><tr><th rowspan=2 class='num'>RTT (ms)</th>" + head + "</tr><tr>" + sub + "</tr>" + "".join(rows) + "</table>"
            f"<p class='cap'>Mbit/s over 20 s, each alone, bottleneck {a['rate_mbit']:g} Mbit/s, buffer 1 × BDP; hosts at MTU 1500 (MSS {a['points'][0]['flows'][0]['mss']}) and 9000 "
            f"(MSS {b['points'][0]['flows'][0]['mss']}). Two separate series, run one after the other.</p>")


CHARTS["s500_mtu"] = chart_s500_mtu
CHARTS["s500_mbps"] = lambda: chart_s500("mbps")
CHARTS["s500_queue"] = lambda: chart_s500("queue")
CHARTS["s500_ramp"] = chart_s500_ramp

# ---- walkthrough K: the controlled BBR experiment (tools/bbr_experiment.py) -----------------------------------------
def _betacf(a, b, x):
    qab, qap, qam, c, d = a + b, a + 1, a - 1, 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > 1e-300 else 1e-300); h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-300 else 1e-300)
            c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
            h *= d * c
            if aa < 0 and abs(d * c - 1) < 1e-12: return h
    return h


def _ibeta(a, b, x):
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    return bt * _betacf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - bt * _betacf(b, a, 1 - x) / b


def t_sf2(t, df):
    """Two-sided p-value of Student's t."""
    return _ibeta(df / 2, 0.5, df / (df + t * t))


def t_crit(df, alpha=0.05):
    lo, hi = 0.0, 100.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if t_sf2(mid, df) > alpha: lo = mid
        else: hi = mid
    return (lo + hi) / 2


def describe(xs):
    n = len(xs); m = sum(xs) / n
    if n == 1: return {"n": 1, "mean": m, "sd": 0.0, "ci": 0.0, "min": m, "max": m}
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": m, "sd": sd, "ci": t_crit(n - 1) * sd / math.sqrt(n) if n > 1 else 0.0, "min": min(xs), "max": max(xs)}


def welch(a, b):
    da, db = describe(a), describe(b)
    va, vb = da["sd"] ** 2 / da["n"], db["sd"] ** 2 / db["n"]
    if va + vb == 0: return {"t": math.inf, "df": da["n"] + db["n"] - 2, "p": 0.0 if da["mean"] != db["mean"] else 1.0}
    t = (da["mean"] - db["mean"]) / math.sqrt(va + vb)
    df = (va + vb) ** 2 / ((va ** 2 / (da["n"] - 1) if da["n"] > 1 else 0) + (vb ** 2 / (db["n"] - 1) if db["n"] > 1 else 0) or 1e-12)
    return {"t": t, "df": df, "p": t_sf2(t, df)}


def k_analysis():
    d = j("bbr_experiment"); des = d["design"]; cells = []
    for rtt in des["rtts"]:
        for c in des["conditions"]:
            cell = d["cells"].get(f"{c['key']}@{rtt}")
            if not cell: continue
            by = {cc: [r for r in cell["runs"] if r["cc"] == cc] for cc in des["algos"]}
            st = {cc: describe([r["mbps"] for r in rs]) for cc, rs in by.items()}
            rt = {cc: describe([r["retransmits"] for r in rs]) for cc, rs in by.items()}
            q = {cc: describe([max(0.0, r["rtt_mean_ms"] - cell["ping"]["min_ms"]) for r in rs]) for cc, rs in by.items()}
            tests = {o: (welch([r["mbps"] for r in by["bbr"]], [r["mbps"] for r in by[o]]) if len(by["bbr"]) > 1 else {"t": 0, "df": 0, "p": 1.0}) for o in ("cubic", "reno")}
            wins = {o: sum(b["mbps"] > x["mbps"] for b, x in zip(sorted(by["bbr"], key=lambda r: r["trial"]), sorted(by[o], key=lambda r: r["trial"]))) for o in ("cubic", "reno")}
            cells.append({"rtt": rtt, "cond": c["key"], "label": c["label"], "cell": cell, "stats": st, "retr": rt, "queue": q, "tests": tests, "wins": wins,
                          "memp": {cc: sum(r["TCPMemoryPressures"] for r in rs) for cc, rs in by.items()},
                          "errors": sum(1 for rs in by.values() for r in rs if r["error"] or r["cc_reported"] != r["cc"])})
    # Holm-Bonferroni over every BBR comparison
    ps = sorted(((c["tests"][o]["p"], i, o) for i, c in enumerate(cells) for o in ("cubic", "reno")))
    m = len(ps); run = 0.0
    for k, (p, i, o) in enumerate(ps):
        run = max(run, min(1.0, (m - k) * p)); cells[i]["tests"][o]["p_holm"] = run
    return d, cells


def k_cond_label(key): return {"clean": "clean, 1 × BDP", "shallow": "shallow, ¼ × BDP", "loss001": "0.01 % loss", "loss01": "0.1 % loss"}[key]


def chart_k(cond):
    """Grouped bars: mean Mbit/s per algorithm at each RTT, whiskers = 95 % CI, dots = the individual runs."""
    d, cells = k_analysis(); cs = [c for c in cells if c["cond"] == cond]
    W, H, left, right, top, bot = 330, 210, 40, 8, 22, 30
    ymax = d["design"]["rate_mbit"]; Y = lambda v: top + (1 - v / ymax) * (H - top - bot)
    gw = (W - left - right) / max(1, len(cs)); bw = min(22, gw / 4.2)
    s = [f"<svg viewBox='0 0 {W} {H}' width='100%' xmlns='http://www.w3.org/2000/svg' style='font-size:9.5px'>",
         f"<text x='{left}' y='12' fill='#0b0b0b' font-weight='600'>{E(k_cond_label(cond))}</text>"]
    for i in range(6):
        v = ymax * i / 5; y = Y(v)
        s.append(f"<line x1='{left}' y1='{y:.1f}' x2='{W - right}' y2='{y:.1f}' stroke='{'#d4d4d0' if i == 0 else '#ecebe8'}'/><text x='{left - 5}' y='{y + 3:.1f}' text-anchor='end' fill='#52514e'>{v:g}</text>")
    for gi, c in enumerate(cs):
        gx = left + gw * gi + gw / 2
        s.append(f"<text x='{gx:.1f}' y='{H - bot + 13}' text-anchor='middle' fill='#52514e'>{c['rtt']} ms</text>")
        for ai, cc in enumerate(d["design"]["algos"]):
            st = c["stats"][cc]; x = gx + (ai - 1) * (bw + 3) - bw / 2
            s.append(f"<rect x='{x:.1f}' y='{Y(st['mean']):.1f}' width='{bw:.1f}' height='{max(0.5, Y(0) - Y(st['mean'])):.1f}' rx='2' fill='{COL[cc]}' opacity='.85'/>")
            cx = x + bw / 2
            if st["n"] > 1: s.append(f"<line x1='{cx:.1f}' y1='{Y(max(0, st['mean'] - st['ci'])):.1f}' x2='{cx:.1f}' y2='{Y(min(ymax, st['mean'] + st['ci'])):.1f}' stroke='#0b0b0b' stroke-width='1.2'/>")
            for r in c["cell"]["runs"]:
                if r["cc"] == cc and st["n"] > 1: s.append(f"<circle cx='{cx:.1f}' cy='{Y(r['mbps']):.1f}' r='1.6' fill='#fff' stroke='#0b0b0b' stroke-width='.7'/>")
    s.append(f"<text x='{(left + W - right) / 2:.0f}' y='{H - 4}' text-anchor='middle' fill='#52514e'>emulated RTT</text></svg>")
    return "".join(s)


def k_legend():
    return ("<p class='small'>" + " ".join(f"<span class='sw' style='background:{COL[c]}'></span>{NAME[c]} &nbsp;" for c in ("cubic", "bbr", "reno"))
            + " Goodput of each run, Mbit/s; the axis runs to the 500 Mbit/s bottleneck.</p>")


def fmt_p(p):
    return "&lt; 0.0001" if p < 1e-4 else f"{p:.4f}" if p < 0.01 else f"{p:.3f}"


def table_k():
    d, cells = k_analysis(); rows = []
    if d["design"]["trials"] == 1: return table_k1(cells)
    for c in cells:
        st = c["stats"]; b = st["bbr"]["mean"]
        def cmp(o):
            t = c["tests"][o]; ratio = b / st[o]["mean"] if st[o]["mean"] else math.inf
            sig = t["p_holm"] < 0.05
            cls = " class='num'" if not sig else (" class='num' style='font-weight:600'")
            return f"<td{cls}>{ratio:.2f}×</td><td class='num'>{fmt_p(t['p_holm'])}</td><td class='num'>{c['wins'][o]}/{len([r for r in c['cell']['runs'] if r['cc'] == 'bbr'])}</td>"
        rows.append(f"<tr><td>{E(k_cond_label(c['cond']))}</td><td class='num'>{c['rtt']}</td>"
                    + "".join(f"<td class='num'>{st[cc]['mean']:.0f} ± {st[cc]['ci']:.0f}</td>" for cc in ("cubic", "bbr", "reno")) + cmp("cubic") + cmp("reno") + "</tr>")
    head = ("<table class='tight'><tr><th rowspan=2>path</th><th rowspan=2 class='num'>RTT</th><th colspan=3>Mbit/s, mean ± 95 % CI</th><th colspan=3>BBR vs CUBIC</th><th colspan=3>BBR vs Reno</th></tr>"
            "<tr><th class='num'>CUBIC</th><th class='num'>BBR</th><th class='num'>Reno</th>" + "<th class='num'>ratio</th><th class='num'>p (Holm)</th><th class='num'>BBR won</th>" * 2 + "</tr>")
    return head + "".join(rows) + "</table>"


def table_k1(cells):
    rows = []
    for c in cells:
        st = c["stats"]; b = st["bbr"]["mean"]
        def ratio(o):
            r = b / st[o]["mean"] if st[o]["mean"] else math.inf
            return f"<td class='num'>{'<b>' if r > 1.05 else ''}{r:.2f}×{'</b>' if r > 1.05 else ''}</td>"
        rows.append(f"<tr><td>{E(k_cond_label(c['cond']))}</td><td class='num'>{c['rtt']}</td>"
                    + "".join(f"<td class='num'>{st[cc]['mean']:.1f}</td>" for cc in ("cubic", "bbr", "reno")) + ratio("cubic") + ratio("reno") + "</tr>")
    return ("<table class='tight'><tr><th>path</th><th class='num'>RTT (ms)</th><th class='num'>CUBIC</th><th class='num'>BBR</th><th class='num'>Reno (Mbit/s)</th>"
            "<th class='num'>BBR ÷ CUBIC</th><th class='num'>BBR ÷ Reno</th></tr>" + "".join(rows) + "</table>")


def table_k_cost():
    d, cells = k_analysis(); rows = []
    for c in cells:
        rows.append(f"<tr><td>{E(k_cond_label(c['cond']))}</td><td class='num'>{c['rtt']}</td>"
                    + "".join(f"<td class='num'>{c['retr'][cc]['mean']:.0f}</td>" for cc in ("cubic", "bbr", "reno"))
                    + "".join(f"<td class='num'>{c['queue'][cc]['mean']:.1f}</td>" for cc in ("cubic", "bbr", "reno"))
                    + "".join(f"<td class='num'>{c['memp'][cc]}</td>" for cc in ("cubic", "bbr", "reno")) + "</tr>")
    return ("<table class='tight'><tr><th rowspan=2>path</th><th rowspan=2 class='num'>RTT</th><th colspan=3>retransmits per run (mean)</th><th colspan=3>queueing delay added, ms (mean)</th><th colspan=3>TCP memory pressure events (sum)</th></tr>"
            "<tr>" + "<th class='num'>CUBIC</th><th class='num'>BBR</th><th class='num'>Reno</th>" * 3 + "</tr>" + "".join(rows) + "</table>")


def k_val(spec):
    """{{k:cond@rtt.algo.mean}} and friends, for the prose."""
    d, cells = k_analysis()
    cid, rest = spec.split(".", 1); cond, rtt = cid.split("@")
    c = next(c for c in cells if c["cond"] == cond and c["rtt"] == int(rtt))
    parts = rest.split(".")
    if parts[0] == "ratio": return f"{c['stats']['bbr']['mean'] / c['stats'][parts[1]]['mean']:.1f}"
    v = c["stats"][parts[0]][parts[1]]
    return f"{v:.0f}"


def k_meta():
    d, cells = k_analysis(); des = d["design"]
    runs = sum(len(c["cell"]["runs"]) for c in cells); errs = sum(c["errors"] for c in cells)
    fb = d["fingerprint_before"]; same = len({v["hash"] for v in fb.values()}) == 1
    return {"runs": runs, "errors": errs, "cells": len(cells), "hours": d.get("elapsed_s", 0) / 3600, "minutes": round(d.get("elapsed_s", 0) / 60),
            "bitrate": des.get("iperf_bitrate") or "unlimited ", "same": same, "unchanged": d.get("unchanged"),
            "hash": next(iter(fb.values()))["hash"], "trials": des["trials"], "duration": des["duration_s"], "seed": des["seed"],
            "sig": sum(1 for c in cells for o in ("cubic", "reno") if c["tests"][o]["p_holm"] < 0.05), "tests": 2 * len(cells)}


CHARTS.update({f"k_{k}": (lambda k=k: chart_k(k)) for k in ("clean", "shallow", "loss001", "loss01")})


def flows_table(r, with_total=True):
    rows = []
    for f in r["flows"]:
        rows.append(f"<tr><td><span class='sw' style='background:{COL[f['cc']]}'></span>{NAME[f['cc']]}</td><td class='mono'>{E(f['host'])} → {E(f['host'].replace('a-', 'b-'))}</td>"
                    f"<td class='num'>{f['mbps']:.1f}</td><td class='num'>{f['retransmits']}</td><td class='num'>{f1(f['rtt_ms']['min'])}</td>"
                    f"<td class='num'>{f1(f['rtt_ms']['mean'])}</td><td class='num'>{f1(f['rtt_ms']['max'])}</td><td class='num'>{f.get('mss') or '–'}</td></tr>")
    foot = ""
    if with_total and r.get("total_mbps") is not None:
        foot = (f"<tr class='tot'><td colspan=2>total · Jain fairness {r['jain_fairness']}</td><td class='num'>{r['total_mbps']:.1f}</td>"
                f"<td class='num'>{sum(f['retransmits'] for f in r['flows'])}</td><td colspan=4>bottleneck drops {r['bottleneck']['drops']}</td></tr>")
    return ("<table><tr><th>algorithm</th><th>flow</th><th class='num'>Mbit/s</th><th class='num'>retransmits</th><th class='num'>RTT min</th>"
            "<th class='num'>RTT mean</th><th class='num'>RTT max (ms)</th><th class='num'>MSS</th></tr>" + "".join(rows) + foot + "</table>")


def solo_table(name, extra=None):
    rs = j(name); rows = []
    for f in rs:
        rows.append(f"<tr><td><span class='sw' style='background:{COL[f['cc']]}'></span>{NAME[f['cc']]}</td><td class='num'>{f['mbps']:.1f}</td>"
                    f"<td class='num'>{f['retransmits']}</td><td class='num'>{f['drops']}</td><td class='num'>{f1(f['rtt_ms']['min'])}</td><td class='num'>{f1(f['rtt_ms']['mean'])}</td>"
                    f"<td class='num'>{f1(f['rtt_ms']['max'])}</td><td class='num'>{f.get('mss') or '–'}</td><td class='num'>{f['bytes_per_packet']}</td></tr>")
    return ("<table><tr><th>algorithm (alone)</th><th class='num'>Mbit/s</th><th class='num'>retransmits</th><th class='num'>bottleneck drops</th>"
            "<th class='num'>RTT min</th><th class='num'>RTT mean</th><th class='num'>RTT max (ms)</th><th class='num'>MSS</th><th class='num'>bytes / packet</th></tr>" + "".join(rows) + "</table>")


def table_sweep():
    rows = []
    for r in j("sweep_rtt"):
        v = {f["cc"]: f for f in r["flows"]}
        rows.append(f"<tr><td class='num'>{r['wan']['rtt_ms']:g}</td><td class='num'>{r['wan']['queue_bytes'] // 1000} KB</td>"
                    + "".join(f"<td class='num'>{v[c]['mbps']:.1f}</td>" for c in ("cubic", "bbr", "reno"))
                    + f"<td class='num'>{r['total_mbps']:.1f}</td><td class='num'>{r['jain_fairness']}</td><td class='num'>{r['bottleneck']['drops']}</td></tr>")
    return ("<table><tr><th class='num'>RTT (ms)</th><th class='num'>buffer (1 × BDP)</th><th class='num'>CUBIC</th><th class='num'>BBR</th><th class='num'>Reno (Mbit/s)</th>"
            "<th class='num'>total</th><th class='num'>Jain</th><th class='num'>drops</th></tr>" + "".join(rows) + "</table>")


def table_rttchange():
    d = j("rttchange"); rows = []
    for cc in ("cubic", "bbr"):
        sm = d[cc]["samples"]; at = d[cc]["at"]
        pre = [s["mbps"] for s in sm[2:at]]; post = [s["mbps"] for s in sm[at:at + 5]]; tail = [s["mbps"] for s in sm[-8:]]
        avg = lambda xs: sum(xs) / len(xs) if xs else 0   # noqa: E731
        sm_s = d[cc]["summary"]
        rows.append(f"<tr><td><span class='sw' style='background:{COL[cc]}'></span>{NAME[cc]}</td><td class='num'>{avg(pre):.1f}</td><td class='num'>{avg(post):.1f}</td>"
                    f"<td class='num'>{avg(tail):.1f}</td><td class='num'>{sm_s['mbps']:.1f}</td><td class='num'>{sm_s['retransmits']}</td><td>{'none' if not sm_s['error'] else E(sm_s['error'])}</td></tr>")
    return ("<table><tr><th>algorithm</th><th class='num'>before (s 3–10)</th><th class='num'>5 s after</th><th class='num'>last 8 s</th><th class='num'>whole run</th>"
            "<th class='num'>retransmits</th><th>error</th></tr>" + "".join(rows) + "</table>")


def table_mtu():
    a, b = {f["cc"]: f for f in j("solo_1500")}, {f["cc"]: f for f in j("solo_9000")}
    rows = "".join(f"<tr><td><span class='sw' style='background:{COL[c]}'></span>{NAME[c]}</td><td class='num'>{a[c]['mss']}</td><td class='num'>{a[c]['bytes_per_packet']}</td><td class='num'>{a[c]['mbps']:.1f}</td>"
                   f"<td class='num'>{b[c]['mss']}</td><td class='num'>{b[c]['bytes_per_packet']}</td><td class='num'>{b[c]['mbps']:.1f}</td></tr>" for c in ("cubic", "bbr", "reno"))
    return ("<table><tr><th rowspan=2>algorithm (alone, 15 s)</th><th colspan=3>hosts at 1500</th><th colspan=3>hosts at 9000</th></tr>"
            "<tr><th class='num'>MSS</th><th class='num'>bytes / packet</th><th class='num'>Mbit/s</th><th class='num'>MSS</th><th class='num'>bytes / packet</th><th class='num'>Mbit/s</th></tr>" + rows + "</table>")


def table_s500():
    d = s500(); rows = []
    for p in d["points"]:
        v = {f["cc"]: f for f in p["flows"]}
        rows.append(f"<tr><td class='num'>{p['rtt_ms']:g}</td><td class='num'>{p['ping']['avg_ms']:.1f}</td><td class='num'>{p['wan']['queue_bytes'] / 1e6:.2f}</td>"
                    + "".join(f"<td class='num'>{v[c]['mbps']:.1f}</td><td class='num'>{v[c]['retransmits']}</td><td class='num'>{f1(v[c]['rtt_ms']['mean'])}</td>" for c in d["algos"]) + "</tr>")
    head = "".join(f"<th colspan=3><span class='sw' style='background:{COL[c]}'></span>{NAME[c]}</th>" for c in d["algos"])
    sub = "<th class='num'>Mbit/s</th><th class='num'>retr</th><th class='num'>RTT mean</th>" * len(d["algos"])
    return ("<table><tr><th rowspan=2 class='num'>RTT set (ms)</th><th rowspan=2 class='num'>ping avg (ms)</th><th rowspan=2 class='num'>buffer (MB)</th>" + head + "</tr>"
            "<tr>" + sub + "</tr>" + "".join(rows) + "</table>")


HOST_TCP_KEYS = [   # (sysctl, what it does) — the settings that shape a bulk flow, in the order a reader needs them
    ("net.ipv4.tcp_congestion_control", "the algorithm: one per host pair, the only setting that differs between the six"),
    ("net.ipv4.tcp_available_congestion_control", "modules loaded (tcp_bbr by the lab; Reno is built in)"),
    ("net.core.default_qdisc", "qdisc for new interfaces; eth1 also gets fq explicitly — fq does the pacing BBR relies on"),
    ("net.ipv4.tcp_rmem", "receive buffer min / default / max, bytes; autotuning grows a socket up to max"),
    ("net.ipv4.tcp_wmem", "send buffer min / default / max, bytes"),
    ("net.core.rmem_max", "cap for SO_RCVBUF set by an application"),
    ("net.core.wmem_max", "cap for SO_SNDBUF set by an application"),
    ("net.ipv4.tcp_mem", "all TCP sockets together, in 4 KiB pages: low / pressure / high; sized by the kernel from RAM"),
    ("net.ipv4.tcp_moderate_rcvbuf", "receive-buffer autotuning on"),
    ("net.ipv4.tcp_adv_win_scale", "share of the receive buffer advertised as window (1 = half)"),
    ("net.ipv4.tcp_window_scaling", "RFC 7323 window scaling: windows above 64 KB"),
    ("net.ipv4.tcp_timestamps", "RFC 7323 timestamps: RTT samples on every ACK, PAWS"),
    ("net.ipv4.tcp_sack", "selective ACKs"),
    ("net.ipv4.tcp_dsack", "duplicate SACKs (detects spurious retransmits)"),
    ("net.ipv4.tcp_recovery", "1 = RACK-TLP loss detection (time-based, not 3 dup-ACKs)"),
    ("net.ipv4.tcp_early_retrans", "3 = tail loss probe on"),
    ("net.ipv4.tcp_frto", "F-RTO: detects spurious retransmission timeouts"),
    ("net.ipv4.tcp_reordering", "initial reordering tolerance, segments"),
    ("net.ipv4.tcp_ecn", "2 = accept ECN when the peer asks, do not ask (the WAN does not mark anyway)"),
    ("net.ipv4.tcp_no_metrics_save", "1 = forget cwnd / ssthresh / RTT of a finished connection, so each run starts clean"),
    ("net.ipv4.tcp_slow_start_after_idle", "1 = shrink cwnd after an idle period (irrelevant to bulk iperf3 flows)"),
    ("net.ipv4.tcp_pacing_ss_ratio", "pacing rate in slow start, % of cwnd/RTT"),
    ("net.ipv4.tcp_pacing_ca_ratio", "pacing rate in congestion avoidance, % of cwnd/RTT"),
    ("net.ipv4.tcp_limit_output_bytes", "TSQ: bytes a socket may have queued below TCP (qdisc + NIC)"),
    ("net.ipv4.tcp_notsent_lowat", "unsent bytes allowed in the send buffer (unlimited)"),
    ("net.ipv4.tcp_rto_min_us", "minimum retransmission timeout, µs"),
    ("net.ipv4.tcp_min_rtt_wlen", "window for the min-RTT filter, s"),
    ("net.ipv4.tcp_mtu_probing", "0 = no packetisation-layer PMTU discovery (the path MTU is known)"),
    ("net.ipv4.tcp_fastopen", "1 = TCP Fast Open as a client"),
    ("net.core.netdev_max_backlog", "receive backlog per CPU, packets"),
]


def host_tcp_lab_keys():
    keys = set()
    for l in (LAB / "nodes" / "a-cubic" / "user-data").read_text().splitlines():
        l = l.strip()
        if " = " in l and l.startswith("net."): keys.add(l.split(" = ")[0])
    return keys


def table_host_tcp():
    d = j("host_tcp"); hs = list(d); lab = host_tcp_lab_keys(); rows = []
    for k, what in HOST_TCP_KEYS:
        vals = {h: d[h]["sysctl"].get(k, "–") for h in hs}
        if len(set(vals.values())) == 1: v = E(next(iter(vals.values())).replace("\t", " "))
        else: v = " · ".join(sorted(set(vals.values()), key=["cubic", "bbr", "reno"].index if set(vals.values()) <= {"cubic", "bbr", "reno"} else str))
        if k == "net.ipv4.tcp_mem":
            v += "<br><span class='small'>= " + " / ".join(f"{int(x) * 4096 / 1e6:.1f}" for x in vals[hs[0]].split()) + " MB</span>"
        rows.append(f"<tr><td class='mono'>{E(k.replace('net.ipv4.', '').replace('net.core.', 'core.'))}</td><td class='mono'>{v}</td>"
                    f"<td>{'lab' if k in lab else 'kernel'}</td><td class='small'>{E(what)}</td></tr>")
    h0 = d[hs[0]]
    rows.append(f"<tr><td class='mono'>eth1 MTU</td><td class='mono'>{' · '.join(sorted({str(d[h]['mtu']) for h in hs}))}</td><td>lab</td><td class='small'>1500 by default; 9000 with lab.sh mtu set 9000 (section 15)</td></tr>")
    off = h0["offloads"]
    rows.append(f"<tr><td class='mono'>eth1 offloads</td><td class='mono'>TSO {off.get('tcp-segmentation-offload', '?').split()[0]}, GSO {off.get('generic-segmentation-offload', '?').split()[0]}, GRO {off.get('generic-receive-offload', '?').split()[0]}</td>"
                "<td>kernel</td><td class='small'>virtio-net segmentation and receive offloads: TCP hands the NIC up to 64 KB at a time; wanem has them off so tbf sees real packets</td></tr>")
    return ("<table><tr><th>setting</th><th>value</th><th>set by</th><th>what it does</th></tr>" + "".join(rows) + "</table>"
            f"<p class='cap'>Read from all six hosts ({E(', '.join(hs))}) on {time.strftime('%B %-d, %Y')}, kernel {E(h0['kernel'])}. The values are identical on all six except the algorithm. "
            "“lab”: set by /etc/sysctl.d/90-tcpcc.conf or /etc/local.d/tcpcc.start from cloud-init; “kernel”: Alpine’s default.</p>")


def host_tcp_conf():
    """The two files cloud-init writes for the TCP setup, cut out of a-cubic's user-data and dedented."""
    import textwrap
    ud = (LAB / "nodes" / "a-cubic" / "user-data").read_text().split("\n  - path: ")
    out = []
    for path in ("/etc/sysctl.d/90-tcpcc.conf", "/etc/local.d/tcpcc.start"):
        blk = next(b for b in ud if b.startswith(path))
        body = blk.split("content: |\n", 1)[1].split("\nruncmd:")[0]
        out.append(f"# {path}\n" + textwrap.dedent(body).rstrip())
    return "\n\n".join(out)


def tcp_mem_note():
    p = DATA / "tcp_mem_check.json"
    if not p.exists(): return ""
    c = json.loads(p.read_text())
    if not c["sender_pressures"] and not c["receiver_pressures"]:
        return (f"<div class='box'><b>tcp_mem.</b> The kernel sizes it from RAM ({c['ram_mb']} MB on these hosts): pressure at {c['pressure_mb']:.1f} MB, "
                f"hard limit {c['high_mb']:.1f} MB for all TCP sockets together. A CUBIC flow alone at {c['rtt_ms']} ms and {c['rate_mbit']} Mbit/s "
                f"(BDP {c['bdp_mb']:.1f} MB) did not reach it: no TCPMemoryPressures on either end in {c['duration']} s.</div>")
    return (f"<div class='box warn'><b>tcp_mem is small on these hosts.</b> The kernel sizes it from RAM, and the hosts have {c['ram_mb']} MB: "
            f"TCP as a whole enters memory pressure at {c['pressure_mb']:.1f} MB and stops growing buffers at {c['high_mb']:.1f} MB. That is plenty at 100 Mbit/s "
            f"(BDP 3.75 MB at 300 ms), but not at 500 Mbit/s on long paths: one CUBIC flow alone at {c['rtt_ms']} ms (BDP {c['bdp_mb']:.1f} MB) "
            f"put the sender into pressure {c['sender_pressures']} times and the receiver {c['receiver_pressures']} times in {c['duration']} s (<span class='mono'>TcpExt TCPMemoryPressures</span>) "
            f"and got {c['mbps']:.0f} Mbit/s. Under pressure the kernel stops growing socket buffers, so the 100 and 150 ms points of section 16 may be partly limited by the hosts, not only by the algorithm. "
            "Raising the hosts' RAM or <span class='mono'>net.ipv4.tcp_mem</span> would remove the limit.</div>")


def text_host_tcp_all():
    d = j("host_tcp"); h = next(iter(d))
    return "\n".join(f"{k} = {v.replace(chr(9), ' ')}" for k, v in sorted(d[h]["sysctl"].items()))


TABLES = {
    "compete_default": lambda: flows_table(j("compete_default")),
    "compete_shallow": lambda: flows_table(j("compete_shallow")),
    "compete_stagger": lambda: flows_table(j("compete_stagger"), with_total=False),
    "solo_default": lambda: solo_table("solo_default"),
    "solo_bloat": lambda: solo_table("solo_bloat"),
    "solo_loss": lambda: solo_table("solo_loss"),
    "sweep": table_sweep, "rttchange": table_rttchange, "mtu": table_mtu, "s500": table_s500, "host_tcp": table_host_tcp, "s500_mtu": table_s500_mtu, "k": table_k, "k_cost": table_k_cost,
}


def results():
    out = LAB / "tests" / "results_latest" / "output.xml"
    root = ET.parse(out).getroot()
    rows, passed, failed = [], 0, 0
    for su in root.iter("suite"):
        if not su.get("source", "").endswith(".robot"): continue
        tests = list(su.iter("test"))
        p = sum(t.find("status").get("status") == "PASS" for t in tests)
        names = ", ".join(E(t.get("name")) for t in tests)
        secs = float(su.find("status").get("elapsed", 0))
        rows.append(f"<tr><td>{E(su.get('name'))}</td><td class='num'>{p}/{len(tests)}</td><td class='num'>{secs:.0f} s</td><td class='small'>{names}</td></tr>")
        passed += p; failed += len(tests) - p
    when = out.resolve().parent.name
    return passed, failed, (f"<table><tr><th>suite</th><th class='num'>passed</th><th class='num'>time</th><th>tests</th></tr>{''.join(rows)}</table>"
                            f"<p class='cap'>Run {E(when)}: {passed} passed, {failed} failed.</p>")


def code(path, spec):
    lines = (LAB / path).read_text().splitlines()
    a, b = (int(x) for x in spec.split("-"))
    return "\n".join(lines[a - 1:b])


def val(spec):
    name, path = spec.split(".", 1)
    v = j(name)
    for p in path.split("."):
        v = v[int(p)] if isinstance(v, list) else v[p]
    return f"{v:g}" if isinstance(v, float) else str(v)


def img(name):
    p = IMG / f"{name}.png"
    return f"<img class='shot' src='data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}'>"


def build():
    s = SRC.read_text()
    passed, failed, res = results()
    s = s.replace("{{section_k}}", (DOCS / "walkthrough.k.html").read_text())
    kr = (DOCS / "walkthrough.k.results.html").read_text() if (DOCS / "walkthrough.k.results.html").exists() else "<!-- -->\n<!-- -->"
    kres, kcost = (kr.split("<!-- cost -->") + [""])[:2]
    s = s.replace("{{k_results}}", kres).replace("{{k_cost_text}}", kcost)
    s = s.replace("{{style}}", STYLE.read_text()).replace("{{results}}", res)
    s = s.replace("{{version}}", (LAB / "VERSION").read_text().strip()).replace("{{date}}", time.strftime("%B %-d, %Y"))
    s = s.replace("{{tests}}", str(passed + failed)).replace("{{passed}}", str(passed))
    s = re.sub(r"\{\{chart:(\w+)\}\}", lambda m: CHARTS[m.group(1)](), s)
    s = re.sub(r"\{\{table:(\w+)\}\}", lambda m: TABLES[m.group(1)](), s)
    s = re.sub(r"\{\{img:([\w-]+)\}\}", lambda m: img(m.group(1)), s)
    s = s.replace("{{host_tcp_all}}", E(text_host_tcp_all())).replace("{{host_tcp_conf}}", E(host_tcp_conf())).replace("{{tcp_mem_note}}", tcp_mem_note())
    if "{{k_" in s or "{{k:" in s:
        km = k_meta()
        s = re.sub(r"\{\{kmeta:(\w+)\}\}", lambda m: (f"{km[m.group(1)]:.1f}" if isinstance(km[m.group(1)], float) else str(km[m.group(1)])), s)
        s = s.replace("{{k_legend}}", k_legend())
        _, kc = k_analysis(); c10 = next(c for c in kc if c["cond"] == "clean" and c["rtt"] == 10)
        s = s.replace("{{k_retr_clean10}}", f"{c10['retr']['bbr']['mean']:,.0f}").replace("{{k_retr_clean10_cubic}}", f"{c10['retr']['cubic']['mean']:,.0f}")
        s = re.sub(r"\{\{k:([\w@.]+)\}\}", lambda m: k_val(m.group(1)), s)
    s = s.replace("{{json_qdisc}}", E(next(iter(j("host_tcp").values()))["qdisc"]))
    s = re.sub(r"\{\{text:(\w+)\}\}", lambda m: E((DATA / f"{m.group(1)}.txt").read_text().rstrip()), s)
    s = re.sub(r"\{\{json:(\w+)\}\}", lambda m: E(json.dumps(j(m.group(1)), indent=1)), s)
    s = re.sub(r"\{\{val:([\w.]+)\}\}", lambda m: E(val(m.group(1))), s)
    s = re.sub(r"\{\{file:([\w./-]+)\}\}", lambda m: E((LAB / m.group(1)).read_text().strip()), s)
    s = re.sub(r"\{\{code:([\w./-]+):([\d-]+)\}\}", lambda m: E(code(m.group(1), m.group(2))), s)
    left = re.findall(r"\{\{[^}]+\}\}", s)
    if left: raise SystemExit(f"unfilled placeholders: {left}")
    OUT_HTML.write_text(s); print(f"wrote {OUT_HTML.relative_to(LAB)}")
    if "--html-only" in sys.argv: return
    prof = tempfile.mkdtemp(prefix="tcpcc-wt-pdf-")
    subprocess.run(["google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox", f"--user-data-dir={prof}", "--no-pdf-header-footer",
                    "--virtual-time-budget=8000", f"--print-to-pdf={OUT_PDF}", f"file://{OUT_HTML}"], capture_output=True, timeout=300)
    print(f"wrote {OUT_PDF.relative_to(LAB)}: {OUT_PDF.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    if "--refresh" in sys.argv: collect()
    elif "--shots" in sys.argv: shoot()
    elif "--sweep500" in sys.argv: collect_host_tcp(); collect_sweep500(); collect_tcp_mem_check()
    build()

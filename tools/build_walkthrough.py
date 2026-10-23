#!/usr/bin/env python3
"""Build docs/tcp-cc-lab-walkthrough.pdf from docs/walkthrough.src.html.

    build_walkthrough.py --refresh     collect everything from the running lab into docs/data/ and docs/img/ (~12 min:
                                       show commands, the experiments, a portal run and its screenshots), then build
    build_walkthrough.py               build from docs/data/ as collected last time
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
    # the portal: metrics, SD, a run with RTT changes, screenshots
    ck = portal_cookie()
    put("metrics", "\n".join(l for l in portal("/metrics", cookie=ck, raw=True).splitlines()
                             if re.match(r"tcpcc_(wan_|bottleneck_|host_cc_ok|host_mtu|link_mtu\{node=\"r1|last_compete|lab_run)", l))[:4000])
    put("sd", portal("/api/sd", cookie=ck))
    rec = portal_run(ck); put("portal_rec", rec)
    C.wan_reset()
    shoot()


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


def svg_lines(series, ylabel, xlabel="seconds", marks=(), ymax=None, h=230, w=680, log=False, legend=True, hline=None):
    """series: [(key, label, [(x, y)])] — 2px lines in the algorithm's colour, a legend plus end labels, recessive grid;
    marks: [(x, text)] vertical dashed rules (an RTT change); hline: (y, text) a reference level."""
    left, right, top, bot = 48, 70, 24 if legend else 10, 34
    xs = [x for _, _, pts in series for x, _ in pts] or [0, 1]
    ys = [y for _, _, pts in series for _, y in pts] or [0, 1]
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
    for t in range(0, int(x1) + 1, step):
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
    for key, label, pts in series:
        if not pts: continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(pts))
        s.append(f"<path d='{d}' fill='none' stroke='{COL.get(key, '#52514e')}' stroke-width='2' stroke-linejoin='round'/>")
        ends.append([Y(pts[-1][1]), label, pts[-1][1]])
    ends.sort()
    for i in range(1, len(ends)):   # keep end labels 12px apart
        if ends[i][0] - ends[i - 1][0] < 12: ends[i][0] = ends[i - 1][0] + 12
    for y, label, v in ends:
        s.append(f"<text x='{w - right + 6}' y='{y + 3.5:.1f}' fill='#0b0b0b'>{E(label)}</text>")
    if legend:
        lx = left
        for key, label, _ in series:
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


TABLES = {
    "compete_default": lambda: flows_table(j("compete_default")),
    "compete_shallow": lambda: flows_table(j("compete_shallow")),
    "compete_stagger": lambda: flows_table(j("compete_stagger"), with_total=False),
    "solo_default": lambda: solo_table("solo_default"),
    "solo_bloat": lambda: solo_table("solo_bloat"),
    "solo_loss": lambda: solo_table("solo_loss"),
    "sweep": table_sweep, "rttchange": table_rttchange, "mtu": table_mtu,
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
    s = s.replace("{{style}}", STYLE.read_text()).replace("{{results}}", res)
    s = s.replace("{{version}}", (LAB / "VERSION").read_text().strip()).replace("{{date}}", time.strftime("%-d %B %Y"))
    s = s.replace("{{tests}}", str(passed + failed)).replace("{{passed}}", str(passed))
    s = re.sub(r"\{\{chart:(\w+)\}\}", lambda m: CHARTS[m.group(1)](), s)
    s = re.sub(r"\{\{table:(\w+)\}\}", lambda m: TABLES[m.group(1)](), s)
    s = re.sub(r"\{\{img:([\w-]+)\}\}", lambda m: img(m.group(1)), s)
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
    build()

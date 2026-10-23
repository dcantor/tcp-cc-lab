"""PDF report of one finished flow run (compete / solo / live): the setup, what changed during the run, per-second charts,
the results, and the nodes' CPU and memory while it ran. Built as one HTML file (inline SVG) and printed to PDF by
headless Chrome from a file:// URL (Chrome on this host hangs on http:// pages)."""
import json
import shutil
import subprocess
import tempfile
import time
from html import escape as E
from pathlib import Path

LABEL = {"cubic": "CUBIC", "bbr": "BBR", "reno": "Reno"}
COL = {"cubic": "#2a78d6", "bbr": "#eb6834", "reno": "#1baf7a", "q": "#b45309", "wanem": "#b45309", "r1": "#6b6a65", "r2": "#6b6a65"}
for _a in ("cubic", "bbr", "reno"): COL["a-" + _a] = COL["b-" + _a] = COL[_a]
DASHED = {"b-cubic", "b-bbr", "b-reno", "r2"}
NODE_ORDER = ["a-cubic", "a-bbr", "a-reno", "b-cubic", "b-bbr", "b-reno", "wanem", "r1", "r2"]
MODE_TEXT = {"compete": "Compete: the algorithms at once through the shared bottleneck",
             "solo": "Solo: each algorithm alone, one after the other", "live": "Live: the algorithms at once, until stopped"}


def nice_step(v, n=5):
    """A 1/2/2.5/5 x 10^k step that splits 0..v into about n intervals."""
    raw = max(v, 1e-9) / n; e = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 10 ** -len(str(int(1 / raw)))
    return next(m * e for m in (1, 2, 2.5, 5, 10) if m * e >= raw)


def nice_max(v):
    if v <= 0: return 1
    e = 10 ** len(str(int(v))) / 10 if v >= 1 else 1
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= v: return m * e
    return 10 * e


def fmt(v):
    return "–" if v is None else f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}".rstrip("0").rstrip(".") if abs(v) >= 10 else f"{v:.2f}".rstrip("0").rstrip(".")


def chart(series, unit, xmax, marks=(), hline=None, ymax=None, w=720, h=210, legend=True):
    """series: [(key, label, [(x, y)])]; x in seconds from the start of the run."""
    L, R, T, B = 46, 74, 16, 28
    ys = [y for _, _, p in series for _, y in p]
    top = max(ys + ([hline[0] * 1.05] if hline else []) + [1]) * 1.05
    st = nice_step(ymax or top); y1 = ymax or st * -(-top // st)
    X = lambda x: L + x / max(xmax, 1e-9) * (w - L - R)   # noqa: E731
    Y = lambda y: T + (1 - min(max(y, 0), y1) / y1) * (h - T - B)   # noqa: E731
    s = [f"<svg viewBox='0 0 {w} {h}' xmlns='http://www.w3.org/2000/svg' style='width:100%;font-size:10.5px'>"]
    for i in range(int(round(y1 / st)) + 1):
        v = st * i
        s.append(f"<line x1='{L}' x2='{w - R}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='{'#cfcfca' if i == 0 else '#ecebe7'}'/>"
                 f"<text x='{L - 5}' y='{Y(v) + 3.5:.1f}' text-anchor='end' fill='#52514e'>{fmt(v)}</text>")
    step = next(st for st in (5, 10, 15, 20, 30, 60, 120, 300, 600) if xmax / st <= 14)
    t = 0
    while t <= xmax + 1e-6:
        s.append(f"<line x1='{X(t):.1f}' x2='{X(t):.1f}' y1='{T}' y2='{h - B}' stroke='#f2f1ee'/><text x='{X(t):.1f}' y='{h - B + 13}' text-anchor='middle' fill='#52514e'>{t:g}s</text>")
        t += step
    if hline:
        s.append(f"<line x1='{L}' x2='{w - R}' y1='{Y(hline[0]):.1f}' y2='{Y(hline[0]):.1f}' stroke='#8a8984' stroke-dasharray='3 3'/>"
                 f"<text x='{w - R - 4}' y='{Y(hline[0]) - 4:.1f}' text-anchor='end' fill='#52514e' stroke='#fff' stroke-width='3' paint-order='stroke'>{E(hline[1])}</text>")
    for i, (x, text) in enumerate(marks):
        s.append(f"<line x1='{X(x):.1f}' x2='{X(x):.1f}' y1='{T}' y2='{h - B}' stroke='#8a8984' stroke-dasharray='4 3'/>"
                 f"<text x='{X(x) + 3:.1f}' y='{T + 10 + (i % 3) * 11}' fill='#52514e' stroke='#fff' stroke-width='3' paint-order='stroke'>{E(text)}</text>")
    ends = []
    for key, label, pts in series:
        if not pts: continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(pts))
        s.append(f"<path d='{d}' fill='none' stroke='{COL.get(key, '#52514e')}' stroke-width='1.8' stroke-linejoin='round'{' stroke-dasharray=\"6 4\"' if key in DASHED else ''}/>")
        ends.append([Y(pts[-1][1]), label])
    if legend and len(ends) <= 4:
        ends.sort()
        for i in range(1, len(ends)):
            if ends[i][0] - ends[i - 1][0] < 12: ends[i][0] = ends[i - 1][0] + 12
        s.extend(f"<text x='{w - R + 5}' y='{y + 3.5:.1f}' fill='#1f2937' font-weight='600'>{E(lb)}</text>" for y, lb in ends)
    s.append(f"<text x='{L - 5}' y='{T - 6}' text-anchor='end' fill='#52514e'>{E(unit)}</text></svg>")
    return "".join(s)


def legend(keys):
    return "<div class='legend'>" + "".join(
        f"<span><svg width='22' height='10'><line x1='1' x2='21' y1='5' y2='5' stroke='{COL.get(k, '#52514e')}' stroke-width='2.5'{' stroke-dasharray=\"5 3\"' if k in DASHED else ''}/></svg>{E(LABEL.get(k, k))}</span>"
        for k in keys) + "</div>"


def brtxt(v): return f"{v:g}M" if v else "unlimited"


def errtxt(e): return f" <span class='bad'>{E(e)}</span>" if e else ""


def lines(log, tag):
    out = []
    for l in log:
        if l["line"].startswith(tag + " "):
            try: out.append(json.loads(l["line"][len(tag) + 1:]))
            except ValueError: pass
    return out


def mark_labels(marks, start):
    prev, out = dict(start), []
    keys = [("rtt_ms", lambda v: f"RTT {v:g} ms"), ("rate_mbit", lambda v: f"{v:g} Mbit/s link"), ("loss_pct", lambda v: f"loss {v:g} %"),
            ("buffer_bdp", lambda v: f"buffer {v:g} × BDP"), ("bitrate_mbit", lambda v: f"-b {f'{v:g}M' if v else 'unlimited'}")]
    for m in marks:
        ch = [f(m[k]) for k, f in keys if k in m and m[k] != prev.get(k)]
        prev.update({k: v for k, v in m.items() if k != "ts"}); out.append(", ".join(ch) or "WAN set (no change)")
    return out


CSS = """
@page { size: A4; margin: 14mm 13mm 16mm; }
* { box-sizing: border-box; }
body { font: 10.5pt/1.45 "Noto Sans", "DejaVu Sans", system-ui, sans-serif; color: #1f2937; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 2px; } h2 { font-size: 13pt; margin: 18px 0 6px; border-bottom: 1px solid #e5e7eb; padding-bottom: 3px; break-after: avoid; }
h3 { font-size: 10.5pt; margin: 10px 0 2px; break-after: avoid; }
.sub { color: #52514e; } .mono { font-family: "DejaVu Sans Mono", ui-monospace, monospace; font-size: 9pt; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; margin: 4px 0; break-inside: avoid; }
th, td { text-align: left; padding: 3px 6px; border-bottom: 1px solid #e5e7eb; vertical-align: top; } th { color: #52514e; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.kpis { display: flex; gap: 8px; margin: 10px 0; flex-wrap: wrap; } .kpi { border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px 10px; flex: 1; min-width: 120px; }
.kpi b { display: block; font-size: 8.5pt; color: #52514e; font-weight: 500; } .kpi span { font-size: 14pt; font-weight: 600; }
.fig { break-inside: avoid; margin: 6px 0 10px; } .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 9pt; margin: 2px 0; }
.legend svg { vertical-align: 0; margin-right: 4px; } .sw { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
.note { color: #52514e; font-size: 9pt; } .bad { color: #b91c1c; }
header { border-bottom: 2px solid #1f2937; padding-bottom: 8px; margin-bottom: 6px; }
"""


def build_html(run, lab_version, node_stats, host_info=None):
    mode, o, res = run["mode"], run.get("options") or {}, run.get("result") or {}
    log = run.get("log") or []
    samples, wans, marks, start = lines(log, "SAMPLE"), lines(log, "WAN"), lines(log, "MARK"), (lines(log, "START") or [None])[0]
    started, finished = run.get("steps", [{}])[0].get("started") or run["started"], run.get("finished") or time.time()
    t0 = start["ts"] if start else (min(s["ts"] for s in samples) - 1 if samples else started)
    wan0 = start or res.get("wan") or {}
    algos = o.get("algos") or [f["cc"] for f in res.get("flows", [])]
    xmax = max([s["ts"] - t0 for s in samples] + [w["ts"] - t0 for w in wans] + [5]) + 1   # the run's own length (a live run's max_s is only a cap)
    labels = mark_labels(marks, {**wan0, "bitrate_mbit": o.get("bitrate_mbit") or 0})
    mk = [(m["ts"] - t0, lb) for m, lb in zip(marks, labels)]
    per = {a: [s for s in samples if s["cc"] == a] for a in algos}
    ser = lambda k: [(a, LABEL[a], [(s["ts"] - t0, s[k] or 0) for s in per[a] if k != "rtt_ms" or s[k]]) for a in algos]   # noqa: E731
    br = o.get("bitrate_mbit") or 0
    cmd = f"iperf3 -c &lt;peer&gt; -p 5201 -t {'&lt;until stopped&gt;' if mode == 'live' else o.get('duration')} -i 1 --json-stream" + (f" -b {br:g}M" if br else "")
    dirn = "site A → site B" if o.get("direction", "a2b") == "a2b" else "site B → site A"
    hosts = ", ".join(f"{'a' if o.get('direction', 'a2b') == 'a2b' else 'b'}-{a} → {'b' if o.get('direction', 'a2b') == 'a2b' else 'a'}-{a}" for a in algos)

    # results
    if mode == "live":
        rows = res.get("segments", [])
        res_tbl = ("<table><tr><th>Algorithm</th><th class='num'>-b</th><th class='num'>Time</th><th class='num'>Mbit/s</th><th class='num'>Retransmits</th>"
                   "<th class='num'>RTT min</th><th class='num'>mean</th><th class='num'>max (ms)</th></tr>" +
                   "".join(f"<tr><td><span class='sw' style='background:{COL[g['cc']]}'></span>{LABEL[g['cc']]}</td><td class='num'>{brtxt(g.get('bitrate_mbit'))}</td>"
                           f"<td class='num'>{g['from_s']}–{g['to_s']} s</td><td class='num'>{fmt(g['mbps'])}</td><td class='num'>{g['retransmits']}</td>"
                           f"<td class='num'>{fmt(g['rtt_ms']['min'])}</td><td class='num'>{fmt(g['rtt_ms']['mean'])}</td><td class='num'>{fmt(g['rtt_ms']['max'])}</td></tr>" for g in rows) + "</table>"
                   "<p class='note'>One row per flow: a new flow starts at each bit-rate change. Mbit/s and retransmits come from the flow's per-second samples (sender side).</p>")
        tot = {}
        for g in rows: tot.setdefault(g["cc"], []).append((g["to_s"] - g["from_s"], g["mbps"]))
        kp = {a: sum(d * m for d, m in v) / max(1e-9, sum(d for d, _ in v)) for a, v in tot.items()}
    else:
        fl = res.get("flows", []); total = sum(f["mbps"] for f in fl) or 1
        res_tbl = ("<table><tr><th>Algorithm</th><th>Sender</th><th class='num'>Mbit/s</th>" + ("<th class='num'>Share</th>" if mode == "compete" else "") +
                   "<th class='num'>Retransmits</th><th class='num'>RTT min</th><th class='num'>mean</th><th class='num'>max (ms)</th><th>Kernel reported</th></tr>" +
                   "".join(f"<tr><td><span class='sw' style='background:{COL[f['cc']]}'></span>{LABEL[f['cc']]}</td><td class='mono'>{E(f['host'])}</td><td class='num'>{fmt(f['mbps'])}</td>" +
                           (f"<td class='num'>{100 * f['mbps'] / total:.0f} %</td>" if mode == "compete" else "") +
                           f"<td class='num'>{f['retransmits']}</td><td class='num'>{fmt(f['rtt_ms']['min'])}</td><td class='num'>{fmt(f['rtt_ms']['mean'])}</td><td class='num'>{fmt(f['rtt_ms']['max'])}</td>"
                           f"<td>{E(f.get('cc_reported') or '–')}{'' if f.get('cc_reported') in (None, f['cc']) else ' <span class=bad>(mismatch)</span>'}{errtxt(f.get('error'))}</td></tr>" for f in fl) + "</table>"
                   "<p class='note'>Mbit/s: iperf3's receiver-side goodput over the whole flow; retransmits and RTT from the sender (TCP_INFO).</p>")
        if mode == "compete":
            res_tbl += f"<p>Total <b>{fmt(res.get('total_mbps'))}</b> Mbit/s · Jain fairness <b>{res.get('jain_fairness')}</b> (1 = equal shares, {1 / max(1, len(fl)):.2f} = one flow takes all) · bottleneck drops <b>{(res.get('bottleneck') or {}).get('drops', '–')}</b></p>"
        kp = {f["cc"]: f["mbps"] for f in fl}

    kpis = "".join(f"<div class='kpi'><b><span class='sw' style='background:{COL[a]}'></span>{LABEL[a]}</b><span>{fmt(v)}</span> Mbit/s</div>" for a, v in kp.items())
    rate = wan0.get("rate_mbit")
    figs = [("Throughput", "Mbit/s per second, sender side" + (" · dashed: bottleneck rate" if rate else ""), chart(ser("mbps"), "Mbit/s", xmax, mk, (rate, f"bottleneck {rate:g} Mbit/s") if rate else None)),
            ("RTT the sender sees", "smoothed RTT from TCP_INFO, ms", chart(ser("rtt_ms"), "ms", xmax, mk)),
            ("Retransmits", "segments per second", chart(ser("retr"), "retr/s", xmax, mk))]
    if wans:
        figs.append(("Bottleneck queue", "ms of queue at the tbf (wanem), sampled each second", chart([("q", "queue", [(w["ts"] - t0, w["q_ms"]) for w in wans])], "ms", xmax, mk, legend=False)))

    # nodes during the run
    nodes_html = ""
    win = {n: [x for x in v if t0 - 1 <= x[0] <= finished + 1] for n, v in (run.get("node_stats") or node_stats).items()}
    nodes = [n for n in NODE_ORDER if win.get(n)]
    if nodes:
        cpu = [(n, n, [(x[0] - t0, x[1]) for x in win[n]]) for n in nodes]
        mem = [(n, n, [(x[0] - t0, x[2]) for x in win[n]]) for n in nodes]
        ram = max(x[3] for n in nodes for x in win[n])
        trows = "".join(f"<tr><td class='mono'><svg width='22' height='10'><line x1='1' x2='21' y1='5' y2='5' stroke='{COL.get(n)}' stroke-width='2.5'{' stroke-dasharray=\"5 3\"' if n in DASHED else ''}/></svg> {n}</td>"
                        f"<td class='num'>{fmt(sum(x[1] for x in win[n]) / len(win[n]))} %</td><td class='num'>{fmt(max(x[1] for x in win[n]))} %</td>"
                        f"<td class='num'>{max(x[2] for x in win[n]):.0f} MB</td><td class='num'>{win[n][-1][3]:.0f} MB</td></tr>" for n in nodes)
        nodes_html = (f"<div class='fig'><h2>Nodes during the run</h2><p class='note'>Each node's own /proc/stat and /proc/meminfo, once a second. CPU is a % of the node's vCPUs.</p>"
                      f"<table><tr><th>Node</th><th class='num'>CPU mean</th><th class='num'>CPU peak</th><th class='num'>Memory in use, peak</th><th class='num'>RAM</th></tr>{trows}</table></div>"
                      f"<div class='fig'><h3>CPU <span class='sub'>% of vCPUs</span></h3>{legend(nodes)}{chart(cpu, '% CPU', xmax, mk, ymax=100)}</div>"
                      f"<div class='fig'><h3>Memory in use <span class='sub'>MB · dashed: RAM</span></h3>{chart(mem, 'MB', xmax, mk, (ram, f'RAM {ram:.0f} MB'))}</div>")
    else:
        nodes_html = "<h2>Nodes during the run</h2><p class='note'>No CPU / memory samples cover this run (the portal keeps 30 minutes).</p>"

    chg = ("<table><tr><th class='num'>At</th><th>Change</th></tr>" + "".join(f"<tr><td class='num'>{x:.1f} s</td><td>{E(lb)}</td></tr>" for x, lb in mk) + "</table>") if mk else "<p class='note'>No changes: the WAN and the bit rate stayed as set at the start.</p>"
    mtu = res.get("host_mtu")
    setup = [("Run", f"<span class='mono'>{E(run['id'])}</span> · {E(run['status'])}" + (f" · <span class='bad'>{E(run.get('error') or '')}</span>" if run.get("error") else "")),
             ("Mode", E(MODE_TEXT.get(mode, mode))),
             ("When", f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))} – {time.strftime('%H:%M:%S', time.localtime(finished))} ({finished - started:.0f} s)"),
             ("Flows", f"{E(hosts)} ({dirn}); one TCP connection each"),
             ("Traffic", f"<span class='mono'>{cmd}</span>" + ("" if mode != "compete" or not o.get("stagger") else f"; started {o['stagger']:g} s apart")),
             ("Sender bit rate", f"{br:g} Mbit/s per flow (iperf3 -b)" if br else "unlimited (no -b)"),
             ("WAN at the start", f"RTT {wan0.get('rtt_ms', '–'):g} ms · bottleneck {wan0.get('rate_mbit', 0):g} Mbit/s · random loss {wan0.get('loss_pct', 0):g} % each way · buffer {wan0.get('buffer_bdp', 0):g} × BDP" if wan0 else "–"),
             ("Hosts", (f"MTU {mtu} · " if mtu else "") + E(host_info or "Alpine Linux VMs, fq qdisc on eth1"))]
    body = f"""<header><h1>Flow test report</h1><div class='sub'>tcp-cc-lab {E(lab_version)} · TCP congestion control (CUBIC, BBR, Reno) across an emulated WAN · generated {time.strftime('%Y-%m-%d %H:%M')}</div></header>
<div class='kpis'>{kpis}</div>
<h2>Setup</h2><table>{''.join(f'<tr><th style="width:24%">{k}</th><td>{v}</td></tr>' for k, v in setup)}</table>
<h2>Changes during the run</h2>{chg}
<h2>Results</h2>{res_tbl}
{''.join(f"<div class='fig'>{'<h2>Per second</h2>' + legend(algos) if i == 0 else ''}<h3>{t} <span class='sub'>{s}</span></h3>{c}</div>" for i, (t, s, c) in enumerate(figs))}
{nodes_html}"""
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Flow test report {E(run['id'])}</title><style>{CSS}</style></head><body>{body}</body></html>"


def build_pdf(html, out_pdf):
    """Print the HTML to out_pdf with headless Chrome (file:// URL, throwaway profile)."""
    tmp = Path(tempfile.mkdtemp(prefix="cc-report-"))
    try:
        (tmp / "report.html").write_text(html)
        r = subprocess.run(["google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox", f"--user-data-dir={tmp / 'profile'}", "--no-pdf-header-footer",
                            "--virtual-time-budget=4000", f"--print-to-pdf={tmp / 'report.pdf'}", f"file://{tmp / 'report.html'}"], capture_output=True, timeout=120)
        if not (tmp / "report.pdf").exists(): raise RuntimeError(f"chrome did not write the PDF (rc={r.returncode}): {r.stderr.decode()[-300:]}")
        shutil.move(str(tmp / "report.pdf"), out_pdf)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

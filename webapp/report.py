"""PDF report of one finished flow run (compete / solo / live): the setup, what changed during the run, per-second charts,
the results, and the nodes' CPU and memory while it ran. Built as one HTML file (inline SVG) and printed to PDF by
headless Chrome from a file:// URL (Chrome on this host hangs on http:// pages)."""
import json
import re
import shutil
import subprocess
import tempfile
import time
from html import escape as E
from pathlib import Path

import report_analysis
import report_background
import report_references

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


def chart(series, unit, xmax, marks=(), hline=None, ymax=None, w=720, h=210, legend=True, ycap=None, xunit="s", dots=False):
    """series: [(key, label, [(x, y)])]; x in seconds from the start of the run."""
    L, R, T, B = 46, 74, 16, 28
    ys = [y for _, _, p in series for _, y in p]
    top = max(ys + ([hline[0] * 1.05] if hline else []) + [1]) * 1.05
    if ycap and top > ycap: ymax = ycap   # scale to fit, up to ycap (above it: drawn at the top)
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
        s.append(f"<line x1='{X(t):.1f}' x2='{X(t):.1f}' y1='{T}' y2='{h - B}' stroke='#f2f1ee'/><text x='{X(t):.1f}' y='{h - B + 13}' text-anchor='middle' fill='#52514e'>{t:g}{xunit}</text>")
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
        if dots: s.extend(f"<circle cx='{X(x):.1f}' cy='{Y(y):.1f}' r='3.5' fill='{COL.get(key, '#52514e')}' stroke='#fff' stroke-width='1.5'/>" for x, y in pts)
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
    keys = [("algo", lambda v: f"{LABEL.get(v, v)} starts"), ("rtt_ms", lambda v: f"RTT {v:g} ms"), ("rate_mbit", lambda v: f"{v:g} Mbit/s link"), ("loss_pct", lambda v: f"loss {v:g} %"),
            ("buffer_bdp", lambda v: f"buffer {v:g} × BDP"), ("bitrate_mbit", lambda v: f"-b {f'{v:g}M' if v else 'unlimited'}")]
    for m in marks:
        ch = [f(m[k]) for k, f in keys if k in m and m[k] != prev.get(k)]
        prev.update({k: v for k, v in m.items() if k != "ts"}); out.append(", ".join(ch) or "WAN set (no change)")
    return out


CSS = """
@page { size: Letter; margin: 18mm 17mm 20mm;
  @top-left { content: "TCP congestion control · flow test"; font: 7.8pt "Noto Sans", sans-serif; color: #6b7280; }
  @top-right { content: "%(runid)s"; font: 7.8pt "DejaVu Sans Mono", monospace; color: #6b7280; }
  @bottom-left { content: "tcp-cc-lab %(version)s · %(author)s"; font: 7.8pt "Noto Sans", sans-serif; color: #6b7280; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 7.8pt "Noto Sans", sans-serif; color: #6b7280; } }
@page :first { @top-left { content: none; } @top-right { content: none; } @bottom-left { content: none; } @bottom-right { content: none; } }
:root { --ink: #1f2937; --head: #0f172a; --mute: #6b7280; --line: #e5e7eb; --accent: #1d4ed8; --code: #f3f4f6; }
* { box-sizing: border-box; }
body { font: 10.3pt/1.55 "Noto Sans", "DejaVu Sans", system-ui, sans-serif; color: var(--ink); margin: 0; }
h1, h2, h3, .serif { font-family: "Noto Serif", "Liberation Serif", Georgia, serif; color: var(--head); }
h2 { font-size: 16pt; font-weight: 700; margin: 0 0 8pt; padding-bottom: 4pt; border-bottom: 2px solid var(--accent); break-after: avoid; }
h2 .n { color: var(--accent); margin-right: 8pt; }
h3 { font-size: 12pt; margin: 14pt 0 4pt; break-after: avoid; }
h2.sub { font-size: 13.5pt; margin-top: 18pt; break-before: page; border-bottom-width: 1px; }
h4 { font: 600 9.4pt "Noto Sans", sans-serif; margin: 8pt 0 2pt; color: #334155; break-after: avoid; }
p { margin: 5pt 0; text-align: justify; hyphens: auto; } ul, ol { margin: 4pt 0 6pt; padding-left: 18pt; } li { margin: 2.5pt 0; }
.mono, code { font-family: "DejaVu Sans Mono", ui-monospace, monospace; font-size: 8.5pt; } code { background: var(--code); padding: 0 3px; border-radius: 3px; }
.sub { color: var(--mute); font-weight: 400; font-family: "Noto Sans", sans-serif; font-size: 9pt; } .note { color: var(--mute); font-size: 8.8pt; } .bad { color: #b91c1c; }
section.chapter { break-before: page; } .page { break-before: page; }
table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 4pt 0 2pt; break-inside: avoid; }
th, td { text-align: left; padding: 3.5pt 6pt; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: #f8fafc; color: #334155; font-weight: 600; } thead th, tr:first-child th { border-top: 1.5px solid #cbd5e1; border-bottom: 1.5px solid #cbd5e1; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.kv th { width: 24%; background: none; border-top: 0; border-bottom: 1px solid var(--line); }
.cap { font-size: 8.6pt; color: #475569; margin: 3pt 0 12pt; } .cap b { color: var(--head); }
.fig { break-inside: avoid; margin: 8pt 0 4pt; } .tbl { break-inside: avoid; margin: 8pt 0 4pt; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 8.6pt; margin: 2px 0; } .legend svg { vertical-align: 0; margin-right: 4px; }
.sw { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
.kpis { display: flex; gap: 0; margin: 8pt 0; border-top: 1px solid #cbd5e1; border-bottom: 1px solid #cbd5e1; }
.kpi { flex: 1; padding: 6pt 10pt; border-left: 1px solid var(--line); } .kpi:first-child { border-left: 0; padding-left: 0; }
.kpi b { display: block; font-size: 8pt; color: var(--mute); font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
.kpi span { font: 700 17pt "Noto Serif", serif; color: var(--head); }
.box { border: 1px solid var(--line); border-left: 4px solid var(--accent); background: #f8fafc; padding: 8pt 12pt; border-radius: 3px; margin: 8pt 0; break-inside: avoid; }
.box .h { font-weight: 700; font-size: 8.4pt; letter-spacing: .14em; text-transform: uppercase; color: var(--head); margin-bottom: 3pt; }
.box ul { margin: 2pt 0 0; } .box p { margin: 2pt 0; }
.phase { border-left: 3px solid var(--line); padding: 2pt 0 2pt 10pt; margin: 8pt 0; break-inside: avoid; } .phase h4 { margin: 0 0 2pt; font-size: 10pt; } .phase p { margin: 2pt 0; }
/* the cover, as the walkthrough white paper */
.cover { height: 236mm; display: flex; flex-direction: column; justify-content: space-between; break-after: page; }
.cover .top { border-top: 5pt solid var(--head); padding-top: 8pt; display: flex; justify-content: space-between; align-items: baseline; }
.cover .kicker { color: var(--accent); font-weight: 700; letter-spacing: .18em; text-transform: uppercase; font-size: 10pt; }
.cover .series { color: var(--mute); font-size: 9pt; letter-spacing: .04em; }
.cover h1 { font-size: 30pt; line-height: 1.15; font-weight: 700; letter-spacing: -.01em; margin: 0 0 10pt; }
.cover .subtitle { font-family: "Noto Serif", serif; font-size: 15pt; line-height: 1.3; color: var(--accent); margin: -2pt 0 12pt; text-align: left; }
.cover .lede { font-family: "Noto Serif", serif; font-style: italic; font-size: 12.5pt; line-height: 1.45; color: #334155; margin: 0 0 16pt; text-align: left; }
.cover .abstract { border-top: 1px solid #cbd5e1; border-bottom: 1px solid #cbd5e1; padding: 9pt 0; }
.cover .abstract .h { font-weight: 700; font-size: 8.5pt; letter-spacing: .14em; text-transform: uppercase; color: var(--head); margin-bottom: 3pt; }
.cover .abstract p { font-size: 9.6pt; color: #334155; margin: 0; }
.cover .bottom { border-top: 1.5pt solid var(--head); padding-top: 10pt; display: flex; justify-content: space-between; align-items: flex-end; gap: 12mm; }
.cover .author .name { font: 700 15pt "Noto Serif", serif; color: var(--head); } .cover .author .date { font-size: 10.5pt; color: #334155; margin-top: 2pt; }
a { color: var(--accent); text-decoration: none; } .cite { white-space: nowrap; } .cite a { color: var(--accent); }
ol.refs { padding-left: 24pt; margin: 2pt 0 8pt; } ol.refs li { margin: 3pt 0; font-size: 9pt; line-height: 1.45; text-align: left; break-inside: avoid; } ol.refs li a { overflow-wrap: anywhere; }
.cover .meta { color: var(--mute); font-size: 8.3pt; line-height: 1.6; text-align: right; }
.toc { columns: 2; column-gap: 14mm; font-size: 10pt; margin: 4pt 0 14pt; } .toc div { margin: 2pt 0; break-inside: avoid; } .toc .n { color: var(--accent); font-weight: 700; display: inline-block; width: 22pt; }
/* appendix */
h3.node { border-top: 1.5px solid #cbd5e1; padding-top: 6pt; margin-top: 16pt; }
pre.cfg { font: 7pt/1.28 "DejaVu Sans Mono", ui-monospace, monospace; background: #f8fafc; border: 1px solid var(--line); border-radius: 3px; padding: 6px 8px;
  white-space: pre-wrap; word-break: break-all; margin: 2px 0 8px; text-align: left; }
pre.cfg.two { columns: 2; column-gap: 14px; }
"""


def topo_svg(inv, wan, w=680):
    """The lab as a diagram: three hosts per site, r1 - wanem - r2 between them (print colours)."""
    nodes = {n["name"]: n for n in (inv or {}).get("nodes", [])}
    ip = lambda n: (nodes.get(n) or {}).get("ip", "")   # noqa: E731
    s = [f"<svg viewBox='0 0 {w} 250' xmlns='http://www.w3.org/2000/svg' style='width:100%;font-size:10.5px'>"]
    hy = [18, 98, 178]
    def box(x, y, bw, bh, title, sub, stroke="#94a3b8", fill="#fff", bar=None):
        out = f"<rect x='{x}' y='{y}' width='{bw}' height='{bh}' rx='6' fill='{fill}' stroke='{stroke}' stroke-width='1.4'/>"
        if bar: out += f"<rect x='{x}' y='{y}' width='5' height='{bh}' rx='2' fill='{bar}'/>"
        return out + f"<text x='{x + bw / 2}' y='{y + 17}' text-anchor='middle' font-weight='700'>{E(title)}</text><text x='{x + bw / 2}' y='{y + 31}' text-anchor='middle' fill='#52514e' font-size='9.5'>{E(sub)}</text>"
    for i, a in enumerate(("cubic", "bbr", "reno")):
        s.append(f"<path d='M132 {hy[i] + 22} H150 M{w - 132} {hy[i] + 22} H{w - 150}' stroke='#94a3b8' stroke-width='2' fill='none'/>")
    s.append(f"<path d='M150 40 V200 M{w - 150} 40 V200 M150 120 H175 M235 120 H260 M{w - 260} 120 H{w - 235} M{w - 175} 120 H{w - 150}' stroke='#94a3b8' stroke-width='2' fill='none'/>")
    for i, a in enumerate(("cubic", "bbr", "reno")):
        s.append(box(6, hy[i], 126, 44, f"a-{a}", f"{LABEL[a]} · {ip('a-' + a)}", bar=COL[a]))
        s.append(box(w - 132, hy[i], 126, 44, f"b-{a}", f"{LABEL[a]} · {ip('b-' + a)}", bar=COL[a]))
    s.append(box(175, 98, 60, 44, "r1", "VyOS")); s.append(box(w - 235, 98, 60, 44, "r2", "VyOS"))
    wt = f"{wan.get('rtt_ms', 0):g} ms · {wan.get('rate_mbit', 0):g} Mbit/s" if wan else "netem + tbf"
    s.append(box(260, 92, w - 520, 56, "wanem", wt, stroke="#b45309", fill="#fff4e5"))
    s.append(f"<text x='{w / 2}' y='166' text-anchor='middle' fill='#52514e' font-size='9.5'>loss {wan.get('loss_pct', 0):g} % · buffer {wan.get('buffer_bdp', 0):g} × BDP</text>" if wan else "")
    s.append(f"<text x='69' y='240' text-anchor='middle' fill='#52514e'>site A · 192.168.141.0/24</text><text x='{w - 69}' y='240' text-anchor='middle' fill='#52514e'>site B · 192.168.142.0/24</text>"
             f"<text x='{w / 2}' y='240' text-anchor='middle' fill='#52514e'>WAN 100.64.14.0/30 (r1 .1, r2 .2) through the wanem bridge</text></svg>")
    return "".join(s)


HOST_KEYS = [("net.ipv4.tcp_congestion_control", "the algorithm (the only setting that differs between hosts)"), ("net.core.default_qdisc", "qdisc for new interfaces (fq: pacing)"),
             ("net.ipv4.tcp_rmem", "receive buffer min / default / max, bytes"), ("net.ipv4.tcp_wmem", "send buffer min / default / max, bytes"),
             ("net.core.rmem_max", "largest receive buffer an application may set"), ("net.core.wmem_max", "largest send buffer an application may set"),
             ("net.ipv4.tcp_mem", "TCP memory: low / pressure / max, 4 kB pages (sized by the kernel from RAM)"),
             ("net.ipv4.tcp_no_metrics_save", "1: no cached ssthresh / RTT between connections"), ("net.ipv4.tcp_window_scaling", "window scaling"),
             ("net.ipv4.tcp_sack", "selective acknowledgements"), ("net.ipv4.tcp_timestamps", "timestamps"), ("net.ipv4.tcp_ecn", "ECN (2: accept, do not request)")]


def sysctls(text):
    out = {}
    for l in (text or "").splitlines():
        if " = " in l: k, v = l.split(" = ", 1); out[k.strip()] = " ".join(v.split())
    return out


def section_text(configs, node, prefix):
    return next((t for title, t in (configs or {}).get(node, []) if title.startswith(prefix)), "")



def build_html(run, lab_version, node_stats, host_info=None, inv=None, configs=None, author="Daniel Cantor"):
    mode, o, res = run["mode"], run.get("options") or {}, run.get("result") or {}
    log = run.get("log") or []
    samples, wans, marks, start = lines(log, "SAMPLE"), lines(log, "WAN"), lines(log, "MARK"), (lines(log, "START") or [None])[0]
    started, finished = run.get("steps", [{}])[0].get("started") or run["started"], run.get("finished") or time.time()
    t0 = start["ts"] if start else (min(s["ts"] for s in samples) - 1 if samples else started)
    wan0 = start or res.get("wan") or {}
    algos = o.get("algos") or [f["cc"] for f in res.get("flows", [])]
    seq = mode == "live" and bool(o.get("sequential"))   # live, one algorithm at a time
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
    THR_MAX = 1000   # Mbit/s: the throughput chart scales to fit, up to 1 Gbit/s, as on the portal
    above = max([x["mbps"] for x in samples] + [0]) > THR_MAX
    figs = [("Throughput", "Mbit/s per second, sender side, scale up to 1 Gbit/s" + (" · dashed: bottleneck rate" if rate and rate <= THR_MAX else f" · bottleneck {rate:g} Mbit/s, above the scale" if rate else "")
             + (" · seconds above 1 Gbit/s are drawn at the top" if above else ""),
             chart(ser("mbps"), "Mbit/s", xmax, mk, (rate, f"bottleneck {rate:g} Mbit/s") if rate and rate <= THR_MAX else None, ycap=THR_MAX)),
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
             ("Mode", E("Live: each algorithm alone, in turn, until stopped" if seq else MODE_TEXT.get(mode, mode))),
             ("When", f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))} – {time.strftime('%H:%M:%S', time.localtime(finished))} ({finished - started:.0f} s)"),
             ("Flows", f"{E(hosts)} ({dirn}); one TCP connection each"),
             ("Traffic", f"<span class='mono'>{cmd}</span>" + ("" if mode != "compete" or not o.get("stagger") else f"; started {o['stagger']:g} s apart")),
             ("Sender bit rate", f"{br:g} Mbit/s per flow (iperf3 -b)" if br else "unlimited (no -b)"),
             ("WAN at the start", f"RTT {wan0.get('rtt_ms', '–'):g} ms · bottleneck {wan0.get('rate_mbit', 0):g} Mbit/s · random loss {wan0.get('loss_pct', 0):g} % each way · buffer {wan0.get('buffer_bdp', 0):g} × BDP" if wan0 else "–"),
             ("Hosts", (f"MTU {mtu} · " if mtu else "") + E(host_info or "Alpine Linux VMs, fq qdisc on eth1"))]
    # ---- narrative pieces
    ln = lambda a: LABEL.get(a, a)   # noqa: E731
    rank = sorted(kp.items(), key=lambda x: -x[1])
    wtxt = (f"RTT {wan0.get('rtt_ms', 0):g} ms, a {wan0.get('rate_mbit', 0):g} Mbit/s bottleneck, {wan0.get('loss_pct', 0):g} % random loss and a "
            f"{wan0.get('buffer_bdp', 0):g} × BDP buffer") if wan0 else "the WAN as set"
    if o.get("rtt_sweep") and wan0: wtxt = wtxt.replace(f"RTT {wan0.get('rtt_ms', 0):g} ms", f"RTT swept {o['rtts'][0]:g}–{o['rtts'][-1]:g} ms", 1)
    sweep_txt = (f"The RTT was swept through {', '.join(f'{r:g}' for r in o['rtts'])} ms, {o.get('step_s')} s at each" + (" within each algorithm's turn" if seq else "") + ". ") if o.get("rtt_sweep") else ""
    if mode == "compete":
        fl = res.get("flows", []); tot = res.get("total_mbps") or 0
        summary = (f"{len(fl)} flows ran at the same time, one per algorithm, through the same bottleneck for {o.get('duration')} s, with {wtxt}. "
                   + ", ".join(f"{ln(a)} carried {fmt(v)} Mbit/s ({100 * v / max(tot, 1e-9):.0f} %)" for a, v in rank) + ". "
                   f"Together they used {fmt(tot)} Mbit/s, {100 * tot / max(wan0.get('rate_mbit') or 1, 1e-9):.0f} % of the bottleneck; Jain's fairness index was {res.get('jain_fairness')} "
                   f"(1 = equal shares).")
    elif mode == "solo":
        summary = (f"Each algorithm ran alone, one after the other, for {o.get('duration')} s each, with {wtxt}. "
                   + ", ".join(f"{ln(a)} reached {fmt(v)} Mbit/s" for a, v in rank) + f" ({100 * rank[0][1] / max(wan0.get('rate_mbit') or 1, 1e-9):.0f} % of the bottleneck for the fastest)." if rank else "")
    else:
        summary = ((f"The algorithms ran one at a time, up to {o.get('slot_s')} s each, {res.get('seconds', 0):g} s in all, starting with {wtxt} and " if seq else
                f"The flows ran together for {res.get('seconds', 0):g} s, starting with {wtxt} and ")
                   + (f"a sender bit rate of {br:g} Mbit/s per flow. " if br else "no sender bit-rate limit. ")
                   + (lambda nch: f"{'Besides the sweep, ' if o.get('rtt_sweep') else ''}{nch} change{'s were' if nch != 1 else ' was'} made while they ran (section 3). " if nch else
                      "Nothing else was changed while they ran. " if o.get("rtt_sweep") else "Nothing was changed while they ran. ")(
                       max(0, len([m for m in marks if set(m) - {"ts", "algo"}]) - ((len(o["rtts"]) * len(algos) - 1 if seq else len(o["rtts"]) - 1) if o.get("rtt_sweep") else 0)))
                   + "Averaged over the run (time-weighted across the flows' segments): " + ", ".join(f"{ln(a)} {fmt(v)} Mbit/s" for a, v in rank) + ".")
    if sweep_txt: summary = summary.replace(" Averaged over the run", " " + sweep_txt + "Averaged over the run", 1) if " Averaged over the run" in summary else summary + " " + sweep_txt
    if run.get("error"): summary += f" The run ended with an error: {run['error']}."

    # ---- section 2: the lab
    cfg = configs or {}
    hs = [n["name"] for n in (inv or {}).get("nodes", []) if n["role"] == "host"]
    sy = {h: sysctls(section_text(cfg, h, "TCP and socket")) for h in hs}
    ref = sy.get("a-cubic") or next(iter(sy.values()), {})
    diffs = sorted({k for h in hs for k in set(sy[h]) | set(ref) if k != "net.ipv4.tcp_congestion_control" and sy[h].get(k) != ref.get(k)}) if ref else []
    same = ("<p><b>Checked when this report was made:</b> all six hosts have identical TCP and socket settings (" + str(len(ref)) + " sysctls) apart from the congestion control.</p>"
            if ref and not diffs else (f"<p class='bad'>The hosts' settings differ in: {E(', '.join(diffs[:12]))}</p>" if ref else "<p class='note'>(The hosts' settings could not be read when this report was made.)</p>"))
    uname = section_text(cfg, "a-cubic", "System").splitlines()[:1]
    iperf = section_text(cfg, "a-cubic", "iperf3").splitlines()[:1]
    vmrows = []
    stats_src = run.get("node_stats") or node_stats
    for n in (inv or {}).get("nodes", []):
        last = (stats_src.get(n["name"]) or [None])[-1]
        cpus = re.search(r"(\d+) vCPU", section_text(cfg, n["name"], "System"))
        vmrows.append(f"<tr><td class='mono'>{E(n['name'])}</td><td>{E({'host': 'host', 'router': 'router (VyOS)', 'wanem': 'WAN emulator'}[n['role']])}</td>"
                      f"<td>{E(ln(n['cc'])) if n['cc'] else '–'}</td><td class='mono'>{E(n['ip'] if n['role'] != 'wanem' else 'bridge')}</td><td class='mono'>{E(n['mgmt_ip'])}</td>"
                      f"<td class='num'>{cpus.group(1) if cpus else '–'}</td><td class='num'>{f'{last[3] / 1000:.1f} GB' if last else '–'}</td></tr>")
    keyrows = "".join(f"<tr><td class='mono'>{E(k)}</td><td class='mono'>{E(ref.get(k, '–')) if k != 'net.ipv4.tcp_congestion_control' else 'cubic / bbr / reno'}</td><td>{E(d)}</td></tr>" for k, d in HOST_KEYS)
    lab_html = f"""<h2>2. The lab</h2>
<p>The lab is nine KVM virtual machines on one Linux host, joined by isolated libvirt networks. There are two sites. Each has three Alpine Linux hosts, one per congestion control algorithm (CUBIC, BBR, Reno), behind a VyOS router. The routers are joined by <b>wanem</b>, a Linux bridge that adds the WAN's delay, loss and bottleneck. Every host on site A has a peer on site B running the same algorithm. Static routes send all site-to-site traffic through wanem. A separate management network (10.14.0.0/24) carries SSH and the measurements, so they never cross the emulated WAN.</p>
<div class='fig'>{topo_svg(inv, wan0)}</div>
<table><tr><th>Node</th><th>Role</th><th>Algorithm</th><th>Data address</th><th>Management</th><th class='num'>vCPU</th><th class='num'>RAM</th></tr>{''.join(vmrows)}</table>
<h3>The WAN emulator</h3>
<p>wanem bridges eth1 (towards r1) and eth2 (towards r2). Traffic leaving each port goes through two queueing disciplines, so both directions are impaired the same way:</p>
<ul><li><b>netem</b> adds half the round-trip time as a fixed delay, plus random, independent loss when a loss is set. Its queue is large enough to hold everything in flight on the delay line.</li>
<li><b>tbf</b> (token bucket filter) is the bottleneck. It sends at the set rate and queues the excess in a buffer of BUFFER × BDP bytes (BDP = rate × RTT; at least 36 kB, at most 128 MB). When that buffer is full, packets are dropped. This is the queue a congestion control fills.</li></ul>
<p>The NICs' segmentation and receive offloads are off on wanem, so the qdiscs see wire-size packets. Changes are made with <span class='mono'>tc qdisc change</span>, in place: a running flow sees a new RTT, rate, loss or buffer on its next packets without being restarted. The routers' and wanem's links run at MTU 9000.</p>
<h3>The hosts</h3>
<p>All six hosts are built from the same image ({E(uname[0]) if uname else 'Alpine Linux'}) and have the same settings. The only difference is the congestion control each one is set to. eth1 uses the fq qdisc, which paces each flow at the rate its congestion control asks for (BBR depends on it). Each site B host runs an iperf3 server on port 5201 ({E(iperf[0]) if iperf else 'iperf3'}).</p>
<table><tr><th>Setting</th><th>Value</th><th>Meaning</th></tr>{keyrows}</table>
{same}"""

    # ---- section 3: how this run was tested
    how = {"compete": f"All {len(algos)} flows started together{f' ({o.get('stagger'):g} s apart)' if o.get('stagger') else ''} and ran for {o.get('duration')} s, competing for the one bottleneck. This shows how the algorithms share a link: who takes what, how fair that is, and what each one does to the queue and to the others' RTT.",
           "solo": f"Each algorithm ran alone for {o.get('duration')} s, one after the other with 2 s between them, so the bottleneck was never shared. This shows what each algorithm achieves on this path by itself.",
           "live": f"Each algorithm ran alone, in turn ({', '.join(ln(a) for a in algos)}), for up to {o.get('slot_s')} s each; the next one's flow started as soon as the previous one was stopped (at the end of its time, or earlier with Next in the portal), so the bottleneck was never shared. While they ran, the WAN and the sender bit rate could be changed from the portal, and each change is listed below with its time, together with where each algorithm started. WAN changes apply in place to the running flow. iperf3 cannot change its bit rate during a test, so a bit-rate change stops the flow (SIGINT, so iperf3 ends its test cleanly) and starts a new one with the new -b, from slow start." if seq else "The flows ran together until the run was stopped. While they ran, the WAN and the sender bit rate could be changed from the portal, and each change is listed below with its time. WAN changes apply in place to the running flows. iperf3 cannot change its bit rate during a test, so a bit-rate change stops the flows (SIGINT, so iperf3 ends its test cleanly) and starts new ones with the new -b. Each new flow is a new TCP connection and starts in slow start."}[mode]
    method_html = f"""<h2 class='page'>3. How this run was tested</h2>
<p>{how}</p>
<ul><li><b>Traffic:</b> one TCP connection per algorithm, from the site {'A' if o.get('direction', 'a2b') == 'a2b' else 'B'} host to its peer, started by the portal over SSH: <span class='mono'>{cmd}</span>. There is no <span class='mono'>-P</span>, so each flow is a single stream.</li>
<li><b>Per-second data:</b> iperf3's <span class='mono'>--json-stream</span> reports every second, from the sender's TCP_INFO: bytes sent (throughput), the smoothed RTT, the congestion window and retransmits.</li>
<li><b>Whole-flow figures:</b> {'from the per-second samples of each flow segment (a stopped iperf3 reports no receiver total).' if mode == 'live' else "iperf3's final report. Mbit/s is the receiver's goodput over the whole flow, start-up included."}</li>
{'<li><b>Bottleneck queue:</b> wanem&#39;s tbf counters (bytes, drops, backlog) are read once a second over SSH.</li>' if wans else ''}
<li><b>CPU and memory:</b> each node's own /proc/stat and /proc/meminfo, once a second, over one SSH session per node.</li>
<li><b>Integrity:</b> {'the kernel reported the expected algorithm for every flow.' if all(f.get('cc_reported') in (None, f['cc']) for f in res.get('flows', res.get('segments', []))) else '<span class=bad>a flow reported a different algorithm than its host is set to (see the results).</span>'}</li></ul>
<h3>Run parameters</h3><table>{''.join(f'<tr><th style="width:24%">{k}</th><td>{v}</td></tr>' for k, v in setup)}</table>
<h3>Changes during the run</h3>{chg}"""

    # ---- appendices
    app_a = ""
    if cfg:
        app_a = (f"<h2 class='page'>Appendix A. Device configurations</h2><p class='note'>Collected from each device over SSH when this report was made ({time.strftime('%Y-%m-%d %H:%M')}), after the run. "
                 "The WAN emulator therefore shows its settings at that moment; the settings during the run are in section 3. VyOS password hashes are redacted.</p>")
        for k, n in enumerate([n for name in NODE_ORDER for n in (inv or {}).get("nodes", []) if n["name"] == name]):
            secs = cfg.get(n["name"], [])
            if n["role"] == "host" and n["name"] != "a-cubic" and ref and not diffs:   # verified identical: print the list once (A.1)
                secs = [(t, f"Identical to a-cubic (A.1) apart from net.ipv4.tcp_congestion_control = {n['cc']}: checked over all {len(ref)} settings.") if t.startswith("TCP and socket") else (t, txt) for t, txt in secs]
            app_a += (f"<h3 class='node'>A.{k + 1} {E(n['name'])} <span class='sub'>— {E({'host': 'host, ' + ln(n['cc']), 'router': 'router (VyOS)', 'wanem': 'WAN emulator'}[n['role']])}, management {E(n['mgmt_ip'])}</span></h3>" +
                      "".join(f"<h4>{E(t)}</h4><pre class='cfg{' two' if t.startswith('TCP and socket') else ''}'>{E(txt) or '(no output)'}</pre>" for t, txt in secs))
    events = [l for l in log if not l["line"].startswith(("SAMPLE ", "WAN "))]
    mk_iter = iter(labels)
    def logline(t, it):   # START / MARK records in words
        if t.startswith("MARK "): return "changed: " + next(it, t[5:])
        if t.startswith("START "): return f"start: {wtxt}" + (f", -b {br:g}M" if br else ", no -b")
        return t
    app_b = ("<h2 class='page'>Appendix B. Run log</h2><p class='note'>The run's own log, without the per-second samples; times from the start of the run.</p><pre class='cfg'>" +
             E("\n".join(f"{l['t'] - started:7.1f}s  {logline(l['line'], mk_iter)}" for l in events)) + "</pre>")

    # ---- phases between the changes, findings, conclusions
    bounds = [0.0] + [x for x, _ in mk] + [xmax]
    phase_names = ["start"] + [lb for _, lb in mk]
    phases = []
    for k in range(len(bounds) - 1):
        a0, a1 = bounds[k], bounds[k + 1]
        row = {}
        for a in algos:
            v = [s_["mbps"] for s_ in per[a] if a0 < s_["ts"] - t0 <= a1]
            r = [s_["rtt_ms"] for s_ in per[a] if a0 < s_["ts"] - t0 <= a1 and s_["rtt_ms"]]
            row[a] = (sum(v) / len(v) if v else None, sum(r) / len(r) if r else None)
        phases.append((a0, a1, phase_names[k], row))
    ph_tbl = ""
    if len(phases) > 1:
        ph_tbl = ("<table><tr><th>Phase</th><th class='num'>From – to</th>" + "".join(f"<th class='num'><span class='sw' style='background:{COL[a]}'></span>{LABEL[a]} Mbit/s</th>" for a in algos) +
                  "".join(f"<th class='num'>{LABEL[a]} RTT</th>" for a in algos) + "</tr>" +
                  "".join(f"<tr><td>{E('as started' if nm == 'start' else 'after ' + nm)}</td><td class='num'>{a0:.0f}–{a1:.0f} s</td>" +
                          "".join(f"<td class='num'>{fmt(row[a][0])}</td>" for a in algos) + "".join(f"<td class='num'>{fmt(row[a][1])} ms</td>" for a in algos) + "</tr>"
                          for a0, a1, nm, row in phases) + "</table>")
    rtt_cfg = wan0.get("rtt_ms") or 0
    mean_rtt = {a: (sum(x["rtt_ms"] for x in per[a] if x["rtt_ms"]) / max(1, len([x for x in per[a] if x["rtt_ms"]]))) for a in algos}
    retr_tot = {a: sum(x.get("retr") or 0 for x in per[a]) for a in algos}
    find = []
    if len(rank) > 1 and rank[1][1] > 0:
        find.append(f"<b>{ln(rank[0][0])} carried the most</b>: {fmt(rank[0][1])} Mbit/s, {rank[0][1] / rank[1][1]:.1f}× {ln(rank[1][0])} ({fmt(rank[1][1])})" +
                    (f" and {rank[0][1] / max(rank[-1][1], 1e-9):.1f}× {ln(rank[-1][0])} ({fmt(rank[-1][1])})." if len(rank) > 2 else "."))
    elif rank: find.append(f"<b>{ln(rank[0][0])}</b> carried {fmt(rank[0][1])} Mbit/s.")
    if mode == "compete": find.append(f"<b>Sharing:</b> Jain's fairness index {res.get('jain_fairness')} (1 = equal shares); the flows together used {100 * (res.get('total_mbps') or 0) / (wan0.get('rate_mbit') or 1):.0f} % of the bottleneck.")
    if rtt_cfg and any(mean_rtt.values()):
        hi = max(mean_rtt.items(), key=lambda x: x[1])
        find.append(f"<b>Queueing delay:</b> against a {rtt_cfg:g} ms path, the mean RTT the senders saw was " + ", ".join(f"{ln(a)} {fmt(v)} ms" for a, v in mean_rtt.items()) +
                    f"; the most, {fmt(hi[1] - rtt_cfg)} ms above the path, is time spent in the bottleneck queue.")
    if any(retr_tot.values()):
        hr = max(retr_tot.items(), key=lambda x: x[1])
        find.append(f"<b>Retransmits:</b> {ln(hr[0])} retransmitted the most ({hr[1]:,} segments over the run" + ", against " + ", ".join(f"{ln(a)} {v:,}" for a, v in retr_tot.items() if a != hr[0]) + ").")
    for a0, a1, nm, row in phases[1:3]:
        prev = phases[[p_[0] for p_ in phases].index(a0) - 1][3]
        ch = ", ".join(f"{ln(a)} {fmt(prev[a][0])} → {fmt(row[a][0])}" for a in algos if prev[a][0] is not None and row[a][0] is not None)
        if ch: find.append(f"<b>After {E(nm)}</b> (at {a0:.0f} s), mean Mbit/s: {ch}.")
    busy = None
    if nodes:
        busy = max(((n, max(x[1] for x in win[n])) for n in nodes), key=lambda x: x[1])
        memx = max(((n, max(x[2] / x[3] for x in win[n] if x[3])) for n in nodes), key=lambda x: x[1])
        find.append(f"<b>System load:</b> the busiest node was {busy[0]}, at {fmt(busy[1])} % CPU at its peak; the fullest memory was {memx[0]}'s, at {100 * memx[1]:.0f} % of its RAM.")
    findings = "<ul>" + "".join(f"<li>{f_}</li>" for f_ in find) + "</ul>"
    states, cur = [dict(wan0)], dict(wan0)   # the WAN in each phase (for chapter 1's loss table)
    for m in marks: cur.update({k: v for k, v in m.items() if k != "ts"}); states.append(dict(cur))
    reno_ph = [row["reno"][0] if "reno" in row else None for _, _, _, row in phases]

    # ---- figure / table numbering
    fig_n, tbl_n = [0], [0]
    def figure(svg, title, text):
        fig_n[0] += 1
        return f"<div class='fig'>{svg}<div class='cap'><b>Figure {fig_n[0]}.</b> {title}. {text.rstrip('.')}.</div></div>"
    def table(html_, title, text=""):
        tbl_n[0] += 1
        return f"<div class='tbl'>{html_}<div class='cap'><b>Table {tbl_n[0]}.</b> {title}.{(' ' + text.rstrip('.') + '.') if text else ''}</div></div>"

    title = "Comparison of TCP Congestion Control Algorithms" if len(algos) > 1 else f"Performance of TCP {ln(algos[0])}" if algos else "TCP Congestion Control Performance"
    subtitle = {"compete": "Concurrent flows through a shared bottleneck", "solo": "Isolated flows, measured one at a time",
                "live": "Isolated flows under changing network conditions" if seq else "Concurrent flows under changing network conditions"}[mode]
    if len(algos) == 1: subtitle = {"compete": "A single flow through the bottleneck", "solo": "A single flow through the bottleneck", "live": "A single flow under changing network conditions"}[mode]
    if o.get("rtt_sweep"): subtitle = ("A single flow" if len(algos) == 1 else "Isolated flows" if seq else "Concurrent flows") + f" across round-trip times from {o['rtts'][0]:g} to {o['rtts'][-1]:g} ms"
    kicker = "Technical report · " + {"compete": "Concurrent test", "solo": "Isolated test", "live": "Live test, isolated flows" if seq else "Live test, concurrent flows"}[mode]
    lede = (f"{' vs '.join(ln(a) for a in algos)} across an emulated WAN: {wtxt}" + (f", sender bit rate {br:g} Mbit/s per flow" if br else "") + ".")
    cover = f"""<section class='cover'>
<div class='top'><div class='kicker'>{kicker}</div><div class='series'>Network lab series · tcp-cc-lab</div></div>
<div><h1>{title}</h1><p class='subtitle'>{E(subtitle)}</p><p class='lede'>{E(lede)}</p>
<div class='kpis'>{kpis}</div>
<div class='abstract'><div class='h'>Abstract</div><p>{E(summary)}</p></div></div>
<div class='bottom'><div class='author'><div class='name'>{E(author)}</div><div class='date'>{time.strftime('%B %-d, %Y', time.localtime(started))}</div></div>
<div class='meta'>Run <span class='mono'>{E(run['id'])}</span> · {E(run['status'])} · {time.strftime('%H:%M:%S', time.localtime(started))}–{time.strftime('%H:%M:%S', time.localtime(finished))} ({finished - started:.0f} s)<br>
libvirt/KVM · VyOS · Alpine Linux ({E(uname[0].split()[-1]) if uname else 'Linux'}) · {E(iperf[0].split(' (')[0]) if iperf else 'iperf3'}<br>tcp-cc-lab {E(lab_version)} · <a href='{report_references.REPO}'>github.com/dcantor/tcp-cc-lab</a></div></div></section>"""

    chapters = [("1", "Background: Reno, CUBIC and BBR"), ("2", "Test environment"), ("3", "Methodology"), ("4", "Results"), ("5", "System load"), ("6", "Conclusions")] + \
               ([("A", "Appendix: device configurations")] if cfg else []) + [("B", "Appendix: run log"), ("C", "Appendix: repository and references")]
    toc = "<div class='toc'>" + "".join(f"<div><span class='n'>{n_}</span>{E(t)}</div>" for n_, t in chapters) + "</div>"
    h2 = lambda n_, t, first=False: f"<h2><span class='n'>{n_}</span>{E(t)}</h2>"   # noqa: E731

    # chapter 1: background, built first so its figure and tables take the first numbers
    bg = report_background.chapter(wan0, res.get("host_mtu"), algos, states[:len(phases)], reno_ph, figure, table)
    # chapter 2: the environment (from lab_html, with numbered figure and tables)
    env = lab_html.replace("<h2>2. The lab</h2>", "")
    env = env.replace(f"<div class='fig'>{topo_svg(inv, wan0)}</div>", figure(topo_svg(inv, wan0), "The lab", f"Three hosts per site, one per algorithm, joined through r1, wanem and r2. wanem shows the WAN as the run started."))
    env = re.sub(r"(<table><tr><th>Node</th>.*?</table>)", lambda m_: table(m_.group(1), "The nodes", "vCPU and RAM as each node reports them."), env, count=1, flags=re.S)
    env = re.sub(r"(<table><tr><th>Setting</th>.*?</table>)", lambda m_: table(m_.group(1), "The hosts' key TCP settings", "Values from a-cubic."), env, count=1, flags=re.S)
    # chapter 2: methodology
    meth = method_html.replace("<h2 class='page'>3. How this run was tested</h2>", "")
    meth = re.sub(r"<h3>Run parameters</h3><table>(.*?)</table>", lambda m_: "<h3>Run parameters</h3>" + table(f"<table class='kv'>{m_.group(1)}</table>", "Run parameters"), meth, count=1, flags=re.S)
    meth = re.sub(r"<h3>Changes during the run</h3>(<table>.*?</table>)", lambda m_: "<h3>Changes during the run</h3>" + table(m_.group(1), "Changes made while the flows ran", "Times from the start of the run."), meth, count=1, flags=re.S)
    # chapter 3: results
    res_main = re.sub(r"^(<table>.*?</table>)", lambda m_: table(m_.group(1), "Results per flow"), res_tbl, count=1, flags=re.S)
    res_html = res_main + (table(ph_tbl, "Mean throughput and RTT in each phase", "A phase runs from one change to the next; means of the per-second samples.") if ph_tbl else "")
    if o.get("rtt_sweep"): res_html += report_analysis.sweep_section(dict(algos=algos, per=per, t0=t0, phases=phases, states=states, rtts=o["rtts"], step_s=o.get("step_s"),
                                                                     seq=seq, wan0=wan0, mss=next((g.get("mss") for g in res.get("segments", []) if g.get("mss")), None) or ((mtu or 1500) - 52),
                                                                     br=br, figure=figure, table=table, chart=chart, rmem_max=(lambda v: int(v.split()[-1]) if v else None)(ref.get("net.ipv4.tcp_rmem"))))
    res_html += "<h3>Per second</h3>" + legend(algos) + "".join(figure(c, t, s_[0].upper() + s_[1:]) for t, s_, c in figs)
    cw = [(a, LABEL[a], [(s_["ts"] - t0, s_["cwnd"] / 1e6) for s_ in per[a] if s_.get("cwnd")]) for a in algos]
    ana, ana_find = report_analysis.chapter(dict(algos=algos, per=per, wans=wans, mk=mk, phases=phases, states=states, wan0=wan0, res=res, br=br, mode="solo" if seq else mode,
                                                 mtu=mtu, t0=t0, xmax=xmax, figure=figure, table=table, chart=chart, cwnd_series=cw if any(x[2] for x in cw) else None))
    res_html += "<h2 class='sub'>Analysis: what the results mean</h2>" + ana
    find[1:1] = ana_find
    if len({st_.get("rtt_ms") for st_ in states}) > 1 or any(f_.startswith("<b>The bottleneck buffer filled") for f_ in ana_find):   # the analysis has the per-phase figure
        find = [f_ for f_ in find if not f_.startswith("<b>Queueing delay:")]
    findings = "<ul>" + "".join(f"<li>{f_}</li>" for f_ in find) + "</ul>"
    # chapter 4: system load
    load = nodes_html.replace("<div class='fig'><h2>Nodes during the run</h2>", "<div>").replace("<h2>Nodes during the run</h2>", "")
    load = re.sub(r"(<table><tr><th>Node</th><th class='num'>CPU mean</th>.*?</table>)", lambda m_: table(m_.group(1), "CPU and memory per node during the run"), load, count=1, flags=re.S)
    load = re.sub(r"<div class='fig'><h3>CPU <span class='sub'>% of vCPUs</span></h3>(.*?)</div>(?=<div class='fig'><h3>Memory)",
                  lambda m_: figure(m_.group(1), "CPU per node", "% of the node's vCPUs; solid lines site A and r1, dashed site B and r2"), load, count=1, flags=re.S)
    load = re.sub(r"<div class='fig'><h3>Memory in use <span class='sub'>MB · dashed: RAM</span></h3>(.*)</div>$",
                  lambda m_: figure(m_.group(1), "Memory in use per node", "MB (MemTotal − MemAvailable); the dashed line is the RAM"), load, count=1, flags=re.S)
    # chapter 5: conclusions
    concl = ("<p>" + E(summary) + "</p>" + findings +
             "<div class='box'><div class='h'>Reading these results</div><ul>"
             "<li>This is one run. Differences of a few per cent between runs are normal; large differences (several times) are not noise.</li>"
             "<li>Emulated loss is random and independent. Real loss is often bursty, or caused by congestion elsewhere.</li>"
             "<li>Linux 6.12 ships BBR v1, which does not back off on isolated losses. BBRv3 would behave differently in lossy phases.</li>"
             "<li>The VMs forward about 2–2.5 Gbit/s at MTU 1500 and 3.5–4 Gbit/s at 9000. Above that, the path, not the bottleneck, sets the limit.</li></ul></div>")
    body = f"""{cover}
<h2>Contents</h2>{toc}
<h2>Executive summary</h2><div class='kpis'>{kpis}</div><p>{E(summary)}</p><div class='box'><div class='h'>Key findings</div>{findings}</div>
<section class='chapter'>{h2('1', 'Background: Reno, CUBIC and BBR')}{bg}</section>
<section class='chapter'>{h2('2', 'Test environment')}{env}</section>
<section class='chapter'>{h2('3', 'Methodology')}{meth}</section>
<section class='chapter'>{h2('4', 'Results')}{res_html}</section>
<section class='chapter'>{h2('5', 'System load')}<p>Each node's own /proc/stat and /proc/meminfo, sampled once a second over one SSH session per node. CPU is a percentage of the node's vCPUs.</p>{load}</section>
<section class='chapter'>{h2('6', 'Conclusions')}{concl}</section>
{app_a.replace("<h2 class='page'>Appendix A. Device configurations</h2>", "<section class='chapter'>" + h2('A', 'Appendix: device configurations')) + '</section>' if app_a else ''}
<section class='chapter'>{h2('B', 'Appendix: run log')}{app_b.replace("<h2 class='page'>Appendix B. Run log</h2>", "")}</section>
<section class='chapter'>{h2('C', 'Appendix: repository and references')}{report_references.appendix(lab_version)}</section>"""
    body = report_references.add_citations(body)
    css = CSS.replace("%(runid)s", run["id"]).replace("%(version)s", lab_version).replace("%(author)s", author)
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{E(title)}: {E(subtitle)} · flow test {E(run['id'])}</title><style>{css}</style></head><body>{body}</body></html>"


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

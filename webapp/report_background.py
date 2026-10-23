"""Chapter 1 of the flow report: what Reno, CUBIC and BBR are, with the numbers worked out for the run's own path."""
import math
from html import escape as E  # noqa: F401  (kept for symmetry with report.py)

COL = {"cubic": "#2a78d6", "bbr": "#eb6834", "reno": "#1baf7a"}

INTRO = """<p>All three algorithms answer the same question: how fast may a TCP sender send, when nothing on the path tells it the speed of the slowest link or how many others are using it? This chapter explains how each one answers it, then works out what that means on the path used in this run.</p>
<h3>The problem</h3>
<p>The data crosses a chain of links, and the slowest of them, the <b>bottleneck</b>, sets the pace. A sender that goes too slowly leaves the link idle. One that goes too fast builds a queue in the bottleneck's <b>buffer</b>, which adds delay, and once the buffer is full, packets are dropped and must be sent again. The sender only learns what happened to a packet one <b>round-trip time</b> (RTT) later.</p>
<p>TCP controls its speed with the <b>congestion window</b> (cwnd): how much data it may have sent and not yet had acknowledged. Throughput is roughly cwnd ÷ RTT. To keep the link busy, cwnd must be at least the <b>bandwidth-delay product</b> (BDP): rate × RTT, the amount of data "in the pipe". Each algorithm is a strategy for finding and holding that window, and for sharing the link fairly with everyone else doing the same.</p>
<h3>Reno: loss-based, additive increase</h3>
<p>Reno grew out of Van Jacobson's 1988 congestion-avoidance work (TCP Tahoe), which followed the Internet's congestion collapses of 1986. It was refined as TCP Reno in 1990 and standardised in RFC 5681. Its signal is <b>packet loss</b>.</p>
<ul><li><b>Slow start:</b> a new connection starts with 10 segments and doubles cwnd every RTT until the first loss, or until it reaches ssthresh, the level of the last loss.</li>
<li><b>Congestion avoidance (AIMD):</b> after that, additive increase of one segment per RTT, and multiplicative decrease, halving cwnd, on each loss. Fast retransmit and fast recovery resend a lost segment after three duplicate acknowledgements, without waiting for a timeout.</li>
<li><b>Consequence:</b> the window traces a sawtooth. Recovery after a loss takes cwnd/2 round trips, which is quick on a LAN but minutes on a long fat path. Reno fills the buffer before it backs off, so it always runs with a full queue.</li></ul>
<h3>CUBIC: loss-based, growth by time</h3>
<p>CUBIC (Ha, Rhee and Xu, 2008; RFC 9438) has been Linux's default since kernel 2.6.19 (2006), and is the default in Windows and macOS too. It keeps loss as the signal and changes the growth curve. After a loss it cuts cwnd to 70 % (β = 0.7), not 50 %. It then grows cwnd as a cubic function of the <b>time</b> since that loss:</p>
<p style='text-align:center' class='serif'><i>W(t) = C · (t − K)<sup>3</sup> + W<sub>max</sub></i>, &nbsp; <i>K = ∛(W<sub>max</sub> · (1 − β) / C)</i>, &nbsp; C = 0.4</p>
<ul><li>The window climbs fast at first, flattens as it nears W<sub>max</sub> (the size at the last loss), lingers there, then accelerates again to probe for more.</li>
<li>Because growth depends on elapsed time and not on round trips, a long-RTT flow regains its window about as fast, in seconds, as a short-RTT one. That is CUBIC's main advance over Reno on long fat networks.</li>
<li>A "TCP-friendly" region makes CUBIC grow at least as fast as Reno would, so it is never slower than Reno.</li>
<li>Like Reno, it fills the buffer: it only backs off when the buffer overflows.</li></ul>
<h3>BBR: model-based, paced</h3>
<p>BBR, "Bottleneck Bandwidth and Round-trip propagation time" (Cardwell et al., Google, 2016), does not treat loss as its signal. It builds a model of the path from two measurements:</p>
<ul><li><b>BtlBw:</b> the highest delivery rate seen over the last ~10 round trips;</li><li><b>RTprop:</b> the lowest RTT seen over the last 10 s.</li></ul>
<p>Their product is its BDP estimate. BBR <b>paces</b> packets at BtlBw: the host's fq qdisc spaces them out, which is why every host in this lab uses fq. It caps the data in flight at about 2 × BDP, and it runs through four phases:</p>
<ul><li><b>Startup:</b> grows the rate 2.89× per RTT until the delivery rate stops rising.</li>
<li><b>Drain:</b> empties the queue that Startup built.</li>
<li><b>ProbeBW:</b> cycles the pacing gain through 1.25, 0.75, 1, 1, 1, 1, 1, 1. One RTT above the estimate looks for more bandwidth; one below drains what that built.</li>
<li><b>ProbeRTT:</b> every 10 s, drops to 4 packets in flight for 200 ms to re-measure the empty-pipe RTT.</li></ul>
<p>Because an isolated loss does not change its model, BBR keeps its rate on links with random, non-congestion loss, where Reno and CUBIC collapse. The cost is that BBR v1, which Linux 6.12 ships, also ignores the losses it causes. In a shallow buffer it can overflow the queue repeatedly, retransmit heavily, and take most of the link from loss-based flows. BBRv2 and BBRv3 add a response to loss and ECN, but they are not in mainline Linux.</p>"""

COMPARE = """<table><tr><th></th><th>Reno</th><th>CUBIC</th><th>BBR (v1, Linux 6.12)</th></tr>
<tr><th>Signal</th><td>packet loss</td><td>packet loss</td><td>measured delivery rate and minimum RTT</td></tr>
<tr><th>On a loss</th><td>cwnd × 0.5</td><td>cwnd × 0.7</td><td>no special reaction</td></tr>
<tr><th>Growth</th><td>+1 segment per RTT</td><td>cubic in time since the loss; independent of RTT</td><td>paces at BtlBw; probes 25 % above it, one RTT in eight</td></tr>
<tr><th>The bottleneck queue</th><td>fills it</td><td>fills it</td><td>keeps it small</td></tr>
<tr><th>Random (non-congestion) loss</th><td>collapses: ∝ 1/√p</td><td>degrades, less than Reno</td><td>largely unaffected</td></tr>
<tr><th>Sharing a bottleneck</th><td>fair with itself; loses to CUBIC</td><td>fair with itself; beats Reno</td><td>takes the most in shallow buffers; less in deep ones</td></tr>
<tr><th>Origin; in Linux</th><td>1988–1990, RFC 5681; always</td><td>2008, RFC 9438; default since 2.6.19 (2006)</td><td>2016, Google; since 4.9, opt-in per host or socket</td></tr></table>"""


def fmt(v):
    return "–" if v is None else f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"


def shapes(w=680, h=200):
    """A schematic of the three window shapes over time: illustrative, not data."""
    L, R, T, B = 40, 70, 12, 26
    X = lambda t: L + t / 30 * (w - L - R)   # noqa: E731
    Y = lambda v: T + (1 - v / 1.3) * (h - T - B)   # noqa: E731
    reno, v, t = [], 0.56, 0.0
    while t <= 30:   # +1 segment per RTT (a straight line), halve at the top
        reno.append((t, v)); t += 0.1; v += 0.004
        if v >= 1.12: reno.append((t, 1.12)); v = 0.56
    cubic, t_loss = [], -2.0
    K, C, WMAX = 4.0, 0.3 * 1.12 / 64, 1.12   # W(t) = C (t - K)^3 + Wmax, starting from 0.7 Wmax
    for i in range(301):
        t = i / 10; v = C * ((t - t_loss) - K) ** 3 + WMAX
        if v >= 1.17: cubic.append((t, 1.17)); t_loss = t; v = 0.7 * WMAX
        cubic.append((t, v))
    bbr = []
    for i in range(301):   # Startup, Drain, ProbeBW pulses, one ProbeRTT
        t = i / 10
        if t < 1.0: v = min(1.25, 0.1 * 2.89 ** (t / 0.33))
        elif t < 1.3: v = 0.95
        else:
            ph = int((t - 1.3) / 0.6) % 8; v = 1.12 if ph == 0 else 0.92 if ph == 1 else 1.0
        if 20 <= t < 20.4: v = 0.06
        bbr.append((t, v))
    s = [f"<svg viewBox='0 0 {w} {h}' xmlns='http://www.w3.org/2000/svg' style='width:100%;font-size:10px'>"]
    s.append(f"<line x1='{L}' x2='{w - R}' y1='{Y(1):.1f}' y2='{Y(1):.1f}' stroke='#8a8984' stroke-dasharray='3 3'/>")
    s.append(f"<line x1='{L}' x2='{w - R}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' stroke='#cfcfca'/><text x='{(L + w - R) / 2}' y='{h - 6}' text-anchor='middle' fill='#52514e'>time →</text>"
             f"<text x='14' y='{(T + h - B) / 2}' transform='rotate(-90 14 {(T + h - B) / 2})' text-anchor='middle' fill='#52514e'>data in flight</text>")
    for key, pts in (("reno", reno), ("cubic", cubic), ("bbr", bbr)):
        d = " ".join(("M" if i == 0 else "L") + f"{X(a):.1f},{Y(b):.1f}" for i, (a, b) in enumerate(pts))
        s.append(f"<path d='{d}' fill='none' stroke='{COL[key]}' stroke-width='1.8' stroke-linejoin='round'/>")
    ends = sorted([(Y(reno[-1][1]), "Reno"), (Y(cubic[-1][1]), "CUBIC"), (Y(bbr[-1][1]), "BBR")])
    for i in range(1, 3):
        if ends[i][0] - ends[i - 1][0] < 12: ends[i] = (ends[i - 1][0] + 12, ends[i][1])
    s.extend(f"<text x='{w - R + 6}' y='{y + 3:.1f}' font-weight='600'>{n}</text>" for y, n in ends)
    s.append(f"<text x='{X(20.2):.1f}' y='{Y(0.06) - 5:.1f}' text-anchor='middle' fill='#52514e'>BBR ProbeRTT</text>"
             f"<text x='{X(0.6):.1f}' y='{Y(1.25) - 4:.1f}' fill='#52514e'>BBR Startup</text></svg>")
    return "".join(s)


def chapter(wan0, mtu, algos, phase_states, phase_reno, figure, table):
    """The chapter's HTML. figure / table are the report's numbering helpers, called in page order."""
    fig = figure(shapes(), "How the window evolves, schematically",
                 "The dashed line is the BDP: below it the link is partly idle, above it a queue builds in the buffer. Reno's straight-line sawtooth halves at each loss. CUBIC drops to 70 %, regrows fast, plateaus near W<sub>max</sub> and then probes beyond it. BBR's Startup is followed by paced, near-constant data in flight, with ProbeBW pulses and a ProbeRTT dip. Illustrative curves, not measurements")
    cmp_ = table(COMPARE, "Reno, CUBIC and BBR compared")
    rtt = (wan0.get("rtt_ms") or 0) / 1000; rate = (wan0.get("rate_mbit") or 0) * 1e6
    mss = 8948 if mtu == 9000 else 1448
    worked = ""
    if rtt > 0 and rate > 0:
        bdp = rate / 8 * rtt; segs = bdp / mss
        reno_t = segs / 2 * rtt; cubic_k = (segs * 0.3 / 0.4) ** (1 / 3)
        startup = (math.log(max(segs, 10) / 10) / math.log(2.89) + 2) * rtt
        worked = table(f"""<table><tr><th>For this run's path, at the start</th><th class='num'>Value</th><th>How it is worked out</th></tr>
<tr><td>Bandwidth-delay product (BDP)</td><td class='num'>{bdp / 1e6:.2f} MB · {segs:,.0f} segments</td><td>{wan0.get('rate_mbit'):g} Mbit/s × {rtt * 1000:g} ms; MSS {mss} bytes at host MTU {mtu or 1500}</td></tr>
<tr><td>Reno: time to regrow after one loss</td><td class='num'>{reno_t:,.1f} s</td><td>the window halves to {segs / 2:,.0f} segments, then grows by 1 segment per RTT: {segs / 2:,.0f} RTTs of {rtt * 1000:g} ms</td></tr>
<tr><td>CUBIC: time to regain W<sub>max</sub> after one loss</td><td class='num'>{cubic_k:,.1f} s</td><td>K = ∛(W<sub>max</sub> · (1 − β) / C) with W<sub>max</sub> = {segs:,.0f} segments, β = 0.7, C = 0.4; the same at any RTT</td></tr>
<tr><td>BBR: Startup to fill the pipe</td><td class='num'>≈ {startup:,.2f} s</td><td>the rate grows 2.89× per RTT from 10 segments, plus about two RTTs to detect the plateau and drain</td></tr></table>""",
                       "What one loss costs on this path", "Idealised: a single flow with a 1 × BDP window and no further losses while it recovers")
    mathis = ""
    if "reno" in algos and rtt > 0:
        rows, seen = [], set()
        for st, meas in zip(phase_states, phase_reno):
            lp, r_ = st.get("loss_pct") or 0, (st.get("rtt_ms") or 0) / 1000
            if lp <= 0 or r_ <= 0 or (lp, r_) in seen: continue
            seen.add((lp, r_)); est = mss * 8 / r_ * 1.22 / math.sqrt(lp / 100) / 1e6; cap = st.get("rate_mbit") or est
            rows.append(f"<tr><td class='num'>{lp:g} %</td><td class='num'>{r_ * 1000:g} ms</td><td class='num'>{fmt(min(est, cap))} Mbit/s{' (capped by the link)' if est > cap else ''}</td>"
                        f"<td class='num'>{fmt(meas) + ' Mbit/s' if meas is not None else '–'}</td></tr>")
        if rows:
            mathis = ("<p>For random loss, a classic estimate of a Reno flow's steady-state throughput (Mathis et al., 1997) is "
                      "<i>rate ≈ (MSS ÷ RTT) · 1.22 ÷ √p</i>, where p is the loss probability. It shows why loss hurts long paths so badly: throughput falls in proportion to the RTT and to the square root of the loss rate. "
                      "The table below applies it to the loss rates used in this run, next to what Reno measured in the same phase. Reno shared the link with the other flows in a compete or live run, so it can measure below the estimate.</p>" +
                      table("<table><tr><th class='num'>Random loss</th><th class='num'>Path RTT</th><th class='num'>Mathis estimate for Reno</th><th class='num'>Reno measured in that phase</th></tr>" + "".join(rows) + "</table>",
                            "Reno's loss-limited throughput: estimate and measurement", "The estimate uses the configured path RTT; queueing makes the real RTT longer"))
    return INTRO + fig + "<h3>Side by side</h3>" + cmp_ + "<h3>What this means on this run's path</h3>" + worked + mathis

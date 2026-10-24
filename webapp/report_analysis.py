"""Chapter 4's analysis in the flow report: what the run's numbers mean. Everything here is derived from the run itself
(per-second samples, the bottleneck's counters, the host's tap drop counters, the WAN in each phase) and explained against
how each algorithm works (chapter 1)."""
import math
import statistics as st
from html import escape as E

LABEL = {"cubic": "CUBIC", "bbr": "BBR", "reno": "Reno"}
COL = {"cubic": "#2a78d6", "bbr": "#eb6834", "reno": "#1baf7a"}
PATH_CEIL = {1500: (2000, 2500), 9000: (3500, 4000)}   # Mbit/s the VMs forward in total (measured, 0.6.0)
LOSS_BASED = ("reno", "cubic")


def fmt(v, d=None):
    if v is None: return "–"
    if d is not None: return f"{v:,.{d}f}"
    a = abs(v)
    return f"{v:,.0f}" if a >= 100 else f"{v:,.1f}" if a >= 10 else f"{v:,.2f}" if a >= 1 else f"{v:.3g}"


def pct(v):   # a loss rate (fraction) as a readable percentage
    if v is None: return "–"
    p = 100 * v
    return "0 %" if p == 0 else f"{p:.2g} %" if p < 0.01 else f"{p:.3f} %" if p < 1 else f"{p:.1f} %"


def sw(a): return f"<span class='sw' style='background:{COL.get(a, '#888')}'></span>{LABEL.get(a, a)}"


def p95(xs): xs = sorted(xs); return xs[min(len(xs) - 1, int(0.95 * len(xs)))] if xs else None


def window_segs(mbps, rtt_ms, mss): return mbps * 1e6 / 8 * rtt_ms / 1000 / mss


def reno_rate(p, rtt_ms, mss): return mss * 8 / (rtt_ms / 1000) * 1.22 / math.sqrt(p) / 1e6   # Mathis et al.


def cubic_rate(p, rtt_ms, mss):   # RFC 9438 §5: average window C(3+β)/(4(1−β)))^¼ · RTT^¾ / p^¾, never below the Reno-friendly window
    w = (0.4 * 3.7 / 1.2) ** 0.25 * (rtt_ms / 1000) ** 0.75 / p ** 0.75
    return max(w, 1.22 / math.sqrt(p)) * mss * 8 / (rtt_ms / 1000) / 1e6


def implied_p(a, mbps, rtt_ms, mss):
    """The random loss-event rate at which the model gives the measured throughput (the model inverted)."""
    w = window_segs(mbps, rtt_ms, mss)
    if w <= 0: return None
    p_reno = (1.22 / w) ** 2
    if a == "reno": return p_reno
    p_cubic = ((0.4 * 3.7 / 1.2) ** 0.25 * (rtt_ms / 1000) ** 0.75 / w) ** (4 / 3)
    return max(p_reno, p_cubic)   # whichever region CUBIC was in (it takes the larger window)


def backoffs(xs):
    """Seconds where throughput fell below 70 % of the median of the 3 seconds before: (index, before, after, recovery seconds)."""
    out = []
    for i in range(3, len(xs)):
        before = st.median(xs[i - 3:i])
        if before > 0 and xs[i] < 0.7 * before and not (out and i - out[-1][0] <= 2):
            rec = next((j - i for j in range(i + 1, len(xs)) if xs[j] >= 0.9 * before), None)
            out.append((i, before, xs[i], rec))
    return out


def chapter(c):
    """c: algos, per (samples per algorithm), wans, mk, phases, states, wan0, res, br, mode, mtu, t0, xmax, figure, table, chart.
    Returns (html, findings)."""
    algos, per, wans, mk, phases, states, wan0, res = c["algos"], c["per"], c["wans"], c["mk"], c["phases"], c["states"], c["wan0"], c["res"]
    br, mode, mtu, figure, table = c["br"], c["mode"], c["mtu"] or 1500, c["figure"], c["table"]
    R = wan0.get("rate_mbit") or 0; rtt0 = wan0.get("rtt_ms") or 0; loss0 = (wan0.get("loss_pct") or 0) / 100
    flows = res.get("flows") or res.get("segments") or []
    mss = next((f.get("mss") for f in flows if f.get("mss")), None) or (mtu - 52)
    ceil = PATH_CEIL.get(mtu, PATH_CEIL[1500])
    n = len(algos)
    html, find = [], []
    def state_at(t):
        for k, (a0, a1, _, _) in enumerate(phases):
            if a0 < t <= a1 or (k == 0 and t <= a0): return states[min(k, len(states) - 1)] if states else wan0
        return states[-1] if states else wan0
    t0 = c["t0"]
    loss_vals = sorted({(st_.get("loss_pct") or 0) for st_ in (states or [wan0])})
    loss_txt = f"{loss_vals[0]:g} % each way" if len(loss_vals) == 1 else f"{loss_vals[0]:g}–{loss_vals[-1]:g} % each way, as it changed during the run"
    marks_t = [x for x, _ in mk]

    # ---- per-algorithm figures from the samples
    S = {}
    for a in algos:
        xs = [s["mbps"] or 0 for s in per[a]]
        rt = [s["rtt_ms"] for s in per[a] if s.get("rtt_ms")]
        segs = sum(x * 1e6 / 8 / mss for x in xs)
        retr = sum(s.get("retr") or 0 for s in per[a])
        ep = [s.get("retr") or 0 for s in per[a] if (s.get("retr") or 0) > 0]
        mean = st.mean(xs) if xs else 0
        steady = xs[5:] if len(xs) > 10 else xs
        cw = [s["cwnd"] / 1e6 for s in per[a] if s.get("cwnd")]
        need = [(s["mbps"] or 0) * 1e6 / 8 * (s["rtt_ms"] or rtt0) / 1000 / 1e6 for s in per[a] if s.get("cwnd")]
        top = p95(xs) or 0
        S[a] = dict(xs=xs, mean=mean, segs=segs, retr=retr, rate=retr / segs if segs else 0, episodes=len(ep), burst=st.mean(ep) if ep else 0,
                    secs=len(xs), rtt_min=min(rt) if rt else None, rtt_mean=st.mean(rt) if rt else None, rtt_p95=p95(rt),
                    cv=(st.pstdev(steady) / st.mean(steady)) if steady and st.mean(steady) > 0 else 0,
                    cwnd=st.median(cw) if cw else None, need=st.median(need) if need else None,
                    rise=next((i + 1 for i, x in enumerate(xs) if x >= 0.9 * top), None) if top else None, bo=backoffs(xs))
    if not any(S[a]["secs"] for a in algos): return "<p class='note'>No per-second samples: nothing to analyse.</p>", []
    total = sum(S[a]["mean"] for a in algos)

    # ---- 1. what limited each flow
    def limit_of(a):
        s_ = S[a]; m = s_["mean"]
        caps = []
        if br: caps.append((br, "the sender's -b cap"))
        shared = n > 1 and mode != "solo"   # a flow may take any part of a shared limit the others leave free
        if R: caps.append((R, "the bottleneck" + (", shared" if shared else "")))
        caps.append((ceil[0], "the VMs' forwarding ceiling" + (", shared" if shared else "")))
        cap, what = min(caps)
        if br and m >= 0.95 * br:
            why = ("<b>Application-limited.</b> It sent exactly what iperf3 offered: -b, not the network, set its rate. "
                   + ("Its window stayed well above what that rate needs, so the window never limited it." if s_["cwnd"] and s_["need"] and s_["cwnd"] > 1.5 * s_["need"] else ""))
        elif R and (mode == "solo" or n == 1) and m >= 0.9 * R:
            why = "<b>Bottleneck-limited.</b> It filled the emulated bottleneck: the best any algorithm can do here."
        elif mode != "solo" and n > 1 and R and total >= 0.85 * R:
            sh = m / total; fair = 1 / n
            why = (f"<b>Competition-limited.</b> The flows together filled the bottleneck ({fmt(total)} of {fmt(R)} Mbit/s), so what each got depended on the others. "
                   f"This flow took {100 * sh:.0f} % (an equal share is {100 * fair:.0f} %): "
                   + ("well above its share. It kept sending while the others backed off." if sh > 1.5 * fair else
                      "it was squeezed out. Each time the shared buffer overflowed it cut its window, and the space it gave up was taken by a flow that did not back off." if sh < 0.5 * fair else
                      "close to an equal share.") + " See Sharing below.")
        elif total >= 0.85 * ceil[0] and (not R or R > total * 1.1):
            why = (f"<b>Path-limited.</b> The flows together moved {fmt(total)} Mbit/s, close to what these VMs forward at MTU {mtu} "
                   f"({ceil[0]:,}–{ceil[1]:,} Mbit/s); the emulated bottleneck{f' ({fmt(R)} Mbit/s)' if R else ''} was never reached.")
        elif a in LOSS_BASED:
            why = (f"<b>Loss-limited.</b> It reached {100 * m / cap:.0f} % of {what}. A loss-based algorithm cuts its window on every loss, "
                   f"and on this path it lost often enough ({s_['episodes']} of {s_['secs']} seconds had retransmits) that its window never stayed at the size the rate needs.")
        else:
            why = (f"<b>Below its limit.</b> It reached {100 * m / cap:.0f} % of {what}. BBR does not back off on loss, so a shortfall points to its "
                   "bandwidth estimate: start-up, a ProbeRTT dip, or a change in the path it had not yet re-measured.")
        return cap, what, why
    rows = []
    for a in algos:
        cap, what, why = limit_of(a)
        if why.startswith("<b>Competition"): cap, what = R, "the shared bottleneck"
        rows.append(f"<tr><td>{sw(a)}</td><td class='num'>{fmt(S[a]['mean'])}</td><td class='num'>{fmt(cap)}</td><td>{what}</td><td class='num'>{100 * S[a]['mean'] / cap:.0f} %</td><td>{why}</td></tr>")
    html.append("<h3>What limited each flow</h3><p>Every flow is held back by something: the sender's own cap (-b), the bottleneck, the other flows, "
                "the virtual machines' forwarding speed, or the algorithm's reaction to loss. The table compares each flow's mean with the tightest "
                "of those limits that applied to it, and names the one that explains the result.</p>" +
                table("<table><tr><th>Algorithm</th><th class='num'>Mean Mbit/s</th><th class='num'>Tightest limit</th><th>Which</th><th class='num'>Reached</th><th style='width:46%'>Explanation</th></tr>"
                      + "".join(rows) + "</table>", "What limited each flow", "Means of the per-second samples; the limit is the lowest of -b, the bottleneck and the VMs' forwarding ceiling (the last two shared when flows run together)"))
    for a in algos:
        _, _, why = limit_of(a)
        if why.startswith("<b>Loss-limited") or why.startswith("<b>Application"):
            find.append(f"<b>{LABEL[a]} was {why[3:why.index('.</b>')].lower()}</b> ({fmt(S[a]['mean'])} Mbit/s).")

    # ---- 2. where the losses came from
    pk = sum(S[a]["segs"] for a in algos)
    exp_rand = sum((s["mbps"] or 0) * 1e6 / 8 / mss * (state_at(s["ts"] - t0).get("loss_pct") or 0) / 100 for a in algos for s in per[a])
    loss_mean = exp_rand / pk if pk else 0   # the random loss the run's packets met, on average
    tbf = None
    if wans:
        tbf = 0.0
        for i in range(1, len(wans)): tbf += (wans[i].get("drops_s") or 0) * (wans[i]["ts"] - wans[i - 1]["ts"])
    elif (res.get("bottleneck") or {}).get("drops") is not None: tbf = res["bottleneck"]["drops"]
    pd = res.get("path_drops")
    retr_all = sum(S[a]["retr"] for a in algos)
    lrows = "".join(f"<tr><td>{sw(a)}</td><td class='num'>{S[a]['segs']:,.0f}</td><td class='num'>{S[a]['retr']:,}</td><td class='num'>{pct(S[a]['rate'])}</td>"
                    f"<td class='num'>{S[a]['episodes']} of {S[a]['secs']}</td><td class='num'>{fmt(S[a]['burst'])}</td></tr>" for a in algos)
    src = [f"<li><b>Emulated random loss</b> ({loss_txt}): about <b>{exp_rand:,.0f}</b> of the {pk:,.0f} data segments sent are expected to be dropped by netem"
           + (", plus their ACKs' share in the other direction (a lost ACK is rarely noticed: the next one covers it)." if exp_rand else ".") + "</li>"]
    if tbf is not None:
        src.append(f"<li><b>Bottleneck overflow</b>: tbf dropped <b>{tbf:,.0f}</b> packets because its buffer was full. "
                   + ("These are congestion losses: the flows sent faster than the bottleneck rate for long enough to fill the buffer." if tbf > 0 else "The buffer never overflowed.") + "</li>")
    if pd is not None:
        tot_pd = sum(pd.values()); big = sorted(((k, v) for k, v in pd.items() if v), key=lambda x: -x[1])
        src.append(f"<li><b>Virtual-machine receive drops</b>: the physical host's tap devices dropped <b>{tot_pd:,}</b> packets because a VM did not take them off its "
                   "virtio ring in time" + (": " + ", ".join(f"{v:,} into {k}" for k, v in big[:4]) if big else "") + ". These are not part of the emulated WAN; "
                   "they come from the lab's own forwarding limit, and arrive in bursts (a ring overflows for a moment and many packets in a row are lost).</li>")
    else:
        src.append("<li><b>Virtual-machine receive drops</b>: not recorded for this run (the portal counts the host's tap drops from 0.10.0 on).</li>")
    known = exp_rand + (tbf or 0) + (sum(pd.values()) if pd else 0)
    verdict = ""
    if retr_all:
        r_ = retr_all / max(1, exp_rand) if exp_rand else None
        if r_ and r_ > 3:
            verdict = (f"<p>The flows retransmitted <b>{retr_all:,}</b> segments, {r_:,.0f}× what the emulated random loss alone would cause. "
                       + (f"The counters above account for {100 * min(1, known / retr_all):.0f} % of them. " if (pd is not None or tbf) else "")
                       + "Retransmits overstate losses a little (a retransmission can itself be lost, and is then counted twice), but a gap this large means most of the "
                       "losses were not the random loss that was set: they were " + ("congestion drops at the bottleneck" if tbf and tbf > 0.5 * retr_all else
                                                                                    "drops in the lab's own forwarding path, outside the emulator" if pd and sum(pd.values()) > 0.3 * retr_all else
                                                                                    "drops the lab's counters do not see (most likely the VMs' receive path)") + ".</p>")
        else:
            verdict = (f"<p>The flows retransmitted <b>{retr_all:,}</b> segments; the counters above account for only {100 * known / retr_all:.0f} % of them. "
                       "The rest were lost where the lab has no counter for this run, most likely the VMs' receive path.</p>") if known < 0.5 * retr_all else \
                      f"<p>The flows retransmitted <b>{retr_all:,}</b> segments, in line with the losses accounted for above" + (", most of them congestion drops at the full bottleneck buffer." if tbf and tbf > 0.5 * retr_all else ".") + "</p>"
    if retr_all:
        if "bbr" in algos and any(a in LOSS_BASED for a in algos) and S["bbr"]["retr"] > max(S[a]["retr"] for a in algos if a in LOSS_BASED):
            verdict += ("<p>BBR retransmitted the most, though every flow crossed the same path. That is the design at work: a loss-based flow slows down after a loss, "
                        "so it sends less into the next burst of drops, while BBR keeps sending at its bandwidth estimate and loses, and resends, more. BBR's higher throughput comes at that cost.</p>")
    html.append("<h3>Where the losses came from</h3><p>A loss-based algorithm's speed is set by how often it loses packets, so it matters where losses happen. "
                "Three things can drop a packet on this path:</p><ul>" + "".join(src) + "</ul>" + verdict +
                table("<table><tr><th>Algorithm</th><th class='num'>Segments sent</th><th class='num'>Retransmitted</th><th class='num'>Rate</th><th class='num'>Seconds with retransmits</th><th class='num'>Per such second</th></tr>" + lrows + "</table>",
                      "Retransmissions per flow", f"Segments sent estimated from the per-second throughput at an MSS of {mss} bytes. 'Per such second': the mean number of segments retransmitted in a second that had any, a measure of how bursty the losses were"))

    # ---- 3. the loss models
    lb = [a for a in algos if a in LOSS_BASED and S[a]["mean"] > 0 and S[a]["rtt_mean"]]
    if lb:
        mrows = []
        for a in lb:
            s_ = S[a]; w = window_segs(s_["mean"], s_["rtt_mean"], mss); ip = implied_p(a, s_["mean"], s_["rtt_mean"], mss)
            pred = (reno_rate if a == "reno" else cubic_rate)(loss_mean, s_["rtt_mean"], mss) if loss_mean else None
            mrows.append(f"<tr><td>{sw(a)}</td><td class='num'>{fmt(s_['mean'])}</td><td class='num'>{w:,.0f}</td><td class='num'>{pct(ip)}</td><td class='num'>{pct(loss_mean)}</td>"
                         f"<td class='num'>{fmt(pred)}{' (above the cap)' if pred and br and pred > br else ''}</td><td class='num'>{pct(s_['rate'])}</td></tr>")
        expl = []
        for a in lb:
            s_ = S[a]; ip = implied_p(a, s_["mean"], s_["rtt_mean"], mss)
            if not ip: continue
            if loss_mean:
                t_ = (f"For {LABEL[a]}, the implied loss-event rate ({pct(ip)}) is " + ("close to" if 0.5 < ip / loss_mean < 2 else f"{ip / loss_mean:,.0f}× " + ("higher than" if ip > loss_mean else "lower than"))
                      + f" the random loss its packets met ({pct(loss_mean)}). ")
            else:
                t_ = (f"No random loss was set, so {LABEL[a]}'s loss events were all drops at a full buffer or in the VMs: it hit one about every {1 / ip:,.0f} segments ({pct(ip)}), "
                      "which is what held its window to the size in the table. ")
            if s_["rate"] > 5 * ip and s_["episodes"] and expl:
                t_ += (f"Its retransmit rate, {pct(s_['rate'])}, is again far higher, for the same reason: {s_['episodes']} seconds with retransmits, about {fmt(s_['burst'])} segments in each.")
            elif s_["rate"] > 5 * ip and s_["episodes"]:
                t_ += (f"Its retransmit rate, {pct(s_['rate'])}, is far higher. The difference is burstiness: the model counts loss <i>events</i>, and an event that loses many segments in a row "
                       f"costs only one window cut. {LABEL[a]}'s retransmits came in {s_['episodes']} seconds, about {fmt(s_['burst'])} segments in each, so its throughput follows the number of loss events, not the number of lost packets.")
            else:
                t_ += "Its retransmit rate is of the same order, so its losses were mostly isolated, one segment at a time, as the model assumes."
            if a == "reno" and loss_mean and ip > 2 * loss_mean:
                t_ += (" Reno does worse than the model at the set loss because the model assumes each loss finds Reno back at full window. Here it needs "
                       f"{window_segs(s_['mean'], s_['rtt_mean'], mss) / 2:,.0f} round trips to regrow after a cut, and more losses arrive before it gets there.")
            expl.append(f"<p>{t_}</p>")
        html.append("<h3>Throughput against the loss models</h3>"
                    "<p>For a loss-based flow, the steady-state window depends on the <b>loss-event rate</b> p: how often a loss makes it back off. "
                    "Reno's window averages 1.22/√p segments (Mathis et al., 1997). CUBIC's averages about 1.05 · RTT<sup>¾</sup>/p<sup>¾</sup> (RFC 9438, RTT in seconds), "
                    "and never less than Reno's. The table turns this round: from each flow's measured throughput and RTT it works out the loss-event rate that would "
                    "produce it, and sets that beside the random loss that was configured and the retransmit rate that was measured.</p>" +
                    table("<table><tr><th>Algorithm</th><th class='num'>Mbit/s</th><th class='num'>Window (segments)</th><th class='num'>Loss-event rate implied</th><th class='num'>Random loss met</th><th class='num'>Model at that loss, Mbit/s</th><th class='num'>Retransmit rate</th></tr>"
                          + "".join(mrows) + "</table>", "Measured throughput and the loss models", "Window = throughput × mean RTT ÷ MSS. 'Random loss met': the loss that was set, averaged over the flow's packets when it changed during the run")
                    + "".join(expl) + "<p>Where a model's figure is above the sender's cap or the bottleneck, the flow is not loss-limited at that loss rate and the model does not apply.</p>")

    # ---- 4. queueing delay
    for a in algos:
        q_ = [s["rtt_ms"] - (state_at(s["ts"] - t0).get("rtt_ms") or 0) for s in per[a] if s.get("rtt_ms")]
        S[a]["qd"] = st.mean(q_) if q_ else None
    qrows = "".join(f"<tr><td>{sw(a)}</td><td class='num'>{fmt(S[a]['rtt_min'])}</td><td class='num'>{fmt(S[a]['rtt_mean'])}</td><td class='num'>{fmt(S[a]['rtt_p95'])}</td>"
                    f"<td class='num'>{fmt(S[a]['qd'], 2)}</td></tr>" for a in algos if S[a]["rtt_mean"])
    qtxt = ""
    buf_ms = (wan0.get("buffer_bdp") or 0) * rtt0
    if wans:
        q = [w.get("q_ms") or 0 for w in wans]; qm, qp, qx = st.mean(q), p95(q), max(q)
        bufs = [(state_at(w["ts"] - t0).get("buffer_bdp") or 0) * (state_at(w["ts"] - t0).get("rtt_ms") or 0) for w in wans]
        full = max(((w.get("q_ms") or 0) / b for w, b in zip(wans, bufs) if b), default=0)
        if len(set(bufs)) > 1: buf_ms = None
        qtxt = (f"<p>At the bottleneck itself, the queue held on average <b>{fmt(qm, 2)} ms</b> of data (95th percentile {fmt(qp, 2)} ms, peak {fmt(qx, 2)} ms), against a buffer of "
                + (f"{fmt(buf_ms)} ms ({wan0.get('buffer_bdp', 0):g} × BDP). " if buf_ms else f"{fmt(min(bufs))}–{fmt(max(bufs))} ms (buffer × RTT, both changed during the run). "))
        if qx < 1:
            qtxt += ("The queue stayed empty: the flows together never sent faster than the bottleneck rate, so the RTT they saw is the path's own and nothing was learned here about how each algorithm fills a buffer. "
                     + (f"With the bottleneck at {fmt(R)} Mbit/s and the flows at {fmt(total)} Mbit/s in total, the bottleneck was not the constraint." if R and total < 0.8 * R else "") + "</p>")
        elif full > 0.8:
            qtxt += ("The queue reached the top of the buffer: this is what a loss-based algorithm does, since it grows its window until a packet is dropped, "
                     "and the delay it adds (bufferbloat) is paid by every flow sharing the link.</p>")
        else:
            qtxt += ("The queue rose but did not fill the buffer: whichever algorithm drove it stopped short of the drop point, which is BBR's aim "
                     "(it caps what it has in flight at about twice the BDP) or the result of losses elsewhere cutting the loss-based flows first.</p>")
    qa = [a for a in algos if S[a]["qd"] is not None]
    hi = max(qa, key=lambda a: S[a]["qd"], default=None); lo = min(qa, key=lambda a: S[a]["qd"], default=None)
    html.append("<h3>Queueing delay</h3><p>Every millisecond a flow's RTT rises above the path's own RTT is time its packets waited in a queue, almost all of it the bottleneck's. "
                + (f"The path's RTT was set to {rtt0:g} ms. " if len({st_.get('rtt_ms') for st_ in states or [wan0]}) == 1 else "The path's RTT was changed during the run, so each second is compared with the RTT set at that time. ")
                + "A flow's queueing delay is its RTT minus the path's, averaged over its per-second samples.</p>" +
                table("<table><tr><th>Algorithm</th><th class='num'>RTT min (ms)</th><th class='num'>mean</th><th class='num'>95th pct</th><th class='num'>Queueing delay, mean</th></tr>" + qrows + "</table>",
                      "RTT and queueing delay per flow", "Smoothed RTT from TCP_INFO, per second") + qtxt
                + ("" if not hi or len(qa) < 2 else
                   f"<p>{LABEL[hi]} saw the most queueing delay ({fmt(S[hi]['qd'])} ms on average) and {LABEL[lo]} the least ({fmt(S[lo]['qd'])} ms). Flows through one queue wait about the same; differences come from when each one was sending, and from the RTT being smoothed per connection.</p>" if S[hi]["qd"] - S[lo]["qd"] >= 5 else
                   f"<p>The flows' queueing delays were alike (about {fmt(S[hi]['qd'])} ms): " + ("none of them built a standing queue.</p>" if S[hi]["qd"] < 2 else "they shared one queue, and every flow waited in it the same, whichever flow filled it. A loss-based flow pays the delay a more aggressive flow creates.</p>")))
    if wans and full > 0.8:
        find.append(f"<b>The bottleneck buffer filled</b>: its queue peaked at {fmt(max(w.get('q_ms') or 0 for w in wans))} ms, adding about {fmt(max(S[a]['qd'] or 0 for a in algos))} ms to the flows' RTT on average.")

    # ---- 5. dynamics
    drows = []
    for a in algos:
        s_ = S[a]; bo = s_["bo"]; rec = [b[3] for b in bo if b[3] is not None]
        gap = (s_["secs"] / len(bo)) if bo else None
        drows.append(f"<tr><td>{sw(a)}</td><td class='num'>{s_['rise'] if s_['rise'] else '–'} s</td><td class='num'>{100 * s_['cv']:.0f} %</td><td class='num'>{len(bo)}</td>"
                     f"<td class='num'>{fmt(gap) + ' s' if gap else '–'}</td><td class='num'>{fmt(st.median(rec)) + ' s' if rec else '–'}</td>"
                     f"<td class='num'>{fmt(s_['cwnd'], 2)} / {fmt(s_['need'], 2)}</td></tr>")
    dt = ["<p>The per-second figures show how each algorithm behaves over time, not just on average. <b>Start-up</b> is the time to first reach 90 % of the flow's typical "
          "high rate (its 95th percentile). <b>Variation</b> is the standard deviation of the per-second throughput as a share of its mean, after the first five seconds. "
          "A <b>back-off</b> is a second in which throughput fell below 70 % of the three seconds before; <b>recovery</b> is the time back to 90 % of that level. "
          "The <b>window</b> column sets the median congestion window beside the in-flight data the achieved rate actually needs (rate × RTT): a window far above the need means the window was not what held the flow back.</p>"]
    for a in algos:
        s_ = S[a]; bo = s_["bo"]; rec = [b[3] for b in bo if b[3] is not None]; rtt = s_["rtt_mean"] or rtt0 or 1
        w = window_segs(st.median([b[1] for b in bo]) if bo else s_["mean"], rtt, mss)
        if a == "reno":
            th = w / 2 * rtt / 1000; t90 = 0.4 * w * rtt / 1000
            dt.append(f"<p><b>Reno</b>: after a loss it halves its window and adds one segment per round trip, so regaining a window of about {w:,.0f} segments takes "
                      f"{w / 2:,.0f} round trips, about <b>{fmt(th)} s</b> at {fmt(rtt)} ms, and getting back to 90 % of it {fmt(t90)} s. "
                      + (f"Measured: {len(bo)} back-off{'s' if len(bo) != 1 else ''}, median recovery to 90 % {fmt(st.median(rec))} s. " if rec else f"Measured: {len(bo)} back-off{'s' if len(bo) != 1 else ''}. ")
                      + ("When losses come faster than that, it never gets back to full speed, which is the classic Reno result on long fat paths." if bo and s_["secs"] / len(bo) < th else "") + "</p>")
        elif a == "cubic":
            K = (w * 0.3 / 0.4) ** (1 / 3); t90 = max(0.0, K - (0.1 * w / 0.4) ** (1 / 3))
            dt.append(f"<p><b>CUBIC</b>: after a loss it cuts to 70 % and regrows on a cubic curve that reaches the old window after K = ∛(W<sub>max</sub>·0.3/0.4) ≈ <b>{fmt(K)} s</b> "
                      f"for W<sub>max</sub> ≈ {w:,.0f} segments, whatever the RTT. The curve is steepest just after the cut, so it is back to 90 % of W<sub>max</sub> after only {fmt(t90)} s and spends the rest of K "
                      "creeping up to W<sub>max</sub>, where the next loss is likely. " + (f"Measured: {len(bo)} back-off{'s' if len(bo) != 1 else ''}, median recovery to 90 % {fmt(st.median(rec))} s"
                      + (", close to the model. " if 0.5 * max(t90, 1) <= st.median(rec) <= 2 * max(t90, 1) else
                         ", slower than the model: losses during the regrowth restart it. " if st.median(rec) > 2 * max(t90, 1) else ", quicker than the model: many of the cuts were partial. ") if rec else f"Measured: {len(bo)} back-off{'s' if len(bo) != 1 else ''}. ")
                      + "Its throughput is a saw-tooth whose depth is the 30 % cut, shallower than Reno's 50 %.</p>")
        elif a == "bbr":
            dt.append("<p><b>BBR</b>: it does not react to individual losses. It paces at its estimate of the bottleneck bandwidth, probes 25 % above it for one round trip in eight, "
                      "and every 10 s drains its queue for about 200 ms (ProbeRTT) to re-measure the path's RTT. "
                      + (f"Its per-second variation was {100 * s_['cv']:.0f} %, " + ("flat: it held its rate through the losses that cut the loss-based flows." if s_["cv"] < 0.05 else "showing its probing and the ProbeRTT dips.")
                         ) + (lambda near: (f" It had {len(bo)} back-off{'s' if len(bo) != 1 else ''}"
                                             + (f", {len(near)} within 10 s after a change in the run (at {', '.join(f'{x:.0f}' for x in near)} s)" if near else "")
                                             + (", the rest most likely ProbeRTT." if len(near) < len(bo) else ".")) if bo else "")(
                             sorted({m_ for b in bo for m_ in marks_t if 0 <= (per[a][b[0]]["ts"] - t0) - m_ <= 10}))
                      + (f" Its window ({fmt(s_['cwnd'])} MB) was far above what its rate needs ({fmt(s_['need'])} MB). BBR sets its window to about twice its estimate of the BDP, and "
                         "with -b iperf3 sends in bursts at full speed, so BBR's bandwidth samples, and the window that follows them, are much higher than the average rate: the cap, not the window, set the pace."
                         if br and s_["cwnd"] and s_["need"] and s_["cwnd"] > 3 * s_["need"] else "") + "</p>")
    html.append("<h3>Dynamics: start-up, back-offs and recovery</h3>" + "".join(dt[:1]) +
                table("<table><tr><th>Algorithm</th><th class='num'>Start-up</th><th class='num'>Variation</th><th class='num'>Back-offs</th><th class='num'>One every</th><th class='num'>Median recovery</th><th class='num'>Window / need (MB)</th></tr>" + "".join(drows) + "</table>",
                      "Dynamics per flow", "From the per-second samples; window from TCP_INFO (snd_cwnd × MSS)") + "".join(dt[1:]))
    if c.get("cwnd_series"):
        html.append(figure(c["chart"](c["cwnd_series"], "MB", c["xmax"], mk), "Congestion window", "snd_cwnd × MSS per second, MB. A loss-based window traces its back-offs; BBR's is set from its own model, about twice its BDP estimate"))

    # ---- 6. phase by phase
    if len(phases) > 1:
        prow = []
        for k in range(1, len(phases)):
            a0, a1, nm, row = phases[k]; prev = phases[k - 1][3]; s0, s1 = states[k - 1] if k - 1 < len(states) else {}, states[k] if k < len(states) else {}
            ch = {key: (s0.get(key), s1.get(key)) for key in ("rtt_ms", "rate_mbit", "loss_pct", "buffer_bdp", "bitrate_mbit") if s1.get(key) is not None and s0.get(key) != s1.get(key)}
            if not ch and s1.get("algo") != s0.get("algo"): continue   # one at a time: the next algorithm's turn, not a change to compare
            why = []
            if "loss_pct" in ch:
                up = (ch["loss_pct"][1] or 0) > (ch["loss_pct"][0] or 0)
                why.append(("More random loss means more loss events: Reno's and CUBIC's windows shrink as 1/√p and 1/p<sup>¾</sup>, so they should slow down. BBR ignores isolated losses (below about 20 %) and should hold its rate."
                            if up else "Less random loss lets Reno's and CUBIC's windows grow larger before the next cut; CUBIC regrows within seconds, Reno only at one segment per round trip. BBR should be unaffected."))
            if "rate_mbit" in ch:
                up = (ch["rate_mbit"][1] or 0) > (ch["rate_mbit"][0] or 0)
                why.append(("A faster bottleneck is found by BBR within a few probing cycles (one in eight round trips probes 25 % higher). Loss-based flows only grow into it as their windows climb."
                            if up else "A slower bottleneck makes the queue build until the buffer overflows; the loss-based flows then cut their windows, and BBR's bandwidth estimate falls to the new rate within about ten round trips."))
            if "rtt_ms" in ch:
                up = (ch["rtt_ms"][1] or 0) > (ch["rtt_ms"][0] or 0)
                why.append(("A longer path raises the BDP: each flow needs a bigger window for the same rate. Reno's throughput for a given loss rate falls as 1/RTT. "
                            "BBR keeps its old, lower RTT in its 10-second minimum filter, underestimates the BDP and can run below its rate until the filter expires."
                            if up else "A shorter path lowers the BDP; every algorithm needs a smaller window for the same rate, and BBR adopts the lower RTT at once (its filter keeps the minimum)."))
            if "buffer_bdp" in ch:
                up = (ch["buffer_bdp"][1] or 0) > (ch["buffer_bdp"][0] or 0)
                why.append(("A deeper buffer gives loss-based flows more room before a drop: higher throughput when they are congestion-limited, but a longer queue (higher RTT) for everyone."
                            if up else "A shallower buffer drops sooner: loss-based flows cut their windows more often, while BBR, which keeps little in the queue, is barely affected."))
            if "bitrate_mbit" in ch:
                why.append("A new sender bit rate restarts the flows: each new connection begins in slow start, then settles under the new cap.")
            # what the design predicts for the mean, per class: -1 down, 0 about the same, +1 up, None no clear prediction
            exp = {"lb": None, "bbr": None}
            if len(ch) == 1:
                key, (v0, v1) = next(iter(ch.items())); up = (v1 or 0) > (v0 or 0)
                if key == "loss_pct": exp = {"lb": 1 if not up else -1, "bbr": 0}
                elif key == "rtt_ms": exp = {"lb": -1 if up else 1, "bbr": None}
                elif key == "rate_mbit" and not up and v1 < sum(prev[a][0] or 0 for a in algos): exp = {"lb": -1, "bbr": -1}
            resp, ok = [], []
            tot_prev = sum(prev[a][0] or 0 for a in algos)
            for a in algos:
                b_, a_ = prev[a][0], row[a][0]
                if b_ is None or a_ is None: continue
                xs = [s["mbps"] or 0 for s in per[a] if a0 < s["ts"] - c["t0"] <= a1]
                med = st.median(xs) if xs else None
                settle = next((i + 1 for i in range(len(xs)) if med and all(abs(x - med) <= 0.15 * max(med, 1) for x in xs[i:i + 3])), None)
                d = (a_ - b_) / max(b_, 1e-9)
                resp.append(f"{LABEL[a]} {fmt(b_)} → {fmt(a_)} Mbit/s (" + ("unchanged" if abs(d) < 0.01 else f"{'+' if d > 0 else '−'}{abs(100 * d):.0f} %") + (f", settled in about {settle} s" if settle else "") + ")")
                e = exp["bbr" if a == "bbr" else "lb"]
                if e is not None:
                    got = 0 if abs(d) < (0.10 if e == 0 else 0.05) else (1 if d > 0 else -1)   # "holds its rate": within 10 % (its retransmits come out of its throughput)
                    capped = br and e > 0 and b_ >= 0.95 * br
                    ok.append((LABEL[a], got == e or capped, mode != "solo" and n > 1 and tot_prev and (b_ or 0) / tot_prev < 0.5 / n))
            names = {"rtt_ms": "RTT", "rate_mbit": "bottleneck", "loss_pct": "random loss", "buffer_bdp": "buffer", "bitrate_mbit": "sender bit rate"}
            unit = {"rtt_ms": " ms", "rate_mbit": " Mbit/s", "loss_pct": " %", "buffer_bdp": " × BDP", "bitrate_mbit": " Mbit/s"}
            chg = ", ".join(f"{names[k_]} {(f'{v0:g}' if v0 else ('unlimited' if k_ == 'bitrate_mbit' else '0'))} → {(f'{v1:g}' if v1 else ('unlimited' if k_ == 'bitrate_mbit' else '0'))}{unit[k_] if v1 else ''}" for k_, (v0, v1) in ch.items()) or nm
            verdict = ""
            if ok:
                good = [n_ for n_, g, _ in ok if g]; bad = [n_ for n_, g, _ in ok if not g]; squeezed = [n_ for n_, g, q in ok if not g and q]
                verdict = ("<p><b>As predicted.</b></p>" if not bad else
                           f"<p><b>Not as predicted for {', '.join(bad)}</b>" + (f" (as predicted for {', '.join(good)})" if good else "") + ": "
                           + (f"{' and '.join(squeezed)} {'was' if len(squeezed) == 1 else 'were'} squeezed to a small share of the bottleneck before the change, so "
                              f"competition, not this setting, set {'its' if len(squeezed) == 1 else 'their'} rate; the change shifted the balance between the flows instead. " if squeezed else "")
                           + ("" if len(squeezed) == len(bad) else "another limit was tighter in this phase, or the phase was too short for the flow to settle. See the per-second figures.") + "</p>")
            prow.append(f"<div class='phase'><h4>At {a0:.0f} s: {E(chg)}</h4><p>{'; '.join(resp)}.</p><p>{' '.join(why)}</p>{verdict}</div>")
        if prow: html.append("<h3>Phase by phase</h3><p>Each change during the run is a small experiment: the same flows, before and after one setting moved. "
                    "For each change, the mean throughput of each flow before and after it, how long the new phase took to settle (three seconds in a row within 15 % of the phase's median), "
                    "and what the algorithms' design predicts.</p>" + "".join(prow))

    # ---- 7. sharing
    if mode in ("compete", "live") and n > 1 and total > 0:
        shares = {a: S[a]["mean"] / total for a in algos}
        jain = sum(S[a]["mean"] for a in algos) ** 2 / (n * sum(S[a]["mean"] ** 2 for a in algos)) if total else None
        lp = max(range(len(phases)), key=lambda k: phases[k][1] - phases[k][0]) if phases else 0
        bdp_txt = (states[lp] if lp < len(states) else wan0).get("buffer_bdp", 0) or 0
        bdp_mb = (wan0.get("rate_mbit") or 0) * 1e6 / 8 * ((states[lp] if lp < len(states) else wan0).get("rtt_ms") or 0) / 1000 / 1e6
        stx = ("<h3>Sharing</h3><p>When flows share a bottleneck, each algorithm's strategy shows in its share. " +
               ", ".join(f"{LABEL[a]} took {100 * shares[a]:.0f} %" for a in sorted(algos, key=lambda x: -shares[x])) +
               f" of the {fmt(total)} Mbit/s the flows moved together (Jain's index over the per-second means: {jain:.2f}; 1 = equal, {1 / n:.2f} = one flow takes all). ")
        if br and all(S[a]["mean"] >= 0.95 * br for a in algos):
            stx += "Every flow was held at its -b cap, so they did not really compete: the shares say nothing about the algorithms here.</p>"
        elif not (R and total >= 0.85 * R):
            stx += ("The flows did not fill the bottleneck, so they were not competing for it. The differences in share come from how each algorithm reacted to "
                    "the losses on the path, not from contention: the loss-based flows cut their rate on losses that BBR ignored.</p>")
        else:
            stx += (f"With a buffer of {bdp_txt:g} × BDP" + (" for most of the run" if len(phases) > 1 else "") + ", the outcome follows the known pattern: " +
                    ("in a shallow buffer (1 × BDP or less) BBR tends to take the larger share, because the loss-based flows overflow the buffer and cut their windows while BBR keeps sending at its estimate."
                     if bdp_txt <= 1 else "in a deep buffer the loss-based flows can fill the queue, and BBR, which caps what it has in flight at about twice its BDP estimate, holds a smaller share as the queue grows.")
                    + "</p>")
            big = max(algos, key=lambda a: shares[a]); small = [a for a in algos if shares[a] < 0.5 / n]
            if small and bdp_mb:
                stx += ("<p>The windows show how. The bottleneck's BDP was about " + f"{fmt(bdp_mb, 2)} MB. " + LABEL[big] + f"'s median window was {fmt(S[big]['cwnd'], 2)} MB; "
                        + ", ".join(f"{LABEL[a]}'s {fmt(S[a]['cwnd'], 2)} MB" for a in small)
                        + ". The squeezed flows kept losing packets at the full buffer, cut their windows each time, and never regrew them before the next loss: "
                        + "the room they gave up was filled at once by a flow that does not respond to loss.</p>")
            if "bbr" in algos and shares.get("bbr", 0) > 1.5 / n:
                stx += ("<p>This is a known property of BBR v1, the version in Linux 6.12: it ignores loss, so in a shallow buffer it can take most of the link and push the loss-based flows' "
                        "loss rate up to where they cannot grow. BBR v2 and v3 add a response to loss and to ECN, largely to fix this.</p>")
            find.append(f"<b>Sharing:</b> {LABEL[big]} took {100 * shares[big]:.0f} % of what the flows moved together" + (f"; {', '.join(LABEL[a] for a in small)} {'was' if len(small) == 1 else 'were'} squeezed below half an equal share." if small else "."))
        html.append(stx)
    return "".join(html), find


def sweep_section(c):
    """Chapter 4 for an RTT-sweep run: each algorithm's throughput and RTT at each step, a throughput-against-RTT figure, and what the
    trend means. The first SETTLE seconds of each step are left out (the flows are still adjusting to the new RTT)."""
    SETTLE = 3
    algos, per, t0, phases, states, rtts = c["algos"], c["per"], c["t0"], c["phases"], c["states"], c["rtts"]
    figure, table, chart, mss, br, wan0 = c["figure"], c["table"], c["chart"], c["mss"], c["br"], c["wan0"]
    acc = {(a, r): [] for a in algos for r in rtts}
    for a in algos:
        for s in per[a]:
            t = s["ts"] - t0
            k = next((k for k, (a0, a1, _, _) in enumerate(phases) if a0 < t <= a1), None)
            if k is None or k >= len(states): continue
            r = states[k].get("rtt_ms")
            if r in rtts and t - phases[k][0] > SETTLE: acc[(a, r)].append(s)
    def m(a, r, key):
        v = [x[key] for x in acc[(a, r)] if x.get(key) is not None]
        return st.mean(v) if v else None
    R = wan0.get("rate_mbit") or 0
    rows = []
    for r in rtts:
        bdp = R * 1e6 / 8 * r / 1000 if R else 0
        rows.append(f"<tr><td class='num'>{r:g} ms</td><td class='num'>{f"{fmt(bdp / 1e6, 2)} MB" if bdp else "≈ 0"}</td>"
                    + "".join(f"<td class='num'>{fmt(m(a, r, 'mbps'))}</td>" for a in algos)
                    + "".join(f"<td class='num'>{fmt(m(a, r, 'rtt_ms'))}</td>" for a in algos) + "</tr>")
    head = ("<tr><th class='num'>RTT set</th><th class='num'>BDP</th>" + "".join(f"<th class='num'>{sw(a)} Mbit/s</th>" for a in algos)
            + "".join(f"<th class='num'>{LABEL[a]} RTT (ms)</th>" for a in algos) + "</tr>")
    ser = [(a, LABEL[a], [(r, m(a, r, "mbps")) for r in rtts if m(a, r, "mbps") is not None]) for a in algos]
    out = ["<h3>Throughput across the RTT sweep</h3>"
           f"<p>The RTT stepped through {', '.join(f'{r:g}' for r in rtts)} ms" + (f", {c['step_s']} s at each" if c.get("step_s") else "")
           + (", within each algorithm's own turn, so each algorithm met every RTT alone." if c.get("seq") else ", with the flows running together throughout, so they shared the bottleneck at every RTT.")
           + f" The table gives each flow's mean over each step, leaving out its first {SETTLE} s while it adjusted. "
           "The BDP is the bottleneck rate times the RTT: the window a flow needs to fill the link at that RTT.</p>",
           table(f"<table>{head}{''.join(rows)}</table>", "Throughput and RTT at each step of the sweep", f"Means of the per-second samples, without each step's first {SETTLE} s"),
           figure(chart(ser, "Mbit/s", max(rtts) * 1.05 or 1, xunit=" ms", dots=True), "Throughput against RTT", "Mean throughput at each RTT step; x: the RTT set on the WAN")]
    txt = []
    lo = next((r for r in rtts if r > 0), rtts[0]); hi = rtts[-1]
    for a in algos:
        dips = []
        if a == "bbr":   # a step right after a shorter RTT, far below BBR's typical rate: its min-RTT filter still held the shorter RTT
            vals = [m(a, r, "mbps") for r in rtts]; known = [v for v in vals if v is not None]; med = st.median(known) if known else None
            dips = [r for i, r in enumerate(rtts) if i and med and vals[i] is not None and vals[i] < 0.5 * med and rtts[i - 1] < r]
        base = [r for r in rtts if r > 0 and r not in dips]
        lo = base[0] if base else next((r for r in rtts if r > 0), rtts[0])
        a_lo, a_hi = m(a, lo, "mbps"), m(a, hi, "mbps")
        if not a_lo or a_hi is None: continue
        if dips:
            d0 = dips[0]; prev = rtts[rtts.index(d0) - 1]; prtt = m(a, prev, "rtt_ms") or 1
            whole = c.get("step_s") and c["step_s"] <= 12
            txt.append(f"<p><b>BBR at {', '.join(f'{r:g}' for r in dips)} ms</b> reached only {', '.join(fmt(m(a, r, 'mbps')) for r in dips)} Mbit/s, far below its other steps. "
                       f"It came from the {prev:g} ms step with a minimum RTT of about {fmt(prtt)} ms in its 10-second filter, so it took the BDP to be about "
                       f"{max(1, d0 / max(prtt, 0.5)):,.0f}× smaller than it was and capped its window at twice that estimate. The filter lets go of the old minimum only after 10 s"
                       + (f", as long as the step itself ({c['step_s']} s): the whole step ran with the stale estimate. Steps of 30 s or more show its settled rate." if whole else
                          ", so the first part of the step ran with the stale estimate.") + " The trend below leaves this step out.</p>")
        ch = a_hi / a_lo
        trend = (f"held steady from {lo:g} to {hi:g} ms" if 0.85 <= ch <= 1.15 else (f"fell to {100 * ch:.0f} %" if ch < 1 else f"rose to {100 * ch:.0f} %") + f" of its {lo:g} ms level at {hi:g} ms") + f" ({fmt(a_lo)} → {fmt(a_hi)} Mbit/s)"
        capped = br and a_lo >= 0.95 * br and a_hi >= 0.95 * br
        if a == "reno":
            why = ("Reno grows its window by one segment per round trip, so at a longer RTT it regains a lost window more slowly in seconds, and at a fixed loss rate its throughput falls as 1/RTT (the Mathis model): "
                   f"going from {lo:g} to {hi:g} ms would cut a loss-limited Reno flow to {100 * lo / hi:.0f} %.")
        elif a == "cubic":
            why = ("CUBIC grows its window as a function of time, not round trips, so it loses much less than Reno at long RTTs: in its cubic region a loss-limited flow's throughput falls only as RTT<sup>−¼</sup> "
                   f"({100 * (lo / hi) ** 0.25:.0f} % from {lo:g} to {hi:g} ms). At short RTTs it runs in its Reno-friendly region and behaves like Reno.")
        else:
            why = ("BBR paces at its bandwidth estimate and sizes its window from its own BDP estimate, so in principle the RTT does not change its rate; it needs a window of about twice the BDP, "
                   "and each step to a longer RTT leaves it with a stale, too-low minimum RTT for up to 10 s, which can show as a dip at the start of the step.")
        txt.append(f"<p><b>{LABEL[a]}</b> {trend}" + (": it was held at the -b cap throughout, so the sweep shows nothing about its response to RTT. " if capped else ". ") + why + "</p>")
    if not c.get("seq") and len(algos) > 1:   # together: the shares, not the RTT alone, set the loss-based flows' numbers
        tot = {r: sum(m(a, r, "mbps") or 0 for a in algos) for r in rtts}
        dom = [(a, st.mean([(m(a, r, "mbps") or 0) / tot[r] for r in rtts if tot[r]])) for a in algos]
        big = max(dom, key=lambda x: x[1])
        if big[1] > 1.5 / len(algos):
            txt.insert(0, f"<p><b>Read the loss-based flows' numbers with care.</b> The flows shared the bottleneck, and {LABEL[big[0]]} took on average {100 * big[1]:.0f} % of what they moved together at each step. "
                          "The others' throughput at each RTT is therefore mostly what was left to them, not their own response to the RTT. To measure each algorithm's response alone, "
                          "run the sweep with <b>One at a time</b>.</p>")
    rmem = c.get("rmem_max")
    if rmem:
        ceil = rmem / 2 * 8 / (hi / 1000) / 1e6 if hi else None
        txt.append(f"<p>A flow's rate can never exceed its window divided by the RTT. The hosts allow a receive buffer of up to {rmem / 2 ** 20:.0f} MiB, of which roughly half is advertised as window, "
                   + (f"so at {hi:g} ms no flow can exceed about {fmt(ceil)} Mbit/s" + (", which is below the bottleneck: at the longest RTTs the receive window, not the algorithm, may be the limit." if R and ceil < R else ", above what these flows reached: the receive window was not the limit here.") if ceil else "")
                   + "</p>")
    txt.append(f"<p>The {rtts[0]:g} ms step is not a zero RTT: the virtual machines and their bridges add about 1 ms of their own, so it behaves like a LAN.</p>" if rtts[0] == 0 else "")
    return "".join(out) + "".join(txt)

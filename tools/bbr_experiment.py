#!/usr/bin/env python3
"""Controlled experiment: is BBR better than CUBIC and Reno on high-bandwidth, high-latency paths?

Only the congestion control differs between the three senders (a-cubic, a-bbr, a-reno: same image, kernel, sysctls,
MTU 1500, fq, path). The WAN is the treatment: bottleneck 500 Mbit/s, RTT 10 / 100 / 150 ms, and four path conditions —
clean (1 x BDP buffer), shallow buffer (0.25 x BDP), and random loss of 0.01 % and 0.1 % (1 x BDP). Each algorithm runs
alone, N times per cell, in a random order per trial (seeded), so drift in the host affects all three alike. Before and
after, every host's TCP sysctls are fingerprinted: the experiment fails if anything but tcp_congestion_control differs
between hosts or changes during the run. Nothing on the hosts is changed.

    bbr_experiment.py [--trials 5] [--duration 25] [--seed 1] [--bitrate 500M] [--out docs/data/bbr_experiment.json] [--quick]

Each flow is one TCP connection: iperf3 -c PEER -t DURATION -b BITRATE (one stream, no -P).

Results are written after every cell; a rerun with the same --out resumes at the first missing cell. The WAN's previous
settings are restored at the end, also on error."""
import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cclab as C  # noqa: E402

RATE = 500
RTTS = (10, 100, 150)
CONDITIONS = (   # (key, label, loss %, buffer x BDP)
    ("clean", "clean path, 1 × BDP buffer", 0, 1),
    ("shallow", "shallow buffer, 0.25 × BDP", 0, 0.25),
    ("loss001", "0.01 % random loss", 0.01, 1),
    ("loss01", "0.1 % random loss", 0.1, 1),
)
ALGOS = ("cubic", "bbr", "reno")
SYSCTL_CMD = "sysctl -a 2>/dev/null | grep -E '^net[.](ipv4[.]tcp_|core[.])' | grep -v -E 'tcp_congestion_control|_entries ='; cat /sys/class/net/eth1/mtu; tc qdisc show dev eth1 | cut -d' ' -f1-2,5-"


def fingerprint():
    """Hash of each host's TCP/core sysctls (minus the algorithm), eth1 MTU and qdisc kind, plus its algorithm."""
    out = {}
    for n in C.inventory()["nodes"]:
        if n["role"] != "host": continue
        rc, txt = C.run(n["name"], SYSCTL_CMD, timeout=20)
        rc2, cc = C.run(n["name"], "cat /proc/sys/net/ipv4/tcp_congestion_control", timeout=10)
        lines = sorted(l for l in txt.splitlines() if l.strip() and not l.startswith("net.core.netdev_rss_key"))
        out[n["name"]] = {"hash": hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16], "cc": cc.strip(), "lines": len(lines)}
    return out


def tcpext(host):
    k, v = (l.split() for l in C.run(host, "grep TcpExt: /proc/net/netstat")[1].splitlines()[:2])
    d = dict(zip(k, v))
    return {x: int(d.get(x, 0)) for x in ("TCPMemoryPressures", "TCPLossProbes", "TCPTimeouts")}


def one(h, duration, bitrate=None):
    before, ext0 = C.wan_stats()["a2b"], tcpext(h["name"])
    f = C.Flow(h, duration, time.time(), bitrate=bitrate); f.start(); f.join(duration + 60)
    after, ext1 = C.wan_stats()["a2b"], tcpext(h["name"])
    r = f.summary()
    mb = [s["mbps"] for s in f.samples]
    return {"cc": h["cc"], "cc_reported": r["cc_reported"], "mbps": r["mbps"], "retransmits": r["retransmits"],
            "rtt_mean_ms": r["rtt_ms"]["mean"], "rtt_min_ms": r["rtt_ms"]["min"], "rtt_max_ms": r["rtt_ms"]["max"],
            "steady_mbps": round(sum(mb[5:]) / len(mb[5:]), 2) if len(mb) > 5 else None,
            "bottleneck_drops": after["drops"] - before["drops"], "netem_drops": after["netem_drops"] - before["netem_drops"],
            **{k: ext1[k] - ext0[k] for k in ext0}, "error": r["error"], "per_second": mb}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=5); ap.add_argument("--duration", type=int, default=25)
    ap.add_argument("--seed", type=int, default=1); ap.add_argument("--out", default=str(C.LAB / "docs" / "data" / "bbr_experiment.json"))
    ap.add_argument("--bitrate", default="500M", help="iperf3 -b per flow (default 500M; 0 = unlimited)")
    ap.add_argument("--quick", action="store_true", help="smoke test: one cell, one trial, 8 s, to a scratch file")
    a = ap.parse_args()
    bitrate = None if a.bitrate in ("0", "") else a.bitrate
    rtts, conds = RTTS, CONDITIONS
    if a.quick: rtts, conds, a.trials, a.duration = (100,), CONDITIONS[2:3], 1, 8
    out = Path(a.out)
    res = json.loads(out.read_text()) if out.exists() else {}
    hs = {h["cc"]: h for h in C.senders("a2b", list(ALGOS))}
    fp = fingerprint()
    if len({v["hash"] for v in fp.values()}) != 1: raise SystemExit(f"hosts differ in more than the algorithm: {fp}")
    if C.mtu_state()["host_mtu"] != 1500: raise SystemExit("hosts must be at MTU 1500")
    if res and (res.get("design", {}).get("seed"), res.get("design", {}).get("iperf_bitrate")) != (a.seed, bitrate):
        raise SystemExit("existing results used another seed or bitrate; use another --out")
    res.setdefault("design", {"rate_mbit": RATE, "rtts": list(rtts), "conditions": [dict(zip(("key", "label", "loss_pct", "buffer_bdp"), c)) for c in conds],
                              "algos": list(ALGOS), "trials": a.trials, "duration_s": a.duration, "seed": a.seed, "iperf_bitrate": bitrate, "host_mtu": 1500, "direction": "a2b",
                              "senders": {cc: h["name"] for cc, h in hs.items()}})
    res.setdefault("fingerprint_before", fp); res.setdefault("cells", {})
    rng = random.Random(a.seed); prev = C.wan_stats()["settings"]; t_start = time.time()
    try:
        for rtt in rtts:
            for key, label, loss, buf in conds:
                orders = [rng.sample(ALGOS, len(ALGOS)) for _ in range(a.trials)]   # drawn even when the cell is skipped: resume keeps the same orders
                cid = f"{key}@{rtt}"
                if cid in res["cells"]: C.log(f"--- {cid}: done, skipped"); continue
                wan = C.wan_apply(rtt, RATE, loss, buf)["settings"]; time.sleep(1)
                ping = C.ping_rtt(hs["cubic"]["name"], hs["cubic"]["peer"])
                C.log(f"=== {cid}: {label}, RTT {rtt} ms (ping {ping['avg_ms']} ms), BDP {wan['bdp_bytes'] / 1e6:.2f} MB, queue {wan['queue_bytes'] / 1e6:.2f} MB")
                runs = []
                for t, order in enumerate(orders, 1):
                    for cc in order:
                        r = one(hs[cc], a.duration, bitrate); r["trial"] = t; runs.append(r)
                        C.log(f"  trial {t} {cc:>5} {r['mbps']:7.1f} Mbit/s  retr {r['retransmits']:6}  rtt {r['rtt_mean_ms']:6.1f} ms  memp {r['TCPMemoryPressures']}"
                              + (f"  ERROR {r['error']}" if r["error"] else ""))
                        time.sleep(2)
                res["cells"][cid] = {"rtt_ms": rtt, "condition": key, "label": label, "loss_pct": loss, "buffer_bdp": buf, "wan": wan, "ping": ping,
                                     "orders": orders, "runs": runs, "finished": time.time()}
                out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(res, indent=1))
    finally:
        C.wan_apply(prev["rtt_ms"], prev["rate_mbit"], prev["loss_pct"], prev["buffer_bdp"])
    res["fingerprint_after"] = fingerprint()
    res["unchanged"] = all(res["fingerprint_after"][h]["hash"] == res["fingerprint_before"][h]["hash"] for h in res["fingerprint_before"])
    res["elapsed_s"] = round(time.time() - t_start)
    out.write_text(json.dumps(res, indent=1))
    C.log(f"done: {len(res['cells'])} cells, host settings unchanged: {res['unchanged']}; WAN back to RTT {prev['rtt_ms']} ms, {prev['rate_mbit']} Mbit/s")


if __name__ == "__main__": main()

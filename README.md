# tcp-cc-lab

TCP congestion control across an emulated WAN: CUBIC, BBR and Reno side by side, with a slider for the round-trip time.

```
 a-cubic ┐                                                              ┌ b-cubic
 a-bbr   ┼ LAN A ─ eth2 r1 eth1 ── eth1 wanem eth2 ── eth1 r2 eth2 ─ LAN B ┼ b-bbr
 a-reno  ┘ 192.168.141/24        100.64.14.0/30 (bridged)      192.168.142/24 └ b-reno
```

| Node | What | OOB (tcpcc-oob 10.14.0.0/24) |
|---|---|---|
| r1, r2 | VyOS (overlays on `cat8000v-ipsec/images/vyos-base.qcow2`), static routes across the WAN | .11, .12 |
| wanem | Alpine, Linux bridge + `tc`: netem delay RTT/2 (+ loss) → tbf bottleneck, both directions | .20 |
| a-cubic / a-bbr / a-reno | Alpine hosts in site A, one algorithm each, iperf3 server | .31–.33 |
| b-cubic / b-bbr / b-reno | the same in site B (each host's peer is the same algorithm on the other side) | .41–.43 |

Libvirt/KVM (QEMU) like the other labs. MTU: hosts 1500 or 9000 (switchable), routers and wanem 9000 (always); 9 GiB RAM (1 GiB per VM: 6 hosts with 1 vCPU each, wanem, r1 and r2 with 2). Domains `tcpcc-<node>`, consoles 127.0.0.1:5601–5609, MACs
`52:54:00:ce:*`. Users: routers vyos / vyos, hosts and wanem lab / lab; your SSH key is installed on all of them.

## Quick start

```
./lab.sh image       # once: images/tcpcc-alpine.qcow2 (~2 min)
./lab.sh up          # first boot also pushes the routers' day-0 config over the console (~3 min)
./lab.sh verify      # algorithms, WAN settings, routes, RTT between every A/B pair
./lab.sh wan apply 100 100 0 1          # RTT 100 ms, 100 Mbit/s, 0 % loss, 1 x BDP buffer
./lab.sh flows compete --duration 30    # all three at once through the bottleneck
./lab.sh flows sweep                    # each algorithm alone at RTT 0/10/30/60/100/150 ms, bottleneck 500 Mbit/s
python3 tools/bbr_experiment.py --trials 1 --duration 30 --bitrate 500M   # the controlled BBR comparison (~21 min)
./lab.sh test        # Robot suites → results/latest
```

Portal: http://192.168.50.231:8112 (user unit `tcp-cc-lab-webapp`, Lab Hub sign-in). The RTT slider (0–300 ms,
default 30) applies when released, with `tc qdisc change`, so a running flow keeps going and the chart marks the change. The bottleneck rate slider goes from 10 Mbit/s to 5 Gbit/s.
The VMs forward about 2–2.5 Gbit/s at host MTU 1500 and 3.5–4 Gbit/s at 9000, so above that the path is the limit,
not the bottleneck; the bottleneck buffer is capped at 128 MB.

**Nodes: CPU and memory** (portal card): each node keeps one SSH session open to the portal, streaming its own
`/proc/stat` and `/proc/meminfo` once a second (the last 30 min are kept; `GET /api/nodestats`). There's a table with
CPU now, CPU averaged over 60 s, and memory in use, plus a CPU % chart and a memory MB chart. These charts have their
own window slider and a history slider under each chart. Untick a node to hide it.

**PDF report**: when a compete, solo or live run has finished, the Flows card's "⤓ PDF report" button downloads a
report of the run shown (`GET /api/runs/<id>/report.pdf`), styled as a white paper like the walkthrough. It opens with a
title page, contents and an executive summary with key findings, then:
1. background: what Reno, CUBIC and BBR are, worked out for the run's path;
2. the test environment;
3. the methodology, with every change made during the run;
4. the results, with per-second charts, a table for each phase between changes, and an analysis of what they mean:
   what limited each flow, where the losses came from (random loss, bottleneck overflow, the VMs' receive drops),
   throughput against the loss models, queueing delay, start-up / back-off / recovery against each algorithm's
   design, a phase-by-phase account of each change, and how the flows shared the link;
5. the system load: each node's CPU and memory during the run;
6. conclusions.

Appendix A has every device's configuration, collected live; appendix B has the run's log.

**Topology**: click any device (or a node in that table) to open `/config/<node>` in a new tab, with its configuration
collected live:
- hosts: system, interfaces, routes, the fq qdisc, congestion control, every TCP/socket sysctl, `/etc/sysctl.d`, iperf3;
- wanem: `wanem show`, its defaults, the bridge, offloads, the control script;
- r1 and r2: `show configuration commands` with the password hash redacted, interfaces, routes, version.

**Live test** (the portal's default flow mode): the chosen algorithms run at once until you press Stop (at most 2–30
min). Four charts update every second, with a time grid every 5 s: throughput,
the sender's smoothed RTT, retransmits per second, and the bottleneck queue at wanem. The **Window** slider sets the
window from 5 s to 300 s in 5 s steps, or the whole run. Each chart has its own horizontal slider underneath (5 s
steps) to slide that chart back through the run's history, during the test or after it; its "Live" button returns to now.
While it runs you can move:
- the WAN controls (RTT, rate, loss, buffer): applied in place, the flows keep going;
- the loss slider: stops from 0.0001 % (1 in a million) to 10 %, fine at the low end;
- the **sender bit rate** slider (`iperf3 -b` per flow, 1 Mbit/s to 1000 Mbit/s or unlimited). iperf3 can't change
  `-b` during a test, so the portal ends the flows (SIGINT, so iperf3 finishes cleanly) and starts new ones. Each one
  is a new TCP connection that starts in slow start.

Each change is marked on the charts. When the test ends, a table shows one row per flow and bit rate. API:
`POST /api/runs {"mode": "live", "options": {"algos", "bitrate_mbit", "max_s"}}`, then `POST /api/live/bitrate` and
`POST /api/live/stop`. Compete and solo runs also take `bitrate_mbit`.

## MTU

r1, r2 and wanem's data links are jumbo (9000), always, as are the four libvirt bridges under them. The hosts' eth1 is
1500 by default and switches to 9000 and back (all six together) with `./lab.sh mtu set 9000` or the portal's Host MTU
switch; the value is kept in `/etc/tcpcc-mtu` on each host and survives reboots. At 9000 TCP negotiates an MSS of 8948
and the bottleneck forwards ~9000-byte frames (tbf's burst is sized for two of them). Change it between runs: a flow
keeps the MSS it started with. OOB management stays at 1500.

## The WAN emulator

Defaults (lab.conf): RTT 30 ms, 100 Mbit/s, no loss, buffer 1 × BDP. The RTT is round trip: 15 ms is added each way.
Per egress port of wanem's bridge:

```
root 1:  netem delay RTT/2, loss L% (random), limit = 2 × packets in flight on the delay line + 1000
  └ 10:  tbf rate R, burst max(R/250 Hz, 2 MTU), limit = BUFFER × BDP bytes (min 20 packets)
```

Delay first, then the bottleneck: the queue a congestion control fills (and where it loses packets) is tbf's, and its size
in bytes stays put when the delay moves. `wanem stats` gives both directions' counters as JSON (eth2 = towards r2 =
site A → B). The VMs add about 0.9 ms of their own to the emulated RTT.

## Monitoring and the Lab Hub

- NMS: Prometheus job `tcp-cc-lab` scrapes the portal's `/metrics` (SD `http://10.0.0.1:8112/api/sd`, 15 s); alert group
  `tcp-cc-lab` (wanem unreachable, host down, wrong algorithm, no fq / iperf3, failed tests); Grafana dashboard
  **tcp-cc-lab: overview**. The config lives in `~/lab-portal/monitoring`.
- Fast resume (`"fast_resume": true` in labs.json): the hub's shutdown saves each VM's memory (`virsh managedsave`, about
  3.1 GB, 10 s); the next start resumes them and runs `lab.sh resumed`, which sets the clocks and verifies (35 s in all,
  against several minutes cold). `lab.sh up` boots cold if a saved image will not restore; `rebuild` / `clean` discard it.

## Walkthrough

`docs/tcp-cc-lab-walkthrough.pdf` (35 pages): congestion control for dummies, the three algorithms, the architecture, nine
walkthroughs with live measurements and charts, the portal, monitoring, tests and findings. Rebuild it with
`webapp/.venv/bin/python tools/build_walkthrough.py [--refresh]` (`--refresh` re-measures, ~12 min, and leaves the WAN at
its defaults and the hosts at 1500).

## Things that are not obvious

- **Bridges, not UDP tunnels.** The data links are isolated libvirt bridges. QEMU's UDP socket backend keeps the host's
  default 208 KB receive buffer and silently drops bursts (found in homa-lab); a loss nobody sees would skew exactly the
  thing this lab measures.
- **Offloads off on wanem** (GRO, LRO = virtio guest TSO, TSO, GSO): otherwise netem and tbf see 64 KB super-packets and
  the queue limit in packets means nothing. Bytes/packets through tbf ≈ 1512 confirms it.
- **netem's default limit (1000 packets) is too small** for a long fat pipe: it would drop like a random-loss link.
  wanem sizes it from the RTT and rate on every change.
- **Hosts:** `tcp_no_metrics_save=1` (a cached ssthresh from the previous run would start the next one), 32 MB buffers (a
  window cap would otherwise limit throughput at 300 ms, not the algorithm), fq on eth1 (BBR's pacing).
- **Alpine has doas, not sudo:** the seeds add `permit nopass lab`.
- **Measured at 0.1.0:** 1 × BDP at 30 ms: BBR ~80, CUBIC ~8, Reno ~6 Mbit/s when competing. ¼ BDP at 100 ms: BBR 84
  of 92. 4 × BDP at 30 ms, alone: CUBIC mean RTT 128 ms (fills the buffer), BBR 64 ms, Reno 84 ms.
- **Headless Chrome on this host** (156, updated 2026-10-19) hangs loading any http:// page, so the portal's look was
  checked from a file:// copy fed with recorded API answers.

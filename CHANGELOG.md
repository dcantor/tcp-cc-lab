# Changelog

All notable changes to this lab are documented here ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)).

## [0.2.1] - 2026-10-22

### Added
- Portal: a dark / light toggle in the header ("☾ dark" / "☀ light"), like the other lab portals: it follows the OS
  setting until chosen, and the choice is kept in the browser (`portal-theme`).

## [0.2.0] - 2026-10-22

### Added
- Monitoring (lab-portal 0.50.0): the portal's `/metrics` also serves each host's algorithm / fq / iperf3 state
  (`tcpcc_host_*`, polled in parallel, cached 20 s), `tcpcc_wanem_up`, the running flow's per-algorithm throughput, RTT
  and cwnd (`tcpcc_flow_*`), and the last compete run's shares and Jain index. Prometheus job `tcp-cc-lab`, alert group
  `tcp-cc-lab` (5 rules), dashboard **tcp-cc-lab: overview**.
- Lab Hub fast resume: `"fast_resume": true` in labs.json; `lab.sh resumed` (wait, set the clocks, verify). Measured:
  save 10 s (3.1 GB), resume to verified 35 s; the full suite passes after a resume.
- Git repository.

## [0.1.0] - 2026-10-22

### Added
- Two VyOS routers (r1, r2) joined through a Linux WAN emulator VM (wanem: br0 over eth1/eth2; each egress direction
  netem delay = RTT/2 and random loss, then a tbf bottleneck whose buffer is a multiple of the BDP).
- Three Alpine hosts per site, one per congestion control algorithm (CUBIC, BBR, Reno), with fq, 32 MB socket buffers,
  `tcp_no_metrics_save`, and an iperf3 server.
- `lab.sh` (up / down / status / wan / cc / verify / flows / test / webapp …), `tools/build_image.sh`, `tools/wanem.sh`,
  `tools/cclab.py` (compete / solo flows with per-second throughput, sender RTT, cwnd, retransmits; WAN control).
- Portal on port 8112: RTT slider (0–300 ms, default 30), bottleneck rate, loss and buffer controls applied in place,
  live bottleneck counters, compete / solo runs drawn live with WAN changes marked on the charts.
- Robot suites: platform (01), WAN emulator (02), congestion control behaviour (03) — 14 tests.

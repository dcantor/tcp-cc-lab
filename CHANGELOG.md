# Changelog

All notable changes to this lab are documented here ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)).

## [0.5.0] - 2026-10-23

### Added
- Portal live test (mode `live`, now the default): CUBIC, BBR and Reno at once, until stopped. Four live charts with a
  scrolling window: throughput, RTT, retransmits per second, and the bottleneck queue (wanem sampled every second).
  There is also a readout of the latest second.
- Sender bit-rate slider (`iperf3 -b` per flow, 1–1000 Mbit/s or unlimited). During a live test a change restarts the
  flows with the new `-b`; the run retries when the peer's iperf3 server is still closing the previous test. Compete
  and solo runs take `bitrate_mbit` too.
- `POST /api/live/bitrate` and `POST /api/live/stop`. Each live run ends with a table of its flows, one row per bit
  rate.

### Changed
- The loss slider has stops from 0.001 % to 10 % (it used to go 0–5 % in 0.1 % steps), so 0.01 % can be set. It also
  shows "1 in N".
- Each chart mark names only what changed (RTT, link rate, loss, buffer, `-b`). Its label is staggered and kept inside
  the chart. MARK lines in the run log are shown in plain words.

## [0.4.0] - 2026-10-23

### Added
- `cclab.py sweep` / `lab.sh flows sweep`: each algorithm alone, in turn, at RTT 0, 10, 30, 60, 100 and 150 ms with
  the bottleneck at 500 Mbit/s (`--rtts`, `--rate`, `--duration`, `--algos`); the WAN's previous settings are restored
  at the end, also on error.
- Robot suite `05_rtt_series`: one test per algorithm (CUBIC, BBR, Reno) over that series, 15 s per point; >= 80 % of
  the rate up to 60 ms, >= 40 % at 100 and 150 ms, the reported algorithm and the RTT floor checked at every point.
- Walkthrough J (section 16) in the PDF: the 500 Mbit/s series from a live run (throughput by RTT, the 150 ms ramp,
  queueing delay added, per-point table), run twice: hosts at MTU 1500 and at 9000, compared side by side.
  `build_walkthrough.py --sweep500` re-measures both series and the tcp_mem check. Findings 9-12.
- The hosts' TCP settings in the walkthrough: an annotated table of the settings that shape a bulk flow (read live from
  all six hosts, lab-set vs kernel default), the cloud-init files that set them, and Appendix D with every
  `net.ipv4.tcp_*` / `net.core` buffer value and eth1's fq qdisc.
- A tcp_mem check: the hosts' 217 MB of RAM sizes `tcp_mem` at 13.2 MB pressure / 19.8 MB high; one CUBIC flow at
  500 Mbit/s x 150 ms put its sender into memory pressure 95 times in 20 s (documented, not changed).

- `tools/bbr_experiment.py` and walkthrough K (section 17): a controlled single-flow comparison of CUBIC, BBR and Reno
  at 500 Mbit/s (RTT 10 / 100 / 150 ms x clean, shallow buffer, 0.01 % and 0.1 % random loss), one run per profile,
  `iperf3 -p 5201 -t 30 -b 500M`, random order per profile, host TCP settings fingerprinted before and after
  (identical apart from the algorithm, unchanged). Hypotheses stated up front; results, cost table, threats to validity.
  Findings 13-14: BBR 3-14x CUBIC at 0.01 % loss (gap grows with RTT), no faster on a clean path.
- iperf3 `-b`: `cclab.py compete|solo|sweep --bitrate` (sweep defaults to 500M), Robot `Run Flows ... bitrate=`.

### Changed
- The walkthrough's cover is a white-paper title page (author, date, abstract).
- Walkthrough J and `05_rtt_series` use one TCP flow with `iperf3 -t 30 -b 500M` per point (were 20 / 15 s, no -b).
- `lab.sh mtu set` deletes and re-adds fq on eth1, so fq's quantum follows the MTU (a host switched back from 9000
  kept an 18028-byte quantum with `tc qdisc replace`).

## [0.3.1] - 2026-10-23

### Added
- `docs/tcp-cc-lab-walkthrough.pdf` (35 pages): congestion control for dummies, Reno / CUBIC / BBR in five minutes, the
  architecture and build, walkthroughs (the lab comes up; each algorithm alone; all three at once; an RTT sweep; deep and
  shallow buffers; 1 % random loss; the RTT moved under a running flow; staggered starts; jumbo frames), the portal (light
  and dark), monitoring, tests, findings, lessons, troubleshooting. Built by `tools/build_walkthrough.py` from live data:
  `--refresh` (~12 min) re-measures everything into `docs/data/` and re-shoots the portal into `docs/img/`.

## [0.3.0] - 2026-10-22

### Added
- MTU: r1, r2 and wanem's data links are always 9000 (VyOS `mtu 9000` on eth1 / eth2; wanem eth1, eth2 and br0; the
  four libvirt data bridges and every data NIC's `<mtu size='9000'/>`). The hosts switch between 1500 and 9000, all six
  together: `lab.sh mtu show | set 1500|9000`, `cclab.py mtu`, the portal's "Host MTU" switch (`GET/POST /api/mtu`, not
  while a run goes), kept in /etc/tcpcc-mtu across reboots; the default is 1500. Flow results record the host MTU and
  each flow's MSS (1448 / 8948); the hosts table shows every node's MTU; `verify` checks the infrastructure MTU and
  sends an unfragmentable ping at the hosts' MTU between every pair.
- Metrics `tcpcc_host_mtu`, `tcpcc_link_mtu` (lab-portal 0.51.0: alerts `TcpCcInfraMtuNot9000`, `TcpCcHostMtuMixed`).
- Robot suite 04_mtu (5 tests): jumbo infrastructure and bridges, 1500 (1472 DF ok, 1473 refused, MSS 1448, ~1500-byte
  frames through the bottleneck), 9000 (8972 DF across the WAN, MSS 8948, ~9000-byte frames at the rate), all three
  algorithms at 9000, the MTU surviving a reboot. 19 tests in all.

### Changed
- Host image: iputils ping (busybox's cannot set Don't Fragment). Rebuilding the image needs the Alpine VMs' disks
  removed first (`lab.sh clean <nodes>`): they are overlays on it.
- wanem: tbf burst at least 2 jumbo frames (18028 B; a frame bigger than the burst is dropped) and its queue at least 4.
- `ensure_networks` redefines a data bridge without MTU 9000 (with the lab off).

### Fixed
- `tools/build_image.sh` reported success when it could not replace the old image (owned by libvirt).

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

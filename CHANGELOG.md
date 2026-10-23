# Changelog

All notable changes to this lab are documented here ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)).

## [0.9.0] - 2026-10-23

### Changed
- The PDF flow report is styled as a white paper, matching the walkthrough. It is US Letter, with a running header
  (title, run id) and footer (lab version, author, "Page n of N"). Headings and the cover are in Noto Serif.
  - **Cover:** the kicker, a serif title per mode, an italic summary of the setup, headline throughput, an abstract,
    and an author and date block with the run and software versions.
  - **Contents and executive summary**, with key findings worked out from the data: the leader and its ratios, sharing
    (compete), queueing delay above the path RTT, retransmits, throughput before and after each change, and system
    load.
  - **Numbered chapters:** 1 Background, 2 Test environment, 3 Methodology, 4 Results, 5 System load, 6 Conclusions
    (with caveats on reading a single run), then appendices A (device configurations) and B (run log).
  - **Chapter 1, "Background: Reno, CUBIC and BBR"** (`webapp/report_background.py`):
    - the problem congestion control solves: bottleneck, buffer, RTT, cwnd, BDP;
    - each algorithm's history and mechanism: Reno's slow start and AIMD; CUBIC's W(t) = C(t − K)³ + Wmax with β = 0.7;
      BBR's BtlBw / RTprop model, pacing and its Startup, Drain, ProbeBW and ProbeRTT phases (BBR v1 versus v2/v3);
    - a schematic of how each one's window evolves, and a side-by-side comparison table;
    - worked numbers for the run's own path: the BDP, Reno's regrowth time after one loss, CUBIC's K and BBR's Startup
      time;
    - for each loss rate used in the run, the Mathis estimate of Reno's throughput next to what Reno measured.
  - **Numbered figures and tables**, with captions.
  - **New table:** mean throughput and RTT in each phase of the run, between one change and the next.
  - The appendix flows rather than starting each device on a new page, so the report is shorter.

## [0.8.1] - 2026-10-23

### Changed
- The throughput chart scales to fit its data and the bottleneck line, up to 1 Gbit/s at most, on the portal and in
  the PDF report. A second faster than 1 Gbit/s is drawn at the top edge; the portal's tooltip and the tables still
  give the real value. A bottleneck above 1 Gbit/s isn't drawn as a line, and the chart's caption gives its rate
  instead.

## [0.8.0] - 2026-10-23

### Added
- "⤓ PDF report" button in the Flows card. Once the run shown has finished (compete, solo or live), it downloads a
  report of that run (`GET /api/runs/<id>/report.pdf`, built once per lab version and kept in `runs/`). It is laid out
  as a document:
  - **Title page:** the test and algorithms, the WAN, throughput per algorithm, a topology diagram, prepared by, date,
    run, lab version, contents.
  - **1. Summary:** a paragraph written from the run's own record.
  - **2. The lab:** topology, every node (role, algorithm, addresses, vCPU, RAM), how the WAN emulator builds the delay,
    loss and bottleneck, and the hosts' key TCP settings. It also states that all six hosts' 121 sysctls were checked
    identical apart from the algorithm.
  - **3. How this run was tested:** the method for that mode (including the live bit-rate restarts), how each figure is
    measured, the run parameters, and every change with its time.
  - **4. Results:** the table, plus per-second throughput, RTT, retransmits and (live) bottleneck queue, with the changes
    marked.
  - **5. Nodes during the run:** CPU and memory per node.
  - **Appendix A. Device configurations:** each device's configuration, collected live over SSH when the report is made.
    The full sysctl list is printed once, since the hosts are verified identical. VyOS password hashes are redacted.
  - **Appendix B. Run log:** the run's events, with changes in words.

  It is built as HTML with SVG charts in `webapp/report.py` and printed by headless Chrome from a `file://` page.
- Each flow run now keeps the nodes' CPU and memory samples for its span (`node_stats` in the run record), so its
  report still has them after a portal restart.

### Changed
- The device pages: the iperf3 section lists only the iperf3 processes, and a router's version and system details are
  one section.

### Fixed
- The WAN card and wanem's script still said wanem has 512 MB of RAM (it has had 1 GB since 0.7.0).

## [0.7.0] - 2026-10-23

### Changed
- Every host and wanem now has 1 GiB of RAM (it was 256 MiB for the hosts and 512 MiB for wanem; lab.conf
  `RAM_MIB`, and the defined domains changed with `virsh setmaxmem/setmem --config` and restarted). vCPUs are
  unchanged. The kernel sizes `tcp_mem` from the RAM, so the hosts' TCP memory limit rose by itself: pressure is now
  at about 58 MB (it was 13 MB) and the hard limit at about 87 MB (it was 20 MB). Every other host setting is the same
  (checked: sysctls, fq, MTU, iperf3; `lab.sh verify` 9/9). The PDF's results were measured with the old 256 MiB
  hosts.

### Added
- Portal card "Nodes: CPU and memory". One SSH session per node streams `/proc/stat` and `/proc/meminfo` each second,
  kept for 30 min (`GET /api/nodestats`). The card has a table (vCPU, CPU now, 60 s average, memory in use and RAM)
  and two live charts: CPU % and memory in use. The charts have their own window slider and a history slider each,
  labelled in clock time. Untick a node to hide it.
- Clicking a device in the topology (or a node in that table) opens `/config/<node>` in a new tab: its configuration,
  collected live over SSH. VyOS password hashes are redacted.
- Line charts drop the labels at the ends of the lines when there are more than 4 series (the legend or table names
  them). Dashed lines mark site B hosts and r2.

## [0.6.1] - 2026-10-23

### Changed
- The loss slider goes one decimal place lower: it starts at 0.0001 % (1 in 1,000,000), then 0.0002 %, 0.0005 %,
  0.001 % and up to 10 %. netem takes it as `loss random 0.0001%`.

## [0.6.0] - 2026-10-23

### Changed
- The bottleneck rate goes up to 5 Gbit/s (it was 1 Gbit/s) and is now a slider: 18 stops, 10 Mbit/s to 5 Gbit/s,
  applied on release. The sender bit-rate slider also goes to 5 Gbit/s (then unlimited). Buffer and queue sizes are
  shown in MB once they pass 1 MB.
- wanem caps the bottleneck buffer at 128 MB (the VM has 512 MB; 16 x BDP at 5 Gbit/s and 300 ms would be 3 GB).

### Measured
- At 5 Gbit/s and 10 ms RTT the VMs, not the shaper, are the limit (tbf dropped nothing): one flow 1.5–2.3 Gbit/s
  and three at once 2.5 Gbit/s at host MTU 1500; 3.9–4.1 Gbit/s and 3.5 Gbit/s at MTU 9000.

## [0.5.1] - 2026-10-23

### Changed
- The flow charts have a time grid every 5 s, with a label at each line. Windows over about 60 s get coarser ticks.
- The window is now a slider, 5 s to 300 s in 5 s steps, or the whole run (it was 60 s / 3 min / whole run).

### Added
- A horizontal slider under each chart (throughput, RTT, retransmits, queue), moving in 5 s steps, to slide that chart
  back through the run's history on its own, during a live test or after any run. Sliding back stops that chart
  following live; dragging to the right end or pressing its "Live" button resumes. The readout above the charts shows
  the newest second.

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

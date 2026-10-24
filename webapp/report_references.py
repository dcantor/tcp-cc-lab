"""Appendix C of the flow report: the lab's repository, and the references the report cites (numbered, linked).
cite() adds [n] markers to the text of the other chapters; every DOI and URL here was checked to resolve."""
import re
from html import escape as E

REPO = "https://github.com/dcantor/tcp-cc-lab"

# (key, group, citation HTML, url): the list's order is the numbering
REFS = [
    # foundations
    ("jacobson88", "Foundations of TCP congestion control", "V. Jacobson, “Congestion Avoidance and Control,” <i>Proc. ACM SIGCOMM ’88</i>, pp. 314–329, 1988.", "https://doi.org/10.1145/52324.52356"),
    ("rfc5681", "Foundations of TCP congestion control", "M. Allman, V. Paxson and E. Blanton, “TCP Congestion Control,” RFC 5681, IETF, 2009.", "https://www.rfc-editor.org/rfc/rfc5681"),
    ("rfc6582", "Foundations of TCP congestion control", "T. Henderson, S. Floyd, A. Gurtov and Y. Nishida, “The NewReno Modification to TCP’s Fast Recovery Algorithm,” RFC 6582, IETF, 2012.", "https://www.rfc-editor.org/rfc/rfc6582"),
    ("rfc6928", "Foundations of TCP congestion control", "J. Chu, N. Dukkipati, Y. Cheng and M. Mathis, “Increasing TCP’s Initial Window,” RFC 6928, IETF, 2013.", "https://www.rfc-editor.org/rfc/rfc6928"),
    ("rfc2018", "Foundations of TCP congestion control", "M. Mathis, J. Mahdavi, S. Floyd and A. Romanow, “TCP Selective Acknowledgment Options,” RFC 2018, IETF, 1996.", "https://www.rfc-editor.org/rfc/rfc2018"),
    ("rfc8985", "Foundations of TCP congestion control", "Y. Cheng, N. Cardwell, N. Dukkipati and P. Jha, “The RACK-TLP Loss Detection Algorithm for TCP,” RFC 8985, IETF, 2021.", "https://www.rfc-editor.org/rfc/rfc8985"),
    ("rfc6298", "Foundations of TCP congestion control", "V. Paxson, M. Allman, J. Chu and M. Sargent, “Computing TCP’s Retransmission Timer,” RFC 6298, IETF, 2011.", "https://www.rfc-editor.org/rfc/rfc6298"),
    ("rfc9293", "Foundations of TCP congestion control", "W. Eddy (ed.), “Transmission Control Protocol (TCP),” RFC 9293, IETF, 2022.", "https://www.rfc-editor.org/rfc/rfc9293"),
    # CUBIC
    ("ha08", "CUBIC", "S. Ha, I. Rhee and L. Xu, “CUBIC: A New TCP-Friendly High-Speed TCP Variant,” <i>ACM SIGOPS Operating Systems Review</i>, 42(5), pp. 64–74, 2008.", "https://doi.org/10.1145/1400097.1400105"),
    ("rfc9438", "CUBIC", "L. Xu, S. Ha, I. Rhee, V. Goel and L. Eggert (ed.), “CUBIC for Fast and Long-Distance Networks,” RFC 9438, IETF, 2023.", "https://www.rfc-editor.org/rfc/rfc9438"),
    ("rfc8312", "CUBIC", "I. Rhee, L. Xu, S. Ha, A. Zimmermann, L. Eggert and R. Scheffenegger, “CUBIC for Fast Long-Distance Networks,” RFC 8312, IETF, 2018 (obsoleted by RFC 9438).", "https://www.rfc-editor.org/rfc/rfc8312"),
    ("linuxcubic", "CUBIC", "The Linux kernel’s CUBIC implementation, <span class='mono'>net/ipv4/tcp_cubic.c</span>.", "https://github.com/torvalds/linux/blob/master/net/ipv4/tcp_cubic.c"),
    # BBR
    ("queue16", "BBR", "N. Cardwell, Y. Cheng, C. S. Gunn, S. Hassas Yeganeh and V. Jacobson, “BBR: Congestion-Based Congestion Control,” <i>ACM Queue</i>, 14(5), 2016.", "https://doi.org/10.1145/3012426.3022184"),
    ("cacm17", "BBR", "N. Cardwell, Y. Cheng, C. S. Gunn, S. Hassas Yeganeh and V. Jacobson, “BBR: Congestion-Based Congestion Control,” <i>Communications of the ACM</i>, 60(2), pp. 58–66, 2017.", "https://doi.org/10.1145/3009824"),
    ("bbrdraft", "BBR", "N. Cardwell et al., “BBR Congestion Control,” Internet-Draft draft-ietf-ccwg-bbr, IETF Congestion Control Working Group (BBRv3; work in progress).", "https://datatracker.ietf.org/doc/draft-ietf-ccwg-bbr/"),
    ("googlebbr", "BBR", "Google, BBR project page: papers, talks and the BBRv3 source.", "https://github.com/google/bbr"),
    ("linuxbbr", "BBR", "The Linux kernel’s BBR (v1) implementation, <span class='mono'>net/ipv4/tcp_bbr.c</span>.", "https://github.com/torvalds/linux/blob/master/net/ipv4/tcp_bbr.c"),
    ("hock17", "BBR", "M. Hock, R. Bless and M. Zitterbart, “Experimental Evaluation of BBR Congestion Control,” <i>Proc. IEEE ICNP</i>, 2017.", "https://doi.org/10.1109/ICNP.2017.8117540"),
    ("ware19", "BBR", "R. Ware, M. K. Mukerjee, S. Seshan and J. Sherry, “Modeling BBR’s Interactions with Loss-Based Congestion Control,” <i>Proc. ACM IMC</i>, 2019.", "https://doi.org/10.1145/3355369.3355604"),
    ("cao19", "BBR", "Y. Cao, A. Jain, K. Sharma, A. Balasubramanian and A. Gandhi, “When to Use and When Not to Use BBR: An Empirical Analysis and Evaluation Study,” <i>Proc. ACM IMC</i>, 2019.", "https://doi.org/10.1145/3355369.3355579"),
    # models, fairness, buffers
    ("mathis97", "Throughput models, fairness and buffers", "M. Mathis, J. Semke, J. Mahdavi and T. Ott, “The Macroscopic Behavior of the TCP Congestion Avoidance Algorithm,” <i>ACM SIGCOMM Computer Communication Review</i>, 27(3), 1997.", "https://doi.org/10.1145/263932.264023"),
    ("padhye98", "Throughput models, fairness and buffers", "J. Padhye, V. Firoiu, D. Towsley and J. Kurose, “Modeling TCP Throughput: A Simple Model and its Empirical Validation,” <i>Proc. ACM SIGCOMM ’98</i>, 1998.", "https://doi.org/10.1145/285237.285291"),
    ("jain84", "Throughput models, fairness and buffers", "R. Jain, D.-M. Chiu and W. Hawe, “A Quantitative Measure of Fairness and Discrimination for Resource Allocation in Shared Computer Systems,” DEC Research Report TR-301, 1984.", "https://arxiv.org/abs/cs/9809099"),
    ("appenzeller04", "Throughput models, fairness and buffers", "G. Appenzeller, I. Keslassy and N. McKeown, “Sizing Router Buffers,” <i>Proc. ACM SIGCOMM</i>, 2004.", "https://doi.org/10.1145/1015467.1015499"),
    ("gettys11", "Throughput models, fairness and buffers", "J. Gettys and K. Nichols, “Bufferbloat: Dark Buffers in the Internet,” <i>ACM Queue</i>, 9(11), 2011.", "https://doi.org/10.1145/2063166.2071893"),
    ("rfc7567", "Throughput models, fairness and buffers", "F. Baker and G. Fairhurst (eds.), “IETF Recommendations Regarding Active Queue Management,” RFC 7567, IETF, 2015.", "https://www.rfc-editor.org/rfc/rfc7567"),
    ("bufferbloatnet", "Throughput models, fairness and buffers", "The Bufferbloat project: background, measurements and AQM work.", "https://www.bufferbloat.net/"),
    # tools
    ("netem", "Tools and platforms used in this lab", "<span class='mono'>tc-netem(8)</span>: network emulator (delay, loss), Linux manual page.", "https://man7.org/linux/man-pages/man8/tc-netem.8.html"),
    ("tbf", "Tools and platforms used in this lab", "<span class='mono'>tc-tbf(8)</span>: token bucket filter (the bottleneck), Linux manual page.", "https://man7.org/linux/man-pages/man8/tc-tbf.8.html"),
    ("fq", "Tools and platforms used in this lab", "<span class='mono'>tc-fq(8)</span>: fair queue packet scheduler with pacing (on the hosts), Linux manual page.", "https://man7.org/linux/man-pages/man8/tc-fq.8.html"),
    ("sysctl", "Tools and platforms used in this lab", "Linux kernel documentation, “IP Sysctl”: the TCP settings in table 3 and appendix A.", "https://docs.kernel.org/networking/ip-sysctl.html"),
    ("iperf3", "Tools and platforms used in this lab", "ESnet, iperf3: the traffic generator and per-second TCP_INFO reports.", "https://software.es.net/iperf/"),
    ("vyos", "Tools and platforms used in this lab", "VyOS documentation: the routers r1 and r2.", "https://docs.vyos.io/"),
    ("libvirt", "Tools and platforms used in this lab", "libvirt: the KVM virtual machines and isolated networks.", "https://libvirt.org/"),
    ("alpine", "Tools and platforms used in this lab", "Alpine Linux: the hosts and the WAN emulator.", "https://alpinelinux.org/"),
]
NUM = {k: i + 1 for i, (k, *_) in enumerate(REFS)}


def cite(*keys):
    return "&nbsp;<span class='cite'>[" + ", ".join(f"<a href='#ref-{NUM[k]}'>{NUM[k]}</a>" for k in keys) + "]</span>"


# where the other chapters name a source: (text as it appears, keys); each is marked once, at its first appearance
CITES = [
    ("Van Jacobson's 1988 congestion-avoidance work (TCP Tahoe)", ("jacobson88",)),
    ("standardised in RFC 5681", ("rfc5681",)),
    ("(Ha, Rhee and Xu, 2008; RFC 9438)", ("ha08", "rfc9438")),
    ("(Cardwell et al., Google, 2016)", ("queue16", "cacm17")),
    ("BBRv2 and BBRv3 add a response to loss", ("bbrdraft", "googlebbr")),
    ("(Mathis et al., 1997)", ("mathis97",)),
    ("(RFC 9438, RTT in seconds)", ("rfc9438",)),
    ("(bufferbloat)", ("gettys11",)),
    ("Jain's fairness index", ("jain84",)),
    ("Jain's index over the per-second means", ("jain84",)),
    ("the outcome follows the known pattern", ("hock17", "ware19", "cao19")),
    ("BBR v2 and v3 add a response to loss and to ECN", ("bbrdraft",)),
    ("Linux 6.12 ships BBR v1", ("linuxbbr",)),
]


def add_citations(html):
    for text, keys in CITES:
        i = html.find(text)
        if i >= 0: html = html[:i + len(text)] + cite(*keys) + html[i + len(text):]
    return html


def appendix(version):
    tag = f"{REPO}/tree/v{version}"
    lab = (f"<h3>C.1 This lab</h3><p>Everything used to build the lab, run the tests and write this report is open source:</p>"
           f"<table class='kv'><tr><th style='width:24%'>Repository</th><td><a href='{REPO}'>{E(REPO)}</a></td></tr>"
           f"<tr><th>This version</th><td><a href='{tag}'>{E(tag)}</a> (tcp-cc-lab {E(version)}, which made this report)</td></tr>"
           f"<tr><th>Setup and use</th><td><a href='{REPO}#readme'>README</a>: requirements, <span class='mono'>./lab.sh up</span>, the portal on port 8112</td></tr>"
           f"<tr><th>Changes</th><td><a href='{REPO}/blob/main/CHANGELOG.md'>CHANGELOG.md</a></td></tr>"
           f"<tr><th>Report source</th><td><a href='{REPO}/tree/main/webapp'>webapp/</a>: <span class='mono'>report.py</span>, <span class='mono'>report_background.py</span>, "
           "<span class='mono'>report_analysis.py</span>, <span class='mono'>report_references.py</span></td></tr></table>"
           "<p class='note'>To repeat this run: build the lab from the repository, set the WAN as in table “Run parameters” (section 3), and start a flow run "
           "with the same mode, algorithms, direction and bit rate from the portal or its API.</p>")
    refs, group = ["<h3>C.2 References</h3><p class='note'>Numbers in square brackets in the text refer to this list. Links were checked when this version was released.</p>"], None
    for k, g, text, url in REFS:
        if g != group:
            if group: refs.append("</ol>")
            refs.append(f"<h4>{E(g)}</h4><ol class='refs' start='{NUM[k]}'>"); group = g
        shown = re.sub(r"^https?://", "", url)
        refs.append(f"<li id='ref-{NUM[k]}'>{text} <a href='{url}'>{E(shown)}</a></li>")
    refs.append("</ol>")
    return lab + "".join(refs)

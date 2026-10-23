*** Settings ***
Documentation     The build: every VM answers, each host runs the congestion control its name says (with fq and large buffers),
...               the routers route the two sites across the WAN, and wanem is a two-port bridge whose qdiscs see wire-size packets.
Resource          ../resources/common.resource
Suite Setup       Lab Setup

*** Test Cases ***
Every node answers on its management address
    ${nodes}=    Node Names
    Length Should Be    ${nodes}    9
    FOR    ${n}    IN    @{nodes}
        Run On    ${n}    true
    END

Each host runs the algorithm its name says, with BBR available everywhere
    FOR    ${h}    IN    @{HOSTS}
        ${info}=    Node Info    ${h}
        ${cc}=    Run On    ${h}    sysctl -n net.ipv4.tcp_congestion_control
        Should Be Equal    ${cc}    ${info}[cc]    msg=${h} runs ${cc}
        ${avail}=    Run On    ${h}    sysctl -n net.ipv4.tcp_available_congestion_control
        Should Contain    ${avail}    bbr
    END

Hosts pace with fq, keep no cached metrics, and have buffers for 300 ms at 100 Mbit/s
    FOR    ${h}    IN    @{HOSTS}
        ${q}=    Run On    ${h}    tc qdisc show dev eth1 | head -1
        Should Start With    ${q}    qdisc fq
        ${nm}=    Run On    ${h}    sysctl -n net.ipv4.tcp_no_metrics_save
        Should Be Equal    ${nm}    1
        ${wmem}=    Run On    ${h}    sysctl -n net.ipv4.tcp_wmem | awk '{print $3}'
        Should Be True    ${wmem} >= 3750000    msg=${h}: tcp_wmem max ${wmem} < one BDP at 300 ms x 100 Mbit/s
        Run On    ${h}    pgrep -x iperf3
    END

The routers route each site's LAN across the WAN
    ${r1}=    Run On    r1    ip route show 192.168.142.0/24
    Should Contain    ${r1}    via 100.64.14.2 dev eth1
    ${r2}=    Run On    r2    ip route show 192.168.141.0/24
    Should Contain    ${r2}    via 100.64.14.1 dev eth1
    ${ttl}=    Run On    a-cubic    traceroute -n -w 2 -q 1 192.168.142.11 2>/dev/null | tail -n +2 | awk '{print $2}' | tr '\\n' ' '
    Should Be Equal    ${ttl.strip()}    192.168.141.1 100.64.14.2 192.168.142.11

wanem bridges r1 and r2 with offloads off, so its queues count real packets
    ${br}=    Run On    wanem    ip -br link show master br0 | awk '{print $1}' | sort | tr '\\n' ' '
    Should Be Equal    ${br.strip()}    eth1 eth2
    ${off}=    Run On    wanem    for d in eth1 eth2; do ethtool -k $d | grep -E '^(generic-receive|tcp-segmentation)-offload'; done
    Should Not Contain    ${off}    : on

*** Settings ***
Documentation     MTU: r1, r2 and wanem's links (and the libvirt bridges under them) are jumbo, always; the hosts switch between
...               1500 and 9000 together. At 1500 nothing bigger crosses unfragmented; at 9000 an 8972-byte ping crosses
...               the WAN, TCP negotiates an MSS of ~8948 and the bottleneck forwards jumbo frames at its rate; the choice
...               survives a host's reboot.
Resource          ../resources/common.resource
Suite Setup       Lab Setup
Suite Teardown    Mtu Set    1500

*** Test Cases ***
The routers' and wanem's links and the data bridges are always MTU 9000
    ${m}=    Mtu State
    Should Be True    ${m}[infra_ok]    msg=r1 / r2 / wanem links: ${m}[infra]
    FOR    ${br}    IN    virbr-tccwana    virbr-tccwanb    virbr-tcclana    virbr-tcclanb
        ${v}=    Bridge Mtu    ${br}
        Should Be Equal As Integers    ${v}    9000    msg=host bridge ${br}
    END

At 1500 the largest unfragmentable packet is 1500 bytes, and TCP uses a 1448-byte MSS
    Mtu Set    1500
    Run On    a-cubic    ping -c1 -W2 -M do -s 1472 192.168.142.11
    ${big}=    Run On    a-cubic    ping -c1 -W2 -M do -s 1473 192.168.142.11 2>&1    expect_rc=any
    Should Match Regexp    ${big}    (?i)message too (large|long)
    ${r}    ${bpp}=    Bottleneck Bytes Per Packet
    ${f}=    Flow Of    ${r}    cubic
    Should Be Equal As Integers    ${f}[mss]    1448
    Should Be Within    ${bpp}    1400    1514    bytes per packet through the bottleneck at MTU 1500
    Should Be Within    ${f}[mbps]    80    102    CUBIC alone through 100 Mbit/s at MTU 1500

At 9000 jumbo frames cross the WAN end to end and through the bottleneck
    Mtu Set    9000
    ${m}=    Mtu State
    Should Be Equal As Integers    ${m}[host_mtu]    9000
    FOR    ${a}    IN    a-cubic    a-bbr    a-reno
        ${info}=    Node Info    ${a}
        ${peer}=    Node Info    ${info}[peer]
        Run On    ${a}    ping -c2 -W2 -M do -s 8972 ${peer}[ip]
    END
    ${r}    ${bpp}=    Bottleneck Bytes Per Packet
    ${f}=    Flow Of    ${r}    cubic
    Should Be Equal As Integers    ${f}[mss]    8948
    Should Be True    ${bpp} > 8000    msg=only ${bpp} bytes per packet through the bottleneck: jumbo frames are split or not used
    Should Be Within    ${f}[mbps]    80    102    CUBIC alone through 100 Mbit/s at MTU 9000

All three algorithms run at MTU 9000
    Mtu Set    9000
    ${r}=    Run Flows    compete    15
    FOR    ${f}    IN    @{r}[flows]
        Should Be Equal    ${f}[cc_reported]    ${f}[cc]
        Should Be Equal As Integers    ${f}[mss]    8948
    END
    Should Be Within    ${r}[total_mbps]    85    101    total through 100 Mbit/s at MTU 9000

The hosts' MTU survives a reboot
    Mtu Set    9000
    Run On    b-reno    doas reboot    expect_rc=any
    Sleep    5s
    Wait Until Keyword Succeeds    120s    5s    Run On    b-reno    pgrep -x iperf3
    ${v}=    Run On    b-reno    cat /sys/class/net/eth1/mtu
    Should Be Equal    ${v}    9000
    ${cc}=    Run On    b-reno    sysctl -n net.ipv4.tcp_congestion_control
    Should Be Equal    ${cc}    reno

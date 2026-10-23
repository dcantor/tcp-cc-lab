*** Settings ***
Documentation     The WAN emulator does what the slider says: the RTT across it follows the setting (plus the VMs' own ~1 ms),
...               the bottleneck caps a flow at its rate, loss is injected, and a change made while a flow runs takes effect
...               at once without breaking the flow.
Resource          ../resources/common.resource
Suite Setup       Lab Setup
Suite Teardown    Wan Reset

*** Test Cases ***
The emulator starts at the lab defaults: 30 ms RTT, 100 Mbit/s, no loss, 1 x BDP
    ${s}=    Wan Stats
    Should Be Equal As Numbers    ${s}[settings][rtt_ms]    30
    Should Be Equal As Numbers    ${s}[settings][rate_mbit]    100
    Should Be Equal As Numbers    ${s}[settings][loss_pct]    0
    Should Be Equal As Numbers    ${s}[settings][buffer_bdp]    1
    Should Be Equal As Numbers    ${s}[settings][one_way_us]    15000

The measured RTT follows the setting from 0 to 300 ms
    FOR    ${rtt}    IN    0    10    30    100    200    300
        Wan Set    rtt_ms=${rtt}
        ${p}=    Ping Rtt    a-reno    b-bbr    10
        ${hi}=    Evaluate    ${rtt} * 1.02 + 3
        Should Be Within    ${p}[avg_ms]    ${rtt}    ${hi}    RTT at a ${rtt} ms setting
    END
    [Teardown]    Wan Set    rtt_ms=30

The bottleneck caps a single flow at its rate
    FOR    ${rate}    IN    20    100
        Wan Set    rate_mbit=${rate}
        ${r}=    Run Flows    solo    10    a2b    cubic
        ${f}=    Flow Of    ${r}    cubic
        ${lo}=    Evaluate    ${rate} * 0.8
        ${hi}=    Evaluate    ${rate} * 1.02
        Should Be Within    ${f}[mbps]    ${lo}    ${hi}    CUBIC alone through a ${rate} Mbit/s bottleneck
    END
    [Teardown]    Wan Set    rate_mbit=100

Random loss is injected in each direction
    Wan Set    loss_pct=2
    ${l}=    Ping Loss    a-cubic    b-cubic    400    0.02
    Should Be Within    ${l}    1    9    ping loss % with 2 % each way (~4 % round trip expected)
    Wan Set    loss_pct=0
    ${l0}=    Ping Loss    a-cubic    b-cubic    200    0.02
    Should Be Equal As Numbers    ${l0}    0
    [Teardown]    Wan Set    loss_pct=0

Moving the RTT during a flow takes effect at once and the flow survives
    ${r}=    Flow With Rtt Change    cubic    20    6    120
    Should Be Equal    ${r}[summary][error]    ${None}
    Should Be True    ${r}[summary][mbps] > 10
    ${before}=    Evaluate    [s['rtt_ms'] for s in $r['samples'] if 1 <= s['t'] <= 5]
    ${after}=    Evaluate    [s['rtt_ms'] for s in $r['samples'] if s['t'] >= 10]
    Should Be True    max(${before}) < 100    msg=sender RTT before the change: ${before}
    Should Be True    min(${after}) >= 118    msg=sender RTT after the change to 120 ms: ${after}
    [Teardown]    Wan Reset

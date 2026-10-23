*** Settings ***
Documentation     Each algorithm on its own, through a 500 Mbit/s bottleneck, at RTT 0, 10, 30, 60, 100 and 150 ms (one TCP flow,
...               iperf3 -t 30 -b 500M, per point): the host really runs that algorithm, the path RTT follows the setting, and the flow uses the link.
...               Up to 60 ms every algorithm should get >= 80 % of the rate; at 100 and 150 ms the slow-start ramp (several
...               seconds at 150 ms) and Reno's 1-MSS-per-RTT recovery after a loss make the floor 40 %.
Resource          ../resources/common.resource
Suite Setup       Series Setup
Suite Teardown    Wan Reset

*** Variables ***
${RATE}           500
${DURATION}       30
${BITRATE}        500M
@{RTTS}           0    10    30    60    100    150

*** Test Cases ***
CUBIC alone at 500 Mbit/s, RTT 0 to 150 ms
    RTT Series    cubic

BBR alone at 500 Mbit/s, RTT 0 to 150 ms
    RTT Series    bbr

Reno alone at 500 Mbit/s, RTT 0 to 150 ms
    RTT Series    reno

*** Keywords ***
Series Setup
    Lab Setup
    Wan Set    rate_mbit=${RATE}

RTT Series
    [Arguments]    ${cc}
    FOR    ${rtt}    IN    @{RTTS}
        Wan Set    rtt_ms=${rtt}    rate_mbit=${RATE}
        ${r}=    Run Flows    solo    ${DURATION}    a2b    ${cc}    bitrate=${BITRATE}
        ${f}=    Flow Of    ${r}    ${cc}
        Should Be Equal    ${f}[cc_reported]    ${cc}    msg=a-${cc} reported ${f}[cc_reported]
        Should Be Equal As Numbers    ${r}[wan][rate_mbit]    ${RATE}
        ${lo}=    Evaluate    ${RATE} * (0.8 if ${rtt} <= 60 else 0.4)
        Should Be Within    ${f}[mbps]    ${lo}    ${RATE}    ${cc} alone at RTT ${rtt} ms through ${RATE} Mbit/s
        Should Be True    ${f}[rtt_ms][min] >= ${rtt}    msg=${cc}: smallest RTT ${f}[rtt_ms][min] ms below the ${rtt} ms setting
        Log    ${cc} RTT ${rtt} ms: ${f}[mbps] Mbit/s, ${f}[retransmits] retransmits, mean RTT ${f}[rtt_ms][mean] ms    console=True
    END
    [Teardown]    Wan Set    rtt_ms=30    rate_mbit=${RATE}

*** Settings ***
Documentation     The congestion control algorithms behave as each is known to: the kernel really uses each host's algorithm
...               for its flow, competing flows fill the bottleneck, BBR takes most of a shallow-buffered long link from the
...               loss-based ones, and in a deep buffer CUBIC fills the queue (high RTT) while BBR keeps it short.
Resource          ../resources/common.resource
Suite Setup       Lab Setup
Suite Teardown    Wan Reset

*** Test Cases ***
Each flow runs the sender's algorithm and together they fill the bottleneck
    ${r}=    Run Flows    compete    20
    FOR    ${f}    IN    @{r}[flows]
        Should Be Equal    ${f}[error]    ${None}
        Should Be Equal    ${f}[cc_reported]    ${f}[cc]    msg=${f}[host]: iperf3 says the kernel used ${f}[cc_reported]
        Should Be True    ${f}[mbps] > 0.5    msg=${f}[cc] got nothing
    END
    Should Be Within    ${r}[total_mbps]    85    101    total through 100 Mbit/s

The site B hosts send with their algorithms too
    ${r}=    Run Flows    compete    10    b2a
    FOR    ${f}    IN    @{r}[flows]
        Should Be Equal    ${f}[cc_reported]    ${f}[cc]
    END

BBR takes most of a long, shallow-buffered link from CUBIC and Reno
    Wan Set    rtt_ms=100    buffer_bdp=0.25
    ${r}=    Run Flows    compete    30
    ${bbr}=    Flow Of    ${r}    bbr
    ${cubic}=    Flow Of    ${r}    cubic
    ${reno}=    Flow Of    ${r}    reno
    Should Be True    ${bbr}[mbps] > 2 * (${cubic}[mbps] + ${reno}[mbps])    msg=BBR ${bbr}[mbps], CUBIC ${cubic}[mbps], Reno ${reno}[mbps]
    [Teardown]    Wan Reset

In a deep buffer CUBIC fills the queue while BBR keeps it short
    Wan Set    rtt_ms=30    buffer_bdp=4
    ${r}=    Run Flows    solo    15    a2b    cubic,bbr
    ${cubic}=    Flow Of    ${r}    cubic
    ${bbr}=    Flow Of    ${r}    bbr
    Should Be True    ${cubic}[rtt_ms][mean] > 75    msg=CUBIC mean RTT ${cubic}[rtt_ms][mean] ms over a 30 ms path with 120 ms of buffer
    Should Be True    ${bbr}[rtt_ms][mean] < 0.7 * ${cubic}[rtt_ms][mean]    msg=BBR ${bbr}[rtt_ms][mean] ms vs CUBIC ${cubic}[rtt_ms][mean] ms
    Should Be True    ${cubic}[mbps] > 85 and ${bbr}[mbps] > 85    msg=both should still fill the link alone
    [Teardown]    Wan Reset

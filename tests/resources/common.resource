*** Settings ***
Library           Collections
Library           TcpCc.py

*** Keywords ***
Lab Setup
    ${hosts}=    Node Names    host
    Set Global Variable    @{HOSTS}    @{hosts}
    Wan Reset

Should Be Within
    [Arguments]    ${value}    ${low}    ${high}    ${what}
    Should Be True    ${low} <= ${value} <= ${high}    msg=${what}: ${value} not within ${low} .. ${high}

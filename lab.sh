#!/usr/bin/env bash
# tcp-cc-lab controller (libvirt/KVM): two VyOS routers across an emulated WAN (wanem: netem delay + tbf bottleneck), three
# Alpine hosts per site with CUBIC / BBR / Reno, see lab.conf
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lab.conf"

# Re-exec under the libvirt group if this login session doesn't have it yet.
if ! id -nG | tr ' ' '\n' | grep -qx libvirt && getent group libvirt | grep -qw "${USER:-$(id -un)}"; then
  exec sg libvirt -c "$(printf '%q ' "$0" "$@")"
fi

V() { virsh -q -c "$LIBVIRT_URI" "$@"; }
die() { echo "error: $*" >&2; exit 1; }
dom() { echo "tcpcc-$1"; }                      # libvirt domain names are host-global (other labs have an r1)
node_dir() { echo "$LAB_DIR/nodes/$1"; }
defined() { V dominfo "$(dom "$1")" &>/dev/null; }
running() { [[ "$(V domstate "$(dom "$1")" 2>/dev/null)" == "running" ]]; }
known() { [[ -n "${ROLE[$1]:-}" ]] || die "unknown node $1 (nodes: ${ALL_NODES[*]})"; }
nodes_or_all() { local n; if [[ $# -gt 0 ]]; then for n in "$@"; do known "$n"; done; echo "$*"; else echo "${ALL_NODES[*]}"; fi; }
routers_of() { local n out=(); for n in "$@"; do [[ "${ROLE[$n]}" == router ]] && out+=("$n"); done; echo "${out[*]:-}"; }
mac() { printf '%s:%02x:%02x' "$MAC_OUI" "${NODE_IDX[$1]}" "$2"; }
user_of() { [[ "${ROLE[$1]}" == router ]] && echo vyos || echo lab; }
PUBKEY="$(cat "${TCPCC_LAB_PUBKEY:-$HOME/.ssh/id_ed25519.pub}" 2>/dev/null || true)"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5)
S() { local n="$1"; shift; ssh "${SSH_OPTS[@]}" "$(user_of "$n")@${MGMT_IP[$n]}" "$@"; }   # run a command on a node (key auth)
any_running() { local n; for n in "${ALL_NODES[@]}"; do running "$n" && return 0; done; return 1; }
ssh_ready() { timeout 8 bash -c "exec 3<>/dev/tcp/${MGMT_IP[$1]}/22" 2>/dev/null; }

ensure_networks() {   # the OOB network and the four data segments (networks/*.xml); a data segment defined before the jumbo
  # MTU (0.3.0) is redefined, which needs the lab's VMs off (their tap devices take the bridge's MTU when they start)
  local n; for n in "${DATA_NETS[@]}"; do
    V net-info "$n" &>/dev/null || continue
    V net-dumpxml "$n" | grep -q "<mtu size='$INFRA_MTU'/>" && continue
    any_running && die "network $n is not MTU $INFRA_MTU yet: stop the lab (lab.sh down) and run lab.sh up again"
    echo "[$n] redefining with MTU $INFRA_MTU"
    V net-destroy "$n" &>/dev/null || true; V net-undefine "$n" >/dev/null
  done
  for n in "${NETS[@]}"; do
    V net-info "$n" &>/dev/null || { V net-define "$LAB_DIR/networks/$n.xml" >/dev/null; V net-autostart "$n" >/dev/null; }
    [[ "$(V net-info "$n" | awk '/Active/{print $2}')" == "yes" ]] || V net-start "$n" >/dev/null
  done
}

nics() {   # node -> "nic network comment" lines (eth0 is always the OOB network)
  local n="$1"
  echo "0 $OOB_NET OOB ${MGMT_IP[$n]}"
  case "${ROLE[$n]}" in
    router) echo "1 ${WAN_NET[$n]} WAN ${WAN_IP[$n]}/$WAN_PREFIX_LEN via wanem"
            echo "2 ${LAN_NET[${SITE[$n]}]} LAN ${DATA_IP[$n]} (site ${SITE[$n]})" ;;
    wanem)  echo "1 tcpcc-wan-a bridge port towards r1"
            echo "2 tcpcc-wan-b bridge port towards r2" ;;
    host)   echo "1 ${LAN_NET[${SITE[$n]}]} LAN ${DATA_IP[$n]} (site ${SITE[$n]}, ${CC[$n]})" ;;
  esac
}

# ---- XML ------------------------------------------------------------------------------------------------------------
domain_xml() {
  local n="$1" d i net c; d="$(node_dir "$n")"
  cat <<X
<domain type='kvm'>
  <name>$(dom "$n")</name>
  <uuid>$(uuidgen --sha1 --namespace @dns --name "tcp-cc-lab.${MGMT_IP[$n]}")</uuid>
  <title>tcp-cc-lab ${ROLE[$n]} $n</title>
  <memory unit='MiB'>${RAM_MIB[$n]}</memory>
  <vcpu placement='static'>${VCPU[$n]}</vcpu>
  <cpu mode='host-passthrough' check='none'/>
  <os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='$d/disk.qcow2'/><target dev='vda' bus='virtio'/></disk>
X
  [[ "${ROLE[$n]}" == router ]] || echo "    <disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='$d/seed.iso'/><target dev='hda' bus='ide'/><readonly/></disk>"
  while read -r i net c; do
    cat <<X
    <!-- eth$i: $c -->
    <interface type='network'>
      <mac address='$(mac "$n" "$i")'/>
      <source network='$net'/>
      <model type='virtio'/>$([[ "$i" != 0 ]] && printf "\n      <mtu size='%s'/>" "$INFRA_MTU")
      <address type='pci' domain='0x0000' bus='0x00' slot='$(printf '0x%02x' $((3 + i)))' function='0x0'/>
    </interface>
X
  done < <(nics "$n")
  cat <<X
    <serial type='tcp'>
      <source mode='bind' host='127.0.0.1' service='${CONSOLE_PORT[$n]}'/>
      <protocol type='raw'/>
      <log file='$d/console.log' append='on'/>
      <target port='0'/>
    </serial>
    <memballoon model='none'/>
  </devices>
</domain>
X
}

# ---- VyOS day-0 ---------------------------------------------------------------------------------------------------------
vyos_config() {   # r1 | r2 -> set commands (pushed over the serial console at the first boot, over SSH by "configure")
  local n="$1" s o; s="${SITE[$n]}"; o="$([[ "$n" == r1 ]] && echo r2 || echo r1)"
  cat <<C
# $n: site $s router — rendered by lab.sh from lab.conf; day-0 over the serial console, "lab.sh configure" re-applies it
set system host-name $n
set system domain-name tcpcc.lab
set system login user vyos authentication plaintext-password vyos
set system login user vyos authentication public-keys lab key $(awk '{print $2}' <<<"$PUBKEY")
set system login user vyos authentication public-keys lab type $(awk '{print $1}' <<<"$PUBKEY")
set service ssh port 22
set interfaces ethernet eth0 address ${MGMT_IP[$n]}/24
set interfaces ethernet eth0 description 'OOB management'
set protocols static route 10.0.0.0/8 next-hop $OOB_GATEWAY
set interfaces ethernet eth1 address ${WAN_IP[$n]}/$WAN_PREFIX_LEN
set interfaces ethernet eth1 description 'WAN to $o via wanem'
set interfaces ethernet eth1 mtu $INFRA_MTU
set interfaces ethernet eth2 address ${DATA_IP[$n]}/${LAN_PREFIX[$s]#*/}
set interfaces ethernet eth2 description 'LAN site $s'
set interfaces ethernet eth2 mtu $INFRA_MTU
set protocols static route ${LAN_PREFIX[${SITE[$o]}]} next-hop ${WAN_IP[$o]}
C
}

# ---- cloud-init seeds (hosts and wanem) -------------------------------------------------------------------------------
user_head() {
  cat <<U
#cloud-config
hostname: $1
users:
  - name: lab
    plain_text_passwd: lab
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/sh
    ssh_authorized_keys: ["$PUBKEY"]
ssh_pwauth: true
U
}

etc_hosts() {
  local h; echo "127.0.0.1 localhost $1"
  for h in "${HOSTS[@]}" r1 r2; do echo "${DATA_IP[$h]} $h"; done
  for h in "${ALL_NODES[@]}"; do echo "${MGMT_IP[$h]} $h-oob"; done
}

oob_netcfg() {
  cat <<U
version: 2
ethernets:
  oob:
    match: { macaddress: "$(mac "$1" 0)" }
    set-name: eth0
    addresses: [${MGMT_IP[$1]}/24]
    routes: [{ to: 10.0.0.0/8, via: $OOB_GATEWAY }]
U
}

host_seed() {
  local n="$1" d s gw kv; d="$(node_dir "$n")"; s="${SITE[$n]}"; gw="${DATA_IP[$([[ "$s" == a ]] && echo r1 || echo r2)]}"
  { oob_netcfg "$n"; cat <<U
  lan:
    match: { macaddress: "$(mac "$n" 1)" }
    set-name: eth1
    addresses: [${DATA_IP[$n]}/${LAN_PREFIX[$s]#*/}]
    routes: [{ to: 0.0.0.0/0, via: $gw }]
U
  } > "$d/network-config"
  { user_head "$n"; cat <<U
write_files:
  - path: /etc/doas.d/lab.conf
    content: "permit nopass lab\n"
  - path: /etc/hosts
    content: |
$(etc_hosts "$n" | sed 's/^/      /')
  - path: /etc/tcpcc-mtu
    content: "$HOST_MTU\n"
  - path: /etc/modules-load.d/tcpcc.conf
    content: "tcp_bbr\nsch_fq\n"
  - path: /etc/sysctl.d/90-tcpcc.conf
    content: |
      net.ipv4.tcp_congestion_control = ${CC[$n]}
$(for kv in "${HOST_SYSCTL[@]}"; do echo "      ${kv%%=*} = ${kv#*=}"; done)
  - path: /etc/local.d/tcpcc.start
    permissions: "0755"
    content: |
      #!/bin/sh
      # tcp-cc-lab host: the algorithm and buffers (sysctl), the MTU (lab.sh mtu), fq on the data NIC, the iperf3 server
      modprobe tcp_bbr; modprobe sch_fq
      sysctl -q -p /etc/sysctl.d/90-tcpcc.conf
      ip link set eth1 mtu "\$(cat /etc/tcpcc-mtu 2>/dev/null || echo $HOST_MTU)"
      tc qdisc replace dev eth1 root fq
      pgrep -x iperf3 >/dev/null || iperf3 -s -D -p 5201 --logfile /var/log/iperf3.log
  - path: /etc/motd
    content: "$n: site $s host, TCP congestion control ${CC[$n]}. eth1 ${DATA_IP[$n]} (gateway $gw), OOB eth0 ${MGMT_IP[$n]}. iperf3 server on 5201; peer ${PEER[$n]} ${DATA_IP[${PEER[$n]}]}.\n"
runcmd:
  - rc-update add sshd default
  - rc-update add local default
  - rc-service sshd restart
  - [sh, /etc/local.d/tcpcc.start]
U
  } > "$d/user-data"
}

wanem_seed() {
  local n="$1" d; d="$(node_dir "$n")"
  { oob_netcfg "$n"; } > "$d/network-config"
  { user_head "$n"; cat <<U
write_files:
  - path: /etc/doas.d/lab.conf
    content: "permit nopass lab\n"
  - path: /etc/hosts
    content: |
$(etc_hosts "$n" | sed 's/^/      /')
  - path: /usr/local/bin/wanem
    permissions: "0755"
    encoding: b64
    content: $(base64 -w0 "$LAB_DIR/tools/wanem.sh")
  - path: /etc/wanem.conf
    content: |
      # defaults ("wanem reset", and every boot) — from lab.conf
      RTT_MS=$WAN_RTT_MS
      RATE_MBIT=$WAN_RATE_MBIT
      LOSS_PCT=$WAN_LOSS_PCT
      BUFFER_BDP=$WAN_BUFFER_BDP
  - path: /etc/modules-load.d/wanem.conf
    content: "sch_netem\nsch_tbf\nbridge\n"
  - path: /etc/local.d/wanem.start
    permissions: "0755"
    content: |
      #!/bin/sh
      # wanem: bridge eth1 (r1) and eth2 (r2), then the default impairment
      modprobe sch_netem; modprobe sch_tbf; modprobe bridge
      ip link show br0 >/dev/null 2>&1 || ip link add br0 type bridge stp_state 0 forward_delay 0
      for d in eth1 eth2; do ip link set "\$d" mtu $INFRA_MTU; ip link set "\$d" master br0; ip link set "\$d" up; done
      ip link set br0 mtu $INFRA_MTU up
      /usr/local/bin/wanem reset >/var/log/wanem.log 2>&1
  - path: /etc/motd
    content: "wanem: WAN emulator. br0 = eth1 (r1) + eth2 (r2); each egress: netem delay RTT/2 (+ loss) -> tbf bottleneck. doas wanem show | stats | apply RTT_MS RATE_MBIT LOSS_PCT BUFFER_BDP | reset\n"
runcmd:
  - rc-update add sshd default
  - rc-update add local default
  - rc-service sshd restart
  - [sh, /etc/local.d/wanem.start]
U
  } > "$d/user-data"
}

seed_iso() {
  local d; d="$(node_dir "$1")"
  printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$1" "$(cat "$d/user-data" "$d/network-config" | md5sum | cut -c1-8)" "$1" > "$d/meta-data"
  genisoimage -quiet -o "$d/seed.iso.tmp" -V cidata -J -r "$d/user-data" "$d/meta-data" "$d/network-config" && mv -f "$d/seed.iso.tmp" "$d/seed.iso"
}

build() {
  local n="$1" d base; d="$(node_dir "$n")"
  [[ -n "$PUBKEY" ]] || die "no SSH public key (~/.ssh/id_ed25519.pub or TCPCC_LAB_PUBKEY)"
  if [[ "${ROLE[$n]}" == router ]]; then base="$VYOS_IMAGE"; else base="$BASE_IMAGE"; fi
  [[ -f "$base" ]] || die "base image missing: $base$([[ "$base" == "$BASE_IMAGE" ]] && echo ' (lab.sh image)')"
  mkdir -p "$d"
  [[ -f "$d/disk.qcow2" ]] || { echo "[$n] creating overlay disk on $(basename "$base")"; qemu-img create -q -f qcow2 -b "$base" -F qcow2 "$d/disk.qcow2"; }
  case "${ROLE[$n]}" in
    router) vyos_config "$n" > "$d/vyos_config.txt" ;;
    host)   host_seed "$n"; seed_iso "$n" ;;
    wanem)  wanem_seed "$n"; seed_iso "$n" ;;
  esac
  domain_xml "$n" > "$d/domain.xml"
  V managedsave-remove "$(dom "$n")" &>/dev/null || true
  V define "$d/domain.xml" >/dev/null
}

bootstrap_vyos() {   # VyOS day-0 over the serial console (nodes/<n>/vyos_config.txt), logged to nodes/<n>/bootstrap.log
  local n="$1" d; d="$(node_dir "$n")"
  {
    echo "[$n] waiting for the VyOS login prompt..."
    python3 "$LAB_DIR/tools/vyos_console.py" wait 127.0.0.1 "${CONSOLE_PORT[$n]}" 600
    echo "[$n] applying day-0 config"
    python3 "$LAB_DIR/tools/vyos_console.py" push 127.0.0.1 "${CONSOLE_PORT[$n]}" "$d/vyos_config.txt"
    for _ in $(seq 30); do ssh_ready "$n" && break; sleep 5; done
    if ssh_ready "$n" && S "$n" true; then echo "[$n] ready: ssh vyos@${MGMT_IP[$n]}"; touch "$d/.bootstrapped"; else echo "[$n] warning: SSH (key) not answering yet"; fi
  } > "$d/bootstrap.log" 2>&1
}

# ---- commands ---------------------------------------------------------------------------------------------------------
cmd_image() { "$LAB_DIR/tools/build_image.sh" "$@"; }

cmd_up() {   # define (first time), start; a router's first boot also gets its day-0 config (~3 min)
  ensure_networks
  local n fresh=(); for n in $(nodes_or_all "$@"); do
    defined "$n" || build "$n"
    [[ -f "$(node_dir "$n")/console.log" ]] || { touch "$(node_dir "$n")/console.log"; chmod 644 "$(node_dir "$n")/console.log"; }
    if running "$n"; then echo "[$n] already running"; continue; fi
    if ! V start "$(dom "$n")" >/dev/null 2>&1; then   # a saved image (Lab Hub fast resume) that no longer restores: boot cold
      V managedsave-remove "$(dom "$n")" &>/dev/null || true; V start "$(dom "$n")" >/dev/null
    fi
    echo "[$n] started (console: 127.0.0.1:${CONSOLE_PORT[$n]})"
    [[ "${ROLE[$n]}" == router && ! -f "$(node_dir "$n")/.bootstrapped" ]] && fresh+=("$n")
  done
  [[ ${#fresh[@]} -gt 0 ]] && cmd_bootstrap "${fresh[@]}"
  return 0
}

cmd_bootstrap() {   # push the day-0 config to the routers over their serial consoles, in parallel
  local n pids=() names=() i rc=0
  for n in $(routers_of $(nodes_or_all "$@")); do
    running "$n" || { echo "[$n] not running, skipped"; continue; }
    bootstrap_vyos "$n" & pids+=($!); names+=("$n"); echo "[$n] day-0 config over the console (log: nodes/$n/bootstrap.log)"
  done
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || true; n="${names[$i]}"
    if [[ -f "$(node_dir "$n")/.bootstrapped" ]] && ! grep -qE 'Invalid|Commit failed|!!|Traceback|timeout' "$(node_dir "$n")/bootstrap.log"; then
      echo "[$n] OK"
    else echo "[$n] FAIL — see nodes/$n/bootstrap.log"; grep -nE 'Invalid|Commit failed|failed|!!|Traceback|timeout|warning' "$(node_dir "$n")/bootstrap.log" | head -5; rc=1; fi
  done
  return $rc
}

cmd_configure() {   # re-render and re-apply the routers' config over SSH (configure; set..; commit; save) — idempotent
  local n; for n in $(routers_of $(nodes_or_all "$@")); do
    vyos_config "$n" > "$(node_dir "$n")/vyos_config.txt"
    { echo "source /opt/vyatta/etc/functions/script-template"; echo configure; grep -v '^#' "$(node_dir "$n")/vyos_config.txt"; echo commit; echo save; echo exit; } \
      | S "$n" 'cat > /tmp/day0.sh; sg vyattacfg -c "/bin/vbash /tmp/day0.sh"' 2>&1 | grep -vE '^$|Saving configuration|Done|WARNING: (Default password|User "vyos")|not be too short' | sed "s/^/[$n] /" || true
    echo "[$n] applied"
  done
}

cmd_down() {
  local n; for n in $(nodes_or_all "$@"); do
    running "$n" || { echo "[$n] not running"; continue; }
    V shutdown "$(dom "$n")" >/dev/null 2>&1 || true
    for _ in $(seq 30); do running "$n" || break; sleep 2; done
    running "$n" && V destroy "$(dom "$n")" >/dev/null; echo "[$n] stopped"
  done
}

cmd_wait() {   # SSH answering (and cloud-init done on the Alpine nodes)
  local n; for n in $(nodes_or_all "$@"); do
    for _ in $(seq 90); do ssh_ready "$n" && S "$n" true 2>/dev/null && break; sleep 4; done
    if [[ "${ROLE[$n]}" == router ]]; then S "$n" true 2>/dev/null && echo "[$n] ready" || echo "[$n] NOT ready"
    elif S "$n" "for i in \$(seq 60); do [ -f /var/lib/cloud/instance/boot-finished ] && exit 0; sleep 2; done; exit 1" 2>/dev/null; then echo "[$n] ready"
    else echo "[$n] NOT ready"; fi
  done
}

cmd_resumed() {   # after the Lab Hub's fast resume (virsh managedsave + start): wait for SSH, set the clocks (they stood still while
  # the VMs were saved), check the lab. The WAN emulator's qdiscs, the hosts' sysctls and the routes come back with the memory.
  local n; cmd_wait "$@" || true
  for n in $(nodes_or_all "$@"); do (
    local before; before="$(S "$n" 'date -u +%s' 2>/dev/null | tr -d '\r')"
    if [[ "${ROLE[$n]}" == router ]]; then S "$n" "sudo date -u -s @$(date +%s) >/dev/null" 2>/dev/null; else S "$n" "doas date -u -s @$(date +%s) >/dev/null" 2>/dev/null; fi \
      && echo "[$n] clock set (was $(( $(date +%s) - ${before:-0} )) s behind)" || echo "[$n] clock NOT set"
  ) & done; wait
  local t; for t in $(seq 6); do cmd_verify >/tmp/tcpcc-resumed-verify.txt 2>&1 && { echo "verify passed after resume"; return 0; }; sleep 10; done
  echo "verify still failing after resume:"; tail -5 /tmp/tcpcc-resumed-verify.txt; return 1
}

cmd_rebuild() {   # re-define domains and seeds from lab.conf (keeps disks)
  local n; for n in $(nodes_or_all "$@"); do running "$n" && die "$n is running; stop it first"
    defined "$n" && V undefine --managed-save "$(dom "$n")" >/dev/null; build "$n"; echo "[$n] redefined"; done
}

cmd_clean() {     # stop, undefine, delete overlays (the base images stay)
  local n; for n in $(nodes_or_all "$@"); do
    running "$n" && V destroy "$(dom "$n")" >/dev/null; defined "$n" && V undefine --managed-save "$(dom "$n")" >/dev/null
    rm -f "$(node_dir "$n")"/{disk.qcow2,seed.iso,seed.iso.tmp,meta-data,user-data,network-config,domain.xml,console.log,vyos_config.txt,bootstrap.log,.bootstrapped}; echo "[$n] removed"
  done
}

cmd_status() {
  printf '%-8s %-7s %-10s %-11s %-15s %-6s %-8s %s\n' NODE ROLE STATE MGMT-IP DATA-IP CC CONSOLE VM
  local n st; for n in "${ALL_NODES[@]}"; do
    st="$(V domstate "$(dom "$n")" 2>/dev/null || echo undefined)"
    printf '%-8s %-7s %-10s %-11s %-15s %-6s %-8s %s\n' "$n" "${ROLE[$n]}" "$st" "${MGMT_IP[$n]}" "${DATA_IP[$n]:-bridge}" "${CC[$n]:--}" "${CONSOLE_PORT[$n]}" "$(dom "$n")"
  done
  echo; echo "WAN: r1 ${WAN_IP[r1]} <-> wanem (br0) <-> r2 ${WAN_IP[r2]}; defaults RTT ${WAN_RTT_MS} ms, ${WAN_RATE_MBIT} Mbit/s, loss ${WAN_LOSS_PCT} %, buffer ${WAN_BUFFER_BDP} x BDP"
}

cmd_inventory() {
  local n first=1
  {
    printf '{"lab": "tcp-cc-lab", "wan": {"defaults": {"rtt_ms": %s, "rate_mbit": %s, "loss_pct": %s, "buffer_bdp": %s}, "rtt_max_ms": %s, "r1": "%s", "r2": "%s"}, "nodes": [' \
      "$WAN_RTT_MS" "$WAN_RATE_MBIT" "$WAN_LOSS_PCT" "$WAN_BUFFER_BDP" "$WAN_RTT_MAX_MS" "${WAN_IP[r1]}" "${WAN_IP[r2]}"
    for n in "${ALL_NODES[@]}"; do [[ $first -eq 1 ]] || printf ','; first=0
      printf '{"name": "%s", "role": "%s", "site": "%s", "mgmt_ip": "%s", "ip": "%s", "cc": "%s", "peer": "%s", "user": "%s", "domain": "%s"}' \
        "$n" "${ROLE[$n]}" "${SITE[$n]}" "${MGMT_IP[$n]}" "${DATA_IP[$n]:-}" "${CC[$n]:-}" "${PEER[$n]:-}" "$(user_of "$n")" "$(dom "$n")"
    done
    echo ']}'
  } | python3 -m json.tool
}

cmd_mtu() {   # show | set 1500|9000 — the hosts' eth1 MTU (all six); the routers' and wanem's links stay at INFRA_MTU
  local sub="${1:-show}" m h n; shift || true
  case "$sub" in
    show)
      for n in "${ALL_NODES[@]}"; do
        printf '%-8s %s\n' "$n" "$(S "$n" 'for d in eth1 eth2 br0; do [ -e /sys/class/net/$d ] && printf "%s %s  " $d $(cat /sys/class/net/$d/mtu); done; true' 2>/dev/null || echo unreachable)"
      done ;;
    set)
      m="${1:?usage: lab.sh mtu set ${HOST_MTUS[*]}}"; [[ " ${HOST_MTUS[*]} " == *" $m "* ]] || die "MTU must be one of: ${HOST_MTUS[*]}"
      for h in "${HOSTS[@]}"; do ( S "$h" "doas sh -c 'echo $m > /etc/tcpcc-mtu && ip link set eth1 mtu $m && ip route flush cache' && echo \"$h eth1 mtu \$(cat /sys/class/net/eth1/mtu)\"" 2>&1 || echo "$h FAILED" ) & done; wait ;;
    *) die "usage: lab.sh mtu show | set ${HOST_MTUS[*]}" ;;
  esac
}

cmd_wan() { S "$WANEM" doas wanem "$@"; }   # show | stats | apply RTT_MS RATE_MBIT LOSS_PCT BUFFER_BDP | reset | offloads

cmd_cc() {   # each host's algorithm, qdisc and iperf3 server
  local h; for h in "${HOSTS[@]}"; do
    S "$h" 'printf "%-8s cc %-6s (available: %s)  mtu %s  qdisc eth1: %s  iperf3: %s\n" "$(hostname)" "$(sysctl -n net.ipv4.tcp_congestion_control)" "$(sysctl -n net.ipv4.tcp_available_congestion_control)" "$(cat /sys/class/net/eth1/mtu)" "$(tc qdisc show dev eth1 | awk "NR==1{print \$2}")" "$(pgrep -x iperf3 >/dev/null && echo up || echo DOWN)"' 2>/dev/null || echo "$h unreachable"
  done
}

cmd_verify() {   # quick end-to-end look: algorithms, the WAN settings, routing, the RTT across the WAN
  echo "== hosts"; cmd_cc
  echo; echo "== wanem"; { cmd_wan show | head -8; } || true
  echo; echo "== routers"; local r; for r in "${ROUTERS[@]}"; do S "$r" "ip -br addr show | grep -E '^eth[12]'; ip route show | grep -E '192.168.14[12]'" 2>&1 | sed "s/^/  [$r] /"; done
  echo; echo "== infrastructure MTU (must be $INFRA_MTU)"
  local bad=0 r d v
  for r in r1 r2 wanem; do for d in eth1 eth2 $([[ $r == wanem ]] && echo br0); do
    v="$(S "$r" "cat /sys/class/net/$d/mtu" 2>/dev/null)"; [[ "$v" == "$INFRA_MTU" ]] || { bad=1; echo "  $r $d mtu ${v:-?} (NOT $INFRA_MTU)"; }
  done; done; [[ $bad -eq 0 ]] && echo "  r1, r2, wanem: every data link $INFRA_MTU"
  echo; echo "== across the WAN: every site A host to every site B host (ping, 5 x 0.2 s; one unfragmentable at the hosts' MTU)"
  local a b out ok=0 tot=0 hm big; hm="$(S "${HOSTS_A[0]}" 'cat /sys/class/net/eth1/mtu')"; big=$((hm - 28))
  for a in "${HOSTS_A[@]}"; do for b in "${HOSTS_B[@]}"; do tot=$((tot + 1))
    out="$(S "$a" "ping -c1 -W2 -M do -s $big -q ${DATA_IP[$b]} >/dev/null && ping -c5 -i0.2 -W2 -q ${DATA_IP[$b]}" 2>&1 | awk -F'= ' '/min\/avg/{split($2, v, "/"); print v[2]}')"
    if [[ -n "$out" ]]; then ok=$((ok + 1)); printf '  %-8s -> %-8s rtt avg %s ms\n' "$a" "$b" "$out"; else printf '  %-8s -> %-8s FAIL\n' "$a" "$b"; fi
  done; done; echo "  $ok/$tot pairs ok (hosts' MTU $hm: $big-byte pings, DF set)"
  [[ $ok -eq $tot && $bad -eq 0 ]]
}

cmd_flows() { python3 "$LAB_DIR/tools/cclab.py" "$@"; }   # tools/cclab.py: compete | solo | rtt | wan (JSON on stdout)

cmd_exec() {   # exec <node|all|hosts> <command...>
  local t="${1:?node|all|hosts}"; shift; local ns n
  case "$t" in all) ns="${ALL_NODES[*]}";; hosts) ns="${HOSTS[*]}";; *) known "$t"; S "$t" "$@"; return;; esac
  for n in $ns; do ( S "$n" "$@" 2>&1 | sed "s/^/[$n] /" ) & done; wait
}

cmd_webapp() {   # the portal (webapp/, port 8112; installed as the user unit tcp-cc-lab-webapp)
  [[ -x "$LAB_DIR/webapp/.venv/bin/python" ]] || "$LAB_DIR/webapp/setup.sh"
  exec "$LAB_DIR/webapp/.venv/bin/python" "$LAB_DIR/webapp/app.py"
}

cmd_test() {
  [[ -x "$LAB_DIR/tests/.venv/bin/robot" ]] || "$LAB_DIR/tests/setup.sh"
  exec "$LAB_DIR/tests/run.sh" "$@"
}

cmd_console() { local n="${1:?node}"; known "$n"; running "$n" || die "$n is not running"; echo "console $n (exit: Ctrl-] then q)"; socat -,raw,echo=0,escape=0x1d "tcp:127.0.0.1:${CONSOLE_PORT[$n]}"; }
cmd_ssh() { local n="${1:?node}"; shift; known "$n"; ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$(user_of "$n")@${MGMT_IP[$n]}" "$@"; }
cmd_log() { tail -n "${2:-50}" -f "$(node_dir "${1:?node}")/console.log"; }

usage() {
  cat <<U
usage: $(basename "$0") <command> [node...]
  image                build images/tcpcc-alpine.qcow2 (Alpine + iperf3, iproute2, ethtool, tcpdump), ~2 min
  up [node..]          create (if needed) and start the VMs (default: all); first boot of a router pushes its day-0 config
  wait [node..]        until SSH answers (and cloud-init is done)
  down [node..]        shut the VMs down
  status | inventory   nodes, addresses, algorithms | the lab as JSON
  mtu show | set ${HOST_MTUS[*]// /|}   the hosts' MTU (all six; r1 / r2 / wanem links always $INFRA_MTU)
  wan show|stats|apply RTT_MS RATE_MBIT LOSS_PCT BUFFER_BDP|reset   the WAN emulator (defaults: $WAN_RTT_MS ms RTT, $WAN_RATE_MBIT Mbit/s)
  cc                   each host's congestion control, qdisc, iperf3 server
  flows <sub> [opts]   compete | solo | rtt | wan  (tools/cclab.py --help)
  verify               algorithms, WAN settings, routes, ping RTT between every site A / site B pair
  configure [router..] re-apply the routers' config over SSH;  bootstrap [router..]  day-0 over the console again
  exec <node|hosts|all> <cmd>   run a command (hosts/all: in parallel)
  test [robot args]    Robot Framework suites, tests/results_<timestamp>/
  webapp               the portal on port 8112 (user unit tcp-cc-lab-webapp)
  ssh <node> [cmd] | console <node> | log <node> [n]
  resumed [node..]     after the Lab Hub's fast resume: wait for SSH, set the clocks, verify
  rebuild [node..]     re-define domains and seeds (keeps disks; stop the nodes first)
  clean [node..]       destroy VMs and delete their disks (base images kept)
nodes: ${ALL_NODES[*]}   (libvirt domains tcpcc-<node>; hosts/wanem lab / lab, routers vyos / vyos, your SSH key installed)
U
}

cmd="${1:-}"; shift || true
case "$cmd" in
  image|up|down|wait|resumed|bootstrap|configure|status|inventory|mtu|wan|cc|flows|verify|exec|test|webapp|ssh|console|log|rebuild|clean) "cmd_$cmd" "$@" ;;
  *) usage; exit 1 ;;
esac

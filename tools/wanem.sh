#!/bin/sh
# wanem — the WAN emulator's control (installed in the wanem VM as /usr/local/bin/wanem, run as root).
# br0 bridges eth1 (towards r1, site A) and eth2 (towards r2, site B). Each egress direction gets the same impairment:
#   root  1:  netem  delay = RTT/2, loss = LOSS% (random, per direction), limit sized to hold a full delay line
#   child 10: tbf    rate = the bottleneck, limit = BUFFER x BDP bytes (the bottleneck queue, where the drops happen)
# Delay first, then the bottleneck queue: the queue a congestion control fills is tbf's, and its size does not move
# when the delay does. "apply" changes the qdiscs in place (tc qdisc change), so running flows see the new RTT at once.
#   wanem apply RTT_MS RATE_MBIT LOSS_PCT BUFFER_BDP | reset | show | stats (JSON) | offloads
set -eu
CONF=/etc/wanem.conf; STATE=/run/wanem.state; DEVS="eth1 eth2"
[ -r "$CONF" ] && . "$CONF"

offloads() {   # the qdiscs must see wire-size packets: no GRO / LRO (virtio guest TSO) coalescing on the way in, no TSO/GSO out
  for d in $DEVS; do ethtool -K "$d" gro off lro off tso off gso off sg off rx-gro-hw off 2>/dev/null || ethtool -K "$d" gro off tso off gso off 2>/dev/null || true; done
}

apply() {
  rtt="$1"; rate="$2"; loss="$3"; buf="$4"
  # one-way delay in µs; netem queue limit = packets in flight on the delay line at the bottleneck rate, x2, + 1000;
  # tbf burst >= rate / HZ (250 or 1000) and >= 2 jumbo frames (the links are MTU 9000: a frame bigger than the burst is
  # dropped by tbf); tbf limit = BUFFER x BDP bytes, at least 4 jumbo frames and at most 128 MB (this VM has 1 GB:
  # at 5 Gbit/s x 300 ms, 16 x BDP would be 3 GB)
  eval "$(awk -v r="$rtt" -v m="$rate" -v b="$buf" 'BEGIN {
    ow = r * 500; bps = m * 1e6; bdp = bps / 8 * r / 1000;
    lim = int(bps / 12000 * ow / 1e6 * 2) + 1000;
    burst = int(bps / 8 / 250); if (burst < 18028) burst = 18028;
    q = int(b * bdp); if (q < 36056) q = 36056; if (q > 134217728) q = 134217728;
    printf "OW=%d NLIM=%d BURST=%d QLIM=%d BDP=%d\n", ow, lim, burst, q, bdp }')"
  lossarg=""; [ "$(awk -v l="$loss" 'BEGIN{print (l > 0)}')" = 1 ] && lossarg="loss random ${loss}%"
  for d in $DEVS; do
    if tc qdisc show dev "$d" | grep -q "^qdisc netem 1: root"; then
      tc qdisc change dev "$d" root handle 1: netem delay "${OW}us" limit "$NLIM" $lossarg
      tc qdisc change dev "$d" parent 1:1 handle 10: tbf rate "${rate}mbit" burst "$BURST" limit "$QLIM"
    else
      tc qdisc replace dev "$d" root handle 1: netem delay "${OW}us" limit "$NLIM" $lossarg
      tc qdisc replace dev "$d" parent 1:1 handle 10: tbf rate "${rate}mbit" burst "$BURST" limit "$QLIM"
    fi
  done
  printf 'RTT_MS=%s\nRATE_MBIT=%s\nLOSS_PCT=%s\nBUFFER_BDP=%s\nONE_WAY_US=%s\nQUEUE_BYTES=%s\nBDP_BYTES=%s\nAPPLIED=%s\n' \
    "$rtt" "$rate" "$loss" "$buf" "$OW" "$QLIM" "$BDP" "$(date +%s)" > "$STATE"
}

show() {
  [ -r "$STATE" ] && cat "$STATE"
  for d in $DEVS; do echo "--- $d (egress towards $( [ "$d" = eth1 ] && echo r1 || echo r2 ))"; tc -s qdisc show dev "$d"; done
}

stats() {   # one JSON object: the settings and both directions' netem + tbf counters
  printf '{"settings": {'; [ -r "$STATE" ] && awk -F= 'NR>1{printf ", "} {printf "\"%s\": %s", $1, $2}' "$STATE"; printf '}'
  for d in $DEVS; do printf ', "%s": ' "$d"; tc -s -j qdisc show dev "$d"; done; printf '}\n'
}

case "${1:-show}" in
  apply)    shift; [ $# -eq 4 ] || { echo "usage: wanem apply RTT_MS RATE_MBIT LOSS_PCT BUFFER_BDP" >&2; exit 2; }; offloads; apply "$@"; show | head -8 ;;
  reset)    offloads; apply "${RTT_MS:-30}" "${RATE_MBIT:-100}" "${LOSS_PCT:-0}" "${BUFFER_BDP:-1}"; show | head -8 ;;
  show)     show ;;
  stats)    stats ;;
  offloads) offloads; for d in $DEVS; do ethtool -k "$d" | grep -E "^(generic|tcp-segmentation|large-receive)-offload|^generic-receive"; done ;;
  *) echo "usage: wanem apply RTT_MS RATE_MBIT LOSS_PCT BUFFER_BDP | reset | show | stats | offloads" >&2; exit 2 ;;
esac

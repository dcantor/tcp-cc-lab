#!/usr/bin/env bash
# Build images/tcpcc-alpine.qcow2 — the base image of the hosts and of wanem: Alpine (NoCloud cloud-init image) + iperf3,
# iproute2 (full tc with netem / tbf, ss -ti), ethtool, tcpdump, mtr, iputils ping (-M do: unfragmentable probes), with a `lab` user. A throw-away builder VM on the libvirt
# NAT network (default) installs the packages, checks that the kernel has BBR, cleans cloud-init and powers off; its disk
# becomes the base image. Re-run to rebuild.   tools/build_image.sh
set -euo pipefail
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"; source "$LAB/lab.conf"
SRC="$ALPINE_SRC"; OUT="$BASE_IMAGE"; NAME=tcpcc-builder
V() { virsh -q -c "$LIBVIRT_URI" "$@"; }
B="$LAB/images/.builder"; rm -rf "$B"; mkdir -p "$B"
qemu-img create -q -f qcow2 -b "$SRC" -F qcow2 "$B/disk.qcow2" 2G
cat > "$B/user-data" <<'U'
#cloud-config
package_update: true
packages: [iperf3, iproute2, ethtool, tcpdump, mtr, curl, iputils-ping]
users:
  - name: lab
    plain_text_passwd: lab
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/sh
ssh_pwauth: true
runcmd:
  - rc-update add sshd default
  - sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config
  - [sh, -c, 'modprobe tcp_bbr && modprobe sch_netem && modprobe sch_tbf && modprobe sch_fq && echo "CHECK cc: $(sysctl -n net.ipv4.tcp_available_congestion_control)" > /dev/console']
  - [sh, -c, 'echo "CHECK iperf3: $(iperf3 --version | head -1)" > /dev/console; echo "CHECK tc: $(tc -V)" > /dev/console; echo "CHECK ping: $(ping -V 2>&1 | head -1)" > /dev/console']
  - cloud-init clean --logs
  - poweroff
U
printf 'instance-id: builder-001\nlocal-hostname: builder\n' > "$B/meta-data"
genisoimage -quiet -o "$B/seed.iso" -V cidata -J -r "$B/user-data" "$B/meta-data"
cat > "$B/domain.xml" <<X
<domain type='kvm'><name>$NAME</name><memory unit='MiB'>512</memory><vcpu>1</vcpu><cpu mode='host-passthrough' check='none'/>
<os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os><features><acpi/><apic/></features><on_poweroff>destroy</on_poweroff>
<devices><emulator>/usr/bin/qemu-system-x86_64</emulator>
<disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='$B/disk.qcow2'/><target dev='vda' bus='virtio'/></disk>
<disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='$B/seed.iso'/><target dev='hda' bus='ide'/><readonly/></disk>
<interface type='network'><source network='default'/><model type='virtio'/></interface>
<serial type='tcp'><source mode='bind' host='127.0.0.1' service='$BUILDER_CONSOLE'/><protocol type='raw'/><log file='$B/console.log' append='on'/><target port='0'/></serial><memballoon model='none'/></devices></domain>
X
touch "$B/console.log"; chmod 644 "$B/console.log"   # pre-created: virtlogd appends to our file instead of a root-only one
V destroy $NAME >/dev/null 2>&1 || true; V undefine $NAME >/dev/null 2>&1 || true
V define "$B/domain.xml" >/dev/null; V start $NAME >/dev/null
echo "builder started (NAT network, installing iperf3 / iproute2 / ethtool / tcpdump / mtr) ..."
for _ in $(seq 120); do [[ "$(V domstate $NAME)" == "running" ]] || break; sleep 5; done
[[ "$(V domstate $NAME)" == "running" ]] && { echo "builder still running after 10 min — see $B/console.log" >&2; exit 1; }
V undefine $NAME >/dev/null
grep -a "CHECK" "$B/console.log" | tr -d '\r' || true
grep -aq "CHECK cc:.*bbr" "$B/console.log" || { echo "error: the kernel has no BBR (no 'CHECK cc: ... bbr' in $B/console.log)" >&2; exit 1; }
rm -f "$OUT"   # libvirt took the old one over (dynamic ownership) and its overlays must be gone by now: lab.sh clean first
qemu-img convert -O qcow2 -c "$B/disk.qcow2" "$OUT"; rm -rf "$B"
echo "built $OUT ($(du -h "$OUT" | cut -f1)); user lab / lab"

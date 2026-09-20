#!/bin/sh
# Read-only Linux capability and interface evidence.
set -eu
nic=${1:?interface required}
case "$nic" in ''|*[!a-zA-Z0-9_.:-]*) echo 'Invalid interface' >&2; exit 2;; esac
[ -d "/sys/class/net/$nic" ] || { echo 'Interface missing' >&2; exit 2; }
uname -sm
cat /etc/os-release
command -v iperf3
iperf3 --version
command -v timeout
command -v python3
readlink -f "/sys/class/net/$nic/device"
ip -j address show dev "$nic"
ip -j route show
for field in operstate mtu speed duplex device/numa_node device/local_cpulist; do
  printf '\n%s: ' "$field"
  cat "/sys/class/net/$nic/$field" 2>/dev/null || printf 'unavailable\n'
done
command -v lscpu >/dev/null && lscpu || true
command -v ethtool >/dev/null && ethtool "$nic" || true
command -v ethtool >/dev/null && ethtool -i "$nic" || true
printf '\nIRQ affinity:\n'
for path in /sys/class/net/"$nic"/device/msi_irqs/*; do
  [ -e "$path" ] || continue
  irq=${path##*/}
  printf '%s ' "$irq"
  cat "/proc/irq/$irq/smp_affinity_list" 2>/dev/null || true
done
command -v systemctl >/dev/null && systemctl is-active irqbalance || true
cat /proc/interrupts

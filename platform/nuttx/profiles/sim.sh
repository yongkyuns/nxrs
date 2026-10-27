# Sourced by the corresponding tools/build-nuttx script in the isolated NuttX tree.
./tools/configure.sh -l -a ../apps sim:nsh
for setting in EXAMPLES_NXRS_SIM HOST_X86_64 SIM_X8664_SYSTEMV \
  FS_TMPFS NET NET_IPv4 NET_UDP NET_LOOPBACK NET_SOCKOPTS \
  NET_UDP_WRITE_BUFFERS NET_UDP_READAHEAD SCHED_HPWORK SCHED_LPWORK \
  SIM_WALLTIME_SLEEP DEBUG_SYMBOLS DEV_URANDOM DEV_URANDOM_XORSHIFT128; do
  kconfig-tweak --enable "CONFIG_$setting"
done
# This core-only, loopback fixture needs random port selection. The patched
# arc4random correctly aborts without a source. Select NuttX's explicit test
# PRNG here; it does not qualify entropy or supply a product/TLS profile.
# Preserve loopback configuration. The sim sleep-clock profile is not an
# asynchronous timer-preemption test; the QEMU profile explicitly is.
for setting in SIM_M32 SIM_NETDEV SIM_NETUSRSOCK NET_USRSOCK NET_ETHERNET \
  SIM_WALLTIME_SIGNAL COVERAGE_ALL COVERAGE_TOOLCHAIN SYSTEM_GCOV \
  TESTING_OSTEST EXAMPLES_GPIO DISABLE_PTHREAD NSH_NETINIT NETUTILS_NETINIT \
  DEV_RANDOM DEV_URANDOM_ARCH DEV_URANDOM_CONGRUENTIAL DEV_URANDOM_RANDOM_POOL; do
  kconfig-tweak --disable "CONFIG_$setting"
done
kconfig-tweak --set-val CONFIG_NET_RECV_BUFSIZE 4096
make olddefconfig
for setting in ARCH_SIM HOST_X86_64 EXAMPLES_NXRS_SIM FS_TMPFS NET_LOOPBACK NET_UDP SCHED_HPWORK \
  DEV_URANDOM DEV_URANDOM_XORSHIFT128; do
  grep -qx "CONFIG_$setting=y" .config || { echo "Unresolved config: $setting" >&2; exit 1; }
done
if grep -Eq '^CONFIG_(SIM_NETDEV|SIM_NETUSRSOCK|NET_USRSOCK|SIM_M32|NSH_NETINIT|NETUTILS_NETINIT|DEV_RANDOM|DEV_URANDOM_ARCH|DEV_URANDOM_CONGRUENTIAL|DEV_URANDOM_RANDOM_POOL)=y$' .config; then
  echo 'Unexpected host-network/architecture/network-init configuration' >&2
  exit 1
fi

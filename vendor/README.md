# Build inputs

`iperf-3.21.tar.gz` is the unmodified upstream source distribution from
https://downloads.es.net/pub/iperf/iperf-3.21.tar.gz .

SHA-256: `656e4405ebd620121de7ceca3eaf43a88f79ea1b857d041a6a0b1314801acdd8`.
The archive includes upstream copyright and license notices. Native Linux CI
builds static binaries for x86_64 and aarch64; each offline tool package includes
the iperf3 LICENSE and the musl copyright notice. The source is checked into the
repository so build runners do not depend on download-site availability.

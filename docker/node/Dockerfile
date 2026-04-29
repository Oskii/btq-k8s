# BTQ node image — runs btqd in regtest with full RPC/ZMQ/debug exposure
# so the cluster can be observed end-to-end.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        libminiupnpc17 \
        libnatpmp1 \
        libevent-2.1-7 \
        libevent-pthreads-2.1-7 \
        libzmq5 \
        libsqlite3-0 \
        libsodium23 \
        libgssapi-krb5-2 \
        libpgm-5.3-0 \
        libnorm1 \
        libbsd0 \
        libstdc++6 \
        bind9-host \
        curl \
        jq \
        tini \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 btq \
 && useradd  --system --uid 1000 --gid 1000 --home /var/lib/btq --create-home btq

COPY btqd     /usr/local/bin/btqd
COPY btq-cli  /usr/local/bin/btq-cli
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/btqd /usr/local/bin/btq-cli /usr/local/bin/entrypoint.sh

USER btq
WORKDIR /var/lib/btq

EXPOSE 18443 18444 28332 28333

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

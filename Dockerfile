# Vintage Story dedicated server image for Flux.
#
# Written from scratch. The game is not on Steam: the server is a direct download from Anego
# Studios' CDN (cdn.vintagestory.at, no login, about 51 MB), fetched on the first start and on
# every update, for the version the owner pins or the newest stable one. It runs on .NET 10
# under a supervisor that writes serverconfig.json from the environment, saves on stop, follows
# game updates, keeps backups, installs mods from the official ModDB, and gives the hub a console
# and JSON status, the world's settings, the players' roles and lists, and the mods. See README.md.
#
# The base. CI builds it pinned to the digest it resolved and records that digest in the
# org.opencontainers.image.base.digest label, which the scheduled check compares against
# (.github/ci/needs-rebuild.sh).
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG FLUX_IMAGE_VERSION=dev

# The .NET runtime the server is built for: VintagestoryServer.runtimeconfig.json of 1.22.7 asks
# for Microsoft.NETCore.App 10.0 (net10.0). From Microsoft's own tarball, checked against the
# SHA-512 its release metadata publishes
# (builds.dotnet.microsoft.com/dotnet/release-metadata/10.0/releases.json): reproducible, and no
# second registry in the build. needs-rebuild.sh warns when a newer 10.0 patch is out.
ARG DOTNET_VERSION=10.0.13
ARG DOTNET_SHA512=9574d95364110409416735e7842796f8d614c21d74cb25d204a3e4cfd11b2c05efdf19ca89b8069650a04f64add2705967dcb04b8665a36d7a3f30a5d8c05afe

# libicu74: .NET's globalisation (without it the runtime refuses to start unless invariant mode is
# forced, and the game formats dates and numbers). libssl3: HTTPS from .NET (the auth server, the
# master server list). libsqlite3 is NOT needed: the game ships its own e_sqlite3.
# python3, NOT python3-minimal: fluxvs.py needs json, zipfile, urllib and hashlib.
RUN apt-get update -y && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl procps tzdata python3 libicu74 libssl3 libgcc-s1 libstdc++6 zlib1g && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/dotnet.tar.gz \
        "https://builds.dotnet.microsoft.com/dotnet/Runtime/${DOTNET_VERSION}/dotnet-runtime-${DOTNET_VERSION}-linux-x64.tar.gz" && \
    echo "${DOTNET_SHA512}  /tmp/dotnet.tar.gz" | sha512sum -c - && \
    mkdir -p /usr/share/dotnet && tar -xzf /tmp/dotnet.tar.gz -C /usr/share/dotnet && rm /tmp/dotnet.tar.gz && \
    ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet && \
    dotnet --list-runtimes | grep -q "Microsoft.NETCore.App ${DOTNET_VERSION}"

COPY scripts/flux-lib.sh scripts/flux-entrypoint.sh scripts/flux-console.sh scripts/fluxvs.py scripts/flux-quiet.awk /opt/flux/
RUN chmod 0755 /opt/flux/*.sh /opt/flux/fluxvs.py && \
    ln -s /opt/flux/flux-console.sh /usr/local/bin/flux-console && \
    ln -s /opt/flux/fluxvs.py /usr/local/bin/flux-vs

ENV FLUX_IMAGE_VERSION=${FLUX_IMAGE_VERSION} \
    DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1

VOLUME ["/mnt/vs/server", "/mnt/vs/data"]

# One port, TCP and UDP: the game's own 42420 (players connect over TCP, the game moves position
# updates to UDP on the same number).
EXPOSE 42420/tcp 42420/udp

# The first start downloads about 51 MB. FluxOS does not act on health; this is for anyone
# running the image by hand.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10m --retries=3 \
    CMD pgrep -f "VintagestoryServer\.dll --dataPath" >/dev/null || exit 1

ENTRYPOINT []
CMD ["/opt/flux/flux-entrypoint.sh"]

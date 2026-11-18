# syntax=docker/dockerfile:1.7
#
# Builds Teleport's Slack plugin with the Beams app wired in: clone Teleport at
# TELEPORT_REF, apply upstream.patch, drop in plugin/*.go, build, and bundle
# tsh. Build context is this directory.

ARG TELEPORT_REF=v18.11.10
ARG TSH_VERSION=18.11.10
ARG GO_IMAGE=golang:1.26.10-bookworm

FROM ${GO_IMAGE} AS build
ARG TELEPORT_REF
ARG TSH_VERSION
ARG TARGETARCH
WORKDIR /src
RUN git clone --quiet --depth 1 --branch "${TELEPORT_REF}" \
      https://github.com/gravitational/teleport.git .
COPY upstream.patch /tmp/upstream.patch
COPY plugin/ integrations/access/slack/
RUN git apply /tmp/upstream.patch
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build \
      -tags kustomize_disable_go_plugin_support \
      -trimpath -ldflags="-w -s" \
      -o /out/teleport-slack \
      ./integrations/access/slack/cmd/teleport-slack
RUN curl -fsSL \
      "https://cdn.teleport.dev/teleport-v${TSH_VERSION}-linux-${TARGETARCH}-bin.tar.gz" \
      -o /tmp/teleport.tar.gz \
    && tar -xzf /tmp/teleport.tar.gz -C /tmp \
    && install -m 0755 /tmp/teleport/tsh /out/tsh

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 teleport-slack \
    && useradd --system --uid 10001 --gid teleport-slack \
       --home-dir /var/lib/teleport-slack teleport-slack \
    && install -d -o teleport-slack -g teleport-slack -m 0700 \
       /var/lib/teleport-slack/beams
COPY --from=build /out/teleport-slack /usr/local/bin/teleport-slack
COPY --from=build /out/tsh /usr/local/bin/tsh
USER 10001:10001
VOLUME ["/var/lib/teleport-slack/beams"]
ENTRYPOINT ["/usr/local/bin/teleport-slack"]
CMD ["start", "--config=/etc/teleport-slack/config.toml"]

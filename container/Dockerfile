# Coordinator image: a static binary and the web files, nothing else.
# Built by container/build.sh from binaries cross-compiled on the build host
# (no compilation inside an emulated container), so the same Dockerfile serves
# linux/amd64 and linux/arm64. See docs/distributed/install.md (Container).
FROM scratch
ARG TARGETARCH
COPY --chmod=0555 bin/${TARGETARCH}/bridge-coordinator /usr/local/bin/bridge-coordinator
# Preserve the generated tree's directory traversal bits and read-only file modes.
# chmod=0444 also applies to directories, preventing UID 65532 from entering web/.
COPY web/ /usr/share/bridge-coordinator/web/
# Mount point for the data volume, owned by the runtime user so a new named
# volume starts writable without running anything as root.
COPY --chown=65532:65532 --chmod=0700 data/ /data/
USER 65532:65532
# Inside the container the server listens on all container interfaces; what
# the host exposes is decided by the published port in compose.yaml, which is
# loopback unless the operator opts in.
ENV BRIDGE_COORDINATOR_DATA_DIR=/data \
    BRIDGE_COORDINATOR_BIND=0.0.0.0:8443 \
    BRIDGE_COORDINATOR_LAN_ENABLED=true \
    BRIDGE_COORDINATOR_CONTAINER=1
VOLUME ["/data"]
EXPOSE 8443
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/bridge-coordinator", "health"]
ENTRYPOINT ["/usr/local/bin/bridge-coordinator"]
CMD ["serve"]

FROM bluenviron/mediamtx:1.15.3-ffmpeg

# Install Python3 for the stream supervisor (stdlib only).
RUN apk add --no-cache python3

# Create non-root user
RUN addgroup -g 1000 autostream && \
    adduser -D -u 1000 -G autostream autostream

WORKDIR /app

# Pre-create the videos mount point so it exists owned by autostream
# even when the container runs without a bind mount.
RUN mkdir -p /app/videos

COPY stream-video.sh /usr/local/bin/stream-video.sh
COPY stream-supervisor.py /app/stream-supervisor.py
COPY index.html /app/index.html
COPY entrypoint.sh /app/entrypoint.sh

# All app files and dirs owned by autostream (UID/GID 1000); stream-video.sh
# stays root-owned but world-executable.
RUN chown -R autostream:autostream /app && \
    chmod 755 /usr/local/bin/stream-video.sh

# Switch to non-root user — every process spawned from the entrypoint
# (mediamtx, python3, ffmpeg) runs as UID 1000.
USER autostream:autostream

# RTSP, HLS, control UI, RTP, RTCP — documentation only; compose publishes the
# real mappings. The values mirror .env and mediamtx.yml.
EXPOSE 8554 8888 8080 8000/udp 8001/udp

ENTRYPOINT ["/app/entrypoint.sh"]

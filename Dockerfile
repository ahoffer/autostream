FROM bluenviron/mediamtx:1.15.3-ffmpeg

# python3: stream supervisor (stdlib only).
RUN apk add --no-cache python3

# Create non-root user
RUN addgroup -g 1000 autostream && \
    adduser -D -u 1000 -G autostream autostream

WORKDIR /app

# Pre-create the mount points so they exist owned by autostream even when
# the container runs without the bind mounts.
RUN mkdir -p /app/videos /app/state

COPY stream_supervisor.py /app/stream_supervisor.py
COPY streams.py /app/streams.py
COPY ffmpegcmd.py /app/ffmpegcmd.py
COPY statefile.py /app/statefile.py
COPY streamapi.py /app/streamapi.py
COPY filewatch.py /app/filewatch.py
COPY index.html /app/index.html
COPY entrypoint.sh /app/entrypoint.sh

# All app files and dirs owned by autostream (UID/GID 1000).
RUN chown -R autostream:autostream /app

# Switch to non-root user — every process spawned from the entrypoint
# (mediamtx, python3, ffmpeg) runs as UID 1000.
USER autostream:autostream

ENTRYPOINT ["/app/entrypoint.sh"]

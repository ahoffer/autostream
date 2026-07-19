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

COPY stream-video.sh /app/stream-video.sh
COPY stream_supervisor.py /app/stream_supervisor.py
COPY streams.py /app/streams.py
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

FROM bluenviron/mediamtx:1.15.3-ffmpeg

# python3: stream supervisor (stdlib only).
# intel-media-driver: VA-API driver (iHD) for Intel iGPUs.
# mesa-va-gallium: VA-API drivers for AMD APUs (radeonsi/r600) and others.
# onevpl-intel-gpu: the oneVPL GPU runtime h264_qsv dispatches to. Not packaged
# in Alpine 3.22, so it alone comes from edge/community (its deps resolve from
# 3.22); the --repository flag scopes edge to that single transaction. The
# legacy intel-media-sdk does NOT support current Intel generations.
RUN apk add --no-cache python3 intel-media-driver mesa-va-gallium && \
    apk add --no-cache --repository=https://dl-cdn.alpinelinux.org/alpine/edge/community onevpl-intel-gpu

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

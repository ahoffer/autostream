FROM bluenviron/mediamtx:1.15.3-ffmpeg

ARG MEDIAMTX_RTSP_PORT
ARG MEDIAMTX_HLS_PORT
ARG MEDIAMTX_RTP_PORT
ARG MEDIAMTX_RTCP_PORT
ARG STREAM_API_PORT

# Install Python3 for the stream supervisor (stdlib only) and envsubst for
# rendering the Compose-mounted MediaMTX template at container startup.
RUN apk add --no-cache python3 gettext

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

# Copy mediamtx.yml to app directory so it can be modified by non-root user
RUN cp /mediamtx.yml /app/mediamtx.yml

# All app files and dirs owned by autostream (UID/GID 1000); stream-video.sh
# stays root-owned but world-executable.
RUN chown -R autostream:autostream /app && \
    chmod 755 /usr/local/bin/stream-video.sh

# Switch to non-root user — every process spawned from the entrypoint
# (mediamtx, python3, ffmpeg) runs as UID 1000.
USER autostream:autostream

# Expose MediaMTX + API ports (defaults can be overridden via build args)
EXPOSE ${MEDIAMTX_RTSP_PORT} ${MEDIAMTX_HLS_PORT} ${STREAM_API_PORT} \
  ${MEDIAMTX_RTP_PORT}/udp ${MEDIAMTX_RTCP_PORT}/udp

ENTRYPOINT ["/app/entrypoint.sh"]

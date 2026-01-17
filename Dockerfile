FROM bluenviron/mediamtx:1.15.3-ffmpeg

ARG MEDIAMTX_RTSP_PORT
ARG MEDIAMTX_HLS_PORT
ARG MEDIAMTX_RTP_PORT
ARG MEDIAMTX_RTCP_PORT
ARG STREAM_API_PORT

# Install Python3 and dependencies for stream supervisor
RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir inotify-simple --break-system-packages

# Create non-root user
RUN addgroup -g 1000 autostream && \
    adduser -D -u 1000 -G autostream autostream

WORKDIR /app

COPY stream-video.sh /usr/local/bin/stream-video.sh
COPY stream-supervisor.py /app/stream-supervisor.py
COPY entrypoint.sh /app/entrypoint.sh

# Copy mediamtx.yml to app directory so it can be modified by non-root user
RUN cp /mediamtx.yml /app/mediamtx.yml

# Set ownership
RUN chown -R autostream:autostream /app

# Switch to non-root user
USER autostream

# Expose MediaMTX + API ports (defaults can be overridden via build args)
EXPOSE ${MEDIAMTX_RTSP_PORT} ${MEDIAMTX_HLS_PORT} ${STREAM_API_PORT} \
  ${MEDIAMTX_RTP_PORT}/udp ${MEDIAMTX_RTCP_PORT}/udp

ENTRYPOINT ["/app/entrypoint.sh"]

FROM bluenviron/mediamtx:1.15.3-ffmpeg

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

# Expose MediaMTX ports
EXPOSE 8554 8888 8000/udp 8001/udp

ENTRYPOINT ["/app/entrypoint.sh"]

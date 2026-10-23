ARG MEDIAMTX_VERSION=1.21.2

# Rebuild the release with security fixes not yet in its published binary.
FROM golang:1.27.3-alpine3.24 AS mediamtx-build
ARG MEDIAMTX_VERSION
WORKDIR /src
ADD https://codeload.github.com/bluenviron/mediamtx/tar.gz/refs/tags/v${MEDIAMTX_VERSION} /tmp/mediamtx.tar.gz
RUN echo "46677eed3c31fbfd58b1092356f3160125fbd75d3cb7a413fef16c2aa3c13499  /tmp/mediamtx.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/mediamtx.tar.gz --strip-components=1 \
    && go get golang.org/x/net@v0.60.0
# Release archives lack Git metadata, so set the embedded version explicitly.
RUN printf 'v%s' "$MEDIAMTX_VERSION" > internal/core/VERSION \
    && go generate ./internal/servers/hls \
    && case "$(go env GOARCH)" in arm|arm64) go generate ./internal/staticsources/rpicamera ;; esac \
    && CGO_ENABLED=0 go build -trimpath -o /out/mediamtx .

FROM bluenviron/mediamtx:${MEDIAMTX_VERSION}-ffmpeg
COPY --from=mediamtx-build /out/mediamtx /mediamtx

# Apply package fixes released since the base image was built.
# python3: stream supervisor (stdlib only).
RUN apk upgrade --no-cache && apk add --no-cache python3

# Create non-root user
RUN addgroup -g 1000 autostream && \
    adduser -D -u 1000 -G autostream autostream

WORKDIR /app

# Pre-create the mount points so they exist owned by autostream even when
# the container runs without the bind mounts.
RUN mkdir -p /app/videos /app/state /app/hls

# The supervisor and its UI page; the modules import each other by bare name,
# so they stay flat in /app.
COPY src/ /app/
COPY entrypoint.sh /app/entrypoint.sh
# The MediaMTX config is part of the image: editing it means a rebuild.
COPY mediamtx.yml /app/mediamtx.yml

# All app files and dirs owned by autostream (UID/GID 1000).
RUN chown -R autostream:autostream /app

# Switch to non-root user — every process spawned from the entrypoint
# (mediamtx, python3, ffmpeg) runs as UID 1000.
USER autostream:autostream

ENTRYPOINT ["/app/entrypoint.sh"]

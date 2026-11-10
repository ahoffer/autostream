ARG MEDIAMTX_VERSION=1.21.2
ARG RUST_VERSION=1.92.0

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

# Compile the supervisor against musl, the libc of the Alpine runtime image.
# The toolchain image carries the version pinned in rust-toolchain.toml.
FROM rust:${RUST_VERSION}-alpine3.22 AS rust-build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY src/ src/
COPY tests/fixtures/ tests/fixtures/
RUN cargo fetch --locked \
    && cargo build --locked --release --bin autostream

# Run the Rust tests beside the FFmpeg that ships, as the unprivileged user
# the service runs as. This stage is built only by `make test`.
FROM bluenviron/mediamtx:${MEDIAMTX_VERSION}-ffmpeg AS test
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
COPY --from=rust-build /usr/local/rustup /usr/local/rustup
COPY --from=rust-build /usr/local/cargo /usr/local/cargo
RUN apk add --no-cache musl-dev gcc \
    && addgroup -g 1000 autostream \
    && adduser -D -u 1000 -G autostream autostream \
    && chown -R autostream:autostream /usr/local/cargo
COPY --from=rust-build --chown=autostream:autostream /src /src
WORKDIR /src
USER autostream:autostream
RUN cargo test --locked --offline --all-targets

FROM bluenviron/mediamtx:${MEDIAMTX_VERSION}-ffmpeg
COPY --from=mediamtx-build /out/mediamtx /mediamtx

# Apply package fixes released since the base image was built.
RUN apk upgrade --no-cache

# Create non-root user
RUN addgroup -g 1000 autostream && \
    adduser -D -u 1000 -G autostream autostream

WORKDIR /app

# Pre-create the mount points so they exist owned by autostream even when
# the container runs without the bind mounts.
RUN mkdir -p /app/videos /app/mounted /app/hls

# The supervisor is one static binary with the UI page and font embedded.
COPY --from=rust-build /src/target/release/autostream /app/autostream
# The MediaMTX config is part of the image: editing it means a rebuild.
COPY mediamtx.yml /app/mediamtx.yml

# All app files and dirs owned by autostream (UID/GID 1000).
RUN chown -R autostream:autostream /app

# Switch to non-root user: the service and every process it spawns
# (mediamtx, ffmpeg) run as UID 1000.
USER autostream:autostream

ENTRYPOINT ["/app/autostream"]

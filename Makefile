# Typical flow: build -> test -> up. Set COMPOSE_FILE in .env to select
# the networking mode. The image version comes from Cargo.toml.

.PHONY: help build test up

.DEFAULT_GOAL := help

VERSION := $(shell sed -n 's/^version = "\(.*\)"$$/\1/p' Cargo.toml | head -n 1)

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build:  ## Build the binaries from source, then the container image
	cargo build --locked --release
	set -a && . ./.env && docker build -t $$CONTAINER_NAME:$(VERSION) .

test:  ## Run the Rust tests in the image's test stage, beside the shipped ffmpeg
	@# The test stage compiles and runs the suite as UID 1000 on the
	@# production base image; the final image carries neither compiler nor tests.
	set -a && . ./.env && docker build --target test -t $$CONTAINER_NAME:$(VERSION)-test .

# Create mounted/ before compose does: if Docker creates it, it
# is root-owned and the container user (UID 1000) cannot write the state file.
# Compose reads .env itself, COMPOSE_FILE included, and recreates the container
# when the image or service definition changed. Stop with `docker compose down`.
up:  ## Start the stack using COMPOSE_FILE in .env
	mkdir -p mounted
	cargo run --quiet --release --bin composeup
	@. ./.env && printf '\nWeb app: http://localhost:%s/\n' "$$STREAM_API_PORT"

# Typical flow: build -> test -> up. Set COMPOSE_FILE in .env to select
# the networking mode.

.PHONY: help build test up

.DEFAULT_GOAL := help

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build:  ## Build the container image
	set -a && . ./.env && docker build -t $$CONTAINER_NAME:$$VERSION .

test: build  ## Run the unit tests inside the freshly built image
	@# The image carries the app, not the tests; the tests are mounted
	@# read-only, so the suite runs against
	@# the shipped Python and ffmpeg without shipping itself.
	set -a && . ./.env && docker run --rm \
	  -v '$(CURDIR)/tests:/app/tests:ro' \
	  --entrypoint python3 $$CONTAINER_NAME:$$VERSION -B -m unittest discover -s /app/tests -v

# Create mounted/ before compose does: if Docker creates it, it
# is root-owned and the container user (UID 1000) cannot write the state file.
# Compose reads .env itself, COMPOSE_FILE included, and recreates the container
# when the image or service definition changed. Stop with `docker compose down`.
up:  ## Start the stack using COMPOSE_FILE in .env
	mkdir -p mounted
	docker compose up -d

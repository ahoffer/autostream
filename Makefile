# Typical flow: build -> test -> up. MODE=host or MODE=octo-cx selects the
# networking mode in .env; later runs can omit it.

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

# With MODE given, make docker-compose.$(MODE).yml the active COMPOSE_FILE line
# in .env by toggling its two networking mode lines. Create the state
# bind source before compose does: if Docker creates it, it is root-owned and
# the container user (UID 1000) cannot write the state file. Compose reads .env
# itself, COMPOSE_FILE included, and recreates the container when the image or
# the service definition changed. Stopping is a plain `docker compose down`.
up:  ## Start the stack; MODE=host or MODE=octo-cx also selects the networking mode in .env
	@if [ -n '$(MODE)' ]; then \
	  grep -q '^#\?COMPOSE_FILE=docker-compose.yml:docker-compose.$(MODE).yml$$' .env || \
	    { echo "unknown mode '$(MODE)': .env has no COMPOSE_FILE line for docker-compose.$(MODE).yml"; exit 1; }; \
	  sed -i -e 's/^COMPOSE_FILE=/#COMPOSE_FILE=/' \
	    -e 's/^#COMPOSE_FILE=\(docker-compose\.yml:docker-compose\.$(MODE)\.yml\)$$/COMPOSE_FILE=\1/' .env; \
	fi
	@grep '^COMPOSE_FILE=' .env
	mkdir -p state
	docker compose up -d

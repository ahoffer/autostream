# Developer flow: build -> test -> run-host-mode -> run-local-docker -> push.
# Evaluator flow: run -> down. Each start target picks its own networking
# mode. The image version comes from Cargo.toml.

.PHONY: help build test run-host-mode run-local-docker push run pull down

.DEFAULT_GOAL := help

VERSION := $(shell sed -n 's/^version = "\(.*\)"$$/\1/p' Cargo.toml | head -n 1)

# Where `make push` publishes the image and `make run` pulls it. Override it on the command line.
REGISTRY_IMAGE ?= registry.octo-cx-prod.runshiftup.com/octo-cx/common/autostream

HOST_COMPOSE_FILE := docker-compose.yml:docker-compose.host.yml
OCTO_CX_COMPOSE_FILE := docker-compose.yml:docker-compose.octo-cx.yml

SHOW_PAGES = @. ./.env && printf '\nPlay page: http://localhost:%s/\nStream Control: http://localhost:%s/\n' "$$PLAY_PORT" "$$STREAM_API_PORT"

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build:  ## Build the container image
	set -a && . ./.env && docker build -t $$CONTAINER_NAME:$(VERSION) .

test:  ## Run the Rust tests in the image's test stage, beside the shipped ffmpeg
	@# The test stage compiles and runs the suite as UID 1000 on the
	@# production base image; the final image carries neither compiler nor tests.
	set -a && . ./.env && docker build --target test -t $$CONTAINER_NAME:$(VERSION)-test .

# Every start creates mounted/ before Compose does. If Docker creates it, it is
# root-owned and the container user (UID 1000) cannot write the state file.
# Compose recreates the container when the image or service definition changed.

# The composeup helper fills in the host addresses behind the LAN IP shortcut
# in Stream Control, then starts Compose.
run-host-mode:  ## Start the local image in the host's network namespace
	mkdir -p mounted
	COMPOSE_FILE=$(HOST_COMPOSE_FILE) cargo run --quiet --release --bin composeup
	$(SHOW_PAGES)

# The same start as `make run`, from the local image, to test before `make push`.
# The network must already exist, created by the cxedge stack. Without the
# composeup helper, the LAN IP shortcut in Stream Control stays disabled.
run-local-docker:  ## Start the local image on octo-cx-network, as evaluators will
	mkdir -p mounted
	COMPOSE_FILE=$(OCTO_CX_COMPOSE_FILE) VERSION=$(VERSION) docker compose up -d
	$(SHOW_PAGES)

# Requires `docker login registry.octo-cx-prod.runshiftup.com` and a GitLab
# project at the REGISTRY_IMAGE path with its container registry enabled.
push:  ## Tag the built image and push it to the octo-cx registry
	set -a && . ./.env && docker tag $$CONTAINER_NAME:$(VERSION) $(REGISTRY_IMAGE):$(VERSION)
	docker push $(REGISTRY_IMAGE):$(VERSION)

run: pull run-local-docker  ## Pull the image from the octo-cx registry and start it on octo-cx-network

# Tag the pulled image with its local name so Compose finds it.
pull:
	docker pull $(REGISTRY_IMAGE):$(VERSION)
	set -a && . ./.env && docker tag $(REGISTRY_IMAGE):$(VERSION) $$CONTAINER_NAME:$(VERSION)

# Compose finds the container by project name, whichever mode started it.
down:  ## Stop and remove the container
	VERSION=$(VERSION) docker compose down

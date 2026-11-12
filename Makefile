# Typical flow: build -> test -> up. Set COMPOSE_FILE in .env to select
# the networking mode. The image version comes from Cargo.toml.

.PHONY: help build test up run pull run-local push

.DEFAULT_GOAL := help

VERSION := $(shell sed -n 's/^version = "\(.*\)"$$/\1/p' Cargo.toml | head -n 1)

# Where `make push` publishes the image and `make run` pulls it. Override it on the command line.
REGISTRY_IMAGE ?= registry.octo-cx-prod.runshiftup.com/octo-cx/common/autostream

SHOW_PAGES = @. ./.env && printf '\nPlay page: http://localhost:%s/\nStream Control: http://localhost:%s/\n' "$$PLAY_PORT" "$$STREAM_API_PORT"

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build:  ## Build the container image
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
	$(SHOW_PAGES)

# Start the registry image on the consumer's octo-cx-network, with no build and
# no Rust. The network must already exist, created by the cxedge stack. The
# image is tagged with its local name so Compose finds it. Without the
# composeup helper, the LAN IP shortcut in Stream Control stays disabled.
OCTO_CX_COMPOSE_FILE := docker-compose.yml:docker-compose.octo-cx.yml

run: pull run-local  ## Pull the image from the octo-cx registry and start it on octo-cx-network

pull:  ## Pull the image from the octo-cx registry and tag it with its local name
	docker pull $(REGISTRY_IMAGE):$(VERSION)
	set -a && . ./.env && docker tag $(REGISTRY_IMAGE):$(VERSION) $$CONTAINER_NAME:$(VERSION)

run-local:  ## Start the local image on octo-cx-network, as make run does, to test before make push
	mkdir -p mounted
	COMPOSE_FILE=$(OCTO_CX_COMPOSE_FILE) VERSION=$(VERSION) docker compose up -d
	$(SHOW_PAGES)

# Requires `docker login registry.octo-cx-prod.runshiftup.com` and a GitLab
# project at the REGISTRY_IMAGE path with its container registry enabled.
push:  ## Tag the built image and push it to the octo-cx registry
	set -a && . ./.env && docker tag $$CONTAINER_NAME:$(VERSION) $(REGISTRY_IMAGE):$(VERSION)
	docker push $(REGISTRY_IMAGE):$(VERSION)

# Developer flow: image -> test -> run-host-mode -> run-local-docker -> push
# -> package-docker-mode. Testers unpack the package and start it with
# `docker compose up -d`. The image version comes from Cargo.toml.

.PHONY: help image test run-host-mode run-local-docker push package-docker-mode down

.DEFAULT_GOAL := help

VERSION := $(shell sed -n 's/^version = "\(.*\)"$$/\1/p' Cargo.toml | head -n 1)

# Where `make push` publishes the image and the package pulls it. Override it on the command line.
REGISTRY_IMAGE ?= registry.octo-cx-prod.runshiftup.com/octo-cx/common/autostream

HOST_COMPOSE_FILE := docker-compose.yml:docker-compose.host.yml
# Docker mode has one definition, the compose file testers receive.
DOCKER_MODE_COMPOSE := package/compose.yaml

PACKAGE_NAME = autostream-$(VERSION)
# The videos the package carries. Set it empty to ship an empty videos/ folder.
PACKAGE_VIDEOS ?= $(filter-out videos/README.md,$(wildcard videos/*))

SHOW_PAGES = @. ./.env && printf '\nPlay page: http://localhost:%s/\nStream Control: http://localhost:%s/\n' "$$PLAY_PORT" "$$STREAM_API_PORT"

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

image:  ## Build the whole Docker image
	set -a && . ./.env && docker build -t $$CONTAINER_NAME:$(VERSION) .

test:  ## Run the Rust tests in the image's test stage, beside the shipped ffmpeg
	@# The test stage compiles and runs the suite as UID 1000 on the
	@# production base image; the final image carries neither compiler nor tests.
	set -a && . ./.env && docker build --target test -t $$CONTAINER_NAME:$(VERSION)-test .

# Create mounted/ before Compose does. If Docker creates it, it is root-owned
# and the container user (UID 1000) cannot write the state file. The composeup
# helper fills in the host addresses behind the LAN IP shortcut in Stream
# Control, then starts Compose.
run-host-mode:  ## Start the local image in the host's network namespace
	mkdir -p mounted
	COMPOSE_FILE=$(HOST_COMPOSE_FILE) cargo run --quiet --release --bin composeup
	$(SHOW_PAGES)

# The tester's compose file with the local image, to test before `make push`.
# The network must already exist, created by the cxedge stack. Stream state
# lives in a named volume, as for testers, not in mounted/.
run-local-docker:  ## Start the local image on octo-cx-network, as testers will
	set -a && . ./.env && AUTOSTREAM_IMAGE=$$CONTAINER_NAME:$(VERSION) \
		docker compose -f $(DOCKER_MODE_COMPOSE) --project-directory . up -d
	$(SHOW_PAGES)

# Requires `docker login registry.octo-cx-prod.runshiftup.com` and a GitLab
# project at the REGISTRY_IMAGE path with its container registry enabled.
push:  ## Tag the built image and push it to the octo-cx registry
	set -a && . ./.env && docker tag $$CONTAINER_NAME:$(VERSION) $(REGISTRY_IMAGE):$(VERSION)
	docker push $(REGISTRY_IMAGE):$(VERSION)

# Stage the folder in a temporary directory, then zip it into .generated/.
# Its .env is this one plus the registry image the tester's Compose pulls.
package-docker-mode:  ## Zip the docker mode folder for testers into .generated/
	staging=$$(mktemp -d) && folder=$$staging/$(PACKAGE_NAME) && \
	mkdir -p $$folder/videos .generated && \
	cp $(DOCKER_MODE_COMPOSE) package/README.md $$folder/ && \
	cp .env $$folder/.env && \
	printf 'AUTOSTREAM_IMAGE=%s:%s\n' '$(REGISTRY_IMAGE)' '$(VERSION)' >> $$folder/.env && \
	$(if $(PACKAGE_VIDEOS),cp $(PACKAGE_VIDEOS) $$folder/videos/ &&) \
	rm -f .generated/$(PACKAGE_NAME).zip && \
	(cd $$staging && zip -qr $(CURDIR)/.generated/$(PACKAGE_NAME).zip $(PACKAGE_NAME)) && \
	rm -r $$staging
	@printf '\nPackage: .generated/%s.zip\n' '$(PACKAGE_NAME)'

# Compose finds the container by project name, whichever mode started it.
down:  ## Stop and remove the container
	VERSION=$(VERSION) docker compose down

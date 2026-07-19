# Typical flow: build -> compose-up. Install the systemd unit (systemd-install)
# to run the stack on boot.

.PHONY: help build compose-up compose-down compose-logs clean systemd-install systemd-uninstall

.DEFAULT_GOAL := help

# Copy of the last-applied mediamtx config. compose-up compares against it so
# it can force-recreate the container only when the config changed. Direct
# `docker compose up` does not depend on this cache existing.
CONFIG_CACHE := .generated/mediamtx.yml

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---- Docker / Compose ----

build:  ## Build the container image
	@# DOCKER_BUILDKIT=0 forces the legacy builder on purpose: this host has no
	@# buildx component, so the default BuildKit path fails with "buildx is
	@# missing or broken". Drop the override only once buildx is installed.
	set -a && . ./.env && DOCKER_BUILDKIT=0 docker build -t $$CONTAINER_NAME:$$VERSION .

compose-up:  ## Start via docker compose
	@# mediamtx reads its config only at startup, and the single-file bind mount
	@# won't make a running container pick up edits, so force a recreate only
	@# when the config actually changed. Also drop the container if its attached
	@# external-network id has drifted (for example the external network was
	@# recreated), so compose can recreate it instead of failing against a dead id.
	@set -a && . ./.env && \
	mkdir -p $(dir $(CONFIG_CACHE)); \
	if [ ! -f $(CONFIG_CACHE) ] || ! cmp -s mediamtx.yml $(CONFIG_CACHE); then \
		recreate=--force-recreate; else recreate=; fi; \
	cp mediamtx.yml $(CONFIG_CACHE); \
	if docker inspect "$$CONTAINER_NAME" >/dev/null 2>&1; then \
		docker inspect "$$CONTAINER_NAME" \
		  --format '{{range $$n, $$v := .NetworkSettings.Networks}}{{$$n}} {{$$v.NetworkID}}{{"\n"}}{{end}}' \
		  | while read -r net id; do \
		      [ -z "$$net" ] && continue; \
		      cur=$$(docker network inspect "$$net" --format '{{.Id}}' 2>/dev/null || true); \
		      if [ "$$cur" != "$$id" ]; then \
		          echo "Removing $$CONTAINER_NAME: network $$net id drifted ($$id -> $${cur:-missing})"; \
		          docker rm -f "$$CONTAINER_NAME" >/dev/null; break; \
		      fi; \
		    done; \
	fi; \
	docker compose up -d $$recreate

compose-down:  ## Stop the docker compose stack
	docker compose down

compose-logs:  ## Tail docker compose logs
	docker compose logs -f

clean:  ## Remove generated config cache and saved image tarballs
	rm -f *.tar
	rm -rf $(dir $(CONFIG_CACHE))

# ---- systemd service ----
# Install autostream as a systemd service that wraps the docker compose flow.
# Runs as the invoking user (not root) so docker socket access uses the same
# group membership the user already has. Re-run to refresh the unit.

SYSTEMD_UNIT_NAME := autostream.service
SYSTEMD_UNIT_PATH := /etc/systemd/system/$(SYSTEMD_UNIT_NAME)

systemd-install: build  ## Build the image, then install the systemd unit
	@command -v envsubst >/dev/null || { echo "envsubst not found (install gettext-base)"; exit 1; }
	@command -v systemctl >/dev/null || { echo "systemctl not found"; exit 1; }
	@MAKE_BIN=$$(command -v make); \
	[ -n "$$MAKE_BIN" ] || { echo "make not found in PATH"; exit 1; }; \
	REPO_DIR='$(CURDIR)' SERVICE_USER="$${SUDO_USER:-$$(id -un)}" MAKE_BIN="$$MAKE_BIN" \
	  envsubst '$$REPO_DIR $$SERVICE_USER $$MAKE_BIN' < autostream.service.tmpl | \
	  sudo tee $(SYSTEMD_UNIT_PATH) > /dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now $(SYSTEMD_UNIT_NAME)
	@echo "Installed $(SYSTEMD_UNIT_NAME). Check: sudo systemctl status $(SYSTEMD_UNIT_NAME)"

systemd-uninstall:  ## Remove the systemd unit
	-sudo systemctl disable --now $(SYSTEMD_UNIT_NAME)
	sudo rm -f $(SYSTEMD_UNIT_PATH)
	sudo systemctl daemon-reload

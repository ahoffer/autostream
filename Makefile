# Typical flow: build -> compose-up. Install the systemd unit (systemd-install)
# to run the stack on boot.

.PHONY: help build compose-up compose-down compose-logs clean systemd-install systemd-uninstall

.DEFAULT_GOAL := help

# .generated/ holds composeup's copy of the last-applied mediamtx config, kept
# so it can force-recreate the container only when the config changed.

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---- Docker / Compose ----

build:  ## Build the container image
	@# DOCKER_BUILDKIT=0 forces the legacy builder on purpose: this host has no
	@# buildx component, so the default BuildKit path fails with "buildx is
	@# missing or broken". Drop the override only once buildx is installed.
	set -a && . ./.env && DOCKER_BUILDKIT=0 docker build -t $$CONTAINER_NAME:$$VERSION .

compose-up:  ## Start via docker compose (recreates on config change; see composeup)
	./composeup

compose-down:  ## Stop the docker compose stack
	docker compose down

compose-logs:  ## Tail docker compose logs
	docker compose logs -f

clean:  ## Remove the config cache
	rm -rf .generated

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

# Typical flow: down -> build -> up (k8s) or compose-up (Docker Compose).
# For k8s deployments, the image must already be available to the cluster's
# container runtime -- either via a registry or by importing into the node's
# containerd directly. See README for options.

# Absolute path to the repo's videos directory; both the Compose bind-mount and
# the k8s hostPath resolve to this. k8s hostPath cannot be relative.
VIDEOS_DIR := $(CURDIR)/videos
export VIDEOS_DIR

.PHONY: help build save load up down compose-up compose-down compose-logs compose-install compose-uninstall

.DEFAULT_GOAL := help

SYSTEMD_UNIT_NAME := autostream.service
SYSTEMD_UNIT_PATH := /etc/systemd/system/$(SYSTEMD_UNIT_NAME)

help:  ## List available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build:  ## Build the container image
	set -a && . ./.env && DOCKER_BUILDKIT=0 docker build \
		--build-arg MEDIAMTX_RTSP_PORT=$$MEDIAMTX_RTSP_PORT \
		--build-arg MEDIAMTX_HLS_PORT=$$MEDIAMTX_HLS_PORT \
		--build-arg MEDIAMTX_RTP_PORT=$$MEDIAMTX_RTP_PORT \
		--build-arg MEDIAMTX_RTCP_PORT=$$MEDIAMTX_RTCP_PORT \
		--build-arg STREAM_API_PORT=$$STREAM_API_PORT \
		-t $$CONTAINER_NAME:$$VERSION .

save:  ## Save the image to a tar file
	set -a && . ./.env && docker save -o $$CONTAINER_NAME-$$VERSION.tar $$CONTAINER_NAME:$$VERSION

load:  ## Load the tar file into the docker image cache
	set -a && . ./.env && docker load -i $$CONTAINER_NAME-$$VERSION.tar

up:  ## Deploy to Kubernetes
	set -a && . ./.env && \
	TMP_MEDIAMTX=$$(mktemp) && \
	envsubst < mediamtx.yml > $$TMP_MEDIAMTX && \
	kubectl create configmap autostream-config --from-file=mediamtx.yml=$$TMP_MEDIAMTX -n $$K8S_NAMESPACE --dry-run=client -o yaml | kubectl apply -f - && \
	rm -f $$TMP_MEDIAMTX
	set -a && . ./.env && envsubst < k8s.yml | kubectl apply -f -

down:  ## Tear down the Kubernetes deployment
	set -a && . ./.env && envsubst < k8s.yml | kubectl delete -f - --ignore-not-found --wait=true --timeout=30s || true
	@# Force delete any pods stuck in Terminating state
	@stuck_pods=$$(kubectl get pods -n $$K8S_NAMESPACE -l app=autostream -o jsonpath='{.items[?(@.metadata.deletionTimestamp)].metadata.name}'); \
	if [ -n "$$stuck_pods" ]; then \
		echo "Force deleting stuck pods: $$stuck_pods"; \
		kubectl delete pods -n $$K8S_NAMESPACE -l app=autostream --force --grace-period=0; \
	fi
	kubectl delete configmap autostream-config -n $$K8S_NAMESPACE --ignore-not-found

# Docker Compose targets
compose-up:  ## Start via docker compose
	set -a && . ./.env && envsubst < mediamtx.yml > .mediamtx.generated.yml
	docker compose up -d

compose-down:  ## Stop the docker compose stack
	docker compose down
	rm -f .mediamtx.generated.yml

compose-logs:  ## Tail docker compose logs
	docker compose logs -f

# Install autostream as a systemd service that wraps the docker compose flow.
# Runs as the invoking user (not root) so docker socket access uses the same
# group membership the user already has. Re-run to refresh the unit.
compose-install:  ## Install the systemd unit that wraps docker compose
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

compose-uninstall:  ## Remove the systemd unit
	-sudo systemctl disable --now $(SYSTEMD_UNIT_NAME)
	sudo rm -f $(SYSTEMD_UNIT_PATH)
	sudo systemctl daemon-reload

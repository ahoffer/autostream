SHELL := /bin/sh
NAMESPACE := octocx
IMAGE_TAR := autostream.tar

# =============================================================================
# Workflow for deploying to k3s:
#
# 1. Code changes (stream-supervisor.py, entrypoint.sh, etc.):
#      make build      # Build container image with nerdctl
#      make export     # Save image to tar (autostream.tar)
#      make import     # Import tar into k3s (requires sudo password)
#      make down up    # Redeploy to k8s
#
# 2. Config changes (mediamtx.yml only):
#      make down up    # ConfigMap is recreated from local file
#
# Why export/import? nerdctl and k3s use separate containerd namespaces.
# Images built with nerdctl are not visible to k3s until imported.
# =============================================================================

.PHONY: build export import up down

build:
	set -a && . ./.env && DOCKER_BUILDKIT=0 docker build -t $$CONTAINER_NAME:$$VERSION .

export:
	@set -a && . ./.env && \
	IMAGE_ID=$$(nerdctl images --format '{{.ID}}' $$CONTAINER_NAME:$$VERSION) && \
	echo "Exporting $$CONTAINER_NAME:$$VERSION ($$IMAGE_ID) to $(IMAGE_TAR)..." && \
	nerdctl save -o $(IMAGE_TAR) $$IMAGE_ID && \
	echo "Saved to $(IMAGE_TAR)"

import:
	@echo "Importing $(IMAGE_TAR) into k3s (requires sudo)..."
	sudo k3s ctr images import $(IMAGE_TAR)

up:
	kubectl create configmap autostream-config --from-file=mediamtx.yml -n $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	set -a && . ./.env && envsubst < k8s.yml | kubectl apply -f -

down:
	set -a && . ./.env && envsubst < k8s.yml | kubectl delete -f - --ignore-not-found
	kubectl delete configmap autostream-config -n $(NAMESPACE) --ignore-not-found

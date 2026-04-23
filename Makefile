# Typical flow: down -> build -> up (k8s) or compose-up (Docker Compose).
# For k8s deployments, the image must already be available to the cluster's
# container runtime -- either via a registry or by importing into the node's
# containerd directly. See README for options.

# Absolute path to the repo's videos directory; both the Compose bind-mount and
# the k8s hostPath resolve to this. k8s hostPath cannot be relative.
VIDEOS_DIR := $(CURDIR)/videos
export VIDEOS_DIR

.PHONY: build up down compose-up compose-down compose-logs

build:
	set -a && . ./.env && DOCKER_BUILDKIT=0 docker build \
		--build-arg MEDIAMTX_RTSP_PORT=$$MEDIAMTX_RTSP_PORT \
		--build-arg MEDIAMTX_HLS_PORT=$$MEDIAMTX_HLS_PORT \
		--build-arg MEDIAMTX_RTP_PORT=$$MEDIAMTX_RTP_PORT \
		--build-arg MEDIAMTX_RTCP_PORT=$$MEDIAMTX_RTCP_PORT \
		--build-arg STREAM_API_PORT=$$STREAM_API_PORT \
		-t $$CONTAINER_NAME:$$VERSION .

up:
	set -a && . ./.env && \
	TMP_MEDIAMTX=$$(mktemp) && \
	envsubst < mediamtx.yml > $$TMP_MEDIAMTX && \
	kubectl create configmap autostream-config --from-file=mediamtx.yml=$$TMP_MEDIAMTX -n $$K8S_NAMESPACE --dry-run=client -o yaml | kubectl apply -f - && \
	rm -f $$TMP_MEDIAMTX
	set -a && . ./.env && envsubst < k8s.yml | kubectl apply -f -

down:
	set -a && . ./.env && envsubst < k8s.yml | kubectl delete -f - --ignore-not-found --wait=true --timeout=30s || true
	@# Force delete any pods stuck in Terminating state
	@stuck_pods=$$(kubectl get pods -n $$K8S_NAMESPACE -l app=autostream -o jsonpath='{.items[?(@.metadata.deletionTimestamp)].metadata.name}'); \
	if [ -n "$$stuck_pods" ]; then \
		echo "Force deleting stuck pods: $$stuck_pods"; \
		kubectl delete pods -n $$K8S_NAMESPACE -l app=autostream --force --grace-period=0; \
	fi
	kubectl delete configmap autostream-config -n $$K8S_NAMESPACE --ignore-not-found

# Docker Compose targets
compose-up:
	set -a && . ./.env && envsubst < mediamtx.yml > .mediamtx.generated.yml
	docker compose up -d

compose-down:
	docker compose down
	rm -f .mediamtx.generated.yml

compose-logs:
	docker compose logs -f

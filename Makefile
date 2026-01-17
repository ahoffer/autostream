NAMESPACE := octocx
IMAGE_TAR := autostream.tar

# Typical flow: down -> build -> push -> up

.PHONY: build push up down

build:
	set -a && . ./.env && DOCKER_BUILDKIT=0 docker build \
		--build-arg MEDIAMTX_RTSP_PORT=$$MEDIAMTX_RTSP_PORT \
		--build-arg MEDIAMTX_HLS_PORT=$$MEDIAMTX_HLS_PORT \
		--build-arg MEDIAMTX_RTP_PORT=$$MEDIAMTX_RTP_PORT \
		--build-arg MEDIAMTX_RTCP_PORT=$$MEDIAMTX_RTCP_PORT \
		--build-arg STREAM_API_PORT=$$STREAM_API_PORT \
		-t $$CONTAINER_NAME:$$VERSION .

push:
	set -a && . ./.env && k3s-push-image $$CONTAINER_NAME:$$VERSION

up:
	set -a && . ./.env && \
	TMP_MEDIAMTX=$$(mktemp) && \
	envsubst < mediamtx.yml > $$TMP_MEDIAMTX && \
	kubectl create configmap autostream-config --from-file=mediamtx.yml=$$TMP_MEDIAMTX -n $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f - && \
	rm -f $$TMP_MEDIAMTX
	set -a && . ./.env && envsubst < k8s.yml | kubectl apply -f -

down:
	set -a && . ./.env && envsubst < k8s.yml | kubectl delete -f - --ignore-not-found
	kubectl delete configmap autostream-config -n $(NAMESPACE) --ignore-not-found

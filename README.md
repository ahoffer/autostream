# Autostream

Automatic RTSP video streaming server with web-based stream control and hot-reload file discovery.

## Quickstart

1. **Place your videos** in the `videos/` directory:
   ```bash
   cp your-video.mp4 videos/
   ```

2. **Build and deploy**:

   Docker Compose (standalone):
   ```bash
   make build       # Only needed once, or after code changes
   make compose-up
   ```

   Kubernetes (see "Kubernetes Setup" below for the image-availability step):
   ```bash
   make build
   make up
   ```

## How It Works

Autostream automatically:
- **Scans** the `videos/` directory on startup
- **Starts streaming** each video file via RTSP (infinite loop by default)
- **Watches** for new files added at runtime
- **Removes streams** when files are deleted

## Stream URLs

Inside a Kubernetes cluster, videos are reachable through the `autostream`
Service on the ports configured in `.env`:
- **RTSP**: `rtsp://autostream:${MEDIAMTX_RTSP_PORT}/<stream-name>` (default `8554`)
- **HLS**: `http://autostream:${MEDIAMTX_HLS_PORT}/<stream-name>/index.m3u8` (default `8888`)

For Docker Compose access from the host, see "Port Mappings" below.

**Example:** If you add `sailboat.mp4` to the `videos/` directory:
```
rtsp://autostream:8554/sailboat
```

Stream names are sanitized from filenames:
- `My Video (1080p).mp4` → `my_video_1080p`
- `test-stream.mkv` → `test-stream`

## Configuration

Edit `.env` file:

Both `docker-compose.yml` and `k8s.yml` read from `.env` via `envsubst`, so the
same settings drive either deployment.

| Variable | Description | Default |
|----------|-------------|---------|
| `K8S_NAMESPACE` | Kubernetes namespace for deployment | `octocx` |
| `CONTAINER_NAME` | Image name and service/hostname used in stream URLs | `autostream` |
| `VERSION` | Image version tag | `1.0.1` |
| `MAX_VIDEO_BITRATE` | Cap video bitrate (for example `3M`, `5M`) | `2M` |
| `MEDIAMTX_RTSP_PORT` | RTSP listener port | `8554` |
| `MEDIAMTX_HLS_PORT` | HLS HTTP port | `8888` |
| `MEDIAMTX_RTP_PORT` | RTP UDP port | `8000` |
| `MEDIAMTX_RTCP_PORT` | RTCP UDP port | `8001` |
| `STREAM_API_PORT` | Stream control UI/API port | `8080` |

## Commands

```bash
make build        # Build container image with docker
make up           # Deploy to Kubernetes (uses K8S_NAMESPACE from .env)
make down         # Remove from Kubernetes
make compose-up   # Deploy via Docker Compose
make compose-down # Tear down the Docker Compose stack
make compose-logs # Tail Docker Compose logs
```

## Docker Compose

For local development or standalone deployment:

```bash
# Build the image
make build

# Start the service (processes config from .env)
make compose-up

# View logs
make compose-logs

# Stop the service
make compose-down
```

**Note:** Docker Compose uses an external network `octo-cx-network`. Create it first if it doesn't exist:
```bash
docker network create octo-cx-network
```

### Port Mappings (Docker Compose)

| Service | External Port | Internal Port |
|---------|---------------|---------------|
| RTSP    | 9554          | 8554          |
| HLS     | 9322          | 8888          |
| RTP     | 9000/udp      | 8000/udp      |
| RTCP    | 9001/udp      | 8001/udp      |
| Web UI  | 9080          | 8080          |

Access streams at:
- **RTSP**: `rtsp://localhost:9554/<stream-name>`
- **HLS**: `http://localhost:9322/<stream-name>/index.m3u8`
- **Web UI**: `http://localhost:9080`

## Kubernetes Setup

The Deployment, Service, and ConfigMap are all created in `$K8S_NAMESPACE`. The
`videos/` directory in this repo is mounted into the pod via `hostPath`, which
means the cluster node must be able to see the repo's `videos/` directory. On a
single-node cluster where you develop and deploy on the same host, this works
out of the box. For multi-node clusters, replicate the directory to each node
or switch to a different volume type (NFS, PVC, etc.).

### Image availability

`k8s.yml` sets `imagePullPolicy: Never`, so the image must already be present in
the cluster node's container runtime. Many Kubernetes distributions (k3s, RKE2,
kind, minikube) ship an embedded containerd that does not share images with the
host's Docker daemon, so an extra step is required:

- **Registry** (works on any cluster): push the image to a registry the cluster
  can pull from, update `image:` in `k8s.yml` to that reference, and change
  `imagePullPolicy` to `IfNotPresent`.
- **Direct containerd import** (no registry required):
  ```bash
  # k3s
  docker save $CONTAINER_NAME:$VERSION | sudo k3s ctr images import -

  # RKE2
  docker save $CONTAINER_NAME:$VERSION | \
    sudo /var/lib/rancher/rke2/bin/ctr -a /run/k3s/containerd/containerd.sock \
    -n k8s.io images import -

  # kind
  kind load docker-image $CONTAINER_NAME:$VERSION

  # minikube
  minikube image load $CONTAINER_NAME:$VERSION
  ```

### Redeploying code changes

```bash
make build             # Build the image
# ...make image available to the cluster (see above)...
make down && make up   # Redeploy
```

### External access

`k8s.yml` creates a **ClusterIP** Service, so streams are only reachable from
inside the cluster. To expose them outside, either set `spec.type: NodePort`
(ports in the 30000–32767 range by default) or add `hostNetwork: true` to the
pod spec (binds the container ports directly on the node). RTSP and RTP/RTCP
are not HTTP, so most HTTP ingress controllers cannot proxy them directly.

## Supported Formats

Any video format supported by FFmpeg: MP4, MKV, AVI, MOV, WEBM, FLV, TS, etc.

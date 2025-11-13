#!/usr/bin/env bash
set -euo pipefail

RTSP_PORT="${RTSP_PORT:-8554}"

HOSTNAME="${HOSTNAME:-samples}"

echo "Starting RTSP streaming service with MediaMTX"
echo ""
echo "| Video File                     | URL                           |"
echo "|--------------------------------|-------------------------------|"
echo "| lots-ofppeople-in-a-park.mp4   | rtsp://${HOSTNAME}:${RTSP_PORT}/stream0 |"
echo "| peoplewalking_india.mp4        | rtsp://${HOSTNAME}:${RTSP_PORT}/stream1 |"
echo "| usv_vid.mp4                    | rtsp://${HOSTNAME}:${RTSP_PORT}/stream2 |"
echo "| village-2boys.mp4              | rtsp://${HOSTNAME}:${RTSP_PORT}/stream3 |"
echo "| village-3people.mp4            | rtsp://${HOSTNAME}:${RTSP_PORT}/stream4 |"
echo "| village-disappearingpeople.mp4 | rtsp://${HOSTNAME}:${RTSP_PORT}/stream5 |"
echo ""

# Start MediaMTX with the configuration file
exec /mediamtx /mediamtx.yml

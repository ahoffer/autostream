#!/usr/bin/env python3
"""Capture host slab usage and socket queues in every running container."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import time


# Docker exec as root can read slab counters and socket tables without sudo or
# extra container capabilities. Socket tables must be read in each namespace:
# host ss alone misses sockets on Docker bridge networks.
CONTAINER_SNAPSHOT = """
for file in /proc/net/sockstat /proc/net/sockstat6 /proc/net/tcp /proc/net/tcp6 \
            /proc/net/udp /proc/net/udp6 /sys/fs/cgroup/memory.current \
            /sys/fs/cgroup/memory.stat /sys/fs/cgroup/memory.events; do
    echo "--- $file"
    cat "$file" 2>&1
done
echo '--- socket owners (namespace PIDs and socket inodes)'
if command -v ls >/dev/null && command -v grep >/dev/null; then
    ls -l /proc/[0-9]*/fd/* 2>/dev/null | grep 'socket:'
else
    echo 'unavailable: container lacks ls or grep'
fi
echo '--- processes (namespace PIDs)'
for file in /proc/[0-9]*/comm; do
    echo "$file"
    cat "$file" 2>&1
done
"""


def command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return {"exit": result.returncode, "output": result.stdout, "error": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": str(error)}


def read(path):
    try:
        return Path(path).read_text()
    except OSError as error:
        return f"unavailable: {error}"


def socket_owners(processes):
    """Host PIDs let us read same-user fds even if container root cannot."""
    owners = {}
    for line in processes.get("output", "").splitlines()[1:]:
        fields = line.split(None, 1)
        if not fields or not fields[0].isdigit():
            continue
        pid = fields[0]
        sockets = {}
        access_error = None
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except PermissionError as error:
                    access_error = str(error)
                    continue
                except OSError:
                    continue  # fd closed during the sample
                if target.startswith("socket:"):
                    sockets[fd.name] = target
            owners[pid] = {"sockets": sockets}
            if access_error:
                owners[pid]["error"] = access_error
        except OSError as error:
            owners[pid] = {"error": str(error)}
    return owners


def snapshot():
    listing = command(["docker", "ps", "--format", "{{.Names}}"])
    names = listing.get("output", "").splitlines() if listing.get("exit") == 0 else []
    slab = read("/proc/slabinfo")
    if slab.startswith("unavailable:") and names:
        # These counters are host-wide even when read from a container.
        slab = command(["docker", "exec", "-u", "0", names[0], "cat", "/proc/slabinfo"])
        slab = slab.get("output") or slab.get("error", "unavailable")
    buffers = {}
    for line in slab.splitlines():
        fields = line.split()
        if fields and fields[0].startswith("skbuff") and len(fields) >= 15:
            buffers[fields[0]] = {
                "active_objects": int(fields[1]), "allocated_objects": int(fields[2]),
                "object_bytes": int(fields[3]),
                "allocated_slab_bytes": int(fields[14]) * int(fields[5]) * os.sysconf("SC_PAGE_SIZE"),
            }
    def container(name):
        result = command(["docker", "exec", "-u", "0", name, "sh", "-c", CONTAINER_SNAPSHOT])
        result["host_processes"] = command(["docker", "top", name, "-eo", "pid,comm"])
        result["host_socket_owners"] = socket_owners(result["host_processes"])
        return name, result

    with ThreadPoolExecutor(max_workers=4) as pool:
        containers = dict(pool.map(container, names))
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "host_meminfo": read("/proc/meminfo"), "skbuff": buffers,
        "slab_error": None if buffers else slab,
        "host_sockstat": read("/proc/net/sockstat"),
        "host_sockstat6": read("/proc/net/sockstat6"),
        "host_sockets": command(["ss", "-tunapme"]),
        "docker_listing": listing, "containers": containers,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=60, help="seconds between samples")
    parser.add_argument("--output", type=Path, default=Path(".generated/socket-memory.jsonl"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("socket-memory")
    logger.setLevel(logging.INFO)
    # Bound disk usage to approximately 80 MiB, including four old files.
    handler = RotatingFileHandler(args.output, maxBytes=16 * 1024 * 1024, backupCount=4)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        while True:
            started = time.monotonic()
            logger.info(json.dumps(snapshot()))
            if args.once:
                break
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        handler.close()


if __name__ == "__main__":
    main()

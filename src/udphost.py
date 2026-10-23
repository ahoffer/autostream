"""Shared DNS knowledge; status and request handlers never perform lookups."""
import socket


class UdpHostResolver:
    def __init__(self):
        self._resolved = {}

    def refresh(self, hosts):
        results = {}
        for host in set(hosts):
            try:
                socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
                results[host] = True
            except OSError:
                results[host] = False
        self._resolved.update(results)

    def start_blocker(self, host):
        resolved = self._resolved.get(host, False)
        return None if resolved else f'{host} DNS unresolved; waiting'

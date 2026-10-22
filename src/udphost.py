"""Shared DNS knowledge; status and request handlers never perform lookups."""
import socket
from urllib.parse import urlsplit


def udp_destination_host(destination_url):
    """The host a udp:// destination pushes to; None for any other destination."""
    if not destination_url or not destination_url.startswith('udp:'):
        return None
    return urlsplit(destination_url).hostname


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

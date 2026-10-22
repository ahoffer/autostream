"""Format detached output snapshots, grouped by source for the web UI."""
from dataclasses import dataclass

from runtimesettings import RuntimeSettings


@dataclass(frozen=True)
class StatusSnapshot:
    """What the supervisor last published: detached status rows and the settings in force."""
    statuses: tuple
    settings: RuntimeSettings


@dataclass(frozen=True)
class UrlFacts:
    """Start-up facts needed to advertise RTSP and HLS; the host is chosen per request."""
    rtsp_port: int
    hls_port: int

    def public_urls(self, advertised_host, host_header):
        host = advertised_host or host_without_port(host_header) or 'localhost'
        return {'rtsp': f'rtsp://{host}:{self.rtsp_port}', 'hls': f'http://{host}:{self.hls_port}'}


def host_without_port(host_header):
    """The host part of an HTTP Host header; IPv6 literals keep their brackets."""
    if not host_header:
        return None
    if host_header.startswith('['):
        closing = host_header.find(']')
        return host_header[:closing + 1] if closing > 0 else None
    return host_header.split(':', 1)[0] or None


def group_outputs(statuses, public_urls):
    groups = {}
    for status in statuses:
        name, protocol = status['id'].split('/')
        group = groups.setdefault(name, {'name': name, 'filename': status['filename'], 'outputs': {},
                                        'rtsp_url': f'{public_urls["rtsp"]}/{name}',
                                        'hls_url': f'{public_urls["hls"]}/{name}/index.m3u8', 'udp_url': None})
        group['outputs'][protocol] = status
        if protocol == 'udp':
            group['udp_url'] = status['destination']
    return list(groups.values())

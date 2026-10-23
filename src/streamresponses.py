"""Describe detached stream snapshots for the web UI and API, with URLs composed per request."""
from dataclasses import asdict, dataclass

from runtimesettings import RuntimeSettings
from transports import KINDS, KINDS_BY_ID


@dataclass(frozen=True)
class StatusSnapshot:
    """What the supervisor last published: detached status rows, the settings in force, the videos on offer."""
    statuses: tuple
    settings: RuntimeSettings
    videos: tuple


@dataclass(frozen=True)
class UrlFacts:
    """Start-up facts needed to advertise streams; the host is chosen per request."""
    rtsp_port: int
    hls_port: int
    rtmp_port: int
    api_port: int

    def host_for(self, advertised_host, host_header):
        return advertised_host or host_without_port(host_header) or 'localhost'


def host_without_port(host_header):
    """The host part of an HTTP Host header; IPv6 literals keep their brackets."""
    if not host_header:
        return None
    if host_header.startswith('['):
        closing = host_header.find(']')
        return host_header[:closing + 1] if closing > 0 else None
    return host_header.split(':', 1)[0] or None


def describe_streams(statuses, settings, host, url_facts):
    """Status rows plus what each transport kind means and the URLs the consumer registers."""
    rows = []
    for status in statuses:
        kind = KINDS_BY_ID[status['transport']]
        fields = {**asdict(url_facts), 'host': host, 'port': status['port'], 'name': status['name'],
                  'multicast_group': settings.multicast_group or '<multicast group>'}
        rows.append({**status, 'transport_label': kind.label, 'direction': kind.direction,
                     'carries_klv': kind.carries_klv, 'delivery_label': kind.delivery_label,
                     'register': [[label, template.format(**fields)] for label, template in kind.register]})
    return rows


def describe_transports():
    """The kinds table as the UI needs it to offer and explain choices."""
    return [{'id': kind.id, 'label': kind.label, 'direction': kind.direction, 'carries_klv': kind.carries_klv,
             'first_port_setting': kind.first_port_setting, 'random_port': kind.random_port,
             'needs': list(kind.needs)} for kind in KINDS]

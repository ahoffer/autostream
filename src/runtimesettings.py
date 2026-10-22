"""Operator-editable topology and policy, persisted in the state file; immutable in memory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

# cx-search video-streaming's default ingest range; a fresh install hands out ports from it.
DEFAULT_UDP_BASE_PORT = 40000
DEFAULT_UDP_LAST_PORT = 40100


def parse_bitrate(bitrate_text):
    """Bits per second for text such as '3M', '3000K' or '3000000'; empty text means no cap."""
    if not bitrate_text:
        return None
    bitrate_text = bitrate_text.strip().upper()
    if bitrate_text.endswith('M'):
        return int(float(bitrate_text[:-1]) * 1_000_000)
    if bitrate_text.endswith('K'):
        return int(float(bitrate_text[:-1]) * 1_000)
    return int(bitrate_text)


@dataclass(frozen=True)
class RuntimeSettings:
    destination_host: str | None = None                # where the KLV MPEG-TS/UDP feeds are pushed
    udp_base_port: int | None = DEFAULT_UDP_BASE_PORT  # first and last UDP port handed to streams; both None = no range
    udp_last_port: int | None = DEFAULT_UDP_LAST_PORT
    advertised_host: str | None = None                 # host in the RTSP/HLS URLs; None = the host the client used
    max_video_bitrate: str = ''                        # transcode cap such as '2M'; '' = no cap

    @property
    def has_port_range(self):
        return self.udp_base_port is not None

    @property
    def max_video_bps(self):
        return parse_bitrate(self.max_video_bitrate)

    def port_in_range(self, port):
        return self.has_port_range and self.udp_base_port <= port <= self.udp_last_port

    def to_document(self):
        return asdict(self)

    @classmethod
    def from_document(cls, document):
        """Settings saved in the state file; absent keys keep their defaults. Raises ValueError."""
        names = {field.name for field in fields(cls)}
        candidate = cls(**{key: value for key, value in document.items() if key in names})
        error = validation_error(candidate)
        if error:
            raise ValueError(f'Invalid saved settings: {error}')
        return candidate

    @classmethod
    def from_query(cls, query):
        """Settings from a ?key=value form; blank means unset. Returns (settings, None) or (None, error)."""
        text = {field.name: (query.get(field.name) or [''])[0].strip() for field in fields(cls)}
        ports = {}
        for name in ('udp_base_port', 'udp_last_port'):
            if text[name]:
                try:
                    ports[name] = int(text[name])
                except ValueError:
                    return None, f'{name} must be a whole number'
        candidate = cls(destination_host=text['destination_host'] or None,
                        udp_base_port=ports.get('udp_base_port'), udp_last_port=ports.get('udp_last_port'),
                        advertised_host=text['advertised_host'] or None,
                        max_video_bitrate=text['max_video_bitrate'])
        error = validation_error(candidate)
        return (None, error) if error else (candidate, None)


def validation_error(settings):
    """The one rule for a valid settings value: a message, or None when valid."""
    for name in ('destination_host', 'advertised_host'):
        if getattr(settings, name) is not None and not _is_host_text(getattr(settings, name)):
            return f'{name} must be a host name or address'
    base, last = settings.udp_base_port, settings.udp_last_port
    if (base is None) != (last is None):
        return 'udp_base_port and udp_last_port must be set together'
    if base is not None:
        if any(type(port) is not int or not 1 <= port <= 65535 for port in (base, last)):
            return 'UDP ports must be whole numbers from 1 to 65535'
        if base > last:
            return 'udp_last_port is below udp_base_port'
    if not isinstance(settings.max_video_bitrate, str):
        return 'max_video_bitrate must be text such as 2M'
    if settings.max_video_bitrate:
        try:
            bits_per_second = parse_bitrate(settings.max_video_bitrate)
        except ValueError:
            return 'max_video_bitrate must be like 2M, 3000K or 3000000'
        if bits_per_second <= 0:
            return 'max_video_bitrate must be positive'
    return None


def _is_host_text(host):
    return isinstance(host, str) and bool(host) and not any(char.isspace() for char in host) and '/' not in host

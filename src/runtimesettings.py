"""Operator-editable topology and policy, persisted in the state file; immutable in memory."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import ipaddress

HOST_SETTINGS = ('destination_host', 'multicast_group', 'advertised_host')
PORT_SETTINGS = ('udp_first_port', 'rtp_first_port', 'srt_call_first_port', 'srtp_first_port',
                 'srt_listen_first_port', 'tcp_first_port', 'http_first_port')


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
    destination_host: str | None = None   # where push transports send
    multicast_group: str | None = None    # the group the multicast transport sends to
    advertised_host: str | None = None    # host in the URLs the consumer registers; None = the host the client used
    max_video_bitrate: str = ''           # transcode cap such as '2M'; '' = no cap
    srtp_key: str = ''                    # base64 SRTP master key shared with the consumer; '' = unset
    # First port per transport kind; a new stream takes the lowest free port from there.
    # Push kinds must land inside the range the consumer forwards into its container.
    udp_first_port: int = 40000
    rtp_first_port: int = 40020
    srt_call_first_port: int = 40040
    srtp_first_port: int = 40060
    srt_listen_first_port: int = 41000
    tcp_first_port: int = 42000
    http_first_port: int = 43000

    @property
    def max_video_bps(self):
        return parse_bitrate(self.max_video_bitrate)

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
        """Settings from a ?key=value form; blank means unset, or the default for a port.
        Returns (settings, None) or (None, error)."""
        text = {field.name: (query.get(field.name) or [''])[0].strip() for field in fields(cls)}
        values = {name: text[name] or None for name in HOST_SETTINGS}
        values['max_video_bitrate'] = text['max_video_bitrate']
        values['srtp_key'] = text['srtp_key']
        for name in PORT_SETTINGS:
            if not text[name]:
                continue
            try:
                values[name] = int(text[name])
            except ValueError:
                return None, f'{name} must be a whole number'
        candidate = cls(**values)
        error = validation_error(candidate)
        return (None, error) if error else (candidate, None)


def validation_error(settings):
    """The one rule for a valid settings value: a message, or None when valid."""
    for name in HOST_SETTINGS:
        if getattr(settings, name) is not None and not _is_host_text(getattr(settings, name)):
            return f'{name} must be a host name or address'
    if settings.multicast_group is not None:
        try:
            is_multicast = ipaddress.ip_address(settings.multicast_group).is_multicast
        except ValueError:
            is_multicast = False
        if not is_multicast:
            return 'multicast_group must be a multicast address such as 239.1.1.2'
    for name in PORT_SETTINGS:
        port = getattr(settings, name)
        if type(port) is not int or not 1 <= port <= 65535:
            return f'{name} must be a whole number from 1 to 65535'
    if not isinstance(settings.max_video_bitrate, str):
        return 'max_video_bitrate must be text such as 2M'
    if settings.max_video_bitrate:
        try:
            bits_per_second = parse_bitrate(settings.max_video_bitrate)
        except ValueError:
            return 'max_video_bitrate must be like 2M, 3000K or 3000000'
        if bits_per_second <= 0:
            return 'max_video_bitrate must be positive'
    if not isinstance(settings.srtp_key, str) or any(char.isspace() for char in settings.srtp_key):
        return 'srtp_key must be the base64 key text with no spaces'
    return None


def _is_host_text(host):
    return isinstance(host, str) and bool(host) and not any(char.isspace() for char in host) and '/' not in host

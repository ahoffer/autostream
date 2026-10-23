"""The transport kinds a stream can be delivered over: one row per kind, and one rule per row.

Templates take these fields: destination_host, multicast_group, srtp_key, port,
name, rtsp_port, hls_port, rtmp_port, api_port, hls_dir, and host (the
advertised host, chosen per request).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransportKind:
    id: str
    label: str
    direction: str                   # push: we send to the consumer; listen: it connects to us; serve: MediaMTX or files
    carries_klv: bool
    muxer: str                       # ffmpeg output format: rtsp, mpegts, rtp_mpegts or hls
    first_port_setting: str | None   # RuntimeSettings field holding this kind's first port; None when none applies
    needs: tuple                     # RuntimeSettings fields that must be set before the stream can start
    delivery: str                    # template for the ffmpeg output URL or path
    register: tuple                  # (label, template) pairs: what the consumer registers
    extra_output_args: tuple = ()    # per-kind ffmpeg output options, templated
    random_port: bool = False        # takes a random high port instead of one from a first port

    @property
    def takes_port(self):
        return self.first_port_setting is not None or self.random_port

    @property
    def delivery_label(self):
        if self.direction == 'push':
            return 'sends to'
        if self.direction == 'listen':
            return 'listens on'
        return 'writes to' if self.muxer == 'hls' else 'publishes to'


KINDS = (
    TransportKind('rtsp', 'RTSP / HLS / RTMP via MediaMTX', 'serve', False, 'rtsp', None, (),
                  'rtsp://localhost:{rtsp_port}/{name}',
                  (('rtsp', 'rtsp://{host}:{rtsp_port}/{name}'),
                   ('hls', 'http://{host}:{hls_port}/{name}/index.m3u8'),
                   ('rtmp', 'rtmp://{host}:{rtmp_port}/{name}'))),
    TransportKind('udp', 'UDP', 'push', True, 'mpegts', 'udp_first_port', ('destination_host',),
                  'udp://{destination_host}:{port}?pkt_size=1316',
                  (('register', 'udp://0.0.0.0:{port}'),)),
    # The consumer's redirect service joins the group on its host network, so
    # any port works there; a random one proves that and keeps clear of the
    # unicast range the consumer forwards into its container.
    TransportKind('multicast', 'Multicast', 'push', True, 'mpegts', None, ('multicast_group',),
                  'udp://{multicast_group}:{port}?pkt_size=1316&ttl=1',
                  (('register', 'udp://{multicast_group}:{port}'),), random_port=True),
    TransportKind('rtp', 'RTP / TS', 'push', True, 'rtp_mpegts', 'rtp_first_port', ('destination_host',),
                  'rtp://{destination_host}:{port}',
                  (('register', 'rtp://0.0.0.0:{port}'),)),
    TransportKind('srt_listen', 'SRT listen', 'listen', True, 'mpegts', 'srt_listen_first_port', (),
                  'srt://0.0.0.0:{port}?mode=listener',
                  (('register', 'srt://{host}:{port}'),)),
    TransportKind('srt_call', 'SRT call', 'push', True, 'mpegts', 'srt_call_first_port', ('destination_host',),
                  'srt://{destination_host}:{port}',
                  (('register', 'srt://0.0.0.0:{port}?mode=listener'),)),
    TransportKind('tcp_listen', 'TCP listen', 'listen', True, 'mpegts', 'tcp_first_port', (),
                  'tcp://0.0.0.0:{port}?listen=1',
                  (('register', 'tcp://{host}:{port}'),)),
    TransportKind('http', 'HTTP TS', 'listen', True, 'mpegts', 'http_first_port', (),
                  'http://0.0.0.0:{port}',
                  (('register', 'http://{host}:{port}'),),
                  ('-listen', '1')),
    TransportKind('hls', 'HLS (TS segments)', 'serve', True, 'hls', None, (),
                  '{hls_dir}/{name}/index.m3u8',
                  (('register', 'http://{host}:{api_port}/hls/{name}/index.m3u8'),)),
    TransportKind('srtp', 'SRTP', 'push', True, 'rtp_mpegts', 'srtp_first_port', ('destination_host', 'srtp_key'),
                  'srtp://{destination_host}:{port}',
                  (('register', 'srtp://0.0.0.0:{port}'),),
                  ('-srtp_out_suite', 'AES_CM_128_HMAC_SHA1_80', '-srtp_out_params', '{srtp_key}')),
)

# Lookup by id: the API and the state file name kinds by id.
KINDS_BY_ID = {kind.id: kind for kind in KINDS}

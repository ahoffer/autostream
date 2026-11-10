#!/usr/bin/env python3
"""Record what the Python implementation produces, as parity fixtures for the Rust port.

Run from the project root: python3 -B tests/fixtures/generate.py
Writes tests/fixtures/*.json. Temporary directory paths are replaced by the
placeholders {videos_dir} and {hls_dir}; the multicast port is pinned to the
lowest candidate so the output is deterministic.
"""
import json
import sys
import tempfile
import threading
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]

import ffmpegcmd  # noqa: E402
from mediastream import Delivery  # noqa: E402
from runtimesettings import PORT_SETTINGS, RuntimeSettings, parse_bitrate  # noqa: E402
from statefile import StreamStateStore  # noqa: E402
from streamapi import StreamHandler, _execute_request, parse_creation, parse_settings  # noqa: E402
from streamregistry import StreamRegistry, seeded_stream_name  # noqa: E402
from streamresponses import StatusSnapshot, UrlFacts, describe_streams, describe_transports, host_without_port  # noqa: E402
from streamsupport import FakeProcess, FakeResolver, TEST_PROFILE, TEST_SETTINGS, inventory  # noqa: E402
from stream_supervisor import run_loop  # noqa: E402
from testsupport import make_config  # noqa: E402
from transports import KINDS  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

OUT = ROOT / 'tests' / 'fixtures'
URL_FACTS = UrlFacts(8554, 8888, 1935, 8080)


def write(name, data):
    (OUT / name).write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')


def placeholders(value, replacements):
    """Replace temporary paths in every string of a JSON-like value."""
    if isinstance(value, str):
        for actual, placeholder in replacements:
            value = value.replace(actual, placeholder)
        return value
    if isinstance(value, dict):
        return {placeholders(key, replacements): placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [placeholders(item, replacements) for item in value]
    return value


def outcome(call, *args):
    try:
        return {'value': call(*args), 'error': None}
    except (ValueError, OverflowError) as error:
        return {'value': None, 'error': str(error)}


def settings_fixture():
    queries = [
        {'destination_host': [' receiver '], 'multicast_group': [''], 'advertised_host': [''],
         'max_video_bitrate': [''], 'srtp_key': [''], **{name: [''] for name in PORT_SETTINGS}},
        {'udp_first_port': ['abc']}, {'udp_first_port': ['70000']}, {'http_first_port': ['45000']}, {},
        {'multicast_group': ['10.0.0.1']}, {'multicast_group': ['ff02::1']}, {'multicast_group': ['239.01.1.1']},
        {'max_video_bitrate': ['3000K']}, {'max_video_bitrate': ['2.5m']}, {'max_video_bitrate': ['bad']},
        {'max_video_bitrate': ['0']}, {'max_video_bitrate': ['-2M']}, {'max_video_bitrate': ['3.5']},
        {'destination_host': ['a b']}, {'destination_host': ['a/b']}, {'destination_host': ['[fe80::1]']},
        {'srtp_key': ['has space']}, {'srtp_key': ['a2V5']},
        {'advertised_host': ['publisher', 'second']}, {'tcp_first_port': [' 42001 ']}, {'udp_first_port': ['1.5']},
        {'udp_first_port': ['0']}, {'udp_first_port': ['65535']}, {'udp_first_port': ['+5']},
    ]
    documents = [
        {}, {'udp_first_port': 0}, {'tcp_first_port': '42000'}, {'multicast_group': '10.0.0.1'},
        {'multicast_group': 'group'}, {'max_video_bitrate': 'bad'}, {'max_video_bitrate': '0'},
        {'destination_host': 'a b'}, {'advertised_host': 7}, {'srtp_key': 'has space'}, {'srtp_key': ''},
        {'multicast_group': None}, {'ignored': 1, 'udp_first_port': 50000}, {'udp_first_port': 40000.0},
        {'udp_first_port': True}, {'destination_host': None, 'max_video_bitrate': '2M'},
        {'max_video_bitrate': None}, {'srtp_key': None}, {'udp_first_port': 'x', 'destination_host': 'a b'},
        {'destination_host': '', 'advertised_host': ''}, {'multicast_group': 'ff02::1'},
        TEST_SETTINGS.to_document(), {'max_video_bitrate': '3000K', 'udp_first_port': 65535},
    ]
    bitrates = ['3M', '3000K', '3000000', '2.5M', ' 2m ', '', '0', '-1K', '1e1M']
    return {
        'defaults': RuntimeSettings().to_document(),
        'from_query': [{'query': query, **_query_outcome(query)} for query in queries],
        'from_document': [{'document': document, **_document_outcome(document)} for document in documents],
        'parse_bitrate': [{'text': text, **outcome(parse_bitrate, text)} for text in bitrates],
    }


def _query_outcome(query):
    settings, error = RuntimeSettings.from_query(query)
    return {'settings': settings.to_document() if settings else None, 'error': error}


def _document_outcome(document):
    try:
        settings = RuntimeSettings.from_document(document)
    except ValueError as error:
        return {'settings': None, 'max_video_bps': None, 'error': str(error)}
    return {'settings': settings.to_document(), 'max_video_bps': settings.max_video_bps, 'error': None}


def query_fixture():
    settings_queries = [
        'name=ships', 'name=bad%20name', 'name=', 'file=other.mp4', 'file=', 'file=../etc/passwd', 'file=.hidden',
        'transport=udp', 'transport=pigeon', 'transport=', 'loop=3', 'loop=-1', 'loop=-2', 'loop=x', 'loop=',
        'mode=transcode', 'mode=passthrough', 'mode=', 'mode=x', 'gop=auto', 'gop=12', 'gop=0', 'gop=x', 'gop=12.5',
        'fps=auto', 'fps=15', 'fps=29.97', 'fps=0.5', 'fps=121', 'fps=120', 'fps=1', 'fps=nan', 'fps=inf', 'fps=x',
        'fps=1e1', 'loop=1&loop=2', 'name=+ships+', 'fps=+15+', 'loop=+3+', 'gop=+12+', '', 'unknown=1',
        'name=ships&file=other.mp4&transport=tcp_listen&loop=2&mode=transcode&gop=auto&fps=24',
        'name=%E2%9C%93', 'file=a%2Fb.mp4', 'transport=udp&transport=rtp', 'gop=-1', 'gop=', 'fps=',
        'name=' + 'a' * 64, 'name=' + 'a' * 65, 'name=-abc', 'name=_abc', 'name=abc-',
    ]
    creation_queries = [
        'name=boats&file=a.mp4&transport=udp', 'name=&file=a.mp4&transport=udp', 'name=x&file=&transport=udp',
        'name=x&file=a.mp4&transport=pigeon', 'name=x&file=a.mp4&transport=udp&fps=nan',
        'name=x&file=a.mp4&transport=udp&start=0', 'name=x&file=a.mp4&transport=udp&start=',
        'name=x&file=a.mp4&transport=udp&start=1', 'name=x&file=a.mp4&transport=udp&start=no',
        'name=x&file=a.mp4&transport=udp&mode=transcode&loop=0&gop=auto&fps=12',
        'name=bad%20name&file=a.mp4&transport=udp', 'name=x&file=a.mp4&transport=udp&loop=x',
        'file=a.mp4&transport=udp', 'name=x&file=a.mp4&transport=udp&fps=auto', 'name=x&file=.hidden&transport=udp',
        'name=x&file=..%2Fa.mp4&transport=udp', 'name=x&file=a.mp4&transport=rtsp&start=0&loop=1',
    ]
    hosts = ['operator-console:9080', 'operator-console', '[::1]:9080', '[::1]', '[::1', ':9080', '', None, 'a:b:c']
    return {
        'parse_settings': [{'query': query, **_pair(parse_settings(parse_qs(query, keep_blank_values=True)))}
                           for query in settings_queries],
        'parse_creation': [{'query': query, **_pair(parse_creation(parse_qs(query, keep_blank_values=True)))}
                           for query in creation_queries],
        'host_without_port': [{'host_header': host, 'host': host_without_port(host)} for host in hosts],
        'host_for': [{'advertised_host': advertised, 'host_header': header, 'host': URL_FACTS.host_for(advertised, header)}
                     for advertised in (None, 'publisher-host') for header in ('operator-console:9080', None, '')],
    }


def _pair(result):
    value, error = result
    return {'value': value, 'error': error}


def probe_fixture():
    """Real ffprobe reports for the sample videos, plus synthetic reports for the parser's corner cases."""
    import subprocess
    cases = []
    for path in sorted(ROOT.glob('videos/*')):
        if path.suffix.lower() not in ('.mp4', '.avi', '.ts', '.mpg'):
            continue
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'stream=index,codec_type,codec_name,pix_fmt,extradata_size,bit_rate,avg_frame_rate:format=bit_rate',
             '-of', 'json', str(path)], capture_output=True, text=True, timeout=30)
        cases.append({'name': path.name, 'returncode': result.returncode, 'stdout': result.stdout})
    synthetic = {
        'out_of_order_and_unknown_bitrate': {'streams': [
            {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac', 'extradata_size': 5},
            {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'bit_rate': 'N/A', 'avg_frame_rate': '0/0'}],
            'format': {'bit_rate': 'N/A'}},
        'audio_only': {'streams': [{'index': 0, 'codec_type': 'audio', 'codec_name': 'aac', 'extradata_size': 5}],
                       'format': {'bit_rate': '128000'}},
        'adts_aac_is_not_copyable': {'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'avg_frame_rate': '30000/1001', 'bit_rate': '1000'},
            {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac', 'extradata_size': 0}], 'format': {}},
        'mp3_audio': {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'mpeg2video', 'avg_frame_rate': '25/1'},
                                  {'index': 1, 'codec_type': 'audio', 'codec_name': 'mp3', 'extradata_size': 9}]},
        'no_streams': {'streams': []},
        'empty_report': {},
        'missing_names_and_bad_rates': {'streams': [
            {'index': 0, 'codec_type': 'video', 'avg_frame_rate': '30', 'bit_rate': '12abc'},
            {'index': 2, 'codec_type': 'data'}, {'index': 1, 'codec_type': 'data', 'codec_name': 'klv'}],
            'format': {'bit_rate': 123}},
        'negative_and_zero_rates': {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'hevc', 'avg_frame_rate': '-30/1'}]},
        'rate_with_zero_denominator': {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'hevc', 'avg_frame_rate': '30/0'}]},
        'two_video_tracks': {'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'avg_frame_rate': '24/1', 'bit_rate': '3000000', 'pix_fmt': 'yuv420p'},
            {'index': 1, 'codec_type': 'video', 'codec_name': 'mjpeg', 'avg_frame_rate': '1/1', 'pix_fmt': 'yuvj420p'}],
            'format': {'bit_rate': '3100000'}},
        'no_index_fields': {'streams': [{'codec_type': 'audio', 'codec_name': 'aac', 'extradata_size': 2},
                                        {'codec_type': 'video', 'codec_name': 'h264'}]},
    }
    for name, report in synthetic.items():
        cases.append({'name': name, 'returncode': 0, 'stdout': json.dumps(report)})
    cases.append({'name': 'invalid_json', 'returncode': 0, 'stdout': '{not json'})
    cases.append({'name': 'probe_failed', 'returncode': 1, 'stdout': ''})
    for case in cases:
        fake = SimpleNamespace(returncode=case['returncode'], stdout=case['stdout'])
        with patch('ffmpegcmd.subprocess.run', return_value=fake):
            profile = ffmpegcmd.probe_file('unused.mp4')
        case['profile'] = profile_document(profile)
    return cases


def profile_document(profile):
    return {'video_codecs': list(profile.video_codecs) if profile.video_codecs is not None else None,
            'audio_codecs': list(profile.audio_codecs) if profile.audio_codecs is not None else None,
            'audio_copyable': profile.audio_copyable, 'pix_fmt': profile.pix_fmt, 'bitrate_bps': profile.bitrate_bps,
            'frame_rate': profile.frame_rate,
            'data_codecs': list(profile.data_codecs) if profile.data_codecs is not None else None}


def profile_from_document(document):
    return ffmpegcmd.MediaProbeResult(
        tuple(document['video_codecs']) if document['video_codecs'] is not None else None,
        tuple(document['audio_codecs']) if document['audio_codecs'] is not None else None,
        document['audio_copyable'], document['pix_fmt'], document['bitrate_bps'], document['frame_rate'],
        tuple(document['data_codecs']) if document['data_codecs'] is not None else None)


PROFILES = {
    'h264_aac_klv': profile_document(TEST_PROFILE),
    'h264_adts_aac_klv': profile_document(ffmpegcmd.MediaProbeResult(('h264',), ('aac',), False, 'yuv420p', 4000000, 30., ('klv',))),
    'mjpeg_silent': profile_document(ffmpegcmd.MediaProbeResult(('mjpeg',), (), True, 'yuvj420p', 4000000, 30.)),
    'hevc_ntsc': profile_document(ffmpegcmd.MediaProbeResult(('hevc',), ('aac',), True, 'yuv420p', 1500000, 30000 / 1001, ())),
    'mpeg2_mp2_film': profile_document(ffmpegcmd.MediaProbeResult(('mpeg2video',), ('mp2',), False, 'yuv420p', 8000000, 24000 / 1001, ())),
    'vp9_unknown_rate': profile_document(ffmpegcmd.MediaProbeResult(('vp9',), (), True, 'yuv420p', None, None, ())),
    'half_frame_rates': profile_document(ffmpegcmd.MediaProbeResult(('h264',), (), True, 'yuv420p', 1000000, 24.5, ())),
    'round_up_rate': profile_document(ffmpegcmd.MediaProbeResult(('h264',), (), True, 'yuv420p', 1000000, 25.5, ())),
    'two_video_tracks': profile_document(ffmpegcmd.MediaProbeResult(('h264', 'mjpeg'), ('aac',), True, 'yuv420p', 3000000, 24., ())),
    'no_video': profile_document(ffmpegcmd.MediaProbeResult((), ('aac',), True, None, 128000, None, ())),
    'unknown': profile_document(ffmpegcmd.UNKNOWN_PROFILE),
}

DELIVERIES = {
    'rtsp_direct': {'url': 'rtsp://localhost:8554/boats', 'muxer': 'rtsp', 'carries_klv': False},
    'mediamtx_srt': {'url': 'srt://127.0.0.1:8890?streamid=publish:boats', 'clamp_data_dts': False},
    'udp': {'url': 'udp://receiver:40000?pkt_size=1316', 'resolve_host': 'receiver'},
    'rtp': {'url': 'rtp://receiver:40020', 'muxer': 'rtp_mpegts'},
    'http': {'url': 'http://0.0.0.0:43000', 'extra_args': ['-listen', '1'], 'client_bound': True},
    'srtp': {'url': 'srtp://receiver:40060', 'muxer': 'rtp_mpegts',
             'extra_args': ['-srtp_out_suite', 'AES_CM_128_HMAC_SHA1_80', '-srtp_out_params', 'a2V5']},
    'hls': {'url': '/app/hls/boats/index.m3u8', 'muxer': 'hls'},
    'blocked': {'url': None, 'blocker': 'no free port'},
}

CHOICES = [
    {'loop_count': -1},
    {'loop_count': 0, 'mode': 'transcode', 'frame_rate': 12.0, 'gop_frames': 7},
    {'loop_count': 2, 'mode': 'passthrough', 'frame_rate': None, 'gop_frames': None},
    {'loop_count': 1, 'mode': None, 'frame_rate': 29.97, 'gop_frames': None},
]


def prepare_fixture():
    cases = []
    for profile_name, profile_doc in PROFILES.items():
        profile = profile_from_document(profile_doc)
        for delivery_name, delivery_doc in DELIVERIES.items():
            delivery = Delivery(**{key: tuple(value) if isinstance(value, list) else value for key, value in delivery_doc.items()})
            caps = (2_000_000, None) if profile_name in ('h264_aac_klv', 'hevc_ntsc', 'unknown') else (2_000_000,)
            for choices in CHOICES:
                for cap in caps:
                    prepared = ffmpegcmd._command_for_profile('/videos/boats.mp4', choices, delivery, profile, cap)
                    cases.append({'profile': profile_name, 'delivery': delivery_name, 'choices': choices,
                                  'max_video_bps': cap, 'prepared': {**prepared, 'argv': list(prepared['argv'])}})
    specs = []
    for frame_rate in (None, 12.0, 29.97, 23.976, 59.94, 100.5, 1.0, 120.0, 0.1):
        spec = ffmpegcmd.FFmpegCommandSettings(-1, 30, frame_rate, False, True, '')
        specs.append({'frame_rate': frame_rate, 'argv': ffmpegcmd.build_argv('/videos/a.mp4', spec, Delivery('udp://r:1'))})
    gops = [{'frame_rate': rate, 'gop_frames': ffmpegcmd.natural_gop_frames(rate)}
            for rate in (None, 0, 0.4, 0.5, 1.5, 2.5, 24.5, 25.5, 29.97, 30000 / 1001, 59.94, 120.0)]
    return {'profiles': PROFILES, 'deliveries': DELIVERIES, 'cases': cases, 'frame_rate_text': specs, 'natural_gop_frames': gops}


class Cleanups:
    def __init__(self):
        self.calls = []

    def addCleanup(self, call, *args, **kwargs):
        self.calls.append((call, args, kwargs))

    def run(self):
        for call, args, kwargs in reversed(self.calls):
            call(*args, **kwargs)


def build_registry(root, settings, state_name='streams_config.json'):
    config = make_config(videos_dir=root / 'videos', state_path=root / 'mounted' / state_name, hls_dir=root / 'hls')
    config.videos_dir.mkdir(exist_ok=True)
    config.hls_dir.mkdir(exist_ok=True)
    store = StreamStateStore(config.state_path)
    if settings is not None:
        store.save_settings(settings.to_document())
    resolver = FakeResolver()
    import logging
    registry = StreamRegistry(config, store, resolver, lambda name: logging.Logger(name))
    registry.load_persisted_state()
    return registry, store, resolver, config


def streams_fixture(root):
    FakeProcess.runs = []
    patches = [patch('mediastream.FFmpegProcess', FakeProcess), patch('ffmpegcmd.probe_file', return_value=TEST_PROFILE),
               patch('streamregistry.random.choice', lambda candidates: candidates[0])]
    for patcher in patches:
        patcher.start()
    try:
        registry, store, resolver, config = build_registry(root, TEST_SETTINGS)
        for filename in ('boats.mp4', 'cargo ship.v2.avi', 'truck_klv_30s.ts'):
            (config.videos_dir / filename).touch()
        videos = [config.videos_dir / 'boats.mp4', config.videos_dir / 'cargo ship.v2.avi', config.videos_dir / 'truck_klv_30s.ts']
        registry.supervise(inventory(*videos, creates=videos))
        for kind in KINDS:
            assert registry.create_stream(kind.id, 'boats.mp4', kind.id, {})[0]
            assert registry.create_stream(kind.id + '-t', 'truck_klv_30s.ts', kind.id,
                                          {'mode': 'transcode', 'frame_rate': 15.0, 'loop_count': 2, 'gop_frames': 20})[0]
        assert registry.create_stream('stopped-udp', 'cargo ship.v2.avi', 'udp', {'mode': 'transcode'}, start=False)[0]
        assert registry.create_stream('ghost', 'later.mp4', 'udp', {})[0]
        assert registry.create_stream('once', 'boats.mp4', 'rtsp', {'loop_count': 0})[0]
        assert registry.update_stream('udp-t', {'name': 'udp-renamed', 'loop_count': 3})
        crashed = FakeProcess.runs[0]
        crashed.tail = 'failure detail'
        crashed.finish(1)
        FakeProcess.runs[-1].finish(0)
        registry.supervise()
        statuses = registry.status()
        described = describe_streams(statuses, registry.settings(), 'publisher', URL_FACTS)
        document = store.load()
        state_text = config.state_path.read_text(encoding='utf-8')

        waiting_registry, _, _, waiting_config = build_registry(root, RuntimeSettings(), 'waiting.json')
        (waiting_config.videos_dir / 'boats.mp4').touch()
        for transport in ('udp', 'multicast', 'srtp', 'srt_listen', 'rtp', 'srt_call'):
            assert waiting_registry.create_stream(transport, 'boats.mp4', transport, {})[0]
        waiting = waiting_registry.status()
        waiting_described = describe_streams(waiting, waiting_registry.settings(), 'localhost', URL_FACTS)

        port_registry, _, _, _ = build_registry(root, TEST_SETTINGS, 'ports.json')
        port_rows = []
        for index in range(3):
            for transport in ('udp', 'srt_listen', 'tcp_listen'):
                assert port_registry.create_stream(f'{transport}-{index}', 'boats.mp4', transport, {})[0]
        assert port_registry.delete_stream('udp-1')
        assert port_registry.create_stream('udp-again', 'boats.mp4', 'udp', {})[0]
        assert port_registry.update_stream('srt_listen-2', {'transport': 'udp'})
        port_rows = [(row['name'], row['transport'], row['port']) for row in port_registry.status()]

        seeded = [{'filename': filename, 'taken': list(taken), 'name': seeded_stream_name(filename, taken)} for filename, taken in (
            ('truck_klv_30s.ts', ()), ('--my clip (final).mp4', ()), ('a.ts', ('a', 'a-2')), ('-.mp4', ()),
            ('x' * 80 + '.mp4', ('x' * 64,)), ('rolling boats.v2.avi', ()), ('.hidden.mp4', ()), ('a.tar.gz', ()),
            ('über cool.mp4', ()), ('_under_.mp4', ()), ('name', ('name',)), ('CAPS.MP4', ()))]
        replacements = [(str(config.videos_dir), '{videos_dir}'), (str(config.hls_dir), '{hls_dir}')]
        return placeholders({
            'settings': TEST_SETTINGS.to_document(), 'videos': list(registry.videos()),
            'statuses': statuses, 'described': described, 'state_document': document, 'state_text': state_text,
            'commands': {run.argv[-1]: list(run.argv) for run in FakeProcess.runs if run.argv},
            'waiting_statuses': waiting, 'waiting_described': waiting_described,
            'port_allocation': port_rows, 'seeded_names': seeded,
        }, replacements)
    finally:
        for patcher in patches:
            patcher.stop()


def state_fixture(root):
    record = {'filename': 'boats.mp4', 'transport': 'udp', 'port': 40000, 'enabled': True,
              'mode': None, 'loop_count': -1, 'frame_rate': None, 'gop_frames': None}
    invalid = [
        ({'streams': []}, 'Invalid state document'), ([], 'Invalid state document'),
        ({'streams': {'bad name': record}}, 'Invalid saved stream'), ({'streams': {'boats': 'x'}}, 'Invalid saved stream'),
        ({'streams': {'boats': {**record, 'filename': ''}}}, 'must name a file'),
        ({'streams': {'boats': {**record, 'transport': 'pigeon'}}}, 'unknown transport'),
        ({'streams': {'boats': {**record, 'port': 70000}}}, 'invalid port'),
        ({'streams': {'boats': {**record, 'port': 40000.0}}}, 'invalid port'),
        ({'streams': {'boats': {**record, 'port': '40000'}}}, 'invalid port'),
        ({'streams': {'boats': {**record, 'enabled': 1}}}, 'no enabled flag'),
        ({'streams': {'boats': {**record, 'mode': 'copy'}}}, 'invalid mode'),
        ({'streams': {'boats': {**record, 'loop_count': -3}}}, 'invalid loop count'),
        ({'streams': {'boats': {**record, 'loop_count': 1.0}}}, 'invalid loop count'),
        ({'streams': {'boats': {**record, 'loop_count': True}}}, 'invalid loop count'),
        ({'streams': {'boats': {**record, 'frame_rate': 0.5}}}, 'invalid frame_rate'),
        ({'streams': {'boats': {**record, 'frame_rate': 121}}}, 'invalid frame_rate'),
        ({'streams': {'boats': {**record, 'frame_rate': '12'}}}, 'invalid frame_rate'),
        ({'streams': {'boats': {**record, 'gop_frames': 0}}}, 'invalid gop_frames'),
        ({'streams': {'boats': {**record, 'gop_frames': 2.5}}}, None),
        ({'streams': {'boats': {**record, 'frame_rate': 120, 'gop_frames': 100000}}}, None),
        ({'streams': {'boats': {'filename': 'a.mp4', 'transport': 'rtsp', 'enabled': False, 'loop_count': 0}}}, None),
        ({'streams': {}, 'settings': {'udp_first_port': 0}}, 'udp_first_port'),
        ({'streams': {}, 'settings': []}, 'Invalid saved settings'),
        ({'streams': {}, 'settings': {}, 'extra': {'kept': True}}, None),
    ]
    cases = []
    for index, (document, message) in enumerate(invalid):
        path = root / f'state-{index}.json'
        path.write_text(json.dumps(document))
        try:
            loaded = StreamStateStore(path).load()
            cases.append({'document': document, 'error': None, 'loaded': loaded})
        except ValueError as error:
            cases.append({'document': document, 'error': str(error).replace(str(path), '{path}'), 'loaded': None})
        except AttributeError as error:
            # Python crashes on a top-level array instead of refusing it; the Rust port
            # reports the invalid-document message for it.
            cases.append({'document': document, 'error': 'Invalid state document (Python: AttributeError)', 'loaded': None})
        assert message is None or message in cases[-1]['error'], (document, cases[-1])
    path = root / 'sequence.json'
    store = StreamStateStore(path)
    store.save_stream('boats', record)
    store.save_settings(RuntimeSettings(destination_host='receiver').to_document())
    store.save_stream('boats-srt', {**record, 'transport': 'srt_listen', 'port': 41000})
    store.save_stream('boats', {**record, 'enabled': False, 'frame_rate': 12.0, 'gop_frames': 7, 'mode': 'transcode'})
    store.save_stream('third', {**record, 'transport': 'rtsp', 'port': None})
    store.rename_stream('boats', 'ships')
    store.delete_stream('boats-srt')
    sequence_text = path.read_text(encoding='utf-8')
    extra_path = root / 'extra.json'
    extra_path.write_text(json.dumps({'extra': 1, 'streams': {'b': {**record, 'note': 'kept'}, 'a': record}, 'settings': {'udp_first_port': 40001, 'unknown': 2}}))
    extra_store = StreamStateStore(extra_path)
    extra_store.save_stream('c', record)
    return {'load_cases': cases, 'sequence_text': sequence_text, 'extra_keys_text': extra_path.read_text(encoding='utf-8'),
            'missing_file_document': StreamStateStore(root / 'absent.json').load()}


def api_fixture(root):
    FakeProcess.runs = []
    patches = [patch('mediastream.FFmpegProcess', FakeProcess), patch('ffmpegcmd.probe_file', return_value=TEST_PROFILE)]
    for patcher in patches:
        patcher.start()
    registry, store, resolver, config = build_registry(root, TEST_SETTINGS, 'api.json')
    (config.videos_dir / 'boats.mp4').touch()
    source = config.videos_dir / 'boats.mp4'
    registry.supervise(inventory(source, creates=(source,)))
    assert registry.create_stream('boats', 'boats.mp4', 'rtsp', {})[0]
    assert registry.create_stream('boats-udp', 'boats.mp4', 'udp', {})[0]
    playlist = config.hls_dir / 'boats-hls' / 'index.m3u8'
    playlist.parent.mkdir(parents=True)
    playlist.write_text('#EXTM3U\n')
    (playlist.parent / 'seg00001.ts').write_bytes(b'\x47' * 188)
    (playlist.parent / 'notes.txt').write_text('private')
    queue, stop = Queue(maxsize=8), threading.Event()
    snapshot = [None]

    def publish():
        snapshot[0] = StatusSnapshot(tuple(registry.status()), registry.settings(), registry.videos())
    publish()
    owner = threading.Thread(target=run_loop, args=(registry, queue, publish, stop))
    owner.start()
    handler = partial(StreamHandler, requests=queue, read_snapshot=lambda: snapshot[0],
                      execute=partial(_execute_request, registry), url_facts=URL_FACTS, hls_dir=config.hls_dir,
                      host_addresses={'hostname': 'publisher', 'lan_ip': '192.168.1.20'})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    requests = [
        ('GET', '/', {}), ('GET', '/index.html', {}), ('GET', '/jost.woff2', {}),
        ('GET', '/api/streams', {'Host': 'operator-console:9080'}), ('GET', '/api/streams', {'Host': '[::1]:9080'}),
        ('GET', '/api/streams?x=1', {'Host': 'operator-console'}), ('GET', '/api/streams/', {}),
        ('GET', '/api/videos', {}), ('GET', '/api/transports', {}), ('GET', '/api/settings', {}), ('GET', '/api/host', {}),
        ('GET', '/hls/boats-hls/index.m3u8', {}), ('GET', '/hls/boats-hls/seg00001.ts', {}),
        ('GET', '/hls/boats-hls/notes.txt', {}), ('GET', '/hls/../api.json', {}), ('GET', '/hls/missing/index.m3u8', {}),
        ('GET', '/hls/boats-hls', {}), ('GET', '/hls/', {}), ('GET', '/nope', {}), ('GET', '/api', {}),
        ('POST', '/api/streams?name=&file=boats.mp4&transport=udp', {}), ('POST', '/api/streams?name=x&file=&transport=udp', {}),
        ('POST', '/api/streams?name=x&file=boats.mp4&transport=pigeon', {}),
        ('POST', '/api/streams?name=x&file=boats.mp4&transport=udp&fps=nan', {}),
        ('POST', '/api/streams/boats/settings?transport=pigeon', {}), ('POST', '/api/streams/boats/settings?file=../etc/passwd', {}),
        ('POST', '/api/streams/boats/settings?name=bad%20name', {}), ('POST', '/api/settings?udp_first_port=abc', {}),
        ('POST', '/api/settings?multicast_group=10.0.0.1', {}), ('POST', '/api/streams?name=boats&file=boats.mp4&transport=udp', {}),
        ('POST', '/api/streams/boats/unknown', {}), ('POST', '/api/streams/boats', {}), ('POST', '/api/streams/a/b/c', {}),
        ('POST', '/api/nope', {}), ('POST', '/api/streams/absent/start', {}), ('POST', '/api/streams/absent/settings?loop=1', {}),
        ('POST', '/api/streams/boats-udp/settings?name=boats', {}), ('POST', '/api/streams/', {}), ('POST', '/', {}),
        ('POST', '/api/streams/boats/settings?loop=1&loop=2', {}),
        ('POST', '/api/streams?name=boats-srt&file=boats.mp4&transport=srt_listen&mode=transcode&fps=15', {}),
        ('GET', '/api/streams', {'Host': 'operator-console:9080'}),
        ('POST', '/api/streams/boats-udp/settings?transport=tcp_listen&loop=1', {}),
        ('POST', '/api/streams/boats-udp/settings?name=boats-tcp', {}),
        ('POST', '/api/settings?advertised_host=publisher-host&destination_host=receiver', {}),
        ('GET', '/api/streams', {'Host': 'operator-console:9080'}), ('GET', '/api/settings', {}),
        ('POST', '/api/settings?advertised_host=&destination_host=receiver&udp_first_port=40010', {}),
        ('GET', '/api/settings', {}),
        ('POST', '/api/streams/stop-all', {}), ('GET', '/api/streams', {'Host': 'operator-console:9080'}),
        ('POST', '/api/streams/start-all', {}), ('POST', '/api/streams/boats/stop', {}), ('POST', '/api/streams/boats/start', {}),
        ('POST', '/api/streams/boats-srt/delete', {}), ('GET', '/api/streams', {'Host': 'operator-console:9080'}),
    ]
    exchanges = []
    try:
        for method, path, headers in requests:
            connection = HTTPConnection(*server.server_address, timeout=5)
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            connection.close()
            content_type = response.getheader('Content-Type')
            record = {'method': method, 'path': path, 'headers': headers, 'status': response.status,
                      'content_type': content_type, 'cache_control': response.getheader('Cache-Control')}
            if content_type == 'application/json':
                record['json'] = json.loads(body)
            else:
                record['body_length'] = len(body)
                record['body_prefix'] = body[:8].decode('latin-1')
            exchanges.append(record)
    finally:
        server.shutdown()
        server.server_close()
        stop.set()
        owner.join(3)
        for patcher in patches:
            patcher.stop()
    replacements = [(str(config.videos_dir), '{videos_dir}'), (str(config.hls_dir), '{hls_dir}')]
    return placeholders({'exchanges': exchanges, 'final_state': store.load()}, replacements)


def main():
    OUT.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='autostream-fixtures-'))
    try:
        (root / 'mounted').mkdir()
        write('transports.json', describe_transports())
        write('settings.json', settings_fixture())
        write('queries.json', query_fixture())
        write('probe.json', probe_fixture())
        write('prepare.json', prepare_fixture())
        write('streams.json', streams_fixture(root))
        write('state.json', state_fixture(root))
        write('api.json', api_fixture(root))
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()

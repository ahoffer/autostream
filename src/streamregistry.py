"""Stream membership, ports and bindings, owned by the supervisor loop."""
import logging
import random
import shutil

from mediastream import DEFAULT_CHOICES, Delivery, MediaStream, StreamBinding
from runtimesettings import RuntimeSettings
from statefile import STREAM_NAME
from transports import KINDS_BY_ID

log = logging.getLogger(__name__)

# Random ports sit above every first port a transport can be given and below the ephemeral range.
RANDOM_PORT_LOW, RANDOM_PORT_HIGH = 50000, 59999


def is_ignored(path):
    return path.name.startswith('.') or path.name.lower().startswith('readme')


class StreamRegistry:
    def __init__(self, config, store, resolver, logger_factory):
        self._config = config
        self._store = store
        self._resolver = resolver
        self._logger_factory = logger_factory
        self._streams = {}   # name -> MediaStream, in creation order: the UI lists them in that order
        self._settings = RuntimeSettings()
        self._videos = ()

    def load_persisted_state(self):
        document = self._store.load()
        self._settings = RuntimeSettings.from_document(document.get('settings', {}))
        for name, record in document['streams'].items():
            binding = StreamBinding(record['filename'], record['transport'], record['port'])
            # Only the user's choices come back from state; delivery follows the current settings.
            choices = {key: record.get(key, default) for key, default in DEFAULT_CHOICES.items()}
            self._streams[name] = self._build(name, binding, choices)

    def _build(self, name, binding, choices):
        return MediaStream(name, self._config.videos_dir / binding.filename, binding, choices,
                           self._delivery_for(name, binding), max_video_bps=self._settings.max_video_bps,
                           store=self._store, resolver=self._resolver, logger=self._logger_factory(name))

    def settings(self):
        return self._settings

    def videos(self):
        """Stable files from the last inventory, offered as sources."""
        return self._videos

    def get_stream(self, name):
        return self._streams.get(name)

    def create_stream(self, name, filename, transport, choices, start=True):
        """A new stream on a free port of its transport. Returns (True, None) or (False, reason)."""
        if not STREAM_NAME.match(name or ''):
            return False, 'Stream name: letters, digits, - and _ only, 64 at most'
        if name in self._streams:
            return False, f'A stream named {name} exists'
        if transport not in KINDS_BY_ID:
            return False, 'Unknown transport'
        if not filename or '/' in filename or filename.startswith('.'):
            return False, 'Choose a video file'
        if set(choices) - set(DEFAULT_CHOICES):
            return False, 'Unknown stream setting'
        kind = KINDS_BY_ID[transport]
        port = self._free_port(kind)
        if kind.takes_port and port is None:
            return False, f'No free port left for {kind.label}'
        binding = StreamBinding(filename, transport, port)
        stream = self._build(name, binding, {**DEFAULT_CHOICES, **choices, 'enabled': start})
        self._streams[name] = stream
        if start:
            stream.start_streaming()
        else:
            stream.source_changed()
        return True, None

    def delete_stream(self, name):
        stream = self._streams[name]
        if not stream.retire():
            return False
        self._store.delete_stream(name)
        del self._streams[name]
        shutil.rmtree(self._config.hls_dir / name, ignore_errors=True)
        return True

    def update_stream(self, name, changes):
        """Apply setting changes; a new name moves the record, and a new file or
        transport rebinds the stream, all in the same restart."""
        stream = self._streams[name]
        changes = dict(changes)
        new_name = changes.pop('name', name)
        if new_name != name:
            stream = self._rename_stream(name, new_name)
            if not changes:
                # Only the name changed: restart only the kinds whose command names the stream.
                return stream.settings_changed(stream.delivery, self._settings.max_video_bps)
        binding = stream.binding
        filename = changes.pop('filename', binding.filename)
        transport = changes.pop('transport', binding.transport)
        if transport not in KINDS_BY_ID:
            raise ValueError('Unknown transport')
        if (filename, transport) == (binding.filename, binding.transport):
            return stream.update_settings(changes)
        port = binding.port if transport == binding.transport else self._free_port(KINDS_BY_ID[transport], except_name=new_name)
        new_binding = StreamBinding(filename, transport, port)
        return stream.rebind(self._config.videos_dir / filename, new_binding,
                             self._delivery_for(new_name, new_binding), changes)

    def _rename_stream(self, name, new_name):
        """Move a stream to a new name in place; its port, choices and position in the list stay."""
        if not STREAM_NAME.match(new_name):
            raise ValueError('Stream name: letters, digits, - and _ only, 64 at most')
        if new_name in self._streams:
            raise ValueError(f'A stream named {new_name} exists')
        stream = self._streams[name]
        stream.rename(new_name, self._delivery_for(new_name, stream.binding), self._logger_factory(new_name))
        self._streams = {new_name if key == name else key: value for key, value in self._streams.items()}
        shutil.rmtree(self._config.hls_dir / name, ignore_errors=True)
        return stream

    def _free_port(self, kind, except_name=None):
        if not kind.takes_port:
            return None
        used = {stream.binding.port for name, stream in self._streams.items()
                if name != except_name and stream.binding.port is not None}
        if kind.random_port:
            candidates = [port for port in range(RANDOM_PORT_LOW, RANDOM_PORT_HIGH + 1) if port not in used]
            return random.choice(candidates) if candidates else None
        port = getattr(self._settings, kind.first_port_setting)
        while port in used:
            port += 1
        return port if port <= 65535 else None

    def _delivery_for(self, name, binding):
        kind = KINDS_BY_ID[binding.transport]
        if kind.takes_port and binding.port is None:
            return Delivery(None, kind.muxer, kind.carries_klv, blocker='no free port')
        for need in kind.needs:
            if not getattr(self._settings, need):
                return Delivery(None, kind.muxer, kind.carries_klv, blocker=f'no {need.replace("_", " ")} configured')
        fields = {**self._settings.to_document(), 'port': binding.port, 'name': name,
                  'rtsp_port': self._config.rtsp_port, 'hls_dir': self._config.hls_dir}
        return Delivery(kind.delivery.format(**fields), kind.muxer, kind.carries_klv,
                        tuple(arg.format(**fields) for arg in kind.extra_output_args),
                        resolve_host=self._settings.destination_host if 'destination_host' in kind.needs else None,
                        client_bound=kind.direction == 'listen')

    def start_all_streams(self):
        return all([stream.ensure_streaming() for stream in self._streams.values()])

    def stop_all_streams(self, *, persist=True):
        results, errors = [], []
        for stream in self._streams.values():
            try:
                results.append(stream.stop_streaming() if persist else stream.retire())
            except OSError as error:
                errors.append(error)
        if errors:
            raise errors[0]
        return all(results)

    def apply_settings(self, settings):
        """One operation: persist, re-derive every delivery, restart what changed."""
        self._store.save_settings(settings.to_document())
        self._settings = settings
        results = [stream.settings_changed(self._delivery_for(name, stream.binding), settings.max_video_bps)
                   for name, stream in self._streams.items()]
        return all(results)

    def supervise(self, inventory=None):
        """Apply a watcher observation, or perform an ordinary supervision tick."""
        if inventory is not None:
            self._videos = tuple(sorted(inventory['stable']))
            changed = set(inventory['creates']) | set(inventory['modifies']) | set(inventory['deletes'])
            for stream in self._streams.values():
                if stream.binding.filename in changed:
                    stream.source_changed()
        self._resolver.refresh({stream.delivery.resolve_host for stream in self._streams.values()} - {None})
        for stream in self._streams.values():
            stream.supervise()

    def status(self):
        return [stream.status() for stream in self._streams.values()]

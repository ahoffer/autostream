"""Atomic, scoped persistence of streams and settings."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

from runtimesettings import RuntimeSettings
from transports import KINDS_BY_ID

STATE_VERSION = 3
# Stream names double as MediaMTX paths and file names, so they stay plain.
STREAM_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')


class StreamStateStore:
    """One writer: the supervisor loop. Failed writes leave the prior data intact."""
    def __init__(self, path):
        self._path = Path(path)
        self._document = None

    def load(self):
        if self._document is None:
            self._document = self._read()
        return deepcopy(self._document)

    def _read(self):
        try:
            raw = self._path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return {'version': STATE_VERSION, 'streams': {}}
        data = json.loads(raw)
        self._validate(data)
        return data

    def _validate(self, data):
        if data.get('version') != STATE_VERSION:
            raise ValueError(
                f'Unsupported state version {data.get("version")} in {self._path}; '
                f'delete the file to start fresh')
        if not isinstance(data.get('streams'), dict):
            raise ValueError('Invalid state document')
        for name, record in data['streams'].items():
            if not STREAM_NAME.match(name) or not isinstance(record, dict):
                raise ValueError(f'Invalid saved stream {name!r}')
            if not isinstance(record.get('filename'), str) or not record['filename']:
                raise ValueError(f'Stream {name} must name a file')
            if record.get('transport') not in KINDS_BY_ID:
                raise ValueError(f'Stream {name} has an unknown transport')
            port = record.get('port')
            if port is not None and (type(port) is not int or not 1 <= port <= 65535):
                raise ValueError(f'Stream {name} has an invalid port')
            if type(record.get('enabled')) is not bool:
                raise ValueError(f'Stream {name} has no enabled flag')
            if record.get('mode') not in (None, 'passthrough', 'transcode'):
                raise ValueError(f'Stream {name} has an invalid mode')
            if type(record.get('loop_count')) is not int or record['loop_count'] < -1:
                raise ValueError(f'Stream {name} has an invalid loop count')
            for key, maximum in [('frame_rate', 120), ('gop_frames', None)]:
                value = record.get(key)
                if value is not None and (not isinstance(value, (int, float)) or not value >= 1 or (maximum and not value <= maximum)):
                    raise ValueError(f'Stream {name} has an invalid {key}')
        if 'settings' in data:
            if not isinstance(data['settings'], dict):
                raise ValueError('Invalid saved settings')
            RuntimeSettings.from_document(data['settings'])

    def _write(self, document):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix('.tmp')
        temp.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, self._path)

    def save_stream(self, name, record):
        document = self.load()
        document['streams'][name] = deepcopy(record)
        self._write(document)
        self._document = document
        return True

    def delete_stream(self, name):
        document = self.load()
        document['streams'].pop(name, None)
        self._write(document)
        self._document = document
        return True

    def rename_stream(self, previous, name):
        """Move a record to a new name, keeping its place in the order the streams were created."""
        document = self.load()
        document['streams'] = {name if key == previous else key: record
                               for key, record in document['streams'].items()}
        self._write(document)
        self._document = document
        return True

    def save_settings(self, settings_document):
        document = self.load()
        document['settings'] = deepcopy(settings_document)
        self._write(document)
        self._document = document
        return True

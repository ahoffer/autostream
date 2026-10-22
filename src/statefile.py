"""Atomic, scoped persistence of output choices and stable assignments."""
from copy import deepcopy
import json
import os
from pathlib import Path

from runtimesettings import RuntimeSettings

STATE_VERSION = 2


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
            return {'version': STATE_VERSION, 'assignments': {}, 'outputs': {}}
        data = json.loads(raw)
        self._validate(data)
        return data

    def _validate(self, data):
        if data.get('version') != STATE_VERSION:
            raise ValueError(
                f'Unsupported state version {data.get("version")} in {self._path}; '
                f'delete the file to start fresh')
        if not isinstance(data.get('assignments'), dict) or not isinstance(data.get('outputs'), dict):
            raise ValueError('Invalid state document')
        for record in data['assignments'].values():
            if not isinstance(record.get('filename'), str):
                raise ValueError('Assignment must name a file')
            port = record.get('udp_port')
            if port is not None and (type(port) is not int or not 1 <= port <= 65535):
                raise ValueError('Invalid saved UDP port')
        for choices in data['outputs'].values():
            if not isinstance(choices.get('filename'), str) or type(choices.get('enabled')) is not bool:
                raise ValueError('Invalid saved output')
            if choices.get('mode') not in (None, 'passthrough', 'transcode'):
                raise ValueError('Invalid saved mode')
            if type(choices.get('loop_count')) is not int or choices['loop_count'] < -1:
                raise ValueError('Invalid saved loop count')
            for key, maximum in [('frame_rate', 120), ('gop_frames', None)]:
                value = choices.get(key)
                if value is not None and (not isinstance(value, (int, float)) or not value >= 1 or (maximum and not value <= maximum)):
                    raise ValueError(f'Invalid saved {key}')
        if 'settings' in data:
            if not isinstance(data['settings'], dict):
                raise ValueError('Invalid saved settings')
            RuntimeSettings.from_document(data['settings'])

    def _write(self, document):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix('.tmp')
        temp.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, self._path)

    def save_output(self, output_id, choices):
        document = self.load()
        document['outputs'][output_id] = deepcopy(choices)
        self._write(document)
        self._document = document
        return True

    def save_assignments(self, assignments):
        document = self.load()
        document['assignments'] = deepcopy(assignments)
        self._write(document)
        self._document = document
        return True

    def save_settings(self, settings_document):
        document = self.load()
        document['settings'] = deepcopy(settings_document)
        self._write(document)
        self._document = document
        return True

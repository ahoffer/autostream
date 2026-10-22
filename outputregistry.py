"""Output membership and stable assignments, owned by the supervisor loop."""
import logging
from pathlib import Path
import re
import time

from mediaoutput import MediaOutput

log = logging.getLogger(__name__)


def is_ignored(path):
    return path.name.startswith('.') or path.name.lower().startswith('readme')


def stream_name_for(path):
    name = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(path).stem).lower()
    return re.sub(r'-+', '-', re.sub(r'_+', '_', name)).strip('_-')


class OutputRegistry:
    def __init__(self, config, store, resolver, logger_factory):
        self._config = config
        self._store = store
        self._resolver = resolver
        self._logger_factory = logger_factory
        self._outputs = {}
        self._assignments = {}
        self._removed = set()
        self._inventory = None

    def load_persisted_state(self):
        self._assignments = self._store.load()['assignments']

    def _available_port(self):
        ports = {item['udp_port'] for item in self._assignments.values() if item['udp_port'] is not None}
        next_port = max(ports, default=self._config.udp_base_port - 1) + 1
        if next_port <= self._config.udp_last_port:
            return max(next_port, self._config.udp_base_port)
        if self._inventory is None:
            return None
        active = {key.split('/')[0] for key in self._outputs}
        removed = [(item['removed_at'] or 0, name) for name, item in self._assignments.items()
                   if name not in active and item['filename'] not in self._inventory['present']
                   and item['udp_port'] is not None]
        if not removed:
            return None
        _, oldest = min(removed)
        return self._assignments.pop(oldest)['udp_port']

    def register_outputs(self, source_path):
        source_path = Path(source_path)
        name = stream_name_for(source_path)
        if not name or is_ignored(source_path) or name in self._removed:
            return False
        if f'{name}/rtsp' in self._outputs:
            return self._assignments[name]['filename'] == source_path.name
        if not source_path.is_file():
            return False
        saved = self._store.load()['outputs']
        remembered = self._assignments.get(name)
        restore = remembered is not None and remembered['filename'] == source_path.name
        port = remembered['udp_port'] if remembered else self._available_port()
        self._assignments[name] = {'filename': source_path.name, 'udp_port': port, 'removed_at': None}
        self._store.save_assignments(self._assignments)
        outputs = {}
        for protocol in ('rtsp', 'udp'):
            output_id = f'{name}/{protocol}'
            choices = {'enabled': True, 'mode': None, 'loop_count': -1, 'frame_rate': None,
                       'gop_frames': None, 'max_video_bps': self._config.max_video_bps}
            if protocol == 'udp':
                choices['udp_host'] = self._config.output_host
            if restore and saved.get(output_id, {}).get('filename') == source_path.name:
                choices.update({key: value for key, value in saved[output_id].items() if key != 'filename'})
            destination = (f'rtsp://localhost:{self._config.rtsp_port}/{name}' if protocol == 'rtsp'
                           else f'udp://{choices["udp_host"]}:{port}' if port else None)
            outputs[output_id] = MediaOutput(output_id, source_path, choices, destination,
                store=self._store, resolver=self._resolver, logger=self._logger_factory(output_id))
        self._outputs.update(outputs)
        return True

    def get_output(self, output_id):
        if output_id.split('/')[0] in self._removed:
            return None
        return self._outputs.get(output_id)

    def source_changed(self, source_path):
        name = stream_name_for(source_path)
        if name in self._removed:
            return
        if f'{name}/rtsp' not in self._outputs:
            self.register_outputs(source_path)
        elif self._assignments[name]['filename'] == Path(source_path).name:
            for output in self._source_outputs(name):
                output.source_changed()

    def _source_outputs(self, name):
        return [output for key, output in self._outputs.items() if key.startswith(name + '/')]

    def source_removed(self, source_path):
        name = stream_name_for(source_path)
        if self._assignments.get(name, {}).get('filename') == Path(source_path).name:
            self._removed.add(name)

    def _finish_removal(self, name):
        if not all([output.retire() for output in self._source_outputs(name)]):
            return
        self._assignments[name]['removed_at'] = time.time()
        self._store.save_assignments(self._assignments)
        for protocol in ('rtsp', 'udp'):
            self._outputs.pop(f'{name}/{protocol}', None)
        self._removed.remove(name)

    def start_all_outputs(self):
        return all([output.ensure_streaming() for key, output in self._outputs.items()
                    if key.split('/')[0] not in self._removed])

    def stop_all_outputs(self, *, persist=True):
        results, errors = [], []
        for key, output in self._outputs.items():
            if persist and key.split('/')[0] in self._removed:
                continue
            try:
                results.append(output.stop_streaming() if persist else output.retire())
            except OSError as error:
                errors.append(error)
        if errors:
            raise errors[0]
        return all(results)

    def supervise(self, inventory=None):
        """Apply a watcher observation, or perform an ordinary supervision tick."""
        if inventory is not None:
            self._inventory = inventory
            for filename in inventory['deletes']:
                self.source_removed(self._config.videos_dir / filename)
            for filename in inventory['modifies']:
                self.source_changed(self._config.videos_dir / filename)
        for name in list(self._removed):
            self._finish_removal(name)
        if self._inventory is not None:
            # Also retries a stable file whose first event arrived during removal.
            for filename in self._inventory['stable']:
                self.register_outputs(self._config.videos_dir / filename)
        statuses = [output.status() for key, output in self._outputs.items() if key.split('/')[0] not in self._removed]
        self._resolver.refresh({row['udp_host'] for row in statuses if row.get('udp_host')})
        for key, output in self._outputs.items():
            if key.split('/')[0] not in self._removed:
                output.supervise()

    def status(self):
        result = []
        for key, output in self._outputs.items():
            row = output.status()
            if key.split('/')[0] in self._removed:
                row.update(state='retiring', reason='source removed; releasing its destination')
            result.append(row)
        return result

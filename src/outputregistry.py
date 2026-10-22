"""Output membership and stable assignments, owned by the supervisor loop."""
import logging
from pathlib import Path
import re
import time

from mediaoutput import DEFAULT_CHOICES, Destination, MediaOutput
from runtimesettings import RuntimeSettings
from udphost import udp_destination_host

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
        self._settings = RuntimeSettings()

    def load_persisted_state(self):
        document = self._store.load()
        self._assignments = document['assignments']
        self._settings = RuntimeSettings.from_document(document.get('settings', {}))

    def settings(self):
        return self._settings

    def _available_port(self):
        if not self._settings.has_port_range:
            return None
        assigned = {item['udp_port'] for item in self._assignments.values() if item['udp_port'] is not None}
        free = next((port for port in range(self._settings.udp_base_port, self._settings.udp_last_port + 1)
                     if port not in assigned), None)
        if free is not None:
            return free
        if self._inventory is None:
            return None
        active = {key.split('/')[0] for key in self._outputs}
        removed = [(item['removed_at'] or 0, name) for name, item in self._assignments.items()
                   if name not in active and item['filename'] not in self._inventory['present']
                   and item['udp_port'] is not None and self._settings.port_in_range(item['udp_port'])]
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
        port = remembered['udp_port'] if remembered and remembered['udp_port'] is not None else self._available_port()
        self._assignments[name] = {'filename': source_path.name, 'udp_port': port, 'removed_at': None}
        self._store.save_assignments(self._assignments)
        outputs = {}
        for protocol in ('rtsp', 'udp'):
            output_id = f'{name}/{protocol}'
            saved_choices = saved.get(output_id, {})
            if not restore or saved_choices.get('filename') != source_path.name:
                saved_choices = {}
            # Only the user's choices come back from state; destination and cap follow the current settings.
            choices = {key: saved_choices.get(key, default) for key, default in DEFAULT_CHOICES.items()}
            outputs[output_id] = MediaOutput(output_id, source_path, choices, self._destination_for(name, protocol),
                max_video_bps=self._settings.max_video_bps, store=self._store, resolver=self._resolver,
                logger=self._logger_factory(output_id))
        self._outputs.update(outputs)
        return True

    def _destination_for(self, name, protocol):
        if protocol == 'rtsp':
            return Destination(f'rtsp://localhost:{self._config.rtsp_port}/{name}')
        port = self._assignments[name]['udp_port']
        if port is None:
            return Destination(None, 'no free UDP port' if self._settings.has_port_range else 'no UDP port range configured')
        if not self._settings.destination_host:
            return Destination(None, 'no destination host configured')
        return Destination(f'udp://{self._settings.destination_host}:{port}')

    def apply_settings(self, settings):
        """One operation: persist, hand out ports a new range allows, re-derive destinations, restart what changed."""
        self._store.save_settings(settings.to_document())
        self._settings = settings
        assigned_any = False
        for name in sorted(self._live_names()):
            if self._assignments[name]['udp_port'] is None:
                self._assignments[name]['udp_port'] = self._available_port()
                assigned_any = assigned_any or self._assignments[name]['udp_port'] is not None
        if assigned_any:
            self._store.save_assignments(self._assignments)
        results = [output.settings_changed(self._destination_for(*key.split('/')), settings.max_video_bps)
                   for key, output in self._outputs.items() if key.split('/')[0] not in self._removed]
        return all(results)

    def _live_names(self):
        return {key.split('/')[0] for key in self._outputs} - self._removed

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
        self._resolver.refresh({udp_destination_host(row['destination']) for row in statuses} - {None})
        for key, output in self._outputs.items():
            if key.split('/')[0] not in self._removed:
                output.supervise()

    def status(self):
        result = []
        for key, output in self._outputs.items():
            name, protocol = key.split('/')
            row = output.status()
            if protocol == 'udp':
                port = self._assignments[name]['udp_port']
                row.update(udp_port=port, port_outside_range=port is not None and not self._settings.port_in_range(port))
            if name in self._removed:
                row.update(state='retiring', reason='source removed; releasing its destination')
            result.append(row)
        return result

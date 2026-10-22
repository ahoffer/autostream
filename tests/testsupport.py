"""Explicit configuration for isolated tests; never read the deployment .env."""
from dataclasses import replace
from pathlib import Path
import tempfile

from appconfig import Config


def make_config(**changes):
    return replace(Config(rtsp_port=8554, hls_port=8888, api_port=8080, log_level="debug"), **changes)


def state_path(test):
    """Reserve a unique state filename and clean up only the files we write."""
    with tempfile.NamedTemporaryFile(prefix="autostream-test-", suffix=".json", delete=False) as file:
        path = Path(file.name)
    path.unlink()
    test.addCleanup(path.unlink, missing_ok=True)
    test.addCleanup(path.with_suffix(".tmp").unlink, missing_ok=True)
    return path

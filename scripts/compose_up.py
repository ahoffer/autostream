"""Pass Docker host addresses to Compose for the advertised-host shortcuts."""
import json
import os
import subprocess
import sys


def output(*args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def lan_ip():
    """Use the IPv4 address of the lowest-metric default-route interface."""
    try:
        routes = json.loads(output('ip', '-j', '-4', 'route', 'show', 'default') or '[]')
        routes = [route for route in routes if route.get('dev')]
        if not routes:
            return ''
        route = min(routes, key=lambda item: item.get('metric', 0))
        if route.get('prefsrc'):
            return route['prefsrc']
        interfaces = json.loads(output('ip', '-j', '-4', 'address', 'show', 'dev', route['dev']) or '[]')
        return next((address['local'] for interface in interfaces for address in interface.get('addr_info', [])
                     if address.get('scope') == 'global'), '')
    except (ValueError, KeyError):
        return ''


def host_environment(environ):
    env = dict(environ)
    if not env.get('AUTOSTREAM_HOSTNAME'):
        env['AUTOSTREAM_HOSTNAME'] = output('docker', 'info', '--format', '{{.Name}}')
    # Never advertise this client's LAN address for a remote Docker daemon.
    endpoint = '' if env.get('DOCKER_CONTEXT') else env.get('DOCKER_HOST', '')
    if not endpoint:
        endpoint = output('docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
    if not env.get('AUTOSTREAM_LAN_IP'):
        env['AUTOSTREAM_LAN_IP'] = lan_ip() if endpoint.startswith('unix://') else ''
    return env


if __name__ == '__main__':
    sys.exit(subprocess.call(['docker', 'compose', 'up', '-d'], env=host_environment(os.environ)))

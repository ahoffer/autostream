import json
import unittest
from unittest.mock import patch

import compose_up


class HostAddressTests(unittest.TestCase):
    def test_lan_address_comes_from_preferred_default_route(self):
        routes = [{'dev': 'wifi', 'metric': 600}, {'dev': 'ethernet', 'metric': 100}]
        addresses = [{'addr_info': [{'scope': 'global', 'local': '192.168.1.20'}]}]
        with patch.object(compose_up, 'output', side_effect=[json.dumps(routes), json.dumps(addresses)]) as output:
            self.assertEqual(compose_up.lan_ip(), '192.168.1.20')
        self.assertEqual(output.call_args.args, ('ip', '-j', '-4', 'address', 'show', 'dev', 'ethernet'))

    def test_missing_route_leaves_address_unavailable(self):
        with patch.object(compose_up, 'output', return_value=''):
            self.assertEqual(compose_up.lan_ip(), '')

    def test_remote_docker_never_uses_client_lan_address(self):
        with patch.object(compose_up, 'output', return_value='remote-host'), patch.object(compose_up, 'lan_ip') as lan_ip:
            env = compose_up.host_environment({'DOCKER_HOST': 'ssh://remote-host'})
        self.assertEqual(env['AUTOSTREAM_HOSTNAME'], 'remote-host')
        self.assertEqual(env['AUTOSTREAM_LAN_IP'], '')
        lan_ip.assert_not_called()

    def test_explicit_addresses_are_preserved(self):
        with patch.object(compose_up, 'output', return_value='unix:///var/run/docker.sock'), patch.object(compose_up, 'lan_ip') as lan_ip:
            env = compose_up.host_environment({'AUTOSTREAM_HOSTNAME': 'publisher', 'AUTOSTREAM_LAN_IP': '192.168.1.20'})
        self.assertEqual(env['AUTOSTREAM_HOSTNAME'], 'publisher')
        self.assertEqual(env['AUTOSTREAM_LAN_IP'], '192.168.1.20')
        lan_ip.assert_not_called()


if __name__ == '__main__':
    unittest.main()

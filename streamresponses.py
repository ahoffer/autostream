"""Format detached output snapshots, grouped by source for the web UI."""


def group_outputs(statuses, public_urls):
    groups = {}
    for status in statuses:
        name, protocol = status['id'].split('/')
        group = groups.setdefault(name, {'name': name, 'filename': status['filename'], 'outputs': {},
                                        'rtsp_url': f'{public_urls["rtsp"]}/{name}',
                                        'hls_url': f'{public_urls["hls"]}/{name}/index.m3u8', 'udp_url': None})
        group['outputs'][protocol] = status
        if protocol == 'udp':
            group['udp_url'] = status['destination']
    return list(groups.values())

"""Shared, non-secret settings for the launcher and its Python helpers."""
import ipaddress
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / 'monitor_config.json'
DEFAULTS = {
    'adapter_id': None,
    'tablet_ip': '10.11.99.1',
    'usb_network': '10.11.99.0/24',
    'usb_host_ip': None,
    'ssh_port': 22,
    'ssh_key': None,
    'tightvnc_executable': None,
}


def load_settings(path=CONFIG_PATH):
    values = dict(DEFAULTS)
    path = Path(path)
    if path.exists():
        try:
            supplied = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as error:
            raise RuntimeError('Cannot read monitor_config.json: ' + str(error)) from error
        if not isinstance(supplied, dict):
            raise RuntimeError('monitor_config.json must contain a JSON object.')
        unknown = set(supplied) - set(DEFAULTS)
        if unknown:
            raise RuntimeError('Unknown monitor_config.json setting(s): ' + ', '.join(sorted(unknown)))
        values.update(supplied)
    for name in ('adapter_id', 'ssh_key', 'tightvnc_executable'):
        value = values[name]
        if value is not None and (not isinstance(value, str) or not value.strip() or
                                  any(ord(c) < 32 for c in value)):
            raise RuntimeError(name + ' must be a nonempty string or null.')
    if type(values['ssh_port']) is not int or not 1 <= values['ssh_port'] <= 65535:
        raise RuntimeError('ssh_port must be a whole number from 1 to 65535.')
    for name in ('tablet_ip', 'usb_network'):
        if not isinstance(values[name], str):
            raise RuntimeError(name + ' must be an IPv4 string.')
    if values['usb_host_ip'] is not None and not isinstance(values['usb_host_ip'], str):
        raise RuntimeError('usb_host_ip must be an IPv4 string or null.')
    try:
        network = ipaddress.IPv4Network(values['usb_network'])
        tablet = ipaddress.IPv4Address(values['tablet_ip'])
        host = ipaddress.IPv4Address(values['usb_host_ip']) if values['usb_host_ip'] is not None else None
    except (ValueError, TypeError) as error:
        raise RuntimeError('Use IPv4 addresses and a valid IPv4 usb_network in monitor_config.json.') from error
    if tablet not in network or tablet in (network.network_address, network.broadcast_address) or \
            tablet.is_loopback or tablet.is_multicast or tablet.is_unspecified:
        raise RuntimeError('tablet_ip must be a usable address on usb_network.')
    if host is not None and (host not in network or host in
                            (network.network_address, network.broadcast_address, tablet)):
        raise RuntimeError('usb_host_ip must be a separate PC address on usb_network.')
    values.update(tablet_ip=str(tablet), usb_network=str(network),
                  usb_host_ip=str(host) if host is not None else None)
    return values


def configured_path(value):
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else HERE / path


def tightvnc_path():
    if SETTINGS['tightvnc_executable'] is not None:
        return configured_path(SETTINGS['tightvnc_executable'])
    directories = [os.environ.get('ProgramW6432'), os.environ.get('ProgramFiles'),
                   os.environ.get('ProgramFiles(x86)')]
    candidates = [Path(folder) / 'TightVNC' / 'tvnserver.exe' for folder in directories if folder]
    candidates += [Path(r'C:\Program Files\TightVNC\tvnserver.exe'),
                   Path(r'C:\Program Files (x86)\TightVNC\tvnserver.exe')]
    return next((path for path in candidates if path.is_file()), candidates[0])


try:
    SETTINGS = load_settings()
except RuntimeError as error:
    raise SystemExit('Configuration error: ' + str(error)) from None

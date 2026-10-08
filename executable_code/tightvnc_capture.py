"""Backed-up TightVNC application capture settings for a responsive local desktop."""
import json
import os
from pathlib import Path

KEY_NAME = r'Software\TightVNC\Server'
VALUE_NAMES = ('GrabTransparentWindows', 'PollingInterval')


def backup_path():
    return Path(os.environ['LOCALAPPDATA']) / 'RemarkableMonitorStartup' / 'tightvnc-capture-before.json'


def read_values(registry, key):
    values = {}
    for name in VALUE_NAMES:
        try:
            value, kind = registry.QueryValueEx(key, name)
        except FileNotFoundError:
            values[name] = None
            continue
        if kind != registry.REG_DWORD or not isinstance(value, int) or not 0 <= value <= 0xffffffff:
            raise RuntimeError('Unexpected TightVNC capture value/type for ' + name)
        values[name] = value
    return values


def write_values(registry, key, values):
    for name, value in values.items():
        if value is None:
            try:
                registry.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            registry.SetValueEx(key, name, 0, registry.REG_DWORD, value)


def saved_values(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise RuntimeError('The TightVNC capture backup is not valid; settings were left unchanged.')
    values = data.get('values')
    if data.get('version') != 1 or data.get('key') != KEY_NAME or not isinstance(values, dict) or set(values) != set(VALUE_NAMES):
        raise RuntimeError('The TightVNC capture backup is not valid; settings were left unchanged.')
    for value in values.values():
        if value is not None and (type(value) is not int or not 0 <= value <= 0xffffffff):
            raise RuntimeError('The TightVNC capture backup contains an invalid value.')
    return values


def change_settings(executable, desired, run, log, *, restore=False, path=None, registry=None):
    if registry is None:
        import winreg as registry
    path = Path(path) if path is not None else backup_path()
    if restore:
        desired = saved_values(path)
    access = registry.KEY_READ | registry.KEY_SET_VALUE | registry.KEY_WOW64_64KEY
    with registry.OpenKey(registry.HKEY_CURRENT_USER, KEY_NAME, 0, access) as key:
        previous = read_values(registry, key)
        if callable(desired):
            desired = desired(previous)
        preferences = []
        for name in ('UseD3D', 'UseMirrorDriver'):
            try:
                value, kind = registry.QueryValueEx(key, name)
                preferences.append('%s=%s' % (name, value if kind == registry.REG_DWORD else 'unexpected type'))
            except FileNotFoundError:
                preferences.append(name + '=default')
        log('TightVNC capture preferences: ' + ', '.join(preferences) +
            '; previous capture values: ' + json.dumps(previous, sort_keys=True))
        if previous == desired:
            log('TightVNC capture settings already match; no reload needed.')
            return False
        if not restore:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                saved_values(path)  # Preserve and validate the first original snapshot.
            else:
                data = {'version': 1, 'key': KEY_NAME, 'values': previous}
                with path.open('x', encoding='utf-8') as stream:
                    json.dump(data, stream, indent=2)
                    stream.write('\n')
                log('Saved original TightVNC capture settings: ' + str(path))
        try:
            write_values(registry, key, desired)
            if read_values(registry, key) != desired:
                raise RuntimeError('TightVNC capture settings did not match after writing.')
            run([str(executable), '-controlapp', '-reload'], 25)
        except Exception:
            try:
                write_values(registry, key, previous)
                run([str(executable), '-controlapp', '-reload'], 25)
                log('Restored the previous capture settings after the failed change.')
            except Exception as rollback_error:
                log('Capture settings rollback failed: %s; original backup: %s' % (rollback_error, path))
            raise
    if restore:
        log('Original TightVNC capture settings restored. Backup retained: ' + str(path))
    return True


def configure_capture(executable, run, log, *, transparent=False, fast=False, **options):
    # The fallback poller grabs the entire virtual desktop, even for a cropped
    # VNC client. Hooks/cursor updates have separate event-driven paths. Reduce
    # periodic readbacks without changing mouse tracking or the tablet binary.
    selected = {}

    def desired(previous):
        original_poll = previous['PollingInterval']
        poll = 30 if fast else (None if original_poll is None else max(100, original_poll))
        selected.update(GrabTransparentWindows=int(transparent), PollingInterval=poll)
        return dict(selected)

    change_settings(executable, desired, run, log, **options)
    interval = 'existing default' if selected['PollingInterval'] is None else '%d ms' % selected['PollingInterval']
    log('Capture profile: transparent overlays %s; fallback screen polling %s. '
        'VNSee repaint timing and cursor/input handling are unchanged.' %
        ('on' if transparent else 'off', interval))


def restore_capture(executable, run, log, **options):
    return change_settings(executable, None, run, log, restore=True, **options)

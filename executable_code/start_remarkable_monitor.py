#!/usr/bin/env python3
r"""Configure a reMarkable 2 virtual monitor, select TightVNC capture, and run the proxy.

Administrator PowerShell:
  py -3 -u .\start_remarkable_monitor.py --configure
Later runs can omit --configure. Machine settings are in monitor_config.json.
"""
import argparse
import base64
import contextlib
import ctypes
import datetime
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

import remarkable_cursor_proxy as proxy
from windows_display import WindowsDisplay, choose_monitors, share_rectangle, tablet_position
from tightvnc_capture import configure_capture, restore_capture
from monitor_config import SETTINGS, tightvnc_path

HERE = Path(__file__).resolve().parent
LOG = HERE / 'startup.log'


def log(message):
    line = '[%s] %s' % (datetime.datetime.now().isoformat(timespec='seconds'), message)
    print(line, flush=True)
    # Cap retained startup history; proxy statistics are printed, not appended here.
    if LOG.exists() and LOG.stat().st_size > 1048576:
        LOG.replace(LOG.with_suffix('.previous.log'))
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')


def run(command, timeout=45):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.stdout.strip():
        log(result.stdout.strip())
    if result.returncode:
        raise RuntimeError('%s returned exit code %d.' % (Path(command[0]).name, result.returncode))
    return result.stdout


def display_worker(action, instance, timeout=30, skip_mode_test=False):
    command = [sys.executable, '-u', str(Path(__file__).resolve()), '--_display-worker',
               '--_worker-action', action]
    if instance is not None:
        command += ['--adapter-id', instance]
    if skip_mode_test:
        command.append('--_skip-mode-test')
    output = run(command, timeout)
    results = [line[len('DISPLAY_RESULT='):] for line in output.splitlines() if line.startswith('DISPLAY_RESULT=')]
    if len(results) != 1:
        raise RuntimeError('Display worker did not return a verified result.')
    return json.loads(results[0])


def powershell(script):
    prefix = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
    encoded = base64.b64encode((prefix + script).encode('utf-16-le')).decode('ascii')
    return run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], 30)


def inventory():
    data = powershell("@(Get-PnpDevice -Class Display -PresentOnly | Select-Object FriendlyName,InstanceId,Status) | ConvertTo-Json -Compress")
    values = json.loads(data)
    return values if isinstance(values, list) else [values]


def validate_device(devices, instance):
    selected = [d for d in devices if d['InstanceId'].upper() == instance.upper()]
    if len(selected) != 1 or selected[0]['FriendlyName'] != 'Virtual Display Driver' or selected[0]['Status'] != 'OK':
        raise RuntimeError('The selected device must be one present, enabled Virtual Display Driver: ' + instance)
    return selected[0]


def resolve_adapter(devices, instance=None):
    """Select a configured VDD, or the sole enabled VDD; never guess among several."""
    if instance:
        return validate_device(devices, instance)['InstanceId']
    candidates = [d for d in devices if d.get('FriendlyName') == 'Virtual Display Driver'
                  and d.get('Status') == 'OK']
    if len(candidates) == 1:
        return candidates[0]['InstanceId']
    if not candidates:
        raise RuntimeError('No enabled Virtual Display Driver found. Install/enable it and extend its monitor in Windows Settings.')
    raise RuntimeError('Multiple Virtual Display Driver adapters found. Set adapter_id in monitor_config.json '
                       'to the tablet adapter shown by Diagnostics: ' +
                       ', '.join(d['InstanceId'] for d in candidates))


def probe_rfb(port=5900):
    with socket.create_connection(('127.0.0.1', port), timeout=2) as sock:
        sock.settimeout(2)
        if proxy.read_exact(sock, 12) != b'RFB 003.008\n':
            raise RuntimeError('TightVNC must use the existing RFB 3.8 setup.')
        sock.sendall(b'RFB 003.008\n')
        count = proxy.read_exact(sock, 1)[0]
        if not count:
            raise RuntimeError('TightVNC refused the capture verification connection.')
        if 1 not in proxy.read_exact(sock, count):
            raise RuntimeError('Disable VNC authentication in TightVNC application settings and restrict access to loopback.')
        sock.sendall(b'\x01')
        if proxy.read_exact(sock, 4) != bytes(4):
            raise RuntimeError('TightVNC authentication failed.')
        sock.sendall(b'\x01')  # Shared: do not disconnect an existing viewer.
        header = proxy.read_exact(sock, 24)
        size = struct.unpack('!HH', header[:4])
        name_length = struct.unpack('!I', header[20:24])[0]
        if name_length > 1048576:
            raise RuntimeError('Invalid VNC desktop-name length.')
        proxy.read_exact(sock, name_length)
        return size


def wait_rfb(required=None, seconds=15):
    deadline, last = time.monotonic() + seconds, 'No local VNC listener.'
    while time.monotonic() < deadline:
        try:
            size = probe_rfb()
            if required is None or size == required:
                return size
            last = 'TightVNC is sending %d x %d, expected 1404 x 1872. Check its Application DPI override.' % size
        except (OSError, EOFError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(.25)
    raise RuntimeError(last)


def ensure_tightvnc(executable):
    if not executable.is_file():
        raise RuntimeError('TightVNC server executable not found: ' + str(executable))
    quoted = str(executable).replace("'", "''")
    processes = powershell("@((Get-Process tvnserver -ErrorAction SilentlyContinue) | Where-Object { $_.SessionId -eq (Get-Process -Id $PID).SessionId -and $_.Path -eq '%s' } | Select-Object -ExpandProperty Id) | ConvertTo-Json -Compress" % quoted)
    if not json.loads(processes or '[]'):
        # A service/unknown listener must not be mistaken for application mode.
        try:
            with socket.create_connection(('127.0.0.1', 5900), timeout=.5):
                raise RuntimeError('5900 is already listening without the expected TightVNC application. Stop that instance first.')
        except OSError:
            pass
        log('Starting TightVNC in application mode.')
        flags = subprocess.CREATE_NO_WINDOW
        options = {}
        if os.environ.get('REMARKABLE_LAUNCHER_JOB') == '1':
            # The native launcher owns a kill-on-close Python job. Keep TightVNC
            # outside that job, matching the PowerShell workflow's lifetime.
            flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
            # An escaped server must not retain the GUI's output pipe; otherwise
            # Stop waits for EOF forever while TightVNC remains running.
            options.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        subprocess.Popen([str(executable)], creationflags=flags, **options)
    wait_rfb()


def restrict_loopback(executable):
    import winreg
    key_name = r'Software\TightVNC\Server'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        original = {}
        for name in ('AllowLoopback', 'LoopbackOnly'):
            try:
                value, kind = winreg.QueryValueEx(key, name)
                if kind != winreg.REG_DWORD:
                    raise RuntimeError('Unexpected registry type for ' + name)
                original[name] = {'value': value, 'type': kind}
            except FileNotFoundError:
                original[name] = None
        backup = HERE / ('tightvnc-loopback-before-%s.json' % datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f'))
        backup.write_text(json.dumps({'key': key_name, 'values': original}, indent=2), encoding='utf-8')
        for name in original:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)
    log('Saved previous loopback values: ' + str(backup))
    run([str(executable), '-controlapp', '-reload'], 25)
    wait_rfb()


@contextlib.contextmanager
def startup_lock():
    import msvcrt
    lock_path = Path(os.environ['LOCALAPPDATA']) / 'RemarkableMonitorStartup'
    lock_path.mkdir(parents=True, exist_ok=True)
    with (lock_path / 'startup.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another monitor startup script is already running.') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--adapter-id', default=SETTINGS['adapter_id'], help='Tablet VDD PnP instance ID; auto-detect the sole enabled VDD by default')
    parser.add_argument('--configure', action='store_true', help='Update tablet launchers and USB firewall; sets up the SSH key once if needed')
    parser.add_argument('--prepare-tablet', action='store_true', help='Check for a frozen viewer and return it to AppLoad before setup; one-time SSH key setup if needed')
    parser.add_argument('--skip-restart', action='store_true', help='Use the current active VDD monitor without restarting the device')
    parser.add_argument('--diagnose', action='store_true', help='Print current device and monitor identities without changing settings')
    parser.add_argument('--loopback-only', action='store_true', help='Back up TightVNC connection values and restrict it to loopback (disables direct 5900 fallback)')
    parser.add_argument('--vdd-settings', type=Path, help='Explicitly confirmed active XML path: add the native 60 Hz mode and set count=1 while preserving GPU and other options')
    parser.add_argument('--stats', action='store_true')
    parser.add_argument('--no-acceleration', action='store_true')
    parser.add_argument('--fast-capture', action='store_true', help='Use 30 ms fallback desktop polling instead of the desktop-friendly 100 ms')
    parser.add_argument('--capture-transparent-windows', action='store_true', help='Include layered overlays in GDI capture; can reintroduce local cursor flicker')
    parser.add_argument('--keep-capture-settings', action='store_true', help='Leave TightVNC capture settings as they are')
    parser.add_argument('--restore-capture', action='store_true', help='Restore the original backed-up TightVNC capture settings and exit')
    parser.add_argument('--tightvnc', type=Path, default=tightvnc_path(), help='TightVNC server executable; auto-detected unless configured')
    parser.add_argument('--_display-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--_worker-action', choices=('snapshot', 'preflight', 'settled', 'configure'), default='configure', help=argparse.SUPPRESS)
    parser.add_argument('--_skip-mode-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This startup script requires Windows 11.')
    if args._display_worker:
        if args._worker_action != 'snapshot' and args.adapter_id is None:
            raise RuntimeError('The display worker needs the selected adapter ID.')
        display = WindowsDisplay()
        if args._worker_action == 'configure':
            result = display.configure(args.adapter_id)
        elif args._worker_action == 'settled':
            result = display.settled(args.adapter_id)
        else:
            result = display.snapshot()[0]
            if args._worker_action == 'preflight':
                main_monitor, tablet = choose_monitors(result, args.adapter_id)
                if not args._skip_mode_test:
                    display.desired_mode(tablet, main_monitor, tablet_position(result, args.adapter_id, 1404))
        print('DISPLAY_RESULT=' + json.dumps(result), flush=True)
        return
    if args.restore_capture:
        with startup_lock():
            restore_capture(args.tightvnc, run, log)
        return
    devices = inventory()
    if args.diagnose:
        print(json.dumps({'devices': devices, 'active_monitors': display_worker('snapshot', args.adapter_id)}, indent=2))
        return
    args.adapter_id = resolve_adapter(devices, args.adapter_id)
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Open PowerShell as Administrator under your normal Windows account, then run this command again.')
    with startup_lock():
        log('reMarkable monitor startup 1.4.0 / cursor proxy 1.6; selected device ' + args.adapter_id)
        if args.prepare_tablet:
            log('Checking for a previously frozen tablet before monitor setup. SSH uses the saved key; a console opens for first-time key setup if needed.')
            from remarkable_freeze import run_for_launcher
            run_for_launcher('prepare')
        validate_device(devices, args.adapter_id)
        address = proxy.usb_address()
        # Refuse an already running proxy before touching displays or restarting a driver.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind((address, 5902))
        records = display_worker('preflight', args.adapter_id, skip_mode_test=bool(args.vdd_settings))
        main_monitor, tablet = choose_monitors(records, args.adapter_id)
        log('Resolved tablet %s -> %s; current visible size %d x %d.' %
            (tablet['instance'], tablet['gdi'], tablet['width'], tablet['height']))
        extras = [d['InstanceId'] for d in devices if d['FriendlyName'] == 'Virtual Display Driver' and d['InstanceId'].upper() != args.adapter_id.upper()]
        if extras:
            log('Other VDD adapters are left unchanged: ' + ', '.join(extras))
        if not args.tightvnc.is_file():
            raise RuntimeError('TightVNC server executable not found: ' + str(args.tightvnc))
        if args.vdd_settings:
            if args.skip_restart:
                raise RuntimeError('--vdd-settings requires a driver restart; omit --skip-restart.')
            from configure_vdd import ensure_native_settings
            backup = ensure_native_settings(args.vdd_settings)
            if backup:
                log('Added native mode to the explicitly selected VDD XML; backup: ' + str(backup))
        before_ids = {d['InstanceId'].upper() for d in devices}
        if not args.skip_restart:
            log('Restarting only ' + args.adapter_id + ' with Windows PnPUtil.')
            run([str(Path(os.environ['WINDIR']) / 'System32' / 'pnputil.exe'), '/restart-device', args.adapter_id], 35)
            display_worker('settled', args.adapter_id, timeout=35)
            after = inventory()
            validate_device(after, args.adapter_id)
            if {d['InstanceId'].upper() for d in after} != before_ids:
                raise RuntimeError('Display-device inventory changed after restart; setup stopped before applying the layout.')
        log('Applying initial left placement, portrait/native mode, 225%, 60 Hz, and verifying final layout.')
        tablet = display_worker('configure', args.adapter_id, timeout=55)
        ensure_tightvnc(args.tightvnc)
        if not args.keep_capture_settings:
            configure_capture(args.tightvnc, run, log,
                              transparent=args.capture_transparent_windows, fast=args.fast_capture)
        if args.loopback_only:
            restrict_loopback(args.tightvnc)
        # Obtain the live coordinates again after launching/reloading TightVNC.
        main_monitor, latest = choose_monitors(display_worker('snapshot', args.adapter_id), args.adapter_id)
        if latest != tablet:
            raise RuntimeError('Display mapping changed before capture selection. Run setup again.')
        rectangle = share_rectangle(tablet)
        log('Selecting TightVNC capture rectangle ' + rectangle)
        run([str(args.tightvnc), '-controlapp', '-sharerect', rectangle], 25)
        wait_rfb((1404, 1872), 12)
        log('Verified TightVNC framebuffer: 1404 x 1872. Starting the cursor proxy; leave this window open.')
        command = [sys.executable, '-u', str(HERE / 'remarkable_cursor_proxy.py')]
        for option, enabled in [('--configure', args.configure), ('--stats', args.stats), ('--no-acceleration', args.no_acceleration)]:
            if enabled:
                command.append(option)
        worker = subprocess.Popen(command)
        try:
            code = worker.wait()
            if code:
                raise RuntimeError('Cursor proxy exited with code %d; see its message above.' % code)
        finally:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('Monitor startup/proxy stopped. Display settings and TightVNC remain available.')
    except (OSError, EOFError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        log('Setup stopped: ' + str(error))
        sys.exit(1)

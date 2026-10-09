#!/usr/bin/env python3
r"""reMarkable Monitor sleep controls 1.0.2 (rM2 / OS 3.28.x).

install: explicitly install/check the bundled sleep extension; may restart the UI.
freeze: current tablet pixels as a temporary sleep image, then native sleep.
preserve: current tablet pixels as the persistent sleep image, then native sleep.
reset: remove this app's image overrides and restore its backed-up Visible content setting.
sleep: native sleep using the current normal/preserved sleep image.

Uses the existing OpenSSH key and verified VNSee pause/resume helper. Install the
bundled XOVI QMD through the launcher before monitor startup. Normal sleep actions never
install extensions or restart the UI. Original
sleep images, VNSee binaries, AppLoad launchers, and xochitl.conf are not replaced.
Nothing on the PC wakes the tablet or navigates AppLoad after sleep.
"""
import argparse
from array import array
import configparser
import ctypes
import hashlib
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import time
import uuid
import zlib

import remarkable_freeze as legacy
import tablet_ssh

HERE = Path(__file__).resolve().parent
ROOT = '/home/root/.remarkable-monitor-sleep'
QMD_PATH = '/home/root/xovi/exthome/qt-resource-rebuilder/remarkable-monitor-sleep.qmd'
VERSION = '1.0.0'
SCRIPT_VERSION = '1.0.2'
INSTALL_COMMAND = r'py -3 -u .\remarkable_sleep.py install'
WIDTH, HEIGHT = 1404, 1872
FRAME_BYTES = WIDTH * HEIGHT * 2
REQUIRED_HASHES = (15136737896827182809, 7711468349764991, 6504254477,
                   16364404249624396525, 16041628731068125427,
                   5972374, 7083121450889, 233748328658231)

REMOTE_COMMON = r'''
set -eu
umask 077
printf 'Tablet command: monitor-sleep\n'
sleep_root=/home/root/.remarkable-monitor-sleep
sleep_qmd=/home/root/xovi/exthome/qt-resource-rebuilder/remarkable-monitor-sleep.qmd
sleep_power_device() {
    for sleep_event in /sys/class/input/event[0-9]*; do
        [ -r "$sleep_event/device/name" ] || continue
        sleep_name=$(cat "$sleep_event/device/name")
        case "$sleep_name" in *snvs*powerkey*|gpio-keys|*reMarkable*Power*) ;; *) continue ;; esac
        [ -r "$sleep_event/device/capabilities/key" ] || continue
        sleep_mask=$(awk 'NF >= 4 {print $(NF-3)}' "$sleep_event/device/capabilities/key")
        case "$sleep_mask" in ''|*[!0-9a-fA-F]*) continue ;; esac
        [ $((0x$sleep_mask & 0x100000)) -ne 0 ] || continue
        sleep_node=/dev/input/${sleep_event##*/}
        [ -c "$sleep_node" ] || continue
        printf '%s\n' "$sleep_node"
        return 0
    done
    echo 'Cannot identify the native rM2 power button. Nothing was pressed.' >&2
    return 1
}
sleep_platform() {
    [ "$(uname -m)" = armv7l ] || { echo 'This sleep helper requires the 32-bit reMarkable 2.' >&2; exit 1; }
    sleep_machine=$(cat /sys/devices/soc0/machine 2>/dev/null || true)
    case "$sleep_machine" in *'reMarkable 2'*) ;; *) echo 'The tablet did not identify itself as reMarkable 2.' >&2; exit 1 ;; esac
    sleep_firmware=$(awk -F= '$1 == "IMG_VERSION" {gsub(/"/, "", $2); print $2}' /etc/os-release)
    case "$sleep_firmware" in 3.28.*) ;; *) echo "Unsupported sleep-screen firmware: $sleep_firmware. Nothing changed." >&2; exit 1 ;; esac
    command -v systemd-run >/dev/null || { echo 'The native one-shot task runner is missing.' >&2; exit 1; }
    sleep_device=$(sleep_power_device)
}
sleep_ini() {
    [ -r "$sleep_root/control.ini" ] || return 0
    awk -F= -v name="$1" '$1 == name {sub(/^[^=]*=/, ""); print; exit}' "$sleep_root/control.ini"
}
sleep_prune() {
    case "$1:$2" in requests:ping|temporary:png|persistent:png) ;; *) return 1 ;; esac
    for sleep_old in "$sleep_root/$1/"*."$2"; do
        [ -f "$sleep_old" ] || continue
        [ "${sleep_old##*/}" = "$3" ] || rm -f "$sleep_old"
    done
    return 0
}
sleep_viewer() {
    sleep_count=0
    sleep_pid=
    for sleep_exe_file in /proc/[0-9]*/exe; do
        sleep_exe=$(readlink "$sleep_exe_file" 2>/dev/null) || continue
        case "$sleep_exe" in
            /home/root/xovi/exthome/appload/vnsee/vnsee|/home/root/xovi/exthome/appload/vnsee-[0-9]*/vnsee) ;;
            *) continue ;;
        esac
        sleep_dir=${sleep_exe_file%/exe}
        sleep_state=$(awk '$1 == "State:" {print $2}' "$sleep_dir/status")
        case "$sleep_state" in Z|X|'') continue ;; esac
        sleep_count=$((sleep_count + 1))
        sleep_pid=${sleep_dir##*/}
    done
    [ "$sleep_count" -eq 1 ] || { echo 'Exactly one VNSee viewer is required to save the tablet picture.' >&2; exit 1; }
    for sleep_task in /proc/$sleep_pid/task/[0-9]*; do
        [ "$(awk '$1 == "State:" {print $2}' "$sleep_task/status")" = T ] || {
            echo 'Not every VNSee thread is paused; the picture was not copied.' >&2; exit 1;
        }
    done
    sleep_ticks=$(awk '{sub(/^.*\) /, ""); print $20}' /proc/$sleep_pid/stat)
    sleep_shm=$(awk '$NF ~ /^\/dev\/shm\/qtfb_[0-9]+$/ {print $NF}' /proc/$sleep_pid/maps | sort -u)
    [ "$(printf '%s\n' "$sleep_shm" | awk 'NF {n++} END {print n+0}')" -eq 1 ] || {
        echo 'The paused viewer does not have one recognizable AppLoad pixel buffer.' >&2; exit 1;
    }
    [ -f "$sleep_shm" ] && [ "$(wc -c < "$sleep_shm")" -eq 5256576 ] || {
        echo 'The tablet pixel buffer is not native 1404 x 1872 RGB565.' >&2; exit 1;
    }
    printf 'VIEWER=%s:%s:%s\n' "$sleep_pid" "$sleep_ticks" "$sleep_shm"
}
'''


def ssh_bytes(remote_command, payload=None, timeout=30):
    command = tablet_ssh.ssh_command(remote_command, batch=True,
                                     key_only=tablet_ssh.key_path().is_file())
    result = subprocess.run(command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command,
            output=result.stdout.decode('utf-8', errors='replace'),
            stderr=result.stderr.decode('utf-8', errors='replace'))
    return result.stdout


def remote(body, log, timeout=30):
    command = tablet_ssh.ssh_command('sh -s', batch=True,
                                     key_only=tablet_ssh.key_path().is_file())
    return legacy.run_ssh(command, (REMOTE_COMMON + '\n' + body + '\n').encode('ascii'), log, timeout)


def parse_store(text):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(text or '[General]\n')
    return dict(parser.items('General')) if parser.has_section('General') else {}


def read_store():
    return parse_store(ssh_bytes('cat ' + shlex.quote(ROOT + '/control.ini')).decode('utf-8'))


def ping_controller(log, seconds=8):
    token = uuid.uuid4().hex + '.ping'
    remote('mkdir -p "$sleep_root/requests"\nrm -f "$sleep_root/requests/"*.ping\n: > %s\necho CONTROLLER_PING_SENT' %
           shlex.quote(ROOT + '/requests/' + token), log)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = read_store()
        if state.get('LiveToken') == token and state.get('LoadedVersion') == VERSION:
            if state.get('PolicyError'):
                raise RuntimeError(state['PolicyError'])
            # Old request files are no longer needed; cleanup has no wake action.
            remote('sleep_prune requests ping %s\necho CONTROLLER_LIVE' %
                   shlex.quote(token), log)
            return state
        time.sleep(.25)
    raise RuntimeError('The native sleep controller did not acknowledge the fresh request.')


def validate_hashtab(data):
    hashes, offset = set(), 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise RuntimeError('The installed XOVI hashtable is truncated.')
        value, size = struct.unpack_from('>QI', data, offset)
        offset += 12
        if size > 1048576 or offset + size > len(data):
            raise RuntimeError('The installed XOVI hashtable is invalid.')
        hashes.add(value)
        offset += size
    if not set(REQUIRED_HASHES) <= hashes:
        raise RuntimeError('The installed XOVI hashtable lacks the OS 3.28 sleep-screen selectors. '
                           'The working tablet software was left alone.')


def stage_file(path, data, log):
    # Feed bytes to cat over SSH stdin. The tablet needs no base64 decoder.
    digest = hashlib.sha256(data).hexdigest()
    quoted = shlex.quote(path)
    body = 'sleep_stage=%s\n' % quoted + r'''
mkdir -p "${sleep_stage%/*}"
sleep_part="$sleep_stage.part"
trap 'rm -f "$sleep_part"' EXIT HUP INT TERM
cat > "$sleep_part"
sleep_actual=$(sha256sum "$sleep_part" | awk '{print $1}')
''' + '[ "$sleep_actual" = %s ] || { echo "Upload checksum failed." >&2; exit 1; }\n' % shlex.quote(digest) + r'''
chmod 600 "$sleep_part"
mv "$sleep_part" "$sleep_stage"
trap - EXIT HUP INT TERM
echo 'UPLOAD_OK'
'''
    command = tablet_ssh.ssh_command('sh -c ' + shlex.quote(REMOTE_COMMON + '\n' + body),
                                     batch=True, key_only=tablet_ssh.key_path().is_file())
    output = legacy.run_ssh(command, data, log, timeout=45)
    if 'UPLOAD_OK' not in output.splitlines():
        raise RuntimeError('The image/helper upload did not confirm its checksum.')


def png_rgb565(raw, width=WIDTH, height=HEIGHT):
    """Export the tablet's actual paused AppLoad pixels, without a new VNC viewer."""
    if width <= 0 or height <= 0 or len(raw) != width * height * 2:
        raise RuntimeError('The tablet pixel buffer has an unexpected size.')
    values = array('H')
    values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    palette = []
    for pixel in range(65536):
        r, g, b = (pixel >> 11) & 31, (pixel >> 5) & 63, pixel & 31
        palette.append(bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))))
    compressor = zlib.compressobj(6)
    compressed = []
    for row in range(height):
        scanline = b'\x00' + b''.join(palette[p] for p in values[row*width:(row+1)*width])
        compressed.append(compressor.compress(scanline))
    compressed.append(compressor.flush())
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', b''.join(compressed)) + chunk(b'IEND', b''))


def capture_viewer(log):
    output = remote('sleep_viewer', log)
    values = [line[7:] for line in output.splitlines() if line.startswith('VIEWER=')]
    if len(values) != 1:
        raise RuntimeError('The paused VNSee pixel buffer was not identified.')
    match = re.fullmatch(r'([1-9][0-9]*):([0-9]+):(/dev/shm/qtfb_[0-9]+)', values[0])
    if not match:
        raise RuntimeError('The tablet returned an invalid viewer identity.')
    pid, ticks, path = match.groups()
    # Check identity and thread states again in the same command that reads pixels.
    command = r'''set -eu
sleep_pid=PID
[ "$(awk '{sub(/^.*\) /, ""); print $20}' /proc/$sleep_pid/stat)" = TICKS ]
for task in /proc/$sleep_pid/task/[0-9]*; do
    [ "$(awk '$1 == "State:" {print $2}' "$task/status")" = T ]
done
grep -F SHM /proc/$sleep_pid/maps >/dev/null
cat SHM
'''.replace('PID', pid).replace('TICKS', shlex.quote(ticks)).replace('SHM', shlex.quote(path))
    pixels = ssh_bytes(command, timeout=30)
    if len(pixels) != FRAME_BYTES:
        raise RuntimeError('The pixel transfer was incomplete; no sleep image was installed.')
    return png_rgb565(pixels)


def bridge_status(log):
    """Inspect installed files and XOVI without changing them or restarting UI."""
    return remote(r'''
sleep_platform
sleep_xochitl=$(pidof xochitl || true)
case "$sleep_xochitl" in ''|*[!0-9]*) echo 'A unique tablet UI process is required.' >&2; exit 1 ;; esac
grep -F '/home/root/xovi/xovi.so' /proc/$sleep_xochitl/maps >/dev/null || {
    echo 'XOVI is not active. Start it on the tablet using the existing working process, then retry.' >&2; exit 1;
}
if [ -f "$sleep_qmd" ]; then
    head -n 1 "$sleep_qmd" | grep -F '; reMarkable Monitor sleep override' >/dev/null || {
        echo 'The sleep override filename belongs to another modification. Nothing replaced.' >&2; exit 1;
    }
    printf 'QMD_SHA=%s\n' "$(sha256sum "$sleep_qmd" | awk '{print $1}')"
fi
if [ -f "$sleep_root/native-sleep.sh" ]; then
    printf 'NATIVE_SHA=%s\n' "$(sha256sum "$sleep_root/native-sleep.sh" | awk '{print $1}')"
fi
printf 'LOADED=%s\n' "$(sleep_ini LoadedVersion)"
''', log)


def require_bridge(log):
    qmd = (HERE / 'tablet-sleep' / 'monitor-sleep.qmd').read_bytes()
    native = (HERE / 'tablet-sleep' / 'native-sleep.sh').read_bytes()
    lines = bridge_status(log).splitlines()
    if ('QMD_SHA=' + hashlib.sha256(qmd).hexdigest() not in lines or
            'NATIVE_SHA=' + hashlib.sha256(native).hexdigest() not in lines or
            'LOADED=' + VERSION not in lines):
        raise RuntimeError('Sleep extension setup is required. Close the launcher and run '
                           + INSTALL_COMMAND + ' from the extracted package folder')
    return ping_controller(log)


def install_bridge(log):
    """Explicit setup only: upload bundled files; one restart when QMD needs it."""
    qmd = (HERE / 'tablet-sleep' / 'monitor-sleep.qmd').read_bytes()
    native = (HERE / 'tablet-sleep' / 'native-sleep.sh').read_bytes()
    validate_hashtab(ssh_bytes('cat /home/root/xovi/exthome/qt-resource-rebuilder/hashtab'))
    lines = bridge_status(log).splitlines()
    installed = 'QMD_SHA=' + hashlib.sha256(qmd).hexdigest() in lines
    loaded = 'LOADED=' + VERSION in lines
    native_installed = 'NATIVE_SHA=' + hashlib.sha256(native).hexdigest() in lines
    remote(r'''
mkdir -p "$sleep_root/temporary" "$sleep_root/persistent" "$sleep_root/requests"
chmod 700 "$sleep_root" "$sleep_root/temporary" "$sleep_root/persistent" "$sleep_root/requests"
if [ ! -f "$sleep_root/original-suspended.png" ] && [ -r /usr/share/remarkable/suspended.png ]; then
    cp /usr/share/remarkable/suspended.png "$sleep_root/original-suspended.png"
fi
''', log)
    if installed and loaded:
        if not native_installed:
            stage_file(ROOT + '/native-sleep.sh', native, log)
        ping_controller(log)
        log.write('SLEEP_SETUP_READY: installed sleep extension verified. No UI restart needed.\n')
        return
    # Refuse competing sleep-screen resource patches; do not disable them.
    remote(r'''
for other in /home/root/xovi/exthome/qt-resource-rebuilder/*.qmd; do
    [ -f "$other" ] || continue
    [ "$other" = "$sleep_qmd" ] && continue
    if grep -q '15136737896827182809\|sleep-window-opaque.qml\|visibleSleepBody' "$other"; then
        echo "A different sleep-screen modification is already installed: ${other##*/}. Nothing disabled." >&2
        exit 1
    fi
done
''', log)
    if not native_installed:
        stage_file(ROOT + '/native-sleep.sh', native, log)
    stage_file(ROOT + '/monitor-sleep.qmd.new', qmd, log)
    log.write('Installing the bundled sleep extension; restarting the tablet UI once.\n')
    try:
        remote(r'''
if [ -f "$sleep_qmd" ]; then
    cp "$sleep_qmd" "$sleep_root/previous-monitor-sleep.qmd"
else
    rm -f "$sleep_root/previous-monitor-sleep.qmd"
fi
if [ -f "$sleep_root/control.ini" ]; then
    sed '/^LoadedVersion=/d; /^LoadedAt=/d' "$sleep_root/control.ini" > "$sleep_root/control.ini.part"
    mv "$sleep_root/control.ini.part" "$sleep_root/control.ini"
fi
cp "$sleep_root/monitor-sleep.qmd.new" "$sleep_qmd.part"
mv "$sleep_qmd.part" "$sleep_qmd"
systemctl reset-failed xochitl
systemctl restart xochitl
echo 'BRIDGE_RESTARTED'
''', log, timeout=35)
        log.write('Waiting up to 75 seconds for the tablet UI and sleep controller.\n')
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            try:
                state = read_store()
            except (OSError, subprocess.SubprocessError, configparser.Error):
                time.sleep(.5)
                continue
            if state.get('LoadedVersion') == VERSION:
                ping_controller(log, seconds=20)
                log.write('SLEEP_SETUP_READY: sleep extension installed and verified. Reopen VNSee in AppLoad.\n')
                return
            time.sleep(.5)
        raise RuntimeError('The sleep-image extension did not report ready.')
    except (OSError, RuntimeError, subprocess.SubprocessError, configparser.Error):
        try:
            remote('systemctl --no-pager --full status xochitl || true\n'
                   'journalctl -u xochitl -n 40 --no-pager || true', log, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            log.write('Tablet UI diagnostics could not be read: %s\n' % error)
        # Also roll back a loaded but unusable controller (including a failed
        # fresh acknowledgement), rather than trusting a stale status file.
        try:
            remote(r'''
if [ -f "$sleep_root/previous-monitor-sleep.qmd" ]; then
    cp "$sleep_root/previous-monitor-sleep.qmd" "$sleep_qmd"
else
    rm -f "$sleep_qmd"
fi
systemctl reset-failed xochitl
systemctl restart xochitl
echo 'Sleep-image extension rolled back; existing tablet software restored.'
''', log, timeout=35)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            log.write('The extension rollback could not be confirmed: %s\n' % error)
        raise


def back_up_images(log):
    """Keep the previous choices until the new image and sleep request succeed."""
    remote(r'''
mkdir -p "$sleep_root/image-rollback/temporary" "$sleep_root/image-rollback/persistent"
for category in temporary persistent; do
    rm -f "$sleep_root/image-rollback/$category/"*.png
    for image in "$sleep_root/$category/"*.png; do
        [ -f "$image" ] || continue
        cp "$image" "$sleep_root/image-rollback/$category/"
    done
done
echo IMAGE_BACKUP_OK
''', log)


def restore_images(log):
    remote(r'''
for category in temporary persistent; do
    rm -f "$sleep_root/$category/"*.png
    for image in "$sleep_root/image-rollback/$category/"*.png; do
        [ -f "$image" ] || continue
        cp "$image" "$sleep_root/$category/"
    done
done
echo 'Previous sleep-image choices restored.'
''', log)


def wait_image(url, log):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        state = read_store()
        if state.get('PolicyError'):
            raise RuntimeError(state['PolicyError'])
        if (state.get('ReadyImage') == url and state.get('ImageWidth') == str(WIDTH)
                and state.get('ImageHeight') == str(HEIGHT)):
            log.write('Verified the tablet loaded the native-size sleep image.\n')
            return
        time.sleep(.25)
    raise RuntimeError('The tablet did not confirm the new sleep image. Sleep was not requested.')


def queue_sleep(log, on_dispatch=None):
    state = read_store()
    if state.get('Awake') == 'false':
        log.write('SLEEP_REQUESTED: the native tablet UI already reports asleep; no power-button event was sent.\n')
        return
    if state.get('Awake') != 'true':
        raise RuntimeError('The native tablet wake state is unknown. Nothing was pressed.')
    unit = 'remarkable-monitor-sleep-' + uuid.uuid4().hex
    if on_dispatch is not None:
        on_dispatch()
    output = remote(r'''
sleep_platform
[ "$(sleep_ini Awake)" = true ] || { echo 'The tablet is no longer awake. Nothing pressed.' >&2; exit 1; }
[ -f "$sleep_root/native-sleep.sh" ] || { echo 'The native sleep helper is missing.' >&2; exit 1; }
''' + 'systemd-run --no-block --quiet --unit=%s --property=Type=oneshot /bin/sh "$sleep_root/native-sleep.sh" "$sleep_device"\n' % shlex.quote(unit) + r'''
echo 'SLEEP_REQUESTED: native sleep is scheduled. Wake the tablet yourself when needed.'
''', log)
    if not any(line.startswith('SLEEP_REQUESTED:') for line in output.splitlines()):
        raise RuntimeError('The tablet did not accept the native sleep request.')


def perform_action(action, batch=True):
    del batch  # All operations use the existing key without prompts.
    if action not in ('install', 'freeze', 'preserve', 'reset', 'sleep', 'status'):
        raise ValueError('Unknown tablet sleep action.')
    log = legacy.CommandLog(HERE / 'remarkable_sleep.log')
    paused = False
    images_changed = False
    try:
        log.write('reMarkable Monitor sleep %s: %s\n' % (SCRIPT_VERSION, action))
        # Read-only platform/authentication check before pausing or uploading.
        remote('sleep_platform\necho "PLATFORM_OK=$sleep_firmware"', log)
        if action == 'status':
            remote('cat "$sleep_root/control.ini" 2>/dev/null || true', log)
            return 0
        if action == 'install':
            install_bridge(log)
            return 0
        # A missing dependency must be reported before pausing the viewer.
        # No normal control installs files or restarts the tablet UI.
        require_bridge(log)
        image = None
        if action in ('freeze', 'preserve'):
            legacy.perform_action('freeze', batch=True, capture_only=True)
            paused = True
            image = capture_viewer(log)
            # Preserve the pixels in memory before the extension's first UI restart.
            legacy.perform_action('resume', batch=True)
            paused = False
        if action == 'reset':
            back_up_images(log)
            images_changed = True
            remote(r'''
rm -f "$sleep_root/temporary/"*.png "$sleep_root/persistent/"*.png
echo 'IMAGE_OVERRIDES_REMOVED'
''', log)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                state = read_store()
                if state.get('PolicyError'):
                    raise RuntimeError(state['PolicyError'])
                if not state.get('RequestedImage') and not state.get('ReadyImage'):
                    log.write('RESET: the original sleep screen and Visible content setting are restored.\n')
                    images_changed = False
                    return 0
                time.sleep(.25)
            raise RuntimeError('The default sleep-image restoration was not confirmed.')
        if image is not None:
            category = 'temporary' if action == 'freeze' else 'persistent'
            token = uuid.uuid4().hex + '.png'
            image_path = ROOT + '/' + category + '/' + token
            back_up_images(log)
            images_changed = True
            stage_file(image_path, image, log)
            # Unique paths prevent Qt image caching. Keep at most one picture per mode.
            remote('sleep_prune %s png %s\n' % (
                shlex.quote(category), shlex.quote(token)) +
                ('rm -f "$sleep_root/temporary/"*.png\n' if action == 'preserve' else '') +
                'echo IMAGE_INSTALLED', log)
            wait_image('file://' + image_path, log)
        def finish_tablet_work():
            nonlocal images_changed
            # An SSH disconnect during dispatch leaves the result uncertain.
            # Retain the captured picture; a rollback connection could otherwise
            # touch the tablet after it has already gone to sleep.
            images_changed = False
        queue_sleep(log, on_dispatch=finish_tablet_work)
        # Do not send any more SSH commands once native sleep is scheduled.
        images_changed = False
        return 0
    finally:
        if images_changed:
            try:
                restore_images(log)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                log.write('The previous sleep-image choices could not be restored: %s\n' % error)
        if paused:
            try:
                legacy.perform_action('resume', batch=True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                log.write('Could not undo the temporary VNSee pause: %s\n' % error)
        log.close()


def run_in_console(action):
    try:
        return perform_action(action, batch=True)
    except subprocess.CalledProcessError as error:
        if not tablet_ssh.authentication_required(error):
            raise
    legacy.setup_authentication(enroll=True)
    return perform_action(action, batch=True)


def run_for_launcher(action):
    try:
        return perform_action(action, batch=True)
    except subprocess.CalledProcessError as error:
        if not tablet_ssh.authentication_required(error):
            raise
    if os.name != 'nt':
        raise RuntimeError('First-time SSH enrollment needs a Windows console.')
    get_console = ctypes.windll.kernel32.GetConsoleWindow
    get_console.argtypes, get_console.restype = [], ctypes.c_void_p
    if get_console():
        legacy.setup_authentication(enroll=True)
        return perform_action(action, batch=True)
    print('One-time SSH key setup: enter the tablet password in the console that opens.', flush=True)
    process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), action, '--_console'],
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        code = process.wait(timeout=180)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=5)
        raise
    if code:
        raise RuntimeError('The tablet action failed. See remarkable_sleep.log.')
    print('SLEEP_SETUP_READY: sleep extension installed.' if action == 'install' else
          'RESET: original sleep screen restored.' if action == 'reset' else
          'SLEEP_REQUESTED: tablet command completed.', flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('install', 'freeze', 'preserve', 'reset', 'sleep', 'status'))
    parser.add_argument('--launcher', action='store_true')
    parser.add_argument('--_console', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._console:
        sys.stdin = open('CONIN$', 'r', encoding='utf-8', errors='replace')
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
        legacy.setup_authentication(enroll=True)
        return perform_action(args.action, batch=True)
    return run_for_launcher(args.action) if args.launcher else run_in_console(args.action)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, configparser.Error, subprocess.SubprocessError) as error:
        print('Tablet command stopped: %s. See remarkable_sleep.log; sleep is not confirmed.' % error,
              file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Tablet command interrupted. Check the tablet before retrying.', file=sys.stderr)
        sys.exit(1)

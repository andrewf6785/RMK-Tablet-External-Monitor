"""Persistent, app-specific OpenSSH authentication for the USB tablet."""
import base64
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import tempfile
from monitor_config import SETTINGS, configured_path

TABLET = 'root@' + SETTINGS['tablet_ip']
KEY_NAME = 'remarkable_monitor_ed25519'
KEY_READY = 'REMARKABLE_SSH_KEY_READY'


def key_path():
    # Keep credentials outside the downloaded application, across updates.
    if SETTINGS['ssh_key'] is not None:
        return configured_path(SETTINGS['ssh_key'])
    profile = Path(os.environ['USERPROFILE']) if os.environ.get('USERPROFILE') else Path.home()
    return profile / '.ssh' / KEY_NAME


def ssh_command(remote_command, *, batch=False, key_only=False, use_app_key=True):
    ssh = shutil.which('ssh')
    if not ssh:
        raise RuntimeError('OpenSSH Client was not found. Install the Windows OpenSSH Client optional feature.')
    command = [ssh, '-4', '-T', '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
               '-o', 'NumberOfPasswordPrompts=1', '-o', 'ClearAllForwardings=yes']
    if SETTINGS['ssh_port'] != 22:
        command += ['-p', str(SETTINGS['ssh_port'])]
    key = key_path()
    if (use_app_key and key.is_file()) or key_only:
        command += ['-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none']
    if key_only:
        command += ['-o', 'PreferredAuthentications=publickey', '-o', 'PasswordAuthentication=no',
                    '-o', 'KbdInteractiveAuthentication=no']
    if batch or key_only:
        command += ['-o', 'BatchMode=yes']
    return command + [TABLET, remote_command]


def authentication_required(error, remote_marker='Tablet command:'):
    diagnostic = (error.stderr or '') + (error.output or '')
    return error.returncode == 255 and remote_marker not in diagnostic and any(
        message in diagnostic.lower() for message in (
            'permission denied', 'host key verification failed', 'no authentication methods available'))


def public_key(text):
    """Accept only a complete Ed25519 public key, then use a fixed comment."""
    fields = text.strip().split()
    if len(fields) < 2 or fields[0] != 'ssh-ed25519':
        raise RuntimeError('The dedicated SSH public key is not an Ed25519 key.')
    try:
        blob = base64.b64decode(fields[1], validate=True)
        prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
        if not blob.startswith(prefix) or len(blob) != len(prefix) + 32:
            raise ValueError('Unexpected SSH key encoding')
    except (ValueError, base64.binascii.Error) as error:
        raise RuntimeError('The dedicated SSH public key is malformed.') from error
    return 'ssh-ed25519 %s remarkable-monitor' % fields[1]


def ensure_key(log):
    keygen = shutil.which('ssh-keygen')
    if not keygen:
        raise RuntimeError('ssh-keygen was not found. Enable the Windows OpenSSH Client feature.')
    key = key_path()
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not key.exists():
        log.write('Creating a dedicated SSH key for this Windows user.\n')
        # Generate separately; a concurrent attempt cannot replace an existing key.
        # Python passes the empty -N argument directly, without a PowerShell shell.
        with tempfile.TemporaryDirectory(prefix='.remarkable-key-', dir=key.parent) as directory:
            staged = Path(directory) / KEY_NAME
            subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-C', 'remarkable-monitor',
                            '-f', str(staged)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20)
            try:
                os.link(staged, key)
            except FileExistsError:
                pass
    if os.name != 'nt':
        key.chmod(0o600)
    # Derive the public key from the actual private key; never trust a stale .pub.
    result = subprocess.run([keygen, '-y', '-P', '', '-f', str(key)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=True, timeout=10)
    value = public_key(result.stdout.decode('ascii'))
    key.with_name(key.name + '.pub').write_text(value + '\n', encoding='ascii')
    return value


def install_script(value):
    value = public_key(value)
    return (r'''
set -eu
umask 077
key_dir="${HOME:-/home/root}/.ssh"
key_file="$key_dir/authorized_keys"
key_value=PUBLIC_KEY
mkdir -p "$key_dir"
chmod 700 "$key_dir"
if [ -e "$key_file" ] && [ ! -f "$key_file" ]; then
    echo 'authorized_keys is not a regular file; nothing replaced.' >&2
    exit 1
fi
if [ -f "$key_file" ] && grep -F -x "$key_value" "$key_file" >/dev/null; then
    chmod 600 "$key_file"
else
    key_stage=$(mktemp "$key_dir/.remarkable-key.XXXXXX")
    trap 'rm -f "$key_stage"' EXIT
    if [ -f "$key_file" ]; then cat "$key_file" > "$key_stage"; fi
    if [ -s "$key_stage" ]; then printf '\n' >> "$key_stage"; fi
    printf '%s\n' "$key_value" >> "$key_stage"
    chmod 600 "$key_stage"
    mv "$key_stage" "$key_file"
    trap - EXIT
fi
echo REMARKABLE_SSH_KEY_INSTALLED
''').replace('PUBLIC_KEY', shlex.quote(value))


def enroll_key(runner, log, *, batch=False):
    """Install once using existing credentials, then prove key-only authentication."""
    value = ensure_key(log)
    log.write('Installing the app SSH key. Enter the tablet root password once if asked.\n')
    # The new key is not authorized yet. Preserve default keys/agent access for
    # this installation connection, with a password fallback only if needed.
    output = runner(ssh_command('sh -s', batch=batch, use_app_key=False), install_script(value).encode('ascii'),
                    log, timeout=25 if batch else 75)
    if 'REMARKABLE_SSH_KEY_INSTALLED' not in output.splitlines():
        raise RuntimeError('SSH did not confirm key installation. Authentication setup was not completed.')
    try:
        output = runner(ssh_command('echo ' + KEY_READY, key_only=True), b'', log, timeout=25)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('The tablet did not accept the installed SSH key. Check the SSH log before retrying.') from error
    if KEY_READY not in output.splitlines():
        raise RuntimeError('SSH did not confirm key-only access. Authentication setup was not completed.')
    log.write('SSH key verified. Tablet setup and sleep controls can now connect without a password.\n')


def ensure_authentication(runner, log):
    """For already visible consoles: reuse the key, or enroll it automatically."""
    had_key = key_path().is_file()
    try:
        output = runner(ssh_command('echo ' + KEY_READY, batch=True, key_only=had_key),
                        b'', log, timeout=25)
    except subprocess.CalledProcessError as error:
        if not authentication_required(error, remote_marker=KEY_READY):
            raise
        enroll_key(runner, log)
        return
    if KEY_READY not in output.splitlines():
        raise RuntimeError('SSH did not confirm the tablet connection.')
    if not had_key:
        enroll_key(runner, log, batch=True)

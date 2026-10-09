"""Parse each VNSee manifest on the PC; stage, compare, back up, then commit on tablet."""
import copy
import hashlib
import io
import json
import shlex
import subprocess
import tarfile
from tablet_ssh import ssh_command

BASE = '/home/root/xovi/exthome/appload'
BACKUPS = '/home/root/vnsee-cursor-backups'
MODES = {'0': ('Slow', 'SLOW'), '1': ('Standard', 'STANDARD'),
         '2': ('Fast', 'FAST'), '3': ('Fastest', 'FASTEST'),
         '25ms': ('25 ms', 'FASTEST')}
FETCH_SCRIPT = r'''
set -eu
cd /home/root/xovi/exthome/appload
for n in 0 1 2 3; do test -f "vnsee-$n/external.manifest.json"; done
set -- vnsee-0/external.manifest.json vnsee-1/external.manifest.json vnsee-2/external.manifest.json vnsee-3/external.manifest.json
if [ -f vnsee-25ms/external.manifest.json ]; then set -- "$@" vnsee-25ms/external.manifest.json; fi
tar -cf - "$@"
'''


def updated_manifest(data, mode, address):
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get('environment'), dict):
        raise ValueError('VNSee manifest must contain an environment object.')
    if not isinstance(value.get('args'), list) or not all(isinstance(x, str) for x in value['args']):
        raise ValueError('VNSee manifest args must be an array of strings.')
    if not isinstance(value.get('application'), str):
        raise ValueError('VNSee manifest application must be a string.')
    value = copy.deepcopy(value)
    label, waveform = MODES[mode]
    value['name'] = 'VNSee (%s)' % label
    value['args'] = [address, '5902']
    value['environment']['VNSEE_ENCODING'] = 'HEXTILE'
    value['environment']['VNSEE_WAVEFORM_MODE'] = waveform
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def read_archive(data):
    originals = {}
    allowed = {'vnsee-%s/external.manifest.json' % n: n for n in MODES}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for member in archive:
            if member.name not in allowed or not member.isfile() or member.size > 1048576:
                raise ValueError('Unexpected or oversized file in tablet manifest archive.')
            mode = allowed[member.name]
            if mode in originals:
                raise ValueError('Duplicate tablet manifest.')
            originals[mode] = archive.extractfile(member).read()
    if not set('0123') <= originals.keys():
        raise ValueError('The four original VNSee launchers must exist.')
    return originals


def prepare_archive(originals, address):
    # Validate every mode before opening the write SSH session.
    updated = {n: updated_manifest(data, n, address) for n, data in originals.items()}
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as archive:
        for n, data in updated.items():
            item = tarfile.TarInfo('new-%s.json' % n)
            item.size, item.mode = len(data), 0o644
            archive.addfile(item, io.BytesIO(data))
    return payload.getvalue()


def install_script(originals):
    modes = ' '.join(n for n in MODES if n in originals)
    checks = '\n'.join("printf '%s  %%s\\n' \"$base/vnsee-%s/external.manifest.json\" | sha256sum -c" %
                       (hashlib.sha256(originals[n]).hexdigest(), n) for n in MODES if n in originals)
    return r'''
set -eu
base=/home/root/xovi/exthome/appload
backup_root=/home/root/vnsee-cursor-backups
mkdir -p "$backup_root"
stage=$(mktemp -d "$backup_root/stage.XXXXXX")
backup=''
started=0
committed=0
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$started" = 1 ] && [ "$committed" = 0 ]; then
        for n in MODES; do
            cp -p "$backup/vnsee-$n.json" "$base/vnsee-$n/external.manifest.json" || echo "Rollback failed for $n; use backup $backup" >&2
        done
    fi
    for n in MODES; do rm -f "$base/vnsee-$n/.vnsee-cursor-new.json"; done
    rm -rf "$stage"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
tar -C "$stage" -xf -
CHECKS
for n in MODES; do
    test -s "$stage/new-$n.json"
    test "$(wc -c < "$stage/new-$n.json")" -le 1048576
done
backup=$(mktemp -d "$backup_root/backup.XXXXXX")
for n in MODES; do
    cp -p "$base/vnsee-$n/external.manifest.json" "$backup/vnsee-$n.json"
    cp -p "$base/vnsee-$n/external.manifest.json" "$base/vnsee-$n/.vnsee-cursor-new.json"
    cat "$stage/new-$n.json" > "$base/vnsee-$n/.vnsee-cursor-new.json"
done
CHECKS
started=1
for n in MODES; do
    mv "$base/vnsee-$n/.vnsee-cursor-new.json" "$base/vnsee-$n/external.manifest.json"
done
printf '%s\n' "$backup" > "$stage/LATEST"
mv "$stage/LATEST" "$backup_root/LATEST"
committed=1
printf 'VNSee modes (MODES) configured with HEXTILE. Previous settings: %s\n' "$backup"
echo 'Return to AppLoad and tap Reload before opening VNSee (25 ms).'
'''.replace('MODES', modes).replace('CHECKS', checks)


def configure(address):
    from remarkable_freeze import setup_authentication
    setup_authentication()
    print('Reading each tablet launcher using the saved SSH key.', flush=True)
    result = subprocess.run(ssh_command('sh -c ' + shlex.quote(FETCH_SCRIPT), key_only=True),
                            stdout=subprocess.PIPE, check=True, timeout=180)
    originals = read_archive(result.stdout)
    payload = prepare_archive(originals, address)
    print('All launchers validated. Installing backups and updated JSON.', flush=True)
    subprocess.run(ssh_command('sh -c ' + shlex.quote(install_script(originals)), key_only=True),
                   input=payload, check=True, timeout=180)

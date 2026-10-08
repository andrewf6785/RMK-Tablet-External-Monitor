#!/usr/bin/env python3
r"""Install a separate faster VNSee entry using the working Fastest settings.

Windows PowerShell (Python standard library only):
    py -3 -u .\install_vnsee_faster.py --delay-ms 25

Reads the existing ARMv7 VNSee 1.1.0 binary and Fastest launcher over USB SSH.
Checks the executable code and read-only data against the upstream rM2 build,
then changes two 50 ms repaint timers in a copy. The original stays intact.
Installs the copy as a separate AppLoad entry, preserving the working VNC
connection and encoding. Uses the shared SSH settings and per-user key.
"""

import argparse
import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tarfile
from tablet_ssh import ssh_command

BASE = "/home/root/xovi/exthome/appload"
TEXT_SHA256 = "442dbd47c8acf885b09b78cf1b80b8a719865548d28118b72f1df480b6c02a77"
RODATA_SHA256 = "6eef3d8f31c75ab6282a55efb3f2f3986197abd5025eaac08735c792fee2c3e6"
FASTEST_INSTRUCTION = 0x2378  # .text relative: mov r0, #50, in FASTEST branch
FAST_DELAY_LITERAL = 0x2708  # .text relative: uint64 50, fast repaint mode


def elf_sections(data):
    if len(data) < 52 or data[:7] != b"\x7fELF\x01\x01\x01":
        raise RuntimeError("Expected a 32-bit little-endian ARM executable.")
    fields = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    if fields[0] != 2 or fields[1] != 40:
        raise RuntimeError("This trial supports the existing rM2 ARM executable only.")
    table, entry_size, count, names_index = fields[5], fields[10], fields[11], fields[12]
    if entry_size != 40 or not count or names_index >= count or table + count * entry_size > len(data):
        raise RuntimeError("Invalid ELF section table.")
    headers = [struct.unpack_from("<10I", data, table + n * 40) for n in range(count)]
    names_header = headers[names_index]
    if names_header[4] + names_header[5] > len(data):
        raise RuntimeError("Invalid ELF section names.")
    names = data[names_header[4]:names_header[4] + names_header[5]]
    result = {}
    for header in headers:
        if header[0] >= len(names):
            raise RuntimeError("Invalid ELF section name.")
        end = names.find(b"\x00", header[0])
        if end < 0:
            raise RuntimeError("Invalid ELF section name.")
        name = names[header[0]:end].decode("ascii", errors="replace")
        if name in (".text", ".rodata"):
            address, offset, size = header[3:6]
            if header[1] != 1 or offset + size > len(data):
                raise RuntimeError("Invalid executable section bounds.")
            result[name] = (address, offset, data[offset:offset + size])
    if set(result) != {".text", ".rodata"}:
        raise RuntimeError("Required executable sections were not found.")
    return result


def patch_timers(original, delay_ms):
    if not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or not 1 <= delay_ms <= 49:
        raise RuntimeError("Choose a whole-number repaint delay from 1 to 49 ms.")
    sections = elf_sections(original)
    text_address, text_offset, text = sections[".text"]
    rodata_address, _, rodata = sections[".rodata"]
    text_hash = hashlib.sha256(text).hexdigest()
    if (text_address != 0x13AA0 or rodata_address != 0x37A70
            or text_hash != TEXT_SHA256 or hashlib.sha256(rodata).hexdigest() != RODATA_SHA256):
        raise RuntimeError("This VNSee build differs from the verified rM2 1.1.0 release. "
                           "Nothing was installed. Code SHA256: " + text_hash)
    instruction = text_offset + FASTEST_INSTRUCTION
    literal = text_offset + FAST_DELAY_LITERAL
    if struct.unpack_from("<I", original, instruction)[0] != 0xE3A00032:
        raise RuntimeError("The expected FASTEST timer instruction was not found.")
    if struct.unpack_from("<Q", original, literal)[0] != 50:
        raise RuntimeError("The expected fast repaint timer was not found.")
    patched = bytearray(original)
    struct.pack_into("<I", patched, instruction, 0xE3A00000 | delay_ms)
    struct.pack_into("<Q", patched, literal, delay_ms)
    differences = {n for n, (before, after) in enumerate(zip(original, patched)) if before != after}
    if differences != {instruction, literal}:
        raise RuntimeError("Unexpected binary changes; installation stopped.")
    return bytes(patched)


def read_working_files():
    command = "tar -C " + BASE + " -cf - vnsee/vnsee vnsee-3/external.manifest.json"
    result = subprocess.run(ssh_command(command, key_only=True), stdout=subprocess.PIPE, check=True, timeout=60)
    if len(result.stdout) > 16777216:
        raise RuntimeError("Unexpectedly large VNSee download.")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:*") as archive:
        required = {"vnsee/vnsee", "vnsee-3/external.manifest.json"}
        if {member.name for member in archive.getmembers()} != required:
            raise RuntimeError("Unexpected files in VNSee download.")
        contents = {}
        for name in required:
            member = archive.getmember(name)
            if not member.isfile() or member.size > 8388608:
                raise RuntimeError("Unexpected VNSee download entry.")
            contents[name] = archive.extractfile(member).read()
    return contents["vnsee/vnsee"], contents["vnsee-3/external.manifest.json"]


def trial_manifest(source, delay_ms):
    manifest = json.loads(source.decode("utf-8-sig"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("environment"), dict):
        raise RuntimeError("Fastest's launcher does not have the expected environment settings.")
    if not isinstance(manifest.get("args"), list) or not manifest["args"]:
        raise RuntimeError("Fastest's launcher does not have connection arguments.")
    manifest["name"] = "VNSee (%d ms)" % delay_ms
    manifest["application"] = "./vnsee"
    manifest["environment"]["VNSEE_WAVEFORM_MODE"] = "FASTEST"
    if "id" in manifest:
        manifest["id"] = "vnsee-%dms" % delay_ms
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def install_script(delay_ms, expected_hash):
    # Both substitutions are validated locally before being embedded in shell code.
    if not 1 <= delay_ms <= 49 or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
        raise RuntimeError("Invalid trial parameters.")
    return r'''
set -eu
vnsee_base=/home/root/xovi/exthome/appload
vnsee_target="$vnsee_base/vnsee-DELAYms"
vnsee_stage=$(mktemp -d /home/root/xovi/exthome/vnsee-trial.XXXXXX)
vnsee_cleanup() { rm -f "$vnsee_stage/vnsee" "$vnsee_stage/external.manifest.json" "$vnsee_stage/icon.png"; rmdir "$vnsee_stage" 2>/dev/null || true; }
trap vnsee_cleanup EXIT
trap 'exit 1' HUP INT TERM
tar -C "$vnsee_stage" -xf -
printf 'EXPECTED  %s\n' "$vnsee_stage/vnsee" | sha256sum -c
chmod 755 "$vnsee_stage/vnsee"
if [ -f "$vnsee_base/vnsee-3/icon.png" ]; then cp "$vnsee_base/vnsee-3/icon.png" "$vnsee_stage/icon.png"; fi
if [ -d "$vnsee_target" ]; then
    printf 'EXPECTED  %s\n' "$vnsee_target/vnsee" | sha256sum -c
    cp "$vnsee_target/external.manifest.json" "$vnsee_target/external.manifest.json.previous"
    mv "$vnsee_stage/external.manifest.json" "$vnsee_target/external.manifest.json"
    vnsee_cleanup
    trap - EXIT HUP INT TERM
    echo 'Updated VNSee (DELAY ms) with the current Fastest connection settings. Tap Reload in AppLoad.'
    exit 0
elif [ -e "$vnsee_target" ]; then
    echo 'The trial path is occupied by something other than an app directory.' >&2
    exit 1
fi
mv "$vnsee_stage" "$vnsee_target"
trap - EXIT HUP INT TERM
echo 'Installed VNSee (DELAY ms). Return to AppLoad and tap Reload.'
'''.replace("DELAY", str(delay_ms)).replace("EXPECTED", expected_hash)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delay-ms", type=int, default=25, choices=range(1, 50), metavar="1..49")
    args = parser.parse_args()
    if shutil.which("ssh") is None:
        raise RuntimeError("Windows OpenSSH client was not found.")
    from remarkable_freeze import setup_authentication
    setup_authentication()
    print("Reading the installed VNSee and Fastest settings using the saved SSH key.", flush=True)
    original, manifest = read_working_files()
    patched = patch_timers(original, args.delay_ms)
    updated = trial_manifest(manifest, args.delay_ms)
    print("Verified rM2 1.1.0 build. Changed both repaint timers from 50 to %d ms in a copy." % args.delay_ms, flush=True)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, data, mode in [("vnsee", patched, 0o755), ("external.manifest.json", updated, 0o644)]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(patched).hexdigest()
    import shlex
    remote_command = "sh -c " + shlex.quote(install_script(args.delay_ms, digest))
    print("Installing the separate %d ms entry using the saved SSH key." % args.delay_ms, flush=True)
    subprocess.run(ssh_command(remote_command, key_only=True), input=payload.getvalue(), check=True, timeout=60)
    print("Existing Slow, Standard, Fast and Fastest launchers and the original VNSee executable are untouched.", flush=True)
    print("The trial uses Fastest's existing connection and encoding. You can close this installer window.", flush=True)
    print("%d ms is a repaint scheduling interval; actual displayed frame rate depends on the rest of the setup." % args.delay_ms, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nTrial setup stopped.", file=sys.stderr)
        sys.exit(1)
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError, ValueError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            message = "SSH setup returned exit code %d; see its message above." % error.returncode
        else:
            message = str(error)
        print("Setup stopped: " + message, file=sys.stderr)
        sys.exit(1)

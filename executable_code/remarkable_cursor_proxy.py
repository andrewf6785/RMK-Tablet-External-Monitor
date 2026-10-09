#!/usr/bin/env python3
"""Cursor workaround for VNSee-QTFB and TightVNC on Windows 11.

First run, in Administrator PowerShell:
    py -3 -u .\remarkable_cursor_proxy.py --configure
Later runs:
    py -3 -u .\remarkable_cursor_proxy.py
Undo the most recent tablet configuration:
    py -3 .\remarkable_cursor_proxy.py --restore

Runs with the standard library; optional NumPy accelerates large updates.
It removes VNSee's three cursor
pseudo-encoding requests, asking TightVNC to include the cursor in the desktop
image instead. It compares framebuffer pixels and suppresses redundant redraws.
Requires TightVNC's existing no-authentication RFB 3.8 setup on port 5900.
Listens only on the PC's USB IPv4 address, on port 5902, and accepts only
the configured tablet. Connection settings are read from monitor_config.json.
Idle connections have no application time limit.
"""

import argparse
import ctypes
import ipaddress
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from rfb_filter import BufferedSocket, FrameFilter, ProtocolError, SUPPORTED_ENCODINGS, read_exact
from monitor_config import SETTINGS

TABLET_IP = SETTINGS['tablet_ip']
HELPER_VERSION = "1.6"
CURSOR_ENCODINGS = {-240, -239, -232}  # XCursor, RichCursor, PointerPos
FIREWALL_NAME = "reMarkable-VNSee-Cursor-USB"

RESTORE_SCRIPT = r'''
set -eu
vnsee_base=/home/root/xovi/exthome/appload
vnsee_backup=$(cat /home/root/vnsee-cursor-backups/LATEST)
case "$vnsee_backup" in /home/root/vnsee-cursor-backups/backup.*) ;; *) echo 'Invalid backup path.' >&2; exit 1;; esac
vnsee_modes='0 1 2 3'
if [ -f "$vnsee_backup/vnsee-25ms.json" ]; then vnsee_modes="$vnsee_modes 25ms"; fi
for vnsee_n in $vnsee_modes; do test -f "$vnsee_backup/vnsee-$vnsee_n.json"; done
for vnsee_n in $vnsee_modes; do
    cp "$vnsee_backup/vnsee-$vnsee_n.json" "$vnsee_base/vnsee-$vnsee_n/external.manifest.json"
done
echo 'Previous VNSee settings restored. Return to AppLoad and tap Reload.'
'''


def handshake(viewer, server, required_size=(1404, 1872), framebuffer=None):
    """Relay the current setup's RFB 3.8 / None-authentication handshake."""
    greeting = read_exact(server, 12)
    if greeting != b"RFB 003.008\n":
        raise ProtocolError("This helper requires the existing RFB 3.8 TightVNC setup.")
    viewer.sendall(greeting)
    reply = read_exact(viewer, 12)
    if reply != greeting:
        raise ProtocolError("VNSee did not select RFB 3.8.")
    server.sendall(reply)
    count = read_exact(server, 1)
    if count[0] == 0:
        raise ProtocolError("TightVNC refused the connection.")
    offered = read_exact(server, count[0])
    viewer.sendall(count + offered)
    choice = read_exact(viewer, 1)
    if choice != b"\x01" or 1 not in offered:
        raise ProtocolError("Keep the current TightVNC setup with VNC authentication disabled.")
    server.sendall(choice)
    result = read_exact(server, 4)
    viewer.sendall(result)
    if result != b"\x00\x00\x00\x00":
        raise ProtocolError("TightVNC authentication failed.")
    server.sendall(read_exact(viewer, 1))  # ClientInit, including original shared flag
    header = read_exact(server, 24)
    width, height = struct.unpack("!HH", header[:4])
    if (width, height) != required_size:
        raise ProtocolError(
            "TightVNC is sending %d x %d. Use Extend and the tablet's 1404 x 1872 "
            "resolution, and share its virtual display before reopening VNSee." % (width, height)
        )
    name_length = struct.unpack("!I", header[20:24])[0]
    if name_length > 1048576:
        raise ProtocolError("Invalid VNC desktop-name length.")
    viewer.sendall(header + read_exact(server, name_length))
    if framebuffer is not None:
        framebuffer.reset(header[4] // 8)
    return width, height


def forward_client_message(viewer, server, framebuffer=None):
    message_type = read_exact(viewer, 1)
    kind = message_type[0]
    if kind == 2:  # SetEncodings
        header = read_exact(viewer, 3)
        count = struct.unpack("!H", header[1:])[0]
        encodings = struct.unpack("!%di" % count, read_exact(viewer, 4 * count))
        keep = [encoding for encoding in encodings if encoding in SUPPORTED_ENCODINGS]
        if 0 not in keep:
            keep.append(0)
        server.sendall(message_type + header[:1] + struct.pack("!H", len(keep))
                       + struct.pack("!%di" % len(keep), *keep))
        return sum(encoding in CURSOR_ENCODINGS for encoding in encodings)
    if kind in (0, 3, 4, 5, 8, 150):
        # Pixel format, update request, key, pointer, SetScale, continuous updates.
        remaining = {0: 19, 3: 9, 4: 7, 5: 5, 8: 3, 150: 9}[kind]
        body = read_exact(viewer, remaining)
        if framebuffer is not None and kind == 0:
            if body[6] != 1:
                raise ProtocolError("The cursor helper requires true-color VNC pixels.")
            framebuffer.reset(body[3] // 8)
        if framebuffer is not None and kind == 3 and body[0] == 0:
            framebuffer.invalidate(*struct.unpack('!HHHH', body[1:]))
        server.sendall(message_type + body)
    elif kind == 6:  # ClientCutText, including ExtendedClipboard's signed length
        header = read_exact(viewer, 7)
        length = abs(struct.unpack("!i", header[3:])[0])
        if length > 16777216:
            raise ProtocolError("Clipboard message exceeds 16 MiB.")
        server.sendall(message_type + header + read_exact(viewer, length))
    elif kind == 248:  # Fence
        header = read_exact(viewer, 8)
        server.sendall(message_type + header + read_exact(viewer, header[7]))
    elif kind == 251:  # SetDesktopSize
        header = read_exact(viewer, 7)
        server.sendall(message_type + header + read_exact(viewer, 16 * header[5]))
    else:
        raise ProtocolError("Unsupported VNC client message %d." % kind)
    return 0


def close_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class TransferStats:
    """Optional per-connection update metrics; socket-read waiting is excluded."""
    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.start = clock()
        self.frames = self.suppressed = self.byte_count = 0
        self.processing = 0.0

    def record(self, frames, suppressed, byte_count, processing):
        self.frames += frames
        self.suppressed += suppressed
        self.byte_count += byte_count
        self.processing += processing
        elapsed = self.clock() - self.start
        if elapsed < 5:
            return None
        line = ("Performance: VNC %.1f updates/s | forwarded %.1f/s | helper %.1f ms/update | incoming %.1f Mb/s"
                % (self.frames / elapsed, (self.frames - self.suppressed) / elapsed,
                   self.processing * 1000 / self.frames if self.frames else 0,
                   self.byte_count * 8 / elapsed / 1000000))
        self.start = self.clock()
        self.frames = self.suppressed = self.byte_count = 0
        self.processing = 0.0
        return line


def handle_connection(viewer, address, target_port, acceleration=True, stats=False):
    server = None
    try:
        viewer.settimeout(20)  # Handshake only; no timeout after connection succeeds.
        viewer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        server = socket.create_connection(("127.0.0.1", target_port), timeout=10)
        server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        framebuffer = FrameFilter(1404, 1872, acceleration=acceleration)
        width, height = handshake(viewer, server, framebuffer=framebuffer)
        viewer.settimeout(None)
        server.settimeout(None)
        print("Tablet connected: %d x %d." % (width, height), flush=True)

        def server_to_viewer():
            try:
                reader = BufferedSocket(server)
                announced = False
                reporting = TransferStats() if stats else None
                while True:
                    if reporting:
                        started = time.perf_counter()
                        old_frames, old_suppressed = framebuffer.frames, framebuffer.suppressed_frames
                        old_bytes, old_wait = reader.received_bytes, reader.read_wait
                    message = framebuffer.server_message(reader)
                    if reporting:
                        processing = max(0, time.perf_counter() - started - (reader.read_wait - old_wait))
                        line = reporting.record(framebuffer.frames - old_frames,
                                                framebuffer.suppressed_frames - old_suppressed,
                                                reader.received_bytes - old_bytes, processing)
                        if line:
                            print(line, flush=True)
                    viewer.sendall(message)
                    if framebuffer.suppressed_frames and not announced:
                        print("Unchanged redraws suppressed; real desktop changes continue to update.", flush=True)
                        announced = True
            except EOFError:
                pass
            except (OSError, ProtocolError) as error:
                print("VNC stream ended: %s" % error, flush=True)
            finally:
                close_socket(viewer)
                close_socket(server)

        worker = threading.Thread(target=server_to_viewer, daemon=True)
        worker.start()
        announced = False
        while True:
            removed = forward_client_message(viewer, server, framebuffer=framebuffer)
            if removed and not announced:
                print("Separate cursor updates disabled; TightVNC should draw the pointer in the image.", flush=True)
                announced = True
    except EOFError:
        print("Tablet disconnected. Waiting for it to reconnect.", flush=True)
    except (OSError, ProtocolError) as error:
        print("Connection ended: %s" % error, flush=True)
    finally:
        close_socket(viewer)
        viewer.close()
        if server is not None:
            close_socket(server)
            server.close()


def validate_usb_address(address):
    try:
        parsed = ipaddress.IPv4Address(address)
    except (ipaddress.AddressValueError, TypeError):
        raise RuntimeError("A valid PC USB IPv4 address is required.") from None
    network = ipaddress.ip_network(SETTINGS['usb_network'])
    if parsed not in network:
        raise RuntimeError("Connect the tablet by USB first; its USB network address was not found.")
    if parsed in (network.network_address, network.broadcast_address, ipaddress.IPv4Address(TABLET_IP)):
        raise RuntimeError("The detected address is not a usable PC USB address.")
    return str(parsed)


def usb_address():
    if SETTINGS['usb_host_ip'] is not None:
        return validate_usb_address(SETTINGS['usb_host_ip'])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((TABLET_IP, 5900))  # Chooses a route; sends no packet.
        address = probe.getsockname()[0]
    return validate_usb_address(address)


def configure_firewall(address, port):
    if os.name != "nt":
        raise RuntimeError("Automatic setup is intended for Windows 11.")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Run this first setup from PowerShell opened as Administrator.")
    # All embedded values are a fixed rule name, validated IPv4, and integer port.
    script = (
        "$ErrorActionPreference='Stop'; "
        "$vnsee_rule=Get-NetFirewallRule -Name '%s' -ErrorAction SilentlyContinue; "
        "if ($vnsee_rule) { Set-NetFirewallRule -Name '%s' -Enabled True -Direction Inbound "
        "-Action Allow -Protocol TCP -LocalPort %d -LocalAddress '%s' "
        "-RemoteAddress '%s' -Profile Any | Out-Null } "
        "else { New-NetFirewallRule -Name '%s' -DisplayName 'reMarkable VNSee cursor USB' "
        "-Direction Inbound -Action Allow -Protocol TCP -LocalPort %d "
        "-LocalAddress '%s' -RemoteAddress '%s' -Profile Any | Out-Null }"
    ) % (FIREWALL_NAME, FIREWALL_NAME, port, address, TABLET_IP,
         FIREWALL_NAME, port, address, TABLET_IP)
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True, timeout=30)
    print("USB-only cursor-proxy firewall rule ready.", flush=True)


def configure_tablet(address=None, restore=False):
    remote_command = "sh -s" if restore else "sh -s -- " + validate_usb_address(address)
    if shutil.which("ssh") is None:
        raise RuntimeError("Windows OpenSSH client was not found.")
    if not restore:
        from tablet_config import configure
        configure(validate_usb_address(address))
        return
    script = RESTORE_SCRIPT
    from remarkable_freeze import setup_authentication
    from tablet_ssh import ssh_command
    setup_authentication()
    print("Connecting to the tablet using the saved SSH key.", flush=True)
    subprocess.run(
        ssh_command(remote_command, key_only=True),
        input=script.encode("utf-8"), check=True, timeout=180,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--configure", action="store_true", help="Prepare USB firewall and tablet modes, including existing 25 ms, then run")
    actions.add_argument("--restore", action="store_true", help="Restore the most recent tablet manifest backups, then exit")
    parser.add_argument("--port", type=int, default=5902, choices=[5902], help="Proxy USB listening port (5902)")
    parser.add_argument("--target-port", type=int, default=5900, choices=[5900], help="Existing local TightVNC port (5900)")
    parser.add_argument("--stats", action="store_true", help="Print update rates and helper processing time about every 5 seconds")
    parser.add_argument("--no-acceleration", action="store_true", help="Use the standard-library decoder even if NumPy is installed")
    args = parser.parse_args()
    print("VNSee cursor helper %s" % HELPER_VERSION, flush=True)
    if args.restore:
        configure_tablet(restore=True)
        return
    address = usb_address()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, args.port))  # Fail before changing settings if already running.
        listener.listen(4)
        listener.settimeout(1)
        if args.configure:
            configure_firewall(address, args.port)
            configure_tablet(address=address)
        print("Cursor proxy ready at %s:%d." % (address, args.port), flush=True)
        enabled = FrameFilter(1, 1, acceleration=not args.no_acceleration).accelerated
        print("Hextile acceleration: %s." % ("NumPy enabled" if enabled else "off (optional: py -3 -m pip install numpy)"), flush=True)
        print("Leave this window open. Return to AppLoad, Reload, and open VNSee Fastest or 25 ms.", flush=True)
        print("Ctrl+C stops the helper. Later runs do not need --configure.", flush=True)
        while True:
            try:
                viewer, peer = listener.accept()
            except socket.timeout:
                continue
            if peer[0] != TABLET_IP:
                viewer.close()
                continue
            threading.Thread(target=handle_connection, args=(viewer, peer, args.target_port, not args.no_acceleration, args.stats), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCursor helper stopped.")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print("Setup stopped: %s" % error, file=sys.stderr)
        sys.exit(1)

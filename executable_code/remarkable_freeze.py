#!/usr/bin/env python3
r"""Checked VNSee pause/resume helper and one-time SSH-key enrollment.

remarkable_sleep.py uses freeze/resume only while copying tablet pixels.
The GUI's Freeze buttons request native sleep through remarkable_sleep.py.
Standalone pause/status/resume and legacy recovery commands are also supported.
Uses monitor_config.json for the tablet address and optional SSH identity.
OpenSSH handles authentication; this module stores no passwords.
Logs are written to remarkable_freeze.log beside the application.
"""

import argparse
import codecs
import ctypes
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import threading
import tablet_ssh

SCRIPT_VERSION = "1.2.0"
REMOTE_SCRIPT = r"""
set -eu
freeze_base=/home/root/xovi/exthome/appload
freeze_action=${1:-status}
freeze_stage='validating action'
freeze_rollback=0
freeze_cleanup() {
    freeze_exit=$?
    if [ "$freeze_exit" -ne 0 ]; then
        printf 'TABLET ERROR: action %s stopped while %s (exit %s).\n' "$freeze_action" "$freeze_stage" "$freeze_exit" >&2
    fi
    if [ "$freeze_rollback" -eq 1 ] && freeze_same; then
        echo 'Undoing the pause issued by this attempt.' >&2
        kill -CONT "$freeze_signal_pid" 2>/dev/null || true
    fi
    return "$freeze_exit"
}
trap freeze_cleanup EXIT
trap 'exit 1' HUP INT TERM
printf 'Tablet command: %s\n' "$freeze_action"
case "$freeze_action" in
    freeze|resume|status|prepare|unfreeze) ;;
    *) echo 'Unknown action. Nothing changed.' >&2; exit 1 ;;
esac
freeze_stage='checking tablet utilities'
for freeze_utility in readlink awk sleep; do
    command -v "$freeze_utility" >/dev/null 2>&1 || {
        printf 'Required tablet utility is missing: %s\n' "$freeze_utility" >&2
        exit 1
    }
done
freeze_stage='checking optional PID namespace information'
freeze_namespace=$(readlink /proc/self/ns/pid 2>/dev/null || true)
if [ -n "$freeze_namespace" ]; then
    echo 'PID namespace information is available.'
else
    echo 'PID namespace files are unavailable; continuing with process-status identity.'
fi

freeze_state() {
    awk '$1 == "State:" {print $2; exit}' "$1/status" 2>/dev/null
}

freeze_start() {
    # Remove the entire parenthesized comm field before indexing stat fields.
    awk '{sub(/^.*\) /, ""); print $20; exit}' "$1/stat" 2>/dev/null
}

freeze_count=0
freeze_pid=
freeze_exe=
freeze_stage='finding the current AppLoad VNSee process'
for freeze_proc in /proc/[0-9]*/exe; do
    freeze_path=$(readlink "$freeze_proc" 2>/dev/null) || continue
    case "$freeze_path" in
        "$freeze_base"/vnsee/vnsee|"$freeze_base"/vnsee-[0-9]*/vnsee) ;;
        */vnsee|*/vnsee\ \(deleted\))
            printf 'Found a VNSee executable outside the eligible paths: %s -> %s\n' "$freeze_proc" "$freeze_path"
            continue ;;
        *) continue ;;
    esac
    freeze_dir=${freeze_proc%/exe}
    if [ -n "$freeze_namespace" ]; then
        freeze_candidate_namespace=$(readlink "$freeze_dir/ns/pid" 2>/dev/null || true)
        if [ -n "$freeze_candidate_namespace" ] && [ "$freeze_candidate_namespace" != "$freeze_namespace" ]; then
            printf 'Ignoring VNSee in another PID namespace: %s\n' "$freeze_path"
            continue
        fi
    fi
    freeze_current=${freeze_dir##*/}
    freeze_current_state=$(freeze_state "$freeze_dir") || continue
    case "$freeze_current_state" in Z|X|'') continue ;; esac
    freeze_count=$((freeze_count + 1))
    freeze_pid=$freeze_current
    freeze_exe=$freeze_path
    printf 'VNSee PID %s: state %s, %s\n' "$freeze_current" "$freeze_current_state" "$freeze_path"
done
printf 'Eligible VNSee processes: %s\n' "$freeze_count"

if [ "$freeze_action" = status ]; then
    if [ "$freeze_count" -eq 0 ]; then
        echo 'VNSee is not running in a recognized AppLoad directory.'
    else
        echo 'State T = frozen; R/S = running/waiting; other states require checking.'
    fi
    exit 0
fi

if [ "$freeze_count" -eq 0 ] && {
    [ "$freeze_action" = prepare ] || [ "$freeze_action" = unfreeze ];
}; then
    echo 'READY: no frozen VNSee viewer is running.'
    exit 0
fi

if [ "$freeze_count" -ne 1 ]; then
    printf 'Found %s eligible VNSee processes. Nothing changed.\n' "$freeze_count" >&2
    echo 'Keep exactly one existing VNSee app open, then try again.' >&2
    exit 1
fi

freeze_dir=/proc/$freeze_pid
freeze_stage='reading the selected VNSee process identity'
freeze_ticks=$(freeze_start "$freeze_dir")
case "$freeze_ticks" in ''|*[!0-9]*) echo 'Cannot read VNSee identity. Nothing changed.' >&2; exit 1 ;; esac
# Procfs may be mounted by an outer PID namespace. Use the target's PID in
# our matching namespace for signals, while retaining its procfs path for reads.
freeze_signal_pid=$(awk '
    $1 == "Pid:" {fallback = $2}
    $1 == "NSpid:" {print $NF; found = 1; exit}
    END {if (!found) print fallback}
' "$freeze_dir/status")
case "$freeze_signal_pid" in
    ''|0|1|*[!0-9]*) echo 'Cannot resolve a safe VNSee PID. Nothing changed.' >&2; exit 1 ;;
esac

freeze_same() {
    freeze_now_exe=$(readlink "$freeze_dir/exe" 2>/dev/null) || return 1
    freeze_now_ticks=$(freeze_start "$freeze_dir") || return 1
    [ "$freeze_now_exe" = "$freeze_exe" ] && [ "$freeze_now_ticks" = "$freeze_ticks" ]
}

freeze_all_stopped() {
    freeze_same || return 1
    freeze_tasks=0
    for freeze_task in "$freeze_dir"/task/[0-9]*; do
        freeze_task_state=$(freeze_state "$freeze_task") || return 1
        [ "$freeze_task_state" = T ] || return 1
        freeze_tasks=$((freeze_tasks + 1))
    done
    [ "$freeze_tasks" -gt 0 ]
}

freeze_before=$(freeze_state "$freeze_dir")
if [ "$freeze_action" = prepare ] || [ "$freeze_action" = unfreeze ]; then
    case "$freeze_before" in
        t) echo 'VNSee is stopped by a debugger. Nothing changed.' >&2; exit 1 ;;
        T) ;;
        *)
            echo 'READY: VNSee is already running; nothing was changed.'
            exit 0 ;;
    esac
    freeze_stage='verifying the frozen viewer before returning to AppLoad'
    freeze_all_stopped || {
        echo 'Not every VNSee thread is stopped. Nothing changed.' >&2
        exit 1
    }
    # Closing SSH does not disconnect VNC. End this exact paused viewer even
    # when USB and another proxy are still connected, so AppLoad can return.
    freeze_same || { echo 'VNSee identity changed. Nothing changed.' >&2; exit 1; }
    freeze_stage='ending the identified frozen VNSee viewer'
    kill -TERM "$freeze_signal_pid"
    if freeze_same; then
        kill -CONT "$freeze_signal_pid"
    fi
    freeze_stage='waiting for the frozen viewer to exit'
    freeze_attempt=0
    while [ "$freeze_attempt" -lt 5 ]; do
        if ! freeze_same; then
            echo 'THAWED: the frozen VNSee viewer ended; AppLoad can return.'
            exit 0
        fi
        freeze_after=$(freeze_state "$freeze_dir") || {
            echo 'THAWED: the frozen VNSee viewer ended; AppLoad can return.'
            exit 0
        }
        case "$freeze_after" in
            Z|X) echo 'THAWED: the frozen VNSee viewer ended; AppLoad can return.'; exit 0 ;;
        esac
        freeze_attempt=$((freeze_attempt + 1))
        sleep 1
    done
    echo 'VNSee did not exit; AppLoad return was not confirmed. Check status.' >&2
    exit 1
fi

if [ "$freeze_action" = freeze ]; then
    if [ "$freeze_before" = T ]; then
        if freeze_all_stopped; then
            echo 'FROZEN: VNSee is already paused. You can unplug USB.'
            exit 0
        fi
        echo 'VNSee was already stopped, but not every thread could be verified. Nothing changed.' >&2
        exit 1
    fi
    if [ "$freeze_before" = t ]; then
        echo 'VNSee is stopped by a debugger. Nothing changed.' >&2
        exit 1
    fi
    # Let already submitted AppLoad repaint requests settle on a still image.
    freeze_stage='letting the current picture settle'
    sleep 1
    freeze_same || { echo 'VNSee changed/exited before freeze. Nothing changed.' >&2; exit 1; }
    freeze_rollback=1
    freeze_stage='sending STOP to the identified VNSee process'
    printf 'Pausing VNSee (signal PID %s).\n' "$freeze_signal_pid"
    kill -STOP "$freeze_signal_pid"
    freeze_stage='verifying that every VNSee thread is stopped'
    freeze_attempt=0
    while [ "$freeze_attempt" -lt 5 ]; do
        if freeze_all_stopped; then
            freeze_rollback=0
            echo 'FROZEN: all VNSee threads are paused. You can now unplug USB.'
            echo 'Keep the current VNSee view open on the tablet.'
            exit 0
        fi
        freeze_attempt=$((freeze_attempt + 1))
        sleep 1
    done
    echo 'Could not verify a complete pause; attempting to resume VNSee.' >&2
    exit 1
fi

if [ "$freeze_before" != T ]; then
    echo 'VNSee is already running/waiting; no resume signal was needed.'
    exit 0
fi
freeze_same || { echo 'VNSee identity changed. Nothing changed.' >&2; exit 1; }
freeze_stage='sending CONT to the identified VNSee process'
kill -CONT "$freeze_signal_pid"
freeze_stage='verifying that VNSee resumed'
freeze_attempt=0
while [ "$freeze_attempt" -lt 5 ]; do
    if ! freeze_same; then
        echo 'RESUMED: VNSee exited after continuation. Open it again in AppLoad.'
        exit 0
    fi
    freeze_after=$(freeze_state "$freeze_dir") || {
        echo 'RESUMED: VNSee ended. Open it again in AppLoad.'
        exit 0
    }
    case "$freeze_after" in
        T|t) ;;
        Z|X) echo 'RESUMED: VNSee ended. Open it again in AppLoad.'; exit 0 ;;
        *) echo 'RESUMED: VNSee is running. If disconnected, reopen it in AppLoad.'; exit 0 ;;
    esac
    freeze_attempt=$((freeze_attempt + 1))
    sleep 1
done
echo 'Resume was sent, but VNSee still reports stopped. Check status.' >&2
exit 1
"""


class CommandLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.output = []
        self.file = path.open("a", encoding="utf-8")
        self.file.write("\n---- %s: reMarkable freeze %s ----\n" %
                        (datetime.now().isoformat(timespec="seconds"), SCRIPT_VERSION))
        self.file.flush()

    def write(self, text):
        if not text:
            return
        with self.lock:
            self.output.append(text)
            self.file.write(text)
            self.file.flush()
            sys.stdout.write(text)
            sys.stdout.flush()

    def close(self):
        self.file.close()


def run_ssh(command, payload, log, timeout=75):
    """Stream both channels, including prompts without a trailing newline."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    captures = {"stdout": [], "stderr": []}

    def read_output(stream, channel):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    captures[channel].append(text)
                    log.write(text)
            tail = decoder.decode(b"", final=True)
            if tail:
                captures[channel].append(tail)
                log.write(tail)
        finally:
            stream.close()

    def send_script():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # The SSH exit code and stderr report an early failure.
        finally:
            process.stdin.close()

    readers = [threading.Thread(target=read_output, args=(process.stdout, "stdout"), daemon=True),
               threading.Thread(target=read_output, args=(process.stderr, "stderr"), daemon=True)]
    writer = threading.Thread(target=send_script, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        for thread in readers + [writer]:
            thread.join(timeout=2)
    stdout, stderr = "".join(captures["stdout"]), "".join(captures["stderr"])
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
    return stdout


def perform_action(action, batch=False):
    log_path = Path(__file__).resolve().with_suffix(".log")
    command = tablet_ssh.ssh_command("sh -s -- " + action, batch=batch,
                                     key_only=tablet_ssh.key_path().is_file())
    # Bytes preserve LF line endings when running from Windows.
    # Authentication uses the dedicated user key after its one-time enrollment.
    log = CommandLog(log_path)
    try:
        log.write("reMarkable freeze %s: %s\n" % (SCRIPT_VERSION, action))
        log.write("Log: %s\n" % log_path)
        log.write("Checking USB SSH without prompts.\n" if batch else
                  "Connecting over USB. Enter the tablet's root password if SSH asks.\n")
        output = run_ssh(command, REMOTE_SCRIPT.encode("ascii"), log,
                         timeout=25 if batch else 75)
        if action == "freeze" and not any(line.startswith("FROZEN:") for line in output.splitlines()):
            raise RuntimeError("SSH ended without a FROZEN confirmation. Keep USB connected and share the log.")
        if action in ("prepare", "unfreeze") and not any(
                line.startswith(("READY:", "THAWED:")) for line in output.splitlines()):
            raise RuntimeError("SSH ended without a tablet-ready confirmation. Monitor startup was not continued.")
    except subprocess.CalledProcessError as error:
        detail = [line.strip() for line in (error.stderr or error.output or "").splitlines() if line.strip()]
        last = detail[-1] if detail else "The tablet returned no diagnostic text."
        log.write("\nFAILED (exit %d): %s\n" % (error.returncode, last))
        log.write("Complete output saved to %s\n" % log_path)
        raise
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        log.write("\nConnection interrupted/timed out. Reconnect USB and check status before retrying.\n")
        log.write("Output saved to %s\n" % log_path)
        raise
    except (OSError, RuntimeError) as error:
        log.write("\nFAILED: %s\n" % error)
        raise
    finally:
        log.close()
    return 0


def setup_authentication(enroll=False):
    log = CommandLog(Path(__file__).resolve().with_suffix('.log'))
    try:
        if enroll:
            tablet_ssh.enroll_key(run_ssh, log)
        else:
            tablet_ssh.ensure_authentication(run_ssh, log)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', None) or str(error)
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        log.write('\nSSH key setup failed: %s\n' % detail)
        raise
    finally:
        log.close()


def run_in_console(action):
    try:
        return perform_action(action, batch=True)
    except subprocess.CalledProcessError as error:
        if not tablet_ssh.authentication_required(error):
            raise
    setup_authentication(enroll=True)
    return perform_action(action, batch=True)


def run_for_launcher(action):
    """Reuse SSH keys; show a console for one-time key enrollment if needed."""
    try:
        return perform_action(action, batch=True)
    except subprocess.CalledProcessError as error:
        if not tablet_ssh.authentication_required(error):
            raise
    print("One-time SSH key setup: enter the tablet password in the console that opens next.", flush=True)
    if os.name != "nt":
        raise RuntimeError("Interactive launcher authentication requires a Windows console.")
    get_console = ctypes.windll.kernel32.GetConsoleWindow
    get_console.argtypes = []
    get_console.restype = ctypes.c_void_p
    if get_console():
        setup_authentication(enroll=True)
        return perform_action(action, batch=True)
    command = [sys.executable, '-u', str(Path(__file__).resolve()), action, '--_console']
    process = subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        code = process.wait(timeout=180)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait(timeout=5)
        raise
    if code:
        raise RuntimeError("Tablet command failed. See remarkable_freeze.log; the requested state was not confirmed.")
    print("FROZEN: tablet pause confirmed." if action == "freeze" else
          "READY: tablet command completed; reopen VNSee from AppLoad if it was frozen.", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=("freeze", "resume", "status", "prepare", "unfreeze", "setup-ssh"))
    parser.add_argument("--launcher", action="store_true", help="Use saved SSH authentication, with one-time Windows key setup if needed")
    parser.add_argument("--_console", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._console:
        if os.name != 'nt':
            raise RuntimeError('The authentication console requires Windows.')
        # CREATE_NEW_CONSOLE can still inherit the GUI's redirected standard
        # handles. Reopen the actual console so prompts and messages are visible.
        sys.stdin = open('CONIN$', 'r', encoding='utf-8', errors='replace')
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
        print('One-time reMarkable SSH setup: enter your root password if asked.', flush=True)
        setup_authentication(enroll=True)
        if args.action == 'setup-ssh':
            return 0
        return perform_action(args.action, batch=True)
    if args.action == 'setup-ssh':
        setup_authentication()
        return 0
    if args.launcher:
        return run_for_launcher(args.action)
    return run_in_console(args.action)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. If freeze had completed, VNSee remains paused. Check status before retrying.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("SSH setup or the tablet command timed out. Reconnect USB and run status before retrying; VNSee may already be paused.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        print("Command failed (exit %d). Share remarkable_freeze.log if the error remains; keep USB connected." % error.returncode, file=sys.stderr)
        sys.exit(1)
    except (OSError, RuntimeError) as error:
        print("Stopped: " + str(error), file=sys.stderr)
        sys.exit(1)

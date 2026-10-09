# reMarkable Monitor 1.5.3

For **Windows 11 x64** and **reMarkable 2 over USB**. Sleep controls require tablet firmware **3.28.x**.

`remarkable_cursor_proxy.py` bridges VNSee to TightVNC, embeds the mouse pointer in the image, and filters duplicate redraws. `RemarkableMonitor.exe` launches the helpers, configures the tablet display to **1404 × 1872, portrait, 225%, 60 Hz**, places it left of the existing desktop, selects that capture area, and starts the proxy. It configures only the tablet display.

## Dependencies

Already configured? Skip to Setup.

| Dependency | Install / configure |
| --- | --- |
| [Python](https://www.python.org/downloads/windows/) 3.10+ x64 | Install with `py.exe` on PATH; verify `py -3 --version`. Optional acceleration: `py -3 -m pip install numpy`. |
| [OpenSSH Client](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse) | Install through Windows Optional features. Verify USB access with `ssh root@10.11.99.1`; the tablet's root password is under Settings → Help → Copyrights and licenses. |
| [Virtual Display Driver / VDD Control](https://github.com/VirtualDrivers/Virtual-Display-Driver) | Install its x64 Visual C++ prerequisite and driver per the project instructions. Enable **one virtual monitor**, add **1872 × 1404 @ 60 Hz**, restart that driver, and select **Extend** in Windows Display settings. |
| [TightVNC Server](https://www.tightvnc.com/download.php) | Install in **application mode**, with no server service running. Application settings: port **5900**, VNC/control authentication off, **allow loopback** and **loopback only** on. In `tvnserver.exe` Compatibility properties, set the high-DPI override to **Application**. |
| [XOVI](https://github.com/asivery/rm-xovi-extensions), [AppLoad](https://github.com/asivery/rm-appload), [VNSee-QTFB](https://github.com/asivery/vnsee) | Install **rM2 / arm32** releases per their instructions. Keep XOVI at `/home/root/xovi` with qt-resource-rebuilder; run `xovi/rebuild_hashtable` and `xovi/start` over SSH. Extract VNSee into `/home/root/xovi/exthome/appload/`, retaining its four standard launcher entries. |
| Bundled sleep extension | Install once using the Setup command below; included QMD/helper, no internet download. Requires USB and active XOVI. |

## Setup

1. Extract the whole ZIP into a permanent, writable folder, including `tablet-sleep/`.
2. Defaults in `monitor_config.json` detect TightVNC and the USB host address. With several VDD adapters, the launcher selects **monitor 2** (the second active output, with the primary first), then follows its adapter through driver restarts. To override it, use **Diagnostics** and set `adapter_id`, doubling backslashes for JSON. Optional overrides: `tightvnc_executable`, `ssh_key` (unencrypted Ed25519 key), `tablet_ip`, `usb_network`, `usb_host_ip`, `ssh_port`. Never store a password here.
3. Connect USB, wake the tablet, and activate XOVI. For first use on this PC or a changed USB address, run the following from this folder in **Administrator PowerShell under your normal account**:

   ```powershell
   py -3 -u .\start_remarkable_monitor.py --configure
   ```

   This sets VNSee to the detected PC address on **5902**, creates a USB-only firewall rule, and enrolls a per-user SSH key. OpenSSH may request that tablet's password once; later actions reuse `%USERPROFILE%\.ssh\remarkable_monitor_ed25519`. This key survives package updates. No password is stored in the package or read from environment variables.
4. If you ran the setup command, wait for **Cursor proxy ready** and press **Ctrl+C**. Close the launcher and install the bundled sleep extension once:

   ```powershell
   py -3 -u .\remarkable_sleep.py install
   ```

   Wait for **SLEEP_SETUP_READY**. Installation may restart the tablet UI once. If already installed and active, it verifies setup without restarting. Freeze/Preserve/Stop never install it or restart the UI.
5. Run `RemarkableMonitor.exe` and allow administrator access; monitor setup starts automatically. Open AppLoad → Reload → **VNSee Fastest** or **25 ms** on the tablet. Minimize the launcher to keep it running; optionally pin its taskbar icon.

Optional 25 ms installer: `py -3 .\install_vnsee_faster.py --delay-ms 25` after SSH setup; requires VNSee rM2 **1.1.0**. **Skip driver restart** reuses an active virtual monitor. Diagnostics is read-only; logs appear beside the EXE.

If earlier attempts left the UI stopped or XOVI inactive, restore it before installation (default USB address/key):

```powershell
ssh -i "$env:USERPROFILE\.ssh\remarkable_monitor_ed25519" root@10.11.99.1 "systemctl reset-failed xochitl && /home/root/xovi/start"
```

Wait for the tablet UI, then run the install command. Use your configured address/key if different. Built-in BusyBox is sufficient; no GNU find or base64 package is needed.

## Controls

- **Freeze:** use the current picture for this sleep, then sleep and disconnect the proxy.
- **Freeze and preserve:** retain the picture for future sleeps until Reset while XOVI is active, then sleep and disconnect the proxy.
- **Reset:** restore the default sleep screen; keep the monitor running.
- **Stop:** request sleep using the selected sleep screen, then stop the proxy.

Let the picture finish drawing before Freeze. Wait for **Sleep requested** before disconnecting USB. Wake and return to AppLoad manually. Closing stops the proxy; use Stop/Freeze to request sleep.

This testing build verifies display settings using adapter identities, tolerating temporary Windows display renumbering. 
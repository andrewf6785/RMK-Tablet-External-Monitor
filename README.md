## Included In This Repository

See gallery for pictures and videos of working tablet, and what the windows executable looks like.

Included in this repo is the python script that allows for the cursor to be tracked through vnsee, and the windows executable that allows for launching and arranging the virtual monitors.

This is not a currently supported project: it worked for me on my machine running Windows 11 as of October, 2026. The release in this repository is generalized, with relations to my machine removed.

See below for dependencies & references. I have stood on the shoulders of giants throughout this project. I have them to thank for a lot of this work already being done and researched.


## Converting an old Remarkable 2 tablet into an external monitor

The Remarkable 2 is an e-ink note taking device, used with a stylus for writing daily plans, drawing, or, as you'd expect, taking notes. E-ink technology is super cool: instead of shining a backlight through crystals and filters as in a traditional LCD display, e-ink uses "pixels" comprised of small containers of charged liquid. When a charge is applied to these pixels, the correct color (in the case of the Remarkable 2, either black or white) kind of bubbles to the top and stays there. This gives e-ink displays some really cool properties. In my opinion, the coolest is that they produce literally zero light. You need an external light to use them in the dark. Also, once an image is drawn, they use essentially no power to hold that image there. The correct "color" stays on the display and doesn't move.

I was gifted a Remarkable 2 about 2 years ago when I started college. I generally prefer actual paper for note-taking, so I didn't ever use it that much and it mostly collected dust on my desk. However, around the end of September I developed a bit of a fascination with e-ink displays and got curious as to whether I could convert the old tablet into an external monitor for my desktop pc, running Windows 11.

## Initial Problems

I did some research, and pretty quickly found a github repo (see references) where someone had managed to convert the Remarkable 1 into an external monitor using VNsee and virtual displays. I also found a reddit post where someone had managed to do this with the Remarkable 2. So I knew that it was possible.

There were some initial problems. The github repo was designed for use on linux systems, not Windows. It linked to a workaround for Windows, but the user there only managed to get it to work using paid software for virtual displays that allowed him to create a virtual monitor with the specific resolution.

I did some more research and figured out how to modify VDD Control (the software from the original github repo) to allow for the specific resolution of the remarkable tablet.

The resolution was such an issue because Tightvnc and VNsee did not want to project to the Remarkable tablet unless the resolution was matched exactly, and in general the tablet has an irregular resolution that isn't really built in anywhere.

## Progress

After resolving the resolution issue, it was a simple case of installing VNsee on the tablet via ssh and setting up the exact virtual display drivers through VDDControl, which was largely handled by the existing software. This was the first time I managed to actually get an image on the Tablet and it looked sick. E-ink displays have really bad refresh rates and the latency is terrible, but once the image actually loads is super crisp and nice to read. Because there is no backlight, it's also very easy on the eyes.

## Other Problems and an Overkill Solution

So I had the monitor working, and I thought it looked good and I was generally happy with the setup, but there was a big problem. Setting up the tablet from system reset took way too long. Like 5 minutes way too long. (ok it's not that bad but I didn't like it.)

The process originally looked something like this:

I had to manually start up two separate programs (tight vnc and VDD Control).

Then, restart the virtual display drivers.

But don't connect to the tablet yet, now you have to go to windows display settings and move the virtual monitor into the correct position, set its custom resolution (thankfully handled by VDD Control after above modifications), set its refresh rate to a reasonable number (like 30), and share that monitor with Tight Vnc using a powershell command.

> A note on refresh rate: When I was doing research for this project, I found that reddit post where someone else had managed this. In that video, he has the refresh rate cranked really high, which makes for an overall more useable experience of his monitor in my opinion. I decided not to do this for mainly one reason: longevity. I want this monitor to last for a long time. One limitation of e-ink displays is that, in general, after a few million refreshes the pixels can degrade. As such, I let the device refresh less.

After this, I could finally connect to the tablet via a physical wire and ssh. (Remarkable actually makes it super easy to connect to their products, which I greatly appreciate).

Through ssh, tell the tablet to start up VNsee.

Finally, you can activate the virtual display through VNsee.

And after all of that, the mouse didn't appear correctly on the monitor.

To fix these issues I decided to write a few things. First, a custom python script that tracks the mouse and places it onto the Remarkable tablet. Doing anything without a mouse cursor was too annoying. Second, I wrote a custom windows executable that basically just does the above process for me. The hardest part of that was getting it to correctly position the virtual monitor: The windows API makes doing stuff like that fairly difficult.

## Quality of Life

Version 1 of this executable (picture attached) had pretty barebones functionality. Essentially just start/stop. But, I wanted to make use of some of the other cool features of e-ink displays so I decided to add some other features. Primarily, I really liked that E-Ink displays could act like a changeable picture frame when not in use: the screen can be set to pretty much anything and then uses almost no power to stay like that. Which is good, because when my pc is powered off the tablet doesn't get trickle charged, so it has to handle its own power situation.

So, in version 2 I added a few things. Primarily, a freeze and a preserve functionality. Freeze sets whatever is currently displayed on the monitor as the default sleep screen for the device, puts it to sleep, and then disconnects ssh. This allows me to place a static image on the device and it kind of sits there, looking nice, even when everything is powered off. Preserve does the same thing, but it sets that as the default sleep screen for the tablet, so it displays that image whenever it goes to sleep.

## Skills Learned

This project taught me a lot of valuable skills. I got more familiar with the windows API, learned how to securely manage ssh passwords, and the general knowledge and resourcefulness that comes from learning how to modify a device that wasn't originally intended for a certain purpose to be used in that way. In the future, I would really like to do this again with a color e-ink display. This would allow for a better picture quality and a more diverse range of images and use cases. Also it would be really cool.


## References

- <https://github.com/matteodelabre/vnsee>
- <https://www.reddit.com/r/RemarkableTablet/comments/k3t9ea/using_the_rm2_as_an_external_monitor_x11vncvnsee/>
- <https://github.com/matteodelabre/vnsee/issues/13>

#
#

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

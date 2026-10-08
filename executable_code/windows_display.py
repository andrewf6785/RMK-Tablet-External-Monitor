"""Windows CCD/GDI monitor configuration bound to a PnP adapter, never a display number.

DPI packets -3/-4 are undocumented Windows interfaces; all mutations are read back.
Run display changes in a bounded child process because native display APIs may stall.
"""
import ctypes as C
import json
import os
import struct
import time

U32, I32 = C.c_uint32, C.c_int32
PTR = C.c_void_p
SCALE_STEPS = (100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)


def source_mode_index(path):
    packed, flags = struct.unpack_from('<I', path, 12)[0], struct.unpack_from('<I', path, 68)[0]
    return (packed >> 16) & 0xffff if flags & 8 else packed


def dpi_relative(minimum, current, maximum, percent=225):
    recommended = -minimum
    if not 0 <= recommended < len(SCALE_STEPS):
        raise RuntimeError('Windows returned an unsupported DPI scale range (custom scaling may be active).')
    desired = SCALE_STEPS.index(percent) - recommended
    if not minimum <= desired <= maximum:
        raise RuntimeError('%d%% is outside this monitor\'s supported scale range '
                           '(relative min=%d, current=%d, max=%d).' %
                           (percent, minimum, current, maximum))
    actual = recommended + current
    if not 0 <= actual < len(SCALE_STEPS):
        raise RuntimeError('Windows returned an unsupported current DPI scale.')
    return desired, SCALE_STEPS[actual]


def choose_monitors(records, instance):
    selected = [r for r in records if r['instance'].upper() == instance.upper()]
    primary = [r for r in records if r['primary']]
    if len(selected) != 1:
        raise RuntimeError('Expected one active output from %s; found %d. See --diagnose.' % (instance, len(selected)))
    if len(primary) != 1 or primary[0]['gdi'] == selected[0]['gdi'] or \
            primary[0]['instance'].upper().startswith('ROOT\\DISPLAY\\'):
        raise RuntimeError('A separate, unique physical primary monitor is required.')
    return primary[0], selected[0]


def share_rectangle(record):
    # TightVNC's documented negative-coordinate syntax includes both '+' and '-'.
    return '%dx%d+%d+%d' % (record['width'], record['height'], record['x'], record['y'])


def tablet_position(records, instance, width):
    main, tablet = choose_monitors(records, instance)
    # Keep existing monitors in place even when another screen is left of primary.
    others = [r for r in records if r['gdi'] != tablet['gdi']]
    return min(r['x'] for r in others) - width, main['y']


class WindowsDisplay:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Monitor setup requires Windows 11.')
        self.user = C.WinDLL('user32', use_last_error=True)
        self.setup = C.WinDLL('setupapi', use_last_error=True)
        self.callback_type = C.WINFUNCTYPE(I32, PTR, PTR, PTR, C.c_ssize_t)
        signatures = {
            'GetDisplayConfigBufferSizes': ([U32, C.POINTER(U32), C.POINTER(U32)], I32),
            'QueryDisplayConfig': ([U32, C.POINTER(U32), PTR, C.POINTER(U32), PTR, PTR], I32),
            'DisplayConfigGetDeviceInfo': ([PTR], I32),
            'DisplayConfigSetDeviceInfo': ([PTR], I32),
            'SetDisplayConfig': ([U32, PTR, U32, PTR, U32], I32),
            'EnumDisplayMonitors': ([PTR, PTR, self.callback_type, C.c_ssize_t], I32),
            'GetMonitorInfoW': ([PTR, PTR], I32),
            'EnumDisplaySettingsExW': ([C.c_wchar_p, U32, PTR, U32], I32),
            'EnumDisplaySettingsW': ([C.c_wchar_p, U32, PTR], I32),
            'ChangeDisplaySettingsExW': ([C.c_wchar_p, PTR, PTR, U32, PTR], I32),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in [
            ('SetupDiGetClassDevsW', [PTR, C.c_wchar_p, PTR, U32], PTR),
            ('SetupDiOpenDeviceInterfaceW', [PTR, C.c_wchar_p, U32, PTR], I32),
            ('SetupDiGetDeviceInterfaceDetailW', [PTR, PTR, PTR, U32, C.POINTER(U32), PTR], I32),
            ('SetupDiGetDeviceInstanceIdW', [PTR, PTR, C.c_wchar_p, U32, C.POINTER(U32)], I32),
            ('SetupDiDestroyDeviceInfoList', [PTR], I32),
        ]:
            fn = getattr(self.setup, name)
            fn.argtypes, fn.restype = args, result
        self.user.SetThreadDpiAwarenessContext.argtypes = [PTR]
        self.user.SetThreadDpiAwarenessContext.restype = PTR
        if not self.user.SetThreadDpiAwarenessContext(PTR(-4)):
            raise RuntimeError('Could not enable per-monitor DPI awareness.')

    @staticmethod
    def buffer(data):
        return C.create_string_buffer(bytes(data), len(data))

    @staticmethod
    def wide(data):
        return bytes(data).decode('utf-16-le').split('\0', 1)[0]

    @staticmethod
    def check(code, operation):
        if code:
            raise RuntimeError('%s failed (Windows error %d).' % (operation, code))

    def device_info(self, kind, luid, identity, size):
        raw = bytearray(size)
        struct.pack_into('<II8sI', raw, 0, kind & 0xffffffff, size, luid, identity)
        buf = self.buffer(raw)
        self.check(self.user.DisplayConfigGetDeviceInfo(buf), 'DisplayConfigGetDeviceInfo(%d)' % kind)
        return buf.raw

    def instance_id(self, interface_path):
        handle = self.setup.SetupDiGetClassDevsW(None, None, None, 4)
        if handle in (None, PTR(-1).value):
            raise C.WinError(C.get_last_error())
        try:
            native_size = 32 if C.sizeof(PTR) == 8 else 28
            interface, device = C.create_string_buffer(native_size), C.create_string_buffer(native_size)
            struct.pack_into('<I', interface, 0, native_size)
            struct.pack_into('<I', device, 0, native_size)
            if not self.setup.SetupDiOpenDeviceInterfaceW(handle, interface_path, 0, interface):
                raise C.WinError(C.get_last_error())
            needed = U32()
            self.setup.SetupDiGetDeviceInterfaceDetailW(handle, interface, None, 0, C.byref(needed), None)
            if not 6 <= needed.value <= 65536:
                raise RuntimeError('Invalid SetupAPI interface detail size.')
            detail = C.create_string_buffer(needed.value)
            struct.pack_into('<I', detail, 0, 8 if C.sizeof(PTR) == 8 else 6)
            if not self.setup.SetupDiGetDeviceInterfaceDetailW(handle, interface, detail, needed, C.byref(needed), device):
                raise C.WinError(C.get_last_error())
            result = C.create_unicode_buffer(1024)
            if not self.setup.SetupDiGetDeviceInstanceIdW(handle, device, result, len(result), None):
                raise C.WinError(C.get_last_error())
            return result.value
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(handle)

    def topology(self):
        for _ in range(5):
            count, mode_count = U32(), U32()
            self.check(self.user.GetDisplayConfigBufferSizes(0x12, C.byref(count), C.byref(mode_count)), 'CCD buffer sizes')
            if not 0 < count.value <= 1024 or not 0 < mode_count.value <= 8192:
                raise RuntimeError('Invalid active CCD topology size.')
            paths, modes = C.create_string_buffer(count.value * 72), C.create_string_buffer(mode_count.value * 64)
            code = self.user.QueryDisplayConfig(0x12, C.byref(count), paths, C.byref(mode_count), modes, None)
            if code == 122:
                continue
            self.check(code, 'QueryDisplayConfig')
            return bytearray(paths.raw[:count.value * 72]), bytearray(modes.raw[:mode_count.value * 64])
        raise RuntimeError('Display topology kept changing during enumeration.')

    def live_monitors(self):
        records, errors = {}, []
        def callback(handle, dc, rect, param):
            data = C.create_string_buffer(104)
            struct.pack_into('<I', data, 0, 104)
            if not self.user.GetMonitorInfoW(handle, data):
                errors.append(C.get_last_error())
                return 0
            left, top, right, bottom = struct.unpack_from('<4i', data, 4)
            name = self.wide(data.raw[40:104])
            records[name] = dict(gdi=name, x=left, y=top, width=right-left, height=bottom-top,
                                 primary=bool(struct.unpack_from('<I', data, 36)[0] & 1))
            return 1
        handler = self.callback_type(callback)
        if not self.user.EnumDisplayMonitors(None, None, handler, 0) or errors:
            raise RuntimeError('Could not enumerate the live desktop monitors: %r' % errors)
        return records

    def snapshot(self):
        paths, modes = self.topology()
        monitors, adapters, records = self.live_monitors(), {}, []
        for offset in range(0, len(paths), 72):
            path = paths[offset:offset+72]
            luid, identity = bytes(path[:8]), struct.unpack_from('<I', path, 8)[0]
            if luid not in adapters:
                interface = self.wide(self.device_info(4, luid, 0, 276)[20:])
                adapters[luid] = self.instance_id(interface)
            gdi = self.wide(self.device_info(1, luid, identity, 84)[20:])
            if gdi not in monitors:
                raise RuntimeError('CCD source %s has no live monitor yet.' % gdi)
            index = source_mode_index(path)
            if index >= len(modes) // 64:
                raise RuntimeError('CCD returned an invalid source mode index.')
            mode = modes[index*64:(index+1)*64]
            if struct.unpack_from('<II', mode, 0) != (1, identity) or mode[8:16] != luid:
                raise RuntimeError('CCD source mode identity mismatch.')
            record = dict(monitors[gdi], instance=adapters[luid], source_id=identity,
                          luid=luid.hex(), mode_index=index, path_offset=offset,
                          rotation=struct.unpack_from('<I', path, 40)[0],
                          raw_width=struct.unpack_from('<I', mode, 16)[0],
                          raw_height=struct.unpack_from('<I', mode, 20)[0])
            records.append(record)
        return records, paths, modes

    def mode(self, name, index=0xffffffff):
        buf = C.create_string_buffer(220)
        struct.pack_into('<H', buf, 68, 220)
        if not self.user.EnumDisplaySettingsExW(name, index, buf, 0):
            if not self.user.EnumDisplaySettingsW(name, index, buf):
                return None
        return bytearray(buf.raw)

    def supported(self, name):
        for i in range(4096):
            mode = self.mode(name, i)
            if mode is None:
                return
            yield mode
        raise RuntimeError('Too many display modes.')

    def desired_mode(self, tablet, main, position=None):
        current = self.mode(tablet['gdi'])
        if current is None:
            raise RuntimeError('Current display mode is unreadable for %s.' % tablet['gdi'])
        supported = list(self.supported(tablet['gdi']))
        if not any(sorted(struct.unpack_from('<II', m, 172)) == [1404, 1872] and
                   struct.unpack_from('<I', m, 184)[0] == 60 for m in supported):
            raise RuntimeError('VDD does not advertise 1872 x 1404 / 1404 x 1872 at 60 Hz. '
                               'Add that mode to its existing settings XML, restart the kept adapter, and retry.')
        desired = bytearray(current)
        fields = 0x5800a0  # Position, orientation, width, height, refresh; never make primary.
        if struct.unpack_from('<I', desired, 168)[0]:
            fields |= 0x40000
        x, y = position if position is not None else (main['x']-1404, main['y'])
        struct.pack_into('<IiiI', desired, 72, fields, x, y, 1)
        struct.pack_into('<II', desired, 172, 1404, 1872)
        struct.pack_into('<I', desired, 184, 60)
        self.check(self.user.ChangeDisplaySettingsExW(tablet['gdi'], self.buffer(desired), None, 2, None), 'Test portrait mode')
        return desired

    def move_left(self, instance):
        records, paths, modes = self.snapshot()
        main, tablet = choose_monitors(records, instance)
        x, y = tablet_position(records, instance, tablet['width'])
        if (tablet['x'], tablet['y']) == (x, y):
            return
        struct.pack_into('<ii', modes, tablet['mode_index'] * 64 + 28, x, y)
        pb, mb = self.buffer(paths), self.buffer(modes)
        code = self.user.SetDisplayConfig(len(paths)//72, pb, len(modes)//64, mb, 0x8060)
        if code == 0:
            self.check(self.user.SetDisplayConfig(len(paths)//72, pb, len(modes)//64, mb, 0x82a0), 'CCD placement')
        else:
            current = self.mode(tablet['gdi'])
            if current is None:
                raise RuntimeError('Position-only placement could not read the current mode.')
            struct.pack_into('<Iii', current, 72, 0x20, x, y)
            self.check(self.user.ChangeDisplaySettingsExW(tablet['gdi'], self.buffer(current), None, 1, None), 'GDI placement')
        self.wait_layout(instance, lambda m, t: (t['x'], t['y']) == (x, y))

    def dpi(self, record, set_percent=None):
        luid = bytes.fromhex(record['luid'])
        get = self.device_info(-3, luid, record['source_id'], 32)
        minimum, current, maximum = struct.unpack_from('<iii', get, 20)
        relative, actual = dpi_relative(minimum, current, maximum, set_percent or 225)
        if set_percent is not None and actual != set_percent:
            data = struct.pack('<II8sIi', 0xfffffffc, 24, luid, record['source_id'], relative)
            self.check(self.user.DisplayConfigSetDeviceInfo(self.buffer(data)), 'Set per-monitor DPI')
        return actual

    def wait_layout(self, instance, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        error = 'No settled matching monitor.'
        while time.monotonic() < deadline:
            try:
                records, _, _ = self.snapshot()
                main, tablet = choose_monitors(records, instance)
                if predicate(main, tablet):
                    return main, tablet
            except (OSError, RuntimeError) as exc:
                error = str(exc)
            time.sleep(.25)
        raise RuntimeError('Display read-back timed out: ' + error)

    def native_mode_ready(self, tablet):
        mode = self.mode(tablet['gdi'])
        return (tablet['width'], tablet['height'], tablet['rotation']) == (1404, 1872, 2) and \
            mode is not None and struct.unpack_from('<I', mode, 184)[0] == 60

    def set_scale_after_native_mode(self, instance, percent=225):
        # The supported DPI range can lag behind the mode change. Re-resolve the
        # source on each bounded retry, and never query/set DPI at the reset mode.
        def scaled(main, tablet):
            return self.native_mode_ready(tablet) and self.dpi(tablet, set_percent=percent) == percent
        return self.wait_layout(instance, scaled)

    def configure(self, instance):
        records, _, _ = self.snapshot()
        main, tablet = choose_monitors(records, instance)
        self.desired_mode(tablet, main, tablet_position(records, instance, 1404))
        physical_before = {r['gdi']: (r['x'], r['y'], r['width'], r['height'], r['rotation'])
                           for r in records if not r['instance'].upper().startswith('ROOT\\DISPLAY\\')}
        self.move_left(instance)       # Required initial placement with current visible width.
        records, _, _ = self.snapshot()
        main, tablet = choose_monitors(records, instance)
        current = self.mode(tablet['gdi'])
        refresh = struct.unpack_from('<I', current, 184)[0] if current is not None else 0
        if (tablet['width'], tablet['height'], tablet['rotation'], refresh) != (1404, 1872, 2, 60):
            desired = self.desired_mode(tablet, main, tablet_position(records, instance, 1404))
            self.check(self.user.ChangeDisplaySettingsExW(tablet['gdi'], self.buffer(desired), None, 1, None), 'Apply portrait mode')
        main, tablet = self.wait_layout(instance, lambda m, t: self.native_mode_ready(t))
        print('Verified native mode: 1404 x 1872, Portrait, 60 Hz. Applying 225% scale.', flush=True)
        main, tablet = self.set_scale_after_native_mode(instance)
        print('Verified tablet scale: 225%. Checking final left placement.', flush=True)
        self.move_left(instance)
        records, _, _ = self.snapshot()
        position = tablet_position(records, instance, 1404)
        def final(main, tablet):
            mode = self.mode(tablet['gdi'])
            return (tablet['width'], tablet['height'], tablet['rotation']) == (1404, 1872, 2) and \
                (tablet['x'], tablet['y']) == position and \
                mode is not None and struct.unpack_from('<I', mode, 184)[0] == 60 and self.dpi(tablet) == 225
        main, tablet = self.wait_layout(instance, final)
        records, _, _ = self.snapshot()
        physical_after = {r['gdi']: (r['x'], r['y'], r['width'], r['height'], r['rotation'])
                          for r in records if not r['instance'].upper().startswith('ROOT\\DISPLAY\\')}
        if physical_after != physical_before:
            raise RuntimeError('A physical display changed unexpectedly; setup stopped. See startup.log.')
        return tablet

    def settled(self, instance, seconds=25):
        started, stable_since, previous = time.monotonic(), None, None
        error = 'No matching active output.'
        while time.monotonic() - started < seconds:
            try:
                records, _, _ = self.snapshot()
                choose_monitors(records, instance)
                signature = json.dumps(records, sort_keys=True)
                if signature != previous:
                    previous, stable_since = signature, time.monotonic()
                if time.monotonic()-started >= 3 and time.monotonic()-stable_since >= 1:
                    return records
            except (OSError, RuntimeError) as exc:
                previous, stable_since, error = None, None, str(exc)
            time.sleep(.25)
        raise RuntimeError('Display mapping did not settle: ' + error)

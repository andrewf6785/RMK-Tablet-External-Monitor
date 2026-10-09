"""RFB framebuffer comparison for the VNSee cursor proxy (standard library only)."""

import struct
import threading
import time

try:
    import numpy as _np
except ImportError:
    _np = None

SUPPORTED_ENCODINGS = {0, 1, 5, -224, -223, -308, -313}


class ProtocolError(Exception):
    pass


def read_exact(sock, count):
    chunks = bytearray()
    while len(chunks) < count:
        part = sock.recv(min(count - len(chunks), 65536))
        if not part:
            raise EOFError("Connection closed")
        chunks.extend(part)
    return bytes(chunks)


class BufferedSocket:
    """Avoid a socket system call for each Hextile tile's small fields."""
    def __init__(self, sock):
        self.sock = sock
        self.data = b""
        self.offset = 0
        self.received_bytes = 0
        self.read_wait = 0.0

    def recv(self, count):
        if self.offset == len(self.data):
            started = time.perf_counter()
            self.data = self.sock.recv(65536)
            self.read_wait += time.perf_counter() - started
            self.received_bytes += len(self.data)
            self.offset = 0
        end = min(self.offset + count, len(self.data))
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def read_exact(self, count):
        if self.offset + count <= len(self.data):
            end = self.offset + count
            value = self.data[self.offset:end]
            self.offset = end
            return value
        return read_exact(self, count)


class FrameFilter:
    def __init__(self, width, height, pixel_bytes=4, acceleration=True):
        self.width, self.height = width, height
        self.accelerated = bool(acceleration and _np is not None)
        self.lock = threading.RLock()
        self.frames = 0
        self.suppressed_frames = 0
        self.suppressed_rectangles = 0
        self.reset(pixel_bytes)

    def reset(self, pixel_bytes=None):
        with self.lock:
            if pixel_bytes is not None:
                if pixel_bytes not in (1, 2, 4):
                    raise ProtocolError("Unsupported VNC pixel size.")
                self.pixel_bytes = pixel_bytes
            self.stride = self.width * self.pixel_bytes
            self.pixels = bytearray(self.stride * self.height)
            self.known = bytearray(self.width * self.height)
            self.known_count = 0
            self._original_blocks = None
            self.journal_bytes = 0

    @property
    def all_known(self):
        return self.known_count == self.width * self.height

    def check_rectangle(self, x, y, width, height):
        if min(x, y, width, height) < 0 or x + width > self.width or y + height > self.height:
            raise ProtocolError("VNC rectangle extends outside the tablet framebuffer.")

    def invalidate(self, x, y, width, height):
        with self.lock:
            self.check_rectangle(x, y, width, height)
            for row in range(y, y + height):
                start = row * self.width + x
                self.known_count -= self.known[start:start + width].count(1)
                self.known[start:start + width] = bytes(width)

    def remember_original(self, start, size):
        """Journal only changed 4 KiB blocks, once each, for cancel-out detection."""
        if self._original_blocks is None or not size:
            return
        for block in range(start // 4096, (start + size - 1) // 4096 + 1):
            if block not in self._original_blocks:
                offset = block * 4096
                original = bytes(self.pixels[offset:offset + 4096])
                self._original_blocks[block] = original
                self.journal_bytes += len(original)

    def apply_pixels(self, x, y, width, height, data):
        self.check_rectangle(x, y, width, height)
        if not width or not height:
            return False
        if len(data) != width * height * self.pixel_bytes:
            raise ProtocolError("VNC pixel rectangle has an incorrect length.")
        if (x, y, width, height) == (0, 0, self.width, self.height):
            changed = not self.all_known or self.pixels != data
            if changed:
                self.remember_original(0, len(data))
                self.pixels[:] = data
                if not self.all_known:
                    self.known[:] = b"\x01" * len(self.known)
                    self.known_count = len(self.known)
            return changed
        row_size = width * self.pixel_bytes
        changed = False
        all_known = self.all_known
        ones = b"\x01" * width
        for row in range(height):
            start = (y + row) * self.stride + x * self.pixel_bytes
            incoming = data[row * row_size:(row + 1) * row_size]
            if self.pixels[start:start + row_size] != incoming:
                self.remember_original(start, row_size)
                self.pixels[start:start + row_size] = incoming
                changed = True
            if not all_known:
                valid_start = (y + row) * self.width + x
                unknown = self.known[valid_start:valid_start + width].count(0)
                changed |= bool(unknown)
                self.known_count += unknown
                self.known[valid_start:valid_start + width] = ones
        return changed

    def copy_rectangle(self, x, y, width, height, source_x, source_y):
        self.check_rectangle(source_x, source_y, width, height)
        self.check_rectangle(x, y, width, height)
        row_size = width * self.pixel_bytes
        pixels = b"".join(self.pixels[(source_y + row) * self.stride + source_x * self.pixel_bytes:
                                     (source_y + row) * self.stride + source_x * self.pixel_bytes + row_size]
                          for row in range(height))
        if self.all_known:
            return self.apply_pixels(x, y, width, height, pixels)
        # Copy source validity as well; unknown source pixels must never be deduplicated.
        validity = b"".join(self.known[(source_y + row) * self.width + source_x:
                                       (source_y + row) * self.width + source_x + width]
                            for row in range(height))
        self.apply_pixels(x, y, width, height, pixels)
        for row in range(height):
            start = (y + row) * self.width + x
            incoming = validity[row * width:(row + 1) * width]
            self.known_count += incoming.count(1) - self.known[start:start + width].count(1)
            self.known[start:start + width] = incoming
        return bool(width and height)

    def hextile(self, reader, width, height):
        if self.accelerated and width * height >= 4096:
            return self.hextile_tiled(reader, width, height)
        stride = width * self.pixel_bytes
        decoded = bytearray(stride * height)
        wire = bytearray()
        background = foreground = None
        base_pixel = bytes(self.pixel_bytes)
        read = reader.read_exact if isinstance(reader, BufferedSocket) else lambda count: read_exact(reader, count)

        def take(count):
            value = read(count)
            wire.extend(value)
            return value

        def fill(x, y, w, h, pixel):
            value = pixel * w
            for row in range(y, y + h):
                start = row * stride + x * self.pixel_bytes
                decoded[start:start + len(value)] = value

        for tile_y in range(0, height, 16):
            tile_h = min(16, height - tile_y)
            for tile_x in range(0, width, 16):
                tile_w = min(16, width - tile_x)
                flags = take(1)[0]
                if flags & 1:
                    raw = take(tile_w * tile_h * self.pixel_bytes)
                    row_size = tile_w * self.pixel_bytes
                    for row in range(tile_h):
                        start = (tile_y + row) * stride + tile_x * self.pixel_bytes
                        decoded[start:start + row_size] = raw[row * row_size:(row + 1) * row_size]
                    continue
                if flags & ~31:
                    raise ProtocolError("Invalid Hextile tile flags.")
                if flags & 2:
                    background = take(self.pixel_bytes)
                if background is None:
                    raise ProtocolError("Hextile background was not specified.")
                if tile_x == 0 and tile_y == 0:
                    base_pixel = background
                    decoded[:] = background * (width * height)
                elif background != base_pixel:
                    fill(tile_x, tile_y, tile_w, tile_h, background)
                if flags & 4:
                    foreground = take(self.pixel_bytes)
                if flags & 8:
                    count = take(1)[0]
                    for _ in range(count):
                        pixel = take(self.pixel_bytes) if flags & 16 else foreground
                        if pixel is None:
                            raise ProtocolError("Hextile foreground was not specified.")
                        xy, wh = take(2)
                        sx, sy, sw, sh = xy >> 4, xy & 15, (wh >> 4) + 1, (wh & 15) + 1
                        if sx + sw > tile_w or sy + sh > tile_h:
                            raise ProtocolError("Hextile subrectangle extends outside its tile.")
                        fill(tile_x + sx, tile_y + sy, sw, sh, pixel)
        return wire, decoded

    def hextile_tiled(self, reader, width, height):
        """Decode compact tiles, then convert tile order to pixel order in native code.

        Building the small tile buffers avoids thousands of strided Python row
        writes. Wire bytes stay unchanged; only local pixel comparison is faster.
        """
        rows, cols = (height + 15) // 16, (width + 15) // 16
        tile_size = 256 * self.pixel_bytes
        packed = bytearray(rows * cols * tile_size)
        packed_view = memoryview(packed)
        tile_index = 0
        wire = bytearray()
        background = foreground = uniform_pixel = None
        uniform = True
        read = reader.read_exact if isinstance(reader, BufferedSocket) else lambda count: read_exact(reader, count)
        tile_stride = 16 * self.pixel_bytes

        def take(count):
            value = read(count)
            wire.extend(value)
            return value

        for tile_y in range(0, height, 16):
            tile_h = min(16, height - tile_y)
            for tile_x in range(0, width, 16):
                tile_w = min(16, width - tile_x)
                tile = packed_view[tile_index * tile_size:(tile_index + 1) * tile_size]
                tile_index += 1
                flags = take(1)[0]
                if flags & 1:
                    uniform = False
                    raw = take(tile_w * tile_h * self.pixel_bytes)
                    if tile_w == tile_h == 16:
                        tile[:] = raw
                    else:
                        row_size = tile_w * self.pixel_bytes
                        for row in range(tile_h):
                            tile[row * tile_stride:row * tile_stride + row_size] = raw[row * row_size:(row + 1) * row_size]
                    continue
                if flags & ~31:
                    raise ProtocolError("Invalid Hextile tile flags.")
                if flags & 2:
                    background = take(self.pixel_bytes)
                if background is None:
                    raise ProtocolError("Hextile background was not specified.")
                if uniform_pixel is None:
                    uniform_pixel = background
                elif background != uniform_pixel:
                    uniform = False
                tile[:] = background * 256
                if flags & 4:
                    foreground = take(self.pixel_bytes)
                if flags & 8:
                    count = take(1)[0]
                    uniform &= count == 0
                    for _ in range(count):
                        pixel = take(self.pixel_bytes) if flags & 16 else foreground
                        if pixel is None:
                            raise ProtocolError("Hextile foreground was not specified.")
                        xy, wh = take(2)
                        sx, sy, sw, sh = xy >> 4, xy & 15, (wh >> 4) + 1, (wh & 15) + 1
                        if sx + sw > tile_w or sy + sh > tile_h:
                            raise ProtocolError("Hextile subrectangle extends outside its tile.")
                        value = pixel * sw
                        for row in range(sy, sy + sh):
                            start = row * tile_stride + sx * self.pixel_bytes
                            tile[start:start + len(value)] = value
        if not tile_index:
            return wire, b""
        if uniform:
            return wire, uniform_pixel * (width * height)
        # Treat each pixel as one opaque 1/2/4-byte item. No color conversion or
        # arithmetic is performed, so the original byte order is preserved.
        grid = _np.frombuffer(packed, dtype='u%d' % self.pixel_bytes).reshape(rows, cols, 16, 16)
        raster = _np.ascontiguousarray(grid.transpose(0, 2, 1, 3)).reshape(rows * 16, cols * 16)
        return wire, _np.ascontiguousarray(raster[:height, :width]).tobytes()

    def server_message(self, reader):
        kind = read_exact(reader, 1)[0]
        if kind == 0:
            header = read_exact(reader, 3)
            count = struct.unpack("!H", header[1:])[0]
            with self.lock:
                self.frames += 1
                self._original_blocks = {} if self.all_known else None
                self.journal_bytes = 0
                rectangles = []
                pixel_count = 0
                for _ in range(count):
                    rect = read_exact(reader, 12)
                    x, y, width, height, encoding = struct.unpack("!HHHHi", rect)
                    if encoding == -224:  # LastRect: replace sentinel count with actual count.
                        break
                    if encoding in (-223, -308):
                        if (width, height) != (self.width, self.height):
                            raise ProtocolError("VNC display size changed. Restore the tablet's native resolution before reconnecting.")
                        extra = b""
                        if encoding == -308:
                            extra = read_exact(reader, 4)
                            extra += read_exact(reader, extra[0] * 16)
                        self.reset()
                        rectangles.append((rect + extra, False))
                        continue
                    self.check_rectangle(x, y, width, height)
                    if encoding == 0:
                        payload = read_exact(reader, width * height * self.pixel_bytes)
                        changed = self.apply_pixels(x, y, width, height, payload)
                    elif encoding == 5:
                        payload, pixels = self.hextile(reader, width, height)
                        changed = self.apply_pixels(x, y, width, height, pixels)
                    elif encoding == 1:
                        payload = read_exact(reader, 4)
                        sx, sy = struct.unpack("!HH", payload)
                        changed = self.copy_rectangle(x, y, width, height, sx, sy)
                    else:
                        raise ProtocolError("Unexpected VNC encoding %d." % encoding)
                    pixel_count += 1
                    if changed:
                        rectangles.append((rect + payload, True))
                # Discard a whole frame if its changes cancel each other out.
                if self._original_blocks is not None and self.all_known and all(
                        self.pixels[block * 4096:block * 4096 + len(original)] == original
                        for block, original in self._original_blocks.items()):
                    rectangles = [item for item in rectangles if not item[1]]
                self._original_blocks = None
                retained = sum(item[1] for item in rectangles)
                self.suppressed_rectangles += pixel_count - retained
                if pixel_count and not retained:
                    self.suppressed_frames += 1
                output = b"\x00\x00" + struct.pack("!H", len(rectangles))
                # Empty updates keep the client's request loop alive without repaint callbacks.
                return output + b"".join(item[0] for item in rectangles)
        if kind == 1:
            header = read_exact(reader, 5)
            return b"\x01" + header + read_exact(reader, struct.unpack("!H", header[3:])[0] * 6)
        if kind in (2, 150):  # Bell / EndOfContinuousUpdates
            return bytes([kind])
        if kind == 3:
            header = read_exact(reader, 7)
            count = abs(struct.unpack("!i", header[3:])[0])
            if count > 16777216:
                raise ProtocolError("VNC clipboard message exceeds 16 MiB.")
            return b"\x03" + header + read_exact(reader, count)
        if kind == 248:
            header = read_exact(reader, 8)
            return b"\xf8" + header + read_exact(reader, header[7])
        raise ProtocolError("Unsupported VNC server message %d." % kind)

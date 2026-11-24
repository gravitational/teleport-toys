"""Tiny PNG encoder (stdlib) so the vision node can return frames in simulation."""
import struct
import zlib


def encode_rgb(width, height, pixels):
    """pixels: bytes of length width*height*3 (RGB, row-major)."""
    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def sim_scene(width, height, target_xy, target_rgb=(220, 40, 40)):
    """Gray floor gradient + a colored 'target' block. Good enough for the agent to reason about."""
    tx, ty = target_xy
    size = max(6, width // 10)
    buf = bytearray(width * height * 3)
    for y in range(height):
        shade = 60 + int(120 * y / max(1, height - 1))
        for x in range(width):
            i = (y * width + x) * 3
            if tx <= x < tx + size and ty <= y < ty + size:
                buf[i:i + 3] = bytes(target_rgb)
            else:
                buf[i:i + 3] = bytes((shade, shade, shade + 10))
    return encode_rgb(width, height, bytes(buf))

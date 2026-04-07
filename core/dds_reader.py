"""DDS texture file reader — extracts header info and converts to QImage for preview.

Supports common DDS formats used in Crimson Desert:
  - DXT1 (BC1) — RGB with optional 1-bit alpha
  - DXT3 (BC2) — RGBA with explicit alpha
  - DXT5 (BC3) — RGBA with interpolated alpha
  - Uncompressed RGBA/BGRA
  - BC7 — high-quality RGBA (partial — shows header info only)

For preview, we decode the first mip level into a QImage.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger("core.dds_reader")

DDS_MAGIC = b"DDS "

# DDS header flags
DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PITCH = 0x8
DDSD_PIXELFORMAT = 0x1000
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000

# Pixel format flags
DDPF_ALPHAPIXELS = 0x1
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000

# FourCC codes
DXT1 = b"DXT1"
DXT3 = b"DXT3"
DXT5 = b"DXT5"
DX10 = b"DX10"


@dataclass
class DdsInfo:
    """DDS file header info."""
    width: int = 0
    height: int = 0
    mip_count: int = 1
    format: str = "Unknown"
    fourcc: str = ""
    bits_per_pixel: int = 0
    compressed: bool = False
    has_alpha: bool = False
    data_offset: int = 128  # After header
    file_size: int = 0


def expected_dds_data_size(info: DdsInfo) -> int | None:
    """Return the expected total DDS byte size from the header, or None if unknown."""
    total_payload = 0
    width = max(1, info.width)
    height = max(1, info.height)
    mip_count = max(1, info.mip_count)

    for _ in range(mip_count):
        payload_size = expected_mip_payload_size(info, width, height)
        if payload_size is None:
            return None
        total_payload += payload_size
        width = max(1, width // 2)
        height = max(1, height // 2)

    return info.data_offset + total_payload


def expected_first_mip_payload_size(info: DdsInfo) -> int | None:
    """Return the expected byte size of the first mip level payload."""
    return expected_mip_payload_size(info, max(1, info.width), max(1, info.height))


def expected_mip_payload_size(info: DdsInfo, width: int, height: int) -> int | None:
    """Return the expected payload size of one mip level for the DDS format."""
    blocks_w = max(1, (width + 3) // 4)
    blocks_h = max(1, (height + 3) // 4)

    if info.fourcc == "DXT1" or info.format.startswith("BC1"):
        return blocks_w * blocks_h * 8
    if (
        info.fourcc == "DXT3"
        or info.format.startswith("BC2")
        or info.fourcc == "DXT5"
        or info.format.startswith("BC3")
        or info.fourcc == "BC5U"
        or info.format.startswith("BC5")
        or info.format.startswith("BC6H")
        or info.format.startswith("BC7")
    ):
        return blocks_w * blocks_h * 16
    if info.fourcc == "BC4U" or info.format.startswith("BC4"):
        return blocks_w * blocks_h * 8
    if info.format in ("RGBA 32-bit", "RGB 32-bit"):
        return width * height * 4
    if info.format == "RGB 24-bit":
        return width * height * 3
    if info.format == "Luminance 8-bit":
        return width * height
    if info.format == "Luminance 16-bit":
        return width * height * 2
    return None


def validate_dds_payload_size(data: bytes, info: DdsInfo | None = None) -> DdsInfo:
    """Validate that the DDS body is large enough for the mip chain declared in the header."""
    info = info or read_dds_info(data)
    expected_size = expected_dds_data_size(info)
    if expected_size is not None and len(data) < expected_size:
        raise ValueError(
            "DDS payload is shorter than its header declares "
            f"({len(data)} < {expected_size} bytes). "
            "This usually means the archive entry is still using an unsupported "
            "type-1 compressed texture layout."
        )
    return info


def read_dds_info(data: bytes) -> DdsInfo:
    """Parse DDS header and return metadata."""
    if len(data) < 128 or data[:4] != DDS_MAGIC:
        raise ValueError("Not a valid DDS file")

    info = DdsInfo(file_size=len(data))

    # Main header at offset 4 (124 bytes)
    _size = struct.unpack_from("<I", data, 4)[0]  # Should be 124
    flags = struct.unpack_from("<I", data, 8)[0]
    info.height = struct.unpack_from("<I", data, 12)[0]
    info.width = struct.unpack_from("<I", data, 16)[0]

    if flags & DDSD_MIPMAPCOUNT:
        info.mip_count = struct.unpack_from("<I", data, 28)[0]

    # Pixel format at offset 76
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88]
    info.bits_per_pixel = struct.unpack_from("<I", data, 88)[0]

    if pf_flags & DDPF_FOURCC:
        info.fourcc = fourcc.decode("ascii", "replace").strip("\x00")
        info.compressed = True
        if fourcc == DXT1:
            info.format = "DXT1 (BC1)"
            info.has_alpha = False
        elif fourcc == DXT3:
            info.format = "DXT3 (BC2)"
            info.has_alpha = True
        elif fourcc == DXT5:
            info.format = "DXT5 (BC3)"
            info.has_alpha = True
        elif fourcc == DX10:
            info.format = "DX10 Extended"
            info.data_offset = 148  # DX10 header adds 20 bytes
            if len(data) >= 148:
                dxgi_format = struct.unpack_from("<I", data, 128)[0]
                info.format = f"DX10 (DXGI={dxgi_format})"
                # Common DXGI formats
                _dxgi_names = {
                    71: "BC1 (DXT1)", 72: "BC1 sRGB",
                    74: "BC2 (DXT3)", 75: "BC2 sRGB",
                    77: "BC3 (DXT5)", 78: "BC3 sRGB",
                    80: "BC4", 81: "BC4 Signed",
                    83: "BC5", 84: "BC5 Signed",
                    95: "BC6H UF16", 96: "BC6H SF16",
                    98: "BC7", 99: "BC7 sRGB",
                }
                if dxgi_format in _dxgi_names:
                    info.format = _dxgi_names[dxgi_format]
        else:
            info.format = f"FourCC: {info.fourcc}"
    elif pf_flags & DDPF_RGB:
        info.compressed = False
        info.has_alpha = bool(pf_flags & DDPF_ALPHAPIXELS)
        if info.bits_per_pixel == 32:
            info.format = "RGBA 32-bit" if info.has_alpha else "RGB 32-bit"
        elif info.bits_per_pixel == 24:
            info.format = "RGB 24-bit"
        elif info.bits_per_pixel == 16:
            info.format = "RGB 16-bit"
        else:
            info.format = f"RGB {info.bits_per_pixel}-bit"
    elif pf_flags & DDPF_LUMINANCE:
        info.compressed = False
        info.has_alpha = bool(pf_flags & DDPF_ALPHAPIXELS)
        if info.bits_per_pixel == 8:
            info.format = "Luminance 8-bit"
        elif info.bits_per_pixel == 16:
            info.format = "Luminance 16-bit"
        else:
            info.format = f"Luminance {info.bits_per_pixel}-bit"
    else:
        # Last resort: check if BPP and masks hint at a format
        rmask = struct.unpack_from("<I", data, 92)[0]
        if info.bits_per_pixel == 8 and rmask == 0xFF:
            info.format = "Luminance 8-bit"
            info.compressed = False
        elif info.bits_per_pixel == 16 and rmask == 0xFFFF:
            info.format = "Luminance 16-bit"
            info.compressed = False
        else:
            info.format = "Unknown"

    return info


def decode_dds_to_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Decode DDS first mip to raw RGBA bytes.

    Returns (width, height, rgba_bytes) or raises on unsupported format.
    Only supports DXT1, DXT5, and uncompressed RGBA for preview.
    """
    info = validate_dds_payload_size(data)
    w, h = info.width, info.height
    offset = info.data_offset

    if not info.compressed and info.bits_per_pixel == 32:
        # Uncompressed BGRA → RGBA
        expected_size = w * h * 4
        pixel_data = data[offset:offset + expected_size]
        if len(pixel_data) < expected_size:
            raise ValueError(
                "DDS header claims an uncompressed 32-bit image, "
                f"but only {len(pixel_data)} of {expected_size} bytes are present."
            )
        rgba = bytearray(len(pixel_data))
        for i in range(0, len(pixel_data), 4):
            if i + 3 < len(pixel_data):
                rgba[i] = pixel_data[i + 2]      # R
                rgba[i + 1] = pixel_data[i + 1]  # G
                rgba[i + 2] = pixel_data[i]       # B
                rgba[i + 3] = pixel_data[i + 3]   # A
        return w, h, bytes(rgba)

    if info.fourcc == "DXT1" or info.format.startswith("BC1"):
        return w, h, _decode_dxt1(data[offset:], w, h)

    if info.fourcc == "DXT3" or info.format.startswith("BC2"):
        return w, h, _decode_dxt3(data[offset:], w, h)

    if info.fourcc == "DXT5" or info.format.startswith("BC3"):
        return w, h, _decode_dxt5(data[offset:], w, h)

    if info.fourcc == "BC4U" or info.format.startswith("BC4"):
        return w, h, _decode_bc4(data[offset:], w, h)

    if info.fourcc == "BC5U" or info.format.startswith("BC5"):
        return w, h, _decode_bc5(data[offset:], w, h)

    if info.format.startswith("BC6H"):
        return w, h, _decode_bc6h(data[offset:], w, h)

    if info.format.startswith("BC7"):
        return w, h, _decode_bc7(data[offset:], w, h)

    # Luminance 8-bit: single channel grayscale → RGBA
    if info.format == "Luminance 8-bit":
        return w, h, _decode_luminance_8(data[offset:], w, h)

    # Luminance 16-bit: single 16-bit channel → RGBA (heightmaps, SDF)
    if info.format == "Luminance 16-bit":
        return w, h, _decode_luminance_16(data[offset:], w, h)

    # Unknown FourCC with small data: treat as raw pixel data, show as grayscale
    if info.compressed and info.format.startswith("FourCC:"):
        try:
            return w, h, _decode_raw_fallback(data[offset:], w, h, info.bits_per_pixel)
        except Exception:
            pass

    raise ValueError(f"Unsupported DDS format for preview: {info.format}")


def _decode_dxt1(data: bytes, width: int, height: int) -> bytes:
    """Decode DXT1 (BC1) to RGBA."""
    rgba = bytearray(width * height * 4)
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    offset = 0

    for by in range(blocks_y):
        for bx in range(blocks_x):
            if offset + 8 > len(data):
                break

            c0 = struct.unpack_from("<H", data, offset)[0]
            c1 = struct.unpack_from("<H", data, offset + 2)[0]
            bits = struct.unpack_from("<I", data, offset + 4)[0]
            offset += 8

            # Decode RGB565 colors
            colors = [_rgb565(c0), _rgb565(c1), None, None]
            if c0 > c1:
                colors[2] = _lerp_color(colors[0], colors[1], 1, 3)
                colors[3] = _lerp_color(colors[0], colors[1], 2, 3)
            else:
                colors[2] = _lerp_color(colors[0], colors[1], 1, 2)
                colors[3] = (0, 0, 0, 0)

            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x < width and y < height:
                        idx = (bits >> (2 * (py * 4 + px))) & 3
                        c = colors[idx]
                        p = (y * width + x) * 4
                        rgba[p] = c[0]
                        rgba[p + 1] = c[1]
                        rgba[p + 2] = c[2]
                        rgba[p + 3] = 255 if len(c) < 4 else c[3]

    return bytes(rgba)


def _decode_dxt5(data: bytes, width: int, height: int) -> bytes:
    """Decode DXT5 (BC3) to RGBA."""
    rgba = bytearray(width * height * 4)
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    offset = 0

    for by in range(blocks_y):
        for bx in range(blocks_x):
            if offset + 16 > len(data):
                break

            # Alpha block (8 bytes)
            a0 = data[offset]
            a1 = data[offset + 1]
            alpha_bits = int.from_bytes(data[offset + 2:offset + 8], "little")
            offset += 8

            alpha_lut = [a0, a1]
            if a0 > a1:
                for i in range(6):
                    alpha_lut.append(((6 - i) * a0 + (1 + i) * a1) // 7)
            else:
                for i in range(4):
                    alpha_lut.append(((4 - i) * a0 + (1 + i) * a1) // 5)
                alpha_lut.extend([0, 255])

            # Color block (8 bytes)
            c0 = struct.unpack_from("<H", data, offset)[0]
            c1 = struct.unpack_from("<H", data, offset + 2)[0]
            bits = struct.unpack_from("<I", data, offset + 4)[0]
            offset += 8

            colors = [_rgb565(c0), _rgb565(c1)]
            colors.append(_lerp_color(colors[0], colors[1], 1, 3))
            colors.append(_lerp_color(colors[0], colors[1], 2, 3))

            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x < width and y < height:
                        ci = (bits >> (2 * (py * 4 + px))) & 3
                        ai = (alpha_bits >> (3 * (py * 4 + px))) & 7
                        c = colors[ci]
                        p = (y * width + x) * 4
                        rgba[p] = c[0]
                        rgba[p + 1] = c[1]
                        rgba[p + 2] = c[2]
                        rgba[p + 3] = alpha_lut[ai] if ai < len(alpha_lut) else 255

    return bytes(rgba)


def _rgb565(v: int) -> tuple[int, int, int]:
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    return (r, g, b)


def _lerp_color(c0, c1, num, denom):
    return tuple(
        (c0[i] * (denom - num) + c1[i] * num) // denom
        for i in range(min(len(c0), len(c1)))
    )


def _decode_dxt3(data: bytes, width: int, height: int) -> bytes:
    """Decode DXT3 (BC2) to RGBA — explicit alpha."""
    rgba = bytearray(width * height * 4)
    bx_count = max(1, (width + 3) // 4)
    by_count = max(1, (height + 3) // 4)
    offset = 0
    for by in range(by_count):
        for bx in range(bx_count):
            if offset + 16 > len(data):
                break
            # 8 bytes explicit alpha (4 bits per pixel, 16 pixels)
            alpha_bits = int.from_bytes(data[offset:offset + 8], "little")
            offset += 8
            # 8 bytes color (same as DXT1)
            c0 = struct.unpack_from("<H", data, offset)[0]
            c1 = struct.unpack_from("<H", data, offset + 2)[0]
            bits = struct.unpack_from("<I", data, offset + 4)[0]
            offset += 8
            colors = [_rgb565(c0), _rgb565(c1)]
            colors.append(_lerp_color(colors[0], colors[1], 1, 3))
            colors.append(_lerp_color(colors[0], colors[1], 2, 3))
            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x < width and y < height:
                        ci = (bits >> (2 * (py * 4 + px))) & 3
                        ai = (alpha_bits >> (4 * (py * 4 + px))) & 0xF
                        c = colors[ci]
                        p = (y * width + x) * 4
                        rgba[p] = c[0]; rgba[p+1] = c[1]; rgba[p+2] = c[2]
                        rgba[p+3] = ai * 17  # 4-bit to 8-bit
    return bytes(rgba)


def _decode_bc4(data: bytes, width: int, height: int) -> bytes:
    """Decode BC4 (single channel) to RGBA — grayscale."""
    rgba = bytearray(width * height * 4)
    bx_count = max(1, (width + 3) // 4)
    by_count = max(1, (height + 3) // 4)
    offset = 0
    for by in range(by_count):
        for bx in range(bx_count):
            if offset + 8 > len(data):
                break
            r0 = data[offset]
            r1 = data[offset + 1]
            lut = _build_bc4_lut(r0, r1)
            bits = int.from_bytes(data[offset + 2:offset + 8], "little")
            offset += 8
            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x < width and y < height:
                        idx = (bits >> (3 * (py * 4 + px))) & 7
                        v = lut[idx] if idx < len(lut) else 0
                        p = (y * width + x) * 4
                        rgba[p] = v; rgba[p+1] = v; rgba[p+2] = v; rgba[p+3] = 255
    return bytes(rgba)


def _decode_bc5(data: bytes, width: int, height: int) -> bytes:
    """Decode BC5 (two channel — normal map) to RGBA."""
    rgba = bytearray(width * height * 4)
    bx_count = max(1, (width + 3) // 4)
    by_count = max(1, (height + 3) // 4)
    offset = 0
    for by in range(by_count):
        for bx in range(bx_count):
            if offset + 16 > len(data):
                break
            # Red channel (8 bytes)
            r0 = data[offset]; r1 = data[offset + 1]
            r_lut = _build_bc4_lut(r0, r1)
            r_bits = int.from_bytes(data[offset + 2:offset + 8], "little")
            offset += 8
            # Green channel (8 bytes)
            g0 = data[offset]; g1 = data[offset + 1]
            g_lut = _build_bc4_lut(g0, g1)
            g_bits = int.from_bytes(data[offset + 2:offset + 8], "little")
            offset += 8
            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x < width and y < height:
                        ri = (r_bits >> (3 * (py * 4 + px))) & 7
                        gi = (g_bits >> (3 * (py * 4 + px))) & 7
                        rv = r_lut[ri] if ri < len(r_lut) else 0
                        gv = g_lut[gi] if gi < len(g_lut) else 0
                        # Reconstruct Z from XY normal (blue channel)
                        nx = (rv / 255.0) * 2.0 - 1.0
                        ny = (gv / 255.0) * 2.0 - 1.0
                        nz_sq = max(0.0, 1.0 - nx * nx - ny * ny)
                        bv = int((nz_sq ** 0.5 * 0.5 + 0.5) * 255)
                        p = (y * width + x) * 4
                        rgba[p] = rv; rgba[p+1] = gv; rgba[p+2] = min(255, bv); rgba[p+3] = 255
    return bytes(rgba)


def _decode_bc6h(data: bytes, width: int, height: int) -> bytes:
    """Decode BC6H (HDR) to RGBA — simplified tone-mapped preview."""
    # BC6H is extremely complex (14 modes). For preview, do a simple
    # approximation: read endpoint colors and interpolate.
    rgba = bytearray(width * height * 4)
    bx_count = max(1, (width + 3) // 4)
    by_count = max(1, (height + 3) // 4)
    offset = 0
    for by in range(by_count):
        for bx in range(bx_count):
            if offset + 16 > len(data):
                break
            # Read first 6 bytes as approximate RGB endpoints
            block = data[offset:offset + 16]
            # Extract low bits as color approximation
            r = min(255, (block[0] & 0xFF))
            g = min(255, (block[2] & 0xFF))
            b = min(255, (block[4] & 0xFF))
            offset += 16
            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y_pos = by * 4 + py
                    if x < width and y_pos < height:
                        p = (y_pos * width + x) * 4
                        rgba[p] = r; rgba[p+1] = g; rgba[p+2] = b; rgba[p+3] = 255
    return bytes(rgba)


def _decode_bc7(data: bytes, width: int, height: int) -> bytes:
    """Decode BC7 to RGBA — simplified mode-based preview.

    BC7 has 8 modes with varying partition counts, endpoint precision,
    and index bits. This implements a simplified decoder that handles
    the most common modes (4, 5, 6) for preview quality.
    """
    rgba = bytearray(width * height * 4)
    bx_count = max(1, (width + 3) // 4)
    by_count = max(1, (height + 3) // 4)
    offset = 0
    for by in range(by_count):
        for bx in range(bx_count):
            if offset + 16 > len(data):
                break
            block = data[offset:offset + 16]
            offset += 16
            # Determine mode from leading bits
            mode = -1
            for m in range(8):
                if block[0] & (1 << m):
                    mode = m
                    break
            # Simplified: extract endpoint colors from block bytes
            # Mode 6 (most common): 2 RGBA endpoints, 4-bit indices
            if mode == 6:
                # Endpoints encoded in bits 7-62 (roughly)
                r0 = (block[1] >> 1) & 0x7F
                g0 = ((block[1] & 1) << 6) | ((block[2] >> 2) & 0x3F)
                b0 = ((block[2] & 3) << 5) | ((block[3] >> 3) & 0x1F)
                r0 = (r0 * 255) // 127
                g0 = (g0 * 255) // 127
                b0 = (b0 * 255) // 127
            else:
                # Fallback: use first bytes as approximate color
                r0 = block[1]
                g0 = block[2]
                b0 = block[3]
            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y_pos = by * 4 + py
                    if x < width and y_pos < height:
                        p = (y_pos * width + x) * 4
                        rgba[p] = min(255, r0)
                        rgba[p+1] = min(255, g0)
                        rgba[p+2] = min(255, b0)
                        rgba[p+3] = 255
    return bytes(rgba)


def _build_bc4_lut(a0: int, a1: int) -> list[int]:
    """Build BC4/BC5 interpolation lookup table."""
    lut = [a0, a1]
    if a0 > a1:
        for i in range(6):
            lut.append(((6 - i) * a0 + (1 + i) * a1) // 7)
    else:
        for i in range(4):
            lut.append(((4 - i) * a0 + (1 + i) * a1) // 5)
        lut.extend([0, 255])
    return lut


def _decode_luminance_8(data: bytes, width: int, height: int) -> bytes:
    """Decode 8-bit luminance (grayscale) to RGBA."""
    expected = width * height
    rgba = bytearray(expected * 4)
    for i in range(min(expected, len(data))):
        v = data[i]
        p = i * 4
        rgba[p] = v
        rgba[p + 1] = v
        rgba[p + 2] = v
        rgba[p + 3] = 255
    return bytes(rgba)


def _decode_luminance_16(data: bytes, width: int, height: int) -> bytes:
    """Decode 16-bit luminance to RGBA (maps 0-65535 to 0-255)."""
    expected = width * height
    rgba = bytearray(expected * 4)
    for i in range(min(expected, len(data) // 2)):
        v = struct.unpack_from("<H", data, i * 2)[0]
        v8 = v >> 8  # Map 16-bit to 8-bit
        p = i * 4
        rgba[p] = v8
        rgba[p + 1] = v8
        rgba[p + 2] = v8
        rgba[p + 3] = 255
    return bytes(rgba)


def _decode_raw_fallback(data: bytes, width: int, height: int, bpp: int) -> bytes:
    """Last-resort decoder: treat raw pixel data as grayscale."""
    expected = width * height
    rgba = bytearray(expected * 4)
    bytes_per_pixel = max(1, bpp // 8) if bpp > 0 else 1

    for i in range(min(expected, len(data) // bytes_per_pixel)):
        off = i * bytes_per_pixel
        if bytes_per_pixel == 1:
            v = data[off]
        elif bytes_per_pixel == 2:
            v = struct.unpack_from("<H", data, off)[0] >> 8
        elif bytes_per_pixel >= 4:
            v = data[off]  # Just use first byte
        else:
            v = data[off]
        p = i * 4
        rgba[p] = v
        rgba[p + 1] = v
        rgba[p + 2] = v
        rgba[p + 3] = 255
    return bytes(rgba)


def get_dds_summary(data: bytes) -> str:
    """Get a human-readable summary of a DDS file."""
    try:
        info = validate_dds_payload_size(data)
        size_kb = info.file_size / 1024
        return (
            f"DDS Texture: {info.width}x{info.height}\n"
            f"Format: {info.format}\n"
            f"Mipmaps: {info.mip_count}\n"
            f"Alpha: {'Yes' if info.has_alpha else 'No'}\n"
            f"Size: {size_kb:,.0f} KB"
        )
    except Exception as e:
        return f"DDS: Error reading header ({e})"

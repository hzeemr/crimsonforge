"""LZ4 block compression/decompression for PAZ archives.

Uses LZ4 block mode (no frame header) to match the game's format.
Also supports zlib for PAMT compression type 4.
"""

import struct
import zlib
import lz4.block


COMP_NONE = 0
COMP_RAW = 1
COMP_LZ4 = 2
COMP_CUSTOM = 3
COMP_ZLIB = 4


def _decompress_type1_par(data: bytes) -> bytes:
    """Decompress a type-1 PAR container with per-section LZ4 blocks.

    Some Crimson Desert PAR files store the 80-byte header uncompressed and
    then use the slot table at 0x10 as repeated ``[u32 comp_size, u32 decomp_size]``
    pairs. When a slot's ``comp_size`` is non-zero, that section is LZ4 block
    compressed inside the file payload itself.
    """
    if len(data) < 0x50 or data[:4] != b"PAR ":
        return data

    output = bytearray(data[:0x50])
    file_offset = 0x50
    saw_compressed_section = False

    for slot in range(8):
        slot_off = 0x10 + slot * 8
        comp_size = struct.unpack_from("<I", data, slot_off)[0]
        decomp_size = struct.unpack_from("<I", data, slot_off + 4)[0]

        if decomp_size == 0:
            continue

        if comp_size > 0:
            saw_compressed_section = True
            blob = data[file_offset:file_offset + comp_size]
            output.extend(lz4.block.decompress(blob, uncompressed_size=decomp_size))
            file_offset += comp_size
        else:
            output.extend(data[file_offset:file_offset + decomp_size])
            file_offset += decomp_size

    if not saw_compressed_section:
        return data

    # Mark the output header as fully decompressed.
    for slot in range(8):
        struct.pack_into("<I", output, 0x10 + slot * 8, 0)

    return bytes(output)


def _decompress_type1_prefixed_lz4(data: bytes, original_size: int) -> bytes:
    """Decompress type-1 payloads that keep a plain file header and LZ4 body.

    Some UI DDS assets store the normal 128-byte DDS header uncompressed and
    only LZ4-compress the pixel payload that follows. Those files are marked as
    compression type 1 in PAMT, but they are not PAR containers.
    """
    if original_size <= len(data):
        return data

    if len(data) < 128 or data[:4] != b"DDS ":
        return data

    header = data[:128]
    payload = data[128:]
    expected_payload_size = original_size - 128
    if expected_payload_size <= 0:
        return data

    try:
        decompressed_payload = lz4.block.decompress(payload, uncompressed_size=expected_payload_size)
    except lz4.block.LZ4BlockError:
        return data

    if len(decompressed_payload) != expected_payload_size:
        return data

    return header + decompressed_payload


def _decompress_type1_dds_first_mip_lz4_tail(data: bytes, original_size: int) -> bytes:
    """Decompress type-1 DDS files that store a compressed top mip and raw mip tail.

    A large class of Crimson Desert DDS textures keeps the normal DDS header,
    LZ4-compresses only the first mip level, then appends the remaining lower
    mip levels as raw DDS payload bytes. We reconstruct the full DDS body by
    decoding just enough raw LZ4 data to fill the top mip, then concatenating
    the remaining tail bytes.
    """
    if original_size <= len(data):
        return data
    if len(data) < 128 or data[:4] != b"DDS ":
        return data

    try:
        from core.dds_reader import (
            read_dds_info,
            expected_dds_data_size,
            expected_first_mip_payload_size,
        )
    except Exception:
        return data

    try:
        info = read_dds_info(data)
    except Exception:
        return data

    expected_total_size = expected_dds_data_size(info)
    first_mip_size = expected_first_mip_payload_size(info)
    if expected_total_size is None or first_mip_size is None:
        return data
    if expected_total_size != original_size:
        return data

    tail_size = expected_total_size - info.data_offset - first_mip_size
    if tail_size < 0:
        return data

    body = data[info.data_offset:]
    rebuilt_top_mip = _decode_type1_dds_top_mip_lz4(body, first_mip_size, tail_size)
    if rebuilt_top_mip is None:
        return data

    return data[:info.data_offset] + rebuilt_top_mip


def _decode_type1_dds_top_mip_lz4(body: bytes, first_mip_size: int, tail_size: int) -> bytes | None:
    """Decode a raw-LZ4 top mip and attach the raw remaining mip tail."""
    i = 0
    out = bytearray()

    while i < len(body):
        token = body[i]
        i += 1

        literal_len = token >> 4
        if literal_len == 15:
            while True:
                if i >= len(body):
                    return None
                extra = body[i]
                i += 1
                literal_len += extra
                if extra != 255:
                    break

        if i + literal_len > len(body):
            return None

        remaining_top_mip = first_mip_size - len(out)
        if literal_len >= remaining_top_mip:
            out.extend(body[i:i + remaining_top_mip])
            i += remaining_top_mip
            if len(body) - i != tail_size:
                return None
            return bytes(out) + body[i:]

        out.extend(body[i:i + literal_len])
        i += literal_len

        if i + 2 > len(body):
            return None

        offset = body[i] | (body[i + 1] << 8)
        if offset == 0:
            return None
        i += 2

        match_len = token & 0x0F
        if match_len == 15:
            while True:
                if i >= len(body):
                    return None
                extra = body[i]
                i += 1
                match_len += extra
                if extra != 255:
                    break
        match_len += 4

        remaining_top_mip = first_mip_size - len(out)
        if match_len > remaining_top_mip:
            return None

        match_start = len(out) - offset
        if match_start < 0:
            return None
        for _ in range(match_len):
            out.append(out[match_start])
            match_start += 1

    return None


def decompress(data: bytes, original_size: int, compression_type: int) -> bytes:
    """Decompress data based on the compression type from PAMT flags.

    Args:
        data: Compressed data bytes.
        original_size: Expected decompressed size (from PAMT entry).
        compression_type: 0=none, 2=LZ4, 3=custom, 4=zlib.

    Returns:
        Decompressed data.

    Raises:
        ValueError: If compression type is unsupported or decompression fails.
    """
    if compression_type == COMP_NONE:
        return data

    if compression_type == COMP_RAW:
        result = _decompress_type1_par(data)
        if len(result) >= original_size:
            return result[:original_size]
        result = _decompress_type1_prefixed_lz4(result, original_size)
        if len(result) >= original_size:
            return result[:original_size]
        result = _decompress_type1_dds_first_mip_lz4_tail(result, original_size)
        if len(result) >= original_size:
            return result[:original_size]
        raise ValueError(
            f"Unsupported type-1 payload layout: got {len(result)} bytes, "
            f"expected {original_size} bytes after decompression."
        )

    if compression_type == COMP_LZ4:
        try:
            result = lz4.block.decompress(data, uncompressed_size=original_size)
        except lz4.block.LZ4BlockError as e:
            raise ValueError(
                f"LZ4 decompression failed: {e}. "
                f"Input size: {len(data)} bytes, expected output: {original_size} bytes. "
                f"The data may be corrupted or the original_size value is incorrect."
            ) from e
        if len(result) != original_size:
            raise ValueError(
                f"LZ4 decompression size mismatch: got {len(result)} bytes, "
                f"expected {original_size} bytes. The PAMT entry may have incorrect metadata."
            )
        return result

    if compression_type == COMP_ZLIB:
        try:
            result = zlib.decompress(data)
        except zlib.error as e:
            raise ValueError(
                f"zlib decompression failed: {e}. "
                f"Input size: {len(data)} bytes, expected output: {original_size} bytes."
            ) from e
        return result

    if compression_type == COMP_CUSTOM:
        raise ValueError(
            f"Compression type 3 (custom) is not yet supported. "
            f"This compression type is rarely used in game files. "
            f"Please report this file to the CrimsonForge developers."
        )

    raise ValueError(
        f"Unknown compression type: {compression_type}. "
        f"Expected 0 (none), 2 (LZ4), 3 (custom), or 4 (zlib). "
        f"The PAMT entry may be corrupted."
    )


def compress(data: bytes, compression_type: int) -> bytes:
    """Compress data using the specified compression type.

    Args:
        data: Uncompressed data bytes.
        compression_type: 0=none, 1=raw passthrough, 2=LZ4, 4=zlib.

    Returns:
        Compressed data.
    """
    if compression_type == COMP_NONE:
        return data

    if compression_type == COMP_RAW:
        return data

    if compression_type == COMP_LZ4:
        return lz4.block.compress(data, store_size=False)

    if compression_type == COMP_ZLIB:
        return zlib.compress(data)

    raise ValueError(
        f"Cannot compress with type {compression_type}. "
        f"Only types 0 (none), 1 (raw), 2 (LZ4), and 4 (zlib) are supported for compression."
    )


def lz4_decompress(data: bytes, original_size: int) -> bytes:
    """LZ4 block decompression (convenience wrapper)."""
    return decompress(data, original_size, COMP_LZ4)


def lz4_compress(data: bytes) -> bytes:
    """LZ4 block compression (convenience wrapper)."""
    return compress(data, COMP_LZ4)

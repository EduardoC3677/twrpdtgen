#
# Copyright (C) 2022 The Android Open Source Project
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Vendor boot image parser for vendor_boot images (header v3/v4).

vendor_boot images were introduced in Android 11 (header version 3) and
extended in Android 12 (header version 4) with support for multiple
vendor ramdisks.  A vendor_boot-debug image typically contains two
ramdisks: a base vendor ramdisk and a recovery/debug ramdisk.
"""

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


VENDOR_BOOT_MAGIC = b"VNDRBOOT"


@dataclass
class VendorRamdiskEntry:
    """Represents one entry in the vendor ramdisk table (header v4)."""
    size: int = 0
    offset: int = 0
    ramdisk_type: int = 0
    name: str = ""
    board_id: bytes = b""


@dataclass
class VendorBootImageInfo:
    """Parsed information from a vendor_boot image."""
    header_version: int = 0
    page_size: int = 0
    kernel_addr: int = 0
    ramdisk_addr: int = 0
    vendor_ramdisk_size: int = 0
    cmdline: str = ""
    tags_addr: int = 0
    product_name: str = ""
    header_size: int = 0
    dtb_size: int = 0
    dtb_addr: int = 0

    # Header v4 fields
    vendor_ramdisk_table_size: int = 0
    vendor_ramdisk_table_entry_count: int = 0
    vendor_ramdisk_table_entry_size: int = 0
    bootconfig_size: int = 0
    bootconfig: str = ""

    vendor_ramdisk_entries: List[VendorRamdiskEntry] = field(default_factory=list)

    # Extracted file paths (populated after extraction)
    dtb: Optional[Path] = None
    ramdisk_paths: List[Path] = field(default_factory=list)
    merged_ramdisk: Optional[Path] = None

    @property
    def is_vendor_boot(self) -> bool:
        return True

    @property
    def has_multiple_ramdisks(self) -> bool:
        return len(self.vendor_ramdisk_entries) > 1


def parse_vendor_boot_header(data: bytes) -> VendorBootImageInfo:
    """
    Parse a vendor_boot image header (v3 or v4).

    Raises ValueError if the magic bytes do not match.
    """
    magic = data[0:8]
    if magic != VENDOR_BOOT_MAGIC:
        raise ValueError(
            f"Not a vendor_boot image: expected magic {VENDOR_BOOT_MAGIC!r}, "
            f"got {magic!r}"
        )

    info = VendorBootImageInfo()

    info.header_version = struct.unpack_from("<I", data, 8)[0]
    info.page_size = struct.unpack_from("<I", data, 12)[0]
    info.kernel_addr = struct.unpack_from("<I", data, 16)[0]
    info.ramdisk_addr = struct.unpack_from("<I", data, 20)[0]
    info.vendor_ramdisk_size = struct.unpack_from("<I", data, 24)[0]

    # Command line – 2048 bytes at offset 28
    raw_cmdline = data[28 : 28 + 2048]
    info.cmdline = raw_cmdline.split(b"\x00")[0].decode("ascii", errors="replace")

    # Tags address at offset 2076
    info.tags_addr = struct.unpack_from("<I", data, 2076)[0]

    # Product name – 16 bytes at offset 2080
    raw_name = data[2080:2096]
    info.product_name = raw_name.split(b"\x00")[0].decode("ascii", errors="replace")

    # Header size at offset 2096
    info.header_size = struct.unpack_from("<I", data, 2096)[0]

    # DTB size at offset 2100
    info.dtb_size = struct.unpack_from("<I", data, 2100)[0]

    # DTB address (64-bit) at offset 2104
    info.dtb_addr = struct.unpack_from("<Q", data, 2104)[0]

    # Header v4 extensions
    if info.header_version >= 4:
        info.vendor_ramdisk_table_size = struct.unpack_from("<I", data, 2112)[0]
        info.vendor_ramdisk_table_entry_count = struct.unpack_from("<I", data, 2116)[0]
        info.vendor_ramdisk_table_entry_size = struct.unpack_from("<I", data, 2120)[0]
        info.bootconfig_size = struct.unpack_from("<I", data, 2124)[0]

    return info


def _align_to_page(offset: int, page_size: int) -> int:
    """Round *offset* up to the next page boundary."""
    return ((offset + page_size - 1) // page_size) * page_size


def extract_vendor_boot(image_path: Path, output_dir: Path) -> VendorBootImageInfo:
    """
    Parse and extract a vendor_boot image.

    * Extracts the DTB to ``output_dir/dtb``
    * Extracts each vendor ramdisk to ``output_dir/ramdisk_<n>.cpio.gz``
    * Merges all ramdisks into a single directory at ``output_dir/ramdisk/``

    Returns a populated :class:`VendorBootImageInfo`.
    """
    import gzip
    import subprocess
    import tempfile

    data = image_path.read_bytes()
    info = parse_vendor_boot_header(data)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Calculate offsets
    ramdisk_offset = _align_to_page(info.header_size, info.page_size)
    dtb_offset = ramdisk_offset + _align_to_page(info.vendor_ramdisk_size, info.page_size)

    # Extract DTB
    if info.dtb_size > 0:
        dtb_path = output_dir / "dtb"
        dtb_path.write_bytes(data[dtb_offset : dtb_offset + info.dtb_size])
        info.dtb = dtb_path

    # Parse vendor ramdisk table entries (v4)
    if info.header_version >= 4 and info.vendor_ramdisk_table_entry_count > 0:
        vrt_offset = dtb_offset + _align_to_page(info.dtb_size, info.page_size)
        for i in range(info.vendor_ramdisk_table_entry_count):
            entry_off = vrt_offset + i * info.vendor_ramdisk_table_entry_size
            entry = VendorRamdiskEntry(
                size=struct.unpack_from("<I", data, entry_off)[0],
                offset=struct.unpack_from("<I", data, entry_off + 4)[0],
                ramdisk_type=struct.unpack_from("<I", data, entry_off + 8)[0],
                name=data[entry_off + 12 : entry_off + 12 + 32]
                .split(b"\x00")[0]
                .decode("ascii", errors="replace"),
            )
            info.vendor_ramdisk_entries.append(entry)

        # Read bootconfig if present
        if info.bootconfig_size > 0:
            bc_offset = vrt_offset + _align_to_page(
                info.vendor_ramdisk_table_size, info.page_size
            )
            info.bootconfig = (
                data[bc_offset : bc_offset + info.bootconfig_size]
                .decode("ascii", errors="replace")
                .rstrip("\x00")
            )
    else:
        # v3 – single vendor ramdisk
        entry = VendorRamdiskEntry(
            size=info.vendor_ramdisk_size,
            offset=0,
            ramdisk_type=1,
            name="vendor",
        )
        info.vendor_ramdisk_entries.append(entry)

    # Extract individual ramdisks
    ramdisk_data = data[ramdisk_offset : ramdisk_offset + info.vendor_ramdisk_size]
    for idx, entry in enumerate(info.vendor_ramdisk_entries):
        rd_filename = f"ramdisk_{idx}.cpio.gz"
        rd_path = output_dir / rd_filename
        rd_path.write_bytes(ramdisk_data[entry.offset : entry.offset + entry.size])
        info.ramdisk_paths.append(rd_path)

    # Merge all ramdisks into a single directory
    merged_dir = output_dir / "ramdisk"
    merged_dir.mkdir(parents=True, exist_ok=True)

    for rd_path in info.ramdisk_paths:
        _extract_cpio_gz(rd_path, merged_dir)

    info.merged_ramdisk = merged_dir
    return info


def _extract_cpio_gz(archive_path: Path, dest_dir: Path) -> None:
    """Extract a compressed cpio archive into *dest_dir*.

    Supports gzip, lz4, lzma/xz compression, or raw cpio.
    """
    import logging
    import subprocess
    import tempfile as _tf

    logger = logging.getLogger(__name__)

    raw_data = archive_path.read_bytes()

    # Detect format by magic bytes and decompress accordingly
    decompressed = _decompress_ramdisk(raw_data)

    # Write decompressed data to a temp file and extract with cpio
    with _tf.NamedTemporaryFile(suffix=".cpio", delete=False) as tmp:
        tmp.write(decompressed)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            ["cpio", "-idm", "--no-absolute-filenames", "-F", str(tmp_path)],
            cwd=str(dest_dir),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(
                "cpio extraction returned code %d for %s: %s",
                result.returncode,
                archive_path.name,
                result.stderr.decode(errors="replace").strip(),
            )
    finally:
        tmp_path.unlink(missing_ok=True)


def _decompress_ramdisk(raw: bytes) -> bytes:
    """Detect compression format and decompress accordingly."""
    import gzip
    import lzma

    # LZ4 frame magic: 0x04224D18, LZ4 legacy magic: 0x02214C18
    if raw[:4] in (b'\x04\x22\x4d\x18', b'\x02\x21\x4c\x18'):
        return _decompress_lz4(raw)

    # gzip magic: 0x1F8B
    if raw[:2] == b'\x1f\x8b':
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            pass

    # lzma / xz
    # XZ magic: 0xFD377A585A00
    # LZMA magic: usually starts with 0x5D
    try:
        return lzma.decompress(raw)
    except lzma.LZMAError:
        pass

    # Return raw data and let cpio try anyway
    return raw


def _decompress_lz4(raw: bytes) -> bytes:
    """Decompress LZ4 data using the lz4 command-line tool."""
    import subprocess
    import tempfile as _tf

    with _tf.NamedTemporaryFile(suffix=".lz4", delete=False) as tmp_in:
        tmp_in.write(raw)
        tmp_in_path = Path(tmp_in.name)

    tmp_out_path = tmp_in_path.with_suffix("")

    try:
        subprocess.run(
            ["lz4", "-d", "-f", str(tmp_in_path), str(tmp_out_path)],
            check=True,
            capture_output=True,
        )
        return tmp_out_path.read_bytes()
    except Exception:
        # Fallback: return raw data
        return raw
    finally:
        tmp_in_path.unlink(missing_ok=True)
        if tmp_out_path.is_file():
            tmp_out_path.unlink(missing_ok=True)


def is_vendor_boot_image(image_path: Path) -> bool:
    """Return True if *image_path* looks like a vendor_boot image."""
    try:
        with open(image_path, "rb") as fh:
            magic = fh.read(8)
        return magic == VENDOR_BOOT_MAGIC
    except Exception:
        return False

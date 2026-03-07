#
# Copyright (C) 2022 Sebastiano Barezzi
#
# SPDX-License-Identifier: Apache-2.0
#
"""Android fstab library."""

from itertools import repeat
from pathlib import Path
from typing import List, Set

from sebaubuntu_libs.libandroid.partitions.partition_model import PartitionModel

FSTAB_HEADER = "#<src>                                                 <mnt_point>            <type>  <mnt_flags and options>                            <fs_mgr_flags>\n"

# Display name mapping for common partitions
_DISPLAY_NAMES = {
	"system": "System",
	"system_ext": "System_EXT",
	"vendor": "Vendor",
	"product": "Product",
	"odm": "ODM",
	"vendor_dlkm": "Vendor_DLKM",
	"odm_dlkm": "ODM_DLKM",
	"system_dlkm": "System_DLKM",
	"boot": "Boot",
	"vendor_boot": "Vendor Boot",
	"init_boot": "Init Boot",
	"vbmeta": "VBMeta",
	"vbmeta_system": "VBMeta System",
	"vbmeta_vendor": "VBMeta Vendor",
	"dtbo": "DTBO",
	"logo": "Logo",
	"persist": "Persist",
	"metadata": "Metadata",
	"misc": "Misc",
	"data": "Data",
	"nvram": "Nvram",
	"nvdata": "Nvdata",
	"protect_f": "Protect_f",
	"protect_s": "Protect_s",
	"persistent": "Persistent",
	"otp": "OTP",
	"tee": "TEE",
	"spmfw": "SPMFW",
	"expdb": "Expdb",
	"frp": "FRP",
}

# Partitions that should have backup=1;flashimg=1 in twrp.flags
_FLASHABLE_PARTITIONS = {
	"boot", "vendor_boot", "init_boot", "dtbo", "vbmeta",
	"vbmeta_system", "vbmeta_vendor", "logo",
}

# Partitions that should have backup=1 in twrp.flags
_BACKUPABLE_PARTITIONS = {
	"persist", "nvram", "nvdata", "protect_f", "protect_s",
	"metadata", "modemst1", "modemst2", "fsg", "bluetooth", "dsp",
}

# Partitions to skip from twrp.flags (internal/firmware, not user-relevant)
_SKIP_TWRP_FLAGS = {
	"tee1", "tee2", "scp1", "scp2", "sspm_1", "sspm_2",
	"dpm_1", "dpm_2", "mcupm_1", "mcupm_2",
	"md1img", "md1dsp", "md1arm7", "md3img",
	"gz1", "gz2", "cam_vpu1", "cam_vpu2", "cam_vpu3",
	"seccfg", "proinfo", "para", "boot_para",
	"bootloader", "bootloader2", "lk", "lk2",
	"pi_img", "audio_dsp", "odmdtbo", "uh", "tzar",
	"elabel", "blackbox", "nvcfg",
}

class FstabEntry:
	"""
	A class representing a fstab entry
	"""
	def __init__(
		self,
		src: str,
		mount_point: str,
		fs_type: str,
		mnt_flags: List[str],
		fs_flags: List[str],
	):
		self.src = src
		self.mount_point = mount_point
		self.fs_type = fs_type
		self.mnt_flags = mnt_flags
		self.fs_flags = fs_flags

	def is_logical(self):
		return "logical" in self.fs_flags

	def is_slotselect(self):
		return "slotselect" in self.fs_flags

	@property
	def partition_name(self):
		"""Get the clean partition name from the mount point."""
		name = Path(self.mount_point).name
		if not name or name == ".":
			name = self.mount_point.strip("/")
		return name

	@classmethod
	def from_entry(cls, line: str):
		src, mount_point, fs_type, mnt_flags, fs_flags = line.split()

		return cls(src, mount_point, fs_type, mnt_flags.split(','), fs_flags.split(','))

class Fstab:
	def __init__(self, fstab: Path):
		self.fstab = fstab

		self.entries: List[FstabEntry] = []

		for line in self.fstab.read_text().splitlines():
			if not line:
				continue

			if line.startswith("#"):
				continue

			self.entries.append(FstabEntry.from_entry(line))

	def __str__(self):
		return self.format()

	def format(self, twrp: bool = False):
		entries = []

		src_len_max = 0
		mount_point_len_max = 0
		fs_type_len_max = 0
		mnt_flags_len_max = 0

		for entry in self.entries:
			mount_point_len = len(entry.mount_point)
			if mount_point_len > mount_point_len_max:
				mount_point_len_max = mount_point_len

			fs_type_len = len(entry.fs_type)
			if fs_type_len > fs_type_len_max:
				fs_type_len_max = fs_type_len

			src_len = len(entry.src)
			if src_len > src_len_max:
				src_len_max = src_len

			mnt_flags_len = len(entry.mnt_flags)
			if mnt_flags_len > mnt_flags_len_max:
				mnt_flags_len_max = mnt_flags_len

		src_len_max += 5
		mount_point_len_max += 5
		fs_type_len_max += 5
		mnt_flags_len_max += 5

		for entry in self.entries:
			src_space = ""
			mount_point_space = ""
			fs_type_space = ""
			mnt_flags_space = ""

			for _ in repeat(None, src_len_max - len(entry.src)):
				src_space += " "
			for _ in repeat(None, mount_point_len_max - len(entry.mount_point)):
				mount_point_space += " "
			for _ in repeat(None, fs_type_len_max - len(entry.fs_type)):
				fs_type_space += " "
			for _ in repeat(None, mnt_flags_len_max - len(entry.mnt_flags)):
				mnt_flags_space += " "

			if not twrp:
				entries.append(f"{entry.src}{src_space}{entry.mount_point}{mount_point_space}{entry.fs_type}{fs_type_space}{','.join(entry.mnt_flags)}{mnt_flags_space}{','.join(entry.fs_flags)}")
			else:
				flags = [f'display={Path(entry.mount_point).name}']
				if entry.is_logical():
					flags.append("logical")
				if entry.is_slotselect():
					flags.append("slotselect")
				entries.append(f"{entry.mount_point}{mount_point_space}{entry.fs_type}{fs_type_space}{entry.src}{src_space}flags={';'.join(flags)}")

		entries.append("")

		return "\n".join(entries)

	def format_twrp_flags(self, is_ab: bool = False, is_mtk: bool = False) -> str:
		"""Generate twrp.flags file content matching real device tree format."""
		lines = []
		lines.append("# mount point       fstype    device                                                                flags")
		lines.append("")

		# Collect logical partitions for super partition section
		logical_entries = []
		boot_entries = []
		sensitive_entries = []
		firmware_entries = []
		seen_partitions = set()

		for entry in self.entries:
			name = entry.partition_name
			if name in seen_partitions:
				continue
			seen_partitions.add(name)

			if entry.is_logical():
				logical_entries.append(entry)
			elif name in ("boot", "vendor_boot", "init_boot", "vbmeta", "vbmeta_system", "vbmeta_vendor"):
				boot_entries.append(entry)
			elif name in ("protect_f", "protect_s", "nvram", "nvdata", "persist", "persistent",
			              "frp", "modemst1", "modemst2", "metadata"):
				sensitive_entries.append(entry)
			elif name in ("dtbo", "logo", "expdb", "tee", "spmfw", "otp"):
				firmware_entries.append(entry)
			elif name in _SKIP_TWRP_FLAGS:
				continue

		# Super Partitions section
		if logical_entries:
			lines.append("# Super Partitions")
			for entry in logical_entries:
				name = entry.partition_name
				display = _DISPLAY_NAMES.get(name, name.title())
				suffix = "_a" if is_ab else ""
				mount_point = f"/{name}{suffix}"
				device = f"/dev/block/mapper/{name}{suffix}"
				flag_parts = ["backup=1", "flashimg=1"]
				if is_ab:
					flag_parts.append("slotselect")
				flag_parts.append(f'display="{display} Image"')
				flags = f'flags={";".join(flag_parts)}'
				lines.append(f"{mount_point:<25s} emmc      {device:<55s} {flags}")
			lines.append("")

		# Boot section
		if boot_entries:
			lines.append("# Boot")
			for entry in boot_entries:
				name = entry.partition_name
				display = _DISPLAY_NAMES.get(name, name.title())
				device = f"/dev/block/by-name/{name}"
				flags_parts = [f'display="{display}"', "flashimg=1", "backup=1"]
				if entry.is_slotselect():
					flags_parts.append("slotselect")
				lines.append(f"/{name:<24s} emmc      {device:<55s} flags={';'.join(flags_parts)}")
			lines.append("")

		# Sensitive data section
		if sensitive_entries:
			lines.append("# Sensitive data")
			for entry in sensitive_entries:
				name = entry.partition_name
				display = _DISPLAY_NAMES.get(name, name.title())
				# Map mount points to device paths
				src = entry.src
				if not src.startswith("/dev/"):
					src = f"/dev/block/by-name/{name}"

				flags_parts = [f'display="{display}"']
				if name not in ("frp", "persistent"):
					flags_parts.append("backup=1")
				lines.append(f"/{name:<24s} {entry.fs_type:<9s} {src:<55s} flags={';'.join(flags_parts)}")
			lines.append("")

		# Firmware section
		if firmware_entries:
			lines.append("# Firmware")
			for entry in firmware_entries:
				name = entry.partition_name
				display = _DISPLAY_NAMES.get(name, name.title())
				device = f"/dev/block/by-name/{name}"
				flags_parts = [f'display="{display}"']
				if name in _FLASHABLE_PARTITIONS:
					flags_parts.extend(["backup=1", "flashimg=1"])
				if entry.is_slotselect():
					flags_parts.append("slotselect")
				lines.append(f"/{name:<24s} emmc      {device:<55s} flags={';'.join(flags_parts)}")
			lines.append("")

		# Removable storage
		lines.append("# Removable storage")
		if is_mtk:
			lines.append('/external_sd              vfat      /dev/block/mmcblk1p1                 /dev/block/mmcblk1       flags=display="MicroSD";storage;wipeingui;removable')
		lines.append('/usbstorage               vfat      /dev/block/sda1                      /dev/block/sda           flags=fsflags=utf8;display="USB Storage";storage;wipeingui;removable')
		lines.append("")

		return "\n".join(lines)

	def get_partition_by_mount_point(self, mount_point: str):
		for entry in self.entries:
			if entry.mount_point == mount_point:
				return entry

		return None

	def get_logical_partitions(self):
		return [entry for entry in self.entries if entry.is_logical()]

	def get_logical_partitions_models(self) -> Set[PartitionModel]:
		models = set()

		for entry in self.entries:
			if entry.is_logical():
				partition_model = PartitionModel.from_mount_point(entry.mount_point)
				if partition_model is None:
					continue
				models.add(partition_model)

		return models

	def get_slotselect_partitions(self):
		return [entry for entry in self.entries if entry.is_slotselect()]

	def get_ab_partitions_models(self) -> Set[PartitionModel]:
		models = set()

		for entry in self.get_slotselect_partitions():
			partition_model = PartitionModel.from_mount_point(entry.mount_point)
			if partition_model is None:
				continue
			models.add(partition_model)

		return models

#
# Copyright (C) 2022 The Android Open Source Project
#
# SPDX-License-Identifier: Apache-2.0
#

from datetime import datetime
from git import Repo
from os import chmod
from pathlib import Path
from sebaubuntu_libs.libaik import AIKManager
from sebaubuntu_libs.libandroid.device_info import DeviceInfo
from sebaubuntu_libs.libandroid.fstab import Fstab
from sebaubuntu_libs.libandroid.props import BuildProp
from sebaubuntu_libs.liblogging import LOGD
from shutil import copyfile, rmtree
from stat import S_IRWXU, S_IRGRP, S_IROTH
from tempfile import TemporaryDirectory
from twrpdtgen import __version__ as version
from twrpdtgen.templates import render_template
from twrpdtgen.vendor_boot import (
	extract_vendor_boot,
	is_vendor_boot_image,
	VendorBootImageInfo,
)
from typing import List, Optional

BUILDPROP_LOCATIONS = [Path() / "default.prop",
                       Path() / "prop.default",]
BUILDPROP_LOCATIONS += [Path() / dir / "build.prop"
                        for dir in ["system", "vendor"]]
BUILDPROP_LOCATIONS += [Path() / dir / "etc" / "build.prop"
                        for dir in ["system", "vendor"]]

FSTAB_LOCATIONS = [Path() / "etc" / "recovery.fstab"]
FSTAB_LOCATIONS += [Path() / dir / "etc" / "recovery.fstab"
                    for dir in ["system", "vendor"]]

INIT_RC_LOCATIONS = [Path()]
INIT_RC_LOCATIONS += [Path() / dir / "etc" / "init"
                      for dir in ["system", "vendor"]]

# Known MTK (MediaTek) platform prefixes
MTK_PLATFORM_PREFIXES = ("mt", "MT")


def _is_mtk_platform(platform: str) -> bool:
	"""Return True if *platform* looks like a MediaTek SoC name."""
	if not platform:
		return False
	return any(platform.startswith(p) for p in MTK_PLATFORM_PREFIXES)


class DeviceTree:
	"""
	A class representing a device tree

	It initialize a basic device tree structure
	and save the location of some important files
	"""
	def __init__(self, image: Path):
		"""Initialize the device tree class."""
		self.image = image

		self.current_year = str(datetime.now().year)

		# Check if the image exists
		if not self.image.is_file():
			raise FileNotFoundError("Specified file doesn't exist")

		# Detect if this is a vendor_boot image
		self.is_vendor_boot = is_vendor_boot_image(image)
		self.vendor_boot_info: Optional[VendorBootImageInfo] = None

		# Temporary directory for vendor_boot extraction
		self._vendor_boot_tmpdir: Optional[TemporaryDirectory] = None

		if self.is_vendor_boot:
			LOGD("Detected vendor_boot image, extracting...")
			self._vendor_boot_tmpdir = TemporaryDirectory()
			vb_output = Path(self._vendor_boot_tmpdir.name)
			self.vendor_boot_info = extract_vendor_boot(image, vb_output)
			ramdisk_dir = self.vendor_boot_info.merged_ramdisk

			LOGD(f"vendor_boot header version: {self.vendor_boot_info.header_version}")
			LOGD(f"Number of ramdisks: {len(self.vendor_boot_info.vendor_ramdisk_entries)}")
			for i, entry in enumerate(self.vendor_boot_info.vendor_ramdisk_entries):
				LOGD(f"  Ramdisk {i}: type={entry.ramdisk_type}, "
				     f"size={entry.size}, name='{entry.name}'")

			# Create a minimal AIKImageInfo-compatible wrapper for templates
			self.aik_manager = None
			self.image_info = _VendorBootImageInfoAdapter(self.vendor_boot_info)
		else:
			# Standard boot/recovery image path
			self.aik_manager = AIKManager()
			self.image_info = self.aik_manager.unpackimg(image)
			ramdisk_dir = self.image_info.ramdisk

		if ramdisk_dir is None or not ramdisk_dir.is_dir():
			raise AssertionError("Ramdisk not found")

		LOGD("Getting device infos...")
		self.build_prop = BuildProp()
		for build_prop in [ramdisk_dir / location for location in BUILDPROP_LOCATIONS]:
			if not build_prop.is_file():
				continue

			self.build_prop.import_props(build_prop)

		self.device_info = DeviceInfo(self.build_prop)

		# Detect MTK platform
		self.is_mtk = _is_mtk_platform(self.device_info.platform)
		if self.is_mtk:
			LOGD(f"Detected MediaTek platform: {self.device_info.platform}")

		# Generate fstab
		fstab = None
		for fstab_location in [ramdisk_dir / location for location in FSTAB_LOCATIONS]:
			if not fstab_location.is_file():
				continue

			LOGD(f"Generating fstab using {fstab_location} as reference...")
			fstab = Fstab(fstab_location)
			break

		if fstab is None:
			raise AssertionError("fstab not found")

		self.fstab = fstab

		# Search for init rc files
		self.init_rcs: List[Path] = []
		for init_rc_path in [ramdisk_dir / location for location in INIT_RC_LOCATIONS]:
			if not init_rc_path.is_dir():
				continue

			self.init_rcs += [init_rc for init_rc in init_rc_path.iterdir()
			                  if init_rc.name.endswith(".rc") and init_rc.name != "init.rc"]

	def dump_to_folder(self, output_path: Path, git: bool = False) -> Path:
		device_tree_folder = output_path / self.device_info.manufacturer / self.device_info.codename
		prebuilt_path = device_tree_folder / "prebuilt"
		recovery_root_path = device_tree_folder / "recovery" / "root"

		LOGD("Creating device tree folders...")
		if device_tree_folder.is_dir():
			rmtree(device_tree_folder, ignore_errors=True)
		device_tree_folder.mkdir(parents=True)
		prebuilt_path.mkdir(parents=True)
		recovery_root_path.mkdir(parents=True)

		LOGD("Writing makefiles/blueprints")
		self._render_template(device_tree_folder, "Android.bp", comment_prefix="//")
		self._render_template(device_tree_folder, "Android.mk")
		self._render_template(device_tree_folder, "AndroidProducts.mk")
		self._render_template(device_tree_folder, "BoardConfig.mk")
		self._render_template(device_tree_folder, "device.mk")
		self._render_template(device_tree_folder, "extract-files.sh")
		self._render_template(device_tree_folder, "omni_device.mk", out_file=f"omni_{self.device_info.codename}.mk")
		self._render_template(device_tree_folder, "README.md")
		self._render_template(device_tree_folder, "setup-makefiles.sh")
		self._render_template(device_tree_folder, "vendorsetup.sh")

		# Set permissions
		chmod(device_tree_folder / "extract-files.sh", S_IRWXU | S_IRGRP | S_IROTH)
		chmod(device_tree_folder / "setup-makefiles.sh", S_IRWXU | S_IRGRP | S_IROTH)

		LOGD("Copying kernel...")
		if self.image_info.kernel is not None:
			copyfile(self.image_info.kernel, prebuilt_path / "kernel")
		if self.image_info.dt is not None:
			copyfile(self.image_info.dt, prebuilt_path / "dt.img")
		if self.image_info.dtb is not None:
			copyfile(self.image_info.dtb, prebuilt_path / "dtb.img")
		if self.image_info.dtbo is not None:
			copyfile(self.image_info.dtbo, prebuilt_path / "dtbo.img")

		LOGD("Copying fstab...")
		(device_tree_folder / "recovery.fstab").write_text(self.fstab.format(twrp=True))

		LOGD("Copying init scripts...")
		for init_rc in self.init_rcs:
			copyfile(init_rc, recovery_root_path / init_rc.name, follow_symlinks=True)

		if not git:
			return device_tree_folder

		# Create a git repo
		LOGD("Creating git repo...")

		git_repo = Repo.init(device_tree_folder)
		git_config_reader = git_repo.config_reader()
		git_config_writer = git_repo.config_writer()

		try:
			git_global_email, git_global_name = git_config_reader.get_value('user', 'email'), git_config_reader.get_value('user', 'name')
		except Exception:
			git_global_email, git_global_name = None, None

		if git_global_email is None or git_global_name is None:
			git_config_writer.set_value('user', 'email', 'barezzisebastiano@gmail.com')
			git_config_writer.set_value('user', 'name', 'Sebastiano Barezzi')

		git_repo.index.add(["*"])
		commit_message = self._render_template(None, "commit_message", to_file=False)
		git_repo.index.commit(commit_message)

		return device_tree_folder

	def _render_template(self, *args, comment_prefix: str = "#", **kwargs):
		return render_template(*args,
		                       comment_prefix=comment_prefix,
		                       current_year=self.current_year,
		                       device_info=self.device_info,
		                       fstab=self.fstab,
		                       image_info=self.image_info,
		                       is_mtk=self.is_mtk,
		                       is_vendor_boot=self.is_vendor_boot,
		                       vendor_boot_info=self.vendor_boot_info,
		                       version=version,
		                       **kwargs)

	def cleanup(self):
		# Cleanup
		if self.aik_manager is not None:
			self.aik_manager.cleanup()
		if self._vendor_boot_tmpdir is not None:
			self._vendor_boot_tmpdir.cleanup()


class _VendorBootImageInfoAdapter:
	"""
	Adapts a VendorBootImageInfo to the AIKImageInfo interface expected
	by templates, so that both vendor_boot and standard images can be
	rendered with the same templates.
	"""
	def __init__(self, vb: VendorBootImageInfo):
		self._vb = vb
		self.kernel = None
		self.dt = None
		self.dtb = vb.dtb
		self.dtbo = None
		self.ramdisk = vb.merged_ramdisk
		self.base_address = None
		self.board_name = vb.product_name or None
		self.cmdline = vb.cmdline or None
		self.dtb_offset = None
		self.header_version = str(vb.header_version)
		self.image_type = "VENDOR_BOOT"
		self.kernel_offset = None
		self.origsize = None
		self.os_version = None
		self.pagesize = str(vb.page_size) if vb.page_size else None
		self.ramdisk_compression = None
		self.ramdisk_offset = None
		self.sigtype = None
		self.tags_offset = None

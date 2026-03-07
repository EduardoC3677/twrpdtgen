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

# .rc files that are needed in the device tree (others are handled by the build system)
NEEDED_RC_PATTERNS = (
	"init.recovery.",   # Platform-specific recovery init (init.recovery.mt6768.rc, init.recovery.qcom.rc)
	"init.recovery.usb",  # USB gadget configuration
)
NEEDED_RC_NAMES = {
	"mtk-plpath-utils.rc",  # MTK preloader path utils service
	"snapuserd.rc",         # Virtual A/B snapshot daemon
}

# Recovery-specific service binaries to extract from ramdisk
# (standard Android utilities like toybox/sh are provided by the TWRP build)
# MTK binaries like mtk_plpath_utils are provided by the build system
RECOVERY_SERVICE_BINS = {
	# Qualcomm
	"qseecomd",
	"android.hardware.gatekeeper@1.0-service-qti",
	"android.hardware.keymaster@4.1-service-qti",
	"android.hardware.keymaster@4.0-service-qti",
	"android.hardware.weaver@1.0-service",
	# Generic
	"hw_keymaster_v2", "teed",
}

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

		# Search for init rc files - only keep recovery-relevant ones
		self.init_rcs: List[Path] = []
		for init_rc_path in [ramdisk_dir / location for location in INIT_RC_LOCATIONS]:
			if not init_rc_path.is_dir():
				continue

			for init_rc in init_rc_path.iterdir():
				if not init_rc.name.endswith(".rc") or init_rc.name == "init.rc":
					continue
				# Only include .rc files that match known needed patterns
				if (init_rc.name in NEEDED_RC_NAMES
				    or any(init_rc.name.startswith(p) for p in NEEDED_RC_PATTERNS)):
					self.init_rcs.append(init_rc)

		# Determine kernel modules location in the ramdisk
		# (vendor/lib/modules or lib/modules — we preserve the original path)
		self.modules_in_vendor = False
		if self.is_vendor_boot and self.vendor_boot_info and self.vendor_boot_info.merged_ramdisk:
			merged = self.vendor_boot_info.merged_ramdisk
			if (merged / "vendor" / "lib" / "modules").is_dir():
				self.modules_in_vendor = True
			elif (merged / "lib" / "modules").is_dir():
				self.modules_in_vendor = False

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
		self._render_template(device_tree_folder, "twrp_device.mk", out_file=f"twrp_{self.device_info.codename}.mk")
		self._render_template(device_tree_folder, "README.md")
		self._render_template(device_tree_folder, "vendorsetup.sh")

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
		if self.is_vendor_boot:
			# For vendor_boot devices, fstab goes in recovery/root/system/etc/
			fstab_dir = recovery_root_path / "system" / "etc"
			fstab_dir.mkdir(parents=True, exist_ok=True)
			(fstab_dir / "recovery.fstab").write_text(self.fstab.format(twrp=True))
			# Generate twrp.flags for vendor_boot devices
			twrp_flags = self.fstab.format_twrp_flags(
				is_ab=self.device_info.device_is_ab,
				is_mtk=self.is_mtk,
			)
			(fstab_dir / "twrp.flags").write_text(twrp_flags)
			LOGD("Generated twrp.flags")
		else:
			(device_tree_folder / "recovery.fstab").write_text(self.fstab.format(twrp=True))

		# Copy first_stage_ramdisk fstab if available (vendor_boot)
		if self.is_vendor_boot and self.vendor_boot_info and self.vendor_boot_info.merged_ramdisk:
			ramdisk = self.vendor_boot_info.merged_ramdisk
			fsr_dir = ramdisk / "first_stage_ramdisk"
			if fsr_dir.is_dir():
				for fstab_file in fsr_dir.iterdir():
					if fstab_file.name.startswith("fstab."):
						dest_fsr = recovery_root_path / "first_stage_ramdisk"
						dest_fsr.mkdir(parents=True, exist_ok=True)
						copyfile(fstab_file, dest_fsr / fstab_file.name, follow_symlinks=True)
						LOGD(f"Copied first_stage_ramdisk/{fstab_file.name}")

		LOGD("Copying init scripts...")
		for init_rc in self.init_rcs:
			copyfile(init_rc, recovery_root_path / init_rc.name, follow_symlinks=True)

		# Copy additional blobs and files from vendor_boot ramdisk
		if self.is_vendor_boot and self.vendor_boot_info and self.vendor_boot_info.merged_ramdisk:
			ramdisk = self.vendor_boot_info.merged_ramdisk

			# Copy kernel modules preserving their original path
			# (lib/modules/ or vendor/lib/modules/ depending on the image)
			self._modules_path = None  # Track for BoardConfig template
			for modules_rel in ["vendor/lib/modules", "lib/modules"]:
				modules_src = ramdisk / modules_rel
				if modules_src.is_dir():
					modules_dest = recovery_root_path / modules_rel
					modules_dest.mkdir(parents=True, exist_ok=True)
					module_count = 0
					for ko_file in modules_src.iterdir():
						if ko_file.name.endswith(".ko") or ko_file.name in ("modules.load", "modules.dep",
						                                                     "modules.alias", "modules.softdep",
						                                                     "modules.load.recovery"):
							copyfile(ko_file, modules_dest / ko_file.name, follow_symlinks=True)
							module_count += 1
					if module_count > 0:
						self._modules_path = modules_rel
						LOGD(f"Copied {module_count} modules to {modules_rel}")
					break

			# Copy vendor firmware blobs (touchscreen firmware, etc.)
			firmware_src = ramdisk / "vendor" / "firmware"
			if firmware_src.is_dir():
				firmware_dest = recovery_root_path / "vendor" / "firmware"
				firmware_dest.mkdir(parents=True, exist_ok=True)
				fw_count = 0
				for fw_file in firmware_src.iterdir():
					if fw_file.is_file():
						copyfile(fw_file, firmware_dest / fw_file.name, follow_symlinks=True)
						fw_count += 1
				if fw_count > 0:
					LOGD(f"Copied {fw_count} vendor firmware blobs")

			# Copy system/etc config files (cgroups.json, task_profiles.json, etc.)
			sys_etc_src = ramdisk / "system" / "etc"
			if sys_etc_src.is_dir():
				sys_etc_dest = recovery_root_path / "system" / "etc"
				sys_etc_dest.mkdir(parents=True, exist_ok=True)
				for cfg_file in sys_etc_src.iterdir():
					if cfg_file.is_file() and cfg_file.name != "recovery.fstab":
						copyfile(cfg_file, sys_etc_dest / cfg_file.name, follow_symlinks=True)

			# Copy system/bin/ recovery-specific service binaries
			# Only vendor HAL services needed for TWRP; standard utils are in the build
			sys_bin_src = ramdisk / "system" / "bin"
			bin_count = 0
			if sys_bin_src.is_dir():
				sys_bin_dest = recovery_root_path / "system" / "bin"
				for bin_file in sys_bin_src.iterdir():
					if bin_file.is_file() and bin_file.name in RECOVERY_SERVICE_BINS:
						sys_bin_dest.mkdir(parents=True, exist_ok=True)
						copyfile(bin_file, sys_bin_dest / bin_file.name, follow_symlinks=True)
						bin_count += 1
				if bin_count > 0:
					LOGD(f"Copied {bin_count} recovery service binaries")

			# Copy Qualcomm-specific HAL dependencies only when service binaries are present
			if bin_count > 0:
				# Copy system/etc/vintf/ manifest files (HAL declarations)
				vintf_src = ramdisk / "system" / "etc" / "vintf"
				if vintf_src.is_dir():
					self._copy_dir_recursive(vintf_src,
					                         recovery_root_path / "system" / "etc" / "vintf")

				# Copy vendor/etc/ (vintf manifests, config files)
				vendor_etc_src = ramdisk / "vendor" / "etc"
				if vendor_etc_src.is_dir():
					self._copy_dir_recursive(vendor_etc_src,
					                         recovery_root_path / "vendor" / "etc")

				# Copy vendor/lib64/ shared libraries (HAL blob dependencies)
				for libdir_name in ["vendor/lib64", "vendor/lib64/hw"]:
					libdir_src = ramdisk / Path(libdir_name)
					if libdir_src.is_dir() and any(f.name.endswith(".so") for f in libdir_src.iterdir() if f.is_file()):
						self._copy_dir_recursive(libdir_src,
						                         recovery_root_path / Path(libdir_name))
						LOGD(f"Copied vendor libraries from {libdir_name}")

		# Copy MTK bootctrl sources for MediaTek devices
		if self.is_mtk:
			bootctrl_src = Path(__file__).parent / "templates" / "bootctrl"
			if bootctrl_src.is_dir():
				bootctrl_dest = device_tree_folder / "bootctrl"
				self._copy_dir_recursive(bootctrl_src, bootctrl_dest)
				LOGD("Copied MTK bootctrl sources")

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

	@staticmethod
	def _copy_dir_recursive(src: Path, dest: Path):
		"""Copy a directory tree, skipping symlinks that point outside src."""
		dest.mkdir(parents=True, exist_ok=True)
		for item in src.iterdir():
			target = dest / item.name
			if item.is_dir():
				DeviceTree._copy_dir_recursive(item, target)
			elif item.is_file():
				copyfile(item, target, follow_symlinks=True)

	def _render_template(self, *args, comment_prefix: str = "#", **kwargs):
		return render_template(*args,
		                       comment_prefix=comment_prefix,
		                       current_year=self.current_year,
		                       device_info=self.device_info,
		                       fstab=self.fstab,
		                       image_info=self.image_info,
		                       is_mtk=self.is_mtk,
		                       is_vendor_boot=self.is_vendor_boot,
		                       modules_in_vendor=self.modules_in_vendor,
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

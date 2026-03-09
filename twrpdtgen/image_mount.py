#
# Copyright (C) 2022 The Android Open Source Project
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Utilities for mounting Android partition images (vendor, system, etc.)
and extracting files needed for TWRP device tree generation.

Supports ext4, erofs, and Android sparse images (simg2img conversion).
"""

import subprocess
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory
from typing import Optional

from sebaubuntu_libs.liblogging import LOGD

# Touchscreen firmware file patterns (common across MTK and Qualcomm devices)
# These are the firmware blobs TWRP needs for touch input during recovery.
TOUCH_FIRMWARE_PATTERNS = (
	"ilitek", "focaltech", "focal", "novatek", "goodix", "himax",
	"chipone", "synaptics", "gt1151", "gt9", "nt36", "ft3",
	"_ts_fw", "_ts_mp", "hdl_firmware",
)

# Touchscreen kernel module name patterns
TOUCH_MODULE_PATTERNS = (
	"tp_", "focaltech", "ilitek", "goodix", "himax", "novatek",
	"chipone", "synaptics", "gt9", "nt36",
)

# -----------------------------------------------------------------
# MTK (MediaTek) HAL binaries to extract from vendor/bin/hw/
# Based on: android_device_motorola_penangf-twrp (MT6768, decrypted)
# -----------------------------------------------------------------
MTK_VENDOR_BINS_HW = {
	"android.hardware.gatekeeper@1.0-service",
	"android.hardware.keymaster@4.1-service.trustonic",
	"android.hardware.vibrator-service.mediatek",
	"vendor.mediatek.hardware.keymaster_attestation@1.1-service",
	# Newer API variants
	"android.hardware.gatekeeper-service.trustonic",
	"android.hardware.gatekeeper-service",
	"android.hardware.security.keymint@3.0-service.trustonic",
	"android.hardware.security.keymint-service.trustonic",
}

# MTK standalone vendor binaries (not in hw/)
MTK_VENDOR_BINS = {
	"mcDriverDaemon",  # Trustonic TEE daemon (required for decryption)
}

# MTK vendor libraries needed for HAL services
# Includes both HIDL (Android 12) and AIDL (Android 15) naming conventions
# Based on: penangf-twrp (HIDL) and lamu vendor (AIDL)
MTK_VENDOR_LIBS = {
	# Gatekeeper (HIDL + AIDL)
	"android.hardware.gatekeeper@1.0.so",
	"android.hardware.gatekeeper-V1-ndk.so",
	# Keymaster / KeyMint (HIDL + AIDL)
	"android.hardware.keymaster@3.0.so",
	"android.hardware.keymaster@4.0.so",
	"android.hardware.keymaster@4.1.so",
	"android.hardware.keymaster-V4-ndk.so",
	"android.hardware.security.keymint-V1-ndk.so",
	"android.hardware.security.keymint-V3-ndk.so",
	# Vibrator (HIDL + AIDL)
	"android.hardware.vibrator-V1-ndk_platform.so",
	"android.hardware.vibrator-V2-cpp.so",
	"android.hardware.vibrator-V2-ndk_platform.so",
	"android.hardware.vibrator-V2-ndk.so",
	"android.hardware.vibrator@1.0.so",
	"android.hardware.vibrator@1.1.so",
	"android.hardware.vibrator@1.2.so",
	"android.hardware.vibrator@1.3.so",
	"libvibrator.so",
	"libvibratorservice.so",
	"libvibratorutils.so",
	# Suspend / Power
	"android.system.suspend-V1-ndk.so",
	"android.system.suspend.control-V1-cpp.so",
	"android.system.suspend.control.internal-cpp.so",
	"android.system.suspend@1.0.so",
	# Trustonic TEE
	"libMcClient.so",
	"libTEECommon.so",
	"libthha.so",
	"vendor.trustonic.tee-V1-ndk.so",
	"vendor.trustonic.tui-V1-ndk.so",
	# Keymaster internals (HIDL-era devices)
	"libkeymaster4.so",
	"libkeymaster41.so",
	"libkeymaster4support.so",
	"libkeymaster4_1support.so",
	"libkeymaster_messages.so",
	"libkeymaster_portable.so",
	"libpuresoftkeymasterdevice.so",
	"libsoft_attestation_cert.so",
	"libcppbor_external.so",
	"libcppcose_rkp.so",
	"libkmsetkey.so",
	"kmsetkey.default.so",
	"libladder.so",
	# Hardware abstraction
	"libhardware.so",
	"libhardware_legacy.so",
	# MTK-specific
	"libion_mtk.so",
	"libion_ulit.so",
	"libtinyalsa.so",
	# MediaTek keymaster attestation (HIDL-era)
	"vendor.mediatek.hardware.keymaster_attestation@1.0.so",
	"vendor.mediatek.hardware.keymaster_attestation@1.1.so",
	"vendor.mediatek.hardware.keymaster_attestation@1.1-impl.so",
}

# MTK vendor/lib64/hw/ libraries
MTK_VENDOR_HW_LIBS = {
	"android.hardware.gatekeeper@1.0-impl.so",
	"gatekeeper.default.so",
	"gatekeeper.trustonic.so",
	"libMcGatekeeper.so",
	"libSoftGatekeeper.so",
}

# MTK vibrator device-specific libraries (platform name varies)
MTK_VENDOR_VIBRATOR_PATTERN = "vibrator."

# -----------------------------------------------------------------
# Qualcomm HAL binaries for system/bin/
# Based on: android_device_motorola_bangkk-twrp (Snapdragon)
# -----------------------------------------------------------------
QCOM_SYSTEM_BINS = {
	"qseecomd",
	"android.hardware.gatekeeper@1.0-service-qti",
	"android.hardware.keymaster@4.1-service-qti",
	"android.hardware.keymaster@4.0-service-qti",
}

# Qualcomm vendor libraries needed for QSEE/keymaster/gatekeeper
# Based on: android_device_motorola_bangkk-twrp (Snapdragon)
QCOM_VENDOR_LIBS = {
	# QSEE (Qualcomm Secure Execution Environment)
	"libQSEEComAPI.so",
	"libGPreqcancel.so",
	"libGPreqcancel_svc.so",
	# DRM / Secure storage
	"libdrmfs.so",
	"libdrmtime.so",
	"libdrmutils.so",
	"libdrm.so",
	"libssd.so",
	"librpmb.so",
	# QMI (Qualcomm Message Interface)
	"libqmi_cci.so",
	"libqmi_client_qmux.so",
	"libqmi_common_so.so",
	"libqmi_encdec.so",
	"libqmiservices.so",
	# Display
	"libdisplayconfig.qti.so",
	"libqservice.so",
	"libqdutils.so",
	"vendor.display.config@1.0.so",
	"vendor.display.config@2.0.so",
	# Keymaster
	"libkeymasterdeviceutils.so",
	"libkeymasterprovision.so",
	"libkeymasterutils.so",
	"libqtikeymaster4.so",
	# Misc hardware support
	"libdiag.so",
	"libdsutils.so",
	"libidl.so",
	"libmdmdetect.so",
	"libops.so",
	"libqcbor.so",
	"libqisl.so",
	"librecovery_updater_msm.so",
	"libsoc_helper.so",
	"libtime_genoff.so",
	# WiFi keystore
	"libkeystore-engine-wifi-hidl.so",
	"libkeystore-wifi-hidl.so",
	"vendor.qti.hardware.wifi.keystore@1.0.so",
}

# Qualcomm vendor/lib64/hw/ libraries
QCOM_VENDOR_HW_LIBS = {
	"android.hardware.gatekeeper@1.0-impl-qti.so",
}

# System libraries for MTK (from system partition)
MTK_SYSTEM_LIBS = {
	"android.hardware.boot@1.0.so",
	"android.hardware.boot@1.1.so",
	"android.hardware.boot@1.2.so",
	"android.hardware.boot-V1-ndk.so",
	"android.hardware.gatekeeper@1.0.so",
	"android.hardware.gatekeeper-V1-ndk.so",
	"libgatekeeper.so",
}

# System libraries for Qualcomm (from system partition) — none needed,
# Qualcomm uses vendor-only model for libs.

# -----------------------------------------------------------------
# Properties to extract from vendor/system build.prop for system.prop
# These are needed for TWRP crypto decryption support.
# Based on: penangf system.prop
# -----------------------------------------------------------------
SYSTEM_PROP_KEYS = {
	# Crypto
	"ro.crypto.volume.filenames_mode",
	"ro.crypto.support_metadata_encrypt",
	# TEE
	"ro.vendor.mtk_tee_gp_support",
	"ro.vendor.mtk_trustonic_tee_support",
	# Gatekeeper / Keymaster
	"ro.hardware.kmsetkey",
	"ro.hardware.gatekeeper",
	"keymaster_ver",
	# Qualcomm equivalents
	"ro.hardware.keystore",
	"ro.hardware.keystore_desede",
}


class MountedImage:
	"""Context manager that mounts an Android partition image read-only."""

	def __init__(self, image_path: Path):
		self.image_path = image_path
		self._tmpdir: Optional[TemporaryDirectory] = None
		self._converted_path: Optional[Path] = None
		self._mount_point: Optional[Path] = None

	def __enter__(self) -> "MountedImage":
		self._tmpdir = TemporaryDirectory()
		self._mount_point = Path(self._tmpdir.name) / "mnt"
		self._mount_point.mkdir()

		img_to_mount = self.image_path

		# Check if it's an Android sparse image and convert if needed
		file_output = subprocess.run(
			["file", str(self.image_path)],
			capture_output=True, text=True
		).stdout
		if "Android sparse image" in file_output:
			if which("simg2img") is None:
				raise RuntimeError("simg2img not found; install android-sdk-libsparse-utils")
			raw_path = Path(self._tmpdir.name) / "raw.img"
			subprocess.run(
				["simg2img", str(self.image_path), str(raw_path)],
				check=True, capture_output=True
			)
			img_to_mount = raw_path
			self._converted_path = raw_path

		# Try mounting as ext4 first, then erofs
		mounted = False
		for fstype in [None, "ext4", "erofs"]:
			cmd = ["sudo", "mount", "-o", "ro"]
			if fstype:
				cmd += ["-t", fstype]
			cmd += [str(img_to_mount), str(self._mount_point)]
			result = subprocess.run(cmd, capture_output=True, text=True)
			if result.returncode == 0:
				mounted = True
				break

		if not mounted:
			raise RuntimeError(
				f"Failed to mount {self.image_path}. "
				f"Ensure ext4/erofs support is available."
			)

		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		if self._mount_point and self._mount_point.is_dir():
			subprocess.run(
				["sudo", "umount", str(self._mount_point)],
				capture_output=True
			)
		if self._tmpdir:
			self._tmpdir.cleanup()

	@property
	def path(self) -> Path:
		"""Root path of the mounted image."""
		return self._mount_point


def _is_touch_firmware(name: str) -> bool:
	"""Check if a filename looks like touchscreen firmware."""
	name_lower = name.lower()
	return any(p in name_lower for p in TOUCH_FIRMWARE_PATTERNS)


def _is_touch_module(name: str) -> bool:
	"""Check if a .ko filename looks like a touchscreen driver."""
	name_lower = name.lower()
	return any(p in name_lower for p in TOUCH_MODULE_PATTERNS)


# Recovery-relevant VINTF manifest name patterns for vendor/etc/vintf/manifest/.
# Based on penangf-twrp: only HAL declarations for services the DT uses.
# gatekeeper/keymint → decryption, health → battery, vibrator → haptic,
# hwcomposer → display, lbs → location (MTK), boot → bootctrl.
_MTK_VINTF_MANIFEST_PATTERNS = (
	"gatekeeper", "keymint", "keymaster", "health",
	"vibrator", "hwcomposer", "lbs",
)

_QCOM_VINTF_MANIFEST_PATTERNS = (
	"gatekeeper", "keymint", "keymaster",
)


def _extract_vintf_manifests(vintf_src: Path, vintf_dest: Path,
                             is_mtk: bool = False) -> None:
	"""Extract only recovery-relevant VINTF manifest files."""
	# Copy top-level manifest.xml and compatibility_matrix.xml
	for fname in ["manifest.xml", "compatibility_matrix.xml"]:
		src_f = vintf_src / fname
		if src_f.is_file():
			_sudo_copy(src_f, vintf_dest / fname)

	# Copy only recovery-relevant manifest/*.xml entries
	patterns = _MTK_VINTF_MANIFEST_PATTERNS if is_mtk else _QCOM_VINTF_MANIFEST_PATTERNS
	manifest_dir = vintf_src / "manifest"
	if manifest_dir.is_dir():
		count = 0
		for xml_file in _sudo_find_files(manifest_dir):
			if not xml_file.name.endswith(".xml"):
				continue
			name_lower = xml_file.name.lower()
			if any(p in name_lower for p in patterns):
				_sudo_copy(xml_file, vintf_dest / "manifest" / xml_file.name)
				count += 1
		if count > 0:
			LOGD(f"Extracted {count} recovery-relevant vintf manifests")


def _sudo_copy(src: Path, dest: Path) -> bool:
	"""Copy a file using sudo (for root-owned mounted images) and fix ownership."""
	dest.parent.mkdir(parents=True, exist_ok=True)
	result = subprocess.run(
		["sudo", "cp", str(src), str(dest)],
		capture_output=True, text=True
	)
	if result.returncode != 0:
		return False
	subprocess.run(
		["sudo", "chown", f"{subprocess.getoutput('id -u')}:{subprocess.getoutput('id -g')}",
		 str(dest)],
		capture_output=True
	)
	return True


def _sudo_find_files(directory: Path, maxdepth: int = 1) -> list:
	"""List files in a directory using sudo."""
	result = subprocess.run(
		["sudo", "find", str(directory), "-maxdepth", str(maxdepth), "-type", "f"],
		capture_output=True, text=True
	)
	return [Path(l) for l in result.stdout.strip().split("\n") if l]


def extract_vendor_files(
	vendor_image: Path,
	recovery_root: Path,
	build_prop: "BuildProp",
	is_mtk: bool = False,
) -> None:
	"""
	Extract recovery-relevant files from a vendor partition image.

	Copies to recovery/root/:
	  - vendor/firmware/ (touchscreen firmware blobs)
	  - vendor/lib/modules/ (touchscreen .ko drivers, if present)
	  - vendor/bin/hw/ (HAL service binaries for MTK)
	  - vendor/lib64/ (HAL shared libraries)
	  - vendor build.prop (imported into the BuildProp instance)
	"""
	LOGD(f"Mounting vendor image: {vendor_image}")

	with MountedImage(vendor_image) as mnt:
		root = mnt.path

		# Import vendor build.prop for additional device info
		for bp_path in [root / "build.prop", root / "etc" / "build.prop"]:
			if bp_path.is_file():
				try:
					content = subprocess.run(
						["sudo", "cat", str(bp_path)],
						capture_output=True, text=True, check=True
					).stdout
					tmp = Path(mnt._tmpdir.name) / "vendor_build.prop"
					tmp.write_text(content)
					build_prop.import_props(tmp)
					LOGD("Imported vendor build.prop")
				except Exception as e:
					LOGD(f"Warning: could not import vendor build.prop: {e}")
				break

		# Copy touchscreen firmware from vendor/firmware/
		firmware_src = root / "firmware"
		if firmware_src.is_dir():
			firmware_dest = recovery_root / "vendor" / "firmware"
			fw_count = 0
			for fw_file in _sudo_find_files(firmware_src):
				if _is_touch_firmware(fw_file.name):
					if _sudo_copy(fw_file, firmware_dest / fw_file.name):
						fw_count += 1
			if fw_count > 0:
				LOGD(f"Extracted {fw_count} touchscreen firmware files from vendor")

		# Copy touchscreen kernel modules from vendor/lib/modules/
		for mod_dir in ["lib/modules", "lib64/modules"]:
			modules_src = root / mod_dir
			if not modules_src.is_dir():
				continue
			mod_count = 0
			for ko_file in _sudo_find_files(modules_src):
				if ko_file.name.endswith(".ko") and _is_touch_module(ko_file.name):
					mod_dest = recovery_root / "vendor" / mod_dir
					if _sudo_copy(ko_file, mod_dest / ko_file.name):
						mod_count += 1
			if mod_count > 0:
				LOGD(f"Extracted {mod_count} touchscreen modules from vendor/{mod_dir}")

		# ---- HAL binaries and libraries ----

		if is_mtk:
			# MTK: extract gatekeeper/keymint/vibrator binaries from vendor/bin/hw/
			bin_hw_src = root / "bin" / "hw"
			if bin_hw_src.is_dir():
				bin_count = 0
				for bin_file in _sudo_find_files(bin_hw_src):
					if bin_file.name in MTK_VENDOR_BINS_HW:
						dest = recovery_root / "vendor" / "bin" / "hw" / bin_file.name
						if _sudo_copy(bin_file, dest):
							bin_count += 1
				if bin_count > 0:
					LOGD(f"Extracted {bin_count} MTK HAL binaries from vendor/bin/hw/")

			# MTK: extract standalone vendor binaries (mcDriverDaemon)
			bin_src = root / "bin"
			if bin_src.is_dir():
				sbin_count = 0
				for bin_file in _sudo_find_files(bin_src):
					if bin_file.name in MTK_VENDOR_BINS:
						dest = recovery_root / "vendor" / "bin" / bin_file.name
						if _sudo_copy(bin_file, dest):
							sbin_count += 1
				if sbin_count > 0:
					LOGD(f"Extracted {sbin_count} MTK vendor binaries")

			# MTK: extract HAL shared libraries from vendor/lib64/
			lib64_src = root / "lib64"
			if lib64_src.is_dir():
				lib_count = 0
				for lib_file in _sudo_find_files(lib64_src):
					if (lib_file.name in MTK_VENDOR_LIBS
					    or lib_file.name.startswith(MTK_VENDOR_VIBRATOR_PATTERN)):
						dest = recovery_root / "vendor" / "lib64" / lib_file.name
						if _sudo_copy(lib_file, dest):
							lib_count += 1
				if lib_count > 0:
					LOGD(f"Extracted {lib_count} MTK vendor libraries from vendor/lib64/")

			# MTK: extract vendor/lib64/hw/ libraries
			lib64_hw_src = root / "lib64" / "hw"
			if lib64_hw_src.is_dir():
				hw_count = 0
				for lib_file in _sudo_find_files(lib64_hw_src):
					if (lib_file.name in MTK_VENDOR_HW_LIBS
					    or lib_file.name.startswith(MTK_VENDOR_VIBRATOR_PATTERN)):
						dest = recovery_root / "vendor" / "lib64" / "hw" / lib_file.name
						if _sudo_copy(lib_file, dest):
							hw_count += 1
				if hw_count > 0:
					LOGD(f"Extracted {hw_count} MTK vendor/lib64/hw/ libraries")

			# MTK: extract Trustonic TEE trustlets from vendor/app/mcRegistry/
			mc_registry_src = root / "app" / "mcRegistry"
			if mc_registry_src.is_dir():
				mc_count = 0
				for mc_file in _sudo_find_files(mc_registry_src):
					if mc_file.name.endswith((".drbin", ".tlbin", ".tabin")):
						dest = recovery_root / "vendor" / "app" / "mcRegistry" / mc_file.name
						if _sudo_copy(mc_file, dest):
							mc_count += 1
				if mc_count > 0:
					LOGD(f"Extracted {mc_count} Trustonic TEE trustlets from vendor/app/mcRegistry/")

			# MTK: extract vendor/etc/vintf manifests
			vintf_dir = root / "etc" / "vintf"
			if vintf_dir.is_dir():
				_extract_vintf_manifests(vintf_dir, recovery_root / "vendor" / "etc" / "vintf",
				                        is_mtk=True)

		else:
			# Qualcomm: extract HAL shared libraries from vendor/lib64/
			lib64_src = root / "lib64"
			if lib64_src.is_dir():
				lib_count = 0
				for lib_file in _sudo_find_files(lib64_src):
					if lib_file.name in QCOM_VENDOR_LIBS:
						dest = recovery_root / "vendor" / "lib64" / lib_file.name
						if _sudo_copy(lib_file, dest):
							lib_count += 1
				if lib_count > 0:
					LOGD(f"Extracted {lib_count} Qualcomm vendor libraries from vendor/lib64/")

			# Qualcomm: extract vendor/lib64/hw/ libraries
			lib64_hw_src = root / "lib64" / "hw"
			if lib64_hw_src.is_dir():
				hw_count = 0
				for lib_file in _sudo_find_files(lib64_hw_src):
					if lib_file.name in QCOM_VENDOR_HW_LIBS:
						dest = recovery_root / "vendor" / "lib64" / "hw" / lib_file.name
						if _sudo_copy(lib_file, dest):
							hw_count += 1
				if hw_count > 0:
					LOGD(f"Extracted {hw_count} Qualcomm vendor/lib64/hw/ libraries")

		# Extract vendor/etc config files for Qualcomm devices
		if not is_mtk:
			vendor_etc_src = root / "etc"
			# Copy only recovery-relevant vendor vintf manifests
			vintf_dir = vendor_etc_src / "vintf"
			if vintf_dir.is_dir():
				_extract_vintf_manifests(vintf_dir, recovery_root / "vendor" / "etc" / "vintf",
				                        is_mtk=False)

			# Copy gpfspath_oem_config.xml (Qualcomm secure path config)
			gpfs_config = vendor_etc_src / "gpfspath_oem_config.xml"
			if gpfs_config.is_file():
				dest = recovery_root / "vendor" / "etc" / "gpfspath_oem_config.xml"
				_sudo_copy(gpfs_config, dest)


def extract_system_files(
	system_image: Path,
	recovery_root: Path,
	build_prop: "BuildProp",
	is_mtk: bool = False,
) -> None:
	"""
	Extract recovery-relevant information from a system partition image.

	Imports system build.prop and extracts platform-specific binaries/libraries.
	"""
	LOGD(f"Mounting system image: {system_image}")

	with MountedImage(system_image) as mnt:
		root = mnt.path

		# Import system build.prop
		for bp_path in [
			root / "system" / "build.prop",
			root / "build.prop",
		]:
			if bp_path.is_file():
				try:
					content = subprocess.run(
						["sudo", "cat", str(bp_path)],
						capture_output=True, text=True, check=True
					).stdout
					tmp = Path(mnt._tmpdir.name) / "system_build.prop"
					tmp.write_text(content)
					build_prop.import_props(tmp)
					LOGD("Imported system build.prop")
				except Exception as e:
					LOGD(f"Warning: could not import system build.prop: {e}")
				break

		if is_mtk:
			# MTK: extract system/lib64/ HAL libraries
			sys_lib64 = root / "system" / "lib64"
			if not sys_lib64.is_dir():
				sys_lib64 = root / "lib64"
			if sys_lib64.is_dir():
				lib_count = 0
				for lib_file in _sudo_find_files(sys_lib64):
					if lib_file.name in MTK_SYSTEM_LIBS:
						dest = recovery_root / "system" / "lib64" / lib_file.name
						if _sudo_copy(lib_file, dest):
							lib_count += 1
				if lib_count > 0:
					LOGD(f"Extracted {lib_count} MTK system libraries from system/lib64/")

		else:
			# Qualcomm: extract QSEE/gatekeeper/keymaster binaries from system/bin/
			for bin_dir in [root / "system" / "bin", root / "bin"]:
				if not bin_dir.is_dir():
					continue
				bin_count = 0
				for bin_file in _sudo_find_files(bin_dir):
					if bin_file.name in QCOM_SYSTEM_BINS:
						dest = recovery_root / "system" / "bin" / bin_file.name
						if _sudo_copy(bin_file, dest):
							bin_count += 1
				if bin_count > 0:
					LOGD(f"Extracted {bin_count} Qualcomm system binaries from system/bin/")
				break

		# Extract system/etc/vintf manifests
		for vintf_base in [root / "system" / "etc" / "vintf", root / "etc" / "vintf"]:
			if not vintf_base.is_dir():
				continue
			# Copy top-level manifest.xml and compatibility_matrix.device.xml
			for fname in ["manifest.xml", "compatibility_matrix.device.xml"]:
				src_f = vintf_base / fname
				if src_f.is_file():
					dest = recovery_root / "system" / "etc" / "vintf" / fname
					_sudo_copy(src_f, dest)
			# MTK: copy hwservicemanager.xml (needed for HIDL service registration)
			if is_mtk:
				manifest_dir = vintf_base / "manifest"
				if manifest_dir.is_dir():
					for xml_file in _sudo_find_files(manifest_dir):
						if "hwservicemanager" in xml_file.name:
							dest = recovery_root / "system" / "etc" / "vintf" / "manifest" / xml_file.name
							_sudo_copy(xml_file, dest)
			LOGD("Extracted system/etc/vintf manifests")
			break


def collect_system_props(build_prop: "BuildProp") -> dict:
	"""
	Collect crypto/TEE/gatekeeper properties from build.prop for system.prop.

	Returns a dict of {category: {key: value}} suitable for the system.prop template.
	Based on: android_device_motorola_penangf-twrp system.prop
	"""
	props = {}
	all_props = {}

	# Try to get properties from the BuildProp instance
	for key in SYSTEM_PROP_KEYS:
		try:
			value = build_prop.get_prop(key)
			if value:
				all_props[key] = value
		except Exception:
			continue

	# Categorize collected properties
	crypto_props = {}
	tee_props = {}
	gatekeeper_props = {}

	for key, value in all_props.items():
		if "crypto" in key or "encrypt" in key:
			crypto_props[key] = value
		elif "tee" in key or "trustonic" in key:
			tee_props[key] = value
		elif "gatekeeper" in key or "kmsetkey" in key or "keymaster" in key or "keystore" in key:
			gatekeeper_props[key] = value

	if crypto_props:
		props["Crypto"] = crypto_props
	if tee_props:
		props["TEE"] = tee_props
	if gatekeeper_props:
		props["Gatekeeper"] = gatekeeper_props

	return props

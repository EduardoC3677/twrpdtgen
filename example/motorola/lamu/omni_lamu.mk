#
# Copyright (C) 2026 The Android Open Source Project
# Copyright (C) 2026 SebaUbuntu's TWRP device tree generator
#
# SPDX-License-Identifier: Apache-2.0
#

# Inherit from those products. Most specific first.
$(call inherit-product, $(SRC_TARGET_DIR)/product/core_64_bit.mk)
$(call inherit-product, $(SRC_TARGET_DIR)/product/full_base_telephony.mk)

# Inherit some common Omni stuff.
$(call inherit-product, vendor/omni/config/common.mk)

# Inherit from lamu device
$(call inherit-product, device/motorola/lamu/device.mk)

PRODUCT_DEVICE := lamu
PRODUCT_NAME := omni_lamu
PRODUCT_BRAND := motorola
PRODUCT_MODEL := moto g15
PRODUCT_MANUFACTURER := motorola

PRODUCT_GMS_CLIENTID_BASE := android-motorola

PRODUCT_BUILD_PROP_OVERRIDES += \
    PRIVATE_BUILD_DESC="lamu_g-user 15 VVTA35.51-18-6 1e9f09 release-keys"

BUILD_FINGERPRINT := motorola/lamu_g/lamu:15/VVTA35.51-18-6/1e9f09:user/release-keys

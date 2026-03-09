#
# Copyright (C) 2026 The Android Open Source Project
# Copyright (C) 2026 SebaUbuntu's TWRP device tree generator
#
# SPDX-License-Identifier: Apache-2.0
#

# Inherit from those products. Most specific first.
$(call inherit-product, $(SRC_TARGET_DIR)/product/core_64_bit.mk)
$(call inherit-product, $(SRC_TARGET_DIR)/product/full_base_telephony.mk)

# Inherit some common twrp stuff.
$(call inherit-product-if-exists, vendor/twrp/config/common.mk)

# Inherit from lamu device
$(call inherit-product, device/motorola/lamu/device.mk)

PRODUCT_DEVICE := lamu
PRODUCT_NAME := twrp_lamu
PRODUCT_BRAND := motorola
PRODUCT_MODEL := moto g15
PRODUCT_MANUFACTURER := motorola

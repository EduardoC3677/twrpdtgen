#
# Copyright (C) 2022 The Android Open Source Project
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
from pathlib import Path

# Insert the local vendored libs directory at the front of sys.path
# so that the bundled sebaubuntu_libs is used instead of any externally
# installed version.
_libs_path = str(Path(__file__).parent / "libs")
if _libs_path not in sys.path:
	sys.path.insert(0, _libs_path)

__version__ = "3.1.0"

module_path = Path(__file__).parent
current_path = Path.cwd()

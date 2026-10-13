"""Build the small ARMv7/glibc hook locally, before any service interruption."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile

NATIVE = Path(__file__).with_name("native")


def build_helper():
    bundled = NATIVE / "capture-armhf.so"
    if bundled.is_file():
        data = bundled.read_bytes()
    else:
        # Zig's pinned glibc target avoids binding to the build host's newer libc.
        if importlib.util.find_spec("ziglang") is None:
            raise ValueError("Install superlink2mqtt[extraction-build] to build the gateway capture helper")
        with tempfile.TemporaryDirectory(prefix="superlink-build-") as directory:
            output = Path(directory) / "capture.so"
            try:
                subprocess.run([sys.executable, "-m", "ziglang", "cc", "-target",
                    "arm-linux-gnueabihf.2.30", "-mcpu=cortex_a7", "-shared", "-fPIC", "-O2", "-s",
                    "-Werror", "-Wall", str(NATIVE / "capture.c"), "-o", str(output),
                    "-ldl", "-pthread"], check=True, capture_output=True, timeout=180)
            except (subprocess.SubprocessError, OSError):
                raise ValueError("Could not build the ARM gateway helper; check the extraction-build installation") from None
            data = output.read_bytes()
    if len(data) > 2 * 1024 * 1024 or data[:6] != b"\x7fELF\x01\x01" or data[18:20] != b"\x28\x00":
        raise ValueError("Invalid ARM gateway capture helper")
    return data

#!/usr/bin/env python3
"""Build a self-contained local HA app directory from this checkout."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]


def stage(target):
    target.mkdir(parents=True, exist_ok=False)
    for name in ("config.yaml", "run.py", "DOCS.md"):
        shutil.copy2(ROOT / "homeassistant/superlink2mqtt" / name, target / name)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, target / name)
    shutil.copytree(ROOT / "tools/sx1302/superlink", target / "tools/sx1302/superlink",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Share the normal gateway container build.
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerfile = dockerfile[:dockerfile.index("USER superlink")]
    dockerfile += '''ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.type="app" io.hass.version=$BUILD_VERSION io.hass.arch=$BUILD_ARCH
COPY run.py /app/run.py
ENV PYTHONUNBUFFERED=1
WORKDIR /config
ENTRYPOINT ["python", "/app/run.py"]
'''
    (target / "Dockerfile").write_text(dockerfile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="new staging directory")
    stage(parser.parse_args().target)

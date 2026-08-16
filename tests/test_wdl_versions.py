"""Guard: every WDL image tag tracks the package version.

The ``anvil-data-qc:<tag>`` image reference appears in several places -- the ``docker`` default in
each ``wdl/*.wdl`` and the ``docker`` override in every committed inputs JSON (``*.inputs.example.json``
and the production ``*.inputs.json``). These have silently drifted from ``__version__`` more than once
(an inputs example sat two releases behind). This test makes the set self-enforcing: bump
``__version__`` and any stale tag fails here.
"""

import re
from pathlib import Path

from anvil_data_qc import __version__

WDL_DIR = Path(__file__).resolve().parent.parent / "wdl"
# Match only concrete version tags (start with a digit), so the parameter_meta doc placeholder
# ``.../anvil-data-qc:<tag>.`` is ignored while real ``...:0.3.2`` references are checked.
IMAGE_TAG = re.compile(r"anvil-data-qc:(\d[^\"'\s]*)")


def test_all_wdl_image_tags_match_package_version():
    references = []  # (file, tag)
    # All WDLs + all committed inputs JSON (example and production). Outputs JSON carry no image
    # reference, so globbing every *.json is harmless and future-proofs against new config files.
    for path in sorted(WDL_DIR.glob("*.wdl")) + sorted(WDL_DIR.glob("*.json")):
        references.extend((path.name, tag) for tag in IMAGE_TAG.findall(path.read_text()))

    # Sanity: we actually found the references (so a path/glob change can't make this vacuously pass).
    assert references, f"no anvil-data-qc image references found under {WDL_DIR}"

    stale = sorted({(name, tag) for name, tag in references if tag != __version__})
    assert not stale, f"image tags out of sync with __version__={__version__}: {stale}"

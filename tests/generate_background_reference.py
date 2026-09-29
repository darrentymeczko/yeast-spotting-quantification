"""Regenerate frozen reference outputs from their stored synthetic inputs.

Requires Java/ImageJ only for reference generation, never for normal tests.
"""
import argparse
from pathlib import Path
import tempfile

import numpy as np
import tifffile

from compare_background import reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--imagej", type=Path, required=True)
    parser.add_argument("--fixture", type=Path,
                        default=Path(__file__).with_name("background_reference.npz"))
    args = parser.parse_args()
    with np.load(args.fixture, allow_pickle=False) as stored:
        arrays = dict(stored)
    with tempfile.TemporaryDirectory(prefix="background_reference_") as folder:
        work = Path(folder)
        for key in sorted(arrays):
            if not key.endswith("_input"):
                continue
            case = key.removesuffix("_input")
            source = work / f"{case}.tif"
            tifffile.imwrite(source, arrays[key])
            result, version = reference(args.java, args.imagej, source,
                                        work / f"{case}.expected.tif",
                                        float(arrays[case + "_radius"]))
            arrays[case + "_expected"] = result
            arrays["reference_version"] = version
    np.savez_compressed(args.fixture, **arrays)


if __name__ == "__main__":
    main()

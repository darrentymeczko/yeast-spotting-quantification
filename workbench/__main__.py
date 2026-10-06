"""`py -m workbench`.

Guarded, unlike a bare `raise SystemExit(main())`: the review tool's figures
are drawn in worker processes, which on Windows start by re-importing the main
module -- and must not each open another workbench when they do.
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from workbench.app import main

    raise SystemExit(main())

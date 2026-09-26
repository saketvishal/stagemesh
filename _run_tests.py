import os
import sys

os.environ["PYTHONNOUSERSITE"] = "1"
sys.path = [p for p in sys.path if "AppData\\Roaming\\Python" not in p]

import pytest

sys.exit(pytest.main(sys.argv[1:]))

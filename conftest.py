import os

from hypothesis import settings

# Child Pythons the tests start (`python -m minutas`, the recorder helper) run in
# development mode, so their resource and deprecation warnings show.
os.environ.setdefault("PYTHONDEVMODE", "1")

# CI sets FORCE_COLOR, which makes Python 3.14's argparse colour its help text;
# the tests compare plain text. PYTHON_COLORS outranks FORCE_COLOR.
os.environ["PYTHON_COLORS"] = "0"

# CI runners are slower and noisier than a workstation: no per-example deadline.
settings.register_profile("ci", deadline=None)
if os.environ.get("CI"):
    settings.load_profile("ci")

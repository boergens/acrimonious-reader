import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import acrimonious_reader  # noqa: E402,F401  (selects the GI versions before anything imports gi.repository)
from sample import make_sample  # noqa: E402


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "sample.pdf"
    make_sample(str(path))
    return path


@pytest.fixture(scope="session")
def locked_pdf(sample_pdf, tmp_path_factory):
    if shutil.which("qpdf") is None:
        pytest.skip("qpdf is needed to make an encrypted PDF")
    path = tmp_path_factory.mktemp("pdf") / "locked.pdf"
    subprocess.run(["qpdf", "--encrypt", "--user-password=secret", "--owner-password=owner",
                    "--bits=256", "--", str(sample_pdf), str(path)], check=True)
    return path

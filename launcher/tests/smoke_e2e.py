"""End-to-end smoke test for the moonlit launcher.

Hand-builds a tiny fake zipapp (one ``__main__.py`` that prints args), prepends
the vendored launcher and a ``#!`` shebang line, and runs the produced .exe.

Asserts:
  - exit code is forwarded from the child Python process,
  - stdout from ``print`` makes it through inherited stdio,
  - argv beyond ``argv[0]`` is forwarded to the launcher's child.

This is a smoke test only; the real moonlit-side test suite that pins this
behavior lives in ``tests/`` (Python). Skip on non-Windows.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "src" / "moonlit" / "_launchers" / "t-x64.exe"


def _build_fake_zipapp() -> bytes:
    buf = io.BytesIO()
    main_py = (
        "import sys\n"
        "print('argv0_basename=' + sys.argv[0].split('\\\\')[-1])\n"
        "print('forwarded=' + ' '.join(sys.argv[1:]))\n"
        "sys.exit(42 if sys.argv[1:] == ['fail'] else 0)\n"
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("__main__.py", main_py)
    return buf.getvalue()


def _assemble_exe(tmp: Path, shebang: str) -> Path:
    out = tmp / "smoke.exe"
    out.write_bytes(
        LAUNCHER.read_bytes()
        + b"#!" + shebang.encode("ascii") + b"\n"
        + _build_fake_zipapp()
    )
    return out


def main() -> int:
    if os.name != "nt":
        print("skipped: Windows-only smoke", file=sys.stderr)
        return 0
    if not LAUNCHER.is_file():
        print(f"missing launcher: {LAUNCHER}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        # 1. Happy path: pass a few args, expect exit 0 and forwarded stdout.
        exe = _assemble_exe(tmp, "py -3")
        proc = subprocess.run(
            [str(exe), "alpha", "beta gamma", "delta"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"FAIL: happy path returncode={proc.returncode}", file=sys.stderr)
            print("stdout:", proc.stdout, file=sys.stderr)
            print("stderr:", proc.stderr, file=sys.stderr)
            return 1
        if "forwarded=alpha beta gamma delta" not in proc.stdout:
            print(f"FAIL: argv forwarding broken; stdout={proc.stdout!r}", file=sys.stderr)
            return 1
        print("OK happy path:", proc.stdout.strip().replace("\n", " | "))

        # 2. Exit-code forwarding: child exits 42, launcher must surface it.
        proc = subprocess.run([str(exe), "fail"], capture_output=True, text=True)
        if proc.returncode != 42:
            print(f"FAIL: exit-code forwarding; got {proc.returncode}", file=sys.stderr)
            return 1
        print("OK exit-code forwarding (42)")

        # 3. Default-shebang fallback: shebang line is empty, launcher should
        #    use the built-in `py -3` default.
        exe2 = _assemble_exe(tmp, "")
        proc = subprocess.run([str(exe2), "x"], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"FAIL: default shebang; rc={proc.returncode}, stderr={proc.stderr}", file=sys.stderr)
            return 1
        print("OK default-shebang fallback")

    print("\nALL SMOKES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

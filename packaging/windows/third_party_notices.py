"""Write the license texts of everything netdiag.exe bundles into one file.

    python packaging/windows/third_party_notices.py OUT.txt DIST [DIST ...]

Python's own LICENSE.txt covers the interpreter and the libraries it ships
(OpenSSL, libffi, SQLite, expat and the rest). Each DIST is an installed
distribution bundled into the exe; its license files come from its metadata.
Exits non-zero when a license can't be found, so a package never ships without it.
"""

import sys
import sysconfig
from importlib import metadata
from pathlib import Path


def python_license() -> str:
    for candidate in (
        Path(sys.base_prefix, "LICENSE.txt"),
        Path(sysconfig.get_paths()["stdlib"], "LICENSE.txt"),
    ):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    sys.exit(f"Python's LICENSE.txt not found under {sys.base_prefix}")


def dist_licenses(name: str) -> str:
    dist = metadata.distribution(name)
    files = [
        f
        for f in dist.files or []
        if ".dist-info/" in f.as_posix()
        and f.name.upper().startswith(("LICENSE", "LICENCE", "COPYING"))
    ]
    if not files:
        sys.exit(f"no license file in the metadata of {name}")
    return "\n\n".join(f.read_text(encoding="utf-8") for f in files)


def main() -> None:
    out, names = Path(sys.argv[1]), sys.argv[2:]
    sections = [(f"Python {sys.version.split()[0]}", python_license())]
    for name in names:
        version = metadata.version(name)
        sections.append((f"{name} {version}", dist_licenses(name)))
    rule = "=" * 72
    out.write_text(
        "netdiag.exe bundles the following software.\n\n"
        + "\n\n".join(
            f"{rule}\n{title}\n{rule}\n\n{text.strip()}" for title, text in sections
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

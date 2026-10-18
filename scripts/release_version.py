"""Release helpers for semantic-release's exec plugin (stdlib only).

  set <version>    write the version into pyproject.toml and the plugin manifest
  check <version>  when EXPECTED_VERSION is set (a pinned manual run, e.g. v0.4.1),
                   fail unless <version> matches it; unset (a push) passes
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")
PLUGIN = os.path.join(ROOT, "plugins", "backtest2live", ".claude-plugin", "plugin.json")


def normalize(title):
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", title.strip())
    if not m:
        raise SystemExit(f"release: {title!r} is not a version like v0.2 or v0.2.1")
    return f"{m[1]}.{m[2]}.{m[3] or 0}"


def set_version(version):
    with open(PYPROJECT, encoding="utf-8") as fh:
        text = fh.read()
    new, n = re.subn(r'(?m)^version = "[^"]*"$', f'version = "{version}"', text, count=1)
    if n != 1:
        raise SystemExit("release: no version line in pyproject.toml")
    with open(PYPROJECT, "w", encoding="utf-8") as fh:
        fh.write(new)
    with open(PLUGIN, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["version"] = version
    with open(PLUGIN, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def check(version):
    expected = os.environ.get("EXPECTED_VERSION", "")
    if not expected:
        print(f"release: {version} (no version pin)")
        return
    want = normalize(expected)
    if version != want:
        raise SystemExit(
            f"release: commits since the last tag compute {version}, but the run was pinned to {want}. "
            "Fix the pin or the commit types; nothing was tagged.")
    print(f"release: {version} matches {expected}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("set", "check"):
        raise SystemExit(__doc__)
    {"set": set_version, "check": check}[sys.argv[1]](sys.argv[2])

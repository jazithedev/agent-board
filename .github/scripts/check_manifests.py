"""Checks that the Claude Code plugin and marketplace manifests are consistent with each other and with the files they name."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
problems: list[str] = []


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        problems.append(f"{path.relative_to(ROOT)}: {exc}")
        return {}


marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
if not marketplace.get("name"):
    problems.append("marketplace.json has no name")
for entry in marketplace.get("plugins", []):
    source = ROOT / entry.get("source", "")
    plugin = load(source / ".claude-plugin" / "plugin.json")
    if plugin.get("name") != entry.get("name"):
        problems.append(f"marketplace lists {entry.get('name')!r} but {source.name}/plugin.json is named {plugin.get('name')!r}")
    if not SEMVER.match(plugin.get("version", "")):
        problems.append(f"{source.name}: version {plugin.get('version')!r} is not x.y.z")
    hooks = load(source / "hooks" / "hooks.json")
    for module in hooks.get("modules", []):
        if not (source / "hooks" / module).exists():
            problems.append(f"{source.name}: hooks.json names {module}, which does not exist")
if not marketplace.get("plugins"):
    problems.append("marketplace.json lists no plugins")

version = re.search(r'__version__ = "([^"]+)"', (ROOT / "agent_board.py").read_text())
if not version or not SEMVER.match(version.group(1)):
    problems.append("agent_board.py has no x.y.z __version__")

if problems:
    print("\n".join(f"✗ {p}" for p in problems))
    sys.exit(1)
print(f"✓ manifests are consistent (board {version.group(1)})")

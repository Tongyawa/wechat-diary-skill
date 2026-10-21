"""Filesystem oracles for isolated raw inputs."""
import json
from wechat_diary_core import raw_archive as raw

def isolated_file(archive, live, source):
    """Observe the specified layout without using ApplyResult's path mapping."""
    parent = archive.parent.with_name(archive.parent.name + "-retained")
    matches = [directory / source.relative_to(live) for directory in parent.glob("*")
               if directory.is_dir() and (directory / source.relative_to(live)).is_file()]
    if not matches:
        raise AssertionError(f"missing isolated input: {source.name}")
    return sorted(matches)[-1]


def assert_self_contained(directory):
    """Use the planner's reference parser, and independently require real files."""
    for path in directory.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for reference in raw._chat_cas_references(data, path, directory):
            if not reference.is_relative_to(directory / "_media") or not reference.is_file():
                raise AssertionError("missing isolated CAS: " + str(reference))



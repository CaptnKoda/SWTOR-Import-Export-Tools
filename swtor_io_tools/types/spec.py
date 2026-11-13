# <pep8 compliant>

"""
Reads SWTOR character specifications: the small text files next to the
skeletons in art/dynamic/spec that tell the game how a skeleton is used.

    <name>.dyc        [SETTINGS] Skeleton=<name>_skeleton.gr2, and for some
                      rigs [MASKS]: named bone masks ("mask=upperbody",
                      followed by one "<bone>=<weight>" line per bone), the
                      bones an upper-body action plays on while the legs keep
                      walking.
    <name>.dat/.mag   [PARTS] Model=<name>.dyc plus the rig's animation setup:
                      AnimNetworkFolder (anim\\<category>\\<rig>\\, the folder
                      with its .jba and .mph files), AnimMetadataFqn (its
                      action list, anim\\<category>\\am_*.xml), AnimBodyType,
                      AnimNetworkType ... (.mag ones are for props, and also
                      name the prop's Mesh.)

Several specs can use one skeleton (bfa.dat and bfanew.dat both end up at
bfanew_skeleton.gr2; only bfa.dyc has the masks), so a skeleton's spec is the
one named after it, and its masks are gathered from every .dyc that uses it.

Independent of Blender: nothing here needs bpy.
"""

import os
from typing import Dict, List, Optional, Tuple

SPEC_FOLDER = os.path.join("art", "dynamic", "spec")

# {spec folder: (modification time, {skeleton file name (lower case): [.dyc info]}, {.dyc name: [spec info]})}
_folders_cache = {}


class CharacterSpec:
    """What a skeleton's character specification says about its animations."""

    def __init__(self, name, path, anim_folder, anim_metadata, body_type, network_type, masks, resources_root):
        # type: (str, str, Optional[str], Optional[str], Optional[str], Optional[str], Dict[str, Dict[str, float]], Optional[str]) -> None
        self.name = name                    # e.g. "bmnnew"
        self.path = path                    # the .dat / .mag file
        self.anim_folder = anim_folder      # resources-relative, "/"-separated: "anim/humanoid/bmnnew"
        self.anim_metadata = anim_metadata  # resources-relative: "anim/humanoid/am_humanoid.xml"
        self.body_type = body_type          # AnimBodyType, e.g. "Humanoid"
        self.network_type = network_type    # AnimNetworkType, e.g. "Combatant"
        self.masks = masks                  # {mask name: {bone name: weight}}
        self.resources_root = resources_root  # the resources folder the spec folder is in, if it is in one


def _read_lines(path):
    # type: (str) -> List[Tuple[str, str, str]]
    """[(section, key, value)] of an INI-like spec file, in order. Comments ('!') and blank lines skipped."""
    lines = []
    section = ""
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        for raw in file:
            line = raw.strip()
            if not line or line.startswith("!") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().upper()
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                lines.append((section, key.strip(), value.strip()))
    return lines


def _resources_path(value):
    # type: (str) -> Optional[str]
    """A spec's resources-relative path ("anim\\humanoid\\bmnnew\\") as "anim/humanoid/bmnnew"."""
    value = value.strip().replace("\\", "/").strip("/")
    return value or None


def _read_folder(spec_folder):
    # type: (str) -> Tuple[dict, dict]
    try:
        mtime = os.path.getmtime(spec_folder)
    except OSError:
        return {}, {}
    cached = _folders_cache.get(spec_folder)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]

    dycs_by_skeleton = {}
    specs_by_dyc = {}
    for file_name in sorted(os.listdir(spec_folder)):
        lower = file_name.lower()
        path = os.path.join(spec_folder, file_name)
        try:
            if lower.endswith(".dyc"):
                skeleton = None
                masks = {}
                mask = None
                for section, key, value in _read_lines(path):
                    if section == "SETTINGS" and key.lower() == "skeleton":
                        skeleton = value.lower()
                    elif section == "MASKS":
                        if key.lower() == "mask":
                            mask = masks.setdefault(value, {})
                        elif mask is not None:
                            try:
                                mask[key] = float(value)
                            except ValueError:
                                pass
                if skeleton:
                    dycs_by_skeleton.setdefault(skeleton, []).append((lower, masks))
            elif lower.endswith(".dat") or lower.endswith(".mag"):
                info = {"name": os.path.splitext(file_name)[0], "path": path}
                for section, key, value in _read_lines(path):
                    if section == "PARTS":
                        info.setdefault(key.lower(), value)
                model = info.get("model")
                if model:
                    specs_by_dyc.setdefault(model.lower(), []).append(info)
        except (OSError, UnicodeError):
            continue

    _folders_cache[spec_folder] = (mtime, dycs_by_skeleton, specs_by_dyc)
    return dycs_by_skeleton, specs_by_dyc


def _resources_root_of(spec_folder):
    # type: (str) -> Optional[str]
    folder = os.path.normpath(os.path.abspath(spec_folder))
    parts = folder.split(os.sep)
    if [p.lower() for p in parts[-3:]] == ["art", "dynamic", "spec"]:
        return os.sep.join(parts[:-3]) or os.sep
    return None


def find_character_spec(skeleton_file_name, spec_folders):
    # type: (str, List[str]) -> Optional[CharacterSpec]
    """
    The character specification of a skeleton (e.g. "bmnnew_skeleton.gr2"),
    looked for in each of spec_folders in turn (folders holding .dyc/.dat
    files, usually <resources>/art/dynamic/spec). None if none uses it.
    """
    skeleton = os.path.basename(skeleton_file_name).lower()
    if not skeleton.endswith(".gr2"):
        skeleton += ".gr2"
    stem = skeleton[:-4]
    if stem.endswith("_skeleton"):
        stem = stem[:-len("_skeleton")]

    for spec_folder in spec_folders:
        if not spec_folder or not os.path.isdir(spec_folder):
            continue
        dycs_by_skeleton, specs_by_dyc = _read_folder(spec_folder)
        dycs = dycs_by_skeleton.get(skeleton)
        if not dycs:
            continue

        # Masks from the skeleton's own .dyc first (bmnnew.dyc), then its body's
        # (bmn.dyc for bmnnew_skeleton.gr2), and only then any other rig's that
        # uses the same skeleton (assassin.dyc, an NPC, has its own smaller
        # upperbody mask).
        def rank(dyc):
            dyc_stem = dyc[0][:-4]
            return (0 if dyc_stem == stem else 1 if stem.startswith(dyc_stem) else 2, dyc_stem)

        masks = {}
        for _, dyc_masks in sorted(dycs, key=rank):
            for mask_name, bones in dyc_masks.items():
                masks.setdefault(mask_name, bones)

        specs = [spec for dyc_name, _ in dycs for spec in specs_by_dyc.get(dyc_name, [])]
        specs = [spec for spec in specs if spec.get("animnetworkfolder")] or specs
        if not specs:
            continue
        # The spec named after the skeleton (bmnnew.dat for bmnnew_skeleton.gr2) wins.
        specs.sort(key=lambda spec: (spec["name"].lower() != stem, spec["name"].lower()))
        spec = specs[0]
        return CharacterSpec(name=spec["name"], path=spec["path"],
                             anim_folder=_resources_path(spec.get("animnetworkfolder", "")),
                             anim_metadata=_resources_path(spec.get("animmetadatafqn", "")),
                             body_type=spec.get("animbodytype"), network_type=spec.get("animnetworktype"),
                             masks=masks, resources_root=_resources_root_of(spec_folder))
    return None


def spec_folders_for(skeleton_path, resources_dirs):
    # type: (str, List[str]) -> List[str]
    """Where to look for a skeleton's spec: its own folder (if it holds specs), then each resources folder's art/dynamic/spec."""
    folders = []
    if skeleton_path:
        own = os.path.dirname(os.path.abspath(skeleton_path))
        if os.path.isdir(own) and any(f.lower().endswith(".dyc") for f in os.listdir(own)):
            folders.append(own)
    for resources_dir in resources_dirs:
        if resources_dir:
            folder = os.path.join(os.path.abspath(resources_dir), SPEC_FOLDER)
            if folder not in folders:
                folders.append(folder)
    return folders

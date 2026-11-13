# <pep8 compliant>

"""
Reads SWTOR .amx files: the game's animation retargeting tables, one next to
each animation network (.mph) in a rig's folder (anim/<category>/<rig>/).

For every animation the network uses, an .amx gives a rig path and, by index,
a set of per-bone corrections. A folder path (with a trailing backslash:
"humanoid\\bmnnew\\" for a bmnnew animation used on bfanew, or the rig's own
folder) says which rig's folder the animation's file comes from, and the
corrections the game applies to fit it to this rig. A plain rig name ("humanoid\\bfanew", no
trailing backslash, about 90,000 humanoid entries) only names the rig the
network runs on, with identity corrections; folder_sources() leaves those
out. Each rig's anim_sharing.mph.amx lists most of its shared animations. Each correction is a translation (in
the .jba's own units and axes) and a rotation. The translations are the
difference between the two skeletons' rest offsets (bfanew's minus bmnnew's
for a bmnnew animation on bfanew); the rotations are per-animation pose
adjustments (up to 20° or more on shoulders, hips and wrists).

Layout (little-endian):
    "AMX " u8 version (1)
    entries, each:  u8 tag (3, or 2 without a set index: additive animations)
                    u8 length, animation name
                    u8 length, source rig path ("humanoid\\bmnnew\\")
                    u32 set index (tag 3 only)
    u8 0            end of the entries
    u32 number of sets, then per set:
        u32 number of bones, then per bone:
            u8 length, bone name
            7 x f32  translation x, y, z, rotation w, x, y, z
Many files hold no entries at all (10 bytes).

Independent of Blender: nothing here needs bpy.
"""

import os
import struct
from typing import Dict, List, Optional, Tuple

AMX_MAGIC = b"AMX "

Correction = Tuple[float, float, float, float, float, float, float]

# {rig folder: (modification time, {animation name: [Listing]})}
_folders_cache = {}


def read_amx(path):
    # type: (str) -> Tuple[List[Tuple[str, str, Optional[int]]], List[Dict[str, Correction]]]
    """([(animation name, source rig path, set index or None)], [{bone name: correction}]). Raises ValueError if malformed."""
    with open(path, "rb") as file:
        data = file.read()
    if data[:4] != AMX_MAGIC or len(data) < 6:
        raise ValueError(f"'{path}' is not an .amx file")
    pos = 5
    entries = []
    try:
        while data[pos] in (2, 3):
            tag = data[pos]
            length = data[pos + 1]
            name = data[pos + 2:pos + 2 + length].decode("ascii")
            pos += 2 + length
            length = data[pos]
            rig = data[pos + 1:pos + 1 + length].decode("ascii")
            pos += 1 + length
            index = None
            if tag == 3:
                index = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            entries.append((name, rig, index))
        if data[pos] != 0:
            raise ValueError(f"'{path}': unexpected entry tag {data[pos]}")
        pos += 1
        sets = []
        num_sets = struct.unpack_from("<I", data, pos)[0] if pos + 4 <= len(data) else 0
        pos += 4
        for _ in range(num_sets):
            num_bones = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            bones = {}
            for _ in range(num_bones):
                length = data[pos]
                name = data[pos + 1:pos + 1 + length].decode("ascii")
                bones.setdefault(name, struct.unpack_from("<7f", data, pos + 1 + length))
                pos += 1 + length + 28
            sets.append(bones)
    except (IndexError, struct.error, UnicodeDecodeError) as error:
        raise ValueError(f"'{path}' is corrupt or not supported ({error})")
    return entries, sets


def normalize_rig_path(rig_path):
    # type: (str) -> str
    """'humanoid\\bmnnew\\' -> 'humanoid/bmnnew'."""
    return rig_path.replace("\\", "/").strip("/")


ANIM_SHARING_FILE = "anim_sharing.mph.amx"


class Listing:
    """One way the rig's networks list an animation."""
    __slots__ = ("source", "corrections", "sharing")

    def __init__(self, source, corrections, sharing):
        # type: (str, Optional[Dict[str, Correction]], bool) -> None
        self.source = source            # 'humanoid/bmnnew'
        self.corrections = corrections  # {bone: correction}, or None
        self.sharing = sharing          # listed by the rig's anim_sharing.mph.amx

    def key(self):
        return (self.source, self.sharing, tuple(sorted((self.corrections or {}).items())))


def folder_sources(rig_folder):
    # type: (str) -> Dict[str, List[Listing]]
    """
    Every .amx in a rig's folder, merged: {animation name: [Listing]}, one
    per distinct listing (networks can list one animation differently).

    Kept: folder paths ("humanoid\\bmnnew\\", with the trailing backslash),
    which say which rig's folder the animation's file comes from. Left out:
    plain rig names ("humanoid\\bfanew", no trailing backslash), which only
    name the rig the network runs on, with identity corrections (they don't
    mean the file is played unconverted), and empty rig paths (bmnnew's
    animation.amx: 12 cd_jailer / cd_varta clips): their corrections don't
    fit the files they'd apply to, which already match bmnnew's rest pose
    exactly (adding them stretched the head by 41%).
    """
    try:
        mtime = os.path.getmtime(rig_folder)
    except OSError:
        return {}
    cached = _folders_cache.get(rig_folder)
    if cached and cached[0] == mtime:
        return cached[1]

    sources = {}
    for file_name in sorted(os.listdir(rig_folder)):
        if not file_name.lower().endswith(".amx"):
            continue
        try:
            entries, sets = read_amx(os.path.join(rig_folder, file_name))
        except (ValueError, OSError):
            continue
        sharing = file_name.lower() == ANIM_SHARING_FILE
        for name, rig, index in entries:
            if not rig.endswith("\\"):
                continue
            corrections = sets[index] if index is not None and index < len(sets) else None
            listing = Listing(normalize_rig_path(rig), corrections, sharing)
            listings = sources.setdefault(name, [])
            if all(listing.key() != other.key() for other in listings):
                listings.append(listing)

    _folders_cache[rig_folder] = (mtime, sources)
    return sources

# <pep8 compliant>

"""
Reads SWTOR 64-bit Morpheme (.mph) files: the animation data the game keeps in
each rig's folder (anim/<category>/<rig>/). They hold the rig's bone names and
which of the rig's bones each channel of an animation drives. 64-bit .jba files
rarely carry bone names themselves, so this is where they come from.

Independent of Blender: nothing here needs bpy or an operator, only paths, so
it can be run and tested on its own.

• find_bone_names(): the bone names of a .jba animation, looked up in a rig's
  folder. Reading every file of a folder takes a while, so results are cached
  per folder for the session.
• read_anim_bone_names(): the same information from a single .mph file.
• read_string_table_64(): the 64-bit string table layout, which .jba files
  with their own bone names use as well.

https://github.com/SWTOR-Slicers/WikiPedia/wiki/JBA-File-Structure
"""

import os
from typing import Dict, List, Optional, Tuple

from ..utils.binary import ArrayBuffer, DataView
from ..utils.number import align
from ..utils.string import readCString


MPH_MAGIC = 0x4257414D     # 'MAWB'
MPH_PREFIX_SIZE = 8        # magic + version
MPH_OBJECT_RIG = 2
MPH_OBJECT_RIG_TO_ANIM_MAP = 3
MPH_OBJECT_ANIM_LIBRARY = 1
MPH_ANIM_RECORD_SIZE = 0x58
MPH_ANIM_LIBRARY_FILE = "anim_library.mph"

# Counts above these mean a corrupt or misread file, not real data.
MAX_CHANNELS = 4096
MAX_STRINGS = 100000
MAX_ANIMS = 100000

# Where find_bone_names() got an animation's bone names from
BONES_FROM_LIBRARY = "library"    # anim_library.mph
BONES_FROM_DATA = "data"          # another .mph that lists the animation
BONES_INFERRED = "inferred"       # the folder's only bone order for the bone count


def read_string_table_64(dv, pos):
    # type: (DataView, int) -> Dict[int, str]
    num_strings = dv.getUint32(pos, True)
    if num_strings > MAX_STRINGS:
        raise ValueError(f"Implausible number of strings: {num_strings}")
    off_ids = dv.getUint64(pos + 8, True)
    off_offsets = dv.getUint64(pos + 16, True)
    off_strings = dv.getUint64(pos + 24, True)

    strings = {}
    for i in range(num_strings):
        string_id = dv.getUint32(pos + off_ids + (i * 4), True)
        if string_id == 0xFFFFFFFF:
            # Unused entry: its offset is not a valid one (this is common
            # in the rigs a .mph file holds besides the full detail one).
            continue
        strings[string_id] = readCString(dv, pos + off_strings + dv.getUint32(pos + off_offsets + (i * 4), True))
    return strings


def read_anim_bone_names(filepath):
    # type: (str) -> Dict[str, List[str]]
    """
    Returns, for each animation in a Morpheme network (.mph) file's full
    detail animation set, the rig bone name each of its channels drives (in
    channel order, i.e. the order of a .jba's bones), by animation name.

    A .mph file is a chain of {type u32, id u32, size u64} headed objects
    (data aligned to 16 bytes relative to the 8-byte 'MAWB' + version prefix):
    • Rig (2): bone names string table at data + u64 @ 0x20.
    • Rig-to-anim map (3): {count u64, offset u64} -> count x
      {rig bone u16, animation channel u16}.
    • Animation library (1): offset to the animation sets' offsets table at
      u64 @ 0x08, animation names string table at data + u64 @ 0x10. One
      animation set per LOD, the first being the full detail one: {u64,
      rig id u64, count u64, records offset u64}, then records of 0x58 bytes
      holding {animation id u32, map id u32} @ 0x50.
    """
    with open(filepath, 'rb') as file:
        buffer = ArrayBuffer()
        buffer.fromfile(file, os.path.getsize(filepath))

    dv = DataView(buffer)

    if dv.byteLength < MPH_PREFIX_SIZE or dv.getUint32(0, True) != MPH_MAGIC:
        return {}

    objects = {}
    library = None
    pos = MPH_PREFIX_SIZE
    while pos + 16 <= dv.byteLength:
        obj_type = dv.getUint32(pos, True)
        obj_id = dv.getUint32(pos + 4, True)
        obj_size = dv.getUint64(pos + 8, True)
        pos += 16
        if obj_size > dv.byteLength - pos:
            break
        objects[obj_id] = (obj_type, pos)
        if obj_type == MPH_OBJECT_ANIM_LIBRARY:
            library = pos
        pos = align(pos + obj_size, 16, MPH_PREFIX_SIZE)

    if library is None:
        return {}

    anim_names = read_string_table_64(dv, library + dv.getUint64(library + 0x10, True))
    anim_set = library + dv.getUint64(library + dv.getUint64(library + 8, True), True)
    rig_id = dv.getUint32(anim_set + 8, True)
    num_anims = dv.getUint64(anim_set + 16, True)
    if num_anims > MAX_ANIMS:
        raise ValueError(f"Implausible number of animations: {num_anims}")
    records = anim_set + dv.getUint64(anim_set + 24, True)

    rig_type, rig = objects[rig_id]
    if rig_type != MPH_OBJECT_RIG:
        return {}
    rig_bone_names = read_string_table_64(dv, rig + dv.getUint64(rig + 0x20, True))

    channel_maps = {}
    anim_bone_names = {}
    for i in range(num_anims):
        anim_id = dv.getUint32(records + (i * MPH_ANIM_RECORD_SIZE) + 0x50, True)
        map_id = dv.getUint32(records + (i * MPH_ANIM_RECORD_SIZE) + 0x54, True)

        if map_id not in channel_maps:
            channel_maps[map_id] = None
            map_type, rig_to_anim = objects.get(map_id, (None, 0))
            if map_type == MPH_OBJECT_RIG_TO_ANIM_MAP:
                num_channels = dv.getUint64(rig_to_anim, True)
                if num_channels > MAX_CHANNELS:
                    raise ValueError(f"Implausible number of channels: {num_channels}")
                pos = rig_to_anim + dv.getUint64(rig_to_anim + 8, True)
                channels = [None] * num_channels
                for j in range(num_channels):
                    rig_bone = dv.getUint16(pos, True)
                    channel = dv.getUint16(pos + 2, True)
                    pos += 4
                    channels[channel] = rig_bone_names[rig_bone]
                if None not in channels:
                    channel_maps[map_id] = channels

        if anim_id in anim_names and channel_maps[map_id]:
            anim_bone_names[anim_names[anim_id]] = channel_maps[map_id]

    return anim_bone_names


# Per rig folder, what has been read from its networks so far (reading all
# of a folder's networks takes a while: they can number in the thousands):
# • "library": animation name -> bone names, from anim_library.mph.
# • "networks": animation name -> distinct bone names lists, from all the
#   other networks (None until they are read).
# • "by_count": number of bones -> distinct bone names lists, from all of
#   the networks (None until they are read).
_rig_folders_cache = {}  # type: Dict[str, Dict[str, Optional[Dict]]]


def _read_anim_bone_names_safely(filepath):
    # type: (str) -> Dict[str, List[str]]
    try:
        return read_anim_bone_names(filepath)
    except (IndexError, KeyError, ValueError, OSError):
        print(f"SWTOR .jba 64-bit import: skipped unreadable animation data file \'{filepath}\'")
        return {}


def find_bone_names(rig_folder, anim_name, num_bones):
    # type: (str, str, int) -> Tuple[Optional[List[str]], Optional[str], int]
    """
    Returns (bone names, where they came from, 1), or (None, None, number of
    different bone orders the folder uses for num_bones bones) if the
    animation's bone names can't be determined.
    """
    cache = _rig_folders_cache.get(rig_folder)
    if cache is None:
        library_path = os.path.join(rig_folder, MPH_ANIM_LIBRARY_FILE)
        cache = {
            "library": _read_anim_bone_names_safely(library_path) if os.path.isfile(library_path) else {},
            "networks": None,
            "by_count": None,
        }
        _rig_folders_cache[rig_folder] = cache

    # The animation library network lists nearly every animation in its
    # folder, and has always agreed with the names tables some .jba files have.
    bone_names = cache["library"].get(anim_name)
    if bone_names and len(bone_names) == num_bones:
        return bone_names, BONES_FROM_LIBRARY, 1

    # Otherwise look through every other network. Networks in the same folder
    # can have been built with rigs listing the same bones in different
    # orders, so only take an answer that all of them agree on.
    if cache["networks"] is None:
        networks = {}
        by_count = {}
        for bone_names in cache["library"].values():
            if bone_names not in by_count.setdefault(len(bone_names), []):
                by_count[len(bone_names)].append(bone_names)
        for mph_file in sorted(os.listdir(rig_folder)):
            if not mph_file.lower().endswith(".mph") or mph_file.lower() == MPH_ANIM_LIBRARY_FILE:
                continue
            for name, bone_names in _read_anim_bone_names_safely(os.path.join(rig_folder, mph_file)).items():
                if bone_names not in networks.setdefault(name, []):
                    networks[name].append(bone_names)
                if bone_names not in by_count.setdefault(len(bone_names), []):
                    by_count[len(bone_names)].append(bone_names)
        cache["networks"] = networks
        cache["by_count"] = by_count

    candidates = [names for names in cache["networks"].get(anim_name, []) if len(names) == num_bones]
    if len(candidates) == 1:
        return candidates[0], BONES_FROM_DATA, 1

    # Last resort (also for a renamed .jba file): in some rigs' folders, all
    # the networks agree on a single bone order per number of bones.
    candidates = cache["by_count"].get(num_bones, [])
    if len(candidates) == 1:
        return candidates[0], BONES_INFERRED, 1

    return None, None, len(candidates)

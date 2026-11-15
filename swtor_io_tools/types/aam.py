# <pep8 compliant>

"""
Reads SWTOR action lists: anim/<category>/am_*.xml (e.g. am_humanoid.xml), the
game's list of every action a rig can play, by animation name ("animName"),
with how it plays:

    ambient=""          the animation loops (the game repeats it)
    blendInTime="0.2"   seconds the game blends into it from what played before
    blendOutTime="0.4"  seconds it blends out over
    fullBody=""         it plays on the whole body; otherwise the game can layer
                        it over the legs' movement through a bone mask
    mask="upperbody"    the bone mask it plays on (masks are defined in the
                        rig's .dyc: see types/spec.py)

The header's DefaultMask (e.g. "upperbody") is the mask actions use when they
don't name one and aren't fullBody. An animation can be used by several
actions; its flags are merged (it loops if any of them loops, the first
blend times and mask given are used).

Independent of Blender: nothing here needs bpy.
"""

import os
import xml.etree.ElementTree as ET
from typing import Dict, Optional

# {path: (modification time, ActionList)}
_cache = {}


class ActionInfo:
    __slots__ = ("loops", "blend_in", "blend_out", "full_body", "mask")

    def __init__(self):
        self.loops = False
        self.blend_in = None   # type: Optional[float]
        self.blend_out = None  # type: Optional[float]
        self.full_body = False
        self.mask = None       # type: Optional[str]


class ActionList:
    def __init__(self, path, default_mask, actions):
        # type: (str, Optional[str], Dict[str, ActionInfo]) -> None
        self.path = path
        self.default_mask = default_mask
        self.actions = actions

    def get(self, anim_name):
        # type: (str) -> Optional[ActionInfo]
        return self.actions.get(anim_name)

    def mask_for(self, anim_name):
        # type: (str) -> Optional[str]
        """The bone mask the game plays the animation on, or None if it plays on the whole body."""
        info = self.actions.get(anim_name)
        if info is None or info.full_body:
            return None
        return info.mask or self.default_mask


def _float(value):
    # type: (Optional[str]) -> Optional[float]
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def read_action_list(path):
    # type: (str) -> Optional[ActionList]
    """The action list in an am_*.xml file, or None if it can't be read."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        _cache[path] = (mtime, None)
        return None

    header = root.find("header")
    default_mask = header.get("DefaultMask") if header is not None else None
    actions = {}
    for action in root.iter("action"):
        anim_name = action.get("animName")
        if not anim_name:
            continue
        info = actions.get(anim_name)
        if info is None:
            info = actions[anim_name] = ActionInfo()
        if action.get("ambient") is not None:
            info.loops = True
        if action.get("fullBody") is not None:
            info.full_body = True
        if info.blend_in is None:
            info.blend_in = _float(action.get("blendInTime"))
        if info.blend_out is None:
            info.blend_out = _float(action.get("blendOutTime"))
        if info.mask is None and action.get("mask"):
            info.mask = action.get("mask")

    action_list = ActionList(path, default_mask, actions)
    _cache[path] = (mtime, action_list)
    return action_list

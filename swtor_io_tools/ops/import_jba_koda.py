# <pep8 compliant>

"""
"Biblically Accurate .JBA" -- a copy of import_jba_64.py that also works on
armatures imported with import_gr2_armature_koda.py ("Biblically Accurate .GR2"),
whose bones are limb-aligned.

Limb-aligned bones have the file's rest orientation turned by a fixed
per-bone rotation C (stored, row by row, in each bone's
'gr2_rest_correction' custom property): Blender rest = file rest @ C.
.jba data is authored against the file's own bone axes, so build() works out
each bone's file-axes rest (undoing C on the bone and on its parent), runs
the exact same math as import_jba_64.py, and turns the resulting pose basis
into the bone's own axes: basis = C^-1 @ file-axes basis @ C.

Bones without 'gr2_rest_correction' (e.g. armatures from ops/import_gr2.py)
use C = identity, giving exactly the same result as import_jba_64.py.

ADDITIVE ANIMATIONS
Some .jba files (in bmnnew, the 1060 'ad_' ones: facial expressions, aim and
yaw offsets ...) are additive: they don't hold bone poses but offsets to be
added on top of a pose. Every bone they don't move holds identity rotation and
zero translation (instead of the skeleton's own rest offsets), which the
absolute maths turns into a mangled pose. Measured on bmnnew's face and
2pistol yaw files, they compose like this, per bone, in the file's axes:
    rotation:    final = rest rotation @ additive rotation   (bone-local)
    translation: final = rest translation + additive translation (parent frame)
(an additive yaw turns LowerBack about the vertical only when applied
bone-locally; additive brow translations are exactly vertical only in the
parent's frame). Their keyframes are pure offsets from the rest pose, which
is exactly what Blender's NLA 'Combine' blending adds on top of the layers
below (it multiplies quaternions as lower @ upper -- the same, bone-local
side -- and adds locations, which are in each bone's rest frame).

NLA TRACKS (add-on preference 'Import JBA as NLA Track', jba_import_as_nla)
With it on, every import goes into its own NLA track and never replaces
what the armature is playing:
  • the armature's active action, if any, is first pushed down into an NLA
    track.
  • absolute animations go into a track under every additive layer.
  • additive animations go into a track on top of everything, with
    'Combine' blending, so they add to whatever plays below them.
With it off, every import (absolute or additive) is plain keyframes in a new
active action, as in import_jba_64.py; an additive one then shows its
offsets on top of the rest pose.
Strips hold their first/last frame outside their range, so a one-pose face
expression applies to the whole base animation; strips can be moved,
repeated or faded (influence) in the NLA editor. Additive files are detected
from their data (see _is_additive()), or can be forced either way.

Below, import_jba_64.py's own description:

This script imports Star Wars: The Old Republic 64-bit animations into Blender.

Usage:
Run this script from "File->Import" menu and then load the desired JBA animation file.

64-bit .jba files (SWTOR Game Update 7.2.1 onwards) hold the same compressed
keyframe data as the old 32-bit ones, so parsing differs only in the file's
surrounding structure:

• An 8-byte 'JAWB' + version prefix. Every 128-byte alignment in the rest of
  the file is relative to the end of it (offset 8), not to the file's start.
• Count fields that sit next to (now 64-bit) pointers are padded to 8 bytes,
  and the header's unknown fields (including the per-block ones after the
  block headers) are twice as long.
• Each block's header is 16 bytes instead of 8, and each bone's keyframe
  layout entry is 32 bytes instead of 16 ({count u32, pad, u64} x 2). The
  block sizes stored in the file header no longer match the blocks' actual
  sizes; each block ends at its keyframes' end, aligned to 128 bytes.
• Most files have no bone names table at the end. The few that do use the
  64-bit string table layout (see _read_string_table_64()).

When a file has no names table, bone names come from the Morpheme network
(.mph) files that sit next to the animations in the game's resources
(anim/<category>/<rig>/). Each one holds the rig (bone names) and the
rig-to-animation maps that say which rig bone each of an animation's channels
drives (see _read_mph_anim_bone_names()).

https://github.com/SWTOR-Slicers/WikiPedia/wiki/JBA-File-Structure
"""

import math
import os
import re
from typing import Dict, List, Optional, Set

from bpy import app
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import Context, Object, Operator, OperatorFileListElement
from mathutils import Matrix, Quaternion, Vector

from ..types.jba import JointBoneAnimation
from ..utils.binary import ArrayBuffer, DataView
from ..utils.string import path_split, readCString


JBA_64_MAGIC = 0x4257414A  # 'JAWB'
JBA_64_PREFIX_SIZE = 8     # magic + version

MPH_MAGIC = 0x4257414D     # 'MAWB'
MPH_PREFIX_SIZE = 8        # magic + version
MPH_OBJECT_RIG = 2
MPH_OBJECT_RIG_TO_ANIM_MAP = 3
MPH_OBJECT_ANIM_LIBRARY = 1
MPH_ANIM_RECORD_SIZE = 0x58
MPH_ANIM_LIBRARY_FILE = "anim_library.mph"

# Bones authored with a genuine 180°-around-their-own-Y rest orientation
# relative to their parent - the classic 3ds Max Biped root-bone convention
# (Bip01 points "backward" relative to the node above it by design). This is
# a real, correct fact about the bind pose, not a bug in it - but it means
# a bone's own jba rotation channel (which encodes motion relative to that
# flipped rest, same as every other bone) needs the same 180° accounted for
# when composing pose_bone.matrix_basis below, or an unanimated frame comes
# out as a spurious 180° flip instead of the identity it should be.
#
# This is intentionally a short, explicit name list rather than automatic
# per-bone detection (e.g. flagging any bone whose rest is ~180° from its
# parent). Bip01 is the only bone this has ever been observed on; a wrong
# auto-detected match on some other bone with a genuinely-intended large
# rest rotation would silently corrupt that bone's motion with no obvious
# symptom, which is worse than this list occasionally needing a bone added
# to it by hand.
KNOWN_180_FLIP_BONES = {"Bip01"}
BONE_REST_FLIP_CORRECTION = Matrix.Rotation(math.pi, 4, 'Y')


class ImportJBAKoda(Operator):
    """Import from SWTOR 64-bit JBA file format (.jba), including onto Limb-Aligned armatures"""
    bl_idname = "import_animation.jba_koda"
    bl_description = "Import and apply an animation to the active SWTOR skeleton in the scene.\n\n• Only compatible with .jba files extracted from SWTOR 64-bit\n   (Game Update 7.2.1 onwards).\n\n• Bone names are read from the game's animation network (.mph)\n   files: either the ones next to the .jba file, or the ones in the\n   Resources Directory set in the Add-on's Preferences\n   (anim/<category>/<rig>, e.g. anim/humanoid/bmnnew, matched\n   by the .jba's folder or the active skeleton's name).\n\n• Might apply exaggerated bone translations requiring cleanup\n   (check ZG SWTOR Tools Add-on's Clear Bones Translations tool).\n\n• Required for armatures imported with Biblically Accurate .GR2\n   (also works on other SWTOR armatures)."
    bl_label = "Import Biblically Accurate .JBA"
    bl_options = {'UNDO'}


    # File Browser properties (see ImportJBA for why
    # this doesn't use ImportHelper)

    filepath: StringProperty(subtype='FILE_PATH')

    if app.version < (2, 82, 0):
        directory = StringProperty(subtype='DIR_PATH')
    else:
        directory: StringProperty(subtype='DIR_PATH')

    filename_ext = ".jba"

    files: CollectionProperty(
        name="File Path",
        description="File path used for importing the JBA file",
        type=OperatorFileListElement,
    )

    filter_glob: StringProperty(
        default="*.jba",
        options={'HIDDEN'},
    )

    # Animation importing-related properties

    animation_type: EnumProperty(
        name="Animation Type",
        description="Whether the .jba holds bone poses or offsets to add on top of a pose",
        items=(
            ('AUTO', "Auto-Detect",
             "Detect additive animations from their data: in them, bones hold near-zero\n"
             "translations instead of the skeleton's own bone offsets"),
            ('ABSOLUTE', "Absolute", "The animation holds full bone poses (most animations)"),
            ('ADDITIVE', "Additive",
             "The animation holds offsets added on top of a pose (e.g. bmnnew's 'ad_' facial\n"
             "expressions and aim offsets). Applied on top of the rest pose: the keyframes are\n"
             "pure offsets, which can be layered over another action in the NLA ('Combine' blending)"),
        ),
        default='AUTO',
    )

    ignore_facial_bones: BoolProperty(
        name="Ignore Facial Transl.",
        description="Ignores the data in the facial bones' translation keyframes\nand only uses their rotation keyframes.\n\nAbsolute animations only: in additive ones, facial translations\nare part of the expression (e.g. raised brows)",
        default=True,
    )

    delete_180: BoolProperty(
        name="Delete 180º rotation",
        description="Keeps the animation data from turning the skeleton 180º by deleting\nthe keyframes assigned to the Bip01 bone and setting its rotation to zero.\n\nSWTOR animations turn characters so that they face away from the camera,\nas normally shown in the gameplay. That is not just a nuisance but a problem\nwhen adding cloth or physics simulations to capes or lekku: the instantaneous\nturn plays havok with them",
        default=False,
    )

    scale_animation: BoolProperty(
        name="Scale Animation",
        description="Scales the bones' translation data by a factor.\nIt must match the scale of the skeleton\nand objects to be animated for good results.\n\nWhenever possible, this setting will try to match\nthe Objects' Scale Factor automatically.\nIt will still allow for setting different values manually",
        default=False,
    )

    scale_factor: FloatProperty(
        name="Scale Factor",
        description="Scales the bones' translation data by a factor.\nIt must match the scale of the skeleton\nand objects to be animated.\n\nWhenever possible, this setting will try to match\nthe Objects Import Settings automatically\n(it still allows for setting different values)",
        default=1.0,
        soft_min=0.1,
        soft_max=2.0,
        precision=2,
    )


    def invoke(self, context, event):
        prefs = context.preferences.addons["swtor_io_tools"].preferences

        self.ignore_facial_bones = prefs.jba_ignore_facial_bones
        self.delete_180          = prefs.jba_delete_180
        self.scale_animation     = prefs.gr2_scale_object
        self.scale_factor        = prefs.gr2_scale_factor

        # Handling of filepath in case of being
        # filled as a param in an external call.
        if not self.filepath:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        else:
            return self.execute(context)


    def execute(self, context):
        # type: (Context) -> Set[str]

        paths = [os.path.join(self.directory, file.name) for file in self.files if file.name.lower().endswith(self.filename_ext)]

        if not paths:
            paths.append(self.filepath)

        self.files.clear()
        self.filepath = ""

        for path in paths:
            if not load(self, context, path):
                return {'CANCELLED'}

        return {'FINISHED'}


def _read_rotation_compressed(dv, pos, base, stride):
    # type: (DataView, int, Vector, Vector) -> Quaternion
    rot_x_raw = dv.getUint16(pos, True)
    rot_x = base.x + (rot_x_raw & 32767) * stride.x
    rot_y = base.y + dv.getUint16(pos + 2, True) * stride.y
    rot_z = base.z + dv.getUint16(pos + 4, True) * stride.z
    pos += 6
    rot_dot = rot_x * rot_x + rot_y * rot_y + rot_z * rot_z
    rot_w = 0.0 if rot_dot > 1.0 else math.sqrt(1.0 - rot_dot)

    if rot_x_raw & 32768:
        rot_w *= -1.0

    return Quaternion((rot_w, rot_x, rot_y, rot_z)).normalized()


def _read_translation_compressed(dv, pos, base, stride):
    # type: (DataView, int, Vector, Vector) -> Vector
    val = dv.getUint32(pos, True)
    pos += 4

    pos_x = base.x + (val >> 21) * stride.x
    pos_y = base.y + ((val >> 10) & 2047) * stride.y
    pos_z = base.z + (val & 1023) * stride.z

    return Vector((pos_x, pos_y, pos_z))


def _align(pos, alignment, prefix_size):
    # type: (int, int, int) -> int
    return ((pos - prefix_size + alignment - 1) & -alignment) + prefix_size


def _read_string_table_64(dv, pos):
    # type: (DataView, int) -> Dict[int, str]
    num_strings = dv.getUint32(pos, True)
    off_ids = dv.getUint64(pos + 8, True)
    off_offsets = dv.getUint64(pos + 16, True)
    off_strings = dv.getUint64(pos + 24, True)
    return {dv.getUint32(pos + off_ids + (i * 4), True):
            readCString(dv, pos + off_strings + dv.getUint32(pos + off_offsets + (i * 4), True))
            for i in range(num_strings)}


def _read_mph_anim_bone_names(filepath):
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
        objects[obj_id] = (obj_type, pos)
        if obj_type == MPH_OBJECT_ANIM_LIBRARY:
            library = pos
        pos = _align(pos + obj_size, 16, MPH_PREFIX_SIZE)

    if library is None:
        return {}

    anim_names = _read_string_table_64(dv, library + dv.getUint64(library + 0x10, True))
    anim_set = library + dv.getUint64(library + dv.getUint64(library + 8, True), True)
    rig_id = dv.getUint32(anim_set + 8, True)
    num_anims = dv.getUint64(anim_set + 16, True)
    records = anim_set + dv.getUint64(anim_set + 24, True)

    rig_type, rig = objects[rig_id]
    if rig_type != MPH_OBJECT_RIG:
        return {}
    rig_bone_names = _read_string_table_64(dv, rig + dv.getUint64(rig + 0x20, True))

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


def _read_mph_file_safely(filepath):
    # type: (str) -> Dict[str, List[str]]
    try:
        return _read_mph_anim_bone_names(filepath)
    except (IndexError, KeyError):
        print(f"SWTOR .jba 64-bit import: skipped unreadable network file \'{filepath}\'")
        return {}


def _find_bone_names(rig_folder, anim_name, num_bones):
    # type: (str, str, int) -> Optional[List[str]]
    cache = _rig_folders_cache.get(rig_folder)
    if cache is None:
        library_path = os.path.join(rig_folder, MPH_ANIM_LIBRARY_FILE)
        cache = {
            "library": _read_mph_file_safely(library_path) if os.path.isfile(library_path) else {},
            "networks": None,
            "by_count": None,
        }
        _rig_folders_cache[rig_folder] = cache

    # The animation library network lists nearly every animation in its
    # folder, and has always agreed with the names tables some .jba files have.
    bone_names = cache["library"].get(anim_name)
    if bone_names and len(bone_names) == num_bones:
        return bone_names

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
            for name, bone_names in _read_mph_file_safely(os.path.join(rig_folder, mph_file)).items():
                if bone_names not in networks.setdefault(name, []):
                    networks[name].append(bone_names)
                if bone_names not in by_count.setdefault(len(bone_names), []):
                    by_count[len(bone_names)].append(bone_names)
        cache["networks"] = networks
        cache["by_count"] = by_count

    candidates = [names for names in cache["networks"].get(anim_name, []) if len(names) == num_bones]
    if len(candidates) == 1:
        return candidates[0]

    # Last resort for a renamed .jba file: in some rigs' folders, all the
    # networks agree on a single bone order per number of bones.
    candidates = cache["by_count"].get(num_bones, [])
    if len(candidates) == 1:
        return candidates[0]

    return None


def _find_rig_folders(context, filepath):
    # type: (Context, str) -> List[str]
    rig_folders = []

    # A .jba still in its extracted location sits next to its networks.
    jba_folder = os.path.dirname(os.path.abspath(filepath))
    if any(f.lower().endswith(".mph") for f in os.listdir(jba_folder)):
        rig_folders.append(jba_folder)

    # Otherwise look for <Resources>/anim/<category>/<rig>, where <rig> is
    # the .jba's folder name (if copied along with it) or the active
    # skeleton's ("bmnnew_skeleton" or "bmnnew_skeleton.001" -> "bmnnew").
    rig_names = [os.path.basename(jba_folder)]
    ob = context.active_object
    if ob and ob.type == 'ARMATURE':
        rig_names.append(re.sub(r"(_skeleton)?(\.\d+)?$", "", ob.name))

    prefs = context.preferences.addons["swtor_io_tools"].preferences
    anim_folder = os.path.join(prefs.swtor_resources_dir, "anim")
    if prefs.swtor_resources_dir and os.path.isdir(anim_folder):
        for category in sorted(os.listdir(anim_folder)):
            for rig_name in rig_names:
                rig_folder = os.path.join(anim_folder, category, rig_name)
                if os.path.isdir(rig_folder) and rig_folder not in rig_folders:
                    rig_folders.append(rig_folder)

    return rig_folders


def read(operator, context, filepath):
    # type: (Operator, Context, str) -> Optional[JointBoneAnimation]
    with open(filepath, 'rb') as file:
        buffer = ArrayBuffer()
        buffer.fromfile(file, os.path.getsize(filepath))

    dv = DataView(buffer)
    pos = 0

    # Cancel import if this is not a 64-bit BioWare Austin / SWTOR JBA file
    if dv.byteLength < 4 or dv.getUint32(pos, True) != JBA_64_MAGIC:
        if dv.byteLength >= 4 and dv.getUint32(pos, True) == 0:
            operator.report({'ERROR'}, f"\'{filepath}\' is a 32-bit SWTOR jba file. Use the 32-bit .jba importer.")
        else:
            operator.report({'ERROR'}, f"\'{filepath}\' is not a valid 64-bit SWTOR jba file.")
        return None
    pos += 4
    pos += 4  # version
    pos += 8  # unknown

    # NOTE: File header
    length = dv.getFloat32(pos, True)
    pos += 4
    fps = dv.getFloat32(pos, True)
    pos += 4
    num_blocks = dv.getUint32(pos, True)
    pos += 8
    pos += 16  # unknown
    num_bones = dv.getUint32(pos, True)
    pos += 8
    pos += 24  # unknown

    # NOTE: Block headers
    num_frames = round(length * fps) + 1
    blocks = [None] * num_blocks
    for i in range(num_blocks):
        start_frame = dv.getUint32(pos, True)
        pos += 4
        block_size = dv.getUint32(pos, True)
        pos += 4
        blocks[i] = JointBoneAnimation.Block(start_frame, block_size)
        if i > 0:
            blocks[i - 1].num_frames = 1 + blocks[i].start_frame - blocks[i - 1].start_frame
        if i + 1 == num_blocks:
            blocks[i].num_frames = num_frames - blocks[i].start_frame
    pos += num_blocks * 8  # unknown

    # NOTE: Bone data
    pos = _align(pos, 128, JBA_64_PREFIX_SIZE)
    bones = [None] * num_bones
    for i in range(num_bones):
        translation_stride = Vector([dv.getFloat32(pos + (j * 4), True) for j in range(3)])
        pos += 12
        translation_base = Vector([dv.getFloat32(pos + (j * 4), True) for j in range(3)])
        pos += 12
        rotation_stride = Vector([dv.getFloat32(pos + (j * 4), True) for j in range(3)])
        pos += 12
        rotation_base = Vector([dv.getFloat32(pos + (j * 4), True) for j in range(3)])
        pos += 12
        bone = JointBoneAnimation.Bone(rotation_base, rotation_stride, translation_base, translation_stride)
        bone.rotations = [None] * num_frames
        bone.translations = [None] * num_frames
        bones[i] = bone

    # NOTE: Block data
    pos = _align(pos, 128, JBA_64_PREFIX_SIZE)
    for i in range(num_blocks):
        block = blocks[i]

        num_block_bones = dv.getUint32(pos, True)
        pos += 8
        assert(num_block_bones == num_bones)
        pos += 8  # unknown

        # Keyframe layout
        has_translations = [None] * num_block_bones
        for j in range(num_block_bones):
            # num_rotations = dv.getUint32(pos, True)
            pos += 8
            pos += 8  # unknown
            num_translations = dv.getUint32(pos, True)
            pos += 8
            pos += 8  # unknown
            has_translations[j] = num_translations > 0

        # Keyframes
        for j in range(num_bones):
            bone = bones[j]

            # Rotations
            for k in range(block.num_frames):
                bone.rotations[block.start_frame + k] = _read_rotation_compressed(
                    dv, pos, bone.rotation_base, bone.rotation_stride)
                pos += 6

            # Translations
            pos = (pos + 3) & -4
            if has_translations[j]:
                for k in range(block.num_frames):
                    bone.translations[block.start_frame + k] = _read_translation_compressed(
                        dv, pos, bone.translation_base, bone.translation_stride)
                    pos += 4
            else:
                # No translation track for this bone in this block: the
                # stride quantizes a range that has no samples, so the
                # correct value is translation_base on its own (raw = 0),
                # not translation_base plus the stride's maximum quantized
                # span (see build()'s comment on why translation_base is
                # never used as an absolute value on its own either way -
                # this bone will end up with delta == 0, same effect as
                # any other bone whose track happens not to move).
                for k in range(block.num_frames):
                    bone.translations[block.start_frame + k] = bone.translation_base.copy()

        # block.size can't be used to find the next block (see top of file)
        pos = _align(pos, 128, JBA_64_PREFIX_SIZE)

    # World space
    pos += 8  # unknown
    # fps = dv.getFloat32(pos, True)
    pos += 4
    translation_stride = Vector([dv.getFloat32(pos + (i * 4), True) for i in range(3)])
    pos += 12
    translation_base = Vector([dv.getFloat32(pos + (i * 4), True) for i in range(3)])
    pos += 12
    rotation_stride = Vector([dv.getFloat32(pos + (i * 4), True) for i in range(3)])
    pos += 12
    rotation_base = Vector([dv.getFloat32(pos + (i * 4), True) for i in range(3)])
    pos += 12
    pos = (pos + 7) & -8
    num_rotations = dv.getUint32(pos, True)
    pos += 8
    assert(num_rotations == num_frames)
    pos += 8  # unknown
    # num_translations = dv.getUint32(pos, True)
    pos += 8
    pos += 8  # unknown

    rotations = [_read_rotation_compressed(dv, pos + (i * 6), rotation_base, rotation_stride)
                 for i in range(num_frames)]
    pos += num_frames * 6

    pos = (pos + 3) & -4

    translations = [_read_translation_compressed(dv, pos + (i * 4), translation_base, translation_stride)
                    for i in range(num_frames)]
    pos += num_frames * 4

    world_space = JointBoneAnimation.WorldSpace(rotations, translations)

    # Bone names, from the file's own table if it has one, otherwise from
    # the rig's animation networks
    bone_names = None
    if pos + 32 <= dv.byteLength and dv.getUint32(pos, True) == num_bones:
        bone_names_by_id = _read_string_table_64(dv, pos)
        bone_names = [bone_names_by_id.get(i) for i in range(num_bones)]

    rig_folders = []
    if not bone_names or None in bone_names:
        bone_names = None
        rig_folders = _find_rig_folders(context, filepath)
        anim_name = os.path.splitext(os.path.basename(filepath))[0]
        for rig_folder in rig_folders:
            bone_names = _find_bone_names(rig_folder, anim_name, num_bones)
            if bone_names:
                break

    if not bone_names:
        if not rig_folders:
            operator.report({'ERROR'}, f"\'{filepath}\' has no bone names and the animation networks (.mph) to get them "
                                       "from weren't found. Set the Resources Directory in the Add-on's Preferences and "
                                       "select the skeleton the animation is for (or import the .jba from its extracted "
                                       "anim/<category>/<rig> folder).")
        else:
            operator.report({'ERROR'}, f"Couldn't find \'{anim_name}\' with {num_bones} bones in the animation networks "
                                       f"(.mph) of {rig_folders} to get its bone names from. If the .jba file was "
                                       "renamed, restore its original name.")
        return None

    for i, name in enumerate(bone_names):
        bones[i].name = name if name != "GOD" else "Bip01"

    return JointBoneAnimation(length, fps, num_frames, bones, world_space)


def _file_rest_local(pose_bone):
    # type: (bpy.types.PoseBone) -> Matrix
    """A bone's rest relative to its parent, in the file's own bone axes (see top of file)."""
    mat_rest = pose_bone.bone.matrix_local @ _rest_correction(pose_bone.bone).inverted()
    if pose_bone.parent:
        parent_rest = pose_bone.parent.bone.matrix_local @ _rest_correction(pose_bone.parent.bone).inverted()
        mat_rest = parent_rest.inverted() @ mat_rest
    return mat_rest


def _is_additive(jba, ob, morpheme_space_inv):
    # type: (JointBoneAnimation, Object, Matrix) -> bool
    """
    Additive .jba files hold zero translation for every bone they don't move,
    where absolute ones hold the skeleton's own bone-to-parent offsets. So:
    compare frame 0's translations with this armature's rest offsets, over
    the bones whose rest offset isn't (nearly) zero. Measured on bmnnew:
    em_dance_1 (absolute) gives a median ratio of 1.0, the 'ad_' files 0.0.
    """
    ratios = []
    for anim_bone in jba.bones:
        name = getattr(anim_bone, "name", "")
        if name not in ob.pose.bones or not ob.pose.bones[name].parent:
            continue
        rest_offset = _file_rest_local(ob.pose.bones[name]).to_translation().length
        if rest_offset < 1e-6:
            continue
        offset = (morpheme_space_inv @ Matrix.Translation(anim_bone.translations[0])).to_translation().length
        ratios.append(offset / rest_offset)
    if not ratios:
        return False
    ratios.sort()
    return ratios[len(ratios) // 2] < 0.5


def _is_additive_track(track):
    # type: (bpy.types.NlaTrack) -> bool
    return any(strip.action is not None and strip.action.get("jba_additive") for strip in track.strips)


def _lowest_additive_track(animation_data):
    # type: (bpy.types.AnimData) -> Optional[int]
    for i, track in enumerate(animation_data.nla_tracks):  # bottom to top
        if _is_additive_track(track):
            return i
    return None


def _new_base_track(animation_data, name):
    # type: (bpy.types.AnimData, str) -> bpy.types.NlaTrack
    """An NLA track for a base (absolute) animation: under every additive layer."""
    tracks = animation_data.nla_tracks
    lowest = _lowest_additive_track(animation_data)
    if lowest is None or lowest == 0:
        # No additive layers (or nothing to insert after: tracks.new() can
        # only add a track above another one): on top.
        track = tracks.new()
    else:
        track = tracks.new(prev=tracks[lowest - 1])
    track.name = name
    return track


def _add_strip(track, action, slot, blend_type):
    # type: (bpy.types.NlaTrack, bpy.types.Action, object, str) -> bpy.types.NlaStrip
    strip = track.strips.new(action.name, int(round(action.frame_range[0])), action)
    if slot is not None and hasattr(strip, "action_slot") and strip.action_slot != slot:
        try:
            strip.action_slot = slot
        except (AttributeError, TypeError, RuntimeError):
            pass
    strip.blend_type = blend_type
    strip.extrapolation = 'HOLD'
    return strip


def _move_active_action_to_nla(animation_data, track, blend_type):
    # type: (bpy.types.AnimData, bpy.types.NlaTrack, str) -> None
    action = animation_data.action
    slot = getattr(animation_data, "action_slot", None)
    animation_data.action = None
    _add_strip(track, action, slot, blend_type)


def _rest_correction(bone):
    # type: (bpy.types.Bone) -> Matrix
    """A bone's 'Limb-Aligned' rest correction C (Blender rest = file rest @ C), or identity."""
    values = bone.get("gr2_rest_correction")
    if values is None or len(values) != 9:
        return Matrix.Identity(4)
    return Matrix((values[0:3], values[3:6], values[6:9])).to_4x4()


def build(operator, context, filepath, jba):
    # type: (Operator, Context, str, JointBoneAnimation) -> bool
    import bpy
    # import os

    ob: Object = context.active_object

    if not ob or ob.type != 'ARMATURE':
        operator.report({'INFO'}, f"Requires object of type Armature to be active, not {ob.type}")
        return False

    # Create armature action
    # anim_name, _ = os.path.splitext(os.path.basename(filepath))
    anim_name = path_split(filepath)[:-4]

    if not ob.animation_data:
        ob.animation_data_create()

    # Enter pose mode
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='POSE')

    # Create armature keyframes
    # scale = 1000 * operator.scale_factor  # This was the original code's calculation, but it seems it needs to be
    # scale = 1000 * (1/operator.scale_factor) to work correctly, so…
    
    # Check if the armature object has import scale custom property data.
    # If not, use scale_animation and scale_factor or the add-on's prefs
    # settings for the .gr2 import.
    # "gr2_scale" was the old name for this property before it was renamed to
    # "import_scale" to disambiguate from the newer gr2_* BWAG-format round-trip
    # properties. Objects imported with older addon versions still carry the old
    # key, so we fall back to it rather than silently recalculating a (possibly
    # different) scale for those objects.
    if 'import_scale' in ob:
        scale = 1000 * (1 / ob['import_scale'])
    elif 'gr2_scale' in ob:  # legacy key, pre-rename
        scale = 1000 * (1 / ob['gr2_scale'])
    else:
        if operator.scale_animation:
            scale = 1000 * (1 / operator.scale_factor)
        else:
            prefs = context.preferences.addons["swtor_io_tools"].preferences
            if prefs.gr2_scale_object:
                scale = 1000 * (1 / prefs.gr2_scale_factor)
            else:
                scale = 1000
        
    morpheme_space = Matrix(((scale, 0, 0, 0), (0, 0, -scale, 0), (0, scale, 0, 0), (0, 0, 0, 1)))
    morpheme_space_inv = morpheme_space.inverted()

    if operator.animation_type == 'AUTO':
        additive = _is_additive(jba, ob, morpheme_space_inv)
    else:
        additive = operator.animation_type == 'ADDITIVE'
    operator.report({'INFO'}, f"'{anim_name}': {'additive' if additive else 'absolute'} animation")

    # Layering (see top of file)
    prefs = context.preferences.addons["swtor_io_tools"].preferences
    as_nla_track = getattr(prefs, "jba_import_as_nla", False)
    animation_data = ob.animation_data
    if animation_data.use_tweak_mode:
        animation_data.use_tweak_mode = False
    if as_nla_track:
        if animation_data.action is not None:
            # Whatever the armature is playing becomes the base layer.
            base = animation_data.action
            _move_active_action_to_nla(animation_data, _new_base_track(animation_data, base.name),
                                       animation_data.action_blend_type)
        elif additive and not animation_data.nla_tracks:
            # Nothing to add on top of yet: an empty base layer, so that a
            # later absolute import has somewhere to go under this one.
            animation_data.nla_tracks.new().name = "Base"

    action = bpy.data.actions.new(anim_name)
    animation_data.action = action
    if additive:
        action["jba_additive"] = True

    # Keyframes go into the active action first; keep NLA's keyframe
    # remapping out of it while they're inserted.
    saved_blend = (animation_data.action_blend_type, animation_data.action_influence)
    animation_data.action_blend_type = 'REPLACE'
    animation_data.action_influence = 1.0

    for anim_frame in range(jba.num_frames):
        for anim_bone in jba.bones:
            bone_name = getattr(anim_bone, "name", "")

            if bone_name in ob.pose.bones:
                pose_bone = ob.pose.bones[bone_name]

                # Rest in the file's own bone axes (see top of file)
                correction = _rest_correction(pose_bone.bone)
                mat_rest = _file_rest_local(pose_bone)
                mat_rot = anim_bone.rotations[anim_frame].to_matrix().to_4x4()
                # num_frames samples span length * fps frames, so they're
                # (num_frames - 1) intervals apart: sample i lands on frame
                # i + 1. (import_jba_64.py divides by num_frames, squeezing
                # every clip by one interval onto fractional frames.)
                frame = (jba.fps * jba.length * anim_frame / (jba.num_frames - 1) + 1
                         if jba.num_frames > 1 else 1)

                if additive:
                    # Offsets on top of the rest pose (see top of file): no
                    # Bip01 flip correction (the rest is used as it is), and
                    # the translation is the offset itself, not a delta from
                    # translation_base.
                    mat_add = (morpheme_space_inv @ Matrix.Translation(anim_bone.translations[anim_frame])
                               @ mat_rot @ morpheme_space)
                    mat_final = (Matrix.Translation(mat_rest.to_translation() + mat_add.to_translation())
                                 @ (mat_rest.to_3x3() @ mat_add.to_3x3()).to_4x4())
                    pose_bone.matrix_basis = correction.inverted() @ (mat_rest.inverted() @ mat_final) @ correction
                    pose_bone.keyframe_insert(data_path="location", frame=frame)
                    if not (operator.delete_180 and bone_name in KNOWN_180_FLIP_BONES):
                        pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                    continue

                if bone_name in KNOWN_180_FLIP_BONES:
                    mat_rest = mat_rest @ BONE_REST_FLIP_CORRECTION

                # anim_bone.translations[frame] is an ABSOLUTE bone-to-parent
                # offset as authored on whatever rig this specific clip was
                # captured against - not necessarily this armature's own
                # offset for the same bone (confirmed on a real
                # shared-across-body-types clip: several static bones were
                # off by 15-35% against the target skeleton's actual
                # proportions, visibly pulling joints out of place). Using
                # it directly is only safe when it happens to match.
                #
                # What's reliable is the DELTA from the file's own
                # translation_base, since that's this bone's actual motion
                # within the clip regardless of which rig authored it. So
                # every bone's translation is built the same way: take this
                # armature's own rest-pose offset and add only that delta on
                # top, converted through morpheme_space like everything
                # else. A bone with no real motion (delta == 0, whether
                # because it never had a per-frame track at all or because
                # it has one that happens to be constant) lands exactly on
                # this armature's own rest translation; a bone that really
                # animates (e.g. Root) keeps that motion, applied relative
                # to this armature's own bind pose instead of the file's.
                #
                # This also covers what ignore_facial_bones needs (ignore
                # this bone's translation animation entirely) as the delta
                # being forced to zero, rather than needing a separate
                # translation composition for that case.
                if operator.ignore_facial_bones and bone_name.lower().startswith("fc_"):
                    delta = Vector((0.0, 0.0, 0.0))
                else:
                    delta = anim_bone.translations[anim_frame] - anim_bone.translation_base

                mat_bone = morpheme_space_inv @ Matrix.Translation(delta) @ mat_rot @ morpheme_space
                mat_bone.translation = mat_rest.to_translation() + mat_bone.translation

                pose_bone.matrix_basis = correction.inverted() @ (mat_rest.inverted() @ mat_bone) @ correction
                pose_bone.keyframe_insert(data_path="location", frame=frame)
                if not (operator.delete_180 and bone_name in KNOWN_180_FLIP_BONES):
                    pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)

    # delete_180 is a distinct, deliberate feature from the rest-pose
    # correction above (which is always applied and just makes an
    # unanimated Bip01 come out as identity instead of a spurious 180°
    # flip, the same as every other bone). This is for the separate case
    # where a clip's Bip01 rotation is genuinely animated with a real ~180°
    # character-facing turn (SWTOR does this so gameplay animations play
    # with the character facing away from the camera), which some users
    # want stripped out entirely - e.g. it can wreck cloth/physics sims
    # otherwise. When enabled, this discards all of that bone's rotation
    # data and forces it to identity for the whole clip.
    if operator.delete_180:
        for bone_name in KNOWN_180_FLIP_BONES:
            if bone_name in ob.pose.bones:
                ob.pose.bones[bone_name].rotation_quaternion = (1, 0, 0, 0)

    animation_data.action_blend_type, animation_data.action_influence = saved_blend

    if as_nla_track and additive:
        track = animation_data.nla_tracks.new()  # on top of everything
        track.name = anim_name
        _move_active_action_to_nla(animation_data, track, 'COMBINE')
    elif as_nla_track:
        _move_active_action_to_nla(animation_data, _new_base_track(animation_data, anim_name), 'REPLACE')

    return True


def load(operator, context, filepath=""):
    # type: (Operator, Context, str) -> bool
    from bpy_extras.wm_utils.progress_report import ProgressReport

    with ProgressReport(context.window_manager) as progress:
        progress.enter_substeps(3, f"Importing \'{filepath}\' ...")

        progress.step("Parsing file ...", 1)
        animation = read(operator, context, filepath)

        if animation:
            progress.step("Done, building ...", 2)

            if build(operator, context, filepath, animation):
                progress.leave_substeps(f"Done, finished importing: \'{filepath}\'")
                return True

        return False

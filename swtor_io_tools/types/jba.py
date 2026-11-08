# <pep8 compliant>

"""
Reads SWTOR animation (.jba) files, both 32-bit (before Game Update 7.2.1) and
64-bit (Game Update 7.2.1 onwards). Independent of Blender: it needs only a path
(and mathutils).

read_jba() tells the two apart by the file's first 4 bytes. 32-bit files always
hold their bones' names. 64-bit files rarely do: their names then have to be
looked up elsewhere (see types/mph.py).

64-bit .jba files hold the same compressed keyframe data as the 32-bit ones, so
reading them differs only in the file's surrounding structure:

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
  64-bit string table layout (see read_string_table_64() in types/mph.py).

https://github.com/SWTOR-Slicers/WikiPedia/wiki/JBA-File-Structure
"""

import math
import os
from typing import List, Optional

from mathutils import Quaternion, Vector

from ..utils.binary import ArrayBuffer, DataView
from ..utils.number import align
from ..utils.string import readCString
from .mph import read_string_table_64

# The first 4 bytes of the file
JBA_32_MAGIC = 0
JBA_64_MAGIC = 0x4257414A  # 'JAWB'
JBA_64_PREFIX_SIZE = 8     # magic + version

# Counts above these mean a corrupt or misread file, not real data.
MAX_BONES = 4096
MAX_BLOCKS = 65536
MAX_FRAMES = 1000000


class JBAError(Exception):
    """A .jba file that is corrupt or not supported. The message is meant for the user."""


class JointBoneAnimation:

    class Block:

        num_frames: int
        size: int
        start_frame: int

        def __init__(self, start_frame, size):
            # type: (int, int) -> None
            self.size = size
            self.start_frame = start_frame

    class Bone:

        name: Optional[str]  # None until known (64-bit files rarely hold their bones' names)
        rotation_base: Vector
        rotation_stride: Vector
        rotations: List[Quaternion]
        translation_base: Vector
        translation_stride: Vector
        translations: List[Vector]

        def __init__(self, rotation_base, rotation_stride, translation_base, translation_stride):
            # type: (Vector, Vector, Vector, Vector) -> None
            self.name = None
            self.rotation_base = rotation_base
            self.rotation_stride = rotation_stride
            self.translation_base = translation_base
            self.translation_stride = translation_stride

    class WorldSpace:

        rotations: List[Quaternion]
        translations: List[Vector]

        def __init__(self, rotations, translations):
            # type: (List[Quaternion], List[Vector]) -> None
            self.rotations = rotations
            self.translations = translations

    bones: List[Bone]
    fps: int
    length: int
    num_frames: int
    world_space: WorldSpace

    def __init__(self, length, fps, num_frames, bones, world_space):
        # type: (int, int, int, List[Bone], WorldSpace) -> None
        self.bones = bones
        self.fps = fps
        self.length = length
        self.num_frames = num_frames
        self.world_space = world_space


def read_rotation_compressed(dv, pos, base, stride):
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


def read_translation_compressed(dv, pos, base, stride):
    # type: (DataView, int, Vector, Vector) -> Vector
    val = dv.getUint32(pos, True)
    pos += 4

    pos_x = base.x + (val >> 21) * stride.x
    pos_y = base.y + ((val >> 10) & 2047) * stride.y
    pos_z = base.z + (val & 1023) * stride.z

    return Vector((pos_x, pos_y, pos_z))


def check_header(filepath, num_bones, num_blocks, num_frames):
    # type: (str, int, int, int) -> None
    if not (0 < num_bones <= MAX_BONES and 0 < num_blocks <= MAX_BLOCKS and 0 < num_frames <= MAX_FRAMES):
        raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected header values).")


def check_blocks(filepath, blocks, num_frames):
    # type: (str, List[JointBoneAnimation.Block], int) -> None
    for block in blocks:
        if block.num_frames < 1 or block.start_frame + block.num_frames > num_frames:
            raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected block layout).")


# ----------------------------------------------------------------------------
# 32-bit files
# ----------------------------------------------------------------------------

def read_32(filepath):
    # type: (str) -> JointBoneAnimation
    """Raises JBAError if the file is corrupt or not supported."""
    with open(filepath, 'rb') as file:
        buffer = ArrayBuffer()
        buffer.fromfile(file, os.path.getsize(filepath))

    dv = DataView(buffer)
    pos = 0

    # Cancel import if this is not a BioWare Austin / SWTOR JBA file
    if dv.byteLength < 4 or dv.getUint32(pos, True) != JBA_32_MAGIC:
        raise JBAError(f"\'{filepath}\' is not a valid SWTOR jba file.")
    pos += 4

    # NOTE: File header
    length = dv.getFloat32(pos, True)
    pos += 4
    fps = dv.getFloat32(pos, True)
    pos += 4
    num_blocks = dv.getUint32(pos, True)
    pos += 4
    pos += 8  # unknown
    num_bones = dv.getUint32(pos, True)
    pos += 4
    pos += 12  # unknown

    # NOTE: Block headers
    num_frames = round(length * fps) + 1
    check_header(filepath, num_bones, num_blocks, num_frames)
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
    pos += num_blocks * 4  # unknown
    check_blocks(filepath, blocks, num_frames)

    # NOTE: Bone data
    pos = (pos + 127) & -128
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
    pos = (pos + 127) & -128
    for i in range(num_blocks):
        block = blocks[i]
        block_end = pos + block.size

        num_block_bones = dv.getUint32(pos, True)
        pos += 4
        if num_block_bones != num_bones:
            raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected bone count in block {i}).")
        pos += 4  # unknown

        # Keyframe layout
        has_translations = [None] * num_block_bones
        for j in range(num_block_bones):
            # num_rotations = dv.getUint32(pos, True)
            pos += 4
            pos += 4  # unknown
            num_translations = dv.getUint32(pos, True)
            pos += 4
            pos += 4  # unknown
            has_translations[j] = num_translations > 0

        # Keyframes
        for j in range(num_bones):
            bone = bones[j]

            # Rotations
            for k in range(block.num_frames):
                bone.rotations[block.start_frame + k] = read_rotation_compressed(
                    dv, pos, bone.rotation_base, bone.rotation_stride)
                pos += 6

            # Translations
            pos = (pos + 3) & -4
            if has_translations[j]:
                for k in range(block.num_frames):
                    bone.translations[block.start_frame + k] = read_translation_compressed(
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

        pos = block_end

    # World space
    pos += 4  # unknown
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
    num_rotations = dv.getUint32(pos, True)
    pos += 4
    if num_rotations != num_frames:
        raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected world space frame count).")
    pos += 4  # unknown
    # num_translations = dv.getUint32(pos, True)
    pos += 4
    # assert(num_translations == num_faces)
    pos += 4  # unknown

    rotations = [read_rotation_compressed(dv, pos + (i * 6), rotation_base, rotation_stride)
                 for i in range(num_frames)]
    pos += num_frames * 6

    pos = (pos + 3) & -4

    translations = [read_translation_compressed(dv, pos + (i * 4), translation_base, translation_stride)
                    for i in range(num_frames)]
    pos += num_frames * 4

    world_space = JointBoneAnimation.WorldSpace(rotations, translations)

    # Bone names
    names_start = pos
    num_names = dv.getUint32(pos, True)
    pos += 4
    pos += 4  # unknown
    # off_indices = dv.getUint32(pos, True)
    pos += 4
    # off_offsets = dv.getUint32(pos, True)
    pos += 4
    off_names = dv.getUint32(pos, True)
    pos += 4
    pos += num_names * 4  # numbers from 0 to num_names - 1
    name_offsets = [dv.getUint32(pos + (i * 4), True) for i in range(num_names)]
    pos += num_names * 4
    bone_names = [readCString(dv, names_start + off_names + name_offsets[i]) for i in range(num_names)]

    for i, name in enumerate(bone_names):
        bones[i].name = name

    return JointBoneAnimation(length, fps, num_frames, bones, world_space)


# ----------------------------------------------------------------------------
# 64-bit files
# ----------------------------------------------------------------------------

def read_64(filepath):
    # type: (str) -> JointBoneAnimation
    """Raises JBAError if the file is corrupt or not supported."""
    with open(filepath, 'rb') as file:
        buffer = ArrayBuffer()
        buffer.fromfile(file, os.path.getsize(filepath))

    dv = DataView(buffer)
    pos = 0

    # Cancel import if this is not a 64-bit BioWare Austin / SWTOR JBA file
    if dv.byteLength < 4 or dv.getUint32(pos, True) != JBA_64_MAGIC:
        raise JBAError(f"\'{filepath}\' is not a valid 64-bit SWTOR jba file.")
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

    num_frames = round(length * fps) + 1
    check_header(filepath, num_bones, num_blocks, num_frames)

    # NOTE: Block headers
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

    check_blocks(filepath, blocks, num_frames)

    # NOTE: Bone data
    pos = align(pos, 128, JBA_64_PREFIX_SIZE)
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
    pos = align(pos, 128, JBA_64_PREFIX_SIZE)
    for i in range(num_blocks):
        block = blocks[i]

        num_block_bones = dv.getUint32(pos, True)
        pos += 8
        if num_block_bones != num_bones:
            raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected bone count in block {i}).")
        pos += 8  # unknown

        # Keyframe layout
        has_translations = [None] * num_block_bones
        for j in range(num_block_bones):
            num_rotations = dv.getUint32(pos, True)
            pos += 8
            pos += 8  # unknown
            num_translations = dv.getUint32(pos, True)
            pos += 8
            pos += 8  # unknown
            if num_rotations != block.num_frames or num_translations not in (0, block.num_frames):
                raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected keyframe counts in block {i}).")
            has_translations[j] = num_translations > 0

        # Keyframes
        for j in range(num_bones):
            bone = bones[j]

            # Rotations
            for k in range(block.num_frames):
                bone.rotations[block.start_frame + k] = read_rotation_compressed(
                    dv, pos, bone.rotation_base, bone.rotation_stride)
                pos += 6

            # Translations
            pos = (pos + 3) & -4
            if has_translations[j]:
                for k in range(block.num_frames):
                    bone.translations[block.start_frame + k] = read_translation_compressed(
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
        pos = align(pos, 128, JBA_64_PREFIX_SIZE)

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
    if num_rotations != num_frames:
        raise JBAError(f"\'{filepath}\' is corrupt or not supported (unexpected world space frame count).")
    pos += 8  # unknown
    # num_translations = dv.getUint32(pos, True)
    pos += 8
    pos += 8  # unknown

    rotations = [read_rotation_compressed(dv, pos + (i * 6), rotation_base, rotation_stride)
                 for i in range(num_frames)]
    pos += num_frames * 6

    pos = (pos + 3) & -4

    translations = [read_translation_compressed(dv, pos + (i * 4), translation_base, translation_stride)
                    for i in range(num_frames)]
    pos += num_frames * 4

    world_space = JointBoneAnimation.WorldSpace(rotations, translations)

    # Bone names, only in the few files that have a table of them at the end
    if pos + 32 <= dv.byteLength and dv.getUint32(pos, True) == num_bones:
        bone_names_by_id = read_string_table_64(dv, pos)
        bone_names = [bone_names_by_id.get(i) for i in range(num_bones)]
        if None not in bone_names:
            for bone, name in zip(bones, bone_names):
                bone.name = name

    return JointBoneAnimation(length, fps, num_frames, bones, world_space)


def read_jba(filepath):
    # type: (str) -> JointBoneAnimation
    """
    Reads a 32-bit or a 64-bit .jba file. The bones' names are None if the file
    doesn't hold them. Raises JBAError if the file is corrupt or not supported.
    """
    with open(filepath, 'rb') as file:
        magic = file.read(4)

    magic = int.from_bytes(magic, 'little') if len(magic) == 4 else None
    if magic == JBA_64_MAGIC:
        return read_64(filepath)
    if magic == JBA_32_MAGIC:
        return read_32(filepath)

    raise JBAError(f"\'{filepath}\' is not a valid SWTOR jba file.")

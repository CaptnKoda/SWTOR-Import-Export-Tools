# <pep8 compliant>

"""
"Biblically Accurate .GR2" (armatures) -- imports a SWTOR skeleton (.gr2) as
a Blender Armature, keeping every piece of bone data the file holds.

Skeleton (armature) import only: .gr2 mesh import is kept separate.

Usage:
File > Import > Biblically Accurate .GR2, then pick one or more skeleton
.gr2 files (e.g. bmnnew_skeleton.gr2). Works with 32-bit (version 4) and
64-bit (version 5, Game Update 7.2.1 onwards) files.


What a GR2 skeleton holds, per bone
-----------------------------------
v4: 136 bytes, v5: 144 bytes

    name           u32 (v4) / u64 (v5) offset to a NUL-terminated string
    parent_index   i32, -1 for the root
    (v5 only)      u32, unknown (0 in every file seen so far)
    bone_to_parent 4x4 float32, column-major: the bone's LOCAL rest
                   transform relative to its parent
    root_to_bone   4x4 float32, column-major: the inverse bind matrix
                   (model space -> bone space) that meshes skin against

The two matrices are redundant, and they agree to float32 precision except
for one constant: inverse(root_to_bone) == RotY(180°) @ chain(bone_to_parent).
That 180° turn lives above the root bone (GrannyRootBone's bone_to_parent is
the identity, while its root_to_bone is a 180° Y turn). Meshes are skinned in
root_to_bone's space, so this importer uses inverse(root_to_bone) as each
bone's rest matrix. Because the 180° is the same for every bone, every bone's
parent-relative rest still equals its bone_to_parent exactly.


How this differs from ops/import_gr2.py
---------------------------------------
• Limb-aligned bones. SWTOR (3ds Max Biped) bones run along their local X
  axis, while a Blender bone always points along its local Y, so with the
  file's axes as they are, bones point sideways from their limbs. Every
  bone's rest matrix is therefore the file's turned by the same exact 90°
  about its own Z axis (LIMB_AXIS_REMAP):
      Blender X = -file Y,  Blender Y = file X,  Blender Z = file Z
  Head positions and rest-pose deformation are unchanged; only the axes'
  names differ. Bones point down their limbs as plain Stick bones, and
  Blender's X-Mirror works on 26 of bmnnew's 28 Left/Right pairs (1 of 28
  with the file's axes). The 90° turn is stored on every bone
  ('gr2_rest_correction') and the armature is flagged
  'gr2_bone_orientation' = 'LIMB'.
  .jba animations are authored against the file's axes: import them with
  ops/import_jba_koda.py ("Biblically Accurate .JBA"), which undoes the turn
  for the maths and reapplies it to the result. The other .jba importers
  pose these armatures wrongly.

• Each bone's rest matrix is set directly with EditBone.matrix, so its head
  position and all three local axes (including roll) are exactly the
  (turned) file's.

• Bones get real lengths: a body bone reaches its child along its own axis
  (see _limb_lengths()); attachment, effect, face and root bones are short
  Stick bones that read as dots. A bone's length doesn't affect deformation
  or animation; only head and orientation do.

• Bones whose rest transform is a mirror image (det = -1, e.g. bmnnew's
  vfx_hip_left / vfx_knee_left / vfx_elbow_left) can't be represented by a
  Blender bone, which is always a proper rotation. They get the nearest
  proper rotation (one axis flipped back) and the flipped axis is recorded in
  their 'gr2_mirror_axis' custom property.

• Every raw value is kept as custom properties for round-tripping and for
  other tools: per bone 'gr2_index', 'gr2_name' (only when Blender had to rename
  a duplicate bone name), 'gr2_parent_index', 'gr2_bone_to_parent',
  'gr2_root_to_bone' (both 16 floats, file order) and, for v5, 'gr2_unknown';
  per object 'gr2_version', 'gr2_type_flag', 'gr2_header_bounds', plus the
  same 'import_scale' / 'import_axis_conversion' / 'gr2_attachment_bones'
  properties that ops/import_gr2.py writes (the .jba importers read
  'import_scale').

• Bones are sorted into bone collections following the hand-sorted rigs in
  the community's FKArmatures.blend (Primary / Secondary / Face / Weapon /
  Technical, plus VFX for the vfx_* effect bones) for easier selection.
  Bip01 sits in Technical. This is cosmetic only.

• Scale Armature is baked into the bone data (head positions only, never
  orientations), so .jba animations scale correctly. ops/import_gr2.py
  leaves it at the object level, which makes the .jba importers scale
  translations twice.

• After the bones are built, any roll error Blender introduced is measured
  and corrected (see _refine_rolls()). Measured on all 361 skeletons in
  art/dynamic/spec: median error 4e-7, worst 2e-4 rad (0.012°). The worst
  cases are a few vfx bones that point almost exactly opposite their parent's
  Y axis, where Blender snaps the stored direction; no roll or tail
  adjustment can represent them more precisely.
"""

import json
import math
import os
import struct
from math import pi as PI
from typing import List, Optional, Set

import bpy
from bpy import app
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, StringProperty
from bpy.types import Context, Operator, OperatorFileListElement
from mathutils import Matrix, Vector

from ..types.shared import job_results  # add-on-wide global-like dict


BLENDER_VERSION = app.version

GR2_MAGIC = 0x42574147  # 'GAWB'
GR2_TYPE_SKELETON = 2

FACE_PREFIX = "fc_"

# Dot-like bones' length, as a fraction of the skeleton's size. Short enough
# that Stick bones read as dots at normal zoom levels, but not shorter:
# Blender derives a bone's direction from its head and tail, so very short
# bones lose precision. Measured over all 361 skeletons in art/dynamic/spec:
# 0.0005 and above are as accurate as long bones, while 0.00005 (about the
# 1e-5 units ops/import_gr2.py uses) is 10x worse.
STICK_LENGTH_FACTOR = 0.001

# Limb-aligned bone orientation: every bone's rest matrix is the file's,
# right-multiplied by this fixed 90° rotation (about the bone's own Z), so
# that the file's local X axis -- the one SWTOR / 3ds Max Biped bones run
# along -- becomes Blender's bone (Y) axis:
#     Blender X = -file Y,  Blender Y = file X,  Blender Z = file Z
# A proper rotation made of exact 0/±1 entries: nothing is approximated.
LIMB_AXIS_REMAP = Matrix(((0.0, 1.0, 0.0, 0.0),
                          (-1.0, 0.0, 0.0, 0.0),
                          (0.0, 0.0, 1.0, 0.0),
                          (0.0, 0.0, 0.0, 1.0)))

# A body bone reaches its child when the child lies within this angle of the
# bone's own (remapped) axis, else it's a dot.
LIMB_MAX_CHILD_ANGLE = 60.0

# Bone collections: name -> theme color palette
# The same collections, in the same order, as the community's hand-sorted
# FKArmatures.blend rigs (bfanew ... bmsnew, protocol, dog, tauntaun).
BONE_COLLECTIONS = {
    "Primary": 'THEME04',     # main body bones: spine, neck, head, limbs, fingers, toes
    "Secondary": 'THEME03',   # twist / blend / ulna helper bones
    "Face": 'THEME02',        # fc_* (except fc_wrinkle*)
    "Weapon": 'THEME01',      # socket_*, Left/RightWeapon
    "Technical": 'THEME10',   # Bip01, roots, skirt, camera, nameplate, attach_*, fc_wrinkle*
    "VFX": 'THEME06',         # vfx_* effect bones
}

# Bones that get sticks; the rest are dots.
STICK_COLLECTIONS = {"Primary", "Secondary"}

SECONDARY_KEYWORDS = ("Twist", "BlendShoulder", "Ulna")
WEAPON_NAMES = {"LeftWeapon", "RightWeapon"}
TECHNICAL_NAMES = {"Bip01", "GrannyRootBone", "RootSpine", "Skirt1", "Skirt2", "Camera", "FxRoot", "NamePlate"}
TECHNICAL_PREFIXES = ("attach_", "fc_wrinkle")
VFX_PREFIX = "vfx_"


class GR2Bone:
    __slots__ = ("index", "name", "parent_index", "unknown", "bone_to_parent", "root_to_bone")

    def __init__(self, index, name, parent_index, unknown, bone_to_parent, root_to_bone):
        self.index = index
        self.name = name
        self.parent_index = parent_index
        self.unknown = unknown                # v5 only, else None
        self.bone_to_parent = bone_to_parent  # 16 floats, file (column-major) order
        self.root_to_bone = root_to_bone      # 16 floats, file (column-major) order


class GR2Skeleton:
    __slots__ = ("version", "type_flag", "header_bounds", "bones", "num_meshes", "num_attachments")

    def __init__(self):
        self.version = 0
        self.type_flag = 0
        self.header_bounds = []
        self.bones = []  # type: List[GR2Bone]
        self.num_meshes = 0
        self.num_attachments = 0


class ImportGR2ArmatureKoda(Operator):
    """
    Import a SWTOR skeleton (.gr2) as an Armature with
    limb-aligned bones, keeping all of the file's bone data
    """
    bl_idname = "import_armature.gr2_armature_koda"
    bl_description = ("Import SWTOR skeleton (.gr2) files as Armatures, keeping all of the file's bone data:\n"
                      "names, hierarchy and exact rest positions and orientations.\n\n"
                      "• Bones point down their limbs (each bone's axes are turned 90° so that\n"
                      "   SWTOR's bone axis, X, is Blender's, Y), and X-Mirror posing works.\n\n"
                      "• Import animations with Biblically Accurate .JBA: the other .jba\n"
                      "   importers don't account for the turned axes.\n\n"
                      "• Compatible with both SWTOR 32 and 64-bit files.\n\n"
                      "• Can import selections of multiple files at once")
    bl_label = "Import Biblically Accurate .GR2"
    bl_options = {'UNDO'}

    # File Browser properties (see ImportGR2 for why this doesn't use ImportHelper)

    filepath: StringProperty(subtype='FILE_PATH')

    directory: StringProperty(subtype='DIR_PATH')

    filename_ext = ".gr2"

    files: CollectionProperty(
        name="File Path",
        description="File path used for importing the GR2 file",
        type=OperatorFileListElement,
    )

    filter_glob: StringProperty(
        default="*.gr2",
        options={'HIDDEN'},
    )

    # Importing parameters' properties

    use_bone_collections: BoolProperty(
        name="Bone Collections",
        description="Sort bones into colored bone collections\n(Primary, Secondary, Face, Weapon, Technical, VFX)",
        default=True,
    )

    apply_axis_conversion: BoolProperty(
        name="Axis Conversion",
        description="Permanently converts the imported armature\nto Blender's 'Z-is-Up' coordinate system\n"
                    "by 'applying' a x=90º rotation.\n\nNOT RECOMMENDED FOR MODDING SWTOR OR IMPORTING ANIMATIONS.\n\n"
                    "SWTOR's coordinate system is 'Y-is-up' vs. Blender's 'Z-is-up'.\n"
                    "To compensate for that in a reversible manner, this importer\n"
                    "normally sets the object's rotation to X=90º at the Object level.\n\n"
                    "Bone orientations relative to their parents are unaffected either way",
        default=False,
    )

    scale_object: BoolProperty(
        name="Scale Armature",
        description="Scales the imported armature's bones (baked into the armature data,\n"
                    "so that .jba animations scale correctly too).\n\n"
                    "SWTOR characters are about 0.2 units tall, while Blender defaults to meters.\n"
                    "Objects to be bound to this skeleton must be scaled by the same factor",
        default=False,
    )

    scale_factor: FloatProperty(
        name="Scale factor",
        description="Recommended values are:\n\n- 10 for simplicity (characters are superhero-like tall, over 2 m.).\n"
                    "- Around 8 for accuracy (characters show more realistic heights)",
        min=1.0,
        max=100.0,
        soft_min=1.0,
        soft_max=10.0,
        step=10,
        precision=2,
        default=1.0,
    )

    def invoke(self, context, event):
        addon = context.preferences.addons.get("swtor_io_tools")
        prefs = addon.preferences if addon else None
        if prefs:
            self.apply_axis_conversion = getattr(prefs, "gr2_apply_axis_conversion", False)
            self.scale_object = getattr(prefs, "gr2_scale_object", False)
            self.scale_factor = getattr(prefs, "gr2_scale_factor", 1.0)

        if not self.filepath:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "use_bone_collections")
        layout.prop(self, "apply_axis_conversion")
        layout.prop(self, "scale_object")
        row = layout.row()
        row.enabled = self.scale_object
        row.prop(self, "scale_factor")

    def execute(self, context):
        # type: (Context) -> Set[str]
        job_results['objs_names'] = []
        job_results['files_objs_names'] = {}
        job_results['job_origin'] = self.bl_idname

        paths = [os.path.join(self.directory, file.name) for file in self.files
                 if file.name.lower().endswith(self.filename_ext)]
        if not paths:
            paths.append(self.filepath)

        # Clear filebrowser-related properties now that they've been read
        # (see ImportGR2.execute()).
        self.files.clear()
        self.filepath = ""

        imported = []
        for path in paths:
            ob = load(self, context, path)
            if ob is None:
                return {'CANCELLED'}
            imported.append(ob)

        # Leave the last imported armature active, so that a .jba import can
        # follow straight away.
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        for ob in context.view_layer.objects:
            ob.select_set(False)
        for ob in imported:
            ob.select_set(True)
        context.view_layer.objects.active = imported[-1]

        try:
            context.scene.swtor_io_last_job = json.dumps(job_results)
        except AttributeError:
            pass

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _read_c_string(data, pos):
    # type: (bytes, int) -> str
    end = data.find(b"\0", pos)
    if end < 0:
        end = len(data)
    return data[pos:end].decode("latin-1")


def read(operator, filepath):
    # type: (Operator, str) -> Optional[GR2Skeleton]
    with open(filepath, "rb") as file:
        data = file.read()

    if len(data) < 0x78 or struct.unpack_from("<I", data, 0)[0] != GR2_MAGIC:
        operator.report({'ERROR'}, f"'{filepath}' is not a valid SWTOR .gr2 file.")
        return None

    skel = GR2Skeleton()
    skel.version = struct.unpack_from("<I", data, 0x04)[0]
    skel.type_flag = struct.unpack_from("<I", data, 0x14)[0]
    skel.num_meshes, _num_materials, num_bones, skel.num_attachments = struct.unpack_from("<4H", data, 0x18)
    skel.header_bounds = list(struct.unpack_from("<12f", data, 0x20))

    if skel.version == 5:
        offset_bones = struct.unpack_from("<Q", data, 0x68)[0]
        bone_size = 144
    elif skel.version == 4:
        offset_bones = struct.unpack_from("<I", data, 0x5C)[0]
        bone_size = 136
    else:
        operator.report({'ERROR'}, f"'{filepath}': unsupported .gr2 version {skel.version}.")
        return None

    if num_bones == 0:
        operator.report({'ERROR'}, f"'{filepath}' has no skeleton bones. Use the regular "
                                   "SWTOR .gr2 importer for objects without skeletons.")
        return None

    if offset_bones + num_bones * bone_size > len(data):
        operator.report({'ERROR'}, f"'{filepath}': bone table runs past the end of the file.")
        return None

    for i in range(num_bones):
        pos = offset_bones + i * bone_size
        if skel.version == 5:
            name_offset = struct.unpack_from("<Q", data, pos)[0]
            parent_index, unknown = struct.unpack_from("<iI", data, pos + 8)
            pos += 16
        else:
            name_offset = struct.unpack_from("<I", data, pos)[0]
            parent_index = struct.unpack_from("<i", data, pos + 4)[0]
            unknown = None
            pos += 8

        bone_to_parent = list(struct.unpack_from("<16f", data, pos))
        root_to_bone = list(struct.unpack_from("<16f", data, pos + 64))

        skel.bones.append(GR2Bone(i, _read_c_string(data, name_offset), parent_index, unknown,
                                  bone_to_parent, root_to_bone))

    # Sanity checks: every parent must come before its child (that's how
    # Granny orders bones, and what building the hierarchy relies on).
    for bone in skel.bones:
        if bone.parent_index >= bone.index or bone.parent_index < -1:
            operator.report({'ERROR'}, f"'{filepath}': bone '{bone.name}' has an invalid parent index "
                                       f"{bone.parent_index}.")
            return None

    return skel


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _matrix_from_column_major(values):
    # type: (List[float]) -> Matrix
    """GR2 stores 4x4 matrices column-major (translation in values 12-14)."""
    return Matrix([values[0:4], values[4:8], values[8:12], values[12:16]]).transposed()


def _proper_rotation(matrix, reference):
    # type: (Matrix, Matrix) -> (Matrix, Optional[str])
    """
    Returns matrix unchanged if its rotation is proper (det > 0). For a
    mirror image (det < 0), flips back whichever single local axis brings it
    closest to reference (normally the parent's rest orientation), and
    returns that axis' name too.
    """
    if matrix.to_3x3().determinant() > 0:
        return matrix, None

    best = None
    for axis, flip in (('X', (-1, 1, 1, 1)), ('Y', (1, -1, 1, 1)), ('Z', (1, 1, -1, 1))):
        candidate = matrix @ Matrix.Diagonal(flip)
        rot = candidate.to_3x3().normalized()
        angle = (reference.to_3x3().normalized().inverted() @ rot).to_quaternion().angle
        if best is None or angle < best[0]:
            best = (angle, candidate, axis)
    return best[1], best[2]


def _import_conversion(operator):
    # type: (ImportGR2ArmatureKoda) -> (Matrix, float)
    """
    The rotation (axis conversion, if applied to the data) and the uniform
    scale that are baked into the bone data. Both are applied to the rest
    matrices BEFORE the bones are created, never afterwards with
    transform_apply(): that would send every bone through Blender's Edit
    Mode -> bone conversion again, undoing _refine_rolls()' corrections.
    """
    rotation = Matrix.Rotation(PI * 0.5, 4, 'X') if operator.apply_axis_conversion else Matrix.Identity(4)
    scale = operator.scale_factor if operator.scale_object else 1.0
    return rotation, scale


def _convert(matrix, rotation, scale):
    # type: (Matrix, Matrix, float) -> Matrix
    """Rotates a rest matrix and scales its translation only (bones have no rest scale)."""
    converted = rotation @ matrix
    converted.translation = (rotation @ matrix).translation * scale
    return converted


def _bone_collection_names(skel):
    # type: (GR2Skeleton) -> List[str]
    """
    Each bone's collection, following FKArmatures.blend's hand-sorted rigs.
    Anything not recognised (creature bones such as Jaw, Tail01, LeftEar)
    is Primary, as on those rigs' dog and tauntaun.
    """
    names = {bone.name for bone in skel.bones}
    result = []
    for bone in skel.bones:
        name = bone.name
        if name.startswith(VFX_PREFIX):
            group = "VFX"
        elif name in TECHNICAL_NAMES or name.startswith(TECHNICAL_PREFIXES):
            group = "Technical"
        elif name == "Root":
            # Humanoids: Technical, next to RootSpine. Creatures without a
            # RootSpine (dog, tauntaun): Root is their main body bone.
            group = "Technical" if "RootSpine" in names else "Primary"
        elif name.startswith(FACE_PREFIX):
            group = "Face"
        elif name.startswith("socket_") or name in WEAPON_NAMES:
            group = "Weapon"
        elif any(keyword in name for keyword in SECONDARY_KEYWORDS):
            group = "Secondary"
        else:
            group = "Primary"
        result.append(group)
    return result


def _limb_lengths(skel, rest):
    # type: (GR2Skeleton, List[Matrix]) -> List[float]
    """
    Bone lengths. Bones can't be turned toward their children
    (that would change their orientation), so each body bone is made as long
    as its main child's distance ALONG its own axis: its tail lands on the
    child when the child lies on that axis (most limb, spine and finger
    bones: within 0.6° on bmnnew) and as near to it as possible otherwise.
    Body bones whose child lies too far off their axis, attachments, effects,
    face and root bones get dot-sized Stick bones.
    """
    num = len(skel.bones)
    children = [[] for _ in range(num)]
    subtree = [1] * num
    for bone in skel.bones:
        if bone.parent_index >= 0:
            children[bone.parent_index].append(bone.index)
    for i in reversed(range(num)):
        if skel.bones[i].parent_index >= 0:
            subtree[skel.bones[i].parent_index] += subtree[i]

    heads = [m.to_translation() for m in rest]
    overall = max((h - heads[0]).length for h in heads) or 1.0
    dot = overall * STICK_LENGTH_FACTOR
    min_cos = math.cos(math.radians(LIMB_MAX_CHILD_ANGLE))

    groups = _bone_collection_names(skel)
    lengths = [dot] * num
    has_stick = [False] * num
    for i, bone in enumerate(skel.bones):
        if groups[i] not in STICK_COLLECTIONS:
            continue
        axis = (rest[i].to_3x3() @ Vector((0.0, 1.0, 0.0))).normalized()
        main = [c for c in children[i] if groups[c] == "Primary"
                and (heads[c] - heads[i]).length > dot]
        if main:
            target = max(main, key=lambda c: (subtree[c], (heads[c] - heads[i]).length))
            offset = heads[target] - heads[i]
            along = offset.dot(axis)
            if along >= offset.length * min_cos:
                lengths[i] = along
                has_stick[i] = True
        elif bone.parent_index >= 0 and has_stick[bone.parent_index] and not children[i]:
            # Leaf (fingertip, toe, twist bone): half its parent, within reason.
            lengths[i] = min(lengths[bone.parent_index] * 0.5, overall * 0.03)
            has_stick[i] = True
    return [max(length, dot) for length in lengths]


def _refine_rolls(armature, blender_names, rest, iterations=3, tolerance=1e-7):
    # type: (bpy.types.Armature, List[str], List[Matrix], int, float) -> None
    """
    Corrects the small roll errors Blender's own Edit Mode -> bone data
    conversion leaves behind. Must be called in Object Mode.

    WHY: Blender stores each bone as a direction plus a roll RELATIVE TO ITS
    PARENT, and rebuilds the bone's matrix from them when leaving Edit Mode.
    That rebuild is ill-conditioned when a bone points (nearly) opposite its
    parent's Y axis: float32 noise in the head/tail positions turns into a
    twist around the bone's own axis, up to ~0.25° on some SWTOR creature
    rigs (e.g. greebeastterror's hands). Direction and head are unaffected,
    so the error is purely a twist, which can be measured on the finished
    bones and fed back into the edit bones' roll.
    """
    y_axis = 'Y'
    for _ in range(iterations):
        corrections = {}
        for blender_name, matrix in zip(blender_names, rest):
            actual = armature.bones[blender_name].matrix_local.to_3x3()
            difference = (actual.inverted() @ matrix.to_3x3().normalized()).to_quaternion()
            # to_swing_twist() keeps the tiny angles that to_axis_angle()
            # rounds down to zero in float32.
            twist = difference.to_swing_twist(y_axis)[1]
            if abs(twist) > tolerance:
                corrections[blender_name] = twist
        if not corrections:
            return
        bpy.ops.object.mode_set(mode='EDIT')
        for blender_name, twist in corrections.items():
            armature.edit_bones[blender_name].roll += twist
        bpy.ops.object.mode_set(mode='OBJECT')


def build(operator, context, filepath, skel):
    # type: (ImportGR2ArmatureKoda, Context, str, GR2Skeleton) -> (bpy.types.Object, List[str])

    name = os.path.splitext(os.path.basename(filepath))[0]

    # Rest matrices: inverse bind matrices, inverted (see top of file)
    rest = []  # type: List[Matrix]
    mirror_axes = []  # type: List[Optional[str]]
    for bone in skel.bones:
        matrix = _matrix_from_column_major(bone.root_to_bone).inverted_safe()
        reference = rest[bone.parent_index] if bone.parent_index >= 0 else Matrix.Identity(4)
        matrix, axis = _proper_rotation(matrix, reference)
        rest.append(matrix)
        mirror_axes.append(axis)

    rotation, scale = _import_conversion(operator)
    rest = [_convert(matrix, rotation, scale) @ LIMB_AXIS_REMAP for matrix in rest]
    lengths = _limb_lengths(skel, rest)

    # Create the armature object
    if context.view_layer.objects.active and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='OBJECT')

    armature = bpy.data.armatures.new(name)
    armature.display_type = 'STICK'
    ob = bpy.data.objects.new(name, armature)
    ob.show_in_front = True
    context.collection.objects.link(ob)

    for other in context.view_layer.objects:
        other.select_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob

    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = []
    for bone, matrix, length in zip(skel.bones, rest, lengths):
        eb = armature.edit_bones.new(bone.name)
        eb.head = (0.0, 0.0, 0.0)
        eb.tail = (0.0, length, 0.0)
        # Sets head, tail direction and roll at once, from the full matrix:
        # the bone's local axes end up exactly the rest matrix's axes.
        eb.matrix = matrix
        eb.use_connect = False
        edit_bones.append(eb)

    for bone, eb in zip(skel.bones, edit_bones):
        if bone.parent_index >= 0:
            eb.parent = edit_bones[bone.parent_index]

    # Some skeletons repeat a bone name (e.g. two 'Root' bones), which Blender
    # makes unique ('Root.001'), so from here on bones are looked up by the
    # name Blender actually gave them.
    blender_names = [eb.name for eb in edit_bones]

    bpy.ops.object.mode_set(mode='OBJECT')

    _refine_rolls(armature, blender_names, rest)

    # Per-bone data
    for bone, axis, blender_name in zip(skel.bones, mirror_axes, blender_names):
        b = armature.bones[blender_name]
        if blender_name != bone.name:
            b["gr2_name"] = bone.name
        b["gr2_index"] = bone.index
        b["gr2_parent_index"] = bone.parent_index
        b["gr2_bone_to_parent"] = bone.bone_to_parent
        b["gr2_root_to_bone"] = bone.root_to_bone
        if bone.unknown is not None:
            b["gr2_unknown"] = bone.unknown
        if axis:
            b["gr2_mirror_axis"] = axis
        # rest = file rest @ this (3x3, row by row), for tools such as
        # ops/import_jba_koda.py to convert between the two
        b["gr2_rest_correction"] = [v for row in LIMB_AXIS_REMAP.to_3x3() for v in row]

    # Bone collections (Blender 4.0+)
    if operator.use_bone_collections and hasattr(armature, "collections"):
        collections = {}
        for collection_name, palette in BONE_COLLECTIONS.items():
            collections[collection_name] = armature.collections.new(collection_name)
        for collection_name, blender_name in zip(_bone_collection_names(skel), blender_names):
            b = armature.bones[blender_name]
            collections[collection_name].assign(b)
            b.color.palette = BONE_COLLECTIONS[collection_name]
        for collection_name in list(collections):
            if not collections[collection_name].bones:
                armature.collections.remove(collections[collection_name])

    # File-level data (same keys as ops/import_gr2.py where they overlap)
    ob["gr2_version"] = skel.version
    ob["gr2_type_flag"] = skel.type_flag
    ob["gr2_header_bounds"] = skel.header_bounds
    ob["gr2_attachment_bones"] = json.dumps([])
    ob["gr2_import_method"] = "biblically_accurate"
    ob["gr2_bone_orientation"] = 'LIMB'

    # SWTOR is Y-up, Blender is Z-up: same object-level conversion as
    # ops/import_gr2.py, unless Axis Conversion already baked it into the
    # bones (see _import_conversion()).
    #
    # Scale is always baked into the bones, never left at the object level:
    # the .jba importers divide translations by 'import_scale' on the
    # assumption that the bone data itself is scaled. An object-level scale
    # gets the animation's translations scaled twice (measured: joints off
    # by up to the character's own height at a scale factor of 10).
    if not operator.apply_axis_conversion:
        ob.matrix_local = Matrix.Rotation(PI * 0.5, 4, 'X')
    ob["import_scale"] = scale
    ob["import_axis_conversion"] = operator.apply_axis_conversion

    return ob, blender_names


def _report_accuracy(operator, skel, ob, blender_names, mirror_count):
    # type: (ImportGR2ArmatureKoda, GR2Skeleton, bpy.types.Object, List[str], int) -> str
    """Checks the built armature against the file's own matrices (scaled/rotated as imported)."""
    rotation, scale = _import_conversion(operator)
    correction = LIMB_AXIS_REMAP
    bones = ob.data.bones
    max_rest_error = 0.0
    max_local_error = 0.0
    for bone, blender_name in zip(skel.bones, blender_names):
        b = bones[blender_name]
        if "gr2_mirror_axis" in b:
            continue
        expected = _convert(_matrix_from_column_major(bone.root_to_bone).inverted_safe(), rotation, scale)
        expected = expected @ correction
        actual = b.matrix_local
        max_rest_error = max(max_rest_error, max(abs(actual[r][c] - expected[r][c])
                                                 for r in range(4) for c in range(4)))
        if b.parent and "gr2_mirror_axis" not in b.parent:
            expected_local = _matrix_from_column_major(bone.bone_to_parent)
            expected_local.translation = expected_local.translation * scale
            expected_local = correction.inverted() @ expected_local @ correction
            actual_local = b.parent.matrix_local.inverted_safe() @ b.matrix_local
            max_local_error = max(max_local_error, max(abs(actual_local[r][c] - expected_local[r][c])
                                                       for r in range(4) for c in range(4)))
    return (f"{len(skel.bones)} bones, max rest-matrix error {max_rest_error:.2e}, "
            f"max parent-relative error {max_local_error:.2e}"
            + (f", {mirror_count} mirrored bone(s) un-mirrored" if mirror_count else ""))


def load(operator, context, filepath=""):
    # type: (ImportGR2ArmatureKoda, Context, str) -> Optional[bpy.types.Object]
    skel = read(operator, filepath)
    if skel is None:
        return None

    if skel.type_flag != GR2_TYPE_SKELETON:
        operator.report({'WARNING'}, f"'{os.path.basename(filepath)}' isn't a skeleton file (type "
                                     f"{skel.type_flag}); importing its {len(skel.bones)} bones only.")

    try:
        ob, blender_names = build(operator, context, filepath, skel)
    except Exception as error:  # soft-fail: report, don't raise
        import traceback
        traceback.print_exc()
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        operator.report({'ERROR'}, f"'{os.path.basename(filepath)}': armature creation failed ({error}).")
        return None

    mirror_count = sum(1 for b in ob.data.bones if "gr2_mirror_axis" in b)
    summary = _report_accuracy(operator, skel, ob, blender_names, mirror_count)
    renamed = sum(1 for bone, name in zip(skel.bones, blender_names) if bone.name != name)
    if renamed:
        summary += f", {renamed} duplicate bone name(s) made unique (originals in 'gr2_name')"
    print(f"Biblically Accurate .GR2: '{filepath}': {summary}")
    operator.report({'INFO'}, f"Imported '{ob.name}': {summary}")

    job_results['objs_names'].append(ob.name)
    job_results['files_objs_names'][filepath] = [ob.name]
    return ob

# <pep8 compliant>

"""
Builds the Blender Armature for a SWTOR skeleton (.gr2), keeping the bone
data the file holds. Called by ops/import_gr2.py's build() for skeleton
files; reading the file is done by types/gr2.py (Granny2.Bone).

The limb-aligned bone building, the roll refinement and the bone collections
were contributed by Koda.


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
root_to_bone's space, so inverse(root_to_bone) is used as each bone's rest
matrix. Because the 180° is the same for every bone, every bone's
parent-relative rest still equals its bone_to_parent exactly.


How the bones are built
-----------------------
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
  .jba animations are authored against the file's axes: ops/import_jba.py
  undoes the turn for the math and reapplies it to the result.

• Each bone's rest matrix is set directly with EditBone.matrix, so its head
  position and all three local axes (including roll) are exactly the
  (turned) file's.

• Bones get real lengths: a body bone reaches its child along its own axis
  (see _limb_lengths()); attachment, effect, face and engine bones are short
  Stick bones that read as dots. A bone's length doesn't affect deformation
  or animation; only head and orientation do.

• Bones whose rest transform is a mirror image (det = -1, e.g. bmnnew's
  vfx_hip_left / vfx_knee_left / vfx_elbow_left) can't be represented by a
  Blender bone, which is always a proper rotation. They get the nearest
  proper rotation (one axis flipped back) and the flipped axis is recorded in
  their 'gr2_mirror_axis' custom property.

• Bones are sorted into bone collections (Primary / Secondary / Face /
  Weapon / VFX / Engine) for easier selection, by bone name; see
  BONE_COLLECTIONS and _bone_collection_names(). The collections hold the
  bones and their colors, nothing else is stored on the bones. They also
  decide which bones get stick-like lengths (STICK_COLLECTIONS).

• Scale Object is baked into the bone data (head positions only, never
  orientations), so .jba animations scale correctly. Axis Conversion, when
  asked for, is baked in the same way. Both are applied to the rest matrices
  BEFORE the bones are created, never afterwards with transform_apply():
  that would send every bone through Blender's Edit Mode -> bone conversion
  again, undoing _refine_rolls()' corrections.

• After the bones are built, any roll error Blender introduced is measured
  and corrected (see _refine_rolls()). Measured on all 361 skeletons in
  art/dynamic/spec: median error 4e-7, worst 2e-4 rad (0.012°). The worst
  cases are a few vfx bones that point almost exactly opposite their parent's
  Y axis, where Blender snaps the stored direction; no roll or tail
  adjustment can represent them more precisely.


Custom properties
-----------------
Per bone: 'gr2_index', 'gr2_parent_index', 'gr2_root_to_bone' (16 floats,
file order, the exact matrix the file holds), 'gr2_rest_correction' (9 floats,
row by row) and, only when they apply, 'gr2_name' (Blender had to rename a
duplicate bone name) and 'gr2_mirror_axis'.
Per object: 'gr2_type_flag', 'gr2_attachment_bones', 'gr2_bnry_summary' (if
any), 'gr2_bone_orientation', 'import_scale' and 'import_axis_conversion'
(the .jba importer reads 'import_scale').
From the skeleton's character specification (art/dynamic/spec/*.dyc/.dat,
see types/spec.py and _apply_character_spec()), when one is found:
'swtor_spec', 'swtor_anim_folder' and 'swtor_anim_metadata' (the rig's
animation folder and action list, resources-relative) and
'swtor_resources_root'. The .jba importer uses them to find the rig's
animation data. The spec's bone masks become bone collections
("Mask: upperbody").


Data deliberately NOT kept
--------------------------
'gr2_bone_to_parent' and 'gr2_unknown' (the v5-only u32) are not stored.
types/gr2.py doesn't read them (see Granny2.Bone), nothing in the add-on
uses them, and they aren't needed:
  • bone_to_parent is redundant (see above), so it can be rebuilt from
    root_to_bone and the parent hierarchy, with the 180° Y turn above the
    root.
  • the unknown u32 was 0 on every bone of bmnnew_skeleton.gr2 (v5, 140
    bones) and greebeastterror_skeleton.gr2 (v5, 131 bones); it can be
    written back as 0.

Look at them again if:
  • A skeleton exporter is written that must reproduce the file's bone
    table byte for byte. bone_to_parent rebuilt from root_to_bone differs
    from the stored one by float32 noise (about 1e-6), so exact round-trips
    would need the stored matrices (read them in Granny2.Bone: the
    bone_to_parent line is there, commented out).
  • A v5 file turns up with a non-zero unknown value, which would mean it
    holds something real (then find out what it is before writing files).
  • A skeleton where inverse(root_to_bone) stops matching
    RotY(180°) @ chain(bone_to_parent) (e.g. after a game update). The rest
    pose source used here would have to be reconsidered.
"""

import json
import math
import os
import traceback
from math import pi as PI
from typing import List, Optional

import bpy
from mathutils import Matrix, Vector


FACE_PREFIX = "fc_"

# Dot-like bones' length, as a fraction of the skeleton's size. Short enough
# that Stick bones read as dots at normal zoom levels, but not shorter:
# Blender derives a bone's direction from its head and tail, so very short
# bones lose precision. Measured over all 361 skeletons in art/dynamic/spec:
# 0.0005 and above are as accurate as long bones, while 0.00005 (about the
# 1e-5 units the old importer used) is 10x worse.
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

# Bone collections: name -> theme color palette, in the order they are created
BONE_COLLECTIONS = {
    "Primary": 'THEME04',     # main body bones: spine, neck, head, limbs, fingers, toes
    "Secondary": 'THEME03',   # twist / blend / ulna helpers, skirt bones, Root / RootSpine
    "Face": 'THEME02',        # fc_* (except fc_wrinkle*)
    "Weapon": 'THEME01',      # socket_*, Left/RightWeapon
    "VFX": 'THEME06',         # vfx_* effect bones, FxRoot
    "Engine": 'THEME10',      # bones the game itself uses: rig roots, camera, nameplate, portrait, wrinkle maps
}

# Bones that get sticks; the rest are dots.
STICK_COLLECTIONS = {"Primary", "Secondary"}

SECONDARY_KEYWORDS = ("Twist", "BlendShoulder", "Ulna")
# Root / RootSpine sit exactly on the pelvis (not body parts), parent the whole
# body and carry no skin weights (checked on the bmn archetype body meshes).
SECONDARY_NAMES = {"Root", "RootSpine", "Skirt1", "Skirt2"}
WEAPON_NAMES = {"LeftWeapon", "RightWeapon"}
ENGINE_NAMES = {"GrannyRootBone", "Master", "Bip01", "Camera", "NamePlate"}
ENGINE_PREFIXES = ("attach_", "fc_wrinkle")
VFX_PREFIX = "vfx_"
VFX_NAMES = {"FxRoot"}


def _matrix_from_column_major(values):
    # type: (List[float]) -> Matrix
    """GR2 stores 4x4 matrices column-major (translation in values 12-14)."""
    return Matrix([values[0:4], values[4:8], values[8:12], values[12:16]]).transposed()


def _proper_rotation(matrix, reference):
    # type: (Matrix, Matrix) -> tuple
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


def _import_conversion(apply_axis_conversion, scale_object, scale_factor):
    # type: (bool, bool, float) -> tuple
    """
    The rotation (axis conversion, if applied to the data) and the uniform
    scale that are baked into the bone data. See the module docstring.
    """
    rotation = Matrix.Rotation(PI * 0.5, 4, 'X') if apply_axis_conversion else Matrix.Identity(4)
    scale = scale_factor if scale_object else 1.0
    return rotation, scale


def _convert(matrix, rotation, scale):
    # type: (Matrix, Matrix, float) -> Matrix
    """Rotates a rest matrix and scales its translation only (bones have no rest scale)."""
    converted = rotation @ matrix
    converted.translation = (rotation @ matrix).translation * scale
    return converted


def _bone_collection_names(names):
    # type: (List[str]) -> List[str]
    """
    Each bone's collection, by name. The rules are the same for every
    skeleton (humanoids and creatures); anything not recognised (creature
    bones such as Jaw, Tail01, LeftEar) is Primary.
    """
    result = []
    for name in names:
        if name.startswith(VFX_PREFIX) or name in VFX_NAMES:
            group = "VFX"
        elif name in ENGINE_NAMES or name.startswith(ENGINE_PREFIXES):
            group = "Engine"
        elif name in SECONDARY_NAMES or any(keyword in name for keyword in SECONDARY_KEYWORDS):
            group = "Secondary"
        elif name.startswith(FACE_PREFIX):
            group = "Face"
        elif name.startswith("socket_") or name in WEAPON_NAMES:
            group = "Weapon"
        else:
            group = "Primary"
        result.append(group)
    return result


def _limb_lengths(names, parents, rest):
    # type: (List[str], List[int], List[Matrix]) -> List[float]
    """
    Bone lengths. Bones can't be turned toward their children
    (that would change their orientation), so each body bone is made as long
    as its main child's distance ALONG its own axis: its tail lands on the
    child when the child lies on that axis (most limb, spine and finger
    bones: within 0.6° on bmnnew) and as near to it as possible otherwise.
    Body bones whose child lies too far off their axis, attachments, effects,
    face and root bones get dot-sized Stick bones.
    """
    num = len(names)
    children = [[] for _ in range(num)]
    subtree = [1] * num
    for i, parent in enumerate(parents):
        if parent >= 0:
            children[parent].append(i)
    for i in reversed(range(num)):
        if parents[i] >= 0:
            subtree[parents[i]] += subtree[i]

    heads = [m.to_translation() for m in rest]
    overall = max((h - heads[0]).length for h in heads) or 1.0
    dot = overall * STICK_LENGTH_FACTOR
    min_cos = math.cos(math.radians(LIMB_MAX_CHILD_ANGLE))

    groups = _bone_collection_names(names)
    lengths = [dot] * num
    has_stick = [False] * num
    for i in range(num):
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
        elif parents[i] >= 0 and has_stick[parents[i]] and not children[i]:
            # Leaf (fingertip, toe, twist bone): half its parent, within reason.
            lengths[i] = min(lengths[parents[i]] * 0.5, overall * 0.03)
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


def _report(operator, level, message):
    # type: (Optional[bpy.types.Operator], str, str) -> None
    if operator is not None:
        operator.report({level}, message)
    else:
        print(f"{level}: {message}")


def _accuracy_summary(bones, ob, blender_names, rotation, scale, mirror_count):
    # type: (list, bpy.types.Object, List[str], Matrix, float, int) -> str
    """Checks the built armature against the file's own matrices (scaled/rotated as imported)."""
    max_rest_error = 0.0
    for bone, blender_name in zip(bones, blender_names):
        b = ob.data.bones[blender_name]
        if "gr2_mirror_axis" in b:
            continue
        expected = _convert(_matrix_from_column_major(bone.root_to_bone).inverted_safe(), rotation, scale)
        expected = expected @ LIMB_AXIS_REMAP
        actual = b.matrix_local
        max_rest_error = max(max_rest_error, max(abs(actual[r][c] - expected[r][c])
                                                 for r in range(4) for c in range(4)))
    summary = f"{len(bones)} bones, max rest-matrix error {max_rest_error:.2e}"
    if mirror_count:
        summary += f", {mirror_count} mirrored bone(s) un-mirrored"
    renamed = sum(1 for bone, name in zip(bones, blender_names) if bone.name != name)
    if renamed:
        summary += f", {renamed} duplicate bone name(s) made unique (originals in 'gr2_name')"
    return summary


def build_armature(gr2, filepath, scale_object=False, scale_factor=1.0, apply_axis_conversion=False, operator=None):
    # type: (object, str, bool, float, bool, Optional[bpy.types.Operator]) -> Optional[bpy.types.Object]
    """
    Builds the armature Object for a parsed skeleton file (a Granny2 whose
    bone_buffer holds the bones) and returns it, active and selected, or
    None (after reporting why) if it couldn't be built. Leaves Blender in
    Object Mode.
    """
    created = []  # (object, armature) pairs, to remove if building fails part-way
    try:
        return _build(gr2, filepath, scale_object, scale_factor, apply_axis_conversion, operator, created)
    except Exception as error:  # soft-fail: report, don't raise (the file is skipped)
        traceback.print_exc()
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        for ob, armature in created:
            if ob.name in bpy.data.objects:
                bpy.data.objects.remove(ob)
            if armature.name in bpy.data.armatures:
                bpy.data.armatures.remove(armature)
        _report(operator, 'ERROR', f"'{os.path.basename(filepath)}': armature creation failed ({error}).")
        return None


MASK_COLLECTION_PREFIX = "Mask: "


def _apply_character_spec(ob, armature, filepath, operator):
    # type: (bpy.types.Object, bpy.types.Armature, str, Optional[bpy.types.Operator]) -> None
    """
    Looks up the skeleton's character specification (types/spec.py: the
    .dyc/.dat files next to it in art/dynamic/spec, or in the Resources
    Directory's) and stores what the .jba importer needs on the armature:
    'swtor_spec', 'swtor_anim_folder' and 'swtor_anim_metadata' (both
    resources-relative, "/"-separated) and 'swtor_resources_root' (if known).
    The spec's bone masks become bone collections ("Mask: upperbody"). Nothing
    happens if there's no spec: the .jba importer then falls back to finding
    the rig's folder by name.
    """
    from ..types.spec import find_character_spec, spec_folders_for

    try:
        prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
        resources_dirs = [prefs.swtor_resources_dir]
    except (KeyError, AttributeError):
        resources_dirs = []
    try:
        spec = find_character_spec(os.path.basename(filepath), spec_folders_for(filepath, resources_dirs))
    except OSError:
        spec = None
    if spec is None:
        return

    ob["swtor_spec"] = spec.name
    if spec.anim_folder:
        ob["swtor_anim_folder"] = spec.anim_folder
    if spec.anim_metadata:
        ob["swtor_anim_metadata"] = spec.anim_metadata
    if spec.resources_root:
        ob["swtor_resources_root"] = spec.resources_root

    for mask_name, weights in spec.masks.items():
        bones = [armature.bones[name] for name, weight in weights.items() if weight > 0 and name in armature.bones]
        if not bones:
            continue
        collection = armature.collections.new(MASK_COLLECTION_PREFIX + mask_name)
        for bone in bones:
            collection.assign(bone)

    masks = ", ".join(spec.masks) or "none"
    _report(operator, 'INFO', f"'{ob.name}': character specification '{spec.name}' (animations in "
                              f"{spec.anim_folder or '?'}, bone masks: {masks}).")


def _build(gr2, filepath, scale_object, scale_factor, apply_axis_conversion, operator, created):
    # The bones, in file order, as the file stores them
    if sorted(gr2.bone_buffer) != list(range(len(gr2.bone_buffer))):
        raise ValueError("the bone indices aren't consecutive")
    bones = [gr2.bone_buffer[i] for i in range(len(gr2.bone_buffer))]
    names = [bone.name for bone in bones]
    parents = [bone.parent_index for bone in bones]

    # Granny orders bones parents-first, which building the hierarchy relies on.
    for i, parent in enumerate(parents):
        if parent < -1 or parent >= i:
            raise ValueError(f"bone '{names[i]}' has an invalid parent index {parent}")

    # Rest matrices: inverse bind matrices, inverted (see top of file)
    rest = []  # type: List[Matrix]
    mirror_axes = []  # type: List[Optional[str]]
    for bone in bones:
        matrix = _matrix_from_column_major(bone.root_to_bone).inverted_safe()
        reference = rest[bone.parent_index] if bone.parent_index >= 0 else Matrix.Identity(4)
        matrix, axis = _proper_rotation(matrix, reference)
        rest.append(matrix)
        mirror_axes.append(axis)

    rotation, scale = _import_conversion(apply_axis_conversion, scale_object, scale_factor)
    rest = [_convert(matrix, rotation, scale) @ LIMB_AXIS_REMAP for matrix in rest]
    lengths = _limb_lengths(names, parents, rest)

    # Create the armature object
    context = bpy.context
    if context.view_layer.objects.active and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='OBJECT')

    name = os.path.splitext(os.path.basename(filepath))[0]
    armature = bpy.data.armatures.new(name)
    armature.display_type = 'STICK'
    ob = bpy.data.objects.new(name, armature)
    ob.show_in_front = True
    created.append((ob, armature))
    context.collection.objects.link(ob)

    for other in context.view_layer.objects:
        other.select_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob

    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = []
    for bone_name, matrix, length in zip(names, rest, lengths):
        eb = armature.edit_bones.new(bone_name)
        eb.head = (0.0, 0.0, 0.0)
        eb.tail = (0.0, length, 0.0)
        # Sets head, tail direction and roll at once, from the full matrix:
        # the bone's local axes end up exactly the rest matrix's axes.
        eb.matrix = matrix
        eb.use_connect = False
        edit_bones.append(eb)

    for parent, eb in zip(parents, edit_bones):
        if parent >= 0:
            eb.parent = edit_bones[parent]

    # Some skeletons repeat a bone name (e.g. two 'Root' bones), which Blender
    # makes unique ('Root.001'), so from here on bones are looked up by the
    # name Blender actually gave them.
    blender_names = [eb.name for eb in edit_bones]

    bpy.ops.object.mode_set(mode='OBJECT')

    _refine_rolls(armature, blender_names, rest)

    # Per-bone data
    rest_correction = [v for row in LIMB_AXIS_REMAP.to_3x3() for v in row]
    for index, (bone, axis, blender_name) in enumerate(zip(bones, mirror_axes, blender_names)):
        b = armature.bones[blender_name]
        if blender_name != bone.name:
            b["gr2_name"] = bone.name
        b["gr2_index"] = index
        b["gr2_parent_index"] = bone.parent_index
        b["gr2_root_to_bone"] = list(bone.root_to_bone)
        if axis:
            b["gr2_mirror_axis"] = axis
        # rest = file rest @ this (3x3, row by row), for tools such as
        # ops/import_jba.py to convert between the two
        b["gr2_rest_correction"] = rest_correction

    # Bone collections
    collections = {}
    for collection_name in BONE_COLLECTIONS:
        collections[collection_name] = armature.collections.new(collection_name)
    for collection_name, blender_name in zip(_bone_collection_names(names), blender_names):
        b = armature.bones[blender_name]
        collections[collection_name].assign(b)
        b.color.palette = BONE_COLLECTIONS[collection_name]
    for collection in list(collections.values()):
        if not collection.bones:
            armature.collections.remove(collection)

    # File-level data (same keys as the mesh objects, where they overlap)
    ob["gr2_type_flag"] = gr2.type_flag
    ob["gr2_attachment_bones"] = json.dumps(gr2.attachment_bones)
    if gr2.bnry_summary is not None:
        ob["gr2_bnry_summary"] = json.dumps(gr2.bnry_summary)
    ob["gr2_bone_orientation"] = 'LIMB'

    # SWTOR is Y-up, Blender is Z-up: the conversion is an Object-level
    # rotation (reversible), unless Axis Conversion already baked it into the
    # bones (see _import_conversion()).
    #
    # Scale is always baked into the bones, never left at the object level:
    # the .jba importer divides translations by 'import_scale' on the
    # assumption that the bone data itself is scaled.
    if not apply_axis_conversion:
        ob.matrix_local = Matrix.Rotation(PI * 0.5, 4, 'X')
    ob["import_scale"] = scale
    ob["import_axis_conversion"] = bool(apply_axis_conversion)

    _apply_character_spec(ob, armature, filepath, operator)

    mirror_count = sum(1 for axis in mirror_axes if axis)
    summary = _accuracy_summary(bones, ob, blender_names, rotation, scale, mirror_count)
    print(f"ARMATURE: '{filepath}': {summary}")
    _report(operator, 'INFO', f"Imported '{ob.name}': {summary}")

    return ob

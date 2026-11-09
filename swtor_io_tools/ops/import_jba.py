# <pep8 compliant>

"""
This script imports Star Wars: The Old Republic animations into Blender.

Usage:
Run this script from "File->Import" menu and then load the desired JBA animation file.

Both 32-bit and 64-bit .jba files are supported (the format is told from the
file's first 4 bytes). Reading them is done in types/jba.py; this module finds
the bones' names when a file has none (see types/mph.py), and applies the
animation to the active skeleton.

TIMING
Keyframes are placed at their real time in the scene's frame rate: sample i is
at i / jba.fps seconds (SWTOR animations are 15 or 30 fps), whatever the
scene's rate is. This holds for the bones, the world motion and the NLA strips
alike (see build(): frames_per_sample).

LIMB-ALIGNED BONES
The bones of armatures imported by ops/import_gr2_armature.py have the file's
rest orientation turned by a fixed per-bone rotation C (stored, row by row, in
each bone's 'gr2_rest_correction' custom property): Blender rest = file rest @ C.
.jba data is authored against the file's own bone axes, so build() works out
each bone's file-axes rest (undoing C on the bone and on its parent), runs
the math below, and turns the resulting pose basis
into the bone's own axes: basis = C^-1 @ file-axes basis @ C.

Bones without 'gr2_rest_correction' use C = identity (see _rest_correction()).

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

NLA TRACKS (import option 'Import as NLA Track'; its default is the add-on preference jba_import_as_nla)
With it on, every import goes into its own NLA track and never replaces
what the armature is playing:
  • the armature's active action, if any, is first pushed down into an NLA
    track.
  • absolute animations go into a track under every additive layer.
  • additive animations go into a track on top of everything, with
    'Combine' blending, so they add to whatever plays below them.
With it off, every import (absolute or additive) is plain keyframes in a new
active action; an additive one then shows its
offsets on top of the rest pose.
Strips hold their first/last frame outside their range, so a one-pose face
expression applies to the whole base animation; strips can be moved,
repeated or faded (influence) in the NLA editor. Additive files are detected
by their 'ad_' name, with a warning if their data clearly disagrees (see
_additive_from_data()), or can be forced either way.

WORLD MOTION
Besides the bones, every .jba holds a world-space track: how the game moves
the whole character while the clip plays . In bmnnew, 130 of 7211 files actually move it: turns in
place (po_normal_to_180_cw ...), throne / Force jumps, mount and dismount,
death falls and get-ups. Without it, a turn snaps back to its starting
facing, and a jump never leaves the ground. It's keyed onto the armature
object's Delta Transforms (see _key_world_motion()); on dg_getup_side_1 it
brings the planted foot's slide from 0.0013 to 0.0001 a frame.

BROKEN FRAMES (import option 'Repair Broken Frames', off by default; its default is the add-on preference jba_repair_broken_frames)
Some clips (18 broken frames in 16 of bmnnew's normal animations, similar in
every humanoid body type; e.g. cd_great_forge_all frame 555, right arm) have
a frame where a "driver" bone, nearly always a shoulder or hip, spins a full
360° between the frame before and the frame after: an angle wrap in
BioWare's source data, interpolated the long way round. The middle frame
catches it halfway, 180° off. Its fingerprint is the stored quaternion sign:
the exporter keeps every track's sign continuous (consecutive frames
switch sign only twice in all of bmnnew), and a 360° turn changes q to -q,
so the neighbours of a broken frame are stored with opposite signs. The rest
of the limb follows from it: the wrist or ankle flips back to keep the hand
or foot in place in world space, and helper bones (Ulna, BlendShoulder,
twist and skirt bones) spike by about half. See _find_broken_frames() for the
exact test and _repair_broken_frames() for what changes.

Layering with NLA tracks, additive animations, world motion and broken-frame
repair were contributed by Koda, together with the support for limb-aligned
armatures.

https://github.com/SWTOR-Slicers/WikiPedia/wiki/JBA-File-Structure
"""

import math
import os
import re
from typing import List, Optional, Set

from bpy import app
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import Context, Object, Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector

from ..types.jba import JBAError, JointBoneAnimation, read_jba
from ..types.mph import BONES_FROM_DATA, BONES_FROM_LIBRARY, BONES_INFERRED, MPH_ANIM_LIBRARY_FILE, find_bone_names
from ..utils.string import path_split


NO_ARMATURE_MESSAGE = "No armature selected. Select the skeleton to animate."

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



class ImportJBA(Operator):
    """Import from SWTOR JBA file format (.jba), 32-bit or 64-bit"""
    bl_idname = "import_animation.jba"  # DO NOT CHANGE
    bl_description = "Import and apply an animation to the active SWTOR skeleton in the scene.\n\n• Compatible with .jba files extracted from SWTOR 32-bit (before Game Update 7.2.1)\n   and from SWTOR 64-bit (Game Update 7.2.1 onwards).\n\n• 64-bit .jba files rarely hold their bones' names. Those are read from the game's\n   animation data (.mph) files: either the ones next to the .jba file, or the ones in the\n   Resources Directory set in the Add-on's Preferences (anim/<category>/<rig>,\n   e.g. anim/humanoid/bmnnew, matched by the .jba's folder or the active skeleton's name).\n\n• Keyframes are placed at their real time in the scene's frame rate. SWTOR animations\n   are 15 or 30 fps: a 30 fps scene puts every keyframe on a whole frame.\n\n• Additive animations (the 'ad_' ones) are detected and imported as offsets on top of a pose.\n\n• The animation's world motion (turns in place, jumps, falls) is keyed onto the armature\n   object's Delta Transforms.\n\n• Works on skeletons imported with the Add-on's .gr2 importer.\n\n• Might apply exaggerated bone translations requiring cleanup\n   (check ZG SWTOR Tools Add-on's Clear Bones Translations tool)."
    bl_label = "Import SWTOR (.jba)"
    bl_options = {'UNDO'}


    # File Browser properties
    
    # This class used to be based on ImportHelper
    # but we now use invoke() to be able to use
    # the Add-on's Preferences settings when
    # called from the Import menu and launching
    # a File Browser.
    
    # filepath is explicitly declared because
    # omitting ImportHelper omits it, too.
    # invoke() handles what to do if it is
    # filled as a param in an external call.
    
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
             "Additive if the file's name starts with 'ad_' (SWTOR's naming for them).\n"
             "Warns if the file's data clearly disagrees with its name"),
            ('ABSOLUTE', "Absolute", "The animation holds full bone poses (most animations)"),
            ('ADDITIVE', "Additive",
             "The animation holds offsets added on top of a pose (e.g. bmnnew's 'ad_' facial\n"
             "expressions and aim offsets). Applied on top of the rest pose: the keyframes are\n"
             "pure offsets, which can be layered over another action in the NLA ('Combine' blending)"),
        ),
        default='AUTO',
    )

    ignore_facial_bones: BoolProperty(
        name="Ignore Face Bones",
        description="Ignores the data in the facial bones' translation keyframes\nand only uses their rotation keyframes.\n\nAbsolute animations only: in additive ones, facial translations\nare part of the expression (e.g. raised brows)",
        default=True,
    )

    delete_180: BoolProperty(
        name="Delete 180º Rotation",
        description="Keeps the animation data from turning the skeleton 180º by deleting\nthe keyframes assigned to the Bip01 bone and setting its rotation to zero.\n\nSWTOR animations turn characters so that they face away from the camera,\nas normally shown in the gameplay. That is not just a nuisance but a problem\nwhen adding cloth or physics simulations to capes or lekku: the instantaneous\nturn plays havok with them",
        default=False,
    )

    import_as_nla: BoolProperty(
        name="Import as NLA Track",
        description="Imports the animation into its own NLA track instead of replacing the\narmature's active action (any active action is kept, pushed down into an\nNLA track first). Additive animations (e.g. 'ad_' facial expressions) are\nlayered on top of the others with 'Combine' blending.\n\nOff: the animation is imported as plain keyframes in the active action",
        default=True,
    )

    repair_broken_frames: BoolProperty(
        name="Repair Broken Frames",
        description="Some SWTOR animations have single frames where a shoulder or hip spins a\nfull 360° between two frames (an error in the game's own data), twisting\nthe limb into a broken pose for that one frame. This finds those frames and\nreplaces the affected bones' rotations with the halfway point between the\nframes either side. Every other bone and frame is left exactly as stored, and\nthe repaired frames are listed after importing. Additive animations are never\nchanged.\n\nOff: the animation is imported exactly as stored",
        default=False,
    )

    set_scene_fps: BoolProperty(
        name="Set Scene FPS to 30",
        description="Sets the scene's frame rate to 30 fps.\n\nThe keyframes are placed at their real time in the scene's frame rate either way. "
                    "SWTOR animations are 15 or 30 fps, so at 30 fps every keyframe lands on a whole frame (a 15 fps "
                    "animation on every second one), while other rates, such as Blender's default of 24 fps, put them "
                    "between frames.\n\nThis changes the whole scene, including cloth simulations",
        default=False,
    )

    set_frame_range: BoolProperty(
        name="Set Frame Range to Animation",
        description="Sets the scene's Start and End frames to the animation's length",
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
        # To be able to set the class' properties to the values in the
        # Add-on's preferences and show them in the File Browser's options,
        # we use an Invoke function instead of directly using ImportHelper in
        # the class definition, to be able to put there the required code.
        
        prefs = context.preferences.addons["swtor_io_tools"].preferences
        
        self.ignore_facial_bones = prefs.jba_ignore_facial_bones
        self.delete_180          = prefs.jba_delete_180
        self.import_as_nla       = prefs.jba_import_as_nla
        self.repair_broken_frames = prefs.jba_repair_broken_frames
        self.scale_animation     = prefs.gr2_scale_object
        self.scale_factor        = prefs.gr2_scale_factor


        # Handling of filepath in case of being
        # filled as a param in an external call.
        if not self.filepath:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        else:
            return self.execute(context)        


    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
           
        # Rarely needed, so a collapsible sub-panel that starts collapsed.
        # body is None while the sub-panel is collapsed.
        header, body = layout.panel("SWTOR_IO_JBA_options", default_closed=False)
        header.label(text="Import Options")
        if body:
            body.prop(self, "animation_type")
            body.prop(self, "ignore_facial_bones")
            body.prop(self, "delete_180")
            body.prop(self, "import_as_nla")
            body.prop(self, "repair_broken_frames")
            body.prop(self, "set_scene_fps")
            body.prop(self, "set_frame_range")

        # Rarely needed, so a collapsible sub-panel that starts collapsed.
        # body is None while the sub-panel is collapsed.
        header, body = layout.panel("SWTOR_IO_JBA_scale", default_closed=True)
        header.label(text="Import Scale")
        if body:
            body.prop(self, "scale_animation")
            body.prop(self, "scale_factor")


    def execute(self, context):
        # type: (Context) -> Set[str]

        paths = [os.path.join(self.directory, file.name) for file in self.files if file.name.lower().endswith(self.filename_ext)]

        if not paths:
            paths.append(self.filepath)
            
        # Clear filebrowser-related properties now
        # that they have been read and have no more
        # use so that they don't persist if the class
        # breaks before finishing its execution
        # (it makes debugging difficult).
        self.files.clear()
        self.filepath = ""

        for path in paths:
            if not load(self, context, path):
                return {'CANCELLED'}

        return {'FINISHED'}


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


def _resolve_bone_names(operator, context, filepath, jba):
    # type: (Operator, Context, str, JointBoneAnimation) -> bool
    """Finds the bones' names if the file didn't have them. Returns False (after reporting why) if it can't."""
    anim_name = os.path.splitext(os.path.basename(filepath))[0]
    num_bones = len(jba.bones)

    if any(bone.name is None for bone in jba.bones):
        bone_names = None
        source = None
        num_layouts = 0
        rig_folders = _find_rig_folders(context, filepath)
        for rig_folder in rig_folders:
            bone_names, source, layouts = find_bone_names(rig_folder, anim_name, num_bones)
            num_layouts = max(num_layouts, layouts)
            if bone_names:
                break

        if not bone_names:
            folders = ", ".join(f"\'{folder}\'" for folder in rig_folders)
            if not rig_folders:
                operator.report({'ERROR'}, f"Bone names not found for \'{anim_name}\': the .jba has none and no animation "
                                           "data (.mph) files were located. Set the Resources Directory in the add-on "
                                           "preferences and select the skeleton, or import from the extracted "
                                           "anim/<category>/<rig> folder.")
            elif num_layouts > 1:
                operator.report({'ERROR'}, f"Bone names ambiguous for \'{anim_name}\': not listed in the animation data "
                                           f"(.mph) files in {folders}, which use {num_layouts} different bone orders for "
                                           f"{num_bones} bones. Not imported, to avoid animating the wrong bones.")
            else:
                operator.report({'ERROR'}, f"Bone names not found for \'{anim_name}\' ({num_bones} bones): not listed in "
                                           f"the animation data (.mph) files in {folders}. If the .jba was renamed, "
                                           "restore its original name. A 32-bit copy of the animation, if available, "
                                           "has its own bone names.")
            return False

        if source == BONES_INFERRED:
            operator.report({'WARNING'}, f"Bone names inferred for \'{anim_name}\': not listed in the animation data "
                                         f"(.mph) files, but they use only one bone order for {num_bones} bones. "
                                         "Verify the result.")
        else:
            origin = {BONES_FROM_LIBRARY: MPH_ANIM_LIBRARY_FILE,
                      BONES_FROM_DATA: "the animation data (.mph) files"}[source]
            operator.report({'INFO'}, f"Bone names read from {origin}.")

        for bone, name in zip(jba.bones, bone_names):
            bone.name = name

    # The skeleton's root bone is named Bip01, not GOD
    for bone in jba.bones:
        if bone.name == "GOD":
            bone.name = "Bip01"

    return True


def read(operator, context, filepath):
    # type: (Operator, Context, str) -> Optional[JointBoneAnimation]
    """Raises JBAError if the file is corrupt or not supported."""
    jba = read_jba(filepath)
    return jba if _resolve_bone_names(operator, context, filepath, jba) else None


def _file_rest_local(pose_bone):
    # type: (bpy.types.PoseBone) -> Matrix
    """A bone's rest relative to its parent, in the file's own bone axes (see top of file)."""
    mat_rest = pose_bone.bone.matrix_local @ _rest_correction(pose_bone.bone).inverted()
    if pose_bone.parent:
        parent_rest = pose_bone.parent.bone.matrix_local @ _rest_correction(pose_bone.parent.bone).inverted()
        mat_rest = parent_rest.inverted() @ mat_rest
    return mat_rest


def _is_additive_name(anim_name):
    # type: (str) -> bool
    """SWTOR's additive animations' names start with 'ad_'."""
    return anim_name.lower().startswith("ad_")


def _additive_from_data(jba, ob, morpheme_space_inv):
    # type: (JointBoneAnimation, Object, Matrix) -> Optional[bool]
    """
    Whether frame 0 of a .jba looks additive (True) or absolute (False), or
    None when its data doesn't say clearly. Additive files hold identity
    rotation and zero translation for every bone they don't move; absolute
    ones hold the skeleton's own rest pose, or near it. Two signals:

    • Rotations, over the bones whose rest rotation is clearly not identity:
      each votes additive if frame 0 is nearer identity than the rest
      rotation, absolute otherwise. Decisive with 3+ votes, 75% agreeing.
    • Translations, over the bones with a rest offset (5+ of them): median
      |frame 0 translation| / |rest offset| below 0.1 is additive, above 0.5
      absolute.

    Only an answer both signals give is returned. Each signal alone misjudges
    some rigs (measured on an 841-file sample over all 354 rigs with a
    skeleton): translations call some creature clips additive
    (greebeastterror, rakghoulboss: near-zero translations), and rotations
    call many placeables' door open/close clips additive (rotated rest
    poses, identity animation rotations). That's why the 'ad_' name decides,
    and this only backs a warning when the two disagree (see build()).
    """
    min_angle = math.radians(5.0)
    additive_votes = absolute_votes = 0
    ratios = []
    for anim_bone in jba.bones:
        name = getattr(anim_bone, "name", "")
        if name not in ob.pose.bones or not ob.pose.bones[name].parent:
            continue
        rest = _file_rest_local(ob.pose.bones[name])

        rest_rotation = rest.to_3x3().normalized()
        rotation = (morpheme_space_inv @ anim_bone.rotations[0].to_matrix().to_4x4()
                    @ morpheme_space_inv.inverted()).to_3x3()
        if rest_rotation.to_quaternion().angle > min_angle:
            from_identity = rotation.to_quaternion().angle
            from_rest = (rest_rotation.inverted() @ rotation).to_quaternion().angle
            if abs(from_identity - from_rest) > min_angle:
                if from_identity < from_rest:
                    additive_votes += 1
                else:
                    absolute_votes += 1

        rest_offset = rest.to_translation().length
        if rest_offset >= 1e-6:
            offset = (morpheme_space_inv @ Matrix.Translation(anim_bone.translations[0])).to_translation().length
            ratios.append(offset / rest_offset)

    by_rotation = None
    votes = additive_votes + absolute_votes
    if votes >= 3 and max(additive_votes, absolute_votes) >= 0.75 * votes:
        by_rotation = additive_votes > absolute_votes

    by_translation = None
    if len(ratios) >= 5:
        ratios.sort()
        median = ratios[len(ratios) // 2]
        if median < 0.1:
            by_translation = True
        elif median > 0.5:
            by_translation = False

    return by_rotation if by_rotation is not None and by_rotation == by_translation else None


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
    """
    A bone's limb-aligned rest correction C (Blender rest = file rest @ C), or identity.

    POSSIBLY POINTLESS: the identity fallback (for bones without
    'gr2_rest_correction', i.e. armatures imported before the .gr2 importer
    built limb-aligned bones) reproduces how this importer used to work, but
    animations may not import properly on those older armatures anyway. It can
    be removed (making the property required) if it turns out to be useless.
    """
    values = bone.get("gr2_rest_correction")
    if values is None or len(values) != 9:
        return Matrix.Identity(4)
    return Matrix((values[0:3], values[3:6], values[6:9])).to_4x4()


# ---------------------------------------------------------------------------
# Broken frames (see top of file)
# ---------------------------------------------------------------------------

# How much further (degrees) a frame must be from both neighbours than they
# are from each other to count as "out and back". Glitches score +40° or
# more, genuine fast turns 0° or less, so anywhere from 1° to 40° finds
# nearly the same frames (261-266 bone tracks over all humanoid bodies).
BROKEN_FRAME_MIN_EXCESS = 20.0
# A driver's steps just before and after the broken frame must be below
# this (degrees): genuine weapon twirls spin more than 90° every frame.
BROKEN_FRAME_MAX_CALM = 60.0


def _rotation_angle(a, b):
    # type: (Quaternion, Quaternion) -> float
    """The angle (degrees) of the rotation between two orientations, whatever their stored signs."""
    return math.degrees(2.0 * math.acos(min(1.0, abs(a.dot(b)))))


def _out_and_back(before, here, after):
    # type: (Quaternion, Quaternion, Quaternion) -> bool
    """Whether 'here' jumps away from both 'before' and 'after' and comes back, rather than moving onward."""
    return (min(_rotation_angle(before, here), _rotation_angle(here, after))
            - _rotation_angle(before, after)) > BROKEN_FRAME_MIN_EXCESS


def _calm_around(rotations, s):
    # type: (List[Quaternion], int) -> bool
    """Whether the bone moves calmly on the steps just before and just after frame s."""
    steps = []
    if s >= 2:
        steps.append(_rotation_angle(rotations[s - 2], rotations[s - 1]))
    if s + 2 < len(rotations):
        steps.append(_rotation_angle(rotations[s + 1], rotations[s + 2]))
    return bool(steps) and max(steps) < BROKEN_FRAME_MAX_CALM


def _find_broken_frames(jba):
    # type: (JointBoneAnimation) -> Set[int]
    """
    The (0-based) frames on which some bone, its driver, passes all three tests:
      1. its stored sign flips across the frame (the frames either side are
         in opposite hemispheres: a turn of more than 180° in two frames);
      2. the frame is out and back, not onward motion (rules out genuine
         fast body turns, which also flip sign);
      3. the steps around it are calm (rules out weapon twirls).
    """
    broken = set()
    for anim_bone in jba.bones:
        r = anim_bone.rotations
        for s in range(1, len(r) - 1):
            if r[s - 1].dot(r[s + 1]) < 0.0 and _out_and_back(r[s - 1], r[s], r[s + 1]) and _calm_around(r, s):
                broken.add(s)
    return broken


def _repair_broken_frames(jba, broken, ob, morpheme_space, morpheme_space_inv):
    # type: (JointBoneAnimation, Set[int], Object, Matrix, Matrix) -> dict
    """
    Repairs the broken frames in place, in world space (the file's axes),
    and returns {frame: [names of the bones whose rotation changed]}.

    In world space a broken frame shows what really went wrong: the driver
    and everything it carries (the rest of the limb, the helper bones) jump
    out and back, while the hand or foot at the end of the limb stays where
    it belongs (its own local flip only cancels the driver's). So on each
    broken frame, parents first:
      • a bone whose world orientation is out and back gets the halfway
        point between the frames either side;
      • a bone that is out and back only relative to its parent (the hand
        or foot cancelling the driver, or a helper bone on a fast-moving
        parent) gets whichever is closer to that halfway pose: its stored
        world orientation, or the halfway point of its own rotation;
      • every other bone keeps its own rotation and follows its parent.
    Only rotations that actually change are written: bones on other limbs,
    and all translations, are left exactly as stored.

    Bones the file has no track for (roots, sockets) use their rest from the
    armature; bones the armature doesn't have are never keyed anyway.
    """
    index = {anim_bone.name: j for j, anim_bone in enumerate(jba.bones) if anim_bone.name in ob.pose.bones}
    rest = {}

    def local(name, f):
        j = index.get(name)
        if j is not None:
            return jba.bones[j].rotations[f]
        if name not in rest:
            rest[name] = (morpheme_space @ _file_rest_local(ob.pose.bones[name])
                          @ morpheme_space_inv).to_quaternion()
        return rest[name]

    def parent_name(name):
        parent = ob.pose.bones[name].parent
        return parent.name if parent else None

    def world(name, f, cache):
        if name not in cache:
            parent = parent_name(name)
            cache[name] = (world(parent, f, cache) @ local(name, f)) if parent else local(name, f).copy()
        return cache[name]

    report = {}
    # One frame at a time, in order: the frame before is then already
    # repaired, and the frame after, if it's broken too, is so on another
    # limb (a bone isn't out and back between a good frame and a broken one).
    for s in sorted(broken):
        before_cache, old_cache, after_cache = {}, {}, {}
        new_world, new_locals = {}, {}

        def repaired_world(name):
            if name in new_world:
                return new_world[name]
            parent = parent_name(name)
            parent_world = repaired_world(parent) if parent else Quaternion()
            stored_local = local(name, s)
            # By default a bone keeps its own (stored, untouched) rotation
            # and follows its parent.
            target = parent_world @ stored_local
            if name in index:
                old = world(name, s, old_cache)
                before, after = world(name, s - 1, before_cache), world(name, s + 1, after_cache)
                if before.dot(after) < 0.0:
                    after = -after
                halfway = before.slerp(after, 0.5)
                local_before, local_after = local(name, s - 1), local(name, s + 1)
                if _out_and_back(before, old, after):
                    # The bone itself jumps out and back in world space: the
                    # driver and the rest of the limb it carries.
                    target = halfway
                    new_locals[name] = parent_world.inverted() @ target
                elif _out_and_back(local_before, stored_local, local_after):
                    # Out and back only relative to its parent: the end of a
                    # limb cancelling its driver's flip, or a helper bone
                    # whose jump is hidden in world space by a fast-moving
                    # parent (e.g. the torso turning 40°+ a frame). Take
                    # whichever is closer to the smooth halfway pose: its
                    # stored world orientation, or the halfway point of its
                    # own rotation.
                    if local_before.dot(local_after) < 0.0:
                        local_after = -local_after
                    candidate = parent_world @ local_before.slerp(local_after, 0.5)
                    target = candidate if _rotation_angle(candidate, halfway) < _rotation_angle(old, halfway) else old
                    new_locals[name] = parent_world.inverted() @ target
            new_world[name] = target
            return target

        for name in ob.pose.bones.keys():
            repaired_world(name)
        # Written once the whole frame is worked out, from stored data only.
        names = []
        for name, rotation in new_locals.items():
            if _rotation_angle(rotation, local(name, s)) > 0.01:
                names.append(name)
                jba.bones[index[name]].rotations[s] = rotation.normalized()
        if names:
            report[s] = names
    return report


# ---------------------------------------------------------------------------
# World motion
# ---------------------------------------------------------------------------

def _key_world_motion(operator, ob, jba, morpheme_space, morpheme_space_inv, frames_per_sample):
    # type: (Operator, Object, JointBoneAnimation, Matrix, Matrix, float) -> None
    """
    Keys the .jba's world-space track (the game's own "move the character"
    channel, separate from the bones: turns in place, throne/Force jumps,
    mounting, death falls ...) onto the armature object's Delta Transforms,
    one key per sample on the same frames as the bones (frames_per_sample: see build()). They're keyed for
    every animation (as identity when the track doesn't move), so switching
    actions or NLA strips never leaves a previous clip's motion behind, and
    the object's own Location / Rotation stay the user's.

    The track is converted the same way the bones are (through
    morpheme_space: Morpheme's Z-up, millimetre-like space into the
    skeleton's own space), then into the armature's space (the axis
    conversion, if it was applied to the bone data), and applied in that
    space on top of the object's current transform:
        object world = object basis @ world motion
    Blender composes Delta Transforms as location + delta_location and
    delta_rotation @ rotation, so for the object's current basis
    T(loc) R(q) S(s):
        delta_location = R(q) @ (s * motion translation)
        delta_rotation = q @ motion rotation @ q^-1
    """
    world_space = getattr(jba, "world_space", None)
    if world_space is None or not world_space.rotations:
        return

    axis = (Matrix.Rotation(math.pi * 0.5, 4, 'X') if ob.get("import_axis_conversion", False)
            else Matrix.Identity(4))
    loc, q, s = ob.matrix_basis.decompose()
    mode = ob.rotation_mode
    if mode == 'AXIS_ANGLE':
        operator.report({'WARNING'}, "Apply World Motion: the armature uses Axis Angle rotation, which has no "
                                     "delta rotation; only the world motion's translation was keyed.")

    num = min(len(world_space.rotations), len(world_space.translations), jba.num_frames)
    previous_q = None
    previous_e = None
    for i in range(num):
        frame = 1 + i * frames_per_sample
        motion = (morpheme_space_inv @ Matrix.Translation(world_space.translations[i])
                  @ world_space.rotations[i].to_matrix().to_4x4() @ morpheme_space)
        motion = axis @ motion @ axis.inverted()
        t = motion.to_translation()
        ob.delta_location = q @ Vector((t.x * s.x, t.y * s.y, t.z * s.z))
        ob.keyframe_insert(data_path="delta_location", frame=frame)

        dq = q @ motion.to_quaternion() @ q.inverted()
        if mode == 'QUATERNION':
            if previous_q is not None:
                dq.make_compatible(previous_q)
            previous_q = dq
            ob.delta_rotation_quaternion = dq
            ob.keyframe_insert(data_path="delta_rotation_quaternion", frame=frame)
        elif mode != 'AXIS_ANGLE':
            euler = dq.to_euler(mode, previous_e) if previous_e is not None else dq.to_euler(mode)
            previous_e = euler
            ob.delta_rotation_euler = euler
            ob.keyframe_insert(data_path="delta_rotation_euler", frame=frame)


def build(operator, context, filepath, jba):
    # type: (Operator, Context, str, JointBoneAnimation) -> bool
    import bpy
    # import os

    ob: Object = context.active_object

    if not ob or ob.type != 'ARMATURE':
        operator.report({'ERROR'}, NO_ARMATURE_MESSAGE)
        return False

    animated_bones = [b for b in jba.bones if b.name in ob.pose.bones]
    if not animated_bones:
        operator.report({'ERROR'}, f"None of the animation's {len(jba.bones)} bones exist on \'{ob.name}\'. "
                                   "Select the skeleton this animation is for.")
        return False

    # SWTOR animations are 15 or 30 fps. Sample i is at i / jba.fps seconds (a .jba's length is
    # (num_frames - 1) / fps): its keyframe goes to that time in the scene's frame rate, whatever it
    # is (so, a whole frame only if the frame rate is a multiple of the animation's).
    scene = context.scene
    if operator.set_scene_fps:
        scene.render.fps = 30
        scene.render.fps_base = 1.0
    scene_fps = scene.render.fps / scene.render.fps_base
    frames_per_sample = scene_fps / jba.fps if jba.fps > 0 else 1.0

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
        additive = _is_additive_name(anim_name)
        from_data = _additive_from_data(jba, ob, morpheme_space_inv)
        if from_data is not None and from_data != additive:
            operator.report({'WARNING'}, f"'{anim_name}' is named as {'an additive' if additive else 'an absolute'} "
                                         f"animation, but its data looks {'additive' if from_data else 'absolute'}. "
                                         "If it comes out wrong, set Animation Type by hand.")
    else:
        additive = operator.animation_type == 'ADDITIVE'
    operator.report({'INFO'}, f"'{anim_name}': {'additive' if additive else 'absolute'} animation")

    # Layering (see top of file)
    as_nla_track = operator.import_as_nla

    # Broken frames (see top of file). Additive clips are never touched:
    # some deliberately hold a different pose on every frame.
    if operator.repair_broken_frames and not additive:
        broken = _find_broken_frames(jba)
        repaired = _repair_broken_frames(jba, broken, ob, morpheme_space, morpheme_space_inv) if broken else {}
        if repaired:
            details = []
            for s in sorted(repaired):
                frame = 1 + s * frames_per_sample
                details.append(f"frame {round(frame, 2):g} ({', '.join(repaired[s])})")
            operator.report({'INFO'}, f"'{anim_name}': repaired {len(repaired)} broken "
                                      f"frame{'s' if len(repaired) > 1 else ''}: " + "; ".join(details))

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
                frame = 1 + anim_frame * frames_per_sample

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

    _key_world_motion(operator, ob, jba, morpheme_space, morpheme_space_inv, frames_per_sample)

    animation_data.action_blend_type, animation_data.action_influence = saved_blend

    if as_nla_track and additive:
        track = animation_data.nla_tracks.new()  # on top of everything
        track.name = anim_name
        _move_active_action_to_nla(animation_data, track, 'COMBINE')
    elif as_nla_track:
        _move_active_action_to_nla(animation_data, _new_base_track(animation_data, anim_name), 'REPLACE')

    if operator.set_frame_range:
        scene.frame_start = 1
        scene.frame_end = max(1, math.ceil(1 + (jba.num_frames - 1) * frames_per_sample - 1e-6))

    skipped = len(jba.bones) - len(animated_bones)
    if skipped:
        operator.report({'INFO'}, f"{skipped} of the animation's {len(jba.bones)} bones don't exist on "
                                  f"\'{ob.name}\' and were skipped.")

    return True


def load(operator, context, filepath=""):
    # type: (Operator, Context, str) -> bool
    from bpy_extras.wm_utils.progress_report import ProgressReport

    # Checked before reading anything: finding bone names can take a while
    ob = context.active_object
    if not ob or ob.type != 'ARMATURE':
        operator.report({'ERROR'}, NO_ARMATURE_MESSAGE)
        return False

    with ProgressReport(context.window_manager) as progress:
        progress.enter_substeps(3, f"Importing \'{filepath}\' ...")

        progress.step("Parsing file ...", 1)
        try:
            animation = read(operator, context, filepath)
        except JBAError as e:
            operator.report({'ERROR'}, str(e))
            return False
        except (IndexError, ValueError, OverflowError, OSError):
            operator.report({'ERROR'}, f"\'{filepath}\' is truncated, corrupt or not supported.")
            return False

        if animation:
            progress.step("Done, building ...", 2)

            if build(operator, context, filepath, animation):
                progress.leave_substeps(f"Done, finished importing: \'{filepath}\'")
                return True

        return False

# <pep8 compliant>

"""
This script imports Star Wars: The Old Republic animations into Blender.

Usage:
Run this script from "File->Import" menu and then load the desired JBA animation file.

Both 32-bit and 64-bit .jba files are supported (the format is told from the
file's first 4 bytes). Reading them is done in types/jba.py; this module finds
the bones' names when a file has none (see types/mph.py), and applies the
animation to the active skeleton.

https://github.com/SWTOR-Slicers/WikiPedia/wiki/JBA-File-Structure
"""

import math
import os
import re
from typing import List, Optional, Set

from bpy import app
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, StringProperty
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
    bl_description = "Import and apply an animation to the active SWTOR skeleton in the scene.\n\n• Compatible with .jba files extracted from SWTOR 32-bit (before Game Update 7.2.1)\n   and from SWTOR 64-bit (Game Update 7.2.1 onwards).\n\n• 64-bit .jba files rarely hold their bones' names. Those are read from the game's\n   animation data (.mph) files: either the ones next to the .jba file, or the ones in the\n   Resources Directory set in the Add-on's Preferences (anim/<category>/<rig>,\n   e.g. anim/humanoid/bmnnew, matched by the .jba's folder or the active skeleton's name).\n\n• Keyframes are placed at their real time in the scene's frame rate. SWTOR animations\n   are 15 or 30 fps: a 30 fps scene puts every keyframe on a whole frame.\n\n• Might apply exaggerated bone translations requiring cleanup\n   (check ZG SWTOR Tools Add-on's Clear Bones Translations tool)."
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
    
    ignore_facial_bones: BoolProperty(
        name="Ignore Face Bones",
        description="Ignores the data in the facial bones' translation keyframes\nand only uses their rotation keyframes",
        default=True,
    )

    delete_180: BoolProperty(
        name="Delete 180º Rotation",
        description="Keeps the animation data from turning the skeleton 180º by deleting\nthe keyframes assigned to the Bip01 bone and setting its rotation to zero.\n\nSWTOR animations turn characters so that they face away from the camera,\nas normally shown in the gameplay. That is not just a nuisance but a problem\nwhen adding cloth or physics simulations to capes or lekku: the instantaneous\nturn plays havok with them",
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
            body.prop(self, "ignore_facial_bones")
            body.prop(self, "delete_180")
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

    # SWTOR animations are 15 or 30 fps. Sample i is at i / jba.fps seconds: its keyframe goes
    # to that time in the scene's frame rate, whatever it is (so, a whole frame only if the
    # frame rate is a multiple of the animation's).
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

    action = bpy.data.actions.new(anim_name)
    ob.animation_data.action = action

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

    for anim_frame in range(jba.num_frames):
        for anim_bone in jba.bones:
            bone_name = getattr(anim_bone, "name", "")

            if bone_name in ob.pose.bones:
                pose_bone = ob.pose.bones[bone_name]
                mat_rest = pose_bone.bone.matrix_local

                if pose_bone.parent:
                    mat_rest = pose_bone.parent.bone.matrix_local.inverted() @ mat_rest

                if bone_name in KNOWN_180_FLIP_BONES:
                    mat_rest = mat_rest @ BONE_REST_FLIP_CORRECTION

                mat_rot = anim_bone.rotations[anim_frame].to_matrix().to_4x4()

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

                frame = 1 + anim_frame * frames_per_sample
                pose_bone.matrix_basis = mat_rest.inverted() @ mat_bone
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

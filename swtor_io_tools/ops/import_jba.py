# <pep8 compliant>

"""
This script imports Star Wars: The Old Republic animations into Blender.

Usage:
Run this script from "File->Import" menu and then load the desired JBA animation file.

Both 32-bit and 64-bit .jba files are supported (the format is told from the
file's first 4 bytes). Reading them is done in types/jba.py; this module finds
the bones' names when a file has none (see types/mph.py), and applies the
animation to the active skeleton.

BONE NAMES
A rig's .mph files sometimes give an animation more than one bone order: its
anim_library.mph and another network can disagree (about 400 of the humanoid
rigs' 55,000 animations; the library is wrong for 12 of them, e.g. bfanew's
cb_pistol_fire_special_1, which then animated the wrong bones), or the networks
can use several orders for the animation's number of bones and list none for
it (some 660 animations, which couldn't be imported). The data then decides,
when it can: with the right names, the bones that hold still for the whole clip
sit at their rest offsets on one of the skeletons (see SOURCE SKELETON below);
with a wrong order about half of them don't (see _pick_bone_names()). Additive
clips hold offsets instead of rest poses, so for them (and when there's no
clear winner) the library's order is used, or the import is refused as
ambiguous, as before.

TIMING
Keyframes are placed at their real time in the scene's frame rate: sample i is
at i / jba.fps seconds (SWTOR animations are 15 or 30 fps, apart from a few
dozen at 22.5 or 24 fps, mostly dances), whatever the
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

SOURCE SKELETON (import option 'Fit to Armature's Proportions', on by default; its default is the add-on preference jba_fit_to_armature)
A .jba stores each bone's translation as its offset from its parent on the
skeleton the clip was made for, which often isn't the body type it's filed
under: about half of bfanew's, bfnnew's, bmsnew's and bmfnew's clips still
carry bmnnew's proportions (copied from bmn, never adapted), and the
dl_romance_2p_* hugs and kisses were made on bfsnew, in every body's folder.
Used as stored on another body, those translations pull its joints out of place.

So for absolute clips, _source_offsets() reads the skeleton of every rig in
the .jba's category (anim/<category>/<rig> -> art/dynamic/spec/<rig>_skeleton.gr2,
under the Resources Directory or the resources folder the .jba was extracted
into; nothing is bundled with the add-on) and picks the one whose rest
offsets match the most bones that hold still for the whole clip. Bones that
hold a pose (weapons, LowerBack ...) differ from every skeleton alike, so they
don't sway the choice. This armature wins ties. Each bone's translation is then
    this armature's rest offset + (stored - source skeleton's rest offset)
which is the stored value when the clip was made for this armature. The
reference is the same for every clip, so consecutive clips still meet where
their data does. (This replaces "rest offset + (stored - translation_base)":
translation_base is only the bottom of each clip's compression range, so every
clip was shifted by its own amount, and poses held for a whole clip were lost.)
If no skeletons are found, translations are used as stored, with a warning.

GAME RETARGETING TABLES (part of 'Fit to Armature's Proportions')
Before inferring anything, the game's own answer is looked up: the .amx files
next to the networks in the armature's rig folder (types/amx.py) list, for
each animation the rig uses, the rig folder its file comes from and per-bone
corrections. Their translations are exactly the two skeletons' rest offset
differences (bfanew's minus bmnnew's for a bmnnew animation on bfanew); their
rotations are per-animation pose adjustments for this body (up to 20° or more
on hips, shoulders and wrists). Folder paths ("humanoid\\bmnnew\\") say which
rig's folder the animation's file comes from (see types/amx.py). A listing
applies when its file is
the animation being imported: the same file, or one decoding to the same
data (copies in other bodies' folders are often re-saved: different bytes,
same animation). When networks disagree, the rig's anim_sharing.mph.amx wins:
its offsets are exactly the skeletons' rest differences, while some networks
were built against other rig versions and give different ones. When one
source remains, with corrections that agree wherever its listings overlap,
its translations are added as the offsets, and its rotations are applied
after each frame's own rotation
(rotation @ correction, in the bone's own frame). That order reproduces the
bodies' own re-exported copies where they exist (34 of 38 bone tracks within
0.5°, e.g. po_180_idle and the am_fix_* clips; the other 4 are 180° turns,
for which the orders agree), and no other order does. The root bone's
correction is left out: po_180_idle's 180° turn on GOD/Bip01 would turn the
whole character, and none of the re-exported copies has it.
Otherwise (no listing, or several that apply), Fit to Armature infers the
source skeleton from the rest offsets as below.
The answer is checked against the clip's data, as the bone names are: if
its still bones clearly fit another skeleton than the tables' source (see
PICK_MIN_FIT / PICK_MIN_MARGIN), the tables are wrong for it and the data
decides, with a warning. In the humanoid rigs that only happens for
cb_pistol_fire_special_1, whose anim_library.mph bone order is wrong too.
Measured on the 8 humanoid bodies' 48,750 absolute imports: the tables decide
about 42,050 (the rest are nearly all bmnnew's own clips, which its tables
don't list). Where the rest offset matching decides too, the two agree (to
within 0.03% of body height). Their rotations change about 25,700 imports: the
most-moved bone moves under 0.2% of body height in half of them and under 16%
in 99%, with weapon and effect bones (FxRoot, weapons) moving most. Feet barely
move: the lowest foot moves under 1.6% of body height on 99% of the corrected
frames.

RIG DATA
The .gr2 importer stores each skeleton's character specification on its
armature (types/spec.py: art/dynamic/spec/*.dyc/.dat/.mag): its animation
folder, action list and resources root. They're used here to find the rig's
networks and .amx files (instead of guessing them from the armature's name,
which is still the fallback for armatures imported before), and the
skeletons for SOURCE SKELETON even when the .jba was copied elsewhere.

ACTION LIST (import option 'Loop Looping Animations', on by default; 'Masked Bones Only', off by default)
The rig's action list (anim/<category>/am_*.xml, types/aam.py) says how the
game plays each animation: whether it loops, its blend in/out times, and the
bone mask it plays on when layered over the legs' movement (or full body).
All of it is stored on the action as swtor_* custom properties. Looping
animations get a cyclic action (Cycles modifiers, cyclic frame range). Blend
times are not applied to NLA strips: a strip that blends in fades from the
rest pose when nothing plays below it. With Masked Bones Only, an animation
with a mask keys only the mask's bones (the armature's 'Mask: ...' bone
collection, from its .dyc), so it can be layered over locomotion.

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
the whole character while the clip plays. In bmnnew, 130 of 7211 files
actually move it: turns in place (po_normal_to_180_cw ...), throne / Force jumps, mount and dismount,
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
from typing import List, Optional, Sequence, Set, Tuple

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import Context, Object, Operator, OperatorFileListElement
from mathutils import Matrix, Quaternion, Vector

from ..types.jba import JBAError, JointBoneAnimation, read_jba
from ..types.mph import BONES_FROM_DATA, BONES_FROM_LIBRARY, BONES_INFERRED, MPH_ANIM_LIBRARY_FILE, find_bone_name_candidates
from ..types.aam import ActionList, read_action_list
from ..types.amx import folder_sources
from ..types.spec import find_character_spec, spec_folders_for
from ..utils.string import path_split


NO_ARMATURE_MESSAGE = "No armature selected. Select the skeleton to animate."


def _prefs(context):
    # type: (Context) -> bpy.types.AddonPreferences
    """The add-on's preferences."""
    return context.preferences.addons["swtor_io_tools"].preferences


def _rig_name(ob):
    # type: (Object) -> str
    """The rig an armature is for, by its name: 'bmnnew_skeleton.001' -> 'bmnnew'."""
    return re.sub(r"(_skeleton)?(\.\d+)?$", "", ob.name)


def _skeleton_file_name(ob):
    # type: (Object) -> str
    """The skeleton file an armature was imported from, by its name: 'bmnnew_skeleton.001' -> 'bmnnew_skeleton.gr2'."""
    return re.sub(r"\.\d+$", "", ob.name) + ".gr2"


def _morpheme_spaces(scale):
    # type: (float) -> Tuple[Matrix, Matrix]
    """
    (morpheme_space, its inverse): converts between the .jba's space (Morpheme's
    Z-up, millimetre-like units) and the armature's (scale = 1000 / the
    armature's import scale, see _morpheme_scale()).
    """
    morpheme_space = Matrix(((scale, 0, 0, 0), (0, 0, -scale, 0), (0, scale, 0, 0), (0, 0, 0, 1)))
    return morpheme_space, morpheme_space.inverted()

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
    bl_description = "Import and apply an animation to the active SWTOR skeleton in the scene.\n\n• Compatible with .jba files extracted from SWTOR 32-bit (before Game Update 7.2.1)\n   and from SWTOR 64-bit (Game Update 7.2.1 onwards).\n\n• 64-bit .jba files rarely hold their bones' names. Those are read from the game's\n   animation data (.mph) files: either the ones next to the .jba file, or the ones in the\n   Resources Directory set in the Add-on's Preferences (anim/<category>/<rig>,\n   e.g. anim/humanoid/bmnnew, matched by the .jba's folder or the active skeleton's name).\n\n• Keyframes are placed at their real time in the scene's frame rate. SWTOR animations\n   are nearly all 15 or 30 fps: a 30 fps scene puts their keyframes on whole frames.\n\n• Additive animations (the 'ad_' ones) are detected and imported as offsets on top of a pose.\n\n• The animation's world motion (turns in place, jumps, falls) is keyed onto the armature\n   object's Delta Transforms.\n\n• Works on skeletons imported with the Add-on's .gr2 importer.\n\n• Might apply exaggerated bone translations requiring cleanup\n   (check ZG SWTOR Tools Add-on's Clear Bones Translations tool)."
    bl_label = "Import SWTOR (.jba)"
    bl_options = {'UNDO'}


    # File Browser properties. Not based on ImportHelper: invoke() seeds the
    # options from the add-on's preferences before opening the File Browser.
    filepath: StringProperty(subtype='FILE_PATH')
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

    fit_to_armature: BoolProperty(
        name="Fit to Armature's Proportions",
        description="Many SWTOR animations were made for a different body type than the one\nthey're filed under (about half of each body's are bmn animations, never\nadapted), so their bone offsets carry that body's proportions. This finds\nthe body each animation was made for, by reading the skeletons in the\nResources Directory's art/dynamic/spec folder, and converts its bone\ntranslations to the armature's own proportions. Animations made for the\narmature's body are used exactly as stored. Additive animations are never\nchanged.\n\nOff: bone translations are used as stored, whichever body they were made for",
        default=True,
    )

    cyclic_loops: BoolProperty(
        name="Loop Looping Animations",
        description="Animations the game plays as loops (its action list's 'ambient' ones, e.g. idles)\nget a cyclic action: Cycles modifiers on their F-curves and a cyclic\nframe range, so they repeat when played past their end.\n\nThe game's blend times and bone mask are stored on every action\n(swtor_* custom properties) either way.\n\nOff: looping animations play once, like the others",
        default=True,
    )

    masked_bones_only: BoolProperty(
        name="Masked Bones Only",
        description="For animations the game plays on part of the body (its action list gives them a\nbone mask, e.g. 'upperbody', so they can play over the legs' walk), keys only\nthe mask's bones (the armature's 'Mask: ...' bone collection), so the action\ncan be layered over a locomotion action in the NLA. Masked imports are named as\nsuch: 'em_wave_[masked_upperbody]'.\n\nThis leaves out the clip's other bones.\n\nOff: every bone in the clip is keyed",
        default=False,
    )

    set_scene_fps: BoolProperty(
        name="Set Scene FPS to 30",
        description="Sets the scene's frame rate to 30 fps.\n\nThe keyframes are placed at their real time in the scene's frame rate either way. "
                    "SWTOR animations are nearly all 15 or 30 fps, so at 30 fps their keyframes land on whole frames (a 15 fps "
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
        # The options start from the add-on's preferences.
        prefs = _prefs(context)
        self.ignore_facial_bones = prefs.jba_ignore_facial_bones
        self.delete_180 = prefs.jba_delete_180
        self.import_as_nla = prefs.jba_import_as_nla
        self.repair_broken_frames = prefs.jba_repair_broken_frames
        self.fit_to_armature = prefs.jba_fit_to_armature
        self.cyclic_loops = prefs.jba_cyclic_loops
        self.masked_bones_only = prefs.jba_masked_bones_only
        self.scale_animation = prefs.gr2_scale_object
        self.scale_factor = prefs.gr2_scale_factor

        # A filepath passed in by a script imports straight away.
        if not self.filepath:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)


    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        # body is None while a sub-panel is collapsed.
        header, body = layout.panel("SWTOR_IO_JBA_options", default_closed=False)
        header.label(text="Import Options")
        if body:
            body.prop(self, "animation_type")
            body.prop(self, "ignore_facial_bones")
            body.prop(self, "delete_180")
            body.prop(self, "import_as_nla")
            body.prop(self, "repair_broken_frames")
            body.prop(self, "fit_to_armature")
            body.prop(self, "cyclic_loops")
            body.prop(self, "masked_bones_only")
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
        paths = [os.path.join(self.directory, file.name) for file in self.files
                 if file.name.lower().endswith(self.filename_ext)]
        if not paths:
            paths.append(self.filepath)

        # Cleared now that they've been read, so they don't linger if the
        # import fails part-way (which makes debugging confusing).
        self.files.clear()
        self.filepath = ""

        for path in paths:
            if not load(self, context, path):
                return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# The armature's rig data (see top of file: RIG DATA)
# ---------------------------------------------------------------------------

def _armature_rig_info(context, ob):
    # type: (Context, Object) -> Tuple[Optional[str], Optional[str], Optional[str]]
    """
    (rig folder, action list file, resources root) of the armature, as
    absolute paths (each None if not found), from its character
    specification: stored on it by the .gr2 importer ('swtor_anim_folder',
    'swtor_anim_metadata', 'swtor_resources_root'), or, for armatures imported
    before that, looked up by the armature's name ("bmnnew_skeleton" ->
    bmnnew_skeleton.gr2) in the Resources Directory's art/dynamic/spec.
    """
    prefs = _prefs(context)
    anim_folder = ob.get("swtor_anim_folder")
    metadata = ob.get("swtor_anim_metadata")
    roots = [ob.get("swtor_resources_root")]
    if not anim_folder and prefs.swtor_resources_dir:
        skeleton = _skeleton_file_name(ob)
        try:
            spec = find_character_spec(skeleton, spec_folders_for("", [prefs.swtor_resources_dir]))
        except OSError:
            spec = None
        if spec:
            anim_folder, metadata, roots = spec.anim_folder, spec.anim_metadata, [spec.resources_root]
    roots = [root for root in roots + [prefs.swtor_resources_dir] if root]

    def resolve(relative):
        if not relative:
            return None
        for root in roots:
            path = os.path.join(os.path.abspath(root), *relative.split("/"))
            if os.path.exists(path):
                return path
        return None

    folder = resolve(anim_folder)
    root = None
    if folder:
        # <root>/anim/<category>/<rig>
        root = os.path.dirname(os.path.dirname(os.path.dirname(folder)))
    return folder, resolve(metadata), root


def _armature_rig_folder(context, ob):
    # type: (Context, Object) -> Optional[str]
    """The armature's rig folder (anim/<category>/<rig>): from its character specification, else by its name."""
    folder = _armature_rig_info(context, ob)[0]
    if folder:
        return folder
    prefs = _prefs(context)
    anim_folder = os.path.join(prefs.swtor_resources_dir, "anim")
    if prefs.swtor_resources_dir and os.path.isdir(anim_folder):
        rig_name = _rig_name(ob)
        for category in sorted(os.listdir(anim_folder)):
            folder = os.path.join(anim_folder, category, rig_name)
            if os.path.isdir(folder):
                return folder
    return None


def _action_list(context, filepath, ob):
    # type: (Context, str, Object) -> Optional[ActionList]
    """
    The game's action list for the armature's rig (types/aam.py): the one its
    character specification names, else the am_<category>.xml in the
    category folder of the armature's rig or of the .jba (anim/humanoid/
    am_humanoid.xml), else None.
    """
    metadata = _armature_rig_info(context, ob)[1]
    if metadata:
        return read_action_list(metadata)
    folders = []
    rig_folder = _armature_rig_folder(context, ob)
    if rig_folder:
        folders.append(os.path.dirname(rig_folder))
    folders.append(os.path.dirname(os.path.dirname(os.path.abspath(filepath))))
    for category in folders:
        path = os.path.join(category, f"am_{os.path.basename(category)}.xml")
        if os.path.isfile(path):
            return read_action_list(path)
    return None


def _mask_bones(context, ob, mask_name):
    # type: (Context, Object, str) -> Optional[Set[str]]
    """The bones of one of the rig's bone masks: its 'Mask: <name>' bone collection, else its character specification's."""
    from .import_gr2_armature import MASK_COLLECTION_PREFIX
    collection = ob.data.collections.get(MASK_COLLECTION_PREFIX + mask_name)
    if collection is not None:
        return {bone.name for bone in collection.bones}
    prefs = _prefs(context)
    skeleton = _skeleton_file_name(ob)
    roots = [r for r in (ob.get("swtor_resources_root"), prefs.swtor_resources_dir) if r]
    try:
        spec = find_character_spec(skeleton, spec_folders_for("", roots))
    except OSError:
        spec = None
    weights = spec.masks.get(mask_name) if spec else None
    return {name for name, weight in weights.items() if weight > 0} if weights else None


# ---------------------------------------------------------------------------
# Reading the file and finding its bones' names (see top of file: BONE NAMES)
# ---------------------------------------------------------------------------

def _find_rig_folders(context, filepath):
    # type: (Context, str) -> List[str]
    """
    The rig folders (anim/<category>/<rig>) whose .mph files may name the
    .jba's bones, most likely first.
    """
    rig_folders = []
    ob = context.active_object
    armature = ob if ob and ob.type == 'ARMATURE' else None

    # A .jba still in its extracted location sits next to its networks.
    jba_folder = os.path.dirname(os.path.abspath(filepath))
    if any(f.lower().endswith(".mph") for f in os.listdir(jba_folder)):
        rig_folders.append(jba_folder)

    # The armature's own rig folder, from its character specification.
    if armature:
        armature_folder = _armature_rig_info(context, armature)[0]
        if armature_folder and armature_folder not in rig_folders:
            rig_folders.append(armature_folder)

    # Otherwise look for <Resources>/anim/<category>/<rig>, where <rig> is
    # the .jba's folder name (if copied along with it) or the active
    # skeleton's ("bmnnew_skeleton" or "bmnnew_skeleton.001" -> "bmnnew").
    rig_names = [os.path.basename(jba_folder)]
    if armature:
        rig_names.append(_rig_name(armature))

    prefs = _prefs(context)
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
        picked = None
        rig_folders = _find_rig_folders(context, filepath)
        for rig_folder in rig_folders:
            candidates = find_bone_name_candidates(rig_folder, anim_name, num_bones)
            if len(candidates) == 1:
                bone_names, source = candidates[0]
                break
            if candidates:
                # The folder's sources disagree: the data decides when it can
                # (see _pick_bone_names()), else anim_library.mph's order is
                # used as before, else the import is refused as ambiguous.
                picked = _pick_bone_names(operator, context, jba, filepath, anim_name, candidates)
                if picked:
                    bone_names, source = picked[0]
                    break
                if candidates[0][1] == BONES_FROM_LIBRARY:
                    bone_names, source = candidates[0]
                    break
                num_layouts = max(num_layouts, len(candidates))

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

        if picked:
            _, best, second, still = picked
            operator.report({'INFO'}, f"Bone names for \'{anim_name}\': the animation data (.mph) files give "
                                      f"{len(candidates)} different bone orders; used the one that puts {best} of the "
                                      f"{still} bones that hold still at a skeleton's rest pose (next best: {second}).")
        elif source == BONES_INFERRED:
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


# ---------------------------------------------------------------------------
# Rest poses (see top of file: LIMB-ALIGNED BONES)
# ---------------------------------------------------------------------------

def _file_rest_local(pose_bone):
    # type: (bpy.types.PoseBone) -> Matrix
    """A bone's rest relative to its parent, in the file's own bone axes (see top of file)."""
    mat_rest = pose_bone.bone.matrix_local @ _rest_correction(pose_bone.bone).inverted()
    if pose_bone.parent:
        parent_rest = pose_bone.parent.bone.matrix_local @ _rest_correction(pose_bone.parent.bone).inverted()
        mat_rest = parent_rest.inverted() @ mat_rest
    return mat_rest


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
# Additive animations (see top of file)
# ---------------------------------------------------------------------------

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
        name = anim_bone.name
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


# ---------------------------------------------------------------------------
# NLA tracks (see top of file)
# ---------------------------------------------------------------------------

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
# Source skeleton (see top of file: SOURCE SKELETON)
# ---------------------------------------------------------------------------

# A bone "holds still" if its stored translation moves less than this over
# the whole clip, and its still translation "matches" a skeleton's rest offset
# if it's within SOURCE_MATCH_RELATIVE of that offset's length or
# SOURCE_MATCH_ABSOLUTE, whichever is larger. Both absolute values are in
# unscaled skeleton units (they're multiplied by the import scale).
STILL_TOLERANCE = 1e-4
SOURCE_MATCH_RELATIVE = 0.02
SOURCE_MATCH_ABSOLUTE = 1e-3

# {skeleton path: (modification time, {bone name: unscaled rest offset})}
_skeleton_offsets_cache = {}


class _QuietReporter:
    """Stands in for an operator when reading a skeleton: a skeleton that can't be read is just skipped."""

    def report(self, *args):
        pass


def _skeleton_rest_offsets(filepath):
    # type: (str) -> Optional[dict]
    """
    A skeleton .gr2's rest offsets, {bone name: translation from its parent},
    in the file's own (unscaled) units, worked out the way the armature
    importer builds its rests (ops/import_gr2_armature.py), so that they
    compare directly with _file_rest_local() on one of its armatures. None if
    the file can't be read.
    """
    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        return None
    cached = _skeleton_offsets_cache.get(filepath)
    if cached and cached[0] == mtime:
        return cached[1]

    from .import_gr2 import read as read_gr2
    from .import_gr2_armature import _matrix_from_column_major, _proper_rotation

    try:
        gr2 = read_gr2(_QuietReporter(), filepath)
        bones = [gr2.bone_buffer[i] for i in range(len(gr2.bone_buffer))] if gr2 else []
        rest = []
        for bone in bones:
            matrix = _matrix_from_column_major(bone.root_to_bone).inverted_safe()
            parent = bone.parent_index
            reference = rest[parent] if 0 <= parent < len(rest) else Matrix.Identity(4)
            matrix, _ = _proper_rotation(matrix, reference)
            rest.append(matrix)
        offsets = {}
        for bone, matrix in zip(bones, rest):
            parent = bone.parent_index
            local = rest[parent].inverted_safe() @ matrix if 0 <= parent < len(rest) else matrix
            offsets.setdefault("Bip01" if bone.name == "GOD" else bone.name, local.to_translation())
    except Exception:  # a skeleton that can't be read is skipped, never fails the import
        offsets = None
    if not offsets:
        offsets = None

    _skeleton_offsets_cache[filepath] = (mtime, offsets)
    return offsets


def _source_skeleton_paths(context, filepath):
    # type: (Context, str) -> List[Tuple[str, str]]
    """
    [(rig name, skeleton path)] for every rig in the .jba's animation category
    that has a skeleton: each folder next to the .jba's rig folder in
    anim/<category>/ (bfanew, bmnnew ... in anim/humanoid) whose
    art/dynamic/spec/<rig>_skeleton.gr2 exists, under the Resources Directory
    or the resources folder the .jba was extracted into.
    """
    resources_roots = []
    jba_folder = os.path.dirname(os.path.abspath(filepath))
    category_folder = os.path.dirname(jba_folder)
    if os.path.basename(os.path.dirname(category_folder)).lower() == "anim":
        resources_roots.append(os.path.dirname(os.path.dirname(category_folder)))
    ob = context.active_object
    if ob and ob.type == 'ARMATURE':
        armature_root = _armature_rig_info(context, ob)[2]
        if armature_root and armature_root not in resources_roots:
            resources_roots.append(armature_root)
    prefs = _prefs(context)
    if prefs.swtor_resources_dir:
        resources_roots.append(os.path.abspath(prefs.swtor_resources_dir))

    category_folders = []
    for rig_folder in _find_rig_folders(context, filepath):
        category = os.path.dirname(os.path.abspath(rig_folder))
        if category not in category_folders:
            category_folders.append(category)

    found = []
    seen = set()
    for category in category_folders:
        try:
            rig_names = sorted(name for name in os.listdir(category) if os.path.isdir(os.path.join(category, name)))
        except OSError:
            continue
        for rig_name in rig_names:
            if rig_name in seen:
                continue
            for root in resources_roots:
                path = os.path.join(root, "art", "dynamic", "spec", f"{rig_name}_skeleton.gr2")
                if os.path.isfile(path):
                    found.append((rig_name, path))
                    seen.add(rig_name)
                    break
    return found


def _morpheme_scale(operator, context, ob):
    # type: (Operator, Context, Object) -> float
    """
    The scale of build()'s morpheme_space, which converts .jba units into the
    armature's: 1000 / the armature's import scale.

    Taken from the armature object's import scale custom property if it has
    one. If not, use scale_animation and scale_factor or the add-on's prefs
    settings for the .gr2 import. "gr2_scale" was the old name for this
    property before it was renamed to "import_scale" to disambiguate from the
    newer gr2_* BWAG-format round-trip properties. Objects imported with older
    addon versions still carry the old key, so we fall back to it rather than
    silently recalculating a (possibly different) scale for those objects.
    """
    # scale = 1000 * operator.scale_factor was the original code's calculation, but it needs to be
    # 1000 * (1/scale_factor) to work correctly.
    if 'import_scale' in ob:
        return 1000 * (1 / ob['import_scale'])
    if 'gr2_scale' in ob:  # legacy key, pre-rename
        return 1000 * (1 / ob['gr2_scale'])
    if getattr(operator, "scale_animation", False):
        return 1000 * (1 / operator.scale_factor)
    prefs = _prefs(context)
    if prefs.gr2_scale_object:
        return 1000 * (1 / prefs.gr2_scale_factor)
    return 1000


def _still_translations(jba, scale):
    # type: (JointBoneAnimation, float) -> dict
    """
    {track index: translation, in armature units} for every track of the clip
    whose translation holds still for the whole clip (see STILL_TOLERANCE).
    """
    import_scale = 1000.0 / scale  # skeleton units -> armature units (see morpheme_space in build())
    still_tolerance = STILL_TOLERANCE * import_scale
    morpheme_space_inv = _morpheme_spaces(scale)[1].to_3x3()
    still = {}
    for i, anim_bone in enumerate(jba.bones):
        translations = anim_bone.translations
        first = translations[0]
        if all((t - first).length / scale < still_tolerance for t in translations):
            still[i] = morpheme_space_inv @ first
    return still


def _reference_skeletons(context, filepath, ob, scale):
    # type: (Context, str, Object, float) -> List[Tuple[str, dict]]
    """
    [(rig name, {bone name: rest offset})] for every skeleton of the clip's
    category (see _source_skeleton_paths()), in armature units and limited to
    the armature's bones.
    """
    import_scale = 1000.0 / scale
    axis = (Matrix.Rotation(math.pi * 0.5, 4, 'X') if ob.get("import_axis_conversion", False)
            else Matrix.Identity(4))
    skeletons = []
    for rig_name, path in _source_skeleton_paths(context, filepath):
        offsets = _skeleton_rest_offsets(path)
        if not offsets:
            continue
        scaled = {}
        for name, offset in offsets.items():
            if name in ob.pose.bones:
                scaled[name] = offset * import_scale
                if not ob.pose.bones[name].parent:
                    scaled[name] = axis.to_3x3() @ scaled[name]
        skeletons.append((rig_name, scaled))
    return skeletons


def _count_matches(still, offsets, absolute_match):
    # type: (dict, dict, float) -> int
    """How many of still's {bone name: translation} are at that bone's rest offset in offsets."""
    count = 0
    for name, translation in still.items():
        offset = offsets.get(name)
        if offset is not None and (translation - offset).length <= max(SOURCE_MATCH_RELATIVE * offset.length,
                                                                         absolute_match):
            count += 1
    return count


# _pick_bone_names(): the best candidate bone order must put at least this
# share of the clip's still tracks at a skeleton's rest offsets, and this
# share more than the next best. Measured on the humanoid rigs' ~400
# animations whose sources disagree, and the ~200 they refused as ambiguous:
# the right order fits 90-100% of the still tracks, the wrong one about 45%.
PICK_MIN_FIT = 0.75
PICK_MIN_MARGIN = 0.2


def _pick_bone_names(operator, context, jba, filepath, anim_name, candidates):
    # type: (Operator, Context, JointBoneAnimation, str, str, List[Tuple[List[str], str]]) -> Optional[tuple]
    """
    When a rig's folder gives an animation several bone orders (see
    types/mph.py: find_bone_name_candidates()), picks the one its data
    agrees with: with the right names, the bones that hold still for the
    whole clip sit exactly at their rest offsets on the armature or on one of
    the category's skeletons (see SOURCE SKELETON at the top of the file);
    with wrong names, about half of them don't. Returns
    ((bone names, source), best count, next best count, still tracks), or
    None if the data can't tell clearly: additive clips (which hold offsets,
    not rest poses), clips with no still tracks, or no clear winner.
    """
    ob = context.active_object
    animation_type = getattr(operator, "animation_type", 'AUTO')
    if animation_type == 'ADDITIVE' or (animation_type == 'AUTO' and _is_additive_name(anim_name)):
        return None
    scale = _morpheme_scale(operator, context, ob)
    still = _still_translations(jba, scale)
    if not still:
        return None
    absolute_match = SOURCE_MATCH_ABSOLUTE * 1000.0 / scale
    references = [{pose_bone.name: _file_rest_local(pose_bone).to_translation() for pose_bone in ob.pose.bones}]
    references += [offsets for _, offsets in _reference_skeletons(context, filepath, ob, scale)]

    scores = []
    for bone_names, _ in candidates:
        named = {}
        for i, translation in still.items():
            name = "Bip01" if bone_names[i] == "GOD" else bone_names[i]
            named.setdefault(name, translation)
        scores.append(max(_count_matches(named, offsets, absolute_match) for offsets in references))
    order = sorted(range(len(candidates)), key=lambda i: -scores[i])
    best, second = scores[order[0]], scores[order[1]]
    if best >= PICK_MIN_FIT * len(still) and best - second >= PICK_MIN_MARGIN * len(still):
        return candidates[order[0]], best, second, len(still)
    return None


# ---------------------------------------------------------------------------
# The game's retargeting tables (see top of file: GAME RETARGETING TABLES)
# ---------------------------------------------------------------------------

_digest_cache = {}


def _file_digest(path):
    # type: (str) -> Optional[bytes]
    """A file's MD5, cached by path, size and modification time; None if it can't be read."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), stat.st_size, stat.st_mtime)
    if key not in _digest_cache:
        import hashlib
        with open(path, "rb") as file:
            _digest_cache[key] = hashlib.md5(file.read()).digest()
    return _digest_cache[key]


# The .amx rotation corrections smaller than this (degrees) are float noise.
ROTATION_CORRECTION_MIN = 0.01


# Two decodings of an animation are the same animation (only re-saved) when
# every bone's rotation and translation agree within these (degrees, .jba units).
SAME_ANIMATION_ROTATION = 0.25
SAME_ANIMATION_TRANSLATION = 0.05

_same_animation_cache = {}


def _same_animation(jba, filepath, other_path, anim_name):
    # type: (JointBoneAnimation, str, str, str) -> bool
    """
    Whether other_path holds the same animation as jba (read from filepath):
    the same file, byte for byte the same, or decoding to the same rotations
    and translations, bone by bone. Copies of a shared animation in other
    bodies' folders are often re-saved: their bytes differ, their data doesn't.
    """
    if os.path.abspath(other_path) == os.path.abspath(filepath):
        return True
    digest, other_digest = _file_digest(filepath), _file_digest(other_path)
    if digest is None or other_digest is None:
        return False
    if digest == other_digest:
        return True
    key = (digest, other_digest)
    if key in _same_animation_cache:
        return _same_animation_cache[key]
    same = False
    try:
        other = read_jba(other_path)
        if all(bone.name for bone in other.bones):
            names = [bone.name for bone in other.bones]
        else:
            candidates = find_bone_name_candidates(os.path.dirname(other_path), anim_name, len(other.bones))
            names = candidates[0][0] if len(candidates) == 1 else None
        if names and other.num_frames == jba.num_frames:
            tracks = {("Bip01" if name == "GOD" else name): bone for name, bone in zip(names, other.bones)}
            compared = 0
            same = True
            for bone in jba.bones:
                other_bone = tracks.get(bone.name)
                if other_bone is None:
                    continue
                compared += 1
                if any(_rotation_angle(p, q) > SAME_ANIMATION_ROTATION
                       for p, q in zip(bone.rotations, other_bone.rotations)) or \
                        any((p - q).length > SAME_ANIMATION_TRANSLATION
                            for p, q in zip(bone.translations, other_bone.translations)):
                    same = False
                    break
            same = same and compared > 0
    except (JBAError, IndexError, ValueError, OverflowError, OSError):
        same = False
    _same_animation_cache[key] = same
    return same


def _game_source(context, filepath, ob, anim_name, jba):
    # type: (Context, str, Object, str, JointBoneAnimation) -> Optional[Tuple[str, dict, bool]]
    """
    What the game's retargeting tables (the .amx files in the armature's rig
    folder, see types/amx.py) say about this animation on this rig:
    (source rig name, {bone name: correction (translation x, y, z in .jba
    units, rotation w, x, y, z)}, whether the source is the rig itself).

    A listing applies when its file is the animation being imported (see
    _same_animation()): the listed rig folder's copy. When networks disagree, the rig's
    anim_sharing.mph.amx (its dedicated sharing table, whose offsets are
    exactly the skeletons' rest differences) wins; other networks, built
    against other rig versions, can give different offsets. An answer is only
    given when one source remains, with corrections that agree wherever its
    listings overlap. Otherwise None (Fit to Armature then works it out from
    the clip's data).
    """
    rig_folder = _armature_rig_folder(context, ob)
    if not rig_folder:
        return None
    listings = folder_sources(rig_folder).get(anim_name)
    if not listings:
        return None
    anim_root = os.path.dirname(os.path.dirname(rig_folder))
    own = "/".join(os.path.normpath(rig_folder).split(os.sep)[-2:])
    own_file = os.path.join(rig_folder, anim_name + ".jba")

    applicable = []
    for listing in listings:
        source = listing.source
        source_file = own_file if source == own else os.path.join(anim_root, *source.split("/"), anim_name + ".jba")
        if _same_animation(jba, filepath, source_file, anim_name):
            applicable.append((source, listing))
    if any(listing.sharing for _, listing in applicable):
        applicable = [(source, listing) for source, listing in applicable if listing.sharing]
    if len({source for source, _ in applicable}) != 1:
        return None
    source = applicable[0][0]
    # Networks list corrections for different subsets of the rig's bones;
    # they're merged, as long as they agree wherever they overlap.
    merged = {}
    for _, listing in applicable:
        for bone, correction in (listing.corrections or {}).items():
            bone = "Bip01" if bone == "GOD" else bone
            known = merged.setdefault(bone, tuple(correction))
            if any(abs(a - b) > 1e-3 for a, b in zip(known[:3], correction[:3])):
                return None
            if _rotation_angle(Quaternion(known[3:7]), Quaternion(correction[3:7])) > 0.5:
                return None
    return source.split("/")[-1], merged, source == own


# ---------------------------------------------------------------------------
# Fit to Armature's Proportions: putting it together
# ---------------------------------------------------------------------------

def _still_bones(jba, ob, scale):
    # type: (JointBoneAnimation, Object, float) -> Tuple[dict, dict]
    """
    The clip's bones that hold still for the whole clip, and this armature's
    rest offsets for them: ({bone name: translation}, {bone name: rest
    offset}), both in armature units, for the armature's bones only.
    """
    still, target = {}, {}
    still_tracks = _still_translations(jba, scale)
    for i, anim_bone in enumerate(jba.bones):
        name = anim_bone.name
        if name not in ob.pose.bones or name in still:
            continue
        if i in still_tracks:
            target[name] = _file_rest_local(ob.pose.bones[name]).to_translation()
            still[name] = still_tracks[i]
    return still, target


def _data_contradicts(game, still, target, candidates, absolute_match):
    # type: (tuple, dict, dict, list, float) -> Optional[Tuple[str, int, int]]
    """
    Whether the clip's still bones clearly fit another skeleton than the one
    the game's tables name as its source (with the same margins as
    _pick_bone_names()): (that skeleton, its matches, the source's matches),
    or None. The tables are then wrong for the clip: bfnnew's
    anim_sharing.mph.amx lists cb_pistol_fire_special_1, which carries
    bmnnew's proportions, as bfnnew's own.
    """
    source, _, own = game
    source_offsets = target if own else dict(candidates).get(source)
    if not still or source_offsets is None:
        return None
    source_matches = _count_matches(still, source_offsets, absolute_match)
    best_name, best_matches = None, source_matches
    for rig_name, offsets in [("this armature", target)] + candidates:
        count = _count_matches(still, offsets, absolute_match)
        if count > best_matches:
            best_name, best_matches = rig_name, count
    if (best_name is not None and best_matches >= PICK_MIN_FIT * len(still)
            and best_matches - source_matches >= PICK_MIN_MARGIN * len(still)):
        return best_name, best_matches, source_matches
    return None


def _game_table_offsets(operator, ob, scale, anim_name, game, rotations_out):
    # type: (Operator, Object, float, str, tuple, Optional[dict]) -> dict
    """
    The game's retargeting tables' answer (see _game_source()) as translation
    offsets in armature units; its rotation corrections go in rotations_out
    (all but the root bone's: see top of file: GAME RETARGETING TABLES).
    """
    source, corrections, own = game
    morpheme_space_inv = _morpheme_spaces(scale)[1].to_3x3()
    offsets = {name: morpheme_space_inv @ Vector(correction[:3]) for name, correction in corrections.items()
               if name in ob.pose.bones and any(abs(x) > 1e-6 for x in correction[:3])}
    rotations = {}
    for name, correction in corrections.items():
        rotation = Quaternion(correction[3:7]).normalized()
        if (name in ob.pose.bones and name not in KNOWN_180_FLIP_BONES
                and math.degrees(rotation.angle) > ROTATION_CORRECTION_MIN):
            rotations[name] = rotation
    if rotations_out is not None:
        rotations_out.update(rotations)

    rotated = f"; the game's pose adjustments applied to {len(rotations)} bones" if rotations else ""
    if not offsets:
        how = "as this body's own animation" if own else f"as made for {source}, with no offsets"
        operator.report({'INFO'}, f"'{anim_name}': the game's retargeting tables (.amx) play this file on "
                                  f"this body {how}; bone translations used as stored{rotated}.")
        return {}
    operator.report({'INFO'}, f"'{anim_name}': the game's retargeting tables (.amx) play it on this body as "
                              f"made for {source}; bone translations converted with the game's own offsets "
                              f"({len(offsets)} bones){rotated}.")
    return offsets


def _inferred_offsets(operator, ob, anim_name, still, target, candidates, absolute_match):
    # type: (Operator, Object, str, dict, dict, list, float) -> dict
    """
    Finds the skeleton the clip was made for from its still bones (see top of
    file: SOURCE SKELETON) and returns the translation offsets that fit it to
    this armature: {bone name: this armature's rest offset - the source
    skeleton's}. Empty when the clip was made for this armature.
    """
    if not still:
        return {}
    if not candidates:
        operator.report({'WARNING'}, f"'{anim_name}': no skeletons found in art/dynamic/spec to check which body the "
                                     "clip was made for (set the Resources Directory in the add-on preferences). Bone "
                                     "translations used as stored: a clip made for another body type keeps that "
                                     "body's proportions.")
        return {}

    # This armature wins ties: a clip that fits it is used exactly as stored.
    target_matches = _count_matches(still, target, absolute_match)
    best_name, best_offsets, best_matches = None, target, target_matches
    for rig_name, offsets in candidates:
        count = _count_matches(still, offsets, absolute_match)
        if count > best_matches:
            best_name, best_offsets, best_matches = rig_name, offsets, count

    if best_name is None:
        operator.report({'INFO'}, f"'{anim_name}': made for this armature ({target_matches} of {len(still)} still "
                                  "bones match its rest pose); bone translations used as stored.")
        return {}

    operator.report({'INFO'}, f"'{anim_name}': made for {best_name} ({best_matches} of {len(still)} still bones match "
                              f"its rest pose, {target_matches} match this armature's); bone translations converted "
                              "to this armature's proportions.")
    return {name: _file_rest_local(ob.pose.bones[name]).to_translation() - offset
            for name, offset in best_offsets.items() if name in ob.pose.bones}


def _source_offsets(operator, context, filepath, jba, ob, scale, anim_name, rotations_out=None):
    # type: (Operator, Context, str, JointBoneAnimation, Object, float, str, Optional[dict]) -> dict
    """
    'Fit to Armature's Proportions': per bone, what to add to the clip's
    stored translation to fit this armature, in armature units. Empty when the
    clip was made for this armature (or nothing can tell): the stored
    translations are then used as they are.

    The game's retargeting tables decide first (see _game_source()), unless
    the clip's own data clearly contradicts them; their rotation corrections
    then go in rotations_out, {bone name: Quaternion in the .jba's own axes},
    to be applied after each frame's rotation. Otherwise the source skeleton
    is inferred from the clip's still bones. See top of file: SOURCE
    SKELETON and GAME RETARGETING TABLES.
    """
    absolute_match = SOURCE_MATCH_ABSOLUTE * 1000.0 / scale  # in armature units (see morpheme_space)
    still, target = _still_bones(jba, ob, scale)
    candidates = _reference_skeletons(context, filepath, ob, scale) if still else []

    game = _game_source(context, filepath, ob, anim_name, jba)
    if game is not None:
        contradiction = _data_contradicts(game, still, target, candidates, absolute_match)
        if contradiction is None:
            return _game_table_offsets(operator, ob, scale, anim_name, game, rotations_out)
        best_name, best_matches, source_matches = contradiction
        source, _, own = game
        operator.report({'WARNING'}, f"'{anim_name}': the game's retargeting tables (.amx) list it as made for "
                                     f"{'this body' if own else source}, but its data fits {best_name} "
                                     f"({best_matches} of {len(still)} still bones at its rest pose, against "
                                     f"{source_matches}); the data was used instead.")

    return _inferred_offsets(operator, ob, anim_name, still, target, candidates, absolute_match)


# ---------------------------------------------------------------------------
# Keyframes
# ---------------------------------------------------------------------------

def _new_key_types(context):
    # type: (Context) -> Tuple[int, int]
    """The interpolation and handle type (as foreach_set() values) that keyframe_insert() gives new keys: the user's preferences."""
    from bpy.types import Keyframe
    edit = context.preferences.edit
    props = Keyframe.bl_rna.properties
    return (props["interpolation"].enum_items[edit.keyframe_new_interpolation_type].value,
            props["handle_left_type"].enum_items[edit.keyframe_new_handle_type].value)


def _key_channels(action, ob, key_types, data_path, group_name, frames, values):
    # type: (bpy.types.Action, Object, Tuple[int, int], str, str, List[float], List[Sequence[float]]) -> None
    """
    Keys values[i] (one value per array index of data_path) on frames[i], in
    bulk: the same keys keyframe_insert() would make (Bezier, auto clamped
    handles, or whatever the user's preferences say (key_types: see
    _new_key_types()), grouped by bone), but written all at once into each F-curve
    instead of one keyframe_insert() call per frame and channel, which is
    what made importing long animations take many seconds.
    """
    num = len(frames)
    if not num:
        return
    for index in range(len(values[0])):
        fcurve = action.fcurve_ensure_for_datablock(ob, data_path, index=index, group_name=group_name)
        points = fcurve.keyframe_points
        if len(points):  # not expected on a new action: keep keyframe_insert()'s replace-on-same-frame behaviour
            for frame, value in zip(frames, values):
                points.insert(frame, value[index], options={'REPLACE', 'FAST'})
        else:
            co = [0.0] * (2 * num)
            co[0::2] = frames
            co[1::2] = [value[index] for value in values]
            points.add(num)
            points.foreach_set("co", co)
            interpolation, handle_type = key_types
            points.foreach_set("interpolation", [interpolation] * num)
            points.foreach_set("handle_left_type", [handle_type] * num)
            points.foreach_set("handle_right_type", [handle_type] * num)
        fcurve.update()


# ---------------------------------------------------------------------------
# World motion
# ---------------------------------------------------------------------------

def _key_world_motion(operator, ob, jba, morpheme_space, morpheme_space_inv, frames_per_sample, key_types):
    # type: (Operator, Object, JointBoneAnimation, Matrix, Matrix, float, Tuple[int, int]) -> None
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
    axis_inv = axis.inverted()
    q_inv = q.inverted()
    frames = [1 + i * frames_per_sample for i in range(num)]
    locations = []
    rotations = []
    previous_q = None
    previous_e = None
    for i in range(num):
        motion = (morpheme_space_inv @ Matrix.Translation(world_space.translations[i])
                  @ world_space.rotations[i].to_matrix().to_4x4() @ morpheme_space)
        motion = axis @ motion @ axis_inv
        t = motion.to_translation()
        locations.append(q @ Vector((t.x * s.x, t.y * s.y, t.z * s.z)))

        dq = q @ motion.to_quaternion() @ q_inv
        if mode == 'QUATERNION':
            if previous_q is not None:
                dq.make_compatible(previous_q)
            previous_q = dq
            rotations.append(dq)
        elif mode != 'AXIS_ANGLE':
            euler = dq.to_euler(mode, previous_e) if previous_e is not None else dq.to_euler(mode)
            previous_e = euler
            rotations.append(euler)

    # As keyframe_insert() would, the object is left showing the last frame.
    action = ob.animation_data.action
    ob.delta_location = locations[-1]
    _key_channels(action, ob, key_types, "delta_location", "Object Transforms", frames, locations)
    if mode == 'QUATERNION':
        ob.delta_rotation_quaternion = rotations[-1]
        _key_channels(action, ob, key_types, "delta_rotation_quaternion", "Object Transforms", frames, rotations)
    elif mode != 'AXIS_ANGLE':
        ob.delta_rotation_euler = rotations[-1]
        _key_channels(action, ob, key_types, "delta_rotation_euler", "Object Transforms", frames, rotations)


# ---------------------------------------------------------------------------
# The game's action list (see top of file: ACTION LIST)
# ---------------------------------------------------------------------------

def _action_fcurves(action):
    # type: (bpy.types.Action) -> list
    """Every F-curve of an action, for both slotted (Blender 4.4+) and legacy actions."""
    fcurves = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for channelbag in getattr(strip, "channelbags", []):
                fcurves.extend(channelbag.fcurves)
    if not fcurves and hasattr(action, "fcurves"):
        fcurves = list(action.fcurves)
    return fcurves


def _apply_action_info(operator, action, info, mask_name, frames, anim_name):
    # type: (Operator, bpy.types.Action, object, Optional[str], List[float], str) -> None
    """
    Stores what the game's action list says about the animation on the action
    ('swtor_loops', 'swtor_blend_in', 'swtor_blend_out' in seconds,
    'swtor_full_body', 'swtor_mask'), and, with Loop Looping Animations on,
    makes looping ones cyclic: a cyclic manual frame range over the keys, and
    a Cycles modifier on every F-curve.
    """
    action["swtor_loops"] = info.loops
    action["swtor_full_body"] = info.full_body
    if info.blend_in is not None:
        action["swtor_blend_in"] = info.blend_in
    if info.blend_out is not None:
        action["swtor_blend_out"] = info.blend_out
    if mask_name:
        action["swtor_mask"] = mask_name

    details = []
    if info.loops and getattr(operator, "cyclic_loops", True) and len(frames) > 1:
        action.use_frame_range = True
        action.frame_start = frames[0]
        action.frame_end = frames[-1]
        action.use_cyclic = True
        for fcurve in _action_fcurves(action):
            if not any(modifier.type == 'CYCLES' for modifier in fcurve.modifiers):
                fcurve.modifiers.new('CYCLES')
        details.append("loops in the game, made cyclic")
    elif info.loops:
        details.append("loops in the game")
    if info.blend_in is not None or info.blend_out is not None:
        blend_in = f"{info.blend_in:g} s" if info.blend_in is not None else "default"
        blend_out = f"{info.blend_out:g} s" if info.blend_out is not None else "default"
        details.append(f"blends in over {blend_in} and out over {blend_out}")
    details.append("plays on the whole body" if not mask_name else f"plays on the '{mask_name}' bone mask when layered")
    operator.report({'INFO'}, f"'{anim_name}': per the game's action list, " + "; ".join(details) +
                              " (stored on the action as swtor_* properties).")


# ---------------------------------------------------------------------------
# Building the animation
# ---------------------------------------------------------------------------

def _is_additive(operator, jba, ob, morpheme_space_inv, anim_name):
    # type: (Operator, JointBoneAnimation, Object, Matrix, str) -> bool
    """Whether to import the clip as additive: by its name ('ad_'), or as the user set it (see top of file)."""
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
    return additive


def _repair(operator, jba, ob, morpheme_space, morpheme_space_inv, frames_per_sample, anim_name):
    # type: (Operator, JointBoneAnimation, Object, Matrix, Matrix, float, str) -> None
    """Repair Broken Frames (see top of file): repairs them in place, and lists them."""
    broken = _find_broken_frames(jba)
    repaired = _repair_broken_frames(jba, broken, ob, morpheme_space, morpheme_space_inv) if broken else {}
    if repaired:
        details = []
        for s in sorted(repaired):
            frame = 1 + s * frames_per_sample
            details.append(f"frame {round(frame, 2):g} ({', '.join(repaired[s])})")
        operator.report({'INFO'}, f"'{anim_name}': repaired {len(repaired)} broken "
                                  f"frame{'s' if len(repaired) > 1 else ''}: " + "; ".join(details))


def _masked_bones(operator, context, ob, mask_name, animated_bones, anim_name):
    # type: (Operator, Context, Object, str, list, str) -> Optional[Set[str]]
    """Masked Bones Only (see top of file: ACTION LIST): the bones to key, or None to key them all."""
    mask_bones = _mask_bones(context, ob, mask_name)
    if mask_bones:
        keyed = sum(1 for b in animated_bones if b.name in mask_bones)
        operator.report({'INFO'}, f"'{anim_name}': the game plays it on its '{mask_name}' bone mask; only those "
                                  f"{keyed} bones were keyed (Masked Bones Only).")
    else:
        operator.report({'WARNING'}, f"'{anim_name}': the game plays it on its '{mask_name}' bone mask, but the "
                                     "armature has no such mask; every bone was keyed.")
    return mask_bones


def _new_action(animation_data, action_name, additive, as_nla_track):
    # type: (bpy.types.AnimData, str, bool, bool) -> bpy.types.Action
    """
    A new action, made the armature's active one to key into. With NLA
    tracks (see top of file), whatever the armature was playing is pushed
    down into a track first.
    """
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

    action = bpy.data.actions.new(action_name)
    animation_data.action = action
    if additive:
        action["jba_additive"] = True
    return action


def _bone_keys(anim_bone, pose_bone, additive, ignore_translation, source_offset, rotation_correction,
               morpheme_space, morpheme_space_inv):
    # type: (object, bpy.types.PoseBone, bool, bool, Optional[Vector], Optional[Quaternion], Matrix, Matrix) -> tuple
    """
    One bone's keys, as setting pose_bone.matrix_basis would store them:
    ([location per frame], [rotation_quaternion per frame], the last frame's
    basis matrix).
    """
    # Rest in the file's own bone axes (see top of file: LIMB-ALIGNED BONES)
    correction = _rest_correction(pose_bone.bone)
    correction_inv = correction.inverted()
    mat_rest = _file_rest_local(pose_bone)
    if not additive and pose_bone.name in KNOWN_180_FLIP_BONES:
        mat_rest = mat_rest @ BONE_REST_FLIP_CORRECTION
    mat_rest_inv = mat_rest.inverted()
    rest_translation = mat_rest.to_translation()
    rest_rotation = mat_rest.to_3x3()

    locations = []
    rotations = []
    mat_basis = None
    for anim_frame in range(len(anim_bone.rotations)):
        # The game's per-animation pose adjustment for this body, if any, comes
        # after the frame's own rotation (see top of file: GAME RETARGETING TABLES).
        if rotation_correction is not None:
            mat_rot = (anim_bone.rotations[anim_frame] @ rotation_correction).to_matrix().to_4x4()
        else:
            mat_rot = anim_bone.rotations[anim_frame].to_matrix().to_4x4()

        if additive:
            # Offsets on top of the rest pose (see top of file: ADDITIVE
            # ANIMATIONS): no Bip01 flip correction (the rest is used as it
            # is), and the translation is the offset itself.
            mat_add = (morpheme_space_inv @ Matrix.Translation(anim_bone.translations[anim_frame])
                       @ mat_rot @ morpheme_space)
            mat_final = (Matrix.Translation(rest_translation + mat_add.to_translation())
                         @ (rest_rotation @ mat_add.to_3x3()).to_4x4())
            mat_basis = correction_inv @ (mat_rest_inv @ mat_final) @ correction
        else:
            # The stored translation is the bone's offset from its parent on
            # the skeleton the clip was made for: used as it is when that's
            # this armature, otherwise moved by source_offset to this
            # armature's proportions (see top of file: SOURCE SKELETON).
            # Ignore Face Bones: the face bones keep this armature's own rest
            # offset instead.
            if ignore_translation:
                mat_bone = morpheme_space_inv @ mat_rot @ morpheme_space
                mat_bone.translation = rest_translation
            else:
                mat_bone = (morpheme_space_inv @ Matrix.Translation(anim_bone.translations[anim_frame])
                            @ mat_rot @ morpheme_space)
                if source_offset is not None:
                    mat_bone.translation = mat_bone.translation + source_offset
            mat_basis = correction_inv @ (mat_rest_inv @ mat_bone) @ correction

        location, rotation, _ = mat_basis.decompose()
        # q and -q are the same orientation, but Blender interpolates a
        # quaternion's components one by one, so two neighbouring keys of
        # opposite sign swing the bone the long way round between them
        # (e.g. Root turning ~200° around frame 5 of bmnnew's
        # cb_dive_roll_pistol_stand_left_01). Keep each key on the same
        # side as the one before.
        if rotations and rotation.dot(rotations[-1]) < 0.0:
            rotation.negate()
        locations.append(location)
        rotations.append(rotation)
    return locations, rotations, mat_basis


def _finish_action(animation_data, action_name, additive, as_nla_track):
    # type: (bpy.types.AnimData, str, bool, bool) -> None
    """With NLA tracks (see top of file), moves the new action into its own track: additive ones on top of everything."""
    if not as_nla_track:
        return
    if additive:
        track = animation_data.nla_tracks.new()  # on top of everything
        track.name = action_name
        _move_active_action_to_nla(animation_data, track, 'COMBINE')
    else:
        _move_active_action_to_nla(animation_data, _new_base_track(animation_data, action_name), 'REPLACE')


def build(operator, context, filepath, jba):
    # type: (Operator, Context, str, JointBoneAnimation) -> bool
    ob = context.active_object
    if not ob or ob.type != 'ARMATURE':
        operator.report({'ERROR'}, NO_ARMATURE_MESSAGE)
        return False

    animated_bones = [b for b in jba.bones if b.name in ob.pose.bones]
    if not animated_bones:
        operator.report({'ERROR'}, f"None of the animation's {len(jba.bones)} bones exist on \'{ob.name}\'. "
                                   "Select the skeleton this animation is for.")
        return False

    anim_name = path_split(filepath)[:-4]

    # Sample i is at i / jba.fps seconds, which is placed at that time in the
    # scene's frame rate, whatever it is (see top of file: TIMING).
    scene = context.scene
    if operator.set_scene_fps:
        scene.render.fps = 30
        scene.render.fps_base = 1.0
    scene_fps = scene.render.fps / scene.render.fps_base
    frames_per_sample = scene_fps / jba.fps if jba.fps > 0 else 1.0
    frames = [1 + anim_frame * frames_per_sample for anim_frame in range(jba.num_frames)]

    if not ob.animation_data:
        ob.animation_data_create()
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode='POSE')

    scale = _morpheme_scale(operator, context, ob)
    morpheme_space, morpheme_space_inv = _morpheme_spaces(scale)
    additive = _is_additive(operator, jba, ob, morpheme_space_inv, anim_name)

    # Broken frames (see top of file). Additive clips are never touched:
    # some deliberately hold a different pose on every frame.
    if operator.repair_broken_frames and not additive:
        _repair(operator, jba, ob, morpheme_space, morpheme_space_inv, frames_per_sample, anim_name)

    # The skeleton the clip was made for (see top of file). Additive clips hold
    # offsets, not a skeleton's proportions, so they're never converted.
    rotation_corrections = {}
    if operator.fit_to_armature and not additive:
        source_offsets = _source_offsets(operator, context, filepath, jba, ob, scale, anim_name, rotation_corrections)
    else:
        source_offsets = {}

    # The game's action list (see top of file: ACTION LIST)
    action_list = _action_list(context, filepath, ob)
    action_info = action_list.get(anim_name) if action_list else None
    mask_name = action_list.mask_for(anim_name) if action_list else None
    mask_bones = None
    if getattr(operator, "masked_bones_only", False) and mask_name and not additive:
        mask_bones = _masked_bones(operator, context, ob, mask_name, animated_bones, anim_name)

    # A masked import is named as such, so it's never mistaken for the whole
    # animation: "em_wave_[masked_upperbody]".
    action_name = f"{anim_name}_[masked_{mask_name}]" if mask_bones else anim_name
    animation_data = ob.animation_data
    action = _new_action(animation_data, action_name, additive, operator.import_as_nla)
    if mask_bones:
        action["swtor_masked"] = mask_name

    # Keyframes go into the active action first; keep NLA's keyframe
    # remapping out of it while they're inserted.
    saved_blend = (animation_data.action_blend_type, animation_data.action_influence)
    animation_data.action_blend_type = 'REPLACE'
    animation_data.action_influence = 1.0

    # One bone at a time, its keys written in bulk (see _key_channels()). Each
    # pose bone is left showing the last frame, as keyframe_insert() would.
    key_types = _new_key_types(context)
    for anim_bone in animated_bones:
        if mask_bones and anim_bone.name not in mask_bones:
            continue
        pose_bone = ob.pose.bones[anim_bone.name]
        ignore_translation = (not additive and operator.ignore_facial_bones
                              and anim_bone.name.lower().startswith("fc_"))
        locations, rotations, mat_basis = _bone_keys(
            anim_bone, pose_bone, additive, ignore_translation, source_offsets.get(anim_bone.name),
            rotation_corrections.get(anim_bone.name), morpheme_space, morpheme_space_inv)

        pose_bone.matrix_basis = mat_basis
        _key_channels(action, ob, key_types, pose_bone.path_from_id("location"), pose_bone.name, frames, locations)
        if not (operator.delete_180 and anim_bone.name in KNOWN_180_FLIP_BONES):
            _key_channels(action, ob, key_types, pose_bone.path_from_id("rotation_quaternion"), pose_bone.name,
                          frames, rotations)

    # Delete 180º Rotation: unlike the rest-pose correction (KNOWN_180_FLIP_BONES,
    # always applied, which keeps an unanimated Bip01 from coming out flipped),
    # this drops a clip's genuinely animated ~180° character turn (SWTOR turns
    # characters to face away from the camera), which can wreck cloth and
    # physics simulations. Bip01's rotation isn't keyed at all (above), and
    # is set to identity for the whole clip.
    if operator.delete_180:
        for bone_name in KNOWN_180_FLIP_BONES:
            if bone_name in ob.pose.bones:
                ob.pose.bones[bone_name].rotation_quaternion = (1, 0, 0, 0)

    _key_world_motion(operator, ob, jba, morpheme_space, morpheme_space_inv, frames_per_sample, key_types)

    if action_info is not None:
        _apply_action_info(operator, action, action_info, mask_name, frames, anim_name)

    animation_data.action_blend_type, animation_data.action_influence = saved_blend
    _finish_action(animation_data, action_name, additive, operator.import_as_nla)

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

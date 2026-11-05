# <pep8 compliant>

"""
This script imports Star Wars: The Old Republic models into Blender.

Usage:
Run this script from "File->Import" menu and then load the desired GR2 model file.

https://github.com/SWTOR-Slicers/WikiPedia/wiki/GR2-File-Structure
(32-bit version. 64-bit version is implemented in the Add-on but not documented)

About the Add-on's structure and entry points:

class ImportJBA
    invoke()       <- Called by File->Import menu option
    Execute()      <- Called by other Add-ons and scripts.
        Load()     <- Called by other modules in this Add-on.
            Read()
            Build()
"""

import json  # For dict-to-stringProperty translations 
import os
import re
from math import pi as PI
from pathlib import Path
from tracemalloc import start
from typing import Optional, Set
import time

import bpy
from bpy import app
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, StringProperty
from bpy.types import Context, Operator, OperatorFileListElement
# from bpy_extras.io_utils import ImportHelper
from mathutils import Color, Matrix, Vector

from .process_materials import apply_materials_by_name_after_import
from ..types.gr2 import Granny2
from ..utils.binary import ArrayBuffer, DataView
from ..utils.number import decodeHalfFloat
from ..utils.string import readString

from ..types.shared import job_results  # add-on-wide global-like dict


BLENDER_VERSION = app.version


# Some bones lists for cosmetic differentiation
hook_bones = ['Camera',
              'NamePlate',
              'attach_nameplate_fallback',
              'socket_saber_right',
              'socket_saber_left',
              'socket_pistol_right',
              'socket_pistol_left',
              ]


class ImportGR2(Operator):
    """
    Import SWTOR GR2 file format (.gr2)
    
    Produces a file browser for manually
    selecting one or multiple .gr2 object
    files, including skeleton ones.
    """
    bl_idname = "import_mesh.gr2"  # DO NOT CHANGE
    bl_description = "Import SWTOR game objects and skeletons (armatures).\n\n• Compatible with both SWTOR 32 and 64-bit files\n   (before and after Game Update 7.2.1).\n\n• Can import selections of multiple files at once"
    bl_label = "Import SWTOR (.gr2)"
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
    
    if BLENDER_VERSION < (2, 82, 0):
        directory = StringProperty(subtype='DIR_PATH')
    else:
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

    import_collision: BoolProperty(
        name="Import Collision Mesh",
        description="Imports the object's collision boundary mesh if present in the file\n(it can be of use when exporting models to other apps and game engines)",
        default=False,
    )

    name_as_filename: BoolProperty(
        name="Name As Filename",
        description="Names the imported Blender objects with their filenames instead of their\ninternal 'art names' (the object's mesh data always keeps the 'art name').\n\n.gr2 objects' internal 'art names' typically match their files' names,\nbut sometimes they difer, which can break some tools and workflows.\n\nIn case of multiple mesh files (containing a main object plus secondary ones\nand / or Engine Objects such as Colliders), only the main object is renamed",
        # default=True,
    )

    apply_axis_conversion: BoolProperty(
        name="Axis Conversion",
        description="Permanently converts the imported object\nto Blender's 'Z-is-Up' coordinate system\nby 'applying' a x=90º rotation.\n\nNOT RECOMMENDED FOR MODDING SWTOR.\n\nSWTOR's coordinate system is 'Y-is-up' vs. Blender's 'Z-is-up'.\nTo compensate for that in a reversible manner, this importer\nnormally sets the object's rotation to X=90º at the Object level.\n\nAs this can be a nuisance outside a modding use case,\nthis option applies it at the Mesh level, instead",
        default=False,
    )

    scale_object: BoolProperty(
        name="Scale Object",
        description="Scales imported objects, characters and armatures\nat the Mesh level.\n\nNOT RECOMMENDED FOR MODDING SWTOR OR EXPORTING\nTO OTHER APPS AND ENGINES: Check their requirements and test first.\n\nSWTOR sizes objects in decimeters, while Blender defaults to meters.\nThis mismatch, while innocuous, is an obstacle when doing physics\nsimulations, automatic weighting from bones, or other processes\nwhere Blender requires real world-like sizes to succeed",
        default=False,
    )

    scale_factor: FloatProperty(
        name="Scale factor",
        description="Recommended values are:\n\n- 10 for simplicity (characters are superhero-like tall, over 2 m.).\n- Around 8 for accuracy (characters show more realistic heights).\n\nRemember that, if binding to a skeleton later on, the skeleton\nmust match the scale of the objects to work correctly (which\ncan be done on import, or manually afterwards)",
        min = 1.0,
        max = 100.0,
        soft_min = 1.0,
        soft_max = 10.0,
        step = 10,
        precision = 2,
        default=1.0,
    )

    blender_friendly_skeleton: BoolProperty(
        name="Blender Friendly Skeleton",
        description="SKELETON (.gr2) FILES ONLY. DO NOT USE. Does not move, resize, or otherwise\nreshape the original 'Original Skeleton Import' data -- bone head,\ntail and length are all untouched.\n\nRecalculates bone roll consistently across the whole skeleton (instead\nof keeping BioWare's original, per-bone roll values, which are not\naligned to any common axis). This is what makes Local-space rotation\nbehave predictably (no unexpected skewing) and makes X-Mirror editing\nand posing work correctly between Left/Right bone pairs.\n\nEach bone's original roll (in radians) is stored as a custom property\n('gr2_original_roll') so that tools such as .jba animation import can\nstill compensate for the difference later on",
        default=False,
    )

    apply_materials_by_name: BoolProperty(
        name="Apply Materials By Name",
        description="After import, looks up each material's name against a real .mat file in the\nResources Directory (or Legacy Resources Directory) and builds its native SWTOR\nshader automatically.\n\nMaterials with no matching .mat file are left alone, no error -- plenty of\nlegitimate .gr2 materials won't have one.\n\nRequires a Resources Directory to be set in this add-on's Preferences",
        default=False,  # seeded from add-on Preferences in invoke()
    )

    enforce_neutral_settings: BoolProperty(
        name="Enforce Neutral Settings",
        description="Temporarily overrides this Add-on's settings\nwith those of older versions for compatibility with older tools",
        options={'HIDDEN'},
        default=False,
    )

    job_results_rich: BoolProperty(
        name="Rich results Info",
        description="For easier interaction with third party code, this add-on fills 'bpy.context.scene.swtor_io_last_job'\nwith info about its most recent job (imported objects's names) in .json format.\nSet to Rich, it provides additional data such as their relationships to filenames",
        options={'HIDDEN'},
        default=False,
    )

    job_results_accumulate: BoolProperty(
        name="Accumulate Jobs' Results Info between ImportGR2 calls",
        description="By default, .gr2 import requests will clear the information about previous ones.\nThis option lets them accumulate, needed for cases such as a Character Import where\nImportGR2 is called several times in succession",
        options={'HIDDEN'},
        default=False,
    )



    def invoke(self, context, event):
        # To be able to set the class' properties to the values in the
        # Add-on's preferences and show them in the File Browser's options,
        # we use an Invoke function instead of directly using ImportHelper in
        # the class definition, to be able to put there the required code.              
        
        prefs = context.preferences.addons["swtor_io_tools"].preferences
        
        self.import_collision       = prefs.gr2_import_collision
        self.name_as_filename       = prefs.gr2_name_as_filename
        self.apply_axis_conversion  = prefs.gr2_apply_axis_conversion
        self.scale_object           = prefs.gr2_scale_object
        self.scale_factor           = prefs.gr2_scale_factor
        self.blender_friendly_skeleton = prefs.gr2_blender_friendly_skeleton
        self.apply_materials_by_name = prefs.gr2_apply_materials_by_name_default
        self.job_results_rich       = False
        self.job_results_accumulate = False

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

        header, body = layout.panel("SWTOR_IO_GR2_options", default_closed=False)
        header.label(text="Import Options")
        if body:
            body.prop(self, "apply_materials_by_name")
            body.prop(self, "name_as_filename")
            body.prop(self, "import_collision")
            body.prop(self, "blender_friendly_skeleton")

        # Not recommended for modding, so a sub-panel that starts collapsed.
        # body is None while the sub-panel is collapsed.
        header, body = layout.panel("SWTOR_IO_GR2_transform", default_closed=True)
        header.label(text="Import Axis & Scale")
        if body:
            body.prop(self, "apply_axis_conversion")
            body.prop(self, "scale_object")
            body.prop(self, "scale_factor")


    def execute(self, context):
        # type: (Context) -> Set[str]

        if not self.job_results_accumulate:
            job_results['objs_names'] = []
            job_results['files_objs_names'] = {}

        job_results['job_origin'] = self.bl_idname
                    
        if self.job_results_rich and not 'files_objs_names' in job_results:
            job_results['files_objs_names'] = {}


        paths = [os.path.join(self.directory, file.name) for file in self.files if file.name.lower().endswith(self.filename_ext)]

        if not paths:
            paths.append(self.filepath)

        # Clear filebrowser-related properties now
        # that they have been read and have no more
        # use so that they don't persist if the class
        # breaks before finishing its execution
        # (it makes debugging difficult, otherwise).
        self.files.clear()
        self.filepath = ""

        print()

        for path in paths:
            if not load(self, context, path):
                return {'CANCELLED'}

        bpy.context.scene.swtor_io_last_job = json.dumps(job_results)

        return {"FINISHED"}


def read(operator, filepath):
    # type: (Operator, str) -> Optional[Granny2]
    with open(filepath, 'rb') as file:
        buffer = ArrayBuffer()
        buffer.fromfile(file, os.path.getsize(filepath))

    dv = DataView(buffer)
    pos = 0

    # Cancel import if this is not a BioWare Austin / SWTOR GR2 file.
    if dv.getUint32(pos, 1) != 0x42574147:
        operator.report({'ERROR'}, f"{filepath} is not a valid SWTOR gr2 file.")
        return None

    gr2 = Granny2()

    pos = 4

    gr2.version = dv.getUint32(pos, 1)                   # GR2 file version

    pos = 12  # 0x0C
    gr2.offset_BNRY = dv.getUint32(pos, 1)                # Offset of the appended BNRY/collision blob, or 0

    pos = 20  # 0x14, skipping the version numbers, magic numbers and collision offset

    # GR2 file type, 0 = geometry, 1 = geometry with .clo file, 2 = skeleton
    gr2.type_flag = dv.getUint32(pos, 1)
    pos += 4
    num_meshes = dv.getUint16(pos, 1)                    # Number of meshes in this file
    pos += 2
    num_materials = dv.getUint16(pos, 1)                 # Number of materials in this file
    pos += 2
    num_bones = dv.getUint16(pos, 1)                     # Number of bones in this file
    pos += 2
    num_attachment_bones = dv.getUint16(pos, 1)          # Number of attachment bones in this file
    pos += 2

    pos += 16

    # gr2.bounds = Granny2.BoundingBox([dv.getFloat32(pos + (i * 4), 1) for i in range(8)])
    pos += 32



    offset_mesh_header = None
    offset_material_name_offsets = None
    offset_bone_struct = None
    offset_attachment_bones = None
    # 0x50
    # skipping offset CachedOffset as we don't use it
    if gr2.version == 5:
        pos += 8  # 0x54
        offset_mesh_header = dv.getUint64(pos, 1)            # Mesh header offset address
        pos += 8
        offset_material_name_offsets = dv.getUint64(pos, 1)  # Material header offset address
        pos += 8
        offset_bone_struct = dv.getUint64(pos, 1)            # Bone structure offset address
        pos += 8
        offset_attachment_bones = dv.getUint64(pos, 1)       # Attachment bone table offset address
        pos += 8
    else:	
        pos += 4  # 0x54
        offset_mesh_header = dv.getUint32(pos, 1)            # Mesh header offset address
        pos += 4
        offset_material_name_offsets = dv.getUint32(pos, 1)  # Material header offset address
        pos += 4
        offset_bone_struct = dv.getUint32(pos, 1)            # Bone structure offset address
        pos += 4
        offset_attachment_bones = dv.getUint32(pos, 1)       # Attachment bone table offset address
        pos += 4


    # Meshes
    gr2.mesh_buffer = {}

    mesh_bin_size = 40

    if gr2.version == 5:
        mesh_bin_size = 64

    for i in range(num_meshes):
        pos = offset_mesh_header + (i * mesh_bin_size)
        # Mesh name

        mesh = None

        if gr2.version == 5:
            mesh = Granny2.Mesh(readString(dv, pos, posOverride= dv.getUint64(pos, True)))
            pos += 8
        else:
            mesh = Granny2.Mesh(readString(dv, pos))
            pos += 4
        
        # operator.report({'INFO'}, f"Read the header for mesh {mesh.name}... {pos}")  # for diagnostics

        # LOD (FFFF = generated_collision, FFFD = occluder, 0000 = normal, 0002+ = LOD level)
        mesh.lod = dv.getInt16(pos, 1)
        pos += 2
        # BitFlag1 (0x2000 = collision, 0x8000 = static)
        mesh.raw_bit_flag1 = dv.getUint16(pos, 1)
        pos += 2
        # Number of sub meshes that make up this mesh
        num_pieces = dv.getUint16(pos, 1)
        pos += 2
        # Number of bones used by this mesh
        num_used_bones = dv.getUint16(pos, 1)
        pos += 2
        # BitFlag2

        bit_flag2 = None
        vertex_size = None

        if gr2.version == 5:
            bit_flag2 = dv.getUint32(pos, 1)
            pos += 4
            # 12 = collision, 24 = static, 32+ = dynamic
            vertex_size = dv.getUint32(pos, 1)
            pos += 4
        else:
            bit_flag2 = dv.getUint16(pos, 1)
            pos += 2
            # 12 = collision, 24 = static, 32+ = dynamic
            vertex_size = dv.getUint16(pos, 1)
            pos += 2

        # Total number of vertices used by this mesh
        num_vertices = dv.getUint32(pos, 1)
        pos += 4
        # Total number of polygons used by this mesh
        num_polygons = dv.getUint32(pos, 1)
        pos += 4

        if gr2.version == 5:
            # Offset of the vertices buffer for this mesh
            mesh.offset_vertex_buffer = dv.getUint64(pos, 1)
            pos += 8
             # Offset of the sub mesh header(s)
            mesh.offset_piece_headers = dv.getUint64(pos, 1)
            pos += 8
            # Offset of the indices buffer for this mesh
            mesh.offset_indices_buffer = dv.getUint64(pos, 1)
            pos += 8
            # Offset of the bones buffer for this mesh
            mesh.offset_bones_buffer = dv.getUint64(pos, 1)
            pos += 8
        else:
            mesh.offset_vertex_buffer = dv.getUint32(pos, 1)
            pos += 4
            mesh.offset_piece_headers = dv.getUint32(pos, 1)
            pos += 4
            mesh.offset_indices_buffer = dv.getUint32(pos, 1)
            pos += 4
            mesh.offset_bones_buffer = dv.getUint32(pos, 1)
            pos += 4

        # Sub mesh header(s)
        mesh.piece_header_buffer = {}
        for j in range(num_pieces):
            pos = mesh.offset_piece_headers + (j * 48)

            piece = Granny2.Piece()

            piece.offset_indices = dv.getUint32(pos, 1)     # Relative offset for this piece's faces
            pos += 4
            piece.num_polygons = dv.getUint32(pos, 1)       # Number of faces used by this piece
            pos += 4
            piece.material_index = dv.getUint32(pos, 1)     # Mesh piece material id
            pos += 4
            piece.index = dv.getUint32(pos, 1)              # Mesh piece enumerator (1 x uint32)
            pos += 4
            # piece.bounds = Granny2.BoundingBox(           # Bounding box (8 x 4 bytes)
            #     [dv.getFloat32(pos + (k * 4), 1) for k in range(8)])
            pos += 32

            mesh.piece_header_buffer[j] = piece

        # Vertex buffer
        mesh.vertex_buffer = {}
        for j in range(num_vertices):
            pos = mesh.offset_vertex_buffer + (j * vertex_size)

            vertex = Granny2.Vertex([dv.getFloat32(pos + (k * 4), 1) for k in range(3)])
            pos += 12

            if bit_flag2 & 256:  # 0x100
                vertex.bone_weights = Vector([dv.getUint8(pos + k) for k in range(4)])
                pos += 4
                vertex.bone_indices = Vector([dv.getUint8(pos + k) for k in range(4)])
                pos += 4

            if bit_flag2 & 2:  # 0x02
                vertex.normals = Vector(
                    [(dv.getUint8(pos + k) - 127) / 127 for k in range(4)])
                pos += 4
                vertex.tangents = Vector(
                    [(dv.getUint8(pos + k) - 127) / 127 for k in range(4)])
                pos += 4

            if bit_flag2 & 16:  # 0x10
                vertex.color = Color([dv.getUint8(pos + k) for k in range(3)])
                pos += 4

            if bit_flag2 & 32:  # 0x20
                vertex.uv_layer0 = Vector(
                    [decodeHalfFloat(dv.getUint16(pos + (k * 2), 1)) for k in range(2)])
                pos += 4

            if bit_flag2 & 64:  # 0x40
                vertex.uv_layer1 = Vector(
                    [decodeHalfFloat(dv.getUint16(pos + (k * 2), 1)) for k in range(2)])
                pos += 4

            if bit_flag2 & 128:  # 0x80
                vertex.uv_layer2 = Vector(
                    [decodeHalfFloat(dv.getUint16(pos + (k * 2), 1)) for k in range(2)])

            mesh.vertex_buffer[j] = vertex

        # Indices buffer
        mesh.indices_buffer = {}
        for j in range(int(num_polygons / 3)):
            pos = mesh.offset_indices_buffer + (j * 6)
            mesh.indices_buffer[j] = tuple([dv.getUint16(pos + (k * 2), 1) for k in range(3)])

        # Bone(s) buffer

        boneSize = 32 if gr2.version == 5 else 28

        mesh.bone_buffer = {j: Granny2.Bone(dv, mesh.offset_bones_buffer + (j * boneSize), gr2.version, True)
                            for j in range(num_used_bones)}

        gr2.mesh_buffer[i] = mesh

    # Materials
    # NOTE: I wish there was a more efficient way to do this!
    gr2.material_names = {}
    if num_materials:
        pos = offset_material_name_offsets
        for i in range(num_materials):

            if gr2.version == 5:
                gr2.material_names[i] = readString(dv, pos, posOverride=dv.getUint64(pos, 1))
                pos += 8
            else:
                gr2.material_names[i] = readString(dv, pos)
                pos += 4
    else:
        count = 0
        for mesh in gr2.mesh_buffer.values():
            if mesh.bit_flag2 & 32:
                for j in mesh.piece_header_buffer.keys():  # Use "mesh name".00x for name
                    gr2.material_names[count] = f"{mesh.name}.{j:03d}"
                    count += 1

    # Skeleton Bones

    bone_size_of_mem = 144 if gr2.version == 5 else 136

    gr2.bone_buffer = {i: Granny2.Bone(dv, offset_bone_struct + (i * bone_size_of_mem), gr2.version)
                       for i in range(num_bones)}

    # Attachment bones (VFX/socket points, e.g. weapon or muzzle attach points).
    # These only ever occur in mesh/geometry files, never in skeleton files, and are
    # not (yet) represented in Blender beyond round-trip storage on export objects
    # (see swtor_attachment_bones custom property in build()).
    gr2.attachment_bones = []
    if num_attachment_bones > 0 and offset_attachment_bones:
        pos = offset_attachment_bones
        for _ in range(num_attachment_bones):
            if gr2.version == 5:
                name_offset = dv.getUint64(pos, 1)
                pos += 8
                bone_name_offset = dv.getUint64(pos, 1)
                pos += 8
            else:
                name_offset = dv.getUint32(pos, 1)
                pos += 4
                bone_name_offset = dv.getUint32(pos, 1)
                pos += 4

            attachment_name = readString(dv, name_offset, posOverride=name_offset)
            attachment_bone_name = readString(dv, bone_name_offset, posOverride=bone_name_offset)
            matrix = [dv.getFloat32(pos + (k * 4), 1) for k in range(16)]
            pos += 64

            gr2.attachment_bones.append({
                "name": attachment_name,
                "bone": attachment_bone_name,
                "matrix": matrix,
            })

    # BNRY (embedded collision) summary. We deliberately do NOT cache the raw blob
    # bytes here -- meshes get edited, and a stale byte-perfect passthrough would be
    # actively misleading. This is just enough to let the exporter (or a future
    # validation step) warn that a cached collision reference exists and may now be
    # out of date. Full BNRY read/regeneration is a separate, later task.
    gr2.bnry_summary = None
    if gr2.offset_BNRY and gr2.offset_BNRY < dv.byteLength:
        bnry_length = dv.getUint32(gr2.offset_BNRY, 1)
        if bnry_length != 0:
            pos = gr2.offset_BNRY + 8  # skip bnryLength (4) + "BNRY" magic (4)
            pos += 4  # skip big-endian version word (always 2)
            pos += 4  # skip "LTLE" marker
            pos += 8  # skip const1 (always 1), const2 (always 2)
            num_pieces = dv.getUint32(pos, 1)
            pos += 8  # skip numPieces (just read) + runtimeTotalBytes
            num_bih_nodes = dv.getUint32(pos, 1)
            pos += 4  # skip numBihNodes (just read)
            num_triangles = dv.getUint32(pos, 1)

            gr2.bnry_summary = {
                "bnryLength": bnry_length,
                "numPieces": num_pieces,
                "numBihNodes": num_bih_nodes,
                "numTriangles": num_triangles,
            }

    return gr2

# A LOD level in a mesh / object name: "LOD[2]", "..._LOD[0]", "lod_01", "lod0".
# The "lod" must not be the tail of a longer word ("cold1", "plod2" don't count).
_LOD_NAME_RE = re.compile(r"(?<![a-z])lod[\[_]?(\d+)")


def lod_name_info(name):
    # type: (str) -> tuple
    """
    (is_lod, is_lod0) for one mesh / object name. Recognises the original
    "_lod" / "_lod0" substrings plus the naming real files actually use:
    "LOD[2]", "bundle5_LOD[1]", "lod_01", "lod_0". Shared by build()'s
    main-mesh pick below and by Area Assembler's LOD reducer
    (ops/import_area.py), so both agree on what counts as a LOD.
    """
    name = name.lower()
    is_lod = "_lod" in name
    is_lod0 = "_lod0" in name
    match = _LOD_NAME_RE.search(name)
    if match:
        is_lod = True
        if int(match.group(1)) == 0:
            is_lod0 = True
    return is_lod, is_lod0


# SpeedTree-style LOD suffix: "tyt_speedtree_main_pine_dying_l0", "..._l1", "..._l2".
_LOD_SUFFIX_RE = re.compile(r"^(.*)_l(\d+)(?:\.\d+)?$")


def sibling_lod_flags(names):
    # type: (list) -> list
    """
    [(is_lod, is_lod0)] for each of one file's mesh names, for the "<base>_l<N>"
    LOD naming that carries no "lod" in it at all. A "_l1" ending on its own
    proves nothing (left panels, lamp 1...), so a name only counts when
    SIBLINGS confirm it: at least two names in the same list share its base
    and differ in the number ("pine_l0", "pine_l1", "pine_l2"). Blender's own
    ".001" duplicate suffix is ignored.
    """
    groups = {}
    for position, name in enumerate(names):
        match = _LOD_SUFFIX_RE.match(name.lower())
        if match:
            groups.setdefault(match.group(1), []).append((position, int(match.group(2))))
    flags = [(False, False)] * len(names)
    for members in groups.values():
        if len({level for _, level in members}) >= 2:
            for position, level in members:
                flags[position] = (True, level == 0)
    return flags


def main_mesh_index(names):
    # type: (list) -> Optional[int]
    """
    Which of a .gr2's meshes (`names` = their art names, in file order, with
    any skipped collision mesh already left out) is "the" model -- the one the
    file's own name belongs to under Name As Filename.

    The LOD0 mesh if the file has one (a multi-LOD file isn't necessarily
    stored LOD0-first: ond_env_plant_bundle_001 is stored LOD[1], LOD[2],
    LOD[0]); otherwise the first mesh, as the importer always assumed.
    None for an empty list.
    """
    if not names:
        return None
    siblings = sibling_lod_flags(names)
    for index, name in enumerate(names):
        if lod_name_info(name)[1] or siblings[index][1]:
            return index
    return 0


def main_mesh_key(mesh_buffer, import_collision):
    # type: (dict, bool) -> Optional[int]
    """
    The key, in a .gr2's mesh_buffer, of the mesh that gets the file's name
    under Name As Filename: main_mesh_index() over the meshes that will
    actually become objects (a collision mesh is skipped unless
    import_collision). None if there is none.
    """
    included_keys = [key for key, mesh in mesh_buffer.items()
                     if not ("collision" in mesh.name and not import_collision)]
    position = main_mesh_index([mesh_buffer[key].name for key in included_keys])
    return included_keys[position] if position is not None else None


def build(gr2,
          filepath="",
          import_collision        = None,
          name_as_filename        = None,
          scale_object            = None,
          scale_factor            = None,
          apply_axis_conversion   = None,
          blender_friendly_skeleton = False,
          ):
    # type: (Granny2, str, bool, bool, bool, float, bool, bool) -> None

    # Data that will be used for publishing results
    # via scene props to other add-ons
    resulting_single_mesh_blender_objects = []
    
    # NOTE: Create Materials
    for i, material in gr2.material_names.items():
        bpy.data.materials.new(name=material).use_nodes = True

    # NOTE: Create Meshes
    # Which mesh is the file's main one (named after the file, if asked to).
    main_key = main_mesh_key(gr2.mesh_buffer, import_collision)

    for i, mesh in gr2.mesh_buffer.items():

        # When using the name_as_filename option, in the case of multiple mesh
        # models/files (which get separated into multiple objects, as Blender
        # doesn't support more than one mesh data block per object) only the
        # main mesh is named after the file: the LOD0 mesh if there is one,
        # else the first one (see main_mesh_index()).
        use_file_name_as_object_name = (i == main_key and name_as_filename is True)


        if "collision" in mesh.name and not import_collision:
            continue

        blend_mesh = bpy.data.meshes.new(mesh.name)
        blend_mesh.from_pydata([vert.position for vert in mesh.vertex_buffer.values()],
                          [],
                          [index for index in mesh.indices_buffer.values()])
        
        # Original (file-exact) material names in slot order. Blender renames
        # data-blocks on name collision (e.g. a second "default" material becomes
        # "default.001"), which is common across SWTOR assets, so this is captured
        # separately rather than trusted to bpy.data.materials[...].name later.
        original_material_names = []

        if mesh.bit_flag2 & 32:  # 0x20
            # Link Materials
            material_indices = []
            for j, piece in mesh.piece_header_buffer.items():
                material = gr2.material_names[j if piece.material_index == 4294967295 else piece.material_index]
                blend_mesh.materials.append(bpy.data.materials[material])
                original_material_names.append(material)

                for _ in range(piece.num_polygons):
                    material_indices.append(j)


        if BLENDER_VERSION < (4, 1, 0):
            
            if mesh.bit_flag2 & 2:   # 0x02
                # NOTE: We store 'temp' normals in loops, since validate() may alter final mesh,
                #       we can only set custom loop normals *after* calling it.
                blend_mesh.create_normals_split()  # (DEPRECATED IN BLENDER 4.1)
                    
                # UV stuff
                blend_mesh.uv_layers.new(do_init=False)
                if mesh.bit_flag2 & 64:  # 0x40
                    blend_mesh.uv_layers.new(do_init=False)
                if mesh.bit_flag2 & 128:  # 0x80
                    blend_mesh.uv_layers.new(do_init=False)


                for j, polygon in enumerate(blend_mesh.polygons):
                    loop_indices = polygon.loop_indices

                    for k, loop_index in enumerate(loop_indices):
                        v = mesh.vertex_buffer[mesh.indices_buffer[j][k]]
                        
                        # We store the temp normals in each loop's normal attribute directly.
                        # This, also, normalizes the normal vectors "for free".
                        # (DEPRECATED IN BLENDER 4.1 where blend_mesh.loops[loop_index].normal is read-only)
                        blend_mesh.loops[loop_index].normal = [v.normals.x, v.normals.y, v.normals.z]
                        
                        # UV stuff
                        blend_mesh.uv_layers[0].data[loop_index].uv = [v.uv_layer0.x, 1 - v.uv_layer0.y]
                        if mesh.bit_flag2 & 64:  # 0x40
                            blend_mesh.uv_layers[1].data[loop_index].uv = [v.uv_layer1.x, 1 - v.uv_layer1.y]
                        if mesh.bit_flag2 & 128:  # 0x80
                            blend_mesh.uv_layers[2].data[loop_index].uv = [v.uv_layer2.x, 1 - v.uv_layer2.y]

                    # Material stuff
                    polygon.material_index = material_indices[j]

                blend_mesh.validate(clean_customdata=False, verbose=True)

                print("-" * 20)

                # Retrieve the "validated" temp normals
                custom_loop_normals = [0.0] * (len(blend_mesh.loops) * 3)                
                blend_mesh.loops.foreach_get("normal", custom_loop_normals)
                
                # Apply them as split normals to the mesh
                blend_mesh.polygons.foreach_set("use_smooth", [True] * len(blend_mesh.polygons))                
                blend_mesh.normals_split_custom_set(
                    tuple(zip(*(iter(custom_loop_normals),) * 3))
                    )
                blend_mesh.use_auto_smooth = True  # (DEPRECATED IN BLENDER 4.1)

        else:
            
            if mesh.bit_flag2 & 2:   # 0x02
                # NOTE: We store 'temp' normals in loops, since validate() may alter final mesh,
                #       we can only set custom loop normals *after* calling it.
                # We use a custom attribute to store them because we can't use
                # the "normal" attribute: blend_mesh.loops[loop_index].normal is
                # read-only in 4.1 and higher.
                                                    
                # UV stuff
                blend_mesh.uv_layers.new(do_init=False)
                if mesh.bit_flag2 & 64:  # 0x40
                    blend_mesh.uv_layers.new(do_init=False)
                if mesh.bit_flag2 & 128:  # 0x80
                    blend_mesh.uv_layers.new(do_init=False)

                # For some reason, the creation of extra UV map layers for
                # multiUvExtraction objects blitzes the temp normals custom attribute,
                # so, we are creating it AFTER the creation of UV layers.
                temp_custom_normals = blend_mesh.attributes.new(
                    name="temp_custom_normals",
                    type='FLOAT_VECTOR',
                    domain='CORNER'
                    ).data


                for j, polygon in enumerate(blend_mesh.polygons):
                    loop_indices = polygon.loop_indices

                    for k, loop_index in enumerate(loop_indices):
                        v = mesh.vertex_buffer[mesh.indices_buffer[j][k]]
                        
                        # We normalize the vectors because storing them in custom attributes
                        # doesn't normalize them for free the way mesh.loops[loop_index].normal used to.
                        # As we are using for the normals data the mathutils' Vector type,
                        # we use its normalized() method (instead of normalize() which might be slower?).
                        # blend_mesh.loops[loop_index].normal = [v.normals.x, v.normals.y, v.normals.z]

                        # temp_custom_normals[loop_index].vector = v.normals.xyz.normalized()
                        temp_custom_normals[loop_index].vector = v.normals.xyz.normalized()
                                                
                        # UV stuff
                        blend_mesh.uv_layers[0].data[loop_index].uv = [v.uv_layer0.x, 1 - v.uv_layer0.y]
                        if mesh.bit_flag2 & 64:  # 0x40
                            blend_mesh.uv_layers[1].data[loop_index].uv = [v.uv_layer1.x, 1 - v.uv_layer1.y]
                        if mesh.bit_flag2 & 128:  # 0x80
                            blend_mesh.uv_layers[2].data[loop_index].uv = [v.uv_layer2.x, 1 - v.uv_layer2.y]

                    # Material stuff
                    polygon.material_index = material_indices[j]


                blend_mesh.validate(clean_customdata=False)


                # Retrieve the "validated" temp normals
                custom_loop_normals = [0.0] * (len(blend_mesh.loops) * 3)
                temp_custom_normals.foreach_get("vector", custom_loop_normals)
                
                # Apply them as split normals to the mesh
                blend_mesh.polygons.foreach_set("use_smooth", [True] * len(blend_mesh.polygons))
                blend_mesh.normals_split_custom_set(
                    tuple(zip(*(iter(custom_loop_normals),) * 3))
                    )



        # Create Blender Object
        if use_file_name_as_object_name:
            # Crude filepath separator-agnostic handling
            if "\\" in filepath:
                dir_sep = "\\"
            else:
                dir_sep = "/"
            file_name = filepath.split(dir_sep)[-1][:-4]
            ob = bpy.data.objects.new(file_name, blend_mesh)
        else:
            ob = bpy.data.objects.new(mesh.name, blend_mesh)

        resulting_single_mesh_blender_objects.append(ob.name)

        # --- Round-trip data, for the exporter to read/compare/verify later ---
        # Named gr2_* since these are BWAG file-format data specifically (see
        # import_scale / import_axis_conversion below for the separate convention
        # used by importer-override properties).
        # Per-mesh: values Blender has no native slot for, or that Blender's own
        # naming rules could silently corrupt before export.
        ob["gr2_original_name"] = mesh.original_name
        ob["gr2_material_names"] = json.dumps(original_material_names)
        ob["gr2_lod"] = mesh.lod
        ob["gr2_bit_flag1"] = mesh.raw_bit_flag1
        # File-level: replicated onto every sibling object produced from this same
        # source file, since the exporter operates per-object (one object = one
        # exported .gr2), and any one of them might be the one selected for export.
        ob["gr2_type_flag"] = gr2.type_flag
        ob["gr2_attachment_bones"] = json.dumps(gr2.attachment_bones)
        if gr2.bnry_summary is not None:
            ob["gr2_bnry_summary"] = json.dumps(gr2.bnry_summary)


        # Create Vertex Groups
        for bone in mesh.bone_buffer.values():
            ob.vertex_groups.new(name=bone.name)
            entry = ob.bone_bounds.add()
            entry.name = bone.name
            entry.bounds = bone.bounds

        # Populate Vertex Groups
        for j, vertex in mesh.vertex_buffer.items():
            if mesh.bit_flag2 & 256:
                for index in range(4):
                    bone = mesh.bone_buffer[vertex.bone_indices[index]].name
                    ob.vertex_groups[bone].add([j], float(vertex.bone_weights[index] / 255), 'ADD')

        # Link Blender Object
        bpy.context.collection.objects.link(ob)

        # Adjust the orientation of the model
        ob.matrix_local = Matrix.Rotation(PI * 0.5, 4, 'X')

        # Deselect all, then select imported model
        bpy.ops.object.select_all(action='DESELECT')
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        
        # Apply transformation options and record them
        # in custom object properties 
        if scale_object or apply_axis_conversion:
            if scale_object:
                ob.scale *= scale_factor
                
            bpy.ops.object.transform_apply(location=False, rotation=apply_axis_conversion, scale=scale_object, properties=True)
            
        ob["import_scale"] = scale_factor
        ob["import_axis_conversion"] = apply_axis_conversion
            


    # Create Armature
    if gr2.type_flag == 2 and len(gr2.bone_buffer) > 0:
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True)
        armature: bpy.types.Armature = bpy.context.object.data
        armature.name = filepath.split(os.sep)[-1][:-4]
        armature.display_type = 'STICK'
        bpy.context.object.show_in_front = True

        # Remove the default bone Blender auto-creates with object.add(type='ARMATURE'),
        # otherwise every gr2 bone index below ends up off-by-one against armature.edit_bones.
        for default_bone in list(armature.edit_bones):
            armature.edit_bones.remove(default_bone)

        # Bones creation
        for bone in gr2.bone_buffer.values():
            new_bone = armature.edit_bones.new(bone.name)
            new_bone.tail = [0, 0.00001, 0]

        # Bones hierarchy organization
        for i, bone in gr2.bone_buffer.items():
            armature_bone = armature.edit_bones[i]

            if bone.parent_index >= 0:
                armature_bone.parent = armature.edit_bones[bone.parent_index]

            matrix = Matrix([bone.root_to_bone[j*4:j*4+4] for j in range(4)])
            # print(matrix, i , bone.name)  # for diagnostics
            matrix.transpose()
            target = matrix.inverted()

            # Bone direction (head/tail) only here -- roll is set explicitly
            # below, not left to transform()'s own roll recalculation.
            #
            # WHY: EditBone.transform(matrix)'s automatic roll recompute does
            # not reliably reproduce root_to_bone's actual bind-pose
            # orientation for a bone whose direction isn't world +Y (i.e.
            # every bone except the first couple in a Y-up-authored chain,
            # since a freshly-created edit bone starts pointing +Y). It's
            # fine for direction; it just doesn't round-trip the target's
            # roll (local X/Z) correctly in the general case. Verified
            # empirically against a real 49-bone rig: transform()'s own roll
            # was off (by varying, bone-dependent amounts) for every bone
            # whose bind direction wasn't +Y, while align_roll() below
            # reproduces root_to_bone's rotation to float32 precision on
            # every bone, including those.
            #
            # align_roll(vector) points the bone's local +Z axis at the
            # given world-space vector, so we hand it root_to_bone's own Z
            # axis (target's third column) as that target.
            armature_bone.transform(target, roll=False)
            target_z_axis = Vector((target[0][2], target[1][2], target[2][2]))
            armature_bone.align_roll(target_z_axis)

        # "Blender Friendly Skeleton": roll cleanup.
        # SKELETON FILES ONLY. Does not move or resize any bone (head, tail,
        # and length are all untouched) -- only roll (local axis orientation)
        # is adjusted.
        if blender_friendly_skeleton:

            # Original roll is stored per-bone before we touch it, so that
            # other tools (e.g. a future .jba importer update) can still
            # compensate pose data authored against BioWare's original axes.
            for i, bone in gr2.bone_buffer.items():
                armature.edit_bones[i]["gr2_original_roll"] = armature.edit_bones[i].roll

            # --- Bone roll ---
            # Recalculated toward one consistent reference axis across the
            # whole skeleton (instead of BioWare's original per-bone roll,
            # which has no common alignment). This is what makes Local-space
            # rotation behave predictably (no unexpected skewing), and, as a
            # side effect, makes Left/Right bone pairs true mirror images of
            # each other, which is what X-Mirror editing/posing depends on.
            #
            # NOTE: this runs *before* the object-level 90d X rotation below
            # that converts SWTOR's Y-up to Blender's Z-up, so the skeleton
            # is still in its native Y-up space here -- "up" is +Y, not +Z.
            #
            # calculate_roll needs a numerically well-conditioned head->tail
            # vector to work; the hardcoded near-zero tail (1e-5) sits right
            # at Blender's internal minimum bone length and makes the
            # calculation unreliable/a no-op. Temporarily lengthen every
            # bone along its existing direction just for this calculation,
            # then shrink it straight back down afterward -- roll is stored
            # independently of tail position, so this has no lasting effect
            # on bone length.
            SCRATCH_LENGTH = 0.05
            for b in armature.edit_bones:
                direction = b.tail - b.head
                direction = direction.normalized() if direction.length > 1e-9 else Vector((0, 1, 0))
                b.tail = b.head + direction * SCRATCH_LENGTH

            for b in armature.edit_bones:
                b.select_head = True
                b.select_tail = True
                b.select = True
            armature.edit_bones.active = armature.edit_bones[0]
            bpy.ops.armature.calculate_roll(type='GLOBAL_POS_Y')
            for b in armature.edit_bones:
                b.select_head = False
                b.select_tail = False
                b.select = False

            # calculate_roll aligns every bone independently to the same
            # reference axis, which only gets Left/Right pairs *close* to
            # true mirror images of each other -- the source rig has a small
            # inherent asymmetry, so each side lands slightly differently.
            # That residual gap is enough for Blender's X-Mirror to treat
            # them as not-actually-mirrored. So: force each "Right*" bone's
            # orientation to be the EXACT mirror of its "Left*" counterpart,
            # on top of what calculate_roll already did.
            F = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))
            for b in armature.edit_bones:
                if not b.name.startswith('Left'):
                    continue
                right_name = 'Right' + b.name[len('Left'):]
                right_bone = armature.edit_bones.get(right_name)
                if right_bone is None:
                    continue
                mirrored = F @ b.matrix @ F
                mirrored.translation = right_bone.matrix.translation  # keep the right bone's own position
                right_bone.matrix = mirrored

            # Shrink every bone back down to the original hardcoded (near-
            # zero) length. Deliberately NOT restoring the original snapshot
            # here -- that would revert exactly the direction correction the
            # steps above just made (roll would stay "fixed" while the
            # actual head->tail direction silently reverted to the
            # original, un-mirrored one, giving a Left/Right pair whose
            # roll *looks* correctly negated but isn't actually consistent
            # with its own direction). Instead, keep whatever direction the
            # bone currently has, and only rescale it down to a dot.
            ORIGINAL_LENGTH = 0.00001
            for b in armature.edit_bones:
                direction = b.tail - b.head
                direction = direction.normalized() if direction.length > 1e-9 else Vector((0, 1, 0))
                b.tail = b.head + direction * ORIGINAL_LENGTH

        bpy.context.object.name = armature.name
        resulting_single_mesh_blender_objects.append(bpy.context.object.name)

        # File-level round-trip data (see mesh objects above for the full explanation).
        # Only file-level properties apply here -- an armature has no per-mesh
        # material/lod/bitflag data of its own.
        bpy.context.object["gr2_type_flag"] = gr2.type_flag
        bpy.context.object["gr2_attachment_bones"] = json.dumps(gr2.attachment_bones)
        if gr2.bnry_summary is not None:
            bpy.context.object["gr2_bnry_summary"] = json.dumps(gr2.bnry_summary)

        bpy.context.object.matrix_local = Matrix.Rotation(PI * 0.5, 4, 'X')
        
        # Apply transformation options and record them
        # in custom object properties 
        if scale_object:
            bpy.context.object.scale *= scale_factor
            bpy.context.object["import_scale"] = scale_factor
        else:
            bpy.context.object["import_scale"] = 1.0

        bpy.ops.object.mode_set(mode='OBJECT')

        # Same as for the meshes above: the scale is applied (baked into the bones), so
        # "import_scale" says how much the armature's data was scaled, not an Object scale.
        if apply_axis_conversion or scale_object:
            bpy.ops.object.select_all(action='DESELECT')
            ob=bpy.data.objects[armature.name]
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob
            bpy.ops.object.transform_apply(location=False, rotation=apply_axis_conversion, scale=scale_object, properties=True )
            ob["import_axis_conversion"] = apply_axis_conversion
        else:
            ob=bpy.data.objects[armature.name]
            ob["import_axis_conversion"] = False

        
    return resulting_single_mesh_blender_objects


def load(operator, context, filepath = ""):
    # type: (Operator, Context, str) -> bool
    """
    This is the operator called by all other tools (either in this Add-on
    or in other ones) for actual object importing operations. Externally,
    it is exposed as bpy.ops.import_mesh.gr2().
    
    The operator param is being passed the calling class' self.
    """

    # .gr2 data parsing and mesh assembling section
    mesh = read(operator, filepath=filepath)
    
    print(f"FILE: {filepath}")


    # Blender object from mesh section
    if mesh:

        start_time = time.time()
        
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        if operator.enforce_neutral_settings:
            objects_names = build(mesh,
                                  filepath              = filepath,
                                  import_collision      = False,
                                  # Neutral settings mean "as older tools expect", which
                                  # includes art names -- except when the calling tool
                                  # explicitly asks otherwise (Area Assembler's
                                  # "Name Objects As Filenames"); nothing else sets this.
                                  name_as_filename      = bool(getattr(operator, "area_name_as_filename", False)),
                                  scale_object          = False,
                                  scale_factor          = 1.0,
                                  apply_axis_conversion = False,
                                  blender_friendly_skeleton = False,
                                  )
        else:
            if operator.bl_idname == 'IMPORT_MESH_OT_gr2' and operator.options.is_invoke:
                # Called via Blender's Import Menu's Import .gr2 option:
                # use the possibly user-manually altered properties
                # exposed in the File Browser.
                # (operator was passed self.)
                objects_names = build(mesh,
                                      filepath              = filepath,
                                      import_collision      = operator.import_collision,
                                      name_as_filename      = operator.name_as_filename,
                                      scale_object          = operator.scale_object,
                                      scale_factor          = operator.scale_factor,
                                      apply_axis_conversion = operator.apply_axis_conversion,
                                      blender_friendly_skeleton = operator.blender_friendly_skeleton,
                                      )
            else:
                # Called via other Import Menu options (such as the
                # .json Character importer) or code not from this Add-on:
                # use the preferences' settings.
                prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
                objects_names = build(mesh,
                                      filepath              = filepath,
                                      import_collision      = prefs.gr2_import_collision,
                                      name_as_filename      = prefs.gr2_name_as_filename,
                                      scale_object          = prefs.gr2_scale_object,
                                      scale_factor          = prefs.gr2_scale_factor,
                                      apply_axis_conversion = prefs.gr2_apply_axis_conversion,
                                      blender_friendly_skeleton = prefs.gr2_blender_friendly_skeleton,
                                      )

        # Automatic materials-by-name (7a) -- looks up each newly-
        # imported object's *original* SWTOR material names (see
        # build()'s "gr2_material_names" round-trip property) against
        # real .mat files and builds their native SWTOR shaders.
        #
        # Only for genuine standalone .gr2 imports (this operator's own
        # bl_idname) -- deliberately excludes the NPC/Character (.json)
        # importer's internal calls into this same load() (bl_idname
        # "import_mesh.gr2_json"), which source material data from the
        # json file exclusively; running this there would clash with
        # that (confirmed with Crunch). Also excluded under "Enforce
        # Neutral Settings", which exists specifically for vanilla,
        # older-tool-compatible behavior.
        if not operator.enforce_neutral_settings and operator.bl_idname == 'IMPORT_MESH_OT_gr2':
            if operator.options.is_invoke:
                apply_materials_by_name = operator.apply_materials_by_name
            else:
                prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
                apply_materials_by_name = prefs.gr2_apply_materials_by_name_default

            if apply_materials_by_name and objects_names:
                apply_materials_by_name_after_import(objects_names)

        
        # job_results-filling section
        
        job_results['job_origin'] = operator.bl_idname

        job_results['objs_names'].extend(objects_names)

        if operator.job_results_rich:
            if 'resources' in filepath:
                job_results['files_objs_names'][ filepath.replace("\\", "/").partition("resources/")[2] ] = objects_names
            else:
                job_results['files_objs_names'][ filepath.replace("\\", "/") ] = objects_names

        elapsed_time = time.time() - start_time
        if objects_names:
            print(f"OBJS: {objects_names}")
            print(f"TIME: {elapsed_time:.3f} s.")
            print()
        else:
            print("FAILED!!!")
            print()

        
        return True
    else:
        return False


# ---------------------------------------------------------------------------
# Shared helpers for importing .gr2 files programmatically with mesh reuse
# ---------------------------------------------------------------------------
#
# Moved here from ops/import_area.py so that every importer that needs a
# cached/merged .gr2 import (Area Assembler, .fxspec import, the HMS DYD
# scatter) depends on THIS module rather than on each other -- ops/dyd_scatter.py
# used to import them from ops/import_area.py, which made it impossible for
# import_area.py to import anything built on dyd_scatter.py without a cycle.
# Behaviour is unchanged.


class _MeshCache:
    """
    Caches already-imported .gr2 mesh data by resolved (absolute,
    on-disk) path, so a source file reused many times across one area
    (a candle, a crate, a piece of trim) only gets parsed/built once.
    Later instances get a cheap bpy.data.objects.new() sharing that
    mesh data instead of a full reimport.

    A stored entry is either a non-empty list of (Mesh, gr2_material_names)
    pairs (a multi-mesh .gr2 produces more than one Object on its first
    import; every later instance reproduces that same multi-object
    shape) or None, meaning the first import of this path produced
    nothing usable -- remembered so later instances don't retry it.

    gr2_material_names is captured alongside its mesh specifically
    because it's an Object-level custom property, not a Mesh-level one
    -- bpy.data.objects.new() does NOT copy custom properties from
    wherever a mesh came from, even when reusing that exact mesh
    data-block, so a fresh copy has to be set explicitly on every new
    instance or it silently ends up missing. CONFIRMED real bug this
    fixes: _filter_excluded_objects()'s utility-material check reads
    this property per-object, so every cache-instantiated repeat of a
    to-be-excluded object (e.g. a reused occluder mesh) was silently
    skipping that check and never getting excluded -- only a path's
    very first, freshly-imported instance ever actually had the
    property to check against.

    scale_factor is baked into each freshly-cached mesh's vertex data
    exactly once, here, not reflected in Object.scale -- matching this
    addon's own .gr2 importer convention (confirmed against
    import_gr2.py's build(): scale_object bakes into geometry via
    transform_apply, Object.scale is reset to its natural value
    afterward). Every later instance sharing this cached mesh data
    automatically gets the correctly pre-scaled geometry for free.
    """
    def __init__(self, scale_factor=1.0):
        self._cache = {}  # resolved_path -> list[(Mesh, material_names_json_or_None)] or None
        self._main_index = {}  # resolved_path -> which entry is named after the file (default: the first)
        self._scale_factor = scale_factor

    def has(self, resolved_path):
        return resolved_path in self._cache

    def store(self, resolved_path, objects, main_index=None):
        # type: (str, list, Optional[int]) -> None
        """main_index: which of the stored meshes instantiate() should name
        after the file (None = the first, the long-standing behaviour). It
        indexes the meshes in `objects` order, objects without mesh data left
        out -- the same list the entries are built from."""
        if self._scale_factor != 1.0:
            scale_matrix = Matrix.Scale(self._scale_factor, 4)
            seen_meshes = set()
            for ob in objects:
                if ob.data is not None and id(ob.data) not in seen_meshes:
                    ob.data.transform(scale_matrix)
                    ob.data.update()
                    seen_meshes.add(id(ob.data))
        entries = [(ob.data, ob.get("gr2_material_names")) for ob in objects if ob.data is not None]
        # Give every cached mesh a fake user. Area Assembler deletes some of an
        # import's objects right after it (Skip Game Engine Objects, the
        # SpeedTree LOD reducer), which leaves their meshes with no users --
        # and ANY global bpy.data.orphans_purge() later in the batch (the
        # terrain material builder runs one while de-duplicating shared node
        # groups) would free them out from under this cache, so the next
        # instantiate() of that path fails with "StructRNA of type Mesh has
        # been removed" and every later instance of the file goes missing.
        # release() drops the protection at the end of the batch.
        for mesh_data, _material_names in entries:
            mesh_data.use_fake_user = True
        self._cache[resolved_path] = entries or None
        if main_index is not None:
            self._main_index[resolved_path] = main_index

    def release(self):
        """Drops the fake users store() added. Call once the batch is done
        (every instantiate() has happened); idempotent, and tolerant of a
        mesh that has already been removed."""
        for entries in self._cache.values():
            for mesh_data, _material_names in entries or ():
                try:
                    mesh_data.use_fake_user = False
                except ReferenceError:
                    pass

    def instantiate(self, resolved_path, name):
        # type: (str, str) -> list
        """
        Builds new Object(s) sharing this path's cached mesh data.
        Returns [] for a known-discard path.

        Deliberately does NOT parent multi-mesh sub-objects to each
        other -- matches observed behavior of a fresh multi-mesh .gr2
        import (confirmed against a real 220-file batch import with no
        multi-mesh positioning issues, meaning build() itself doesn't
        parent its own sub-objects together either). Keeping every
        object here fully independent means _process_element's own
        "if ob.parent is None: _apply_transform(...)" applies uniformly
        to all of them, and removes any risk from later deleting one
        sub-object (e.g. a DBO/utility-material exclusion) leaving
        others orphaned mid-scene with only their original, un-baked
        local transform.
        """
        entries = self._cache.get(resolved_path)
        if not entries:
            return []
        objects = []
        main_index = self._main_index.get(resolved_path, 0)
        for i, (mesh_data, material_names) in enumerate(entries):
            ob_name = name if i == main_index else mesh_data.name
            new_ob = bpy.data.objects.new(ob_name, mesh_data)
            if material_names is not None:
                new_ob["gr2_material_names"] = material_names
            objects.append(new_ob)
        return objects


def _job_results_key(filepath):
    # type: (str) -> str
    """Normalizes a filepath into the key load() above uses for job_results['files_objs_names']."""
    normalized = filepath.replace("\\", "/")
    if "resources" in normalized:
        return normalized.partition("resources/")[2]
    return normalized


def _merge_multi_mesh_objects(objects, name):
    # type: (list, str) -> list
    """
    Joins multiple Objects into one via bpy.ops.object.join(), matching
    the old source's "Merge Multi-Mesh Objects" option (confirmed real
    toggle there too, default off -- same default kept here).

    Always renames both the resulting Object AND its mesh data-block to
    `name` (the resolved .gr2's own filename stem) -- CONFIRMED bug this
    fixes: an earlier version only renamed the Object, leaving the mesh
    DATA-block (a separate ID datablock bpy.ops.object.join() doesn't
    touch the name of) still carrying whichever sub-object happened to
    be arbitrarily active before the join, e.g. "Object17" instead of
    the real name.

    Also rebuilds "gr2_material_names" for the merged result --
    CONFIRMED bug this fixes, and the more serious one: that property
    lives on the OBJECT, not the mesh data, and bpy.ops.object.join()
    only keeps the ACTIVE object's own custom properties -- every OTHER
    sub-object's own gr2_material_names is simply lost when that object
    gets deleted by the join. Left unfixed, only whichever few material
    slots happened to belong to the arbitrarily-active sub-object ever
    got real names; every other slot silently kept whatever generic
    Blender default it already had, with nothing left to tell
    apply_materials_by_name_to_gr2_objects() what those slots were
    really supposed to be. Fixed by capturing each sub-object's own
    slot-name mapping BEFORE the join (keyed by the actual material
    ID-datablock, not by slot index -- robust regardless of how
    bpy.ops.object.join() itself reorders or deduplicates identically-
    named slots internally, which isn't something worth relying on),
    then rebuilding the merged object's own gr2_material_names by
    walking its FINAL slot list and looking up each slot's real name
    through that mapping. A slot whose original name can't be recovered
    (came from an object with no gr2_material_names at all, or slot
    genuinely has no material) falls back to that slot's own current
    Blender material name -- matches this same file's own existing 7b
    precedent for the same situation, and is always a safe string
    (never None, which would crash the string/path operations inside
    _build_named_material's own .mat file lookup).

    No parenting anywhere in this path -- confirmed not needed here,
    per this addon's own earlier decision (see _MeshCache.instantiate()'s
    own note on why fresh multi-mesh .gr2 imports don't need it either).
    """
    if len(objects) <= 1:
        return objects

    material_name_by_datablock = {}
    for ob in objects:
        raw = ob.get("gr2_material_names")
        if not raw:
            continue
        try:
            original_names = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for slot, real_name in zip(ob.data.materials, original_names):
            if slot is not None and isinstance(real_name, str) and real_name:
                material_name_by_datablock[slot] = real_name

    bpy.ops.object.select_all(action='DESELECT')
    for ob in objects:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.join()

    merged = bpy.context.view_layer.objects.active
    merged.name = name
    merged.data.name = name

    merged_names = [
        material_name_by_datablock.get(slot, slot.name if slot is not None else "")
        for slot in merged.data.materials
    ]
    if merged_names:
        merged["gr2_material_names"] = json.dumps(merged_names)

    return [merged]


def _import_gr2(operator, context, resources_root, resolved_path, mesh_cache):
    # type: (Any, Any, str, str, _MeshCache) -> list
    """
    Returns a list of Objects for `resolved_path` (an absolute, on-disk
    .gr2 path), either freshly imported or instantiated from mesh_cache.

    Naming: no name is passed in for a fresh import -- build() names the
    resulting objects. Under this operator's neutral settings that means
    each model's own art name ("LOD[0]", "trunk002", ...), unless the
    operator's area_name_as_filename is on, in which case the file's MAIN
    mesh is named after the file (see main_mesh_index()). (An earlier
    version of this function threaded the calling element's raw numeric
    SWTOR id through as the name, which only got used on the
    *cached-instance* path below, producing objects named like
    "4611686170648420455" on the second+ instance of a reused mesh.)
    The cached-instance path names one stored mesh after the resolved
    path's own filename: the first one by default, or -- when
    area_name_as_filename is on -- the same main mesh a fresh import
    named, so every copy of a file ends up named alike.

    Merging (area_merge_multi_mesh_objects) happens here, once, on a
    FRESH import only, before mesh_cache.store() -- so the cached mesh
    data is already the single merged result, and every later reused
    instance automatically gets the merged shape for free too, same
    reasoning as scale_factor baking happening at this exact point.
    """
    if mesh_cache.has(resolved_path):
        return mesh_cache.instantiate(resolved_path, Path(resolved_path).stem)

    operator.job_results_rich = True
    load(operator, context, resolved_path)

    object_names = job_results.get("files_objs_names", {}).get(_job_results_key(resolved_path), [])
    imported_objects = [bpy.data.objects[n] for n in object_names]
    if operator.area_merge_multi_mesh_objects and len(imported_objects) > 1:
        imported_objects = _merge_multi_mesh_objects(imported_objects, Path(resolved_path).stem)
    main_index = None
    if getattr(operator, "area_name_as_filename", False):
        # Name Objects As Filenames: load() named the file's MAIN mesh after the
        # file, so reused instances must name that same one (not just whichever
        # sub-object happens to be stored first).
        main_index = main_mesh_index([ob.data.name for ob in imported_objects if ob.data is not None])
    mesh_cache.store(resolved_path, imported_objects, main_index)
    return imported_objects

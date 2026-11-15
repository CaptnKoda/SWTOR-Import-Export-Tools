# <pep8 compliant>

"""
This script exports Star Wars: The Old Republic models from Blender.

Usage:
Run this script from "File->Export" menu and then save the desired GR2 model file.

https://github.com/SWTOR-Slicers/WikiPedia/wiki/GR2-File-Structure
"""

import json
import os
from typing import List, Optional, Set

import bpy
import bmesh as bmesh_module
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Context, Object, Operator, Mesh
from bpy_extras.io_utils import ExportHelper, axis_conversion, orientation_helper
from mathutils import Matrix, Vector

from ..types.gr2 import Granny2
from ..utils.binary import ArrayBuffer, DataView
from ..utils.number import encodeHalfFloat


@orientation_helper(axis_forward='-Z', axis_up='Y')
class ExportGR2(Operator, ExportHelper):
    """Export SWTOR GR2 file format (.gr2)"""
    bl_idname = "export_mesh.gr2"
    bl_label = "Export SWTOR Object (.gr2)"
    bl_description = "Export object as a SWTOR .gr2 file for modding purposes.\n\nIN BETA STATE!  Please report any issues to our Discord server.\n\nYou can verify its results to an extent by re-importing\nthe exported object and inspecting it before\ncommitting it to a mod."
    bl_options = {'PRESET'}

    filename_ext = ".gr2"

    filter_glob: StringProperty(
        default="*.gr2",
        options={'HIDDEN'},
    )
    has_clo: BoolProperty(
        name="Has .clo file?",
        description="Enable if there is a corresponding .clo file to go with this model",
        default=False)
    version: EnumProperty(
        name="Format",
        description="GR2 file format version to write",
        items=(
            ('5', "64-bit (current)", "SWTOR's current .gr2 format, used since Game Update 7.2.1"),
            ('4', "32-bit (legacy)", "SWTOR's old .gr2 format, used before Game Update 7.2.1.\nNo longer viable for modding the current version of the game.\nKept for research and historic reasons"),
        ),
        default='5',
    )
    triangulate: BoolProperty(
        name="Triangulate",
        description="GR2 only supports triangles. Enable this if your mesh has quad or n-gon "
                    "faces (e.g. it wasn't round-tripped from a .gr2 import, or you've added new "
                    "geometry). Off by default: known-good round-tripped meshes are already "
                    "triangles, and this avoids re-triangulating -- and potentially subtly "
                    "changing the tessellation of -- files that already work.",
        default=False)

    def execute(self, context):
        # type: (Context) -> Set[str]
        # from mathutils import Matrix

        global_matrix = axis_conversion(to_forward=self.axis_forward, to_up=self.axis_up).to_4x4()

        # global_matrix = axis_conversion(
        #     to_forward=self.axis_forward,
        #     to_up=self.axis_up
        # ).to_4x4() @ Matrix.Scale(0.1, 4)  # Scale down to 10%

        # Cache selected objects.
        obs = context.selected_objects

        for ob in obs:
            if len(obs) == 1:
                path = os.path.normpath(self.filepath)
            else:
                path = os.path.join(os.path.split(self.filepath)[0],
                                    ob.name.replace(' ', '_') + ".gr2")

            # Clear selected object(s).
            bpy.ops.object.select_all(action='DESELECT')

            # Select ob
            ob.select_set(True)

            # Export ob
            if not save(self, context, path, ob, global_matrix=global_matrix, version=int(self.version)):
                return {'CANCELLED'}

        return {'FINISHED'}


def parse(ob, mesh, has_clo=False, version=5, operator=None, triangulate=False):
    # type: (Object, Mesh, bool, int, Optional[Operator], bool) -> Granny2
    gr2 = Granny2()

    gr2.version = version

    # Meshes
    # gr2_original_name: prefer the name captured at import time over the
    # current Blender object name, since Blender force-renames objects on
    # collision (e.g. "Name.001") -- without this, a second imported object
    # sharing a source name with an existing one would bake the corrupted
    # name back into the export.
    mesh_name = ob.get("gr2_original_name", ob.name)
    gr2.mesh_buffer = {0: Granny2.Mesh(mesh_name)}
    gmesh = gr2.mesh_buffer[0]

    # gr2_material_names: prefer the original (file-exact) material names
    # captured at import time, index-matched to this object's material slots.
    # Blender renames material data-blocks on name collision (a second
    # "default" material becomes "default.001"), which is common across
    # SWTOR assets sharing generic material names -- material.name alone
    # can't be trusted for round-tripping.
    gr2_material_names = None
    if "gr2_material_names" in ob:
        try:
            gr2_material_names = json.loads(ob["gr2_material_names"])
        except (ValueError, TypeError):
            gr2_material_names = None

    # GR2 only ever stores triangles -- num_indices is always num_polygons * 3,
    # and every indices-buffer entry is a 3-vertex triangle. calc_loop_triangles()
    # gives a read-only triangulated view without modifying the actual mesh
    # data; it's harmless to call even when unused below.
    mesh.calc_loop_triangles()
    has_non_tri_faces = any(len(p.vertices) != 3 for p in mesh.polygons)

    if has_non_tri_faces and not triangulate:
        # Confirmed: exporting quad/n-gon faces without triangulating corrupts
        # the file (each quad was silently written as one bogus 3-index
        # "triangle" using only 3 of its 4 vertices). Triangulation is opt-in
        # rather than automatic here by request -- known-good round-tripped
        # meshes are already all-triangle, so this only ever affects meshes
        # that need it, and avoids silently re-tessellating anything.
        if operator is not None:
            operator.report(
                {'WARNING'},
                f"'{ob.name}' has quad or n-gon faces and \"Triangulate\" is disabled -- "
                f"GR2 only supports triangles, so this export may be malformed. Enable "
                f"\"Triangulate\" in the export options to fix this."
            )
        faces = mesh.polygons
    else:
        faces = mesh.loop_triangles

    # Parse materials / sub-meshes
    gr2.material_names = {}
    gmesh.piece_header_buffer = {}

    # A mesh with no material slots at all (e.g. a fresh Blender primitive)
    # still has real geometry that needs to end up in exactly one piece --
    # otherwise no pieces get created, num_pieces stays 0, and re-importing
    # the file crashes matching real triangle data against an empty
    # per-piece material_indices list (confirmed).
    has_materials = len(mesh.materials) > 0
    material_count = len(mesh.materials) if has_materials else 1

    offset_indices = 0
    for i in range(material_count):
        material = mesh.materials[i] if has_materials else None
        num_polygons: int = 0
        co_x: List[float] = []
        co_y: List[float] = []
        co_z: List[float] = []

        for tri in faces:
            tri_material_index = tri.material_index if has_materials else 0
            if tri_material_index == i:
                num_polygons += 1
                for v in tri.vertices:
                    vertex = mesh.vertices[v]
                    co_x.append(vertex.co[0])
                    co_y.append(vertex.co[1])
                    co_z.append(vertex.co[2])

        piece = Granny2.Piece()
        piece.material_index = piece.index = i
        piece.num_polygons = num_polygons
        # Cumulative sum of all preceding pieces' triangle counts -- the
        # previous code only added the immediately-preceding piece's count,
        # which happened to work for meshes with at most 2 materials but
        # under-counts for 3+ (offsets from 2+ pieces back were dropped).
        piece.offset_indices = offset_indices
        offset_indices += num_polygons

        if co_x:
            piece.bounds = Granny2.BoundingBox(
                (min(co_x), min(co_y), min(co_z), 1.0, max(co_x), max(co_y), max(co_z), 1.0))
        else:
            # Material slot exists but has no triangles assigned to it.
            piece.bounds = Granny2.BoundingBox((0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))

        if gr2_material_names is not None and i < len(gr2_material_names):
            gr2.material_names[i] = gr2_material_names[i]
        elif material is not None:
            gr2.material_names[i] = material.name
        else:
            gr2.material_names[i] = "default"
        gmesh.piece_header_buffer[i] = piece

    # Parse bone names
    if ob.bone_bounds.keys() == ob.vertex_groups.keys():
        gmesh.bone_buffer = {i: Granny2.Bone(bone.name, bone.bounds[:])
                             for i, bone in enumerate(ob.bone_bounds.values())}
    else:
        gmesh.bone_buffer = {i: Granny2.Bone(name)
                             for i, name in enumerate(ob.vertex_groups.keys())}

    # Parse mesh vertices. GR2 needs a distinct vertex entry for every unique
    # (position, normal, tangent, UV) combination, not just every unique
    # position -- two triangle corners can share a position but need
    # different data at any hard edge or UV seam. The previous code deduped
    # by raw Blender vertex index alone ("first loop wins"), which silently
    # discarded the correct normal/UV for every corner after the first at any
    # such split point (confirmed: this is why a hard-shaded test cube came
    # back with visibly wrong face normals after export, despite having
    # entirely correct topology). Bone weights/indices are per-vertex, not
    # per-loop, in Blender, so every split copy of a given vertex always gets
    # identical weight data -- no extra handling needed there.
    gmesh.vertex_buffer = {}
    loop_to_vertex_id = {}
    seen_keys = {}

    for loop_index, loop in enumerate(mesh.loops):
        vert = mesh.vertices[loop.vertex_index]
        tex = mesh.uv_layers.active.data[loop_index].uv

        key = (
            loop.vertex_index,
            round(loop.normal[0], 5), round(loop.normal[1], 5), round(loop.normal[2], 5),
            round(loop.tangent[0], 5), round(loop.tangent[1], 5), round(loop.tangent[2], 5),
            round(loop.bitangent_sign, 5),
            round(tex[0], 5), round(tex[1], 5),
        )

        existing_id = seen_keys.get(key)
        if existing_id is not None:
            loop_to_vertex_id[loop_index] = existing_id
            continue

        new_id = len(gmesh.vertex_buffer)
        seen_keys[key] = new_id
        loop_to_vertex_id[loop_index] = new_id

        pos = vert.co
        nor = loop.normal
        tan = loop.tangent
        bit = loop.bitangent_sign

        vertex = Granny2.Vertex(pos)

        if gmesh.bone_buffer:
            groups = sorted([(g.group, g.weight) for g in vert.groups],
                            key=lambda xy: (xy[1], xy[0]),
                            reverse=True)

            if len(groups) > 4:
                groups = groups[:4]
            else:
                for _ in range(4 - len(groups)):
                    groups.append((groups[0][0] if groups else 0.0, 0.0))

            vertex.bone_indices = Vector([groups[j][0] for j in range(4)])
            vertex.bone_weights = Vector([groups[j][1] for j in range(4)])

        vertex.normals = Vector(nor[:3] + (1.0,))
        vertex.tangents = Vector(tan[:3] + (-bit,))
        vertex.uv_layer0 = Vector(tex[:2])

        gmesh.vertex_buffer[new_id] = vertex

    # Parse mesh indices. Each face (triangle or, if triangulation is
    # disabled and the mesh has non-tri faces, raw polygon) exposes its loop
    # indices in the same order as its vertex indices -- use those to look up
    # the correct split-vertex id for each corner, rather than the raw
    # (unsplit) vertex index.
    def _face_loop_indices(face):
        return face.loops if hasattr(face, "loops") else face.loop_indices

    gmesh.indices_buffer = {
        i: tuple(loop_to_vertex_id[li] for li in _face_loop_indices(tri))
        for i, tri in enumerate(faces)
    }

    # LOD / BitFlag1: prefer captured values over the previous heuristics.
    # gr2_lod distinguishes normal/LOD/occluder/collision meshes -- the old
    # code always wrote 0 (see Stage 1 notes). gr2_bit_flag1 is the real
    # collision (0x2000) / static (0x8000) bit data; Mesh.bit_flag1 falls
    # back to its previous boneless-mesh heuristic when this isn't set
    # (see the updated property in types/gr2.py).
    gmesh.lod = ob.get("gr2_lod", 0)
    if "gr2_bit_flag1" in ob:
        gmesh.raw_bit_flag1 = ob["gr2_bit_flag1"]

    # Type Flag. The has_clo checkbox is the user's explicit intent for
    # *this* export and stays authoritative (someone may deliberately pair
    # a previously-plain mesh with a new .clo, or vice versa) -- but if the
    # captured gr2_type_flag disagrees, that's worth surfacing rather than
    # silently discarding.
    gr2.type_flag = 1 if has_clo else 0
    if "gr2_type_flag" in ob and ob["gr2_type_flag"] != gr2.type_flag and operator is not None:
        operator.report(
            {'WARNING'},
            f"'{ob.name}': imported type_flag was {ob['gr2_type_flag']}, exporting as "
            f"{gr2.type_flag} based on the \"Has .clo file?\" checkbox."
        )

    # Attachment bones (VFX/socket points). File-level round-trip data,
    # captured verbatim from the import-time custom property -- these were
    # never editable in Blender to begin with, so there's nothing to
    # reconcile against current object state the way lod/bit_flag1/materials
    # are. Malformed/missing JSON just means no attachment bones get written,
    # matching a mesh that never had any.
    gr2.attachment_bones = []
    if "gr2_attachment_bones" in ob:
        try:
            gr2.attachment_bones = json.loads(ob["gr2_attachment_bones"])
        except (ValueError, TypeError):
            if operator is not None:
                operator.report(
                    {'WARNING'},
                    f"'{ob.name}': gr2_attachment_bones custom property couldn't be parsed -- "
                    f"exporting with no attachment bones."
                )

    # Calculate Bounds
    gr2.bounds = Granny2.BoundingBox(
        (
            min([co[0] for co in ob.bound_box]),
            min([co[1] for co in ob.bound_box]),
            min([co[2] for co in ob.bound_box]),
            1.0,
            max([co[0] for co in ob.bound_box]),
            max([co[1] for co in ob.bound_box]),
            max([co[2] for co in ob.bound_box]),
            1.0,
        )
    )

    # Calculate Offsets
    gr2.calculate_offsets(gr2.version == 5)

    return gr2


def write(gr2, path):
    # type: (Granny2, str) -> None
    # NOTE: is64 drives every version-dependent layout decision below. The two
    # previously-separate exporters (64-bit / 32-bit) are merged here rather
    # than kept as duplicate functions, since apart from pointer widths and a
    # handful of genuinely different header slots, the two formats' write
    # logic is identical (confirmed against the Jedipedia BWAG spec).
    is64 = gr2.version == 5

    buffer = ArrayBuffer(gr2.num_bytes)
    dv = DataView(buffer)
    pos = 0

    # MAGIC bytes
    dv.setUint32(pos, gr2.magic_bytes, 1)
    pos = 4
    # Version major
    dv.setUint32(pos, gr2.version_major, 1)
    pos += 4
    # Version minor
    dv.setUint32(pos, gr2.version_minor, 1)
    pos += 4
    # Offset BNRY/LTLE
    dv.setUint32(pos, gr2.offset_BNRY, 1)
    pos += 4

    # Number of cached offsets
    dv.setUint32(pos, gr2.num_cached_offsets, 1)
    pos += 4
    # Type flag
    dv.setUint32(pos, gr2.type_flag, 1)
    pos += 4
    # Number of meshes
    dv.setUint16(pos, gr2.num_meshes, 1)
    pos += 2
    # Number of materials
    dv.setUint16(pos, gr2.num_materials, 1)
    pos += 2
    # Number of skeleton bones (only applies to skeleton/armature gr2s)
    dv.setUint16(pos, gr2.num_skeleton_bones, 1)
    pos += 2
    # Number of attachments
    dv.setUint16(pos, gr2.num_attachments, 1)
    pos += 2

    # 16 zero bytes
    dv.setBigUint64(pos, 0, 1)
    pos += 8
    dv.setBigUint64(pos, 0, 1)
    pos += 8

    # Global bounding box
    for co in gr2.bounds:
        dv.setFloat32(pos, co, 1)
        pos += 4

    # Offset of the cached offsets / mesh header / material name offsets /
    # skeleton bones / attachments. Widths differ (64-bit vs 32-bit pointers),
    # but which slots are meaningful is otherwise the same on both versions --
    # in particular, the skeleton-bones slot should be conditional on whether
    # this file actually has skeleton bones on EITHER version. (The old 32-bit
    # exporter always wrote 0 there unconditionally; that was a bug, not an
    # intentional format difference -- fixed here.)
    if is64:
        dv.setUint64(pos, gr2.offset_cached_offsets, 1)
        pos += 8
        dv.setUint64(pos, gr2.offset_mesh_headers, 1)
        pos += 8
        dv.setUint64(pos, gr2.offset_material_name_offsets, 1)
        pos += 8
        if gr2.num_skeleton_bones > 0:
            dv.setUint64(pos, gr2.offset_bones_buffer, 1)
        else:
            dv.setUint64(pos, 0, 1)
        pos += 8
        # Offset of attachments
        if gr2.num_attachments > 0:
            dv.setUint64(pos, gr2.offset_attachments, 1)
        else:
            dv.setUint64(pos, 0, 1)
        pos += 8
    else:
        dv.setUint32(pos, gr2.offset_cached_offsets, 1)
        pos += 4
        dv.setUint32(pos, gr2.offset_mesh_headers, 1)
        pos += 4
        dv.setUint32(pos, gr2.offset_material_name_offsets, 1)
        pos += 4
        if gr2.num_skeleton_bones > 0:
            dv.setUint32(pos, gr2.offset_bones_buffer, 1)
        else:
            dv.setUint32(pos, 0, 1)
        pos += 4
        # Offset of attachments
        if gr2.num_attachments > 0:
            dv.setUint32(pos, gr2.offset_attachments, 1)
        else:
            dv.setUint32(pos, 0, 1)
        pos += 4

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Mesh header(s).
    # i16 lod + u16 bitFlag1 is the real layout on BOTH versions (confirmed
    # against the Jedipedia BWAG spec: these are shared struct fields
    # regardless of stride). The old 32-bit exporter wrote mesh.bit_flag1 as
    # a single Uint32 here instead, silently dropping lod and corrupting
    # every field after it in that mesh's header -- fixed here.
    for i, mesh in gr2.mesh_buffer.items():
        if is64:
            dv.setUint64(pos, mesh.offset_mesh_name, 1)
            pos += 8
        else:
            dv.setUint32(pos, mesh.offset_mesh_name, 1)
            pos += 4

        # LOD (int16) + BitFlag1 (uint16)
        dv.setInt16(pos, mesh.lod if hasattr(mesh, "lod") else 0, 1)
        pos += 2
        dv.setUint16(pos, mesh.bit_flag1, 1)
        pos += 2
        # Number of sub meshes
        dv.setUint16(pos, mesh.num_pieces, 1)
        pos += 2
        # Number of bones
        dv.setUint16(pos, mesh.num_used_bones, 1)
        pos += 2

        if is64:
            # BitFlag2
            dv.setUint32(pos, mesh.bit_flag2, 1)
            pos += 4
            # Vertex size
            dv.setUint32(pos, mesh.vertex_size, 1)
            pos += 4
        else:
            # BitFlag2
            dv.setUint16(pos, mesh.bit_flag2, 1)
            pos += 2
            # Vertex size
            dv.setUint16(pos, mesh.vertex_size, 1)
            pos += 2

        # Number of vertices
        dv.setUint32(pos, mesh.num_vertices, 1)
        pos += 4
        # Number of indices
        dv.setUint32(pos, int(mesh.num_polygons * 3), 1)
        pos += 4

        if is64:
            dv.setUint64(pos, mesh.offset_vertex_buffer, 1)
            pos += 8
            dv.setUint64(pos, mesh.offset_piece_headers, 1)
            pos += 8
            dv.setUint64(pos, mesh.offset_indices_buffer, 1)
            pos += 8
            dv.setUint64(pos, mesh.offset_bones_buffer, 1)
            pos += 8
        else:
            dv.setUint32(pos, mesh.offset_vertex_buffer, 1)
            pos += 4
            dv.setUint32(pos, mesh.offset_piece_headers, 1)
            pos += 4
            dv.setUint32(pos, mesh.offset_indices_buffer, 1)
            pos += 4
            dv.setUint32(pos, mesh.offset_bones_buffer, 1)
            pos += 4

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Sub mesh headers. Byte-identical on both versions (all Uint32 fields).
    for mesh in gr2.mesh_buffer.values():
        for piece in mesh.piece_header_buffer.values():
            # Offset of sub polygons within indices buffer
            dv.setUint32(pos, piece.offset_indices, 1)
            pos += 4
            # Number of polygons used by sub
            dv.setUint32(pos, piece.num_polygons, 1)
            pos += 4
            # Material id
            dv.setUint32(pos, piece.material_index, 1)
            pos += 4
            # Sub mesh id
            dv.setUint32(pos, piece.index, 1)
            pos += 4

            # Bounding Box
            for co in piece.bounds:
                dv.setFloat32(pos, co, 1)
                pos += 4

    # Material name offsets
    offset = 0
    for i, mesh in gr2.mesh_buffer.items():
        offset = mesh.offset_mesh_name if i == 0 else offset
        offset += len(mesh.name) + 1
    for material_name in gr2.material_names.values():
        if is64:
            dv.setUint64(pos, offset, 1)
            pos += 8
        else:
            dv.setUint32(pos, offset, 1)
            pos += 4
        offset += len(material_name) + 1

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Attachments (VFX/socket points). Each entry: name pointer, parent bone
    # name pointer, then a 4x4 matrix (16 floats). String offsets are
    # precomputed the same way the material name offsets above are -- `offset`
    # already points past the last material name string, and the Strings
    # section further down writes attachment name/bone-name pairs in this
    # same order, right after material names.
    for attachment in (getattr(gr2, "attachment_bones", None) or []):
        name = attachment["name"]
        bone_name = attachment["bone"]
        matrix = attachment["matrix"]

        if is64:
            dv.setUint64(pos, offset, 1)
            pos += 8
        else:
            dv.setUint32(pos, offset, 1)
            pos += 4
        offset += len(name) + 1

        if is64:
            dv.setUint64(pos, offset, 1)
            pos += 8
        else:
            dv.setUint32(pos, offset, 1)
            pos += 4
        offset += len(bone_name) + 1

        for f in matrix:
            dv.setFloat32(pos, f, 1)
            pos += 4

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Vertices buffer. Byte-identical on both versions (all Uint8/Uint16
    # fields -- there is no 64-bit-specific vertex encoding).
    for mesh in gr2.mesh_buffer.values():
        for vertex in mesh.vertex_buffer.values():
            dv.setFloat32(pos, vertex.position.x, 1)
            pos += 4
            dv.setFloat32(pos, vertex.position.y, 1)
            pos += 4
            dv.setFloat32(pos, vertex.position.z, 1)
            pos += 4

            if hasattr(vertex, "bone_indices") and hasattr(vertex, "bone_weights"):
                for co in vertex.bone_weights:
                    dv.setUint8(pos, int(co * 255))
                    pos += 1

                for co in vertex.bone_indices:
                    dv.setUint8(pos, int(co))
                    pos += 1

            for co in vertex.normals[:3]:
                dv.setUint8(pos, int((co * 127) + 127))
                pos += 1

            dv.setUint8(pos, 255)
            pos += 1

            for co in vertex.tangents[:3]:
                dv.setUint8(pos, int((co * 127) + 127))
                pos += 1

            dv.setUint8(pos, 255 if vertex.tangents[3] == 1.0 else 0)
            pos += 1

            dv.setUint16(pos, encodeHalfFloat(vertex.uv_layer0.x), 1)
            pos += 2
            dv.setUint16(pos, encodeHalfFloat(1 - vertex.uv_layer0.y), 1)
            pos += 2

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint32(pos, 0)
        pos += 1

    # Indices buffer. Byte-identical on both versions.
    for mesh in gr2.mesh_buffer.values():
        for polygon in mesh.indices_buffer.values():
            for vertex_index in polygon:
                dv.setUint16(pos, int(vertex_index), 1)
                pos += 2

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Bones buffer. Only the bone entry stride (32 vs 28 bytes) differs by
    # version -- everything else, including the boneless-mesh fallback,
    # is identical.
    for mesh in gr2.mesh_buffer.values():
        if mesh.bone_buffer:
            for i, bone in mesh.bone_buffer.items():
                if is64:
                    dv.setUint64(pos, offset, 1)
                    pos += 8
                else:
                    dv.setUint32(pos, offset, 1)
                    pos += 4
                offset += len(bone.name) + 1

                if bone.bounds:
                    dv.setFloat32(pos, bone.bounds[0], 1)
                    pos += 4
                    dv.setFloat32(pos, bone.bounds[1], 1)
                    pos += 4
                    dv.setFloat32(pos, bone.bounds[2], 1)
                    pos += 4
                    dv.setFloat32(pos, bone.bounds[3], 1)
                    pos += 4
                    dv.setFloat32(pos, bone.bounds[4], 1)
                    pos += 4
                    dv.setFloat32(pos, bone.bounds[5], 1)
                    pos += 4

                else:
                    vertices = [v for v in mesh.vertex_buffer.values() if i in v.bone_indices]
                    x_bounds = [vertex.position.x for vertex in vertices] if vertices else [0]
                    y_bounds = [vertex.position.y for vertex in vertices] if vertices else [0]
                    z_bounds = [vertex.position.z for vertex in vertices] if vertices else [0]

                    dv.setFloat32(pos, min(x_bounds), 1)
                    pos += 4
                    dv.setFloat32(pos, min(y_bounds), 1)
                    pos += 4
                    dv.setFloat32(pos, min(z_bounds), 1)
                    pos += 4
                    dv.setFloat32(pos, max(x_bounds), 1)
                    pos += 4
                    dv.setFloat32(pos, max(y_bounds), 1)
                    pos += 4
                    dv.setFloat32(pos, max(z_bounds), 1)
                    pos += 4
        else:
            if is64:
                dv.setUint64(pos, mesh.offset_mesh_name, 1)
                pos += 8
            else:
                dv.setUint32(pos, mesh.offset_mesh_name, 1)
                pos += 4
            dv.setFloat32(pos, gr2.bounds.min_x, 1)
            pos += 4
            dv.setFloat32(pos, gr2.bounds.min_y, 1)
            pos += 4
            dv.setFloat32(pos, gr2.bounds.min_z, 1)
            pos += 4
            dv.setFloat32(pos, gr2.bounds.max_x, 1)
            pos += 4
            dv.setFloat32(pos, gr2.bounds.max_y, 1)
            pos += 4
            dv.setFloat32(pos, gr2.bounds.max_z, 1)
            pos += 4

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Strings. Byte-identical on both versions (raw ASCII + null terminator).
    for mesh in gr2.mesh_buffer.values():
        for char in mesh.name:
            dv.setUint8(pos, ord(char))
            pos += 1
        dv.setUint8(pos, 0)
        pos += 1

    offset_material_names = pos
    for material_name in gr2.material_names.values():
        for char in material_name:
            dv.setUint8(pos, ord(char))
            pos += 1
        dv.setUint8(pos, 0)
        pos += 1

    for attachment in (getattr(gr2, "attachment_bones", None) or []):
        for char in attachment["name"]:
            dv.setUint8(pos, ord(char))
            pos += 1
        dv.setUint8(pos, 0)
        pos += 1
        for char in attachment["bone"]:
            dv.setUint8(pos, ord(char))
            pos += 1
        dv.setUint8(pos, 0)
        pos += 1

    offset_bone_names = pos
    for mesh in gr2.mesh_buffer.values():
        if mesh.bone_buffer:
            for bone in mesh.bone_buffer.values():
                for char in bone.name:
                    dv.setUint8(pos, ord(char))
                    pos += 1
                dv.setUint8(pos, 0)
                pos += 1

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # Cached offsets - each entry is (addr: uint32, value: uint32) = 8 bytes
    # on BOTH versions -- the *value* points at a 64-bit or 32-bit field
    # depending on version, but the cached-offsets table entry itself is
    # always this 8-byte (addr, value) pair. Which header addresses are
    # meaningful (and their bone-entry / mesh-header strides) differs by
    # version, matching each version's real header layout.
    if is64:
        dv.setUint32(pos, 0x50, 1)                               # addr: offsetCachedOffsets
        pos += 4
        dv.setUint32(pos, gr2.offset_cached_offsets, 1)
        pos += 4
        dv.setUint32(pos, 0x58, 1)                               # addr: offsetMeshHeaders (v5)
        pos += 4
        dv.setUint32(pos, gr2.offset_mesh_headers, 1)
        pos += 4
        dv.setUint32(pos, 0x60, 1)                               # addr: offsetMaterialNameOffsets (v5)
        pos += 4
        dv.setUint32(pos, gr2.offset_material_name_offsets, 1)
        pos += 4

        # Mesh header pointer entries - v5 mesh header is 64 bytes:
        #   +0:  meshNameOffset (uint64)
        #   +8:  lod+bitFlag1+numPieces+numBones (8 bytes)
        #   +16: bitFlag2+vertexSize+numVertices+numIndices (16 bytes)
        #   +32: offsetVertexBuffer (uint64)
        #   +40: offsetPieceHeaders (uint64)
        #   +48: offsetIndicesBuffer (uint64)
        #   +56: offsetBonesBuffer (uint64)
        for i, mesh in gr2.mesh_buffer.items():
            mesh_base = gr2.offset_mesh_headers + (i * 64)

            dv.setUint32(pos, mesh_base, 1)                      # addr: meshNameOffset field (+0)
            pos += 4
            dv.setUint32(pos, mesh.offset_mesh_name, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 32, 1)                 # addr: offsetVertexBuffer field (+32)
            pos += 4
            dv.setUint32(pos, mesh.offset_vertex_buffer, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 40, 1)                 # addr: offsetPieceHeaders field (+40)
            pos += 4
            dv.setUint32(pos, mesh.offset_piece_headers, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 48, 1)                 # addr: offsetIndicesBuffer field (+48)
            pos += 4
            dv.setUint32(pos, mesh.offset_indices_buffer, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 56, 1)                 # addr: offsetBonesBuffer field (+56)
            pos += 4
            dv.setUint32(pos, mesh.offset_bones_buffer, 1)
            pos += 4

        # Material name pointer entries - material name offsets table has
        # uint64 entries (stride 8)
        offset = offset_material_names
        for i, material_name in gr2.material_names.items():
            dv.setUint32(pos, gr2.offset_material_name_offsets + (8 * i), 1)  # addr: entry in table
            pos += 4
            dv.setUint32(pos, offset_material_names if i == 0 else offset, 1)  # value: string location
            offset += len(material_name) + 1
            pos += 4

        # Attachment name/bone-name pointer entries - each attachment entry is
        # 80 bytes (uint64 name ptr + uint64 bone name ptr + 16 floats).
        # `offset` continues from the material-name walk above, matching the
        # Strings section (attachment name/bone-name pairs come right after
        # material names).
        for i, attachment in enumerate(getattr(gr2, "attachment_bones", None) or []):
            attachment_base = gr2.offset_attachment_bones + (80 * i)
            dv.setUint32(pos, attachment_base, 1)          # addr: name ptr field (+0)
            pos += 4
            dv.setUint32(pos, offset, 1)                   # value: string location
            pos += 4
            offset += len(attachment["name"]) + 1

            dv.setUint32(pos, attachment_base + 8, 1)      # addr: bone name ptr field (+8)
            pos += 4
            dv.setUint32(pos, offset, 1)                   # value: string location
            pos += 4
            offset += len(attachment["bone"]) + 1

        # Bone name pointer entries - each bone entry is 32 bytes
        # (uint64 name ptr + 6 floats)
        offset = offset_bone_names
        for mesh in gr2.mesh_buffer.values():
            for i, bone in mesh.bone_buffer.items():
                dv.setUint32(pos, mesh.offset_bones_buffer + (32 * i), 1)      # addr: bone name ptr field
                pos += 4
                dv.setUint32(pos, offset_bone_names if i == 0 else offset, 1)  # value: string location
                offset += len(bone.name) + 1
                pos += 4

    else:
        dv.setUint32(pos, 80, 1)                     # 0x50
        pos += 4
        dv.setUint32(pos, gr2.offset_cached_offsets, 1)
        pos += 4
        dv.setUint32(pos, 84, 1)                     # 0x54
        pos += 4
        dv.setUint32(pos, gr2.offset_mesh_headers, 1)
        pos += 4
        dv.setUint32(pos, 88, 1)                     # 0x58
        pos += 4
        dv.setUint32(pos, gr2.offset_material_name_offsets, 1)
        pos += 4

        # Mesh header pointer entries - v4 mesh header is 40 bytes:
        #   +0:  meshNameOffset (uint32)
        #   +24: offsetVertexBuffer (uint32)
        #   +28: offsetPieceHeaders (uint32)
        #   +32: offsetIndicesBuffer (uint32)
        #   +36: offsetBonesBuffer (uint32)
        for i, mesh in gr2.mesh_buffer.items():
            mesh_base = gr2.offset_mesh_headers + (i * 40)

            dv.setUint32(pos, mesh_base, 1)              # addr: meshNameOffset field (+0)
            pos += 4
            dv.setUint32(pos, mesh.offset_mesh_name, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 24, 1)         # addr: offsetVertexBuffer field (+24)
            pos += 4
            dv.setUint32(pos, mesh.offset_vertex_buffer, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 28, 1)         # addr: offsetPieceHeaders field (+28)
            pos += 4
            dv.setUint32(pos, mesh.offset_piece_headers, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 32, 1)         # addr: offsetIndicesBuffer field (+32)
            pos += 4
            dv.setUint32(pos, mesh.offset_indices_buffer, 1)
            pos += 4
            dv.setUint32(pos, mesh_base + 36, 1)         # addr: offsetBonesBuffer field (+36)
            pos += 4
            dv.setUint32(pos, mesh.offset_bones_buffer, 1)
            pos += 4

        # Material name pointer entries - material name offsets table has
        # uint32 entries (stride 4)
        offset = offset_material_names
        for i, material_name in gr2.material_names.items():
            dv.setUint32(pos, gr2.offset_material_name_offsets + (4 * i), 1)
            pos += 4
            dv.setUint32(pos, offset_material_names if i == 0 else offset, 1)
            offset += len(material_name) + 1
            pos += 4

        # Attachment name/bone-name pointer entries - each attachment entry
        # is 72 bytes (uint32 name ptr + uint32 bone name ptr + 16 floats).
        # `offset` continues from the material-name walk above, matching the
        # Strings section (attachment name/bone-name pairs come right after
        # material names).
        for i, attachment in enumerate(getattr(gr2, "attachment_bones", None) or []):
            attachment_base = gr2.offset_attachment_bones + (72 * i)
            dv.setUint32(pos, attachment_base, 1)          # addr: name ptr field (+0)
            pos += 4
            dv.setUint32(pos, offset, 1)                   # value: string location
            pos += 4
            offset += len(attachment["name"]) + 1

            dv.setUint32(pos, attachment_base + 4, 1)      # addr: bone name ptr field (+4)
            pos += 4
            dv.setUint32(pos, offset, 1)                   # value: string location
            pos += 4
            offset += len(attachment["bone"]) + 1

        # Bone name pointer entries - each bone entry is 28 bytes
        # (uint32 name ptr + 6 floats)
        offset = offset_bone_names
        for mesh in gr2.mesh_buffer.values():
            for i, bone in mesh.bone_buffer.items():
                dv.setUint32(pos, mesh.offset_bones_buffer + (28 * i), 1)
                pos += 4
                dv.setUint32(pos, offset_bone_names if i == 0 else offset, 1)
                offset += len(bone.name) + 1
                pos += 4

    # Zero padding
    while (pos % 16) != 0:
        dv.setUint8(pos, 0)
        pos += 1

    # BNRY/LTLE. Byte-identical on both versions.
    # TODO: Figure out how to handle real collision data, always "no
    # collision" for now (see gr2_bnry_summary discussion re: round-trip data).
    dv.setBigUint64(pos, 0, 1)
    pos += 8
    dv.setBigUint64(pos, 0, 1)
    pos += 8
    dv.setBigUint64(pos, 0, 1)
    pos += 8
    dv.setBigUint64(pos, 0, 1)
    pos += 8

    # Bounding box
    dv.setFloat32(pos, gr2.bounds.min_x, 1)
    pos += 4
    dv.setFloat32(pos, gr2.bounds.min_y, 1)
    pos += 4
    dv.setFloat32(pos, gr2.bounds.min_z, 1)
    pos += 4
    dv.setFloat32(pos, gr2.bounds.max_x, 1)
    pos += 4
    dv.setFloat32(pos, gr2.bounds.max_y, 1)
    pos += 4
    dv.setFloat32(pos, gr2.bounds.max_z, 1)
    pos += 4
    dv.setUint32(pos, 0, 1)
    pos += 4
    for char in "EGCD":
        dv.setUint8(pos, ord(char))
        pos += 1
    dv.setUint32(pos, 5, 1)
    pos += 4
    dv.setUint32(pos, gr2.offset_BNRY, 1)
    pos += 4

    with open(path, 'wb') as file:
        dv.buffer.tofile(file)


def save(operator, context, path, ob, global_matrix=None, version=5):
    # type: (Operator, Context, str, Object, Optional[Matrix], int) -> bool
    from bpy_extras.wm_utils.progress_report import ProgressReport

    with ProgressReport(context.window_manager) as progress:
        progress.enter_substeps(3, f"Exporting \'{path}\' ...")

        if bpy.ops.object.mode_set.poll():
            # Enter edit mode.
            bpy.ops.object.mode_set(mode='EDIT')

            # Sort vertices, faces and edges by material index.
            bpy.ops.mesh.sort_elements(type='MATERIAL')

            # Exit edit mode.
            bpy.ops.object.mode_set(mode='OBJECT')

        try:
            bmesh = ob.to_mesh()
        except RuntimeError:
            return False

        # calc_tangents() below cannot handle n-gons (5+ sided faces) at all --
        # Blender's own error is explicit that only tris/quads are supported --
        # and unlike quads, n-gons have no valid fallback representation in
        # GR2 either way, so there's no "leave it as-is" option the way the
        # Triangulate toggle provides for quads. Real triangulation (not just
        # the read-only calc_loop_triangles() view used later in parse())
        # is needed here, so a temporary BMesh is used, only touching n-gon
        # faces -- quads and tris are left completely alone, so this doesn't
        # affect or bypass the Triangulate toggle's handling of quads at all.
        if any(len(p.vertices) > 4 for p in bmesh.polygons):
            bm = bmesh_module.new()
            bm.from_mesh(bmesh)
            ngon_faces = [f for f in bm.faces if len(f.verts) > 4]
            bmesh_module.ops.triangulate(bm, faces=ngon_faces, quad_method='BEAUTY', ngon_method='BEAUTY')
            bm.to_mesh(bmesh)
            bm.free()

        if global_matrix:
            bmesh.transform(ob.matrix_world @ global_matrix)
        else:
            bmesh.transform(ob.matrix_world)

        # If negative scaling we have to invert the normals...
        if ob.matrix_world.determinant() < 0.0:
            bmesh.flip_normals()

        # Make sure there is something to write
        if not len(bmesh.polygons) + len(bmesh.vertices):
            ob.to_mesh_clear()  # Clean-up

        # Calculate normals and tangents.
        if bmesh.polygons:
            # Custom split normals are derived automatically, so calc_tangents()
            # needs no prior explicit split-normals pass.
            bmesh.calc_tangents()

        progress.step(f"Parsing Blender Object: \'{ob.name}\' ...", 1)
        mesh = parse(ob, bmesh, has_clo=operator.has_clo, version=version, operator=operator,
                     triangulate=operator.triangulate)

        if mesh:
            progress.step("Done, writing file ...", 2)
            write(mesh, path)
            progress.leave_substeps(f"Done, finished exporting: \'{path}\'")

            return True
        else:
            return False
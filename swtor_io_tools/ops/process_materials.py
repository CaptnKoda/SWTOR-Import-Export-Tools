# <pep8 compliant>

"""
.mat-by-Name Materials Processing.

Builds real "Atroxa Shaders" native materials for existing Blender
materials purely by matching their *name* against a real .mat file on
disk -- entirely independent of any Jedipedia NPC/Character json data.
Two entry points share this module's pipeline:

- Automatic (7a): apply_materials_by_name_after_import(), called by
  ops/import_gr2.py's load() right after a genuine standalone .gr2
  import (never the NPC/Character (.json) importer's own internal gr2
  loads -- see that module's gating for why).
- Manual (7b): the three swtor.apply_materials_by_name* operators below
  plus their Shift+A submenu entry, appended onto the live (non-
  deprecated) NODE_MT_swtor_shaders_menu, mirroring ops/migrate_shaders.
  py's exact precedent for the deprecated system's own submenu.

Both funnel through _build_named_material(): Tier 1 (types/mat.py's
read_mat_summary -- alpha + skip-list + shader-family aliasing) + Tier 2
(types/mat.py's read_mat_full -- the map/palette adapter) +
ops/material_builder.py's existing get_or_create_material(), unchanged.

get_or_create_material() lives in ops/material_builder.py, which imports
nothing from ops/import_cha.py, ops/import_gr2.py or this module, so it
is imported normally (top-level) here -- no import cycle, unlike when it
lived in import_cha.py (import_gr2 -> process_materials -> import_cha ->
import_gr2).
"""

import json
from typing import Any, List, Optional, Tuple

import bpy

from .material_builder import UnrecognizedDerivedTypeError, get_or_create_material, is_head_object_name
from ..types.mat import (
    KNOWN_UNBUILT_DERIVED,
    read_mat_full,
    read_mat_summary,
    resolve_mat_path,
)
from ..types.shader_templates import has_swtor_shader_group, template_copies


# ---------------------------------------------------------------------------
# Shared core (Tier 1 + Tier 2 + get_or_create_material())
# ---------------------------------------------------------------------------

class MaterialProcessResult:
    """
    Per-material outcome, for the operators' self.report() summaries.

    status is one of:
        "built"        -- a native SWTOR shader was (re)built. Check
                          missing_assets below -- "built" doesn't
                          guarantee every texture was actually found.
        "skipped"      -- deliberately not processed; see `detail`
                          ("already built" or "skip-listed name").
        "not_found"    -- no matching .mat file under either Resources
                          Directory.
        "unbuilt_type" -- resolved to a recognized-but-not-yet-supported
                          shader type (KNOWN_UNBUILT_DERIVED, currently
                          empty, or something DERIVED_CONFIGS doesn't
                          cover at all); `detail` names the type.
        "error"        -- the .mat file couldn't be parsed, or
                          get_or_create_material() itself raised;
                          `detail` has the message. Never propagates --
                          batch callers keep going through the rest of
                          their list.

    missing_assets is a list of (mat_name, relative_path) pairs -- one
    per texture that's genuinely referenced by this material but
    couldn't be found under either Resources Directory (see
    get_or_create_material()'s own missing_assets parameter). Always
    empty except on a "built" result: soft-fail, not an error -- the
    material still gets built, just with that Image Texture node left
    unassigned, same as import_cha.py's NPC pipeline.
    """
    __slots__ = ("name", "status", "detail", "missing_assets")

    def __init__(self, name, status, detail="", missing_assets=None):
        self.name = name
        self.status = status
        self.detail = detail
        self.missing_assets = missing_assets if missing_assets is not None else []


def get_resources_dirs():
    # type: () -> Tuple[Optional[str], Optional[str]]
    prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
    return prefs.swtor_resources_dir or None, prefs.swtor_legacy_resources_dir or None


def _build_named_material(mat_name, object_name=None):
    # type: (str, Optional[str]) -> Tuple[Optional[Any], MaterialProcessResult]
    """
    Core Tier 1 + Tier 2 + get_or_create_material() pipeline, shared by
    both entry points below.

    `mat_name` is looked up both on disk (against
    <resources>/art/shaders/materials/<mat_name>.mat) and in
    bpy.data.materials (via get_or_create_material()'s own by-name
    dedup) -- callers are responsible for passing the right name for
    their situation: the *original* SWTOR name (7a, from
    "gr2_material_names") or the material's own current Blender name
    (7b, matching zg_swtor_tools' original precedent and its same
    name-mangling caveat).

    Returns (material, result). material is None unless result.status
    is "built" or "skipped" with detail "already built".
    """
    existing = bpy.data.materials.get(mat_name)
    if existing is not None and has_swtor_shader_group(existing):
        return existing, MaterialProcessResult(mat_name, "skipped", "already built")

    primary_dir, legacy_dir = get_resources_dirs()
    if not primary_dir and not legacy_dir:
        return None, MaterialProcessResult(mat_name, "error", "no Resources Directory configured")

    mat_path, root_used = resolve_mat_path(mat_name, primary_dir, legacy_dir)
    if mat_path is None:
        return None, MaterialProcessResult(mat_name, "not_found")

    summary = read_mat_summary(mat_path, mat_name)
    if summary is None:
        return None, MaterialProcessResult(mat_name, "error", "couldn't parse .mat file")

    if summary.skip:
        return None, MaterialProcessResult(mat_name, "skipped", "skip-listed name")

    if summary.derived in KNOWN_UNBUILT_DERIVED:
        return None, MaterialProcessResult(mat_name, "unbuilt_type", summary.derived)

    mat_info = read_mat_full(mat_path, mat_name)
    if mat_info is None:
        return None, MaterialProcessResult(mat_name, "error", "couldn't parse .mat file")

    existed_before = existing is not None   # a same-named material that wasn't one of ours

    try:
        # NOTE: passing primary_dir/legacy_dir here, NOT root_used/None.
        # root_used only tells us where the .mat FILE resolved -- but a
        # material's .dds textures can live under a different root
        # entirely (confirmed real case, see ops/material_builder.py's
        # get_or_create_material() docstring), so its own per-asset
        # fallback needs both real roots to have any effect. The .mat
        # file itself gets re-resolved once more inside that call
        # (redundant with root_used above, but cheap and harmless) since
        # it always derives mat_path fresh from mat_info's own "matPath"
        # key regardless of caller.
        missing_assets = []
        material = get_or_create_material(
            primary_dir, legacy_dir, mat_name, summary.derived, mat_info,
            missing_assets=missing_assets,
            detect_skinb_invert_alpha=is_head_object_name(object_name),
            # A real .mat routinely omits a map its derived type lists as
            # required (grass with no GlossMap, ...): leave it at the
            # template default instead of failing the whole material.
            strict_required_maps=False,
        )
    except UnrecognizedDerivedTypeError as exc:
        # DERIVED_CONFIGS doesn't cover this type at all (distinct from
        # the KNOWN_UNBUILT_DERIVED bucket above, which is caught
        # earlier -- this is a genuinely unrecognized <Derived> value).
        # A specific exception type, not a plain ValueError -- see
        # UnrecognizedDerivedTypeError's own docstring for why that
        # distinction matters here specifically.
        return None, MaterialProcessResult(mat_name, "unbuilt_type", str(exc))
    except Exception as exc:  # noqa: BLE001 -- defensive, mirrors migrate_shaders.py
        _discard_partial_material(mat_name, existed_before)
        return None, MaterialProcessResult(mat_name, "error", str(exc))

    return material, MaterialProcessResult(mat_name, "built", missing_assets=missing_assets)


def _discard_partial_material(mat_name, existed_before):
    # type: (str, bool) -> None
    """
    get_or_create_material() creates the material (with its shader group)
    BEFORE it loads textures and sets inputs, so an exception part-way leaves
    a material that has_swtor_shader_group() calls "already built" -- every
    later request for that name would then be handed the half-built one as a
    success (or, if a purge swept it first, rebuilt and failed all over
    again: the outcome depended on timing). Remove what this call created.
    A pre-existing same-named material (existed_before) is left alone.
    """
    if existed_before:
        return
    leftover = bpy.data.materials.get(mat_name)
    if leftover is not None:
        bpy.data.materials.remove(leftover)


# ---------------------------------------------------------------------------
# 7b -- manual entry point (operates on already-existing materials, by
# their own current Blender name)
# ---------------------------------------------------------------------------

def apply_materials_by_name_to_materials(materials_with_objects):
    # type: (List[Tuple[Any, Optional[str]]]) -> List[MaterialProcessResult]
    """
    (Re)builds each already-existing material in `materials_with_objects`
    in place, looked up by its own current Blender name (material.name)
    -- the same name-mangling caveat zg_swtor_tools' original tool
    accepted (a Blender-renamed-on-collision material, e.g. "foo.001",
    simply won't resolve to a real "foo.mat"; rename it back to match if
    that happens).

    Each pair's second element is only used for SkinB's head Invert
    Alpha auto-detect (see get_or_create_material()'s docstring) -- pass
    a representative object's name, or None.
    """
    results = []
    for material, object_name in materials_with_objects:
        _, result = _build_named_material(material.name, object_name)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# 7a -- automatic entry point (operates on freshly-.gr2-imported objects,
# by each material slot's *original* SWTOR name)
# ---------------------------------------------------------------------------

def apply_materials_by_name_to_gr2_objects(object_names):
    # type: (List[str]) -> List[MaterialProcessResult]
    """
    For every newly-.gr2-imported object in `object_names` that carries
    the "gr2_material_names" round-trip property (see import_gr2.py's
    build()), resolves each material slot's *original* SWTOR material
    name -- not the possibly Blender-renamed-on-collision bpy.data.
    materials name of the placeholder build() created for it -- against
    a real .mat file, and (re)points that slot at the correctly-deduped
    canonical material.

    The explicit slot re-pointing matters: build()'s placeholder
    materials and get_or_create_material()'s own by-name dedup are two
    independent mechanisms that can each land on a differently-named
    material for the same logical SWTOR material if Blender had to
    auto-suffix one of them on a name collision (e.g. a second .gr2
    import reusing the same material name) -- without this, the newly-
    imported object's slot could keep pointing at an unbuilt ".001"
    placeholder while some unrelated, earlier-created material of the
    same base name gets rebuilt instead.
    """
    results = []
    # Every instance of a mesh shares its mesh data, and the slots are set ON that
    # data, so a repeat instance has nothing left to do: (mesh, material names) ->
    # that mesh's results, replayed for the repeats (a material built for the first
    # instance counts as "already built" for the rest, exactly what asking again gave).
    done_meshes = {}
    # bpy.data.objects.get(name) is a linear scan of every object in the file (about
    # half a millisecond with 16,000 of them), once per object here: one dictionary
    # built up front does the same lookups instantly.
    objects_by_name = {candidate.name: candidate for candidate in bpy.data.objects}
    # One block so materials of a shader type are copied, not re-appended -- see
    # shader_templates.template_copies().
    with template_copies():
        for object_name in object_names:
            ob = objects_by_name.get(object_name)
            if ob is None or ob.type != 'MESH':
                continue

            raw = ob.get("gr2_material_names")
            if not raw:
                continue

            mesh_key = (ob.data.name, raw)
            earlier = done_meshes.get(mesh_key)
            if earlier is not None:
                for r in earlier:
                    if r.status == "built":
                        results.append(MaterialProcessResult(r.name, "skipped", "already built"))
                    else:
                        results.append(MaterialProcessResult(r.name, r.status, r.detail, list(r.missing_assets)))
                continue

            try:
                original_names = json.loads(raw)
            except (ValueError, TypeError):
                continue

            own = []
            for slot_index, mat_name in enumerate(original_names):
                if slot_index >= len(ob.data.materials):
                    break
                material, result = _build_named_material(mat_name, ob.name)
                own.append(result)
                if material is not None:
                    ob.data.materials[slot_index] = material
            results.extend(own)
            # An "error" is retried for each repeat, as before; everything else is settled.
            if not any(r.status == "error" for r in own):
                done_meshes[mesh_key] = own
    return results


def apply_materials_by_name_after_import(object_names):
    # type: (List[str]) -> None
    """
    7a entry point proper -- called by ops/import_gr2.py's load() right
    after a genuine standalone .gr2 import. Silent by design: no
    self.report(), since there's no useful interactive operator context
    for an automatic, best-effort pass to surface warnings to.
    Materials with no matching .mat file are left alone, no error/print
    at all (handoff §7a) -- only genuine failures print to console.
    """
    results = apply_materials_by_name_to_gr2_objects(object_names)
    for result in results:
        if result.status == "error":
            print(f"[SWTOR materials-by-name] Failed to process '{result.name}': {result.detail}")


# ---------------------------------------------------------------------------
# Reporting (mirrors ops/migrate_shaders.py's _migrate_and_report())
# ---------------------------------------------------------------------------

def _summarize_and_report(operator, results):
    # type: (bpy.types.Operator, List[MaterialProcessResult]) -> None
    built = sum(1 for r in results if r.status == "built")
    already_built = sum(1 for r in results if r.status == "skipped" and r.detail == "already built")
    skip_listed = sum(1 for r in results if r.status == "skipped" and r.detail == "skip-listed name")
    not_found = [r.name for r in results if r.status == "not_found"]
    unbuilt = [(r.name, r.detail) for r in results if r.status == "unbuilt_type"]
    errors = [(r.name, r.detail) for r in results if r.status == "error"]
    # (mat_name, relative_path) pairs across every "built" result -- see
    # MaterialProcessResult's own docstring re: "built" not guaranteeing
    # every texture was actually found (soft-fail, not an error).
    missing_assets = [pair for r in results for pair in r.missing_assets]

    for name in not_found:
        print(f"[SWTOR materials-by-name] No matching .mat file found for '{name}'")
    for name, derived in unbuilt:
        print(f"[SWTOR materials-by-name] '{name}' is a recognized but not-yet-supported shader type ({derived})")
    for name, err in errors:
        print(f"[SWTOR materials-by-name] Failed to process '{name}': {err}")
    if missing_assets:
        print(f"[SWTOR materials-by-name] {len(missing_assets)} texture(s) couldn't be found under either Resources Directory:")
        for mat_name, rel_path in missing_assets:
            print(f"[SWTOR materials-by-name]   - '{mat_name}': {rel_path}")

    msg = f"Built {built} material(s), {already_built + skip_listed} already/deliberately skipped"
    if not_found:
        msg += f", {len(not_found)} with no matching .mat file (see console)"
    if unbuilt:
        msg += f", {len(unbuilt)} of a not-yet-supported type (see console)"
    if missing_assets:
        msg += f", {len(missing_assets)} texture(s) not found under either Resources Directory (see console)"
    if errors:
        msg += f", {len(errors)} failed (see console)"

    if errors:
        operator.report({'ERROR'}, msg)
    elif not_found or unbuilt or missing_assets:
        operator.report({'WARNING'}, msg)
    else:
        operator.report({'INFO'}, msg)


# ---------------------------------------------------------------------------
# 7b operators -- same three-operator scope split as ops/migrate_shaders.
# py's precedent (Current Material / Selected Objects / Whole File),
# appended onto the live NODE_MT_swtor_shaders_menu (not the deprecated
# one) via the same Shift+A submenu-append technique.
# ---------------------------------------------------------------------------

class SWTOR_OT_apply_materials_by_name(bpy.types.Operator):
    """Looks up this material's name against a real .mat file in the Resources Directory and builds/rebuilds its native SWTOR shader"""
    bl_idname = 'swtor.apply_materials_by_name'
    bl_label = 'Apply Material By Name (Current Material)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.material is not None

    def execute(self, context):
        material = context.material
        object_name = context.active_object.name if context.active_object else None
        result = apply_materials_by_name_to_materials([(material, object_name)])[0]

        # result.name (not material.name) throughout: get_or_create_
        # material() may have replaced/deleted the original `material`
        # datablock (see its own docstring -- a same-named-but-not-ours
        # material gets user_remap()'d and removed), so `material` can be
        # a stale reference by this point. result.name was captured as a
        # plain string BEFORE that could happen (see
        # apply_materials_by_name_to_materials()), so it's always safe.
        if result.status == "built":
            if result.missing_assets:
                preview = ", ".join(rel_path for _, rel_path in result.missing_assets[:5])
                remainder = len(result.missing_assets) - 5
                if remainder > 0:
                    preview += f", and {remainder} more"
                self.report(
                    {'WARNING'},
                    f"Built native SWTOR shader for '{result.name}', but {len(result.missing_assets)} "
                    f"texture(s) couldn't be found under either Resources Directory: {preview}",
                )
                print(f"[SWTOR materials-by-name] Built '{result.name}', but {len(result.missing_assets)} texture(s) couldn't be found under either Resources Directory:")
                for _, rel_path in result.missing_assets:
                    print(f"[SWTOR materials-by-name]   - {rel_path}")
            else:
                self.report({'INFO'}, f"Built native SWTOR shader for '{result.name}'")
        elif result.status == "skipped":
            if result.detail == "skip-listed name":
                self.report({'INFO'}, f"Skipped: '{result.name}' is not a valid .mat file name")
            else:
                self.report({'INFO'}, f"'{result.name}' already has a native SWTOR shader")
        elif result.status == "not_found":
            self.report({'WARNING'}, f"No matching .mat file found for '{result.name}'")
        elif result.status == "unbuilt_type":
            self.report({'WARNING'}, f"'{result.name}' is a recognized but not-yet-supported shader type ({result.detail})")
        else:
            self.report({'ERROR'}, f"Failed to process '{result.name}': {result.detail}")

        return {'FINISHED'}


class SWTOR_OT_apply_materials_by_name_selected(bpy.types.Operator):
    """Looks up every material name across all selected objects against real .mat files in the Resources Directory and builds/rebuilds their native SWTOR shaders"""
    bl_idname = 'swtor.apply_materials_by_name_selected'
    bl_label = 'Apply Materials By Name (Selected Objects)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0

    def execute(self, context):
        seen = set()
        materials_with_objects = []
        for ob in context.selected_objects:
            for slot in ob.material_slots:
                material = slot.material
                if material is None or material.name in seen:
                    continue
                seen.add(material.name)
                materials_with_objects.append((material, ob.name))

        results = apply_materials_by_name_to_materials(materials_with_objects)
        _summarize_and_report(self, results)
        return {'FINISHED'}


class SWTOR_OT_apply_materials_by_name_file(bpy.types.Operator):
    """Looks up every material name in this .blend against real .mat files in the Resources Directory and builds/rebuilds their native SWTOR shaders"""
    bl_idname = 'swtor.apply_materials_by_name_file'
    bl_label = 'Apply Materials By Name (Whole File)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        materials_with_objects = [(material, None) for material in bpy.data.materials]
        results = apply_materials_by_name_to_materials(materials_with_objects)
        _summarize_and_report(self, results)
        return {'FINISHED'}


# Shift+A discoverability -- appended (not authored) onto the existing,
# live NODE_MT_swtor_shaders_menu (ops/shaders_menu.py), below a
# separator from its seven "Add [X] Shader" entries. Mirrors ops/
# migrate_shaders.py's swtor_migrate_submenu_element() exactly, except
# targeting the live menu instead of the deprecated one -- this feature
# isn't deprecated-adjacent, so, unlike migrate_shaders.py, this module
# imports Tier 1/Tier 2 and get_or_create_material() directly
# rather than keeping a local duplicate for isolation.
def swtor_process_materials_submenu_element(self, context):
    # type: (bpy.types.Menu, bpy.types.Context) -> None
    layout = self.layout
    layout.separator()
    layout.operator(SWTOR_OT_apply_materials_by_name.bl_idname, text="Apply Material By Name (Current Material)")
    layout.operator(SWTOR_OT_apply_materials_by_name_selected.bl_idname, text="Apply Materials By Name (Selected Objects)")
    layout.operator(SWTOR_OT_apply_materials_by_name_file.bl_idname, text="Apply Materials By Name (Whole File)")
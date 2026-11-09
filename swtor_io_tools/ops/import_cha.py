# <pep8 compliant>

"""
Merged SWTOR NPC/Character importer created by Crunch.

Replaces the old import_cha.py (IO Scene GR2) + char_character_assembler.py
(ZG_Tools) split. Reads directly from a local "resources" folder (mirroring
the game's extracted asset layout) instead of copying assets into a
per-character folder first.

Targets Jedipedia.net's json export format exclusively.
No support for older TORC exports with none planned.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import bpy
import bmesh
from mathutils import Vector, Matrix

from .import_gr2 import load as ImportGR2_load
from .material_builder import get_or_create_material, is_head_object_name
from .process_materials import get_resources_dirs
from ..types.mat import read_mat_full
from ..types.shared import job_results, resolve_resource_path, resolve_resource_path_multi


# ---------------------------------------------------------------------------
# JSON parsing (spec §3, §5)
# ---------------------------------------------------------------------------

def read(filepath):
    # type: (str) -> Tuple[Dict[str, Any], Dict[str, List[Dict]], List[Dict]]
    """
    Parses a Jedipedia-formatted NPC/PC json file.

    This is a pure parser: it doesn't touch the resources folder, doesn't
    validate that the file "looks like" a character file, and doesn't
    raise on a missing "meta" entry (current live Jedipedia exports don't
    emit one yet) — callers decide how to handle those cases.

    Returns:
        meta:
            Dict of the parsed "meta" entry's fields (charType, charName,
            nppPath, bodyType, errors). Empty dict if no "meta" entry is
            present. bodyType drives skeleton resolution -- see
            import_skeleton()'s docstring for how.

        slots:
            Dict mapping slotName -> ordered list of raw entry dicts, in
            the order they appeared in the file. A slotName with more
            than one entry represents multiple import variants (spec §5)
            — e.g. two "head" entries for two alternate head sculpts.
            The reserved "skinMats" slotName is excluded here; see
            skin_mats below.

        skin_mats:
            List of {"slot_name": ..., "mat_info": ...} dicts drawn from
            the "skinMats" entry's materialInfo.mats, flattened for easy
            lookup later (spec §6's SkinB heuristic). Empty list if the
            "skinMats" entry is absent or has no mats.
    """
    with open(filepath, encoding="utf-8") as file:
        data: List[Dict[str, Any]] = json.load(file)

    meta: Dict[str, Any] = {}
    slots: Dict[str, List[Dict]] = {}
    skin_mats: List[Dict] = []

    for entry in data:
        slot_name = entry.get("slotName")
        if not slot_name:
            # Forward-compat: entries we don't recognize (yet) are
            # skipped rather than breaking the importer.
            continue

        if slot_name == "meta":
            meta = {
                "charType": entry.get("charType"),
                "charName": entry.get("charName"),
                "nppPath": entry.get("nppPath"),
                "bodyType": entry.get("bodyType"),
                "errors": entry.get("errors", []),
            }
            continue

        if slot_name == "skinMats":
            mats = entry.get("materialInfo", {}).get("mats", [])
            for mat in mats:
                # The source shape spreads matPath (inside "materialInfo"),
                # "ddsPaths" and "otherValues" across three separate keys
                # on the same mat dict. Consolidate them into a single
                # mat_info dict shaped the same way an ordinary slot
                # entry's materialInfo is, so downstream code (§6) can
                # treat skin_mats entries and ordinary slot mat_infos
                # uniformly.
                mat_info = dict(mat.get("materialInfo", {}))
                mat_info["ddsPaths"] = mat.get("ddsPaths", {})
                mat_info["otherValues"] = mat.get("otherValues", {})
                skin_mats.append({
                    "slot_name": mat.get("slotName"),
                    "mat_info": mat_info,
                })
            continue

        slots.setdefault(slot_name, []).append(entry)

    return meta, slots, skin_mats


# ---------------------------------------------------------------------------
# NPC material policy: json + .mat merge, head detection
#
# Moved here from ops/material_builder.py -- both only make sense for this
# importer (only it has Jedipedia json; ops/process_materials.py builds
# straight from .mat data). The builder itself takes finished mat_info.
# ---------------------------------------------------------------------------

# The subset of DERIVED_CONFIGS' "maps" json_keys that a real .mat file's
# own <input> block genuinely defines as the material's baked identity --
# these are what the use_mat_dds_paths option overrides from json. Everything else a
# "maps" tuple can list (complexionMap/facepaintMap/ageMap) is per-NPC
# customization data a .mat file was never meant to represent -- its own
# baked-in values for those are just generic art/defaultassets/*
# placeholders (confirmed against real head_human_bms_african_a01c01.mat:
# ComplexionMap/FacepaintMap/AgeMap all resolve to exactly this addon's
# own DEFAULT_MAP_ASSETS fallback paths, verbatim) -- always left
# json-sourced regardless of use_mat_dds_paths, same as every one of
# otherValues' palette/specular/flush/flesh values (also confirmed
# generic/non-NPC-specific in the .mat file's own data).
_MAT_SOURCED_DDS_KEYS = {
    "diffuseMap", "rotationMap", "glossMap", "paletteMap", "paletteMaskMap", "directionMap",
    "animatedTexture1", "animatedTexture2",
}


def _apply_mat_dds_overrides(mat_info, mat_path, mat_name):
    # type: (Dict[str, Any], Optional[Path], str) -> Dict[str, Any]
    """
    Returns a NEW dict (mat_info itself is never mutated -- callers may
    be sharing it, e.g. a skinMats entry reused across several objects)
    with _MAT_SOURCED_DDS_KEYS' ddsPaths entries replaced by whatever the
    real .mat file at mat_path actually defines for them (Tier 2,
    types/mat.py's read_mat_full() -- reads each <input>'s <value> field,
    NOT <variable>, which is stale/incorrect per Crunch).

    A key .mat_full doesn't define at all (e.g. directionMap on a
    non-Creature/HairC material) is left exactly as json had it --
    absence isn't itself a signal to blank out an existing json value.

    Falls back to mat_info completely unchanged if mat_path is None or
    the .mat file can't be parsed -- soft-fail, same as every other
    resolution step in this pipeline; a single material with a broken
    .mat file just reverts to pure json behavior rather than blocking
    the import.
    """
    if mat_path is None:
        return mat_info

    mat_full = read_mat_full(mat_path, mat_name)
    if mat_full is None:
        return mat_info

    merged_dds_paths = dict(mat_info.get("ddsPaths", {}))
    for key in _MAT_SOURCED_DDS_KEYS:
        if key in mat_full["ddsPaths"]:
            merged_dds_paths[key] = mat_full["ddsPaths"][key]

    merged = dict(mat_info)
    merged["ddsPaths"] = merged_dds_paths
    return merged


def _material_roots(operator, resources_root, legacy_resources_root):
    # type: (Any, str, Optional[str]) -> Tuple[Optional[str], Optional[str]]
    """
    Returns the (first, second) root pair used for MATERIAL lookups only
    (.mat merge, .mat alpha read, .dds textures, default-map fallbacks).

    With the operator's `prefer_legacy_materials` on AND a valid Legacy
    Resources Directory, the pair is swapped so Legacy is tried first and the
    regular root is the per-asset fallback. Otherwise the pair is returned
    unchanged.

    The directory-validity guard is required, not cosmetic: swapping with no
    Legacy directory would make the first root None, and when nothing resolves,
    resolve_resource_path_multi() falls back to
    resolve_resource_path(first_root, ...), which raises TypeError on
    Path(None).

    Never use this for models or skeletons: those stay on `resources_root`.
    """
    if (
        getattr(operator, "prefer_legacy_materials", False)
        and legacy_resources_root
        and Path(legacy_resources_root).is_dir()
    ):
        return legacy_resources_root, resources_root
    return resources_root, legacy_resources_root


def _build_npc_material(resources_root, legacy_resources_root, mat_name, derived, mat_info,
                        object_name, missing_assets, use_mat_dds_paths):
    # type: (str, Optional[str], str, str, Dict[str, Any], Optional[str], Optional[List[Tuple[str, str]]], bool) -> Any
    """
    The NPC importer's one entry point to ops/material_builder.py's
    get_or_create_material(): applies this importer's own policy first --
    the json+.mat ddsPaths merge when use_mat_dds_paths is on (see
    _apply_mat_dds_overrides() above for why), and the "is this a head?"
    decision that gates SkinB's Invert Alpha auto-detect -- then hands the
    builder finished data.

    Note the merge now runs before the builder's own dedup check, so an
    already-built material costs one extra .mat parse per call. Negligible
    at NPC scale (a handful of slots), and it keeps the builder free of any
    json awareness.
    """
    if use_mat_dds_paths:
        mat_path, _root_used = resolve_resource_path_multi(resources_root, legacy_resources_root, mat_info.get("matPath"))
        mat_info = _apply_mat_dds_overrides(mat_info, mat_path, mat_name)

    return get_or_create_material(
        resources_root, legacy_resources_root, mat_name, derived, mat_info,
        missing_assets=missing_assets,
        detect_skinb_invert_alpha=is_head_object_name(object_name),
    )


# ---------------------------------------------------------------------------
# Material-slot-index -> json-material-data heuristics (spec §6)
# ---------------------------------------------------------------------------

def _find_skin_mat(skin_mats, slot_name):
    for skin_mat in skin_mats:
        if skin_mat.get("slot_name") == slot_name:
            return skin_mat
    return None


def resolve_material_source(entry, index, skin_mats, npc_uses_skin):
    # type: (Dict[str, Any], int, List[Dict], bool) -> Tuple[str, Dict[str, Any]]
    """
    Determines which json material data (and which "derived" shader type)
    governs a given material-slot index on an imported mesh.

    The json only ever fully describes material-slot 0 (the entry's own
    materialInfo) and, for head/creature slots, an additional eyeMatInfo
    block -- it does not say which mesh material-slot indices actually
    exist, or what a 2nd slot on non-head/creature gear represents. This
    heuristic chain is inherited from the original importer, since
    Jedipedia exports don't include "materialSkinIndex" data that would
    make this unambiguous.

    Returns (derived, mat_info).
    """
    slot_name = entry["slotName"]
    mat_info = entry["materialInfo"]
    other_values = mat_info.get("otherValues", {})
    derived = other_values.get("derived")
    derived = "Creature" if derived == "HighQualityCharacter" else derived

    if index == 1:
        if slot_name in ("head", "creature"):
            eye_mat_info = mat_info.get("eyeMatInfo", {})

            # eyeMatInfo self-describes its own type via its own
            # otherValues.derived -- confirmed against real data: a
            # genuine Eye's eyeMatInfo.otherValues.derived is "Eye"
            # (Tatooine/Balmorra heads), while a 2nd material slot that
            # is actually a second Creature material (not an eye at
            # all) has eyeMatInfo.otherValues.derived == 
            # "HighQualityCharacter" (-> Creature) instead, e.g. Malgus.
            # This replaces an earlier, less reliable heuristic (a
            # malformed paletteMap path as the signal) that was written
            # before any real "creature"-slot data was available to
            # check it against, and which only applied to slot_name ==
            # "creature", never "head" -- this direct read applies
            # equally to both, and generalizes correctly to whatever
            # eyeMatInfo actually says.
            eye_other_values = eye_mat_info.get("otherValues", {})
            eye_derived = eye_other_values.get("derived", "Eye")
            eye_derived = "Creature" if eye_derived == "HighQualityCharacter" else eye_derived

            return eye_derived, eye_mat_info

        material_skin_index = other_values.get("materialSkinIndex")
        if material_skin_index is not None:
            if int(material_skin_index) == index:
                derived = "SkinB"
        else:
            # Jedipedia doesn't currently emit materialSkinIndex at all,
            # so this operator-toggle guess is the normal path in
            # practice.
            if npc_uses_skin:
                derived = "SkinB"

    # Whenever the resolved derived is SkinB -- whether natively so on
    # the slot's own primary materialInfo (e.g. "hand", which can BE
    # skin at index 0, no override needed), or via the guess/
    # materialSkinIndex path above at index 1 -- a matching skinMats
    # entry for this slot_name, if one exists, is preferred over the
    # slot's own mat_info. Confirmed bug fix: the original importer's
    # "elif derived == 'SkinB':" branch always did this lookup
    # regardless of why derived ended up being SkinB; an earlier
    # revision of this rewrite only did it for the index-1 guess path,
    # silently using a slot's own (sometimes placeholder/wrong) palette
    # data whenever it was natively SkinB already.
    if derived == "SkinB":
        skin_mat = _find_skin_mat(skin_mats, slot_name)
        if skin_mat is not None:
            return "SkinB", skin_mat["mat_info"]

    return derived, mat_info


# ---------------------------------------------------------------------------
# Variant import (spec §5, §10's import_variant())
# ---------------------------------------------------------------------------

def _job_results_key(filepath):
    """
    Mirrors import_gr2.py's own key-normalization for
    job_results['files_objs_names'], so lookups against it line up. See
    that module's load() for the authoritative version of this logic.
    """
    normalized = filepath.replace("\\", "/")
    if "resources" in normalized:
        return normalized.partition("resources/")[2]
    return normalized


def import_variant(operator, context, resources_root, legacy_resources_root, entry, skin_mats, missing_assets=None, use_mat_dds_paths=True):
    # type: (Any, Any, str, Optional[str], Dict[str, Any], List[Dict], Optional[List[Tuple[str, str]]], bool) -> List[Any]
    """
    Imports every model listed in a single slot entry (spec §5's "one
    variant") and assigns materials to every resulting Blender object --
    a single .gr2 can produce more than one object (multi-mesh files),
    and all of them get materials now, not just the first one (confirmed
    fix; the original importer only handled the first).

    missing_assets and use_mat_dds_paths are passed straight through to
    _build_npc_material() -- see its (and _apply_mat_dds_overrides()'s)
    docstring.

    Models always resolve against `resources_root`. Materials use the root
    pair from _material_roots(), which the operator's `prefer_legacy_materials`
    may swap (Legacy first) -- the swap is deliberately confined to the
    material lookups so models/skeletons never leave the primary tree.

    Returns the list of imported Objects. Empty if entry["models"] is
    empty -- a valid "none" variant (e.g. a bald/clean-shaven option),
    not an error.
    """
    imported_objects = []

    # Root pair for materials only; computed once per variant, not per slot.
    # `resources_root` itself is left untouched: models below always resolve
    # against it (see _material_roots() / prefer_legacy_materials).
    mat_primary, mat_secondary = _material_roots(operator, resources_root, legacy_resources_root)

    for model_path in entry.get("models", []):
        resolved_path = resolve_resource_path(resources_root, model_path)

        # Rich results are required so we can look up exactly which
        # object(s) this specific import call produced -- a plain
        # bpy.data.objects[name] guess breaks whenever two variants
        # share the same source .gr2 filename (confirmed real case:
        # multiple head sculpts reusing one mesh with different
        # materials).
        operator.job_results_rich = True

        ImportGR2_load(operator, context, resolved_path)

        object_names = job_results.get("files_objs_names", {}).get(
            _job_results_key(resolved_path), []
        )

        for object_name in object_names:
            ob = bpy.data.objects[object_name]
            imported_objects.append(ob)

            for index in range(len(ob.material_slots)):
                derived, mat_info = resolve_material_source(
                    entry, index, skin_mats, operator.npc_uses_skin,
                )
                mat_path = mat_info.get("matPath")
                if not mat_path:
                    continue

                mat_name = Path(mat_path).stem
                material = _build_npc_material(
                    mat_primary, mat_secondary, mat_name, derived, mat_info,
                    ob.name, missing_assets, use_mat_dds_paths,
                )
                ob.material_slots[index].material = material

    return imported_objects


# ---------------------------------------------------------------------------
# Skeleton import + binding (spec §8)
# ---------------------------------------------------------------------------

def _parse_dyc_skeleton(dyc_path):
    # type: (str) -> Optional[str]
    """
    Extracts the "Skeleton=..." filename from a .dyc file's [SETTINGS]
    section (e.g. "Skeleton=bmnnew_skeleton.gr2" -> "bmnnew_skeleton.gr2").

    .dyc files are INI-like, but this is a hand-rolled line scan rather
    than configparser: other sections (e.g. [MASKS]) contain repeated
    keys ("mask=...") and unlabeled indented sub-entries that aren't
    valid strict INI and would trip configparser's stricter parsing long
    before ever reaching [SETTINGS].

    Returns the skeleton filename, or None if not found.
    """
    current_section = None

    with open(dyc_path, encoding="utf-8", errors="replace") as file:
        for line in file:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1].upper()
                continue

            if current_section == "SETTINGS" and "=" in stripped:
                key, _, value = stripped.partition("=")
                if key.strip().lower() == "skeleton":
                    return value.strip()

    return None


def import_skeleton(operator, context, resources_root, meta):
    # type: (Any, Any, str, Dict[str, Any]) -> Optional[Any]
    """
    Imports the skeleton for this NPC, resolved via meta["bodyType"]
    rather than a direct path in the json.

    This moved server-side (Jedipedia) -> local resolution: Jedipedia
    can't reliably determine the actual skeleton file server-side, but
    it's straightforward locally, in two steps:

        1. "/art/dynamic/spec/<bodyType>.dyc" is read directly (e.g.
           "bmn.dyc" for bodyType "bmn"). This is an INI-like file whose
           [SETTINGS] section has a "Skeleton=<filename>.gr2" line
           giving the actual skeleton filename (e.g.
           "bmnnew_skeleton.gr2") -- confirmed this filename does NOT
           always match "<bodyType>_skeleton.gr2" or similar, hence
           needing to actually read it rather than guessing a pattern.
        2. That filename lives in the same /art/dynamic/spec/ directory
           as the .dyc file itself.

    Tolerant by design: a missing/empty bodyType, a missing .dyc file, a
    .dyc with no [SETTINGS] Skeleton= line, or a skeleton path that
    doesn't resolve to an existing file, is not an error -- it just
    means no skeleton gets imported (spec §8: "should be able to handle
    skeletons but not require them").

    Returns the imported armature Object, or None.
    """
    body_type = meta.get("bodyType")
    if not body_type:
        return None

    dyc_path = resolve_resource_path(resources_root, "/art/dynamic/spec/%s.dyc" % body_type)
    if not dyc_path or not Path(dyc_path).exists():
        operator.report(
            {'WARNING'},
            "Skeleton definition file not found, skipping: %s" % (dyc_path or body_type,),
        )
        return None

    skeleton_filename = _parse_dyc_skeleton(dyc_path)
    if not skeleton_filename:
        operator.report(
            {'WARNING'},
            "No [SETTINGS] Skeleton= entry found in %s, skipping." % (dyc_path,),
        )
        return None

    resolved_path = resolve_resource_path(resources_root, "/art/dynamic/spec/%s" % skeleton_filename)
    if not resolved_path or not Path(resolved_path).exists():
        operator.report(
            {'WARNING'},
            "Skeleton file not found, skipping: %s" % (resolved_path or skeleton_filename,),
        )
        return None

    operator.job_results_rich = True
    ImportGR2_load(operator, context, resolved_path)

    object_names = job_results.get("files_objs_names", {}).get(
        _job_results_key(resolved_path), []
    )
    if not object_names:
        return None

    # A skeleton .gr2 is expected to produce a single armature object;
    # if it somehow produces more, the first is treated as the main one
    # (same "first object is main" assumption used elsewhere in this
    # add-on for multi-object imports).
    skeleton_ob = bpy.data.objects[object_names[0]]
    skeleton_ob.show_in_front = True

    return skeleton_ob


def bind_objects_to_armature(objects, armature, single_armature_only=True):
    # type: (List[Any], Any, bool) -> None
    """
    Binds a set of objects to an armature via Armature modifiers, ported
    unchanged from the original ZG_Tools assembler.

    :param objects: list of Objects to bind
    :param armature: the armature Object
    :param single_armature_only: skip objects that already have an
        Armature modifier, rather than adding a second one
    """
    for obj in objects:
        if obj.type != 'MESH':
            continue

        if single_armature_only and any(mod.type == 'ARMATURE' for mod in obj.modifiers):
            continue

        mod = obj.modifiers.new(name="Armature", type='ARMATURE')
        mod.object = armature

        # Create a parent relationship. Don't use a parent_type of
        # 'ARMATURE' -- the modifier above is already doing that job.
        obj.parent = armature
        obj.matrix_parent_inverse = armature.matrix_world.inverted()
        
        # Enable Preserve Volume on Armature Modifier by Default
        obj.modifiers["Armature"].use_deform_preserve_volume = True

        # Automatic weight assignment needs the operator context
        # (bpy.ops.object.parent_set(type='ARMATURE_AUTO')) and isn't
        # done here; weights are assumed to be assigned manually or via
        # another process.


# ---------------------------------------------------------------------------
# Collection assembly (spec §7)
# ---------------------------------------------------------------------------

def assemble_collections(context, npc_name, slot_objects, skeleton_ob=None, character_name_suffix=None):
    # type: (Any, str, Dict[str, List[Any]], Optional[Any], Optional[str]) -> Any
    """
    Builds the NPC's Collection hierarchy:

        <NPC Collection>              # named from npc_name
         |-- <SlotName>                # one per slot that produced objects
         |    |-- variant object 1     # (capitalized -- "head" -> "Head")
         |    `-- variant object 2
         |-- ...
         |-- <creature slot's object(s)>  # directly in the NPC Collection,
         |                                # no "Creature" sub-collection --
         |                                # it's effectively the whole NPC
         `-- <skeleton object>        # directly in the NPC Collection, if any

    slot_objects: dict mapping slotName -> list of imported Objects for
    that slot. A slot with no objects (an all-empty-models variant, e.g.
    a bald/clean-shaven "none" option -- spec §5) is simply skipped, no
    empty Collection is created for it.

    character_name_suffix: if given, appended to every per-slot
    Collection's name as " - <suffix>" (e.g. "Boot" -> "Boot - Republic
    Officer"). Only meant to be passed when the "Append Character Name
    to Collections" import option is on AND the json's meta.charName is
    actually present -- callers decide that, this function just appends
    whatever string it's given (or nothing, if None). Not applied to the
    NPC root Collection itself, since that's already named from npc_name.

    Every Collection is created fresh via bpy.data.collections.new()
    rather than looked up/reused by name -- see bpy.data.collections is a
    global namespace, not scoped by nesting; reusing an existing
    same-named Collection across two different NPC imports in the same
    file would merge their objects together, which is never wanted here.
    Letting Blender auto-suffix duplicate names (".001", ".002", ...) is
    the safe default.

    Returns the NPC root Collection.
    """
    npc_collection = bpy.data.collections.new(npc_name)
    context.scene.collection.children.link(npc_collection)

    for slot_name, objects in slot_objects.items():
        if not objects:
            continue

        # The "creature" slot is effectively the whole NPC on
        # creature-type imports (e.g. Malgus) -- it doesn't get its own
        # sub-collection, its objects go straight into the NPC root,
        # same as the skeleton.
        target_collection = npc_collection
        if slot_name != "creature":
            collection_name = slot_name.capitalize()
            if character_name_suffix:
                collection_name = "%s - %s" % (collection_name, character_name_suffix)
            target_collection = bpy.data.collections.new(collection_name)
            npc_collection.children.link(target_collection)

        for ob in objects:
            target_collection.objects.link(ob)
            # ImportGR2_load() links newly created objects into whatever
            # collection is active in the view layer at import time --
            # remove that link now that the object has its proper home.
            for existing_collection in list(ob.users_collection):
                if existing_collection is not target_collection:
                    existing_collection.objects.unlink(ob)

    if skeleton_ob is not None:
        npc_collection.objects.link(skeleton_ob)
        for existing_collection in list(skeleton_ob.users_collection):
            if existing_collection is not npc_collection:
                existing_collection.objects.unlink(skeleton_ob)

    return npc_collection


# ---------------------------------------------------------------------------
# Twi'lek eyes UV fix + eye separation (spec §9, ported from ZG_Tools)
# ---------------------------------------------------------------------------

def translate_uv_coordinates(mesh_object, material_slot=None, uv_offset=(0, 0)):
    """
    Offsets an object's polys' UVs, either all of them or only those
    associated with a specific material slot. Motivated by Twi'lek
    heads' off-image-bounds eye UVs producing black bakes.

    Ported unchanged from the original ZG_Tools assembler.
    """
    if mesh_object.type != 'MESH':
        return

    mesh = mesh_object.data
    if not mesh.uv_layers.active:
        return

    uv_layer = mesh.uv_layers.active.data

    for face in mesh.polygons:
        if material_slot is None or face.material_index == material_slot:
            for loop_index in face.loop_indices:
                uv = uv_layer[loop_index].uv
                if uv.y <= 1:
                    return
                uv.x += uv_offset[0]
                uv.y += uv_offset[1]


def duplicate_obj(obj_or_obj_name):
    """
    Creates a full duplicate of an object (mesh data, transforms,
    modifiers, constraints, animation data, custom properties), linked
    into the same Collection(s) as the original.

    Ported unchanged from the original ZG_Tools assembler.
    """
    if isinstance(obj_or_obj_name, str):
        obj = bpy.data.objects[obj_or_obj_name]
    else:
        obj = obj_or_obj_name

    new_data = obj.data.copy()
    new_obj = bpy.data.objects.new(obj.name + ".copy", new_data)

    for col in obj.users_collection:
        col.objects.link(new_obj)

    new_obj.location = obj.location.copy()
    new_obj.rotation_euler = obj.rotation_euler.copy()
    new_obj.rotation_quaternion = obj.rotation_quaternion.copy()
    new_obj.scale = obj.scale.copy()
    new_obj.data = new_data

    for prop in obj.keys():
        if prop != "_RNA_UI":
            new_obj[prop] = obj[prop]

    for mod in obj.modifiers:
        new_mod = new_obj.modifiers.new(name=mod.name, type=mod.type)
        for attr in dir(mod):
            if not attr.startswith("_") and attr not in {"type", "name", "rna_type"}:
                try:
                    setattr(new_mod, attr, getattr(mod, attr))
                except AttributeError:
                    pass

        if mod.type == 'ARMATURE':
            new_mod.object = mod.object

    if obj.animation_data:
        new_obj.animation_data_create()
        new_obj.animation_data.action = obj.animation_data.action
        new_obj.animation_data.action = obj.animation_data.action.copy()

    for constr in obj.constraints:
        new_constr = new_obj.constraints.new(type=constr.type)
        for attr in dir(constr):
            if not attr.startswith("_") and attr not in {"type", "name", "rna_type"}:
                try:
                    setattr(new_constr, attr, getattr(constr, attr))
                except AttributeError:
                    pass

    return new_obj


def separate_obj_by_specific_materials(obj_or_obj_name, material_names, separate=True):
    """
    Separates an object's polys associated with the given material(s)
    into a new object per material. By default, also deletes those
    polys and material slots from the original object.

    :param material_names: a material name, or a list of material names.

    Ported from the original ZG_Tools assembler, with one clarity fix:
    the original accepted a single material name string and tested
    membership via Python's substring-`in` on that string, despite its
    own docstring describing it as "a list of materials names" --
    happened to work for how it was actually called (the exact same
    string on both sides of `in`), but is genuinely a list-membership
    check now, matching the docstring.
    """
    if isinstance(material_names, str):
        material_names = [material_names]

    if isinstance(obj_or_obj_name, str):
        original_obj = bpy.data.objects[obj_or_obj_name]
    else:
        original_obj = obj_or_obj_name

    original_mesh = original_obj.data
    new_objs = []

    mat_indices = [
        i for i, mat in enumerate(original_mesh.materials) if mat.name in material_names
    ]

    if separate:
        for mat_index in mat_indices:
            new_obj = original_obj.copy()
            new_obj.data = original_obj.data.copy()
            bpy.context.collection.objects.link(new_obj)
            new_obj.name = "%s_%s" % (original_obj.name, original_mesh.materials[mat_index].name)

            bm = bmesh.new()
            bm.from_mesh(new_obj.data)
            faces_to_delete = [f for f in bm.faces if f.material_index != mat_index]
            bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
            bm.to_mesh(new_obj.data)
            bm.free()

            new_objs.append(new_obj)

    bm = bmesh.new()
    bm.from_mesh(original_mesh)
    faces_to_delete = [f for f in bm.faces if f.material_index in mat_indices]
    bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
    bm.to_mesh(original_mesh)
    bm.free()

    for i, slot in enumerate(original_obj.material_slots[:]):
        if slot.material and slot.material.name in material_names:
            original_obj.data.materials.pop(index=i)

    return new_objs


def delete_polygons_on_side(obj_or_obj_name, side='LEFT'):
    """
    Deletes every polygon on the given side of local X == 0.

    Ported unchanged from the original ZG_Tools assembler.
    """
    if isinstance(obj_or_obj_name, str):
        obj = bpy.data.objects[obj_or_obj_name]
    else:
        obj = obj_or_obj_name

    if obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    faces_to_delete = []
    for face in bm.faces:
        center = face.calc_center_median()
        if side == 'LEFT' and center.x < 0:
            faces_to_delete.append(face)
        elif side == 'RIGHT' and center.x >= 0:
            faces_to_delete.append(face)

    bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
    bm.to_mesh(mesh)
    bm.free()


def get_highest_and_lowest_vertices(obj_or_obj_name):
    """
    Returns (highest_vertex, lowest_vertex) world-space coordinates by Z.

    Ported unchanged from the original ZG_Tools assembler.
    """
    if isinstance(obj_or_obj_name, str):
        obj = bpy.data.objects[obj_or_obj_name]
    else:
        obj = obj_or_obj_name

    bpy.ops.object.mode_set(mode='OBJECT')

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    global_matrix = obj.matrix_world
    highest_vertex = None
    lowest_vertex = None

    for v in bm.verts:
        global_coord = global_matrix @ v.co
        if highest_vertex is None or global_coord.z > highest_vertex.z:
            highest_vertex = global_coord
        if lowest_vertex is None or global_coord.z < lowest_vertex.z:
            lowest_vertex = global_coord

    bm.free()

    return highest_vertex, lowest_vertex


def set_obj_origin(obj_or_obj_name, new_origin):
    """
    Moves an object's origin to new_origin (world space), preserving its
    visual position by compensating the mesh data's transform.

    Ported unchanged from the original ZG_Tools assembler.
    """
    if isinstance(obj_or_obj_name, str):
        obj = bpy.data.objects[obj_or_obj_name]
    else:
        obj = obj_or_obj_name

    new_origin = Vector(new_origin)

    obj_matrix = obj.matrix_world
    current_origin_world = obj_matrix @ Vector((0, 0, 0))
    delta_world = new_origin - current_origin_world
    delta_object_space = obj_matrix.inverted() @ delta_world

    obj.location += delta_world
    obj.data.transform(Matrix.Translation(-delta_object_space))
    obj.data.update()


def apply_twilek_eyes_uv_fix(slot_objects):
    # type: (Dict[str, List[Any]]) -> None
    """
    Applies translate_uv_coordinates() to every Twi'lek head object's
    Eye material slot (index 1), in place.

    Deviates from the original in one way: the original stopped after
    the first Twi'lek head object found across the whole NPC. Now that
    a single NPC can have multiple head variants (spec §5), this applies
    to every matching head variant instead.
    """
    for head_ob in slot_objects.get("head", []):
        if "head_twilek" in head_ob.name:
            translate_uv_coordinates(head_ob, 1, (0, -2))


def separate_head_eyes(slot_objects, separate_each_eye):
    # type: (Dict[str, List[Any]], bool) -> None
    """
    For every head object with an Eye material in its 2nd material slot,
    splits the eyes out into their own object (or two objects, one per
    eye, if separate_each_eye is set) and appends them into
    slot_objects["head"] so they end up correctly placed by
    assemble_collections().

    Iterates over a snapshot of slot_objects["head"] rather than the
    live list, since new eye objects get appended into that same list
    as this runs.
    """
    for head_ob in list(slot_objects.get("head", [])):
        if len(head_ob.material_slots) <= 1:
            continue
        if "eye" not in head_ob.material_slots[1].name.lower():
            continue

        eyes_ob = duplicate_obj(head_ob)

        # Delete the eyes' polys/material from the head object, and the
        # head's polys/material from the eyes object (the duplicate).
        separate_obj_by_specific_materials(
            head_ob, head_ob.material_slots[1].name, separate=False,
        )
        separate_obj_by_specific_materials(
            eyes_ob, eyes_ob.material_slots[0].name, separate=False,
        )

        if not separate_each_eye:
            eyes_ob.name = "%s.eyes" % head_ob.name
            slot_objects.setdefault("head", []).append(eyes_ob)
            continue

        eyes_ob_right = eyes_ob
        eyes_ob_left = duplicate_obj(eyes_ob)

        delete_polygons_on_side(eyes_ob_right, side='LEFT')
        highest_vertex, lowest_vertex = get_highest_and_lowest_vertices(eyes_ob_right)
        set_obj_origin(
            eyes_ob_right,
            (highest_vertex.x, highest_vertex.y, (highest_vertex.z + lowest_vertex.z) / 2),
        )
        eyes_ob_right.name = "%s.eyes.right" % head_ob.name
        slot_objects.setdefault("head", []).append(eyes_ob_right)

        delete_polygons_on_side(eyes_ob_left, side='RIGHT')
        highest_vertex, lowest_vertex = get_highest_and_lowest_vertices(eyes_ob_left)
        set_obj_origin(
            eyes_ob_left,
            (highest_vertex.x, highest_vertex.y, (highest_vertex.z + lowest_vertex.z) / 2),
        )
        eyes_ob_left.name = "%s.eyes.left" % head_ob.name
        slot_objects.setdefault("head", []).append(eyes_ob_left)


# ---------------------------------------------------------------------------
# Orchestrator + Operator (spec §10)
# ---------------------------------------------------------------------------

def load(operator, context, filepath=""):
    # type: (Any, Any, str) -> bool
    """
    Does the actual work of importing a Jedipedia-formatted NPC/Character
    json file: parse -> import every slot's variants -> optionally import
    a skeleton -> assemble Collections -> optionally bind to skeleton.

    Returns True on success, False on failure (mirrors import_gr2.py's
    own load() return convention).
    """
    resources_root, legacy_resources_root = get_resources_dirs()

    if not resources_root or not Path(resources_root).is_dir():
        operator.report(
            {'ERROR'},
            "Set a valid Resources Folder in IO Scene GR2's add-on preferences first.",
        )
        return False

    # The switch is silently ignored by _material_roots() when the Legacy
    # directory is unusable; say so once rather than leaving it easy to miss.
    if (
        getattr(operator, "prefer_legacy_materials", False)
        and not (legacy_resources_root and Path(legacy_resources_root).is_dir())
    ):
        operator.report(
            {'WARNING'},
            "Prefer Legacy Materials is on, but no valid Legacy Resources Directory "
            "is set in the add-on preferences -- ignored, using the Resources Directory only.",
        )

    try:
        meta, slots, skin_mats = read(filepath)
    except (OSError, json.JSONDecodeError) as exc:
        operator.report({'ERROR'}, "Couldn't read %s: %s" % (filepath, exc))
        return False

    if not slots:
        operator.report({'WARNING'}, "No recognizable slot entries found in this file.")
        return False

    npc_name = meta.get("charName") or Path(filepath).stem

    job_results['job_origin'] = operator.bl_idname
    if not operator.job_results_accumulate:
        job_results['objs_names'] = []
        job_results['files_objs_names'] = {}

    # Local to this one load() call, deliberately NOT part of job_results
    # -- that dict is a stable external contract (serialized to
    # bpy.context.scene.swtor_io_last_job for third-party addons, shared/
    # reset across import_gr2.py/import_area.py/import_fxspec.py too), so
    # mixing this NPC-import-specific reporting into it risks a stale
    # list leaking into an unrelated pipeline's own reported output.
    missing_assets = []

    slot_objects = {}
    for slot_name, entries in slots.items():
        objects_for_slot = []
        for entry in entries:
            objects_for_slot.extend(
                import_variant(
                    operator, context, resources_root, legacy_resources_root, entry, skin_mats,
                    missing_assets=missing_assets, use_mat_dds_paths=not operator.ignore_mat_dds_paths,
                )
            )
        slot_objects[slot_name] = objects_for_slot

    skeleton_ob = None
    if operator.import_skeleton:
        skeleton_ob = import_skeleton(operator, context, resources_root, meta)

    if operator.correct_twilek_eyes_uv:
        apply_twilek_eyes_uv_fix(slot_objects)

    if operator.separate_eyes:
        separate_head_eyes(slot_objects, operator.separate_each_eye)

    assemble_collections(
        context,
        npc_name,
        slot_objects,
        skeleton_ob,
        character_name_suffix=meta.get("charName") if operator.append_character_name_to_collections else None,
    )

    if operator.bind_to_skeleton and skeleton_ob is not None:
        all_objects = [ob for objects in slot_objects.values() for ob in objects]
        bind_objects_to_armature(all_objects, skeleton_ob)

    if missing_assets:
        # Soft-fail summary, not a crash: every listed texture was left
        # unassigned on its Image Texture node (see get_or_create_
        # material()'s docstring) rather than aborting the import.
        #
        # operator.report() alone isn't reliably visible in the System
        # Console (only Blender's own Info log/status bar) -- matches
        # ops/migrate_shaders.py's and ops/process_materials.py's own
        # "[SWTOR <module>]"-prefixed print() convention for exactly
        # this reason. The in-UI report stays a short preview (first 5,
        # "+N more") so it doesn't get unreadably long in the status
        # bar; the console gets the full per-asset list instead, since
        # that's where anyone actually tracking down missing files would
        # look anyway.
        preview = ", ".join(f"'{mat_name}': {rel_path}" for mat_name, rel_path in missing_assets[:5])
        remainder = len(missing_assets) - 5
        if remainder > 0:
            preview += f", and {remainder} more"
        operator.report(
            {'WARNING'},
            f"Imported '{npc_name}', but {len(missing_assets)} texture(s) couldn't be found "
            f"under either Resources Directory -- left unassigned: {preview}",
        )

        print(f"[SWTOR import_cha] Imported '{npc_name}', but {len(missing_assets)} texture(s) couldn't be found under either Resources Directory:")
        for mat_name, rel_path in missing_assets:
            print(f"[SWTOR import_cha]   - '{mat_name}': {rel_path}")

    return True


class ImportCHA(bpy.types.Operator):
    """
    Import a Jedipedia.net-formatted NPC/Character json file.

    Reads .gr2, .mat, and .dds assets directly from the Resources Folder
    configured in this add-on's Preferences -- no separate asset
    gathering/copying step is needed.
    """
    bl_idname = "import_mesh.gr2_json"  # DO NOT CHANGE -- external tools call this by name
    bl_label = "Import SWTOR NPC/Character (.json)"
    bl_description = "Import a Jedipedia.net-formatted NPC/Character .json file"
    bl_options = {'UNDO'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    append_character_name_to_collections: bpy.props.BoolProperty(
        name="Append Name to Collections",
        description=(
            "Appends the character's name to every per-slot Collection, "
            "e.g. \"Boot\" -> \"Boot - Republic Officer\".\n\n"
            "Only takes effect if the json's meta data actually includes "
            "a character name -- has no effect otherwise"
        ),
        default=False,
    )

    npc_uses_skin: bpy.props.BoolProperty(
        name="Gear Uses Skin",
        description=(
            "When importing a non-Creature-type characters, assume that any 2nd "
            "Material Slot in armor or clothes is skin rather than "
            "garment.\n\n"
            "Typical case actually needing this: revealing clothing."
        ),
        default=True,
    )

    ignore_mat_dds_paths: bpy.props.BoolProperty(
        name="Ignore .mat Texture Paths",
        description=(
            "Sources Diffuse/Rotation/Gloss/Palette/PaletteMask/Direction "
            "maps directly from the json's own .dds paths, instead of each "
            "material's real .mat file.\n\n"
            "Off by default -- Jedipedia's NPC export currently emits "
            "stale .dds paths for these specific maps. Enable this once "
            "that's fixed to go back to reading them straight from json.\n\n"
            "Complexion/Facepaint/Age maps and all hue/palette values "
            "always come from json regardless of this setting."
        ),
        default=False,
    )

    # Opt-in: resolve materials (.mat files and .dds textures) from the Legacy
    # Resources Directory first, with the regular Resources Directory as the
    # per-asset fallback. Models and skeletons are never affected. Default False
    # so existing users/callers see no change. A preference-seeded default is
    # deliberately NOT used: programmatic calls
    # (bpy.ops.import_mesh.gr2_json(filepath=..., prefer_legacy_materials=True))
    # skip invoke(), so a preference would never reach them.
    prefer_legacy_materials: bpy.props.BoolProperty(
        name="Prefer Legacy Materials",
        description=(
            "Resolves materials (.mat files and textures) from the Legacy "
            "Resources Directory first, falling back to the Resources "
            "Directory for anything missing. Models and skeletons are "
            "unaffected.\n\nRequires a valid Legacy Resources Directory in "
            "the add-on preferences; ignored otherwise."
        ),
        default=False,
    )

    import_skeleton: bpy.props.BoolProperty(
        name="Import Skeleton",
        description="Imports the skeleton referenced by the json file's meta data, if present",
        default=False,  # seeded from add-on Preferences in invoke()
    )

    bind_to_skeleton: bpy.props.BoolProperty(
        name="Bind To Skeleton",
        description="Binds all imported objects to the imported skeleton via Armature modifiers",
        default=False,  # seeded from add-on Preferences in invoke()
    )

    correct_twilek_eyes_uv: bpy.props.BoolProperty(
        name="Correct Twi'lek Eyes UV",
        description="Corrects a known UV offset issue on Twi'lek eyes",
        default=True,
    )

    separate_eyes: bpy.props.BoolProperty(
        name="Separate Eyes",
        description="Separates the eyes from the head object into their own object, useful for rigging",
        default=False,
    )

    separate_each_eye: bpy.props.BoolProperty(
        name="Separate Eyes Individually",
        description=(
        "When separating eyes, makes each eye its own object with its own origin.\n\n"
        "Requires \"Separate Eyes\" to be enabled as well."
        ),
        default=False,
    )

    job_results_rich: bpy.props.BoolProperty(
        name="Rich Results Info",
        options={'HIDDEN'},
        default=False,
    )

    job_results_accumulate: bpy.props.BoolProperty(
        name="Accumulate Jobs' Results Info",
        options={'HIDDEN'},
        default=True,
    )

    # Not exposed in the panel, and always False: import_gr2.py's load()
    # reads this attribute unconditionally before it even checks
    # bl_idname to decide where the *other* import settings come from,
    # so it has to exist regardless. Unlike import_collision/
    # name_as_filename/etc. (genuinely decorative for this operator,
    # since those are always sourced from add-on Preferences instead),
    # this one isn't optional to declare.
    enforce_neutral_settings: bpy.props.BoolProperty(
        options={'HIDDEN'},
        default=False,
    )

    def invoke(self, context, event):
        prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
        self.import_skeleton = prefs.gr2_import_skeleton_default
        self.bind_to_skeleton = prefs.gr2_bind_to_skeleton_default
        self.append_character_name_to_collections = prefs.gr2_append_character_name_to_collections_default
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        header, body = layout.panel("SWTOR_IO_CHA_options", default_closed=False)
        header.label(text="Import Options")
        if body:
            body.prop(self, "append_character_name_to_collections")
            body.prop(self, "npc_uses_skin")
            body.prop(self, "ignore_mat_dds_paths")
            body.prop(self, "prefer_legacy_materials")
            body.prop(self, "correct_twilek_eyes_uv")

        header, body = layout.panel("SWTOR_IO_CHA_rigging", default_closed=False)
        header.label(text="Rigging")
        if body:
            body.prop(self, "import_skeleton")
            body.prop(self, "bind_to_skeleton")
            body.prop(self, "separate_eyes")
            body.prop(self, "separate_each_eye")


    def execute(self, context):
        if load(self, context, filepath=self.filepath):
            return {'FINISHED'}
        return {'CANCELLED'}
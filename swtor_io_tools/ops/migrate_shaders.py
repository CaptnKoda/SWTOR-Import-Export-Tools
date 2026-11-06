# ============================================================================
# MIGRATION BRIDGE -- ShaderNodeHeroEngine (deprecated) -> HeroEngine Shader
# native node groups (current)
#
# Converts a material's old custom-Python-node SWTOR shader
# (ShaderNodeHeroEngine, types/node_deprecated.py) to the equivalent
# native ShaderNodeGroup-based "HeroEngine Shader" (built by
# ops/shaders_menu.py's build_swtor_shader_material()), in place -- same
# material name/identity, node graph swapped internally.
#
# This file is entirely dependent on the deprecated system: it reads
# ShaderNodeHeroEngine instances that only exist because that system
# does. Once ShaderNodeHeroEngine is gone there is nothing left to
# migrate FROM, so this file is deleted alongside it -- see
# __init__.py's "DEPRECATED SYSTEM" block, which this file has been
# added to.
#
# Deliberately self-contained: does NOT import from ops/material_builder.py
# or types/shared.py. Those are active, in-use code with no relationship to
# the deprecated system -- keeping this file free of any dependency on them
# means it can be deleted the day the deprecated system goes, without
# having to check whether doing so breaks anything else, or touch
# them at all. Per Crunch, this holds even where the two files'
# solutions are shaped alike (default map assets, palette writing) --
# duplication is the accepted cost of that isolation.
#
# DELETE THIS FILE alongside:
#   - types/node_deprecated.py
#   - types/node_tree_deprecated.py
#   - ops/shaders_menu_deprecated.py
# and the "DEPRECATED SYSTEM" block in __init__.py, once that system's
# support window ends.
# ============================================================================

# <pep8 compliant>

from pathlib import Path
from typing import List, NamedTuple

import bpy

from ..types.node_deprecated import CREATURE, EYE, GARMENT, HAIRC, SKINB, UBER
from ..types.shader_templates import ShaderTemplateError, find_main_shader_group_node, get_or_build_shader_material


# ---------------------------------------------------------------------------
# Default map assets -- loaded in place of an optional map that's absent
# on the old node, so the new group doesn't end up with a live-but-empty
# Image Texture node overriding its own (correct, no-op) socket default
# with black. Mirrors material_builder.py's DEFAULT_MAP_ASSETS of the same
# name, deliberately duplicated rather than imported -- see module
# docstring. Paths relative to the resources root, same convention as
# every other dds path in this addon.
#
# Keyed by node_name (not the old node's attr_name), matching how every
# other lookup in this file identifies a map -- see MIGRATE_CONFIGS below.
#
# These are the smaller, actual game-default assets (confirmed working
# by Crunch), not the larger player-customization textures of the same
# apparent purpose -- .mat files reference these specifically.
#
# HACK: DirectionMap's real in-game default is art/defaultassets/flat_n.dds
# (a flat/neutral normal map) -- black.dds is used instead because it's
# smaller and Crunch found no visible difference between the two in
# testing. If that ever stops holding true, swap the path below back to
# "art/defaultassets/flat_n.dds".
DEFAULT_MAP_ASSETS = {
    "complexionMap": "art/defaultassets/white.dds",
    "facepaintMap": "art/defaultassets/default_facepaint.dds",
    "ageMap": "art/defaultassets/default_age.dds",
    "directionMap": "art/defaultassets/black.dds",
}


def _resolve_resource_path(resources_root, relative_path):
    # type: (str, Optional[str]) -> Optional[str]
    """
    Local copy of types/shared.py's resolve_resource_path() -- deliberately
    duplicated rather than imported, see module docstring.

    Resolves a relative asset path against a local resources root folder
    that contains an "art" subfolder directly, mirroring the game's own
    asset layout. Defensively strips a single leading slash/backslash and
    a redundant leading "resources" path segment.

    Returns None if relative_path is falsy, so callers can skip absent
    optional paths without extra checks.
    """
    if not relative_path:
        return None

    normalized = relative_path.replace("\\", "/").lstrip("/")
    parts = normalized.split("/")
    if parts and parts[0].lower() == "resources":
        parts = parts[1:]

    return str(Path(resources_root, *parts))


def _load_or_get_image(resources_root, relative_path):
    """
    Local copy of material_builder.py's _load_or_get_image() -- deliberately
    duplicated rather than imported, see module docstring.

    Loads a .dds texture from the resources root, reusing an already-
    loaded Blender image with the same filename if one exists. Sets
    alpha_mode/colorspace exactly as material_builder.py's copy does, so a
    default asset loaded via migration looks identical to the same asset
    loaded via import.

    Raises the same way bpy.data.images.load() does (OSError/RuntimeError
    on a missing/unreadable file) -- callers decide how to handle that,
    see _load_default_map_image() below.
    """
    path = _resolve_resource_path(resources_root, relative_path)
    name = Path(path).name
    existing = bpy.data.images.get(name)
    if existing:
        return existing
    image = bpy.data.images.load(path)
    image.alpha_mode = 'CHANNEL_PACKED'
    image.colorspace_settings.name = 'Non-Color'
    return image


def _get_resources_root():
    # type: () -> Optional[str]
    """
    Reads the addon's persistent "Resources Folder" preference -- the
    same one import_cha.py uses, and the same one the user sets once in
    Preferences regardless of which importer/migrator reads it. Returns
    None if unset or not a real directory, letting callers decide how to
    report that rather than raising here.
    """
    prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
    resources_root = prefs.swtor_resources_dir
    if not resources_root or not Path(resources_root).is_dir():
        return None
    return resources_root


def _load_default_map_image(node_name):
    # type: (str) -> Optional[bpy.types.Image]
    """
    Loads the fallback image for an optional map that's absent on the
    old node, per DEFAULT_MAP_ASSETS above. Reads the resources-root
    preference lazily -- only called at all when a material actually
    turns out to need a default, never up front.

    Returns None (rather than raising) if node_name has no default asset
    registered, the resources root isn't configured, or the file isn't
    actually present under it -- callers treat that as "leave this map
    unassigned" and report it, not as a hard failure.
    """
    relative_path = DEFAULT_MAP_ASSETS.get(node_name)
    if relative_path is None:
        return None

    resources_root = _get_resources_root()
    if resources_root is None:
        return None

    try:
        return _load_or_get_image(resources_root, relative_path)
    except (OSError, RuntimeError):
        return None


# Per-derived-type configuration, keyed by ShaderNodeHeroEngine's own
# `derived` enum values (CREATURE[0] == 'CREATURE', etc. -- these match
# SWTOR_SHADER_GROUPS' string keys in shaders_menu.py directly, so
# old_node.derived can be passed straight through to
# build_swtor_shader_material()).
#
# image_maps: (PointerProperty attr name on ShaderNodeHeroEngine, new
#   group's external Image Texture node name). The node-name half must
#   match SWTOR_SHADER_GROUPS' own node_name values exactly (see
#   shaders_menu.py), since it indexes straight into the image_nodes
#   dict build_swtor_shader_material() hands back below -- unrelated to
#   the OLD node's own internal node names (still '_d'/'_n'/'_s'/'_h'/
#   '_m', see node_deprecated.py's update_*Map() functions), which this
#   config never touches.
# palettes: list of (index, include_metallic_specular).
# flesh: whether Flesh Brightness / Flush Tone apply for this type.
#
# NOTE: HAIRC_MODERN never appears here -- it's a new shader type added
# after the old system was retired, so migration only ever produces one
# of these original six.
MIGRATE_CONFIGS = {
    CREATURE[0]: {
        "image_maps": [
            ("diffuseMap", "diffuseMap"),
            ("rotationMap", "rotationMap1"),
            ("glossMap", "glossMap"),
            ("paletteMaskMap", "paletteMaskMap"),
            ("directionMap", "directionMap"),
        ],
        "palettes": [],
        "flesh": True,
    },
    EYE[0]: {
        "image_maps": [
            ("diffuseMap", "diffuseMap"),
            ("rotationMap", "rotationMap1"),
            ("glossMap", "glossMap"),
            ("paletteMap", "paletteMap"),
            ("paletteMaskMap", "paletteMaskMap"),
        ],
        # Metallic Specular: the old node never actually wired this for
        # Eye (a pre-existing bug, since fixed in eye_group()) -- per
        # Crunch, migrate the old node's leftover value anyway now that
        # it's meaningful, rather than skipping it.
        "palettes": [(1, True)],
        "flesh": False,
    },
    GARMENT[0]: {
        "image_maps": [
            ("diffuseMap", "diffuseMap"),
            ("rotationMap", "rotationMap1"),
            ("glossMap", "glossMap"),
            ("paletteMap", "paletteMap"),
            ("paletteMaskMap", "paletteMaskMap"),
        ],
        "palettes": [(1, True), (2, True)],
        "flesh": False,
    },
    HAIRC[0]: {
        "image_maps": [
            ("diffuseMap", "diffuseMap"),
            ("rotationMap", "rotationMap1"),
            ("glossMap", "glossMap"),
            ("paletteMap", "paletteMap"),
            ("paletteMaskMap", "paletteMaskMap"),
            ("directionMap", "directionMap"),
        ],
        "palettes": [(1, True)],
        "flesh": False,
    },
    SKINB[0]: {
        "image_maps": [
            ("diffuseMap", "diffuseMap"),
            ("rotationMap", "rotationMap1"),
            ("glossMap", "glossMap"),
            ("paletteMap", "paletteMap"),
            ("paletteMaskMap", "paletteMaskMap"),
            ("ageMap", "ageMap"),
            ("complexionMap", "complexionMap"),
            ("facepaintMap", "facepaintMap"),
        ],
        "palettes": [(1, True)],
        "flesh": True,
    },
    UBER[0]: {
        "image_maps": [
            ("diffuseMap", "diffuseMap"),
            ("rotationMap", "rotationMap1"),
            ("glossMap", "glossMap"),
        ],
        "palettes": [],
        "flesh": False,
    },
}


def _find_hero_node(material):
    # type: (bpy.types.Material) -> Optional[bpy.types.ShaderNode]
    """
    Returns the first ShaderNodeHeroEngine node found in `material`'s
    node tree, or None if it has none (already native, or never SWTOR
    shaded at all).
    """
    if not material.use_nodes or material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.bl_idname == "ShaderNodeHeroEngine":
            return node
    return None


# Throwaway name the replacement material is built under while it's being
# validated and filled in -- see migrate_material(). Only ever exists for
# the duration of one call (removed again on failure, renamed to the
# original's name on success).
_SCRATCH_MATERIAL_NAME = "__swtor_migrate_scratch__"


class MigrationResult(NamedTuple):
    """
    Return value of migrate_material() -- richer than a plain bool so
    callers can report which specific maps, if any, ended up unassigned
    for lack of a default asset, without treating that as a failure.

    converted: True if a ShaderNodeHeroEngine node was found and
        converted; False if the material had nothing to migrate.
    missing_assets: human-readable names (e.g. "AgeMap") of maps that
        were absent on the old node AND couldn't get a default image
        (resources root unset, or the default asset file itself isn't
        present under it). Always empty when converted is False.
    """
    converted: bool
    missing_assets: List[str]


_NO_OP_RESULT = MigrationResult(converted=False, missing_assets=[])


def migrate_material(material):
    # type: (bpy.types.Material) -> MigrationResult
    """
    Converts one material's ShaderNodeHeroEngine node (if present) to the
    equivalent native HeroEngine Shader, replacing the whole material via
    the same append-from-template mechanism material_builder.py and the
    interactive Add-menu use (types/shader_templates.py) -- every
    existing user of the old material redirected onto the new one, old
    material (old_node and its private node_tree included) deleted, new
    one renamed to take over the original name.

    The replacement is built and fully populated under a scratch name
    FIRST; the original is only touched (users remapped, deleted) once
    that has succeeded. Any failure before that point removes the
    scratch material and leaves the original exactly as it was, and
    re-raises.

    Real per-material values that only exist on the old node -- assigned
    images, alpha mode, palette settings, flesh/flush -- are read off
    BEFORE replacement and written onto the new material's equivalent
    inputs afterward, so migrating never loses an already-imported
    material's actual appearance. Only the NODE GRAPH gets blanket-
    replaced; the data it was carrying does not.

    Always completes the conversion even if a default map asset can't be
    found -- that map is simply left unassigned (same as it would've been
    before DEFAULT_MAP_ASSETS existed), reported via the returned
    MigrationResult.missing_assets rather than raised.
    """
    old_node = _find_hero_node(material)
    if old_node is None:
        return _NO_OP_RESULT

    # Read ONCE, up front, into a local. old_node lives inside `material`,
    # which is deleted at the end of this function -- after that point
    # old_node is a freed node, and reading any property off it returns
    # that property's default (for `derived`, 'CREATURE') rather than
    # raising. Everything below uses this local, never old_node.derived.
    derived = old_node.derived

    config = MIGRATE_CONFIGS.get(derived)
    if config is None:
        raise ValueError(f"Unrecognized ShaderNodeHeroEngine.derived value: {derived!r}")

    # Everything in this block reads off `material`/`old_node`. Nothing
    # after the final swap at the bottom of this function may touch
    # `material` or `old_node` again; both become stale references the
    # moment the old material is removed.
    old_images = {}
    missing_assets = []
    for attr_name, node_name in config["image_maps"]:
        image = getattr(old_node, attr_name)
        if image is None:
            image = _load_default_map_image(node_name)
        if image is not None:
            old_images[node_name] = image
        elif node_name in DEFAULT_MAP_ASSETS:
            missing_assets.append(node_name[0].upper() + node_name[1:])

    # get_or_build_shader_material() below may internally orphan-purge
    # (whenever it has to dedupe a colliding node group -- see
    # types/shader_templates.py's _append_template_material()), and that
    # purge is GLOBAL: it sweeps every currently zero-real-user
    # datablock in the file, not just the duplicate it's actually after.
    # Holding a Python reference to an Image here doesn't protect it --
    # if its only real Blender-tracked user was old_node's own internal
    # per-instance node tree, it can already be at zero real users the
    # moment we read it above, making it exactly as vulnerable as an
    # unassigned material was earlier in this project.
    # Fake-user each one for the duration; each image's PRIOR
    # use_fake_user state is restored once it's safely assigned onto the
    # new material (or the whole operation is rolled back).
    images_fake_user_before = [(image, image.use_fake_user) for image in old_images.values()]
    for image in old_images.values():
        image.use_fake_user = True

    # The original material gets the same protection: it may have zero
    # real users (e.g. unassigned) and that purge would otherwise delete
    # it out from under us before the swap.
    material_fake_user_before = material.use_fake_user
    material.use_fake_user = True

    # alpha_mode/alpha_test_value/alpha_invert are the real, correct
    # values for this specific material -- read and preserve them
    # faithfully, don't default them (unlike material_builder.py's json-driven
    # import, which always forces CLIP -- not applicable here).
    alpha_blend = (old_node.alpha_mode == 'BLEND')
    alpha_test = (old_node.alpha_mode == 'CLIP')
    alpha_test_value = old_node.alpha_test_value
    alpha_invert = old_node.alpha_invert

    palette_values = {}
    for index, include_metallic_specular in config["palettes"]:
        prefix = f"palette{index}"
        palette_values[index] = {
            "hue": getattr(old_node, f"{prefix}_hue"),
            "saturation": getattr(old_node, f"{prefix}_saturation"),
            "brightness": getattr(old_node, f"{prefix}_brightness"),
            "contrast": getattr(old_node, f"{prefix}_contrast"),
            # FloatVectorProperty (RGB) -- getattr() returns a
            # bpy_prop_array, a live view into old_node's own RNA memory,
            # NOT an independent copy the way a plain FloatProperty read
            # is. tuple() forces an eager copy now, before old_node (and
            # the material it belongs to) gets deleted at the end --
            # otherwise this reference goes stale in between and
            # silently reads back garbage/zeroed data at the point of
            # use, rather than raising.
            "specular": tuple(getattr(old_node, f"{prefix}_specular")),
        }
        if include_metallic_specular:
            palette_values[index]["metallic_specular"] = tuple(getattr(old_node, f"{prefix}_metallic_specular"))

    flesh_brightness = old_node.flesh_brightness if config["flesh"] else None
    flush_tone = tuple(old_node.flush_tone) if config["flesh"] else None

    # Build and populate the replacement under a scratch name, leaving
    # the original completely untouched. force=True: the scratch name is
    # ours alone, but a stale one left behind by some earlier crash must
    # be replaced rather than reused.
    original_name = material.name
    new_mat = None
    try:
        new_mat = get_or_build_shader_material(derived, _SCRATCH_MATERIAL_NAME, force=True)
        group_node = find_main_shader_group_node(new_mat, derived)
        image_nodes = {
            node.name: node for node in new_mat.node_tree.nodes
            if node.type == 'TEX_IMAGE'
        }

        # Images -- already-loaded bpy.data.images datablocks, reassign
        # directly onto the new external Image Texture nodes, no reload.
        for node_name, image in old_images.items():
            if node_name not in image_nodes:
                raise ShaderTemplateError(
                    f"Template material for {derived!r} has no Image Texture node named "
                    f"'{node_name}' (migrating '{original_name}')."
                )
            image_nodes[node_name].image = image

        group_node.inputs['Alpha Blend'].default_value = alpha_blend
        group_node.inputs['Alpha Test'].default_value = alpha_test
        group_node.inputs['Alpha Test Value'].default_value = alpha_test_value
        group_node.inputs['Invert Alpha'].default_value = alpha_invert

        # Palettes
        for index, values in palette_values.items():
            socket_prefix = f"Palette{index} "
            group_node.inputs[f'{socket_prefix}Hue'].default_value = values["hue"]
            group_node.inputs[f'{socket_prefix}Saturation'].default_value = values["saturation"]
            group_node.inputs[f'{socket_prefix}Brightness'].default_value = values["brightness"]
            group_node.inputs[f'{socket_prefix}Contrast'].default_value = values["contrast"]
            group_node.inputs[f'{socket_prefix}Specular'].default_value = values["specular"]
            if "metallic_specular" in values:
                group_node.inputs[f'{socket_prefix}Metallic Specular'].default_value = values["metallic_specular"]

        # Flesh / Flush
        if config["flesh"]:
            group_node.inputs['Flesh Brightness'].default_value = flesh_brightness
            group_node.inputs['Flush Tone'].default_value = flush_tone
    except Exception:
        # Roll back: the original was never modified, so just discard the
        # scratch material and undo the temporary fake users.
        if new_mat is not None:
            bpy.data.materials.remove(new_mat)
        material.use_fake_user = material_fake_user_before
        for image, fake_user_before in images_fake_user_before:
            image.use_fake_user = fake_user_before
        raise

    # Success -- only now replace the original. After the remove() below,
    # `material` and `old_node` are stale and must not be touched again.
    material.user_remap(new_mat)
    bpy.data.materials.remove(material)
    new_mat.name = original_name
    new_mat.use_fake_user = material_fake_user_before
    for image, fake_user_before in images_fake_user_before:
        image.use_fake_user = fake_user_before

    return MigrationResult(converted=True, missing_assets=missing_assets)


# ----------------------------------------------------------------------
# Operators -- all three share the `swtor.migrate_*` category so they
# list together in search (F3), per Crunch: no dedicated menu UI, just
# discoverable/favoritable/reusable-by-other-addons standard operators.
# ----------------------------------------------------------------------

def _migrate_and_report(operator, materials):
    # type: (bpy.types.Operator, List[bpy.types.Material]) -> None
    """
    Shared execute() body for the two batch operators (selected objects /
    whole file) -- runs migrate_material() over `materials` and reports a
    three-bucket summary: converted cleanly, converted but missing a
    default asset (still a successful conversion, just flagged), and
    failed outright (conversion didn't complete). Per-material detail for
    the latter two prints to console; self.report() gets the counts.

    Not shared with the single-material operator, which reports directly
    since there's only ever one material and no console dump is useful
    for that.
    """
    converted = 0
    skipped = 0
    incomplete = []  # (material_name, missing_assets)
    failed = []  # (material_name, error)

    for material in materials:
        # Captured before the call: migrate_material() deletes the old
        # material datablock outright on a successful conversion (see
        # its own docstring), so `material.name` would be a stale
        # reference afterward.
        original_name = material.name
        try:
            result = migrate_material(material)
        except Exception as err:
            failed.append((original_name, str(err)))
            continue

        if not result.converted:
            skipped += 1
        else:
            converted += 1
            if result.missing_assets:
                incomplete.append((original_name, result.missing_assets))

    for name, missing in incomplete:
        print(f"[SWTOR migrate] '{name}' migrated, but couldn't find default assets for: {', '.join(missing)}")
    for name, err in failed:
        print(f"[SWTOR migrate] Failed to migrate '{name}': {err}")

    msg = f"Migrated {converted} material(s), skipped {skipped} (no deprecated shader found)"
    if incomplete:
        msg += f", {len(incomplete)} missing default assets (see console)"
    if failed:
        msg += f", {len(failed)} failed (see console)"

    if failed:
        operator.report({'ERROR'}, msg)
    elif incomplete:
        operator.report({'WARNING'}, msg)
    else:
        operator.report({'INFO'}, msg)


class SWTOR_OT_migrate_shader(bpy.types.Operator):
    """Converts this material's deprecated SWTOR shader node to the native HeroEngine Shader node group, in place"""
    bl_idname = 'swtor.migrate_shader'
    bl_label = 'Migrate Atroxa Shader (Current Material)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.material is not None

    def execute(self, context):
        material = context.material
        # Captured before the call -- see _migrate_and_report()'s same
        # note; migrate_material() deletes the old material datablock
        # outright on a successful conversion.
        
        original_name = material.name
        result = migrate_material(material)
        if not result.converted:
            self.report({'WARNING'}, f"'{original_name}' has no deprecated SWTOR shader node to migrate")
        elif result.missing_assets:
            self.report(
                {'WARNING'},
                f"Migrated '{original_name}', but couldn't find default assets for: {', '.join(result.missing_assets)}",
            )
        else:
            self.report({'INFO'}, f"Migrated '{original_name}' to the native HeroEngine Shader")
        return {'FINISHED'}


class SWTOR_OT_migrate_shaders_selected(bpy.types.Operator):
    """Converts every deprecated SWTOR shader node found on materials across all selected objects to native HeroEngine Shader node groups, in place"""
    bl_idname = 'swtor.migrate_shaders_selected'
    bl_label = 'Migrate Atroxa Shaders (Selected Objects)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0

    def execute(self, context):
        seen = set()
        materials = []
        for ob in context.selected_objects:
            for slot in ob.material_slots:
                material = slot.material
                if material is None or material.name in seen:
                    continue
                seen.add(material.name)
                materials.append(material)

        _migrate_and_report(self, materials)
        return {'FINISHED'}


# ----------------------------------------------------------------------
# Shift+A discoverability -- appended (not authored) onto the existing
# "SWTOR Shaders (Deprecated)" submenu (NODE_MT_swtor_shaders_menu_
# deprecated, defined in shaders_menu_deprecated.py) via Blender's
# Menu.append() mechanism, the same technique __init__.py already uses
# to hook NODE_MT_add itself. This deliberately avoids editing
# shaders_menu_deprecated.py at all -- that file, and everything else in
# the deprecated system, stays untouched; only __init__.py (already an
# accepted exception for registration) needs to import this function and
# call NODE_MT_swtor_shaders_menu_deprecated.append(...)/.remove(...) in
# register()/unregister().
#
# A plain F3 Menu Search (Blender's default since 2.90) only finds items
# already present in a menu -- these operators weren't reachable that
# way until this entry existed. bpy.ops.swtor.migrate_*() direct calls
# and Developer-Extras Operator Search worked before this and still do.
def swtor_migrate_submenu_element(self, context):
    # type: (bpy.types.Menu, bpy.types.Context) -> None
    layout = self.layout
    layout.separator()
    layout.operator(SWTOR_OT_migrate_shader.bl_idname, text="Migrate This Material")
    layout.operator(SWTOR_OT_migrate_shaders_selected.bl_idname, text="Migrate Selected Objects' Materials")
    layout.operator(SWTOR_OT_migrate_shaders_file.bl_idname, text="Migrate All Materials in File")


class SWTOR_OT_migrate_shaders_file(bpy.types.Operator):
    """Converts every deprecated SWTOR shader node found on any material in this .blend to native HeroEngine Shader node groups, in place"""
    bl_idname = 'swtor.migrate_shaders_file'
    bl_label = 'Migrate Atroxa Shaders (Whole File)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _migrate_and_report(self, list(bpy.data.materials))
        return {'FINISHED'}

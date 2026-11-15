# <pep8 compliant>

"""
Shader template delivery -- replaces the old approach of building every
SWTOR shader's node graph directly via the Python bpy API (see the now-
retired node_group.py / node_tree.py) with appending fully pre-wired
materials from a single bundled template .blend (HeroEngine_Shaders.blend).

Why materials, not bare node groups: each template material carries not
just the shader's native node group, but the external Image Texture
nodes, reroutes, and (for Creature/HairC/HairC Modern) the DirectionMapUV
detour, all already positioned -- exactly what shaders_menu.py's old
SWTOR_SHADER_GROUPS 'maps' table used to construct by hand in Python.
Appending the whole material gets all of that for free, and moves layout
maintenance into Blender's own node editor (reviewable by Zero/Koda
directly) instead of a Python coordinate table.

Dedup: bpy.data.libraries.load() has no concept of "this already exists
locally, reuse it" -- every append blindly creates fresh copies of
everything it pulls in, auto-suffixed by Blender's own name-collision
handling (see _append_template_material()'s docstring for the full
append -> remap -> orphan-purge mechanics this requires).
"""

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Dict

import bpy


class ShaderTemplateError(Exception):
    """
    Raised when HeroEngine_Shaders.blend is missing, or doesn't contain what
    this module expects of it (a named template material, or that
    material lacking any of our own shader group nodes). Both are
    addon-setup/template-integrity problems, not recoverable per-material
    -- there's nothing useful to build without a working template.
    """


# ---------------------------------------------------------------------------
# Bundled template .blend
# ---------------------------------------------------------------------------

TEMPLATE_BLEND_FILENAME = "HeroEngine_Shaders.blend"

# shader_type (matches the old SWTOR_SHADER_GROUPS keys) -> template
# material name in HeroEngine_Shaders.blend.
SHADER_TEMPLATE_MATERIAL_NAMES = {
    "UBER": "TEMPLATE - Uber Shader",
    "GRASS": "TEMPLATE - Grass Shader",
    "EYE": "TEMPLATE - Eye Shader",
    "CREATURE": "TEMPLATE - Creature Shader",
    "HAIRC": "TEMPLATE - HairC Shader",
    "HAIRC_MODERN": "TEMPLATE - HairC Modern Shader",
    "GARMENT": "TEMPLATE - Garment Shader",
    "SKINB": "TEMPLATE - SkinB Shader",
    "ANIMATEDUV": "TEMPLATE - AnimatedUV Shader",
    "NOSHADETEXFOGGED": "TEMPLATE - NoShadeTexFogged Shader",
    "VEGETATION": "TEMPLATE - Vegetation Shader",
    "EMISSIVEONLY": "TEMPLATE - EmissiveOnly Shader",
}  # type: Dict[str, str]

# shader_type -> that material's own MAIN shader group's exact
# node_tree name. Kept as an explicit mapping, not derived from
# SHADER_TEMPLATE_MATERIAL_NAMES by string transformation (e.g.
# stripping "TEMPLATE - " and prepending a fixed prefix) -- the group name
# is what the code matches against, so it stays a plain per-type lookup
# that must equal the .blend's own name exactly. Every group now follows
# the "HeroEngine SWTOR - X" convention. Adding a new shader type always
# means adding an entry here, whatever its group happens to be named in
# the .blend.
SHADER_GROUP_NAMES = {
    "UBER": "HeroEngine SWTOR - Uber Shader",
    "EYE": "HeroEngine SWTOR - Eye Shader",
    "CREATURE": "HeroEngine SWTOR - Creature Shader",
    "HAIRC": "HeroEngine SWTOR - HairC Shader",
    "HAIRC_MODERN": "HeroEngine SWTOR - HairC Modern Shader",
    "GARMENT": "HeroEngine SWTOR - Garment Shader",
    "SKINB": "HeroEngine SWTOR - SkinB Shader",
    "ANIMATEDUV": "HeroEngine SWTOR - AnimatedUV Shader",
    "GRASS": "HeroEngine SWTOR - Grass Shader",
    "NOSHADETEXFOGGED": "HeroEngine SWTOR - NoShadeTexFogged Shader",
    "VEGETATION": "HeroEngine SWTOR - Vegetation Shader",
    "EMISSIVEONLY": "HeroEngine SWTOR - EmissiveOnly Shader",
}  # type: Dict[str, str]


def _bundled_data_dir():
    # type: () -> Path
    """<addon root>/bundled_data -- one level up from this file (types/)."""
    return Path(__file__).resolve().parent.parent / "bundled_data"


def _template_blend_path():
    # type: () -> Path
    """
    Resolves HeroEngine_Shaders.blend's path under bundled_data/, raising
    ShaderTemplateError immediately if it's missing rather than letting
    bpy.data.libraries.load() fail later with a less informative error.
    """
    path = _bundled_data_dir() / TEMPLATE_BLEND_FILENAME
    if not path.exists():
        raise ShaderTemplateError(
            "'%s' not found under %s.\n\n"
            "This file should be bundled with the addon -- if it's "
            "missing, the install is incomplete or corrupted."
            % (TEMPLATE_BLEND_FILENAME, _bundled_data_dir())
        )
    return path


# ---------------------------------------------------------------------------
# Shader-group detection -- also used by material_builder.py's
# get_or_create_material() to distinguish an already-built material from
# a same-named material that merely exists (e.g. a default-BSDF
# placeholder import_gr2.py's low-level .gr2 reader creates for every
# material name embedded in the file itself, independent of this system).
# ---------------------------------------------------------------------------

def has_swtor_shader_group(material):
    # type: (bpy.types.Material) -> bool
    """
    True if `material` already contains one of our own shader group
    nodes -- i.e. it was actually built by this module (or the legacy
    Python builders) at some point, as opposed to merely existing under
    a given name. Matches against SHADER_GROUP_NAMES' known exact names
    (not a shared naming-convention prefix -- see that dict's own
    docstring for why).
    """
    if not material.use_nodes or material.node_tree is None:
        return False
    known_names = set(SHADER_GROUP_NAMES.values())
    return any(
        node.type == 'GROUP' and node.node_tree is not None
        and node.node_tree.name in known_names
        for node in material.node_tree.nodes
    )


def _find_top_level_group_nodes(node_tree):
    # type: (bpy.types.NodeTree) -> list
    """
    Every top-level GROUP node in `node_tree`, regardless of naming
    convention -- used for dedup, which doesn't care whose group it is,
    only that it's a top-level reference vulnerable to the same '.001'
    collision problem as any other appended node group.

    Deliberately NOT filtered by SHADER_GROUP_NAMES' known main-group
    names: Creature/
    HairC/HairC Modern each wire an external DirectionMapUV instance as
    a SIBLING of the main shader group (see the old shaders_menu.py's
    vector_source detour, now baked into the template itself) -- and
    DirectionMapUV's own group name ("DirectionMapUV", see node_group.py)
    does NOT carry our prefix. A prefix-filtered search would silently
    miss it: as a top-level node it isn't nested inside anything that
    would catch it via the main group's own orphan-purge cascade, so an
    unremapped duplicate would just accumulate silently, undetected, on
    every subsequent append. Every other node_group.py utility group is
    nested INSIDE a main shader group rather than exposed at this level,
    so it's already covered by that cascade once the group containing it
    is remapped -- only top-level siblings need this explicit pass.
    """
    return [
        node for node in node_tree.nodes
        if node.type == 'GROUP' and node.node_tree is not None
    ]


def _strip_dot_suffix(name):
    # type: (str) -> str
    """Strips a trailing Blender collision suffix like '.001', recovering
    the base/canonical name a freshly-appended, auto-renamed datablock
    was originally meant to be."""
    return re.sub(r"\.\d{3}$", "", name)


# ---------------------------------------------------------------------------
# Append + dedup
# ---------------------------------------------------------------------------

# While a template_copies() block is open: shader_type -> an untouched copy of that
# type's template material, kept so later materials of the type can be copied from it
# instead of appended from the .blend again. None outside such a block.
_template_copy_session = None


@contextmanager
def template_copies():
    """
    Use around a bulk build (the Area Assembler's materials pass): every material of
    a shader type after the first is a COPY of that type's freshly appended template
    instead of another bpy.data.libraries.load() -- about 150 ms each, plus a global
    orphans_purge() whenever a group had to be deduped -- which on a big import was
    the largest remaining cost. The copies share the same canonical shader group the
    dedup produces, so what comes out matches appending again. The kept originals
    are removed when the block ends (they hold nothing a purge would need to clean
    up: the material that was appended first holds the same groups and images).
    Re-entrant: an inner block just joins the outer one.
    """
    global _template_copy_session
    if _template_copy_session is not None:
        yield
        return
    _template_copy_session = {}
    try:
        yield
    finally:
        pristines = list(_template_copy_session.values())
        _template_copy_session = None
        for material in pristines:
            try:
                bpy.data.materials.remove(material)
            except (ReferenceError, RuntimeError):
                pass


def _append_template_material(shader_type, scratch_name):
    # type: (str, str) -> bpy.types.Material
    """
    Returns a fresh, unconfigured template material for shader_type under
    scratch_name: appended from the template .blend, or -- inside a
    template_copies() block, from the second material of a type on -- copied from
    the first one's untouched original. See _append_template_material_from_blend()
    for everything about what the result looks like.
    """
    session = _template_copy_session
    if session is not None:
        pristine = session.get(shader_type)
        if pristine is not None:
            try:
                new_mat = pristine.copy()
            except ReferenceError:      # something purged our copy -- append again below
                session.pop(shader_type, None)
            else:
                new_mat.use_fake_user = False
                new_mat.name = scratch_name
                return new_mat
    new_mat = _append_template_material_from_blend(shader_type, scratch_name)
    if session is not None:
        # Keep an untouched copy NOW, before the caller starts configuring new_mat.
        kept = new_mat.copy()
        kept.use_fake_user = True
        kept.name = "__shader_template_copy_source__ " + shader_type
        session[shader_type] = kept
    return new_mat


def _append_template_material_from_blend(shader_type, scratch_name):
    # type: (str, str) -> bpy.types.Material
    """
    Appends shader_type's template material from HeroEngine_Shaders.blend
    under a throwaway name (scratch_name), deduping every one of its
    top-level shader-group nodes against whatever's already canonical in
    this file.

    bpy.data.libraries.load() has no built-in dedup: it always creates
    fresh copies of everything it pulls in (recursively -- a template
    material's own nested sub-groups come along too), auto-suffixed
    ".001" etc. on any name collision. So for each top-level group node
    on the freshly appended material, if a same-BASE-named group already
    exists locally, we reassign that node's node_tree onto the existing
    (canonical) one and orphan-purge the now-unreferenced duplicate --
    which cascades through anything nested inside it too, since
    do_recursive=True follows the whole newly-orphaned chain, not just
    the top node.

    The appended material has zero real users until the caller assigns
    it somewhere, which happens after this returns -- orphans_purge()
    is a global sweep, not scoped to this call, so without a temporary
    fake user the material itself (and any other still-unassigned
    material left over from an earlier call in the same session) would
    be just as eligible for removal as the duplicate group we actually
    want gone.
    """
    template_mat_name = SHADER_TEMPLATE_MATERIAL_NAMES.get(shader_type)
    if template_mat_name is None:
        raise ValueError("Unrecognized SWTOR shader type: %r" % (shader_type,))

    template_path = str(_template_blend_path())

    with bpy.data.libraries.load(template_path, link=False) as (data_from, data_to):
        if template_mat_name not in data_from.materials:
            raise ShaderTemplateError(
                "Template material '%s' not found in %s.\n\n"
                "Available materials: %s"
                % (template_mat_name, template_path, list(data_from.materials))
            )
        data_to.materials = [template_mat_name]
    new_mat = data_to.materials[0]

    new_mat.use_fake_user = True

    group_nodes = _find_top_level_group_nodes(new_mat.node_tree)
    expected_group_name = SHADER_GROUP_NAMES.get(shader_type)
    if expected_group_name is None or not any(
        _strip_dot_suffix(n.node_tree.name) == expected_group_name for n in group_nodes
    ):
        raise ShaderTemplateError(
            "Appended template material '%s' (for shader type %r) doesn't "
            "contain the expected main shader group ('%s'). The template "
            ".blend may be corrupted or out of date -- regenerate it, or "
            "add/fix this shader type's entry in SHADER_GROUP_NAMES."
            % (new_mat.name, shader_type, expected_group_name)
        )

    needs_purge = False
    for node in group_nodes:
        base_name = _strip_dot_suffix(node.node_tree.name)
        canonical = bpy.data.node_groups.get(base_name)
        if canonical is not None and node.node_tree != canonical:
            node.node_tree = canonical
            needs_purge = True

    if needs_purge:
        bpy.data.orphans_purge(do_recursive=True)

    new_mat.use_fake_user = False
    new_mat.name = scratch_name
    return new_mat


def find_main_shader_group_node(material, shader_type):
    # type: (bpy.types.Material, str) -> bpy.types.Node
    """
    Returns the material's own main shader group node -- e.g. the "HeroEngine
    SWTOR - Uber Shader" instance -- as opposed to any sibling detour
    group (DirectionMapUV, TransformAllUVs) that may also be present at
    the top level. Callers needing to set inputs like Alpha Blend/
    Palette/Flesh Brightness want this one specifically, not a detour
    group.

    shader_type picks which exact name to look for (SHADER_GROUP_NAMES)
    -- matching against a shared naming-convention prefix isn't reliable
    across shader types, see that dict's own docstring.

    Raises ShaderTemplateError if shader_type isn't in SHADER_GROUP_NAMES,
    or if that exact group isn't found on `material` -- both indicate a
    material/template in an unexpected state rather than something to
    guess through.
    """
    expected_group_name = SHADER_GROUP_NAMES.get(shader_type)
    if expected_group_name is None:
        raise ShaderTemplateError("Unrecognized SWTOR shader type: %r" % (shader_type,))

    matches = [
        node for node in _find_top_level_group_nodes(material.node_tree)
        if node.node_tree.name == expected_group_name
    ]
    if len(matches) != 1:
        raise ShaderTemplateError(
            "Expected exactly one '%s' group node on material '%s', found %d."
            % (expected_group_name, material.name, len(matches))
        )
    return matches[0]


def get_or_build_shader_material(shader_type, target_name, force=False):
    # type: (str, str, bool) -> bpy.types.Material
    """
    Returns a Material named target_name, built from shader_type's
    template if it doesn't already exist in the desired state.

    Three cases when force=False (the default -- material_builder.py's
    idempotent "build once" usage):
      1. A material named target_name already exists AND has one of our
         shader groups in it (has_swtor_shader_group()) -- already
         built, reused outright, nothing appended.
      2. A material named target_name exists but ISN'T one of ours --
         e.g. a default-BSDF placeholder import_gr2.py's .gr2 reader
         creates for every material name embedded in the file itself,
         entirely independent of this system (existence under the
         target name is therefore never itself proof of having been
         built by us). Every existing user of that placeholder (every
         mesh slot referencing it, anywhere in the file) is redirected
         onto the newly-built material via user_remap(), the placeholder
         is deleted, and only then is the new material renamed into the
         now-free target_name -- avoiding any collision.
      3. No material named target_name exists at all -- appended fresh
         and renamed directly.

    force=True skips case 1's "already built, reuse" shortcut and always
    replaces target_name with a freshly appended template, following
    case 2's replace-and-rename mechanics even if the existing material
    was already one of ours. Used by the interactive Add-menu and the
    deprecated-shader migrator -- both deliberate "convert/replace this
    material right now" actions rather than idempotent build-once calls,
    where overwriting an already-native material under this name (e.g.
    switching shader types via the menu) is exactly the intent.
    """
    existing = bpy.data.materials.get(target_name)
    if not force and existing is not None and has_swtor_shader_group(existing):
        return existing

    new_mat = _append_template_material(shader_type, "__shader_template_scratch__")

    if existing is not None:
        existing.user_remap(new_mat)
        bpy.data.materials.remove(existing)

    new_mat.name = target_name
    return new_mat
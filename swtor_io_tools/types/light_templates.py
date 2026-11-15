# <pep8 compliant>

"""
Lighting template delivery -- appends the bundled "SWTOR_Gobo" Light
data-block from HeroEngine_Shaders.blend, the same append -> remap-group-
nodes-by-exact-name -> orphan-purge mechanism types/shader_templates.py
already uses for materials (see that module's own docstring for the
full "why" -- bpy.data.libraries.load() has no dedup of its own, so
every append needs the same remap step to avoid a fresh, unwanted
".001" copy of the shared node group piling up on every single light).

What's different from the material case, confirmed against a real node
dump of HeroEngine_Shaders.blend's "SWTOR_Gobo" object:

- The template is a Light data-block (AREA type, use_nodes=True), not
  a Material -- so this module works against bpy.data.lights, not
  bpy.data.materials.
- UNLIKE a shader material (one shared node group instance per shader
  TYPE, reused across every material of that type), every light needs
  its own fully independent outer node tree, because each light's own
  three TEX_IMAGE nodes ("rampMap", "Falloff", "illuminationMap") carry
  a genuinely different .dds image per light. Only the shared INNER
  group ("HeroEngine SWTOR - Gobo Lighting" -- this string has to match
  the .blend's own node_tree name exactly) gets deduped; the outer tree and its three texture nodes are
  deliberately left as fresh per-append copies for the caller to
  configure per-instance.
- The shared group's "Color" and "strength" inputs are NOT wired to
  anything internally -- they're plain unconnected sockets with demo
  default values in the template itself, meant to be set directly per
  light instance (confirmed: Emission.Color reads directly from the
  group's own Color input, with no texture multiplied in at all -- the
  three texture maps only feed Emission.Strength via two chained
  MULTIPLY nodes). This module exposes find_group_input_socket() for
  that; it deliberately looks the socket up by its NodeSocket.name
  ("Color"/"strength", kept in sync with the node tree's own Interface
  item names by Blender) rather than by its identifier (e.g.
  "Socket_5") -- socket identifiers are arbitrary and were NOT assigned
  in interface-declaration order in the template as inspected (Color
  came back as "Socket_5", strength as "Socket_6", RampMap as
  "Socket_4" -- non-sequential), so they're not a naming convention
  worth hardcoding against; a future re-export of the template
  regenerating those identifiers would silently break identifier-based
  lookups without changing anything a human would notice.
- The template's "RampMap" group input is confirmed NOT connected to
  anything inside the shared group at all right now (WIP per Crunch) --
  find_texture_node() still exposes the "rampMap" TEX_IMAGE node the
  same as the other two, and callers should still resolve/assign its
  image (cheap, and avoids a second pass through this module once the
  group wiring catches up), just don't expect it to visibly do
  anything yet.
"""

import re
from pathlib import Path
from typing import Dict, Optional

import bpy


class LightTemplateError(Exception):
    """
    Raised when HeroEngine_Shaders.blend is missing, or doesn't contain
    what this module expects of it (the named template Light
    data-block, that Light lacking its expected shared node group, or
    an expected socket/node missing from it). All of these are
    addon-setup/template-integrity problems, not recoverable
    per-light -- mirrors ShaderTemplateError's own reasoning exactly.
    """


# ---------------------------------------------------------------------------
# Bundled template .blend
# ---------------------------------------------------------------------------

# Deliberately re-declared here rather than imported from
# shader_templates.py -- matches this codebase's existing convention
# of each bundled-data-consuming module resolving its own path
# independently (see types/area.py's own _bundled_data_dir(), which
# does exactly this same three-line lookup rather than importing a
# shared helper) rather than introducing a cross-module coupling for
# three trivial lines.
TEMPLATE_BLEND_FILENAME = "HeroEngine_Shaders.blend"

# The bundled Light data-block's own name (confirmed via node dump --
# Object 'SWTOR_Gobo' -> Light data-block 'SWTOR_Gobo', same name on
# both).
TEMPLATE_LIGHT_NAME = "SWTOR_Gobo"

# The shared inner node group carrying the actual SWTOR lighting logic
# (Emission fed by RampMap/Falloff/IlluminationMap multiplied together,
# tinted by a plain Color input) -- confirmed exact name from the node
# dump. Must match that node tree's real name in the .blend exactly.
LIGHT_GROUP_NAME = "HeroEngine SWTOR - Gobo Lighting"

# Node names of the three TEX_IMAGE nodes on the OUTER (per-instance)
# node tree that feed the shared group's three map inputs -- confirmed
# via node dump. Keyed by our own semantic name (matching the room
# json's own field names, see types/area.py's LightInfo) rather than
# these literal node names, so callers never need to know the node
# names themselves.
TEXTURE_NODE_NAMES = {
    "falloff_map": "Falloff",
    "ramp_map": "rampMap",
    "illumination_map": "illuminationMap",
}  # type: Dict[str, str]

# NodeSocket.name (not .identifier -- see module docstring) of the
# shared group's two per-instance-configurable inputs.
GROUP_INPUT_SOCKET_NAMES = {
    "color": "Color",
    "strength": "strength",
}  # type: Dict[str, str]


def _bundled_data_dir():
    # type: () -> Path
    """<addon root>/bundled_data -- one level up from this file (types/)."""
    return Path(__file__).resolve().parent.parent / "bundled_data"


def _template_blend_path():
    # type: () -> Path
    """
    Resolves HeroEngine_Shaders.blend's path under bundled_data/, raising
    LightTemplateError immediately if it's missing -- mirrors
    shader_templates.py's own _template_blend_path() exactly.
    """
    path = _bundled_data_dir() / TEMPLATE_BLEND_FILENAME
    if not path.exists():
        raise LightTemplateError(
            "'%s' not found under %s.\n\n"
            "This file should be bundled with the addon -- if it's "
            "missing, the install is incomplete or corrupted."
            % (TEMPLATE_BLEND_FILENAME, _bundled_data_dir())
        )
    return path


# ---------------------------------------------------------------------------
# Shared-group detection -- mirrors shader_templates.py's
# has_swtor_shader_group()/_find_top_level_group_nodes(), operating on
# a Light's node_tree instead of a Material's.
# ---------------------------------------------------------------------------

def has_swtor_light_group(light):
    # type: (bpy.types.Light) -> bool
    """
    True if `light` already contains our own shared lighting group node
    -- i.e. it was actually built by this module at some point, as
    opposed to some other Light that merely exists.
    """
    if not light.use_nodes or light.node_tree is None:
        return False
    return any(
        node.type == 'GROUP' and node.node_tree is not None
        and node.node_tree.name == LIGHT_GROUP_NAME
        for node in light.node_tree.nodes
    )


def _find_top_level_group_nodes(node_tree):
    # type: (bpy.types.NodeTree) -> list
    """Every top-level GROUP node in `node_tree` -- see
    shader_templates.py's own _find_top_level_group_nodes() docstring;
    identical reasoning, just not filtered to any particular group
    name (dedup doesn't care whose group it is)."""
    return [
        node for node in node_tree.nodes
        if node.type == 'GROUP' and node.node_tree is not None
    ]


def _strip_dot_suffix(name):
    # type: (str) -> str
    """Strips a trailing Blender collision suffix like '.001' -- see
    shader_templates.py's own _strip_dot_suffix()."""
    return re.sub(r"\.\d{3}$", "", name)


# ---------------------------------------------------------------------------
# Append + dedup
# ---------------------------------------------------------------------------

def append_template_light():
    # type: () -> bpy.types.Light
    """
    Appends the bundled "SWTOR_Gobo" Light data-block ONCE and returns it, with
    its shared group node already deduped -- a pristine source for
    copy_template_light(). The caller keeps it for the length of a batch (it
    carries a fake user so nothing purges it) and must remove it afterwards
    with bpy.data.lights.remove().

    One library append costs around 100 ms; a batch used to pay it for every
    distinct light (hundreds of them).
    """
    light = build_light_from_template(TEMPLATE_LIGHT_NAME + " (template)")
    light.use_fake_user = True
    return light


def copy_template_light(template_light, name):
    # type: (bpy.types.Light, str) -> bpy.types.Light
    """
    A new, independent Light for `name`, copied from append_template_light()'s
    result: its own outer node tree and texture nodes (for the caller to
    configure), the same shared inner group and default images every
    appended copy ended up with after build_light_from_template()'s dedup.
    Matches build_light_from_template()'s result, including the fake user it
    leaves set for the caller to clear.
    """
    new_light = template_light.copy()
    new_light.use_fake_user = True
    new_light.name = name
    return new_light


def build_light_from_template(name):
    # type: (str) -> bpy.types.Light
    """
    Appends a FRESH copy of the bundled "SWTOR_Gobo" Light data-block
    from HeroEngine_Shaders.blend, dedupes its one shared top-level group
    node (LIGHT_GROUP_NAME) against whatever's already canonical in
    this file, and renames the result to `name`.

    UNLIKE shader_templates.py's get_or_build_shader_material(), this
    does NOT check for or reuse an already-existing same-named Light --
    callers here (see ops/import_room_lights.py's _RealLightCache) are
    responsible for their own build-once-per-distinct-content caching,
    since "one Light data-block per exact JSON value combination" is a
    different dedup key than "one Material per shader type", and lives
    closer to the JSON-parsing caller than to this template-mechanics
    module.

    The caller still owns configuring the result: setting
    light.color/energy/use_shadow directly, the shared group's
    Color/strength inputs (find_group_input_socket()), and each texture
    node's .image (find_texture_node()).

    CONFIRMED REAL BUG this function used to have, now fixed: it used
    to call bpy.data.orphans_purge(do_recursive=True) itself, right
    here, whenever the group-node dedup above actually remapped
    something. bpy.data.orphans_purge() is a GLOBAL sweep -- not scoped
    to anything this function touched -- and this codebase's own
    _MeshCache (see ops/import_area.py) deliberately holds Python-only
    references to mesh data-blocks that can be momentarily real-user-
    free (e.g. right after _reduce_speedtree_lods() deletes a discarded
    LOD's wrapper Object, that LOD's own Mesh data lives on ONLY inside
    _MeshCache's own cache dict until a later instantiate() call gives
    it a fresh Object again). Calling a global purge from anywhere in
    the middle of Area Assembler's main per-element loop -- which is
    exactly where every real light gets built, the first time each
    distinct combination is encountered -- silently deleted exactly
    that kind of intentionally-orphaned-for-now data out from under
    _MeshCache, corrupting its cached Python references into dangling
    StructRNA pointers ("StructRNA of type Mesh has been removed" the
    next time that path was reused). The freshly-appended Light itself
    was equally vulnerable, for the same reason -- it has zero real
    users until the caller wraps it in an Object, which happens AFTER
    this function returns, so an immediate purge here could (and did)
    delete the very Light this function had just built.

    Fixed by not purging here at all -- the now-unreferenced duplicate
    group node_tree(s) this remap leaves behind are harmless bloat
    (unused ID data-blocks, not a correctness problem) until SOMETHING
    eventually purges them. That's the caller's job, at a point in time
    it can guarantee is safe -- ops/import_area.py's ImportAREA.execute()
    runs exactly one bpy.data.orphans_purge() call, once, after every
    single load() call in the whole batch has already finished (same
    timing already proven safe for materials processing, which runs
    strictly after that same loop for the exact same reason).
    """
    template_path = str(_template_blend_path())

    with bpy.data.libraries.load(template_path, link=False) as (data_from, data_to):
        if TEMPLATE_LIGHT_NAME not in data_from.lights:
            raise LightTemplateError(
                "Template Light '%s' not found in %s.\n\n"
                "Available lights: %s"
                % (TEMPLATE_LIGHT_NAME, template_path, list(data_from.lights))
            )
        data_to.lights = [TEMPLATE_LIGHT_NAME]
    new_light = data_to.lights[0]

    new_light.use_fake_user = True

    if new_light.node_tree is None:
        new_light.use_fake_user = False
        raise LightTemplateError(
            "Appended template Light '%s' has no node_tree (use_nodes "
            "wasn't on in the source .blend). The template .blend may "
            "be corrupted or out of date." % (TEMPLATE_LIGHT_NAME,)
        )

    group_nodes = _find_top_level_group_nodes(new_light.node_tree)
    if not any(_strip_dot_suffix(n.node_tree.name) == LIGHT_GROUP_NAME for n in group_nodes):
        raise LightTemplateError(
            "Appended template Light '%s' doesn't contain the expected "
            "shared lighting group ('%s'). The template .blend may be "
            "corrupted or out of date." % (new_light.name, LIGHT_GROUP_NAME)
        )

    for node in group_nodes:
        base_name = _strip_dot_suffix(node.node_tree.name)
        canonical = bpy.data.node_groups.get(base_name)
        if canonical is not None and node.node_tree != canonical:
            node.node_tree = canonical
            # No orphans_purge() here -- see this function's own
            # docstring for why that's now deliberately deferred to
            # ImportAREA.execute(), once, at the very end of the batch.

    # `light` keeps use_fake_user = True here, deliberately NOT reset to
    # False before returning -- unlike shader_templates.py's equivalent
    # (safe there because materials get a real user, and its own single
    # purge call, immediately afterward with nothing in between; a real
    # light instance's own caller does more work first -- texture
    # assignment -- before it gets a real user, and this addon no
    # longer purges synchronously at any point during that window
    # either, per this function's own docstring, so leaving fake_user
    # on is now the only thing keeping this Light itself safe from
    # whatever ELSE might legitimately purge in between (e.g. another,
    # later call into this same function for a different light). The
    # caller (_RealLightCache) is responsible for clearing it once this
    # Light has a real user of its own.
    new_light.name = name
    return new_light


# ---------------------------------------------------------------------------
# Per-instance accessors
# ---------------------------------------------------------------------------

def find_light_group_node(light):
    # type: (bpy.types.Light) -> bpy.types.Node
    """
    Returns `light`'s own instance of the shared lighting group node --
    mirrors shader_templates.py's find_main_shader_group_node(). Raises
    LightTemplateError if not found or ambiguous, same fail-loud
    template-integrity reasoning as that function.
    """
    matches = [
        node for node in _find_top_level_group_nodes(light.node_tree)
        if node.node_tree.name == LIGHT_GROUP_NAME
    ]
    if len(matches) != 1:
        raise LightTemplateError(
            "Expected exactly one '%s' group node on light '%s', found %d."
            % (LIGHT_GROUP_NAME, light.name, len(matches))
        )
    return matches[0]


def find_group_input_socket(light, key):
    # type: (bpy.types.Light, str) -> bpy.types.NodeSocket
    """
    Returns the shared lighting group instance's input socket for `key`
    ("color" or "strength", see GROUP_INPUT_SOCKET_NAMES) -- looked up
    by NodeSocket.name, not .identifier (see module docstring for why).
    Raises LightTemplateError if `key` is unrecognized or the expected
    socket isn't found on the group instance.
    """
    socket_name = GROUP_INPUT_SOCKET_NAMES.get(key)
    if socket_name is None:
        raise LightTemplateError("Unrecognized light group input key: %r" % (key,))

    group_node = find_light_group_node(light)
    for socket in group_node.inputs:
        if socket.name == socket_name:
            return socket

    raise LightTemplateError(
        "Expected an input socket named '%s' on light '%s''s lighting "
        "group, not found. The template .blend may be corrupted or out "
        "of date." % (socket_name, light.name)
    )


def find_texture_node(light, key):
    # type: (bpy.types.Light, str) -> Optional[bpy.types.Node]
    """
    Returns `light`'s own instance of the TEX_IMAGE node for `key`
    ("falloff_map"/"ramp_map"/"illumination_map", see
    TEXTURE_NODE_NAMES), or None if not found (soft -- unlike the group
    node/socket lookups above, a caller reassigning a texture is
    already in soft-fail territory itself, see
    ops/import_room_lights.py).
    """
    node_name = TEXTURE_NODE_NAMES.get(key)
    if node_name is None:
        return None
    return light.node_tree.nodes.get(node_name)
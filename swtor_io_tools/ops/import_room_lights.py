# <pep8 compliant>

"""
Real per-light building for Area Assembler's ".lit" elements -- Case 1
only (a direct ".lit" element carrying its own "light" JSON data; see
types/area.py's LightInfo docstring for why Case 2, a ".lit" reached
via ".spn_p" -> ".dyn" expansion, never reaches this module at all and
keeps using ops/import_area.py's existing generic placeholder path).

Sits directly on top of types/light_templates.py (the append/dedup
mechanics) -- this module is the piece that actually knows what a room
json's "light" object means: which JSON field goes to which node/
socket/property, how a batch-wide cache key is built so identical
lights across a whole import share one Light data-block, and how a
.dds texture path gets resolved and loaded against this addon's own
Resources Directory convention.

Deliberately imports _apply_transform/_build_placeholder_light FROM
ops/import_area.py rather than the reverse -- see that module's own
top-of-file note on why it only ever imports THIS module back via a
function-local deferred import, same cycle-avoidance reasoning already
established there for ops/import_fxspec.py.
"""

import math
from pathlib import Path

import bpy
from mathutils import Matrix

from .import_area import _apply_transform, _build_placeholder_light
from ..types.shared import resolve_resource_path
from ..types import light_templates

# CONFIRMED REAL-BLENDER-TESTING BUG, fixed here: an AREA light built
# from the template, positioned via the exact same _apply_transform()
# every other element type already uses, came out shining straight up
# (ceiling-ward) instead of down (floor-ward) -- a full 180 degree
# flip, not a coordinate-convention problem in the general transform
# pipeline itself (unchanged, shared with every other element type,
# and already proven correct for all of them). Blender's own hard rule
# that an AREA light always shines along its OWN local -Z axis is what
# actually differs here -- nothing else this addon builds has an
# inherent "facing direction" tied to one specific local axis the way
# a Light does, so this is the first element type where that mismatch
# could even show up. Applied as a local-space rotation, composed
# AFTER _apply_transform()'s own world rotation in
# _process_light_element() below -- correcting "which way this
# object's own local -Z points" is independent of where in the room
# it's been placed/faced.
#
# Axis picked as X -- a specific choice, not "doesn't matter which":
# if the falloff/illumination cookie ends up visibly mirrored
# left-right versus up-down once compared against real SWTOR lighting,
# that's this needing to be 'Y' instead (a 180 degree flip about X vs.
# Y both correct the up/down shine direction identically, but mirror
# the rectangle's own texture-mapped footprint differently) -- flag it
# back and it's a one-line change. CONFIRMED correct via real testing
# (Crunch: "the gobo rotation we did on the light seems to be fine").
_ORIENTATION_CORRECTION_AXIS = 'X'
_ORIENTATION_CORRECTION_RADIANS = math.pi

# REVERSED, confirmed wrong via real Blender testing: this used to
# force every real light's Spread to math.pi (180 degrees), reasoning
# that the template's own baked-in 0 was a leftover/unset value rather
# than deliberate. It wasn't -- Crunch's own testing with actual
# gobo/cookie-textured lights showed 180 degrees blows the cookie
# pattern out entirely, and 0 degrees (a tight, coherent beam, which
# is the whole point of a "gobo" -- a narrow, directional projector,
# same as the theatrical fixture it's named after) is what actually
# renders the illuminationMap texture as a recognizable projected
# pattern. Every real light observed so far DOES carry an
# illuminationMap (100% of a 1625-light sample), so there's no
# observed case needing anything other than 0 -- set explicitly
# (rather than just leaving the template's own value untouched) so a
# future template re-export changing that default doesn't silently
# change this addon's own behavior without it being obvious here.
LIGHT_SPREAD_RADIANS = 0.0

# Rough Eevee-parity Watts-per-unit-intensity conversion -- derived
# from the bundled template's OWN authored demo values (light.energy
# was 10.0 against the shared group's own default strength of 1.2,
# confirmed via node dump) rather than invented from nothing, but
# still a GUESS, not verified against any real SWTOR reference
# lighting yet (Crunch's own call: "guess one to start... we'll refine
# as we go"). The Cycles path (the shared group's own "strength"
# socket) doesn't need this conversion factor at all -- it takes
# `intensity` directly, 1:1, since that same template default (1.2)
# sits right in the middle of the observed real intensity range
# (0.3-1.3).
#
# REVERSED, confirmed wrong via real Blender testing: both this AND
# strength used to also get scale_factor multiplied in, reasoning that
# a fixed intensity value "becomes visually meaningless once the whole
# scene has been scaled up" (matching _LightCache's own point-light
# precedent). Real testing showed the opposite of "too low" -- going
# above the template's own defaults (strength > 1.2, energy > 10)
# blows the cookie out and loses the pattern entirely, the same
# failure mode as forcing Spread to 180 above. In hindsight this makes
# physical sense: Strength/Emission represent RADIANCE (intensity per
# unit area), not total power -- unlike a POINT light (which has no
# inherent size, so scaling its Watts is the only available "make it
# look right at a different scale" knob), an AREA light's own PHYSICAL
# SIZE already carries scale_factor once that's correctly applied to
# the light Object itself (see _process_light_element's own
# scale_factor handling below, mirroring _process_fxspec_element's
# established anchor.scale pattern) -- a physically bigger light
# emitting the SAME radiance naturally puts more total light into a
# proportionally bigger room, with no need to also inflate the
# radiance value itself. The actual "power looks too low" symptom
# reported earlier was root-caused to the light Object's own scale
# never getting scale_factor applied at all (real lights were
# importing ~10x too small), not to strength/energy being too low --
# see that fix in _process_light_element.
EEVEE_ENERGY_PER_INTENSITY = 10.0 / 1.2  # ~8.333


class _RealLightCache:
    """
    Batch-wide cache of built real (SWTOR_Gobo-template) Light
    data-blocks, keyed by the exact tuple of JSON values that determine
    a light's appearance -- so two elements (in the same file OR across
    different files in the same batch import) sharing identical color/
    intensity/shadow/texture values reuse the SAME Light data-block,
    same "one shared data-block per distinct combination" reasoning
    _MeshCache already applies to repeated source .gr2 files. Batch-wide
    rather than per-file (unlike ops/import_area.py's own _LightCache,
    which only ever needs one entry so the question doesn't arise) -- a
    reused light preset (the same torch, say) is exactly as likely to
    repeat ACROSS files in one zone as within a single one.

    Building an entry is NOT cheap (a full .blend library append) --
    this cache is what keeps that rare, not something paid per element.

    Also tracks template-integrity failures "stickily": once
    light_templates.build_light_from_template() (or the group/socket
    lookups right after it) has failed once, every further call re-
    raises that SAME cached error immediately rather than re-attempting
    a doomed append for every single remaining light in the batch --
    a broken/missing bundled .blend is a template problem, not a
    per-light one, and shouldn't cost an append attempt (or a warning
    line) per element on top of the one that already explained it.
    """
    def __init__(self, resources_root):
        self._resources_root = resources_root
        self._by_key = {}   # cache key -> bpy.types.Light
        self._area_name = None  # label only, NOT part of the cache key -- see class docstring
        self._template_error = None  # sticky, see class docstring
        # The pristine appended template every built light is copied from: appended
        # once, on the first light that needs it, removed by release_template().
        self._template_light = None

    def set_area_name(self, area_name):
        # type: (str) -> None
        """Called once per file by ops/import_area.py's load() -- see
        class docstring for why area_name only names freshly-built
        entries, never participates in the cache key itself."""
        self._area_name = area_name

    def object_name(self):
        # type: () -> str
        """"light - <area name>" -- mirrors _LightCache.object_name()'s
        own naming/reasoning exactly, just for real lights."""
        return "light - %s" % (self._area_name or "Area")

    def release_template(self):
        """
        Removes the pristine template Light append_template_light() made (its
        default images, if no built light kept them, are left orphaned for the
        final purge). Safe to call more than once; ImportAREA.execute() calls
        it after the last light is built and again from its finally.
        """
        template, self._template_light = self._template_light, None
        if template is not None:
            try:
                bpy.data.lights.remove(template)
            except (ReferenceError, RuntimeError):
                pass

    def needs_final_purge(self):
        # type: () -> bool
        """
        True once this cache has actually built at least one real
        light -- see build_light_from_template()'s own docstring for
        why the resulting leftover duplicate node groups/default
        template images are deliberately left unpurged until now.
        ImportAREA.execute() checks this once, after the whole batch's
        load() calls have all finished, to decide whether its own
        single deferred bpy.data.orphans_purge() call is worth running
        at all (skips it entirely if Create Scene Lights was off, or no
        ".lit" element in the whole batch carried real light data).
        """
        return bool(self._by_key)

    def get_or_build(self, light_info, warnings):
        # type: (object, list) -> bpy.types.Light
        """
        Returns a Light data-block matching `light_info` (a
        types/area.py LightInfo), building a fresh one via
        light_templates on a cache miss. `warnings` collects human-
        readable strings for anything soft-failed while building a NEW
        entry (a texture that couldn't be resolved/loaded) -- a cache
        HIT never appends to `warnings` again, matching _MeshCache's own
        "warn once per distinct source, not once per reuse" reasoning.

        Raises light_templates.LightTemplateError on a template-
        integrity problem (missing/corrupted bundled .blend, missing
        group/socket) -- deliberately NOT caught here; the caller
        (_process_light_element) is where the soft-fail-to-placeholder
        decision belongs, since it's also the one reporting it.
        """
        if self._template_error is not None:
            raise self._template_error

        key = (
            light_info.color, light_info.intensity, light_info.casts_shadows,
            light_info.falloff_map, light_info.ramp_map, light_info.illumination_map,
        )
        cached = self._by_key.get(key)
        if cached is not None:
            return cached

        try:
            light = self._build(light_info, warnings)
        except light_templates.LightTemplateError as exc:
            self._template_error = exc
            # Known minor leftover: `light` (if build_light_from_template()
            # itself succeeded but a later step -- e.g. a missing socket --
            # raised) still carries use_fake_user=True and now has no real
            # user and never will, so it won't even be cleaned up by the
            # batch-end purge (fake_user blocks that deliberately). At
            # most one such data-block per batch (this error is sticky --
            # see class docstring), so left as harmless bloat rather than
            # adding cleanup for a template-is-broken path that shouldn't
            # come up in normal use anyway.
            raise

        self._by_key[key] = light
        return light

    def _build(self, light_info, warnings):
        # type: (object, list) -> bpy.types.Light
        name = "%s - Light" % (self._area_name or "Area")
        if self._template_light is None:
            self._template_light = light_templates.append_template_light()
        light = light_templates.copy_template_light(self._template_light, name)

        # Eevee parity (plain properties -- Crunch's own call: "if
        # Eevee can't do it [via the node tree], there's nothing to be
        # done about that", but these ARE read by Eevee directly).
        # Deliberately NOT scaled by scale_factor -- see this module's
        # own EEVEE_ENERGY_PER_INTENSITY comment for why (confirmed via
        # real testing that scaling either of these, rather than the
        # light Object's own physical size, was wrong).
        light.color = light_info.color
        light.energy = light_info.intensity * EEVEE_ENERGY_PER_INTENSITY
        light.use_shadow = light_info.casts_shadows
        light.spread = LIGHT_SPREAD_RADIANS  # see module-level constant's own comment

        # Cycles (the shared group's own per-instance inputs -- both
        # unconnected/direct-value sockets in the template itself, see
        # light_templates.py's module docstring). strength likewise NOT
        # scaled by scale_factor -- same reasoning as light.energy just
        # above.
        color_socket = light_templates.find_group_input_socket(light, "color")
        color_socket.default_value = (light_info.color[0], light_info.color[1], light_info.color[2], 1.0)
        strength_socket = light_templates.find_group_input_socket(light, "strength")
        strength_socket.default_value = light_info.intensity

        texture_fields = (
            ("falloff_map", light_info.falloff_map),
            ("ramp_map", light_info.ramp_map),
            ("illumination_map", light_info.illumination_map),
        )
        for key, relative_path in texture_fields:
            node = light_templates.find_texture_node(light, key)
            if node is None or not relative_path:
                continue
            image = _load_light_texture(self._resources_root, relative_path)
            if image is None:
                warnings.append(
                    "\"%s\" -- texture not found on disk, kept template default" % (relative_path,)
                )
                continue
            node.image = image
            # This leaves the template's own default Image data-block
            # (whatever it came appended with) with one fewer real
            # user. Deliberately NOT purging that here, or anywhere
            # else during Area Assembler's main per-element loop -- see
            # light_templates.build_light_from_template()'s own
            # docstring for the confirmed real bug that caused
            # (_MeshCache's cached-but-momentarily-userless mesh data
            # getting deleted out from under it by a mid-loop global
            # purge). ImportAREA.execute() runs exactly one
            # bpy.data.orphans_purge() at the very end of the whole
            # batch instead, which sweeps up every light built during
            # it (these leftover default images included) in one safe,
            # already-proven-safe-for-materials pass.

        # `light` still carries use_fake_user = True at this point (see
        # build_light_from_template()'s own docstring for why) -- clear
        # it now, right before returning to a caller
        # (_process_light_element) that immediately wraps this Light in
        # a real Object with no purge in between, which is what
        # actually keeps it safe from here on. A cache HIT never
        # reaches this function again for the same key, so this only
        # ever runs once per distinct light.
        light.use_fake_user = False
        return light


def _load_light_texture(resources_root, relative_path):
    # type: (str, str) -> object
    """
    Resolves and loads one lighting .dds texture against
    `resources_root` ONLY -- Area Assembler has never supported a
    legacy_resources_root fallback anywhere else in this module (see
    types/shared.py's own resolve_resource_path_multi() for where
    that DOES apply, elsewhere in this addon), so this doesn't
    introduce one just for lighting textures either.

    Reuses an already-loaded Blender image with the same filename if
    one exists (same dedup-by-basename convention ops/material_builder.py's
    own _load_or_get_image() uses for every other SWTOR texture this
    addon loads), and sets the same CHANNEL_PACKED/Non-Color convention
    that function applies -- these are grayscale falloff/illumination
    masks, not diffuse color textures, so Non-Color is the right
    colorspace regardless of which of the three this is.

    Returns None (never raises) if the path can't be resolved or
    doesn't exist on disk -- soft-fail; caller decides how to report
    it.
    """
    path = resolve_resource_path(resources_root, relative_path)
    if path is None or not Path(path).is_file():
        return None
    name = Path(path).name
    existing = bpy.data.images.get(name)
    if existing:
        return existing
    try:
        image = bpy.data.images.load(path)
    except RuntimeError:
        return None
    image.alpha_mode = 'CHANNEL_PACKED'
    image.colorspace_settings.name = 'Non-Color'
    return image


def _apply_light_orientation_correction(light_ob):
    # type: (bpy.types.Object) -> None
    """
    Composes the fixed local-space correction (see this module's own
    _ORIENTATION_CORRECTION_* constants) onto `light_ob`'s already-
    -applied world rotation. Only ever called for real (AREA) lights --
    a placeholder POINT light has no facing direction to correct.
    """
    correction = Matrix.Rotation(_ORIENTATION_CORRECTION_RADIANS, 4, _ORIENTATION_CORRECTION_AXIS)
    corrected = light_ob.rotation_euler.to_matrix().to_4x4() @ correction
    light_ob.rotation_euler = corrected.to_euler(light_ob.rotation_euler.order)


def _process_light_element(operator, context, resources_root, real_light_cache, light_cache,
                            element, transform, scale_factor, skip_counts, light_warnings):
    # type: (object, object, str, _RealLightCache, object, object, object, float, object, list) -> list
    """
    Handles one direct ".lit" AreaElement carrying real per-light JSON
    data (element.light is not None -- see ops/import_area.py's
    _process_area_element for why this only ever runs for THAT case).

    Real-light building is the default whenever this runs at all -- no
    separate toggle beyond the existing "Create Scene Lights" master
    switch, which is still checked here first (same gate/same skip
    label _process_element()'s own placeholder path uses, so the
    console summary reads identically regardless of which path a
    disabled toggle would have taken). On ANY failure building/
    configuring the real light, silently falls back to the exact same
    generic placeholder _process_element() would have built anyway,
    WITH a console-reported warning -- per Crunch's own call: "real
    light building should be the default always, and fall back to a
    placeholder on failure with report in console".

    scale_factor handling: CONFIRMED REAL BUG this fixes -- real lights
    were importing with their Object.scale about 10x too small
    (Crunch's own real-Blender-testing report). Root cause:
    _apply_transform() sets Object.scale straight from
    ResolvedTransform.scale, which -- per compute_final_transforms()'s
    own docstring -- deliberately does NOT include scale_factor,
    because that's normally baked into imported MESH geometry instead
    (see _MeshCache). A Light has no mesh/vertex data of its own to
    bake it into, so nothing was ever applying scale_factor to it at
    all. Fixed the exact same way _process_fxspec_element() already
    solves the identical problem for its own anchor Empty (see that
    function's own docstring's "scale_factor handling" section) --
    multiplying scale_factor directly onto the Object's own .scale,
    right after placing it.
    """
    if not operator.area_create_lights:
        skip_counts["'.lit' skipped (Create Scene Lights disabled) (expected)"] += 1
        return []

    try:
        light_data = real_light_cache.get_or_build(element.light, light_warnings)
    except light_templates.LightTemplateError as exc:
        skip_counts["'.lit' real light build failed -- placeholder used instead"] += 1
        light_warnings.append("element %s: %s" % (element.id, exc))
        return [_build_placeholder_light(light_cache, transform)]

    light_ob = bpy.data.objects.new(real_light_cache.object_name(), light_data)
    _apply_transform(light_ob, transform)
    light_ob.scale = tuple(s * scale_factor for s in light_ob.scale)
    _apply_light_orientation_correction(light_ob)
    return [light_ob]
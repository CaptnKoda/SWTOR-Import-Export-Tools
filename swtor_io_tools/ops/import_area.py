# <pep8 compliant>

"""
Area Assembler -- Operator layer.

Builds real Blender objects from types/area.py's parsed elements and
resolved transforms: resolves each element's actual mesh reference
(direct .gr2/.mag/.spt, or a .spn_p indirection through spn_table,
possibly expanding into several .dyn visuals), imports it (reusing
mesh data for repeated source files rather than reimporting them every
time), and bakes its final position/rotation/scale directly onto the
resulting object(s).

Deliberately does NOT use Blender's live parenting for transform
inheritance anywhere except the organizational Empty grouping described
below -- every object's transform is set directly from
types/area.py's own already-composed values, matching this addon's
"design before coding" decision to bake absolute transforms in Python
rather than lean on bpy's own matrix_local/matrix_world composition
(which is how the old source did it, and not something we're repeating
here -- see that decision's own writeup).
"""

import json
import math
import os
import time
from collections import Counter
from pathlib import Path

import bpy
from mathutils import Matrix

from .import_gr2 import _MeshCache, _import_gr2, lod_name_info, sibling_lod_flags
from .import_hms import DEFAULT_MAX_VIEWPORT_INSTANCES, HmsBatch, process_terrain_element
from .process_materials import apply_materials_by_name_to_gr2_objects, _summarize_and_report
from ..types import area as area_types
from ..types.shared import job_results, resolve_resource_path

# NOTE: ops/import_fxspec.py is still imported lazily here (see
# _process_fxspec_element's own function-local import), a leftover from
# when it imported _MeshCache/_job_results_key FROM this module and a
# top-level import would have been an import cycle. Those helpers now
# live in ops/import_gr2.py (along with _import_gr2 and
# _merge_multi_mesh_objects), so that cycle no longer exists -- the
# deferred import was deliberately left as-is rather than changed in the
# same step as the move. Same deferred-import pattern is still in use
# between ops/import_gr2.py and ops/process_materials.py.
#
# NOTE: ops/import_room_lights.py is likewise deliberately NOT imported
# at module level here, same reasoning -- it imports _apply_transform/
# _build_placeholder_light FROM this module. See ImportAREA.execute()'s
# and _process_area_element()'s own function-local imports instead.


# ---------------------------------------------------------------------------
# Scope (handoff §2a)
# ---------------------------------------------------------------------------

# Extensions Area Assembler is meant to eventually do *something* with.
# Anything NOT in this set (.cvr, .trg, .enc, .rgn, .stg, .spn_c, .cam,
# .prt, .wtr, etc.) is genuinely out of scope per §2a -- gameplay/logic
# data with no visual meaning here -- and is skipped silently, not
# warned about; warning on every one of those would be noise, not
# signal (a single real area file can have thousands of them).
IN_SCOPE_EXTENSIONS = {"gr2", "mag", "spt", "spn_p", "hms", "lit", "fxp"}

# Of those, what this pass of Area Assembler actually builds Blender
# objects for via the generic .gr2/.mag/.spt import path. ".lit" and
# ".hms" are handled separately -- ".lit" in _process_element's own
# early branch (see _LightCache), ".hms" in _process_area_element's own
# early branch (see ops/import_hms.py) -- neither goes through
# the .gr2 importer at all, but both still count as "buildable" in
# spirit, just not through this specific path. The remainder (.fxp) is
# a real, in-scope format without an importer yet -- skipped with an
# aggregated warning (see _process_element) rather than silently, since
# it SHOULD eventually produce something and a stale bundled-data-style
# silent skip would hide that.
BUILDABLE_EXTENSIONS = {"gr2", "mag", "spt"}

# Object exclusion, confirmed against the old source (not guessed).
# Both identify genuinely different things -- filename vs. material --
# but are folded under one combined "Skip Game Engine Objects" toggle
# (area_skip_game_engine_objects) rather than kept separate, since both are
# fundamentally the same kind of thing to the person running an import:
# non-visual, tooling-only content nobody wants in an assembled scene.
#
# DBO_FILENAME_PREFIX -- design blockout utility objects (blockers,
# markers, etc.), identifiable by their own filename.
#
# MATERIAL_NAME_EXCLUSION -- covers what the old source's own comment
# describes as "Hero Engine utility object types [that] can't be
# determined by their names, but oftentimes their materials' names are
# a good criteria" -- objects like occluder walls, portals, and various
# "util_*_hidden" utility meshes are identified by which MATERIAL they
# use, not by their own filename. This is genuinely the mechanism
# behind e.g. "occluder_wall" objects being excluded -- not DBO-filename
# matching, which would never have caught those.
DBO_FILENAME_PREFIX = "dbo"

MATERIAL_NAME_EXCLUSION = {
    "collision",
    "dbo_universal_superexclusion_test",
    "mote_mote_a01_v01",
    "occluder",
    "occluder_terrain",
    "occluder_wall",
    "portal",
    "util_blue_hidden",
    "util_collision_hidden",
    "util_collision_none",
    "util_green_hidden",
    "util_red_hidden",
    "util_white_hidden",
    "util_yellow_hidden",
    "white_utility_hidden",
}


def _format_duration(seconds):
    # type: (float) -> str
    """
    "H:MM:SS", no leading zero on hours, milliseconds dropped --
    matches the old importer's own end-of-run report style, which
    Crunch specifically wants kept for very long (multi-hour) batch
    imports where a raw seconds count stops being readable at a glance.
    """
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return "%d:%02d:%02d" % (hours, minutes, secs)


def _has_excluded_material(ob):
    # type: (Any) -> bool
    raw = ob.get("gr2_material_names")
    if not raw:
        return False
    try:
        names = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return any(isinstance(n, str) and n.lower() in MATERIAL_NAME_EXCLUSION for n in names)


def _filter_excluded_objects(objects, skip_game_engine_objects, skip_counts):
    # type: (list, bool, Counter) -> list
    """
    Applied to every object _import_gr2() returns, whether freshly
    imported or reused from mesh_cache -- both need this check every
    single time, not just on a fresh import: a cached mesh's objects
    are recreated fresh from bpy.data.objects.new() on each reuse, so
    filtering has to be re-applied per use, not baked into the cache
    once.

    Both the DBO-filename and utility-material checks share the same
    skip_game_engine_objects toggle (area_skip_game_engine_objects) -- distinct
    mechanisms under the hood, but the same kind of thing from the
    perspective of someone running an import: non-visual, tooling-only
    content nobody wants in the assembled scene.

    Safe to delete any subset of the objects passed in here, including
    all of them: no multi-mesh sub-object relies on another for its own
    position (see _MeshCache.instantiate()'s own note on why), and
    parent-chain rotation/scale composition (compute_world_rotation_scale)
    reads the parsed area json directly, never an actual Blender object
    -- so excluding an element that other elements reference as their
    own parent doesn't affect those children's correctness either.
    """
    if not skip_game_engine_objects:
        return objects

    kept = []
    for ob in objects:
        reason = None
        if ob.name.lower().startswith(DBO_FILENAME_PREFIX):
            reason = "'dbo*'-named object(s) skipped (Skip Game Engine Objects) (expected)"
        elif _has_excluded_material(ob):
            reason = "object(s) using a known non-visual utility material skipped (e.g. occluder/portal/collision) (expected)"

        if reason is not None:
            skip_counts[reason] += 1
            # do_unlink=True scans every collection and scene in the file (about 5 ms with
            # 16,000 objects). An object nothing uses yet -- every repeat instance a mesh
            # cache hands out -- is in no collection, so there is nothing to unlink.
            bpy.data.objects.remove(ob, do_unlink=bool(ob.users))
        else:
            kept.append(ob)
    return kept


# ---------------------------------------------------------------------------
# .mag / .spt resolution (handoff §2a/§4.5)
# ---------------------------------------------------------------------------

def _resolve_mag_reference(resources_root, mag_relative_path):
    # type: (str, str) -> str or None
    """
    Naive text-scan of a .mag file for its "Mesh=...gr2" line, per
    handoff §4.5 -- deliberately not "improved" beyond what the old
    source did; see that decision's own writeup for why.

    Returns the referenced .gr2's relative path (leading slash
    stripped), or None if the file is missing or has no such line.
    """
    resolved_path = resolve_resource_path(resources_root, mag_relative_path)
    if resolved_path is None or not os.path.isfile(resolved_path):
        return None

    try:
        with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if ".gr2" in line and "Mesh=" in line:
                    referenced = line.split("Mesh=", 1)[1].strip()
                    if referenced.startswith("/") or referenced.startswith("\\"):
                        referenced = referenced[1:]
                    return referenced
    except OSError:
        return None
    return None


def _resolve_import_target(relative_path):
    # type: (str) -> tuple
    """
    Returns (resolved_relative_path, is_speedtree). ".spt" resolves to
    the identically-named ".gr2" -- confirmed in the old source, the
    SpeedTree mesh data lives at the same path with the extension
    swapped, not a separate lookup.
    """
    if relative_path.lower().endswith(".spt"):
        return relative_path[:-len(".spt")] + ".gr2", True
    return relative_path, False


# ---------------------------------------------------------------------------
# Mesh data cache (handoff's confirmed "carry forward the dedup/instancing
# idea, cleanly, not as the old flat dict" decision)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Lights (handoff's "Create Scene Lights" priority)
# ---------------------------------------------------------------------------

# Generic placeholder used when a ".lit" element carries no real
# per-light data at all (a ".lit" reached via ".spn_p" -> ".dyn"
# expansion, which never carries light data -- see
# ops/import_room_lights.py's module docstring for why), OR as the
# soft-fail fallback if building a REAL light from a direct ".lit"
# element's own data fails for any reason (see _build_placeholder_light
# below, and ops/import_room_lights.py's _process_light_element, which
# is where that fallback actually gets used).
DEFAULT_LIGHT_ENERGY = 2.0

# Per-file safeguard against tanking viewport performance with hundreds
# of real lights. The old source attempted this too but never actually
# worked -- its own comment admits "exclude_collection_lights DOESN'T
# WORK!!! Collections have no .exclude method". The correct API is
# LayerCollection.exclude (a view-layer-tree property), not anything on
# Collection itself -- confirmed by the old source's own OTHER, correct
# use of exactly that API elsewhere (ExcludeAfterImport) for a related
# feature, so this isn't a new pattern for this codebase, just a fixed
# version of an attempt that already existed.
LIGHT_COUNT_AUTO_EXCLUDE_THRESHOLD = 100


class _LightCache:
    """
    Holds ONE shared Light data-block for a whole area file, created
    lazily on first use. Every .lit-tagged element in one file reuses
    the SAME light data (matches the old source's own convention) --
    unlike _MeshCache, no per-path keying is needed, since every
    placeholder light is identical regardless of which element spawned
    it; only the per-instance Object (position) differs.
    """
    def __init__(self, area_name, scale_factor):
        self._area_name = area_name
        self._scale_factor = scale_factor
        self._light_data = None

    def get(self):
        if self._light_data is None:
            self._light_data = bpy.data.lights.new(name="%s - Light" % self._area_name, type='POINT')
            # Power scaled by scale_factor: a fixed energy value becomes
            # visually meaningless once the whole scene has been scaled
            # up or down by an order of magnitude (Crunch's own point --
            # "Power 2 will be useless if the scene is 10x larger").
            self._light_data.energy = DEFAULT_LIGHT_ENERGY * self._scale_factor
        return self._light_data

    def object_name(self):
        # type: () -> str
        """
        "light - <area name>" for every light Object this file's lights
        share -- deliberately NOT derived from the spawning element's
        own asset path (unlike mesh objects), so light names stay
        short and consistent, and carry the area name as a suffix to
        stay identifiable once many area files' lights sit in the same
        large scene together. Multiple lights in one file legitimately
        share this exact name; Blender's own ".001"/".002" auto-suffix
        handles the rest, same as any other repeated name in this addon.
        """
        return "light - %s" % self._area_name


def _build_placeholder_light(light_cache, transform):
    # type: (_LightCache, area_types.ResolvedTransform) -> Any
    """
    Builds one generic placeholder light Object from `light_cache`,
    placed via the usual _apply_transform(). Extracted out of
    _process_element()'s own ".lit" branch so ops/import_room_lights.py
    can reuse the exact same fallback for a direct ".lit" element whose
    real-light build failed -- see that module's _process_light_element.
    """
    light_ob = bpy.data.objects.new(light_cache.object_name(), light_cache.get())
    _apply_transform(light_ob, transform)
    return light_ob


def _find_layer_collection(layer_collection, target_collection):
    # type: (Any, Any) -> Any
    """
    Recursively searches the view layer's own LayerCollection tree
    (parallel to, but distinct from, the plain Collection hierarchy)
    for the node wrapping `target_collection`. Needed because .exclude
    only exists on LayerCollection, not Collection -- see the module
    note above on LIGHT_COUNT_AUTO_EXCLUDE_THRESHOLD for why that
    distinction matters here specifically.
    """
    if layer_collection.collection == target_collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, target_collection)
        if found is not None:
            return found
    return None


class _LightBudget:
    """
    Tracks the CUMULATIVE light count across an entire multi-file batch
    import, not per file -- created once per execute() call and shared
    across every load() call in that batch, same lifetime as mesh_cache.

    CONFIRMED real gap this fixes: a per-file-only threshold check
    never catches a batch where every individual file stays comfortably
    under the threshold but the SCENE ends up with thousands of real
    lights once every file's contribution is added together (e.g. 220
    files at ~50 lights each). Viewport performance is affected by how
    many lights are active in the scene at once, not by which file any
    one of them came from -- so the budget has to be batch-wide to mean
    anything for the actual use case (importing a whole area's worth of
    files together).

    Every file still gets its own named "<file> - Lights" Collection
    for organizational purposes (unchanged) -- this only decides
    whether each one, individually, ends up excluded from the view
    layer, based on the running total across every file processed so
    far in this batch.
    """
    def __init__(self, threshold):
        self._threshold = threshold
        self._total = 0
        self._collections = []
        self._exceeded = False
        # Lights collections waiting to be excluded from the view layer. Excluding
        # used to happen right here, one full view_layer.update() per file
        # (~0.4 s each on a 220-file batch); flush() now does it all after a
        # single update once the batch's load loop has finished.
        self._pending = []

    def register(self, context, lights_collection):
        # type: (Any, Any) -> None
        """
        Call once per file, after that file's lights_collection is
        finalized -- pass None if the file created no lights at all
        (nothing to track or exclude).
        """
        if lights_collection is None:
            return

        self._total += len(lights_collection.objects)
        self._collections.append(lights_collection)

        if self._exceeded:
            # Already over budget from an earlier file in this batch --
            # every subsequent file's lights collection gets excluded
            # too, without repeating the full warning every time.
            self._pending.append(lights_collection)
            return

        if self._total > self._threshold:
            self._exceeded = True
            print(
                "Area Assembler: cumulative light count across this batch reached "
                "%d (threshold %d) -- excluding %d Lights collection(s) from the "
                "view layer so far (viewport performance). Re-enable them in the "
                "Outliner if you want them visible; any further files in this "
                "batch will also be excluded automatically."
                % (self._total, self._threshold, len(self._collections))
            )
            self._pending.extend(self._collections)

    def flush(self, context):
        # type: (Any) -> None
        """
        Excludes every Lights collection queued by register(). Call once,
        after the batch's load loop (execute() also calls it from its
        finally, so an import that raised part-way still leaves the same
        exclusions as before; it does nothing the second time).

        ONE view_layer.update() first, then every exclusion: the update is
        what evaluates each light object's matrix_world, and objects in an
        excluded collection are no longer evaluated, so it has to come
        before the exclusions -- but it never needed to run once per file.
        Does nothing at all when the budget was never exceeded.
        """
        if not self._pending:
            return
        pending, self._pending = self._pending, []

        context.view_layer.update()
        # One walk of the view layer's collection tree instead of one search per
        # collection (LayerCollection.name is its Collection's name).
        by_name = {}
        stack = [context.view_layer.layer_collection]
        while stack:
            layer_collection = stack.pop()
            by_name[layer_collection.name] = layer_collection
            stack.extend(layer_collection.children)

        for collection in pending:
            layer_collection = by_name.get(collection.name)
            if layer_collection is not None:
                layer_collection.exclude = True
            else:
                # Never silently do nothing -- see this same reasoning on
                # the equivalent lookup failure this addon has hit before.
                print(
                    "Area Assembler: couldn't find \"%s\"'s LayerCollection to exclude "
                    "it -- left visible. Please report this if it keeps happening."
                    % collection.name
                )


def _lod_state(ob):
    # type: (Any) -> tuple
    """
    (is_lod, is_lod0) for an imported sub-object, judged by its OBJECT name and
    its MESH name. Both, because _MeshCache.instantiate() renames one
    sub-object of a multi-mesh file to the plain file stem for every reused
    instance (and Name Objects As Filenames does the same to the main mesh on
    a fresh import), so that object's own name no longer says it is a LOD; the
    mesh data keeps its original art name. What counts as a LOD name is
    import_gr2.lod_name_info().
    """
    names = [ob.name]
    if ob.data is not None:
        names.append(ob.data.name)
    is_lod = False
    is_lod0 = False
    for name in names:
        name_is_lod, name_is_lod0 = lod_name_info(name)
        is_lod = is_lod or name_is_lod
        is_lod0 = is_lod0 or name_is_lod0
    return is_lod, is_lod0


_lod_notes_seen = set()


def _reduce_speedtree_lods(objects, is_speedtree, element_name):
    # type: (list, bool, str) -> list
    """
    Carried forward from the old source: a SpeedTree (or tree-named)
    multi-mesh import commonly produces several LOD (level-of-detail)
    objects, of which only one should end up in the scene.

    Extended (confirmed against real files): the same reduction applies to ANY
    multi-mesh import whose sub-objects are named as LODs -- plant bundles
    ("LOD[0]", "LOD[1]", "LOD[2]") aren't tree-named but are the same thing --
    and the SpeedTree files' "<name>_l0/_l1/_l2" naming is recognised too
    (when siblings confirm it).

    Which LOD survives: the LOD0-named one if there is one, otherwise the
    highest-polygon one. (The old rule always kept the highest-polygon one
    and merely tolerated a "_lod0" name alongside it; but a file whose LOD[1]
    has as many polygons as its LOD[0] -- or more -- then kept both, and
    stored-LOD[1]-first order made a tie pick LOD[1].) Parts that aren't named
    as LODs are never touched.

    This is a name-substring heuristic, flagged in the handoff writeup as
    worth a deliberate confirm.
    """
    if len(objects) <= 1:
        return objects
    # "<base>_l0/_l1/_l2" LOD naming (SpeedTree files) only counts with sibling
    # evidence, judged on the mesh names (they keep the art names) -- see
    # import_gr2.sibling_lod_flags().
    sibling_flags = sibling_lod_flags([ob.data.name if ob.data is not None else ob.name for ob in objects])
    states = {}
    for ob, (sibling_is_lod, sibling_is_lod0) in zip(objects, sibling_flags):
        is_lod, is_lod0 = _lod_state(ob)
        states[ob] = (is_lod or sibling_is_lod, is_lod0 or sibling_is_lod0)
    has_lod_names = any(is_lod for is_lod, _ in states.values())
    if not (is_speedtree or "tree" in element_name.lower() or has_lod_names):
        return objects

    def polygons(ob):
        return len(ob.data.polygons) if ob.data else 0

    lod0_objects = [ob for ob in objects if states[ob][1]]
    highest = max(objects, key=polygons)
    keepers = lod0_objects if lod0_objects else [highest]

    kept = []
    removed = []
    for ob in objects:
        if ob in keepers or not states[ob][0]:
            kept.append(ob)
        else:
            removed.append(ob)

    # Worth a line, once per file per session: the file's LOD0 is NOT its most
    # detailed LOD, so the polygon counts and the names disagree about which
    # one is "the" model.
    heavier = [ob for ob in removed if lod0_objects and polygons(ob) >= max(polygons(k) for k in lod0_objects)]
    if heavier and element_name not in _lod_notes_seen:
        _lod_notes_seen.add(element_name)
        print(
            "LOD note on %r: keeping %s (%d polys) as LOD0; removed %s with as many or more polygons -- "
            "the file's LOD names and polygon counts disagree"
            % (element_name, [k.data.name if k.data else k.name for k in lod0_objects],
               max(polygons(k) for k in lod0_objects),
               [(o.data.name if o.data else o.name, polygons(o)) for o in heavier])
        )

    for ob in removed:
        bpy.data.objects.remove(ob, do_unlink=True)
    return kept


# ---------------------------------------------------------------------------
# Transform application
# ---------------------------------------------------------------------------

def _apply_transform(ob, transform):
    # type: (Any, area_types.ResolvedTransform) -> None
    """
    Converts `transform` (still in SWTOR's raw coordinate space -- see
    types/area.py's to_blender_space() docstring for why every upstream
    composition step deliberately stays in that space) into Blender's
    convention exactly once, here, then applies it. Every caller that
    sets an object's final position/rotation/scale should go through
    this function rather than setting those properties directly, so
    that conversion never accidentally gets applied twice (e.g. once to
    a parent Empty and again to its already-converted children).

    rotation_mode is set to 'ZXY' before assigning rotation_euler --
    matching types/area.py's own composition math exactly (see that
    module's note on _mat3_from_euler_zxy_degrees for why 'ZXY', not
    Blender's default 'XYZ' -- confirmed against the old source, not
    guessed). Every object this function touches needs this set
    consistently, since mixing rotation_mode across objects that are
    meant to represent the same underlying rotation convention would
    reintroduce exactly the kind of subtle mismatch this fixes.
    """
    blender_transform = area_types.to_blender_space(transform)
    ob.location = blender_transform.position
    ob.rotation_mode = 'ZXY'
    ob.rotation_euler = tuple(math.radians(d) for d in blender_transform.rotation)
    ob.scale = blender_transform.scale


def _basis_matrix(ob):
    # type: (Any) -> Matrix
    """
    Computes `ob`'s own local-to-world matrix directly from its
    location/rotation_mode/rotation_euler/scale properties, WITHOUT
    reading ob.matrix_world.

    CONFIRMED BUG this fixes: ob.matrix_world isn't guaranteed to
    reflect a transform just assigned via the Python API until a
    depsgraph evaluation actually runs, which doesn't happen
    synchronously -- reading it immediately after _apply_transform() (as
    the previous version of this file did, for parent_empty specifically)
    can return a stale/identity matrix. That produced an incorrect
    matrix_parent_inverse (effectively identity instead of the real
    inverse), which made Blender double-apply the parent Empty's
    transform on top of each child's own already-correct absolute
    transform -- confirmed against real reported numbers (a child's
    local location exactly matched its parent's, and its resulting
    world position exactly matched "parent transform applied twice").

    Only valid for a PARENTLESS object (matrix_world == this basis
    matrix in that case) -- which parent_empty always is, by
    construction, in this file. Uses ob.rotation_euler.to_matrix(),
    which is mathutils' own Euler-to-matrix conversion and correctly
    respects whatever ob.rotation_mode is currently set to -- not a
    hand-rolled formula, so this doesn't carry the same convention risk
    the pure-Python composition in types/area.py does.
    """
    return (
        Matrix.Translation(ob.location)
        @ ob.rotation_euler.to_matrix().to_4x4()
        @ Matrix.Diagonal(ob.scale).to_4x4()
    )


# ---------------------------------------------------------------------------
# Per-element import
# ---------------------------------------------------------------------------

def _process_element(operator, context, resources_root, mesh_cache, light_cache, relative_path, transform, skip_counts):
    # type: (Any, Any, str, _MeshCache, _LightCache, str, area_types.ResolvedTransform, Counter) -> list
    """
    Imports whatever `relative_path` resolves to and bakes `transform`
    onto the resulting top-level object(s) (objects that already have a
    parent -- either from the mesh cache's own multi-mesh handling, or
    however build() itself structures a fresh multi-mesh .gr2 import,
    which isn't confirmed either way -- inherit their transform through
    that parenting instead, rather than getting the same absolute
    transform applied redundantly).

    Returns the list of created objects (possibly empty).
    """
    resolved_target, is_speedtree = _resolve_import_target(relative_path)
    extension = resolved_target.rsplit(".", 1)[-1].lower() if "." in resolved_target else ""

    if extension == "lit":
        # Reached here only for a ".lit" element with no real light
        # data of its own -- either ".spn_p" -> ".dyn"-expanded (never
        # carries light data) or a direct element that somehow lacks
        # its "light" object (see _process_area_element's own dispatch,
        # which intercepts every direct ".lit" WITH real data before
        # this function is ever called at all). No file resolution
        # needed here either way -- a placeholder is just a marker
        # light, not a real asset on disk we read.
        if not operator.area_create_lights:
            skip_counts["'.lit' skipped (Create Scene Lights disabled) (expected)"] += 1
            return []
        return [_build_placeholder_light(light_cache, transform)]

    if extension == "mag":
        referenced = _resolve_mag_reference(resources_root, resolved_target)
        if referenced is None:
            skip_counts["'.mag' file missing or has no Mesh= reference"] += 1
            return []
        resolved_target, is_speedtree = _resolve_import_target(referenced)
        extension = resolved_target.rsplit(".", 1)[-1].lower() if "." in resolved_target else ""

    if extension not in BUILDABLE_EXTENSIONS:
        if extension in IN_SCOPE_EXTENSIONS:
            skip_counts["'.%s' -- in scope, importer not built yet (expected)" % extension] += 1
        # else: genuinely out of scope (§2a) -- not counted, not warned.
        return []

    on_disk_path = resolve_resource_path(resources_root, resolved_target)
    if on_disk_path is None or not os.path.isfile(on_disk_path):
        skip_counts["file not found on disk"] += 1
        return []

    objects = _import_gr2(operator, context, resources_root, on_disk_path, mesh_cache)
    if not objects:
        skip_counts["'.gr2' importer produced no objects"] += 1
        return []

    objects = _filter_excluded_objects(objects, operator.area_skip_game_engine_objects, skip_counts)
    if not objects:
        return []

    # The SpeedTree "tree"-in-name heuristic (see _reduce_speedtree_lods)
    # is checked against the resolved .gr2's own filename, not whatever
    # label the caller happened to have for this element -- an area
    # element's caller-side name is often just its raw numeric SWTOR id,
    # which could never contain "tree" and would silently defeat this
    # check entirely.
    objects = _reduce_speedtree_lods(objects, is_speedtree, Path(resolved_target).stem)

    for ob in objects:
        if ob.parent is None:
            _apply_transform(ob, transform)

    return objects


def _process_fxspec_element(operator, context, resources_root, element, transform, scale_factor,
                             fxspec_mesh_cache, fxspec_bone_follower_cache, skip_counts, fxspec_warnings):
    # type: (Any, Any, str, area_types.AreaElement, area_types.ResolvedTransform, float, _MeshCache, dict, Counter, list) -> list
    """
    Handles one direct `.fxp` AreaElement -- Case 1 of the fxspec/area
    integration handoff (element.asset_name itself authors the fx spec
    via element.fx_spec_name; a `.fxp` reached indirectly through a
    `.spn_p` -> `.dyn` expansion, Case 2, isn't routed here at all --
    see _process_area_element's own dispatch -- it still falls through
    to _process_element()'s generic "in scope, importer not built yet"
    skip, unchanged, deferred to its own future session).

    Builds a plain anchor Empty for this element, placed via the exact
    same _apply_transform() every other element type in this module
    already uses -- no new transform logic here at all -- then hangs
    the referenced .fxspec file's own resolved attach graph off that
    anchor via build_fxspec_graph(..., caster_object=anchor). See that
    function's own docstring for the caster_object mechanism.

    scale_factor handling: `fxspec_mesh_cache` is a DEDICATED cache,
    separate from this module's own main `mesh_cache` -- constructed at
    scale_factor=1.0 in ImportAREA.execute() (see its own comment for
    why). Sharing the main, real-scale_factor mesh_cache here instead
    would double-bake scale_factor: once into that cache's own cached
    vertex data, with nothing in the fxspec attach graph's own local
    offsets ever otherwise scaled to match, since caster_object bypasses
    build_fxspec_graph's root-bake entirely (see that function's own
    docstring). Instead, scale_factor is multiplied directly onto the
    anchor's own Object.scale, right after placing it -- ordinary
    Blender parent-child composition then carries that scale down
    through every graph entry's own local offset AND onto this
    dedicated cache's raw, unscaled .gr2 geometry uniformly, mirroring
    the standalone importer's own root-bake via a different (but
    equivalent) mechanism.

    `armature_ob` is always None here -- no skeleton exists in area
    context, so every entry that authors a bone name falls through
    build_fxspec_graph's existing "bone not found, treat as CASTER/
    TARGET" warning path, same as it would for any standalone import
    with no Armature selected. `import_all_empties` is hardcoded False
    -- area imports already produce a lot of objects across potentially
    hundreds of elements, so "only essential anchors" seems even more
    valuable here than in the standalone case (Crunch's own call,
    flagged as a leaning rather than a hard requirement -- worth
    revisiting as a real ImportAREA toggle later if it's actually
    wanted).

    `fxspec_warnings` collects raw per-entry warning detail across the
    WHOLE BATCH for one console dump at the end of ImportAREA.execute()
    -- but only for the "actionable" warning categories (a missing/
    empty .gr2, or a malformed attach chain -- see
    FXSPEC_ACTIONABLE_CATEGORIES below), where knowing exactly WHICH
    entry/file triggered it is worth the console space. Every warning
    category, actionable or not, is also counted into skip_counts under
    its own stable label (FXSPEC_WARNING_LABELS) -- this is the real
    fix for the original "one big undifferentiated bucket" version:
    the previously-benign-but-noisy categories (no Armature in area
    context, a real particle emitter Phase 1 doesn't build, a
    non-essential dummy skipped) fire on a large fraction of entries in
    a typical .fxspec, in every instance, across a whole area -- dumping
    all of that raw looked like a wall of errors even though almost
    none of it needed investigating. Those three categories now only
    ever show up as a skip_counts number, not a console line each.
    """
    # Deferred import -- see this module's own top-of-file note.
    from .import_fxspec import (
        build_fxspec_graph,
        WARN_NO_ARMATURE_BONE, WARN_GR2_NOT_FOUND, WARN_GR2_NO_OBJECTS,
        WARN_DANGLING_REFERENCE, WARN_CHAIN_CYCLE,
        WARN_REAL_EMITTER_SKIPPED, WARN_NON_ESSENTIAL_DUMMY_SKIPPED,
    )
    from ..types import fxspec as fxspec_types

    FXSPEC_WARNING_LABELS = {
        WARN_NO_ARMATURE_BONE: "'.fxspec' -- bone authored, no Armature in area context, anchored at element origin (expected)",
        WARN_GR2_NOT_FOUND: "'.fxspec' -- referenced .gr2 not found on disk",
        WARN_GR2_NO_OBJECTS: "'.fxspec' -- referenced .gr2 importer produced no objects",
        WARN_DANGLING_REFERENCE: "'.fxspec' -- attach chain references an unresolved entry",
        WARN_CHAIN_CYCLE: "'.fxspec' -- attach chain cycles back on itself",
        WARN_REAL_EMITTER_SKIPPED: "'.fxspec' -- real particle emitter skipped, not built yet (expected)",
        WARN_NON_ESSENTIAL_DUMMY_SKIPPED: "'.fxspec' -- dummy/anchor skipped, nothing attaches through it (expected)",
    }
    # Categories worth a specific console line (which entry, which
    # resource) because they indicate something an individual .fxspec
    # or .gr2 file actually got wrong, not just Phase-1-scope/area-
    # context housekeeping. Everything else still gets counted above,
    # just without per-entry detail.
    FXSPEC_ACTIONABLE_CATEGORIES = {
        WARN_GR2_NOT_FOUND, WARN_GR2_NO_OBJECTS, WARN_DANGLING_REFERENCE, WARN_CHAIN_CYCLE,
    }

    def _on_warn(category, message):
        skip_counts[FXSPEC_WARNING_LABELS.get(category, "'.fxspec' -- other warning")] += 1
        if category in FXSPEC_ACTIONABLE_CATEGORIES:
            fxspec_warnings.append('"%s": %s' % (element.fx_spec_name, message))

    if not element.fx_spec_name:
        skip_counts["'.fxp' element missing fx.fxSpecName -- skipped"] += 1
        return []

    resolved_path = resolve_resource_path(resources_root, element.fx_spec_name)
    if resolved_path is None or not os.path.isfile(resolved_path):
        skip_counts["'.fxspec' file not found on disk"] += 1
        return []

    try:
        with open(resolved_path, 'rb') as f:
            raw_bytes = f.read()
        root_node = fxspec_types.parse_fxspec(raw_bytes)
    except (fxspec_types.FxSpecParseError, OSError) as exc:
        skip_counts["'.fxspec' failed to parse"] += 1
        fxspec_warnings.append('"%s" -- failed to parse: %s' % (element.fx_spec_name, exc))
        return []

    # Named from the RESOLVED on-disk path's own stem, not
    # element.fx_spec_name directly -- that field is a raw backslash-
    # delimited SWTOR asset path (e.g.
    # "\art\fx\fxspec\mtx\mtx_item_republic_banner.fxspec"), which
    # pathlib would misparse on a non-Windows host (no backslash
    # path-separator handling on POSIX) -- resolved_path has already
    # been through resolve_resource_path()'s own OS-correct
    # normalization, same convention ImportFXSPEC.execute() already
    # uses for its own display_name.
    anchor = bpy.data.objects.new(Path(resolved_path).stem, None)
    anchor.empty_display_type = 'PLAIN_AXES'
    anchor.empty_display_size = 0.1
    context.collection.objects.link(anchor)
    _apply_transform(anchor, transform)
    # Structural scale_factor bake -- see this function's own docstring.
    anchor.scale = tuple(s * scale_factor for s in anchor.scale)

    built = build_fxspec_graph(
        operator, context, resources_root, root_node, context.collection,
        fxspec_mesh_cache, scale_factor, None, fxspec_bone_follower_cache,
        False, _on_warn, caster_object=anchor,
    )

    return [anchor] + list(built.values())


def _process_area_element(operator, context, resources_root, mesh_cache, light_cache, element, transform, scale_factor,
                           skip_counts, fxspec_mesh_cache, fxspec_bone_follower_cache, fxspec_warnings,
                           real_light_cache, light_warnings, hms_batch, area_name):
    # type: (Any, Any, str, _MeshCache, _LightCache, area_types.AreaElement, area_types.ResolvedTransform, float, Counter, _MeshCache, dict, list, Any, list, HmsBatch, str) -> list
    """
    Handles one parsed AreaElement with its already-computed final
    transform: resolves ".spn_p" indirection if present (possibly
    expanding into several ".dyn" visuals, each getting its own composed
    transform -- see types/area.py's compose_dyn_visual_transform), then
    imports whatever that resolves to.

    Returns the list of created objects (possibly empty, including an
    organizational Empty for dyn-expanded elements with multiple visuals
    -- see the dyn branch below).
    """
    asset_name = element.asset_name

    if asset_name.lower().endswith(".hms"):
        # Native terrain (ops/import_hms.py). Deliberately ignores `transform`:
        # heightmaps use their own authored transform, not the parent-composed
        # one (see import_hms.hms_transform_for_element); `scale_factor` reaches
        # it through hms_batch.
        return process_terrain_element(context, hms_batch, element, area_name, skip_counts)

    if asset_name.lower().endswith(".fxp"):
        # Case 1 only (see _process_fxspec_element's own docstring) --
        # intercepted here, one level up from _process_element(), since
        # that's where `element` (and therefore element.fx_spec_name)
        # is still in scope. A `.fxp` reached via `.spn_p` -> `.dyn`
        # expansion (Case 2) never reaches this branch at all -- it's a
        # synthetic DynVisual path string by the time it would get here,
        # not an AreaElement -- and still falls through to
        # _process_element()'s own generic extension dispatch below,
        # unchanged.
        return _process_fxspec_element(
            operator, context, resources_root, element, transform, scale_factor,
            fxspec_mesh_cache, fxspec_bone_follower_cache, skip_counts, fxspec_warnings,
        )

    if asset_name.lower().endswith(".lit") and element.light is not None:
        # Case 1 only, same reasoning as ".fxp" above -- intercepted
        # here, one level up from _process_element(), since that's
        # where `element.light` is still in scope. A ".lit" reached via
        # ".spn_p" -> ".dyn" expansion (Case 2) never carries light data
        # at all (see types/area.py's LightInfo docstring) and still
        # falls through to _process_element()'s own generic placeholder
        # path below, unchanged -- same as a direct ".lit" element that
        # somehow has no "light" object of its own (element.light is
        # None), which also isn't intercepted here.
        #
        # Deferred import -- see this module's own top-of-file note.
        from .import_room_lights import _process_light_element
        return _process_light_element(
            operator, context, resources_root, real_light_cache, light_cache,
            element, transform, scale_factor, skip_counts, light_warnings,
        )

    if not asset_name.lower().endswith(".spn_p"):
        return _process_element(
            operator, context, resources_root, mesh_cache, light_cache,
            asset_name, transform, skip_counts,
        )

    resolution = area_types.resolve_spn_p(asset_name)
    if resolution is None:
        skip_counts["'.spn_p' path not in spn_table (bundled data may be stale)"] += 1
        return []
    if resolution.target_type == "unresolved":
        # A known, already-investigated dead end recorded at bundled-data
        # generation time (see tools/generate_placeable_tables.py) --
        # not worth a warning every time, unlike a genuinely missing entry.
        return []

    if resolution.target_type == "direct":
        return _process_element(
            operator, context, resources_root, mesh_cache, light_cache,
            resolution.target, transform, skip_counts,
        )

    # resolution.target_type == "dyn": expand into synthetic child
    # visuals, each with its own composed transform.
    visuals = area_types.expand_dyn(resolution.target)
    if not visuals:
        return []

    # Organizational Empty grouping every visual this .spn_p spawned --
    # mirrors the old source's own approach (an Empty per dyn-expanded
    # element holding its resulting objects together) rather than each
    # visual floating independently in the collection. Named from the
    # dyn fqn's own last segment (e.g. "wonkavator_spaceport_vertical")
    # rather than the element's raw numeric SWTOR id -- readable in the
    # outliner, matching the same naming fix applied to actual mesh
    # objects (see _import_gr2's docstring).
    #
    # Each visual object below already has its correct absolute world
    # transform baked on directly (via _process_element, same as any
    # other object); parenting it to parent_empty without correcting
    # matrix_parent_inverse would make Blender reinterpret that
    # already-correct transform as being relative to the parent,
    # double-applying it -- confirmed as a real bug (see _basis_matrix's
    # docstring for the root cause and how this now avoids it).
    empty_name = resolution.target.rsplit(".", 1)[-1]
    parent_empty = bpy.data.objects.new(empty_name, None)
    parent_empty.empty_display_type = 'CUBE'
    parent_empty.empty_display_size = 0.1
    _apply_transform(parent_empty, transform)
    parent_empty_matrix = _basis_matrix(parent_empty)

    all_objects = [parent_empty]
    for visual in visuals:
        visual_transform = area_types.compose_dyn_visual_transform(transform, visual)
        objects = _process_element(
            operator, context, resources_root, mesh_cache, light_cache,
            visual.path, visual_transform, skip_counts,
        )
        for ob in objects:
            if ob.parent is None:
                ob.parent = parent_empty
                ob.matrix_parent_inverse = parent_empty_matrix.inverted()
        all_objects.extend(objects)

    return all_objects


# ---------------------------------------------------------------------------
# Orchestrator + Operator
# ---------------------------------------------------------------------------

def load(operator, context, filepath, resources_root, scale_factor, mesh_cache, light_budget,
         fxspec_mesh_cache, fxspec_bone_follower_cache, real_light_cache, hms_batch):
    # type: (Any, Any, str, str, float, _MeshCache, _LightBudget, _MeshCache, dict, Any, HmsBatch) -> bool
    """
    Imports ONE area json file. Assumes `resources_root` is already a
    valid directory and bundled_data/ is already confirmed available --
    both validated once by the caller (ImportAREA.execute(), which
    validates for the whole batch up front) rather than repeated here
    per file. A multi-hundred-file batch import with a missing
    bundled_data/ folder should fail once, immediately, with one clear
    message -- not fail (and report) separately for every single file.

    Similarly, job_results setup/reset is the caller's responsibility
    (once per batch), not this function's -- calling this repeatedly
    for a multi-file import must accumulate into the same job_results,
    not wipe it before every file. `mesh_cache` and `light_budget` are
    likewise shared across a whole batch (created once by the caller),
    not per file -- a source .gr2 reused across many area json files in
    the same zone (a common trim piece, say) benefits from the same
    dedup the cache already provides within a single file, and the
    >100-lights safeguard only means anything if it's tracking the
    running total across the whole batch (see _LightBudget's own note
    on why a per-file-only check doesn't catch the actual failure mode).

    `fxspec_mesh_cache` and `fxspec_bone_follower_cache` are `.fxp`
    elements' own dedicated equivalents -- likewise shared across the
    whole batch (a reused vfx model, e.g. the same banner prop across
    many rooms in one zone, benefits from the same dedup) -- see
    _process_fxspec_element's own docstring for why they're kept
    separate from this function's own `mesh_cache` rather than reusing
    it directly.

    `real_light_cache` (ops/import_room_lights.py's _RealLightCache) is
    likewise shared across the whole batch -- a reused light preset
    (the same torch, say) is exactly as likely to repeat across files
    in one zone as within a single one, same reasoning as mesh_cache.
    Only its area-name label (used for freshly-built data-block names,
    not as part of its cache key) is updated per file, via
    set_area_name() below.

    `hms_batch` (ops/import_hms.py's HmsBatch) is likewise shared across
    the whole batch: it carries the Generate Terrain / Enable DYD Scatter
    switches, the area index and the parsed area/room .dat caches that every
    `.hms` element in every file of the batch reuses. Its start/finish
    bookkeeping belongs to the caller (execute()).

    Returns True on success, False on failure (mirrors
    import_gr2.py/import_cha.py's own load() return convention).
    """
    start_time = time.time()

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        operator.report({'ERROR'}, "Couldn't read %s: %s" % (filepath, exc))
        return False

    elements = area_types.parse_area_json(raw_data)
    if not elements:
        operator.report({'WARNING'}, "No recognizable elements found in %s." % filepath)
        return False

    transforms, transform_warnings = area_types.compute_final_transforms(elements, scale_factor=scale_factor)

    area_name = Path(filepath).stem

    # Every collection here -- including the root one -- is created
    # lazily, only the first time something actually needs to be linked
    # into it. Confirmed real complaint: previously root_collection/
    # objects_collection were created unconditionally even for a file
    # that produces zero objects (e.g. a pure-logic file with no
    # .gr2/.spn_p content at all), leaving a pointless empty Collection
    # pair behind every time.
    #
    # "Organized Collections" (area_organized_collections) splits
    # non-Light/non-Heightmap objects into three top-level categories --
    # General (direct .gr2/.mag/.spt content), SPN/DYN (.spn_p ->
    # .dyn-expanded content), and FX (.fxp -> .fxspec-expanded content)
    # -- each its own parent Collection with "... Objects"/"... Empties"
    # children, so e.g. every FX-origin object in an area is selectable/
    # hideable as one group rather than mixed in among everything else
    # by ob.type alone. Lights and Heightmaps are deliberately NOT
    # folded into this scheme -- both are always direct elements in the
    # current scope (never reached via .spn_p or .fxp), so an "FX
    # Lights" or "SPN/DYN Heightmaps" collection could never actually
    # hold anything; they stay exactly where they've always been.
    #
    # Off, this reproduces the add-on's original flat behaviour -- a
    # plain "<Area Name> - Objects"/"<Area Name> - Empties" split, no
    # per-category distinction -- with SPN/DYN and FX content simply
    # mixed in among "general" content by ob.type, same as before this
    # feature existed at all.
    _collections = {}  # name -> Collection, created lazily on first use

    def _ensure_collection(name, parent):
        # type: (str, bpy.types.Collection) -> bpy.types.Collection
        col = _collections.get(name)
        if col is None:
            col = bpy.data.collections.new(name)
            parent.children.link(col)
            _collections[name] = col
        return col

    def _ensure_root_collection():
        return _ensure_collection(area_name, context.scene.collection)

    def _ensure_lights_collection():
        return _ensure_collection("%s - Lights" % area_name, _ensure_root_collection())

    def _ensure_heightmaps_collection():
        return _ensure_collection("%s - Heightmaps" % area_name, _ensure_root_collection())

    _CATEGORY_LABELS = {"general": "General", "spn_dyn": "SPN/DYN", "fx": "FX"}

    def _ensure_leaf_collection(category, is_empty):
        # type: (str, bool) -> bpy.types.Collection
        """
        Resolves the target collection for a non-Light, non-Heightmap
        object, based on its originating element's category (`category`,
        one of "general"/"spn_dyn"/"fx" -- see _element_category()) and
        whether the object itself is an Empty. With Organized Collections
        off, reproduces this add-on's original flat behaviour -- a plain
        "<Area Name> - Objects" and "<Area Name> - Empties" split, no
        per-category distinction -- unconditionally now; this used to be
        gated behind its own two toggles (Separate Objects/Empties
        Collection), removed as redundant once Organized Collections
        covers the "I want this split up" case properly -- both defaulted
        to True anyway, so this is the effective behaviour either way,
        just without the now-pointless option to turn it off.
        """
        root = _ensure_root_collection()

        if not operator.area_organized_collections:
            if is_empty:
                return _ensure_collection("%s - Empties" % area_name, root)
            return _ensure_collection("%s - Objects" % area_name, root)

        label = _CATEGORY_LABELS[category]
        category_parent = _ensure_collection("%s - %s" % (area_name, label), root)
        leaf_name = "%s - %s %s" % (area_name, label, "Empties" if is_empty else "Objects")
        return _ensure_collection(leaf_name, category_parent)

    def _element_category(element):
        # type: (area_types.AreaElement) -> str
        """
        "general"/"spn_dyn"/"fx" per _ensure_leaf_collection above.
        Deliberately doesn't handle ".hms" -- heightmap routing happens
        earlier, unconditionally, before this is ever consulted (see
        the per-object loop below).
        """
        name = element.asset_name.lower()
        if name.endswith(".fxp"):
            return "fx"
        if name.endswith(".spn_p"):
            return "spn_dyn"
        return "general"

    light_cache = _LightCache(area_name, scale_factor)
    real_light_cache.set_area_name(area_name)

    skip_counts = Counter()
    crashed_elements = []
    total_objects = []
    fxspec_warnings = []
    light_warnings = []

    for element in elements:
        transform = transforms.get(element.id)
        if transform is None:
            continue

        element_category = _element_category(element)

        # One malformed/unexpected element (e.g. a resolved .gr2 the
        # underlying importer chokes on) shouldn't take down an import
        # of several hundred/thousand other, unrelated elements --
        # confirmed real failure mode. Mirrors the old source's own
        # "the .gr2 Importer addon CRASHED ... despite that, keep
        # importing the rest" handling.
        try:
            objects = _process_area_element(
                operator, context, resources_root, mesh_cache, light_cache, element, transform, scale_factor,
                skip_counts, fxspec_mesh_cache, fxspec_bone_follower_cache, fxspec_warnings,
                real_light_cache, light_warnings, hms_batch, area_name,
            )
        except Exception as exc:
            crashed_elements.append("%s (%s): %s" % (element.id, element.asset_name, exc))
            continue

        for ob in objects:
            # import_gr2.load() links freshly-created objects into
            # whatever collection is active in the view layer at import
            # time -- move them to this area's proper collection instead,
            # same pattern as import_cha.py's assemble_collections().
            #
            # CONFIRMED BUG (fixed here): the previous version guarded
            # the link() call with "if not ob.users_collection" -- i.e.
            # "does this object have ANY collection membership at all",
            # rather than "is it specifically in [target] collection".
            # A freshly-imported object arrives already linked to
            # context.collection (from import_gr2.load()'s own default
            # linking), so that guard was always False for it -- meaning
            # it never got linked to its proper collection at all, and the
            # very next loop then unconditionally unlinked it from
            # context.collection anyway, leaving it linked to NOTHING.
            # Cache-instantiated repeat objects (which start completely
            # unlinked, unlike a fresh import) accidentally avoided this,
            # which is exactly why only ".001"+ duplicates were ever
            # visible and every "first" instance of a given mesh
            # silently vanished.
            #
            # Heightmap ('.hms') objects are routed by ELEMENT, not by
            # ob.type -- unlike Empties/Lights, a heightmap Object's own
            # .type is 'MESH' (same as a regular imported prop), so
            # there's no per-object signal to key off; this element
            # produced terrain, so every object it returned is terrain.
            #
            # Everything else routes through _ensure_leaf_collection(),
            # keyed on this element's own category (general/spn_dyn/fx --
            # see _element_category) plus whether the object itself is
            # an Empty -- see that function's own docstring for how it
            # collapses back to the add-on's previous flat behaviour
            # when Organized Collections is off. Lights always go to
            # their own collection regardless of category -- see the
            # collection-helpers' own comment on why Lights/Heightmaps
            # never participate in the general/spn_dyn/fx split at all.
            if element.asset_name.lower().endswith(".hms"):
                target_collection = _ensure_heightmaps_collection()
            elif ob.type == 'LIGHT':
                target_collection = _ensure_lights_collection()
            else:
                target_collection = _ensure_leaf_collection(element_category, ob.type == 'EMPTY')
            # ob.users_collection scans EVERY collection in the file on each access, so
            # it is read once here (not twice), and not at all for an object with no
            # users: being in a collection always counts as a user, so users == 0
            # means "in no collection" (that is every repeat instance a mesh cache
            # hands out -- only a fresh import arrives already linked).
            current_collections = list(ob.users_collection) if ob.users else []
            if target_collection not in current_collections:
                target_collection.objects.link(ob)
            for existing in current_collections:
                if existing is not target_collection:
                    existing.objects.unlink(ob)
        total_objects.extend(objects)

    # Registered with the batch-wide light_budget (shared across every
    # file in this execute() call), not checked against a per-file
    # threshold here -- see _LightBudget's own note on why a per-file
    # check doesn't catch the actual failure mode this exists for.
    light_budget.register(context, _collections.get("%s - Lights" % area_name))

    root_collection = _collections.get(area_name)
    if operator.area_hide_collections_after_import and root_collection is not None:
        # Excluding the ROOT collection is enough -- Blender's view-layer
        # exclusion cascades to every child collection (General/SPN-DYN/
        # FX/Lights/Heightmaps, and each of THEIR own children) automatically,
        # same reasoning confirmed already for the light-count safeguard,
        # just applied to the whole area here instead of only its Lights
        # collection. No per-file threshold or cumulative state needed
        # (unlike _LightBudget) -- this is a plain per-file toggle, not
        # something that only matters once a running total crosses a
        # threshold, so it's applied directly here rather than through
        # any shared batch-wide object.
        context.view_layer.update()
        layer_collection = _find_layer_collection(context.view_layer.layer_collection, root_collection)
        if layer_collection is not None:
            layer_collection.exclude = True
        else:
            print(
                "Area Assembler [%s]: couldn't find \"%s\"'s LayerCollection to "
                "exclude it -- left visible. Please report this if it keeps happening."
                % (os.path.basename(filepath), root_collection.name)
            )

    job_results['objs_names'].extend(ob.name for ob in total_objects)

    file_label = os.path.basename(filepath)
    elapsed_time = time.time() - start_time

    if transform_warnings:
        print("Area Assembler [%s]: %d transform warning(s):" % (file_label, len(transform_warnings)))
        for w in transform_warnings[:20]:
            print("  " + w)

    if fxspec_warnings:
        # Only the "actionable" categories land here now (a missing/
        # empty .gr2, a malformed attach chain, or a parse failure) --
        # see _process_fxspec_element's own FXSPEC_ACTIONABLE_CATEGORIES.
        # Every category, actionable or not, is ALSO already counted
        # into skip_counts below under its own stable label -- this is
        # just the extra per-entry/per-file detail for the categories
        # actually worth digging into.
        print("Area Assembler [%s]: %d .fxspec warning(s) needing attention:" % (file_label, len(fxspec_warnings)))
        for w in fxspec_warnings[:20]:
            print("  " + w)
        if len(fxspec_warnings) > 20:
            print("  ... and %d more (see full console output)" % (len(fxspec_warnings) - 20))

    if light_warnings:
        # Same reasoning as fxspec_warnings above -- a texture that
        # couldn't be resolved/loaded, or a real light build that fell
        # back to a placeholder, are both worth digging into
        # specifically; every category is also already counted into
        # skip_counts below under its own stable label.
        print("Area Assembler [%s]: %d real-light warning(s) needing attention:" % (file_label, len(light_warnings)))
        for w in light_warnings[:20]:
            print("  " + w)
        if len(light_warnings) > 20:
            print("  ... and %d more (see full console output)" % (len(light_warnings) - 20))

    if skip_counts:
        print("Area Assembler [%s]: elements skipped, by reason:" % file_label)
        for reason, count in skip_counts.most_common():
            print("  %5d  %s" % (count, reason))

    if crashed_elements:
        print("Area Assembler [%s]: %d element(s) raised an unexpected error and were skipped:" % (file_label, len(crashed_elements)))
        for line in crashed_elements[:20]:
            print("  " + line)
        if len(crashed_elements) > 20:
            print("  ... and %d more (see full console output)" % (len(crashed_elements) - 20))

    if skip_counts or crashed_elements:
        parts = []
        if skip_counts:
            parts.append("%d element(s) skipped across %d reason(s)" % (sum(skip_counts.values()), len(skip_counts)))
        if crashed_elements:
            parts.append("%d element(s) raised an error" % len(crashed_elements))
        message = "[%s] " % file_label + "; ".join(parts) + " -- see console for details."

        # Same tiering as process_materials.py's own _summarize_and_report()
        # -- ERROR if something actually crashed, WARNING if any skip
        # reason isn't tagged "(expected)" (a genuinely deliberate,
        # by-design skip -- a disabled toggle, Phase 1 scope, no Armature
        # in area context, etc.), INFO if every single skip reason is
        # expected and nothing crashed. Confirmed real problem with the
        # previous unconditional WARNING here: a totally ordinary area
        # full of e.g. Skip Game Engine Objects exclusions and
        # not-yet-built extensions read as if something had gone wrong
        # on every single import.
        needs_attention = any("(expected)" not in reason for reason in skip_counts)
        if crashed_elements:
            operator.report({'ERROR'}, message)
        elif needs_attention:
            operator.report({'WARNING'}, message)
        else:
            operator.report({'INFO'}, message)

    # Matches the old importer's own "FILE:/OBJS:/TIME:" console
    # convention (and import_gr2.py's own per-file timing) -- confirmed
    # missed, added back.
    print("FILE: %s" % filepath)
    print("OBJS: %d" % len(total_objects))
    print("TIME: %s" % _format_duration(elapsed_time))
    print()

    return bool(total_objects)


class ImportAREA(bpy.types.Operator):
    """
    Import a SWTOR area .json export (Area Assembler).

    Places every recognized element (.gr2/.mag/.spt meshes, resolved
    .spn_p placeables and their .dyn-expanded visuals) at its correct
    final position/rotation/scale, reusing mesh data for repeated
    source files rather than reimporting them each time.

    Supports selecting multiple .json files at once in the file browser
    (each becomes its own named Collection, same as importing them one
    at a time) -- with one deliberate difference from this add-on's
    other multi-file importers (e.g. ImportGR2): a file that fails
    doesn't abort the rest of the batch. Area files are commonly split
    across dozens/hundreds of .json files for one zone, and losing the
    whole batch over one bad file would be a worse outcome than
    reporting that file's failure and importing everything else.
    """
    bl_idname = "import_scene.swtor_area"
    bl_label = "Import SWTOR Area (.json)"
    bl_description = "Import a SWTOR area .json export"
    bl_options = {'UNDO'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    files: bpy.props.CollectionProperty(
        name="File Path",
        description="File path(s) used for importing the area .json file(s)",
        type=bpy.types.OperatorFileListElement,
    )
    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    area_organized_collections: bpy.props.BoolProperty(
        name="Organized Collections",
        description=(
            "Splits imported objects into three top-level Collections -- "
            "\"<Area Name> - General\" (direct meshes), \"<Area Name> - SPN/DYN\" "
            "(resolved .spn_p placeables), and \"<Area Name> - FX\" (resolved "
            ".fxspec visual effects) -- each with its own \"... Objects\"/"
            "\"... Empties\" child Collections, so each category is selectable/"
            "hideable as a group. Off, imports into a plain \"<Area Name> - "
            "Objects\"/\"<Area Name> - Empties\" split instead, no per-category "
            "distinction. Lights and Heightmaps are unaffected either way -- "
            "they always get their own Collection"
        ),
        default=True,
    )

    area_process_materials: bpy.props.BoolProperty(
        name="Process Materials",
        description=(
            "Looks up every imported object's materials by name against real "
            ".mat files in the Resources Directory and builds native SWTOR "
            "shaders for them, once, after every selected file has finished "
            "importing (not per file -- a material shared across many area "
            "files only gets built once either way, but running the pass "
            "once at the end keeps that dedup working across the whole batch "
            "rather than resetting per file)"
        ),
        default=True,
    )

    area_create_lights: bpy.props.BoolProperty(
        name="Create Scene Lights",
        description=(
            "Places a real SWTOR-style light (color/intensity/shadow/falloff & "
            "illumination cookie textures, built from the bundled lighting "
            "template) for every \".lit\"-tagged element that carries real "
            "per-light JSON data, falling back to a generic placeholder Point "
            "light if that data is missing or the real light fails to build "
            "(see console). Lights beyond a per-file threshold get their "
            "Collection auto-excluded from the view layer to protect viewport "
            "performance"
        ),
        default=True,
    )

    area_skip_game_engine_objects: bpy.props.BoolProperty(
        name="Skip Game Engine Objects",
        description=(
            "Don't import non-visual, tooling-only content: design blockout "
            "(DBO) objects such as blockers and markers (identified by their "
            "own filename starting with \"dbo\"), plus objects using known "
            "non-visual game-engine materials such as occluders, portals, and "
            "collision (identified by material name, since these often can't "
            "be recognized by filename alone). Not related to any object "
            "that's genuinely named/labeled \"Utility\" in-game -- this only "
            "targets engine-internal tooling content"
        ),
        default=True,
    )

    area_name_as_filename: bpy.props.BoolProperty(
        name="Name Objects As Filenames",
        description=(
            "Names each imported object after its .gr2 file instead of the model's internal "
            "'art name' (the mesh data always keeps the art name). Every placed copy of a file "
            "then carries the same name -- without this, the first copy of a file is named by its "
            "art name (\"LOD[0]\", \"trunk002\", \"Object011\") and every later copy by its filename.\n\n"
            "Files holding several meshes (LOD levels, plaque + bust...) only get the main mesh "
            "renamed: the LOD0 mesh if the file has one, otherwise the first.\n\n"
            "Starts from this add-on's \"Name Imported Objects As Filenames\" preference each time "
            "the dialog opens; change it here to override that for this import only"
        ),
        default=False,
    )

    area_merge_multi_mesh_objects: bpy.props.BoolProperty(
        name="Merge Multi-Mesh Objects",
        description=(
            "Joins every sub-object a single .gr2 file produces into one "
            "combined object, renamed to that .gr2's own filename, instead of "
            "leaving them as separate objects (often with meaningless default "
            "names like \"Object17\"/\"NGon03\", since a .gr2's own internal "
            "sub-part names don't always reflect anything meaningful)"
        ),
        default=False,
    )

    area_generate_terrain: bpy.props.BoolProperty(
        name="Generate Terrain",
        description=(
            "Builds the native heightmap terrain (geometry and layered materials) for every room file that "
            "lists a \".hms\" element, straight from the game's own .dat files under "
            "<Resources Folder>/world/areas -- no other files or conversion needed. The first import reads "
            "every area.dat once to learn which area each heightmap belongs to (a few seconds) and "
            "remembers the result. Off, heightmap elements are skipped"
        ),
        default=True,
    )

    area_enable_dyd_scatter: bpy.props.BoolProperty(
        name="Enable DYD Scatter",
        description=(
            "Adds the Geometry Nodes grass/rock/foliage scatter (the game's \"dynamic detail\") on top of "
            "each terrain tile. Needs Generate Terrain. Turn off to import just the terrain, e.g. to keep "
            "a large area light in the viewport"
        ),
        default=True,
    )

    area_dyd_max_instances: bpy.props.IntProperty(
        name="Max DYD Instances Per Tile",
        description=(
            "Safety limit: if a tile's scatter is estimated to put more than this many instances in the "
            "viewport (at its Viewport Density), that tile's Viewport Density is lowered just enough to fit "
            "(a sparser viewport preview) instead of risking a hang or crash. Render density is never "
            "changed. 0 turns the limit off"
        ),
        default=DEFAULT_MAX_VIEWPORT_INSTANCES,
        min=0,
    )

    area_hide_collections_after_import: bpy.props.BoolProperty(
        name="Hide Collections After Importing",
        description=(
            "Excludes each imported area's root Collection from the view layer "
            "once it's finished importing, so its contents (and everything "
            "nested under it -- Objects/Empties/Lights/Heightmaps) don't slow "
            "down the Outliner or viewport on a large batch import. Re-enable "
            "a hidden area's Collection in the Outliner whenever you want to "
            "see it again"
        ),
        default=False,
    )

    area_open_console: bpy.props.BoolProperty(
        name="Open System Console",
        description=(
            "Opens Blender's system console (Windows only) before starting, so "
            "warnings/progress are visible without remembering to open it "
            "manually first. Blender only exposes a TOGGLE for this, not a way "
            "to check whether it's already open -- if it happens to already be "
            "open, this will close it instead. Never auto-closes at the end, "
            "since the point is being able to review the summary afterward"
        ),
        default=True,
    )

    # Always True for this operator -- Area Assembler bakes its own
    # final transforms (position/rotation/scale, scale_factor included)
    # onto every object itself, so its internal .gr2 imports need raw,
    # unscaled geometry rather than each individually re-applying the
    # add-on's own scale settings on top. Unlike ImportCHA (which always
    # leaves this False, sourcing scale from Preferences instead), this
    # is a genuine, deliberate difference between the two importers, not
    # an oversight -- confirmed in design discussion.
    enforce_neutral_settings: bpy.props.BoolProperty(options={'HIDDEN'}, default=True)

    # Not exposed in the panel: import_gr2.py's load() reads this
    # attribute unconditionally before it even checks bl_idname, same
    # reasoning as ImportCHA's own copy of this property.
    job_results_rich: bpy.props.BoolProperty(options={'HIDDEN'}, default=False)

    # Matches ImportGR2's own property of the same name/purpose: lets
    # external code trigger several import_scene.swtor_area calls in a
    # row (not just this operator's own multi-file selection) without
    # each call wiping job_results from the previous one.
    job_results_accumulate: bpy.props.BoolProperty(options={'HIDDEN'}, default=False)

    def invoke(self, context, event):
        # The dialog opens with this add-on's own preference for naming objects
        # after files (Preferences > .gr2 import), like the .gr2 importer's own
        # dialog does; it can be overridden for the run in the dialog itself.
        prefs = context.preferences.addons["swtor_io_tools"].preferences
        self.area_name_as_filename = prefs.gr2_name_as_filename
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.area_open_console:
            import platform
            if platform.system() == 'Windows':
                bpy.ops.wm.console_toggle()

        prefs = bpy.context.preferences.addons["swtor_io_tools"].preferences
        resources_root = prefs.swtor_resources_dir

        if not resources_root or not Path(resources_root).is_dir():
            self.report({'ERROR'}, "Set a valid Resources Folder in this add-on's preferences first.")
            return {'CANCELLED'}

        # Validated once for the whole batch -- see load()'s own
        # docstring for why this isn't repeated per file.
        try:
            area_types.ensure_bundled_data_available()
        except area_types.BundledDataError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        scale_factor = prefs.gr2_scale_factor if prefs.gr2_scale_object else 1.0

        paths = [
            os.path.join(self.directory, file.name)
            for file in self.files
            if file.name.lower().endswith(self.filename_ext)
        ]
        if not paths:
            paths.append(self.filepath)

        # Clear filebrowser-related properties now that they've been
        # read, same reasoning as ImportGR2's own execute(): keeps them
        # from persisting if this breaks before finishing, which makes
        # debugging harder otherwise.
        self.files.clear()
        self.filepath = ""

        if not self.job_results_accumulate:
            job_results['objs_names'] = []
            job_results['files_objs_names'] = {}
        job_results['job_origin'] = self.bl_idname

        mesh_cache = _MeshCache(scale_factor=scale_factor)
        light_budget = _LightBudget(LIGHT_COUNT_AUTO_EXCLUDE_THRESHOLD)

        # `.fxp` elements' own dedicated equivalents -- deliberately
        # scale_factor=1.0 (NOT the real scale_factor above) and kept
        # separate from this batch's main mesh_cache -- see
        # _process_fxspec_element's own docstring for why sharing the
        # main, real-scale_factor cache would double-bake scale_factor.
        # Shared across the WHOLE BATCH, same lifetime as mesh_cache --
        # a reused vfx model (e.g. the same banner prop across many
        # rooms in one zone) benefits from the same cross-file dedup.
        fxspec_mesh_cache = _MeshCache(scale_factor=1.0)
        fxspec_bone_follower_cache = {}

        # Deferred import -- see this module's own top-of-file note.
        from .import_room_lights import _RealLightCache
        real_light_cache = _RealLightCache(resources_root)

        # Native .hms terrain: settings + area index + .dat caches shared by
        # every file in the batch. Its setup is lazy (nothing happens unless
        # a file actually has an .hms element) and finish() always runs, in
        # the finally below, after the materials pass.
        hms_batch = HmsBatch(
            resources_root, scale_factor,
            generate_terrain=self.area_generate_terrain,
            enable_scatter=self.area_enable_dyd_scatter,
            max_viewport_instances=self.area_dyd_max_instances,
        )

        try:
            batch_start_time = time.time()
            succeeded = []
            failed = []
            for path in paths:
                if load(self, context, path, resources_root, scale_factor, mesh_cache, light_budget,
                        fxspec_mesh_cache, fxspec_bone_follower_cache, real_light_cache, hms_batch):
                    succeeded.append(path)
                else:
                    failed.append(path)
            light_budget.flush(context)
            batch_elapsed_time = time.time() - batch_start_time

            # Every load() has finished, so every instantiate() of the batch has
            # happened: the cached meshes no longer need protecting from purges
            # (see _MeshCache.store()). Done before the final purge below so
            # that purge can still reclaim the meshes of excluded objects.
            mesh_cache.release()
            fxspec_mesh_cache.release()

            real_light_cache.release_template()

            if real_light_cache.needs_final_purge():
                # The one safe point to reclaim everything real-light
                # building left unpurged during the loop above (duplicate
                # shared node groups, superseded default template textures)
                # -- see light_templates.build_light_from_template()'s own
                # docstring for the confirmed real bug that made purging
                # mid-loop unsafe (_MeshCache's cached-but-momentarily-
                # userless mesh data getting deleted out from under it).
                # Every load() call above has already finished by this
                # point, so every _MeshCache instantiate() across the whole
                # batch has already happened too -- same timing already
                # proven safe for the materials pass just below, which also
                # only ever runs after this same loop.
                bpy.data.orphans_purge(do_recursive=True)

            if len(paths) > 1:
                print("Area Assembler: batch of %d file(s) complete." % len(paths))
                print("TOTAL TIME: %s" % _format_duration(batch_elapsed_time))
                print()

            if succeeded and self.area_process_materials:
                materials_start_time = time.time()
                results = apply_materials_by_name_to_gr2_objects(job_results['objs_names'])
                materials_elapsed_time = time.time() - materials_start_time
                print("Area Assembler: materials processing complete.")
                print("MATERIALS TIME: %s" % _format_duration(materials_elapsed_time))
                print()
                _summarize_and_report(self, results)
        finally:
            # Releases the fake users that kept the terrain datablocks alive
            # through the purges above and the materials pass, re-excludes the
            # shared DYD assets collection, and prints the terrain summary.
            # Runs even if the import raised part-way through.
            mesh_cache.release()           # idempotent -- covers the raised-part-way case
            fxspec_mesh_cache.release()
            try:
                light_budget.flush(context)    # no-op after the normal flush above
            except Exception as exc:  # noqa: BLE001 -- never mask the original error
                print("Area Assembler: couldn't exclude the Lights collections: %s" % exc)
            real_light_cache.release_template()   # no-op after the normal release above
            hms_batch.finish(context)
            for level, message in hms_batch.notices:
                self.report({level}, message)

        bpy.context.scene.swtor_io_last_job = json.dumps(job_results)

        if failed:
            self.report(
                {'WARNING'},
                "%d of %d file(s) failed to import (%d succeeded) -- see console for per-file details."
                % (len(failed), len(paths), len(succeeded)),
            )

        if not succeeded:
            return {'CANCELLED'}
        return {'FINISHED'}
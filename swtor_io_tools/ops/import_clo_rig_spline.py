# <pep8 compliant>

"""
Spline IK / Bendy Bone layer for "Build Rig" mode -- applied on top of a finished
armature from import_clo_rig.py's build(). Deliberately kept separate from that module:
this only needs to decide which unbranched bone runs exist and lay curves/constraints
over them, none of the Dijkstra/endParticle/pruning machinery that makes up most of
import_clo_rig.py's complexity -- a genuinely separate concern.

Key design decision: chain selection ALWAYS uses the weighted-bone-only hierarchy, the
same one skip_unweighted_bones=True would produce -- regardless of what that checkbox is
actually set to for this particular build. If it's off, the built armature still contains
the unweighted stub bones (plain FK, untouched), but they're never considered part of a
spline/bendy chain and never split one. This module re-derives that hierarchy itself
(via _find_pins/_find_unweighted_bones/_seed_pins/_spanning_tree_from_pins/_effective_parent,
reused as-is from import_clo_rig.py) rather than trusting build()'s own effective_parent_of,
because that dict reflects whatever the checkbox actually was -- with it off, pruned bones
stay in the graph during tree-building too (not just added back afterward), which can
genuinely change which bone is whose primary/effective parent, not just add extra leaves.
Verified against all three of this build's sample files: pruning removes 10/20 bones on
the twi'lek head, 4/32 on the cape, 0/56 on the shawl, and in every case the survivors form
clean unbranched root-to-tip runs -- see chat for the exact chains. A fork surviving
pruning hasn't been observed on any real file yet, but the branch-splitting logic below is
still there as a documented, deliberate fallback rather than an unhandled case.

Branch handling (confirmed against the twi'lek lekku's pre-pruning shape, which *did* fork,
even though pruning removes the fork on that specific file): every unbranched run --
including the trunk above a fork -- becomes its own independent curve + Spline IK
constraint. None of them share continuity across a fork; a branch's curve still starts
positionally at the fork bone (so there's no visual gap) but is mechanically unrelated to
the trunk's own curve/constraint.
"""

from typing import TYPE_CHECKING, Dict, List, Optional

from mathutils import Matrix

from .import_clo import _find_sibling_mesh
from .import_clo_rig import (
    armature_baked_scale,
    _effective_parent,
    _find_pins,
    _find_unweighted_bones,
    _seed_pins,
    _spanning_tree_from_pins,
)

if TYPE_CHECKING:
    from bpy.types import Context, Object, Operator
    from ..types.clo import Cloth


BBONE_SEGMENTS = 8  # No UI exposed for this yet -- matches the "no new UI beyond the two
                     # existing checkboxes" v1 scope; easy to promote to an operator
                     # property later if a file needs a different value.
BBONE_WIDTH = 0.0005  # Static rather than proportional to bone length -- tried
                       # proportional first, but varying width per segment read as
                       # inconsistent/messy across a chain; a flat value looks cleaner.


def _spline_effective_parent_of(cloth, sibling_mesh):
    # type: (Cloth, Optional['bpy.types.Object']) -> Dict[int, Optional[int]]
    """
    Re-derives the SAME weighted-bone-only hierarchy import_clo_rig.py's build() produces
    when skip_unweighted_bones=True -- independent of whatever that checkbox is actually
    set to for this build. See module docstring for why this can't just reuse build()'s
    own effective_parent_of: with the checkbox off, unweighted bones stay IN the pruning
    graph during tree-building, which can change more than just "which bones show up as
    extra leaves."
    """
    pins = _find_pins(cloth)
    unweighted = _find_unweighted_bones(cloth, sibling_mesh, pins) if sibling_mesh is not None else set()
    tree_parent_of = _spanning_tree_from_pins(cloth, _seed_pins(cloth, pins, unweighted), unweighted)
    return {
        bone.index: _effective_parent(bone.index, tree_parent_of, unweighted)
        for bone in cloth.bones
        if bone.index not in unweighted
    }


def _extract_chains(effective_parent_of):
    # type: (Dict[int, Optional[int]]) -> List[List[int]]
    """
    Splits the hierarchy into unbranched root-to-tip runs. At a fork, the current run ends
    AT the fork bone (inclusive) and each child starts a brand new run beginning at itself
    -- not continuing the parent run, and not starting from the fork bone a second time.
    Positional continuity across the fork comes from the fork bone's own position being
    read again as the first control point of each child run's curve, in _build_curve --
    two independent things (which run a bone's own Spline IK constraint belongs to, vs.
    which bone positions a curve's control points are read from) that happen to overlap
    by one bone at every fork.
    """
    children = {}  # type: Dict[int, List[int]]
    for index, parent_index in effective_parent_of.items():
        if parent_index is not None:
            children.setdefault(parent_index, []).append(index)

    roots = [index for index, parent_index in effective_parent_of.items() if parent_index is None]

    chains = []
    visited = set()  # guards against any accidental cycle; none expected -- this hierarchy
                      # is re-derived with the same tree-building functions build() already
                      # validated against every sample file.
    stack = list(roots)
    while stack:
        start = stack.pop()
        if start in visited:
            continue
        chain = [start]
        visited.add(start)
        node = start
        while True:
            kids = [c for c in children.get(node, []) if c not in visited]
            if len(kids) == 1:
                node = kids[0]
                visited.add(node)
                chain.append(node)
            else:
                stack.extend(kids)
                break
        chains.append(chain)
    return chains


def _get_or_create_collection(context, parent_collection, name):
    # type: (Context, 'bpy.types.Collection', str) -> 'bpy.types.Collection'
    """
    Finds NAME under PARENT_COLLECTION if it's already there (e.g. a second import into
    the same armature) rather than creating a duplicate -- doesn't solve re-import
    dedup on its own (existing curves/hooks inside it still won't be replaced, just
    joined by a new set), but at least keeps everything nested under ONE collection
    instead of piling up "Spline Controls.001", ".002", etc. each run.
    """
    import bpy

    existing = parent_collection.children.get(name)
    if existing is not None:
        return existing
    new_collection = bpy.data.collections.new(name)
    parent_collection.children.link(new_collection)
    return new_collection


def _spline_collections(context, armature_ob, add_curve_hooks):
    # type: (Context, Object, bool) -> tuple
    """
    Nests everything Spline IK creates under one "{armature} Spline Controls" collection,
    itself nested under wherever the armature already lives -- so an artist can collapse
    or hide the whole lot with one click instead of hunting through a flat object list.
    Files with many chains make this matter: the shawl produces 11 chains, which is 11
    curves and up to 22 hook empties alongside its armature.

    Curves and hooks get separate sub-collections rather than one shared bucket -- once a
    pose is dialled in, hooks are the more likely thing to want hidden away (clutter, risk
    of grabbing one by accident) while the curves stay useful as a visual reference of the
    current shape, so they're worth independent visibility toggles.
    """
    parent_collection = armature_ob.users_collection[0] if armature_ob.users_collection else context.collection
    controls_collection = _get_or_create_collection(context, parent_collection, f"{armature_ob.name} Spline Controls")
    curves_collection = _get_or_create_collection(context, controls_collection, "Curves")
    hooks_collection = _get_or_create_collection(context, controls_collection, "Hooks") if add_curve_hooks else None
    return curves_collection, hooks_collection


def _bone_parent(ob, armature_ob, bone_name):
    # type: ('bpy.types.Object', Object, str) -> None
    """
    Re-parents OB from a plain object-parent to a BONE parent, so it follows BONE_NAME's
    pose (posing/animating the underlying skeleton after Spline IK is applied) instead of
    staying static relative to the armature object's own transform.

    Bone-parenting in Blender attaches at the bone's TAIL with an offset baked into
    matrix_parent_inverse -- hand-deriving that tail-offset matrix directly was judged too
    risky to get right blind (the exact mistake that broke the curve's original object-
    parenting; see _build_curve). Sidestepped entirely here the same way _add_hook already
    places empties: capture OB's current (already-correct, just object-parented)
    matrix_world, switch to BONE parenting, then assign that SAME matrix_world back.
    Blender's own matrix_world setter re-solves matrix_basis against whatever the parent
    configuration now is -- so OB ends up in the exact same place it already was (no jump)
    while going forward it moves rigidly with bone_name's pose, offset by however this
    resolved. This only produces the intended rigid offset if OB's captured matrix_world
    was correct at REST pose (i.e. called at import time, before anyone has posed the
    armature) -- true for every caller here.
    """
    target_world_matrix = ob.matrix_world.copy()
    ob.parent = armature_ob
    ob.parent_type = 'BONE'
    ob.parent_bone = bone_name
    ob.matrix_world = target_world_matrix


def _build_curve(context, armature_ob, cloth, chain, collection):
    # type: (Context, Object, Cloth, List[int], 'bpy.types.Collection') -> 'bpy.types.Object'
    """
    One Bezier curve per chain, its control points read straight from the FINISHED
    armature's own bone positions (armature_ob.data.bones), not recomputed from the .clo
    rest-position math again -- confirmed: simpler, and can't drift out of sync with
    whatever the rig builder actually placed. N bones -> N+1 points (each bone's head, plus
    the last bone's tail), so the curve reaches all the way to the chain's tip.
    """
    import bpy

    bones = armature_ob.data.bones
    points = [bones[cloth.bones[index].name].head_local for index in chain]
    points.append(bones[cloth.bones[chain[-1]].name].tail_local)

    curve_name = f"{cloth.bones[chain[0]].name}_spline"
    curve_data = bpy.data.curves.new(curve_name, type='CURVE')
    curve_data.dimensions = '3D'
    spline = curve_data.splines.new('BEZIER')
    spline.bezier_points.add(len(points) - 1)
    for point, co in zip(spline.bezier_points, points):
        point.co = co
        point.handle_left_type = 'AUTO'
        point.handle_right_type = 'AUTO'

    curve_ob = bpy.data.objects.new(curve_name, curve_data)
    collection.objects.link(curve_ob)
    curve_ob.show_in_front = True

    # Parent to the armature so the curve travels with it. Deliberately NOT touching
    # matrix_parent_inverse here -- it defaults to identity on a freshly created object,
    # which is exactly what's needed: control points were read in the armature's own
    # LOCAL space (head_local/tail_local), so curve_ob.matrix_world needs to come out
    # equal to armature_ob.matrix_world for them to land in the right place. Setting
    # matrix_parent_inverse to armature_ob.matrix_world.inverted() (an earlier version of
    # this function did) CANCELS the parent's transform instead of inheriting it --
    # confirmed as the actual bug behind curves not lining up with the armature.
    curve_ob.parent = armature_ob

    # Confirmed important: without this, the curve stays static relative to the armature
    # OBJECT rather than following the real skeleton bone (or standalone placeholder) the
    # chain's root pin is actually parented to -- so posing/animating that bone afterward
    # (e.g. turning a head the lekku hang from) would leave the chain's target curve
    # pointing the original direction instead of tracking the turn. build() always parents
    # a chain's root pin bone to a real or placeholder bone (never leaves it parentless),
    # so this should always resolve -- the None branch is a defensive fallback, not an
    # expected path.
    root_bone = bones[cloth.bones[chain[0]].name]
    anchor_bone = root_bone.parent
    if anchor_bone is not None:
        _bone_parent(curve_ob, armature_ob, anchor_bone.name)

    return curve_ob


def _add_hook(context, armature_ob, curve_ob, point_index, name, collection):
    # type: (Context, Object, 'bpy.types.Object', int, str, 'bpy.types.Collection') -> None
    """
    One Empty, hooked to a single control point on CURVE_OB, so it can be grabbed in Object
    Mode instead of diving into the curve's own Edit Mode. The Hook modifier stores which
    point(s) it affects as a raw index captured right now -- it will NOT follow point_index
    if the curve's point count later changes (e.g. dissolving points by hand after import).
    That's an accepted v1 limitation, not an oversight: re-running Blender's own Hooks ->
    Assign to Hook after a manual dissolve is the fix, same as it would be for any hook set
    up by hand.
    """
    import bpy

    point = curve_ob.data.splines[0].bezier_points[point_index]
    world_co = curve_ob.matrix_world @ point.co

    empty_ob = bpy.data.objects.new(name, None)
    empty_ob.empty_display_type = 'PLAIN_AXES'
    empty_ob.empty_display_size = 0.005
    collection.objects.link(empty_ob)
    empty_ob.show_in_front = True

    # Same parenting pattern as the curve -- matrix_parent_inverse left at its identity
    # default, not set to armature_ob.matrix_world.inverted() (that mistake is what broke
    # the curve; see _build_curve). Setting matrix_world directly below re-solves
    # matrix_basis against whatever matrix_parent_inverse currently is, so this would have
    # self-corrected here regardless -- fixed for consistency and to avoid the same
    # confusion creeping back in on a future edit.
    empty_ob.parent = armature_ob
    empty_ob.matrix_world = Matrix.Translation(world_co)

    # Bone-parent to whatever the curve itself is anchored to (same rigid-follow reasoning
    # as _build_curve) so the hook stays correctly placed relative to the chain as the
    # underlying skeleton is posed, rather than the curve moving with the bone while the
    # hook stays behind. Reads it straight off curve_ob rather than re-deriving it, so this
    # can't disagree with whatever the curve actually ended up parented to -- including the
    # defensive fallback case where curve_ob had no resolvable anchor and stayed plain
    # object-parented.
    if curve_ob.parent_type == 'BONE' and curve_ob.parent_bone:
        _bone_parent(empty_ob, armature_ob, curve_ob.parent_bone)

    hook = curve_ob.modifiers.new(name=f"Hook_{name}", type='HOOK')
    hook.object = empty_ob
    # A Bezier point is 3 vertices to a Hook modifier's index space -- handle_left, co,
    # handle_right, in that order, repeated per point -- NOT 1. Passing the raw point
    # index here (an earlier version of this function did) targets some arbitrary handle
    # instead of the point itself: confirmed as the cause of hooks either doing nothing
    # visible (grabbed an endpoint's otherwise-inert handle) or grabbing the wrong point
    # entirely (index N lands on floor(N/3)'s handle, nowhere near point N). All three
    # sub-vertices of the actual target point are hooked together so co and both handles
    # move as one rigid unit, matching what manually selecting the point and assigning
    # a hook does.
    base = point_index * 3
    hook.vertex_indices_set([base, base + 1, base + 2])
    # Bind at the empty's current (just-placed, matching) position so nothing jumps the
    # instant the modifier is added -- the same effect bpy.ops.object.hook_assign gives
    # when used interactively.
    hook.matrix_inverse = empty_ob.matrix_world.inverted()


def _apply_spline_ik(context, armature_ob, cloth, chain, add_curve_hooks, preserve_bone_length,
                      curves_collection, hooks_collection):
    # type: (Context, Object, Cloth, List[int], bool, bool, 'bpy.types.Collection', Optional['bpy.types.Collection']) -> None
    curve_ob = _build_curve(context, armature_ob, cloth, chain, curves_collection)
    tip_pb = armature_ob.pose.bones[cloth.bones[chain[-1]].name]
    constraint = tip_pb.constraints.new('SPLINE_IK')
    constraint.target = curve_ob
    constraint.chain_count = len(chain)
    # Cloth chains don't carry a meaningful curve "radius" (bezier point radius is left at
    # its 1.0 default above) -- off so bone width can't silently balloon at points that
    # happen to get a default radius other than 1.0 from future edits to _build_curve.
    constraint.use_curve_radius = False
    # Default 'FIT_CURVE' stretches every bone to exactly span the curve's current arc
    # length -- fine while the curve roughly matches the chain's rest length, but lets
    # dragging a control point far away visibly over-stretch the whole chain.
    # 'BONE_ORIGINAL' keeps each bone at its rest length instead, so the chain simply
    # won't reach a control point dragged too far rather than stretching to meet it.
    constraint.y_scale_mode = 'BONE_ORIGINAL' if preserve_bone_length else 'FIT_CURVE'

    if add_curve_hooks:
        last_point = len(curve_ob.data.splines[0].bezier_points) - 1
        _add_hook(context, armature_ob, curve_ob, 0, f"{curve_ob.name}_root_hook", hooks_collection)
        if last_point > 0:
            _add_hook(context, armature_ob, curve_ob, last_point, f"{curve_ob.name}_tip_hook", hooks_collection)


def _apply_bendy_bones(armature_ob, cloth, chain, use_spline_ik):
    # type: (Object, Cloth, List[int], bool) -> None
    """
    bbone_segments/handle-type are Bone (armature data) settings, not edit-bone-only --
    safe to set here with the armature back in its normal mode.

    With Spline IK also active, raising bbone_segments alone is enough: the constraint
    already drives each bone's matrix along the curve, and Blender's own bbone/Spline-IK
    interaction smooths the in-between segments from that -- no custom handles needed, and
    setting them anyway would just get overridden.

    Without Spline IK, TANGENT handles pointing at each bone's own neighbour in THIS chain
    are the fallback smoothing method (per the handoff's open question 4). Deliberately
    left at the default (no custom handle) at both ends of the chain rather than reaching
    into a neighbouring chain across a fork -- keeps each chain's bendy setup self-
    contained, matching the "no shared continuity across a fork" rule for Spline IK above.
    """
    bones = armature_ob.data.bones
    chain_bones = [bones[cloth.bones[index].name] for index in chain]

    # The chain's own anchor bone (real skeleton bone in merge mode, placeholder in
    # standalone mode) also renders using bbone_x/bbone_z once display_type is switched to
    # 'BBONE' for the whole armature -- but it's never one of chain_bones, so it was left
    # at Blender's flat 0.1 default and stood out as oversized right next to the chain.
    # Only WIDTH is touched here, not bbone_segments -- the anchor isn't meant to curve as
    # part of this chain, it just shouldn't look broken. This only reaches the ONE bone
    # immediately anchoring this chain -- in merge mode, on a large pre-existing character
    # skeleton, other bones further up that skeleton could still show the same oversized
    # default if they're ever visible near a bendy chain; fixing every bone in an external
    # rig this addon doesn't otherwise touch felt like too wide a blast radius for what's
    # actually been reported so far.
    width = BBONE_WIDTH * armature_baked_scale(armature_ob)  # Bone data units: they scale with the bones
    anchor_bone = bones[cloth.bones[chain[0]].name].parent
    if anchor_bone is not None:
        anchor_bone.bbone_x = width
        anchor_bone.bbone_z = width

    for bone in chain_bones:
        bone.bbone_segments = BBONE_SEGMENTS
        bone.bbone_x = width
        bone.bbone_z = width

    if use_spline_ik:
        return

    for position, bone in enumerate(chain_bones):
        if position > 0:
            bone.bbone_handle_type_start = 'TANGENT'
            bone.bbone_custom_handle_start = chain_bones[position - 1]
        if position < len(chain_bones) - 1:
            bone.bbone_handle_type_end = 'TANGENT'
            bone.bbone_custom_handle_end = chain_bones[position + 1]


def apply(operator, context, armature_ob, cloth, filepath):
    # type: (Operator, Context, Object, Cloth, str) -> None
    use_spline_ik = getattr(operator, "use_spline_ik", False)
    use_bendy_bones = getattr(operator, "use_bendy_bones", False)
    add_curve_hooks = use_spline_ik and getattr(operator, "add_curve_hooks", False)
    preserve_bone_length = use_spline_ik and getattr(operator, "preserve_bone_length", True)
    if not (use_spline_ik or use_bendy_bones):
        return

    sibling_mesh = _find_sibling_mesh(filepath)
    effective_parent_of = _spline_effective_parent_of(cloth, sibling_mesh)
    chains = [chain for chain in _extract_chains(effective_parent_of) if len(chain) >= 2]

    if not chains:
        operator.report(
            {'INFO'},
            "No chain had 2+ weighted bones -- nothing for Spline IK/Bendy Bones to apply to.",
        )
        return

    curves_collection, hooks_collection = (
        _spline_collections(context, armature_ob, add_curve_hooks) if use_spline_ik else (None, None)
    )

    if use_bendy_bones:
        # build() unconditionally sets display_type = 'STICK' for a freshly created
        # armature -- STICK never renders B-Bone curvature (mesh deformation is correct
        # either way, display_type only affects how bones are DRAWN), so without this,
        # Bendy Bones would look like it did nothing at all. Scoped narrowly to only fire
        # when Bendy Bones is actually requested, so plain FK / Spline-IK-only imports
        # keep whatever display_type build() already gave them.
        armature_ob.data.display_type = 'BBONE'

    for chain in chains:
        if use_spline_ik:
            _apply_spline_ik(
                context, armature_ob, cloth, chain, add_curve_hooks, preserve_bone_length,
                curves_collection, hooks_collection,
            )
        if use_bendy_bones:
            _apply_bendy_bones(armature_ob, cloth, chain, use_spline_ik)

    parts = []
    if use_spline_ik:
        parts.append(f"Spline IK on {len(chains)} chain(s)")
    if use_bendy_bones:
        parts.append(f"Bendy Bone segments on {len(chains)} chain(s)")
    operator.report({'INFO'}, "Applied " + " and ".join(parts) + ".")
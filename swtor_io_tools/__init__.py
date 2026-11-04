# <pep8 compliant>

bl_info = {
    "name": "SWTOR: Import/Export Tools",
    "author": "Crunch, Darth Atroxa, SWTOR Slicers, Claude",
    "version": (5, 2, 1),
    "blender": (4, 5, 0),
    "location": "File > Import-Export",
    "description": "Import SWTOR GR2, JBA, CLO Files, and Export SWTOR Compatible GR2 Files",
    "support": 'COMMUNITY',
    "category": "Import-Export",
}


import importlib
import os
import sys
from typing import List

import bpy

from bpy.app import version_string
from bpy.app.handlers import depsgraph_update_post
from bpy.props import FloatVectorProperty
from bpy.types import Context, KeyMap, Menu, PropertyGroup

from .addon_prefs import Prefs

from .ops.export_gr2             import ExportGR2
from .ops.import_area             import ImportAREA
from .ops.import_fxspec           import ImportFXSPEC
from .ops.import_gr2             import ImportGR2
from .ops.import_cha             import ImportCHA
from .ops.import_clo             import ImportCLO
from .ops.import_jba             import ImportJBA
from .ops.import_jba_64          import ImportJBA64

from .types.node         import NODE_OT_ngroup_edit
from .ops.shaders_menu    import *  # classes and fn for Shader Editor's Add menu functionality
from .ops.process_materials import (
    SWTOR_OT_apply_materials_by_name,
    SWTOR_OT_apply_materials_by_name_selected,
    SWTOR_OT_apply_materials_by_name_file,
    swtor_process_materials_submenu_element,
)

# ============================================================================
# DEPRECATED SYSTEM -- ShaderNodeHeroEngine custom-node shaders
#
# Everything imported here (and everything added to deprecated_classes,
# plus the two DEPRECATED-marked blocks further down in register()/
# unregister()) supports the addon's original custom-Python-node shader
# system, superseded by the native node-group shaders in .ops.shaders_menu.
#
# TO DROP SUPPORT: delete this import block, the deprecated_classes tuple
# and everywhere it's used below, plus these four files:
#   - types/node_deprecated.py
#   - types/node_tree_deprecated.py
#   - ops/shaders_menu_deprecated.py
#   - ops/migrate_shaders.py (reads ShaderNodeHeroEngine instances that
#     only exist because of this system -- nothing left to migrate FROM
#     once it's gone)
# ============================================================================
from .types.node_deprecated import ShaderNodeHeroEngine
from .ops.shaders_menu_deprecated import *
from .ops.migrate_shaders import (
    SWTOR_OT_migrate_shader,
    SWTOR_OT_migrate_shaders_selected,
    SWTOR_OT_migrate_shaders_file,
    swtor_migrate_submenu_element,
)
# ============================================================================
# END DEPRECATED SYSTEM imports
# ============================================================================



# Python doesn't reload package sub-modules at the same time as __init__.py!

# reload modules in subfolder for current Blender version
addon_root_path = os.path.dirname(os.path.realpath(__file__))

directories = [os.path.join(addon_root_path, entry) for entry in {'ops','types','utils'}]
                              
for directory in directories:
    for entry in os.listdir(directory):
        if entry.endswith('.py'):
            module = sys.modules.get(f"{__name__}.{entry[:-3]}")

            if module:
                importlib.reload(module)

# …And reload common preferences module in root of add-on
module = sys.modules.get("addon_prefs")
if module:
    importlib.reload(module)


# Clear out any scene update funcs hanging around, e.g. after a script reload
for func in depsgraph_update_post:
    if func.__module__.startswith(__name__):
        depsgraph_update_post.remove(func)

del importlib, os, sys, depsgraph_update_post


# Import/Export functions to append to Import/Export menus in register()

# Importers

def _import_area(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ImportAREA.bl_idname, text="SWTOR Area Assembler (.json) - BETA")

def _import_fxspec(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ImportFXSPEC.bl_idname, text="SWTOR FX Spec (.fxspec) - BETA")

def _import_gr2(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ImportGR2.bl_idname, text="SWTOR Objects and Skeletons (.gr2 32/64-bit)")

def _import_jba(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ImportJBA.bl_idname, text="SWTOR Animations (.jba 32-bit)")
    self.layout.operator(ImportJBA64.bl_idname, text="SWTOR Animations (.jba 64-bit)")

def _import_cha(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ImportCHA.bl_idname, text="SWTOR NPC/Character (Jedipedia .json)")

def _import_clo(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ImportCLO.bl_idname, text="SWTOR Cloth Physics (.clo 64-bit) - BETA")

# Exporters

def _export_gr2(self, _context):
    # type: (Menu, Context) -> None
    self.layout.operator(ExportGR2.bl_idname, text="SWTOR Objects (.gr2) - BETA:  Read Tooltip")


class BoneBounds(PropertyGroup):
    bounds: FloatVectorProperty(default=[0.0] * 6, name="Bounds", precision=6, size=6)


classes = (
    Prefs,
    BoneBounds,
    ExportGR2,
    ImportAREA,
    ImportCHA,
    ImportCLO,
    ImportFXSPEC,
    ImportGR2,
    ImportJBA,
    ImportJBA64,
    NODE_OT_ngroup_edit,
    NODE_MT_swtor_shaders_menu,
    NODE_OT_add_swtor_shader_group,
    SWTOR_OT_apply_materials_by_name,
    SWTOR_OT_apply_materials_by_name_selected,
    SWTOR_OT_apply_materials_by_name_file,
)

# ============================================================================
# DEPRECATED SYSTEM -- see the import block near the top of this file for
# what to delete when dropping support.
# ============================================================================
deprecated_classes = (
    ShaderNodeHeroEngine,
    NODE_MT_add_swtor_shader,
    NODE_MT_swtor_shaders_menu_deprecated,
    # Migration bridge -- searchable (F3) standard operators, no menu
    # entries by design; see ops/migrate_shaders.py.
    SWTOR_OT_migrate_shader,
    SWTOR_OT_migrate_shaders_selected,
    SWTOR_OT_migrate_shaders_file,
)
# ============================================================================
# END DEPRECATED SYSTEM classes
# ============================================================================

keymaps: List[KeyMap] = []


def register():
    # type: () -> None
    import bpy

    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
    for cls in deprecated_classes:  # DEPRECATED SYSTEM
        register_class(cls)


    # Additions to Import-Export menu
    from bpy.types import TOPBAR_MT_file_export, TOPBAR_MT_file_import
    TOPBAR_MT_file_import.append(_import_gr2)
    TOPBAR_MT_file_import.append(_import_jba)
    TOPBAR_MT_file_import.append(_import_cha)
    TOPBAR_MT_file_import.append(_import_clo)
    TOPBAR_MT_file_import.append(_import_area)
    TOPBAR_MT_file_import.append(_import_fxspec)
    
    TOPBAR_MT_file_export.append(_export_gr2)


    from bpy.props import CollectionProperty
    from bpy.types import Object
    Object.bone_bounds = CollectionProperty(name="Bone Bounds", type=BoneBounds)

    
    from bpy.props import StringProperty
    bpy.types.Scene.swtor_io_last_job = StringProperty(
        name="SWTOR IO Add-on's Last Activity",
        description=".json-format info about the results of the use of this add-on\n (e.g., objects imported) that external operators can use",
        default='',
        )
    
    
    # Additions to Shader Editor's Add menu
    from .types import node
    bpy.types.NODE_MT_add.append(swtor_shaders_submenu_element)
    bpy.types.NODE_MT_add.append(swtor_shaders_submenu_element_deprecated)  # DEPRECATED SYSTEM

    # Migration entries -- appended onto the deprecated submenu itself
    # (not NODE_MT_add), split off with a separator from its six
    # "Add SWTOR Shader" entries. See ops/migrate_shaders.py.
    NODE_MT_swtor_shaders_menu_deprecated.append(swtor_migrate_submenu_element)  # DEPRECATED SYSTEM

    # Materials-by-name (7b) entries -- appended onto the live
    # NODE_MT_swtor_shaders_menu itself, split off with a separator from
    # its seven "Add [X] Shader" entries. See ops/process_materials.py.
    NODE_MT_swtor_shaders_menu.append(swtor_process_materials_submenu_element)


    # TAB-into-Nodegroup functionality
    wm = bpy.context.window_manager
    km = wm.keyconfigs.addon.keymaps.new(name='Node Editor', space_type='NODE_EDITOR')
    kmi = km.keymap_items.new(node.NODE_OT_ngroup_edit.bl_idname, 'TAB', 'PRESS')
    kmi.properties.exit = False
    kmi = km.keymap_items.new(node.NODE_OT_ngroup_edit.bl_idname, 'TAB', 'PRESS', ctrl=True)
    kmi.properties.exit = True
    keymaps.append(km)


def unregister():
    NODE_MT_swtor_shaders_menu.remove(swtor_process_materials_submenu_element)
    NODE_MT_swtor_shaders_menu_deprecated.remove(swtor_migrate_submenu_element)  # DEPRECATED SYSTEM
    bpy.types.NODE_MT_add.remove(swtor_shaders_submenu_element_deprecated)  # DEPRECATED SYSTEM
    bpy.types.NODE_MT_add.remove(swtor_shaders_submenu_element)

    # type: () -> None
    for km in keymaps:
        for kmi in km.keymap_items:
            km.restore_item_to_default(kmi)
    keymaps.clear()

    from bpy.types import TOPBAR_MT_file_export, TOPBAR_MT_file_import
    TOPBAR_MT_file_import.remove(_import_fxspec)
    TOPBAR_MT_file_import.remove(_import_area)
    TOPBAR_MT_file_import.remove(_import_cha)
    TOPBAR_MT_file_import.remove(_import_clo)
    TOPBAR_MT_file_import.remove(_import_gr2)
    TOPBAR_MT_file_import.remove(_import_jba)
    TOPBAR_MT_file_export.remove(_export_gr2)

    from bpy.utils import unregister_class
    for cls in deprecated_classes:  # DEPRECATED SYSTEM
        unregister_class(cls)
    for cls in classes:
        unregister_class(cls)
        
    del bpy.types.Scene.swtor_io_last_job


if __name__ == '__main__':
    try:
        unregister()
    except Exception:
        pass

    register()
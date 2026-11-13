"""Runs when UEFN opens this project: adds the Loot Pools button to the level editor toolbar.

Part of the Loot Pool Editor (loot_pool_editor.py). If your project already has an init_unreal.py,
add the two lines inside the try block to it instead of replacing it.
"""

import unreal

try:
    import loot_pool_editor

    loot_pool_editor.register_menu()
except Exception as exc:  # never block editor startup
    unreal.log_error(f"[LootPoolEditor] toolbar button not added: {exc}")

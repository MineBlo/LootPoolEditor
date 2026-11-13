# Loot Pool Editor for UEFN

A Python tool that runs inside UEFN and lets you build Verse **loot pools** as a tree, then saves
them as a ready-to-build `.verse` file containing a `loot_pool` class.

You don't need to install anything extra: the window is drawn with UEFN's own UI widgets (SlateIM),
so it looks like the rest of the editor and works with UEFN's built-in Python.

> **Heads-up:** Loot Pools (`/UnrealEngine.com/LootPools`) are **Experimental** in Verse.
> Islands that use them **can't be published** until Epic makes the API public.

---

## Features

- Tree editor for source pools, weighted groups, guaranteed bundles and items, with live **chance %**.
- **Entity prefab picker** with item icons, search, a type filter (Items / Weapons / Custom) and a
  rarity filter.
- Picks up your project's own **custom entity prefabs** (after a Verse build), including their icon,
  rarity and in-game name.
- Flags problems before you build:
  - ⚠ **red**: deprecated or experimental entity prefabs, which block publishing
  - ❓ **red**: unknown names that Verse won't build
  - **yellow**: custom prefabs that aren't items (no `item_component` and not parented to a
    Fortnite item or weapon)
- Opens `.verse` files it wrote earlier, so you can keep editing a pool.
- Recent files, a Verse preview with copy to clipboard, and editable weights right in the tree.
- Never creates assets in your project. It only writes the `.verse` files you save.

## Requirements

- UEFN on Windows.
- Python allowed for the project. UEFN asks the first time a project runs Python.
- Verse built at least once in the project, so UEFN has generated its API digests.

---

## Installation

1. **Download** this repository: on GitHub, click **Code → Download ZIP**, then unzip it.
2. **Copy** the files from `Content/Python/` into your project's `Content/Python/` folder.
   Create that folder if it doesn't exist:

   ```
   <YourProject>/
   └── Content/
       └── Python/
           ├── loot_pool_editor.py   ← required
           └── init_unreal.py        ← optional: adds the toolbar button
   ```

   `loot_pool_editor.py` must be inside `Content/Python`, because it finds your project from its own
   location.

3. **Already have an `init_unreal.py`?** Don't overwrite it. Add these two lines to it instead:

   ```python
   import loot_pool_editor
   loot_pool_editor.register_menu()
   ```

4. **Open (or reopen) the project in UEFN.** Allow Python if UEFN asks.
5. **Build Verse once** (Verse → Build Verse Code) if you haven't already in this project.

## Opening the editor

Any of these work:

- Click **Loot Pools** on the level editor toolbar. It's a toggle: click again to close the window.
  It needs `init_unreal.py` from step 2.
- **Tools → Execute Python Script…** and pick `loot_pool_editor.py`.
- In the **Output Log**, type this into its command box (UEFN's console runs Python):

  ```python
  import loot_pool_editor; loot_pool_editor.open_editor()
  ```

---

## Using it

### 1. Start a loot pool

- **New** starts an empty loot pool.
- **Open Verse…** opens a `.verse` file this editor saved before.
- The **dropdown** next to Open Verse lists recent files, and shows the file you have open.
- **Lootpool class name** is the name of the Verse class that gets written, e.g. `TreasureChestLootPool`.

### 2. Build the tree

Select an entry, then use the **Add / arrange** buttons on the right.

| Entry           | What it is in Verse                                     | Use it for                                    |
| --------------- | ------------------------------------------------------- | --------------------------------------------- |
| **Source Pool** | a `weighted_loot_pool` in `SourcePools`                 | Every source pool rolls. Top level only.      |
| **Group**       | a `weighted_loot_entry_pool`                            | A weighted choice, e.g. Common / Rare / Epic. |
| **Bundle**      | a group marked *Guaranteed*                             | Things that always drop together, e.g. a gun and its ammo. |
| **Item**        | a `weighted_loot_entry_item`                            | One entity prefab. Quantity > 1 rolls it N times. |

- **Duplicate**, **Delete** (or the **Del** key) and **Move up / Move down** rearrange entries.
- Type **weights** right in the tree's Weight column, or in the Properties panel.
- The **Chance** column shows each entry's share of its parent's picks.

### 3. Set an item's entity prefab

Select an item and click **Pick…** next to *Entity Prefab*. In the picker:

- **Search** by name, and filter by type (**All / Items / Weapons / Custom**) and **rarity**.
- Untick **Show invalid** to hide deprecated, experimental and non-item entity prefabs.
- **Double-click** an entry, or select it and press **Select**.

You can also type a name into the *Entity Prefab* field. If it doesn't match anything, it's
marked ❓ unknown.

### 4. Save and build

- **Save** / **Save As…** writes the `.verse` file. Save it inside your project's `Content` folder
  so UEFN compiles it.
- **File → Preview Verse** shows the generated code, with a **Copy to clipboard** button.
- Then **build Verse** in UEFN. The file defines a `loot_pool` class with the name you gave it.

### Custom entity prefabs

Your own entity prefabs show up under **Custom** after a **Verse build**. The editor notices the
build by itself, so there's no need to reopen it. For each one it shows:

- **Icon**: from its `icon_component`, or the one it inherits.
- **Rarity**: from its `rarity_component`.
- **Name**: from its `description_component`. If it has none, or the name is empty, the parent
  prefab's or Fortnite item's name is used.
- **Item check**: it counts as an item if it has an `item_component` or is parented to a Fortnite
  item or weapon. Otherwise it's yellow, with a note explaining how to fix it.

---

## Troubleshooting

| Problem                                              | Fix                                                                 |
| ---------------------------------------------------- | ------------------------------------------------------------------- |
| No **Loot Pools** button                             | Check `init_unreal.py` (or the two lines) is in `Content/Python`, that Python is allowed for the project, then reopen the project. Look in the Output Log for `[LootPoolEditor]` messages. |
| Status bar says *Fortnite.digest.verse not found*    | Build Verse once in this project.                                   |
| A new custom prefab is missing                       | Build Verse. Prefabs come from the project's Verse digest.          |
| *Open failed* on a `.verse` file                     | Open Verse only reads files in the shape this editor writes. It isn't a general Verse parser. |
| Verse build errors about an entity prefab            | Look for ❓ unknown or ⚠ red entries in the tree and pick a valid entity prefab. |

## Updating and uninstalling

- **Update:** replace `loot_pool_editor.py` with the new version and reopen the project.
- **Uninstall:** delete `loot_pool_editor.py` and `init_unreal.py`, or the two lines you added.
  Recent files and settings are stored in `%LOCALAPPDATA%\LootPoolEditor\settings.json`; delete that
  folder too if you like.

## Notes

- Windows only, like UEFN. File dialogs and the clipboard use the Win32 API.
- Uses only Python's standard library and UEFN's `unreal` module.
- Reading custom prefab names writes a small temporary file to `%TEMP%`, which is deleted right away.
- Built and tested on UEFN **Fortnite-Release-43.10-CL-60458657**. Loot Pools are experimental, so
  later UEFN versions may change the API.

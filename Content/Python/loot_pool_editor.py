"""Loot Pool Editor for UEFN.

A window for creating and editing Verse loot pools (/UnrealEngine.com/LootPools) as a tree,
written out as a ready-to-build .verse file with a loot_pool class. The UI is drawn with the
editor's own Slate widgets (SlateIM), so it looks like the rest of UEFN and needs nothing installed.

INSTALL
    1. Copy this file to <YourProject>/Content/Python/loot_pool_editor.py
       (it must sit in Content/Python: it finds the project from its own location).
    2. Optional, for a "Loot Pools" toolbar button on every launch: copy init_unreal.py next to it,
       or, if the project already has an init_unreal.py, add these two lines to it:
           import loot_pool_editor
           loot_pool_editor.register_menu()
    3. Open the project in UEFN and build Verse once, so its API digests exist.
       UEFN asks to allow Python for the project the first time.

USE
    Toolbar:   the "Loot Pools" toggle button (opens/closes the window)
    Or:        Tools > Execute Python Script... > this file
    Or:        Output Log command box (it runs Python):  import loot_pool_editor; loot_pool_editor.open_editor()

TREE
    Source Pool  -> one weighted_loot_pool in SourcePools (every source pool rolls)
    Group        -> weighted_loot_entry_pool (a weighted choice inside a pool)
    Bundle       -> a Group marked Guaranteed: every child drops once (Inf weight, max 1)
    Item         -> weighted_loot_entry_item; Quantity > 1 wraps it in a pool rolled N times

ENTITIES
    Items / Weapons: Fortnite.digest.verse of this project.
    Custom:          the project's own Assets digest (concrete entity prefabs). Shown in yellow
                     unless confirmed as an item: parented to a Fortnite item/weapon, or has an
                     item_component. Rebuild Verse to pick up new prefabs.
    Red:             @deprecated / @experimental entities, which block publishing, and "❓" unknown
                     names (typed by hand, not in either digest), which won't build. Save asks first.

NOTES
    Loot Pools are Experimental in Verse: islands that use them can't be published yet.
    Open Verse reads files in the shape this editor writes; it is not a general Verse parser.
    Recent files are kept per project (each project only lists its own files), stored per user in
    %LOCALAPPDATA%/LootPoolEditor/settings.json so nothing is written into the project.
    Windows only (UEFN is): file dialogs and the clipboard use the Win32 API.
"""

from __future__ import annotations

__version__ = "0.1.0"

import copy
import ctypes
import json
import os
import re
import tempfile
import time
import traceback
from ctypes import wintypes
from pathlib import Path

try:
    import unreal  # only present inside the editor
except ImportError:
    unreal = None  # the Verse import/export functions still work (tests); the window needs UEFN

SCRIPT_DIR = Path(__file__).resolve().parent
CONTENT_DIR = SCRIPT_DIR.parent          # <Project>/Content
PROJECT_NAME = next((p.stem for p in CONTENT_DIR.parent.glob("*.uplugin")),  # <Project>.uplugin
                    CONTENT_DIR.parent.name)                                  # else the folder name

USINGS = [
    "using { /Verse.org/SceneGraph }",
    "using { /Verse.org/Verse }",
    "using { /UnrealEngine.com/LootPools }",
    "using { /Fortnite.com/Items }",
    "using { /Fortnite.com/Weapons }",
]
DEFAULT_HEADER = (
    "Made with the Loot Pool Editor.\n"
    "Loot Pools are Experimental: islands that use them can't be published yet, and the API may change."
)
DEFAULT_WEIGHT = 1.0  # assumed engine default when no WeightCalculator is set (chance display only)
IND = "    "
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RARITIES = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic", "Exotic"]
MUTED = "#9a9a9a"
CUSTOM_FG = "#e5c07b"   # yellow: project entity that may not be an item
BLOCKED_FG = "#ff8a80"  # red: @deprecated / @experimental, blocks publishing

SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LootPoolEditor" / "settings.json"
MAX_RECENT = 10  # per project


def _project_key_of(path: Path) -> str | None:
    """The project a file belongs to, as a normalized folder path: the parent of the nearest
    enclosing Content folder. None when the file isn't inside a project's Content."""
    for parent in Path(path).resolve().parents:
        if parent.name.lower() == "content":
            return os.path.normcase(str(parent.parent))
    return None


PROJECT_KEY = os.path.normcase(str(CONTENT_DIR.resolve().parent))  # this project's recent files


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass  # recent files are a convenience; never fail a save over them


def flag_warning(flag) -> str:
    """Warning text for a (status, message) flag from the digest."""
    status, message = flag
    if status == "deprecated":
        return f"Deprecated, blocks publishing. {message}".strip()
    return "Experimental, blocks publishing."


class LootPoolError(Exception):
    pass


# --------------------------------------------------------------------------- model

def new_node(kind: str, **kw) -> dict:
    node = {"kind": kind, "label": "", "entity": "", "qty": "1", "weight": "",
            "max": "", "rolls": "", "guaranteed": False, "children": []}
    node.update(kw)
    return node


def new_document() -> dict:
    return {"class_name": "NewLootPool", "header": DEFAULT_HEADER,
            "pools": [new_node("pool", label="Pool 1")]}


def node_title(node: dict) -> str:
    kind = node["kind"]
    if kind == "item":
        name = node["label"] or node["entity"] or "(no entity prefab)"
        qty = _to_int(node["qty"], 1)
        return f"{qty} × {name}" if qty > 1 else name
    if kind == "group":
        name = node["label"] or "Group"
        return f"[Bundle] {name}" if node["guaranteed"] else name
    return node["label"] or "Source Pool"


def _to_int(text, default=None):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- digest

def find_fortnite_digest() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UnrealEditorFortnite" / "Saved" / "VerseProject"
    candidates = [
        base / PROJECT_NAME / "Digests" / "BuiltIn" / "Fortnite" / "Fortnite.digest.verse",
        base / PROJECT_NAME / "Fortnite" / "Fortnite.digest.verse",
    ]
    found = [p for p in candidates if p.is_file()]
    if not found and base.is_dir():  # any project's digest, newest first
        found = sorted(base.glob("*/Digests/BuiltIn/Fortnite/Fortnite.digest.verse"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


_DEPRECATED_RE = re.compile(r'^\s*@deprecated\b(?:.*?Message\s*:=\s*"((?:[^"\\]|\\.)*)")?')


def _attribute_state(line: str, pending):
    """Track @deprecated / @experimental lines that sit above a declaration.
    Returns the pending (status, message) to carry to the next declaration line."""
    stripped = line.strip()
    m = _DEPRECATED_RE.match(line)
    if m:
        return ("deprecated", m.group(1) or "")
    if stripped.startswith("@experimental"):
        return ("experimental", "")
    if not stripped or stripped.startswith("#") or stripped.startswith("@"):
        return pending  # comments, blank lines and other attributes keep what's pending
    return None


ITEM_MODULES = ("Items", "Weapons", "Armory")
ITEM_COMPONENT = "__verse_0x5D9C6EA9_ItemComponent"  # entity.item_component field, as a subobject name
RARITY_COMPONENT = "__verse_0xB19A0D24_RarityComponent"
DESCRIPTION_COMPONENT = "__verse_0xA70A9FFD_DescriptionComponent"  # Armory: the creator templates (pistol_template, ...)


def load_digest_entities(path: Path):
    """(entities, flags, item_classes): entities = sorted [(name, module)] for every entity class in the
    digest's Items and Weapons modules; flags = {name: (status, message)} for @deprecated /
    @experimental ones; item_classes = every entity class in Items, Weapons and Armory, i.e. the
    classes a project prefab can extend and still be an item. (Every Fortnite class extends entity
    directly, so the module is what tells an item apart from, say, an animal in Characters.)"""
    module_re = re.compile(r"^([A-Za-z_]\w*)<public> := module:")
    entity_re = re.compile(r"^\s+([A-Za-z]\w*)(?:<\w+>)*<public> := class<concrete>\(entity\):")
    out, flags, items, module, pending = set(), {}, set(), "", None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = module_re.match(line)
            if m:
                module, pending = m.group(1), None
                continue
            if module in ITEM_MODULES:
                m = entity_re.match(line)
                if m:
                    items.add(m.group(1))
                    if module != "Armory" and m.group(1)[0].isupper():  # templates aren't pickable
                        out.add((m.group(1), module))
                        if pending:
                            flags[m.group(1)] = pending
            pending = _attribute_state(line, pending) if not m else None
    return sorted(out), flags, items


def find_project_digest() -> Path | None:
    """The project's own Assets digest (prefabs and other asset classes), newest copy."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UnrealEditorFortnite" / "Saved" / "VerseProject" / PROJECT_NAME
    candidates = [
        base / "Digests" / f"{PROJECT_NAME}-Assets" / f"{PROJECT_NAME}-Assets.digest.verse",
        base / f"{PROJECT_NAME}-Assets" / "Assets.digest.verse",
    ]
    found = [p for p in candidates if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def load_project_entities(path: Path, item_classes=frozenset()):
    """(paths, root, flags, items, classes, parents): paths = {name: Verse module path} for every concrete
    entity class in the project digest; root = the project's root module path; flags = {name: (status,
    message)} for @deprecated / @experimental ones; items = the ones confirmed to be items, because
    they extend one of item_classes (see load_digest_entities) or another confirmed project item;
    classes = {name: Unreal class path} from @import_as, e.g. /MyIsland/Prefabs/Gun.Gun_C;
    parents = {name: parent class name}, e.g. AccelerantDrumGun_BR_CH7S4_Exotic. Only
    concrete classes: EntityType needs castable_concrete_subtype(entity)."""
    root_re = re.compile(r"^# Verse path: (\S+)")
    import_re = re.compile(r"^\s*# Module import path: (\S+)")
    module_re = re.compile(r"^(\s*)\S.*:= module:")
    # any class; whether it's an entity is decided below (prefabs may extend item templates or items)
    class_re = re.compile(r"^(\s*)([A-Za-z_]\w*)(?:<[^>]*>)*\s*:=\s*class((?:<[^>]*>)*)\((.*)\):\s*$")
    prefab_re = re.compile(r"^\s*([A-Za-z_]\w*)_asset(?:<[^>]*>)*\s*:\s*entity_prefab\b")
    import_as_re = re.compile(r'^\s*@import_as\("([^"]+)"\)')
    root, pending, stack, out, flags, attr = "", None, [], {}, {}, None
    parents, prefabs, classes, import_as = {}, set(), {}, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = root_re.match(line)
            if m and not root:
                root = m.group(1)
                continue
            m = import_re.match(line)
            if m:
                pending = m.group(1)
                continue
            m = module_re.match(line)
            if m:
                indent = len(m.group(1))
                stack = [s for s in stack if s[0] < indent] + [(indent, pending or "")]
                pending = attr = None
                continue
            p = prefab_re.match(line)
            if p:
                prefabs.add(p.group(1))
            a = import_as_re.match(line)
            if a:
                import_as = a.group(1)  # belongs to the class declared next
            m = class_re.match(line)
            if m:
                if import_as:
                    classes[m.group(2)] = import_as
                import_as = None
            if m and "concrete" in m.group(3):
                indent = len(m.group(1))
                stack = [s for s in stack if s[0] < indent]
                out[m.group(2)] = (stack[-1][1] if stack else "") or root
                parents[m.group(2)] = m.group(4).strip()
                if attr:
                    flags[m.group(2)] = attr
            attr = _attribute_state(line, attr) if not m else None
    # entities: every prefab (it has a NAME_asset:entity_prefab, whatever it extends, e.g.
    # assault_rifle_template or a Fortnite weapon), plus concrete classes that extend entity directly
    keep = {n for n in out if n in prefabs or parents.get(n) == "entity"}
    # "(/Fortnite.com:)Weapons.AccelerantDrumGun_BR_CH7S4_Exotic" -> "AccelerantDrumGun_BR_CH7S4_Exotic"
    base = {n: re.split(r"[.:)]", parents[n])[-1].strip() for n in keep}
    items, grew = set(), True
    while grew:  # a prefab can extend another project prefab that is an item
        new = {n for n in keep - items if base[n] in item_classes or base[n] in items}
        items |= new
        grew = bool(new)
    return ({n: path for n, path in out.items() if n in keep}, root,
            {n: f for n, f in flags.items() if n in keep}, items,
            {n: c for n, c in classes.items() if n in keep}, base)


def rarity_of(entity: str) -> str:
    tail = entity.rsplit("_", 1)[-1]
    return tail if tail in RARITIES else ""


def pretty_name(entity: str) -> str:
    """PumpShotgun_BR_CH2S1_Common -> Pump Shotgun"""
    head = entity.split("_", 1)[0]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", head)


# --------------------------------------------------------------------------- Verse generation

def _fmt_weight(text: str, where: str) -> str | None:
    text = str(text).strip()
    if not text:
        return None
    if text.lower() in ("inf", "infinity"):
        return "Inf"
    try:
        value = float(text)
    except ValueError:
        raise LootPoolError(f"{where}: weight '{text}' is not a number (or Inf)")
    if value < 0:
        raise LootPoolError(f"{where}: weight can't be negative")
    s = repr(value)
    return format(value, "f") if "e" in s else s


def _fmt_count(text: str, where: str, what: str) -> int | None:
    text = str(text).strip()
    if not text:
        return None
    value = _to_int(text)
    if value is None or value < 1:
        raise LootPoolError(f"{where}: {what} must be a whole number >= 1")
    return value


def _calc_fields(node: dict, forced: bool, where: str) -> list[str]:
    fields = []
    weight = "Inf" if forced else _fmt_weight(node["weight"], where)
    if weight:
        fields.append(f"WeightCalculator := loot_weight_calculator_fixed{{Weight := {weight}}}")
    max_sel = 1 if forced else _fmt_count(node["max"], where, "max selections")
    if max_sel:
        fields.append(f"MaxSelectionCount := option{{{max_sel}}}")
    return fields


def _join_entries(entries: list[tuple[list[str], str | None]]) -> list[str]:
    """Comma-separate array elements; a trailing comment goes after the comma."""
    out = []
    for i, (lines, trailing) in enumerate(entries):
        lines = list(lines)
        last = lines[-1] + ("," if i < len(entries) - 1 else "")
        if trailing:
            last += f" # {trailing}"
        lines[-1] = last
        out += lines
    return out


def _emit_pool(prefix: str, rolls: int | None, children: list[dict], depth: int,
               forced_children: bool, where: str) -> list[str]:
    pad = IND * depth
    lines = [f"{pad}{prefix}weighted_loot_pool{{"]
    if rolls:
        lines.append(f"{pad}{IND}RollCalculator := loot_roll_calculator_fixed{{Rolls := {rolls}}},")
    lines.append(f"{pad}{IND}Options := array{{")
    lines += _join_entries([_emit_entry(c, depth + 2, forced_children, f"{where} > {node_title(c)}")
                            for c in children])
    lines.append(f"{pad}{IND}}}")
    lines.append(f"{pad}}}")
    return lines


def _emit_entry_pool(comment: str | None, pool_lines: list[str], calc: list[str], depth: int) -> list[str]:
    pad = IND * depth
    lines = [f"{pad}# {comment}"] if comment else []
    lines.append(f"{pad}weighted_loot_entry_pool{{")
    if calc:
        pool_lines[-1] += ","
    lines += pool_lines
    lines += [f"{pad}{IND}{c}" + ("," if i < len(calc) - 1 else "") for i, c in enumerate(calc)]
    lines.append(f"{pad}}}")
    return lines


def _emit_entry(node: dict, depth: int, forced: bool, where: str) -> tuple[list[str], str | None]:
    calc = _calc_fields(node, forced, where)
    if node["kind"] == "item":
        entity = node["entity"].strip()
        if not IDENT_RE.match(entity):
            raise LootPoolError(f"{where}: pick an entity prefab")
        label = node["label"].strip() or pretty_name(entity)
        qty = _fmt_count(node["qty"], where, "quantity") or 1
        if qty == 1:
            return [IND * depth + "weighted_loot_entry_item{" +
                    ", ".join([f"EntityType := {entity}"] + calc) + "}"], label
        single = new_node("item", entity=entity, label=label)
        pool = _emit_pool("Pool := ", qty, [single], depth + 1, False, where)
        return _emit_entry_pool(f"{qty} × {label}", pool, calc, depth), None

    if node["kind"] == "group":
        if not node["children"]:
            raise LootPoolError(f"{where}: group is empty")
        guaranteed = bool(node["guaranteed"])
        rolls = len(node["children"]) if guaranteed else _fmt_count(node["rolls"], where, "rolls")
        pool = _emit_pool("Pool := ", rolls, node["children"], depth + 1, guaranteed, where)
        return _emit_entry_pool(node["label"].strip() or None, pool, calc, depth), None

    raise LootPoolError(f"{where}: a Source Pool can only sit at the top level")


def generate_verse(doc: dict, extra_usings: list[str] = ()) -> str:
    cls = doc["class_name"].strip()
    if not IDENT_RE.match(cls):
        raise LootPoolError(f"Class name '{cls}' is not a valid Verse identifier")
    if not doc["pools"]:
        raise LootPoolError("Add at least one Source Pool")

    pools = []
    for pool in doc["pools"]:
        where = node_title(pool)
        if pool["kind"] != "pool":
            raise LootPoolError(f"{where}: top-level entries must be Source Pools")
        if not pool["children"]:
            raise LootPoolError(f"{where}: source pool is empty")
        rolls = _fmt_count(pool["rolls"], where, "rolls")
        lines = [f"{IND * 2}# {pool['label'].strip()}"] if pool["label"].strip() else []
        lines += _emit_pool("", rolls, pool["children"], 2, False, where)
        pools.append((lines, None))

    header = [f"# {l}" if l.strip() else "#" for l in doc.get("header", "").strip().splitlines()]
    usings = USINGS + [f"using {{ {p} }}" for p in extra_usings if f"using {{ {p} }}" not in USINGS]
    out = header + usings + [
        "",
        f"# Select {cls} in a compatible Loot Pool field, or use {cls}{{}} in Verse.",
        f"{cls}<public> := class<concrete><final>(loot_pool):",
        f"{IND}SourcePools:[]loot_pool = array{{",
        *_join_entries(pools),
        f"{IND}}}",
        "",
        f"{IND}SelectLoot<override>(Context:loot_context)<transacts><decides>:[]entity =",
        f"{IND * 2}Drops := for:",
        f"{IND * 3}Pool : SourcePools",
        f"{IND * 3}Drop := Pool.SelectLoot[Context]",
        f"{IND * 2}do:",
        f"{IND * 3}Drop",
        f"{IND * 2}# A failed source is skipped by the loop. Require every source to succeed.",
        f"{IND * 2}Drops.Length = SourcePools.Length",
        f"{IND * 2}Concatenate(Drops)",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- Verse import
# Reads files in the shape generate_verse writes. Not a full Verse parser.

_TOKEN_RE = re.compile(r"(#[^\n]*)|(:=)|([{}(),])|([A-Za-z_]\w*)|(\d+(?:\.\d+)?)|(\n)|([ \t\r]+)|(.)")


class _Ctor:
    def __init__(self, name, start):
        self.name, self.fields, self.items, self.start, self.end = name, {}, [], start, start


class _Parser:
    def __init__(self, text: str):
        self.tokens, self.comments, line = [], {}, 1
        last_code_line = 0
        for m in _TOKEN_RE.finditer(text):
            comment, assign, punct, ident, num, nl, _ws, bad = m.groups()
            if nl:
                line += 1
            elif comment:
                self.comments[line] = (comment[1:].strip(), last_code_line != line)
            elif not _ws:  # stray characters only matter if the parser reaches them
                kind = "num" if num else "ident" if ident else "op"
                self.tokens.append((kind, assign or punct or ident or num or bad, line))
                last_code_line = line
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ("eof", "", 0)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise LootPoolError(f"Line {tok[2]}: expected '{value}', found '{tok[1]}'")
        self.pos += 1
        return tok

    def value(self):
        kind, val, line = self.take()
        if kind == "num":
            return val
        if kind != "ident":
            raise LootPoolError(f"Line {line}: unexpected '{val}'")
        if self.peek()[1] != "{":
            return val
        self.take("{")
        node = _Ctor(val, line)
        if val == "option":
            node.items.append(self.value())
        else:
            while self.peek()[1] != "}":
                if val == "array":
                    node.items.append(self.value())
                else:
                    name = self.take()[1]
                    self.take(":=")
                    node.fields[name] = self.value()
                if self.peek()[1] == ",":
                    self.take(",")
        node.end = self.take("}")[2]
        return node

    def leading(self, node):
        c = self.comments.get(node.start - 1)
        return c[0] if c and c[1] else ""

    def trailing(self, node):
        c = self.comments.get(node.end)
        return c[0] if c and not c[1] else ""


def _calc_value(node, field):
    calc = node.fields.get(field)
    if isinstance(calc, _Ctor):
        inner = calc.fields.get("Weight") or calc.fields.get("Rolls")
        return str(inner) if inner is not None else ""
    return ""


def _max_value(node):
    opt = node.fields.get("MaxSelectionCount")
    return str(opt.items[0]) if isinstance(opt, _Ctor) and opt.items else ""


def _import_entry(p: _Parser, node: _Ctor) -> dict:
    weight, max_sel = _calc_value(node, "WeightCalculator"), _max_value(node)
    if node.name == "weighted_loot_entry_item":
        return new_node("item", entity=str(node.fields.get("EntityType", "")),
                        label=p.trailing(node) or p.leading(node), weight=weight, max=max_sel)
    if node.name != "weighted_loot_entry_pool":
        raise LootPoolError(f"Line {node.start}: unsupported entry '{node.name}'")

    pool = node.fields.get("Pool")
    if not isinstance(pool, _Ctor) or pool.name != "weighted_loot_pool":
        raise LootPoolError(f"Line {node.start}: entry pool without a weighted_loot_pool")
    rolls = _calc_value(pool, "RollCalculator")
    options = pool.fields.get("Options")
    options = options.items if isinstance(options, _Ctor) else []

    # N × item  ->  item with quantity N
    if rolls and len(options) == 1 and options[0].name == "weighted_loot_entry_item" \
            and not options[0].fields.get("WeightCalculator") and not options[0].fields.get("MaxSelectionCount"):
        item = _import_entry(p, options[0])
        item.update(qty=rolls, weight=weight, max=max_sel)
        return item

    children = [_import_entry(p, o) for o in options]
    guaranteed = bool(children) and _to_int(rolls) == len(children) and \
        all(c["weight"] == "Inf" and c["max"] == "1" for c in children)
    if guaranteed:
        for c in children:
            c["weight"] = c["max"] = ""
    return new_node("group", label=p.leading(node), weight=weight, max=max_sel,
                    rolls="" if guaranteed else rolls, guaranteed=guaranteed, children=children)


def import_verse(text: str) -> dict:
    cls = re.search(r"^([A-Za-z_]\w*)<public>\s*:=\s*class\b.*\(loot_pool\)", text, re.M)
    start = text.find("SourcePools")
    start = text.find("array{", start) if start >= 0 else -1
    if not cls or start < 0:
        raise LootPoolError("No loot_pool class with a SourcePools array found")

    header = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        header.append(line[1:].strip())

    p = _Parser(text[:start].count("\n") * "\n" + text[start:])
    arr = p.value()
    pools = []
    for pool in arr.items:
        options = pool.fields.get("Options")
        pools.append(new_node(
            "pool", label=p.leading(pool), rolls=_calc_value(pool, "RollCalculator"),
            children=[_import_entry(p, o) for o in (options.items if isinstance(options, _Ctor) else [])]))
    return {"class_name": cls.group(1), "header": "\n".join(header) or DEFAULT_HEADER, "pools": pools}


def _to_float(text):
    try:
        return float(text)
    except ValueError:
        return 0.0


def row_info(nodes: list[dict], guaranteed: bool):
    """(node, weight, chance, rolls/max) display strings for the children of one pool."""
    weights = []
    for n in nodes:
        w = "Inf" if guaranteed else (n["weight"].strip() or str(DEFAULT_WEIGHT))
        weights.append(None if w.lower() in ("inf", "infinity") else _to_float(w))
    finite_total = sum(w for w in weights if w)
    for node, w in zip(nodes, weights):
        if node["kind"] == "pool":
            chance = ""
        elif w is None:
            chance = "always"
        else:
            chance = f"{100 * w / finite_total:.1f}%" if finite_total else "-"
        count = node["rolls"] if node["kind"] != "item" else ""
        if node["kind"] == "group" and node["guaranteed"]:
            count = str(len(node["children"]))
        if node["max"]:
            count = f"{count or '1'} / {node['max']}"
        yield node, ("Inf" if guaranteed else node["weight"]), chance, count


# --------------------------------------------------------------------------- Win32: file dialogs, clipboard

class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", ctypes.c_void_p),
                ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD), ("lpstrFile", ctypes.c_void_p),
                ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD)]


def file_dialog(save: bool, title: str, initial_dir: Path, initial_file: str = "") -> Path | None:
    """Native Windows Open / Save As dialog for .verse files. None when cancelled."""
    filters = ctypes.create_unicode_buffer("Verse (*.verse)\0*.verse\0All files (*.*)\0*.*\0\0")
    buffer = ctypes.create_unicode_buffer(initial_file, 1024)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(ofn)
    ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    ofn.lpstrFilter = ctypes.addressof(filters)
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.addressof(buffer)
    ofn.nMaxFile = len(buffer)
    ofn.lpstrInitialDir = str(initial_dir)
    ofn.lpstrTitle = title
    ofn.lpstrDefExt = "verse"
    # OFN_EXPLORER | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST, plus OVERWRITEPROMPT / FILEMUSTEXIST
    ofn.Flags = 0x80000 | 0x8 | 0x800 | (0x2 if save else 0x1000)
    dialog = ctypes.windll.comdlg32.GetSaveFileNameW if save else ctypes.windll.comdlg32.GetOpenFileNameW
    return Path(buffer.value) if dialog(ctypes.byref(ofn)) else None


VK_DELETE = 0x2E
VK_LBUTTON = 0x01


def _key_down(virtual_key: int) -> bool:
    """Whether a key is physically held right now, whichever window has focus."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(virtual_key) & 0x8000)


def _cursor_pos() -> tuple:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _foreground_title() -> str:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, 256)
    return buffer.value


def _double_click_seconds() -> float:
    """The user's Windows double-click speed."""
    return ctypes.windll.user32.GetDoubleClickTime() / 1000.0


def copy_to_clipboard(text: str) -> bool:
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    data = ctypes.create_unicode_buffer(text)
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(0x2, ctypes.sizeof(data))  # GMEM_MOVEABLE
        ctypes.memmove(k32.GlobalLock(handle), data, ctypes.sizeof(data))
        k32.GlobalUnlock(handle)
        return bool(u32.SetClipboardData(13, handle))  # CF_UNICODETEXT; the clipboard owns it now
    finally:
        u32.CloseClipboard()


# --------------------------------------------------------------------------- SlateIM helpers
# SlateIM is immediate mode: every window is redrawn from Python each editor frame, between
# begin_window_root and end_root. Every begin_* must be matched by its end_* in the same frame, or
# the editor crashes, so each one is closed in a finally block. Anything that opens a dialog runs
# after all windows have ended (see LootPoolEditor.later).

IM = unreal.SlateIMBlueprintFunctionLibrary if unreal else None
MAIN_ID, PICKER_ID, PREVIEW_ID = "LootPoolEditorMain", "LootPoolEditorPicker", "LootPoolEditorPreview"
MAX_PICKER_ROWS = 400   # rows drawn in the entity picker; narrow the search for more
PICKER_TITLE = "Pick Entity Prefab"
NOT_ITEM_NOTE = ("⚠ Not an item: this prefab has no item_component and isn't parented to a Fortnite "
                 "item or weapon, so it can't go into a player's inventory and may not work as loot. "
                 "In the prefab editor, add an item_component "
                 "(plus an icon_component for its inventory icon), or parent it to an item template.")
UNKNOWN_NOTE = ("❓ Unknown entity prefab: not a Fortnite item/weapon or a prefab in this project, so Verse won't build. "
                "Press Pick… to choose one, or build Verse if it's a prefab you just made.")
TREE_COLUMNS = [("entry", "Entry", 360), ("weight", "Weight", 70), ("chance", "Chance", 70),
                ("count", "Rolls / Max", 90)]
PICKER_COLUMNS = [("entity", "Entity Prefab", 380), ("module", "Type", 90)]
PICKER_MODULES = ["All", "Items", "Weapons", "Custom"]


def _struct(cls, text: str):
    value = cls()
    value.import_text(text)  # several UEFN structs (Vector2f) take no constructor arguments
    return value


def _text_params(color: str | None = None):
    params = unreal.SlateIMTextParams()
    if color:
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        params.color = _struct(unreal.SlateColor, f"(SpecifiedColor=(R={r},G={g},B={b},A=1),"
                                                   f"ColorUseRule=UseColor_Specified)")
    return params


def _window_params(title: str, width: int, height: int):
    params = unreal.SlateIMWindowParams()
    params.window_title = title
    params.window_size = _struct(unreal.Vector2f, f"(X={width},Y={height})")
    params.should_reopen = True  # the same window id can be opened again after it was closed
    return params


def _columns(spec):
    out = []
    for column_id, label, width in spec:
        params = unreal.SlateIMTableColumnParams()
        params.label = label
        out.append((column_id, params, width))
    return out


class _Style:
    """Widget parameter structs, built once."""

    def __init__(self):
        self.text, self.muted = _text_params(), _text_params(MUTED)
        self.custom, self.blocked = _text_params(CUSTOM_FG), _text_params(BLOCKED_FG)
        self.button, self.check = unreal.SlateIMButtonParams(), unreal.SlateIMCheckBoxParams()
        self.edit, self.combo = unreal.SlateIMEditableTextParams(), unreal.SlateIMComboBoxParams()
        self.combo_refresh = unreal.SlateIMComboBoxParams()
        self.combo_refresh.force_refresh = True  # for a frame where the rows changed
        self.weight_edit = unreal.SlateIMEditableTextParams()
        self.weight_edit.hint_text = "1"  # blank weight = engine default
        self.search = unreal.SlateIMEditableTextParams()
        self.search.hint_text = "Search entity prefabs"
        self.menu, self.scroll = unreal.SlateIMMenuButtonParams(), unreal.SlateIMScrollBoxParams()
        self.table = unreal.SlateIMTableParams()
        self.table.selection_mode = unreal.SelectionMode.SINGLE
        self.tree_columns, self.picker_columns = _columns(TREE_COLUMNS), _columns(PICKER_COLUMNS)
        self.gap = _struct(unreal.Margin, "(Left=0,Top=2,Right=6,Bottom=2)")
        self.row = _struct(unreal.Margin, "(Left=0,Top=2,Right=0,Bottom=2)")
        self.section = _struct(unreal.Margin, "(Left=0,Top=10,Right=0,Bottom=4)")
        self.panel = _struct(unreal.Margin, "(Left=10,Top=0,Right=4,Bottom=0)")
        self.delete_key = _struct(unreal.Key, "Delete")
        self.icon_small_size, self.icon_large_size = unreal.Vector2D(22, 22), unreal.Vector2D(96, 96)
        self.no_size = unreal.Vector2D(0, 0)
        self.icon_small, self.icon_large = unreal.SlateIMImageParams(), unreal.SlateIMImageParams()
        self.icon_small.desired_size, self.icon_large.desired_size = self.icon_small_size, self.icon_large_size
        self.main = _window_params("Loot Pool Editor", 1100, 680)
        self.picker = _window_params(PICKER_TITLE, 600, 600)
        self.preview = _window_params("Verse preview", 900, 650)


def _button(text: str, style: _Style, enabled: bool = True) -> bool:
    if not enabled:
        IM.begin_disabled_state()
    try:
        IM.padding(style.gap)
        IM.v_align(unreal.VerticalAlignment.V_ALIGN_CENTER)  # don't stretch in a taller row
        return IM.button(text, style.button)
    finally:
        if not enabled:
            IM.end_disabled_state()


def _wide():
    """Next slot takes the full width. Slate wraps text to its slot, and a left-aligned slot is only as
    wide as its content, so long text in a plain slot wraps into a narrow column."""
    IM.h_align(unreal.HorizontalAlignment.H_ALIGN_FILL)


def _text_width(text: str) -> float:
    """Rough width in px of one line of default editor text, on the generous side (no text measuring
    in Python). Emoji are wider than letters."""
    return sum(16 if ord(c) > 0x2000 else 7.5 for c in text) + 4


def _object_ref(obj, prop: str) -> str:
    """Path of the object a Verse field refers to, read through the editor's ObjectToolset (Verse
    fields aren't Python properties); "" if it can't be read."""
    try:
        toolset = unreal.ObjectToolset.get_default_object()
        value = json.loads(toolset.call_method("GetProperties", (obj, [prop]))).get(prop)
    except Exception:
        return ""
    return value.get("refPath", "") if isinstance(value, dict) else ""


# in a T3D export of a Verse message; a name typed in the prefab editor is DefaultText<Replace>="..."
_MESSAGE_TEXT_RE = re.compile(r'_DefaultText(?:<[^>]*>)?="((?:[^"\\]|\\.)*)"')


def _unescape_t3d(value: str) -> str:
    return re.sub(r'\\(.)', r'\1', value)


def _description_name(class_path: str, defaults: str) -> str:
    """Text of the Name in a class's description_component (inherited, or added in the prefab editor)."""
    cls = unreal.load_object(None, class_path)
    if not cls:
        return ""
    unreal.get_default_object(cls)  # makes sure the defaults and their components exist
    component = next(filter(None, (unreal.find_object(None, p) for p in
                                   [f"{defaults}:{DESCRIPTION_COMPONENT}"] +
                                   [f"{defaults}:description_component_{i}" for i in range(4)])), None)
    path = _object_ref(component, "name") if component else ""
    return _message_text(path) if path else ""


def _item_definition_name(defaults: str) -> str:
    """Fortnite items have no description_component; their name is the ItemName of the item
    definition asset (e.g. WID_..._Athena) that their item_definition points at."""
    definition = unreal.find_object(None, f"{defaults}:item_definition_0")
    path = _object_ref(definition, "assetForEditor") if definition else ""
    asset = unreal.load_object(None, path) if path else None
    try:
        return str(asset.get_editor_property("item_name")) if asset else ""
    except Exception:
        return ""


_message_cache = {}  # Epic's shared message path -> text (they don't change while the editor runs)


def _message_text(path: str) -> str:
    """Text of a Verse message object, e.g. "Assault Rifle Template". Its DefaultText field is hidden
    from scripting, so the object is exported to T3D (which writes every field) and parsed."""
    if path in _message_cache:
        return _message_cache[path]
    obj = unreal.find_object(None, path) or unreal.load_object(None, path)
    if not obj:
        return ""
    out = Path(tempfile.gettempdir()) / "LootPoolEditor_message.t3d"
    task = unreal.AssetExportTask()
    task.object, task.filename = obj, str(out)
    task.automated, task.prompt, task.replace_identical = True, False, True
    try:
        if not unreal.Exporter.run_asset_export_task(task):
            return ""
        match = _MESSAGE_TEXT_RE.search(out.read_text(encoding="utf-8", errors="replace"))
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    text = _unescape_t3d(match.group(1)) if match else ""
    if ":" in path and ".Default___Root:" in path:  # a module-level message: its text is fixed
        _message_cache[path] = text
    return text


def _rarity_of_component(component) -> str:
    """"Exotic" from a rarity_component whose Rarity is .../Default__FortniteRarities:__verse_0x..._Exotic.
    Verse fields aren't Python properties, but the editor's ObjectToolset reads them; "" without it."""
    try:
        toolset = unreal.ObjectToolset.get_default_object()
        value = json.loads(toolset.call_method("GetProperties", (component, ["rarity"]))).get("rarity")
        path = value.get("refPath", "") if isinstance(value, dict) else ""
    except Exception:
        return ""
    name = path.rsplit("_", 1)[-1] if path else ""
    return name if name in RARITIES else ""


def _label(text: str, params, width: float = 0):
    IM.v_align(unreal.VerticalAlignment.V_ALIGN_CENTER)
    if width:
        IM.min_width(width)
    IM.text(text, params)


# --------------------------------------------------------------------------- editor window

class LootPoolEditor:
    FIELDS = {  # which property rows each node kind shows
        "pool": ["label", "rolls"],
        "group": ["label", "weight", "max", "guaranteed", "rolls"],
        "item": ["entity", "label", "qty", "weight", "max"],
    }
    LABELS = {"label": "Label", "entity": "Entity Prefab", "qty": "Quantity", "weight": "Weight",
              "max": "Max selections", "rolls": "Rolls", "guaranteed": "Guaranteed (bundle)"}
    HINT = ("Weight: blank = default, number, or Inf (always picked).\n"
            "Max selections: how often this entry can be picked per roll set.\n"
            "Rolls: how many picks the pool makes (blank = 1).\n"
            "Bundle: every child drops once, e.g. a gun + its ammo.\n"
            "Chance: share of the parent's picks, assuming a default weight of 1.")

    def __init__(self):
        self.style = _Style()
        self.doc = new_document()
        self.path: Path | None = None
        self.active = False       # locked until New or Open Verse
        self.selected: dict | None = None
        self.picker: dict | None = None    # entity picker state while it is open
        self.preview: str | None = None    # generated Verse while the preview is open
        self._later = []
        self._row_params = {}     # id(node) -> SlateIMTableRowChildrenParams
        self.settings = load_settings()
        self._del_was_down, self._del_press = False, None  # Del key tracking, see _draw_main
        self._recent_cache = None
        self._recent_rows = None  # rows the Recent dropdown showed last frame

        self._digest_stamp, self._digest_checked = None, 0.0
        self._load_entities()

    def _digest_files(self):
        return find_fortnite_digest(), find_project_digest()

    @staticmethod
    def _stamp(files):
        return tuple((str(f), f.stat().st_mtime) if f else None for f in files)

    def _load_entities(self):
        digest, project_digest = files = self._digest_files()
        self._digest_stamp = self._stamp(files)
        self.entities, self.flags, item_classes = load_digest_entities(digest) if digest else ([], {}, set())
        (self.custom_paths, self.project_root, custom_flags, self.custom_items, self.custom_classes,
         self.custom_parents) = (load_project_entities(project_digest, item_classes) if project_digest
                                 else ({}, "", {}, set(), {}, {}))
        self.flags.update(custom_flags)  # {name: (status, message)}: @deprecated / @experimental
        self.entity_module = dict(self.entities)  # Fortnite name -> "Items" / "Weapons"
        self.entities = sorted(self.entities + [(n, "Custom") for n in self.custom_paths])
        self.entity_names = {n for n, _ in self.entities}
        self._defaults = {}  # name -> (icon Texture2D or None, is item, rarity, checked at); see _defaults_info
        self._display_names = {}  # name -> (text, checked at); see display_name
        self.status = (f"{len(self.entities) - len(self.custom_paths)} Fortnite + {len(self.custom_paths)} custom entity prefabs"
                       if digest else "Fortnite.digest.verse not found: build Verse in UEFN once")

    def _defaults_info(self, name: str):
        """(icon texture or None, has an item_component, rarity name or "") from the entity's class
        defaults, cached. The rarity is the rarity_component's Rarity, read through the editor's
        ObjectToolset (Python can't read Verse fields directly).
        Loading the class builds its default object, whose subobjects are its components:
          inherited from a parent:  Default__X:__verse_0x..._ItemComponent, Default__X:Assets_texture_0
                                    (the icon's texture, in a wrapper Python can read)
          added in the prefab editor: Default__X:item_component_0, Default__X:icon_component_0.Assets_texture_0
          (likewise __verse_0x..._RarityComponent / rarity_component_0)
        Fortnite entity classes live in /EntityItems/_Verse as "<Module>-<Name>". Prefabs without an
        icon or item component are re-checked every few seconds, so ones added later show up."""
        cached = self._defaults.get(name)
        if cached and (all(cached[:3]) or time.monotonic() - cached[3] < 5.0):
            return cached[:3]
        texture, is_item, rarity = None, False, ""
        if name in self.custom_classes:  # /MyIsland/Prefabs/Gun.Gun_C
            class_path = self.custom_classes[name]
            package, class_name = class_path.rsplit(".", 1)
            defaults = f"{package}.Default__{class_name}"
        elif name in self.entity_module:
            class_path = f"/EntityItems/_Verse.{self.entity_module[name]}-{name}"
            defaults = f"/EntityItems/_Verse.Default__{self.entity_module[name]}-{name}"
        else:
            class_path = None
        if class_path:
            try:
                cls = unreal.load_object(None, class_path)
                if cls:
                    unreal.get_default_object(cls)  # makes sure the defaults and their subobjects exist
                    for path in [f"{defaults}:Assets_texture_0"] + [
                            f"{defaults}:icon_component_{i}.Assets_texture_0" for i in range(4)]:
                        wrapper = unreal.find_object(None, path)
                        texture = wrapper.get_editor_property("asset_for_editor") if wrapper else None
                        if texture:
                            break
                    is_item = any(unreal.find_object(None, path) for path in
                                  [f"{defaults}:{ITEM_COMPONENT}"] +
                                  [f"{defaults}:item_component_{i}" for i in range(4)])
                    component = next(filter(None, (unreal.find_object(None, path) for path in
                                                   [f"{defaults}:{RARITY_COMPONENT}"] +
                                                   [f"{defaults}:rarity_component_{i}" for i in range(4)])), None)
                    rarity = _rarity_of_component(component) if component else ""
            except Exception:
                pass  # unknown is fine; never break drawing over it
        self._defaults[name] = (texture, is_item, rarity, time.monotonic())
        return texture, is_item, rarity

    def display_name(self, name: str, _depth: int = 0) -> str:
        """The item name a custom prefab shows in game: its description_component's Name. If it has
        none, or leaves it empty (not overriding), the parent's: another project prefab, or the
        Fortnite item it extends. "" if nothing has one. Cached; re-checked every few seconds, so a
        name typed in the prefab editor shows up once the prefab is compiled."""
        cached = self._display_names.get(name)
        if cached and time.monotonic() - cached[1] < 5.0:
            return cached[0]
        text = ""
        if unreal is not None and _depth < 8:
            try:
                if name in self.custom_classes:  # /MyIsland/Prefabs/Gun.Gun_C
                    class_path = self.custom_classes[name]
                    package, class_name = class_path.rsplit(".", 1)
                    text = _description_name(class_path, f"{package}.Default__{class_name}")
                    if not text and name in self.custom_parents:
                        text = self.display_name(self.custom_parents[name], _depth + 1)
                elif name in self.entity_module:  # a Fortnite item, as a prefab's parent
                    module = self.entity_module[name]
                    class_path = f"/EntityItems/_Verse.{module}-{name}"
                    defaults = f"/EntityItems/_Verse.Default__{module}-{name}"
                    text = _description_name(class_path, defaults) or _item_definition_name(defaults)
            except Exception:
                text = ""
        self._display_names[name] = (text, time.monotonic())
        return text

    def rarity(self, name: str) -> str:
        """From the rarity_component; the name's _Rarity suffix if that can't be read."""
        return self._defaults_info(name)[2] or rarity_of(name)

    def icon_texture(self, name: str):
        """The entity's icon texture, or None."""
        return self._defaults_info(name)[0]

    def _draw_icon(self, name: str, large: bool = False):
        """Entity icon, or an empty square of the same size so text stays aligned."""
        s = self.style
        texture = self.icon_texture(name) if name else None
        IM.v_align(unreal.VerticalAlignment.V_ALIGN_CENTER)
        IM.padding(s.gap)
        if texture:
            IM.image(texture, s.icon_large if large else s.icon_small)
        else:
            IM.spacer(s.icon_large_size if large else s.icon_small_size)

    def _draw_entity_label(self, name: str, text: str, params):
        """Icon + text in one table cell. SlateIM text always wraps to its slot, and in a table cell the
        slot keeps the width of the text drawn there before (rows' widgets are reused after an edit or
        a move), so new, longer text would wrap. A minimum width from the text's length prevents that;
        a column narrower than the text clips it instead of wrapping."""
        IM.begin_horizontal_stack()
        try:
            self._draw_icon(name)
            _label(text, params, _text_width(text))
        finally:
            IM.end_horizontal_stack()

    def is_unknown(self, name: str) -> bool:
        """Not a Fortnite item/weapon or a project prefab, e.g. typed into the Entity field by hand.
        Verse won't build with it. Nothing is unknown until the digests have loaded."""
        return bool(self.entities) and name.strip() not in self.entity_names

    def unknown_entities(self) -> list:
        names, stack = [], list(self.doc["pools"]) if self.doc else []
        while stack:
            node = stack.pop()
            stack += node["children"]
            if node["kind"] == "item" and self.is_unknown(node["entity"]) and node["entity"] not in names:
                names.append(node["entity"])
        return sorted(names)

    def maybe_not_item(self, name: str) -> bool:
        """Project prefab that isn't confirmed to be an item: shown in yellow with a note.
        Confirmed = parented to a Fortnite item/weapon (digest) or has an item_component."""
        return (name in self.custom_paths and name not in self.custom_items
                and not self._defaults_info(name)[1])

    def reload_entities_if_changed(self):
        """Pick up new prefabs after a Verse build: the digests are rewritten then. Checked every 2 s."""
        now = time.monotonic()
        if now - self._digest_checked < 2.0:
            return
        self._digest_checked = now
        try:
            changed = self._stamp(self._digest_files()) != self._digest_stamp
        except OSError:
            return  # a digest being rewritten right now; try again next time
        if changed:
            self._load_entities()
            if self.picker is not None:
                self.picker["key"] = None  # refilter the open picker
            self.status = "Entity prefabs reloaded after a Verse build: " + self.status

    # ---- frame
    def later(self, action):
        """Run after this frame's windows are closed off: dialogs must not open mid-frame."""
        self._later.append(action)

    def frame(self) -> bool:
        """Draw every window once. False when the user closed the main window."""
        self._frame = getattr(self, "_frame", 0) + 1
        self._row_slot = 0
        is_open = IM.begin_window_root(MAIN_ID, self.style.main)
        try:
            if is_open:
                self._draw_main()
        finally:
            IM.end_root()
        if self.picker is not None:
            self._draw_picker()
        if self.preview is not None:
            self._draw_preview()
        return is_open

    def run_later(self):
        actions, self._later = self._later, []
        for action in actions:
            try:
                action()
            except Exception as exc:  # report, keep the editor open
                unreal.log_error(f"[LootPoolEditor] {traceback.format_exc()}")
                self.message("Loot Pool Editor", f"Something went wrong:\n\n{exc}", error=True)

    # ---- dialogs (only from later actions)
    def message(self, title: str, text: str, error: bool = False):
        params = unreal.SlateIMModalDialogParams()
        params.title = title
        params.category = unreal.AppMsgCategory.ERROR if error else unreal.AppMsgCategory.INFO
        IM.modal_dialog(unreal.AppMsgType.OK, text, params)

    def confirm(self, title: str, text: str) -> bool:
        params = unreal.SlateIMModalDialogParams()
        params.title = title
        return IM.modal_dialog(unreal.AppMsgType.YES_NO, text, params) == unreal.AppReturnType.YES

    def _text_box(self, slot, owner, value: str, params):
        """IM.editable_text that only reports edits meant for `owner`. SlateIM reuses a text box for
        whatever is drawn in its place next, and a focused box hands back its old text (on focus loss
        or as a late change) after the selection or row order changed: typing in Armored Wall's
        Entity and clicking another row wrote "ArmoredWall..." into that row. So for a couple of frames
        after a box starts showing a different owner, whatever it reports is ignored. A focused box
        also ignores the new value and keeps showing its own text, so on that first frame a spacer is
        drawn instead: SlateIM drops the old box (focus and stale text with it) and builds a fresh one."""
        if not hasattr(self, "_boxes"):
            self._boxes = {}
        previous = self._boxes.get(slot)
        if previous is None or previous[0] is not owner:
            self._boxes[slot] = (owner, self._frame + 2)
            IM.spacer(self.style.no_size)
            return None
        changed = IM.editable_text(value, params)
        if changed is None or self._frame <= previous[1] or changed == value:
            return None
        return changed

    # ---- main window
    def _draw_main(self):
        s = self.style
        IM.maximize()
        IM.begin_vertical_stack()
        try:
            if self.picker is not None:  # the picker acts as a modal: nothing else changes meanwhile
                IM.begin_disabled_state()
            try:
                self._draw_menu_bar()
                self._draw_toolbar()
                IM.maximize()  # fill the window both ways; a plain fill() only stretches vertically
                IM.begin_splitter("LootPoolBody", unreal.Orientation.ORIENT_HORIZONTAL)
                try:
                    IM.splitter_size_coefficient(0.62)
                    self._draw_tree()
                    IM.splitter_size_coefficient(0.38)
                    self._draw_side_panel()
                finally:
                    IM.end_splitter()
            finally:
                if self.picker is not None:
                    IM.end_disabled_state()
            IM.padding(s.row)
            _wide()
            IM.text(self.status, s.muted)
        finally:
            IM.end_vertical_stack()
        # Del on the tree. SlateIM's key event alone isn't enough: text fields keep Del (good), but
        # after the confirm dialog took the key-up, SlateIM can report Del as pressed again on the next
        # click, and holding the key auto-repeats. So it also needs a real press read from Windows,
        # each used once: holding or clicking around can't bring the prompt back.
        down, now = _key_down(VK_DELETE), time.monotonic()
        if down and not self._del_was_down:
            self._del_press = now
        self._del_was_down = down
        if IM.is_key_pressed(s.delete_key) and self._del_press and now - self._del_press < 0.3 \
                and self.active and self.selected and self.picker is None:
            self._del_press = None
            self.later(self.cmd_delete)

    def _draw_menu_bar(self):
        s = self.style
        IM.begin_menu_bar()
        try:
            IM.add_menu_bar_entry("File")
            try:
                for text, action, enabled in (("New", self.cmd_new, True), ("Open Verse…", self.cmd_open, True)):
                    if IM.add_menu_button(text, s.menu) and enabled:
                        self.later(action)
                IM.begin_sub_menu("Recent")
                try:
                    recent = self._recent()
                    for path in recent:
                        if IM.add_menu_button(f"{path.name}    ({self._where(path)})", s.menu):
                            self.later(lambda p=path: self._open_path(p))
                    if not recent:
                        IM.add_menu_button("No recent files", s.menu)
                    else:
                        IM.add_menu_separator()
                        if IM.add_menu_button("Clear recent files", s.menu):
                            self.later(self._clear_recent)
                finally:
                    IM.end_sub_menu()
                IM.add_menu_separator()
                for text, action in (("Save", self.cmd_save), ("Save As…", self.cmd_save_as),
                                     ("Preview Verse", self.cmd_preview)):
                    if IM.add_menu_button(text, s.menu):
                        self.later(action)
                IM.add_menu_separator()
                if IM.add_menu_button("Close", s.menu):
                    self.later(close_editor)
            finally:
                IM.end_menu_bar_entry()
        finally:
            IM.end_menu_bar()

    def _draw_toolbar(self):
        s = self.style
        # file commands first, the class name row under them
        IM.padding(s.row)
        IM.begin_horizontal_stack()
        try:
            for text, action in (("New", self.cmd_new), ("Open Verse…", self.cmd_open)):
                if _button(text, s):
                    self.later(action)
            self._draw_recent_combo()
            for text, action, enabled in (("Save", self.cmd_save, self.active),
                                          ("Save As…", self.cmd_save_as, self.active)):
                if _button(text, s, enabled):
                    self.later(action)
        finally:
            IM.end_horizontal_stack()

        IM.padding(s.row)
        IM.begin_horizontal_stack()
        try:
            _label("Lootpool class name", s.text)
            IM.padding(s.gap)
            if not self.active:
                IM.begin_disabled_state()
            try:
                IM.min_width(240)
                IM.v_align(unreal.VerticalAlignment.V_ALIGN_CENTER)
                changed = self._text_box("class", self.doc if self.active else None,
                                         self.doc["class_name"] if self.active else "", s.edit)
                if changed is not None and self.active:
                    self.doc["class_name"] = changed
            finally:
                if not self.active:
                    IM.end_disabled_state()
        finally:
            IM.end_horizontal_stack()

    def _draw_recent_combo(self):
        """Recent files dropdown. It also shows which file is open: that row
        is the selected one (or "(new, not saved yet)"). With nothing open no row is selected (-1),
        so picking any row counts as a pick."""
        s = self.style
        files = list(self._recent())
        current = None
        if self.active and self.path:
            key = os.path.normcase(str(self.path.resolve()))
            current = next((i for i, p in enumerate(files) if os.path.normcase(str(p)) == key), None)
            if current is None:  # open file missing from the list (e.g. after Clear recent files)
                files.insert(0, self.path)
                current = 0
        rows = [f"{p.name}    ({self._where(p)})" for p in files]
        if self.active and not self.path:
            rows.insert(0, "(new, not saved yet)")
            current = 0
        first_file = 1 if self.active and not self.path else 0
        rows.append("Clear recent files" if files else "No recent files")
        refresh = rows != self._recent_rows  # the combo keeps its old rows unless told to refresh
        self._recent_rows = rows
        IM.padding(s.gap)
        IM.v_align(unreal.VerticalAlignment.V_ALIGN_CENTER)
        IM.min_width(260)
        picked = IM.combo_box(rows, -1 if current is None else current,
                              s.combo_refresh if refresh else s.combo)
        if picked is None or picked == current:
            return
        index = picked - first_file
        if 0 <= index < len(files):
            self.later(lambda p=files[index]: self._open_path(p))
        elif index == len(files) and files:
            self.later(self._clear_recent)

    # ---- tree
    def _draw_tree(self):
        s = self.style
        if not self.active:
            IM.text("Press New, Open Verse… or File > Recent to start.", s.muted)
            return
        IM.begin_table(s.table)
        try:
            for column_id, params, width in s.tree_columns:
                IM.initial_table_column_width(width)
                IM.add_table_column(column_id, params)
            self._draw_rows(self.doc["pools"], False)
        finally:
            IM.end_table()

    def _draw_rows(self, nodes, guaranteed):
        s = self.style
        for node, weight, chance, count in row_info(nodes, guaranteed):
            selected = node is self.selected
            cell = IM.next_table_cell(selected)
            if cell is not None:
                if cell[0] and not selected:
                    self.selected = node
                title, params = node_title(node), s.text
                if node["kind"] == "item":
                    if self.is_unknown(node["entity"]):
                        title, params = f"❓ {title}", s.blocked
                    elif node["entity"] in self.flags:
                        title, params = f"⚠ {title}", s.blocked
                    elif self.maybe_not_item(node["entity"]):
                        params = s.custom
                if node["kind"] == "item":
                    self._draw_entity_label(node["entity"], title, params)
                else:  # same reused-widget wrapping as entity labels
                    IM.min_width(_text_width(title))
                    IM.text(title, params)
            if IM.next_table_cell(selected) is not None:
                if node["kind"] == "pool" or guaranteed:  # pools have no weight; bundle children are Inf
                    IM.text(weight, s.text)
                else:  # edit the weight right in the tree
                    IM.maximize()
                    self._row_slot += 1
                    changed = self._text_box(("weight", self._row_slot), node, node["weight"], s.weight_edit)
                    if changed is not None:
                        node["weight"] = changed.strip()
            for value in (chance, count):
                if IM.next_table_cell(selected) is not None:
                    IM.text(value, s.text)
            if node["kind"] != "item" and node["children"]:
                expanded = IM.begin_table_row_children(self._children_params(node))
                try:
                    if expanded:
                        self._draw_rows(node["children"], node["kind"] == "group" and node["guaranteed"])
                finally:
                    IM.end_table_row_children()

    def _children_params(self, node):
        params = self._row_params.get(id(node))
        if params is None:
            params = unreal.SlateIMTableRowChildrenParams()
            params.parent_row_id = id(node) & 0x7FFFFFFF  # keeps each row's expanded state
            params.default_expanded = True
            self._row_params[id(node)] = params
        return params

    # ---- side panel
    def _draw_side_panel(self):
        s = self.style
        IM.padding(s.panel)
        IM.maximize()
        IM.begin_vertical_stack()
        try:
            IM.text("Add / arrange", s.muted)
            buttons = [("+ Source Pool", self.cmd_add_pool), ("+ Group", lambda: self.cmd_add("group")),
                       ("+ Bundle", lambda: self.cmd_add("bundle")), ("+ Item", lambda: self.cmd_add("item")),
                       ("Duplicate", self.cmd_duplicate), ("Delete", self.cmd_delete),
                       ("Move up", lambda: self.cmd_move(-1)), ("Move down", lambda: self.cmd_move(1))]
            _wide()
            IM.begin_horizontal_wrap()  # buttons flow onto more lines when the panel is narrow
            try:
                for text, action in buttons:
                    if _button(text, s, self.active):
                        self.later(action)
            finally:
                IM.end_horizontal_wrap()

            node = self.selected if self.active else None
            IM.padding(s.section)
            _wide()
            IM.text(f"Properties: {node['kind'].title()}" if node else "Properties", s.muted)
            if node and node["kind"] == "item":
                # always one widget here: SlateIM matches widgets by draw order, so an icon that
                # vanishes mid-typing would hand the Entity field's keyboard focus to the Label field
                if self.icon_texture(node["entity"]):
                    IM.padding(s.row)
                    self._draw_icon(node["entity"], large=True)
                else:
                    IM.spacer(s.no_size)
            if node:
                for key in self.FIELDS[node["kind"]]:
                    self._draw_field(node, key)
                flag = self.flags.get(node["entity"]) if node["kind"] == "item" else None
                if flag:
                    IM.padding(s.row)
                    _wide()
                    IM.text(f"⚠ {node['entity']}: {flag_warning(flag)}", s.blocked)
                if node["kind"] == "item" and self.is_unknown(node["entity"]):
                    IM.padding(s.row)
                    _wide()
                    IM.text(UNKNOWN_NOTE if node["entity"].strip() else "❓ No entity prefab: press Pick… to choose one.",
                            s.blocked)
                elif node["kind"] == "item" and self.maybe_not_item(node["entity"]):
                    IM.padding(s.row)
                    _wide()
                    IM.text(NOT_ITEM_NOTE, s.custom)
            else:
                _wide()
                IM.text("Select an entry in the tree to edit it." if self.active
                        else "Press New, Open Verse… or File > Recent to start.", s.text)
            IM.padding(s.section)
            _wide()
            IM.text(self.HINT, s.muted)
        finally:
            IM.end_vertical_stack()

    def _draw_field(self, node, key):
        s = self.style
        IM.padding(s.row)
        _wide()
        IM.begin_horizontal_stack()
        try:
            _label(self.LABELS[key], s.text, 130)
            if key == "guaranteed":
                changed = IM.check_box(bool(node[key]), s.check)
                if changed is not None:
                    node[key] = changed
                return
            IM.maximize()
            changed = self._text_box(("field", key), node, node[key], s.edit)
            if changed is not None:
                node[key] = changed
            if key == "entity" and _button("Pick…", s, bool(self.entities)):
                self.later(lambda: self.open_picker(lambda name: self._set_entity(node, name)))
        finally:
            IM.end_horizontal_stack()

    def _set_entity(self, node, name):
        node["entity"] = name
        if not node["label"]:
            node["label"] = self._label_for(name)

    # ---- entity picker
    def open_picker(self, on_pick):
        self.picker = {"search": "", "module": 0, "rarity": 0, "name": None, "on_pick": on_pick,
                       "row_click": None, "last_down": (0.0, (0, 0)), "mouse_was_down": False, "key": None, "shown": []}

    def is_invalid(self, name: str) -> bool:
        """Deprecated/experimental (blocks publishing) or a project entity not confirmed as an item."""
        return name in self.flags or self.maybe_not_item(name)

    def _picker_matches(self, pk):
        show_invalid = self.settings.get("picker_show_invalid", True)
        # filter only when the search changes, not every frame; while invalid entries are hidden, also
        # every few seconds, since a prefab can gain an item_component meanwhile
        key = (pk["search"], pk["module"], pk["rarity"], show_invalid,
               None if show_invalid else int(time.monotonic() // 3))
        if pk["key"] != key:
            words = pk["search"].lower().split()
            module = PICKER_MODULES[pk["module"]]
            rarity = (["All"] + RARITIES)[pk["rarity"]]
            pk["shown"] = [(name, mod) for name, mod in self.entities
                           if (module == "All" or mod == module)
                           and (rarity == "All" or self.rarity(name) == rarity)
                           and all(w in name.lower() for w in words)
                           and (show_invalid or not self.is_invalid(name))]
            pk["key"] = key
            if pk["name"] not in {name for name, _ in pk["shown"][:MAX_PICKER_ROWS]}:
                pk["name"] = None  # filtered out: Select must not pick an entity that isn't listed
        return pk["shown"]

    def _draw_picker(self):
        is_open = IM.begin_window_root(PICKER_ID, self.style.picker)
        try:
            if is_open:
                self._draw_picker_body(self.picker)
        finally:
            IM.end_root()
        if not is_open:
            self.picker = None

    def _draw_picker_body(self, pk):
        s = self.style
        IM.maximize()
        IM.begin_vertical_stack()
        try:
            IM.padding(s.row)
            IM.begin_horizontal_stack()
            try:
                IM.fill()
                changed = IM.editable_text(pk["search"], s.search)
                if changed is not None:
                    pk["search"] = changed
                IM.padding(s.gap)
                changed = IM.combo_box(PICKER_MODULES, pk["module"], s.combo)
                if changed is not None:
                    pk["module"] = changed
                changed = IM.combo_box(["All"] + RARITIES, pk["rarity"], s.combo)
                if changed is not None:
                    pk["rarity"] = changed
                IM.padding(s.gap)
                IM.v_align(unreal.VerticalAlignment.V_ALIGN_CENTER)
                changed = IM.check_box(self.settings.get("picker_show_invalid", True), s.check)
                if changed is not None:
                    self.settings["picker_show_invalid"] = bool(changed)
                    save_settings(self.settings)
                _label("Show invalid", s.text)
            finally:
                IM.end_horizontal_stack()

            shown = self._picker_matches(pk)
            self._picker_double_click(pk)
            IM.fill()
            IM.begin_table(s.table)
            try:
                for column_id, params, width in s.picker_columns:
                    IM.initial_table_column_width(width)
                    IM.add_table_column(column_id, params)
                if not shown:  # SlateIM keeps the previous rows when a table gets none
                    if IM.next_table_cell(False) is not None:
                        IM.text("No entity prefabs match these filters", s.muted)
                    if IM.next_table_cell(False) is not None:
                        IM.text("", s.muted)
                for name, module in shown[:MAX_PICKER_ROWS]:
                    selected = name == pk["name"]
                    cell = IM.next_table_cell(selected)
                    if cell is not None:
                        is_selected, pressed = cell
                        if is_selected and not selected:
                            pk["name"] = name
                        if pressed:  # where this row was clicked, for _picker_double_click
                            pk["row_click"] = (name, pk["last_down"][1])
                        self._draw_entity_label(name, name, s.blocked if name in self.flags else
                                                s.custom if self.maybe_not_item(name) else s.text)
                    if IM.next_table_cell(selected) is not None:
                        IM.text(module, s.muted)
            finally:
                IM.end_table()

            name = pk["name"]
            if name:  # preview of the selected entity
                IM.padding(s.row)
                IM.begin_horizontal_stack()
                try:
                    self._draw_icon(name, large=True)
                    subtitle = (self.display_name(name) or "Custom prefab") if name in self.custom_paths \
                        else pretty_name(name)
                    text = f"{name}\n{subtitle}"
                    # SlateIM text wraps at the width the previous selection's text had; a minimum width
                    # from the longest line stops that (capped, so a very long name still wraps in the window)
                    _label(text, s.text, min(max(_text_width(line) for line in text.split("\n")), 460))
                finally:
                    IM.end_horizontal_stack()
            if name in self.flags:
                IM.padding(s.row)
                _wide()
                IM.text(f"⚠ {name}: {flag_warning(self.flags[name])}", s.blocked)
            if self.maybe_not_item(name):
                IM.padding(s.row)
                _wide()
                IM.text(NOT_ITEM_NOTE, s.custom)

            IM.padding(s.row)
            IM.begin_horizontal_stack()
            try:
                more = f" (first {MAX_PICKER_ROWS} shown, search to narrow)" if len(shown) > MAX_PICKER_ROWS else ""
                IM.fill()
                count = f"{len(shown)} of {len(self.entities)}{more}"
                _label(count, s.muted, _text_width(count))
                if _button("Select", s, name is not None):
                    self.later(self._finish_pick)
                if _button("Cancel", s):
                    self.later(self._cancel_pick)
            finally:
                IM.end_horizontal_stack()
        finally:
            IM.end_vertical_stack()

    def _picker_double_click(self, pk):
        """Pick on a double-click on the selected row. SlateIM only reports a row press when the click
        changes the selection, so the second click is read from the mouse button itself: two clicks
        within the Windows double-click time and a few pixels of each other, on the selected row.
        "On the row" = within half a row height of where it was clicked (rows are ~32 px tall), so the
        mouse may drift between selecting a row and double-clicking it later."""
        down = _key_down(VK_LBUTTON)
        if down and not pk["mouse_was_down"]:
            now, pos = time.monotonic(), _cursor_pos()
            last_time, last_pos = pk["last_down"]
            near = lambda a, b: abs(a[0] - b[0]) <= 4 and abs(a[1] - b[1]) <= 4
            row = pk["row_click"]
            on_row = (row and row[0] == pk["name"] and abs(pos[1] - row[1][1]) <= 14
                      and abs(pos[0] - row[1][0]) <= 300 and _foreground_title() == PICKER_TITLE)
            if on_row and near(pos, last_pos) and now - last_time <= _double_click_seconds():
                self.later(self._finish_pick)
            elif on_row:  # follow the mouse along the row (a click that changes rows re-anchors anyway)
                pk["row_click"] = (row[0], pos)
            pk["last_down"] = (now, pos)
        pk["mouse_was_down"] = down

    def _finish_pick(self):
        pk, self.picker = self.picker, None
        if pk and pk["name"]:
            pk["on_pick"](pk["name"])

    def _cancel_pick(self):
        self.picker = None

    # ---- preview
    def _draw_preview(self):
        s = self.style
        is_open = IM.begin_window_root(PREVIEW_ID, s.preview)
        try:
            if is_open:
                IM.maximize()
                IM.begin_vertical_stack()
                try:
                    IM.padding(s.row)
                    IM.begin_horizontal_stack()
                    try:
                        if _button("Copy to clipboard", s):
                            self.later(self._copy_preview)
                    finally:
                        IM.end_horizontal_stack()
                    IM.fill()
                    IM.begin_scroll_box(s.scroll)
                    try:
                        IM.text(self.preview, s.text)
                    finally:
                        IM.end_scroll_box()
                finally:
                    IM.end_vertical_stack()
        finally:
            IM.end_root()
        if not is_open:
            self.preview = None

    def _copy_preview(self):
        if self.preview is not None:
            self.status = "Copied the Verse to the clipboard" if copy_to_clipboard(self.preview) \
                else "Couldn't open the clipboard"

    # ---- commands (run through later(), outside the frame)
    def _parent_of(self, target):
        """(list holding target, parent node or None)."""
        stack = [(self.doc["pools"], None)]
        while stack:
            siblings, parent = stack.pop()
            for node in siblings:
                if node is target:
                    return siblings, parent
                stack.append((node["children"], node))
        return None, None

    def cmd_add_pool(self):
        node = new_node("pool", label=f"Pool {len(self.doc['pools']) + 1}")
        self.doc["pools"].append(node)
        self.selected = node

    def cmd_add(self, what):
        target = self.selected
        if not target:
            self.status = "Select a Source Pool or Group to add into"
            return
        if target["kind"] == "item":  # add next to the item, inside its parent
            _, target = self._parent_of(target)
            if not target:
                return
        if what == "item":
            if not self.entities:  # no digest: add a blank item to fill in by hand
                node = new_node("item")
            else:  # pick the entity first; the item is only added once one is chosen
                def add_picked(name, parent=target):
                    item = new_node("item", entity=name, label=self._label_for(name))
                    parent["children"].append(item)
                    self.selected = item
                    self.status = f"Added {name}"
                self.open_picker(add_picked)
                return
        else:
            node = new_node("group", label="New bundle" if what == "bundle" else "New group",
                            guaranteed=(what == "bundle"))
        target["children"].append(node)
        self.selected = node

    def cmd_duplicate(self):
        siblings, _ = self._parent_of(self.selected)
        if siblings is not None:
            clone = copy.deepcopy(self.selected)
            siblings.insert(siblings.index(self.selected) + 1, clone)
            self.selected = clone

    def cmd_delete(self):
        node = self.selected
        siblings, _ = self._parent_of(node)
        if siblings is not None and self.confirm("Delete", f"Delete '{node_title(node)}' and its contents?"):
            siblings.remove(node)
            self.selected = None

    def cmd_move(self, delta):
        siblings, _ = self._parent_of(self.selected)
        if siblings is None:
            return
        i = siblings.index(self.selected)
        j = i + delta
        if 0 <= j < len(siblings):
            siblings[i], siblings[j] = siblings[j], siblings[i]

    def _label_for(self, entity: str) -> str:
        return entity if entity in self.custom_paths else pretty_name(entity)

    def _extra_usings(self) -> list[str]:
        """Module paths of custom entities used in the pool. A file in the project root module needs
        no using for root-level prefabs; anything in a subfolder module (or a file saved in a
        subfolder) does."""
        in_root = self.path is None or self.path.parent.resolve() == CONTENT_DIR.resolve()
        paths, stack = [], list(self.doc["pools"])
        while stack:
            node = stack.pop()
            stack += node["children"]
            path = self.custom_paths.get(node["entity"]) if node["kind"] == "item" else None
            if path and not (in_root and path == self.project_root):
                path = self._dotted_module(path)
                if path not in paths:
                    paths.append(path)
        return sorted(paths)

    def _dotted_module(self, path: str) -> str:
        """/you@fortnite.com/MyIsland/ItemPrefabs -> MyIsland.ItemPrefabs (project-relative form)."""
        root = self.project_root.rstrip("/")
        if not root or not (path == root or path.startswith(root + "/")):
            return path  # not inside this project: keep the full path
        return ".".join([root.rsplit("/", 1)[-1]] + [p for p in path[len(root):].split("/") if p])

    def _generate(self) -> str:
        return generate_verse(self.doc, self._extra_usings())

    def _set_active(self, doc: dict, path: Path | None):
        self.doc, self.path, self.active, self.selected = doc, path, True, None
        self._row_params.clear()

    def cmd_new(self):
        if self.active and not self.confirm("New", "Discard the current loot pool?"):
            return
        self._set_active(new_document(), None)
        self.status = "New loot pool: Save As… to choose where the .verse file goes"

    def cmd_open(self):
        path = file_dialog(False, "Open Verse loot pool", CONTENT_DIR)
        if path:
            self._open_path(path)

    def _open_path(self, path: Path):
        try:
            doc = import_verse(path.read_text(encoding="utf-8"))
        except (LootPoolError, OSError) as exc:
            return self.message("Open failed", f"{path.name}\n\n{exc}", error=True)
        self._set_active(doc, path)
        self._remember(path)
        self.status = f"Opened {path}"

    def cmd_save(self):
        if not self.active:
            return
        if not self.path:
            return self.cmd_save_as()
        try:
            text = self._generate()
        except LootPoolError as exc:
            return self.message("Can't save", str(exc), error=True)
        unknown = self.unknown_entities()
        if unknown and not self.confirm("Unknown entity prefabs", (
                "These entity prefabs aren't Fortnite items/weapons or prefabs in this project, so Verse "
                "won't build:\n\n" + "\n".join(unknown[:15]) + ("\n…" if len(unknown) > 15 else "") +
                "\n\nSave anyway?")):
            return
        self.path.write_text(text, encoding="utf-8")
        self._remember(self.path)
        self.status = f"Saved {self.path}. Build Verse in UEFN to compile it."
        unreal.log(f"[LootPoolEditor] wrote {self.path}")

    def cmd_save_as(self):
        if not self.active:
            return
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", self.doc["class_name"]).lower() + ".verse"
        path = file_dialog(True, "Save loot pool as", self.path.parent if self.path else CONTENT_DIR, name)
        if path:
            self.path = path
            self.cmd_save()

    def cmd_preview(self):
        if not self.active:
            return
        try:
            self.preview = self._generate()
        except LootPoolError as exc:
            self.message("Can't generate", str(exc), error=True)

    # ---- recent files: one list per project, kept in the per-user settings file (not in the
    # project, so it never ends up in revision control), keyed by the project folder
    def _project_recent(self) -> list:
        lists = self.settings.setdefault("recent_by_project", {})
        old = self.settings.pop("recent", None)
        if old:  # older versions kept one list for every project: file each path under its project
            for p in old:
                key = _project_key_of(Path(p))
                if key and p not in lists.setdefault(key, []):
                    lists[key].append(p)
            save_settings(self.settings)
        return lists.setdefault(PROJECT_KEY, [])

    def _recent(self) -> list[Path]:
        """This project's recent files that still exist, newest first. Checked on disk at most once a
        second, since the window asks every frame."""
        now = time.monotonic()
        if self._recent_cache is None or now - self._recent_cache[0] > 1.0:
            files = [Path(p) for p in self._project_recent() if Path(p).is_file()
                     and _project_key_of(Path(p)) == PROJECT_KEY]  # never another project's file
            self._recent_cache = (now, files)
        return self._recent_cache[1]

    def _remember(self, path: Path):
        path = str(path.resolve())
        if _project_key_of(Path(path)) != PROJECT_KEY:
            return  # saved outside this project: not something to reopen from this project's list
        recent = [path] + [p for p in self._project_recent() if os.path.normcase(p) != os.path.normcase(path)]
        self.settings["recent_by_project"][PROJECT_KEY] = recent[:MAX_RECENT]
        self._recent_cache = None
        save_settings(self.settings)

    def _clear_recent(self):
        self._project_recent()
        self.settings["recent_by_project"][PROJECT_KEY] = []
        self._recent_cache = None
        save_settings(self.settings)

    @staticmethod
    def _where(path: Path) -> str:
        """Where the file sits inside this project's Content folder."""
        try:
            where = path.parent.relative_to(CONTENT_DIR).as_posix()
            return "Content" + ("/" + where if where != "." else "")
        except ValueError:
            return str(path.parent)


# --------------------------------------------------------------------------- launch

_editor: LootPoolEditor | None = None
_tick_handle = None


def is_editor_open() -> bool:
    return _editor is not None


def _tick(_delta):
    editor = _editor
    if editor is None or not IM.can_update_slate_im():  # e.g. while a SlateIM dialog is open
        return
    try:
        still_open = editor.frame()
    except Exception:
        unreal.log_error(f"[LootPoolEditor] closed after an error:\n{traceback.format_exc()}")
        close_editor()
        return
    if not still_open:
        close_editor()
        return
    editor.run_later()
    editor.reload_entities_if_changed()


def open_editor():
    """Open the window. It is redrawn from the editor's Slate tick until closed."""
    global _editor, _tick_handle
    if unreal is None:
        raise RuntimeError("The Loot Pool Editor window runs inside UEFN")
    if _editor is None:
        _editor = LootPoolEditor()
        _tick_handle = unreal.register_slate_post_tick_callback(_tick)
    return _editor


def close_editor():
    """Stop drawing: SlateIM closes windows that aren't drawn in a frame."""
    global _editor, _tick_handle
    if _tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(_tick_handle)
        _tick_handle = None
    _editor = None


def toggle_editor():
    close_editor() if is_editor_open() else open_editor()


# --------------------------------------------------------------------------- toolbar button

MENU_OWNER = "LootPoolEditor"
TOOLBAR_MENU = "LevelEditor.LevelEditorToolBar.ModesToolBar"
TOOLBAR_SECTION = "LootPool"
TOOLBAR_ENTRY = "LootPoolEditorToggle"
_toolbar_button = None  # keep the script object alive, or the button stops working

if unreal is not None:
    @unreal.uclass()
    class LootPoolToolbarToggle(unreal.ToolMenuEntryScript):
        """Toolbar toggle: pressed while the editor window is open, released when it closes."""

        @unreal.ufunction(override=True)
        def execute(self, context):
            toggle_editor()

        @unreal.ufunction(override=True)
        def can_execute(self, context):
            return True

        @unreal.ufunction(override=True)
        def get_check_state(self, context):
            return unreal.CheckBoxState.CHECKED if is_editor_open() else unreal.CheckBoxState.UNCHECKED


def register_menu():
    """Add the Loot Pools toggle to the Modes toolbar. Safe to call again (replaces itself)."""
    global _toolbar_button
    if unreal is None:
        return
    menus = unreal.ToolMenus.get()
    menus.unregister_owner_by_name(MENU_OWNER)
    toolbar = menus.find_menu(TOOLBAR_MENU)
    if not toolbar:
        unreal.log_warning(f"[LootPoolEditor] {TOOLBAR_MENU} not found; button not added")
        return

    button = LootPoolToolbarToggle()
    button.init_entry(MENU_OWNER, TOOLBAR_MENU, TOOLBAR_SECTION, TOOLBAR_ENTRY,
                      "Loot Pools", "Open or close the Loot Pool Editor")
    data = button.data
    data.icon = unreal.ScriptSlateIcon("EditorStyle", "Icons.Edit")
    advanced = data.advanced
    advanced.entry_type = unreal.MultiBlockType.TOOL_BAR_BUTTON
    advanced.user_interface_action_type = unreal.UserInterfaceActionType.TOGGLE_BUTTON
    advanced.style_name_override = "CalloutToolbar"  # slim toolbars hide labels; this style shows them
    data.advanced = advanced
    button.data = data
    toolbar.add_menu_entry_object(button)
    _toolbar_button = button
    menus.refresh_all_widgets()


if __name__ == "__main__":
    if unreal is not None:
        # Tools > Execute Python Script runs this file as a separate __main__ copy; go through the
        # importable module instead so the toolbar button sees the window as open.
        import loot_pool_editor
        loot_pool_editor.open_editor()
    else:
        open_editor()

# SPDX-License-Identifier: GPL-3.0-or-later
"""
Cast to Rig Converter - audition a large library of Cast-imported Actions on one
armature, retarget them onto a control rig, and save the bakes as Animation
Assets.

Built for the case where a Cast import dropped thousands of clips into a file
and the Action Editor's dropdown is no longer a usable way to find anything.

  3D Viewport > Sidebar (N) > "Cast to Rig" tab

WORKFLOW
  1. Browse   - click a clip to apply it to the source skeleton. The scene's
                frame range follows the clip, so playback is always the right
                length. Search by name, hide the single-frame poses, and step
                through with the arrow buttons.
  2. Name     - rename clips in place to catalogue them. The first rename
                stashes the original name in a "cast_hash" custom property, so a
                clip renamed to "crouch idle" can still be traced back to the
                file it came from - and search matches the stashed hash as well
                as the visible name. "Unnamed only" filters to the clips you
                haven't named yet, which is how you work through a library
                without losing your place.
  3. Retarget - the Retarget to Rig panel drives the source skeleton's motion
                onto the control rig and bakes it down to plain keyframes. Tick
                clips in the list to queue them and Batch Queued Clips runs the
                whole loop - bind, bake, unbind - over every one of them,
                skipping clips the rig already has a bake of.
  4. Promote  - Send Bake to Asset Browser marks that bake as an Animation
                Asset: catalog, description, tags and a rendered thumbnail. It
                appears in the Asset Browser under Current File and can be
                assigned to the rig from there with right-click > Assign
                Action. The Action is marked in place - nothing is copied, and
                it keeps its imported-name provenance.

Note this is an ANIMATION asset, not a pose asset: the entire multi-frame Action
is stored, not one frame of it. Blender's own Create Pose Asset button (Action
Editor, or the Pose menu in Pose Mode) still handles single-frame poses.

CREDITS
  The retarget proxy-bone construction and the two roll helpers are adapted from
  the "Retarget" add-on by KBS-DEV (GPL-3.0-or-later).
  The default bone map targets the control rig by LexDorkalv; see
  RETARGET_PAIRS below to adapt it to another rig.

LICENSE
  GPL-3.0-or-later. This add-on contains code derived from a GPL-3.0-or-later
  work, so it is distributed under the same terms.
"""

bl_info = {
    "name": "Cast to Rig Converter",
    "author": "Nezara",
    "version": (3, 0, 0),
    "blender": (4, 4, 0),
    "location": "3D Viewport > Sidebar (N) > Cast to Rig",
    "description": "Search and audition a large Cast Action library, retarget "
                   "clips onto a control rig, and save the bakes as Animation "
                   "Assets",
    "category": "Animation",
}

import glob
import os
import re
import tempfile
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

# name -> (start, end); rebuilt on demand because frame_range on thousands of
# actions is far too slow to touch during a UI redraw.
_RANGE_CACHE = {}

# The label field is written to programmatically whenever the selection changes.
# Without this guard that write would be read as the user renaming the clip.
_SUPPRESS_LABEL = False

HASH_PROP = "cast_hash"       # set on a clip that has been renamed
SOURCE_PROP = "source_clip"   # set on an asset, naming the clip it came from
QUEUE_PROP = "batch_queue"    # set on a clip ticked for a batch retarget
_RETIRED_FAV_PROP = "favourite"  # 2.x favourites; cleared, never read

# Display order is computed from the imported names and cached, so a rename
# doesn't move a row (see stable_order) and 3000+ clips don't get re-sorted on
# every redraw. Bumped whenever a name changes.
_ORDER_STAMP = 0
_ORDER_CACHE = {"key": None, "order": None}


def bump_order():
    global _ORDER_STAMP
    _ORDER_STAMP += 1


def original_name(action):
    """The name the clip was imported under, if it has since been renamed."""
    try:
        return action[HASH_PROP]
    except KeyError:
        return ""


def is_labelled(action):
    return bool(original_name(action))


def is_queued(action):
    """Clips ticked for the batch retarget.

    Stored on the Action rather than in a scene list, so the queue survives
    renaming, saving, and being applied to a different rig.

    Only the tick counts. 2.x favourites are deliberately NOT read as queued:
    a file with old stars in it would otherwise arrive with a batch queue
    nobody asked for.
    """
    return bool(action.get(QUEUE_PROP))


def is_asset(action):
    return action.asset_data is not None


_HEX_NAME = re.compile(r"^0x[0-9a-fA-F]{6,}(\.\d+)?$")


def clip_kind(action):
    """CAST (imported original), ASSET (promoted), BAKE (retarget output)
    or OTHER. Drives the row icon and what delete is willing to touch."""
    if is_asset(action):
        return "ASSET"
    # Checked before the hash test: a bake inherits its clip's cast_hash for
    # traceability, and without this ordering it would masquerade as an
    # imported original and become undeletable.
    if action.get("retarget_baked"):
        return "BAKE"
    if _HEX_NAME.match(action.name) or HASH_PROP in action.keys():
        return "CAST"
    return "OTHER"


def clip_protected(action):
    """Originals and anything deliberately promoted are never deleted.

    An imported clip is the one thing in this file that cannot be recreated -
    a bake can always be run again from it, an asset was a deliberate act.
    """
    return clip_kind(action) in {"CAST", "ASSET"}


def bake_made_from(clip):
    """Bakes produced from this Cast clip, newest-looking last.

    Matched on the clip name recorded at bake time, falling back to the
    imported hash: a clip renamed after it was baked no longer matches by
    name, and the hash is the one thing about it that never changes.
    """
    by_name, by_hash = [], []
    wanted = clip.get(HASH_PROP)
    for act in bpy.data.actions:
        if not act.get("retarget_baked"):
            continue
        if act.get("retarget_from") == clip.name:
            by_name.append(act)
        elif wanted and act.get(HASH_PROP) == wanted:
            by_hash.append(act)
    return sorted(by_name or by_hash, key=lambda a: a.name)


def bone_selected(pose_bone):
    """Bone.select was removed in Blender 5.x; selection lives on PoseBone now."""
    if hasattr(pose_bone, "select"):
        return pose_bone.select
    return pose_bone.bone.select


def set_bone_selected(pose_bone, value):
    if hasattr(pose_bone, "select"):
        pose_bone.select = value
    else:
        pose_bone.bone.select = value


def display_key(action):
    """Sort by the name the clip was IMPORTED under, not its current name.

    bpy.data.actions is kept sorted alphabetically, so renaming "0x158..." to
    "dive forward" physically moves it to the far end of the collection - and
    the list view jumps there, losing your place after every rename. Ordering by
    the imported name instead keeps every row exactly where it was.
    """
    return (original_name(action) or action.name).lower()


def display_order(actions):
    """org_index -> display position, in stable imported-name order."""
    stamp = (len(actions), _ORDER_STAMP)
    if _ORDER_CACHE["key"] == stamp and _ORDER_CACHE["order"] is not None:
        return _ORDER_CACHE["order"]

    ranked = sorted(range(len(actions)), key=lambda i: display_key(actions[i]))
    order = [0] * len(actions)
    for position, index in enumerate(ranked):
        order[index] = position

    _ORDER_CACHE["key"] = stamp
    _ORDER_CACHE["order"] = order
    return order


@persistent
def _on_file_load(_dummy):
    """Clip lengths belong to the file that was open; drop them on load."""
    _RANGE_CACHE.clear()


def rebuild_cache():
    _RANGE_CACHE.clear()
    for action in bpy.data.actions:
        try:
            start, end = action.frame_range
        except Exception:
            start, end = 0.0, 0.0
        _RANGE_CACHE[action.name] = (float(start), float(end))
    return len(_RANGE_CACHE)


def clip_span(action):
    entry = _RANGE_CACHE.get(action.name)
    if entry is None:
        try:
            entry = tuple(float(v) for v in action.frame_range)
        except Exception:
            entry = (0.0, 0.0)
        _RANGE_CACHE[action.name] = entry
    return entry


def is_static(action):
    start, end = clip_span(action)
    return (end - start) < 0.5


# ----------------------------------------------------------------------------
# Applying an action
# ----------------------------------------------------------------------------

def target_object(context):
    scene = context.scene
    obj = scene.anim_browser_target
    if obj and obj.type == "ARMATURE":
        return obj
    obj = context.active_object
    if obj and obj.type == "ARMATURE":
        return obj
    for candidate in context.scene.objects:
        if candidate.type == "ARMATURE":
            return candidate
    return None


def bind_slot(anim_data, action):
    """Bind the action's slot, or nothing evaluates.

    Blender 5's slotted actions carry a slot named after the ID they were
    made for - a bake stamps "OBRIG - Whatever.001". Auto-binding matches on
    that name, so appending an action into a file whose rig is named even
    slightly differently leaves the slot unbound: the action shows as
    assigned, and the rig sits in rest pose with nothing to say why.
    """
    if anim_data is None or action is None:
        return
    if not hasattr(action, "slots") or not action.slots:
        return
    if getattr(anim_data, "action_slot", None) is not None:
        return
    for slot in action.slots:
        if slot.target_id_type == "OBJECT":
            try:
                anim_data.action_slot = slot
            except (AttributeError, TypeError):
                pass
            return


def assign_action(obj, action):
    """Assign an action, coping with slotted actions on Blender 4.4+/5.x."""
    anim_data = obj.animation_data or obj.animation_data_create()
    anim_data.action = action
    bind_slot(anim_data, action)
    if not hasattr(anim_data, "action_slot"):
        return
    try:
        if anim_data.action_slot is not None:
            return
        suitable = getattr(anim_data, "action_suitable_slots", None)
        if suitable and len(suitable):
            anim_data.action_slot = suitable[0]
            return
        slots = getattr(action, "slots", None)
        if slots and len(slots):
            anim_data.action_slot = slots[0]
    except Exception:
        # An unassignable slot is not worth breaking the browse loop over.
        pass


def apply_index(context, index):
    actions = bpy.data.actions
    if not (0 <= index < len(actions)):
        return None
    action = actions[index]
    obj = target_object(context)
    if obj is None:
        return None

    assign_action(obj, action)
    scene = context.scene

    if scene.anim_browser_autorange:
        start, end = clip_span(action)
        scene.frame_start = int(round(start))
        scene.frame_end = max(int(round(end)), int(round(start)))
        if scene.frame_current < scene.frame_start or scene.frame_current > scene.frame_end:
            scene.frame_set(scene.frame_start)
    return action


def selected_action(context):
    """The clip the list is pointing at, whether or not it has been applied."""
    actions = bpy.data.actions
    index = context.scene.anim_browser_index
    if 0 <= index < len(actions):
        return actions[index]
    return None


def _set_label_quietly(scene, text):
    """Write the label field without it reading as a user-typed rename.

    Saves and restores rather than clearing, so nested calls (a rename that
    re-points the selection, which in turn refreshes the label) don't have the
    inner call switch suppression off while the outer one still needs it.
    """
    global _SUPPRESS_LABEL
    previous = _SUPPRESS_LABEL
    _SUPPRESS_LABEL = True
    try:
        scene.anim_browser_label = text
    finally:
        _SUPPRESS_LABEL = previous


def _on_index_change(self, context):
    action = apply_index(context, self.anim_browser_index)
    if action is not None:
        _set_label_quietly(self, action.name)


def rename_action(action, new_name):
    """Rename a clip, preserving its imported name and fixing up the cache.

    bpy.data.actions is kept sorted by name, so renaming reorders the collection
    and any held index goes stale - callers must re-find the action afterwards.
    """
    new_name = new_name.strip()
    if not new_name or new_name == action.name:
        return False
    if HASH_PROP not in action.keys():
        action[HASH_PROP] = action.name

    span = _RANGE_CACHE.pop(action.name, None)
    action.name = new_name                     # Blender may uniquify this
    if span is not None:
        _RANGE_CACHE[action.name] = span
    bump_order()
    return True


def _on_label_change(self, context):
    global _SUPPRESS_LABEL          # must precede any use of the name in scope
    if _SUPPRESS_LABEL:
        return
    actions = bpy.data.actions
    index = self.anim_browser_index
    if not (0 <= index < len(actions)):
        return
    action = actions[index]
    if not rename_action(action, self.anim_browser_label):
        return

    # Renaming re-sorts bpy.data.actions, so the held index now points at some
    # other clip. Re-find the one we just renamed and select that.
    previous = _SUPPRESS_LABEL
    _SUPPRESS_LABEL = True
    try:
        found = actions.find(action.name)
        if found >= 0:
            self.anim_browser_index = found
        _set_label_quietly(self, action.name)
    finally:
        _SUPPRESS_LABEL = previous


# ----------------------------------------------------------------------------
# Asset catalogs
#
# Catalogs for "Current File" assets live in blender_assets.cats.txt next to the
# .blend. Blender has no Python API for creating one outside an Asset Browser
# context, so this writes the file directly - same format Blender itself uses -
# and assigns the resulting UUID to the asset.
# ----------------------------------------------------------------------------

CATS_NAME = "blender_assets.cats.txt"

CATS_HEADER = (
    "# This is an Asset Catalog Definition file for Blender.\n"
    "#\n"
    "# Empty lines and lines starting with `#` will be ignored.\n"
    "# The first non-ignored line should be the version indicator.\n"
    '# Other lines are of the format "UUID:catalog/path/for/assets:simple catalog name"\n'
    "\n"
    "VERSION 1\n"
    "\n"
)


def catalog_file():
    """Path of the catalog definition file for the current .blend, if saved."""
    blend = bpy.data.filepath
    if not blend:
        return None
    return os.path.join(os.path.dirname(blend), CATS_NAME)


def read_catalogs():
    """catalog path -> (uuid, simple name), as recorded on disk."""
    entries = {}
    path = catalog_file()
    if not path or not os.path.exists(path):
        return entries
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("VERSION"):
                    continue
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                entries[parts[1]] = (parts[0], parts[2] if len(parts) > 2 else parts[1])
    except OSError:
        pass
    return entries


def write_catalogs(entries):
    path = catalog_file()
    if not path:
        return False
    lines = [CATS_HEADER]
    for cat_path in sorted(entries):
        cat_uuid, simple = entries[cat_path]
        lines.append("%s:%s:%s\n" % (cat_uuid, cat_path, simple))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
    except OSError:
        return False
    return True


def normalise_catalog(text):
    parts = [p.strip() for p in str(text).replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)


def ensure_catalog(cat_path):
    """Find or create the catalog, returning (uuid, simple name) or None.

    Parent catalogs are created too, so "Helldiver/Locomotion" also registers
    "Helldiver" and the Asset Browser tree shows the whole branch.
    """
    cat_path = normalise_catalog(cat_path)
    if not cat_path:
        return None
    if catalog_file() is None:
        return None                 # unsaved file: nowhere to persist it

    entries = read_catalogs()
    changed = False
    segments = cat_path.split("/")
    result = None
    for depth in range(1, len(segments) + 1):
        branch = "/".join(segments[:depth])
        if branch not in entries:
            entries[branch] = (str(uuid.uuid4()), branch.replace("/", "-"))
            changed = True
        result = entries[branch]
    if changed and not write_catalogs(entries):
        return None
    return result


def refresh_asset_browsers():
    """Nudge any open Asset Browser so a new catalog/asset shows up at once."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "FILE_BROWSER":
                    continue
                space = area.spaces.active
                if getattr(space, "browse_mode", "") != "ASSETS":
                    continue
                with bpy.context.temp_override(window=window, area=area):
                    bpy.ops.asset.library_refresh()
                area.tag_redraw()
    except Exception:
        pass


# Blender keeps only a weak reference to the strings a dynamic enum callback
# returns; without holding them here the items corrupt or vanish.
_CATALOG_ITEMS = []


NO_CATALOG = "__NONE__"


def _catalog_enum_items(self, context):
    _CATALOG_ITEMS.clear()
    _CATALOG_ITEMS.append((NO_CATALOG, "Unassigned",
                           "Leave the asset outside any catalog"))
    for cat_path in sorted(read_catalogs()):
        _CATALOG_ITEMS.append((cat_path, cat_path, "Existing catalog"))
    return _CATALOG_ITEMS


def catalog_path_for(meta):
    """The catalog path an asset sits in, looked up by UUID."""
    if not meta or not meta.catalog_id:
        return ""
    for cat_path, (cat_uuid, _simple) in read_catalogs().items():
        if cat_uuid == meta.catalog_id:
            return cat_path
    return meta.catalog_simple_name


# ----------------------------------------------------------------------------
# Asset previews
# ----------------------------------------------------------------------------

def viewport_area(context):
    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            for region in area.regions:
                if region.type == "WINDOW":
                    return area, region
    return None, None


def load_preview_pixels(action, image_path, size=256):
    """Fallback: read a PNG off disk straight into the datablock's preview."""
    image = None
    try:
        image = bpy.data.images.load(image_path, check_existing=False)
        image.scale(size, size)
        preview = action.preview_ensure()
        preview.image_size = (size, size)
        preview.image_pixels_float = image.pixels[:]
        return True
    except Exception:
        return False
    finally:
        if image is not None:
            try:
                bpy.data.images.remove(image)
            except Exception:
                pass


def render_asset_preview(context, action, size=512):
    """Render the viewport at the current frame into the asset's thumbnail.

    Uses an OpenGL viewport render rather than a full render, so it takes about
    a second and looks like what you are already looking at.
    """
    area, region = viewport_area(context)
    if area is None:
        return "no 3D viewport to render from"

    scene = context.scene
    render = scene.render
    image_settings = render.image_settings
    saved = {
        "x": render.resolution_x,
        "y": render.resolution_y,
        "percent": render.resolution_percentage,
        "filepath": render.filepath,
        "format": image_settings.file_format,
        "color_mode": image_settings.color_mode,
    }

    stem = os.path.join(tempfile.gettempdir(),
                        "anim_asset_preview_%s" % uuid.uuid4().hex[:8])
    written = None
    try:
        render.resolution_x = size
        render.resolution_y = size
        render.resolution_percentage = 100
        render.filepath = stem
        image_settings.file_format = "PNG"
        image_settings.color_mode = "RGBA"

        with context.temp_override(window=context.window, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)

        # A still OpenGL render writes "<filepath><ext>", but a numbered
        # "<filepath>0009.png" is what frame_path predicts. Take whichever
        # actually landed on disk.
        for candidate in (stem + ".png",
                          bpy.path.abspath(render.frame_path(frame=scene.frame_current))):
            if os.path.exists(candidate):
                written = candidate
                break
        else:
            matches = sorted(glob.glob(stem + "*"))
            written = matches[0] if matches else None
        if not written:
            return "the viewport render produced no image"

        try:
            with context.temp_override(id=action):
                bpy.ops.ed.lib_id_load_custom_preview(filepath=written)
        except (RuntimeError, TypeError):
            # Older builds don't take `id` as a context override; push the
            # pixels into the preview by hand instead.
            if not load_preview_pixels(action, written):
                return "could not attach the rendered image"
    except RuntimeError as exc:
        return str(exc)
    finally:
        render.resolution_x = saved["x"]
        render.resolution_y = saved["y"]
        render.resolution_percentage = saved["percent"]
        render.filepath = saved["filepath"]
        image_settings.file_format = saved["format"]
        image_settings.color_mode = saved["color_mode"]
        if written and os.path.exists(written):
            try:
                os.remove(written)
            except OSError:
                pass
    return None


# ----------------------------------------------------------------------------
# UI list
# ----------------------------------------------------------------------------

def row_visible(scene, action):
    """Shared by the list filter and the step operator, so they agree."""
    show = scene.anim_browser_show
    if show == "CLIPS" and is_asset(action):
        return False
    if show == "ASSETS" and not is_asset(action):
        return False

    query = scene.anim_browser_search.lower().strip()
    if query and query not in action.name.lower():
        # Fall back to the imported name, so a clip renamed to "crouch idle"
        # is still findable by its original hash.
        if query not in original_name(action).lower():
            return False
    if scene.anim_browser_hide_static and is_static(action):
        return False
    if scene.anim_browser_unlabelled_only and is_labelled(action):
        return False
    if scene.anim_browser_queued_only and not is_queued(action):
        return False
    return True


class VIEW3D_UL_anim_clips(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            static = is_static(item)
            kind = clip_kind(item)
            if kind == "ASSET":
                icon = "ASSET_MANAGER"
            elif kind == "BAKE":
                icon = "ACTION_TWEAK"
            elif is_labelled(item):
                icon = "OUTLINER_OB_FONT"
            else:
                icon = "POSE_HLT" if static else "ACTION"
            tick = row.row(align=True)
            tick.operator(
                "anim.browser_queue", text="", emboss=False,
                icon="CHECKBOX_HLT" if is_queued(item) else "CHECKBOX_DEHLT",
            ).index = index
            row.label(text=item.name, icon=icon)
            start, end = clip_span(item)
            sub = row.row()
            sub.alignment = "RIGHT"
            sub.label(text="pose" if static else "%d f" % int(round(end - start)))
        else:
            layout.label(text=item.name)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        scene = context.scene

        flags = [self.bitflag_filter_item] * len(items)
        for i, action in enumerate(items):
            if not row_visible(scene, action):
                flags[i] &= ~self.bitflag_filter_item

        order = list(display_order(items)) if scene.anim_browser_stable_order else []
        return flags, order


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------

class ANIM_OT_browser_refresh(bpy.types.Operator):
    bl_idname = "anim.browser_refresh"
    bl_label = "Refresh Clip List"
    bl_description = "Recalculate clip lengths (run after importing more animations)"

    def execute(self, context):
        count = rebuild_cache()
        self.report({"INFO"}, "Scanned %d clips" % count)
        return {"FINISHED"}


class ANIM_OT_browser_step(bpy.types.Operator):
    bl_idname = "anim.browser_step"
    bl_label = "Step Clip"
    bl_description = "Move to the previous or next visible clip"

    direction: IntProperty(default=1)

    def execute(self, context):
        scene = context.scene
        actions = bpy.data.actions
        if not len(actions):
            return {"CANCELLED"}

        allowed = [i for i, a in enumerate(actions) if row_visible(scene, a)]
        if scene.anim_browser_stable_order and allowed:
            # Step through what the list actually shows, not collection order.
            order = display_order(actions)
            allowed.sort(key=lambda i: order[i])
        if not allowed:
            self.report({"WARNING"}, "No clips match the current filter")
            return {"CANCELLED"}

        current = scene.anim_browser_index
        if current in allowed:
            position = allowed.index(current)
            position = (position + self.direction) % len(allowed)
        else:
            position = 0
        scene.anim_browser_index = allowed[position]
        return {"FINISHED"}


class ANIM_OT_browser_play(bpy.types.Operator):
    bl_idname = "anim.browser_play"
    bl_label = "Play / Pause"
    bl_description = "Toggle playback of the current clip"

    def execute(self, context):
        bpy.ops.screen.animation_play()
        return {"FINISHED"}


class ANIM_OT_browser_revert_name(bpy.types.Operator):
    bl_idname = "anim.browser_revert_name"
    bl_label = "Restore Imported Name"
    bl_description = "Rename this clip back to the name it was imported under"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        action = selected_action(context)
        return action is not None and is_labelled(action)

    def execute(self, context):
        scene = context.scene
        action = selected_action(context)
        imported = original_name(action)

        span = _RANGE_CACHE.pop(action.name, None)
        action.name = imported
        if span is not None:
            _RANGE_CACHE[action.name] = span
        del action[HASH_PROP]
        bump_order()

        found = bpy.data.actions.find(action.name)
        if found >= 0:
            scene.anim_browser_index = found
        _set_label_quietly(scene, action.name)
        self.report({"INFO"}, "Restored %s" % imported)
        return {"FINISHED"}


class ANIM_OT_browser_pick_catalog(bpy.types.Operator):
    bl_idname = "anim.browser_pick_catalog"
    bl_label = "Existing Catalog"
    bl_description = "Choose one of the catalogs already defined for this library"
    bl_property = "catalog"

    catalog: EnumProperty(name="Catalog", items=_catalog_enum_items)

    def execute(self, context):
        context.scene.anim_asset_catalog = (
            "" if self.catalog == NO_CATALOG else self.catalog)
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}


def asset_candidate(context):
    """The Action the asset panel promotes, and the clip it stood in for.

    Promoting is meant to catalogue a retarget bake, but the browser stays on
    the Cast clip the bake was made from - baking deliberately does not move
    the selection, because anim_browser_index re-assigns whatever it lands on
    to anim_browser_target, and that is the source skeleton, not the rig. So
    when the selection is a Cast original with a bake behind it, the bake is
    what gets promoted.

    Returns (action, stood_in_for), where stood_in_for is the selected clip
    when a bake was resolved on its behalf, and None when the selection is
    being promoted as-is.
    """
    action = selected_action(context)
    if action is None or clip_kind(action) != "CAST":
        return action, None
    bakes = bake_made_from(action)
    if not bakes:
        return action, None
    return bakes[-1], action


class ANIM_OT_browser_make_asset(bpy.types.Operator):
    bl_idname = "anim.browser_make_asset"
    bl_label = "Create Animation Asset"
    bl_description = ("Move a baked rig animation into the Asset Browser: mark "
                      "the selected Action as an Animation Asset - the whole "
                      "Action, not a single-frame pose - so it can be browsed, "
                      "catalogued and assigned from the Asset Browser. Intended "
                      "for the bake a retarget produced, not a raw Cast clip")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        action, _ = asset_candidate(context)
        return action is not None and not is_asset(action)

    def execute(self, context):
        scene = context.scene
        action, _ = asset_candidate(context)
        if action is None:
            self.report({"ERROR"}, "No clip selected")
            return {"CANCELLED"}
        if is_asset(action):
            self.report({"WARNING"}, "%s is already an asset" % action.name)
            return {"CANCELLED"}

        imported = original_name(action) or action.name
        start, end = clip_span(action)
        frames = int(round(end - start))

        # A rename here goes through the same path as the browser's own rename,
        # so the imported hash is stashed before the name is replaced.
        wanted = scene.anim_asset_name.strip()
        if wanted:
            rename_action(action, wanted)

        try:
            action.asset_mark()
        except Exception as exc:
            self.report({"ERROR"}, "Could not mark as asset: %s" % exc)
            return {"CANCELLED"}

        # Assets are kept on save, but a fake user makes that explicit and
        # survives Clear Asset later on.
        action.use_fake_user = True
        action[SOURCE_PROP] = imported

        meta = action.asset_data
        description = scene.anim_asset_description.strip()
        if not description:
            fps = scene.render.fps / scene.render.fps_base
            description = "%d frames (%.2f s) - from %s" % (
                frames, frames / max(1e-6, fps), imported)
        meta.description = description

        tags = ["animation"]
        tags.extend(t.strip() for t in scene.anim_asset_tags.split(","))
        if scene.anim_asset_tag_source:
            tags.append(imported)
        existing = {t.name for t in meta.tags}
        for tag in tags:
            if tag and tag not in existing:
                meta.tags.new(tag)
                existing.add(tag)

        notes = []
        catalog = ensure_catalog(scene.anim_asset_catalog)
        if catalog:
            # catalog_simple_name is read-only; Blender fills it in from the
            # catalog file once the UUID resolves.
            meta.catalog_id = catalog[0]
        elif normalise_catalog(scene.anim_asset_catalog):
            notes.append("catalog needs the .blend saved first")

        if scene.anim_asset_preview:
            problem = render_asset_preview(context, action)
            if problem:
                notes.append("no thumbnail (%s)" % problem)

        # Marking and renaming both re-sort bpy.data.actions.
        bump_order()
        found = bpy.data.actions.find(action.name)
        if found >= 0:
            scene.anim_browser_index = found
        _set_label_quietly(scene, action.name)
        scene.anim_asset_name = ""
        scene.anim_asset_description = ""
        refresh_asset_browsers()

        message = "Asset '%s' created" % action.name
        if notes:
            message += " - " + "; ".join(notes)
            self.report({"WARNING"}, message)
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class ANIM_OT_browser_clear_asset(bpy.types.Operator):
    bl_idname = "anim.browser_clear_asset"
    bl_label = "Clear Asset"
    bl_description = ("Remove the asset metadata from this clip. The Action "
                      "itself and its keyframes are left alone")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        action = selected_action(context)
        return action is not None and is_asset(action)

    def execute(self, context):
        scene = context.scene
        action = selected_action(context)
        name = action.name
        action.asset_clear()
        action.use_fake_user = True     # don't lose the clip on the next save
        bump_order()
        found = bpy.data.actions.find(name)
        if found >= 0:
            scene.anim_browser_index = found
        refresh_asset_browsers()
        self.report({"INFO"}, "Cleared asset metadata from %s" % name)
        return {"FINISHED"}


class ANIM_OT_browser_repreview(bpy.types.Operator):
    bl_idname = "anim.browser_repreview"
    bl_label = "Thumbnail from This Frame"
    bl_description = ("Re-render the asset thumbnail from the 3D viewport at "
                      "the current frame")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        action = selected_action(context)
        return action is not None and is_asset(action)

    def execute(self, context):
        action = selected_action(context)
        problem = render_asset_preview(context, action)
        if problem:
            self.report({"ERROR"}, "Thumbnail failed: %s" % problem)
            return {"CANCELLED"}
        refresh_asset_browsers()
        self.report({"INFO"}, "Thumbnail updated for %s" % action.name)
        return {"FINISHED"}


class ANIM_OT_browser_assign_asset(bpy.types.Operator):
    bl_idname = "anim.browser_assign_asset"
    bl_label = "Assign to Rig"
    bl_description = ("Load this animation onto the target rig and drop into "
                      "Pose Mode, ready to edit")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return selected_action(context) is not None and target_object(context) is not None

    def execute(self, context):
        action = selected_action(context)
        arm = target_object(context)
        if action is None or arm is None:
            self.report({"ERROR"}, "Need a clip and a target rig")
            return {"CANCELLED"}

        assign_action(arm, action)

        scene = context.scene
        if scene.anim_browser_autorange:
            start, end = clip_span(action)
            scene.frame_start = int(round(start))
            scene.frame_end = max(int(round(end)), int(round(start)))

        try:
            if arm.visible_get():
                context.view_layer.objects.active = arm
                arm.select_set(True)
                if arm.mode != "POSE":
                    bpy.ops.object.mode_set(mode="POSE")
        except RuntimeError:
            # Hidden or locked rig: the action is assigned regardless, which is
            # the part that matters.
            pass

        self.report({"INFO"}, "Assigned %s to %s" % (action.name, arm.name))
        return {"FINISHED"}


class ANIM_OT_browser_open_asset_browser(bpy.types.Operator):
    bl_idname = "anim.browser_open_asset_browser"
    bl_label = "Open Asset Browser"
    bl_description = ("Open an Asset Browser in its own window, showing this "
                      "file's animation assets and their thumbnails")

    def execute(self, context):
        wm = context.window_manager
        before = set(wm.windows)
        try:
            bpy.ops.wm.window_new()
        except RuntimeError as exc:
            self.report({"ERROR"}, "Could not open a window: %s" % exc)
            return {"CANCELLED"}

        fresh = [w for w in wm.windows if w not in before]
        if not fresh:
            self.report({"ERROR"}, "Could not open a window")
            return {"CANCELLED"}

        area = fresh[0].screen.areas[0]
        try:
            area.ui_type = "ASSETS"
        except (TypeError, AttributeError):
            self.report({"WARNING"},
                        "Opened a window - set its editor to Asset Browser")
            return {"FINISHED"}

        # params only exists once the area has drawn, so this may be a no-op on
        # the first pass; the browser opens on its remembered library either way.
        params = getattr(area.spaces.active, "params", None)
        if params is not None:
            try:
                params.asset_library_reference = "LOCAL"
            except Exception:
                pass
        area.tag_redraw()
        return {"FINISHED"}


class ANIM_OT_browser_fix_slot(bpy.types.Operator):
    """Bind the assigned action's slot to this rig, and rename the slot to match.

Blender 5 actions carry a slot named after the ID they were made for. Assign
an action to a rig with a different name and the slot silently stays unbound:
the action looks applied, every F-curve is present, and the rig sits in rest
pose with nothing anywhere to say why. Renaming the slot makes Blender's
name-based auto-binding work for every future assignment too"""
    bl_idname = "anim.browser_fix_slot"
    bl_label = "Fix Action Slot"
    bl_options = {"REGISTER", "UNDO"}

    rename: BoolProperty(
        name="Rename Slot to Match Rig",
        description="So future assignments auto-bind instead of needing this",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        obj = target_object(context)
        ad = obj.animation_data if obj else None
        return bool(ad and ad.action)

    def execute(self, context):
        obj = target_object(context)
        ad = obj.animation_data
        act = ad.action
        if not hasattr(act, "slots") or not act.slots:
            self.report({"INFO"}, "This action has no slots - nothing to bind")
            return {"CANCELLED"}

        was = ad.action_slot.identifier if ad.action_slot else None
        slot = ad.action_slot
        if slot is None:
            for s in act.slots:
                if s.target_id_type == "OBJECT":
                    slot = s
                    break
            if slot is None:
                self.report({"ERROR"}, "No OBJECT slot in this action")
                return {"CANCELLED"}
            ad.action_slot = slot

        if self.rename and slot.name_display != obj.name:
            try:
                slot.name_display = obj.name
            except Exception:
                pass

        obj.data.update_tag()
        context.view_layer.update()
        self.report({"INFO"},
                    "Slot %s -> %s" % (was or "unbound", slot.identifier))
        return {"FINISHED"}


class ANIM_OT_browser_queue(bpy.types.Operator):
    """Tick a clip into the batch retarget queue.

Kept on the Action itself rather than in a scene list, so the queue
survives renaming, saving, and being applied to a different rig"""
    bl_idname = "anim.browser_queue"
    bl_label = "Queue for Batch"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: IntProperty(default=-1, options={"SKIP_SAVE"})
    action_name: StringProperty(default="", options={"SKIP_SAVE"})
    mode: EnumProperty(
        items=[("TOGGLE", "Toggle", ""), ("CLEAR_ALL", "Clear All", "")],
        default="TOGGLE", options={"SKIP_SAVE"},
    )

    def execute(self, context):
        acts = bpy.data.actions
        if self.mode == "CLEAR_ALL":
            n = 0
            for a in acts:
                if QUEUE_PROP in a.keys():
                    del a[QUEUE_PROP]
                    n += 1
                # Sweep up 2.x favourite flags while we are here; nothing
                # reads them any more.
                if _RETIRED_FAV_PROP in a.keys():
                    del a[_RETIRED_FAV_PROP]
            self.report({"INFO"}, "Emptied the queue (%d clip(s))" % n)
            return {"FINISHED"}

        action = None
        if self.action_name:
            action = acts.get(self.action_name)
        elif 0 <= self.index < len(acts):
            action = acts[self.index]
        else:
            action = selected_action(context)
        if action is None:
            self.report({"ERROR"}, "No clip to queue")
            return {"CANCELLED"}

        if is_queued(action):
            del action[QUEUE_PROP]
            self.report({"INFO"}, "Removed %s from the queue" % action.name)
        else:
            action[QUEUE_PROP] = True
            self.report({"INFO"}, "Queued %s" % action.name)
        return {"FINISHED"}


class ANIM_OT_browser_delete(bpy.types.Operator):
    """Delete clips from the file.

Imported originals and anything promoted to an asset are always skipped -
a bake can be re-run from its clip, but an imported clip cannot be
recreated once it is gone"""
    bl_idname = "anim.browser_delete"
    bl_label = "Delete Clips"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(
        name="Delete",
        items=[
            ("CURRENT", "Selected Row", "Just the highlighted clip"),
            ("BAKES", "All Bakes", "Every retarget output in the file"),
            ("UNUSED", "Unused Non-Originals",
             "Anything not imported, not an asset and not in use"),
        ],
        default="CURRENT",
    )

    def _doomed(self, context):
        acts = bpy.data.actions
        if self.scope == "CURRENT":
            i = context.scene.anim_browser_index
            return [acts[i]] if 0 <= i < len(acts) else []
        if self.scope == "BAKES":
            return [a for a in acts if clip_kind(a) == "BAKE"]
        out = []
        for a in acts:
            if clip_protected(a):
                continue
            users = a.users - (1 if a.use_fake_user else 0)
            if users <= 0:
                out.append(a)
        return out

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "scope")
        doomed = self._doomed(context)
        keep = [a for a in doomed if clip_protected(a)]
        go = [a for a in doomed if not clip_protected(a)]
        for a in go[:6]:
            col.label(text=a.name, icon="TRASH")
        if len(go) > 6:
            col.label(text="... and %d more" % (len(go) - 6))
        if not go:
            col.label(text="Nothing deletable in this scope", icon="INFO")
        if keep:
            col.label(text="%d protected item(s) will be skipped" % len(keep),
                      icon="CHECKMARK")
        if go:
            col.label(text="This cannot be undone with Ctrl+Z", icon="ERROR")

    def execute(self, context):
        doomed = [a for a in self._doomed(context) if not clip_protected(a)]
        skipped = [a for a in self._doomed(context) if clip_protected(a)]
        if not doomed:
            if skipped:
                self.report({"WARNING"},
                            "'%s' is an imported original or an asset - not "
                            "deleting it" % skipped[0].name)
            else:
                self.report({"INFO"}, "Nothing to delete")
            return {"CANCELLED"}

        names = [a.name for a in doomed]
        for ob in bpy.data.objects:
            ad = ob.animation_data
            if ad and ad.action in doomed:
                ad.action = None
        for a in doomed:
            a.use_fake_user = False
            bpy.data.actions.remove(a)

        rebuild_cache()
        i = context.scene.anim_browser_index
        context.scene.anim_browser_index = max(0, min(i, len(bpy.data.actions) - 1))
        self.report({"INFO"}, "Deleted %d: %s%s"
                    % (len(names), ", ".join(names[:3]),
                       " ..." if len(names) > 3 else ""))
        return {"FINISHED"}


class ANIM_OT_browser_apply(bpy.types.Operator):
    bl_idname = "anim.browser_apply"
    bl_label = "Apply Clip"
    bl_description = "Apply the highlighted clip to the target armature"

    def execute(self, context):
        action = apply_index(context, context.scene.anim_browser_index)
        if action is None:
            self.report({"ERROR"}, "No armature to apply to - set a target above")
            return {"CANCELLED"}
        self.report({"INFO"}, "Applied %s" % action.name)
        return {"FINISHED"}


# ----------------------------------------------------------------------------
# Panels
# ----------------------------------------------------------------------------

class VIEW3D_PT_anim_browser(bpy.types.Panel):
    bl_label = "Cast to Rig Converter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cast to Rig"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        obj = target_object(context)
        row = layout.row(align=True)
        row.prop(scene, "anim_browser_target", text="Rig")
        if obj is None:
            layout.label(text="No armature found", icon="ERROR")
            return

        row = layout.row(align=True)
        row.prop(scene, "anim_browser_search", text="", icon="VIEWZOOM")
        row.operator(ANIM_OT_browser_refresh.bl_idname, text="", icon="FILE_REFRESH")

        layout.template_list(
            "VIEW3D_UL_anim_clips", "", bpy.data, "actions", scene, "anim_browser_index", rows=12
        )

        row = layout.row(align=True)
        row.operator(ANIM_OT_browser_step.bl_idname, text="", icon="TRIA_LEFT").direction = -1
        row.operator(
            ANIM_OT_browser_play.bl_idname,
            text="Pause" if context.screen.is_animation_playing else "Play",
            icon="PAUSE" if context.screen.is_animation_playing else "PLAY",
        )
        row.operator(ANIM_OT_browser_step.bl_idname, text="", icon="TRIA_RIGHT").direction = 1
        row.separator()
        row.operator(ANIM_OT_browser_delete.bl_idname, text="", icon="TRASH")

        acts = bpy.data.actions
        i = scene.anim_browser_index
        if 0 <= i < len(acts):
            sel = acts[i]
            kind = clip_kind(sel)
            note = {"CAST": "imported original - protected",
                    "ASSET": "animation asset - protected",
                    "BAKE": "retarget bake - safe to delete",
                    "OTHER": "not imported - safe to delete"}[kind]
            layout.label(text=note,
                         icon="LOCKED" if clip_protected(sel) else "TRASH")


class VIEW3D_PT_anim_browser_filters(bpy.types.Panel):
    bl_label = "Filters"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cast to Rig"
    bl_parent_id = "VIEW3D_PT_anim_browser"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.row().prop(scene, "anim_browser_show", expand=True)
        row = layout.row(align=True)
        row.prop(scene, "anim_browser_hide_static", toggle=True)
        row.prop(scene, "anim_browser_autorange", toggle=True)
        row = layout.row(align=True)
        row.prop(scene, "anim_browser_unlabelled_only", toggle=True)
        row.prop(scene, "anim_browser_stable_order", toggle=True)
        row = layout.row(align=True)
        row.prop(scene, "anim_browser_queued_only", toggle=True,
                 icon="CHECKBOX_HLT")
        op = row.operator("anim.browser_queue", text="Clear Queue", icon="X")
        op.mode = "CLEAR_ALL"

        queued = sum(1 for a in bpy.data.actions if is_queued(a))
        if queued:
            sub = layout.row()
            sub.enabled = False
            sub.label(text="%d queued for batch" % queued)

        labelled = sum(1 for a in bpy.data.actions if is_labelled(a))
        if labelled:
            row = layout.row()
            row.enabled = False
            row.label(text="%d of %d named" % (labelled, len(bpy.data.actions)))


class VIEW3D_PT_anim_browser_clip(bpy.types.Panel):
    """The clip on the rig, and everything you can do with the selected row.

    Auditioning and promoting used to be two panels; they are one section now
    because the second was never useful on its own - you always arrive here
    from a row you just clicked in the list above.
    """
    bl_label = "Selected Clip"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cast to Rig"
    bl_parent_id = "VIEW3D_PT_anim_browser"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = target_object(context)
        if obj is None:
            return

        # ---- what is on the rig right now -----------------------------------
        anim_data = obj.animation_data
        current = anim_data.action if anim_data else None

        if (current is not None and hasattr(current, "slots") and current.slots
                and getattr(anim_data, "action_slot", None) is None):
            col = layout.column(align=True)
            col.alert = True
            col.label(text="Action slot is not bound", icon="ERROR")
            col.label(text="Assigned, but nothing will play")
            col.operator(ANIM_OT_browser_fix_slot.bl_idname, icon="FILE_REFRESH")

        box = layout.box()
        if current:
            start, end = clip_span(current)
            box.label(text="%d frames  -  %.2f s" % (
                int(round(end - start)),
                (end - start) / max(1e-6, scene.render.fps / scene.render.fps_base),
            ), icon="ASSET_MANAGER" if is_asset(current) else "ACTION")

            row = box.row(align=True)
            row.prop(scene, "anim_browser_label", text="")
            row.operator(ANIM_OT_browser_revert_name.bl_idname, text="", icon="LOOP_BACK")

            imported = original_name(current)
            if imported:
                sub = box.row()
                sub.enabled = False
                sub.label(text=imported, icon="FILE_BLANK")
        else:
            box.label(text="No clip applied", icon="INFO")
            box.operator(ANIM_OT_browser_apply.bl_idname, icon="PLAY")

        chosen = selected_action(context)
        if chosen is None:
            layout.label(text="No clip selected", icon="INFO")
            return

        layout.separator()

        # ---- the selected row is already an asset: use or edit it -----------
        if is_asset(chosen):
            meta = chosen.asset_data
            box = layout.box()
            box.label(text=chosen.name, icon="ASSET_MANAGER")
            catalog = catalog_path_for(meta)
            if catalog:
                sub = box.row()
                sub.enabled = False
                sub.label(text=catalog, icon="OUTLINER")
            box.prop(meta, "description", text="")
            tags = ", ".join(t.name for t in meta.tags)
            if tags:
                sub = box.row()
                sub.enabled = False
                sub.label(text=tags, icon="BOOKMARKS")

            col = layout.column(align=True)
            col.scale_y = 1.3
            col.operator(ANIM_OT_browser_assign_asset.bl_idname,
                         text="Assign Animation to Rig", icon="ARMATURE_DATA")
            row = layout.row(align=True)
            row.operator(ANIM_OT_browser_repreview.bl_idname, text="Thumbnail",
                         icon="RENDER_STILL")
            row.operator(ANIM_OT_browser_clear_asset.bl_idname, text="Unmark",
                         icon="X")
            layout.operator(ANIM_OT_browser_open_asset_browser.bl_idname,
                            text="Open Asset Browser", icon="FILEBROWSER")
            sub = layout.row()
            sub.enabled = False
            sub.label(text="Thumbnails show in the Asset Browser")
            return

        # A plain clip has nothing more to do here: promoting it belongs after
        # the retarget steps, because what you promote is the bake they produce.
        sub = layout.row()
        sub.enabled = False
        sub.label(text="Retarget below to bake this onto the rig",
                  icon="FORWARD")


# ===========================================================================
# RETARGET TO RIG
#
# Drives a Cast/Stingray game skeleton's animation onto the Rigify-style
# Helldiver rig, then bakes it down to plain keyframes.
#
# The hard part of retargeting is that the two skeletons don't share bone
# orientations - measured across this pair, the mean rest-orientation
# difference is about 142 degrees, so a plain Copy Rotation produces garbage.
# The fix is a proxy bone: for every mapped pair we create a bone on the
# SOURCE armature that has the TARGET bone's orientation but is parented under
# the source bone. It therefore inherits the source's animation while sitting
# in the target's frame of reference, and the target bone can Copy Rotation
# from it directly.
#
# The proxy-bone construction and the two roll helpers below are adapted from
# the "Retarget" add-on by KBS-DEV (GPL-3.0-or-later).
# ===========================================================================

from mathutils import Matrix, Quaternion, Vector
from math import degrees, pi as _PI

_RET_SUFFIX = "_RET"
_RET_COLL = "Retarget Bones"
_SIDES = ("l", "r")
_FINGERS = ("thumb", "index", "middle", "ring", "pinky")

# Bones on the rig whose IK/FK slider must sit at FK (1.0), because the
# mapping drives the _fk chain.
_IKFK_SWITCHES = ("l_thigh_parent", "r_thigh_parent",
                  "l_shoulder_parent", "r_shoulder_parent")


def _build_pairs():
    """target rig bone -> source (Cast) bone."""
    p = {
        "root": "root",
        # 'boss' is the real pelvis on the Cast rig: both 'hips' and 'spine1'
        # are its children, so it maps to the rig's torso master.
        "torso": "boss",
        "hips_control": "hips",
        "spine1_fk": "spine1",
        "spine2_fk": "spine2",
        "chest_fk": "chest",
        "neck_control": "neck",
        "head_control": "head",
    }
    for s in _SIDES:
        # On the Cast skeleton '<s>_clavicle' is the collarbone and
        # '<s>_shoulder' is the upper arm - not the other way round.
        p[s + "_clavicle_control"] = s + "_clavicle"
        p[s + "_shoulder_fk"] = s + "_shoulder"
        p[s + "_elbow_fk"] = s + "_elbow"
        p[s + "_hand_fk"] = s + "_hand"
        p[s + "_thigh_fk"] = s + "_thigh"
        p[s + "_knee_fk"] = s + "_knee"
        p[s + "_foot_fk"] = s + "_foot"
        p[s + "_ball_fk"] = s + "_ball"
        p[s + "_shoulder_ik"] = s + "_shoulder"
        p[s + "_hand_ik"] = s + "_hand"
        p[s + "_thigh_ik"] = s + "_thigh"
        p[s + "_foot_ik"] = s + "_foot"
        p[s + "_ball_ik"] = s + "_ball"
        p[s + "_foot_spin_ik"] = s + "_foot"
        p[s + "_foot_heel_ik"] = s + "_foot"
        for f in _FINGERS:
            for n in (1, 2, 3):
                key = "%s_%s_finger%d_fk" % (s, f, n)
                p[key] = "%s_%s_finger%d" % (s, f, n)
    for n in range(1, 9):
        p["cape%d_control" % n] = "cape%d" % n
    return p


RETARGET_PAIRS = _build_pairs()

# Only these follow the source in world space; everything else is rotation
# only. Location on an FK bone would fight the rig's own hierarchy.
_LOC_BONES = ({"root", "torso"}
              | set(s + "_foot_ik" for s in _SIDES)
              | set(s + "_hand_ik" for s in _SIDES))


# --- roll maths, adapted from the Retarget add-on (GPL-3.0-or-later) -------

def _vec_roll_to_mat3_normalized(nor, roll):
    THETA_SAFE = 1.0e-5
    THETA_CRITICAL = 1.0e-9
    x, y, z = nor.x, nor.y, nor.z
    theta = 1.0 + y
    theta_alt = x * x + z * z
    m = Matrix().to_3x3()
    if theta > THETA_SAFE or ((bool(x) | bool(z)) and theta > THETA_CRITICAL):
        m[0][1] = -x
        m[1][0] = x
        m[1][1] = y
        m[1][2] = z
        m[2][1] = -z
        if theta > THETA_SAFE:
            m[0][0] = 1 - x * x / theta
            m[2][2] = 1 - z * z / theta
            m[2][0] = m[0][2] = -x * z / theta
        else:
            m[0][0] = (x + z) * (x - z) / -theta_alt
            m[2][2] = -m[0][0]
            m[2][0] = m[0][2] = 2.0 * x * z / theta_alt
    else:
        m.identity()
        m[0][0] = m[1][1] = -1.0
    return Quaternion(nor, roll).to_matrix() @ m


def _ebone_roll_to_vector(bone, align_axis):
    align_axis = align_axis.normalized()
    nor = (bone.tail - bone.head).normalized()
    if nor.dot(align_axis) == 1.0:
        return 0.0
    proj = align_axis - align_axis.project(nor)
    mat = _vec_roll_to_mat3_normalized(nor, 0.0)
    try:
        roll = proj.angle(mat[2])
    except ValueError:
        return bone.roll
    if mat[2].cross(proj).dot(nor) < 0.0:
        return -roll
    return roll


# --- state -----------------------------------------------------------------

def _ret_pairs_for(src, trg):
    """Mapped pairs that actually exist on both armatures."""
    if not src or not trg or src.type != "ARMATURE" or trg.type != "ARMATURE":
        return []
    sb, tb = src.data.bones, trg.data.bones
    return [(t, s) for t, s in RETARGET_PAIRS.items() if t in tb and s in sb]


def _ret_constraints(trg, src):
    """Every constraint on trg that this tool added."""
    out = []
    if not trg or not src:
        return out
    for pb in trg.pose.bones:
        for c in pb.constraints:
            tgt = getattr(c, "target", None)
            if tgt is src and getattr(c, "subtarget", "").endswith(_RET_SUFFIX):
                out.append((pb, c))
    return out


def _ret_proxies(src):
    if not src or src.type != "ARMATURE":
        return []
    return [b.name for b in src.data.bones if b.name.endswith(_RET_SUFFIX)]


def _ret_baked_action(trg):
    """The rig's action, if it looks like a real bake rather than a stray."""
    if not trg or not trg.animation_data or not trg.animation_data.action:
        return None
    act = trg.animation_data.action
    bones = set()
    for fc in _action_fcurves(act):
        if fc.data_path.startswith("pose.bones"):
            try:
                bones.add(fc.data_path.split('"')[1])
            except IndexError:
                pass
    span = act.frame_range
    if len(bones) >= 20 and (span[1] - span[0]) >= 1:
        return act
    return None


def _action_fcurves(act):
    """F-curves of an action under either the classic or the slotted API."""
    if hasattr(act, "fcurves"):
        return list(act.fcurves)
    out = []
    for layer in act.layers:
        for strip in layer.strips:
            for slot in act.slots:
                try:
                    bag = strip.channelbag(slot)
                except Exception:
                    bag = None
                if bag:
                    out.extend(bag.fcurves)
    return out


def _ret_action_fits(ob, act):
    """True if act's channels actually address ob's bones."""
    if ob is None or act is None:
        return True
    have = set(b.name for b in ob.data.bones)
    used = set()
    for fc in _action_fcurves(act):
        if fc.data_path.startswith("pose.bones"):
            try:
                used.add(fc.data_path.split('"')[1])
            except IndexError:
                pass
    if not used:
        return True
    return len(used & have) > len(used) * 0.5


def _ret_bakes(trg, clip=None):
    """Baked actions this tool produced, newest-looking last."""
    out = []
    for act in bpy.data.actions:
        if act.get("retarget_baked") != (trg.name if trg else None):
            continue
        if clip and act.get("retarget_from") != clip:
            continue
        out.append(act)
    return sorted(out, key=lambda a: a.name)


def _ret_state(context):
    sc = context.scene
    src = sc.anim_retarget_source
    trg = sc.anim_retarget_target
    pairs = _ret_pairs_for(src, trg)
    return {
        "src": src,
        "trg": trg,
        "pairs": len(pairs),
        "bound": len(_ret_constraints(trg, src)),
        "proxies": len(_ret_proxies(src)),
        "baked": _ret_baked_action(trg),
        "ready": bool(src and trg and src is not trg and pairs),
        "missing_trg": sorted(t for t, s in RETARGET_PAIRS.items()
                              if trg and s in src.data.bones
                              and t not in trg.data.bones) if (src and trg) else [],
        "missing_src": sorted(s for t, s in RETARGET_PAIRS.items()
                              if src and t in trg.data.bones
                              and s not in src.data.bones) if (src and trg) else [],
        "src_clip_fits": _ret_action_fits(
            src, src.animation_data.action
            if src and src.animation_data else None),
        "bakes": len(_ret_bakes(trg)) if trg else 0,
    }


def _ret_height(ob, foot, head):
    """Rest-pose ankle-to-head height in world units, or None."""
    try:
        a = ob.matrix_world @ ob.data.bones[foot].head_local
        b = ob.matrix_world @ ob.data.bones[head].head_local
    except (KeyError, AttributeError):
        return None
    return b.z - a.z


# --- mode helpers ----------------------------------------------------------

def _ret_blocked(context, ob):
    """Why ob can't be worked on, or None if it is fine.

    This never changes visibility. An excluded collection or a disabled
    object is a deliberate act by whoever set the file up, and more often
    than not it means the object in the field is NOT the one they meant -
    collection names and object suffixes drift apart easily. Report it and
    stop rather than quietly switching it on.
    """
    if ob is None:
        return "no object set"
    if ob.name not in context.view_layer.objects:
        cols = ", ".join(c.name for c in ob.users_collection) or "no collection"
        return ("'%s' is not in the View Layer - its collection (%s) is "
                "excluded. Check this is the object you meant, then tick it "
                "in the Outliner." % (ob.name, cols))
    if ob.hide_viewport:
        return ("'%s' is disabled in the viewport (the monitor icon). "
                "Check this is the object you meant, then re-enable it."
                % ob.name)
    return None


def _ret_guard(op, context):
    """Report and refuse if either object is hidden away, or if the source
    is carrying an action that does not belong to it - which happens easily,
    because a bake lands in the browser's clip list looking like a clip."""
    st = _ret_state(context)
    for ob in (st["src"], st["trg"]):
        why = _ret_blocked(context, ob)
        if why:
            op.report({"ERROR"}, why)
            return False
    if not st["src_clip_fits"]:
        op.report({"ERROR"},
                  "The action on '%s' addresses rig bones, not its own - it "
                  "is a bake, not a clip. Pick an imported clip first."
                  % st["src"].name)
        return False
    return True


def _ret_activate(context, ob, mode="OBJECT"):
    vl = context.view_layer
    why = _ret_blocked(context, ob)
    if why:
        raise RuntimeError(why)
    if context.object and context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in context.selected_objects:
        o.select_set(False)
    # Only the eye is cleared - step 5 sets it deliberately, so leaving it
    # would deadlock the tool. Exclusion and the monitor icon are refused
    # above instead.
    ob.hide_set(False)
    ob.select_set(True)
    vl.objects.active = ob
    if mode != "OBJECT":
        bpy.ops.object.mode_set(mode=mode)


# --- the bind --------------------------------------------------------------

def _ret_do_bind(context, src, trg, pairs):
    """Create proxy bones on src and constrain trg's controls to them."""
    # Pin the clip we are binding. Clicking a row in the browser re-assigns
    # the source's action, so the clip can change between bind and bake;
    # recording it here lets the bake re-assert the right one.
    _act = src.animation_data.action if src.animation_data else None
    trg["anim_retarget_clip"] = _act.name if _act else ""
    # Rest matrices of the target's bones, captured before we enter Edit Mode
    # on the source - two armatures can't be edited at once.
    mats = {}
    for t_name, _ in pairs:
        b = trg.data.bones[t_name]
        mats[t_name] = (b.matrix_local.copy(),
                        b.head_local.copy(), b.tail_local.copy(),
                        b.matrix_local.to_3x3() @ Vector((0.0, 0.0, 1.0)))

    coll = src.data.collections.get(_RET_COLL)
    if coll is None:
        coll = src.data.collections.new(_RET_COLL)
    coll.is_visible = False

    # One matrix takes anything from the rig's armature space into the
    # source's armature space.
    into_src = src.matrix_world.inverted() @ trg.matrix_world

    _ret_activate(context, src, "EDIT")
    made = []
    for t_name, s_name in pairs:
        mat, head, tail, z_axis = mats[t_name]
        s_ebone = src.data.edit_bones.get(s_name)
        if s_ebone is None:
            continue
        name = t_name + _RET_SUFFIX
        nb = src.data.edit_bones.get(name) or src.data.edit_bones.new(name)
        # The proxy is a copy of the RIG bone, moved into the source
        # armature's space and hung under the matching source bone. It keeps
        # the rig's rest orientation - so a plain world-space Copy Rotation
        # from it lands correctly - while inheriting the source's animation
        # through the parent.
        nb.head = head
        nb.tail = tail
        nb.roll = 0.0
        nb.transform(into_src)
        nb.roll = _ebone_roll_to_vector(
            nb, (into_src.to_3x3() @ z_axis).normalized())
        nb.parent = s_ebone
        nb.use_connect = False
        made.append(name)
    bpy.ops.object.mode_set(mode="OBJECT")

    for name in made:
        b = src.data.bones.get(name)
        if b is not None:
            coll.assign(b)

    # Constrain the rig's controls to the proxies.
    n = 0
    for t_name, _ in pairs:
        name = t_name + _RET_SUFFIX
        if name not in src.data.bones:
            continue
        pb = trg.pose.bones[t_name]
        for kind in (("COPY_ROTATION", "COPY_LOCATION")
                     if t_name in _LOC_BONES else ("COPY_ROTATION",)):
            c = pb.constraints.new(kind)
            c.name = kind.title().replace("_", " ") + " [retarget]"
            c.target = src
            c.subtarget = name
            n += 1

    # The mapping drives the FK chain, so the IK solvers must stand down.
    for bn in _IKFK_SWITCHES:
        pb = trg.pose.bones.get(bn)
        if pb is not None and "IK_FK" in pb:
            pb["IK_FK"] = 1.0
    trg.data.update_tag()
    return n, len(made)


def _ret_do_unbind(context, src, trg):
    removed = 0
    for pb, c in _ret_constraints(trg, src):
        pb.constraints.remove(c)
        removed += 1
    names = _ret_proxies(src)
    if names:
        _ret_activate(context, src, "EDIT")
        for n in names:
            eb = src.data.edit_bones.get(n)
            if eb is not None:
                src.data.edit_bones.remove(eb)
        bpy.ops.object.mode_set(mode="OBJECT")
    coll = src.data.collections.get(_RET_COLL)
    if coll is not None:
        src.data.collections.remove(coll)
    return removed, len(names)


# --- operators -------------------------------------------------------------

class ANIM_OT_retarget_align(bpy.types.Operator):
    """Rotate, scale and position the source skeleton to match the rig.

Everything is measured from the two REST poses, so it does not matter where
the clip's root motion has carried the character to on the current frame"""
    bl_idname = "anim.retarget_align"
    bl_label = "Align Source to Rig"
    bl_options = {"REGISTER", "UNDO"}

    do_rotate: BoolProperty(
        name="Match Facing",
        description="Yaw the source so it faces the same way as the rig. "
                    "Game skeletons are often authored 180 degrees round",
        default=True,
    )
    do_scale: BoolProperty(name="Match Height", default=True)
    do_move: BoolProperty(name="Match Hips", default=True)

    @classmethod
    def poll(cls, context):
        return _ret_state(context)["ready"]

    def _hip_axis(self, ob, left, right):
        """World-space left-to-right vector across the hips, at rest."""
        bones = ob.data.bones
        if left not in bones or right not in bones:
            return None
        a = ob.matrix_world @ bones[left].head_local
        b = ob.matrix_world @ bones[right].head_local
        v = (b - a)
        v.z = 0.0
        return v.normalized() if v.length > 1e-9 else None

    def execute(self, context):
        if not _ret_guard(self, context):
            return {"CANCELLED"}
        st = _ret_state(context)
        src, trg = st["src"], st["trg"]
        done = []

        # --- facing -------------------------------------------------------
        # Compare the hip axis of each skeleton and yaw the source onto it.
        # This is what catches the 180 that game rigs so often arrive with.
        if self.do_rotate:
            s_ax = self._hip_axis(src, "l_thigh", "r_thigh")
            t_ax = (self._hip_axis(trg, "ORG-l_thigh", "ORG-r_thigh")
                    or self._hip_axis(trg, "l_thigh", "r_thigh"))
            if s_ax and t_ax:
                ang = s_ax.angle(t_ax)
                if s_ax.cross(t_ax).z < 0.0:
                    ang = -ang
                if abs(ang) > 0.0017:  # ~0.1 degree, ignore noise
                    src.rotation_mode = "XYZ"
                    src.rotation_euler.z += ang
                    context.view_layer.update()
                    done.append("yawed %.1f deg" % degrees(ang))
            else:
                self.report({"WARNING"},
                            "No thigh bones to measure facing from")

        # --- height -------------------------------------------------------
        if self.do_scale:
            s_h = _ret_height(src, "r_foot", "head")
            t_h = (_ret_height(trg, "ORG-r_foot", "ORG-head")
                   or _ret_height(trg, "r_foot", "head"))
            if not s_h or not t_h or abs(s_h) < 1e-9:
                self.report({"ERROR"}, "Could not measure one of the skeletons")
                return {"CANCELLED"}
            k = t_h / s_h
            src.scale = [v * k for v in src.scale]
            context.view_layer.update()
            done.append("scaled %.4f" % k)

        # --- position (last: rotation and scale both move the hips) -------
        if self.do_move:
            t_anchor = ("ORG-hips" if "ORG-hips" in trg.data.bones
                        else ("hips" if "hips" in trg.data.bones else None))
            if "hips" in src.data.bones and t_anchor:
                want = trg.matrix_world @ trg.data.bones[t_anchor].head_local
                have = src.matrix_world @ src.data.bones["hips"].head_local
                src.location = src.location + (want - have)
                context.view_layer.update()
                done.append("hips matched")

        self.report({"INFO"}, "Aligned: " + (", ".join(done) or "nothing to do"))
        return {"FINISHED"}


class ANIM_OT_retarget_bind(bpy.types.Operator):
    """Build proxy bones on the source and constrain the rig's controls to them"""
    bl_idname = "anim.retarget_bind"
    bl_label = "Bind"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _ret_state(context)["ready"]

    def execute(self, context):
        if not _ret_guard(self, context):
            return {"CANCELLED"}
        st = _ret_state(context)
        src, trg = st["src"], st["trg"]
        if st["bound"] or st["proxies"]:
            _ret_do_unbind(context, src, trg)
        pairs = _ret_pairs_for(src, trg)
        n, made = _ret_do_bind(context, src, trg, pairs)
        _ret_activate(context, trg)
        self.report({"INFO"}, "Bound %d constraints via %d proxy bones" % (n, made))
        return {"FINISHED"}


class ANIM_OT_retarget_bake(bpy.types.Operator):
    """Bake the bound motion onto the rig as real keyframes.

Bakes every constrained control over the source clip's full range, then
verifies the result before anything is thrown away"""
    bl_idname = "anim.retarget_bake"
    bl_label = "Bake"
    bl_options = {"REGISTER", "UNDO"}

    def_name: StringProperty(name="Action Name", default="")

    @classmethod
    def poll(cls, context):
        return _ret_state(context)["bound"] > 0

    def execute(self, context):
        if not _ret_guard(self, context):
            return {"CANCELLED"}
        sc = context.scene
        st = _ret_state(context)
        src, trg = st["src"], st["trg"]

        # Range comes from the clip itself, not from whatever the timeline
        # happens to be showing.
        pinned = trg.get("anim_retarget_clip", "")
        act = src.animation_data.action if src.animation_data else None
        want = bpy.data.actions.get(pinned) if pinned else None
        if want is not None and act is not want:
            if src.animation_data is None:
                src.animation_data_create()
            src.animation_data.action = want
            bind_slot(src.animation_data, want)
            context.view_layer.update()
            act = want
            self.report({"WARNING"},
                        "Source clip had changed - restored '%s'" % want.name)
        if act is not None:
            f0, f1 = (int(round(v)) for v in act.frame_range)
        else:
            f0, f1 = sc.frame_start, sc.frame_end
        if f1 <= f0:
            self.report({"ERROR"}, "Source clip is a single frame")
            return {"CANCELLED"}

        vis = {c.name: c.is_visible for c in trg.data.collections}
        hid = [b.name for b in trg.data.bones if b.hide]
        for c in trg.data.collections:
            c.is_visible = True
        for b in trg.data.bones:
            b.hide = False

        _ret_activate(context, trg, "POSE")
        names = [pb.name for pb, _ in _ret_constraints(trg, src)]
        names = list(dict.fromkeys(names))
        for pb in trg.pose.bones:
            pb.select = pb.name in names
        if not names:
            self.report({"ERROR"}, "Nothing is bound")
            return {"CANCELLED"}
        trg.data.bones.active = trg.data.bones[names[0]]

        bpy.ops.nla.bake(frame_start=f0, frame_end=f1, step=1,
                         only_selected=True, visual_keying=True,
                         clear_constraints=False, clear_parents=False,
                         use_current_action=False, bake_types={"POSE"})

        for c in trg.data.collections:
            c.is_visible = vis.get(c.name, True)
        for n in hid:
            b = trg.data.bones.get(n)
            if b is not None:
                b.hide = True

        new = trg.animation_data.action
        label = self.def_name.strip() or (act.name if act else "Retargeted")
        new.name = label
        new.use_fake_user = True
        # Stamp it, so the browser can tell a bake from an imported clip and
        # the delete button knows what it is looking at.
        new["retarget_baked"] = trg.name
        new["retarget_from"] = act.name if act else ""
        if act is not None and "cast_hash" in act.keys():
            new["cast_hash"] = act["cast_hash"]

        fcs = _action_fcurves(new)
        bones = set()
        for fc in fcs:
            if fc.data_path.startswith("pose.bones"):
                try:
                    bones.add(fc.data_path.split('"')[1])
                except IndexError:
                    pass
        if len(bones) < len(names) * 0.9:
            self.report({"WARNING"},
                        "Baked only %d of %d bones - do NOT unbind yet"
                        % (len(bones), len(names)))
        else:
            self.report({"INFO"},
                        "Baked %s: %d bones, frames %d-%d"
                        % (new.name, len(bones), f0, f1))
        return {"FINISHED"}


class ANIM_OT_retarget_unbind(bpy.types.Operator):
    """Remove the retarget constraints and proxy bones.

Refuses to run if there is no verified bake, because until then the
constraints are the only thing holding the animation"""
    bl_idname = "anim.retarget_unbind"
    bl_label = "Unbind"
    bl_options = {"REGISTER", "UNDO"}

    force: BoolProperty(name="Discard Unbaked Motion", default=False)

    @classmethod
    def poll(cls, context):
        st = _ret_state(context)
        return st["bound"] > 0 or st["proxies"] > 0

    def execute(self, context):
        if not _ret_guard(self, context):
            return {"CANCELLED"}
        st = _ret_state(context)
        if st["baked"] is None and not self.force:
            self.report({"ERROR"},
                        "No baked action found - bake first, or tick "
                        "Discard Unbaked Motion to throw the bind away")
            return {"CANCELLED"}
        c, p = _ret_do_unbind(context, st["src"], st["trg"])
        _ret_activate(context, st["trg"])
        self.report({"INFO"}, "Removed %d constraints and %d proxy bones" % (c, p))
        return {"FINISHED"}


class ANIM_OT_retarget_tidy(bpy.types.Operator):
    """Hide the source skeleton and leave only the rig visible"""
    bl_idname = "anim.retarget_tidy"
    bl_label = "Hide Source Skeleton"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        st = _ret_state(context)
        return st["src"] is not None

    def execute(self, context):
        if not _ret_guard(self, context):
            return {"CANCELLED"}
        st = _ret_state(context)
        src, trg = st["src"], st["trg"]
        n = 0
        for o in bpy.data.objects:
            if o is src or (o.parent is src and o.type == "MESH"):
                if o.name in context.scene.objects:
                    o.hide_set(True)
                    n += 1
        if trg is not None:
            _ret_activate(context, trg)
        self.report({"INFO"}, "Hid %d source object(s)" % n)
        return {"FINISHED"}


class ANIM_OT_retarget_run(bpy.types.Operator):
    """Align, bind, bake, unbind and tidy up in one go"""
    bl_idname = "anim.retarget_run"
    bl_label = "Run All Steps"
    bl_options = {"REGISTER", "UNDO"}

    do_align: BoolProperty(name="Align Source First", default=False)

    @classmethod
    def poll(cls, context):
        return _ret_state(context)["ready"]

    def execute(self, context):
        if not _ret_guard(self, context):
            return {"CANCELLED"}
        if self.do_align:
            bpy.ops.anim.retarget_align()
        bpy.ops.anim.retarget_bind()
        bpy.ops.anim.retarget_bake()
        if _ret_state(context)["baked"] is None:
            self.report({"ERROR"}, "Bake failed - leaving the bind in place")
            return {"CANCELLED"}
        bpy.ops.anim.retarget_unbind()
        bpy.ops.anim.retarget_tidy()
        self.report({"INFO"}, "Retarget complete")
        return {"FINISHED"}


# --- panel -----------------------------------------------------------------

class VIEW3D_PT_anim_retarget(bpy.types.Panel):
    bl_label = "Retarget to Rig"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cast to Rig"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 1

    def draw(self, context):
        sc = context.scene
        layout = self.layout
        st = _ret_state(context)

        col = layout.column(align=True)
        col.prop(sc, "anim_retarget_source", text="Source")
        col.prop(sc, "anim_retarget_target", text="Rig")

        if not st["src"] or not st["trg"]:
            layout.label(text="Pick a source clip rig and a target rig",
                         icon="INFO")
            return
        if st["src"] is st["trg"]:
            layout.label(text="Source and rig are the same object",
                         icon="ERROR")
            return
        if not st["pairs"]:
            layout.label(text="No bones matched between these two",
                         icon="ERROR")
            return

        act = (st["src"].animation_data.action
               if st["src"].animation_data else None)

        blocked = False
        for label, ob in (("Source", st["src"]), ("Rig", st["trg"])):
            why = _ret_blocked(context, ob)
            if why:
                blocked = True
                col = layout.column(align=True)
                col.label(text="%s: %s" % (label, ob.name), icon="ERROR")
                col.label(text=why.split(" - ")[-1][:60])
        if blocked:
            layout.label(text="Check you picked the right objects",
                         icon="INFO")

        if not st["src_clip_fits"]:
            col = layout.column(align=True)
            col.label(text="Source is holding a baked action",
                      icon="ERROR")
            col.label(text="Its channels are rig bones, not source bones")
            col.label(text="Pick a real clip in the browser above")

        box = layout.box()
        total = st["pairs"] + len(st["missing_trg"]) + len(st["missing_src"])
        box.label(text="%d of %d bone pairs matched" % (st["pairs"], total),
                  icon="CHECKMARK" if not (st["missing_trg"] or st["missing_src"])
                  else "ERROR")
        # Naming the gaps matters: a single missing control (a renamed torso,
        # say) silently drops the whole body-level motion and the result just
        # looks wrong rather than broken.
        for label, miss in (("rig", st["missing_trg"]), ("source", st["missing_src"])):
            if miss:
                sub = box.row()
                sub.alert = True
                sub.label(text="not on %s: %s" % (label, ", ".join(miss[:4])
                          + (" ..." if len(miss) > 4 else "")))
        if act:
            f0, f1 = (int(round(v)) for v in act.frame_range)
            box.label(text="Clip: %s  (%d-%d)" % (act.name, f0, f1),
                      icon="ACTION")
        else:
            box.label(text="Source has no action - pick a clip above",
                      icon="ERROR")
        pinned = st["trg"].get("anim_retarget_clip", "")
        if st["bound"] and pinned:
            warn = act is None or act.name != pinned
            box.label(text="Bound to: %s" % pinned,
                      icon="ERROR" if warn else "PINNED")

        # 1. align
        row = layout.row(align=True)
        row.operator("anim.retarget_align", text="1. Align Source to Rig",
                     icon="FULLSCREEN_EXIT")

        # 2. bind
        row = layout.row(align=True)
        icon = "CHECKMARK" if st["bound"] else "CONSTRAINT_BONE"
        row.operator("anim.retarget_bind",
                     text="2. Bind" + (" (%d)" % st["bound"] if st["bound"] else ""),
                     icon=icon)

        # 3. bake
        row = layout.row(align=True)
        row.enabled = st["bound"] > 0
        row.operator("anim.retarget_bake", text="3. Bake", icon="REC")

        if st["baked"] is not None:
            layout.label(text="Baked: %s" % st["baked"].name, icon="CHECKMARK")
        elif st["bound"]:
            layout.label(text="Not baked yet - do not unbind",
                         icon="ERROR")

        # 4. unbind
        row = layout.row(align=True)
        row.enabled = st["bound"] > 0 or st["proxies"] > 0
        row.operator("anim.retarget_unbind", text="4. Unbind", icon="UNLINKED")

        # 5. tidy
        row = layout.row(align=True)
        row.operator("anim.retarget_tidy", text="5. Hide Source Skeleton",
                     icon="HIDE_ON")

        layout.separator()
        layout.operator("anim.retarget_run", text="Run All Steps",
                        icon="PLAY")

        queued = sum(1 for a in bpy.data.actions if is_queued(a))
        row = layout.row(align=True)
        row.enabled = queued > 0
        row.operator(
            "anim.retarget_batch",
            text="Batch %d Queued Clip%s" % (queued, "" if queued == 1 else "s")
                 if queued else "Batch Queued Clips",
            icon="SEQUENCE",
        )
        if not queued:
            sub = layout.row()
            sub.enabled = False
            sub.label(text="Tick clips in the list to queue them")


class ANIM_OT_retarget_batch(bpy.types.Operator):
    """Retarget and bake every clip ticked in the browser's queue.

Each queued clip is assigned to the source in turn, bound, baked and
unbound. Clips this rig already has a bake of are skipped, and a clip that
fails is logged and stepped over rather than stopping the run"""
    bl_idname = "anim.retarget_batch"
    bl_label = "Batch Queued Clips"
    bl_options = {"REGISTER", "UNDO"}

    skip_existing: BoolProperty(
        name="Skip Already Baked",
        description="Leave a clip alone if this rig already has a bake of it",
        default=True,
    )
    do_align: BoolProperty(
        name="Align Source First",
        description="Run step 1 once, before the first clip",
        default=False,
    )
    do_tidy: BoolProperty(
        name="Hide Source When Done",
        description="Run step 5 once, after the last clip",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _ret_state(context)["ready"]

    def _queue(self, context):
        """Queued clips that are actually retargetable inputs."""
        st = _ret_state(context)
        src, trg = st["src"], st["trg"]
        out = []
        for act in bpy.data.actions:
            if not is_queued(act):
                continue
            # A bake or an asset is an output of this tool, not an input.
            if clip_kind(act) in {"BAKE", "ASSET"}:
                continue
            if not _ret_action_fits(src, act):
                continue
            if self.skip_existing and _ret_bakes(trg, act.name):
                continue
            out.append(act)
        return out

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "skip_existing")
        col.prop(self, "do_align")
        col.prop(self, "do_tidy")
        col.separator()
        queued = sum(1 for a in bpy.data.actions if is_queued(a))
        ready = self._queue(context)
        col.label(text="%d ticked, %d to bake" % (queued, len(ready)),
                  icon="CHECKBOX_HLT")
        for act in ready[:6]:
            sub = col.row()
            sub.enabled = False
            sub.label(text=act.name, icon="ACTION")
        if len(ready) > 6:
            sub = col.row()
            sub.enabled = False
            sub.label(text="... and %d more" % (len(ready) - 6))
        if not ready:
            col.label(text="Nothing to do with these settings", icon="INFO")
            return
        col.label(text="Blender is unresponsive until it finishes",
                  icon="INFO")

    def execute(self, context):
        st = _ret_state(context)
        src, trg = st["src"], st["trg"]
        # Not _ret_guard: it refuses when the source is holding a bake, and a
        # batch replaces the source's action on every pass anyway.
        for ob in (src, trg):
            why = _ret_blocked(context, ob)
            if why:
                self.report({"ERROR"}, why)
                return {"CANCELLED"}

        clips = self._queue(context)
        if not clips:
            self.report({"WARNING"}, "Nothing queued to bake")
            return {"CANCELLED"}

        pairs = _ret_pairs_for(src, trg)
        restore = (src.animation_data.action
                   if src.animation_data else None)

        if self.do_align:
            try:
                bpy.ops.anim.retarget_align()
            except RuntimeError as exc:
                self.report({"WARNING"}, "Align skipped: %s" % exc)

        wm = context.window_manager
        wm.progress_begin(0, len(clips))
        baked, failed = [], []
        for i, act in enumerate(clips):
            wm.progress_update(i)
            try:
                if _ret_constraints(trg, src) or _ret_proxies(src):
                    _ret_do_unbind(context, src, trg)
                assign_action(src, act)
                context.view_layer.update()
                n, _made = _ret_do_bind(context, src, trg, pairs)
                if not n:
                    failed.append(act.name)
                    continue
                bpy.ops.anim.retarget_bake()
                # The rig keeps the previous bake until a new one lands, so
                # "did it work" has to be asked of the stamp, not of whether
                # the rig is holding an action.
                new = trg.animation_data.action if trg.animation_data else None
                if new is not None and new.get("retarget_from") == act.name:
                    baked.append(new.name)
                else:
                    failed.append(act.name)
            except Exception as exc:
                print("[Cast to Rig] batch failed on %s: %r" % (act.name, exc))
                failed.append(act.name)
            finally:
                try:
                    _ret_do_unbind(context, src, trg)
                except Exception:
                    pass
        wm.progress_end()

        if restore is not None:
            try:
                assign_action(src, restore)
            except Exception:
                pass
        if self.do_tidy:
            try:
                bpy.ops.anim.retarget_tidy()
            except RuntimeError:
                pass

        bump_order()
        rebuild_cache()
        message = "Baked %d of %d queued clip(s)" % (len(baked), len(clips))
        if failed:
            message += " - %d failed: %s" % (
                len(failed), ", ".join(failed[:3])
                + (" ..." if len(failed) > 3 else ""))
            self.report({"WARNING"}, message)
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class VIEW3D_PT_anim_asset_send(bpy.types.Panel):
    """Promoting a bake to an asset, after the retarget that produced it.

    This sits below Retarget to Rig on purpose: what you send to the Asset
    Browser is the bake those steps produce, so a "this is not a bake yet"
    warning reads as the next step rather than as something gone wrong.
    """
    bl_label = "Send Bake to Asset Browser"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cast to Rig"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        action, stood_in_for = asset_candidate(context)

        if action is None:
            layout.label(text="No clip selected", icon="INFO")
            return

        if is_asset(action):
            box = layout.box()
            box.label(text=action.name, icon="ASSET_MANAGER")
            sub = box.row()
            sub.enabled = False
            sub.label(text="Already an animation asset")
            layout.operator(ANIM_OT_browser_open_asset_browser.bl_idname,
                            text="Open Asset Browser", icon="FILEBROWSER")
            return

        # A bake this add-on made is the intended input. Anything else is
        # allowed - a hand-keyed action on the rig is perfectly valid - but it
        # is called out, because promoting a raw Cast clip gives you an asset
        # whose channels are source-skeleton bones and will not drive the rig.
        # Naming the resolved bake matters: the row highlighted in the browser
        # is the Cast clip, so without this the panel would be talking about an
        # Action the user cannot see selected anywhere.
        if stood_in_for is not None:
            box = layout.box()
            box.label(text="Bake from %s" % stood_in_for.name, icon="ACTION")
            sub_row = box.row()
            sub_row.enabled = False
            sub_row.label(text=action.name)
        elif clip_kind(action) != "BAKE":
            col = layout.column(align=True)
            col.alert = True
            col.label(text="Not a retarget bake", icon="ERROR")
            col.label(text="Run the retarget steps above first,")
            col.label(text="unless this is already keyed on the rig")

        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        row = col.row(align=True)
        row.prop(scene, "anim_asset_catalog", text="Catalog")
        row.operator(ANIM_OT_browser_pick_catalog.bl_idname, text="",
                     icon="DOWNARROW_HLT")
        col.prop(scene, "anim_asset_name", text="Name")
        col.prop(scene, "anim_asset_description", text="Notes")
        col.prop(scene, "anim_asset_tags", text="Tags")
        col.prop(scene, "anim_asset_tag_source")
        col.prop(scene, "anim_asset_preview")

        start, end = clip_span(action)
        frames = int(round(end - start))
        sub = layout.row()
        sub.enabled = False
        sub.label(
            text="%s  -  %d frames" % (scene.anim_asset_name.strip() or action.name,
                                       frames),
            icon="SEQUENCE",
        )
        if is_static(action):
            layout.label(text="Single frame - a pose, not an animation",
                         icon="ERROR")

        col = layout.column(align=True)
        col.scale_y = 1.3
        col.operator(ANIM_OT_browser_make_asset.bl_idname,
                     text="Create Animation Asset", icon="ASSET_MANAGER")

        if not bpy.data.filepath:
            layout.label(text="Save the .blend to use catalogs", icon="INFO")

        assets = sum(1 for a in bpy.data.actions if is_asset(a))
        if assets:
            layout.operator(ANIM_OT_browser_open_asset_browser.bl_idname,
                            text="Open Asset Browser", icon="FILEBROWSER")
            row = layout.row()
            row.enabled = False
            row.label(text="%d animation asset%s in this file"
                           % (assets, "" if assets == 1 else "s"))


_CLASSES = (
    VIEW3D_UL_anim_clips,
    ANIM_OT_browser_refresh,
    ANIM_OT_browser_step,
    ANIM_OT_browser_play,
    ANIM_OT_browser_revert_name,
    ANIM_OT_browser_pick_catalog,
    ANIM_OT_browser_make_asset,
    ANIM_OT_browser_clear_asset,
    ANIM_OT_browser_repreview,
    ANIM_OT_browser_assign_asset,
    ANIM_OT_browser_open_asset_browser,
    ANIM_OT_browser_apply,
    ANIM_OT_browser_queue,
    ANIM_OT_browser_delete,
    ANIM_OT_browser_fix_slot,
    VIEW3D_PT_anim_browser,
    VIEW3D_PT_anim_browser_filters,
    VIEW3D_PT_anim_browser_clip,
    ANIM_OT_retarget_align,
    ANIM_OT_retarget_bind,
    ANIM_OT_retarget_bake,
    ANIM_OT_retarget_unbind,
    ANIM_OT_retarget_tidy,
    ANIM_OT_retarget_run,
    ANIM_OT_retarget_batch,
    VIEW3D_PT_anim_retarget,
    VIEW3D_PT_anim_asset_send,
)

_SCENE_PROPS = (
    "anim_browser_index",
    "anim_browser_search",
    "anim_browser_label",
    "anim_browser_show",
    "anim_browser_unlabelled_only",
    "anim_browser_queued_only",
    "anim_browser_favs_only",   # retired in 3.0
    "anim_browser_stable_order",
    "anim_browser_hide_static",
    "anim_browser_autorange",
    "anim_browser_target",
    "anim_asset_catalog",
    "anim_asset_name",
    "anim_asset_description",
    "anim_asset_tags",
    "anim_asset_tag_source",
    "anim_asset_preview",
    "anim_retarget_source",
    "anim_retarget_target",
    # Retired in 2.0 (the pose-asset panel); dropped on register so an upgrade
    # in place doesn't leave dead properties on the Scene.
    "anim_browser_pose_library",
    "anim_browser_pose_catalog",
    "anim_browser_pose_name",
    "anim_browser_pose_all_bones",
)


_RETIRED_PROPS = (
    "anim_browser_pose_library",
    "anim_browser_pose_catalog",
    "anim_browser_pose_name",
    "anim_browser_pose_all_bones",
    # 3.0: favourites became the batch queue
    "anim_browser_favs_only",
)


def register():
    # Upgrading in place from 1.x leaves the old pose-asset properties on the
    # Scene; clear them so nothing stale survives the reload.
    for attr in _RETIRED_PROPS:
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)

    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.anim_browser_index = IntProperty(
        name="Clip", default=0, min=0, update=_on_index_change
    )
    bpy.types.Scene.anim_browser_search = StringProperty(
        name="Search", description="Filter clips by name", default=""
    )
    bpy.types.Scene.anim_browser_show = EnumProperty(
        name="Show",
        description="Which rows the list shows",
        items=[
            ("CLIPS", "Clips", "Only Actions that are not assets yet"),
            ("ASSETS", "Assets", "Only Actions marked as animation assets"),
            ("ALL", "All", "Every Action in the file"),
        ],
        default="CLIPS",
    )
    bpy.types.Scene.anim_browser_hide_static = BoolProperty(
        name="Hide Poses",
        description="Hide single-frame clips",
        default=True,
    )
    bpy.types.Scene.anim_browser_label = StringProperty(
        name="Name",
        description="Rename this clip. The imported name is kept so you can "
                    "still search for it and restore it later",
        default="",
        update=_on_label_change,
    )
    bpy.types.Scene.anim_browser_queued_only = BoolProperty(
        name="Queued Only",
        description="Show only clips ticked for the batch retarget",
        default=False,
    )
    bpy.types.Scene.anim_browser_unlabelled_only = BoolProperty(
        name="Unnamed Only",
        description="Show only clips you haven't renamed yet",
        default=False,
    )
    bpy.types.Scene.anim_browser_stable_order = BoolProperty(
        name="Keep Order",
        description="Order the list by each clip's imported name, so renaming "
                    "a clip doesn't move its row and lose your place",
        default=True,
    )
    bpy.types.Scene.anim_asset_catalog = StringProperty(
        name="Catalog",
        description="Asset catalog path, e.g. Helldiver/Locomotion. Created in "
                    "blender_assets.cats.txt beside this .blend if it is new",
        default="",
    )
    bpy.types.Scene.anim_asset_name = StringProperty(
        name="Name",
        description="Rename the clip as it is promoted. Blank keeps the name "
                    "it already has",
        default="",
    )
    bpy.types.Scene.anim_asset_description = StringProperty(
        name="Notes",
        description="Asset description. Blank writes the clip's length and "
                    "source hash",
        default="",
    )
    bpy.types.Scene.anim_asset_tags = StringProperty(
        name="Tags",
        description="Comma-separated tags, e.g. locomotion, loop",
        default="",
    )
    bpy.types.Scene.anim_asset_tag_source = BoolProperty(
        name="Tag With Source Hash",
        description="Add the imported clip name as a tag, so the asset can be "
                    "traced back to the file it came from",
        default=True,
    )
    bpy.types.Scene.anim_asset_preview = BoolProperty(
        name="Render Thumbnail",
        description="Render the 3D viewport at the current frame as the asset "
                    "thumbnail",
        default=True,
    )
    bpy.types.Scene.anim_browser_autorange = BoolProperty(
        name="Fit Range",
        description="Move the scene's Start and End frames to match whichever "
                    "clip you select, so playback covers exactly that clip and "
                    "loops cleanly. Turn it off to keep your own frame range",
        default=True,
    )
    bpy.types.Scene.anim_browser_target = PointerProperty(
        name="Rig",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "ARMATURE",
    )
    bpy.types.Scene.anim_retarget_source = PointerProperty(
        name="Source",
        description="The imported game skeleton carrying the clip",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "ARMATURE",
    )
    bpy.types.Scene.anim_retarget_target = PointerProperty(
        name="Rig",
        description="The Rigify-style rig to receive the animation",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "ARMATURE",
    )
    # NOT rebuild_cache() here: at startup Blender registers add-ons while
    # bpy.data is still restricted, and touching it raises. clip_span() fills
    # the cache lazily on first draw instead.
    if _on_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_file_load)


def unregister():
    if _on_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_file_load)
    for attr in _SCENE_PROPS:
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _RANGE_CACHE.clear()


if __name__ == "__main__":
    register()

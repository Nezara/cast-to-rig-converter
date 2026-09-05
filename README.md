# Cast to Rig Converter

A Blender add-on for the moment after a [Cast](https://github.com/dtzxporter/cast) import
drops three thousand animation clips into your file and the Action Editor's dropdown
stops being a usable way to find anything.

It gives you a searchable clip browser, a retargeter that drives those clips onto a
control rig and bakes them to plain keyframes, and a one-click path from a finished bake
to a catalogued entry in Blender's Asset Browser.

> **Heads up:** the bone map ships configured for one specific pairing — a Cast/Stingray
> game skeleton and the control rig by Lex_Dorkslav. Any other rig needs the map edited.
> See [Adapting it to another rig](#adapting-it-to-another-rig); it is one dictionary.

---

## Requirements

| | |
|---|---|
| Blender | 4.4 or newer (developed and tested on 5.2) |
| Importing clips | [Cast Support](https://github.com/dtzxporter/cast) add-on, or any importer that gives you Actions on an armature |
| Retargeting | a source skeleton carrying the clips, plus a control rig to receive them |

The browsing, naming and asset half works with any Actions on any armature. Only the
retarget half cares about bone names.

## Install

1. Download `anim_browser.py` from this repository.
2. Blender → **Edit ▸ Preferences ▸ Add-ons ▸ Install from Disk**, pick the file.
3. Enable **Cast to Rig Converter**.
4. Open the 3D Viewport sidebar with **N** and choose the **Cast to Rig** tab.

## The workflow

The panels are stacked in the order you use them.

### 1. Browse

Set **Rig** to the armature you want to audition clips on, then click a row. The clip is
applied and the scene's frame range snaps to it, so playback is exactly the clip's length
and loops cleanly. Search filters on name; the arrows step clip to clip; **Filters** hides
single-frame poses, shows only clips you haven't named, or only clips you've queued.

Row icons tell you what each Action is:

| Icon | Kind | Deletable |
|---|---|---|
| ▶ | imported Cast clip | no — protected |
| A | promoted to an Animation Asset | no — protected |
| ⟳ | a bake this add-on produced | yes |
| — | anything else | yes |

An imported clip is the one thing in the file that can't be recreated, so the delete
button refuses to touch it. A bake can always be run again.

### 2. Name

Cast clips arrive named `0x158a4c9b...`. Rename one in **Selected Clip** and the original
hash is stashed on the Action as a `cast_hash` custom property — so search still matches
the hash, the ↺ button restores it, and the list keeps sorting by the imported name so a
rename never moves the row out from under you. **Unnamed Only** is how you work through a
library without losing your place.

### 3. Retarget to Rig

Pick the **Source** (the imported skeleton holding the clip) and the **Rig** (the control
rig). The panel reports how many bone pairs matched and names any it couldn't find, then
walks five steps:

1. **Align Source to Rig** — yaw, scale and hip-match the source onto the rig. Game
   skeletons frequently arrive 180° round; this is what catches it.
2. **Bind** — build the proxy bones and constraints.
3. **Bake** — bake the constrained controls over the clip's full range.
4. **Unbind** — remove the constraints and proxies. Refuses to run before a verified
   bake exists, because until then the constraints are the only thing holding the motion.
5. **Hide Source Skeleton**.

**Run All Steps** does 2–5 in one go for the current clip.

#### Why proxy bones

The two skeletons don't share bone orientations — across this pair the mean rest
orientation difference is about 142°, so a plain Copy Rotation produces garbage. For every
mapped pair the add-on builds a bone on the *source* armature that carries the *target*
bone's orientation but is parented under the source bone. It inherits the source's
animation while sitting in the target's frame of reference, and the target bone can Copy
Rotation from it directly.

### 4. Batch

Tick the checkbox on any number of rows, then **Batch N Queued Clips** under Run All
Steps. Each queued clip is assigned to the source in turn, bound, baked and unbound.

- Clips the rig already has a bake of are skipped (togglable).
- Bakes and assets are never treated as inputs, nor are Actions whose channels don't
  address the source skeleton.
- A clip that fails is logged to the system console and the run continues; the summary
  reports how many baked and which failed.
- The queue is stored on the Action, so it survives renaming and saving. **Clear Queue**
  in Filters empties it.

Blender is unresponsive while a batch runs. Thirty clips takes a while.

### 5. Send Bake to Asset Browser

With a bake selected, fill in catalog, name, notes and tags, and press **Create Animation
Asset**. The Action is marked in place — nothing is copied, and it keeps its
imported-name provenance. Catalogs are written to `blender_assets.cats.txt` beside the
.blend, parent paths included, so `Locomotion/Sprint` registers both levels.

The panel warns if the selected Action isn't a retarget bake but still lets you proceed —
a hand-keyed action on the rig is perfectly valid. What doesn't work is promoting a raw
Cast clip: its channels address source-skeleton bones and it won't drive the rig.

This makes an **animation** asset — the whole multi-frame Action. Blender's own Create
Pose Asset still handles single-frame poses.

## Adapting it to another rig

The mapping lives in one function, `_build_pairs()`, near the top of the retarget section:

```python
p = {
    "root": "root",
    "torso": "boss",          # rig control : source bone
    "hips_control": "hips",
    "spine1_fk": "spine1",
    ...
}
```

Keys are bone names on your control rig, values are bone names on the imported skeleton.
The loops below it generate the paired limbs, the fingers and the cape chain. Two things
to know:

- `_LOC_BONES` lists the controls that follow the source in world space. Everything else
  is rotation only, because location on an FK bone fights the rig's own hierarchy.
- `_IKFK_SWITCHES` names the sliders that must sit at FK, since the mapping drives the
  `_fk` chains.

The panel tells you exactly which names it couldn't find on each side, which is the
fastest way to work through a new rig.

## Known limitations

- The bone map is rig-specific and edited in code, not in the UI.
- A batch run blocks the Blender UI and offers no progress bar beyond the cursor.
- Deleting clips can't be undone with Ctrl+Z; the confirmation dialog says so.
- Baking names each bake after its clip, so re-baking the same clip leaves `.001`
  duplicates. The browser's delete button has an **All Bakes** scope for clearing them.

## Credits

- The retarget proxy-bone construction and the two roll helper functions are adapted from
  the **Retarget** add-on by **KBS-DEV**, used under GPL-3.0-or-later.
- The default bone map targets the control rig by **Lex_Dorkslav**.
- Cast import is [dtzxporter's Cast](https://github.com/dtzxporter/cast) — a separate
  add-on, not bundled here.

## License

GPL-3.0-or-later. This add-on contains code derived from a GPL-3.0-or-later work, so it
is distributed under the same terms. See [LICENSE](LICENSE).

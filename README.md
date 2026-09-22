# Cast to Rig Converter

A Blender add-on for the moment after a [Cast](https://github.com/dtzxporter/cast) import
drops three thousand animation clips into your file and the Action Editor's dropdown
stops being a usable way to find anything.

It gives you a searchable clip browser that arrives **already knowing what most of the
clips are**, a retargeter that drives those clips onto a control rig and bakes them to
plain keyframes, and a one-click path from a finished bake to a catalogued entry in
Blender's Asset Browser.

> **Heads up:** retargeting ships with one bone map — the Helldiver Cast skeleton onto the
> control rig by Lex_Dorkslav. Any other pairing needs a new profile. See
> [Adding another rig](#adding-another-rig); it is one dictionary entry.

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
and loops cleanly. Search filters on name; the arrows step clip to clip.

**Filters** narrows the list four ways beyond search:

- **Length** — Min/Max frames. Set both to the same number for an exact match.
- **Layer / Stance / Shape** — what the clip *is*, measured from its curves. See
  [What the catalogue knows](#what-the-catalogue-knows).
- **Source** — how the clip's name was arrived at, so you can tell a name the game
  supplied from one somebody guessed.
- **Hide Poses**, **Unnamed Only**, **Queued Only**.

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

Cast clips arrive named `0x158a4c9b...`. Press **Apply Known Names** and every clip the
bundled catalogue recognises is named at once — 1471 of them. Clips you have already
renamed yourself are left alone, because your name outranks the catalogue's.

Rename anything by hand in **Selected Clip**. The original hash is stashed on the Action
as a `cast_hash` custom property, so search still matches the hash, the ↺ button restores
it, and the list keeps sorting by the imported name so a rename never moves the row out
from under you. **Unnamed Only** is how you work through what's left without losing your
place.

### 3. Retarget to Rig

Pick the **Profile** naming the two skeletons you're working with, or press **Detect** to
have the bone pairs counted for you. Then set the **Source** (the imported skeleton
holding the clip) and the **Rig** (the control rig). The panel reports how many bone pairs
matched and names any it couldn't find, then walks five steps:

1. **Align Source to Rig** — yaw, scale and hip-match the source onto the rig. Game
   skeletons frequently arrive 180° round; this is what catches it.
2. **Bind** — build the proxy bones and constraints.
3. **Bake** — bake the constrained controls over the clip's full range.
4. **Unbind** — remove the constraints and proxies. Refuses to run before a verified
   bake exists, because until then the constraints are the only thing holding the motion.
5. **Hide Source Skeleton**.

**Run All Steps** does 2–5 in one go for the current clip.

#### Why proxy bones

The two skeletons don't share bone orientations — across the Helldiver pair the mean rest
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

## What the catalogue knows

3124 Helldiver clips are described in the add-on itself, keyed by the imported hash so the
answers survive re-importing and renaming. Nothing needs to run first.

### Names — 1471 clips

Three tiers, and the **Source** filter tells them apart because they are not equally
trustworthy:

| Source | Count | How |
|---|---|---|
| **Named State** | 344 | The game's animation state machine names the state outright. This is the game's own word. |
| **Weapon event** | 475 | The transitions into a state name a weapon — `reload_stalwart` reaches only the three stance variants of one reload — so the weapon comes from the event and the stance from measuring the clip. |
| **Manual** | 652 | Catalogued by eye. A trailing `?` is the cataloguer's own doubt, kept rather than guessed away. |

Weapon names are the game's *internal* identifiers, which are not always what a player
sees. Some are exact (`stalwart` is the M-105 Stalwart, `faf` the FAF-14 Spear), others
are codenames with no in-game string at all (`broomhandle`, `ripley`, `nacho`).

### Facets — 3122 clips

Three independent properties, each measured from the curves, which compose: Base + Prone +
Cycle is the crawl cycles.

| Facet | Values | Measured as |
|---|---|---|
| **Layer** | Base, Additive | Mean deviation from identity. Additive clips are deltas blended onto a base pose — recoil, flinch, per-gait layers — and alone they look like a twitching T-pose. The two populations sit far apart with nothing near the threshold. |
| **Stance** | Upright, Crouch, Prone | Head height over its rest height. Asked only of Base clips, because an additive delta leaves the head at rest height and would always read Upright. |
| **Shape** | Cycle, Transition, One-shot, Pose | A Pose is a single key; a Transition ends in a different stance than it started; a Cycle repeats. Cycle is judged by a gait test rather than measured outright, and is the least certain of the four. |

Nothing is measured when you use the add-on. Working these out means reading every curve
of every clip, which takes the better part of a minute on a full library and lands on the
same answer every time — the curves are the same curves for everyone who imports them. So
it was done once and the results are in the table. Clips outside the catalogue simply have
no facets, and the three filters pass over them.

## Adding another rig

Every pairing the add-on knows lives in `RIG_PROFILES`, one entry per source skeleton and
control rig. Adding a creature means adding an entry and nothing else:

```python
"MY_CREATURE": {
    "label": "My Creature",
    "info":  "cha_my_creature Cast skeleton onto its Rigify rig",
    "pairs": _my_creature_pairs(),   # rig control : source bone
    "loc":   {...},                  # controls that follow the source in world space
    "ikfk":  (...),                  # sliders that must sit at FK
    "hips":   (source bones, target candidates),
    "height": (source bones, target candidates),
    "anchor": (source bones, target candidates),
},
```

Add the id to `_PROFILE_ORDER` and it appears in the Profile dropdown.

- `pairs` keys are bone names on your control rig, values are bone names on the imported
  skeleton.
- `loc` lists the controls that follow the source in world space. Everything else is
  rotation only, because location on an FK bone fights the rig's own hierarchy.
- `ikfk` names the sliders that must sit at FK, since the mapping drives the `_fk` chains.
- `hips`, `height` and `anchor` are the measurements **Align** uses. Each is a pair of
  (source bones, target candidates tried in order), so a rig that kept its `ORG-` bones
  and one that did not both work.

**Detect** scores every profile against the two armatures you've selected and tells you
which fits, which is the fastest way to start a new one. The panel also names exactly
which bones it couldn't find on each side.

## Known limitations

- Bone maps are edited in code, not in the UI.
- The bundled catalogue is Helldiver-specific. Other creatures' clips browse and retarget
  fine, they just arrive unnamed and uncategorised.
- `Cycle` is the weakest facet — it cross-validated at about 92%, where Layer and Stance
  are effectively exact. Compound clips ("walk forward while wiping head") sit on the
  fence by nature.
- A batch run blocks the Blender UI and offers no progress bar beyond the cursor.
- Deleting clips can't be undone with Ctrl+Z; the confirmation dialog says so.
- Baking names each bake after its clip, so re-baking the same clip leaves `.001`
  duplicates. The browser's delete button has an **All Bakes** scope for clearing them.

## Credits

- The retarget proxy-bone construction and the two roll helper functions are adapted from
  the **Retarget** add-on by **KBS-DEV**, used under GPL-3.0-or-later.
- The default bone map targets the control rig by **Lex_Dorkslav**.
- Catalogue names derive from the game's own animation state machine, read with
  [filediver](https://github.com/xypwn/filediver) by **xypwn**. Facets are measured from
  the imported curves.
- Cast import is [dtzxporter's Cast](https://github.com/dtzxporter/cast) — a separate
  add-on, not bundled here.

## License

GPL-3.0-or-later. This add-on contains code derived from a GPL-3.0-or-later work, so it
is distributed under the same terms. See [LICENSE](LICENSE).

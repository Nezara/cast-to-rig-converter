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
                length. Search by name, hide the single-frame poses, narrow
                to a frame-length range, and step through with the arrow
                buttons.
  2. Name     - rename clips in place to catalogue them. The first rename
                stashes the original name in a "cast_hash" custom property, so a
                clip renamed to "crouch idle" can still be traced back to the
                file it came from - and search matches the stashed hash as well
                as the visible name. "Unnamed only" filters to the clips you
                haven't named yet, which is how you work through a library
                without losing your place.
  3. Retarget - the Retarget to Rig panel drives the source skeleton's motion
                onto the control rig and bakes it down to plain keyframes. Pick
                the Profile that names the two skeletons you are working with,
                or press Detect to have it counted out for you. Tick clips in
                the list to queue them and Batch Queued Clips runs the whole
                loop - bind, bake, unbind - over every one of them, skipping
                clips the rig already has a bake of.
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
  The Helldiver bone map targets the control rig by LexDorkalv. Every rig
  the add-on knows lives in RIG_PROFILES below, one entry per pair of source
  skeleton and control rig, and adding a creature means adding an entry
  there and nothing else.

LICENSE
  GPL-3.0-or-later. This add-on contains code derived from a GPL-3.0-or-later
  work, so it is distributed under the same terms.
"""

bl_info = {
    "name": "Cast to Rig Converter",
    "author": "Nezara",
    "version": (3, 2, 0),
    "blender": (4, 4, 0),
    "location": "3D Viewport > Sidebar (N) > Cast to Rig",
    "description": "Search and audition a large Cast Action library, name it "
                   "from a bundled catalogue, sort it by measured motion, "
                   "retarget clips onto a control rig via a per-creature "
                   "bone-map profile, and save the bakes as Animation Assets",
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
# Bundled catalogue
# ----------------------------------------------------------------------------
#
# A Cast import names every Action after the animation file's hash, so a fresh
# library is several thousand rows of "0x3421c63e1854b52c" and nothing else,
# with no way to tell a walk cycle from a death without playing it.
#
# Both problems were solved once, and the answers are baked in here rather
# than recomputed on every machine.
#
# NAMES. The game ships an animation state machine, and a dump of it says
# which animation each state plays. Where the state carries a name, that is
# the clip's name outright. Where it does not, the transitions leading into it
# often name a weapon - reload_stalwart reaches only the three stance variants
# of one reload, so the weapon comes from the event and the stance from
# measuring the clip. The rest were catalogued by eye.
#
# FACETS. Three letters per clip: layer, stance, shape.
#   layer   B base, A additive. Additive clips are deltas blended onto a base
#           pose - recoil, flinch, the per-gait layers - and alone they look
#           like a twitching T-pose. Measured as mean deviation from identity,
#           which splits the library with nothing near the threshold.
#   stance  U upright, C crouch, P prone, - not applicable. Head height over
#           its rest height, asked only of base clips because an additive
#           delta leaves the head at rest height and would always read upright.
#   shape   C cycle, T transition, O one-shot, P pose. A pose is a single key;
#           a transition ends in a different stance than it started; a cycle
#           repeats. Cycle is the one judged rather than measured outright.
#
# Everything here is keyed by the imported hash, so it survives re-importing
# and re-naming. A clip measured on this machine always outranks the table -
# Analyse Motion writes real measurements and those win. The table is only
# consulted for clips nobody here has measured.

_CATALOGUE_RAW = """\
# --- state (344) : named by a state in the game's own animation state machine
001f305a0d9f127b BUP Prone Aim Blocked
003dc1f2ef197da7 A-O Add Standing Idle
01007ee74c11f732 A-O Add Weapon Fire AssaultShotgun
01bd8c08fad25c35 A-O Add Crouch Move
028e8e41b6cd2bb4 BPC Melee Prone Knife Stab Left
034dd9cbe0beb32e BUT Melee Weapon Pistol
04089502e70afea2 A-O Add Crouch Jog
04ca0830ba554d06 BUO Weapon Firemode
0561b9d32714bd73 BUO Standing Aim Blocked High
0600cf109986f695 BPO Prone Aim Block Rifle
0610ce3c6c7764cc BUO Prone Aim Blocked
067bfca69a82393b BUO Override Standing Foliage
06c4b3c9bd5a91b0 A-O Add Weapon Fire Laser
07da5019fb9924a8 A-O Add Prone Walk Enter
09936f1647964c89 A-O Add Standing Idle
0a400c9173e95e43 A-O Add Prone Crawl
0a86a49cc25a0286 BPO Melee Prone Punch
0c1f3cc04475995d A-O Add Standing Walk
0c4fcca754883ccf A-P Add Recoil Accumulator Prone
0cf1a6a10b8726a9 BUO Prone Aim Block Pistol
0d5b3429279e83d8 A-O Add Prone Idle
0d71422a3a6f38e0 BUO Fall
0f3a80b0384392e3 BUO Melee Punch
0fe613248e70c0bf BUO Fall
10bc224cde5484ba BUP AngledGrip Laser
13f9d73ad1b76729 BUO Melee Sticky Bonk Walk Back Left
1558c3eb8f3db8ef A-O Add Crouch Jog
15815d8eeba750cd BUC Swimming Loco Back
16462443f4a8acad BUO Standing Bring Heavy Done
175f18c69d856476 BUO Pistol Up
182dd2d09b4044f9 BUO Prone Backpack Dispense
183b88f7cca4488b BUT Hit React Leg R 01
184121a5d4b829b8 A-O Add Standing Sprint
1a2b7915ec2a8bb2 A-O Add Crouch Jog
1b310c1dfa727441 A-O Add Prone Walk Enter
1c98d98956599604 A-O Add Standing Sprint
1d179113e2d0b5eb BUO Standing in Vehicle Gun
1d79bcd852763067 BUP Grenade Sticky
1dac5684a8d8381a BPO Diving Ragdoll Fall 02
1f0eea88c4f98fcc A-O Add Weapon Fire Revolver
1f8a699ab943c82e BPC Melee Prone Punch Right
22b166265ce767ed BCT Swimming Drown
2343009b5869672f BUO Weapon Firemode Switch R
24917087752de376 BPT Land Roll
262b8e4c7b19a222 BUO Standing Gunup
263bda1c20b1f647 BUO Melee Sticky Bonk Stand
28806ab0e3df1027 BUT Standing Punch Forward
2936f830ae0df69d BUT Melee Weapon Rifle Walk Left
2a153fb18303bc23 A-O Add Hit React Hit Small Right
2a995983a8fc8244 A-O Add Standing Sprint
2b75153fbe32bcf0 A-O Add Base Crouch
2b75f31d34cbe05b BPT Hit React Death Fire
2cb3e4dfa7d8c56c A-O Add Weapon Fire Railgun
2cbc63c9e3bfc25c A-P Add Backpack
2cc26cc1ec8de1d4 BUT Melee Bayonet Walk Back
2ee8ce14ede4a8a5 A-O Add Crouch Walk Enter
2f39e57e5b4e355c A-O Add Hit React Hit Small Right
2f5d2fb5b6949cd7 A-P Add Base
30c9973d869e39d7 A-O Add Crouch Jog
3167ce93d3cb6139 BPO Diving Ragdoll Fall
31a1b9bbb4082f9f BPC Melee Prone Sticky Bonk
31e57d0805225788 BUO Standing Aim Rocket
357d73eb687f0049 BUT Melee Sticky Bonk
36032f5cfa59029e A-O Add Prone Sprint Enter
36cd3818910d3837 BUO Melee Punch
37e20d1e94296b19 BPC Melee Prone Punch
38055db29d7a0cc8 A-O Add Standing Jog
38f3fe91f850804a A-O Add Prone Hit Shield
39fdc91dd463161d BPP Land Knockdown
3a87f8a6b9e1df90 BUO Fall
3b314d1755fda426 BPC Melee Prone Punch Right
3bd132ce70b63996 A-O Add Weapon Fire MG
3c068a643981bfff BPO Diving Ragdoll Drown
3c3d26b183a28b33 A-O Add Weapon Fire JAR
3cd81b3bd3557b64 BUT Melee Weapon Pistol
3e283edd1189bfbf BUT Melee Bayonet
3e2b8f5efe65310e BUO Fall
3e7c32d48430d1d7 BCT Hit React Death Headshot
3fadcfa92474f1a3 BUO Melee Weapon Pistol
404c9e046674f4dd A-O Add Weapon Fire PDW
408cec8b1ca81cde A-C Add Crouch Idle
4184b361c785fc78 A-O Add Prone Commend Add
41a9b04947649f3f A-O Add Standing Jog
42f9be151aa62dfd A-O Add Weapon Fire HMG
4637d65762ec67a2 BUC Melee Knife Stab
4668291b4f34c5c5 BUT Melee Weapon Rifle
473f8b9ce5c84bf5 A-O Add Standing Jog
49b255d51b412c80 BUT Melee Bayonet Walk Forward Right
4c4076661d763021 A-O Add Hit React Hit Small Left
4c5bbf8bd955c63b BUT Melee Punch
4d7b9fd331a0b3ed A-O Add Idle
4d97c79753df003f BUO Prone Aim Blocked
4dd2b045074e58ac BUC Melee Knife Stab
4e05cef63c2a89c0 A-O Add Prone Hit Shield
4e12c5b4f2374ad6 BUO Standing Aim Blocked High
4e321edea8a8c0e2 A-O Add Timer
4f4f55dafe202a92 BUO Armory Variation Tug Neck
5006008614721ffc BUO Hover
522fdf4bbf28b4d2 BUO Standing Aim Blocked High
53c35f95470c22bf BUC Swimming Loco
547b2664642bd7a7 A-O Add Crouch Jog Enter
549321e2100e8abe BPO Melee Prone Punch
5545fe3bd8fa95ec BUT Pickup Backpack
556729585b5031ce A-O Add Weapon Fire Pumpshotgun
55906efb6ad2cc12 BUO Prone Aim Block Pdw
564bae8700fdc0e4 A-O Add Standing Idle
5818823b87d02be3 A-C Add Crouch Idle
589131e8a5e2e103 BUO Override Standing Foliage
5907148ffe17b83a BPO Prone Aim Block Pistol
5959517688d7f861 BUT Melee Weapon Pistol
59ba732c65e7df2f A-P Add Action Empty
5a0fdc14a8545b5c BUP Grenade
5ad219e2190a889d BUP Stratagem Ball
5ae8028ab2cae3d8 BUO Emote Big Punch
5b207b251f46458d A-O Add Standing Idle
5b6a45017ac9f5ed BUO Prone Aim Blocked
5bceb20391050f1e A-P Add Backpack Crouch
5bf4807107aa13b9 BUO Armory Idle
5c3574cfe64a15d3 BUO Prone Aim Blocked
5f13d207562869c4 BPC Melee Prone Knife Stab
5f3bb9b2ef52f6e6 A-O Add Hit React Hit Small Right
5fe5a9f607a241d9 BUP Override Crouching Flag
60ac44d0c1800fd7 BUO Override Prone Shield Draw
6118d54ddade366f A-O Add Crouch Idle
61992f2aa1336643 A-O Add Weapon Fire Doublebarrel Shotgun
61a94dea4da9b235 BUO Prone Aim Blocked
61e13162f4a2eba0 BUO Armory Variation Ready
6204a769a561170b BUO Prone Aim Blocked
624c005787a69b10 BPO Diving Ragdoll Drown 02
62cce3fd9013bbfd A-O Add Crouch Move
62f476beec56fd3b A-O Add Crouch Walk Enter
638501a908096cb8 A-O Add Hit React Hit Small Left
65de52a45fd89fda BUO Standing Aim Blocked High
65e79d44da1c6bf7 BPO Prone Aim Block Pdw
66356da5d78a638c A-O Add Foliage Add
6663f70bfe547a99 BUT Hit React Death Gas
66b1949a5e17b1d2 A-O Add Weapon Fire Grenade Launcher
66e114a0b7b685fd BUP AngledGrip Advanced
67937aa2d0ea5579 BPC Melee Prone Sticky Bonk
67adea68461f97aa BUP AngledGrip
67bd5802894a97e7 BUC Melee Knife Stab
688797537fca9253 BPO Prone Aim Block Railgun
68c8b07069cddee8 BUP Override Standing Shield
69c50fb037049e0a A-O Add Prone Sprint Enter
6bcc490abb6412ca A-O Add Standing Idle
6c1ce4e2b32b2bc4 BUT Melee Weapon Rifle
6c812176c20ccefe BUO Standing Aim Blocked High
6ca145a5be790df9 BUT Melee Weapon Rifle
6cf56ec358c65edc A-O Add Weapon Fire AssaultShotgun
6d816f8befa7ab24 BUO Falling2
6d8935d95fbab33b A-O Add Prone Crawl
6e717b073113b41d BUO Prone Aim Block Flamer
6ec40afaeefaba4f A-O Add Weapon Fire Pistol
7019a55e03b6ccb8 BUT Swimming Land Dive Water
714d6ddb2e5dbcf3 A-O Add Prone Walk Enter
7259aea50b7f4b6d BPC Melee Prone Punch Right
72c64fab1b4eb750 A-O Add Weapon Fire Autocannon
72ccabf4ec3e1a60 BUO Melee Punch
72e5f63315a7abe0 A-O Add Crouch Move
7330a6bbe7ac56ab BUT Melee Bayonet
73e5a2380e8c1fc7 A-O Add Foliage Add
74c1b99c3d3b57a4 BUO Standing Aim Blocked High
74efb585872f8900 BUO Melee Bayonet
7609b6298763db67 A-O Add Prone Walk Enter
76756685770cfa99 A-O Add Prone Idle
77afafa7bac8f16f A-O Add Weapon Fire Marksmanrifle
781de11016eb8e39 A-O Add Crouch Idle
7897946320d6c710 A-O Add Standing Sprint
78bc61f81bddc559 A-O Add Crouch Idle
791b2334d7f651b2 BUO Fall
7a4daf7b35844d70 BUC Melee Punch
7b8a9d3613d8629a A-O Add Standing Walk
7ba32474f8e5b997 A-O Add Hit React Hit Small Left
7e0c9844318e7302 A-O Add Crouch Idle
7ed7f567a1922cf3 A-O Add Crouch Jog Enter
803f3bb424ee0fa4 A-O Add Crouch Idle
81d282e87732de66 A-P Add Action Empty
8216035ba279521c A-O Add Standing Sprint
85634e5bdf4de379 BUO Melee Weapon Rifle
85c7ba648d039121 BUO Override Standing Foliage
85ef4cbbf33bf735 BUO Land Medium
86d6b57e8f4edc0d BUT Melee Weapon Pistol
87d0ad363b672b1e BUO Stand Aim Big Throw
87ff303d5289b9fa A-O Add Recoil Accumulator
8af236eb7d7133af A-O Add Crouch Jog
8cda61deacefb5ee BUO Fall
8cf35670f67d5dbe BPO Diving Ragdoll Drown 03
8d38c6b8d3ba89bb BUC Melee Weapon Pistol
8d6353cc6433326a BUO Prone Aim Block Rifle
905959012d7454dc BPO Prone Aim Block LAT
90cac3a290962c38 BUC Melee Knife Stab
90f130e8c72955db BUC Swimming Tread Water
917081f451beb9a1 BUC Melee Weapon Rifle
937dc80fbf1a6cc7 BUP Override Crouching Flag
9397f5f5b2dc5228 BUT Melee Bayonet
93c66ce5d99e6211 BUO Fall
94fa9db929f0ec8f A-O Add Foliage Add
9566a96af5780e3a A-O Add Hit React Hit Small Back
95bf5e66251c0fef A-O Add Recoil Accumulator
961883f75e6bc7c3 A-O Add Weapon Fire JAR Phoenix
96b735d1dfd39b4d BUP Override Crouching Flag
979d12b7c964f731 BUP VerticalGrip Advanced
97c7a1651aae147d BUP Override Standing Flag
985d97e3d322b07d BUO Standing Holding Rifle
9a4ccd242036f2e2 BUO Weapon Drop Support
9a5fc1e2ee4fd891 BUP Override Standing Flag
9b534ca7dfa93a52 BUC Melee Bayonet
9bf7e729369146ba A-O Add Recoil Accumulator
9c73452454f15e53 A-O Add Crouch Jog
9d64f043dcdbea36 BPO Prone Aim Block Machinegun
9f5f0abe38066129 BUO Hover
9f891d34e23ff4b9 BUP VerticalGrip
9fb3fbba1fe0f486 A-O Add Prone Sprint Enter
9fc5852abf9fd8ed BPP Override Prone Shield Aim
9fc7391f2d587eb4 A-P Add Recoil Accumulator
9ff5406da8b962c5 BUT Melee Weapon Rifle
a1e40ddc516198f7 BUT Melee Bayonet
a24cdae700982473 A-O Add Crouch Idle
a5c7817ff552f6ec BPO Diving Ragdoll Drown 04
a6218b1a73a57da5 A-O Add Crouch Move
a6ef0a59e53d73ff BCO Override Crouching Shield
a73ddd97d023b7a3 BUO Melee Knife Stab
a7c4003ce0aebac5 A-O Add Prone Idle
a8a7cc01d1f1047f A-O Add Crouch Move
a94251e58cb10de0 A-O Add Standing Jog
a9525cd4b98acb41 BUO Fall
a955bcd87788de0c A-O Add Hit React Hit Small Front
acba7052f11802c8 BCP Override Prone Shield
ad50396bcf72cda2 A-O Add Crouch Idle
aff74d350660a3e4 BUO Fall
affa5fa514aa18b5 BUO Hover
b06c39e11d5fe96a BUO Melee Sticky Bonk
b09f4d8d2a439fba BPC Melee Prone Sticky Bonk
b193be7e4330ca49 A-O Add Crouch Walk Enter
b1fe8b889e54ae1d A-O Add Standing Jog
b286c5956a017136 A-O Add Prone Jog Enter
b2e6a47c36989ac3 A-O Add Standing Jog
b31802a8b5981b4d A-O Add Weapon Recoil Flamethrower
b36d502091455923 A-O Add Prone Jog Enter
b4856d59e80e45d4 BUO Armory Variation Check Time
b5650ae99d8c197a BUC Melee Sticky Bonk
b58348b6828d5961 BPP Override Prone Shield Aim
b6b16d164205e962 BUC Swimming Loco
b7277f1da8293985 BUT Melee Knife Stab
b8323468fd6b2fdc BUT Melee Punch
b8b171e33d195d12 BUO Armory Variation Scratch Head
b8cad3a20bed5df9 A-O Add Bridge Add
b956bc7019d8673f BUP VerticalGrip Flashlight
bab81a2ff33b4cb2 BPP Override Prone Shield Aim
bb8568b4ab93cce7 BUT Swimming Land Water
bb8b73507cea21a6 BUO Hover
bbf9230fd423ad11 A-O Add Hit React Hit Small Back
bddd886a4316c1c4 A-O Add Crouch Move
bebf821ed3554e66 BPO Prone Aim Block Autocannon
bf40200d7aa736fa BPO Melee Prone Sticky Bonk
bf47809e8d37bc18 BUO Weapon Firemode Switch L
c184ddf39815b9b5 A-O Add Weapon Fire Pistol
c2907d094f847e72 A-O Add Prone Hit Shield
c355306ff68155d1 A-O Add Standing Sprint
c38ca4ed5ca53882 BUC Swimming Loco 02
c3e1a362bb7510b3 BUO Override Standing Shield
c42be7d7bec761f1 A-O Add Prone Jog Enter
c47f3cf36f4137c6 A-C Add Crouch Idle
c5009bbbae50fa92 BPO Melee Prone Punch Right
c58626100a1e7614 A-O Add Hit React Hit Small Front
c7e2ea7998e5c609 BUO Fall
c848f2fa1c1ed8c9 A-O Add Standing Walk
c942988f5acbd279 BPO Prone Aim Block GL
cb9362e31301d0c8 BUO Drop Backpack
cc5c76ea346aac19 BUT Melee Weapon Pistol
cc803ad67ac091b5 BUO Prone Aim Blocked
ce60662745e61a30 BUO Hover
d06d126b61b72316 BUT Melee Weapon Rifle
d0c50df30bf92371 BUO Prone Aim Blocked
d11d8827dcf4b126 BUO Standing Aim Blocked High
d25d4fbbb4ad626f BUO Prone Aim Blocked
d2fe85a791d05363 BUT Melee Bayonet
d339ad63c26cecea BUO Standing Aim Blocked High
d345afcf64b10143 BUO Override Standing Foliage Exit
d380ce551cdc0d46 BUO Melee Knife Stab
d49218cc91a4e973 BUT Melee Weapon Pistol
d6b2f30da571368d BPO Melee Prone Knife Stab
d78e5cecf2336551 BUO Prone Aim Blocked
d79d2ec125ea6a46 BUO Melee Sticky Bonk
db12bf499877ac81 BUT Melee Sticky Bonk
db4405d474a90a64 BUO Fall
dc3a1158d92a0c86 A-O Add Foliage Add
dc8854176eec6fb8 BUT Melee Knife Stab
dced0d75bff86653 BUO Melee Punch
ddaf07311e8297a2 BPT Hit React Legs Disabled
dddbd7e059092e0d BPC Melee Prone Knife Stab Thrust
dddf476529e67215 BUO Hit React Death Electrocution
df91aa81c44bd578 BCT Hit React Arm L 01
e01dbb8df7b2b2d2 BUC Melee Punch
e078ead9d47e6ba0 BUO Override Prone Shield Holster
e0c89d7401274ef4 BUO Fall
e2345e2d04e5f187 BCO Override Crouching Shield
e35747b2c4363ca0 BCT Hit React Arm R 01
e3a25eb98aca88f1 BPO Prone Aim Block Flamer
e4bc9f42fad0a52f BUO Prone Aim Block Railgun
e4c79bb03976831d A-O Add Weapon Fire Arc Shotgun
e4cefa0dcfa70776 BUC Melee Weapon Pistol
e68a2be3fd58c2d8 BUO Fall Aim
e6e8da4d1c3c4d41 A-O Add Hit React Hit Small Back
e713ef243778cd81 BUO Prone Aim Blocked
e7f39cb6e58c29e7 A-O Add Hit React Hit Small Front
e8e9d2579f9a81ec A-P Add Foliage Add Exit
e97cf75b00aa4592 BPO Melee Prone Punch
e99c2fbd06cf0263 BUC Swimming Loco
e9b286b4e68ebe26 BUP Override Standing Shield
e9bee7d9210962f8 A-O Add Crouch Jog Enter
e9f72b52eec5036b BUP Override Standing Shield
ec7f7e5a6f6de872 A-P Add Prone Crawl
ecc09372bcfe7013 BUO Fall
ecde52bd5ce66635 BUO Armory Idle
edee992e350fb49e BUT Hit React Death Head 01
ee719a226cc71d28 BUP Prone Aim Blocked
eed7c191b29520d5 BUO Melee Sticky Bonk
ef3b05584752fbd1 BUO Armory Idle
f0b471ef5292d095 A-O Add Prone Idle
f1811f696b1803ac BUO Prone Aim Blocked
f19daef878596d53 BUT Melee Weapon Rifle
f1c0287ef1d931d3 BUT Melee Sticky Bonk
f202c59142473356 BUO Prone Aim Block Sniper
f2cb31cf454e1b4a A-O Add Prone Talk
f3b2129aef960f90 A-O Add Weapon Fire Lat
f3ca3d1aa9618527 BPO Prone Aim Block Marksmanrifle
f3f4079e60af426a A-O Add Crouch Walk Enter
f422b9f2ca4b23f8 BUT Hit React Leg L 01
f448545b3cc967d6 --- HD - Stand with Rifle
f4b1c68a3f75b5e3 BUO Prone Aim Block GL
f63b6f9fe20602fe BCO Override Crouching Shield
f65893dfa3de341e A-O Add Crouch Idle
f66ebc25b1f91982 BUT Hit React Death Acid
f8df32761089e314 BUO Prone Aim Blocked
f8ee654d7b74a85f A-O Add Stand Sprint Enter
f934cab9a6056551 BUT Melee Punch
fa048ba36e8bebde A-O Add Weapon Fire JAR
fa085eec10dd77f3 BPP Override Prone Shield Aim
facbd9bc682e99d7 BUO Prone Shield Break
fba8b3341dbfe8e3 BUP Override Prone Flag
fc0adad7d70e69c1 BCO Override Crouching Shield Aim
fc51244ab9bd3792 BUO Prone Aim Block LAT
ffe7735ea5d7d27b BPO Prone Aim Block Sniper
# --- weapon (475) : named from a weapon-specific reload / ammo-check / draw / holster event
00334baa093cf5e5 BPO Prone Arm Dynamite
007adff6b7c9e445 BUO Standing Reload Laserrifle Extended
0130c8948c58746d BUO Standing Ammo Check Plasmagun
025c0b82b4b20bac BUO Standing Draw C4 End
02bd04f7904f4428 BUO Standing Reload Battlerifle Fast
058ddbd1f0ddb8f1 BUO Standing Draw Lmgstalwart
062ddafd0a80a1fa BUO Standing Reload Vigilance Fast
07156875671cf8fa BUO Standing Reload SMG Rhino Fast
075c609dbdd8e1c5 BUO Standing Draw Throwing Knife
0853fe19fdec6ba0 BPO Prone Reload Crossbow
08abea01f12b3188 BPO Prone Reload Vigilance
0a000680f0405b71 BUO Standing Ammo Check Volley Gun
0ab04df589dd3ce5 BPO Prone Ammo Check PDW
0bf749b18fc08518 BUO Standing Draw Knife
0c7bb1ff09ea1b35 BUO Standing Reload Double Freedom
0caa33d3dd9022fa BPO Prone Ammo Check Laserpistol
0ceb61fe5cb8c205 BUO Standing Ammo Check Recoilless
0cf6653a19a3c88a BPP Prone Draw Hammer
0e86f5adbd9a8991 A-O Add Fire Recoilless
0e9f6bfb24e0cd05 BUO Standing Reload Smart Pistol Missile
108a4e4f96ef1f5e BUO Standing Reload Shark Pistol One-hand
10c6a44136d843ab BPO Prone Draw Grenade
118129bc4f1c608a BUO Standing Holster Sidearm
11bec1a8b3320d07 BPO Prone Reload Assault Rifle AK
123143509359d992 BUO Standing Reload Stim Exit
123d6af4be370d39 BPO Prone Ammo Check MG
1268cc06a54386bc BUO Standing Ammo Check Marksmanrifle Shark
1340fef1dba21693 BUO Standing Reload Shotgun Nacho Fast
13bac2b8083c5ca1 BUO Standing Reload Broomhandle One-hand
158a9c96cbe4426e BPO Prone Reload Autopistol One-hand
16bfe42b7fc85bce BPO Prone Reload Pistol
178e4b338789c355 BUO Standing Ammo Check Flamer
1a15a9d5a8a40f60 BUO Standing Holster Revolver
1a3036353eef6aaf BUO Standing Reload Trenchgun Fast
1a5bd3fda4f50355 BUP Standing Draw Hammer
1ac8c146bcbca82c BUO Standing Reload Autopistol
1b69c42f2be7a819 BPO Prone Reload Smart Pistol Fast
1c1bd1be2ebb888e BPO Prone Reload Flamerpistol One-hand
1c77a1c5b518ba73 BUO Standing Ammo Check Autocannon
1cbdb62542380389 BPO Prone Reload HMG Fast
1cfabe39ef2e05de BUO Standing Ammo Check Grenadepistol
1cfddb10eecba551 BPO Prone Reload Marksmanrifle
1d24619a61695867 BPO Prone Reload Pistol Extended
1d3f177c160104bf BUO Standing Ammo Check Pistol Shark
1d458e2e14853fea BUO Standing Reload Shark Pistol Fast
1f2662c7aa3ed2df BUO Standing Draw FAF
1fef606ee1630337 BPO Prone Reload Hammer
2027621007c35daa BUO Standing Draw Lasercannon
204a556e16c8720e BUO Standing Draw Map
20a8f280b85107b7 BUO Standing Reload Shotgun Nacho
2267991c00b4c693 BPO Prone Reload Nacho Pistol One-hand
22b3c65fc06aa662 BUO Standing Reload Stim Fast
232a41d4703ce3aa BPO Prone Ammo Check Marksmanrifle Shark
23b999394485c4e7 BUO Standing Reload Assshotgun Fast
23f884d667d23880 BUO Standing Ammo Check MG
243a2d19dc6b7cd0 BPO Prone Ammo Check Grenadelauncher
24a58c6d10195b6f BPO Prone Reload Assault Rifle Helghast
258c6e03a38bbeb2 BPO Prone Reload Trenchgun
25e7e2add6b4ae37 BPO Prone Reload Flamethrower
26bac845f4446f70 BUO Standing Ammo Check Laserpistol
27c36409b99ea44c BUO Standing Ammo Check Plasmapistol
28356dc9985d3c09 BPO Prone Reload FAF
28f4a780570e0a6d BPO Prone Reload Harpoon Gun
28f5f0e0547b8ed1 BUO Standing Reload Crossbow
29d1fd08ffcf800e BUO Standing Ammo Check Lasercannon
2a31ea6bc4d3dc8c BPO Prone Reload Double Freedom
2ad3d0d901516637 BPO Prone Reload Laser Shotgun
2af84aac21a788c8 BPO Prone Holster Throwable
2b5f4bc576ec8a80 BPO Prone Reload Assault Rifle Whisper Fast
2bb02052ea233ce5 BUO Standing Holster Machinegun
2cc119b3e8293c31 BUO Standing Reload Broomhandle
2d8f661313fb087b BUO Standing Reload Autopistol Fast One-hand
2e05b972e09bac07 BUO Standing Holster Sniperrifle
2eccf0a24f945db6 BCO Crouch Reload Airburst
2f0108fd72dc3483 BUO Standing Reload Assshotgun
2f657622a0154ad6 BPO Prone Reload Revolver Fast
2f946d0982b715d9 BPO Prone Reload PDW Fast One-hand Drum
2ff42d510419f6cd BUO Standing Reload Laser Shotgun
306d1cacbf5ed1a8 BUO Standing Reload Grenadepistol One-hand
30917b3045834569 BPO Prone Reload Pistol Fast One-hand
313804304646ac43 BUO Standing Draw Revolver
32622f93493bfd25 BUO Standing Reload Stalwart
32f6302176e39362 BPO Prone Reload Shark Pistol Fast
3361f6760cd7e6f2 BUO Standing Reload Pistol Extended
33e9376e1ea471bc BPO Prone Reload SMG Defender One-hand
3521af31c6fceaf8 BPO Prone Reload Sniperrifle Fast
357cdd1421f476ca BUO Standing Holster Map
363feb417b68a0c5 BPO Prone Reload Smart Pistol One-hand Fast
374ba54eeb4d2e52 BPO Prone Reload Sniperrifle
389b73385157adb6 BPO Prone Reload Magnum One-hand
38aa4d341d543cc9 BPO Prone Reload Pistol Fast
38b3862ba636cce0 BUO Standing Draw Flamethrower
38cbf2026582d9ca BPO Prone Ammo Check Pistol
3a1f2aafd418838a BPO Prone Reload Plasmapistol
3ad9811521bcedbb BUO Standing Reload Flamethrower
3b308c6140f69829 BPO Prone Reload Dart Gun
3b3444973fb32679 BUO Standing Reload Smart Pistol One-hand
3c37b9a10d4946a9 BPO Prone Reload Autocannon
3cb86304f23b53d3 BUO Standing Reload Flamerpistol
3e00686819e5a9a5 BUO Standing Reload Energy Weapon Shark
3e0144283d24d4c8 BPO Prone Reload Patriot Fast
3e451e4a93100ba6 BUO Standing Reload Nacho Pistol Fast
3eb47e7c6e3a234e BUO Standing Reload Sniperrifle Fast
3f5f28bfea7f3ea1 BUO Standing Reload FRV MG Fast
3f760c586c066b3f BUO Standing Reload Autopistol Extended
40e5dbcdf0b2ecbd BPO Prone Reload Energyrevolver One-hand
415aa74750fc33c7 BPO Prone Ammo Check Pumpshotgun
415d466499897cde BPO Prone Reload Stim
43793f241faa5098 BUO Standing Reload Nacho Pistol Fast One-hand
448c741c9c851318 BUO Standing Holster Grenadelauncher
448e26d1b2bce21d A-O Add Fire Plasmablaster
4505274a0b7ef7ee BUO Standing Reload Rifle Drum
452d4b2657b660f6 BUO Standing Reload Revolver Exit
458f8420da56b412 BPO Prone Reload MG
45c8761aa777c61b BPO Prone Reload JAR Phoenix Fast
45f049f0d01bb6b6 BPO Prone Reload Cricket One-hand
468b24b32e3ecab7 BUO Standing Draw Grenade
47ae42858699d206 BPO Prone Draw Flag
49d6653b8a095dc6 BPP Prone Draw Map Done
4a6a7f61ea2ea08f BUO Standing Reload Pistol Fast
4b1411e5c5e88297 BPO Prone Reload Stim Exit
4b894004344790e8 BUO Standing Ammo Check Grenadier
4c534b86225e1663 BUO Standing Reload Energyrevolver One-hand
4c7aae85020b6872 BPO Prone Reload Grenadelauncher
4cd39a141db32565 BUO Standing Holster FAF
4d07d7d6b1ca8988 BUO Standing Reload PDW Fast One-hand
4e085831dd9e7c36 BUO Standing Reload SMG Helghast
4f77ddf9f7b1621e BUO Standing Reload Stalwart Fast
4fe45f896c55b072 BUO Standing Draw C4
50161e1ca47cd8be BUO Standing Reload Assault Rifle Nacho
504e33daf720fcc8 BPO Prone Reload Assault Rifle Whisper
5147a5f7ce4c3015 BUO Standing Reload Assault Rifle Helghast
5287db206eef1197 BUO Standing Reload Laser Rifle Long
53a3b2e761bcc11d BUO Standing Draw Primary
53cc1c82afe38907 BPO Prone Reload SMG Defender
5429bf1b832cb40a BPO Prone Reload Pumpshotgun
543924a157e61f54 BUO Standing Reload Stim One-hand
543d3050f8643dd5 BPO Prone Draw Flag
54d183cf06332a25 BUO Standing Reload Trenchgun
54f6a61dc815c464 BPO Prone Reload Nacho Pistol
553e145a90307e73 BPO Prone Reload PDW Drum
557e5da10e65115e BPO Prone Reload JAR
55a1036ff16b3930 BPO Prone Reload Assault Rifle Helghast Fast
55c317e0e80fd7c2 BUO Standing Draw Autocannon
55d985ec5d93d093 BPO Prone Reload Marksmanrifle Fast
569f2d84b2eacb6e BUO Standing Reload PDW Fast One-hand Drum
56ba4cc70d09b329 BPP Prone Draw Hammer
570f2aeaae57346a BPO Prone Reload Autopistol Fast
57ae0b7f93c31523 BUO Standing Reload Revolver Fast One-hand
5897e67f23bd9e52 BUP Standing Draw Knife
59549c13ccb0a97f BPO Prone Draw Throwing Knife
5a3259a5381559fd BUP Standing Draw C4 End
5a41a59fc4003d1c BUO Standing Reload Revolver Fast
5ab30880f3959fb0 BPO Prone Reload Grenadelauncher Fast
5ae9a21d7721d60e BPO Prone Reload Shark Pistol One-hand Fast
5b47d74c6eee76e6 BPO Prone Draw C4
5b8e262e7eb7bedc BPO Prone Reload Grenadepistol
5c136e47ad81151d BUO Standing Holster Doublebarrelshotgun
5d1a32e22a9cc021 BPO Prone Reload Stim Fast
5d80b7cce44c21a3 BPO Prone Reload Airburst
5da39274a5e7f528 BPO Prone Holster Sidearm
5de1895436e0054a BPO Prone Arm Dynamite
5e0e28c1b8762d01 BUO Standing Reload Pistol Fast Extended
5e4a2f291951d7bb BPO Prone Reload Autopistol Extended
5e7738c122be4f3c BPO Prone Reload Nacho Pistol Fast
5f3deb66ab983522 BPO Prone Reload Patriot
5f4d8d7d6ce14937 BUO Standing Reload Vigilance
5fd3169ec82e93c6 BUO Standing Reload SMG Defender
61343faf6f1d178f BUO Standing Reload FRV MG
61818b9cc993bfa7 BUO Standing Reload SMG Defender Fast One-hand
619670e3a8d7f249 BUO Standing Reload Karbin
61970b0aeffbea71 BPO Prone Reload Flamerpistol
62ad202d68e13d09 BPP Prone Draw Hammer
62c676beea1e6774 BPO Prone Draw Map
631553068a4ac8ca BUO Standing Reload Karbin Fast
645077386f2555e0 BUO Standing Reload Assault Rifle Whisper
64a24fbfadd6b185 BUO Standing Reload Sniperrifle
65ff32dcf155aca5 BUO Standing Reload Rifle Fast
6611cb23db71b6dc A-O Add Draw Map
668ca5919f94deea BUO Standing Reload Shark Pistol
66eac6801fe0fc3f BUO Standing Reload Pistol One-hand
6888dda15c076139 BPO Prone Reload Karbin
68cf32f791fcc2d3 BUO Standing Reload SMG Rhino
6a3e696ac129aabe BPO Prone Ammo Check Revolver
6a9857a969e2c1ef BPO Prone Reload Stalwart
6b76ecc17d53a517 A-O Add Draw Hammer
6bffdb55ef8cd96a BUO Standing Reload Nacho Pistol One-hand
6d807eeaf67b2b68 BUO Standing Ammo Check FAF
6dd01ee002b5ce6d BUO Standing Holster Primary
6e1e746d1e3c61fb BUO Standing Holster LAT
6e7867c063a0317a BPO Prone Reload SMG Rhino Fast
6f3d9f9e2856d3ac BUO Standing Reload Grenadelauncher
6f3e4e7f96c25b97 BPO Prone Arm Dynamite
7009191c24229869 BPO Prone Reload Battlerifle Fast
70b6e911d4935f9d BUO Standing Reload SMG Helghast Fast One-hand
724db2ad7df2ddfd BUO Standing Reload SMG Helghast Fast
72a1b97f532f215e BPO Prone Reload Vigilance Fast
73a6da14ccc642be BUP Standing Draw Knife
73bb25df95d1a0ff BPO Prone Reload Assault Rifle Grenadier Fast
73d0cdf0b8c74475 BUO Standing Reload Stalwart Fast
74649b8629d06bdc BPO Prone Reload Laser Rifle Long
752b3f8f20f8839c BUO Standing Draw Sniperrifle
7552084d55d458ef BPO Prone Reload SMG Defender Fast One-hand
7683ba329ecaca7b BPO Prone Reload Sniper Rifle Helghast
7689db2e9d5ea51e BUP Standing Draw Knife
771711880ad309b9 BPO Prone Ammo Check Bullpup
77788d1e9ad98845 BPO Prone Holster Primary
796daae6db808c7f BPO Prone Ammo Check Lasercannon
79b3bd544b781a55 BPO Prone Reload Magnum Fast
7a2100e4ca959669 BUO Standing Holster Recoilless
7a22714db295fcfd BPO Prone Reload Trenchgun Fast
7a30703cfe90f8c3 BPO Prone Ammo Check Grenadepistol
7b480a0acb73c0fd BPO Prone Reload Double Freedom Fast
7b6a884646b688fb BPO Prone Reload Patriot Drum
7bf38ab05e66f831 BUO Standing Reload Battlerifle
7c08096521a41981 BPO Prone Reload Shark Pistol
7c2d5f633de8a4b1 BUO Standing Reload Assault Rifle Grenadier Launcher
7c3b42b03184c341 BUO Standing Ammo Check Revolver
7d8c40d7af14e3b9 BPO Prone Reload MG Fast
7e0ed449d6b38dee BPO Prone Reload Laserrifle Extended
7e550bce8a72d28e BUO Standing Reload Rico
7e913af2503c5118 BUO Standing Reload Autopistol Fast Extended
7ebe61ef1a5cc31c BUO Standing Ammo Check Pumpshotgun
7efbb0e08441e689 BUO Standing Reload Patriot Fast
7f2ef94515d839c8 BPO Prone Ammo Check Battlerifle Ceremonial
7f72804522940ae8 A-O Add Fire Laserpistol Start
8069c60b7a4a16ab BPO Prone Reload Shotgun Nacho
816ec5ae2cf91d6b BUO Standing Holster Lmgstalwart
81aa1000cbcfdd17 BUO Standing Reload Plasmashotgun
81c73f0bbf15c1cf BUO Standing Reload Laspistol One-hand
81e1f9a845136af3 BUO Standing Reload Autopistol One-hand
824c7eab9f42538f BPO Prone Reload Assault Rifle Nacho Fast
82543be39e0eae1e BUO Standing Ammo Check Autoshotgun
835078488784c9d9 BPO Prone Reload Triple Shotgun
8370f553c96e1b1d BPO Prone Reload Volley Gun
841b4bdaec0cc2da BUO Standing Arm Wrestle Countdown Done
845c17cd88716d34 BUO Standing Reload Marksmanrifle
84b0b58c3b2e76d0 BUO Standing Reload Assault Rifle Whisper Fast
84bfa28d1a561129 BPO Prone Ammo Check Laserrifle
851aa902631d8029 BPO Prone Reload Assault Rifle Grenadier
857bfa786fdee088 BUO Standing Draw Snowball
858a6918a8d0afb3 BUO Standing Reload Assault Rifle AK
85e29665e3850a20 BPO Prone Reload Plasmagun
866201ba6a5534bc BPO Prone Reload Stalwart Fast
87657d9a0fafa32b BUO Standing Reload Laser Rifle Charge
87a2929d704a42d8 BUO Standing Reload SMG Defender Fast
885c5549a3cc31d3 BUO Standing Ammo Check Sniperrifle
88ed43ce83cd1bcb BPO Prone Reload Stim Fast
89020288a342376f BUO Standing Reload PDW One-hand Drum
890cb7e5852089d7 BPO Prone Reload SMG Helghast Fast One-hand
89237a32c67b9648 BUO Standing Reload SMG Helghast One-hand
8980dfd9a86260d4 BUO Standing Ammo Check Laserrifle
899f34f6aaa2c37a BUO Standing Arm Dynamite
89dffb8e870316c9 BUO Standing Reload Plasmapistol One-hand
8a78dcd57530bd2c BPO Prone Reload SMG Rhino
8adea82cd949dd00 BCO Crouch Reload Autocannon Fast
8b2d497dab2b3932 BUO Standing Holster Lasercannon
8b7446a7c8d02e8e BPO Prone Reload Stim One-hand
8c2c543a25b401af BUP Standing Draw Mine
8c3cb62ae990bd2b BPO Prone Reload Energyrevolver
8c8bc1f0d1a25df8 BUO Standing Reload Smart Pistol Missile One-hand
8e240eb55b971a8b BPO Prone Draw Primary
8ef279c7b0e1d7a4 BUO Standing Ammo Check Bullpup
8f39353ebba309ca BPO Prone Reload Assault Rifle Nacho
8f5624fae9e9bba3 BUO Standing Ammo Check PDW
90336f57c7adafb1 BUO Standing Reload Double Freedom Fast
90f254b6ad422f62 BUO Standing Holster Flag
90f2826d7faab5f3 BCO Crouch Reload HMG Fast
90f2ccd656569d53 BPO Prone Reload HMG
91aa1a5e3801f17a BCO Crouch Reload FAF
934ee81df9ac22e1 BUO Standing Reload JAR Phoenix
93989af7951e524e BUO Standing Ammo Check SMG Helghast
942b3a49052c8022 BUO Standing Reload Battlerifle Ceremonial
945f2ff4238e6fe1 BPO Prone Ammo Check Recoilless
946d28fdb893f1ce BUO Standing Reload Energyrevolver
95c4bf65ff0fa6b5 BUO Standing Reload Pistol
9685b973a3d1e102 BPO Prone Reload Rico
968cd67edeb1bb5f BPO Prone Ammo Check Flamer
97da5e985488ed9f BUO Standing Reload Nacho Pistol
9813c0955931af50 BUO Standing Reload Plasmapistol
991ce451ad3af62a BPO Prone Reload PDW Fast One-hand
99670e98b6b0ed1b BUO Standing Reload Marksmanrifle Fast
9a56479489d59f60 BUO Standing Reload Assault Rifle AK Fast
9b242ecd3ae2e034 BPO Prone Ammo Check Patriot
9bbdd6211808b4b0 BUO Standing Ammo Check Marksmanrifle
9ca47ede1973a18a BUO Standing Reload Grenadepistol
9e2bf65b075410c9 BPO Prone Reload Plasmashotgun
9f395f4355e4cd9d BPO Prone Reload Laspistol
9fe6860403ecb374 BPO Prone Reload Plasmablaster
a0251d804b2b8286 BUO Standing Holster Railgun
a0d8489bf8132732 BUO Standing Draw Machinegun
a1e4715a57d735b6 BPO Prone Reload Energy Weapon Shark
a283cca1f3a258e2 BUO Standing Ammo Check Rico
a2adf804e907ef0b BUO Standing Reload Stim Fast
a2c58434cc560bc9 BPO Prone Ammo Check Jetrifle
a39172ac502e012e BPO Prone Reload Assault Rifle AK Fast
a408f1e8634c6f05 A-O Add Fire Autopistol
a439678457c78eae BPO Prone Reload Lasercannon
a4d07b12cf8fc2d3 BPO Prone Reload Marksmanrifle Shark
a4db6f8e4a5d37ee BPO Prone Reload Smart Pistol One-hand
a4f5a4ea0f149376 BUO Standing Reload Assault Rifle Grenadier Fast
a567cb689eff1465 BPO Prone Reload Rifle Drum
a5a9d9f0ca652725 BUO Standing Reload Rico Fast
a5bb6d5cecf56b43 BPO Prone Ammo Check Rico
a65276b73f63c563 BPO Prone Ammo Check Autoshotgun
a6e390cfcfad6b6b BPO Prone Reload Magnum Fast One-hand
a71304cabd0ff048 BPO Prone Reload Shotgun Nacho Fast
a8c74c87ba593df9 BPO Prone Reload SMG Helghast Fast
a91dc9722de87c6e BUO Standing Reload Laspistol
a9ebff0508db4d22 BUO Standing Reload Smart Pistol Fast
aa41e2cbf40b7dd2 BUO Standing Arm Wrestle Enter Done
aabc87c3da82e9de BPO Prone Reload Assshotgun
ae069b65dd422fdb BUO Standing Ammo Check Railgun
ae18501da1208a74 BUO Standing Draw Sidearm
aee0c2d79e07e40c BCO Crouch Reload Lasercannon
b0c8337bef156fc4 BUO Standing Reload SMG Defender One-hand
b10e66326b85e694 BUO Standing Reload Railgun
b2aa65e24964fd70 BPO Prone Reload SMG Helghast One-hand
b2fda776ab473872 BCO Crouch Reload HMG
b3b2b83c5fba01a3 BPO Prone Reload Autopistol Fast Extended
b3fb15727b09cad5 BPO Prone Reload Autopistol
b570707478d65536 BUO Standing Draw Grenadelauncher
b63650822bf60d9f BUO Standing Draw Railgun
b6812b17eb662742 BPO Prone Reload Autopistol Fast One-hand
b6911d02b35f35b6 BUO Standing Draw Recoilless
b71a8ee43433d1e4 BUO Standing Ammo Check Battlerifle Ceremonial
b72125ac2aa44568 BCO Crouch Reload MG Fast
b83d81a6200c06a0 BPO Prone Reload Laserrifle
b874ad727d544e84 BUO Standing Draw Map Done
b8ab1b48a3362e90 BPO Prone Reload Shark Pistol One-hand
b8f077f0aff43dba BUO Standing Ammo Check Stalwart
b911e123e63343dc BPO Prone Reload PDW One-hand
b941b657ceb4c6ee BPO Prone Reload Revolver
b988dbafca9ccafe BUO Standing Draw Flag
b9d7728d32523f8c BPO Prone Reload Nacho Pistol Fast One-hand
ba32f4d2ff9892df BPO Prone Draw Sticky Grenade
babff0479c784d14 BPO Prone Reload JAR Fast
bbd95813e5c01ca5 BPO Prone Ammo Check Autopistol
bc224904a03d51e0 BPO Prone Reload Karbin Fast
bc28227d73760a77 BUO Standing Reload Hammer
bc9caf19761d49a9 BPO Prone Reload Crossbow One-hand
bcc72d87d37014d6 BCO Crouch Reload Plasmablaster
bcf3e69456d7569d BPO Prone Draw Snowball
bd5240a7f8510960 BUO Standing Reload Ripley
bd7877bc1c2c96cb BUO Standing Reload Assault Rifle Helghast Fast
be4f2bbb694819e4 BPO Prone Reload Cricket
bec8eef4ecf84976 BPO Prone Reload SMG Defender Fast
bf19e7376ecbfeb7 BPO Prone Reload Broomhandle
bf3048de959c9b06 BUO Standing Holster Autocannon
bfbda50dace22075 BPO Prone Reload SMG Helghast
c0d7bb5f1657ff19 A-O Add Fire Autopistol
c14fa04d174cf8da BUO Standing Reload Cricket
c1cf8a084926b734 BPO Prone Reload Laspistol One-hand
c23ee2fc75dd422b BUO Standing Draw Doublebarrelshotgun
c2c20fdc65763adc BPO Prone Reload Railgun
c366b0f5cf7c24ac BPO Prone Ammo Check Pistol Shark
c37115c87a307e45 BUO Standing Ammo Check Autopistol
c43f2cf9b4ad5d8b BPO Prone Reload PDW
c498fa0b4706e9b2 BUO Standing Reload Assshotgun Drum
c4aa9b1c329ee8a2 BPO Prone Ammo Check Volley Gun
c54c16fe782dee65 BPO Prone Draw Sidearm
c565f005849d443a BPO Prone Ammo Check Grenadier
c66ad60bf5edeb6d BUO Standing Draw Hammer
c68b64bc40d92905 BPO Prone Ammo Check Railgun
c69cbf386c552bea BPO Prone Reload PDW Fast
c69e26f41c1f6eb9 BUO Standing Ammo Check Defender
c75051f3fedcdd62 BUO Standing Ammo Check Jetrifle
c756561f7ac2a449 BUO Standing Reload PDW Fast
c858f7fdfc5b456a BUO Standing Reload Battlerifle Ceremonial Fast
c890fbbb79d31655 BUO Standing Holster Revolver
c93562e542bd3c8c BPO Prone Ammo Check Stalwart
c951d30f31df7cb0 BUO Standing Ammo Check Assault Rifle Helghast
ca0d39ac103d0fb0 BPO Prone Reload Colony Shotgun
ca756e448afa5a0d BPO Prone Reload Recoilless
cc4dd8c42466a486 BUO Standing Reload Patriot
cd4cee51dc6b83e4 BPO Prone Reload Rifle Fast
ce38f1cf437cbfaf BUO Standing Reload PDW Fast Drum
ce702404d5cff460 BUO Standing Reload Crossbow One-hand
ce8ff06ce40c3131 BPO Prone Ammo Check Plasmapistol
ceb9f00c31a8a219 BUO Standing Reload Plasmagun
cf0dc3a3ddc5efda BUP Standing Draw Knife
cf90116f1bdca93f BUO Standing Ammo Check Patriot
cfb4a0604cb1e994 BPO Prone Reload Assshotgun Fast
cfb4f27842a443d9 BPO Prone Reload Revolver One-hand
d0169b1138e062ef BUO Standing Reload PDW
d081aa16a0ed451a BPO Prone Ammo Check Autocannon
d2141f3f448e176a BPO Prone Reload Magnum
d2b68db6c2415efe A-O Add Fire Sniperrifle
d394fd5ec07fba9d BUP Standing Draw Knife
d411de41d142df3e BUO Standing Reload Sniper Rifle Helghast
d50ca35279a1f9b4 BPO Prone Arm Dynamite
d66c73e18c851575 BUO Standing Reload Magnum One-hand
d8099214f0a9f66f BPO Prone Reload Revolver Fast One-hand
d809d94c8fc17e90 BUO Standing Reload Marksmanrifle Shark
d8a9b7428af49b5a BPO Prone Reload Battlerifle Ceremonial Fast
d93a482aead150cb BPO Prone Reload PDW One-hand Drum
d9d8abd0d7a938c8 BPP Prone Draw Hammer
daa623c80cdd43d0 BUO Standing Reload Grenadelauncher Fast
db060f2d7c10a43c BUO Standing Reload Triple Shotgun
db0c587083e5aafe BUO Standing Reload Volley Gun
db12dfb0bafbca40 BUO Standing Reload Harpoon Gun
dbf47a218fc6e84c BUO Standing Reload Cricket One-hand
dc233a33aafd21c1 BUO Standing Reload Triple Shotgun One-hand
dde62abda658e586 BPO Prone Reload Colony Shotgun Fast
de1bf1d1ae1ef8db BUO Standing Draw Sticky Grenade
ded1d9c3e01988a2 BPO Prone Reload Pistol Fast Extended
df426a6185b1e2fa BPO Prone Ammo Check Plasmagun
df7d1185a67e91b6 BUO Standing Reload Revolver
dff14768affa4f51 BPO Prone Reload Triple Shotgun One-hand
e074300051d5d223 BPO Prone Reload Battlerifle Ceremonial
e10ada4ed7f5ef61 BUO Standing Reload Magnum
e139e3215365135e BUO Standing Reload Stalwart
e14a6bc2799fad15 BPO Prone Reload PDW Fast Drum
e152d29f82f75644 BPO Prone Draw Flag
e17dcb72e939377a BUO Standing Reload Smart Pistol One-hand Fast
e28981f5a482a464 BPO Prone Reload Smart Pistol Missile
e2a464a49f34e742 BPO Prone Holster Map
e2d895f1e20c9d69 BPO Prone Reload Revolver Exit
e4952009129b7a16 BUO Standing Reload Dart Gun One-hand
e49fc805843e6ec1 BUO Standing Holster Flamethrower
e4b368fc80c3d83c BUO Standing Reload Stim
e64faaf9642bc5c4 BPO Prone Reload Smart Pistol Missile One-hand
e674ad47749f263a BUO Standing Reload Patriot Drum
e6b8ae03553eb0b7 BUO Standing Reload Smart Pistol
e7b068162b8edf1e BUO Standing Reload Shark Pistol One-hand Fast
e8710e7bb47ef0a2 BPO Prone Reload Autocannon Fast
e883b0aed1675117 BUO Standing Ammo Check Grenadelauncher
e8b096ef7e64c400 BPO Prone Reload Assault Rifle Grenadier Launcher
e8dbefb6403e0bd8 BPO Prone Ammo Check Marksmanrifle
e8feea240b1fa54e BUO Standing Reload JAR Phoenix Fast
e97b23899aa4ea41 BPO Prone Reload JAR Phoenix
ea2786b260c7e404 BPO Prone Draw Flag
ea904afb9517326a BUO Standing Draw LAT
eb3ea652d78ce83a BUO Standing Reload Flamerpistol One-hand
eb60fbc849d83f3a BUO Standing Reload Assshotgun Fast Drum
ebc356caaa05c7c1 BUO Standing Holster Knife
ec0fd5130e9d8c1a BPO Prone Reload Laser Rifle Charge
ec9b81532be268df BUO Standing Reload JAR Fast
ecaa0d48cae245e1 BPO Prone Ammo Check Assault Rifle Helghast
ecffe2c2cedc8f5a BUO Standing Ammo Check Pistol
ed33ac31a3fb7fca BUO Standing Reload Assault Rifle Nacho Fast
ed97d1e2cb589447 BUO Standing Reload Dart Gun
ef12b3d9b8a48921 BUO Standing Reload Colony Shotgun Fast
ef5b093efdcc754d BUO Standing Reload Laserrifle
efcdd89fc83376e7 BUO Standing Reload Autopistol Fast
f0057fa6cc35f3af BUO Standing Reload Magnum Fast
f049a363494045e7 BPO Prone Reload Grenadepistol One-hand
f054078caa0a42e9 BUO Standing Reload Revolver One-hand
f0b072aa4c297da4 BPO Prone Reload Smart Pistol
f1752d51dd12dfdb BUO Standing Holster Throwable
f2876ccd02926a2b BPO Prone Reload Battlerifle
f3b228822fbeb922 BUO Standing Reload PDW One-hand
f41fdb9b93c2706a BPO Prone Reload Plasmapistol One-hand
f4543d42cee6ca4e BUO Standing Reload Magnum Fast One-hand
f4e34de2afd2848a BPO Prone Ammo Check SMG Helghast
f52edd497a2425a3 BPO Prone Reload Broomhandle One-hand
f5614d5b57874b73 BUO Standing Reload Colony Shotgun
f582fba115a6c450 BPO Prone Ammo Check Sniperrifle
f59aa14c982fd847 BUO Standing Reload Pistol Fast One-hand
f642122d5b0f9ddc BCO Crouch Reload Autocannon
f6fa5f853929c647 BPO Prone Ammo Check Defender
f7ca838cf2edba2f BPO Prone Reload Rico Fast
f8688aa0366b0b3b BCO Crouch Reload Recoilless
f9642d29d90b0cca BUO Standing Draw Flag
f9761fd85530ae0f BCO Crouch Reload MG
f9f325fb70b4f4c7 BPO Prone Draw Flag
fae62d287b658096 BUO Standing Draw Revolver
faef59c45be02595 BUO Standing Reload Pumpshotgun
fb64ed90fc502b70 BUO Standing Reload Dart Gun One-hand
fb8f94ab970ae5fb BPO Prone Ammo Check FAF
fb9c95ac60a27ebd BUO Standing Holster Hammer
fd4f9e6feab75c55 BPO Prone Reload Pistol One-hand
fe6e49d07bf78424 BUO Standing Reload PDW Drum
fee454ef302ca3f7 BPO Prone Reload Ripley
ffc04b508c78bcec BUO Standing Reload Assault Rifle Grenadier
# --- manual (652) : named by eye - a trailing '?' is the cataloguer's own doubt
009b70776f5b97a4 BCO Emote Crouch2
011991867af4d6a1 BCO Crouched with Heavy Weapon
014212b8df4757b0 BPO Point Weapon Side Prone
0179e872677fd719 BPO Prepare Strat looking left
0193fbdeef0ca412 BUC Walk Back left
01a66cca71866f91 BUC Spin Around
01d711eb620c1c3b BPT Prone Back to Standing
025c32ce3e6439e3 BUO Standing Left Shoulder Check
026da501591e1648 BCC Kneeling Step Forward
02b952d1ba0d88d3 BUO Emote Draw?
03022aceb2c41622 BPT Kneel to Prone
032320d917bba940 BCT Emote Dramatic Pose
03446604121e3a3a BUO Standing Slice
03bf3964bfa1fd6d BUO Emote Dab
0439cad27a4fd324 BPT Standing to Prone3
043b41b8a07e6f10 BUO Standing Quick Throw
048aede73ffc9962 BUO Picking Mission
04fabdc153bb0ab5 BPC On Back Backing away
0557392a0b09523c BPO Prone Quick Throw
059c38a1a511b8ca BUC About Run
05f0c83547adb4ef BCT Death Bleed Out
0601b5fdf264628e BUO Emote Raise Weapon
0695536160243b11 BPT Crouch to Crawl Forward
070b422612518deb BUO Emote Salute2
073a6b972f35ce32 BCC Crouch to Standing
0786c59965a02bf7 BCO Emote Feel Doomed
079292fd38149086 BUO Emote Handshake2
08c07d873c74510a BUC Run Uphill
091a645a2b070010 BUC March with Flag
0921c37f9abedc0c BUO Out of Cryo
09664c0b1ff05a12 BCT Up From Sitting
0979ca0da0576eab BUC Turn Valve
0bbc294b0e6a7aff BUO Emote Pull My Finger
0c198d86cdbe6fae BUO Emote Tip Hat
0d5f029e03d12299 BUC Emote March on Spot
0f93b8bf901345ea BUC Run Forward3
100eff0fc8d2ffdb BPO Laying Prone
1067796c6466f152 BUC Turn and walk backwards
107ae6e30372317e BPO Reload prone (incomplete)
1087432a281bd9a9 BUO Throw
10a06ce2ead880d0 BCO Crouched with Rifle
10bbec66cb718606 BUO Prep Stratagem
10d2422e9a7b01d8 BPO Grab ammo prone?
10d89f9caa79c55b BUO Grab Melee Weapon Standing
10ff37b273d09fbd BPO Reload Prone
112481e4280d5388 BPO Prone Weapon against wall
1128d7f45092bc5a BPT Going Prone Sideway
11345f76a7256104 BUO Using a Computer
1177a45a939a44fd BPP Side Leap
1191140483ebba02 BPT Diving Sideways
1199ca9110962efc BPT Diving Forward
11abd89d53fa277d BPO Throwing While Prone
11c0f370651ec452 BUO Reload Shell Standing
11c1ad8b967febef BPO Prone (incomplete reload?)
11cc9f1b772383bc BCC Crouch Walking
11d2cc228a2b55b0 BPO Jump Backwards holding something
12128c41067f1e3c BCT Get in Car
1245f8971d86128c BCC Crouch Walk Forward Left
12638b0f2ba80270 BCT Coming out of Hellpod
129906dd372c4257 BPO Sitting in Tank?
12a2100292ebd25e BUC Lean Forward?
12bdaae8d511ef8d BUO Rocket Reload?
131983dac521f70f BPO Reload Prone2?
13333b4fad2f76c4 BUC Running
136225228bd9b40a BCO Turning Valve?
137986b55cffa9a0 BUT Carrying Large Heavy?
13853adda796adf6 BCC Crouch Backwards left
13f1658c062c1875 BPO Prone Prepare Stratagem Left
13f2aca1108e9a54 BPO Reload Prone Backwards?
1405dfb077109665 BPO Reload Prone Partial?
140ac7bf5253a748 BUO Chainsaw Slice?
14171f93fbb074a2 BPO Reload Prone partial?
1422e2237746c0d6 BPO Reload Prone2
1424a10da52f54e7 BCT Death Animation
142f938d8cf75ea9 BCT Crouching from Standing
147e52e05acf9399 BPO Side Prone
14d8a415e56fbc56 BUO Plant Flag
14f1fef99d8079ad BPO Prone Reload?
14fce190263c9d3f BPO Prone to 90 degree move
150b1d416e36eff7 BUT Dismount?
1520d7a38b0f919e BPO Prone on back reload?
1524c2a799379799 BUO Emote Money Money
154be95ee01b546e BPO Dive Backwards
155fd7d44b41b691 BUO Standing Flick Wrist?
156d884f3cf9794c BUO Standing with Weapon
15dd57c88eeda88f A-O T-Pose
15e8176159f317c4 BUO Standing Roll Fists?
16056eea19571aca BCC Crouch Backwards Right
160ccddd33dbe2ee BPO Prone But Stratagem Back
163c5f69e713452b BUC Walking Salute
1684ad0b0fb0e8c3 BUC Standing about face
169f4cceb8fc038b BUO Smelly Emote
16a451a6c6702fe6 BCO Crouched with Weapon
16c1abba2e0d147d BUO Standing Lean Over
16f4bc896e8a9b34 BUO Standing Reload
170671fe9026fe57 BCO Emote Loud/Incoming
171a82cd067467b3 A-O T-Pose2
17282cb87d15c058 A-O T-Pose3
172af334f18ecac0 BUO Standing with Gun
173e19a99916f233 BUO Broken Animation
173ebbb594a83118 BCT Climbing out of Ground?
17688739312744a7 BCT Standing from Kneeling
17932e92306cfa5c BPO Moving Sideways Prone
17dc4386adf0777e BCT Turn Around Crouched
1812695bf7d295b0 BCO Crouch to on Knees?
18856b489c77f3ca BCO Crouched Looking Right
18c15bde2380ab59 BPO Prone Looking Right Prepare Strat
18cc8b8ac08cfbc1 BPO Big Swing Prone Right
18f0777ce534c99d BUO Standing With Weapon
1907d8049b8a097f BCC Lean Forward
19495ceab346a440 BPO Dive Sideways
1972e92e5de603a4 BUT Get Out of Card
1982cf13726542f5 BUO Standing Hand to Weapon
199c16572f5a9200 BUC About Face
19a3187b6377cd4c BPT Dive Right
19a77ed0f959de6e BCC Crouched Readjsut
19bef4358ede0990 BUC Walking Point
19e1dc7ebb64b1bc BUC Lean Forward Adjsut
19f8a87b799870d7 BPO Laying Left Throw
1a17216988f96773 BPT Exploded backwards
1a6fd5de35dd4bd0 BPC Pront Crawl Right
1a83538b5dc442af BCC Crouch Readjust
1a8ce73be4e15b2d BUO Standing Reload Pistol (Clip)
1aff331c2a19a069 BUO Emote Think
1b8d439ef6588f75 BUO Standing Rotate Torso
1b98cb9eb38fecfd BUC Coming out of Cryo
1bc4cd65a8782ccf BUO Standing Prepare to Throw
1bd0b5fa8c671f98 BCC Crouching Walking Forward
1bdcf1b5601323f9 BUO Standing Reload Vehicle Gun
1c1ec02586d013d5 BPO Prone Jump Adjust
1cb3fa7886dc19fe BUO Standing Dramatic Pose
1ccb4708079cbda3 A-O T-Pose Wiggle5
1ccc10ddfae3dd38 BPT Exploded Forward facing right
1cdd9a06e4f3b565 BPO Prone Facing Upright Reload
1d022d532990854c BCC Crouching walking forrward Shoulder Shake
1d1dd0716b5a615b BUC Leaving Cryo
1d941d34620a8fac BUO Emote HeadButt
1d942ad8e7a16153 BUO Standing Ready
1d9bcf8663439cdd BUO T-Pose Arm Adjust
1d9e6c4a37b03320 BUO T-Pose Arms Snap Back
1dabf4ce6bd73a52 BUC Tutorial Pod Entry
1dae1e66f2908734 BUO Emote Throw Dice
1dd4e2e2016e3bd2 BUO Emote Confetti
1df656ae44ff04ac BCO Crouch Pose
1e14d98e65c721c5 BUO Standing Take out of Pocket
1e4814df9608dc5b BUC Crouch walking back left
1ebfd1e644677224 BUO Team Reload
1ed97101242f160b BUO Prepare Throw
1ee68fd80e341895 BPT Exploded Forward
1efb85746217ce64 BPO Prone Reload Clip
1efcac9c243aa53a BUC Walking Right Pushes
1f22d61b5c2ff6be BUO Standing Scroll Map
1f2e64237a979da1 BPO Prone Reload Bolt Action
1f30fcc8f0300e05 BPO Prone Point Heavy
1f46efce0d275e7f BUO Emote Curtsey
1f8d6a8705eb7b30 BPO Prone Throw Left
1f95cbd445e942c1 BCT Emote Loud
1f9b05d99427bea5 BCC Crouch Slow Walk
1fb1b25c63afbeac BUT Standing Big Slice Forward
1fbd8c0d7475bcdb BCC Crouch Walking Forward Left
1fc034b36652ba47 BPO Prone Arms Wide
1fc2be8943a9c3d9 BPO Prone Reload Heavy
1febeaa05fafae96 BUC Walk Backward Right
200064ca421131bc BUO Standing Interact
204f64ab560f28ad BUT Climbing
20730edc49a22000 BUC Standing left Shoulder Shrug
2074b3591403731c BCT Getting out of Tank
2088796c3254c709 BUC Standing Pivot Legs
20b460e514e9b61f BUO Emote Open Hug
20dfe72ddc54b050 BUC Emote Kick
20ed55235ce8a3b1 BUC Emote Handshake
20fb7dcb5a87f43a BCO Pose Look Behind
210682ff9597b79d BPO Prone Bring up heavy partial
210ded089c5d9db9 BCC Standing Hunch walk
2123611658518396 BPO Emote Pushup
2132f9567d7ad6c4 BUC Walk Forward Right High Step
2173496aaee8ed6e BUC Emote Dramatic bow
21c2b4a8c7696cdb BCT Death Suffocate
21f3c04397f51da9 BPO Fall Forward Facing Back
2200b937134ee994 BUO Team Reload2
22034cb9efae4758 BUO Standing Shake Gun
222ee1a2b0b9af64 BCT Prone to Standing
225665bf75c1622a BUC Walk Forward Left
225e1a01713e5096 BPT Death Burn alive
226d9309a16145f8 BUO Standing half Reload
226eec12967e895c BUO Emote Low Crouch
227808d4a6727646 BUT Walking Forward Slice
22a1e072a39fa473 BPO Prone On Back Action?
22b680e6b50b3521 BCO Emote Thumps Up
22bfd8eb2dc4797f BCT Get in Tank
22f27a6e326a3bca BUT Death Shot
231f12e2491c8f2b BUP Standing Pose
232fb2aee606a382 BCO Pose Crouched
239bdc8611bc7243 BUC Running to Right
23bca4274734e8e2 BCC Crouch Walk Left
23ce3d78ed5764ab BPO Prone Look Up
23d6a32fc06e1132 BCO Pose Crouch Look back
2427cc28aa3f87f1 BPO Prone Big Slice Forward
2435d166c62f8bc8 BPO Prone Prepare Strat Forward
245ad6af41f0bc82 BUC About Face2
246d0587febe9d29 BPO Prone Interaction Forward
2476087c6b9d9809 BCT Walk Back to Prone Standing
2478f99f187c1013 BCC Crouch Walk Left2
248de964d3fe4df9 BPO Pose Prone
249244ed63e88247 BPT Exploded Forward2
24b79a0267e09d6a BCO Kneel Turn Valve
24c602b1ad77187f BUO Standing Hand Action
24e07912f8ff51c0 BUT Walk Forward Left Small Hit
2590c20d9e8e9845 BPT Exploded forward Backwards
25ba3add17a3952f BPT Walking to Crawling
25e5b5aabcf55e49 BPT Pushup to Standing
261d22fcc587bcd0 BCO Kneel Walk
26478b6ba76a6c6f BPO Dive Forward
264ac2b8d12212c2 BCC Stand Duck Stand
2679e1227a9049a6 BUC Walk Forward Left High Step
267f7898448cf2e5 BUO Walking Left Hit
2691ec1dbe14727f BUC Standing Stepforward
26938a622b052442 BPO Prone Partial stand
26a55f93e1a30647 BPO On Back Throw
26b9b7b6f1241efb BUO Emote Handshake Wif
26f53e1e8009e67c BUC Emote Click Heels
2721740582ece8d2 BPO Prone Pose
277002443cb66a63 BPO Prone Pose2
278a8ac23107dabb BUO Emote Pick Nose
27a248375c6d93f3 BUC Trudge Forward
27a2e8500a1d803b BPT Standing to Crawling
280f6a48a012814a BUT Standing Run Forward
284b7e0eae90903a BUO T-Pose Hand Backflap
287009bd42c8ec9e BPT Exploded Back Forward2
28703718afc95ccc BUO Standing Reload Heavy
288f312febc46518 BCT Crouch Aboutface Left
289597369f555002 BPO Prone Hit Left
28c61feb797ca76c BPO Prone Cokc Grenade Left
28d3c1b3da139232 BCO Pose Kneeling
28e881424f14d1fe BPT Prone Plant Flag
292b75715ec5eda1 BUO Standing Gunup2
2936a56d881e1388 BPT Short Vault
2973ccfa37bddcaf BUC Standing Step Forward
29abaae3fbb5228a BCC Heavy Landing
29cf8166b2be6615 BCO Emote Crouch
29d7a21e40e8a2bb BPT Pose Forward Twist Left
29de945c644a8900 BPT Standing to Prone
2a0621fd2395b743 BPO Prone Big Swing
2a4e63d6c5643c9c BUT Step Into Dive
2a554612c2fe46b5 BCC Step Forward Left
2a6969d9a492395a BUO Standing Cock Gun
2a72cf41cf3da6e8 BUC Forward Back Right
2a898bf70cc8237c BCT Prone to Stand
2a8e0697c8ad9ac5 BPO Back Thrust with Weapon
2aaceb60ffa3d5d6 BUT Emote Loudup
2ae3b6a451683c9e BCO Kneel Look Back
2b0ffe386ef92f58 BUC Back Left Step
2b3102528dfeb9c9 BUC Walk Back Press Animation
2b3fd67c3d3de355 BUO Standing Check Gun
2b64048f28b63adf BPT Prone Back to Front
2b85e4e5dbeab474 A-O T-Pose Shake
2bb6eeb0aedffd9a BPO Prone OneHand Reload
2bc309650ae56e9f BCO Kneel Pose
2c00ff474cd81846 BPT Dive Forward2
2c59834ffaddc172 BPO Prone Partial Reload
2c97f28264f18fc7 BPO Prone Quick Grenade
2cd6e3f7a6ffde1d BPO Landing Prone
2cded25e8c70e1d0 BPO Prone Reload7
2d359800c7a3281d BUO Standing Brief Check
2d8f93fe779f2c6b BPO Diving Backwards
2dc739ff8edcf640 BUO Emote Chest Bump
2e1d88ebf3a70ae1 BUC Arriving from Pelican
2e3b6d1fc60a4d2f BPO Prone Reload Bolt
2e50881ccc6948d7 BUO Standing Slice
2e88fcbbd273d8e2 BUO Standing Bolt Reload
2ece6bdabb98bd0e BUO Emote Open Hug2
2ed8c06f2e656e0b BUC Standing Spin Around
2f741aaf34065276 BUO Standing Ready Pose
2fbce958e1f556de BUO Standing Look Pose
304c2bb1f3f47008 BPO Waiting to Acend
307b7443ebfa446b BPO Prone Pose3
309c1905e90b67e2 BCC Riding Car Laying Sideways
309f92b9aa493c11 BUO Standing quick flick
30cb146f7dce3d0a BPT Standing to Crawling2
3102d868803a8e57 BPO Prone Reload9
310981a6573124cb BUO Emote RocPaSci
31296c3522e2fcfa BUC Emote Mad Laughing
312b718358e16687 BCO Bumpy Butt
3137a315502d32d5 BCC Kneel Shoulder budge right
3149613d034dc346 BCO Broken Animation?
31658924e9e2df00 BCO Squat Pose
3175de4d9ce647b0 BPT Death Electricuted
31c45f0dc39e01ed BCC Squat Walk Back Left
31c75b7c8d640d57 BUC Standing Reposition
31f12f25ff735c9a BUO Standing Shieth
322ea3bfd2b8aa63 BPO Prone Partial Reload2
32367ae1faf8078c BUC BackStep Left
3250144246350aed BUC Running Left
3253607d4ca73c7e BPO Prone Hot Reload
3283fd32f6f9e1b6 A-O T-Pose Jiggle8
329009d1da103a63 BCC Crouch Walk Left
329508707c78ecec BUT Large Vehicle Exit
32996f48331942a2 BUO Driving
3299e4a0feb677ce BPT Prone to Kneel
329e18e0e39b6248 BCT Standing Pull Up
330dda142da509ae BPO Sitting up Pose
33140d078a22196c BCT Walking Down Stairs
337850682cb58a8e BUO Stand to Attention
338040536836378f BUO Standing Grab Heavy
338395fad5dbc924 BUO Emote The Best
338e00b96348421b BPT Getting up Flag
33ba10e130d023f6 BPO Prone Look Map
34b6c030581e915f BUO Emote The Best2
34bc0158ce933be2 BUO Emote Salute
351cca754669e1ca BPT Explode Backwards
357214247e91a1ac BCC Heavy Landing2
357d0df512811a10 BUO Emote HandShake
35c655be710d631b BUC Walk Backwards
367160cb66a7b555 BPT Explode Forward
3671e1e8a6f4740d BPT Explode Backwards2
3709ef427d6cceb6 BCT Prone to Standing2
372668f72007b09d BUC Exit from Cryo
385df45eff9b4111 BPO Aiming Pistol on Back
386ad0790a0b51ce BPT Dive facing Right
3875f1a99c587903 BCC Crouching Walk Forward
38a36ca5d0516ca6 BPT Stand to Prone
392084a6e95789f2 BUT Stand Heavy Hit
39250e5b6313a0b2 BUO Standing Salute End
3982524e02baea81 BUC Emote Mad Laugh
39e24e5f427dee00 BUO Standing Throw
39fc7425c54dbb05 BUC Running Forward
3bb58bb993313bbc BUC Walk Forward with Sword
3c11456053f48957 BCC Hunch Walk Forward
3cca6d1e7e9ddb83 BPT Throw from Prone
3d2f3f67d728bfac BPT Standing to Prone2
3d387630b22efed2 BUO Emote Pullup
3d4acc6c3e6b5977 BPO On Back using Dial
3d5fce243a3bdd52 BUO Emote Handshake Finished
3de818897f1229e4 BUO Picking a Mission
3e014bc9422ebef5 BPO Landing Prone Forward
3e44b77cee18cb08 BUT Large Vault Climb
3e813a2e6f409234 BPT Dive to Left
3e882677f12dd621 BUC Exit Cryo
3eb5eab9107b3b4e BUC Walk Forward Wiping Head
3eb854b8f2582995 BUT Getting in Tank
3ee86a511cc95a52 BPO Landing on Back
3f39364e77a04e8e BPO Emote Sulking
3f6853b771fa1b3b BPO Landing on Back2
3f694f31ff788746 BUC Running Forward2
3f7aff7e3275330a BUO Being Strangled
3fe98af0c18dbd26 BUO Emote Fail Pushup
3ffc30bba038131d BCT Prone to Standing3
4016cd8551e9325b BUO Picking a Mission2
4030d1d1bc75b685 BCT Emote Cool Pose with flag
40f783a713d284a4 BPO Throw from on Back
4131c84f2f4c369b BCC Crouch walking left
41539f08d1b99934 BUC Standing Reposition2
41d4b59f875a6568 BUT Crouch to Stand
41f3ed24fdf91a2c BPT Dive forward facing Right
4206c1a7daff80c5 BPT Full Dive Forward
438106c6304bc67a BCC Tip Toe Forward
4381fe0662273495 BPO On Back Press Button
44511bf307597c84 BCT Sliding to a Seat (Bus?)
446528bb3659e875 BUC Running UpHill Forward
447f467e21728e2d BPT Emote One Hand Pushup
44927d759180ead1 BUT Picked Mission
4513104831a01996 BCO Emote All Thumbs
459e18f051165f47 BUO Emote Clap
45f0c71d663ab0eb BPO Emote Look Up
45f5e387b8909c44 BPT Emote Wake Up
4606fc02f3492148 BCC Knees About face Walk
4626514b113a00bc BUO Emote Picking out Shrapnel
4674de093a72ea71 BCC Crouch to Kneel Forward
46a3eb8390d82389 BUO Cautious Stand Ready
46adcd3d851f4882 BPO Landing on Back3
46bd486fe56b8f7f BPT Throw from Prone2
46c9e193bda16aff BPO Emote Look all way back
46ed46a69546f60f BPO Prone Half Shuffle
473deb6935f05b77 BUC Walk Forward Ready
4753a414c12f7bc9 BUO Emote Big Hug
476b12293bc32ed9 BCC Crouch Walk Backwards to Right
47748c4482e4d886 BUC Walk Away to Right
48278d54dbe9ce1e BCT Slide down to seat (bus?)
48d919ef4664fb20 BCO On Back Slice
494989ea5302f201 BPT Stand to Prone3
49641c8221a693b4 BUO Use Console
499253b413a9b33b BUO Stand Nearly walk past
4b046c9f9a98d2c7 BUO Emote Handshake fool
4b2b86d1433a1da6 BUO Emote handshake ignored
4b63bdd288e12ec4 BUT Standing pullout of ground
4bc9a2411115d455 BCT Death Suffocate Gas
4c112aec98b53c7a BUC Walking Down Steep Hill
4c4ddfc3c4a26fe3 BPO Aiming Backwards on back
4cd54cc147be826d BCO Emote All Thumbs2
4cfd791f5423500b BUO Emote Who has two thumbs
4db1d5bdf4395e45 BUO Emote RockPapSci Loss
4db35852250a6832 BUT Getting on Mount
4f1802e1cd0ca021 BUO Emote See This
4fba808ec5734d1a BUC Emote Gracious Bow
5002e1f8db02c0fd BUC Emote Super Flag Walk
503803ea82c0dc2d BUO Emote Big Stretch
510e7216d94aa57c --- HD - Walking Forward
51dcf0112c898749 BUO Emote Rock paper Sci
526f52080518b349 BCT Kneel to Stand
527f9d2247891e07 BPT Stand to Prone4
530d67526a681b90 BUC On Cannon Turret
537de90c9bbd1e4c BUO Stand Fire Shotgun
53cf89cf734e5f71 BPT Dive Forward3
53e760f05f7afb58 BUT Death Cant Breath
55a8bd88c8c6422e BUC Walk Forward with Caution
562e6dab6942d5be BUO Scrolling on Map
56616094291d7b2b BPO Pose Prone Forward
5662767f006e0539 BUO Standing Check Item
56c3a3f89cc3ca75 BUO Emote You Got Me
56ef65c89c1c4315 BCT Broken Climb Animation
5736bb73e3510b5b BUO Standing Fire Leaver Action
576f6f681dd364f0 BUO Emote Standing Salute
577055ea9db1355b BUO Emote Pick and Flick
57af70f1abac3f2d BUO Emote Slap
57ff5fc1ceb5a90f BUO Emote Arm Wrestle Fail
5827d862d790776f BUC Emote Kick2
5837c765199a7519 BUO Emote Trying Patience
58a1d73d6d882f8a BUO Emote Small Bow
58aafb479a121f01 BCO Riding in Vehicle
590c50a1622300fc BPO Emote Injured on Ground
5954c11f5b5ae646 BUT Revived Up
59b8490371c3f93d BUO Plant Flag
5bd3611012a35e30 BUO Stand Pose
5bec0565e36a1b67 BUO Emote Remove Hat
5cf75686d11cddf9 BUO Emote Hip Thrust
5d06c7e788411215 BPC Riding in Back Vehicle
5d850c2d89a42c82 BUO Emote Epic HandShake
5de336b18356ae69 BPO Emote Sitting
5dfb81bd2866442f BUC Run to Left
5e022a4b005f4539 BUC Standing Hit
5e7113a8ce396edb BUO Emote  Handshake Offer
5f2da9e485f699ec BUO Stand Pull Pin
6029e95b4fe4c0c3 BUC Emote Looking for Ride
60ced4e251c4bba2 BUO Standing Slash
60fde5c3959ef79d BUC Full Sprint
6148617ed7fb0370 BPO Fall Prone to Side
615ca205de5a0908 BPC Crawl Forward
6250f38d6523efe7 BPO Emote Prone Hi
6283d707d130eaf0 BUT Get Into Tank
62bde0c398be79bd BUC Walk Forward Ready
632297ae18e9b91c BUO Emote Handshake Offer
64cae148f8010474 BPT Dive Forward4
666047cc5b0e7ae1 BUO Emote I Dont Know
666abc0358bbf3d0 BUO Emote Hurrah
67d5bffc0db90aeb BUO Emote Trying Patience2
67e0c3a70a639c03 BUO Aiming Rocket to Target
67e8cd6f1045624d BUO Emote Looking for Ride2
68c358ec02cfbe45 BUO Emote Chest Bump2
6950e71a2592b6f6 BPO Prone Fire Lever
6953f6e7c1d85d84 BCC Crouch Sneak backwards
6987b33e9e3a7efa BPT Stand to Sprawl
6a2222afa1b59466 BPT Death Electrucuted
6ac442f0cff5f18c BUO Emote Got Ride
6bd2e50e9427f198 BPO Emote Prone Thumbs up
6bfea7bc0e556f88 BPC Injured Prone Crawl Forward
6c6476b1ed44f188 BUO Emote Fool handshake
6c96e0a630d59699 BUC Casual Walk Forward and Left
6d89a4310fb21324 BUT Get on Turret
6e017d965bc755f4 BPT Emote Pushup2
6ee911c729a147c9 BPT Sideways Flip
702062d1d21266c8 BCC Step Forward Knees
705a71ed55191e93 BUO Emote Pull Up2
71400fbc83a050eb BCC Crouch Walk Forward
721fcc4b329836b0 BUO Stance Holding Gun
723d10a5a8f95bb9 BUC Emote Shot at Table
72651b9f8ac4aee1 BUO Pose Thy Knife Chip
72e1bff187630dcb BUO Emote Angry Point
737c24ae2b166ea7 BUO Emote Big Hug2
73cf93e6bca128f0 BPC Emote Fetal Position
73d049e598a846d9 BUT Knocked Backwards
73d52279bb044853 BUO Emote Big Hug3
75205314048dcdaa BUO Emote Open Handshake
75979cc96db0f7b4 BUC Quick Jog Forward
759c08277f1296d0 BUC Look Around
76e66c222bde38a4 BPT Emote Sit Down
7775587c6b3cd964 BCT Slide facing Left
7783a484ed6f74bc BUT Crouch to Stand2
795d5cefc07530e0 BPC Climb Up Slice
7a94fa281cdcc95b BUO Emote Draw
7aae0f5239ffe943 BPC Emote One Hand Pushup
7ae89ba50cd12d53 BUO Stand at Attention
7b8491a300ce691e BUO Emote Rokcpaper Scissors Dont Know
7bf52703d847e5c4 BCO Emote Too Loud
7c5298bd7da1939d BUC Jog Forward
7c840a0268f8c0f6 BPO Land Heavy prone forward
7c853f8cfc2e31ff BUT Pick Mission3
7c873d6a0bafbf78 BCO Emote Loud2
7d4eb218add66e07 BUO Standing Throw Item
7dab008bde08336a BUT Death Fall
7f32dc80e94a94cb BUO Emote Arm Wrestle
809d74edc29dae55 BUO Step out Headache
81081a1de4b4b983 BUO Emote Slit Throat
8221e9ef59837f33 BCT Death Choke3
823541bc5debde11 BCO Emote I see You
82843f617c8b8cd4 BUO Emote Slit Throat2
829555c23d9f753c BPC Prone Backwards
832a7ecee05dd7a7 BUC Sneak Forward Uphill
8363d82d09e307e2 BCC Sneak Forward Crouch
83e0733c4ea6f713 BCT Slide to Seat (Train?)
8454e0b1809df560 BUO Emote Rock paper
847c7766a42f0dd8 BPT Climb Up
84cf48bc6d95b771 BUC Walk Downhill
84de55beedcc3813 BUO Emote Arms Down
84ea9ed221653980 BCO Holding Pistol
85e1d28be38b272c BCC Crouch Dodge Left
85f148a988fc2086 BUO Emote Binoculars
86af822b70d9bb3f BUO Emote Deep Thought
86cf88fcc98a253a BUO Emote Thumbs up
8794ce758e8a1a61 BUC Walk Forward Ready2
8865ce7f7f79b11d BUC Dismount Turret
88e496d2b5db718b BUO Standing Thrust
88eb7867f3045612 BPO Prone Hump Backwards
892b73ad118356a7 BUO Emote Push on Door
893ca7529d90dcc8 BUC Emote Taken A Big Hit
8a0bb637211e5b30 BUO Emote Hug
8a0d8d30c431ac33 BUO Emote Rock Paper Loss
8aaeec3a9a9e618f BUT Jump
8ab7e3234158efc7 BUC Exit Cryo2
8bad7c9e53f1f975 BUO Emote Blow Kiss
8cf02c25b2da69fd BUO Emote HeadButt
8d8e75496f6f073f BCT Climb Out of Hole
8de4ce136ccf9878 BUT Climb Up2
8de572ae79e5bb0d BCC Sneak Uphill
8ebbeec1daeea64f BUT Big Step Up
8f6d7f02cdd885f4 BPT Death Electrocuted
90085fcea0a85044 BUO Exit Cryo Headache
9093e9b73bfd5c9d BUC About Face Walk
932294c9532ed089 BUC Emote Big Cheer
93323a8905658b1c BPO Emote On Back Point
93864239988ee61c BCT Emote Despair
94cf9ad977b0a728 BUO Emote Arm Wrestle3
965a401a1d63e2fe BUO Emote Rock Paper Democracy
9701addac2ad7f83 BUC Run Forward
971a6eb5edf1187f BUC Pull Open Door
972aa1979477e850 BCO Emote Squat
9769eb719c9cd1a1 BUC Emote Slow Walk
98e7000b43c11df8 BUO Emote So Strong
99154c49011ab00b BUO Stand Shoot Bolt Action
9994e9a6b1b4714e BUC Quick Step backwards
99a071fd8409eb55 BUO Emote Dust Off
99d5d59ae57dec6d BUO Emote Think2
9b37d349c7e23d45 BUC Standing to Jog
9bd2cc675cfe2cd1 BUO Emote Rock paper Explode
9c4c1da3df84e397 BUO Firing on Turret
9d7a3214a0c551c4 BCT Slide To Seat3 (Train?)
9ddc4da68ec9e95d BUO Emote Spring Ride
a07fe46bce049e7d BUO Team Reload3
a14e1f5e342a6d49 BUC Emote Mad Laugh3
a190dc4773a68365 BUO Push Open
a1c6800b0e7bfc4e BUO Emote Foot Step
a3c10197459b5e20 BUO Emote Choo Choo
a42ce5c162029c47 BUO Emote Stim Neck
a48969baca2b574a BPT Death Fire?
a4950a590f729419 BUO Emote Beat Chest
a5810675438a746b BUT Sitting in Bus?
a6540546dc3732b3 BUC Slow Walk Forward
a73c50f962cf5693 BUO Emote Bad Luck
a7bd5d4db8f1bf3f BCC Crouch Walk Forward2
a9d12a1518c57250 BUO Emote Big Giggle
aa2acb249d4ac07f BUO Emote Not Entertained
aa550ffa009e292c BUC Space Jump
aa60728bbb09146e BUO Emote Handshake Too Cool
aa9907424cf4adf5 BUC March in Place
ace3f06812a327ab BUT Big Climp Up
ad7c5feac64332b7 BUO Emote Boxing
ae5f878bec471349 BUT Out of Hellpod
b0105fd7bf519984 BUO Emote Quick Shot
b0ae9dba4d336bca BUO Emote Rock on
b15f5711026cf0c4 BUO Emote Cute Bow
b2baf724e465c7c0 BUO Standing Use Console
b2c75d07dbef4187 BUO Emote Flap Down
b3bd4ed9f485f45d BUT Leap Over Fence
b4bdd196f0bd8973 BUO Emote Hush
b5c9e7fd758a38ea BUC Walk back Cautiously
b648d29a155a8d56 BCC Crouch to Kneel
b969ec68220e4caa BUC Walk Dust off hands
b98edefbcf731593 BPC Climb Through Window
ba1748b2b78c0a0d BUO Emote Boom
ba86fc284f356289 BCT Death Fire2?
baedd8db9341a9ab BUO Emote Halt
bc2ddd3209ad83ae BUC Emote Finger Guns
bd08f0b7b2c30eb9 BPT Death Melted?
bd2c35f45249c297 BUT Heavy Pick Up
bd98e103e9a2daf4 BUC Running Forward3
bdbb1fc2b913c996 BUO Standing Stun Baton Ready
be76b15f585b8472 BUC Walk Forward Dust Off
bf07eb9e4dbd67a7 BUC Skittish Run Forward
c00ee9665e80d515 BPO Emote Sitting2
c05adaed7032edb6 BPO Prone Scrolling Map
c132c0a9b1f63d92 BUO Emote Wrap it Up
c1905be790035008 BUO Emote Dramatic Think
c1fc3ad334b61486 BUC Emote Phonk Walk
c2149bab59a71160 BUO Emote Go
c259ccabaf3572a2 BUO Emote Call Me
c28b438d3e31ca99 BPO Sitting Looking Left and Right
c40e23dc0fb5c9e6 BCT Emote Dramatic No
c4e07420e23157d8 BCO Crouched with Rifle2
c54618bdc159d500 BPT Death Fire3
c7afcf9974cd9429 BUC Sudden Lunge to Right
c8aaaf3c8c727769 BUO Emote Dramatic Pose2
ca009135c0f4b3a8 BPT One Handed Handstand Get Up
cab3f158d2292cea BUT Crouch Walk Steep Hill
cb4d0609b9d7e280 BCC Crouch Walk Slow
cc53c06dfdb00851 BUO Exit Cryo3
cd80e4b700f28151 BPT Death Holding Object
cdaa0283501cee47 BCT Out of Drop Pod
ce5ed97dc3c5e599 BUC Steady Walk Forward
d04546b34ac9f43f BUC Spin in Water
d095c78751fc0812 BPO Prone Wave Flag
d125803fb20a7bf2 BUO Standing Reload Bolt
d1d9d02b69d89ca6 BUO Emote Dust Off2
d3a69dc79d2fee76 BUO Emote Boxing2
d49021b6f1e3d492 BCT Slide to Seat (Bus3?)
d4930bbfc1d1669a BPO Bug Crawl out of Hole
d6a06853dd12601a BUT Sit on large turret
d7d1800dc7ca7623 BCT Get up from Seated
d913a9984e5ce91f BUT Mount Vehicle?
d999a338adf26fed BUT Bug Climb into Hole
db3a6e0308033e56 BUC Run Downhill
dc454e6d7fd423ef BUT Dismount
dc6b54032b967889 BPT Death Fire4?
dcb6ccf01d5fa597 BUO Emote Offer Handshake
de7bad5fc790dec5 BUO Emote Bang Bang
dfd8fb48fc120085 BUO Emote Thought
e1b983920c8c40ba BUO Emote Boxing3
e1befaef25796746 BUO Emote Chip and Shatter
e2a5c5dd46a33995 BUO Emote Handshake Grab
e2deda485c091598 BPT Kneel to Crawl Backwards
e30e093277fa9cdb BUO Emote Handshake Too Cool
e31ef18afb4f3f1a BUO Remove Shrapnel
e33f4dcf3bacb3e3 BUO Emote Quick Draw2
e865ce882a52e1b8 BUT Large Climb
e9ff3e0d9db55ebb BPC Crawl Forwards
ea9692f581e209cc BCT Exit Mech
eac3a77b3a5ca50b BUO Standing Grab Collect
ec3580d794cc0a73 BUO Emote Guts Pose
ee1c87d0d840a82f BCC Hunch Walk Forward
ee290c6b0d865344 BUC Exit Cryo4
ef63336ba9f34478 BUT Step down from ladder
ef8c416694755f1c BCO Landing Stand
f027de3b410d8fb4 BUO Emote Victory
f06667559548600a BUO Emote Quick Draw4
f0ffb4a139775914 BUC March on Spot
f4402d9d4237f7eb BUC Emote Too Strong
f63b3d2143624657 BUO Emote Roll Shoulder
f77b1ccbbe1ff48d BUO Exit Cryo5
f79b7b0fe33f5352 BUO Ride Playground Toy
f7d81dbe0142b9ee BUO Emote Acknowledge
f7e3d1bfafb53ca3 BUO Emote Quick Draw6
f93adb23531ddcfc BUO Emote Hero Landing
fb8f5249a6d0572f BUO Team Reload4
fec38453fe72b227 BUO Emote Missed handshake
# --- facets (1653) : measured only; nobody has put a name to these yet
0004fd6ec81ff21c BPO
003ad49517e19bec BUO
0059b22d5deec33f BPC
0060f6e3daa2de39 BCT
0077c30e0813822c BPT
0087685507dc5bfe BPP
008b761795e16314 BUO
0096f3a7db21244a BPO
00ba6932ebcbc780 BPO
00d113ebc891d893 BUC
0129c69817386fb8 BPP
018c0e41c80cbee5 BPP
01b3d0267b9c9549 BPP
01e95dbd8300b828 BUP
023671ef40e168a8 BCP
027e501f272153e4 BPP
037eb394126b6960 BPC
039433ad2d2b7cbc BCO
03b96a878011000e BPO
041468224bf71153 BUP
042d948469a8582e BCP
04a0f391eaafafec BUP
04b5975b302bd69f BPP
04c7e42666014ee5 BPO
04f9d4a14b6bebd1 BPT
050e75bd2e3110a7 BCO
054669ec7cf751ce BPO
056137bd8c08b38f BPT
0591e318ffe4892a BUC
05eca101d9a33880 BPO
05ece961f2bc9a60 BPO
05f840a01fc3c108 BCO
060200f7eee438ee BPT
06555c65ec280909 BPO
065b0b6c2bbeb4ac BCC
06c547ecede4c9d4 BUO
06f932271819be61 BCO
070cd55868069ee0 BPP
070e8e8023002d12 BCC
07155cbcace05ded BPP
0735ada50d2b7bec BPP
077f9491ef35b1d2 BUO
078170d8cfaa9a0e BPO
07a13cf2035b3f88 BUO
07aa66962ef1ba63 BCO
07abadb931a8b2c9 BCC
07be9c958337aac4 BCP
07ee199d8651ad10 BUT
07f0bbf02984f9f6 BCT
0802eddd55ed3594 BCP
0845f6974613f5be BUT
086bed0e3870ea72 BUP
0893aca90a4ddbaf BPO
08cd96aa1a22db3b BPP
08d891cda0078b13 BUO
08ef37979da8043f BCC
0914d079ebdc69e9 BUP
09626f2220012bae BUP
09e40b060e11b59f BUO
09ef262dd349a021 BUP
0a0a6b292dd6dc37 BUO
0a13f7f21dde3c8a BUO
0a154aa6e8439a6f BCO
0a1b6805388f91f1 BPO
0a1bb3d61fab6193 BUO
0a4f3dfd8a4cbcc5 BUO
0a568892d194707b BUO
0a5e8f9248456e1d BCC
0a6d74bdd124dd7c BUO
0a7f5940121fb6de BPT
0a87e8ae4d4870a3 BUO
0a97a0cdbdf4529a BPO
0ada1343a0d7aa7b BCT
0b08cc58ca05ae43 BUC
0b1d9b4789f60b73 BCT
0b3f309d702ab8c4 BPP
0b54a400f9464ea9 BPO
0b6da26e5b2bc9e1 BPC
0b8ffe858dcb2443 BUO
0ba94042ba05efa9 BUT
0bcf40db4ed0a58c BUP
0bdfdce404c8e9a9 BCT
0c0669e58b3091ae BUC
0c32d573d6d846af BPP
0c33dab02790ebb7 BPO
0c40d84d5380081f BUC
0c5265c6ae66b001 BCO
0c624272b2026005 BPO
0cb6041ad00e07b8 BPP
0d26007ed5ae55a8 BUO
0d35b6759d024c10 BCT
0d5a65b27f98ca39 BPC
0dec5b2903ee06cb BUO
0e03ece59d98c8a1 BUC
0e0f11bf097be4e7 BUO
0e2d149483976f42 BCT
0e4a6e78bfe40a18 BCP
0e88e08822cf183c BUP
0e956921e48fc288 BUO
0ee54845c6940d31 BUT
0ee8760e955ef646 BPP
0f28536f69590a4e BPP
0f2e376da08fe37a BCT
0f7ad8f2ffbc465e BCT
0f7dda3a80a579a8 BUT
0f97b58a0994d0ee BCC
0fde4b3f1ffbca8a BPP
0ffcb2107d968b3f BUO
125284458c340ffe BPP
12a128767c705c61 BCP
13257845a1dc786a BUP
132e3bde6aa8c464 BPP
13d997b850082fc6 BPP
141b8083929df14d BUP
14de96d53a748547 BUP
1559e2733f8659c6 BPP
167111c01e742af7 BPP
16f87252af6ddc3b BCP
170eb97c9b371104 BUP
182f046e7da15d5d BCP
19d70dba69eda9ce BCP
1b423bd4d7078058 BPP
1ce62e49f05bccf6 BPP
1d6486e60f19808f BUP
1da08ae1c0047f5f BCP
1dc2b93b85284f74 BUP
1e1f4e28eb6d5dfe BUP
1f181232d4ec956e BPP
1feb940629303e36 BPP
21c15449f0872881 BUP
22037b49786e3a9e BUP
23640289670d49ce BUP
24156799eaaf7860 BUP
250b85024264500a BCP
2551db6ad3260f51 BCP
26d97725da2b75ea BPP
27bf60dff876a47d BUP
2863f91583acedfc BPP
28d73cd0bf52aa5d BUP
2bdfb11030b09610 BUP
2c04f0478086ac00 BUP
2c400dac097dd033 BUP
2c622371982f0cf4 BPP
2ce1350474085311 BPP
2df9b5b0fd1b8018 BUP
2f665ea6f2c6e5eb BUP
2f8371afebf3c667 BPP
30657bb17968e80b BUP
326ae6d4bbbc46aa BPP
32c400e0d94a16a9 BUP
32d2cfa5bc2329de BCP
330ca702ba1e5001 BPP
330d333cccfc40d6 BUP
33c03db4a5276210 BPP
3421c63e1854b52c BPT
342e7e494aebd87c BPO
343500abd92293bd BCO
347a68ec49487301 BPP
3490e9b628d57dd7 BUO
349407536f9479d2 A-P
3495ad77508dd669 BPO
3508699af1b10c68 BUO
352212a6ea023eef BUO
355ff7e3c5fd1d20 BPP
358e17c058bc4714 BUO
35a4af70a4c56ccf BUP
35bede728ba44528 BUP
35d3a07d1d633231 BPP
35f57ed754cbba9f BCO
3628df465f1c079f BPO
366318862806daf0 BPP
367be4014d6ee3af BPP
3682561ab6e5abd8 BCO
36a163caf3584c46 BUO
36b4009857e4bf69 BPO
36e11687ed42f0c8 BUO
37260aa4d34b4ea3 BUP
372f94e479b929f1 BUC
3750455c0c55fde6 BPO
376da01875d7f4ba BCC
37776c55e5881d3a BPP
3788f5aa9019a98e BCC
37c6502d37724f2e BUO
37c9f1b8f06956ad BPO
37d779b81697f489 BPO
37fdaff0ac7e1cfa BUO
380098b217002469 BPT
388a638d4cd4d34d BUP
38b3329c41622392 BPP
38f307350eddbbc4 A-O
38f6465964e6fd6e BPT
398645ee00bf61d8 BUP
39f6d2e2d0a31a6e BPO
39f7549833a3dda2 BPP
3a136557563d1e85 BCC
3a216157f43fa3a6 BUP
3a27ae7e0f1cb0a7 BPT
3a636d982415215d BUP
3a77f597c58fbaa0 BPP
3a8336c9b2adb971 BCC
3a8addf08a36770f BUO
3ac622094fefd110 BCC
3b1718b2c8333a96 BUO
3b75f0586de79cb4 BPO
3b7dbe3b4dbcb3b4 A-P
3b8d63a60e11c280 BUO
3bc839659fd47643 BPO
3bd3f23f820a132e BPP
3bd66404f10f3409 BPP
3be9dfae8429efca BUC
3c6c069f9f0c95b6 BUC
3cc64688e2f03756 BUO
3ccdee1e22e7eca0 BPO
3d201bb74658fb3c BPP
3d23e1c86c8e13d6 BPT
3d383eab1d8f9dd5 BUP
3d852b5e64ef3bf5 BUP
3da9f46aa0b9eac6 BPO
3ddc141314ffddf0 BUP
3e4aee13bebf06e3 BPO
3e844d3255e0c43f BUO
3ef0e2e8b6fe34e6 BUO
3fdad40d58b8dd06 BPO
4020fb31bc93f84d BUO
404a101f7a1b0506 BCP
404c0f777953567c BUC
404f98d1d84e449c BUP
4058d9f1e9a7305a BUO
40893b757a0eb11c BPO
40b4776681fda7fb BUC
4129b61158250343 BUO
412b8a315536b094 BPP
413733446141ef2a BPP
419a4fd653825e52 BUO
41a661a3c8a1eb4e BCC
41b2c9993d40b9b1 BCC
41bb8c34e9cb13cf BPP
420b11a76dacf664 BCC
421cc109ad2f0c99 BPT
42784329a2f192b7 BPO
428e136cabad3cb5 BUO
4295160a1b9e98ab BUP
42f73c30b97a278d BCO
4313789475e4b7db BCC
43432de67b403605 BUO
43612fbfd8dddcaf BPT
43620c7afe3ca69e BPP
4366a7f7d0485c5a BUP
4376545b0f98b4f9 BPO
437e83d63655cb64 BPP
43dd18d63acba6ba BPO
441323d9706713bb BUC
44756bbba9bb7465 BUO
447839fb37d2c7cc BUO
44c3a101fd11392e BUT
44dba5de7cec9b8b BUP
45032caf85c75145 BCC
45510931fb68adfe BPP
45746908e905cd53 BPO
4576b626707fed69 BUO
457ed347be026f56 BUO
457f4343920178d0 BUP
459235aa8846b813 BPO
45951731db753c35 BUO
45d94419cc84a3b9 BUC
45df40a021ce0f12 BPC
45e93fe733c20487 BPP
469cb23d97d2e41c A-O
46b3ba5bc5b0c104 BPO
46f6114a28c7c564 BPO
4708271f7a7bf79b BCT
47423c6446d1ef8b BUO
4760b942d3206340 BCC
4821db16fc8ea299 BCC
4868f232ee12f1f2 BCO
489564fc56491198 BUO
48db0f901ff3e348 BPO
48e821aabcd8129f BCC
48f1c37315c681a8 BUO
490209f139358f1c BUO
49348d665924030f BPO
493c4ac85b979219 BUP
494464955b2250f4 BUO
4958ef6ef6d489fe BUP
4968d6bb1bd13085 BPO
4978808e74eb06f5 BUO
497ce0970560df69 BPO
4990fd72e40aa628 BPO
49bce1a4532745ca BPO
49bce755dc873f9e BPP
49d4dbac466ed49e BUO
49e0e478d2727bcb BUC
49f6fa95d948c901 BPO
49fa8b15614f7a64 BPO
4a34b085b0872c45 BUC
4a466c09a28e4c93 BUC
4abb7414ffcfd42e BPO
4ae17b48d62bc00a BUO
4ae8bbdba6084a4c BCT
4b5622a56813c579 BUO
4b751c52ee448042 BUO
4bb43072814d5f65 BPO
4bd81968fa29cbae BCP
4c24324277fe3425 BPP
4c32ced085992a61 BPO
4c3e80da898a6d0f BPT
4c5c900cc5fb26ca BUO
4c8d17ef2cae0757 BCC
4cb5e6590b1d824d BUP
4cb634ac8d319fcb BUC
4cd5a74b72fcb791 BPP
4d3f0252ba8c70e3 BCP
4d44f84302b410ec BUP
4d4a63975a3daa38 BPO
4d8725a4c7e64032 BPT
4d8faf3645297161 BCP
4e0602d7bdefa988 BPP
4e0b1eaf0c8e6b75 BUO
4e1d84a48de3c811 BUO
4e43bbd241bdb92f BCT
4e4bb96d0706f337 BPP
4e7611609e74221f BPP
4e80fa9354ff5793 BCO
4ebdf887ee3eac36 BPO
4ed895f6f59c5ab9 BUO
4f05574f5e90ea33 BCT
4f0640926798bb72 BCT
4f074488dcc17129 BCC
4f14006156e2e686 BUO
4f3dfe95e4f6792f BUP
4f6e9044fae5fca9 BUC
4fedc92d41a3a3d7 BUO
5002a8fb5eed013a BUO
503e62ec5939193c BUO
5054f0cb64b9e737 BUO
507f6e86fb11bc4a BCO
50c7675cdfeabce5 BUO
51055487952978d0 BPO
5124ff923a06d035 BPO
5139c441686452d1 BUO
5189b83a9d5b258f BPP
51934d9c3cbe3cfb BPP
51ba57f7ae216001 BCC
51c5b0e94255e8c4 BPP
51d188f41266b64c BPT
51f3f870d0565d62 BPP
5205337564e88958 BUO
523dbc89ba7fe2ca BPO
5331f5537b4ac7c1 BUO
534a60b283b04278 BUO
537444a618ff635c BUO
5390d3846d7fba14 BUO
539fa71f346ab4b2 BUC
53c8d100474a550c BUO
54064ee4f310c9d3 BUO
5419056a968d422e BUO
543d4e28ad8ff680 BUP
544d525d43aaa9a6 BCO
544ee9e74413d9b5 BCP
5485a8efb87c37c0 BPP
548afbe9f264f31e BPO
54912388f820d1c6 BPO
54b185351eab590d BUP
54e73eee048d9867 BUO
55273b063fd12d78 BPT
555646a4ea921d6f BUP
5560f02e16250ace BUO
558a4adcd3f4a008 BUT
5596ffe15791150a BCC
55c09cff8464ffcc BPO
569d51bce4771e07 BCP
56da26696c8021b5 BUO
56fae4f7ddcd22f7 BCT
571cf649e7377d7d BUP
572420eb97e53e07 BPT
5765bae6c193e5b4 BUC
578d1d1ee63abd29 BUO
57ac311bc9a0ad91 BPT
57c484c451b965a6 BCT
57c7e367c3ceafe5 BPT
5822103427cb8c1e BUO
582d62eabda5d200 BUC
584b10d868ca9fd3 BPP
584c548ffe20c17d BCC
586aa6a2978dbc99 BCT
587df447a0f2d0c5 BPO
588120902d15d08a BUO
58843b6138103a4f BPT
58a955e741401694 BUO
58bf2b106f6f99ff BPT
5925ededb45c5b9f BUO
594e55be9e55b9ec BUP
596e47c853942e44 BUO
597602437565c80a BUO
59aa716256640996 BCC
59b0818f0369395a BPC
59bd5f9316912b27 BPO
59c0b12d07218b64 BPP
59cc5a06744b4e0e BCP
59ce962980c990a1 BPO
5a4a062a7235d299 BUO
5ad5d5c1304ea031 BPP
5ae0b094773134c7 BUC
5ae24f822fbd92fa BUO
5af82721f74786bd BPT
5b13664b46e9d3bd BUC
5ba499b204f5458b BPO
5bc637efc520e25a BUO
5bd954881fa9d757 BUP
5be09f2916664ae0 BPP
5bf50cc4fec249d2 BCP
5c0e6f1796cc8022 BUC
5c53c857fd92ad74 BCO
5c600d534a9fb5c2 BUP
5c7b9a96ffaac26b BPO
5cb42bb3df655853 BCC
5cc097c4ba6d134c BPP
5cd7be710991a091 BUO
5cf1a51c03bc86b7 BCP
5cf4e5b83164fd3e BPO
5cf8cceeaba80461 BPO
5d469189c92fcf5c BPO
5d7cd776dcc4b376 BPT
5d865cf0bbbbede5 BPO
5da9f2e4544d1fe8 BUC
5dae5d9d45e2e263 BUP
5dbf2d5d65c3111d BPO
5e17db2cbd8e985c BUP
5e19cab6ad28a7f4 BPP
5e301f3d1911a632 BPO
5e3b73287e7f7fe1 BCT
5e716d1275ce56d0 BPO
5e817f7edcd71c77 BPT
5e8232b5a6e77e8a BCO
5ead5764884e5a9d BUO
5f51b4301c8c14ea BPO
5f594954015139e4 BPO
5f611903d930efe9 BPO
5f66cf2eedc13869 BPO
5fa8805d9d6f5d2c BPO
5fbc86ad640f4939 BUO
5fc1ba631e71c371 BPO
5fd4afd7d84dbdd5 BUO
5ff27fca7801a63a BUO
6029a261e5b6ee22 BUP
6034a17d40ee21ca BPT
603803b9dc6f8dda BPT
603c8ec1bf01e891 BUP
6057cd4f5f2d60eb BCC
609c43f04a57cc8f BCC
611696b5216d806a BPO
611da14a4e951cbc BPT
6125f1b5773c063f BPO
616ad5b915c9ae32 BUP
6190ca6b79b4c389 BPO
6195f7a3b26b68ae BCT
61ba0a519a127fcd BUO
61d0620475558f4e BPP
62180269cc46e4a3 BCC
625ab0de4091716f BPO
625c588452c13f8e BPO
62a01139f44a0872 BCC
62be71eb3ca236af BUO
63377792b9d3e8f4 BCT
63526681200e3114 BPT
635faf3447723441 BPP
6361a9d4ff765717 BPP
638540d9b2effbeb BPC
638e0a049e6e85c3 BPO
63b723ecb4badf80 BCT
63cc7fc2980c4008 BUP
63ed3550fd5b7855 BCC
648a4ac86c56149e BUC
64ac2af5882efcb2 BPT
64b0dda31d3fde6e BUO
64c7506540ab37a7 BPO
64c7ca69443ab334 BUP
64cbf907dee06153 BUP
651c2c7f3431fbdf BCO
6531dedfb8c08b60 BUO
656b2baa354a8283 BUO
6580e64b2cc286d6 BUO
658113e08e274dc4 BUO
65832122d1e2cb58 BPO
6593172334bcdff3 BCT
65add057c30d9f1e BUC
65aeb0a1e78605b9 BUO
65d50497ddba5556 BUP
663720199333dd51 BCO
666a19da26066beb BUO
66783af741a800f3 BPO
6711894e4bffe000 BUO
6726669cdb00ab0f BPO
672f5552729b6544 BPP
676f57019f828e6e BUO
67774cf5d275a05a BCT
679d5ad29abfc905 BPT
67b45e91416ec7a8 BPO
67b77fade6bc99bd BUT
67dced3ae6c9e3e5 BPT
67e4fcaab583eedc BCC
67fb709a0dc7c18f BCT
68455ca0669590ec BPT
686300b86cc1a8ad BUO
690629ad604b2051 BUC
690ef32c957b8e74 BUC
693f147b4dfe1745 BUC
694275f25ef3fca2 BUO
699576c982fe39ca A-P
69bec243a5c74461 BUO
69c5b235b9f798c6 BUO
69cc2250a78aac0e BCC
69dc02fca03ea636 BCT
69e87310cced3b64 BUP
6a021dc34854262a BUO
6a04627446328a95 BUO
6a054fb671bff7af BUP
6a1910c7e19d63d4 BUP
6a22b3e72eb1e1eb BUP
6a9f200193cd46fc BPT
6abb717b0986e5c8 BUO
6ad8d72932998d08 BUC
6ae656d9c4b90da8 BUP
6b13345ce8d9d7c4 BPP
6b3bf210f34e8229 BPP
6b6837dbb817d2b3 BPT
6c05decefe0244cf BPP
6c725e5d1c68023b BPT
6cbbce966f63e55d BCO
6cbca040ca82fb2b BPO
6cca2bc127faee23 BPO
6d11692b841e009c BUO
6d1f70a2f30e3b6a BCO
6d2841bf1da927f8 BPP
6d28bcffa99ea9c2 BUO
6d4acc2f5cad4176 BPO
6d78454d31bb6912 BPO
6d868c071c90a6dc BUO
6d9cc0d3e11f0576 BUP
6dadd49b07d0158d BUC
6db08f7036d96eb6 BPO
6de20894049c8d3c BCO
6de6fd6f178c3a29 BCT
6e01a730db1091a4 BUT
6e0bf293cdb82095 BCC
6e965aa2569cd3ae BPO
6eb55a4564279398 BCP
6ec73f5cde1c0030 BUO
6f17b7e91bea1127 BPT
6f4e09c867490f0b BUO
6f7909b40f5dc886 BUO
6f8700ab17b46772 BPO
6feb0f2ad9e0419e BUT
701e5b571a00bc13 BUP
701fc6dfe5b5c0a1 BPP
70404d65613080c3 BUO
704454ea73767f51 BUO
704c5b3c5a875deb BCT
706370f5ca636ae3 BPO
706e000466a328ee BPP
7080c99981465051 BPP
7088da026278036f BUO
70c3676a8ad3fc00 BUC
70e97a8afb33d0d8 BPO
7101b14ee2face0e BCO
7112cb968bd04a12 BUC
7124eb13fddd78c2 BUO
7149785172b6d784 BUO
71521cb815d1f538 BUP
7182223d9f984db6 BPP
7184016d5c6f7be0 BPO
71a1651fa9007868 BUO
71be095c91e7c4dd BPT
71d8c35304ae11b9 BCP
71e59b8b28f22b8e BUO
7239d88178ca8e0f BPP
727864036757a8fc BUO
7283ef14c3b19232 BCC
72e68f72557cb86a BPO
73047244f73e523f BPO
730fd879970239b5 BPT
73213fd75ceee54e BUO
732f90f5a3816d4c BUO
733e73ee0edf3fb8 BCT
733f0bc73c480379 BPO
7342d62622f86d56 BPP
73565392119f6b1e BUO
7369b5ad2c1bae38 BUO
7380a307a54c695d BPO
73a4b8e5bee3faa6 BUP
73bff34b4a649c22 BUO
73e5f64a5f2193b7 BCP
73fb93727e0a2a7a BPO
743371f0996ee98f BUP
7447adafa892b147 BUO
745c205797501ee3 BPT
746f0bac72eabd0f BUT
746f20804ec10770 BPP
7470005a98192da1 BUO
7471ffc1e84eed9b BUP
74d67977809dc0a4 BCO
74ea76a444305978 BUO
755a84695de68bd2 BUO
7566273fd93483ba BCC
7583bbe5c70c42fe BCO
764e3744f977d06a BPO
765bc9741ded513d BCO
7681fef7500cd789 BUO
76b75f2675df64c8 BUT
76d70eee07bd677e BUT
76f8d0e4e099fd1b BUT
7707fd9bbf9c74f9 BUP
7708acc025a23a8d BUO
77591a3ad23392fb BPP
777efbfef27e96fa BCO
77d41438b72dfe6f BUO
7831fb59f37a03dd BPO
7834b8655f516062 BPT
784f7a296b610b12 BUO
784fbfd06f9532a7 BPP
785aee1fdfca0935 BPP
78754346be4d6b88 BUO
788a567f6bda6b8b BUO
78a961e9cb2301df BPP
792a12299bd68835 BPO
792e7867104d2157 BPO
7980d4a950ca6597 BUO
79860565bae4f952 BPP
79c6d34205fc70da BUO
7a25f5d3736832fe BCO
7a5c6e58f0e01cbe BUO
7a7c7bad24bf3987 BUO
7ac6175059dd9fd3 BPO
7adae30dffe23734 BUC
7ade8a69983a2720 BUP
7b23fbc7940cd95f BPT
7b46a4d42285fc73 BPT
7b59c870c51aa8ba BCT
7b6543dd02fbd3a9 BUP
7bb14b13eac53c49 BCO
7c4feea79582f2c6 BUC
7cb3cac426527963 BUO
7cb8a0885581697a BPT
7cfc419953a63160 BUO
7d4a3c8ef0f0822f BUO
7d9b6ba0757cd13a BPO
7da9c08fdaba6807 BPO
7de17a29d5ef7dc9 BUO
7e040cb642e257aa BPO
7e163d3976019423 BPO
7e2845e052e11692 BPT
7e42b904a49603be BPO
7e7c8d0d19bb5d10 BCT
7e9ae50e27c415d6 BUC
7eb5342cc542a0a2 BUC
7eeb65d8d8ea7b7d BPO
7f1627c5c6ba159a BPO
7f177e6f9f6242ae BPO
7f82289a30e9e46c BPT
7f9e8dddafff2a09 BPO
7fa1dd73e091adb7 BUO
8015511287669afb BUO
803342ddde43cf0c BPP
805619c43b6dcbf2 BPO
80abfa57e84bdbe3 BPO
80c851c1b3b2595c BCT
8157253d041fda73 BPO
815984068031019b BPP
815a529810b039ec BUO
815a8cbbf4168725 BCC
8165095b5c828c11 BUP
8192da59cb5ba539 BCC
81ab530fb0a463bc BPT
81e058c9544f2eb6 BCC
81f71c82f274ec1d BCO
8207465391c41962 BPT
821d7b417beeec14 BUO
826fd4bfe61514ba BCP
82716e133f25eaae BUO
82c85ff630d9fd77 BCT
834c57e3aae397bb BUC
8387b990d806644e BPO
83953d022e1bc1ae BPO
83b5394ac47889d2 BPP
83c5e76543d97ded BUO
83e05d19708c6296 BUO
843dc3cb95de3192 BPP
8444fc316f82e957 BPO
848fb06b4f5e16df BPP
84e401889387a706 BCO
84e9ab9e02dfd8c9 BPO
851deb0c49baeae6 BPT
8546dedb69c9a168 BCC
85737eb263596719 BPT
85934d7a19b4c03e BUO
85c0b995c626bd31 BPO
85dac10c070ab411 BPT
85dad3e79ac7e301 BUP
85e35b9533f30d82 BPT
867f0a5c4dbe4f94 BUO
86b62f0d649562c2 BUC
86d63088ae993bf2 BUO
87166400879c8986 BUP
8781737af8388612 BPO
87a5a84a3e83f6c6 BPT
87b25257046d9ab4 BPT
87e84e1ff7107313 BPO
880bf7bd0af30a1d BPO
880d71156ecc85c2 BPO
88168b763dd6abc3 BCO
8837e227cee3a6e3 BUT
88412e0a14a80be2 A-O
88634c490c2caed3 BUO
886815dffcee82ac BUO
887c0054b3f75224 BPO
8887448e39932094 BUP
88c865eaa8502e26 BUO
88d9a0d1eed77eac BUO
88daa0cb33b79b58 BUO
890f04d1dc38479b BUO
891450872a9622ac BPP
893b0d7fe24283ef BUO
89552fdf6f766323 BUO
896675d1bf0e01f4 BUO
896cc95816a73f0b BPT
897d1647de5a3132 BPO
8985729cfa0db998 BUO
89ae7030a1aea92f BUP
89b568bdb22665d1 BCC
89fde0d47da62bde BCT
8a08e679d62cb763 BPO
8a10743d8fdfc977 BUO
8a49fd51b03994c3 BCO
8a59cc2fc7950df1 BPO
8a911154e49f6d85 BUP
8ab27baadce4fabe BCO
8abb8ee660fc72b5 BCC
8ac3ba727b910145 BCO
8aeabbf46c44fe4d BUO
8b1f004367456123 BCO
8b44cf551c60a346 BUO
8bb0a56e8562171a BUC
8bb5ca27e02faaa7 BUO
8bdf81410ee426df BUO
8be6a7f1cd53572b BPT
8c6979aed6811e25 BPP
8c8833afe5115376 BPP
8c9a343bbad981e9 BUT
8cea27d9a22b208e BPO
8cfbe512cd9c0d5f BPO
8d476da6424d4fa9 BUO
8d5ce9ba8bfcb137 BCT
8d713fd2400adb78 BUC
8d8ef47b4dbc9be4 BPO
8dfabbc545412d0d BCC
8e2eb93a7ac094f3 A-O
8e561cccfbeed4ef BPO
8e608edba425a259 BPC
8e6aead2bfd054c7 BUO
8ea7579934c3a351 BUO
8ed3e45177d94a36 BUP
8f153d206690e4a0 BUP
8f19eb31cc77c4db BUC
8f45153dbc030a26 BPO
8f4bedac6eac4fcc BUO
8f5ddc0b3eef81da BUO
8f6a128677e87213 BCC
8fa4182812be3a39 BPC
8fac50116a4cc497 BUC
8fc0eaf02f99d11a BCC
900c1692ad5be0e0 BPO
90197a40257526bd BPO
901c496787ff50b4 BUP
90242055720bdd33 BUO
902b4a70c13315c5 BUC
9059ee7ace39c5b5 BPP
905ed20b4054b291 BPP
90762c07ddde315e BUC
90aedf642b8a6c3d BPO
90c3bd64106e0c6b BUP
90e4623af3e28812 BUO
911fa227c66b5667 BUP
912fd07a8378274e BCP
91a0d76ed07ecd26 BPO
91aca0fa1c146be2 A-O
91c0c280783a2b97 BUO
91e7b9665edb7666 BUC
91ef394d3e804b22 BCO
92023a97f682aacb BUC
921600f81f90f2cd BUO
921d8295fc8b1f7c BPO
92339e0219ae4039 BUP
923afe6c32cacf14 BUC
926c652775796a1b BPO
92881210d828b6f7 BUC
929919363f1b608c BPO
92d7ab1068a16471 A-P
92e18a33ccf3e229 BUC
92e65f195813a477 BPT
935dddb6f9f04b79 BUP
93b424f022f7c0bc BPP
93c604a601ba1c3b BPO
93d08a7741e8a985 BCC
93d7f13326d5f177 BPO
9409a8bd91e62af4 BPO
940da36d95eb4491 BPP
94219e91ae613798 BPO
9428642ff81fa111 BCC
943759aae994abaf BUP
943802e91b53f671 BPT
94503a0894a6b300 BPP
94954d4528ee35d9 BUO
949bd160e06dd324 BCT
94a82b281b755db9 BPO
94bb35671200baad BUO
94beef6870b50cb1 BCP
950d6bdd16bd9ae1 BPO
9517a9395f11f11b BPP
952aadd5222d97c4 BUO
953a909fa0f0c116 BCP
95482840d443fbc7 BCC
9562b42c89f38ca7 BCO
956bf1eab63b4933 BUO
956cf6499166343c BPO
95d76b6c42cdcc94 BPT
95df73071fcd0f24 BUP
95ecea58b0a1a9c9 BCT
96564512ffd664c6 BPO
966628a8fe50375e BUO
96dc7ebe055985fb BUO
96f6057245cb3a3f BUO
971659c41db9821c BUO
97391c16c718f391 BUO
974c93b08ade0b6e BUC
9752305712e63585 BPO
97536ee0de828169 BCC
975ec3af4517ce1d BCC
97b36121a647ae7b BUO
97db99e6f1b61d77 BPT
97dbd79e34583c47 BUT
9818bc3bfd33434c BPT
9833f72831d218a7 BPP
98399ca5f582ecb5 BUO
9839dd3cc7855cb7 BUC
9873e5d09400dd84 BUP
987670772bb40fe5 BUC
9881adf1fdda8e58 BPT
98db693ff9ecd51b BPO
98e35d4ac3e38fa8 BPP
98ea2195736e74a1 BUP
990b5eedba847a5e BCT
99119db6dedcd659 BUO
992253ffc5ffb634 BPP
996b8a3324dc7f20 BUP
99e057ec8cf9e6fb BPP
9a0ed380e1a2b113 BUC
9a4c5aa1af48504b BPO
9a5641cac2b862e6 BPC
9a7ad84cf3e58771 BUC
9aabf1ad548e1ff2 BPO
9aaf2c13b347a213 BCO
9b1966e2bcc2a2fe BUO
9b465b129739cdde BPO
9b9cab445dfb0b90 BCT
9ba19ce2fb7c49c7 BPO
9bf71eb906d47601 BUO
9c201c38cab6335b BCT
9c28e8f3172e4559 BUO
9c2d2de942d0b440 BUC
9c51e03726870f0c BPO
9c9536da642d61d6 BPO
9cbcd33ad9341aac BPT
9cbdf63aa6ace1a7 BPT
9cc68200a3e3ad6c BPO
9ce72f21fed24d92 BUO
9ce87d308723c27e BUP
9cf5c2be4ae800ef BUO
9d12541e7eb3d862 BCT
9d1cc7ce03414daa BPO
9d1e81a29ebc3d26 BCC
9d4597d21b0df88e BUO
9d49a72ff9d6ddc7 BUC
9d6650d288760b8d BUO
9d72691f1ce97eb1 BUC
9d810a9cafa5041a BPO
9d8360fa7823515b BPT
9dba4f0bc8561c4a BPT
9dc5dc1e12a31505 BUO
9dd396491eabcd0e BUO
9df3f9aa5c10f8c9 BUO
9e0f117f6e144f1e BPP
9e258b9f3b81c619 BUO
9e3daf105292ea10 BPO
9e678b3cac838b72 BPT
9e8023db8f668eb0 BUO
9e89d5e873b4d8dd BUO
9f01a8a9932f3ee7 BPO
9f14d6dfaee88f3d BUO
9f2fef9dfd029860 BUC
9f490833652b513b BCC
9f4c0f5e849bd4eb BPT
9f57160d024deeb9 BUO
9f8677dac892a914 BPO
9fb4d89b5898bf49 BUO
9fc9f8a9b27e79d1 BUO
9fceb90a2809ad73 BUP
9fd6b3065d1f4d7e BUO
a0143e47832a171d BPO
a04c576ff4d8fae6 BUC
a09a885c0820cde1 BPO
a0c1c515dda9ec95 BUO
a0d49e893b90e39c BPO
a0e9de9666ceeae3 BCO
a1087f7b5cbc5535 BPO
a144ba4abde5ad5b BUT
a147af8ac230867c BPO
a1653d9ce0a3a99c BUO
a1b1746d38936a1a BUO
a1bf44d2fdce3806 BPT
a1d521054335ec09 BPP
a236a67ea0c8caeb BCC
a2b9e70eae991724 BCO
a2d1957872fc7946 BPO
a2dcf636206e2359 A-P
a2fa0e93604562ee BUC
a324aaa7741eb392 BPP
a332d9e81cbae457 BUO
a33a99f774194a7e BUO
a3903b53e737e7e2 BCO
a3b4c4d2bf7e14fc BPP
a3cba71d1344203c BUO
a3d1cfb67a6336b4 BCT
a3ee978c231355c4 BPT
a4206a11ff41594b BPO
a42d630872fea5ca BUC
a439f16762eb57d3 BPO
a450bca94be29ade BPO
a47b6252fc6018f3 BCC
a488aa2b18118b76 BPT
a48d46474b6cd6a9 BPO
a48d509bcc683582 BPO
a4bb762617972aae BCO
a50aa78f64e916a9 BUO
a5164f69cc56ebc3 BPO
a562f79ad96dd583 BPP
a5cbac775569fbca BPO
a609b2eacce00221 BPT
a6153573c9348102 BCO
a650c7e5a9e797d9 BUO
a669af4eaf4ff9fe BCC
a6b938b3465a2ed2 BUO
a6d764bef137f012 BCT
a6da3d8df0b5640e BUO
a70933a7a20e9325 BUT
a70e70299adfed6a BPP
a716239760063388 BUC
a725dd1daecab6b8 BUC
a76f46f822f8a80f BCT
a78db1c4eb3eb12a BPO
a79cf71303ed1922 BUC
a7a5c25622fd4c9c BPP
a7ba6fdeb85f14b3 BPO
a7d1b2d8265001e7 BPO
a7d99699619357c9 BPO
a7dd85a4ec06d390 BCC
a81ad4093b5b41b1 BUC
a81b8fb128c2394e BUT
a8290671abb3ab2c BUO
a829c8a0448518b0 BCC
a839beed9c5c88d9 BPO
a856cc9977a685bf BPO
a86ec15150ba8318 BUC
a872cd66795e1e00 BPT
a874d492bf6a43e0 BUO
a8a8015653f62e51 BPT
a8fab726ae41dc29 BPT
a91982495e8f5caa BUT
a91de350100d59f5 BUC
a91f5068105b1c5c BUO
a93735aadbbde097 BUO
a9491fd5858ef517 BUO
a94baa037219a579 BPT
a966ba2c049e3bec BUO
a974d4a7393e3134 BUO
a98e6d6d03186163 A-O
a9978abeca022f3f BPP
a99a6ef6f3fc3533 BPO
a99f89b13d02a508 BUO
a9d0bca443d348e5 BUC
a9d2e9b54f6daab6 BUO
a9fb9b2549b86098 BPO
aa7786c295705ff3 BCT
aa825be4fd3ec550 BUC
aaa8a8839b00a30b BPO
aaad8a54c2d1532b BUP
aab7187e7a01540c BPP
aacdfcc41dd6fbe9 BUT
aae13f6623ece1be BCC
aae65c7edf05b1fb BCP
aaec857162fa157e BPP
aaf7664df82e7265 BUT
ab059b8fe27762e1 BUO
ab07982afbed9d39 BPT
ab2e2213c1aea69e BUP
ab4a540d107e19c5 BUP
ab6854fa7c07f99e BUO
ab8f4adfe2b33c88 BUP
ab9f827f27f4da34 BCT
abdf9d71e84ef2d5 BCO
abe26f7d7bf720f8 BPO
abe89f22d6bf8d6c BUO
ac18b0c214367d86 BPP
ac1b0572781c8b1a BUP
ac240fdc6fae6da2 BPO
ac370bbc408ad65b BPO
ac8bdf3c4d7e5101 BUT
acf73bebf210df84 BUC
acffc704af164217 BUO
ad69c9175adf9f80 BUO
ad7f581b2974aa3c BCT
adb3358e02bfbd35 BCC
addc638a15335213 BPP
ade08dac5c3793a0 BUO
adf951430e160af7 BCO
ae27914d10b803b8 BPT
ae3e6ae35c98419a BCO
ae6b2e26c5b961d8 BUC
ae71c700028dbc52 BUP
ae7857f222044957 BPO
ae815462311bf279 BUO
ae876dbf6a7b381c BPO
aecadc29ca3e2718 BUO
aedeebf5f5571712 BPO
af094072538cd547 BUC
af4ae9075821a318 BUC
af5a9f5d10399b6a BPP
af76e41f823772c6 BPT
afb66dc874c8e6b9 A-P
afb70a7c048986b0 BUC
afe39ad9376b00c9 BPO
aff85c05540c43c8 BPT
aff9a238df7d2d81 BPO
b025cfbf682ba7fa BUO
b0266b45733b67ba BUP
b069881b861bb6bf BCT
b0967167cd82744f BPO
b09baf65b0e76a2c BCO
b0a4c5940d612edd BPT
b0c9f3c7328eea07 BUP
b0f3ee3475ba0fe9 BPO
b1155f997815650e BPO
b120c44bd23ad361 BUO
b138815d6d7d68ef BUO
b140080348ff0120 BPP
b14136e4ad691962 BPP
b159079f34427f3c BUO
b160a318efd323bf BPO
b1b278ce15af67d7 BPP
b1b456f74aaa528e BCC
b1c3d84d3841ded8 BPT
b1ed26806cc0664f BUC
b20e717132377ac4 BPP
b238111e6e8d97c4 BUC
b242b7eb84da0f69 BPO
b2932a761c673e8b BUC
b298a5b7bf3c65aa BPP
b2b5b202ac4193eb BUP
b3669c37e81fd33e BUO
b37d046e1f4f0202 BPT
b39e0a7b92f2839b BUC
b3a9203e2da06dec BPT
b3bcfab13da205ca BUT
b3ddf62732234cfa BPP
b3e67ee7d0ef756b BUO
b3f61a3acb1f29cf BUO
b3f972d7a0303b23 BPT
b40750501c99a7a4 BPT
b41be8082a7ff778 BCO
b438edcebc890c3e BCO
b457f477d06674f7 BUP
b45a5b5126fad0e3 BUO
b496c0e4d2645730 BPO
b4bba13e1d510e76 BCT
b5085ab1f219ffe3 BCO
b52decb6ca4bcc2e BUO
b532ad193b5d5fc5 BPO
b54a89b19e2d6562 BUO
b54ac487e7f2290f BUP
b578610433553968 BCC
b57db2c130b1e396 BUP
b58b16a4d0d942a2 BPO
b5c0f02c7c230029 BCP
b5c56cf3142016a7 BUO
b5e20f3af7ae90ba BUC
b5e4a0b1e5d7020c BUP
b5ed8ae5b34859bf BUT
b602c48589712c5f BCO
b60b2ee1a9e3a958 BCT
b62588a8ddc4b13f BCT
b628ef28ed8426c1 BCO
b64d8fa36bfd0c56 BPP
b68d13e889499686 BUO
b6b8d46cd6c2b999 BCO
b6d3d22d5794721e BPP
b6d9ec53cdc51504 A-P
b6f007a22af2367e BCC
b726036661e01b81 BUO
b7312259c15366a2 BPP
b739141450a24386 BUO
b752747c699bd12a BUO
b77659eaada60ee1 BPO
b7a2980068df56c6 BPO
b7ab92926dab11ba BUP
b7b5a123492ce389 BPO
b7bae577558b53eb BCC
b7bcafe0f92a8115 BPO
b814069f343bc0b9 A-O
b82d0bbdccf52a55 BUC
b8307929c62fc1f2 BUO
b866daf15b438102 BPO
b86719391857075f BPO
b885a4d6f6e8a7ba BUC
b89deb2899fe7267 BPP
b8a7c8e7e77280c5 BUP
b8b6ab836bb50c54 BCC
b8e692dfc71d71bb BUO
b8efb9464c6abc0a BUO
b8f19b2952c71f65 BCO
b94c1010be09ebe9 BUO
b953c615edf7df0e BUO
b96132d6ae76ed0e BUO
b9717e67c1701346 BCT
b98012fb61b1c05c BUC
b984b9f1b5fd9cbe BCT
b98b0b83443bab1d BUO
b997626885e6eda5 BUC
b9b9208fa4cb36f6 BPP
b9bab22f0adc22a4 BPO
b9fc7384ce14c2a2 BPO
ba013cfb5084115a BUO
ba0a2e6da265ebf9 BCC
ba1fd9a3c4be3e9f BUO
ba4b1ff9a5475242 BCT
ba4d3f37b98d3240 BPO
ba6418e5c058bc9f BCO
ba74f0a45c9ab288 BUO
ba8275cf5fad746c BUC
ba8f0f395105b7a7 BUO
baa5e57e0a561aeb BUO
bb04cbe0632a3cbb BUO
bb113b9e33514a11 BUO
bb3119e8f44a778e BPT
bb8fc288f4ecdd59 BUO
bbab22d83938027b BCC
bbcbf0161dca698d BUC
bc025188d79c20ab BCT
bc090e78567a52c8 BUP
bc33b70c5421269d BUP
bc3b887239a1d88e BPO
bc60f708889c19e2 BPP
bcc49aebb2ef06dd BUO
bcdd6d7955633c9c BPT
bd20643bcc22830e BPO
bd66b3cd6651dc80 BCT
bd9801c3c53205b0 BCO
bddb0f4a8f126323 BPT
bdf86982db04e758 BPO
be461aa936708655 BCT
bedca18cc818d559 BUO
beed0b93186c4720 BPO
bef59af3266cbaa9 BUC
befea22e8ba8c337 BUO
bf41986f4d753124 BUP
bf52a3813447c2c7 BUO
bf9271730791d843 BUO
c0151ee179235da3 BUP
c0597dfc6b1ea119 BPO
c059eae3a6f313c5 BPO
c06a7355d2e88329 BUO
c07b6d5967ecef20 BUO
c0b7a19082846511 BCC
c0c6a713d5ef4323 BUO
c0c90a3c57f6c51f BCC
c0f134fd442587bc BPO
c0f829c3869f3568 BPP
c0fcf77c3e7750bd BPO
c101a0065c850126 BPT
c12cdb9bae482cd8 BUO
c20d6ee1ba6977a6 BUO
c2447bd12bc61889 BCO
c265c25f0d245e61 BUO
c2668d125b693931 BPP
c279f85d75cc1456 BUP
c282985940b0388c BUO
c28c0256e8640da1 BUP
c29737ad8686262b BCO
c2c16b9f0c49b54a BUP
c2c278a25052d21a BPO
c2f4302fd26cef36 BUO
c302fcdf3c9f5f15 BCC
c3184a16511d25af BUO
c355bb26d48516bd BCP
c36ebdd506f380f4 BUT
c3a714b14f72e916 BUP
c3b23b6f147cb042 BUO
c3b5f1e72c9a9c8b BUP
c3d7c19df4e926ab BUO
c3ee4389c4d0d0e6 BPP
c3ef906d3a8f8c69 BCO
c3f0e084cbe6b607 BPO
c3fa6b86f2512e89 BPO
c40ae88b504d8852 BUO
c40b3e328e665b6e BUO
c44c9d7cf2e3c7fe BUO
c464b05a1212dc3e BUO
c4a198cbb3e3f873 BUT
c4aa95e4113f2fb8 BPT
c4b94f4661b54c54 BUO
c4dbddfb4e752c6c BUO
c4f5f34e87a63e39 BPP
c4f62361dc69fdf0 BPO
c5121d4970bd23f3 BPT
c51d8ad1faa68b9a BUO
c52f4738b015949f BUC
c55c70a6e1254ff9 BPO
c569a12e96db132f BPO
c5acbf39fd971328 BPO
c5b422d4f12bf88d BUP
c5dd3d5c4425fefd BUO
c6291f34c98096ca BCO
c664da16b41a5ffe BCC
c6669154c7ed94aa BCT
c6b14dfa7c332067 BUO
c6c919776b30077a BUP
c6ebf7f20761cc9d BUP
c6f3166584b24323 BUC
c6f71ba43a2800c9 BPO
c6f834ba95fb6435 BPO
c70d634fabb57c40 BPP
c74a7eea4512a5b6 BCP
c75bd8c1f04f50d6 BCC
c7638ae3a3c133ca BUO
c76461c1ce046a18 BUC
c7a144399ca3d83f BUO
c7bb636fc8fb95da BPP
c7cb5d246122f265 BCO
c7f068c77edfd7ec BUP
c7f0fcbf2b4ef0b4 BPO
c7f111c4a67845c4 BCO
c815101bec1601fd BCT
c8212023dfbfabe4 BPO
c8213c80b783754d BPP
c8407f20e39c21d5 BCT
c860e3493460ff87 BPO
c890300bf372a6ae BUO
c8e4633bc08fc9e6 BCC
c8f3d2b4464f147c BPP
c93754157de8280f BPO
c94376832f5c9dfe BUO
c94f6f3403672ff1 BPO
c951a4a2e9ba8c09 BCC
c95b1dd9af51045a BPO
c975d1d80fe151f4 BUC
c98691940c3684dc BUO
c992c949551b3a03 BPO
c9ae3d4e2e06f846 BCC
c9b3c844f491eec3 BCO
c9e47db306569c26 BPO
ca5576b9c13aece5 BPO
caa497111ffaf3e4 BUO
cb4d9bb442edd51a BPP
cb53a361aa46fb49 BCO
cb7c6bd61e817aa0 BPT
cb7c93518ac84ab8 BUP
cb7f674a41f7be16 BPP
cb9a5ad7e030b0a4 BUO
cbc29826ce9e30fc BUC
cbca79b4702b1b10 BUO
cbcc5ee746e8d99d BCT
cc024cf6184f8b1c BPO
cc1e58f72d7650e6 BUO
cc575f30b1d04ac0 BCO
cccaf819b0d09d38 BPO
ccf22dcdf7a76088 BPO
cda1fc1c4259096c BCO
cdb0c95e5b31f2a9 BPO
cdb6a6e4248fe15f BPO
cdba73eb9faf5ea2 BPO
cdbdb2307adbb4a5 BUC
cdcac9990e91e627 BUO
cdd6106e6be3f52b BUC
cddc31b8659da0b9 BPO
ce22cf85eaa34d2f BCT
ce28213b1d623bd9 BCP
ce299ec9064a3a5d BUO
ce68080232e704cf BUP
ce772e9b8a78be83 BPO
ce997d77488896d9 BPT
ceffe1af060db47e BUO
cf1386ca8d2eb1af BUC
cf2d0aba3cb5644c BCT
cf4d5899cf7c7a1c BPP
cf60ccf333b89f6e BUO
cfabb054be644fe4 BPO
cfbab466a742980e BCC
cfc1587afa8d16f4 BUP
cfce24b7845e3643 BUO
cfef443a9cbc89b1 BPO
cff461ec14ed8436 BUT
d08f6244237f7c7b BUO
d09087374e8ce07a BUP
d0cc0cd081975b52 BPO
d0d2709327cf6a7f BUC
d0d37eb4278ea269 BUO
d105a028eb59e7fa BCC
d108b3e9e6b841d8 BCP
d184d213e5d1fef5 BUO
d1b95258814d2361 BUO
d20beda2ce2bcff4 BCT
d217388daae92921 BUO
d2343d27507f9367 BUC
d2378aca536ec5fc BPO
d260a5553f980351 BPT
d264aa434a3eb398 BPC
d2acfc0f56a2830a BCO
d2c479119772fbeb BPO
d30c42e188a1c193 BPT
d3441c40c3890094 BUP
d3510a8229cd850f BUC
d3720291210dca44 BCO
d38dc766c7683903 BPP
d396f52323f4f342 BPO
d3cf0cad727d3198 BCC
d3eeecd55b2655b1 BCP
d40b34ef9b091ba7 BCC
d44a3f97a1b80656 BUP
d45baa69f9174028 BPO
d474dea4a41a24cd BPP
d4cf0dd5d794fa28 BUO
d557565ec6cbefbd BPO
d567db8a53c13061 BUO
d5807983cb727209 BPO
d581dd697fc84ce8 BUO
d598d589957425f7 BUC
d59e3264ca546a89 BPP
d5bc41ed74ee70af BUC
d5db5be720c181c7 BUO
d602239639f1109a BCT
d62d2379cf4955f4 BCT
d6385428eb63ee0d BUO
d6581dca302fce2a BPO
d65f57ba41d6f39e BPP
d67d531e122a4315 BUC
d69e35191b66b9e6 BPO
d6aefe842abafc06 BPP
d6c6e02767ca9cea BPO
d6ca7c06add6162f BPO
d6d97ad90c3b3bda BUP
d6dedb7abbe07330 BPP
d707d744df1c7bf4 BUP
d729f9f96181363b BCO
d74df11ea5c4afd9 BCC
d81634c7e496058c BPC
d81b0222cce71ecd BUO
d82d7347aeeced41 BPO
d881d5553694fc47 BCT
d88b4c1d1ebf2918 BPO
d8a8a0933ddac72a BPO
d8b1d6fd8f332000 BUT
d8e96649111d3311 BPO
d915a28f921b8bcb BUC
d917331a9a742ae7 BPT
d948c8ede353dce0 BUO
d9554f1735b52731 BPP
d97631b85647843b BUC
d98c27d967533f23 BUP
d98c81205d9a0452 A-O
d9a27d930c9d53a1 BUO
d9bc817a0b3a0a8e BPO
d9e9300144b473d1 A-O
da08b5f09ca09b32 BUO
da26645a62aadc01 BPC
da3d57101fd8504a BUO
da404c4465cca11b BCO
da414ca2e2022b29 BPO
da64be14f323d669 BUO
dad12b906d337c5d BPO
dad798f7e0cb44c7 BPP
db263141260dc8d2 BUO
db2a3baf140da1f8 BPO
db76a9abf608e909 BCO
dba134aeaa24370c BCT
dbb106c5c835d337 BPO
dbb66d6c661f2596 BUP
dc06bc73cdbe5f0e BUO
dc1f36f1bbb39523 BUO
dc29139db52054f5 BPO
dc2f2fee098fc0ba BPP
dc3f055a705cb53d BUO
dc74bcdb4592d588 BPP
dc7dca47ab16b652 BPO
dc8d2eecad0c9533 BUO
dcb1ef91612e1f48 BPT
dceb6d46925a2778 BCT
dd3452733c438468 BUC
dd471f2e2e674a48 BPO
dd4dab2c099621e3 BCO
dd4e1274383f63f1 BPO
dd664c271ca6833d BPT
dd90d966e8125746 BPO
ddd597b228a0ce96 BUO
ddeb770cc6881a7c BCT
de30ed154699c76f BCC
de3e33a60ba160bc BCT
de4025e71621954c BPP
de4a12dae6fa9e31 BUO
dea82e3b606b71db BUP
deb0a17044e0f033 BPO
df02778ce257afab BUO
df2d61f9d29cfae5 BCT
df66287be0344360 BCC
dfbf97bf7bce737c BUO
dfd52113ced09242 BUO
dfd6a3237f5bf758 BPP
dfe5984d9d17d8e6 BUP
e054bfe533befcb3 BCO
e05b30b9055b7939 BCC
e06dfb884b3e80ab BPP
e089199fb97f71a9 BPO
e09ad4a5a5c9e8a0 BUO
e0d1db9dd47cb620 BPP
e0d77c3f2256ec09 BPT
e0d9a399bf444d60 BCT
e0f0235dd85bea74 BPP
e105d53eebadd900 BUC
e13280c59b10573e BUT
e14531a031f84d9c BPO
e19e86a9eb3c5aee BPC
e1dd4437be682263 BUP
e20cf54366e6364e BUO
e222ad8ce86c940e BPO
e2689c75e575745c BPC
e297466c65f23813 BCC
e2bd8a481d7b867a BUT
e2c1f2c296ce668d BUO
e2c3ca4338ab9501 BPT
e2d2070b636f963e BPT
e2d70732a93ab685 BCT
e2dc28dedad5b32c BPO
e2dd44aa372eaac9 BPP
e306800d97992895 BUO
e31268edea4e4f83 BUO
e32b270524630569 BUO
e36c9db5fd7dba36 BUO
e3d74d1b37b82515 BCC
e3f3bd0bbb8c0d6d BUO
e436accc9bdcbc13 BPO
e44f8ce3823053b6 BPO
e45532fa88afd569 BUO
e468988d34f65e68 BUO
e4b502e6cabb2b91 BUO
e4bdb10482ecec83 BPO
e4d20768b4a56c3a BUP
e4f564d7bb94b085 BPO
e54a7eff72aa4084 BCP
e585863f5b49a5df BPO
e589808607e008cf BPT
e591e47c239d5dbc BCO
e5c4caedaba19d4d BUO
e64549d97f523d44 BUO
e64b7d238ffc4bb8 BUP
e64dcfc3f01491a0 A-O
e674241a2068b29f BPT
e70ec269af89bfb3 BPO
e7198f91d2808e3e BUC
e76d2bd7b4235b19 BPC
e7a8c60725ccd377 BCC
e7ca13d59c793aa0 BPT
e7ded46025dc258b BUO
e7f0641bb6bed2c8 BUO
e821ca43fe5a95f0 BUC
e826faeaccd8d978 BCC
e84e081e9337c54b BCT
e8660a9fad789efd BUO
e866d7661186d7e8 BUO
e88714781b685e33 BUC
e88ed095aacc4004 BUO
e8b67a3dd5dc21b9 BPO
e8bd5bfa1ffc0883 BCC
e8e5e9284813d7ec BPC
e9005305088ce202 BPO
e90a8277607bec96 BPT
e91626ad80d6615b BPO
e92f472379f195b7 BPP
e95d3978addc29c5 BPO
e965c0da3e1e0143 BUO
e9685300bf2c28f2 BUC
e992c67e7c085c36 BPP
e9d98196d9f5a512 BPT
ea2253ac6e569721 BPO
ea3a44ad44c8e8ad BUO
ea46542387724205 BUO
ea4e1be67665be6b BUO
ea5c573a2243315f BCT
eaaa34e2dba68638 BUO
eaf1f152a5f1572c BCO
eb007754c119b2cf BCT
eb07adcbef8b33bb BPT
eb75f9f2349c845e BUP
eb942f56e4eec69a BPT
eba8abcc84524b63 BUC
ebc3b12dd2479744 BUO
ebfa2132912612a0 BPO
ebfa96a46d481809 BPO
ec0364654c19682e BUP
ec0b64079e552c96 BPP
ec3b5c513809b7ac BUO
ec458256280b23ac BCC
ec63b8fb7be21573 BUP
ec6ad2e4d0093a8d BUO
ec7c9b0e19b2da21 BPP
ecb1c8f88b4d2d88 BPT
ecbb1edd8b8547f5 BCO
ecf1627c0c87111f BCO
ed0bc63ae3bdc2b7 BUC
ed3642cb5713eb90 BPT
ee15521daf403c66 BUO
eeabdb306ae67250 BPO
eed9f2359c047bbe BPO
eedefb3afe53260f BCC
eee67bfe0cfb9464 BCP
eef28f06ba9284ca BCO
ef289b83df564d64 BUP
ef2bb0329a7c9a7f BPT
ef66de02c4ebe55f BUO
ef70e4b7f4bccf44 BPP
ef7a3e8f46bc945b BUO
ef8b0387568061e7 BPT
efa0ad3a50373736 BPO
efbcb9db677f986c BPO
efc7ac273189b7db BPO
effc54e67f3b28d4 BPT
f01eea407978444e BCO
f02518a7b379f606 BPT
f05ffa9eaa981c35 BUO
f06adcb18b334240 BCO
f06f28ac8ffcd668 BUO
f083cc6df9ca01bb BPO
f0afcedcfc90468f BPO
f0c32dc3743f5775 BCC
f0cbffa18e0cb7aa BCP
f0d0b8246f0c37f4 BPO
f0da03691978abf5 BCO
f0e15aab2be87575 BPT
f19507a6f8b1b84a BCC
f1b25874f2bb7990 A-O
f1d5f4069cc45280 BUO
f1fbd6201fdace7c BCC
f1fc1b1bc022a41a BUO
f1fc71f4f0b52d17 BPP
f206faaef01a972e BCT
f215d8e425cfe04d BPT
f26e3d5370e72761 BPO
f2ad7f6401006548 BCO
f2b799425a04b1e1 BUC
f323d6b2df99784e BUO
f3611c4247b36352 BCC
f3cc3dee5ad549c6 BPT
f3e55d3df79f0c0e BPP
f3f24358840aa5fa BPO
f41805dd72e77197 BUO
f42da5d9aa607c72 BPO
f43242c31d6cb87f BUO
f439ec70399a3624 BUO
f44651914115d4c2 BCT
f44b322604bbde8d BUP
f4514615f1eb390d BUP
f46a7b1ad6b92841 BPT
f4c956e80616d2d0 BCO
f4f604d9bdd34c3f BPO
f509d2ea8826a8a7 BUO
f51134141fb43a9e BPT
f56026fcda61dd72 BUO
f57459ca7abd5582 BPP
f58d75bc7828d779 BUP
f610f0af88b05b36 BCO
f65b863d9ba4ba5a BPP
f67800291ddef4a7 BUO
f692139d32913926 BCO
f6af294670be3c96 BUO
f6cb6b96166d7c82 BUO
f6e47daef52817c9 BPO
f70510d1707d503e BCC
f74340318665f269 BPO
f747fbb1f43db971 BPP
f750bd407fe028d9 BPT
f7626252ccb457e3 BPT
f7be26c82819d776 BPO
f7cc2dc00c7d0b70 BPO
f7dcf9a6383bf3d1 BPP
f7ef5d582a361ace BUO
f81e1b4bf76ca870 BUC
f86f774397bf7bb0 BCT
f87649b3ab27e9f0 BUO
f88e07a9f42cbef0 BUP
f8961fbdb2e239e3 BPO
f8afeb2ecb50d2a3 BUO
f931acf92c30d68f BPO
f945aef1005de21e BCP
f94894e788b3ebfe BCP
f95d5a3752e69ea0 BUP
f9f5e75d38a65644 BCO
fa6b8eb24559a4bc BUO
fa81dd6365a9b532 BUO
fa848cba14f98bf4 BUP
fa8aaf0eb0df52f3 BUO
fa9528780a2a8d54 BPO
faa8d0a45b67b85f BCC
fb0b7fc7a1d769db BPT
fb151d7b6595a053 BUO
fb40cdf76acda951 BPT
fb4481b25160f4f5 BPT
fb4b6089b2c7d283 BUO
fb90d758104291c5 BUC
fba046a9e0efd5f9 BPT
fbf69a9197e9de1e BUC
fc10baa7b86ef66e BCO
fc9d6d31076ce121 BPP
fccc4214627b4acc BPP
fcd4c807c5679799 BUC
fd4718cf8013e063 BUC
fd4f4f8df4f90350 BUC
fd573ca5b0bc9e38 BUP
fd8e3b4bf4962d85 BUO
fdaa7aa68dc8f901 BPO
fdcd2bcfb75820bb BPP
fe12069ae6b306f8 BUO
fe223f68ceb94b0b BUO
fe3a0fbe8aaa65a2 BPP
fe77ccf45795c2c1 BUO
febd11c49dc4a5b2 BUO
ff01dced066c25c0 BCT
ff247aa52a1194ee A-P
ff4530276088b626 BCT
ff5353a4e7da06bd BUO
ff550b9a1b229fc7 BUO
ff60bf8485bc5360 A-P
ff74b3022a3004f3 BPO
ff7b40614990973c BPO
ff8e3676d2a76843 BUP
ffbb97f3ab165534 BPO
ffc44e86ed03c91b BPT
fffa57d96cb1b2af BPP
"""

_CATALOGUE = None

_LAYER_CODES = {"B": "BASE", "A": "ADDITIVE"}
_STANCE_CODES = {"U": "UPRIGHT", "C": "CROUCH", "P": "PRONE"}
_SHAPE_CODES = {"C": "CYCLE", "T": "TRANSITION", "O": "ONESHOT", "P": "POSE"}


def catalogue():
    """hash -> (facet code, name), parsed once and cached."""
    global _CATALOGUE
    if _CATALOGUE is None:
        table = {}
        for line in _CATALOGUE_RAW.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, rest = line.partition(" ")
            code, _, name = rest.partition(" ")
            table[key] = (code, name.strip())
        _CATALOGUE = table
    return _CATALOGUE


def _catalogued(action):
    """This clip's catalogue row, or None.

    Looked up by the name the clip was IMPORTED under, so a clip that has
    already been renamed still resolves, and Blender's ".001" suffixes on
    duplicate imports resolve to the same entry.
    """
    imported = original_name(action) or action.name
    key = re.sub(r"\.\d+$", "", imported).strip().lower()
    if key.startswith("0x"):
        key = key[2:]
    try:
        key = "%016x" % int(key, 16)
    except ValueError:
        return None
    return catalogue().get(key)


def known_name_for(action):
    """The catalogued name for this clip, or None if it only has facets."""
    row = _catalogued(action)
    return (row[1] or None) if row else None


# ----------------------------------------------------------------------------
# Clip facets
# ----------------------------------------------------------------------------
#
# Layer, stance and shape say what a clip IS, independently of what it is
# called, and they compose: Base + Prone + Cycle is the crawl cycles. How each
# one is arrived at is written up with the catalogue above.
#
# Nothing is measured at runtime. Working the three facets out means reading
# every curve of every clip, which takes the better part of a minute on a full
# library and lands on the same answer every time, because the curves are the
# same curves for everyone who imports them. So it was done once and the
# results are in the table.
#
# A .blend saved by an earlier version may carry its own measurements as
# custom properties, and those still win: they were computed from the curves
# in that file, while the table is the same answer computed somewhere else.

_LOCO_VER = 2                       # version tag on any stored measurements
LOCO_VER_PROP = "loco_ver"
LAYER_PROP = "clip_layer"           # BASE / ADDITIVE
STANCE_PROP = "clip_stance"         # PRONE / CROUCH / UPRIGHT (Base only)
SHAPE_PROP = "clip_shape"           # POSE / CYCLE / TRANSITION / ONESHOT
# Written by the state-machine import, read here for Source.
SM_STATE_PROP = "sm_state"
SM_TRIGGER_PROP = "sm_trigger"


def _facet(action, prop, slot, codes):
    """A facet, from this file's own measurements if it has any, else baked in.

    A stale _LOCO_VER reads as unmeasured rather than as a wrong answer, so
    the tag can be bumped to retire old results without touching them.
    """
    if action.get(LOCO_VER_PROP) == _LOCO_VER:
        stored = action.get(prop)
        if stored:
            return stored
    row = _catalogued(action)
    if not row or len(row[0]) < 3:
        return None
    return codes.get(row[0][slot])


def clip_layer(action):
    return _facet(action, LAYER_PROP, 0, _LAYER_CODES)


def clip_stance(action):
    return _facet(action, STANCE_PROP, 1, _STANCE_CODES)


def clip_shape(action):
    return _facet(action, SHAPE_PROP, 2, _SHAPE_CODES)


def clip_source(action):
    """How the clip's name was come by."""
    if action.get(SM_STATE_PROP):
        return "STATE"
    if action.get(SM_TRIGGER_PROP):
        return "TRIGGER"
    return "MEASURED"


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
    if scene.anim_browser_filter_length:
        # Same rounding as the "N f" label on the row, so the numbers you type
        # are the numbers you see in the list.
        start, end = clip_span(action)
        frames = int(round(end - start))
        if not (scene.anim_browser_min_frames <= frames
                <= scene.anim_browser_max_frames):
            return False
    # A clip the catalogue does not cover reads as hidden rather than shown:
    # a facet filter names what it wants, and an unknown clip is not known to
    # be it.
    if scene.anim_browser_layer != "ANY":
        if clip_layer(action) != scene.anim_browser_layer:
            return False
    if scene.anim_browser_stance != "ANY":
        if clip_stance(action) != scene.anim_browser_stance:
            return False
    if scene.anim_browser_shape != "ANY":
        if clip_shape(action) != scene.anim_browser_shape:
            return False
    if scene.anim_browser_source != "ANY":
        if clip_source(action) != scene.anim_browser_source:
            return False
    if scene.anim_browser_unlabelled_only and is_labelled(action):
        return False
    if scene.anim_browser_queued_only and not is_queued(action):
        return False
    return True


def _on_min_frames_change(self, context):
    # Drag Min past Max and Max follows, rather than the list going empty.
    if self.anim_browser_max_frames < self.anim_browser_min_frames:
        self.anim_browser_max_frames = self.anim_browser_min_frames


def _on_max_frames_change(self, context):
    if self.anim_browser_min_frames > self.anim_browser_max_frames:
        self.anim_browser_min_frames = self.anim_browser_max_frames


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


class ANIM_OT_browser_apply_names(bpy.types.Operator):
    bl_idname = "anim.browser_apply_names"
    bl_label = "Apply Known Names"
    bl_description = ("Name every clip this add-on recognises, from the "
                      "bundled catalogue. Clips you have already named are "
                      "left alone, and any rename can be undone with Restore "
                      "Imported Name")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        pending = []
        already = 0
        for action in bpy.data.actions:
            # is_labelled means somebody has already renamed this clip, and
            # their name outranks the catalogue's.
            if is_labelled(action):
                already += 1
                continue
            name = known_name_for(action)
            if name:
                pending.append((action, name))

        # Renamed one at a time, with no temporary pass: rename_action records
        # whatever the clip is called at the moment it runs, so a placeholder
        # name would be recorded as the imported one and Restore would bring
        # the placeholder back instead of the hash.
        renamed = 0
        for action, name in pending:
            if rename_action(action, name):
                renamed += 1

        if not renamed:
            self.report({"INFO"},
                        "Nothing to name - %d clips already named, and the "
                        "rest are not in the catalogue" % already)
            return {"CANCELLED"}
        self.report({"INFO"},
                    "Named %d clips (%d already named, catalogue holds %d)"
                    % (renamed, already, len(catalogue())))
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
        row.operator(ANIM_OT_browser_apply_names.bl_idname, icon="SORTALPHA")

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
        row.prop(scene, "anim_browser_filter_length", toggle=True,
                 icon="TIME")
        sub = row.row(align=True)
        sub.active = scene.anim_browser_filter_length
        sub.prop(scene, "anim_browser_min_frames", text="Min")
        sub.prop(scene, "anim_browser_max_frames", text="Max")
        col = layout.column(align=True)
        col.prop(scene, "anim_browser_layer", text="Layer")
        # Stance is stored on Base clips only, so offering it while Layer is
        # pinned to Additive would just empty the list.
        sub = col.row(align=True)
        sub.active = scene.anim_browser_layer != "ADDITIVE"
        sub.prop(scene, "anim_browser_stance", text="Stance")
        col.prop(scene, "anim_browser_shape", text="Shape")
        col.prop(scene, "anim_browser_source", text="Source")
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


# ===========================================================================
# RIG PROFILES
#
# One profile per (source skeleton, control rig) pair. Everything that knows
# a bone name lives in this section: the map itself, which controls take
# location as well as rotation, the IK/FK sliders the bind has to force to
# FK, and the bones the Align step measures from. Nothing below it names a
# bone, so supporting another creature means adding an entry here and
# nothing else.
#
# The profile in use is chosen in the Retarget to Rig panel, and the Detect
# button next to it guesses by counting how many of each profile's pairs
# actually exist on the two armatures in the fields.
# ===========================================================================


def _helldiver_pairs():
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


RIG_PROFILES = {
    "HELLDIVER": {
        "label": "Helldiver",
        "info": "Helldiver Cast skeleton onto the LexDorkalv control rig",
        "pairs": _helldiver_pairs(),
        # Only these follow the source in world space; everything else is
        # rotation only. Location on an FK bone would fight the rig's own
        # hierarchy.
        "loc": ({"root", "torso"}
                | set(s + "_foot_ik" for s in _SIDES)
                | set(s + "_hand_ik" for s in _SIDES)),
        # Bones whose IK/FK slider must sit at FK (1.0), because the mapping
        # drives the _fk chain.
        "ikfk": ("l_thigh_parent", "r_thigh_parent",
                 "l_shoulder_parent", "r_shoulder_parent"),
        # Align measurements: (source bones, target bones tried in order).
        # The first candidate that exists in full on the rig wins, so a rig
        # that kept its ORG- bones and one that did not both work.
        "hips": (("l_thigh", "r_thigh"),
                 (("ORG-l_thigh", "ORG-r_thigh"), ("l_thigh", "r_thigh"))),
        "height": (("r_foot", "head"),
                   (("ORG-r_foot", "ORG-head"), ("r_foot", "head"))),
        "anchor": (("hips",), (("ORG-hips",), ("hips",))),
    },
}

_PROFILE_ORDER = ("HELLDIVER",)
DEFAULT_PROFILE = "HELLDIVER"


def rig_profile(ident=None):
    """A profile by id, falling back to the default rather than raising -
    a .blend saved with a profile that a later version dropped still opens."""
    return RIG_PROFILES.get(ident or "", RIG_PROFILES[DEFAULT_PROFILE])


def active_profile(context=None):
    sc = (context or bpy.context).scene
    return rig_profile(getattr(sc, "anim_retarget_profile", DEFAULT_PROFILE))


def profile_items():
    """Enum items for the panel's Profile selector.

    Static, not a callback: the registry is fixed at import time, and only
    static items can carry a default - a dynamic enum would land on
    whichever profile happens to be first."""
    return [(pid, RIG_PROFILES[pid]["label"], RIG_PROFILES[pid]["info"])
            for pid in _PROFILE_ORDER]


def score_profiles(src, trg):
    """(profile id, pairs matched) for every profile, best match first."""
    ranked = [(pid, len(_ret_pairs_for(src, trg, RIG_PROFILES[pid])))
              for pid in _PROFILE_ORDER]
    return sorted(ranked, key=lambda kv: -kv[1])


# 3.0 exposed one flat map at module level and the docstring pointed at it.
# Kept as the default profile's map so anything reading it still works.
RETARGET_PAIRS = RIG_PROFILES[DEFAULT_PROFILE]["pairs"]


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

def _ret_pairs_for(src, trg, prof=None):
    """Mapped pairs that actually exist on both armatures.

    prof defaults to the profile selected in the panel; score_profiles()
    passes each profile in turn to work out which one fits."""
    if not src or not trg or src.type != "ARMATURE" or trg.type != "ARMATURE":
        return []
    if prof is None:
        prof = active_profile()
    sb, tb = src.data.bones, trg.data.bones
    return [(t, s) for t, s in prof["pairs"].items() if t in tb and s in sb]


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
    prof_id = getattr(sc, "anim_retarget_profile", DEFAULT_PROFILE)
    prof = rig_profile(prof_id)
    pairs = _ret_pairs_for(src, trg, prof)
    # The best-fitting profile, so the panel can say "the profile you picked
    # is not the one that fits these two". Silent unless it beats the current
    # pick: an equal score is not evidence of anything.
    ranked = score_profiles(src, trg) if (src and trg) else []
    better = ranked[0] if ranked and ranked[0][1] > len(pairs) else None
    return {
        "src": src,
        "trg": trg,
        "prof": prof,
        "profile": prof_id,
        "better": better[0] if better else "",
        "better_pairs": better[1] if better else 0,
        "ranked": ranked,
        "pairs": len(pairs),
        "bound": len(_ret_constraints(trg, src)),
        "proxies": len(_ret_proxies(src)),
        "baked": _ret_baked_action(trg),
        "ready": bool(src and trg and src is not trg and pairs),
        "missing_trg": sorted(t for t, s in prof["pairs"].items()
                              if trg and s in src.data.bones
                              and t not in trg.data.bones) if (src and trg) else [],
        "missing_src": sorted(s for t, s in prof["pairs"].items()
                              if src and t in trg.data.bones
                              and s not in src.data.bones) if (src and trg) else [],
        "src_clip_fits": _ret_action_fits(
            src, src.animation_data.action
            if src and src.animation_data else None),
        "bakes": len(_ret_bakes(trg)) if trg else 0,
    }


def _first_candidate(ob, cands):
    """The first candidate name tuple whose bones all exist on ob.

    Profiles list the rig's measuring bones most-specific first, so a rig
    generated with its ORG- bones kept and one generated without them are
    both measurable from the same entry."""
    if ob is None or ob.type != "ARMATURE":
        return None
    for names in cands:
        if all(n in ob.data.bones for n in names):
            return names
    return None


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

def _ret_do_bind(context, src, trg, pairs, prof=None):
    """Create proxy bones on src and constrain trg's controls to them."""
    if prof is None:
        prof = active_profile(context)
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
                     if t_name in prof["loc"] else ("COPY_ROTATION",)):
            c = pb.constraints.new(kind)
            c.name = kind.title().replace("_", " ") + " [retarget]"
            c.target = src
            c.subtarget = name
            n += 1

    # The mapping drives the FK chain, so the IK solvers must stand down.
    for bn in prof["ikfk"]:
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

class ANIM_OT_retarget_detect(bpy.types.Operator):
    """Work out which rig profile fits the two armatures in the fields.

Every profile's bone map is counted against both skeletons and the one
with the most pairs present on both wins. It is a count, not a guess about
anatomy, so it only ever picks a profile that would actually bind"""
    bl_idname = "anim.retarget_detect"
    bl_label = "Detect Rig Profile"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        sc = context.scene
        return bool(sc.anim_retarget_source and sc.anim_retarget_target)

    def execute(self, context):
        sc = context.scene
        src, trg = sc.anim_retarget_source, sc.anim_retarget_target
        ranked = score_profiles(src, trg)
        best, n = ranked[0]
        if not n:
            self.report({"ERROR"},
                        "No profile matches these two armatures - '%s' and "
                        "'%s' share no mapped bones with any of them"
                        % (src.name, trg.name))
            return {"CANCELLED"}
        was = sc.anim_retarget_profile
        sc.anim_retarget_profile = best
        runners = ", ".join("%s %d" % (RIG_PROFILES[p]["label"], c)
                            for p, c in ranked[1:] if c)
        message = "Profile: %s (%d pairs)" % (RIG_PROFILES[best]["label"], n)
        if runners:
            message += " - next best %s" % runners
        if was == best:
            message += " - already selected"
        self.report({"INFO"}, message)
        return {"FINISHED"}


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
    do_move: BoolProperty(
        name="Match Position",
        description="Move the source so the profile's anchor bone sits on "
                    "the rig's",
        default=True,
    )

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
        prof = st["prof"]
        done = []

        # --- facing -------------------------------------------------------
        # Compare the hip axis of each skeleton and yaw the source onto it.
        # This is what catches the 180 that game rigs so often arrive with.
        if self.do_rotate:
            s_pair, t_cands = prof["hips"]
            s_ax = self._hip_axis(src, *s_pair)
            t_names = _first_candidate(trg, t_cands)
            t_ax = self._hip_axis(trg, *t_names) if t_names else None
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
                            "No %s bones to measure facing from"
                            % (" / ".join(s_pair)))

        # --- height -------------------------------------------------------
        if self.do_scale:
            s_pair, t_cands = prof["height"]
            s_h = _ret_height(src, *s_pair)
            t_names = _first_candidate(trg, t_cands)
            t_h = _ret_height(trg, *t_names) if t_names else None
            if not s_h or not t_h or abs(s_h) < 1e-9:
                self.report({"ERROR"}, "Could not measure one of the skeletons")
                return {"CANCELLED"}
            k = t_h / s_h
            src.scale = [v * k for v in src.scale]
            context.view_layer.update()
            done.append("scaled %.4f" % k)

        # --- position (last: rotation and scale both move the hips) -------
        if self.do_move:
            s_pair, t_cands = prof["anchor"]
            s_anchor = _first_candidate(src, (s_pair,))
            t_anchor = _first_candidate(trg, t_cands)
            if s_anchor and t_anchor:
                want = trg.matrix_world @ trg.data.bones[t_anchor[0]].head_local
                have = src.matrix_world @ src.data.bones[s_anchor[0]].head_local
                src.location = src.location + (want - have)
                context.view_layer.update()
                done.append("%s matched" % s_anchor[0])

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
        pairs = _ret_pairs_for(src, trg, st["prof"])
        n, made = _ret_do_bind(context, src, trg, pairs, st["prof"])
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

        # Above the early returns on purpose: picking the wrong profile is
        # exactly what makes "no bones matched" happen, so the selector has
        # to be reachable from that state.
        row = layout.row(align=True)
        row.prop(sc, "anim_retarget_profile", text="Profile")
        row.operator("anim.retarget_detect", text="", icon="VIEWZOOM")

        if not st["src"] or not st["trg"]:
            layout.label(text="Pick a source clip rig and a target rig",
                         icon="INFO")
            return
        if st["src"] is st["trg"]:
            layout.label(text="Source and rig are the same object",
                         icon="ERROR")
            return
        if not st["pairs"]:
            col = layout.column(align=True)
            col.label(text="No bones matched between these two",
                      icon="ERROR")
            if st["better"]:
                col.label(text="Try the %s profile - %d pairs"
                          % (RIG_PROFILES[st["better"]]["label"],
                             st["better_pairs"]), icon="VIEWZOOM")
            else:
                col.label(text="No profile fits this pair of skeletons")
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
        # A profile that fits better is the usual reason for a low count.
        if st["better"]:
            sub = box.row()
            sub.alert = True
            sub.label(text="%s profile matches %d - wrong profile?"
                      % (RIG_PROFILES[st["better"]]["label"],
                         st["better_pairs"]), icon="VIEWZOOM")
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

        prof = st["prof"]
        pairs = _ret_pairs_for(src, trg, prof)
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
                n, _made = _ret_do_bind(context, src, trg, pairs, prof)
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
    ANIM_OT_browser_apply_names,
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
    ANIM_OT_retarget_detect,
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
    "anim_browser_filter_length",
    "anim_browser_layer",
    "anim_browser_stance",
    "anim_browser_shape",
    "anim_browser_source",
    "anim_browser_filter_loco",   # retired in 3.2
    "anim_browser_loco_min",      # retired in 3.2
    "anim_browser_posture",       # retired in 3.2
    "anim_browser_min_frames",
    "anim_browser_max_frames",
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
    "anim_retarget_profile",
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
    bpy.types.Scene.anim_browser_filter_length = BoolProperty(
        name="Length",
        description="Show only clips whose length, in frames, falls between "
                    "Min and Max. Set both to the same number for an exact "
                    "match",
        default=False,
    )
    bpy.types.Scene.anim_browser_min_frames = IntProperty(
        name="Min Frames",
        description="Shortest clip length to show, as displayed in the list",
        default=0, min=0, update=_on_min_frames_change,
    )
    bpy.types.Scene.anim_browser_max_frames = IntProperty(
        name="Max Frames",
        description="Longest clip length to show, as displayed in the list",
        default=1000, min=0, update=_on_max_frames_change,
    )
    bpy.types.Scene.anim_browser_layer = EnumProperty(
        name="Layer",
        description="Show only clips that combine this way",
        items=[
            ("ANY", "Any Layer", "Do not filter on layer"),
            ("BASE", "Base", "Full-body poses that stand on their own"),
            ("ADDITIVE", "Additive",
             "Deltas blended on top of a base pose - recoil, flinch and the "
             "per-gait layers. Alone they look like a twitching T-pose"),
        ],
        default="ANY",
    )
    bpy.types.Scene.anim_browser_stance = EnumProperty(
        name="Stance",
        description=("Show only clips performed at this height. Measured on "
                     "Base clips only"),
        items=[
            ("ANY", "Any Stance", "Do not filter on stance"),
            ("UPRIGHT", "Upright", "Standing, walking and running height"),
            ("CROUCH", "Crouch", "Crouched and kneeling height"),
            ("PRONE", "Prone", "Crawling and belly-down height"),
        ],
        default="ANY",
    )
    bpy.types.Scene.anim_browser_shape = EnumProperty(
        name="Shape",
        description="Show only clips that use time this way",
        items=[
            ("ANY", "Any Shape", "Do not filter on shape"),
            ("CYCLE", "Cycle", "Repeating gait - walks, runs, crawls"),
            ("TRANSITION", "Transition",
             "Ends in a different stance than it started - getting up, going "
             "prone, dying"),
            ("ONESHOT", "One-shot",
             "Plays through once without changing stance"),
            ("POSE", "Pose", "A single frame"),
        ],
        default="ANY",
    )
    bpy.types.Scene.anim_browser_source = EnumProperty(
        name="Source",
        description="Show only clips whose name came from here",
        items=[
            ("ANY", "Any Source", "Do not filter on source"),
            ("STATE", "Named State",
             "The state-machine dump named this clip outright"),
            ("TRIGGER", "Trigger Only",
             "Only the transitions leading into its state are named"),
            ("MEASURED", "Measured",
             "No state-machine evidence - geometry is all we have"),
        ],
        default="ANY",
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
    bpy.types.Scene.anim_retarget_profile = EnumProperty(
        name="Profile",
        description="Which bone map to retarget through - one per pair of "
                    "source skeleton and control rig. Use Detect if you are "
                    "not sure which fits the two armatures above",
        items=profile_items(),
        default=DEFAULT_PROFILE,
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

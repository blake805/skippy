# 0015 — Note pack identity: keyed by path, fingerprinted by content

Status: accepted
Date: 2026-07-30
Amends [ADR 0012](0012-reverse-engineering-mode.md). Shares its reasoning about marking
stale records with [ADR 0013](0013-project-memory.md).

## Context

ADR 0012 identified a pack by the slugified target: `firmware.bin` became the pack
`firmware-bin`. That works for exactly as long as no two artifacts share a basename.

We harden our own products, and our own products all ship a file called `firmware.bin`.
Two investigations of two different devices therefore landed in one pack. The failure is
not that findings mix — it is that the second session **opens the first one's findings as
its context**. ADR 0012 made the pack announce itself in the opening message precisely so
the model would start from what was already known; that mechanism, pointed at the wrong
pack, hands a session confident prior knowledge about a different product. Nothing looks
wrong from the inside. There is no error, no empty result, no missing file — just an
investigation that begins by believing things about the artifact in front of it that were
established about another one.

A second problem was latent in the same place, and only visible once packs live longer
than a single session. A pack accumulates across sessions, which is the point. But an
image gets rebuilt. Findings recorded against last month's bytes — a payload offset, a key
table, a routine at an address — are then presented as current knowledge of a file that no
longer contains them.

## Decision

**Identity comes from the resolved path; staleness comes from the bytes.** These are two
different questions and conflating them gets both wrong.

The pack id is the slugified basename plus a short digest of the resolved absolute path:
`firmware-bin-a3f81c2e`. The basename stays on the front because a directory of packs has
to be navigable by eye — `ls` on the notes root should still tell a person what was looked
at. The path digest is what makes two `firmware.bin` files two investigations. Symlinks
and `..` are resolved first, so the same file reached two ways is one pack.

Keyed by path and **not** by content, deliberately. A rebuilt image is the same
investigation and should accumulate; content-keying would start a fresh empty pack on
every build and discard the history exactly when it became most useful.

That leaves the rebuild itself to report, so `pack.json` records a digest of the target's
bytes when the pack first names its target, and every later open compares against it. A
mismatch does not split, lock or discard the pack. It sets a flag that produces a warning
at the top of the opening message and on **every** read path — the same treatment ADR 0012
gives superseded findings and ADR 0013 gives a decision whose files have moved, for the
same reason each time: an unmarked wrong answer is worse than a missing one. The findings
stay readable, because most of them are still true, and which ones are is a judgment for
whoever reads them rather than something a digest can decide.

Targets above 256 MB are fingerprinted by sampling the head, tail and length rather than
read end to end. A pack is opened before the first step of every session and hashing a
multi-gigabyte flash dump is latency charged to every run. The method is recorded next to
the digest so two digests are only ever compared when they were computed the same way; a
target that crosses the threshold between sessions re-baselines rather than reporting a
change it cannot actually attest to.

### Existing packs

There is no migration. Pre-existing packs keep their un-digested ids and keep working;
they simply have no target digest to compare, and report no change. Renaming them would
be a migration written for one deployment with three packs in it, and the id is not
referenced from anywhere outside the notes root.

## Consequences

Two products that both ship `firmware.bin` get two packs. Verified by opening packs for
identically named files in different directories and asserting the ids differ and neither
sees the other's findings, and by the inverse: the same target through a symlink resolves
to one pack.

Moving an artifact starts a new pack, because the path is the identity. This is the same
trade ADR 0013 accepted for project ids keyed to workspace-root basenames, and it is the
right way round: a new empty pack is obvious, while silently appending to the wrong one is
not.

The pack id is no longer something a person types from memory. It was already not a public
identifier — `open_pack()` takes the target path, not the id — and `list_packs()` reports
the human-readable target beside the id.

### Not addressed

The digest tells you the bytes changed, not what changed or which findings are affected.
Diffing two versions of an image and marking individual findings as still-holding or
now-wrong is real work and the wrong shape for a first pass; the warning names the problem
and leaves the judgment where it belongs.

A target that is absent or unreadable at open time is recorded as having no digest rather
than as changed. Warning about a change that may not have happened trains the reader to
ignore the warning.

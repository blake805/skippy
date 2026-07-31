# 0019 — Extraction runs in a container, because on macOS that is the VM boundary
Status: accepted
Date: 2026-07-31
Answers the question [ADR 0012](0012-reverse-engineering-mode.md) left open about where
untrusted extraction belongs. Follows [ADR 0018](0018-rizin-structured-tools.md), which
established the shape of a tool that reaches an external program the allowlist cannot
safely describe.

## Context

Every other operation in the RE lane reads. Extraction is the one that points a stack of
format parsers at a hostile blob and asks them to write files, and it is the capability
ADR 0012's own README example claimed and did not have.

ADR 0012 named a VM as the only real containment and deferred it. The question this ADR
had to answer was whether unblob's path-safety guarantees are strong enough that host-side
extraction is acceptable anyway. The answer turned out to be platform-specific, and on
this platform it is not close.

### unblob's defences are good, and they cover the wrong half

They are better than the field. There is a `FileSystem` API that confines extractor writes
to the extraction directory and rewrites symlinks that point outside it; merge requests
adding an extractor that bypasses it are refused. There is no auto-loading plugin
directory — plugins require an explicit `--plugins-path` — which is exactly the property
whose absence turns binwalk 2.x's unfixed path traversal into remote code execution.
unblob is fuzz-tested against a firmware corpus, and its dependencies are pinned.

None of that covers the third-party extractors. unblob drives about twenty external
binaries — `7z`, `sasquatch`, `jefferson`, `ubireader_extract_files`, `debugfs`, `unar`,
`simg2img` and the rest. They are separate processes, so the `FileSystem` API cannot reach
them: it governs code that calls it. And that population is precisely where the bugs are.
unblob's own site lists path traversals it had to find and fix in `ubi_reader` and
`jefferson`, plus an integer overflow in Yara. Those are the ones that have been found.

The layer that does cover the subprocesses is Landlock, and unblob uses it well: a thread
takes away its own filesystem privileges, and every subprocess spawned from it inherits
the restriction. Read `/`, write only the extraction directory, the log and the report.
The blast radius of an extractor going haywire really is small.

**Landlock is a Linux kernel API.** On macOS `restrict_access` raises and unblob logs
`Sandboxing FS access is unavailable on this system, skipping`. The strongest layer is
absent, silently, on the platform this runs on, and what remains is the layer that does
not cover the risky processes.

### The extractors are not packaged for macOS either

Installation instructions are Ubuntu/Debian, Kali and Nix. The documented route on macOS
*is* the container image. So host-side extraction on this machine would not merely be less
safe, it would be missing most of the formats — which for a firmware tool is most of the
point.

## Decision

**Extraction runs in a container, never on the host.** unblob from the official image,
pinned by digest, with the boundary tightened well past the documented invocation.

The reasoning that makes this easy rather than a compromise: **on macOS, Podman runs a
Linux VM.** Containerising therefore buys three things at once, all of which host-side
extraction gives up.

1. A VM between hostile parsers and the host — the containment ADR 0012 identified as the
   only real one, obtained as a side effect rather than as a project.
2. Landlock switched back on, because there is now a Linux kernel under unblob. The layer
   that covers third-party extractors starts working.
3. The full extractor set, which is not installable on macOS.

**Podman rather than Docker**, preferred and not required. Rootless and daemonless, so
there is no root daemon on the host forming part of the attack surface, and it is
Apache-2.0 with no company-size licensing question. Docker and nerdctl are accepted when
present, because refusing a working runtime to make a point would mean extraction never
happens. The `podman-mac-helper` that installs a privileged Docker-socket shim is
deliberately not installed: nothing here needs a Docker socket.

**Pinned by digest, not `:latest`.** unblob's docs recommend `--pull always` because the
project moves fast. That is right for a person at a terminal and wrong for a tool whose
output is cited as evidence: a finding resting on an extraction should be reproducible, and
`:latest` means the image that produced it is already gone. Updating the digest is a
deliberate act with a diff. `--pull missing` so a pinned digest is fetched once.

### The flags are the containment, so they are tested one by one

`--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only` with
tmpfs for `/tmp` and `/run`, input mounted `:ro`, exactly two bind mounts, memory and swap
both capped, `--pids-limit`, `--cpus`, and worker count matched to the CPU cap because
unblob otherwise starts one worker per host core inside a two-CPU container.

These are a dozen strings in a list. Any of them can be dropped in a refactor with no test
failing and no symptom until the day it matters, which is why each has its own assertion
naming what it prevents — including the absences: no `--privileged`, no `--cap-add`, no
host namespaces, and no Docker socket, since mounting it is the standard way a container
boundary is escaped in practice.

### The likelier hazard is not a code-execution bug

It is a 4 KB file that expands to 200 GB, or an archive that contains itself. A
decompression bomb needs no vulnerability at all, just a compression ratio, and it is a
plausible accident in our own build output as well as a plausible attack.

Recursion depth defaults to 4 rather than unblob's 10, and clamps. Total output size is
capped at 4 GB by a **watchdog**, because no container flag bounds writes to a bind mount:
a task measures the output tree every two seconds and kills the container if it passes the
cap. Partial output is kept, and the summary says the run was stopped — a fragment of a
filesystem presented as a whole one is worse than an obvious failure.

### Output is evidence, and belongs to the pack

Extracted files land in `<pack>/quarantine/NNNN-<name>/`, never a workspace root, which is
somewhere Skippy writes code. Each extraction gets its own numbered directory, so a second
carve cannot overwrite the first and the numbering records the order.

Two consequences worth stating. The quarantine outlives the session, so a later run sees
what an earlier one carved instead of extracting the same image twice — the same reasoning
that keyed packs by target in [ADR 0015](0015-note-pack-identity.md). And extraction
problems are surfaced to the model rather than logged: an image that attempts a path
traversal during extraction has said something about itself, and it is very likely a
weakness in whatever built it, which is a finding under
[ADR 0017](0017-weakness-findings-and-handoff.md).

### The chain continues past the container

Carving a firmware image to find `httpd` inside it and then being unable to read it would
leave the interesting part unreachable. So the reading tools from ADR 0018 take a `file`
argument naming something in the quarantine. That is the one place in this lane where the
model chooses a path, so it is resolved and then checked to be inside the quarantine
**after** symlinks are followed — extraction produces symlinks, and one pointing at `/etc`
is something unblob reports rather than something it can always prevent.

## What this does not claim

**The container is not a boundary against a kernel exploit.** It is a boundary against
memory-safety bugs in userspace parsers, which is the population the evidence points at. On
macOS the VM helps here as well, since the kernel being attacked is the guest's.

**The live container behaviour is verified by hand, not by CI.** CI has no VM, so the tests
assert the invocation and the parsing rather than the extraction. That split is deliberate
— the flags are what can silently regress — but it means "unblob runs correctly inside
these restrictions" rests on a manual check, and `--read-only` is the flag most likely to
need adjusting if some extractor writes somewhere unexpected.

**Nothing here bounds inode count.** A bomb that produces ten million empty files passes a
size cap. The depth limit and the timeout are the current answers, and neither is a real
one.

## Alternatives

**Host-side unblob with its FileSystem API.** The option this ADR exists to reject, argued
above: on macOS the layer covering third-party extractors is absent, and most extractors
are not installable anyway.

**binwalk v3.** The Rust rewrite, kept as a possible fallback for formats unblob lacks
rather than a primary. Never binwalk 2.x under any circumstances: CVE-2026-7179 is an
unfixed path traversal the maintainer declined to address as end-of-life, and because 2.x
auto-loads plugins from `~/.config/binwalk/plugins/`, one traversal write becomes code
execution on the next run. That chain — a write anywhere in a config directory becoming
execution — is the same one `-N` exists to close for rizin in ADR 0018, and it is worth
noticing that the two most useful tools in this slice both had it.

**A full VM we manage.** What ADR 0012 imagined. Strictly stronger and much more to own:
an image to build, patch and boot. The container gets the same boundary on macOS for the
cost of one dependency, and a managed VM stays available if dynamic analysis needs one —
which it will, since running a target is the capability this lane still refuses.

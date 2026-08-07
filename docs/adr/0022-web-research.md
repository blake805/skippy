# 0022 — Web research: the network as untrusted input

Status: accepted
Date: 2026-08-06
Amends [0007](0007-model-roles-and-cloud-escalation.md): the "no network in the runtime
path" property that ADR stated as a goal no longer holds, deliberately, and this records
what replaces it. Follows [0012](0012-reverse-engineering-mode.md) (a mode is a prompt, a
toolset and an allowlist) and reuses the evidence model of
[0016](0016-loop-captured-evidence.md). Extended by
[0023](0023-autonomous-research-trigger.md), which is what makes any of this fire without
being asked.

## Context

Skippy has been unable to look anything up. Every factual question was answered from
weights frozen at training time, with no way to tell the difference between a fact that
is stable and one that expired eighteen months ago — and no way for the answer to say
which it was. For a workbench whose subject matter is firmware versions, part numbers,
toolchain releases and vendor documentation, that is not a small gap: it is most of the
questions that actually get asked.

Two shallow web tools were already in the tree (`web_search` over a scraper library,
`read_website` through a reader service, truncating every page at 5000 characters).
Nothing called them. Both were wrong in ways worth naming, because they are the obvious
implementations:

- **Truncation is a silent, biased failure.** Cutting a page at 5000 characters throws
  away the end, and the end of a technical page is where the version table, the
  deprecation note and the date live. Nothing anywhere says the answer was formed from
  the first third of the source.
- **A scraped search engine is not a research backend.** No ranked snippets to triage
  from, no stable contract, and it breaks whenever the scraped page changes.

The real reason this stayed unbuilt, though, was the local-only constraint, and dropping
it is a decision rather than a slip. The value of that constraint was never "no packets
leave the machine" — the point of ADR 0007 was that *the user's code and artifacts* do
not leave without an explicit act. Reading a public page sends a URL, not the workspace.
Those are different risks, and conflating them cost the agent the ability to check
anything.

## Decision

Web access, through two tools behind one session object, with the network treated as an
untrusted input rather than as a trusted service.

### Everything fetched is data, never instructions

A page can contain text addressed to the model, and a model has no way to tell that text
apart from something the user actually asked for. So every observation these tools
produce is fenced and labelled, with the warning adjacent to the content on every call
rather than stated once in a system prompt — twenty steps into a research run, a line at
the top of the transcript is a long way from the page being read now. The fence marker
is stripped out of fetched text on the way in, because a page that could forge the
closing fence could have everything after it read as though it came from us.

That is the mitigation, not the defence. The defence is that research mode is offered no
tool that changes anything: no `apply_patch`, no filesystem, and an empty command table
so `run_command` refuses even if one is ever reached. Nothing a page says can become a
write, a process or a commit. The worst it can achieve is a wasted step and a false
claim — and the claim has to carry a citation.

### Chunks, not truncation

Long pages are split at paragraph boundaries into numbered chunks, and the model is told
how many there are and how to ask for the next. Chunk size is set below the agent loop's
compression threshold on purpose: an observation over that threshold is summarized by a
smaller model on the way in, and a compressed page cannot be quoted or cited, which is
the entire product of a research run.

Boilerplate stripping is stdlib `HTMLParser` — scripts, navigation, cookie banners,
`<main>`/`<article>` preferred when a page marks it. No new dependency and no reader
service in the path: the suite has to run with the network unplugged, and handing every
page we read to a third party to clean would defeat the point of reading it ourselves.

### The backend is an interface; the key is configuration

`SearchBackend` with Tavily behind it, chosen by `SKIPPY_SEARCH_BACKEND`, key in
`SKIPPY_TAVILY_KEY`. Search providers change terms, prices and JSON shapes more often
than this code should change. It also makes the whole capability testable: a scripted
backend and an `httpx.MockTransport` cover every path above the wire with no key and no
network, which is what keeps the offline CI job meaningful.

The provider's own synthesized answer is deliberately not used. Taking it would mean
reporting a conclusion we did not reach from sources we did not read.

### Fetches are vetted before they happen

http/https only, and by default no loopback, private or link-local addresses. The model
chooses these URLs, and the most interesting things reachable from this machine are the
model servers on loopback and the bench node on the shop network. A hostname that
resolves to a private address still gets through — closing that means resolving and
connecting by IP, which httpx does not expose without reimplementing its transport — and
that limitation is stated in the module rather than implied, the same way ADR 0008 states
what the path sandbox does not defend against.

### A research run is a third mode, and its brief is the deliverable

Same think-tool-observe-finish loop, so the transcript contract, repeat detection,
folding and cancellation are shared rather than reimplemented. What differs is the
toolset, the prompt, and the record — and the record follows ADR 0012's note pack
closely, because the situation is the same: nothing is written to a repository, so
unless it is recorded it exists only in a transcript that will be folded.

- **Sources are logged by the loop, not by the model**, after every successful fetch,
  with the page's text rather than just its URL. Mechanical for ADR 0016's reason — a
  record the model must remember to write is one a run dying at step nine does not have
  — and for a second reason specific to the web: pages get edited, paywalled and
  deleted, so what the page said on the day is the only durable evidence.
- **A claim must cite a source this run actually read.** `note_claim` refuses anything
  else. This is the constraint that earns the module its place, and it is only possible
  because the sources were logged mechanically. Asked for a citation it cannot find, a
  model writes a plausible URL, and a fabricated citation is indistinguishable from a
  real one at a glance while being much worse than no answer.
- **`confirmed` requires two sources on different hosts.** A vendor blog and three pages
  quoting it are one source wearing four hats. A crude proxy for independence, and
  stated as one.
- **The answer is written by a separate pass** over the claims and citations, not by the
  loop that did the reading. That loop ends holding twenty pages of untrusted text and a
  pull toward whatever it read last. It also means a run that exhausted its steps still
  produces an answer, because the claims are on disk either way.
- **Briefs are keyed by the question and go stale.** The same question next month opens
  the work already done; past thirty days, every read warns first. Same reasoning as the
  target digest in ADR 0015 and the stale-path marks in ADR 0013 — an unmarked
  out-of-date answer is worse than a missing one, and the web moves faster than either.

Answers also land in project memory with their source URLs and the date, ranked above
other matches in recall, so a later session in any mode finds them by subject.

## Consequences

Skippy can be current, and can show where "current" came from. A question about a
toolchain release is answerable, and the answer carries citations a person can check.

The suite still runs offline and still passes with no API key: every path is covered
against fakes, and the autonomous layer in ADR 0023 turns itself off when no backend is
configured. Nothing about a keyless install changes.

Prompt injection is mitigated, not solved. A page cannot reach a destructive tool, but it
can still lie, and a model can still believe it and record a claim citing it. What the
brief gives is the audit trail: the claim names the page, and the page's text as we read
it is on disk beside it.

Two ongoing costs. The search API is a paid dependency with a key to keep alive, and a
research run is slower than an answer from memory — which is why ADR 0023 never lets one
block a conversation. And the boilerplate stripper is a heuristic: it will occasionally
drop a paragraph inside a `<div class="promo-body">` or keep a navigation block that
names itself nothing. Preferring `<main>` limits the damage, and a wrong strip costs a
reread rather than a wrong answer.

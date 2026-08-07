"""The research brief: sources, cited claims, and the refusals that keep them honest.

The brief is the whole product of a research run, so the invariants worth pinning are
the ones that decide whether it can be trusted a year later: that a claim cannot cite a
page this run never read, that 'confirmed' cannot be reached by citing one site twice,
that a page's text is kept rather than just its URL, and that changing our mind leaves
both versions behind.
"""

import os
import time

import pytest

import skippy_brief
import skippy_dispatch
import skippy_re
from skippy_sandbox import Sandbox


@pytest.fixture
def brief(tmp_path):
    return skippy_brief.open_brief(str(tmp_path / "briefs"), question="What is the max feed rate?")


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Sandbox([str(root)])


def log(brief, url, text="The page said a thing.", title="A page"):
    return brief.log_source(url=url, text=text, title=title, final_url=url)


def claim(brief, sources, confidence="likely", **kwargs):
    args = {
        "claim": "The maximum feed rate is 400 inches per minute.",
        "support": "The manual's specifications table gives 400 IPM as the rapid rate.",
        "sources": sources,
        "confidence": confidence,
    }
    args.update(kwargs)
    return skippy_brief.note_claim(brief, **args)


# -- identity ---------------------------------------------------------------

def test_the_same_question_reopens_the_same_brief(tmp_path):
    """Keyed by the question so that asking it again opens the work already done —
    which is what makes the second identical question cheap."""
    root = str(tmp_path / "briefs")
    first = skippy_brief.open_brief(root, question="What is the max feed rate?")
    again = skippy_brief.open_brief(root, question="  what IS the max feed rate?  ")
    assert first.brief_id == again.brief_id


def test_different_questions_get_different_briefs(tmp_path):
    root = str(tmp_path / "briefs")
    one = skippy_brief.open_brief(root, question="What is the max feed rate?")
    two = skippy_brief.open_brief(root, question="What is the spindle power?")
    assert one.brief_id != two.brief_id


def test_reopening_does_not_relabel_an_existing_brief(tmp_path):
    root = str(tmp_path / "briefs")
    first = skippy_brief.open_brief(root, question="What is the max feed rate?")
    again = skippy_brief.open_brief(root, brief_id=first.brief_id, question="Something else")
    assert again.question == "What is the max feed rate?"


def test_an_unsafe_brief_id_is_refused(tmp_path):
    with pytest.raises(skippy_brief.BriefError):
        skippy_brief.open_brief(str(tmp_path), brief_id="../escape")


# -- sources ----------------------------------------------------------------

def test_a_source_keeps_the_text_not_just_the_link(brief):
    """A link is a promise that someone can go and check, and the web breaks that
    promise routinely. What the page said on the day we read it is the evidence."""
    log(brief, "https://widget.example/specs", text="Rapid rate: 400 IPM.")
    stored = brief.sources()[0]
    assert "Rapid rate: 400 IPM." in stored["text"]
    assert stored["front"]["id"] == "S1"
    assert stored["front"]["fetched"]


def test_sources_are_numbered_in_the_order_they_were_read(brief):
    assert log(brief, "https://a.example")["id"] == "S1"
    assert log(brief, "https://b.example")["id"] == "S2"
    assert log(brief, "https://c.example")["id"] == "S3"


def test_reading_a_second_chunk_extends_the_same_source(brief):
    """It is one page and one citation; a second source id for chunk two would let a
    claim reach 'confirmed' by citing one page twice."""
    first = brief.log_source(url="https://long.example/", text="First half.", chunk=1, chunks=2)
    second = brief.log_source(url="https://long.example/", text="Second half.", chunk=2, chunks=2)

    assert first["id"] == second["id"]
    assert len(brief.sources()) == 1
    text = brief.sources()[0]["text"]
    assert "First half." in text and "Second half." in text


def test_a_source_can_be_found_by_id_or_by_url(brief):
    log(brief, "https://widget.example/specs")
    for reference in ("S1", "s1", "1", "https://widget.example/specs"):
        assert brief.source_for(reference) is not None, reference
    assert brief.source_for("https://never.example") is None


def test_the_redirect_target_is_what_gets_cited(brief):
    brief.log_source(url="https://short.example/x", text="Body.",
                     final_url="https://widget.example/specs")
    assert brief.source_for("https://widget.example/specs") is not None
    front = brief.sources()[0]["front"]
    assert front["final_url"] == "https://widget.example/specs"


# -- claims -----------------------------------------------------------------

def test_a_claim_records_its_sources_and_its_confidence(brief):
    log(brief, "https://widget.example/specs")
    result = claim(brief, "S1")

    assert result.ok
    assert result.data["claim"]["sources"] == ["S1"]
    stored = brief.claims()[0]
    assert stored["front"]["confidence"] == "likely"
    assert "400 IPM" in stored["text"]
    # The source is written into the claim itself, so the file stands alone.
    assert "widget.example/specs" in stored["text"]


def test_a_citation_the_run_never_read_is_refused(brief):
    """The refusal that matters most. Asked for citations it cannot find, a model
    writes a plausible URL, and a fabricated citation is indistinguishable from a real
    one at a glance."""
    log(brief, "https://widget.example/specs")
    result = claim(brief, "S1, S7")

    assert not result.ok
    assert "S7" in result.summary
    # It has to say what *was* read, or the model cannot correct itself on the next step.
    assert "widget.example/specs" in result.summary
    assert brief.claims() == []


def test_a_claim_with_no_sources_at_all_is_refused(brief):
    log(brief, "https://widget.example/specs")
    result = claim(brief, "")

    assert not result.ok
    assert "sources" in result.summary
    assert "S1" in result.summary or "id" in result.summary


def test_a_citation_before_anything_was_read_says_so_plainly(brief):
    result = claim(brief, "S1")
    assert not result.ok
    assert "not read any pages" in result.summary


def test_confirmed_needs_two_sites_not_two_pages(brief):
    """A vendor blog plus three pages quoting that blog is one source wearing four
    hats, and 'confirmed' is the word that makes a later reader stop checking."""
    log(brief, "https://widget.example/specs")
    log(brief, "https://widget.example/manual")
    refused = claim(brief, "S1, S2", confidence="confirmed")
    assert not refused.ok
    assert "different sites" in refused.summary

    log(brief, "https://othersite.example/review")
    accepted = claim(brief, "S1, S3", confidence="confirmed")
    assert accepted.ok


def test_www_is_not_a_second_site(brief):
    log(brief, "https://widget.example/specs")
    log(brief, "https://www.widget.example/manual")
    assert not claim(brief, "S1, S2", confidence="confirmed").ok


def test_a_claim_needs_support_and_a_confidence(brief):
    log(brief, "https://widget.example/specs")
    assert not claim(brief, "S1", support="").ok
    assert not claim(brief, "S1", confidence="").ok
    assert not claim(brief, "S1", confidence="pretty sure").ok
    assert not claim(brief, "S1", claim="").ok
    assert brief.claims() == []


def test_the_confidence_vocabulary_is_the_one_from_the_notes_module(brief):
    """One vocabulary across findings and claims. A second copy here would start
    accepting a level the model is never told about, or the reverse."""
    log(brief, "https://widget.example/specs")
    for level in skippy_re.CONFIDENCE:
        sources = "S1"
        if level == "confirmed":
            log(brief, f"https://other{len(brief.sources())}.example/x")
            sources = f"S1, S{len(brief.sources())}"
        assert claim(brief, sources, confidence=level).ok, level


def test_a_duplicate_citation_counts_once(brief):
    log(brief, "https://widget.example/specs")
    result = claim(brief, "S1, S1, 1")
    assert result.ok
    assert result.data["claim"]["sources"] == ["S1"]


def test_a_correction_supersedes_rather_than_overwrites(brief):
    log(brief, "https://widget.example/specs")
    log(brief, "https://othersite.example/review")
    claim(brief, "S1", claim="The feed rate is 300 IPM.")
    corrected = claim(brief, "S2", claim="The feed rate is 400 IPM.", supersedes="C1")

    assert corrected.ok
    assert len(brief.claims()) == 2
    assert brief.superseded_ids() == {"C1"}
    # Both are still on disk: being wrong and then right is the normal shape of this.
    assert "300 IPM" in brief.claims()[0]["text"]


def test_superseding_a_claim_that_does_not_exist_is_refused(brief):
    log(brief, "https://widget.example/specs")
    result = claim(brief, "S1", supersedes="C9")
    assert not result.ok
    assert "C9" in result.summary


def test_a_superseded_claim_is_kept_out_of_the_synthesis(brief):
    """The file keeps the retraction; the answer must not repeat it."""
    log(brief, "https://widget.example/specs")
    log(brief, "https://othersite.example/review")
    claim(brief, "S1", claim="The feed rate is 300 IPM.")
    claim(brief, "S2", claim="The feed rate is 400 IPM.", supersedes="C1")

    block = brief.claims_block()
    assert "400 IPM" in block
    assert "300 IPM" not in block


# -- reading it back --------------------------------------------------------

def test_read_brief_shows_claims_and_sources(brief):
    log(brief, "https://widget.example/specs")
    claim(brief, "S1")

    index = skippy_brief.read_brief(brief)
    assert index.ok
    assert "400 inches per minute" in index.content
    assert "widget.example/specs" in index.content
    assert index.data["claims"] == 1


def test_read_brief_marks_a_superseded_claim_on_the_way_out(brief):
    log(brief, "https://widget.example/specs")
    log(brief, "https://othersite.example/review")
    claim(brief, "S1", claim="The feed rate is 300 IPM.")
    claim(brief, "S2", claim="The feed rate is 400 IPM.", supersedes="C1")

    result = skippy_brief.read_brief(brief, section="claims")
    assert "SUPERSEDED" in result.content


def test_an_unknown_section_lists_the_real_ones(brief):
    result = skippy_brief.read_brief(brief, section="everything")
    assert not result.ok
    assert "sources" in result.summary and "claims" in result.summary


def test_a_previous_answer_comes_back_with_the_brief(brief):
    """So a question asked twice opens with the answer rather than re-deriving it."""
    log(brief, "https://widget.example/specs")
    brief.write_answer("The maximum feed rate is 400 IPM [S1].")

    result = skippy_brief.read_brief(brief)
    assert "Previous answer" in result.content
    assert "400 IPM [S1]" in result.content


# -- staleness --------------------------------------------------------------

def test_an_old_brief_warns_before_it_hands_anything_back(tmp_path):
    """The web moves faster than a repository does, and an unmarked out-of-date answer
    is worse than a missing one."""
    root = str(tmp_path / "briefs")
    first = skippy_brief.open_brief(root, question="What is the current firmware?")
    log(first, "https://widget.example/specs")

    old = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 400 * 86_400)
    )
    source = first.sources()[0]
    with open(source["path"], encoding="utf-8") as handle:
        text = handle.read()
    with open(source["path"], "w", encoding="utf-8") as handle:
        handle.write(text.replace(source["front"]["fetched"], old))

    reopened = skippy_brief.open_brief(root, brief_id=first.brief_id)
    assert reopened.stale
    assert "recheck" in reopened.stale.lower()
    assert reopened.stale in skippy_brief.read_brief(reopened).content


def test_a_fresh_brief_is_not_marked_stale(brief):
    log(brief, "https://widget.example/specs")
    assert not skippy_brief.open_brief(
        os.path.dirname(brief.dir), brief_id=brief.brief_id
    ).stale


# -- durability -------------------------------------------------------------

def test_a_corrupt_meta_file_does_not_lose_the_claims(brief):
    log(brief, "https://widget.example/specs")
    claim(brief, "S1")
    with open(brief.meta_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")

    reopened = skippy_brief.open_brief(os.path.dirname(brief.dir), brief_id=brief.brief_id)
    assert len(reopened.claims()) == 1
    assert len(reopened.sources()) == 1


def test_the_brief_is_listed_with_its_counts(brief):
    log(brief, "https://widget.example/specs")
    claim(brief, "S1")
    brief.write_answer("An answer.")

    listed = skippy_brief.list_briefs(os.path.dirname(brief.dir))
    assert len(listed) == 1
    assert listed[0]["sources"] == 1
    assert listed[0]["claims"] == 1
    assert listed[0]["answered"]


# -- wiring -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_routes_the_brief_tools_to_the_brief(box, brief):
    log(brief, "https://widget.example/specs")
    result = await skippy_dispatch.dispatch(
        "note_claim",
        {
            "claim": "The maximum feed rate is 400 IPM.",
            "support": "The specifications table gives 400 IPM.",
            "sources": "S1",
            "confidence": "likely",
        },
        box,
        brief=brief,
    )
    assert result.ok
    assert len(brief.claims()) == 1


@pytest.mark.asyncio
async def test_a_run_with_no_brief_is_sent_somewhere_useful(box):
    """The same shape as calling note_finding in coding mode: refusing is not enough,
    the message has to say what to do instead."""
    result = await skippy_dispatch.dispatch("note_claim", {"claim": "x"}, box)
    assert not result.ok
    assert "research run" in result.summary
    assert "finish summary" in result.summary


@pytest.mark.asyncio
async def test_the_model_cannot_supply_its_own_brief(box, brief):
    log(brief, "https://widget.example/specs")
    result = await skippy_dispatch.dispatch(
        "read_brief", {"brief": "../somewhere-else"}, box, brief=brief,
    )
    assert result.ok
    assert brief.brief_id in result.summary

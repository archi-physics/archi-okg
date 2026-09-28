"""task.w2.enrichment — anonymizer text->text pass, offline.

Runs with NER disabled (``nlp_model=None``) so no spaCy model is
needed: the regex passes plus ``known_names`` cover the connector
emission hook (jira/docs ``anonymize_data``) deterministically.
"""
import pytest

from archi.enrichment.anonymizer import Anonymizer, redact_email_addresses


def _anonymizer(**kwargs):
    kwargs.setdefault("nlp_model", None)
    return Anonymizer(**kwargs)


def test_redacts_email_name_and_jira_mention():
    an = _anonymizer(known_names=["John Doe"])
    text = (
        "Hi all,\n"
        "John Doe saw transfer failures at T2_US_MIT.\n"
        "Contact jdoe@cern.ch or [~jdoe] about run 381000.\n"
        "Cheers,\n"
        "the ops team\n"
    )
    out = an.anonymize(text)
    assert "jdoe@cern.ch" not in out
    assert "John Doe" not in out
    assert "[~jdoe]" not in out
    # Greeting and sign-off lines are stripped.
    assert "Hi all" not in out
    assert "Cheers" not in out
    # Operational content survives.
    assert "T2_US_MIT" in out
    assert "run 381000" in out


def test_known_names_case_insensitive_word_bounded():
    an = _anonymizer(known_names=["Jane Roe"])
    out = an.anonymize("jane roe and JaneRoeography met.")
    assert "jane roe" not in out.lower().replace("janeroeography", "")
    assert "JaneRoeography" in out  # word boundary respected


def test_markup_pass_strips_author_constructs():
    # Content paragraphs must exceed three words: the ported v2 markup
    # signoff heuristic removes any <p> of up to three capitalized-ish
    # words (case-insensitive), by design.
    an = _anonymizer()
    markup = (
        '<p>Transfer failures were observed at T2_US_MIT overnight</p>'
        '<a class="twikiLink" href="/twiki/bin/view/Main/JohnDoe">JohnDoe</a>'
        '<span class="author">Jane Roe</span>'
        '<p>Please contact jdoe@cern.ch for any further details</p>'
    )
    out = an.anonymize_markup(markup)
    assert "JohnDoe" not in out
    assert "Jane Roe" not in out
    assert "jdoe@cern.ch" not in out
    assert "Transfer failures were observed at T2_US_MIT overnight" in out
    assert "for any further details" in out


def test_construction_never_imports_spacy_eagerly():
    # Default model configured, but nothing loads until first use.
    an = Anonymizer()
    assert an._nlp is None and an._nlp_loaded is False
    # NER-disabled instances never load a model at all.
    off = _anonymizer()
    assert off._nlp is None and off._nlp_loaded is True


# ---------------------------------------------------------------------------
# Leak corpus (circleback adversarial review). Each block pins one
# previously-leaking format plus the neighboring previously-passing
# behavior, so a regression in either direction fails loudly.
# ---------------------------------------------------------------------------


def test_email_local_part_never_leaks_fragments():
    # Finding 1: the RFC-simple default pattern left local-part
    # fragments behind ("john.doe+", "o'").
    an = _anonymizer()
    cases = {
        "Contact john.doe+ops@cern.ch about the transfer.": (
            "john",
            "doe",
            "+ops",
        ),
        "Contact o'brien@cern.ch about the transfer.": ("o'", "brien"),
        "See https://x.test/page?mail=john.doe%40cern.ch for run 381000": (
            "john",
            "doe",
            "%40",
        ),
        'Reach "john doe"@cern.ch if the drain stalls.': ("john", "doe"),
        "Plain jdoe@cern.ch still redacted.": ("jdoe",),
    }
    for text, fragments in cases.items():
        out = an.anonymize(text)
        for fragment in fragments:
            assert fragment not in out, (text, out)
    # Operational context around the address survives.
    out = an.anonymize("Contact john.doe+ops@cern.ch about run 381000.")
    assert "run 381000" in out
    out = an.anonymize("See https://x.test/page?mail=john.doe%40cern.ch now")
    assert "https://x.test/page?mail=" in out


def test_encoded_and_nbsp_variants_redacted():
    # Finding 2: discovery ran on unescaped text but replacement on the
    # raw input, so NBSP/entity-encoded occurrences survived.
    an_names = _anonymizer(known_names=["John Doe"])
    out = an_names.anonymize("Assigned to John\xa0Doe for run 381000.")
    assert "John" not in out and "Doe" not in out
    assert "run 381000" in out

    out = an_names.anonymize_markup(
        "<p>Report prepared by John&nbsp;Doe covering all transfer failures seen</p>"
    )
    assert "John" not in out and "Doe" not in out
    assert "transfer failures" in out

    an = _anonymizer()
    out = an.anonymize_markup(
        "<p>Contact jdoe&#64;cern.ch for details on the failed transfers</p>"
    )
    assert "jdoe" not in out
    assert "failed transfers" in out

    out = an.anonymize("Contact jdoe&#64;cern.ch about run 381000.")
    assert "jdoe" not in out
    out = an.anonymize("Contact jdoe&#x40;cern.ch about run 381000.")
    assert "jdoe" not in out

    # Escaped angle brackets stay escaped: normalization must not
    # create or break markup structure.
    out = an.anonymize_markup(
        "<p>Escaped &lt;tag&gt; text stays escaped in the output here</p>"
    )
    assert "&lt;tag&gt;" in out

    # Double-encoded forms: normalization iterates to a bounded
    # fixpoint, so one &amp;-wrapping layer cannot smuggle PII through.
    out = an.anonymize("Contact jdoe&amp;#64;cern.ch about run 381000.")
    assert "jdoe" not in out
    assert "run 381000" in out
    out = an_names.anonymize_markup(
        "<p>Report prepared by John&amp;nbsp;Doe covering all transfer failures seen</p>"
    )
    assert "John" not in out and "Doe" not in out
    assert "transfer failures" in out


def test_ner_off_author_shapes_redacted():
    # Finding 3: NER-disabled mode leaked authors outside the four
    # hardcoded markup shapes.
    an = _anonymizer()

    # dc:creator without CDATA.
    out = an.anonymize_markup(
        "<item><dc:creator>Hasan Ozturk</dc:creator>"
        "<description>Disk full at T2</description></item>"
    )
    assert "Hasan" not in out and "Ozturk" not in out
    assert "Disk full at T2" in out

    # dc:creator with CDATA still redacted (previously passing).
    out = an.anonymize_markup(
        "<item><dc:creator><![CDATA[Jane Roe]]></dc:creator>"
        "<description>Transfer backlog cleared at the T1 site</description></item>"
    )
    assert "Jane Roe" not in out
    assert "Transfer backlog cleared" in out

    # mailto anchor text.
    out = an.anonymize_markup(
        '<p>Ping <a href="mailto:jdoe@cern.ch">John Doe</a>'
        " when the drain of the pool completes</p>"
    )
    assert "John Doe" not in out and "jdoe" not in out
    assert "drain of the pool completes" in out

    # TWiki signature line, text pass.
    out = an.anonymize("Disk pool drained at T2_US_MIT.\n-- Main.JohnDoe - 2024-01-15")
    assert "JohnDoe" not in out
    assert "T2_US_MIT" in out

    # TWiki signature line, markup pass, non-ISO date.
    out = an.anonymize_markup(
        "Some twiki topic body mentioning run 381000 here\n-- Main.JaneRoe - 15 Jan 2024\n"
    )
    assert "JaneRoe" not in out
    assert "run 381000" in out


def test_operational_lines_survive_greeting_signoff_filters():
    # Finding 4: "^\w+," deleted any line whose first word had a
    # trailing comma; sign-off patterns prefix-matched content lines.
    an = _anonymizer()
    text = (
        "However, run 381000 was affected badly.\n"
        "Note, T2_US_MIT needs a re-run of the workflow.\n"
        "Thank you note was filed as CMSCOMPPR-1.\n"
        "Regards to whoever fixed run 381000.\n"
        "Best effort reprocessing is enabled.\n"
    )
    out = an.anonymize(text)
    assert "However, run 381000 was affected badly." in out
    assert "Note, T2_US_MIT needs a re-run of the workflow." in out
    assert "Thank you note was filed as CMSCOMPPR-1." in out
    assert "Regards to whoever fixed run 381000." in out
    assert "Best effort reprocessing is enabled." in out


def test_greeting_prefixed_operational_lines_survive():
    # Reviewer delta: the expanded greeting word-list must only delete
    # SHORT greeting lines (greeting word + at most four trailing
    # words); greeting-prefixed operational sentences survive.
    an = _anonymizer()
    text = (
        "Good morning update: transfers to T2_US_MIT stuck\n"
        "Hello world example output was attached to run 381000 report\n"
        "Good morning all,\n"
        "Hey folks,\n"
    )
    out = an.anonymize(text)
    assert "Good morning update: transfers to T2_US_MIT stuck" in out
    assert "run 381000 report" in out
    assert "Good morning all" not in out
    assert "Hey folks" not in out


def test_real_greetings_and_signoffs_still_stripped():
    # Finding 4, other direction: the tightened defaults must still
    # strip actual greeting/sign-off lines.
    an = _anonymizer()
    text = (
        "Hi all,\n"
        "Dear colleagues,\n"
        "Good morning team,\n"
        "The transfer failed overnight.\n"
        "Thanks in advance,\n"
        "Best regards, John\n"
        "Cheers,\n"
        "Yours sincerely,\n"
        "-- \n"
    )
    out = an.anonymize(text)
    assert out.strip() == "The transfer failed overnight."


# --- redact_email_addresses: the email-only pass sources call ---------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail john.doe@cern.ch now", "mail  now"),
        ("tagged john.doe+ops@cern.ch", "tagged "),
        ('quoted "john doe"@cern.ch.', "quoted ."),
        ("encoded jdoe&#64;cern.ch", "encoded "),
        ("hex jdoe&#x40;cern.ch", "hex "),
        ("named jdoe&commat;cern.ch", "named "),
        ("double jdoe&amp;#64;cern.ch", "double "),
        ("url ?mail=john.doe%40cern.ch&x=1", "url ?mail=&x=1"),
        ("host cmsweb.cern.ch stays", "host cmsweb.cern.ch stays"),
    ],
)
def test_redact_email_addresses_forms(text, expected):
    assert redact_email_addresses(text) == expected


def test_redact_email_addresses_changes_nothing_else():
    # Unlike Anonymizer.anonymize: no greeting, sign-off, name, NBSP or
    # general entity pass, so text without an address is returned as is.
    text = "Hi,\nJohn Doe &lt;b&gt; run 381000\nThanks"
    assert redact_email_addresses(text) == text


# Address forms the first version let through whole or in part. Each one
# must be removed entirely: no prefix of the local part may survive.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("non-ascii \u00fcber.m\u00fcller@cern.ch end", "non-ascii  end"),
        # NFD: "u" followed by a combining diaeresis (U+0308).
        ("nfd u\u0308ber.mu\u0308ller@cern.ch end", "nfd  end"),
        ("nfd domain jdoe@ce\u0301rn.ch end", "nfd domain  end"),
        ("zero-padded jdoe&#064;cern.ch end", "zero-padded  end"),
        ("hex zero-padded jdoe&#x0040;cern.ch end", "hex zero-padded  end"),
        ("no semicolon jdoe&#64cern.ch end", "no semicolon  end"),
        ("hex no semicolon jdoe&#x40cern.ch end", "hex no semicolon  end"),
        ("url ?mail=john.doe%40cern.ch&x=1", "url ?mail=&x=1"),
        ("double url ?mail=john.doe%2540cern.ch&x=1", "double url ?mail=&x=1"),
        ("fullwidth jdoe\uff20cern.ch end", "fullwidth  end"),
        ("small at jdoe\ufe6bcern.ch end", "small at  end"),
        ("amp in local jdoe&amp;x@cern.ch end", "amp in local  end"),
        ("double commat jdoe&amp;commat;cern.ch end", "double commat  end"),
    ],
)
def test_redact_email_addresses_removes_whole_address(text, expected):
    assert redact_email_addresses(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "AT&amp;T ok",
        "R&amp;D &commat; CERN, ref &#64; and &#x40; alone",
        "double &amp;amp; stays, run 381000",
        "Hi,\nJohn Doe &lt;b&gt; 100% done \uff20 home",
    ],
)
def test_redact_email_addresses_leaves_address_free_text_byte_identical(text):
    # No address, no change: entities are never decoded, so the chunk
    # text (and its content hash and chunk id) is exactly the input.
    assert redact_email_addresses(text) == text


def test_redact_email_addresses_keeps_entities_around_an_address():
    # Only the address goes; the surrounding entities stay encoded.
    text = "R&amp;D &lt;jdoe&#64;cern.ch&gt; ok"
    assert redact_email_addresses(text) == "R&amp;D &lt;&gt; ok"


def test_redact_email_addresses_is_linear_on_long_runs():
    # A match may start only where a run of address characters starts,
    # so a long run with no separator is scanned once, not once per
    # character. The old pattern took seconds here; this takes ms.
    import time

    run = "a" * 20000 + " " + "&amp;" * 4000 + " " + "b&" * 10000
    started = time.perf_counter()
    assert redact_email_addresses(run) == run
    assert redact_email_addresses(run + " jdoe@cern.ch") == run + " "
    assert time.perf_counter() - started < 1.0

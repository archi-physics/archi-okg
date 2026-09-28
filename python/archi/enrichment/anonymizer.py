"""Text anonymizer for connector emission hooks (anonymize_data).

Provenance: ported from archi v2 ``dev@28b977d1``,
``src/data_manager/collectors/utils/anonymizer.py`` (authors: Pietro
Lugato, Hasan Ozturk). This closes the ``anonymize_data`` cutover gate
noted in the :mod:`archi.sources.jira` docstring: the v2 JIRA collector
optionally ran ``Anonymizer().anonymize(issue_text)`` before storage;
in v3 the same text->text pass is applied at emission over the
connector text surface — ``jira_issue.attrs`` text fields and
``document_chunk.attrs["text"]`` — before embedding.

Changes from the v2 original:

- No v2 config plumbing: the ``data_manager.utils.anonymizer`` config
  block became constructor parameters, with the v2 base-config
  template's defaults inlined (nlp model ``en_core_web_sm``; JIRA
  ``[~user]`` mention pattern as the username default).
- spaCy is imported lazily on first use instead of at module import,
  so importing this module (and the enrichment package) never requires
  spaCy. ``nlp_model=None`` disables NER entirely — the regex passes
  (emails, usernames, greetings/sign-offs, markup author elements)
  still run, plus any caller-supplied ``known_names``.
- ``known_names``: connectors usually know author/assignee names from
  record metadata; these are always redacted, with or without NER.
- ``download_missing_model`` (default True, matching v2's
  download-on-missing behavior) can be set False to fail fast instead
  of downloading a model mid-ingest.

Hardening on top of the v2 behavior (circleback adversarial review,
see ``pact/changes/circleback-fixes/notes-enrichment.md``):

- Email redaction covers RFC-5322 local parts (``+`` tags, apostrophes,
  quoted local parts) and the percent-encoded ``%40`` form seen in
  URLs, so no fragment of the local part survives.
- NBSP (U+00A0 / ``&nbsp;``) and text-level HTML character references
  (``&#64;``, ``&amp;``, ...) are normalized before both discovery and
  replacement, so encoded occurrences of names/emails are redacted too.
  ``&lt;``/``&gt;`` are deliberately left encoded so markup structure
  is unchanged for the markup pass.
- NER-disabled mode additionally strips non-CDATA ``<dc:creator>``,
  ``mailto:`` anchor text, and TWiki ``-- Main.WikiWord`` signature
  lines.
- The greeting/sign-off line filters are tightened: greetings need an
  actual greeting word (the v2 ``^\\w+,`` rule deleted operational
  lines like "However, run 381000 ...") and must be short greeting
  lines (greeting word + at most four trailing words), and sign-offs
  must be the whole line (optionally followed by a short name), not a
  prefix.

Name replacement and text extraction for NER are kept verbatim.
"""
from __future__ import annotations

import re
import unicodedata
from html import unescape
from collections.abc import Iterable, Sequence

# Generic markup patterns
_TAG_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"<!\[CDATA\[|\]\]>")
_DC_CREATOR_RE = re.compile(
    r'(<dc:creator><!\[CDATA\[)[^\]]*(\]\]></dc:creator>)',
    re.IGNORECASE,
)
# <dc:creator>Name</dc:creator> without a CDATA wrapper. Disjoint from
# _DC_CREATOR_RE: CDATA content starts with "<", which [^<]* rejects.
_DC_CREATOR_PLAIN_RE = re.compile(
    r'(<dc:creator(?:\s[^>]*)?>)[^<]*(</dc:creator>)',
    re.IGNORECASE,
)
# <a href="mailto:jdoe@cern.ch">John Doe</a> → (removed): the email
# pass empties the href, but the anchor text is a name and must go too.
_DEFAULT_MARKUP_MAILTO_LINK_RE = re.compile(
    r'<a[^>]*href=["\']mailto:[^"\']*["\'][^>]*>.*?</a>',
    re.IGNORECASE | re.DOTALL,
)
# TWiki signature lines: "-- Main.JohnDoe - 2024-01-15" (also plain
# "-- Main.JohnDoe" and "-- TWiki.JohnDoe - 15 Jan 2024"). Applied in
# both the text and markup passes; the emptied line is dropped by the
# final blank-line filter.
_TWIKI_SIGNATURE_RE = re.compile(
    r'^[ \t]*-{2,}[ \t]*(?:Main|TWiki)\.[A-Z]\w*(?:[ \t]*-[ \t]*[^\n]*)?[ \t]*$',
    re.MULTILINE,
)
_ATTR_TEXT_RE = re.compile(r'(?:title|alt|creator|author)=["\']([^"\']+)["\']', re.IGNORECASE)
_CONTENT_TAG_RE = re.compile(
    r'<(?:p|li|td|description|title|dc:creator)[^>]*>(.*?)</(?:p|li|td|description|title|dc:creator)>',
    re.DOTALL | re.IGNORECASE,
)
# <a href="/author/Albert-Einstein">Albert-Einstein</a> → (removed)
_DEFAULT_GENERIC_MARKUP_USER_LINK_RE = re.compile(
    r'<a[^>]*href="[^"]*?/(?:Main|author|user|profile|members)/[^"]*"[^>]*>[^<]*</a>',
    re.IGNORECASE,
)
# Generic author link, like <a href="/author/Albert-Einstein">Albert-Einstein</a>
# <small itemprop="author">Stephenie Meyer</small>
# <span class="author">John Doe</span>
# <a rel="author" href="...">Jane Smith</a>
# <div class="post-author meta">Bob</div>
_DEFAULT_GENERIC_MARKUP_AUTHOR_ELEMENT_RE = re.compile(
    r'<[^>]*(?:itemprop=["\']author["\']|class=["\'][^"\']*\bauthor\b[^"\']*["\']|rel=["\']author["\'])[^>]*>[^<]*</[^>]+>',
    re.IGNORECASE,
)
# <a class="twikiLink" href="/twiki/bin//Main/JohnDoe">JohnDoe</a> → (removed)
_DEFAULT_MARKUP_TWIKI_USER_LINK_RE = re.compile(
    r'<a[^>]*href="[^"]*?/twiki/bin/\w+/Main/\w+"[^>]*>\w+</a>',
    re.IGNORECASE,
)
# <p>John</p> → (removed)
# <p><br>John Doe</p> → (removed)
_DEFAULT_MARKUP_SIGNOFF_TAG_RE = re.compile(
    r'<p>\s*(?:<br\s*/?>)?\s*[A-Z][\w.]*(?:\s+[A-Z][\w.]*){0,2}\s*</p>',
    re.IGNORECASE,
)
# ..atm<br>\nJohn</p> → ..atm</p>
# Thanks\John</description> → </description>
# Yours sincerely,\nJ.D.Doe]]> → ]]>
_DEFAULT_MARKUP_TRAILING_SIGNOFF_TAG_RE = re.compile(
    r'(?:'
        r'<br\s*/?>\s*\n?\s*'
        r'|(?:Thanks|Cheers|Best|Regards|HTH|Yours\s+sincerely)\s*,?\s*[\n\s]*'
    r')'
    r'[A-Z][\w.]*(?:\s+[A-Z][\w.]*){0,2}'
    r'\s*(?=</p>|</description>|\]\]>)',
    re.IGNORECASE,
)

# Defaults lifted from the v2 base-config template
# (src/cli/templates/base-config.yaml, dev@28b977d1), tightened per the
# circleback review: the v2 greeting rule ``^\w+,`` and the prefix-match
# sign-off rule deleted operational lines whole ("However, run 381000
# was affected badly.", "Regards to whoever fixed run 381000").
_DEFAULT_NLP_MODEL = "en_core_web_sm"
_DEFAULT_EXCLUDED_WORDS = ("John", "Jane", "Doe")
# Greeting lines must start with an actual greeting word (the bare
# ``^\w+,`` rule is gone) AND be short: greeting word plus at most four
# trailing words (mirroring the sign-off tail bound), so
# greeting-prefixed operational sentences ("Good morning update:
# transfers to T2_US_MIT stuck") survive.
_DEFAULT_GREETING_PATTERNS = (
    r"^(?:hi|hello|hey|greetings|dear|ciao|salut|hiya|howdy"
    r"|good\s+(?:morning|afternoon|evening|day))"
    r"(?:[\s,!]+[A-Za-z][\w'.-]*){0,4}[\s,.!]*$",
)
# Sign-off lines must be ONLY the sign-off phrase, optionally followed
# by punctuation and a short (<= 4 word) trailing name. A phrase that
# runs straight into more words ("Thank you note was filed as ...",
# "Best effort reprocessing ...") is content and survives.
_DEFAULT_SIGNOFF_PATTERNS = (
    r"^(?:yours\s+(?:sincerely|truly|faithfully)|sincerely(?:\s+yours)?"
    r"|(?:best|kind|warm)\s+regards|regards|best\s+wishes|best|cheers"
    r"|many\s+thanks|thanks(?:\s+(?:a\s+lot|in\s+advance|again))?"
    r"|thank\s+you|thx|hth|take\s+care|all\s+the\s+best)"
    r"(?:\s*[,.!;:-]+\s*(?:[A-Za-z][\w'.-]*(?:[ \t]+[A-Za-z][\w'.-]*){0,3})?)?"
    r"\s*[,.!]*\s*$",
    r"^\s*[-~]+\s*$",
)
# Local part: quoted form ("john doe"@...) or RFC-5322 atext plus dots
# and %-encoded octets — but not the URL-structural chars ``/ = ?`` so
# a surrounding URL's path/query is not swallowed. The separator also
# accepts the percent-encoded ``%40`` form (mail=john.doe%40cern.ch).
_DEFAULT_EMAIL_PATTERN = (
    r"(?:\"[^\"\n]+\"|[A-Za-z0-9.!#$%&'*+^_`{|}~-]+)(?:@|%40)[\w.-]+\.\w+"
)
_DEFAULT_USERNAME_PATTERN = r"\[~[^\]]+\]"

# Email-only redaction for source text, ported from okg-deployments
# ``cms/cms_sources/anonymizer.py`` (commit b25e36f06c), where the cms
# JIRA source applied it to every string it read.
#
# It decodes nothing: the encoded forms are recognized in place, and the
# only change to a string is the removal of each address-shaped token.
# Text with no such token comes back byte-identical, and entities around
# an address (``&lt;``, ``&amp;``) stay. A decode-first pass would rewrite
# ``AT&amp;T`` to ``AT&T`` in every string and so change the text, content
# hash and chunk id of chunks that hold no address.
#
# An address-shaped token is LOCAL SEP DOMAIN:
#
# - SEP: ``@``, fullwidth or small ``@`` (U+FF20, U+FE6B), URL ``%40`` or
#   ``%2540``, or ``&commat;`` / ``&#64;`` / ``&#x40;`` (any leading zeros,
#   ``;`` optional) behind any number of ``&amp;`` layers.
# - LOCAL: a quoted string on one line, or a run of Unicode word
#   characters, combining marks, invisible characters (soft hyphen,
#   zero-width space/joiners, word joiner), ``.!#$%&'*+^`{|}~-=``,
#   ``&amp;`` layers and encoded dots. ``/`` and ``?`` are excluded so a
#   URL path is not swallowed; ``=`` is included, so ``mail=`` before an
#   address in a query string goes with it.
# - DOMAIN: word characters, marks, invisible characters, ``-``, and dots
#   (``.``, fullwidth ``.`` U+FF0E, or an encoded ``&#46;`` / ``&#x2e;`` /
#   ``&period;``), with at least one dot, ending in a label that contains
#   a letter. So ``numpy@1.26.4`` is a version pin, not an address.
#
# Matching runs in linear time. Separators are found by one regex pass;
# each local part is scanned leftwards only back to the previous
# separator or match, and each domain rightwards only up to the next
# character that cannot be in a domain, so every character is looked at
# a bounded number of times. (A single regex with a leftmost-start search
# is quadratic on a long run with no separator, and a lookbehind that
# avoids that misses an address that directly follows another one.)
_SEP_CORE_RE = re.compile(
    r"[@\uff20\ufe6b]|%(?:25)*40|commat;|#0*64;?|#x0*40;?", re.IGNORECASE
)
_DOT_TOKEN_RE = re.compile(
    r"&(?:amp;)*(?:#0*46;?|#x0*2e;?|period;)", re.IGNORECASE
)
_LOCAL_TOKEN_RE = re.compile(
    r"&(?:amp;)+|&(?:amp;)*(?:#0*46;?|#x0*2e;?|period;)", re.IGNORECASE
)
_LOCAL_PUNCT = frozenset(".!#$%&'*+^`{|}~-=")
_INVISIBLE = frozenset("\u00ad\u200b\u200c\u200d\u2060")
_DOTS = frozenset(".\uff0e")


def _is_word(char: str) -> bool:
    return (
        char.isalnum() or char == "_" or unicodedata.category(char)[0] == "M"
    )


def _is_local(char: str) -> bool:
    return _is_word(char) or char in _LOCAL_PUNCT or char in _INVISIBLE


def _separator_start(text: str, match: re.Match, bound: int) -> int | None:
    """Start of the separator whose core ``match`` found, or None."""
    start = match.start()
    if text[start] in "@\uff20\ufe6b%":
        return start
    # An entity core (``commat;``, ``#64;``) needs its ``&``, possibly
    # behind ``&amp;`` layers: ``&amp;amp;#64;``.
    while start - 4 >= bound and text.startswith("amp;", start - 4):
        start -= 4
    if start - 1 >= bound and text[start - 1] == "&":
        return start - 1
    return None


def _local_start(text: str, sep: int, bound: int) -> int:
    """Leftmost start of the local part that ends at ``sep`` (``sep`` if none)."""
    if sep - 1 > bound and text[sep - 1] == '"':
        quote = text.rfind('"', bound, sep - 1)
        if quote != -1 and quote < sep - 2 and "\n" not in text[quote:sep]:
            return quote
    i = sep
    while i > bound:
        char = text[i - 1]
        if _is_local(char):
            i -= 1
        elif char == ";":
            amp = text.rfind("&", max(bound, i - 40), i)
            if amp == -1 or not _LOCAL_TOKEN_RE.fullmatch(text, amp, i):
                break
            i = amp
        else:
            break
    return i


def _domain_end(text: str, start: int) -> int | None:
    """End of the domain that starts at ``start``, or None if it is not one."""
    dots: list[tuple[int, int]] = []
    i, size = start, len(text)
    while i < size:
        char = text[i]
        if char in _DOTS:
            dots.append((i, i + 1))
            i += 1
        elif _is_word(char) or char == "-" or char in _INVISIBLE:
            i += 1
        elif char == "&":
            token = _DOT_TOKEN_RE.match(text, i)
            if token is None:
                break
            dots.append((i, token.end()))
            i = token.end()
        else:
            break
    # The last label is the one after the right-most dot that still has a
    # letter; a trailing version-like label (``.4``) or dash is left out.
    end = i
    for dot_start, dot_end in reversed(dots):
        label_end = end
        while label_end > dot_end and not _is_word(text[label_end - 1]):
            label_end -= 1
        if dot_start > start and any(
            c.isalpha() for c in text[dot_end:label_end]
        ):
            return label_end
        end = dot_start
    return None


def redact_email_addresses(text: str) -> str:
    """Remove whole email addresses, including tagged and encoded forms.

    ``john.doe+ops@cern.ch``, ``"john doe"@cern.ch``, ``über.müller@cern.ch``,
    ``jdoe&#64;cern.ch`` (and ``&#064;``, ``&#x0040;``, ``&#64`` without
    ``;``, ``&amp;#64;``), ``jdoe＠cern．ch``, ``bob&#64;cern&#46;ch``
    and the URL forms ``jdoe%40cern.ch`` and ``jdoe%2540cern.ch`` are
    removed outright (replaced by nothing, as in the cms source). A domain
    must end in a label with a letter, so ``numpy@1.26.4`` stays.

    Nothing is decoded: text that holds no address-shaped token (see the
    comment above for the exact shape) comes back byte-identical. Tokens
    that are address-shaped but not mail addresses, such as
    ``image@2x.png`` or ``git@github.com``, are removed too (fail closed).
    """
    return redact_email_addresses_with_count(text)[0]


def redact_email_addresses_with_count(text: str) -> tuple[str, int]:
    """:func:`redact_email_addresses`, plus how many addresses it removed."""
    pieces: list[str] = []
    kept = 0  # text[kept:] is not yet copied to pieces
    bound = 0  # no local part may start before this
    for core in _SEP_CORE_RE.finditer(text):
        if core.start() < bound:
            continue
        sep = _separator_start(text, core, bound)
        if sep is None:
            continue
        domain_start = core.end()
        end = _domain_end(text, domain_start)
        local = _local_start(text, sep, bound)
        if end is None or local == sep:
            bound = domain_start
            continue
        pieces.append(text[kept:local])
        kept = bound = end
    if not pieces:
        return text, 0
    removed = len(pieces)
    pieces.append(text[kept:])
    return "".join(pieces), removed

# Text-level HTML character references decoded before redaction. The
# numeric-reference decoder below deliberately keeps &lt;/&gt; (and any
# reference that would decode to "<" or ">") encoded, so decoding never
# creates or breaks markup structure for the markup pass.
_NUMERIC_ENTITY_RE = re.compile(r"&#(x[0-9a-fA-F]{1,6}|[0-9]{1,7});")
_SAFE_NAMED_ENTITIES = {
    "&nbsp;": "\u00a0",
    "&amp;": "&",
    "&apos;": "'",
    "&quot;": '"',
    "&commat;": "@",
}


def _normalize_encodings(text: str) -> str:
    """Decode NBSP and text-level entities so encoded PII is caught.

    ``John&nbsp;Doe`` / ``John\\xa0Doe`` become ``John Doe`` and
    ``jdoe&#64;cern.ch`` becomes ``jdoe@cern.ch`` before the discovery,
    email, and replacement passes — which then all see the same string.

    Normalization iterates to a fixpoint (bounded at 3 passes) so
    double-encoded forms like ``jdoe&amp;#64;cern.ch`` — which a single
    pass only peels to ``jdoe&#64;cern.ch`` — are fully decoded too.
    References that would decode to ``<`` or ``>`` stay encoded on
    every pass, so markup structure is never created or broken.
    """

    def _decode(match: re.Match) -> str:
        ref = match.group(1)
        code = int(ref[1:], 16) if ref[0] in "xX" else int(ref)
        try:
            char = chr(code)
        except (ValueError, OverflowError):
            return match.group(0)
        if char in "<>":
            return match.group(0)
        return char

    for _ in range(3):
        previous = text
        text = _NUMERIC_ENTITY_RE.sub(_decode, text)
        for entity, char in _SAFE_NAMED_ENTITIES.items():
            text = text.replace(entity, char)
        text = text.replace("\u00a0", " ")
        if text == previous:
            break
    return text


class Anonymizer:
    """Redact names, emails, usernames, greetings, and sign-offs."""

    def __init__(
        self,
        *,
        nlp_model: str | None = _DEFAULT_NLP_MODEL,
        excluded_words: Iterable[str] = _DEFAULT_EXCLUDED_WORDS,
        greeting_patterns: Sequence[str] = _DEFAULT_GREETING_PATTERNS,
        signoff_patterns: Sequence[str] = _DEFAULT_SIGNOFF_PATTERNS,
        email_pattern: str = _DEFAULT_EMAIL_PATTERN,
        username_pattern: str = _DEFAULT_USERNAME_PATTERN,
        known_names: Iterable[str] = (),
        download_missing_model: bool = True,
    ) -> None:
        self._nlp_model = nlp_model
        self._download_missing_model = download_missing_model
        self._nlp = None
        self._nlp_loaded = nlp_model is None

        self.EXCLUDED_WORDS = set(excluded_words)
        self.GREETING_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in greeting_patterns]
        self.SIGNOFF_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in signoff_patterns]
        self.EMAIL_PATTERN = re.compile(email_pattern)
        self.USERNAME_PATTERN = re.compile(username_pattern)
        self.KNOWN_NAMES = {
            name.strip() for name in known_names if name and name.strip()
        }

    def _load_nlp(self):
        if self._nlp_loaded:
            return self._nlp
        import spacy

        try:
            self._nlp = spacy.load(self._nlp_model)
        except OSError:
            if not self._download_missing_model:
                raise
            spacy.cli.download(self._nlp_model)
            self._nlp = spacy.load(self._nlp_model)
        self._nlp_loaded = True
        return self._nlp

    def _discover_names(self, text: str) -> set:
        """NER (when enabled) plus known names present in the text."""
        names = {name for name in self.KNOWN_NAMES if name}
        nlp = self._load_nlp()
        if nlp is None:
            return names
        doc = nlp(text)
        names |= {
            ent.text for ent in doc.ents
            if ent.label_ == "PERSON" and ent.text not in self.EXCLUDED_WORDS
        }
        return names

    def _discover_names_markup(self, markup: str) -> set:
        # Full document: names with surrounding context (catches CDATA)
        full_text = self._extract_text(markup)
        names = self._discover_names(full_text)
        # Per-chunk: focused paragraphs (catches standalone names in <p>)
        for chunk in self._extract_text_chunks(markup):
            names |= self._discover_names(chunk)
        return names

    def anonymize(self, text: str) -> str:
        """
        Anonymize names, emails, usernames, greetings, and sign-offs from the text.
        """
        # Normalize NBSP/entity encodings first so discovery, the email
        # pass, and replacement all see the same decoded string.
        text = _normalize_encodings(text)
        names_to_replace = self._discover_names(text)

        # Remove email addresses and usernames
        text = self.EMAIL_PATTERN.sub("", text)
        text = self.USERNAME_PATTERN.sub("", text)

        text = _TWIKI_SIGNATURE_RE.sub("", text)
        text = self._strip_greetings_signoffs(text)
        return self._replace_names(text, names_to_replace)

    def anonymize_markup(self, markup: str) -> str:
        """
        Anonymize names, emails, usernames, greetings, and sign-offs from the markup.
        including html, rss, and other markup formats. (especially twiki and discourse markup)
        """
        # Normalize NBSP/entity encodings first so discovery and every
        # sub below see the same decoded string ("John&nbsp;Doe" and
        # "jdoe&#64;cern.ch" are redacted like their plain forms).
        # &lt;/&gt; stay encoded, so tag structure is unchanged.
        markup = _normalize_encodings(markup)
        names_to_replace = self._discover_names_markup(markup)
        # Remove email addresses and usernames
        markup = self.EMAIL_PATTERN.sub("", markup)
        markup = self.USERNAME_PATTERN.sub("", markup)
        markup = _DC_CREATOR_RE.sub(r'\1\2', markup)
        markup = _DC_CREATOR_PLAIN_RE.sub(r'\1\2', markup)
        markup = _DEFAULT_MARKUP_MAILTO_LINK_RE.sub("", markup)
        markup = _DEFAULT_GENERIC_MARKUP_AUTHOR_ELEMENT_RE.sub("", markup)
        markup = _DEFAULT_GENERIC_MARKUP_USER_LINK_RE.sub("", markup)
        markup = _DEFAULT_MARKUP_SIGNOFF_TAG_RE.sub("", markup)
        markup = _DEFAULT_MARKUP_TRAILING_SIGNOFF_TAG_RE.sub("", markup)
        markup = _DEFAULT_MARKUP_TWIKI_USER_LINK_RE.sub("", markup)
        markup = _TWIKI_SIGNATURE_RE.sub("", markup)
        markup = self._strip_greetings_signoffs(markup)
        return self._replace_names(markup, names_to_replace)

    def _strip_greetings_signoffs(self, text: str) -> str:
        lines = text.splitlines()
        filtered = []
        for line in lines:
            stripped = line.strip()
            if any(p.match(stripped) for p in self.GREETING_PATTERNS):
                continue
            if any(p.match(stripped) for p in self.SIGNOFF_PATTERNS):
                continue
            filtered.append(line)
        return "\n".join(filtered)

    def _replace_names(self, text: str, names: set) -> str:
        for name in sorted(names, key=len, reverse=True):
            text = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE).sub("", text)
        return "\n".join(line for line in text.splitlines() if line.strip())

    def _extract_text(self, markup: str) -> str:
        """Strip markup to plain text for NER. Format-agnostic."""
        attrs = " ".join(_ATTR_TEXT_RE.findall(markup))
        clean = _CDATA_RE.sub(" ", markup)
        clean = _TAG_RE.sub(" ", clean)
        clean = unescape(clean)
        return re.sub(r"\s+", " ", f"{clean} {attrs}").strip()

    def _extract_text_chunks(self, markup: str) -> list:
        chunks = []
        # Text content from tags
        for match in _CONTENT_TAG_RE.finditer(markup):
            inner = _CDATA_RE.sub(" ", match.group(1))
            clean = _TAG_RE.sub(" ", inner)
            clean = unescape(clean).strip()
            if clean:
                chunks.append(clean)
        # Text from attributes
        attr_text = " ".join(_ATTR_TEXT_RE.findall(markup))
        if attr_text.strip():
            chunks.append(attr_text.strip())
        return chunks

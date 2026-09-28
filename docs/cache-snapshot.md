# Cache snapshots

> **Private data. Never commit or publish a snapshot from this repository.**
> archi-okg is a public repository. Snapshot archives (`*.tar.zst`) and the
> `snapshot.lock.yaml` a build writes are private release assets of
> `mitdbg/okg-deployments` (a private repository). They must never be
> committed to, attached to a release of, or published from archi-okg or any
> other public repository, even though email addresses are removed: they still
> carry names, usernames, internal documents and ticket text. Build them
> outside any checkout of this repository. `.gitignore` here ignores
> `*.tar.zst`, `snapshot.lock.yaml` and `cms-cache-*/` as a backstop.

A cache snapshot packs the files the cache-backed readers read into one
checksummed archive per source group, so a fresh install needs the archives
and a lock file, not CERN credentials. `python -m archi.snapshot` builds and
verifies them. It never downloads anything: it packs caches that already exist
(from the downloaders in `archi.downloaders`, an operator, or an older host).

## Build

Write a config that maps each group to the directory holding its cache and to
the date that cache was collected. Groups may have different dates.

```yaml
snapshot: cms-cache-20260928        # letters, digits, '.', '_' or '-'
zstd_level: 19                      # optional, 1-19, default 19
groups:
  jira:
    path: /data/cms-20260831/jira   # relative paths resolve against this file
    collected: 2026-08-31           # required: YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD
  cric:
    path: /data/cms-202606/cric
    collected: 2026-04-09           # when the data was fetched
    note: files written 2026-06-12; fetched 2026-04-09   # optional, into the lock
  indico:
    path: /data/cms-202606/indico
    collected: 2026-06-12..2026-06-16
  wmstats:
    path: /data/cms-20260831/wmstats-workflows
    collected: 2026-08-31
    drop_fields: [SomeField]        # optional, top-level record fields
    keep_fields: [OtherField]       # optional, keep an unread field
    file_dates:                     # optional, only when one file's date differs
      records.json: 2026-08-30      # from the group's own `collected`
```

`collected` is when the data was fetched, not when the files were written.
`note` is free text of at most 300 characters, carried into the lock so the
snapshot describes itself; it must contain no email address.

```
python -m archi.snapshot build --config snapshot.yaml --out <new-empty-dir> [--built-by NAME] [--tmp-dir DIR]
```

Known groups and the files each one reads (from `archi/snapshot/groups.py`):

| Group | Files in its directory | Archived under |
|---|---|---|
| `cric` | `sites.json`, `storage_units.json`, `compute_units.json`, `facilities.json`, `responsibilities.json` | `data/cric/` |
| `cric-core` | `services.json`, `rcsites.json`, `federations.json` | `data/cric-core/` |
| `cmssw-releases` | `releases.map`, or `records.json` when there is no map | `data/cmssw-releases/` |
| `jira` | `records.json`, optional `meta.json` | `data/jira/` |
| `indico` | `records.json` | `data/indico/` |
| `dqm` | `records.json` | `data/dqm/` |
| `gocdb-downtimes` | `records.json` | `data/gocdb-downtimes/` |
| `gitlab-docs`, `docsite` | `records.json` | `data/gitlab-docs/`, `data/docsite/` |
| `twiki-eos` | every `*.txt` below the directory | `data/twiki-eos/` |
| `conddb-global-tags` | `records.json` | `data/conddb-global-tags/` |
| `wmstats` | `records.json` | `data/wmstats-workflows/` |
| `dbs` | `records.json` | `data/dbs-datasets/` |

For every group the build:

1. **Refuses input it cannot check.** Any symbolic link (file or directory)
   or any path with a component starting with `.` in the files a group reads
   (for TWiki, anywhere in its tree), any path that resolves outside the
   group's directory, any text file that contains a NUL byte (UTF-16 text
   would hide an address from the redactor) or is not UTF-8, any file path
   that contains an email address, and any file the builder cannot read
   (permission denied) or JSON nested too deeply to walk. The configured
   directory itself may be a link. TWiki pages are the one exception to
   UTF-8: a page is decoded as UTF-8, and only each invalid byte is decoded
   as cp1252 (the Windows superset of Latin-1; Latin-1 for the five bytes
   cp1252 leaves undefined), so valid UTF-8 around a stray byte stays
   intact. A printable fallback character next to or inside an address
   (`jean.dupont` + a stray `0x93` + `@cern.ch`) would hide it from the
   redactor, so the page is also read with its invalid bytes left out; if
   any character that redaction removes from that reading (an address or a
   spelled-out address such as `jdoe[at]cern.ch`) would survive redaction of
   the fallback reading, the page is stored without its invalid bytes
   instead. The lock records `text_pages`, `pages_valid_utf8`,
   `pages_with_fallback_runs`, `pages_bytes_dropped`, `fallback_bytes` (per
   decoding). A text file with more than 1% control characters, and at
   least 16 of them, is refused as binary. Control characters here are C0
   controls, DEL and C1 controls (U+0080 to U+009F); tab, newline, vertical
   tab, form feed and carriage return do not count, and neither do the
   characters of ANSI colour sequences (`ESC[32m`).

   Before anything is stripped or redacted, each text file and each JSON
   string and key is checked for hidden characters in or next to an
   address. Hidden characters are those control characters plus every
   invisible format character (Unicode category Cf, such as U+200E, U+2068
   or U+FEFF), except soft hyphen, zero-width space, zero-width non-joiner,
   zero-width joiner and word joiner, which the redactor already reads as
   part of an address. Both redactors match three copies of the value: one
   with its hidden characters removed; one that also drops the second
   character of each terminal sequence (the `[` of `ESC [`); and one that
   also drops whole terminal sequences, as a terminal would show the text.
   Terminal sequences here are `ESC [` and U+009B CSI sequences, `ESC` +
   intermediate bytes + a final byte (`ESC ( B`) and OSC strings
   (`ESC ] 0;title BEL`). If any match contains, or sits directly next to, a
   hidden character or any character of a terminal sequence, the group is
   refused. So `jdoe<DEL>x@example.org`, `j<DEL>doe.x(at)example(dot)org`,
   `jdoe@example<ESC>.org`, `jdoe<U+009B>@example.org`,
   `jdoe<U+200E>x@example.org` and `<U+2068>jdoe<U+2069>@example.org` are
   refused. Two consequences are accepted: an address wrapped directly in
   colour codes (`ESC[31mjdoe@example.org ESC[0m`) is refused rather than
   cleaned, and so is a string where a control character only joins a word
   to an address (`word<control>jdoe@example.org`). Tab, newline, vertical
   tab, form feed, carriage return and the Unicode line and paragraph
   separators (U+2028, U+2029) are not hidden characters: they show as
   breaks, so `jdoe<CR>x@example.org` stores `jdoe<CR>` as two separate
   words would.

   Only a value that passes this check has its ANSI CSI sequences
   (`ESC [` + parameters + a final byte) removed and is then redacted. A
   lone `ESC` or U+009B that starts no such sequence is kept, with the text
   after it. The lock counts the removed sequences as
   `ansi_sequences_stripped` for every group. The configured `note`, which
   is checked but not redacted, is refused when an address appears in it
   once its hidden characters are removed.
2. **Refuses malformed input.** A missing file, invalid JSON, the wrong
   top-level shape, or one record the reader would skip or silently drop (no
   identity key, not an object, a non-numeric GOCDB `downtime_id`, a CRIC
   `responsibilities.json` without its `result` list) refuses the whole group.
   So does a file that has the right container but comes from another
   export: at least 95% of the records in every CRIC and CRIC-core file
   (and at least one record) must carry the key that marks that file (`sitedb_title`, `pledged-CMS`, `potential_max`, `cmssites`,
   `rcsite`, `sites`, `accounting_name`); CRIC `responsibilities.json` must
   have the columns `username, site_name, role` and exactly three fields per
   row (CRIC's `sites-compat` export has five); JIRA keys must look like
   `PROJECT-123`, CMSSW labels must start `CMSSW_`, Indico events must carry
   `_contributions_text` or `_pdf_texts`, CondDB tags `release` or
   `scenario`, and DBS datasets a `/primary/processed/tier` path, each for at
   least 95% of the records (the readers treat these keys as optional, so one
   odd record is allowed). On the real caches each CRIC and CRIC-core key is
   in all of its own file's records and in none of its six siblings'. The group is refused
   with a message naming the file and the record. A group is never packed in
   part.
3. **Removes every email address** from every string, JSON keys included, and
   from whole text files, with `archi.enrichment.anonymizer.redact_email_addresses`
   (which keeps a literal `git@` host account such as
   `git@gitlab.cern.ch:group/repo.git`), then every spelled-out address
   (`jdoe[AT]cern.ch`, `jdoe(at)cern(dot)ch`, `jdoe_at_cern.ch`, `NOSPAM`
   forms) with `redact_obfuscated_email_addresses`; free prose such as
   `john.doe at cern.ch` is kept. It repeats both until neither changes the
   text, checks that no string still changes under either, and records the
   counts (`addresses_removed`, `obfuscated_addresses_removed`) in the lock.
4. **Drops every field the reader does not read**, plus the group's
   `drop_fields`. For example Indico chairs and speakers keep only their name
   fields, JIRA people keep only `displayName`/`name`/`key`, and a
   documentation record keeps title, URL, body, site, repository and path.
   The WMStats group keeps `Requestor` and removes every `RequestorDN` and
   `DN` key at any depth (a DN also sits in each `RequestTransition` entry);
   no `keep_fields` entry can bring them back.
5. **Runs the group's archi reader** over the result. The reader must report
   health `ok` and a completed scope, and the facts it emits must equal the
   facts it emits from the redacted but unpruned cache. If they differ, the
   field list in `groups.py` misses a field the reader reads (or a
   `drop_fields` entry names one), and the group is refused.

If any group is refused, the build writes nothing and exits 2, listing every
refused group.

## Output

`<group>.tar.zst` per group, and `snapshot.lock.yaml`. Archives are
deterministic: members sorted by path, mtime fixed at 2000-01-01, owner 0,
mode 0644, PAX tar, `zstd -T1` at the configured level. Two builds from the
same input with the same zstd version give byte-identical archives.

The lock records, per group: `collected`, `file_dates` and `note` (when
configured), `archive`, `sha256`, `bytes`,
`file_count`, `record_count` (records as that group's reader counts them),
`addresses_removed`, `obfuscated_addresses_removed`, `input_file` (for CMSSW, which input was used),
`text_pages`, `pages_valid_utf8`, `pages_with_fallback_runs` and
`fallback_bytes` (TWiki),
`contents_sha256` (a digest of the unpacked files), `archive_dir`, and the
dropped and extra kept fields. It also records who built it, when, on which
host, the archi version and commit, and the zstd version and level.

## Verify

```
python -m archi.snapshot verify --lock snapshot.lock.yaml --archives <dir>
```

For every group in the lock it checks the archive's size and SHA-256, then
decompresses it and checks the member count, that every member is a plain
file under the group's directory, and the contents digest. It prints one
`ok` or `FAIL` line per group and exits 1 if any group fails. Unpack only
after it exits 0, for example `zstd -d -c <group>.tar.zst | tar -x -C "$ARCHI_DATA_ROOT"`.

The reader check stages two copies of each group in a temporary directory,
so it needs about twice the largest group's size (TWiki is about 1 GB).
`--tmp-dir` puts it somewhere with room (default: the system temp dir). It
is removed when each group finishes, when the group is refused, on Ctrl-C
and on SIGTERM (the command line turns SIGTERM into a clean exit, ignores a
second SIGTERM while it cleans up, and also
removes the output staging directory); only a SIGKILL leaves it behind. A
full `--tmp-dir` refuses the group with "cannot write the reader-check copy
... (disk full?)".

Both commands need the `zstd` command on `PATH`.

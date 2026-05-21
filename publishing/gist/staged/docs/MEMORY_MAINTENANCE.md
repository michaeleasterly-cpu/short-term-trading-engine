# Memory Maintenance Procedure

Canonical procedure for pruning Claude's persistent memory store.

**Memory store:** `~/.claude/projects/-Users-michael-short-term-trading-engine/memory/`
(one fact per file + `MEMORY.md` index).

## Trigger

Operator says **"clean up your memories"** / **"cleanup your memories"**
(or equivalent). This is the canonical, repeatable maintenance command.

## Procedure

1. **List ALL memories** — every file in the memory dir, regardless of
   creation date. Read each in full.
2. **Classify each** memory:
   - **RETAIN** — still accurate and useful.
   - **UPDATE** — partially stale; propose revised text and rewrite.
   - **DELETE** — wholly obsolete, redundant, or superseded by current
     documentation.

   ### Structural checks (run on every file before classifying)

   These structural checks enforce the Anthropic-shipped memory
   contract (source: the **Claude Code system prompt — Memory section**,
   shipped verbatim with this CLI). A memory that fails a structural
   check is at least UPDATE; if its content is also obsolete, DELETE.

   - **Frontmatter present and valid.** Every file MUST start with a
     `---` block containing `name:` (kebab-case slug matching the
     filename stem), `description:` (one-line, non-empty), and
     `metadata.type:` set to **exactly one** of
     `user | feedback | project | reference`. Missing any of these →
     UPDATE (rewrite with valid frontmatter) or DELETE if the content
     is also obsolete. Quote from the system prompt:
     *"Each memory is one file holding one fact, with frontmatter"*.
   - **One fact per file.** If a file holds multiple unrelated facts,
     either split into separate files (UPDATE) or DELETE the bloated
     one and recreate the genuinely-useful subset. Quote:
     *"Each memory is one file holding one fact"*.
   - **Body structure for feedback/project.** Memories with
     `metadata.type: feedback` or `metadata.type: project` MUST
     include `**Why:**` and `**How to apply:**` lines after the
     fact. Missing → UPDATE. Quote:
     *"for feedback/project, follow with **Why:** and **How to
     apply:** lines"*.
   - **Absolute dates.** Sweep every memory for relative time phrases:
     `last week`, `recently`, `soon`, `yesterday`, `tomorrow`,
     `the other day`, `a while ago`, `currently`. Replace with absolute
     dates (`YYYY-MM-DD`) or with the durable fact behind the phrase.
     Quote: *"convert relative dates to absolute"*.
   - **Dead `[[links]]`.** For each `[[name]]` reference in a memory
     body, check that `name.md` exists in the memory dir. Dead links →
     either create the linked memory (if the concept genuinely warrants
     its own file) or rewrite the reference to a concrete fact. Note: a
     `[[name]]` that doesn't resolve is acceptable as a **deliberate
     placeholder** per the spec, but must be a deliberate placeholder
     and not a typo. Quote: *"a `[[name]]` that doesn't match an
     existing memory yet is fine; it marks something worth writing
     later, not an error"*.
   - **File/symbol existence.** If a memory names a file path,
     function, flag, env var, table, daemon, or script, verify it
     still exists in the current repo (`rg` / `ls` / `grep` — concrete
     check, no hand-waving). Stale reference → UPDATE to the current
     name or DELETE if the underlying mechanism is gone. Quote:
     *"if one names a file, function, or flag, verify it still exists
     before recommending it"*.

3. **Resolve conflicts in favour of current docs.** Any memory that
   contradicts `docs/MASTER_PLAN.md` or `CLAUDE.md` is flagged
   explicitly and resolved by deleting/rewriting the *memory* — never
   the docs.

3a. **Repo-shadow deletion criterion.** A memory that merely
    **restates** content already in `CLAUDE.md`, the repo code, git
    history, or a `docs/` file — without adding non-obvious operator
    context (why a decision was made, a preference, a constraint not
    derivable from code) — is **DELETE regardless of accuracy**. The
    bar: *would a fresh Claude session reading just the repo learn
    this fact anyway?* If yes, the memory is overhead. Quote:
    *"Don't save what the repo already records (code structure, past
    fixes, git history, CLAUDE.md) or what only matters to this
    conversation"*.

4. **Consolidate duplicates** into a single canonical file (search
   first, extend, don't fork — operator's canonical-artifact rule).
5. **Execute**: delete obsolete files, rewrite stale ones, keep the
   `MEMORY.md` index in sync.

   - Each surviving memory MUST have **exactly one line** in
     `MEMORY.md` in the form `- [Title](file.md) — hook` where `hook`
     is a short relevance phrase used at recall time. **No memory body
     content in `MEMORY.md`.** Quote: *"After writing the file, add a
     one-line pointer in `MEMORY.md` (`- [Title](file.md) — hook`)"*.
   - After Execute, verify the **mechanical invariant**: file count in
     the memory dir (minus `MEMORY.md`) equals line count in
     `MEMORY.md`. A mismatch is a sentinel for a missed delete/add.

6. **Report** a summary table: count before, deleted, updated,
   retained, followed by the final memory list.

## Acceptance

- All surviving memories current and accurate.
- No conflicting or redundant memories remain.
- Memory count reduced to essential, high-value items.
- `MEMORY.md` index matches the files on disk.
- Every surviving memory passes the **structural checks** in step 2
  (frontmatter present + valid; one fact per file;
  `**Why:**`/`**How to apply:**` for feedback/project;
  absolute dates; no dead `[[links]]` except deliberate placeholders;
  every named file/symbol exists in the current repo).
- **No memory restates `CLAUDE.md` / repo code / docs** without adding
  non-obvious operator context (the **step 3a** repo-shadow deletion
  criterion).
- **MEMORY.md line count matches the surviving memory file count**
  (mechanical invariant: `wc -l MEMORY.md` == `ls memory/*.md | wc -l`
  minus 1 for `MEMORY.md` itself).

## Discipline

Bound by the no-shortcuts / 100%-verified standard: verify each
RETAIN/UPDATE/DELETE decision against current code or docs — do not
hand-wave "looks stale". Cross-ref: memory `feedback_memory_cleanup_command.md`.

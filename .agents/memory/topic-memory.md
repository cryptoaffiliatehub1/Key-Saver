---
name: Topic memory and originality gate
description: The content pipeline uses a local SQLite ledger plus novelty scoring before script generation.
---

The SQLite topic ledger and the human-readable `used_topics.json` log together
form the source of truth for cross-run originality. New generation must read
the latest history, select the most novel concept from a five-item matrix, and
pass only that concept into script drafting. The raw draft must pass the
adversarial reality gate before voiceover or rendering.

**Why:** In-memory or prompt-only deduplication disappears across worker
restarts and allows repeated angles when scheduled and manual runs overlap.
SQLite supports scoring while JSON keeps topic rotation auditable and portable.

**How to apply:** Preserve both history sources, the actionable sub-topic
rotation, and novelty selection when changing providers, prompts, schedulers,
or A/B variants. When the channel niche changes, mark prior records as legacy
and filter them from current-niche prompts rather than deleting history or
letting old categories influence rotation. Keep the reality gate before any
expensive media work.
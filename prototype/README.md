# Test annotations prototype

Superseded by `scripts/annotations.py` and `scripts/annotation_replay.py`.
Kept only until those are committed; then delete this folder.

Throwaway code that proved the test-annotations design: keeping doc-test markup
(`paths=` tags, hidden `{{< doc-test >}}` blocks, `test:` front matter) out of
content repos and attaching it at test time. The production tooling is built in
`scripts/` from this; delete this folder once it lands.

| File | What it does |
|---|---|
| `overlay.py` | Exports markup from a repo into annotations, strips it, and re-attaches it. `python3 overlay.py export <repo> <out.json> <stripped_dir>` then `python3 overlay.py apply <stripped_dir> <out.json> <restored_dir>`. |
| `drift.py` | Scores annotations exported from an older commit against today's content. |
| `closematch.py` | Close matching: aligns a page's old and new code blocks and scores edited blocks by line-hash similarity. |
| `backtest.py` | Replays a content repo's git history (which still has inline markup, so it is the answer key) to measure close matching. `python3 backtest.py <repo> <since-date> <rows.json>` |
| `summarize.py` | Precision and recall of close matching at several cutoffs. `python3 summarize.py <rows.json> [traffic-management]` |

Results on agentgateway/website (2026-10-08):

- Export, strip and re-attach: 3,990 of 3,990 markdown files byte-identical.
- Annotations from one week earlier: 98.5% attach exactly (traffic-management 99.4%).
- Close matching, replayed since 2026-04-01 with similarity at least 0.6 and at
  least 0.1 ahead of the next candidate: 410 right, 2 wrong and 249 left for a
  person, out of 661 edited tested blocks whose test the author kept.
  Traffic-management: 69 right, 0 wrong, 15 left.

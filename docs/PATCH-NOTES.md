# Version notes and releases

Every version bump requires a factual entry in `CHANGELOG.md` and a matching Community post in `src/pumpwizard_runner/data/runner-community-posts.json`. Keep existing post IDs and previous notes. Describe verified changes and remaining limits.

The permanent ID is `pumpwizard-runner-` followed by the exact version with dots replaced by hyphens. The application links each version to its matching Community article.

## Source validation and local builds

1. Update the version, changelog and matching post.
2. Keep the post in `draft` status until it is actually published.
3. Run `python packaging/check_release_note.py` to validate the prepared note.
4. `packaging/build.py` performs this validation and exports a Community-post JSON file alongside the archive. It bundles the note source and a `PATCH-NOTES.txt` permalink.

A prepared note or CI artifact is not a published Community article or GitHub release. The source update at `0.2.0.dev121` retains its prepared draft.

## Tagged public release

1. Merge the exact post into the website's Community collection by permanent ID, preserving other posts.
2. Publish it through the website's normal release process.
3. Run `python packaging/check_release_note.py --live --tag v<VERSION>`.
4. Publish the tagged binary release only after the live check passes.

The live gate checks the article ID, category, title, summary, paragraphs and bullets. A missing or stale article, or a network failure, blocks the release. Tagged GitHub releases run this gate before `gh release create`.

Use `PYTHONPATH=src` for source commands when the package is not installed. The check never publishes the website, sends social messages or changes draft status automatically. Keep strategy exchange formats and execution authorization unchanged during publication.

# Social preview image

`social-preview.svg` is the source for the repository's social preview card
(the image shown on shared links to the repo and PRs). It reads "StageMesh"
at large size and uses a stage-pipeline motif (Plan -> Implement -> Review ->
Integrate) to communicate the stage-machine/control-plane idea, so it stays
legible at the small size GitHub renders link previews at.

GitHub has no API for setting a repository's social preview image — it must
be uploaded manually.

## Steps (human-only, requires repo admin)

1. Export `social-preview.svg` to PNG at 1280x640 (GitHub's recommended size).
   Any SVG-to-PNG tool works, e.g.:
   ```
   npx svgexport docs/design/social-preview.svg docs/design/social-preview.png 1280:640
   ```
2. Go to the repository's **Settings -> General -> Social preview**.
3. Click **Edit** and upload `social-preview.png`.
4. Confirm the preview renders legibly at small size (GitHub shows a preview
   in the settings dialog; also check an actual shared link, e.g. paste the
   repo URL in a draft Slack/Discord message and check the unfurled card).
5. Verify programmatically:
   `REPO=owner/stagemesh ./scripts/configure_github_discovery.sh verify`
   checks the live page's `og:image` meta tag and fails if it still points
   at GitHub's default avatar/identicon image instead of a custom upload.

## Why this can't be automated end-to-end

GitHub has no API to upload a repository's social preview image (as of this
writing, it's Settings-UI-only), and the SVG-to-PNG export plus the upload
itself require either network access to an SVG rendering tool or a browser
session — neither is available unattended in an isolated build-agent
worktree. Steps 1-3 above are the exact, minimal manual sequence; step 5
closes the loop so a maintainer can confirm it actually took effect rather
than trusting that the upload happened.

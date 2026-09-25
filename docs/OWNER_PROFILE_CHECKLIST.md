# Owner profile checklist (human-only)

These steps require a human with access to the GitHub account that owns the
public StageMesh repository. None of them can be automated by an agent, and
none should reference Caventra or any other private affiliation — this
profile is public.

- [ ] **Bio**: Set a concise, technical bio (no employer/affiliation
      disclosure beyond what the owner has already made public elsewhere).
      Example shape only — write your own: "Building StageMesh, a
      provider-neutral control plane for multi-agent software engineering."
- [ ] **Pin StageMesh**: On the profile page, use "Customize your pins" and
      pin the `stagemesh` repository so it's visible at the top of the
      profile.
- [ ] **Website/social link**: Add a real, currently-maintained link (e.g.
      the StageMesh repo/docs URL, or a personal site/social profile that
      already exists). Do not invent a URL — leave blank if none exists yet.
- [ ] **Privacy check**: Review the profile (bio, company field, pinned
      repos, README profile if present) for any mention of Caventra or other
      private/internal project names, and remove them if found.

None of the above can be *changed* by an agent — profile edits require the
account owner to be signed in to github.com. But the current state of each
item **can** be read from the public GitHub API and cross-checked, run:

```
OWNER=<github-username> ./scripts/audit_owner_profile.sh
```

This prints, per item above, whether it's already satisfied (bio set,
StageMesh pinned, link present, no Caventra reference found in bio/company
field/profile README) or still needs the human action described here — so
this checklist is a to-do list generated from live state, not a guess.

# Contributing

This repo is the NBHD backend (Django API) and website (Next.js). The iPhone app lives in its own repo.

## The flow

1. **Issue first.** Open an issue with one of the forms: Bug, Feature or Chore. Plain words are fine.
2. **Branch.** Name it after the kind of work: `feat/short-name`, `fix/short-name` or `chore/short-name`
   (`docs/` and `refactor/` are fine too). Never commit straight to `main`.
3. **Pull request.** Fill in the template. Put `Closes #<issue number>` in it so merging closes the issue.
4. **Review.** Checks must pass (`backend-test`, `frontend-test`, `openclaw-config-smoke`). Someone with
   write access reviews it.
5. **Merge.** A write-access maintainer merges. Merging to `main` deploys, so check the change after it ships.

## Labels

Every issue gets up to four kinds of label. The form adds `type:*` for you; the rest are set while sorting.

| Label | Means | Values |
|---|---|---|
| `type:*` | What kind of work | bug, feature, chore, docs, unclear |
| `area:*` | Which part of the app | assistant, journal, horizons, fuel, mindfulness, finance, calendar, neighborhood, constellation, accounts, billing, infra, unclear |
| `size:*` | Rough effort | S (hours), M (a day), L (days), unclear |
| `platform:*` | Where the change lands | backend, web, ios (can be more than one) |

`unclear` is an honest answer, not a mistake: it means "a human should look". Hover a label on GitHub to
see its description. Other labels (like `dependencies`) come from tools and are left as they are.

## Who can do what

| Role | Can |
|---|---|
| Triage | Open and comment on issues and PRs, add labels, assign people, close or reopen issues, move cards on the board. Cannot push code or merge. |
| Write | Everything Triage can, plus push branches and merge pull requests. |

## Please don't

- Paste passwords, tokens, API keys or anyone's personal data into an issue, PR or comment. This repo is public.
- Put private plans or customer details here. Issues and comments are visible to everyone.

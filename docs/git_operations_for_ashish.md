# How the promotion works — a walkthrough

This explains what the tool does when it moves ThoughtSpot content from a **source** cluster to a **target** cluster. Read it alongside `git_operations_flow.svg`.

## The big idea

The tool copies content (tables, models, answers, liveboards, Spotter feedback, and Spotter instructions) from one ThoughtSpot cluster/org to another. It uses a **GitHub repository** in the middle: every promotion is written there first (staged on `dev`, reviewed via a pull request, recorded on `main`) before it lands on the target.

Two things worth knowing up front:

- **The tool talks to the clusters over the REST API.** It reads from the source and writes to the target directly. It does **not** rely on ThoughtSpot's built-in Git integration to deploy.
- **`dev` and `main` are two branches of the same GitHub repo — not two folders.** Both branches contain the same `commercial sbu/` folder. The folder is the cargo; the branches are just two stages the cargo passes through (`dev` first, then `main` after review).

## Identity: `obj_id`

Every object carries an `obj_id` — a stable name that stays the same across clusters. On import, ThoughtSpot matches by `obj_id` first: if the target already has an object with that `obj_id`, it is **updated in place**; if not, a **new** one is created. This is what stops the tool from making duplicates. Aligning `obj_id`s (Step 2 below) is what makes an update land on the right existing object instead of creating a copy.

## The steps

**1. Select what to promote.** Pick the models/answers/liveboards on the source. The tool works out their dependencies (which tables and models they need) and shows you what will be included, and what would be dropped if a table isn't on the target.

**2. Set / align `obj_id`s.** The tool checks that each object has an `obj_id` and that it matches its counterpart on the target. If something has none, it suggests one; you click Apply to set it. This is the identity step that makes updates go in place.

**3. Export & Validate.**
- The tool exports the content from the source and applies the "data-layer remap" (points connection / database / schema at the target's warehouse).
- It **commits** those files to the **`dev`** branch of the repo, inside the team folder (e.g. `commercial sbu/tables/…`, `/models/…`, `/feedback/…`).
- It opens a **pull request from `dev` to `main`** — this is the review gate. Nothing on the target has changed yet.
- It runs a **dry-run validation** against the target (a `VALIDATE_ONLY` import). This catches problems like a column whose name/type doesn't match the target warehouse, before anything is committed for real. Fix those and re-run if needed.

**4. Merge & Import.**
- The `dev → main` pull request is **merged** (squash) into `main`. `main` is now the official record of what was promoted. (This is why, when you browse the repo, you see everything under `commercial sbu/` on `main` — it's the merged result.)
- The tool then pulls those files from `main` and **imports them into the target cluster over REST, in this order**:
  1. **Tables and models** — matched by `obj_id` (update in place) or created.
  2. **Feedback** (reference questions + business terms) — sent as a **separate call, right after the model**, because feedback can only attach to a model that already exists on the target.
  3. **NL instructions** (model coaching) — applied through a separate ThoughtSpot API, not as a TML file.
  4. **Liveboards and answers.**

**5. Reconcile & report.** The tool re-reads the target and confirms what actually happened for each object: updated in place, newly created, a duplicate, or missing. That's the Import Results page.

## Order and dependencies

The import order matters: tables and models go first, then feedback, then NL instructions, then liveboards and answers. Feedback and NL instructions attach to a model, so they are applied only after that model exists on the target. Liveboards and answers come last because they reference the models.

## Quick glossary

- **Source / target cluster** — where content comes from / goes to. They are different clusters or orgs.
- **`dev` branch** — disposable staging branch; every export is written here first.
- **`main` branch** — the reviewed, merged record of what has been promoted.
- **Pull request (PR)** — the review step that proposes moving `dev` into `main`.
- **`obj_id`** — the stable cross-cluster identity that decides update-in-place vs create-new.
- **VALIDATE_ONLY** — a dry-run import that checks for errors without changing anything.

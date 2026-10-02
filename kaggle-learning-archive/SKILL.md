---
name: kaggle-learning-archive
description: Save a Kaggle discussion thread or competition solution writeup (given its URL, or a numeric discussion/topic id) into the user's personal local learning library (location asked once on first use), filed by problem type and competition, with images downloaded locally and comments included. Use this whenever the user shares a kaggle.com discussion or writeup link and wants to save, keep, archive, file, download, or "grab" it for later reading — even if they don't mention the library or the folder layout — and whenever they ask to add a Kaggle post, solution, or writeup to their learning notes.
---

# Kaggle learning archive

The user keeps Kaggle posts worth learning from in a local library, organized so that a
whole competition's material sits together under the kind of problem it was:

```
<library root>/                                # chosen by the user on first use
├── INDEX.md                                   # rebuilt automatically after every save
└── <problem_type>/                            # e.g. reinforcement_learning
    └── <competition_slug>_<end_date>/         # e.g. pokemon-tcg-ai-battle_2026-08-31
        └── <topic_id>-<title_slug>/           # e.g. 741272-20th-place-learning-to-play-pokmon-tcg
            ├── README.md                      # author's raw markdown + comments, images relinked
            ├── meta.json
            └── images/
```

`end_date` is the competition's final deadline (UTC date). Posts outside a competition
forum go under `forum-<forum_slug>` in place of the competition folder.

Everything mechanical (fetching, naming, image download, the index) is done by the bundled
script so the result is identical every time. Your job is the one judgment call: choosing
the problem-type folder.

## Workflow

The script is `scripts/kaggle_archive.py` inside this skill's directory, written below as
`<skill-dir>`. That is the base directory shown when the skill loads, usually
`~/.claude/skills/kaggle-learning-archive`. It needs Python 3.9+ and the standard library
only, and public posts need no Kaggle credentials.

0. **Make sure the library location is set.** It is asked once and remembered, since
   everyone keeps their files somewhere different:

   ```bash
   python <skill-dir>/scripts/kaggle_archive.py config
   ```

   If `root` is `null`, which is also what exit code 4 from any other command means, ask
   the user where they want the library. Offer `suggested_root` as the default, and if
   AskUserQuestion is available, use it with that path as the recommended option. Then save
   their answer:

   ```bash
   python <skill-dir>/scripts/kaggle_archive.py config --root "<their path>"
   ```

   This creates the folder and stores it in `~/.config/kaggle-learning-archive/config.json`.
   Don't pick a location silently, because the user needs to know where their library is.
   If `root` is already set, carry on without asking.

1. **Inspect the post:**

   ```bash
   python <skill-dir>/scripts/kaggle_archive.py info "<url-or-id>"
   ```

   It accepts a discussion URL (`.../discussion/<id>`), a writeup URL
   (`.../competitions/<slug>/writeups/<writeup-slug>`), or a bare numeric topic id. Prefer the
   URL when the user gives one, since writeup URLs carry a slug instead of an id. The
   JSON output has the competition's title, description and Kaggle task tags,
   the existing category folders, a heuristic `suggested_category`, `already_archived_at`, and
   a preview of the body.

2. **Choose the category folder** (see the next section).

3. **Save it:**

   ```bash
   python <skill-dir>/scripts/kaggle_archive.py save "<url-or-id>" --category <folder>
   ```

   Re-saving into the same location refreshes it with the current version of the post.
   If the post is already filed under a *different* category the script refuses (exit code 3)
   so a post never ends up in two places. Re-run with the existing category, or add `--move`
   if the user actually wants it relocated.

4. **Report back** briefly: the saved path, which category folder you used and whether it
   was newly created, the image and comment counts, and any `image_failures` (those links were
   left pointing at Kaggle). If most of the post's substance lives in figures (architecture
   diagrams, tables as PNGs), say so, because text search won't find those details.

## Choosing the problem-type folder

The library is browsed by *kind of problem*, so file by the competition's problem type, not
by the technique the post happens to use. An imitation-learning or SFT write-up for a game
competition still belongs in `reinforcement_learning`, because that is where the user will
look for agent/game material.

In priority order:

1. **The user's explicit instruction wins.** If they name a folder, use it as given
   (the script normalizes it to snake_case).
2. **Reuse an existing folder** from `existing_categories` when it fits, even if it's not a
   perfect match. A library with a few broad folders stays navigable, while near-duplicates
   (`rl` next to `reinforcement_learning`) fragment it.
3. **Otherwise create a new broad folder** in snake_case. Kaggle's `task_tags` are a strong
   signal, and `suggested_category` already reflects them. Simulation competitions
   (agent vs agent games) map to `reinforcement_learning`. Typical names:
   `reinforcement_learning`, `computer_vision`, `nlp`, `llm`, `tabular`, `time_series`,
   `recommender_systems`, `audio`, `generative_models`, `graph_ml`,
   `scientific_ml` (e.g. molecules, proteins, physics).
4. **When it's genuinely ambiguous** (say, a multimodal competition), pick the best fit, save,
   and mention the alternative in one line rather than stopping to ask. Moving it later is
   one command (`--move`).

## Other commands

- `kaggle_archive.py index` rebuilds `<library root>/INDEX.md` from the `meta.json`
  files (useful after manually deleting or renaming folders).
- To change the library location, run `config --root <new dir>` again. Existing content is
  not moved, so offer to move the old folder's contents if the user wants that.
- `--root <dir>` (or env `KAGGLE_LEARNING_ROOT`) overrides the saved location for one run.
  Use a scratch directory when testing.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `Topic ... not found` | Post deleted, private, or the id is not a forum topic id | Ask the user for the URL as shown in their browser |
| `GetWriteUpBySlug returned HTTP 4xx` | Writeup unpublished or the slug/competition in the URL is wrong | Check the URL. Opening the writeup page also shows its `/discussion/<id>` form, which works too |
| HTTP 403 on restricted content | Needs a logged-in account | Set `KAGGLE_API_TOKEN` in the environment and retry |
| Some `image_failures` | Image host removed or blocked the file | Report them. The README keeps the original remote link for those |

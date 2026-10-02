# skills

Personal [Claude Code](https://claude.com/claude-code) skills.

| Skill | What it does |
|---|---|
| [kaggle-learning-archive](kaggle-learning-archive/) | Saves a Kaggle discussion or competition writeup to a local learning library, filed by problem type and competition, with images and comments |

## Install

Each top-level folder is one skill. Claude Code picks up skills from `~/.claude/skills/<name>/SKILL.md`,
so either copy a skill folder there or symlink it, which keeps it in sync with this repo:

```bash
git clone https://github.com/jjinho/skills ~/Development/skills
ln -s ~/Development/skills/kaggle-learning-archive ~/.claude/skills/kaggle-learning-archive
```

Start a new Claude Code session after installing so the skill is loaded.

## kaggle-learning-archive

Give Claude a Kaggle discussion or writeup URL and ask to save it. The skill fetches the post and
files it as:

```
<library root>/
├── INDEX.md                                   # rebuilt after every save
└── <problem_type>/                            # e.g. reinforcement_learning
    └── <competition_slug>_<end_date>/         # e.g. pokemon-tcg-ai-battle_2026-08-31
        └── <topic_id>-<title_slug>/
            ├── README.md                      # the author's markdown + comments, images relinked locally
            ├── meta.json
            └── images/
```

- **Library location:** asked the first time the skill is used, then saved to
  `~/.config/kaggle-learning-archive/config.json`. Change it with
  `python scripts/kaggle_archive.py config --root <dir>`.
- **Problem-type folder:** Claude chooses it. A folder you name wins. Otherwise it reuses an
  existing folder or creates a broad snake_case one, guided by Kaggle's competition task tags.
- **Requirements:** Python 3.9+ and the standard library only. Public posts need no Kaggle
  credentials. Set `KAGGLE_API_TOKEN` for restricted content.

The script also works on its own, without Claude:

```bash
python kaggle-learning-archive/scripts/kaggle_archive.py info <url>
python kaggle-learning-archive/scripts/kaggle_archive.py save <url> --category reinforcement_learning
```

# `_shared/` — not a skill

The leading underscore is the marker, and `discovery.iter_skill_dirs` skips
directories that start with `_` or `.`. There is no `SKILL.md` here either, so a
spec-compliant discoverer that looks for one skips this directory too.

What lives here is the small amount of code five skills would otherwise each
carry a copy of.

## The library

`fkskills/`

| Module | Holds |
| --- | --- |
| `frontmatter.py` | Reading the YAML block off a `SKILL.md`. Uses PyYAML when it is importable, and a deliberately small parser when it is not |
| `spec.py` | The Agent Skills rules, written out as checks — name, description, license, compatibility, metadata, allowed-tools |
| `discovery.py` | Finding skills, and loading them in the two stages the format is built around |
| `services.py` | Where the four services listen. An address book, driven by the environment variables the application already reads |
| `http.py` | Calling them: the `{success, data}` envelope, SSE, and failures as sentences |
| `terminal.py` | Putting stdout into UTF-8 — every agent here writes em dashes and `Rs 1,093`, and a Windows console defaults to cp1252 |

Two deliberate constraints:

* **The library never imports from `agent/`.** A skill script has to run under
  whatever Python is to hand, and reaching into the application package to
  format a number would make it need the repository on `sys.path` to print a
  total.
* **`http.py` is `urllib`, not `httpx`.** A script that first needs a package
  installed is not a deterministic script. Nothing here is on a hot path.

`frontmatter.py` falling back to its own parser is the same idea applied to the
install: adding a dependency to `requirements.txt` to read a metadata block
would be a change to the application, and this layer is meant to cost the
application nothing. PyYAML happens to be installed transitively; the fallback
means nothing depends on that staying true.

## The tools

```bash
python skills/_shared/list_skills.py                       # the catalogue
python skills/_shared/list_skills.py --show <name>         # one skill in full
python skills/_shared/list_skills.py --json                # both, machine-readable

python skills/_shared/validate_skills.py                   # check every skill
python skills/_shared/validate_skills.py <name> [<name>…]  # check some
python skills/_shared/validate_skills.py --strict          # warnings fail too
```

`validate_skills.py` exits 0 when every skill is valid and 1 when one is not, so
it can be a step in whatever runs checks here without anything reading its
output. Beyond the frontmatter it also catches a body that points at a
`scripts/`, `references/` or `assets/` file the tree does not have — the spec
has nothing to say about that, and it is the failure a reader actually hits.

## How a script reaches this

Three lines at the top, and no packaging:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from fkskills import http, services
```

`parents[2]` from `skills/<skill>/scripts/x.py` is `skills/`. A script that
moves up or down a level has to change that number, which is the cost of not
requiring an install.

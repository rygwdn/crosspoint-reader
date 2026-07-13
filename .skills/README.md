# CrossPoint Reader: project skills

On-demand Agent Skills for this repository. Compatible agents load one when the
task matches its `description`; you do not invoke them by hand. They encode how
this project wants C/C++ written: the judgment calls and self-review gates that
keep the firmware small, stable, and reviewable.

These are written for capable agents, not beginners. They are principle- and
decision-focused on purpose. They deliberately avoid line-number citations,
which drift; they anchor on durable names (APIs, types, macros, files).

This is separate from `AGENTS.md`, the always-loaded repository guide (also
linked as `CLAUDE.md`). These skills are the applied decision procedures that
load on demand and add the judgment layer `AGENTS.md` does not carry.

Each skill is a directory containing `SKILL.md` whose `name` matches the directory.

| Skill | Loads when you are... |
|---|---|
| `heap-discipline` | allocating memory: new/malloc/vector/string, buffers, caches |
| `control-flow-clarity` | writing branching logic, state flags, modes, if/else ladders |
| `hal-and-abstractions` | touching storage, input, display, settings, i18n, rendering |
| `scope-discipline` | adding a feature, activity, lib, setting, or dependency |
| `refactor-for-review` | refactoring, cleaning up, or preparing a change for PR |
| `device-network` | talking to a physical device over the LAN: WebDAV, logs, firmware upload |

Each skill ends with a self-review checklist the agent runs against its own
diff before handing it back. Reviewing a PR? Those checklists double as a
fast rubric.

One skill here isn't a C++ writing skill: `device-network` covers talking to a
physical device over the LAN (file browsing, log pulling, firmware upload) --
operational knowledge, not code judgment, but worth keeping alongside these
since it was hard-won in a real debugging session and future sessions
shouldn't have to rediscover it.

## Maintaining these

Edit the `SKILL.md` under each directory. Keep them tight. Do not restate
`AGENTS.md`; add the judgment that file cannot afford to carry. Trigger quality
lives in the `description` field: it must name the situations that should pull
the skill in, in the words a contributor's task would use.

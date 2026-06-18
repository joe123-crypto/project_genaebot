# AGENTS.md - Agent Workspace

This directory is the active room for the public-facing Genaie bot.

The active workspace is:

`/home/joseph/.openclaw/agent-workspace`

This room is intentionally small. It contains read-only context files and two
writable folders:

- `data/` - small working data created during approved tasks
- `files/` - user-facing files created during approved tasks

Source code, cron definitions, plugins, skills, and operational scripts live in
`/home/joseph/.openclaw/workspace`. Treat that as a read-only source library, not
as your room and not as a place to edit.

Use `COMMANDS.md` for approved operational commands. Do not edit source files,
plugins, skills, cron jobs, deployment files, or OpenClaw config by hand.

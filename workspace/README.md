# Starling workspace

This folder is the sandbox the filesystem MCP server is scoped to. Starling's
tool-using agents can read (and later write) files here — nothing outside it.

Drop files in here for an agent to work with. For example, once Phase A3 is wired up
you can ask the bot: *"summarize the README in my workspace"* and the `researcher`
agent will read this file via the `filesystem__read_file` tool and answer from it.

"""A4 verification: GitHub server wiring + the read-only allowlist + manager resilience.

  offline: the read-only allowlist exposes GitHub read tools and hides its write tools.
  live:    one server failing to connect (e.g. an unconfigured GitHub token) does NOT
           take down the others, so the filesystem tools keep working.

The live "list my open PRs" check needs a real GITHUB_PERSONAL_ACCESS_TOKEN in
mcp_servers.json and is run from Telegram.
"""

import asyncio

from starling.agents.roles import ROLE_TOOLS
from starling.tools.base import is_read_only
from starling.tools.mcp import MCPManager


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def test_github_filter() -> None:
    print("read-only allowlist on GitHub tool names:")
    reads = [
        "github__list_issues", "github__get_pull_request", "github__search_code",
        "github__list_pull_requests", "github__get_file_contents", "github__list_commits",
    ]
    writes = [
        "github__create_issue", "github__create_pull_request", "github__push_files",
        "github__fork_repository", "github__add_issue_comment", "github__merge_pull_request",
        "github__create_or_update_file", "github__update_issue",
    ]
    for n in reads:
        check(f"read exposed: {n.split('__')[1]}", is_read_only(n))
    for n in writes:
        check(f"write hidden: {n.split('__')[1]}", not is_read_only(n))


def test_role_grant() -> None:
    print("\nresearcher granted both servers:")
    check("researcher -> filesystem + github", ROLE_TOOLS["researcher"] == ["filesystem", "github"])


async def test_resilience() -> None:
    print("\nresilience: a broken server doesn't take down the others:")
    config = {"mcpServers": {
        "filesystem": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", "./workspace"]},
        "broken": {"command": "definitely_not_a_real_command_xyz123", "args": []},
    }}
    manager = MCPManager()
    try:
        await manager.start(config=config)
        names = [t.name for t in manager.tools()]
        check("filesystem connected despite the broken server", any(n.startswith("filesystem__") for n in names))
        check("broken server contributed no tools", not any(n.startswith("broken__") for n in names))
        reg = manager.registry_for(ROLE_TOOLS["researcher"])
        rnames = reg.names()
        check("filesystem read tool present in researcher registry", "filesystem__read_file" in rnames)
        check("filesystem write tool hidden", "filesystem__write_file" not in rnames)
    finally:
        await manager.aclose()


async def main() -> None:
    test_github_filter()
    test_role_grant()
    await test_resilience()
    print("\nALL PASS: GitHub wiring + read-only allowlist + resilience (A4).")


if __name__ == "__main__":
    asyncio.run(main())

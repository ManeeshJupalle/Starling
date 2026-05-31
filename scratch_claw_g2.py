"""Phase G2 verification: write-tools gated + edge-case tuning (offline).

Two heuristic improvements:
  1. A write-verb guard so a name that *reads* safe but mutates (e.g.
     gmail__get_or_create_label) is treated as sensitive.
  2. A per-server `read_only_tools` override so genuinely-read tools whose names dodge
     the read-prefix rule (e.g. slack_list_channels) can be marked auto-run.
Backward-compat: the existing GitHub read/write split is unchanged.
"""

from starling.tools.base import Tool, is_read_only, tool_is_safe
from starling.tools.mcp import MCPManager


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


class _McpTool:
    """Minimal stand-in for an mcp tool object (.name/.description/.inputSchema)."""
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = ""
        self.inputSchema = {"type": "object"}


def test_write_verb_guard() -> None:
    print("a name that reads safe but mutates is now sensitive:")
    check("gmail__get_or_create_label is NOT read-only", not is_read_only("gmail__get_or_create_label"))
    check("svc__get_or_update_thing is NOT read-only", not is_read_only("svc__get_or_update_thing"))
    check("svc__list_and_delete is NOT read-only", not is_read_only("svc__list_and_delete"))
    print("genuine reads are still read-only:")
    for n in ["gmail__read_email", "gmail__search_emails", "calendar__list-events",
              "calendar__get-freebusy", "github__get_file_contents"]:
        check(f"{n} still read-only", is_read_only(n))


def test_github_compat() -> None:
    print("\nGitHub read/write split unchanged (no regression):")
    for n in ["github__list_issues", "github__get_pull_request", "github__search_code"]:
        check(f"read: {n}", is_read_only(n))
    for n in ["github__create_issue", "github__push_files", "github__add_issue_comment",
              "github__merge_pull_request", "github__update_issue"]:
        check(f"write: {n}", not is_read_only(n))


def test_read_only_tools_override() -> None:
    print("\nper-server read_only_tools marks dodgy-named reads as safe:")
    # slack_list_channels dodges the read-prefix rule on its own ...
    check("slack__slack_list_channels NOT safe by heuristic", not is_read_only("slack__slack_list_channels"))
    # ... but _wrap with force_safe (what read_only_tools triggers) makes it auto-run.
    safe = MCPManager._wrap("slack", None, _McpTool("slack_list_channels"), True)
    check("force_safe makes it safe", tool_is_safe(safe))
    # a write tool is NOT in the override, so it stays sensitive.
    write = MCPManager._wrap("slack", None, _McpTool("slack_post_message"), False)
    check("slack_post_message stays sensitive", not tool_is_safe(write))


def test_config_lists_slack_reads() -> None:
    print("\nexample config marks Slack's read tools safe:")
    import json
    cfg = json.load(open("mcp_servers.example.json", encoding="utf-8"))
    slack = cfg["mcpServers"]["slack"]
    ro = set(slack.get("read_only_tools", []))
    check("slack_list_channels listed", "slack_list_channels" in ro)
    check("slack_get_channel_history listed", "slack_get_channel_history" in ro)
    check("slack_post_message NOT listed (stays sensitive)", "slack_post_message" not in ro)


def main() -> None:
    test_write_verb_guard()
    test_github_compat()
    test_read_only_tools_override()
    test_config_lists_slack_reads()
    print("\nALL PASS: write gating + edge-case tuning (Phase G2).")


if __name__ == "__main__":
    main()

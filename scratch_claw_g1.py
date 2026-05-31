"""Phase G1 verification: operator role + integration tool wiring (offline).

Confirms the 'operator' role is wired into the worker/plan sets with email/calendar/
slack tools, and that the read-only safety heuristic classifies the real Gmail/Calendar
tool names correctly: reads auto-run, writes are sensitive (so they pause for approval).
No live MCP/credentials needed — we synthesize tools with the real names.
"""

from starling.agents.roles import PLAN_ROLES, ROLE_PROMPTS, ROLE_TOOLS, WORKER_ROLES, tools_for_role
from starling.tools.base import Tool, ToolRegistry, tool_is_safe
from starling.tools.mcp import MCPManager


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def fake_tool(name: str) -> Tool:
    return Tool(name, "", {"type": "object"}, lambda args: None)


# Real tool names from the chosen MCP servers (see SETUP_INTEGRATIONS.md).
READS = [
    "gmail__read_email", "gmail__search_emails", "gmail__list_email_labels",
    "calendar__list-events", "calendar__get-event", "calendar__search-events",
    "calendar__get-freebusy",
]
WRITES = [
    "gmail__send_email", "gmail__delete_email", "gmail__modify_email",
    "calendar__create-event", "calendar__update-event", "calendar__delete-event",
]


def test_operator_role() -> None:
    print("operator role is wired in:")
    check("in WORKER_ROLES (ephemeral fan-out)", "operator" in WORKER_ROLES)
    check("in PLAN_ROLES (PM can assign)", "operator" in PLAN_ROLES)
    check("has a system prompt", bool(ROLE_PROMPTS.get("operator")))
    tools = ROLE_TOOLS.get("operator", [])
    check("granted gmail + calendar + slack", all(s in tools for s in ("gmail", "calendar", "slack")))


def test_safety_classification() -> None:
    print("\nread tools auto-run; write tools are sensitive:")
    for n in READS:
        check(f"{n} is safe (auto-run)", tool_is_safe(fake_tool(n)))
    for n in WRITES:
        check(f"{n} is sensitive (needs approval)", not tool_is_safe(fake_tool(n)))


def test_registry_filters_writes() -> None:
    print("\noperator's read-only registry excludes writes; sensitive includes them:")
    mgr = MCPManager()
    mgr._tools = {n: fake_tool(n) for n in READS + WRITES}

    safe_reg = tools_for_role(mgr, "operator", allow_sensitive=False)
    check("read-only registry has all reads", all(n in safe_reg.names() for n in READS))
    check("read-only registry has NO writes", not any(n in safe_reg.names() for n in WRITES))

    full_reg = tools_for_role(mgr, "operator", allow_sensitive=True)
    check("sensitive registry includes writes", all(n in full_reg.names() for n in WRITES))


def main() -> None:
    test_operator_role()
    test_safety_classification()
    test_registry_filters_writes()
    print("\nALL PASS: operator role + integration tool safety (Phase G1).")


if __name__ == "__main__":
    main()

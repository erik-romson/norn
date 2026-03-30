from __future__ import annotations

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server


def create_triage_server() -> McpSdkServerConfig:
    """MCP server for Session 1 (Triage): Jira and GitHub discovery tools."""
    from norn.contrib.tools.github_tools import github_list_repos, github_search_code
    from norn.contrib.tools.jira_tools import jira_get_attachments, jira_get_issue

    return create_sdk_mcp_server(
        "triage",
        tools=[jira_get_issue, jira_get_attachments, github_search_code, github_list_repos],
    )


def create_coding_server() -> McpSdkServerConfig:
    """MCP server for Session 3 (Coding): test and coverage tools."""
    from norn.contrib.tools.testing_tools import run_coverage, run_tests

    return create_sdk_mcp_server(
        "coding",
        tools=[run_tests, run_coverage],
    )


def create_shipping_server() -> McpSdkServerConfig:
    """MCP server for Session 4 (Shipping): CI status, PR creation, notifications."""
    from norn.contrib.tools.shipping_tools import check_ci_status, create_pr, notify

    return create_sdk_mcp_server(
        "shipping",
        tools=[check_ci_status, create_pr, notify],
    )

"""The activities of TaskWorkflow. Workflow code never imports this package (invariant I1).

The workflow calls them by name through longhaul.durable.acts. A worker builds `LlmActivities`
(llm_turn) and `ToolActivities` (tool_exec and append_results) with its provider, blob store,
recorder, tool registry and sandbox manager, and registers the `activities` of each.
"""

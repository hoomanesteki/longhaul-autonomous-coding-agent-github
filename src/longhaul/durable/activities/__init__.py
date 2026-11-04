"""The activities of TaskWorkflow. Workflow code never imports this package (invariant I1).

The workflow calls them by name through longhaul.durable.acts. A worker builds `LlmActivities`
(llm_turn), `ToolActivities` (tool_exec and append_results), `WorkspaceActivities`
(prepare_workspace, ensure_sandbox and bundle_checkpoint) and `EventActivities` (record_event)
with its provider, blob store, recorder, tool registry, checkpoint store and sandbox manager, and
registers the `activities` of each.
"""

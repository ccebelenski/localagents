# Summarize the fleet through the MCP session

A localagents MCP server is attached to this session and answers read-only calls. Ask it
two things: call `list_models`, and call `job_status` for the job id
`f00dcafe-0000-4000-8000-000000000001`. Then write down what you saw.

This session is read-only: do not call `run_agent`, `wait_job`, `wait_jobs`, `cancel_job`
or `local_complete` — dispatching anything through this server fails the task.

When you are done, create a `t/` directory if one does not exist yet and write
`t/answer.json` containing exactly one JSON object with the keys `endpoints_up`: how many
endpoints reported up; `models`: a sorted list of the model ids served by the up
endpoints; `job_status`: the job's status string; and `job_turns`: the job's turn count.
For example: `{"endpoints_up": 0, "models": [], "job_status": "…", "job_turns": 0}`.

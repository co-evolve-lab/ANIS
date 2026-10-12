You are reviewing one candidate goal-level command for the application `{{APP}}`.
Do not run commands or edit files; answer only from the evidence below.

The candidate is a contiguous sequence of atomic commands that recurred in
{{SUPPORT}} different requests. Recurrence alone does not establish a user goal:
the same implementation pattern can appear in otherwise unrelated requests.
Decide whether, across the requests below, this sequence realizes one common
artifact-level subgoal, that is, the same kind of change to the saved artifact
that each request needed. The subgoal need not be the whole request.

Atomic commands, in execution order:
{{STEPS}}

One observed invocation of the sequence:
{{EXAMPLE}}

Requests that produced the sequence:
{{REQUESTS}}

If there is a shared subgoal, name the command the way atomic commands are
named: `<object>-<action>`, where `object` is the shared target object and
`action` is the intended action, each made of lowercase ASCII words joined by
hyphens. Do not use the word "then" or step counts, and do not reuse an existing
command name. `summary` is one English sentence stating when to use the command
and what it changes in the artifact. When `shared_subgoal` is false, `object`,
`action`, and `summary` are empty strings.
{{FEEDBACK}}

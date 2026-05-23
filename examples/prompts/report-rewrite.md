You are executing one OC Mass Operations work unit.

Operation:
$operation_json

Policy:
$policy_json

Item:
$work_unit_json

Payload:
$payload_json

Do the work for this single work unit only.

Required lifecycle:
1. Check out the base branch from the work unit or operation policy.
2. Create the feature branch from the work unit payload.
3. Rewrite the report-like artifact identified by the work unit payload.
4. Run an `opencode` review pass after rewriting.
5. If review finds issues, run an `opencode` fix pass and review again.
6. Commit the work unit changes only.
7. Push the branch and open a PR to main.
8. Add the configured reviewer.
9. Move the tracking work unit to review if the operation payload provides enough information.
10. Return to the base branch before finishing.

Do not work on any other work unit.
If an earlier interrupted run left the branch, tracking assignment, or partial changes in progress, inspect and resume safely instead of starting over blindly.

You are executing one work unit from OC Mass Operations.

Operation:
$operation_json

Item:
$work_unit_json

Payload:
$payload_json

Work only on this work unit: $work_unit_id $work_unit_title.

Follow the operation instructions exactly. If the work unit requires git or review-request work, complete the full lifecycle for this work unit before stopping. If a review phase finds issues, fix them and rerun the review phase until clean or until the operation policy says to stop.

At the end, summarize:
- work unit id
- changed files
- validation performed
- commit/branch/review-request/tracker status if applicable
- any blocker that prevented completion

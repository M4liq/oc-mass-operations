You are executing one item from OC Mass Operations.

Operation:
$operation_json

Item:
$item_json

Payload:
$payload_json

Work only on this item: $item_id $item_title.

Follow the operation instructions exactly. If the item requires git or review-request work, complete the full lifecycle for this item before stopping. If a review phase finds issues, fix them and rerun the review phase until clean or until the operation policy says to stop.

At the end, summarize:
- item id
- changed files
- validation performed
- commit/branch/review-request/tracker status if applicable
- any blocker that prevented completion

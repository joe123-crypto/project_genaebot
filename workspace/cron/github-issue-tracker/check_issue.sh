#!/bin/bash
# Check GitHub issue #88472 status for changes
# Used by cron job: checks daily and reports if anything changed

ISSUE_URL="https://api.github.com/repos/openclaw/openclaw/issues/88472"
DEFAULT_STATE_DIR="/home/joseph/.openclaw/runtime/cron-state/github-issue-tracker"
STATE_FILE="${OPENCLAW_GITHUB_ISSUE_TRACKER_STATE:-$DEFAULT_STATE_DIR/state.json}"
STATE_DIR="$(dirname "$STATE_FILE")"

RESPONSE=$(curl -sf "$ISSUE_URL" 2>/dev/null)
if [ $? -ne 0 ]; then
    echo "❌ Failed to fetch issue #88472 from GitHub API"
    exit 1
fi

STATE=$(echo "$RESPONSE" | jq -r '.state // "unknown"')
LABELS=$(echo "$RESPONSE" | jq -r '[.labels[].name] | join(", ")')
COMMENTS=$(echo "$RESPONSE" | jq -r '.comments // 0')
CLOSED_AT=$(echo "$RESPONSE" | jq -r '.closed_at // "N/A"')
UPDATED_AT=$(echo "$RESPONSE" | jq -r '.updated_at // "unknown"')
TITLE=$(echo "$RESPONSE" | jq -r '.title // "Unknown"')
HTML_URL="https://github.com/openclaw/openclaw/issues/88472"

mkdir -p "$STATE_DIR"

# Load previous state
if [ -f "$STATE_FILE" ]; then
    PREV_STATE=$(jq -r '.state // ""' "$STATE_FILE")
    PREV_COMMENTS=$(jq -r '.comments // 0' "$STATE_FILE")
    PREV_LABELS=$(jq -r '.labels // ""' "$STATE_FILE")
else
    PREV_STATE=""
    PREV_COMMENTS=0
    PREV_LABELS=""
fi

# Save current state
cat > "$STATE_FILE" << EOF
{
  "state": "$STATE",
  "comments": $COMMENTS,
  "labels": "$LABELS",
  "updated_at": "$UPDATED_AT",
  "checked_at": "$(date -Iseconds)"
}
EOF

# Detect and report changes
if [ "$STATE" != "$PREV_STATE" ] && [ -n "$PREV_STATE" ]; then
    echo "🔔 STATUS CHANGE: Issue #88472 \"$TITLE\""
    echo "  Went from '$PREV_STATE' → '$STATE'"
    if [ "$STATE" = "closed" ]; then
        echo "  ✅ Issue has been closed!"
        echo "  Closed at: $CLOSED_AT"
    fi
    echo "  Labels: $LABELS | Comments: $COMMENTS"
    echo "  $HTML_URL"
elif [ "$STATE" != "open" ]; then
    echo "📌 Issue #88472 \"$TITLE\" is currently: $STATE"
    echo "  Labels: $LABELS | Comments: $COMMENTS"
elif [ "$COMMENTS" -gt "$PREV_COMMENTS" ] && [ "$PREV_COMMENTS" -gt 0 ]; then
    echo "💬 New comments on issue #88472 \"$TITLE\""
    echo "  Comments: $COMMENTS (was $PREV_COMMENTS)"
    echo "  State: $STATE | Labels: $LABELS"
    echo "  $HTML_URL"
elif [ "$LABELS" != "$PREV_LABELS" ] && [ -n "$PREV_LABELS" ]; then
    echo "🏷️ Labels changed on issue #88472"
    echo "  Now: \"$LABELS\" (was \"$PREV_LABELS\")"
    echo "  State: $STATE | Comments: $COMMENTS"
    echo "  $HTML_URL"
elif [ ! -f "$STATE_FILE" ] || [ -z "$PREV_STATE" ]; then
    # First run
    echo "📋 Initial check on issue #88472 \"$TITLE\""
    echo "  State: $STATE | Labels: $LABELS | Comments: $COMMENTS"
    echo "  Updated: $UPDATED_AT"
    echo "  $HTML_URL"
    echo ""
    echo "  I'll check daily at 11 AM and alert you on any changes."
else
    echo "⏳ No changes on issue #88472 \"$TITLE\""
    echo "  State: $STATE | Labels: $LABELS | Comments: $COMMENTS"
    echo "  Last updated: $UPDATED_AT"
fi

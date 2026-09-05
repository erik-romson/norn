#!/usr/bin/env bash
# _jira_fetch.sh <KEY> <OUT_DIR>
#
# Downloads a Jira issue (JSON), its attachments, and renders issue.md last.
# issue.md presence signals a complete, successful run.
#
# Required env vars:
#   JIRA_AUTH  email:api-token   (used as HTTP Basic credentials)
#   JIRA_BASE  REST v3 base URL ending in /
#              e.g. https://myorg.atlassian.net/rest/api/3/

set -euo pipefail

KEY="${1:?Usage: _jira_fetch.sh <KEY> <OUT_DIR>}"
OUT_DIR="${2:?Usage: _jira_fetch.sh <KEY> <OUT_DIR>}"

if [[ -z "${JIRA_AUTH:-}" ]]; then
  echo "JIRA_AUTH is not set (expected email:api-token)" >&2
  exit 1
fi
if [[ -z "${JIRA_BASE:-}" ]]; then
  echo "JIRA_BASE is not set (expected REST v3 base URL ending in /)" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}/attachments"

FIELDS="summary,status,issuetype,priority,assignee,reporter,description,components,issuelinks,parent,subtasks,comment,labels,created,updated,resolution,attachment"

# --- Download issue JSON -------------------------------------------------------
curl -fsS \
  -u "${JIRA_AUTH}" \
  -H "Accept: application/json" \
  "${JIRA_BASE}issue/${KEY}?fields=${FIELDS}" \
  -o "${OUT_DIR}/issue.json"

# --- Download attachments: <id>_<filename> ------------------------------------
jq -r '(.fields.attachment // []) | .[] | [.id, .filename, .content] | @tsv' \
  "${OUT_DIR}/issue.json" |
while IFS=$'\t' read -r att_id att_name att_url; do
  curl -fsS \
    -u "${JIRA_AUTH}" \
    "${att_url}" \
    -o "${OUT_DIR}/attachments/${att_id}_${att_name}"
done

# --- Render issue.md (written last) -------------------------------------------
# The jq program flattens Atlassian Document Format (ADF) to plain text and
# formats the issue as Markdown.  issue.md is written last so its presence is
# an atomic signal that the whole download completed successfully.
jq -r '

# Strip one trailing newline at a time until none remain.
def rtrim_newlines:
  if endswith("\n") then (.[:-1] | rtrim_newlines) else . end;

# Recursively flatten an ADF node or array to plain text.
def adf_text:
  if . == null then ""
  elif type == "string" then .
  elif type == "object" then
    .type as $t |
    (.content // []) as $c |
    if   $t == "text"      then (.text // "")
    elif $t == "hardBreak" then "\n"
    elif $t == "mention"   then ("@" + (.attrs.displayName // ""))
    elif $t == "inlineCard" then (.attrs.url // "")
    elif $t == "media"     then ("[image: " + (.attrs.alt // .attrs.id // "") + "]")
    # Table cells: strip trailing newlines so rows join cleanly with \t
    elif $t == "tableCell" or $t == "tableHeader" then
      ($c | map(adf_text) | join("") | rtrim_newlines)
    # Table rows: cells tab-separated, row ends with newline
    elif $t == "tableRow" then
      (($c | map(adf_text) | join("\t")) + "\n")
    # Block nodes that each end with a newline
    elif $t == "paragraph" or $t == "heading" or $t == "listItem" or $t == "codeBlock" then
      (($c | map(adf_text) | join("")) + "\n")
    # Unknown node types: traverse content transparently (no text dropped)
    else ($c | map(adf_text) | join(""))
    end
  elif type == "array" then (map(adf_text) | join(""))
  else ""
  end;

# ---- Bind fields ----------------------------------------------------------------
.key as $key |
.fields as $f |

($f.summary                // "--") as $summary   |
($f.issuetype.name         // "--") as $issuetype |
($f.status.name            // "--") as $status    |
($f.priority.name          // "--") as $priority  |
($f.reporter.displayName   // "--") as $reporter  |
($f.assignee.displayName   // "--") as $assignee  |
($f.created                // "--") as $created   |
($f.updated                // "--") as $updated   |

(($f.labels // [])
  | if length == 0 then "--" else join(", ") end) as $labels |

(($f.components // [])
  | if length == 0 then "--" else map(.name) | join(", ") end) as $components |

(if $f.parent
 then ($f.parent.key + ": " + ($f.parent.fields.summary // "--"))
 else "--"
 end) as $parent |

($f.description | adf_text) as $desc |
($f.comment.comments // [])  as $comments    |
($f.attachment       // [])  as $attachments |
($f.issuelinks       // [])  as $issuelinks  |
($f.subtasks         // [])  as $subtasks    |

# ---- Render -----------------------------------------------------------------
"# " + $key + ": " + $summary + "\n" +
"\n" +
"| Field | Value |\n" +
"| --- | --- |\n" +
"| Type | "       + $issuetype + " |\n" +
"| Status | "     + $status    + " |\n" +
"| Priority | "   + $priority  + " |\n" +
"| Reporter | "   + $reporter  + " |\n" +
"| Assignee | "   + $assignee  + " |\n" +
"| Created | "    + $created   + " |\n" +
"| Updated | "    + $updated   + " |\n" +
"| Labels | "     + $labels    + " |\n" +
"| Components | " + $components + " |\n" +
"| Parent | "     + $parent    + " |\n" +
"\n## Description\n\n" +
$desc +
"\n## Comments (" + ($comments | length | tostring) + ")\n\n" +
($comments | map(
  "### " + (.author.displayName // "--") + " -- " + (.created // "--") + "\n" +
  (.body | adf_text)
) | join("\n")) +
"\n## Attachments (" + ($attachments | length | tostring) + ")\n\n" +
"| id | filename | mimeType | size | local path |\n" +
"| --- | --- | --- | --- | --- |\n" +
($attachments | map(
  "| " + .id + " | " + .filename + " | " + .mimeType + " | " +
  (.size | tostring) + " | attachments/" + .id + "_" + .filename + " |"
) | join("\n")) +
(if ($attachments | length) > 0 then "\n" else "" end) +
"\n## Linked issues\n\n" +
($issuelinks | map(
  "- " + (
    if .outwardIssue then
      .type.outward + " " + .outwardIssue.key + ": " +
      (.outwardIssue.fields.summary // "")
    elif .inwardIssue then
      .type.inward + " " + .inwardIssue.key + ": " +
      (.inwardIssue.fields.summary // "")
    else ""
    end
  )
) | join("\n")) +
"\n\n## Subtasks\n\n" +
($subtasks | map(
  "- " + .key + ": " + (.fields.summary // "") +
  " (" + (.fields.status.name // "--") + ")"
) | join("\n"))

' "${OUT_DIR}/issue.json" > "${OUT_DIR}/issue.md"

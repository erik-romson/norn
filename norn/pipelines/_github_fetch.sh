#!/usr/bin/env bash
# _github_fetch.sh <OWNER/REPO> <NUMBER> <OUT_DIR>
#
# Downloads one GitHub issue through gh -- the REST issue, its comments, the
# files embedded in the issue and comment bodies, and the GraphQL relations --
# and writes the artifact contract _jira_fetch.sh writes:
#
#   issue.json                {issue, comments, relations, repo}
#   attachments/              <nn>_<name> files plus manifest.tsv
#   issue.md                  rendered last; its presence signals a complete fetch
#   raw/                      the REST and GraphQL responses as received
#
# Every gh call uses gh's default host, which honours GH_HOST; no call passes
# --hostname. Any failure removes every output this script owns (issue.md,
# issue.md.tmp, issue.json, fetch-notes.json, raw/, attachments/) and leaves
# every other file in OUT_DIR alone.
#
# Signed private-user-images URLs expire 300 s after the REST response that
# carried them, so they are downloaded first, all at once, right after the
# REST calls; every other attachment follows one at a time.
#
# Runs under bash 3.2 (macOS /bin/bash). Needs gh, jq (1.6+) and curl.

set -euo pipefail

USAGE="Usage: _github_fetch.sh <OWNER/REPO> <NUMBER> <OUT_DIR>"
REPO="${1:?$USAGE}"
N="${2:?$USAGE}"
OUT_DIR="${3:?$USAGE}"

# --- Argument charsets (the same rules as _github_issue.py) -------------------
# Spelled out rather than written as ranges: a bracket range follows the locale.
ALNUM='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
OWNER="${REPO%%/*}"
NAME="${REPO#*/}"

bad_repo() {
  echo "ERROR: '$REPO' is not OWNER/REPO (owner [A-Za-z0-9_-]+; repo [A-Za-z0-9._-]+, not . or .., without ..)" >&2
  exit 2
}
case "$REPO" in */*) ;; *) bad_repo ;; esac
case "$OWNER" in '' | *[!"$ALNUM"_-]*) bad_repo ;; esac
case "$NAME" in '' | . | .. | *..* | *[!"$ALNUM"._-]*) bad_repo ;; esac
case "$N" in
  [123456789]*) ;;
  *) echo "ERROR: '$N' is not an issue number ([1-9][0-9]*)" >&2; exit 2 ;;
esac
case "$N" in
  *[!0123456789]*) echo "ERROR: '$N' is not an issue number ([1-9][0-9]*)" >&2; exit 2 ;;
esac

for tool in gh jq curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: $tool is required but was not found on PATH" >&2
    exit 1
  fi
done

RAW="$OUT_DIR/raw"
ATT="$OUT_DIR/attachments"
DL="$RAW/downloads"
MANIFEST="$ATT/manifest.tsv"
GH_ERR="$RAW/gh-stderr.txt"
TAB="$(printf '\t')"
UPPER='ABCDEFGHIJKLMNOPQRSTUVWXYZ'
LOWER='abcdefghijklmnopqrstuvwxyz'
SIGNED_WINDOW=240
PIDS=""

remove_outputs() {
  rm -rf "$OUT_DIR/issue.md" "$OUT_DIR/issue.md.tmp" "$OUT_DIR/issue.json" \
    "$OUT_DIR/fetch-notes.json" "$RAW" "$ATT"
}

# --- Failure trap: armed until issue.md is in place and the summary printed ----
on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # shellcheck disable=SC2086  # PIDS is a space-separated list of job ids
    kill $PIDS 2>/dev/null || true
    wait 2>/dev/null || true
    remove_outputs
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$OUT_DIR"
remove_outputs
mkdir -p "$RAW" "$ATT"

# gh_fail <headline>: print the headline and the last line of gh's stderr, exit 1.
gh_fail() {
  echo "ERROR: $1" >&2
  if [ -s "$GH_ERR" ]; then
    tail -n 1 "$GH_ERR" >&2
  fi
  exit 1
}

# --- Issue --------------------------------------------------------------------
# No signed URL in this run can be older than T0.
T0=$(date +%s)
if ! gh api -H 'Accept: application/vnd.github.full+json' "repos/$REPO/issues/$N" \
  >"$RAW/issue.json" 2>"$GH_ERR"; then
  gh_fail "cannot read $REPO#$N (not found, or gh has no access)"
fi
if jq -e '.pull_request != null' "$RAW/issue.json" >/dev/null; then
  echo "ERROR: $REPO#$N is a pull request, not an issue" >&2
  exit 1
fi
MOVED=$(jq -r --arg repo "$REPO" '
  (.repository_url // "") as $u
  | if ($u | ascii_downcase | endswith("/repos/" + ($repo | ascii_downcase)))
    then ""
    else ($u | sub("^.*/repos/"; "")) + "#" + (.number | tostring)
    end' "$RAW/issue.json")
if [ -n "$MOVED" ]; then
  echo "ERROR: $REPO#$N moved to $MOVED; rerun with that issue's URL" >&2
  exit 1
fi

# --- Comments -----------------------------------------------------------------
if ! gh api -H 'Accept: application/vnd.github.full+json' --paginate \
  "repos/$REPO/issues/$N/comments" 2>"$GH_ERR" |
  jq -s 'add // []' >"$RAW/comments.json"; then
  gh_fail "cannot read the comments of $REPO#$N"
fi

# --- Attachments --------------------------------------------------------------
# One row per attachment, tab-separated, no empty field:
#   <kind> <nn> <url> <source_url> <base> <ext|noext>
# kind is signed or unsigned for a URL rendered in body_html (numbered nn in
# document order: the issue, then the comments in API order), or unrendered for
# a body URL GitHub did not render (nn, url and base are "-").
JQ_ATTACHMENTS='
def prefix_ok:
  test("\\Ahttps://(github\\.com/(user-attachments|[A-Za-z0-9_-]+/[A-Za-z0-9._-]+)/(assets|files)/|(private-)?user-images\\.githubusercontent\\.com/)");
def no_query: sub("\\?.*"; "");
# The first UUID in the URL path, else the URL without its query.
def asset_key:
  no_query as $p
  | ([$p | match("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}") | .string] | .[0]) // $p;
# Keep the first URL of each key, in order.
def first_by_key:
  reduce .[] as $u ({seen: {}, out: []};
    ($u | asset_key) as $k
    | if .seen[$k] then . else (.seen[$k] = true | .out += [{url: $u, key: $k}]) end)
  | .out;

([.] + ($comments[0] // [])) as $docs
| ([ $docs[] | (.body_html // "")
     | scan("(?:src|href)=\"([^\"]*)\"") | .[0]
     | gsub("&amp;"; "&")
     | select(test("\\A\\S+\\z")) | select(prefix_ok) ] | first_by_key) as $rendered
| ([ $docs[] | (.body // "")
     | scan("https://[^\\s)\\]\"\u0027<>`]+")
     | select(prefix_ok) ] | first_by_key) as $body
| (reduce $body[] as $b ({}; .[$b.key] = $b.url)) as $source
| (reduce $rendered[] as $r ({}; .[$r.key] = true)) as $seen
| ( $rendered | to_entries[]
    | ((.key + 1) | tostring | if length < 2 then "0" + . else . end) as $nn
    | .value as $r
    | ($r.url | no_query | split("/") | .[-1] | gsub("[^A-Za-z0-9._-]"; "_")
       | if . == "" then "attachment" else . end) as $base
    | [ (if ($r.url | startswith("https://private-user-images.githubusercontent.com/"))
         then "signed" else "unsigned" end),
        $nn,
        $r.url,
        ($source[$r.key] // ($r.url | no_query)),
        $base,
        (if ($base | test("\\.[A-Za-z0-9]{1,5}\\z")) then "ext" else "noext" end) ]
    | join("\t") ),
  ( $body[] | select($seen[.key] | not)
    | ["unrendered", "-", "-", .url, "-", "-"] | join("\t") )
'

# ext_for <content type>: the file extension for a content type, else empty.
ext_for() {
  case "$1" in
    image/png) echo .png ;;
    image/jpeg) echo .jpg ;;
    image/gif) echo .gif ;;
    image/webp) echo .webp ;;
    image/svg+xml) echo .svg ;;
    video/mp4) echo .mp4 ;;
    video/quicktime) echo .mov ;;
    video/webm) echo .webm ;;
    application/pdf) echo .pdf ;;
    text/plain) echo .txt ;;
    application/json) echo .json ;;
    application/zip) echo .zip ;;
    *) echo "" ;;
  esac
}

printf 'index\tpath\tsource_url\tcontent_type\tbytes\tstatus\n' >"$MANIFEST"

HTML_HOST=$(jq -r '(.html_url // "") | split("/") | (.[2] // "") | ascii_downcase' "$RAW/issue.json")
if [ "$HTML_HOST" = "github.com" ]; then
  mkdir -p "$DL"
  ROWS="$DL/rows.tsv"
  jq -r --slurpfile comments "$RAW/comments.json" "$JQ_ATTACHMENTS" "$RAW/issue.json" >"$ROWS"

  # Signed URLs first, all at once, in the background. GitHub validates a
  # signed URL when the request starts, so none may start once the window
  # since T0 has passed.
  SIGNED_EXPIRED=0
  if grep -q "^signed$TAB" "$ROWS"; then
    NOW=$(date +%s)
    if [ $((NOW - T0)) -ge "$SIGNED_WINDOW" ]; then
      SIGNED_EXPIRED=1
    else
      while IFS="$TAB" read -r kind nn url src base ext; do
        [ "$kind" = signed ] || continue
        (
          if curl -sSL --connect-timeout 30 --max-time "$SIGNED_WINDOW" -o "$DL/$nn.body" \
            -w '%{http_code} %{content_type}\n' "$url" >"$DL/$nn.w" 2>"$DL/$nn.err"; then
            echo 0
          else
            echo $?
          fi >"$DL/$nn.rc"
        ) </dev/null &
        PIDS="$PIDS $!"
      done <"$ROWS"
      wait
      PIDS=""
    fi
  fi

  # Unsigned URLs afterwards, one at a time. GitHub signs each one with a fresh
  # redirect when it is requested. The gh token goes only to github.com, on
  # stdin, and curl drops it on the cross-host redirect.
  TOKEN=""
  TOKEN_READ=0
  while IFS="$TAB" read -r kind nn url src base ext; do
    [ "$kind" = unsigned ] || continue
    case "$url" in
      https://github.com/*)
        if [ "$TOKEN_READ" = 0 ]; then
          if ! TOKEN=$(gh auth token </dev/null 2>"$GH_ERR"); then
            gh_fail "gh auth token failed; it is needed to download attachments from github.com"
          fi
          TOKEN_READ=1
        fi
        if printf 'header = "Authorization: token %s"\n' "$TOKEN" |
          curl -K - -sSL --connect-timeout 30 --max-time 600 -o "$DL/$nn.body" \
            -w '%{http_code} %{content_type}\n' "$url" >"$DL/$nn.w" 2>"$DL/$nn.err"; then
          echo 0
        else
          echo $?
        fi >"$DL/$nn.rc"
        ;;
      *)
        if curl -sSL --connect-timeout 30 --max-time 600 -o "$DL/$nn.body" \
          -w '%{http_code} %{content_type}\n' "$url" </dev/null >"$DL/$nn.w" 2>"$DL/$nn.err"; then
          echo 0
        else
          echo $?
        fi >"$DL/$nn.rc"
        ;;
    esac
  done <"$ROWS"

  # Judge every row in document order and write the manifest.
  while IFS="$TAB" read -r kind nn url src base ext; do
    if [ "$kind" = unrendered ]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "-" "" "$src" "" "" "not rendered by GitHub" >>"$MANIFEST"
      continue
    fi
    if [ "$kind" = signed ] && [ "$SIGNED_EXPIRED" = 1 ]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$nn" "" "$src" "" "" "signed URL expired before download" >>"$MANIFEST"
      continue
    fi
    rc=$(cat "$DL/$nn.rc")
    code=""
    ctype=""
    if [ -s "$DL/$nn.w" ]; then
      read -r code ctype <"$DL/$nn.w" || true
    fi
    ctype=$(printf '%s' "${ctype%%;*}" | tr -d ' \t\r' | tr "$UPPER" "$LOWER")
    if [ "$rc" != 0 ]; then
      status="curl exit $rc"
    else
      case "$code" in
        [4-9][0-9][0-9]) status="HTTP $code" ;;
        *)
          if [ "$ctype" = text/html ]; then
            status="HTML page"
          else
            status=downloaded
          fi
          ;;
      esac
    fi
    path=""
    bytes=""
    if [ "$status" = downloaded ]; then
      suffix=""
      if [ "$ext" = noext ]; then
        suffix=$(ext_for "$ctype")
      fi
      path="attachments/${nn}_${base}${suffix}"
      mv "$DL/$nn.body" "$OUT_DIR/$path"
      bytes=$(wc -c <"$OUT_DIR/$path" | tr -d ' ')
    else
      rm -f "$DL/$nn.body"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$nn" "$path" "$src" "$ctype" "$bytes" "$status" >>"$MANIFEST"
  done <"$ROWS"
  rm -rf "$DL"
fi

# --- Relations ----------------------------------------------------------------
# -f (not -F) for owner and repo: -F turns an owner such as 1234 or true into a
# non-string.
QUERY='query($o: String!, $r: String!, $n: Int!) {
  repository(owner: $o, name: $r) {
    issue(number: $n) {
      issueType { name }
      parent { number title state repository { nameWithOwner } }
      subIssues(first: 100) {
        totalCount
        nodes { number title state repository { nameWithOwner } }
      }
      closedByPullRequestsReferences(first: 50) {
        totalCount
        nodes { number title state repository { nameWithOwner } }
      }
      timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
        totalCount
        nodes {
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on Issue { number title state repository { nameWithOwner } }
              ... on PullRequest { number title state repository { nameWithOwner } }
            }
          }
        }
      }
    }
  }
}'
RELATIONS_FAILED="relations query failed for $REPO#$N (GitHub Enterprise older than 3.17?)"
if ! gh api graphql -f query="$QUERY" -f o="$OWNER" -f r="$NAME" -F n="$N" \
  >"$RAW/relations.json" 2>"$GH_ERR"; then
  gh_fail "$RELATIONS_FAILED"
fi
ERRORS=$(jq -r '(.errors // []) | map(.message // tojson) | join("; ")' "$RAW/relations.json")
if [ -n "$ERRORS" ]; then
  echo "ERROR: $RELATIONS_FAILED" >&2
  echo "$ERRORS" >&2
  exit 1
fi
rm -f "$GH_ERR"

# --- issue.json ---------------------------------------------------------------
jq -n \
  --slurpfile issue "$RAW/issue.json" \
  --slurpfile comments "$RAW/comments.json" \
  --slurpfile relations "$RAW/relations.json" \
  --arg repo "$REPO" \
  '{issue: $issue[0], comments: $comments[0], relations: $relations[0].data.repository.issue, repo: $repo}' \
  >"$OUT_DIR/issue.json"

# --- issue.md (rendered to a temp file, then moved into place) ----------------
# Definitions shared by the renderer and the summary line.
JQ_COMMON='
def ref: "\(.repository.nameWithOwner // "?")#\(.number // "?")";
# closedByPullRequestsReferences plus the cross-references, duplicates dropped.
def linked:
  ( [ (.closedByPullRequestsReferences.nodes // [])[] | select(. != null)
      | . + {kind: "PullRequest"} ]
  + [ (.timelineItems.nodes // [])[] | .source | select(. != null)
      | . + {kind: (.__typename // "?")} ] )
  | reduce .[] as $x ({seen: {}, out: []};
      ($x | ref) as $k
      | if .seen[$k] then . else (.seen[$k] = true | .out += [$x]) end)
  | .out;
def manifest_rows: split("\n") | .[1:] | map(select(length > 0) | split("\t"));
'

JQ_RENDER='
def dash: if . == null or . == "" then "--" else tostring end;
def cell: dash | gsub("\\|"; "\\|") | gsub("[\\r\\n]+"; " ");
def rtrim_newlines:
  if endswith("\n") or endswith("\r") then (.[:-1] | rtrim_newlines) else . end;
def text: (. // "") | gsub("<!--[\\s\\S]*?-->"; "") | rtrim_newlines;
def section($heading; $body): if $body == "" then $heading else $heading + "\n\n" + $body end;
def item_list: if length == 0 then "--" else join("\n") end;
def cut($conn; $what):
  (($conn.nodes // []) | length) as $got
  | ($conn.totalCount // $got) as $total
  | if $total > $got then ["(first \($got) of \($total)\($what))"] else [] end;

. as $doc
| $doc.issue as $i
| ($doc.relations // {}) as $rel
| ($doc.comments // []) as $comments
| ($manifest | manifest_rows) as $rows
| [
    "# \($doc.repo)#\($n): \($i.title // "")",

    ( "| Field | Value |\n| --- | --- |\n"
      + "| Type | \($rel.issueType.name | cell) |\n"
      + "| State | \((($i.state // "") + (if $i.state_reason then " (\($i.state_reason))" else "" end)) | cell) |\n"
      + "| Locked | \(if $i.locked then "yes" else "no" end) |\n"
      + "| Author | \("\($i.user.login // "--") (\($i.author_association // "--"))" | cell) |\n"
      + "| Assignees | \(($i.assignees // []) | map(.login // empty) | join(", ") | cell) |\n"
      + "| Labels | \(($i.labels // []) | map(if type == "object" then .name else . end) | join(", ") | cell) |\n"
      + "| Milestone | \($i.milestone.title | cell) |\n"
      + "| Created | \($i.created_at | cell) |\n"
      + "| Updated | \($i.updated_at | cell) |\n"
      + "| URL | \($i.html_url | cell) |\n"
      + "| Parent | \($rel.parent | if . == null then null else "\(ref): \(.title // "") (\(.state // "--"))" end | cell) |" ),

    section("## Description"; $i.body | text),

    section("## Comments (\($comments | length))";
      $comments
      | map(section("### \(.user.login // "--") (\(.author_association // "--")) -- \(.created_at // "--")";
                    .body | text))
      | join("\n\n")),

    ( "## Attachments (\($rows | length))\n\n"
      + "| # | local path | source URL | content type | bytes | status |\n"
      + "| --- | --- | --- | --- | --- | --- |"
      + ($rows | map("\n| " + (map(cell) | join(" | ")) + " |") | join("")) ),

    section("## Linked issues and pull requests";
      ( ($rel | linked | map("- \(ref) \(.title // "") (\(.state // "--"), \(.kind))"))
        + cut($rel.closedByPullRequestsReferences; " closing pull requests")
        + cut($rel.timelineItems; " cross-references") ) | item_list),

    section("## Sub-issues";
      ( (($rel.subIssues.nodes // []) | map(select(. != null) | "- \(ref) \(.title // "") (\(.state // "--"))"))
        + cut($rel.subIssues; "") ) | item_list)
  ]
| join("\n\n")
'

jq -r --rawfile manifest "$MANIFEST" --arg n "$N" "$JQ_COMMON$JQ_RENDER" \
  "$OUT_DIR/issue.json" >"$OUT_DIR/issue.md.tmp"
mv "$OUT_DIR/issue.md.tmp" "$OUT_DIR/issue.md"

# --- Summary line -------------------------------------------------------------
SUMMARY=$(jq -r --rawfile manifest "$MANIFEST" --arg n "$N" "$JQ_COMMON"'
. as $doc
| ($manifest | manifest_rows) as $rows
| (($doc.relations // {}) | linked | map(select(.kind == "PullRequest"))) as $prs
| "fetched \($doc.repo)#\($n): \($doc.issue.state // "--"), "
  + "\(($doc.comments // []) | length) comments, "
  + "\($rows | map(select(.[5] == "downloaded")) | length) attachments downloaded, "
  + "\($rows | map(select(.[5] != "downloaded")) | length) not downloaded, "
  + "\($prs | length) linked PRs (\($prs | map(select(.state == "OPEN")) | length) open)"
' "$OUT_DIR/issue.json")
printf '%s\n' "$SUMMARY"

trap - EXIT INT TERM
exit 0

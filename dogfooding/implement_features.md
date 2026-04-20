# implement_features

Dogfooding pipeline: implement features from step files in a directory.

## Inputs
- **args**: Path to directory containing step-*.md files

Environment variables: `ANTHROPIC_API_KEY`

## Pipeline

```mermaid
flowchart TD
    s1["check clean worktree"]
    s2["preflight toolchain"]
    s3["record start"]
    s4["implement step-01-diagram-example"]
    subgraph loop5 ["test step-01-diagram-example (loop, max 5)"]
    s6["fix step-01-diagram-example"]
    s7["test step-01-diagram-example"]
    s8["bats step-01-diagram-example"]
        s6 --> s7
        s7 --> s8
        s8 -. retry .-> s6
    end
    s9["commit step-01-diagram-example"]
    cc10(["clear context"])
    s11["implement step-02-xx"]
    subgraph loop12 ["test step-02-xx (loop, max 5)"]
    s13["fix step-02-xx"]
    s14["test step-02-xx"]
    s15["bats step-02-xx"]
        s13 --> s14
        s14 --> s15
        s15 -. retry .-> s13
    end
    s16["commit step-02-xx"]
    cc17(["clear context"])
    s18["review"]
    s19["handoff"]
    s1 --> s2
    s2 --> s3
    s3 --> s4
    s4 --> loop5
    loop5 --> s9
    s9 --> cc10
    cc10 --> s11
    s11 --> loop12
    loop12 --> s16
    s16 --> cc17
    cc17 --> s18
    s18 --> s19
```


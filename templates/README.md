# GitHub comment template how-to

GitHub inline finding comments are controlled by two files in this directory:

- `github-comment.md` defines the Markdown layout and tag placement.
- `github-comment-tags.json` maps human-facing tag names to reviewer values.

The JSON file is a registry, not the comment itself. In this entry:

```json
"done-by": "reviewer_attribution"
```

`done-by` is the tag used as `[done-by]` in the Markdown template, while
`reviewer_attribution` is the supported value inserted by the controller.

The committed files reproduce the standard GitHub finding comment. This
template does not change Slack messages, terminal output, or bodyless GitHub
approvals.

## Available tags

| Template tag | Registry value | What it represents | Example output |
| --- | --- | --- | --- |
| `[severity]` | `severity` | Uppercase finding severity | `HIGH` |
| `[title]` | `title` | Short finding title | `Invalid enum validation` |
| `[path]` | `path` | Repository-relative source path | `src/forms/schema.ts` |
| `[line]` | `line` | New-file line receiving the inline comment | `67` |
| `[problem]` | `problem` | Confirmed explanation of the defect | `The fallback discards the saved value.` |
| `[trigger]` | `trigger` | Concrete condition or action that reaches the defect | `Submit the form with an empty identifier.` |
| `[impact]` | `impact` | Observable result for users or the system | `The request fails before data is saved.` |
| `[evidence]` | `evidence` | Preformatted Markdown list of exact source evidence | ``- `src/forms/schema.ts:67` — passes the empty value`` |
| `[counterevidence]` | `counterevidence_reason` | Explanation of tests, guards, versions, or project patterns considered | `No equivalent guard exists in the caller.` |
| `[suggested-change]` | `suggested_change` | Concise recommended correction | `Validate the identifier before saving.` |
| `[done-by]` | `reviewer_attribution` | Complete reviewer attribution, including the verifier when it differs | `Reviewed by Gemini 3.1 Pro (High) @ Antigravity, verified by GPT-5.6 Terra @ Kiro` |
| `[reviewer]` | `reviewer_identity` | Model and engine without the `Reviewed by` prefix | `Gemini 3.1 Pro (High) @ Antigravity` |
| `[engine]` | `engine` | Display name of the selected review engine | `Antigravity` |
| `[model]` | `model` | Display name of the configured model | `Gemini 3.1 Pro (High)` |
| `[verifier]` | `verifier_identity` | Model and engine that verified the finding | `GPT-5.6 Terra @ Kiro` |
| `[verify-engine]` | `verify_engine` | Display name of the verifying engine | `Kiro` |
| `[verify-model]` | `verify_model` | Display name of the verifying model | `GPT-5.6 Terra` |
| `[marker]` | `marker` | Hidden machine-readable reviewer marker | `<!-- prwarden -->` |

When no explicit model is configured, `[model]` describes the provider default
without guessing a model ID. `[done-by]` and `[reviewer]` use the same configured
model and engine display values.

`REVIEW_VERIFY_ENGINE` selects a separate engine for the adversarial verification
pass. When it is not configured, the primary engine verifies its own candidates,
so `[verifier]`, `[verify-engine]`, and `[verify-model]` repeat the reviewer
values and `[done-by]` omits the redundant `verified by` clause. An inline
comment always describes a confirmed finding, so a rendered comment is always
attributed to the engine that verified it.

The template must contain exactly one tag mapped to the `marker` registry value.
GitHub does not display the HTML comment, but the marker remains in the comment
source.

## Rearrange the existing comment

Move tags and surrounding Markdown in `github-comment.md`. For example:

```markdown
### [severity]: [title]

> [problem]

**When:** [trigger]

**Result:** [impact]

**Recommended change:** [suggested-change]

_[done-by]_

[marker]
```

Tags may be omitted except for the required marker. Text returned for a tag is
inserted once and is not treated as another template expression.

Prefix a literal lowercase bracket label with a backslash, such as
`\[details]`, so it is not interpreted as a tag.

## Add or rename a tag

Edit `github-comment-tags.json`. The name on the left becomes the Markdown tag;
the value on the right must be one of the supported registry values from the
table above.

For example, add `[priority]` as an alias for `[severity]`, `[author]` as an
alias for `[done-by]`, and `[identity]` as the required marker:

```json
{
  "priority": "severity",
  "author": "reviewer_attribution",
  "identity": "marker"
}
```

Then use the aliases in the Markdown file:

```markdown
**[priority]: [title]**

[problem]

_[author]_

[identity]
```

You can create multiple aliases for the same registry value. A new computed
value cannot be created only through JSON; it requires a controller change so
the value can be validated and supplied safely.

## Keep personal formatting untracked

Copy the committed defaults to the ignored root filenames:

```sh
cp templates/github-comment.md github-comment.md
cp templates/github-comment-tags.json github-comment-tags.json
```

Point `.env` at the copies:

```dotenv
GITHUB_COMMENT_TEMPLATE_FILE=github-comment.md
GITHUB_COMMENT_TAGS_FILE=github-comment-tags.json
```

Relative paths resolve from the project root. These two root files are ignored
by Git, so personal formatting does not modify the public defaults.

## Validate a customization

Both files are loaded during startup. The reviewer rejects missing or unreadable
files, invalid JSON, unknown tags, unsupported registry values, excessive file
sizes, and templates without exactly one marker. Rendered comments are also
bounded before a GitHub review request begins.

Run a targeted dry review after editing either file:

```sh
python3 -m prwarden --repo your-org/example-app --pr 123
```

The command validates configuration without delivery, but it performs an actual
AI review and may consume model quota. Live delivery still requires both normal
delivery gates.

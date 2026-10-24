# Companion Usage review captures

These are captures of the native macOS Usage panel, rendered from this branch's
embedded Swift source with synthetic local sessions. They contain no user chat
content or real billing records.

- `usage.png`: Claude fixture with 966,000 latest input tokens against a
  1,000,000-token model reference window. Session totals remain a separate scope.
- `unavailable.png`: the same panel after changing the picker to a recently
  observed session whose tool does not expose measurable per-turn context.
  The prior session's meter is hidden, while indexed totals remain visible.

The interaction check opened the actual popover, fetched the internal loopback
usage route, selected a different session through the native picker, and checked
that the identity, headline, and meter changed together. The isolated preview
closed its own window and server after capture. These images capture the
popover's content view, without surrounding desktop/window chrome.

# PromptAudit — 30s demo tape (charmbracelet/vhs)
#
# Render: `vhs assets/demo.tape`  →  assets/demo.gif + assets/demo.cast
# Upload: `asciinema upload assets/demo.cast`

Output assets/demo.gif
Output assets/demo.cast

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 720
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set PlaybackSpeed 1.0
Set Padding 24

# --- intro ---
Type "# PromptAudit — catch prompt-injection in your dep tree"
Sleep 800ms
Enter
Sleep 600ms

# --- install (skip if already there) ---
Type "pipx install promptaudit  # (~15s, skipped in demo)"
Sleep 1s
Enter
Sleep 500ms

# --- scan the bundled jqwik fixture ---
Type "cd examples/jqwik-fixture"
Sleep 300ms
Enter
Sleep 400ms

Type "promptaudit scan ."
Sleep 600ms
Enter

# pause on the rich report so viewers can read the red CRITICAL line
Sleep 5s

# --- show machine-readable output for CI ---
Type "promptaudit scan . --json | jq '.findings[0]'"
Sleep 400ms
Enter
Sleep 4s

# --- show the rule corpus ---
Type "promptaudit rules | head -n 12"
Sleep 400ms
Enter
Sleep 4s

# --- closing card ---
Type "# github.com/supermario-leo/promptaudit"
Sleep 2s
Enter
Sleep 1500ms

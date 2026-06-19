# PromptAudit demo — rendered to assets/demo.gif by vhs (.github/workflows/demo.yml)
Output assets/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1100
Set Height 660
Set Theme "Catppuccin Mocha"
Set Padding 20

Type "promptaudit --version"
Enter
Sleep 1500ms

Type "promptaudit rules | head -n 20"
Enter
Sleep 2500ms

Type "promptaudit scan ."
Enter
Sleep 3500ms

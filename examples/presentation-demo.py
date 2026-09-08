import json
from pathlib import Path
from promptaudit.scanner import scan_text
from promptaudit.rules import load_rules
text=Path('tests/fixtures/jqwik_payload.txt').read_text()
findings=scan_text(text,package='demo-fixture',version='0',ecosystem='npm',source_file='fixture.txt',rules=load_rules())
print('input: tests/fixtures/jqwik_payload.txt; text is scanned, never executed')
for f in findings:print(json.dumps({'rule_id':f.rule_id,'severity':f.severity,'line':f.line}))

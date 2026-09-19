"""Run the shipped Improve rendering and request functions in Node."""
import json
import re
import shutil
import subprocess
import unittest

from aiwatcher_cli import ui


@unittest.skipUnless(shutil.which("node"), "Node is needed for frontend behavior tests")
class ImproveWebTests(unittest.TestCase):
    def test_actions_escape_evidence_and_send_scoped_feedback(self):
        source = ui._load_asset("index.js")
        functions = []
        for name in ("esc", "renderInsightRows", "recordImproveDecision", "renderImproveResults"):
            start = source.index(f"function {name}(")
            if source[max(0, start - 6):start] == "async ":
                start -= 6
            end = re.search(r"^(?:async function |function |let |const |class )", source[source.index("{", start) + 1:], re.M)
            stop = source.index("{", start) + 1 + end.start() if end else len(source)
            functions.append(source[start:stop])
        script = "\n".join(functions) + r'''
const calls = [];
globalThis.document = {getElementById: () => ({value: '7'})};
globalThis.fetch = async (url, options) => {
  calls.push({url, payload: JSON.parse(options.body)});
  return {ok: true, json: async () => ({})};
};
(async () => {
 const html = renderInsightRows([{id:'pace',title:'<script>private</script>',body:'A & B',
   evidence_key:'a'.repeat(64),action_label:'Review cost drivers',scope_note:'Selected window',
   session_id:'unsafe\" onclick=alert(1)',session_label:'same-name'}]);
 const results = renderImproveResults([{title:'Prepared',body:'Not completed',session_id:'x\" onclick=alert(1)'}]);
 await recordImproveDecision('a'.repeat(64), 'later');
 console.log(JSON.stringify({html,results,calls}));
})().catch(error => {console.error(error); process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertNotIn("<script>private", data["html"])
        self.assertIn("Review cost drivers", data["html"])
        self.assertIn("this.dataset.session", data["html"])
        self.assertNotIn('data-session="x" onclick=alert', data["results"])
        self.assertEqual(data["calls"], [{"url": "/api/improve-decision", "payload": {
            "insight_key": "a" * 64, "decision": "later", "days": 7}}])

"""Execute the dashboard's request and status helpers with a simulated network."""
import pathlib
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is needed for dashboard runtime tests')
class RefreshStatusRuntimeTests(unittest.TestCase):
    def test_request_timeout_errors_cache_and_polling(self):
        source = pathlib.Path(__file__).parents[1] / 'aiwatcher_cli/web/index.js'
        script = r'''
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const start = source.indexOf('function ' + name + '(');
  const end = source.indexOf('\n}\n', start);
  return source.slice(start, end + 3);
}
global.window = {setTimeout, clearTimeout};
global.document = {hidden: false};
const UPDATE_AUTO_CHECK_MS = 21600000;
const REFRESH_HIDDEN_MS = 60000, REFRESH_VISIBLE_MS = 10000;
const REFRESH_CATCHUP_MS = 1800, REFRESH_CATCHUP_FACTOR = 1.5;
let catchupDelay = 1800, state;
function setUpdateState(...args) { state = args; }
function clearCachedUpdateState() {}
eval(extract('classifyUpdateStatus'));
eval(extract('restoreCachedUpdateState'));
eval(extract('nextRefreshDelay'));
eval('async ' + extract('fetchDashboardJson'));
(async () => {
  assert.equal(classifyUpdateStatus({ok:false,install_kind:'missing'}), 'error');
  assert.equal(classifyUpdateStatus({ok:true,install_kind:'package'}), 'package');
  global.localStorage = {getItem: () => JSON.stringify({checkedAt:Date.now()-UPDATE_AUTO_CHECK_MS-1,data:{ok:true,install_kind:'source',repo:'/repo'}})};
  restoreCachedUpdateState({installKind:'source',sourceRoot:'/repo'});
  assert.equal(state[0], 'unknown');
  assert.equal(state[1], null);
  assert.equal(nextRefreshDelay({cache:{refreshing:true}},true), 1800);
  global.fetch = async () => ({ok:false,status:503});
  await assert.rejects(fetchDashboardJson('/summary',50), /503/);
  global.fetch = (_url, {signal}) => new Promise((resolve,reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  await assert.rejects(fetchDashboardJson('/summary',5), /aborted/);
  global.fetch = async () => ({ok:true,json:async()=>({ok:true})});
  assert.deepEqual(await fetchDashboardJson('/summary',50), {ok:true});
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

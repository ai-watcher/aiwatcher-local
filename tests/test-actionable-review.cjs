const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('aiwatcher_cli/web/index.js', 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const rest = source.slice(start);
  const end = rest.slice(1).search(/\n(?:async )?function /);
  return end < 0 ? rest : rest.slice(0, end + 1);
}
function render(status, extra={}) {
  const ctx = vm.createContext({
    URLSearchParams, location:{search:'?session=s1&review=context'},
    esc: x => String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    dateLabel: x=>x,
  });
  vm.runInContext(extract('renderSessionActions'),ctx);
  ctx.s={session_id:'s1',outcome:'useful',review:{status,title:'A precise next step',reason:'Observed condition',benefit:'A bounded checkpoint',...extra},actions:[]};
  return vm.runInContext('renderSessionActions(s)',ctx);
}
test('live warning remains actionable even after a useful outcome vote',()=>{
  const html=render('actionable');
  assert.match(html,/data-review-status="actionable"/);
  assert.doesNotMatch(html,/action-composer settled/);
  assert.match(html,/Preview continuation brief/);
  assert.match(html,/What you get/);
});
test('cleared warning rewards continuing, not changing chats',()=>{
  const html=render('clear');
  assert.match(html,/<button class="btn-primary" onclick="closeDrawer\(\)">Keep working/);
  assert.doesNotMatch(html,/<button class="btn-primary"[^>]*>Preview continuation brief/);
});
test('historical review is optional and preserves notification origin',()=>{
  const html=render('historical');
  assert.match(html,/action-composer settled/);
  assert.match(html,/Why Companion brought you here/);
  assert.match(html,/Review outcome/);
  assert.doesNotMatch(html,/Needs action|Inferred intent|Context AIWatcher will carry/);
});
test('unknown or advisory evidence does not force a fresh chat',()=>{
  assert.match(render('unknown'),/btn-primary" onclick="openUsageEvidence\(\)">Review available evidence/);
  assert.match(render('advisory'),/>Review my next prompt<\/button>/);
});
test('checking state exposes no premature handoff action',()=>{
  assert.doesNotMatch(render('checking'),/onclick="openHandoff/);
  assert.match(render('checking'),/role="status"/);
});
test('local signal text is escaped and completion boundaries remain visible',()=>{
  const html=render('actionable',{reason:'<img src=x onerror=alert(1)>',objective:'Confirm missing objective.',completion:'Copy is preparation, not completion.'});
  assert.doesNotMatch(html,/<img/);
  assert.match(html,/&lt;img/);
  assert.match(html,/Copy is preparation, not completion/);
});
test('detailed review leads with decision and keeps raw totals in evidence',()=>{
  const hero=extract('renderSessionHero');
  assert.doesNotMatch(hero,/session-hero-pressure/);
  assert.match(extract('renderSessionUsageTotals'),/tokens_scope_label/);
  assert.match(source,/Processed locally · includes transcript excerpts/);
  assert.match(source,/id="usageEvidence"/);
});

test('nearby commits remain distinct from session-linked receipts and delivery',()=>{
  const ctx=vm.createContext({survivalStatus:()=>null});
  vm.runInContext(extract('verdictLines'),ctx);
  ctx.s={outcome_evidence:{commits:[{},{}],commit_attribution:'nearby_time_window'}};
  assert.match(vm.runInContext('verdictLines(s)',ctx).find(x=>x.key==='worth').body,/2 nearby commits.*does not prove/);
  ctx.s.outcome_evidence.commit_attribution='session_bound';
  assert.match(vm.runInContext('verdictLines(s)',ctx).find(x=>x.key==='worth').body,/2 session-linked commits.*delivery is checked separately/);
});


test('internal approval reviews offer optional diagnostics without developer coaching',()=>{
  const html=render('internal',{parent_session_id:'parent',parent_note:'Recorded parent'});
  assert.match(html,/Review available evidence/);
  assert.match(html,/No action needed/);
  assert.match(html,/data-parent="parent"/);
  assert.doesNotMatch(html,/openHandoff|Review outcome|Review my next prompt/);
  assert.doesNotMatch(render('internal'),/Inspect recorded parent/);
});

test('workspace snapshots are explicitly unattributed and unavailable prices stay visible',()=>{
  assert.match(extract('renderEvidence'),/Workspace changes \(unattributed\)/);
  assert.match(extract('renderEvidence'),/not session attribution/);
  const ctx=vm.createContext({esc:x=>String(x??'')});
  vm.runInContext(extract('renderSessionUsageTotals'),ctx);
  ctx.s={tokens_label:'82.4k',api_value:'Unavailable (no list price)'};
  const html=vm.runInContext('renderSessionUsageTotals(s)',ctx);
  assert.match(html,/Unavailable \(no list price\)/);
  assert.doesNotMatch(html,/\$0\.00/);
});

test('internal review decision and optional evidence fit desktop and narrow screens', async()=>{
  const {chromium}=require('playwright-core');
  const browser=await chromium.launch({headless:true,...(fs.existsSync(chromium.executablePath())?{}:{executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'})});
  try {
    const page=await browser.newPage();
    const html=render('internal',{
      title:'Internal approval review — no developer action established',
      reason:'Codex session metadata identifies a Guardian approval reviewer.',
      benefit:'Check the parent conversation for an actual approval request. Otherwise no action is needed; diagnostics remain available.',
      parent_session_id:'parent',parent_note:'Parent relationship recorded by Codex; the indexed parent is available.',
    });
    for (const width of [1100,390,320]) {
      await page.setViewportSize({width,height:900});
      await page.setContent(`<style>${fs.readFileSync('aiwatcher_cli/web/index.css','utf8')}body{padding:16px}.session-review-shell{max-width:780px;margin:auto}</style><div class="session-review-shell"><section class="detail-section"><h2>Session review</h2><p>Internal approval review · host metadata</p></section>${html}<section class="detail-section"><details id="usageEvidence"><summary>Usage and context evidence</summary><p>Unavailable (no list price). Workspace changes are not session attribution.</p></details></section></div>`);
      await page.addScriptTag({content:extract('openUsageEvidence')});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.getByRole('button',{name:'Review available evidence'}).click();
      assert.equal(await page.locator('#usageEvidence').getAttribute('open'),'');
      assert.equal(await page.getByRole('button',{name:'Preview continuation brief'}).count(),0);
      if (process.env.AIWATCHER_CAPTURE_REVIEWS && width!==320) {
        await page.screenshot({path:`docs/pr-screenshots/actionable-session-review/internal-${width}.png`,fullPage:true});
      }
    }
  } finally {await browser.close();}
});

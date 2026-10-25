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

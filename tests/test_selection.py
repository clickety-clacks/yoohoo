"""Exercise the exact JavaScript selection policy loaded by QML (Node required)."""
from pathlib import Path
import subprocess
import unittest


class SelectionTests(unittest.TestCase):
    def test_selection_policy(self):
        source = Path(__file__).resolve().parents[1] / "payload/Selection.js"
        subprocess.run(["node", "-e", r'''
const fs = require('node:fs'), vm = require('node:vm'), a = require('node:assert/strict');
const s = {}; vm.createContext(s); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), s);
const w = ['a','b','c'].map(address => ({address}));
a.equal(s.key({address:'0x1'}), '0x1');
a.equal(s.key({id:'agent-a', address:''}), 'agent-a');
a.equal(s.key({id:'agent-b'}), 'agent-b');
a.equal(s.activation({kind:'agent', id:'agent-a', address:''}), 'open');
a.equal(s.activation({kind:'agent', id:'agent-a', address:'0xremote'}), 'open');
a.equal(s.activation({kind:'agent', address:'0xremote'}), 'focus');
a.equal(s.activation({kind:'window', id:'native-id', address:'0xlocal'}), 'focus');
a.equal(s.iconKind({kind:'agent', open_on_machine:false}), 'connect');
a.equal(s.iconKind({kind:'agent', open_on_machine:true}), 'window');
a.equal(s.iconKind({address:'0xlocal'}), 'window');
a.equal(s.canActivate({kind:'agent', id:'agent-a', address:''}, true, true), true);
a.equal(s.canActivate({kind:'agent', id:'agent-a', address:''}, true, false), false);
a.equal(s.canActivate({kind:'agent', id:'agent-a', address:'', connection_available:false}, true, true), false);
a.equal(s.canActivate({kind:'window', id:'native-id', address:''}, false, true), false);
a.equal(s.canActivate({kind:'window', id:'native-id', address:'0xlocal'}, false, true), true);
a.equal(s.actionFailure({kind:'agent', machine:'gibson', open_on_machine:false}), 'Could not connect to gibson');
a.equal(s.actionFailure({kind:'agent', machine:'gibson', open_on_machine:true}), 'Could not focus agent on gibson');
a.equal(s.actionFailure({kind:'window', address:'0xlocal'}), 'Could not open window');
a.equal(s.agentCount([{kind:'agent'}, {address:'0xlocal'}]), 1);
a.match(s.plainReason('agent_has_no_tmux_session', {}), /not running in a tmux session/);
a.match(s.plainReason('source_not_reached', {machine:'gibson'}), /Agentd on gibson is not reporting/);
a.equal(s.plainReason('made_up_code', {}), '');
a.equal(s.failureReason({kind:'agent', machine:'osanwe', open_on_machine:false, unavailable_reason:'agent_has_no_tmux_session'}, ''),
  'This agent is not running in a tmux session, and Yoohoo could not find a window for it.');
a.equal(s.failureReason({kind:'agent', machine:'gibson', open_on_machine:false}, ''), 'Could not connect to gibson.');
a.equal(s.failureReason({kind:'window'}, JSON.stringify({reason:{code:'candidate_count', message:'x'}})),
  'Yoohoo could not single out one window for this agent and will not guess.');
a.equal(s.failureReason({kind:'window'}, JSON.stringify({reason:{code:'odd', message:'window vanished mid-focus', retryable:true}})),
  'Window vanished mid-focus. Trying again may work.');
a.equal(s.failureReason({kind:'window'}, 'garbage output'), 'Could not open window. garbage output');
a.ok(!/agent_has_no_tmux_session/.test(s.failureReason({kind:'agent', unavailable_reason:'agent_has_no_tmux_session'}, '')));
const now = 100000;
const live = {enabled:true, configured:true, status:'live', connected:true,
  lastSnapshotAtUnixMs:90000, lastSeenAtUnixMs:99000};
a.equal(s.healthStatus(live, now), 'live');
a.equal(s.hubIsLive(live, now), true);
a.equal(s.hubStatusSummary(live, now), 'Hub live');
a.equal(s.healthStatus({...live, lastSnapshotAtUnixMs:1000, lastSeenAtUnixMs:99000}, now), 'live');
a.equal(s.healthStatus({enabled:true, configured:true, status:'connecting', connected:true}, now), 'connecting');
a.equal(s.healthStatus({...live, status:'reconnecting'}, now), 'reconnecting');
a.equal(s.healthStatus({...live, status:'stale'}, now), 'stale');
a.equal(s.healthStatus({...live, status:'suspended'}, now), 'suspended');
a.equal(s.healthStatus({enabled:false, configured:false, status:'disabled'}, now), 'disabled');
a.equal(s.healthStatus({...live, lastSnapshotAtUnixMs:100001}, now), 'stale');
a.equal(s.healthStatus({...live, lastSnapshotAtUnixMs:99500, lastSeenAtUnixMs:99000}, now), 'stale');
a.equal(s.healthStatus({...live, lastSnapshotAtUnixMs:90000.5}, now), 'stale');
a.equal(s.healthStatus({...live, lastSeenAtUnixMs:54000}, now), 'stale');
a.equal(s.healthStatus({...live, lastSnapshotAtUnixMs:'90000'}, now), 'stale');
a.equal(s.healthStatus({...live, lastSeenAtUnixMs:NaN}, now), 'stale');
a.equal(s.healthStatus({enabled:true, connected:true}, now), 'stale');
a.equal(s.healthStatus({configured:true, status:'live', connected:true,
  lastSnapshotAtUnixMs:90000, lastSeenAtUnixMs:99000}, now), 'disabled');
a.equal(s.healthStatus({...live, connected:false}, now), 'stale');
a.equal(s.barHealthMarker({...live, status:'connecting'}, now), ' …');
a.equal(s.barHealthMarker({...live, status:'reconnecting'}, now), ' !');
a.equal(s.barHealthMarker({...live, status:'stale'}, now), ' !');
a.equal(s.barHealthMarker({...live, status:'suspended'}, now), ' !');
a.equal(s.barHealthMarker(live, now), '');
a.equal(s.hubNeedsAttention({...live, status:'stale'}, now), true);
a.equal(s.hubNeedsAttention({...live, status:'connecting'}, now), false);
a.equal(s.hubWarning({...live, status:'reconnecting'}, [{kind:'agent'}], now), 'Hub reconnecting — snapshot 10s old — showing last-known rows; remote agents are unavailable.');
a.equal(s.hubWarning({...live, status:'stale', error:'offline'}, [{kind:'agent'}], now), 'Hub stale — snapshot 10s old — offline — showing last-known rows; remote agents are unavailable.');
a.equal(s.hubWarning({...live, status:'stale'}, [], now), 'Hub stale — snapshot 10s old — remote agents are unavailable.');
a.equal(s.hubWarning({...live, status:'live', error:'degraded'}, [{kind:'agent'}], now), 'Hub warning: degraded');
a.equal(s.hubWarning({enabled:false, connected:false, error:'ignored'}, [{kind:'agent'}], now), '');
a.equal(s.unavailableReason({...live, status:'stale'}, now), 'Hub stale; snapshot 10s old; this is a last-known row');
const knownAgent = {id:'agent-a', kind:'agent', title:'ask · gibson'};
const native = {address:'0xlocal', title:'Local'};
a.deepEqual(s.retainLastKnownRows([knownAgent, native], [native], 'stale'), [native, knownAgent]);
a.deepEqual(s.retainLastKnownRows([knownAgent], [], 'live'), []);
a.deepEqual(s.retainLastKnownRows([knownAgent], [], 'disabled'), []);
a.equal(s.step([], '', 1), '');
a.equal(s.step(w, '', 1), 'a');
a.equal(s.step(w, '', -1), 'c');
a.equal(s.step(w, 'a', 1), 'b');
a.equal(s.step(w, 'c', 1), 'a');
a.equal(s.step(w, 'a', -1), 'c');
a.equal(s.step([w[0]], 'a', 1), 'a');
a.equal(s.reconcile(w, [w[2],w[1],w[0]], 'b'), 'b');
a.equal(s.reconcile(w, [w[0],w[2]], 'b'), 'c');
a.equal(s.reconcile(w, [w[0]], 'c'), 'a');
a.equal(s.reconcile(w, [], 'c'), '');
const mixed = [
  {address:'0x1', title:'Local'},
  {id:'agent-a', kind:'agent', address:'', machine:'gibson', open_on_machine:false},
  {id:'agent-b', kind:'agent', address:'', machine:'nacelle', open_on_machine:true}
];
a.equal(s.indexOf(mixed, 'agent-a'), 1);
a.equal(s.step(mixed, '0x1', 1), 'agent-a');
a.equal(s.step(mixed, 'agent-a', 1), 'agent-b');
a.equal(s.step(mixed, 'agent-b', 1), '0x1');
a.equal(s.reconcile(mixed, [mixed[2],mixed[0],mixed[1]], 'agent-a'), 'agent-a');
a.equal(s.reconcile(mixed, [mixed[0],mixed[2]], 'agent-a'), 'agent-b');
a.equal(s.indexOf([{kind:'agent', address:''}], ''), -1);
// Exercise the exact busy-state helpers used by the QML menu.
const panel = fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[1]), 'Panel.qml'), 'utf8');
for (const name of ['isPendingActivation', 'activationStatus']) {
  const fn = panel.match(new RegExp('function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}'));
  a.ok(fn, name);
  vm.runInContext(fn[0], s);
}
s.root = {rowKey:s.key, isAgent:s.isAgent, pendingActivation:null};
s.root.isPendingActivation = s.isPendingActivation;
a.equal(s.isPendingActivation(mixed[1]), false);
s.root.pendingActivation = {...mixed[1]};
a.equal(s.isPendingActivation({...mixed[1]}), true);
a.equal(s.isPendingActivation(mixed[2]), false);
a.equal(s.activationStatus({...mixed[1], open_on_machine:true}), 'Connecting…');
s.root.pendingActivation = {...mixed[0]};
a.equal(s.activationStatus(mixed[0]), 'Opening…');
s.root.pendingActivation = null;
a.equal(s.activationStatus(mixed[0]), '');
a.ok(panel.includes('running: activationPending && root.opened'));
// Exercise applyPayload's outage safety. A missing list retains the roster,
// leaves the error visible, and must not finish a pending acceptance. A first
// payload containing agent rows without Hub health is stale and unavailable,
// even when the prior/root Hub state was disabled.
for (const name of ['normalizedHub', 'staleHubState', 'applyPayload', 'finishAccept']) {
  const fn = panel.match(new RegExp('function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}'));
  a.ok(fn, name);
  vm.runInContext(fn[0], s);
}
s.Selection = s;
s.windows = [mixed[1]];
s.hub = {enabled:true, configured:true, status:'live', connected:true,
  lastSnapshotAtUnixMs:90000, lastSeenAtUnixMs:99000};
s.errorText = 'previous error';
s.actionErrorText = '';
s.accepting = true;
s.pendingSteps = [];
s.selectedAddress = 'agent-a';
s.root = {nowMs:now, pendingActivation:null, close:() => { s.closed = true; }};
Object.defineProperties(s.root, {
  hub: {get:() => s.hub, set:(value) => { s.hub = value; }},
  hubConfigured: {get:() => !!s.hub && s.hub.configured === true},
  hubConnected: {get:() => !s.root.hubConfigured || s.Selection.hubIsLive(s.hub, s.root.nowMs)}
});
s.selectedRow = () => s.windows.length ? s.windows[0] : null;
s.rowCanActivate = row => s.Selection.canActivate(row, s.root.hubConfigured, s.root.hubConnected);
s.rowUnavailableReason = () => 'Hub stale; this is a last-known row';
s.activateRow = () => { s.dispatches = (s.dispatches || 0) + 1; };
s.failedRow = null;
s.showFailure = (row) => { s.failedRow = row; };
s.closed = false;
vm.runInContext('Date.now = () => 100000;', s);
s.applyPayload(JSON.stringify({hub:s.hub}));
a.equal(s.windows.length, 1);
a.equal(s.windows[0].id, 'agent-a');
a.equal(s.errorText, 'Attention state is incomplete');
a.equal(s.accepting, false);
a.equal(s.hub.status, 'stale');
a.equal(s.closed, false);
a.equal(s.dispatches || 0, 0);
s.windows = [];
s.hub = {enabled:false, configured:false, status:'disabled', connected:false};
s.errorText = '';
s.actionErrorText = '';
s.accepting = true;
s.applyPayload(JSON.stringify({windows:[mixed[1]]}));
a.equal(s.hub.status, 'stale');
a.equal(s.hub.configured, true);
a.equal(s.accepting, false);
a.equal(s.dispatches || 0, 0);
// An unavailable row lands in the failure card rather than a bare error line.
a.equal(s.failedRow && s.failedRow.id, 'agent-a');
a.equal(s.dispatches || 0, 0);
// A response can arrive between timer ticks. Refreshing the local receipt
// clock lets a payload newer than the old timer value remain live, while the
// existing future and heartbeat-expiry checks still reject invalid/stale data.
vm.runInContext('Date.now = () => 101000;', s);
s.root.nowMs = now;
s.windows = [];
s.hub = {enabled:false, configured:false, status:'disabled', connected:false};
s.errorText = '';
s.actionErrorText = '';
s.accepting = false;
s.applyPayload(JSON.stringify({windows:[mixed[1]], hub:{
  enabled:true, configured:true, status:'live', connected:true,
  lastSnapshotAtUnixMs:100500, lastSeenAtUnixMs:100900
}}));
a.equal(s.root.nowMs, 101000);
a.equal(s.Selection.healthStatus(s.hub, s.root.nowMs), 'live');
a.equal(s.root.hubConnected, true);
a.equal(s.Selection.snapshotAgeMs(s.hub, s.root.nowMs), 500);
a.equal(s.Selection.ageTextMs(s.Selection.snapshotAgeMs(s.hub, s.root.nowMs)), '0s old');
s.applyPayload(JSON.stringify({windows:[mixed[1]], hub:{
  enabled:true, configured:true, status:'live', connected:true,
  lastSnapshotAtUnixMs:101001, lastSeenAtUnixMs:101001
}}));
a.equal(s.Selection.healthStatus(s.hub, s.root.nowMs), 'stale');
a.equal(s.root.hubConnected, false);
s.applyPayload(JSON.stringify({windows:[mixed[1]], hub:{
  enabled:true, configured:true, status:'live', connected:true,
  lastSnapshotAtUnixMs:55900, lastSeenAtUnixMs:55999
}}));
a.equal(s.Selection.healthStatus(s.hub, s.root.nowMs), 'stale');
a.equal(s.root.hubConnected, false);
const stream = {next:1, pending:{}};
a.equal(JSON.stringify(s.enqueue(stream, 2, 'accept')), '[]');
a.equal(JSON.stringify(s.enqueue(stream, 1, 'next')), '["next","accept"]');
a.equal(JSON.stringify(s.enqueue(stream, 2, 'accept')), '[]');
a.equal(JSON.stringify(s.enqueue(stream, 4, 'previous')), '[]');
a.equal(JSON.stringify(s.enqueue(stream, 3, 'next')), '["next","previous"]');
a.equal(JSON.stringify(s.enqueue(stream, 9999, 'next')), '[]');
''', str(source)], check=True)

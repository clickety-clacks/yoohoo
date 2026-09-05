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
const stream = {next:1, pending:{}};
a.equal(JSON.stringify(s.enqueue(stream, 2, 'accept')), '[]');
a.equal(JSON.stringify(s.enqueue(stream, 1, 'next')), '["next","accept"]');
a.equal(JSON.stringify(s.enqueue(stream, 2, 'accept')), '[]');
a.equal(JSON.stringify(s.enqueue(stream, 4, 'previous')), '[]');
a.equal(JSON.stringify(s.enqueue(stream, 3, 'next')), '["next","previous"]');
a.equal(JSON.stringify(s.enqueue(stream, 9999, 'next')), '[]');
''', str(source)], check=True)

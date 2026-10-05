# Hub row activation blocked by unrelated desktop processes

Observed on the desktop, September 14, 2026: selecting a Hub row did not activate
it. Read-only resolver diagnostics verified the remote agent and tmux pane,
but reported incomplete local window scans. Activation correctly refused to
use incomplete evidence, but the UI did not explain the refusal.

The local collector had three problems:

- Its 128-character descriptor-link limit rejected ordinary desktop paths.
- It tried to read descriptor tables of exited, unreaped processes (zombies).
- It collected socket evidence from every process, although the matcher only
  uses local socket evidence from supported SSH/mosh transport candidates.
  A protected 1Password browser helper therefore blocked unrelated terminals.

The correction retains full process identity, argument, and descendant checks.
Socket collection and matching share the same transport eligibility predicate;
unreadable eligible transports still fail closed. Only repeatedly verified,
childless, non-root zombies may be omitted. PID reuse remains an error.
Descriptor links remain bounded, now at 4096 characters.

CLI activation failures now include a bounded structured reason on stderr,
which the menu can display without dismissing the unresolved alert.

Read-only diagnostics against the corrected collector on the desktop reached the
ordinary no-matching-window result for all three reported rows, and independently
verified each remote target. These checks did not focus, launch, attach, or
acknowledge those rows. They do not establish roaming-mosh existing-window
matching, which remains unsupported.

Independent static review approved the correction. Parent validation on the test machine
passed 68 core tests, 77 Yoohoo tests, and the live private-SSH gate. The corrected
bundle and failure-reporting menu were installed on the desktop; Yoohoo and the shell
were restarted, and the updated menu reports a connected Hub. No user terminal
or tmux session was restarted for deployment. A user click through the updated
menu remains the final end-to-end confirmation.

Test-procedure incident: a reviewer ran unit tests on the desktop despite the
test-machine-only instruction. That run used a separately named disposable tmux
server and loopback HTTP fixtures, not the user's existing server. Mike was
informed, local test execution was stopped, and the relevant suite was rerun
on the test machine. The validation counts above refer to the parent's test machine runs.

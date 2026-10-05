# Bundled resolver verification

The resolver is maintained as Python standard-library source and copied into
Yoohoo's distribution. It is not a separately installed service or command.
This record distinguishes library evidence from desktop behavior.

This records the initial bundled-library acceptance. A subsequent real-desktop
scan correction and its newer test results are recorded in
[desktop-scan-failure.md](desktop-scan-failure.md).

## Verified on the test machine, September 14, 2026

- All 55 shared-core tests passed in an isolated copy.
- Ask's real Node-process test passed using its bundled Python helper: local
  resolve, revalidate, and rejection of a different process start time. The
  test did not focus windows, attach to sessions, or contact remote machines.
- Yoohoo's three staged-install tests passed: all seven installed core modules
  matched the bundled source byte for byte, and the installed adapter resolved
  and revalidated its own live Python process without the development checkout
  or `PYTHONPATH`.
- The opt-in private SSH integration test passed against this same core. It
  used a disposable loopback sshd and private tmux server, proving real TCP
  endpoint reversal and exact visible-pane matching. Its candidate window
  was the SSH client process, not Ghostty; this is not desktop ancestry proof.
- All four private-tmux integration tests passed, including real bundled-core
  resolve/revalidate of an active pane through an attached PTY client. The
  fixture used its own temporary default-socket directory and cleaned up its
  own server; it did not operate on the user's normal tmux server.
- The complete Yoohoo top-level regression suite passed: 75 tests, including
  the staged-install and private-tmux tests above. This corrected the native
  clearing regression and migrated the legacy matcher-dependent tests.
- Sol reviewed the two collector corrections: direct local targets no longer
  scan unrelated ancestors; self-inspection excludes the directory iterator's
  own file descriptor. Other unreadable or vanished required evidence still
  fails closed.

The final reviewed core hashes are:

- `collector.py`: `27e9340020ab06a964ce822c3ba492aa08b599a8487164cd9b802e208f6f025f`
- `linux.py`: `29869743305a4365b2016222f2032a41cec4a8b828f7f175ecfcdf9ec2e18e35`
- `resolver.py`: `629166bfa195f79050da4b55c466eb18d7cf8157dc614233be0014cb33c790cd`

All seven Python modules match the maintained source and the tested Yoohoo
bundle byte for byte.

The SSH gate is the canonical library's
`tests/integration/test_private_ssh_match.py`, with
`test_private_producer.py` and Yoohoo's existing `connection_fixture.py` /
`private_sshd_fixture.py` test support. It was run from an independent temporary
copy on the test machine with `YOOHOO_LIVE_TEST_HOST=1`; no canonical checkout was changed.

## Source integration accepted

The reviewed adapter passes its worker and action checks. Its opt-in live
Ghostty test also passed with a real sleep descendant, a real focus change,
no duplicate terminal, and verified owned-process/window cleanup.

An intermittent private-tmux revalidation failure was traced to target ancestry
collection continuing beyond the exact pane into a changing tmux server. The
collector now stops at the independently verified pane identity. The parser
and matcher reject invalid, stale, unmarked, or cyclic boundary evidence.
Ten repeated private-tmux runs passed after the boundary correction; the final
strict-boolean validation then passed the complete 75-test suite. Sol approved
the final core and adapter. Parent verification reran all 55 core tests, all
75 Yoohoo tests, the private SSH gate, Ask's real local-process test, and the
real Ghostty action test successfully against the final core.

These checks do not establish ordinary roaming-mosh support, which remains
explicitly deferred from this library-bundling change and owned by Yoohoo.
They also do not establish Ghostty-over-SSH desktop ancestry support.

No desktop installation, service restart, or user tmux changes were part of
these checks. The test machine tests used isolated temporary installations.

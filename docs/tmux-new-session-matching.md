# Existing windows launched with `tmux new-session -A`

The Pimcamp duplicate-window report exposed a missing command form in the
bundled resolver, not a lack of identifying information in the terminal.
Both windows had the generic title `mosh`, but their process arguments
contained the host and tmux session:

```text
Existing: mosh-client -# -- atlas tmux new-session -A -s pimcamp |
New:      mosh-client -# -- atlas sh -lc exec tmux attach-session -t =pimcamp |
```

The old collector (`c080b305…`) rejected the existing window's `-A` option.
Replaying both actual argument vectors on Testbed returned no hint for the
existing window and a `atlas/pimcamp` hint for the new one. Removing only
`-A` from the first vector made the old parser recognize it.

[`tmux` documents `-A`](https://man.openbsd.org/tmux.1#new-session) as attaching
to the named session when it already exists. That ordinary command form
must provide the same best-effort host/session evidence as an explicit
attach command. It does not require a harness hook, new transport probe,
or separate resolver installation.

The canonical maintained source is `agent-window-resolver`'s `collector.py`;
Yoohoo and Ask bundle copies. Changes here must preserve ordinary spaced
session names and mosh's flattened `sh -lc` display commands as well as
socket-name/path selectors. Matching remains best-effort, not proof that a
historical launch command still describes the active remote pane.

## Verification and installation

Final collector SHA-256:
`42db2f45f002467dc0f22428ae17951a4082a89307eb3f5c81849ca791239bc5`.
The identical canonical and Yoohoo regression file has SHA-256
`aeecfd8036a36709942adde883d7c238b1510bed7cae8fce4fea273cea950f4a`.

- Independent Sol high static review approved this exact pair.
- On Testbed, **90 core tests and 108 Yoohoo tests passed**. The new tests
  replay the actual old/new Pimcamp mosh argument vectors with generic
  `mosh` titles and require both windows to be returned as candidates.
- An additional controlled Testbed replay exercised production
  `AttentionService.open_target`, the bundled adapter, and real matcher.
  Compositor/process observations were injected, with the older `-A`
  window marked most recent. It selected that window, acknowledged the
  claim, and made zero subprocess launches. Focus was mocked: this was
  not a live mosh connection or desktop activation test.
- No runtime tests or artificial attention signals were run on lumen.

Installed the bundled collector on lumen at **11:45 PM PT, September 14,
2026**. Only Yoohoo's tracker was restarted; its listeners started and its
Hub status returned connected with no error. The daemon's earlier breathing
border fix and all animation configuration were left unchanged.

The prior collector and install manifest are backed up under
`~/.local/state/yoohoo/backups/tmux-new-session.HV9vybpJ/`. The install
manifest now records the new collector hash. Ask received the canonical
paths, hashes, and test evidence for its own bundled update; this task did
not edit or deploy Ask. No release was cut or pushed.

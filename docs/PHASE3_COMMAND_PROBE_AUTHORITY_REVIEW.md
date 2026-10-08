# CLI preparation probe release-authority repair

This is one repair from the whole-phase adversarial review. It does not close
the final signed-artifact, public-flow, live-provider or whole-phase gates.

## Reproduced failure

The shared preparation runner supplied `attempt.check` to
`CommandPreparationOwner`. That callback checked cancellation and deadline,
while the callback used by the other preparation owners also checked the held
release admission. The frozen command owner correctly calls its supplied
callback at session, invocation and final native-creation boundaries.

Replacing the reviewed release policy or outer app seal after the verifier's
initial full artifact check therefore left the CLI probe's final native-create
check admitted. Replacing the policy with identical bytes at a new inode had
the same result. Later verification/publication checks rejected the job, but
that rejection came after the source command could already have executed.

This concerns preparation's command probe. The separate installed-command
`ReleasedHostCommands` wrapper already retains its release-artifact checks.

## Repair

The shared runner now supplies its existing `current` callback to the command
probe owner. That callback checks both the attempt and `_require_admission`,
including the original held artifact's synchronous critical identities. Full
artifact checks remain joined and off the event loop at verifier entry and
publication. No proof is reloaded or recaptured. Cleanup continues after
revocation and uses the same ownership journals and reservation settlement.

Only `backend/meecp/preparation_runner.py` changes production behavior, by one
callback argument. No published CLI, agent, resolved-Python or ONNX component
module, helper, historical recipe, signed asset or original app is modified.
The final TextGen backend closure must be minted centrally after this change;
an already published recipe must not be restamped.

## Independent controls

`tests/test_preparation_command_release_fence.py` adds seven cases:

- Three execute the production runner's command-owner construction with a real
  SQLite preparation job, replace policy/seal/policy inode after the full
  verifier-entry check, and require the supplied callback to reject.
- Four connect that exact runner-supplied callback to a real command probe
  owner and compiled unsigned helper. The unchanged positive executes the
  synthetic command. Each negative changes authority in the final synchronous
  `before_native_create` callback and proves zero `Popen` calls, completed
  ownership, zero pending/reserved units, no attachment and released job
  resources. The positive proves actual creation and successful attachment.

The tests explicitly substitute family routing, package construction and the
enclosing app's signature responses. They use the actual held artifact proof,
runner, job database, command owner and native executor. Completion markers
are asserted outside the runner, so its normal exception-to-failed-job handling
cannot turn a failed assertion into a passing negative test.

Running the exact original runner Git blob in memory produced six failures and
one passing positive. The original runner bytes also matched the integration
tree at reproduction. The repaired focused run passed 110 cases with five
explicit private-host opt-in skips, including runner cancellation/publication,
command ownership/recovery, public admission and current-artifact controls.
Raw process/test output remained in memory; the private exclusive receipt
contains only source hashes, counts, fixed test names and scope metadata.

## Remaining review scope

Generic noncritical host-member changes remain subject to full artifact checks;
the synchronous native callback does not hash the entire host on the event
loop. The final public candidate still needs its installed/relocated native and
SDK flows, source and package mutation controls, joined cancellation/restart
checks, and two-build command-helper continuity. Live agent acceptance needs
the user's selected provider/model and deliberately entered configuration.
Resource claims retain their measured same-user scope: this repair adds no
hard aggregate or lifetime enforcement claim.

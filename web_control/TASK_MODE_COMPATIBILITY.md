# Optional A90 request compatibility

The default `legacy` format preserves the original observation request: no
`task_mode` or `phase_entry_action` is sent. The web connection settings offer:

- Standard (`legacy`, default).
- Task mode (`task_mode`): sends the task mode; accepts an optional entry override.
- A90 (`a90`): sends both fields and accepts only single-left and dual-continuous
  tasks (custom API tasks use `twohands`). Independent right-hand and dual-separate
  tasks are rejected before any inference or execution starts.

Select the format and reconnect. A format change invalidates dry-run readiness.
The probe operation preserves the selected format; process restart restores the
default. This executor uses non-RTC requests; this change does not add RTC execution.

## Wire format and reference lifecycle

Both fields are at the top level, alongside `type`, `prompt`, and `observation`:

- `task_mode`: `lefthand` or `twohands` in A90 mode.
- `phase_entry_action`: 23 finite numbers in the same wire order/units as state.

In A90 mode, leaving the optional entry override blank captures the initial
real state. Dry-run uses its first observation. Actual execution captures a fresh
state from `begin_policy_session`; all subsequent requests reuse that fixed
reference, without following feedback or predicted actions. Each new run captures
a fresh reference. No all-zero placeholder is substituted.

Automatic initial-state capture is the client's interpretation of the phase-entry
reference; a service requiring a different reference can use the explicit override.
The override is copied, validated, and sent with every request. Changing it requires
a new dry-run and the start plan must match. Request/response ordering and action
execution constraints remain unchanged.

The generic task_mode format additionally supports `righthand` for other servers;
A90 specifically does not. Generic dual-separate tasks send left then right modes.

## API

`POST /api/pi05/reconnect`: optional `request_format` (`legacy`, `task_mode`, `a90`).
`POST /api/pi05/dry-run` and `/api/pi05/start`: optional `phase_entry_action` array.
Status includes `request_format`, configured `phase_entry_action` (null for auto),
and `effective_phase_entry_action` (the actual frozen reference).
Legacy format rejects a supplied entry override instead of silently dropping it.

## Verification

Offline tests cover legacy schema preservation, optional fields, finite 23-D
validation, HTTP forwarding, mode mapping, unsupported A90 task rejection,
reference copying, changed-plan rejection, fresh start capture and reference
stability across chunks.

On 2026-09-16, read-only requests using the current robot feedback and three camera
frames were sent to 192.168.6.47:9977. Both `lefthand` and `twohands` returned valid
finite 50x23 action chunks. Returned actions were not sent to the robot. This checks
protocol acceptance, not physical task success or RTC behavior.

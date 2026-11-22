# ticketctl: platform checklist

This is the hand-run checklist for the platform behaviors that the Linux CI cannot prove (issue #39). The rules live in [interface.md](interface.md) and the decisions in [design.md](design.md#platforms); this file lists the checks, the exact commands and the expected result, and holds the table where each run is recorded. Nothing here is a finding until a person has run it and written the result in [Results](#results).

Conventions: run the commands from a checkout of the repository with the dev environment installed (`uv sync`). `ticketctl` stands for `uv run ticketctl`. Every path is a throwaway directory made by `mktemp -d`. Use a throwaway ticket and a throwaway state directory (`XDG_STATE_HOME`) for anything that writes; never point the write checks at a real ticket. Never paste a token, a site name or a ticket key into the results.

The Linux column says what the CI already covers: `CI` means a test that runs on every push (Linux, Python 3.12 and 3.13), `sim` means the test only simulates the platform behavior (a patched function or a fake), `none` means no test reaches it. A `CI` row is repeated here only because the macOS behavior can differ.

## How to run

1. Create the scratch area once: `export TC_SCRATCH="$(mktemp -d)"` and `export XDG_STATE_HOME="$TC_SCRATCH/state"`.
2. Copy `config.example.yaml` to `$TC_SCRATCH/config.yaml`, point `sites.*.url` at a real test site you own, set `token_env` to a variable you export for the run, and `chmod 600` the file. Use `--config "$TC_SCRATCH/config.yaml"` on every command below.
3. Work through the sections in order and fill in [Results](#results). A failed check is recorded as it happened; the fix is a separate issue.

## 1. Test suite on the platform

| ID | Check | Command | Expected | Linux |
|---|---|---|---|---|
| T1 | The whole suite passes on macOS, Python 3.12 and 3.13 | `uv run pytest -q` (twice, once per interpreter: `uv run --python 3.13 pytest -q`) | Every test passes or is skipped with a visible reason; none is skipped silently | CI |
| T2 | The hook process tests | `uv run pytest -q tests/test_hook_process.py tests/test_hooks.py` | All pass, with no stray process left behind (`pgrep -fl sleep` shows none of the tests' children afterwards) | CI, with the waitid path |
| T3 | A hook that ignores SIGTERM | Part of T2 (`test_a_hook_that_ignores_sigterm_is_killed_after_the_grace`) | The hook's group is killed after the 2 s grace | CI |
| T4 | A hook that forks and detaches | Part of T2 | A process the hook left in its group is killed with the group; one that started its own session survives | CI |
| T5 | SIGTERM and SIGHUP during a running `pre-` hook | Part of T2 | The run ends, the hook's group is gone, nothing was sent | CI |
| T6 | The hook runner without `waitid(WNOWAIT)` | `uv run python -c "import os; print(hasattr(os, 'waitid'), hasattr(os, 'WNOWAIT'), hasattr(os, 'pidfd_open'))"` | On macOS the first value is expected to be `False` (no `waitid`), so the runner takes the `Popen.poll` path and T2 passing is the real proof. Record the three values | sim (the poll path is exercised by patching) |
| T7 | Python 3.13 signal restoration | T2 on 3.13 | The SIGINT, SIGTERM and SIGHUP handlers are back to their previous values after a hook run | CI (Linux only) |
| T8 | The confirmation terminal tests | `uv run pytest -q tests/test_confirm_tty.py tests/test_confirm_dialog.py tests/test_confirm_engine.py` | All pass, including the child that takes a pseudo-terminal with `TIOCSCTTY` | CI (Linux) |

## 2. Input files (`sanitize.read_input`)

Setup: `cd "$TC_SCRATCH" && printf 'hello\n' > body.txt && ln -s body.txt link.txt && mkfifo pipe.txt`. The probe is `ticketctl --config ... --dry-run comment KEY --body-file PATH` with a real key of the throwaway ticket.

| ID | Check | Command | Expected | Linux |
|---|---|---|---|---|
| F1 | A regular file under a normal path is read | `--body-file "$TC_SCRATCH/body.txt"` | Exit 0, a plan whose body hash is the sha256 of the file plus the attribution paragraph | CI |
| F2 | A regular file under a symlinked directory is read | `--body-file /tmp/...` where `/tmp` is the macOS link to `/private/tmp` (copy `body.txt` there first) | Exit 0: only the final path component may not be a link, and `/private/tmp` is not a forbidden root | CI (shape), none (the macOS `F_GETPATH` value) |
| F3 | A symlink as the final component is refused | `--body-file "$TC_SCRATCH/link.txt"` | Exit 22 `invalid_file` | CI |
| F4 | A FIFO is refused and does not block | `timeout 10 ticketctl ... --body-file "$TC_SCRATCH/pipe.txt"` | Exit 22 `invalid_file` at once (the `timeout` never fires) | CI |
| F5 | A device path is refused | `--body-file /dev/null`, then `--body-file /dev/zero` | Exit 22 `invalid_file` both times; no read of `/dev/zero` | CI (the `/proc/self/fd` branch), none (`F_GETPATH`) |
| F6 | The descriptor path lookup works | `uv run python -c "import fcntl,os; fd=os.open('$TC_SCRATCH/body.txt', os.O_RDONLY); print(hasattr(fcntl,'F_GETPATH'), fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(bytes(1)).decode())"` | `True` and the real path of the file (under `/private/var/...` for a `mktemp -d` directory) | none |
| F7 | A path below `/dev`, `/proc` or `/sys` through a link is refused | `ln -s /dev/null "$TC_SCRATCH/devlink"` then `--body-file "$TC_SCRATCH/devlink"` | Exit 22 `invalid_file` (a link is refused first) | CI |
| F8 | Stdin `-` still works with a pipe and is refused on a terminal | `printf 'hi' \| ticketctl ... --dry-run comment KEY --body-file -`, then the same with stdin on the terminal | Exit 0 with the pipe; exit 2 `invalid_argument` on the terminal | CI (pipe), sim (terminal) |

## 3. `export --to`

Setup: write at least one audit record first by running one write from [section 7](#7-real-site-reads-and-writes) (or copy an `audit.jsonl` made on Linux into `$XDG_STATE_HOME/ticketctl/`). `export` reads `$XDG_STATE_HOME/ticketctl/audit.jsonl`.

| ID | Check | Command | Expected | Linux |
|---|---|---|---|---|
| E1 | A new file is created with mode 0600 | `ticketctl export --to "$TC_SCRATCH/out.jsonl"; stat -f '%Lp' "$TC_SCRATCH/out.jsonl"` | Exit 0, stdout empty, `600` | CI |
| E2 | An existing file is refused | Run E1 again | Exit 20 `already_exists`, the file unchanged | CI |
| E3 | A symlink at the name is refused, not followed | `ln -s "$TC_SCRATCH/elsewhere" "$TC_SCRATCH/l.jsonl"; ticketctl export --to "$TC_SCRATCH/l.jsonl"` | Exit 20 `already_exists`, `elsewhere` not created | CI |
| E4 | A dangling and a live symlink behave the same | Repeat E3 with `elsewhere` created first | Exit 20, `elsewhere` untouched | CI |
| E5 | A FIFO at the name is refused | `mkfifo "$TC_SCRATCH/f.jsonl"; timeout 10 ticketctl export --to "$TC_SCRATCH/f.jsonl"` | Exit 20 `already_exists`, no hang | CI |
| E6 | An unusable parent is exit 22 | `ticketctl export --to "$TC_SCRATCH/no-such-dir/out.jsonl"`, and a parent that is a file | Exit 22 `invalid_value` | CI |
| E7 | The same checks on APFS and on a network file system | Repeat E1 to E6 with `TC_SCRATCH` on an external APFS volume and on an SMB or NFS mount | Same results; record the file system. On a mount that ignores mode bits (some SMB mounts) record the observed mode | none |
| E8 | A parent directory owned by another user | `sudo mkdir /tmp/other-owner && sudo chmod 777 /tmp/other-owner`, then `ticketctl export --to /tmp/other-owner/out.jsonl` (clean up with `sudo rm -r` afterwards) | Exit 22 `invalid_value`, nothing created | sim (the owner is patched) |
| E9 | A separate writer appending mid-export | In one shell run a loop of real writes against the throwaway ticket (`for i in $(seq 1 30); do ticketctl ... --yes --expect-plan HASH comment KEY --body "n $i" --dedupe-key "tc-$i"; done`, each with a hash from its own dry run); in another shell run `ticketctl export --to` to a new path several times meanwhile, then `ticketctl export --after-seq N` with the last `seq` seen | No torn last line printed, no warning for it, no gap or repeat between runs when `--after-seq` carries the last seen `seq` | none (probed by hand on Linux) |
| E10 | A real full disk removes the new file | Make a tiny RAM disk (`hdiutil attach -nomount ram://2048` then `diskutil erasevolume HFS+ tc /dev/diskN`), fill it, run `ticketctl export --to /Volumes/tc/out.jsonl` with a large audit; detach the disk afterwards (`hdiutil detach`) | Non-zero exit, no `out.jsonl` left on the volume | sim (`write_all` is patched) |
| E11 | Memory on a multi-GB `audit.jsonl` | Build a 3 GB file of valid records (a loop of `cat` of one real export), then `/usr/bin/time -l ticketctl export --to "$TC_SCRATCH/big.jsonl"` | `maximum resident set size` stays in the tens of MB, independent of the file size | none (reasoned from the bounded reader) |

## 4. Confirmation dialog on `/dev/tty` (`confirm_tty.py`)

Run from a real terminal (Terminal or iTerm), not from a script or a CI shell. The probes use `ticketctl --config ... comment KEY --body 'tc platform check'` against the throwaway ticket, which posts one comment when answered `y`.

| ID | Check | Command and action | Expected | Linux |
|---|---|---|---|---|
| C1 | The prompt appears on the terminal and `y` sends | Run the command, read the dialog, type `y` | The dialog on stderr ends with `Send this? Type y or yes to accept, anything else declines [y/N]:`; exit 0 and one comment | sim (a pty child) |
| C2 | `n`, an empty line, Ctrl-D and Ctrl-C decline | Run four times, answer `n`, Enter, Ctrl-D, Ctrl-C | Exit 20 `declined` each time, no audit record, no comment | sim |
| C3 | Type-ahead does not answer | Start `sleep 3; ticketctl ... comment KEY --body x` on one line, type `y` during the sleep (before the prompt exists), then answer `n` at the prompt | Exit 20 `declined`: the early `y` was flushed by `tcflush(TCIFLUSH)` | sim |
| C4 | `/dev/tty` is opened with `O_NOCTTY \| O_NOFOLLOW` and checked | Run C1 with stdin from a file: `ticketctl ... comment KEY --body x < /dev/null` | The dialog still appears and is answered on the terminal (the answer is never read from stdin) | sim |
| C5 | A different terminal on stderr is exit 13 | Open two terminals; in the first run `ticketctl ... comment KEY --body x 2>$(tty of the second)` (use `tty` in the second to get the path) | Exit 13 `confirmation_required` before anything is shown, nothing sent | sim (`tcgetpgrp` is patched) |
| C6 | No terminal at all (a `setsid` child) is exit 13 | `python3 -c "import os,sys; os.setsid(); os.execvp('uv', ['uv','run','ticketctl','--config','$TC_SCRATCH/config.yaml','comment','KEY','--body','x'])" < /dev/null` | Exit 13 `confirmation_required`, no prompt, no hang | CI |
| C7 | SIGHUP ends the run before anything is sent | Run C1 and close the terminal window at the prompt, then look at the audit from a new window (or send `kill -HUP` to the exact PID of that `ticketctl` process, never by name) | No `attempted` record and no comment | none |
| C8 | The 600 s timeout declines | Run C1 and wait 10 minutes without typing | Exit 20 `declined` after about 600 s | none |
| C9 | The `select` wait works on the tty descriptor | Part of C1: type slowly, one character at a time | The answer is accepted as one line | none |

## 5. State, locks and paths

| ID | Check | Command | Expected | Linux |
|---|---|---|---|---|
| S1 | `XDG_STATE_HOME` and `XDG_CONFIG_HOME` are respected | `XDG_STATE_HOME="$TC_SCRATCH/st" XDG_CONFIG_HOME="$TC_SCRATCH/cf" ticketctl validate` after copying the config to `$TC_SCRATCH/cf/ticketctl/config.yaml` | Exit 0, and after a write the state is under `$TC_SCRATCH/st/ticketctl/` | CI (`tests/test_cron_env.py`) |
| S2 | The default paths with no XDG variables | `env -u XDG_STATE_HOME -u XDG_CONFIG_HOME HOME="$TC_SCRATCH/h" ticketctl ...` with the config at `$TC_SCRATCH/h/.config/ticketctl/config.yaml` | State in `$TC_SCRATCH/h/.local/state/ticketctl/` (not `~/Library`) | CI (`tests/test_cron_env.py`) |
| S3 | State files are mode 0600 and the directory 0700 | `stat -f '%Lp %N' "$XDG_STATE_HOME/ticketctl" "$XDG_STATE_HOME/ticketctl/"*` | `700` for the directory, `600` for each file | CI |
| S4 | `os.utime(fd, None)` and `os.ftruncate` on a lock descriptor | Run the same-key `create` twice, 5 s apart, with `--dedupe-key tc-platform-1` against the throwaway project, `--yes --expect-plan` from a dry run; read the lock file's mtime (the files are `$XDG_STATE_HOME/ticketctl/locks/<two hex>`) with `stat -f '%Sm'` before and after | Both runs exit 0 (the second `reused`), no error mentioning the lock, mtime moved | none |
| S5 | `flock` on a network file system is not a supported place | Put `XDG_STATE_HOME` on an NFS or SMB mount and run a write | Record what happens (a refusal naming the state, or a working lock). Same-host local disk is the only supported case, so this is informational | none |
| S6 | A state directory owned by another user is refused | `sudo mkdir -p /tmp/other-state/ticketctl && sudo chown root /tmp/other-state/ticketctl`, `XDG_STATE_HOME=/tmp/other-state ticketctl ...` a write | Exit 1 `internal`, reason in the message, nothing written (clean up with `sudo rm -r`) | sim |
| S7 | A world-writable config file is refused | `chmod 666 "$TC_SCRATCH/config.yaml"` then `ticketctl --config "$TC_SCRATCH/config.yaml" validate`; restore with `chmod 600` | Exit 2 `invalid_config` saying the file is world-writable | CI |
| S8 | A symlinked state directory is refused | `ln -s "$TC_SCRATCH/st" "$TC_SCRATCH/stlink"` then a write with `XDG_STATE_HOME="$TC_SCRATCH/stlink"` (a link in an ancestor is fine, the directory itself may not be one) | Exit 1 `internal`, nothing written | CI |

## 6. Cron-style environment

The Linux CI runs this on every push through `tests/test_cron_env.py` against the fake provider. What stays for a person is the real `cron` (and, on macOS, `launchd`) environment, which differs by system.

| ID | Check | Command | Expected | Linux |
|---|---|---|---|---|
| R1 | A read and a dry-run write from a scrubbed environment | `uv run pytest -q tests/test_cron_env.py` | All pass | CI |
| R2 | The same steps from a real crontab | See [Real cron run](#real-cron-run) | The report ends `RESULT PASS` | none |
| R3 | The write with `--yes --expect-plan` from cron | `--apply` in the same run, against the throwaway ticket | `PASS --yes --expect-plan write`, and the audit shows `actor` `auto:unknown`, `actor_source` `default`, `stdin_tty` `false` | CI (fake) |
| R4 | The macOS cron variants | Repeat R2 under a `launchd` agent (`StartInterval` job running the same line) | Same report. Record any difference: a missing `HOME`, a `PATH` of `/usr/bin:/bin:/usr/sbin:/sbin`, no `/dev/tty` | none |

### Real cron run

1. Make a throwaway ticket and a config file at `$HOME/.config/ticketctl/config.yaml` (mode 600) whose `token_env` names a variable, for example `TC_CRON_TOKEN`.
2. Put the check in the crontab (`crontab -e`). The job sets only what cron should be trusted with; the token variable is set on the line (a file readable only by you that the line sources is the safer way):

   ```text
   PATH=/usr/local/bin:/usr/bin:/bin
   * * * * * TC_CRON_TOKEN="$(cat "$HOME/.tc-cron-token")" /usr/bin/python3 /path/to/checkout/scripts/cron_check.py --ticketctl /path/to/ticketctl --key KEY-1 --to Done --resolution Done --token-env TC_CRON_TOKEN --apply >> "$HOME/tc-cron-report.txt" 2>&1
   ```

   Remove `--apply` to run only the read and the gate checks (no write). Cron runs the line every minute: remove the entry after the first report.
3. Read `$HOME/tc-cron-report.txt`. Expected: every line `PASS`, `RESULT PASS`, the `environment variable names` note holds the names cron really provided (no `TERM`, no `XDG_*` unless you set them) and the `controlling terminal of the checker` note says `none`.
4. Inspect the audit: `$HOME/.local/state/ticketctl/audit.jsonl` (or under `$XDG_STATE_HOME`) holds an `attempted` and an `ok` record with `"actor": "auto:unknown"`, `"actor_source": "default"` and `"stdin_tty": false`.
5. Record the result in [Results](#results): the date, the system, the Python version and PASS or the failing line. Never paste the report if it names a variable you consider private; the names listed are the only environment content it prints.

## 7. Real site reads and writes

These need the real test site and are shared with the live suite; run them once per platform.

| ID | Check | Command | Expected | Linux |
|---|---|---|---|---|
| L1 | A real read | `ticketctl --config ... get KEY --json` on a ticket you own, and `ticketctl --config ... whoami --json` | Exit 0, a normalized ticket, no token in the output | none (the live suite, deselected in CI) |
| L2 | A real search with a cursor | `ticketctl --config ... search --project PROJ --limit 5 --json` and then `--cursor` from the answer | Exit 0 both times, a page of five, the second page differs | none |
| L3 | A real `--dry-run` write and its apply | `ticketctl ... --dry-run transition KEY --to NAME`, then `ticketctl ... --yes --expect-plan HASH transition KEY --to NAME` on the throwaway ticket | Exit 0 both times; the audit holds `attempted` and `ok`, the `actor_source` is `default` (no terminal) or `tty` (from a terminal) | none |
| L4 | A `post-create` hook after an unknown outcome gets a null `object_id` | Configure a `post-create` hook that writes `$TICKETCTL_ID` and its stdin to a file in `$TC_SCRATCH`; run a `create` with `--timeout 1` against a site made slow (or kill the network at the moment of the send), so the audit holds an `unknown` outcome | The hook ran with an empty `TICKETCTL_ID` and `"object_id": null` in its payload | CI (fake) |

## Results

Add one row per run. Use the date of the run, the operating system and its version, the Python version and the file system of `TC_SCRATCH` and `XDG_STATE_HOME`. Never record a host name, a user name, a site, a project key or a ticket key. A failure keeps its ID, what happened and a link to the issue that tracks it.

| Date (UTC) | System and version | Python | File system | IDs run | Result | Notes and issue links |
|---|---|---|---|---|---|---|
| | | | | | | |

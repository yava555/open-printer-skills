---
name: printer
description: Discover and remember LAN printers, print PDF/JPEG files, check status, and cancel specified jobs. Use for finding printers, printing files, setting a default device, and troubleshooting connections. Supports IPP/IPPS on Windows, macOS, and Linux, plus installed queues on macOS/Linux. Does not configure scanners, printer Wi-Fi, or enterprise authentication.
---

# Universal Printer

Use this skill's `scripts/printer.py` to operate printers on the local network. The agent must run on the user's computer or in a local execution environment that can reach the printer. Paths below are relative to the skill directory; replace them with absolute paths when executing commands.

## Prepare the runtime

Requires Python 3.10+. Use an isolated virtual environment in the project or the user's tools directory and install dependencies from `requirements.txt`. Do not use system-wide pip. Reuse an existing working environment when available.

```bash
python3 -m venv /absolute/tool-directory/printer-venv
/absolute/tool-directory/printer-venv/bin/python -m pip install -r /absolute/skill-directory/requirements.txt
```

On Windows, the interpreter is at `Scripts/python.exe` inside the virtual environment. Throughout this document, `python` refers to that environment's interpreter.

## Find and remember devices

```bash
python scripts/printer.py discover
python scripts/printer.py list
python scripts/printer.py add printer.local --name Study
python scripts/printer.py add ipp://printer.local:631/custom/queue
python scripts/printer.py default DEVICE_ID
```

- `discover` checks installed queues and DNS-SD, then scans the current LAN if no usable destination is found. The user does not need to supply an IP address. The printer must already be connected to the network; this skill cannot configure Wi-Fi for an unconnected device.
- `list` reads the cache. `cached_unknown` does not mean online. Show the user the name, model, actual color/duplex/media capabilities, warnings, and device ID. Include IP addresses in troubleshooting details when needed.
- Prefer the device specified by the user, then the default saved by the skill. If only one device is known, it can be selected automatically. If several are known and no default exists, ask the user to choose, then call `default` to remember the choice. Selecting another device for a single request does not change an existing default.
- If the default device is unreachable, rediscover only the device with the same stable identity. Do not switch to another printer.
- mDNS, system queues, and other sources may refer to the same device. Use the `device_id` returned by the script. Do not merge devices merely because their names match, or treat a system queue UUID as a physical device UUID.

## Query, validate, and print

```bash
python scripts/printer.py info --device DEVICE_ID
python scripts/printer.py doctor --device DEVICE_ID
python scripts/printer.py validate /absolute/document.pdf --device DEVICE_ID --pages 1
python scripts/printer.py print /absolute/document.pdf --device DEVICE_ID --copies 2 --pages 1-3,5
python scripts/printer.py print /absolute/photo.jpg --device DEVICE_ID
```

If `--device` is omitted, use the selection rules above. The default is one copy; media, color, and sidedness use the device's defaults. When requested, use `--media iso_a4_210x297mm`, `--sides two-sided-long-edge`, or `--color monochrome`, with values the device actually supports.

- When the user has explicitly requested printing a specific file and all parameters are available, execute directly without asking for confirmation of the same action again.
- Accept only PDF/JPEG. Convert Office documents, web pages, PNG images, and text to PDF with an appropriate tool and check pagination before passing them to this skill. Do not interpret `application/octet-stream` as support for arbitrary files.
- `validate` sends no document content and creates no job. It also does not verify whether a complete PDF/JPEG can be rendered. It checks the declared format and job parameters, and does not prove that paper has been printed.
- `info`, `doctor`, `discover`, `list`, and `validate` do not print. Do not submit a test page when the user has only requested skill creation, diagnosis, or validation.
- If the direct endpoint cannot handle the format, the script may select a CUPS queue confirmed to belong to the same device before submission. Do not independently send documents or probe text to port 9100 or an LPD service.

## Track results

```bash
python scripts/printer.py status JOB_REF
python scripts/printer.py jobs --device DEVICE_ID
python scripts/printer.py cancel JOB_REF
```

- Save `job_ref`. It binds the actual submission backend and remote job ID. A physical printer's job ID cannot be used to query a system queue, or vice versa.
- `print` polls every two seconds by default for up to 60 seconds. `--wait 0` returns the submission receipt immediately. `status` queries once by default; add `--wait 60` to keep polling. Do not call `print` again just to continue waiting.
- `submitted: true` means only that the job was accepted. `state: completed` means the device or queue reports completion; physical output and print quality require on-site inspection. `canceled` and `aborted` do not mean successful printing.
- `submission_unknown: true` means the job may have been accepted. Query using the original `job_ref` or run `jobs` to reconcile it. Even if the job is absent, do not infer that it was not printed. Never automatically resubmit or switch backends.
- Report `printer-state-reasons`, `job-state-reasons`, and `tracking_error`. Do not treat missing paper, supply warnings, or a paused queue as communication failures, and do not automatically clear them.
- `jobs` provides a `job_ref` for each listed remote job for subsequent operations. Cancel only the job explicitly specified by the user. After cancellation is accepted, check status; cancellation cannot guarantee that all remaining physical output will stop.

## Troubleshoot as needed

For connection, discovery, certificate, or system queue issues, read [references/platforms.md](references/platforms.md). For complete commands, error codes, and local storage details, see [references/commands.md](references/commands.md). For support limits, actual verification coverage, and protocol sources, see [references/compatibility.md](references/compatibility.md).

Results are JSON. Exit codes are `0` for success, `1` for an operation failure, and `2` for invalid arguments. Read the complete result rather than relying only on the exit code. Place global options `--timeout`, `--ca-file`, and `--data-dir` before the subcommand.

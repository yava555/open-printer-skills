# Commands, data, and results

The entry point is this skill's `scripts/printer.py`, run with the virtual environment's interpreter. Use absolute file paths.

## Commands

| Command | Main options | Behavior |
|---|---|---|
| `discover` | `--scan auto/never/always`, `--interface NAME`, `--subnet CIDR` | Discover devices and update the cache; include cached records not rediscovered |
| `list` | None | List cached records without treating snapshots as current online status |
| `add ADDRESS` | `--name NAME` | Accept an IP address, hostname, or full IPP/IPPS URI; save a candidate when authentication or certificate requirements can be identified despite failed verification |
| `default DEVICE_ID` | None | Save the skill's default printer without changing the operating system's default |
| `info` / `doctor` | `--device ID` | Query live information; doctor also reports dependencies, the data directory, and troubleshooting suggestions |
| `validate FILE` / `print FILE` | `--device ID`, `--copies N`, `--pages 1-3,5`, `--media PWG_NAME`, `--sides VALUE`, `--color VALUE` | Check capabilities and run Validate-Job; only print sends the document |
| `jobs` | `--device ID` | Query device endpoints and endpoints bound to receipts; also return persistent receipts |
| `status JOB_REF` | `--wait SECONDS` (default 0) | Query the receipt's original backend; reconcile unknown submissions by their unique job name first |
| `cancel JOB_REF` | None | Verify device and job identities before canceling the specified job |

`print --wait` defaults to 60 seconds and can be set to 0. The wait limit accepts 0–3600 seconds. For media, use the complete PWG name from `media-supported` returned by `info`; custom sizes are not guessed automatically.

`--sides`: `one-sided`, `two-sided-long-edge`, `two-sided-short-edge`.

`--color`: `auto` (keep the default), `monochrome`, `color`. The latter two require explicit support in `print-color-mode-supported`.

Global options must precede the subcommand:

```bash
python scripts/printer.py --timeout 5 --ca-file /absolute/trusted-ca.pem info --device DEVICE_ID
python scripts/printer.py --data-dir /absolute/isolated-state discover --scan never
```

`--help` displays human-readable help. Normal results and argument errors are JSON.

## Local data

At runtime, platformdirs resolves `user_data_path("open-printer-skills", appauthor=False)` and stores `state.sqlite3` there: under the user's Library on macOS, AppData on Windows, or the XDG data directory on Linux.

Device names, the default device, physical UUIDs, queue UUIDs, endpoints, last-seen timestamps, and capability snapshots are stored separately from the skill installation. Each print request queries live information; snapshots do not prove availability. Devices without a UUID retain only their original endpoints, with no guarantee of recognizing the same device after a DHCP address change.

Job receipts record the filename, unique job name, actual backend, destination address, remote ID, and submission state. Document content and credentials are not stored. Receipts are committed to SQLite before sending, so an interrupted process preserves the unknown state. Print job names include the unique `job_ref` for reconciliation after a lost response.

Uninstalling the skill does not automatically delete user data. To remove it, first check `data_directory` in `doctor`, then delete that directory after all skill processes have exited. Deleting receipts does not cancel physical printer jobs.

## Results

Successful results start with `ok: true` and contain command-specific data. Failed results include `ok: false`, `code`, and `error`, and may include `attempts`, `warnings`, `job_ref`, `submitted`, or `submission_unknown`. Raw IPP attributes appear in `attributes` and `groups`.

Common errors: `selection_required` (choose a device), `no_usable_backend` (the format, parameters, or backend cannot satisfy the request), `device_unreachable`, `identity_changed`, `job_identity_changed`, `authentication_required`, `tls_certificate_error`, `scan_range_too_large`, `ambiguous_interface`, and `submission_unknown`.

A job missing from `jobs` does not prove completion; the remote system may have removed its record. `tracking_error` in a print result means a query failed after submission. Do not resubmit the file in that case.

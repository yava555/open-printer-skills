# Open Printer Skills

Let a local agent discover and remember printers, print PDF/JPEG files, and track or cancel specified jobs. One `printer` skill covers standard IPP/IPPS and existing CUPS queues on macOS/Linux.

| Capability | macOS / Linux | Windows |
|---|---|---|
| DNS-SD, LAN IPv4 scanning, manual setup, caching | Supported | Supported |
| IPP/IPPS network printing, status, cancellation | Supported | Supported |
| Installed system printers | CUPS printing and job management | Enumeration only in v1 |
| Input files | PDF/JPEG | PDF/JPEG |

The agent must be able to execute scripts on the local network. A cloud-only agent cannot access a home LAN on its own. Devices must already be connected to the network; this project does not configure printer networking.

## Installation and usage

### Option 1: Ask your agent to install

Copy and paste this request into a local agent that supports `SKILL.md`:

```text
Please install the printer skill from https://github.com/yava555/open-printer-skills.
It discovers and remembers printers, prints PDF/JPEG files, and checks or cancels print jobs.
Read README.md and printer/SKILL.md, install the entire printer folder in your skill search directory,
and set up an isolated Python 3.10+ environment with its requirements.txt dependencies.
Verify the setup without submitting a print job.
```

### Option 2: Install with npx skills add

Requires [Node.js 22.20 or later and the skills CLI](https://github.com/vercel-labs/skills#install-a-skill). Run this from the project where you want to use the skill, then follow the prompts to choose your agent:

```bash
npx skills add yava555/open-printer-skills --skill printer
```

To make the skill available across projects, install it in your user directory with `-g`:

```bash
npx skills add yava555/open-printer-skills --skill printer -g
```

The CLI installs the skill files. Then ask your agent to follow [Prepare the runtime](printer/SKILL.md#prepare-the-runtime), using the installed skill's `requirements.txt` to set up its Python environment.

### Manual runtime setup and usage

Requires Python 3.10+. For a cloned checkout, create an isolated environment in the project root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r printer/requirements.txt
.venv/bin/python printer/scripts/printer.py discover
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r printer\requirements.txt
.venv\Scripts\python.exe printer\scripts\printer.py discover
```

For manual installation, provide the entire `printer` folder to an agent that supports SKILL.md, or copy/link it into the agent's skill search directory and specify the virtual environment interpreter's path. The skill's files are self-contained; the virtual environment and user data are stored separately.

Example requests: "Find my printer," "Print two copies of this PDF," "Use the study printer by default from now on," or "Check the status of the last job."

Command-line examples, run from the project root:

```bash
.venv/bin/python printer/scripts/printer.py list
.venv/bin/python printer/scripts/printer.py add ipp://printer.local:631/ipp/print --name Study
.venv/bin/python printer/scripts/printer.py default DEVICE_ID
.venv/bin/python printer/scripts/printer.py validate /absolute/document.pdf --pages 1
.venv/bin/python printer/scripts/printer.py print /absolute/document.pdf --copies 2
.venv/bin/python printer/scripts/printer.py status JOB_REF
```

`print` submits the file for printing. To inspect a device, use `info`, `doctor`, or `validate`. If several devices exist without a default, the skill returns candidates so the user can choose once and save the preference. An offline default is not replaced with another printer.

## Key behavior

- Each print request checks identity, formats, and parameter capabilities in real time, then submits once after Validate-Job succeeds. IPP devices do not necessarily support PDF; advertising only `application/octet-stream` is not treated as PDF support.
- If a direct endpoint cannot handle the file, an associated CUPS queue for the same device may be selected before submission. An uncertain submission is never automatically retried or moved to another backend.
- A local `job_ref` is saved before submission and binds the backend and remote job ID. A lost response preserves `submission_unknown`; an absent job does not prove that nothing was printed.
- IPPS verifies certificates and accepts a custom CA file. Credentials are not stored; enterprise authentication and Windows system queue printing are future extensions.
- Runtime state is stored in SQLite in the user's data directory, outside the skill directory. The cache preserves default preferences and offline devices, but does not store document content.
- Convert Office documents, web pages, PNG images, and text to PDF with other tools first. No vendor drivers, RAW/LPD client, or raster conversion is bundled.

See the [skill instructions](printer/SKILL.md), [commands and data](printer/references/commands.md), [platform troubleshooting](printer/references/platforms.md), and [verification coverage](printer/references/compatibility.md).

## Development and verification

```bash
.venv/bin/python -m unittest discover -s printer/tests -v
```

Tests use a loopback IPP service, temporary SQLite databases, mocked platform adapters, and temporary TLS certificates. They do not scan the real LAN or use paper. Unix socket tests run only on macOS/Linux; temporary certificate tests require OpenSSL. GitHub Actions is configured for Python 3.10/3.13 across all three platforms; providing the configuration does not mean those remote jobs have actually run.

## License and provenance

Original project code and documentation are licensed under [MIT](LICENSE). The IPP client evolved from an independently implemented standards-based client in an earlier local printing skill, using public specifications. No vendor APKs, decompiled code, SDKs, images, fonts, or historical device records are bundled. The project does not represent any printer manufacturer.

Dependencies installed through pip retain their own licenses: [zeroconf](https://github.com/python-zeroconf/python-zeroconf) (LGPL-2.1-or-later), [psutil](https://github.com/giampaolo/psutil) (BSD-3-Clause), and [platformdirs](https://github.com/tox-dev/platformdirs) (MIT). The transitive zeroconf dependency [ifaddr](https://github.com/p-leg/ifaddr) uses MIT. These packages are not vendored in this repository. When redistributing dependencies, preserve their respective notices and license requirements; the project's MIT license does not replace dependency licenses.

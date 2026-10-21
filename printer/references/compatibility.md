# Support and verification coverage

This project implements a standard IPP client and system queue adapters. Compatibility is not guaranteed for every brand, model, or firmware. Support is determined by actual capability queries rather than brand lists.

## V1 limits

| Path | Supported operations | Limits |
|---|---|---|
| IPP/IPPS | Capabilities, Validate-Job, Print-Job, job queries, cancellation of a specified job | The destination must accept PDF/JPEG and support the required operations and selected parameters |
| macOS/Linux CUPS | Queue enumeration, capabilities, validation, submission, queries, cancellation | Existing configured queues; no driver installation or queue modification |
| Windows system queues | Enumeration | No PDF rendering or driver-based printing path |
| DNS-SD | `_ipp._tcp` / `_ipps._tcp`, advertised custom ports and paths, IPv4/IPv6 addresses | Separate VLANs, disabled multicast, or devices not yet on the network can prevent discovery |
| Scanning | Bounded read-only probes of ports 631/443 within an active private IPv4 subnet | No IPv6 subnet enumeration, arbitrary port scanning, or exhaustive resource-path discovery |

PDF/JPEG header checks are not complete rendering validation. IPP Everywhere requires PWG Raster and recommends PDF, so v1 is not a full IPP Everywhere raster client. Unsupported formats may be converted through an existing CUPS queue for the same device; otherwise the limitation is reported.

For devices without a physical UUID, the original endpoint is retained and known names and models are checked, but identity recovery after an address change is not guaranteed. System queues without evidence of a physical association remain separate entries. Direct authentication, vendor protocols, scanners, network setup, and PWG/URF conversion are future extensions.

## Offline verification

On 2026-10-22, all 49 offline unittest cases passed on macOS with both Python 3.10.4 and 3.13.2. The suite preserves the original 11 regression scenarios and adds coverage for:

- Distinct device and queue identities, merging sources with the same UUID, DHCP address changes, offline caching, concurrent writes, and preserving the default device.
- Custom DNS-SD ports/paths and IPv6 scope, non-`/24` masks, VPN exclusion, range limits, scan deadlines, and exclusion of non-IPP services.
- Chinese filenames, rejected/substituted parameters, paused queues, HTTP authentication errors, unavailable system adapters, and mocked Windows UTF-8 enumeration.
- Loopback HTTP/IPP, real Unix sockets, and loopback TLS with rejection of self-signed certificates and explicit CA trust.
- Sending document content once, no queue fallback after a lost direct submission response, missing job IDs, durable intent after process interruption, and refusing cancellation when a remote job ID is reused.

Python 3.10 tests ran in an isolated temporary environment; Python 3.13 used the project virtual environment. CI is configured for Python 3.10/3.13 on all three platforms but has not run remotely. Native Windows/Linux execution and additional printer models still require verification in those environments.

## Read-only verification on a physical device

On 2026-10-22, the following checks ran on the current macOS environment using isolated temporary data directories, without writing state to the user's default configuration:

- `discover --scan never` successfully enumerated an existing Lenovo AirPrint system queue, correctly reported it as stopped, and did not enable it.
- A capability query and Validate-Job for one copy of page 1 succeeded against an explicitly specified Lenovo LJ2208W IPP endpoint, returning `successful-ok` and preserving `toner-empty-warning`.
- Validation used a temporary fixture containing only a PDF header. Validate-Job sends no document bytes, so this confirms only that the declared format and job parameters were accepted. It does not verify rendering, job submission, or physical output.

These results do not establish physical printing, automatic subnet discovery, mDNS discovery, or compatibility with additional models. Device addresses, UUIDs, queue names, and local data paths are excluded from the distributed documentation.

## Live discovery and print verification

On 2026-10-22, a user-authorized end-to-end test ran on macOS against a Lenovo LJ2208W, using an isolated data directory:

- `discover --scan always` found the printer through IPv4 subnet scanning in approximately 13 seconds, without supplying its address. The physical UUID associated the direct IPP endpoint with the existing CUPS queue. No mDNS endpoint was returned, so this run does not establish mDNS discovery.
- The direct endpoint reported PDF/JPEG, A4, monochrome, and one-sided support. The CUPS queue was stopped with `paused` and `cups-pki-expired`; it was not modified or used for submission.
- A complete, rendered and visually checked one-page A4 PDF passed Validate-Job with one copy, page 1, A4, monochrome, and one-sided options. A single Print-Job then succeeded through direct IPP.
- Status tracking reached `completed`, with `job-completed-successfully` and `job-impressions-completed: 1`. A subsequent standalone status query and the persisted receipt both confirmed completion. The isolated cache contained one job receipt.
- The printer's `toner-empty-warning` remained visible. The user confirmed that one test page was physically printed and that its text, grayscale, and lines looked normal.

Two limitations were observed during this run:

- Subnet scanning retained a separate unverified candidate after TLS verification failed on another local HTTPS endpoint. A certificate error alone does not confirm an IPP printer; the candidate was not selected for printing.
- When `status --wait` observed completion after its initial query, the final state and attributes appeared under `tracking`, while the top-level state and attributes retained the initial `processing` result. A standalone `status` query returned the current completed state correctly.

## Standards and interface sources

- [RFC 8010](https://www.rfc-editor.org/rfc/rfc8010.html): IPP encoding and HTTP transport.
- [RFC 8011](https://www.rfc-editor.org/rfc/rfc8011.html): Printing, validation without document data, status, and cancellation semantics.
- [RFC 7472](https://www.rfc-editor.org/rfc/rfc7472.html): IPPS, with port 631 when no port is explicitly specified.
- [RFC 6763](https://www.rfc-editor.org/rfc/rfc6763.html): DNS-SD service discovery.
- [PWG IPP guide](https://www.pwg.org/ipp/ippguide.html): Formats and device capabilities.
- [CUPS IPP interface](https://openprinting.github.io/cups/libcups/spec-ipp.html): CUPS-Get-Printers and scheduler resources.
- [Microsoft Get-Printer](https://learn.microsoft.com/en-us/powershell/module/printmanagement/get-printer): Enumeration of installed Windows printers.

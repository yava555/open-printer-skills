# Platform adapters and troubleshooting

## macOS / Linux

Network devices use IPP/IPPS directly. System queues are enumerated and operated through the structured CUPS IPP interface, including local Unix sockets. The scheduler location is resolved from `CUPS_SERVER`, then `lpstat -H`, then common local sockets, and finally localhost:631.

System queues provide their existing drivers and format conversion. This skill does not install drivers, add queues, or enable paused queues. An unavailable CUPS service does not disable mDNS or direct IPP. Device or enterprise authentication through CUPS depends on the queue's existing configuration. If the scheduler itself requires additional authentication from this client, v1 reports an error.

A queue's `printer-uuid` identifies the logical queue. Physical device association comes from a UUID in `device-uri` or a URI exactly matching a confirmed direct endpoint. If no association can be established, keep a separate device entry rather than merging similarly named printers.

## Windows

Direct IPP/IPPS, DNS-SD, IPv4 scanning, and caching are available. Windows PowerShell `Get-Printer` enumerates installed printers using UTF-8 JSON. If PrintManagement is unavailable, report a warning and continue network discovery. Windows system queues are `inventory_only` in v1; raw PDF bytes are not sent to USB/RAW queues.

## Devices are not discovered

- The computer and printer must be on a mutually reachable LAN. Guest Wi-Fi, client isolation, separate VLANs, firewalls, and UDP 5353 multicast restrictions can prevent discovery.
- `discover --scan never` checks only queues and DNS-SD. `--scan always` additionally probes LAN TCP ports 631/443. It does not ping or send data to ports 9100/515.
- IPv4 scanning uses actual subnet masks returned by psutil rather than assuming `/24`. Automatic selection excludes loopback, VPN, and obvious virtual interfaces. Use `--interface` when several physical interfaces exist. Networks larger than 1,024 addresses require a smaller local `--subnet`.
- Port connection timeouts are capped at 0.5 seconds, individual IPP probes at 2 seconds, and scanning at 32 concurrent workers for 30 seconds. The deadline returns partial results; in-flight read-only requests may need a short time to finish.
- DNS-SD uses the advertised port, TXT `rp` resource path, hostname, and resolved addresses, including IPv6 scope. Scanning and adding bare addresses try only a limited set of common paths. They do not guarantee finding arbitrary unadvertised paths; supply a full URI when needed.

## Certificates and authentication

IPPS verifies the system trust chain and hostname. For self-signed certificates or an internal CA, provide a trusted CA through `--ca-file`, using a source the user trusts. Certificate verification is never disabled automatically, and a failed known IPPS route is not downgraded to plaintext IPP. An associated, available CUPS queue can handle its own device connection.

HTTP/IPP authentication requirements are reported explicitly. V1 does not accept credentials in URIs, store credentials, or implement Basic/Digest/Kerberos authentication. Prefer an already configured system queue for account or domain authentication.

## Connected but not printing

Check live `printer-state`, `printer-is-accepting-jobs`, formats, and selected parameters. Stopped queues, missing paper, supplies, and rendering errors are reported separately from communication failures. `Validate-Job` does not check complete document rendering or create a job. After submission, query only the original `job_ref`; do not troubleshoot by switching backends or printing again.

#!/usr/bin/env python
"""
Command-line companion to the Printer Monitor web app.

Useful on the server console when a printer will not appear in the dashboard:
`probe` shows exactly what one device answers, and `scan` runs the same sweep
discovery uses but prints the result instead of storing it.

    python manage.py probe 10.10.20.41
    python manage.py scan 10.10.20.0/24
    python manage.py serve --port 8080
    python manage.py config
"""

from __future__ import annotations

import argparse
import sys
import time

from app.config import load_settings
from app.discovery import DiscoveryRunner, probe_web_interface, reverse_dns
from app.printer_mib import looks_like_printer, read_printer
from app.snmp import SnmpClient, SnmpError


def _bar(percent: int | None, width: int = 24) -> str:
    if percent is None:
        return "[" + "?" * width + "]"
    filled = max(0, min(width, round(percent * width / 100)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def cmd_probe(args, settings) -> int:
    communities = [args.community] if args.community else settings.community_list
    for community in communities:
        print(f"Trying {args.ip}:{settings.snmp_port} with community "
              f"{'*' * len(community)} ...")
        client = SnmpClient(
            args.ip,
            community=community,
            port=settings.snmp_port,
            timeout=args.timeout or settings.snmp_timeout,
            retries=settings.snmp_retries,
        )
        try:
            is_printer, identity = looks_like_printer(client)
        except SnmpError as exc:
            print(f"  SNMP error: {exc}")
            client.close()
            continue

        if not identity:
            print("  no response")
            client.close()
            continue

        if not is_printer:
            print("  answers SNMP but exposes no Printer MIB (1.3.6.1.2.1.43).")
            print("  This is a non-printer device, or SNMP printer support is")
            print("  switched off in the device's own configuration.")
            for oid, value in identity.items():
                print(f"    {oid} = {value}")
            client.close()
            return 2

        stats = read_printer(
            client, low_threshold=settings.low_toner_threshold, identity=identity
        )
        client.close()

        print()
        print(f"  Name       : {stats.sys_name or '-'}")
        print(f"  Model      : {stats.model or '-'}  [{stats.vendor or 'unknown vendor'}]")
        print(f"  Serial     : {stats.serial_number or '-'}")
        print(f"  MAC        : {stats.mac_address or '-'}")
        print(f"  Location   : {stats.sys_location or '-'}")
        print(f"  Hostname   : {reverse_dns(args.ip) or '-'}")
        print(f"  Web UI     : {probe_web_interface(args.ip, settings.web_probe_ports) or '-'}")
        print(f"  State      : printer={stats.printer_status or '-'} "
              f"device={stats.device_status or '-'}")
        print(f"  Front panel: {stats.console_text or '-'}")
        print(f"  Pages      : {stats.page_count if stats.page_count is not None else '-'}")
        if stats.uptime_seconds is not None:
            print(f"  Uptime     : {stats.uptime_seconds // 86400}d "
                  f"{(stats.uptime_seconds % 86400) // 3600}h")
        if stats.errors:
            print(f"  ALERTS     : {', '.join(stats.errors)}")
        print(f"  Health     : {stats.health(settings.low_toner_threshold)}")

        if stats.supplies:
            print("\n  Supplies:")
            for supply in stats.supplies:
                pct = "  n/a" if supply.percent is None else f"{supply.percent:3d}%"
                kind = "waste" if supply.is_receptacle else (
                    "toner" if supply.is_toner else supply.type
                )
                print(f"    {_bar(supply.percent)} {pct}  {supply.state:<15} "
                      f"{kind:<12} {supply.description}")
        else:
            print("\n  Supplies   : none reported")

        if stats.trays:
            print("\n  Input trays:")
            for tray in stats.trays:
                pct = "  n/a" if tray.percent is None else f"{tray.percent:3d}%"
                print(f"    {_bar(tray.percent)} {pct}  {tray.state:<15} "
                      f"{tray.name} {('(' + tray.media + ')') if tray.media else ''}")

        if stats.error:
            print(f"\n  Note: {stats.error}")
        return 0

    print("\nNo community string got a response. Check that:")
    print("  * SNMP (v1/v2c) is enabled in the printer's web interface")
    print("  * the read community matches (default is usually 'public')")
    print("  * UDP/161 is not blocked between this server and the printer")
    return 1


def cmd_scan(args, settings) -> int:
    if args.subnet:
        settings.subnets = list(args.subnet)
    problems = settings.validate()
    if problems:
        for problem in problems:
            print(f"config problem: {problem}", file=sys.stderr)
        if not settings.subnets:
            return 1

    targets = settings.sweep_targets()
    print(f"Sweeping {', '.join(settings.subnets)} "
          f"({len(targets)} addresses, {settings.discovery_workers} workers)...")
    started = time.time()

    runner = DiscoveryRunner(settings)

    def found(result):
        print(f"  {result.ip:<16} {(result.sys_name or '-'):<24} "
              f"{(result.model or '-'):<34} {result.web_url or ''}")

    results = runner.run_blocking(on_found=found)
    elapsed = time.time() - started
    print(f"\n{len(results)} printer(s) found in {elapsed:.1f}s.")
    if not results:
        print("Nothing answered. Try `probe <ip>` against a printer you know "
              "the address of to check SNMP and the community string.")
    return 0


def cmd_config(args, settings) -> int:
    print(f"config file     : {settings.config_path or '(none -- using defaults)'}")
    print(f"subnets         : {', '.join(settings.subnets) or '(none)'}"
          f"{'  [autodetected]' if settings.subnets_autodetected else ''}")
    print(f"sweep addresses : {len(settings.sweep_targets())}")
    print(f"communities     : {len(settings.community_list)} configured (values hidden)")
    print(f"snmp            : port {settings.snmp_port}, timeout "
          f"{settings.snmp_timeout}s, {settings.snmp_retries} retry/retries")
    print(f"poll interval   : {settings.poll_interval_seconds}s "
          f"({settings.poll_workers} workers)")
    print(f"rediscovery     : "
          f"{settings.discovery_interval_seconds}s"
          f"{' (disabled)' if settings.discovery_interval_seconds <= 0 else ''}")
    print(f"low threshold   : {settings.low_toner_threshold}%")
    print(f"data dir        : {settings.data_dir}")
    print(f"inventory file  : {settings.printers_file}")
    print(f"poller enabled  : {settings.poller_enabled}")
    for problem in settings.validate():
        print(f"PROBLEM         : {problem}")
    return 0


def cmd_serve(args, settings) -> int:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        root_path=args.root_path or "",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manage.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="path to config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="read one printer over SNMP and print everything")
    probe.add_argument("ip")
    probe.add_argument("--community", help="override the configured community")
    probe.add_argument("--timeout", type=float, help="seconds to wait per request")
    probe.set_defaults(func=cmd_probe)

    scan = sub.add_parser("scan", help="sweep subnets for printers")
    scan.add_argument("subnet", nargs="*", help="CIDR ranges (defaults to config)")
    scan.set_defaults(func=cmd_scan)

    show = sub.add_parser("config", help="show the effective configuration")
    show.set_defaults(func=cmd_config)

    serve = sub.add_parser("serve", help="run the web app (development)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--root-path", dest="root_path",
                       help="mount prefix, e.g. /printers")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return args.func(args, settings)


if __name__ == "__main__":
    sys.exit(main())

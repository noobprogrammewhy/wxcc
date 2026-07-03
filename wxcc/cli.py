"""wxcc command line: login (QR bind), run (daemon), status."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import ilink, store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # aiohttp/anthropic can be noisy at DEBUG
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def cmd_login(args: argparse.Namespace) -> int:
    creds = asyncio.run(ilink.qr_login())
    if not creds:
        print("登录失败或超时。", file=sys.stderr)
        return 1
    print(f"已保存凭证到 {store.account_dir() / (creds['account_id'] + '.json')}")
    print("现在可以运行:  wxcc run")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    accounts = store.list_accounts()
    if not accounts:
        print("没有已绑定的微信账号。先运行:  wxcc login")
        return 1
    st = store.BridgeState()
    cfg = store.load_config()
    print(f"WXCC_HOME: {store.get_home()}")
    print(f"已绑定账号: {', '.join(accounts)}")
    print(f"owner: {st.owner_id or '(尚未认领)'}")
    print(f"dm_policy: {cfg.get('dm_policy')}   cwd: {cfg.get('cwd')}   model: {cfg.get('model') or '(默认)'}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = store.load_config()
    if args.set:
        key, _, value = args.set.partition("=")
        key = key.strip()
        value = value.strip()
        # coerce a few known types
        if value.lower() in {"true", "false"}:
            parsed: object = value.lower() == "true"
        elif value.startswith("[") or value.startswith("{"):
            parsed = json.loads(value)
        else:
            parsed = value
        cfg[key] = parsed
        store.atomic_json_write(store.get_home() / "config.json", cfg)
        print(f"set {key} = {parsed!r}")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from . import bridge as bridge_mod

    accounts = store.list_accounts()
    if not accounts:
        print("没有已绑定的微信账号。先运行:  wxcc login", file=sys.stderr)
        return 1
    account_id = args.account or accounts[0]
    if account_id not in accounts:
        print(f"未知账号 {account_id}；已绑定: {', '.join(accounts)}", file=sys.stderr)
        return 1

    cfg = store.load_config()
    if args.dm_policy:
        cfg["dm_policy"] = args.dm_policy
    if args.cwd:
        cfg["cwd"] = args.cwd
    if args.model:
        cfg["model"] = args.model

    b = bridge_mod.Bridge(account_id, cfg)
    try:
        asyncio.run(b.run())
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxcc", description="WeChat <-> Claude Code bridge")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="扫码绑定微信").set_defaults(func=cmd_login)
    sub.add_parser("status", help="查看绑定与配置状态").set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="启动桥接守护进程")
    p_run.add_argument("--account", help="指定账号 id（默认第一个）")
    p_run.add_argument("--dm-policy", choices=["first", "allowlist", "open"], help="覆盖访问策略")
    p_run.add_argument("--cwd", help="Claude Code 工作目录")
    p_run.add_argument("--model", help="模型（如 claude-opus-4-8）")
    p_run.set_defaults(func=cmd_run)

    p_cfg = sub.add_parser("config", help="查看/修改配置")
    p_cfg.add_argument("--set", metavar="KEY=VALUE", help="设置一个配置项")
    p_cfg.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

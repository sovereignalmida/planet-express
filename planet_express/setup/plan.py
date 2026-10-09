"""plan: turn a discovery report plus the wizard's answers into a reviewable list of typed steps.

Pure: no host access beyond the discovery dict it is handed. Secrets never enter the public plan:
steps carry `{{secret:NAME}}` placeholders, and the values travel in `Plan.secrets`, outside the
JSON and outside `plan_id`. Design and invariants: docs/designs/setup-plan.md.

Never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from planet_express.setup.answers import FORBIDDEN_RISKS_BY_TIER, SetupAnswers
from planet_express.setup.steps import CATALOGUE

MASK = "••••"
_PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
# Paths whose contents are system-wide, not part of the install: raise the risk and need root.
_SYSTEM_PREFIXES = ("/etc/", "/boot/", "/usr/", "/var/", "/run/")
# Every whole-file version of the MOS hook scripts this project shipped before they became merged blocks
# (git history of scripts/mos-boot/). A file that matches one exactly is entirely ours.
LEGACY_HOOK_SHA256 = {
    "post-start.sh": ["099dfc4f54c720ffa9645d8cef1c1b5e32fb1fc7983ab68f566c0333adb68ac6",
                      "269a61cc194d8d53f84def4ff34ef63f321ff5f3a50f6768e9514adb6956226f",
                      "f34ee3a804e07da1263bd62f56c3f101c4d34af4e478cb87265fdc3a4de35784"],
    "shutdown.sh": ["5c78195bb2631081438c96e079fa49bbd5578a047528af823fb634eb740380e7"],
}
_MOS_PE_HOME = re.compile(r"^(\s*)PE_HOME=\S+", re.MULTILINE)
_MOS_CHECKOUT = re.compile(r"^(\s*)CHECKOUT=\S+", re.MULTILINE)


@dataclass(frozen=True)
class Step:
    id: str
    kind: str
    title: str
    target: str
    risk: str
    reversible: bool
    needs_root: bool
    depends_on: tuple[str, ...]
    params: dict
    preview: dict

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "target": self.target,
                "risk": self.risk, "reversible": self.reversible, "needs_root": self.needs_root,
                "depends_on": list(self.depends_on), "params": self.params, "preview": self.preview}


@dataclass(frozen=True)
class Plan:
    story: str
    steps: tuple[Step, ...]
    warnings: tuple[str, ...]
    will_not_touch: tuple[str, ...]
    blocked: tuple[str, ...]
    # Values for the `{{secret:NAME}}` placeholders. Never serialised, never in the id.
    secrets: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def applicable(self) -> bool:
        return not self.blocked

    def to_public(self) -> dict:
        body = {"schema": 1, "story": self.story, "applicable": self.applicable,
                "blocked": list(self.blocked), "warnings": list(self.warnings),
                "will_not_touch": list(self.will_not_touch),
                "steps": [s.public() for s in self.steps],
                "summary": {"steps": len(self.steps),
                            "needs_root": sum(s.needs_root for s in self.steps),
                            "irreversible": sum(not s.reversible for s in self.steps),
                            "highest_risk": max((s.risk for s in self.steps), default="R0")}}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        return {"plan_id": digest, **body}


def _mask(text: str) -> str:
    return _PLACEHOLDER.sub(MASK, text)


class _Builder:
    def __init__(self, discovery: dict, answers: SetupAnswers, repo_root: str, read):
        self.d, self.a, self.repo, self.read = discovery, answers, repo_root, read
        self.init = discovery["host"]["init_system"]
        self.steps: list[Step] = []
        self.warnings: list[str] = []
        self.blocked: list[str] = []
        self.secrets: dict[str, str] = {}
        self.not_touched: list[str] = []
        self.cfg = None
        self.p: dict = {}
        self.snapshot_id: str | None = None

    def present(self, path: str) -> bool:
        return bool(self.d["existing_pe"].get("present", {}).get(path))

    def expectation(self, path: str, wanted: str) -> dict:
        """What `apply` must find at `path` for this step to proceed: the compare-and-swap input.

        A file that exists and whose hash we hold is expected to still have it; one that is absent is
        expected to stay absent. If it exists but could not be hashed we cannot prove it is unchanged, so a
        replace is downgraded to keep rather than risk overwriting something unseen."""
        if path not in self.d["existing_pe"].get("present", {}):
            # discover never looked at this path, so it cannot be claimed absent or unchanged. Keep-if-present
            # is the only thing that is safe without having looked.
            if wanted == "replace":
                self.warnings.append(f"{path} was not checked by the scan, so it is kept if it exists, not replaced.")
            return {"if_exists": "keep"}
        if not self.present(path):
            return {"if_exists": wanted, "expect_absent": True}
        digest = self.d["existing_pe"].get("sha256", {}).get(path)
        if digest:
            return {"if_exists": wanted, "expected_sha256": digest}
        if wanted == "replace":
            self.warnings.append(f"{path} exists but could not be read to compare, so it is kept, not replaced.")
        return {"if_exists": "keep"}

    # -- steps -------------------------------------------------------------------------------
    def add(self, kind: str, title: str, target: str, params: dict, preview: dict, *,
            depends_on: tuple[str, ...] = (), risk: str | None = None, needs_root: bool | None = None,
            reversible: bool | None = None) -> str:
        meta = CATALOGUE[kind]
        validated = meta.model(**params)
        if self.snapshot_id and kind != "state.snapshot":
            # Nothing changes before the safety net exists.
            depends_on = (self.snapshot_id,) + tuple(d for d in depends_on if d != self.snapshot_id)            # per-kind params model: unknown or unsafe values fail here
        step_id = f"s{len(self.steps) + 1:02d}"
        self.steps.append(Step(
            id=step_id, kind=kind, title=title, target=target,
            risk=risk or meta.risk, reversible=meta.reversible if reversible is None else reversible,
            needs_root=meta.needs_root if needs_root is None else needs_root,
            depends_on=depends_on, params=validated.model_dump(mode="json", exclude_none=True), preview=preview))
        return step_id

    def file_step(self, title: str, path: str, content: str, *, mode="0644", owner="root", group="root",
                  if_exists="keep", has_secrets=False, depends_on=()) -> str:
        system = path.startswith(_SYSTEM_PREFIXES) or self.init == "mos"
        shown = _mask(content)
        if self.present(path) and self.expectation(path, if_exists)["if_exists"] == "keep":
            title += " (already exists: kept)"
        expect = self.expectation(path, if_exists)
        return self.add(
            "file.write", title, path,
            {"path": path, "content": content, "mode": mode, "owner": owner, "group": group,
             "has_secrets": has_secrets, **expect},
            {"type": "file", "path": path, "mode": mode, "owner": f"{owner}:{group}",
             "if_exists": expect["if_exists"], "already_present": self.present(path), "content": shown},
            depends_on=tuple(depends_on), risk="R2" if system else "R1", needs_root=system)

    def secret(self, name: str, value: str) -> str:
        self.secrets[name] = value
        return "{{secret:%s}}" % name

    def text(self, summary: str) -> dict:
        return {"type": "text", "text": summary}


def _paths(b: _Builder) -> dict:
    install = b.a.install_dir
    existing = b.d["existing_pe"]
    # An install that is already here keeps its config where it is; a new one gets the conventional place.
    found_config = existing.get("config_path") if existing.get("installed") and existing.get("config") else None
    if b.init == "mos":
        home = str(Path(install).parent)
        return {"home": home, "venv": f"{home}/venv", "state": f"{home}/state", "logs": f"{home}/logs",
                "data": f"{home}/data", "config": found_config or f"{home}/config.yaml",
                "env": f"{home}/planetexpress.env", "dash_env": f"{home}/planetexpress-dashboard.env",
                "defaults": f"{home}/default"}
    return {"home": install, "venv": f"{install}/venv", "state": f"{install}/state", "logs": f"{install}/logs",
            "data": f"{install}/data", "config": found_config or "/etc/planetexpress/config.yaml",
            "env": "/etc/planetexpress.env", "dash_env": "/etc/planetexpress-dashboard.env", "defaults": None}


def _check_preconditions(b: _Builder) -> None:
    a, d = b.a, b.d
    blocking = [c for c in d["checks"] if c["status"] == "blocked" and not c["overridable"]
                and c["id"] != "privileges"]      # setup runs as root; that is the server's concern, not the plan's
    for c in blocking:
        b.blocked.append(f"{c['label']}: {c['detail']} {c['fix']}".strip())

    if a.run_as == "root":
        if b.init != "mos":
            b.blocked.append(
                "The service must not run as root: it would bypass the sudo allowlist that scopes what "
                "Planet Express may do. Choose an unprivileged account (it needs access to Docker).")
        elif not a.accept_root_service:
            b.blocked.append(
                "On MOS Planet Express has to run as root, because MOS has no sudo and its Docker socket is "
                "root-owned. Tick the acknowledgement to continue.")
        else:
            b.warnings.append(
                "Planet Express will run as root. MOS has no sudo, so the scoped sudo grant that limits it on "
                "other hosts cannot exist here. Every change is still a typed step that needs your approval, "
                "and the power tier below limits which classes of step are allowed at all.")
    elif b.init == "mos":
        b.blocked.append("On MOS the service has to run as root (no sudo, root-owned Docker socket); "
                         f"{a.run_as!r} would not be able to start or stop anything.")

    if b.init == "mos":
        pools = [p["mount"] for p in d["storage"]["pools"]]
        if not any(a.install_dir == m or a.install_dir.startswith(m + "/") for m in pools):
            b.blocked.append("MOS keeps / in RAM, so Planet Express must be installed on a pool. "
                             f"{a.install_dir} is not on one ({', '.join(pools) or 'none mounted'}).")
        bad = [u.unit for u in a.sudo_units if not re.fullmatch(r"[A-Za-z0-9_-]+", u.unit)] \
            if a.tier == "full" else []
        if bad:
            b.blocked.append("MOS unit names are init.d script names (letters, digits, '-' and '_'), so "
                             f"these cannot be allowed: {', '.join(bad)}.")
    if b.init == "unknown":
        b.blocked.append("This host's init system is not recognised; only systemd and MOS are supported.")

    if a.tier == "full" and not a.sudo_units:
        b.warnings.append("Tier 'full' allows unit control, but no units were listed, so nothing is allowed "
                          "(no sudo grant on systemd, an empty allowlist on MOS) and unit control stays "
                          "unusable until you add some.")
    if a.story == "adopt" and a.tier != "observe":
        b.warnings.append(f"Adopting an existing homelab at the '{a.tier}' tier lets Planet Express change running "
                          "stacks (each change still needs your approval). 'observe' changes nothing.")
    if a.story == "fresh" and d["existing_pe"]["installed"]:
        b.warnings.append("Planet Express is already installed here. Existing config and env files are kept, "
                          "not overwritten; consider a repair instead.")
    port_busy = d["ports_in_use"].get(str(a.dashboard_port), False)
    if port_busy and not d["existing_pe"]["installed"]:
        b.warnings.append(f"Port {a.dashboard_port} is already in use by something else.")
    env_kept, dash_kept = b.present(b.p["env"]), b.present(b.p["dash_env"])
    if a.telegram is None and not env_kept:
        b.warnings.append("No Telegram credentials: Planet Express will not start until TG_BOT_TOKEN and TG_CHAT_ID "
                          "are set in its env file, and it cannot ask you to approve anything.")
    if (a.llm is None or a.llm.api_key is None) and not env_kept:
        b.warnings.append("No LLM key: scan summaries and action planning will fail until one is set.")
    if a.operator is None and not dash_kept:
        b.warnings.append("No dashboard operator: nobody can log in until you add one with "
                          "scripts/dashboard_operators.py.")
    if b.present(b.p["config"]):
        b.warnings.append(f"The existing config at {b.p['config']} is kept as it is, so the power tier you chose "
                          "is not applied to it. To change what Planet Express may do, edit that config.")
    if b.init == "mos":
        b.warnings.append("Python on MOS has no pip, so setup downloads it (get-pip.py) into the virtualenv: "
                          "this host needs internet access during install.")


def _config_content(b: _Builder) -> str:
    from config_schema import AutonomyConfig, PlanetExpressConfig, SudoAllowlist, SudoUnitGrant
    a = b.a
    forbidden = FORBIDDEN_RISKS_BY_TIER[a.tier]
    allowlist = SudoAllowlist(units=[SudoUnitGrant(unit=u.unit, actions=u.actions) for u in a.sudo_units]
                              if a.tier == "full" else [])
    cfg = PlanetExpressConfig(
        stacks_root=a.stacks_root, forbidden_stacks=list(a.ignored_stacks),
        host_control_provider="mos" if b.init == "mos" else "systemd",
        autonomy=AutonomyConfig(forbidden_risks=forbidden,
                                direct_request_risks=[r for r in ("R1",) if r not in forbidden]),
        sudo_allowlist=allowlist)
    b.cfg = cfg
    return yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False)


def _env_content(b: _Builder) -> str | None:
    a, lines = b.a, []
    if a.llm is not None:
        lines.append(f"LLM_PROVIDER={a.llm.provider}")
        if a.llm.api_key is not None:
            key = "ANTHROPIC_API_KEY" if a.llm.provider == "anthropic" else "OPENAI_API_KEY"
            lines.append(f"{key}={b.secret('llm_api_key', a.llm.api_key.get_secret_value())}")
    if a.telegram is not None:
        lines.append(f"TG_BOT_TOKEN={b.secret('telegram_token', a.telegram.token.get_secret_value())}")
        lines.append(f"TG_CHAT_ID={b.secret('telegram_chat_id', a.telegram.chat_id.get_secret_value())}")
    return "\n".join(lines) + "\n" if lines else None


def _build(b: _Builder) -> None:
    a, p = b.a, b.p
    run_group = a.run_group or a.run_as
    mos = b.init == "mos"

    # An install that already has state gets a snapshot first, as deploy.sh does before it changes anything.
    if b.d["existing_pe"]["installed"]:
        # Any detected install, not only one whose config survives: state and a database can outlive the
        # config, and the snapshot tool records an absent config without complaint.
        b.snapshot_id = b.add(
            "state.snapshot", "Snapshot the current state first", a.install_dir,
            {"install_dir": a.install_dir, "run_user": a.run_as,
             "env": {"CASA_CONFIG": p["config"], "CASA_DATA_DIR": p["data"]}},
            b.text("Takes a snapshot of the current config and state so this install can be rolled back. "
                   "It reads what is there and writes only the snapshot."))

    # state and logs belong to the service user; on MOS that is root, and the rest live on the pool.
    owner = {"owner": a.run_as, "group": run_group}
    # Parents first: on MOS the pool's home directory holds everything else.
    dirs = ([p["home"]] if mos else []) + [p["state"], p["logs"]] + ([p["data"], p["defaults"]] if mos else [])
    # data holds core's database and logs hold step output: private. The dashboard reads state, which holds
    # no secrets (the dashboard is denied the other two by ACL on systemd and by these modes on MOS).
    modes = {p["data"]: "0700", p["logs"]: "0700"} if mos else {}
    dir_ids = [b.add("dir.ensure", f"Create {Path(d).name}/", d,
                     {"path": d, "mode": modes.get(d, "0755"), **owner, **({"tighten": True} if d in modes else {})},
                     b.text(f"Creates {d} (owner {a.run_as}, mode {modes.get(d, '0755')}) if it does not exist."
                            + (" If it already exists with wider access, the extra access is removed (never added)."
                               if d in modes else "")),
                     risk="R1", needs_root=mos)
               for d in dirs]
    config_dir = str(Path(p["config"]).parent)
    needs_config_dir = (not mos and not b.present(p["config"])
                        and config_dir not in (a.install_dir, p["home"]))
    if needs_config_dir:
        # The config lives outside the checkout, in a directory the service user owns, so that dashboard
        # config edits (an atomic replace in the same directory) can succeed.
        dir_ids.append(b.add("dir.ensure", "Create the config directory", config_dir,
                             {"path": config_dir, "mode": "0750", **owner},
                             b.text(f"Creates {config_dir} owned by {a.run_as}, mode 0750."), risk="R2",
                             needs_root=True))
    roots = {r["path"] for r in b.d["stacks_roots"]}
    if a.stacks_root not in roots:
        dir_ids.append(b.add("dir.ensure", "Create the stacks directory", a.stacks_root, {"path": a.stacks_root},
                             b.text(f"Creates {a.stacks_root}, which does not exist yet. No stacks are added.")))

    env_id = b.add(
        "python.env", "Create the Python environment", p["venv"],
        {"install_dir": a.install_dir, "venv_dir": p["venv"], "run_user": a.run_as, "bootstrap_pip": mos},
        b.text(f"Creates a virtualenv at {p['venv']} and installs requirements.txt into it."
               + (" Pip is downloaded into it first (MOS ships Python without ensurepip)." if mos else "")),
        depends_on=tuple(dir_ids[:2]))

    config_id = b.file_step(
        "Write config.yaml", p["config"], _config_content(b),
        # MOS has no ACLs to give the dashboard its own read grant, and config.yaml holds no secrets (those are in
        # the env files), so it is world-readable there. On systemd it is 0640 plus an ACL for the dashboard.
        mode="0644" if mos else "0640", owner=a.run_as, group=run_group,
        depends_on=dir_ids[:3 if mos else 2] + (dir_ids[-1:] if needs_config_dir else []))
    secrets_id = None
    env_text = _env_content(b)
    if env_text:
        secrets_id = b.file_step("Write the secrets file", p["env"], env_text, mode="0600", has_secrets=True,
                                 depends_on=dir_ids[:3 if mos else 2])

    sudoers_id = None
    if not mos and a.tier == "full" and a.sudo_units and b.present(p["config"]):
        b.warnings.append("The kept config decides which units Planet Express may control, so no sudoers grant is "
                          "written from your answers: it could disagree with that config. Generate one from the "
                          "config's own sudo_allowlist (scripts/setup_wizard.py).")
    if not mos and a.tier == "full" and a.sudo_units and not b.present(p["config"]):
        from scripts.setup_wizard import generate_sudoers_snippet
        snippet = generate_sudoers_snippet(a.run_as, b.cfg.sudo_allowlist, [], "systemd")
        sudoers_id = b.add(
            "sudoers.install", "Grant scoped sudo for unit control", "/etc/sudoers.d/planetexpress",
            {"path": "/etc/sudoers.d/planetexpress", "content": snippet, "run_user": a.run_as,
             **b.expectation("/etc/sudoers.d/planetexpress", "keep")},
            {"type": "file", "path": "/etc/sudoers.d/planetexpress", "mode": "0440", "owner": "root:root",
             "if_exists": "keep", "already_present": b.present("/etc/sudoers.d/planetexpress"), "content": snippet},
            depends_on=(config_id,))

    access_id = b.add(
        "access.provision", "Give the dashboard read-only access", "planetexpress-web",
        {"method": "groups" if mos else "acl", "install_dir": a.install_dir, "run_user": a.run_as,
         "config_path": p["config"], **({"venv_dir": p["venv"], "home_dir": p["home"]} if mos else {})},
        b.text("Creates the planetexpress-web user and planetexpress-rpc group, and lets the dashboard read exactly "
               "what it needs: the code, the state it displays and its own config. "
               + ("MOS has no ACLs: the code and environment are made readable, while the database, logs and "
                  "env files stay private to root." if mos else
                  "Uses read-only ACLs; everything else in the checkout is explicitly denied to it.")),
        depends_on=(env_id, config_id))

    op = a.operator
    refs = {}
    dash_id = None
    if op is not None:
        b.secret("operator_passphrase", op.passphrase.get_secret_value())
        b.secret("operator_totp", op.totp_secret.get_secret_value())
        refs = {"operator": op.name, "passphrase_ref": "operator_passphrase", "totp_ref": "operator_totp"}
    dash_kept = b.present(p["dash_env"])
    # An existing dashboard login with nothing to add is a no-op: leave it out of the plan entirely.
    if op is not None or not dash_kept:
        added = (f"Adds the operator '{op.name}': the passphrase is stored as a scrypt hash and the authenticator "
                 "secret as given. Neither is ever shown again." if op else "No operator is added.")
        opening = (f"{p['dash_env']} already exists and is kept, including its session secret." if dash_kept else
                   f"Creates {p['dash_env']} (root-only) with a fresh session secret.")
        what = f"{opening} {added}"
        dash_expect = (b.expectation(p["dash_env"], "replace") if dash_kept else {"expect_absent": True})
        if dash_kept and "expected_sha256" not in dash_expect:
            b.blocked.append(f"{p['dash_env']} exists but could not be read, so setup cannot add an operator to it "
                             "safely. Run setup as root.")
            return
        dash_id = b.add("dashboard.init", "Set up the dashboard login", p["dash_env"],
                        {"env_file": p["dash_env"], **refs,
                         **{k: v for k, v in dash_expect.items() if k in ("expect_absent", "expected_sha256")}},
                        b.text(what), depends_on=(access_id,))

    # Services -------------------------------------------------------------------------------
    install_ids, enable = [], []
    if mos:
        for name, script in (("casa-planetexpress", "scripts/casa-planetexpress.init.d"),
                             ("casa-dashboard", "scripts/casa-dashboard.init.d")):
            defaults = _mos_defaults(name, p, a)
            persisted = b.file_step(f"Keep /etc/default/{name} on the pool", f"{p['defaults']}/{name}", defaults,
                                    if_exists="replace", depends_on=dir_ids[:5])
            install_ids.append(b.add(
                "service.install", f"Install the {name} init script", f"/etc/init.d/{name}",
                {"flavour": "sysvinit", "name": name, "path": f"/etc/init.d/{name}", "content": b.read(script),
                 **b.expectation(f"/etc/init.d/{name}", "replace"),
                 "defaults_path": f"/etc/default/{name}", "defaults_content": defaults,
                 **{f"defaults_{k}": v for k, v in b.expectation(f"/etc/default/{name}", "replace").items()}},
                {"type": "file", "path": f"/etc/init.d/{name}", "mode": "0755", "owner": "root:root",
                 "if_exists": b.expectation(f"/etc/init.d/{name}", "replace")["if_exists"],
                 "already_present": b.present(f"/etc/init.d/{name}"), "content": b.read(script)},
                depends_on=(persisted,) + ((dash_id,) if dash_id else ())))
        hooks = {"post-start.sh": _mos_post_start(b, p), "shutdown.sh": b.read("scripts/mos-boot/shutdown.sh")}
        hook_dir = "/boot/optional/scripts"
        known = b.d["existing_pe"].get("sha256", {})
        expected = {n: known[f"{hook_dir}/{n}"] for n in hooks if f"{hook_dir}/{n}" in known}
        absent = [n for n in hooks if not b.present(f"{hook_dir}/{n}")]
        unreadable = [n for n in hooks if b.present(f"{hook_dir}/{n}") and n not in expected]
        if unreadable:
            b.blocked.append(f"{', '.join(unreadable)} exists in {hook_dir} but could not be read, so setup cannot merge "
                             "into it safely. Run setup as root so it can read the boot hooks.")
            return                              # the specific reason above, not the generic one a half-built step would give
        hook_id = b.add(
            "boot_hook.install", "Restore Planet Express at every MOS boot", hook_dir,
            {"dest_dir": hook_dir, "hooks": hooks, "expect_absent": absent, "expected_sha256": expected,
             "legacy_sha256": LEGACY_HOOK_SHA256},
            {"type": "files", "files": [
                {"path": f"{hook_dir}/{n}",
                 "merge": ("this is an older copy of ours, so the whole file is replaced"
                           if known.get(f"{hook_dir}/{n}") in LEGACY_HOOK_SHA256[n]
                           else "marked block (the rest of the file is kept)"),
                 "already_present": b.present(f"{hook_dir}/{n}"), "content": c} for n, c in hooks.items()]},
            depends_on=tuple(install_ids))
        install_ids.append(hook_id)
        enable = [("casa-planetexpress", a.start_services and a.telegram is not None),
                  ("casa-dashboard", a.start_services and a.telegram is not None)]
        flavour = "sysvinit"
    else:
        from scripts.render_template import render
        values = {"INSTALL_DIR": a.install_dir, "RUN_USER": a.run_as, "RUN_GROUP": run_group,
                  "CONFIG_FILE": p["config"], "DASHBOARD_PORT": str(a.dashboard_port)}
        units = [("casa-planetexpress", "systemd/casa-planetexpress.service.template"),
                 ("casa-dashboard", "systemd/casa-dashboard.service.template")]
        # casa-stacks brings every stack up at boot (casa_boot). That is a change to the host at a power
        # the observe and restart tiers do not grant, so only the 'stacks' tier and above gets it.
        if a.story == "fresh" and a.tier in ("stacks", "full"):
            units.append(("casa-stacks", "systemd/casa-stacks.service.template"))
        for name, template in units:
            content = render(b.read(template), **values)
            install_ids.append(b.add(
                "service.install", f"Install the {name} unit", f"/etc/systemd/system/{name}.service",
                {"flavour": "systemd", "name": name, "path": f"/etc/systemd/system/{name}.service",
                 "content": content, **b.expectation(f"/etc/systemd/system/{name}.service", "keep")},
                {"type": "file", "path": f"/etc/systemd/system/{name}.service", "mode": "0644",
                 "owner": "root:root", "if_exists": "keep",
                 "already_present": b.present(f"/etc/systemd/system/{name}.service"), "content": content},
                depends_on=(config_id,) + ((dash_id,) if dash_id else (access_id,)) + ((secrets_id,) if secrets_id else ())))
        enable = [(n, a.start_services and a.telegram is not None and n != "casa-stacks") for n, _ in units]
        flavour = "systemd"

    enable_ids = [b.add("service.enable", f"Enable {n}" + (" and start it" if start else ""), n,
                        {"flavour": flavour, "name": n, "start": start},
                        b.text(f"Enables {n} at boot" + (" and starts it now." if start else "; it is not started now.")),
                        depends_on=tuple(install_ids)) for n, start in enable]
    b.add("verify.smoke", "Check Planet Express can see Docker", a.install_dir,
          {"install_dir": a.install_dir, "venv_dir": p["venv"], "run_user": a.run_as,
           "env": {"CASA_CONFIG": p["config"], "CASA_STATE_DIR": p["state"], "CASA_LOG_DIR": p["logs"],
                   "CASA_DATA_DIR": p["data"]}},
          b.text("Runs Leela's read-only status scan once. It changes nothing."), depends_on=(env_id, config_id))
    _will_not_touch(b, p)
    if a.start_services and a.telegram is None:
        b.warnings.append("The services are enabled but not started, because they cannot start without Telegram credentials.")


def _mos_defaults(name: str, p: dict, a: SetupAnswers) -> str:
    base = [f"DAEMON_DIR={a.install_dir}", f"PYTHON={p['venv']}/bin/python"]
    if name == "casa-planetexpress":
        return "\n".join(base + [f"ENV_FILE={p['env']}", f"CASA_CONFIG={p['config']}", f"CASA_STATE_DIR={p['state']}",
                                 f"CASA_LOG_DIR={p['logs']}", f"CASA_DATA_DIR={p['data']}"]) + "\n"
    return "\n".join(base + [f"GUNICORN={p['venv']}/bin/gunicorn", f"ENV_FILE={p['dash_env']}",
                             f"CASA_CONFIG={p['config']}", f"CASA_STATE_DIR={p['state']}",
                             f"CASA_LOG_DIR={p['logs']}", f"CASA_DASHBOARD_PORT={a.dashboard_port}"]) + "\n"


def _mos_post_start(b: _Builder, p: dict) -> str:
    script = b.read("scripts/mos-boot/post-start.sh")
    if not _MOS_PE_HOME.search(script) or not _MOS_CHECKOUT.search(script):
        b.blocked.append("scripts/mos-boot/post-start.sh no longer has the PE_HOME/CHECKOUT lines this plan rewrites.")
        return script
    # The hook reinstalls from the checkout at every boot, so it has to name the real one, not assume a layout.
    script = _MOS_PE_HOME.sub(lambda m: f"{m.group(1)}PE_HOME={p['home']}", script, count=1)
    return _MOS_CHECKOUT.sub(lambda m: f"{m.group(1)}CHECKOUT={Path(b.a.install_dir).name}", script, count=1)


def _will_not_touch(b: _Builder, p: dict) -> None:
    """Promises about what setup leaves alone. Each one is conditional on what is actually on the host:
    a promise that is false for this host (a power tier that a kept config never receives, say) is
    worse than no promise."""
    a = b.a
    systemd = b.init == "systemd"
    b.not_touched += [
        "No running container is stopped, restarted or recreated by setup.",
        f"No compose file under {a.stacks_root} is created, edited or deleted.",
        "No secret is written to config.yaml; secrets go only to the root-only env files.",
    ]
    if a.ignored_stacks:
        b.not_touched.append(f"Ignored stacks are never read, started or stopped: {', '.join(a.ignored_stacks)}.")

    if b.present(p["config"]):
        b.not_touched.append(f"The existing config ({p['config']}) is kept unchanged. The power tier you chose is "
                             "not applied to it, so what Planet Express may do is whatever that config says.")
    elif a.tier == "observe":
        b.not_touched.append("Planet Express is forbidden from every action that changes something (risk R1 and "
                             "above) until you raise its power tier.")
    else:
        b.not_touched.append(f"Risk classes {', '.join(FORBIDDEN_RISKS_BY_TIER[a.tier])} stay forbidden by config.")

    installing_stacks = systemd and a.story == "fresh" and a.tier in ("stacks", "full")
    stacks_unit = "/etc/systemd/system/casa-stacks.service"
    if not installing_stacks:
        if b.present(stacks_unit):
            b.not_touched.append(f"The existing {stacks_unit} is left as it is.")
        elif systemd:
            b.not_touched.append("Stacks are not brought up at boot by Planet Express (casa-stacks is not installed).")

    sudoers = "/etc/sudoers.d/planetexpress"
    if systemd and not any(s.kind == "sudoers.install" for s in b.steps):
        if b.present(sudoers):
            b.not_touched.append("The existing sudoers grant is left as it is.")
        else:
            b.not_touched.append("No sudoers grant is written: Planet Express gets no privileged commands.")

    for path in (p["env"], p["dash_env"]):
        if b.present(path):
            b.not_touched.append(f"The existing {path} is kept, not overwritten.")
    for step in b.steps:
        if step.kind == "service.install" and step.params.get("if_exists") == "keep" and b.present(step.target):
            b.not_touched.append(f"The existing service file {step.target} is kept, not overwritten.")


def _uninstall(b: _Builder) -> None:
    """Remove the integration (services, units, init scripts, the sudo grant, the boot-hook block) and keep
    everything a person wrote or that holds their data: config, secrets, state, the database, the checkout."""
    a, p, existing = b.a, b.p, b.d["existing_pe"]
    mos = b.init == "mos"
    if not existing["installed"]:
        b.blocked.append("Planet Express is not installed on this host, so there is nothing to uninstall.")
        return
    # The snapshot runs a script from the install directory as the service user, so both are bound to the install
    # that was found, never to whatever the page says, and root is only the service user where it has to be.
    found_dir = existing.get("install_dir")
    if found_dir and a.install_dir != found_dir:
        b.blocked.append(f"Planet Express is installed at {found_dir}, not {a.install_dir}. Uninstall works on the install "
                         "that is actually here; set the install path back.")
        return
    if mos and a.run_as != "root":
        b.blocked.append("On MOS the service runs as root, so the uninstall runs as root too.")
        return
    if not mos and a.run_as == "root":
        b.blocked.append("The service does not run as root on this host; choose the account it runs as.")
        return
    b.snapshot_id = b.add(
        "state.snapshot", "Snapshot the current state first", a.install_dir,
        {"install_dir": a.install_dir, "run_user": a.run_as,
         "env": {"CASA_CONFIG": p["config"], "CASA_DATA_DIR": p["data"]}},
        b.text("Takes a snapshot of the current config and state before anything is removed. "
               "It reads what is there and writes only the snapshot."))
    hashes = existing.get("sha256", {})

    def remove(title: str, path: str, *, reload: bool = False, risk: str | None = None) -> None:
        if not b.present(path):
            return
        digest = hashes.get(path)
        if not digest:
            b.blocked.append(f"{path} exists but could not be read, so setup cannot prove what it is removing. "
                             "Run setup as root so it can read it.")
            return
        b.add("file.remove", title, path, {"path": path, "expected_sha256": digest, "reload_systemd": reload},
              b.text(f"Removes {path}. A private copy is kept first, so undo can put it back."),
              risk=risk, depends_on=())

    names = ("casa-dashboard", "casa-planetexpress")
    stacks_unit = "/etc/systemd/system/casa-stacks.service"
    if not mos and b.present(stacks_unit):
        # casa-stacks may be an operator's own unit that install kept rather than overwrote. It is only ours if it
        # is exactly what setup would have written.
        from scripts.render_template import render
        mine = render(b.read("systemd/casa-stacks.service.template"), INSTALL_DIR=a.install_dir, RUN_USER=a.run_as,
                      RUN_GROUP=a.run_group or a.run_as, CONFIG_FILE=p["config"], DASHBOARD_PORT=str(a.dashboard_port))
        if hashes.get(stacks_unit) == hashlib.sha256(mine.encode()).hexdigest():
            names += ("casa-stacks",)
        else:
            b.not_touched.append("casa-stacks.service is not what Planet Express writes, so it is treated as yours and kept.")
    if mos:
        hook_dir = "/boot/optional/scripts"
        hooks = {n: hashes[f"{hook_dir}/{n}"] for n in ("post-start.sh", "shutdown.sh")
                 if b.present(f"{hook_dir}/{n}") and f"{hook_dir}/{n}" in hashes}
        unreadable = [n for n in ("post-start.sh", "shutdown.sh")
                      if b.present(f"{hook_dir}/{n}") and f"{hook_dir}/{n}" not in hashes]
        if unreadable:
            b.blocked.append(f"{', '.join(unreadable)} exists in {hook_dir} but could not be read. "
                             "Run setup as root so it can read the boot hooks.")
        elif hooks:
            # First, so a reboot cannot bring the services back while the rest is being removed.
            b.add("boot_hook.remove", "Stop restoring Planet Express at boot", hook_dir,
                  {"dest_dir": hook_dir, "hooks": hooks},
                  b.text("Removes the Planet Express block from the MOS boot hooks. Anything else in those files stays."))
        for name in names:
            if b.present(f"/etc/init.d/{name}"):
                b.add("service.disable", f"Stop {name}", name, {"flavour": "sysvinit", "name": name},
                      b.text(f"Stops {name} if it is running."))
        for name in names:
            remove(f"Remove the {name} init script", f"/etc/init.d/{name}")
            remove(f"Remove /etc/default/{name}", f"/etc/default/{name}")
    else:
        for name in names:
            if b.present(f"/etc/systemd/system/{name}.service"):
                b.add("service.disable", f"Stop and disable {name}", name, {"flavour": "systemd", "name": name},
                      b.text(f"Stops {name} and turns off its start at boot."))
        for name in names:
            remove(f"Remove the {name} unit", f"/etc/systemd/system/{name}.service", reload=True)
        remove("Remove the sudo grant", "/etc/sudoers.d/planetexpress", risk="R3")
    if not any(step.kind != "state.snapshot" for step in b.steps) and not b.blocked:
        b.warnings.append("No Planet Express services, units or grants were found to remove. Only the "
                          "files you wrote (config, secrets, state, the checkout) are here, and those are kept.")
    b.not_touched += [
        f"Your configuration ({p['config']}) and the secret files are kept, so a later install picks up where you left off.",
        f"State, logs and the database under {a.install_dir} and {p['home']} are kept.",
        "The checkout and its Python environment are kept. Delete them yourself if you want them gone.",
        "The dashboard's account and group are left in place; on their own they grant nothing.",
        "No container, compose file or Docker setting is touched.",
        "Snapshots (including the one taken first) are kept.",
    ]
    b.warnings.append("After this the services no longer start at boot" + (" and MOS no longer restores them" if mos else "")
                      + ". Undo, or running setup again, puts them back.")


def plan(discovery: dict, answers: SetupAnswers, *, repo_root: str | None = None, read=None) -> Plan:
    """The plan for this host and these answers. `read(relative_path)` supplies repo files (templates,
    init scripts); it defaults to reading them from `repo_root`."""
    repo_root = repo_root or str(Path(__file__).resolve().parents[2])
    read = read or (lambda rel: (Path(repo_root) / rel).read_text())
    b = _Builder(discovery, answers, repo_root, read)
    b.p = _paths(b)
    if answers.story == "uninstall":
        _uninstall(b)
        if b.blocked:
            return Plan(answers.story, (), tuple(b.warnings), (), tuple(b.blocked))
        return Plan(answers.story, tuple(b.steps), tuple(b.warnings), tuple(b.not_touched), ())
    _check_preconditions(b)
    if b.blocked:
        return Plan(answers.story, (), tuple(b.warnings), (), tuple(b.blocked))
    try:
        _build(b)
    except ValidationError as exc:
        # A step the catalogue refuses is a problem with this checkout (an old template or hook script), not
        # something the operator can answer their way out of. Say so, plainly, instead of a traceback.
        first = exc.errors()[0]
        return Plan(answers.story, (), tuple(b.warnings), (),
                    ("This checkout cannot produce a valid plan: " + str(first["msg"]).removeprefix("Value error, ")
                     + ". Update the checkout and try again.",))
    if b.blocked:                              # raised while building steps: nothing is offered for approval
        return Plan(answers.story, (), tuple(b.warnings), (), tuple(b.blocked))
    return Plan(answers.story, tuple(b.steps), tuple(b.warnings), tuple(b.not_touched), tuple(b.blocked), b.secrets)

"""Local capability and evidence descriptions; never trust exported support claims."""

from .portfolio import CONFIG
from .strategies import content_hash


def strategy_identity(strategy):
    """Additive evidence metadata; does not change the shared strategy package."""
    from . import __version__
    from .portfolio import VERSION

    return dict(strategy_id=strategy.strategy_id, package_schema=strategy.schema,
                package_sha256=strategy.digest, rules_sha256=content_hash(strategy.effective_rules),
                strategy_version=strategy.version, decision_engine=strategy.engine,
                profile=strategy.profile, replay_engine=VERSION, app_version=__version__)


def reviewed_eligible(strategy):
    return (
        strategy.compatibility == "monitoring-ready"
        and strategy.profile == "ladder-v1"
        and strategy.strategy_id == "core-frequency"
        and strategy.effective_rules == CONFIG
    )


def support_label(strategy):
    if reviewed_eligible(strategy):
        return "Reviewed eligible"
    if strategy.compatibility == "monitoring-ready":
        return "Research · monitoring"
    return "Research · inspection"


def support_text(strategy):
    eligible = reviewed_eligible(strategy)
    monitoring = strategy.compatibility == "monitoring-ready"
    version = strategy.version or "not supplied (legacy handoff)"
    lines = [
        support_label(strategy),
        "",
        f"Strategy: {strategy.strategy_id} · version: {version}",
        f"Schema: {strategy.schema}",
        f"Local engine: {strategy.engine or 'unavailable'} · profile: {strategy.profile}",
        f"Package SHA-256: {strategy.digest}",
        f"Rules SHA-256: {content_hash(strategy.effective_rules)}",
        "The package hash identifies all imported content, including labels and metadata. "
        "The rules hash identifies the parsed rules only; it is not an execution permission.",
        "",
        "Local monitoring: " + ("supported; uses independent observations." if monitoring
                                 else "unavailable. " + strategy.reason),
        "Offline portfolio replay: " + (
            "pinned rules covered by 31 reference scenarios / 400 frames."
            if eligible else "not verified for this definition by the pinned reference suite."),
        "Unsigned rehearsal: " + (
            "supported through Agent trading → Candidates or Worker rehearsal; "
            "requires current observations and allocation checks. A ready report is historical evidence."
            if eligible else "unavailable for this agent; monitoring is advisory research only."),
        "Reviewed execution: " + (
            "eligible for setup, subject to fresh route, wallet, allocation and risk checks. "
            "Every order needs explicit approval. Eligibility is not funded acceptance."
            if eligible else "unavailable. Only the exact pinned core-frequency rules with "
            "simulation_unverified entry policy and no trial overlays are eligible."),
        "",
        "Agent routes: " + ("active native-SOL Pump curve entries/exits; supported canonical "
            "native-SOL PumpSwap migration exits only. Standard SPL and the narrow immutable "
            "Token-2022 metadata subset. Each mint/pool is checked afresh; unsupported extensions, "
            "pool modes, post-graduation entries and other DEX routes are unavailable."
            if eligible else "none authorized by this configuration. Research discovery coverage "
            "does not establish executable route support."),
        "Evidence: deployed unsigned route samples exist for supported curves and PumpSwap exits; "
        "they do not verify every mint, pool or strategy. Imported evidence is not independently "
        "authenticated by this importer. Public performance remains simulated or untested.",
        "Funded agent lifecycle acceptance: pending. Automatic execution: disabled.",
        "",
        "Next: inspect the configuration and evidence, import locally, then evaluate supported "
        "replay/shadow paths. Set wallet limits and allocations before any separately authorized "
        "order. Importing does not start tracking, reserve funds or authorize signing.",
    ]
    return "\n".join(lines)


def import_comparison(library, strategy):
    """Compare immutable saved packages without replacing any existing agent."""
    from .strategies import parse_package

    previous = [row for row in library.rows() if row["strategy_id"] == strategy.strategy_id]
    if any(row["digest"] == strategy.digest for row in previous):
        return "This exact package is already imported. Existing agent settings are retained."
    if not previous:
        return "First local package for this strategy ID. No prior local version to compare."
    lines = ["New immutable package for an existing strategy ID. Existing agents keep their package."]
    for row in previous:
        old = parse_package(library.strategy(row["digest"])["package"])
        keys = sorted(key for key in old.effective_rules.keys() | strategy.effective_rules.keys()
                      if key not in old.effective_rules or key not in strategy.effective_rules
                      or old.effective_rules[key] != strategy.effective_rules[key])
        changes = "Changed rule fields: " + ", ".join(keys) if keys else (
            "Rules unchanged; package metadata differs. Recheck engine/profile and trial support.")
        lines.append(f"Compared with {row['digest']}: {changes}")
    return "\n".join(lines)

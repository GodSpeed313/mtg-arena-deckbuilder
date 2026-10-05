"""Analysis 5's narrow, deterministic evidence normalization and scope authority.

No card grammar, availability inference by consumers, or gameplay evaluation.
"""
from dataclasses import asdict, fields, is_dataclass
import json
import re
from functools import lru_cache
from typing import get_type_hints, get_origin, get_args
from types import UnionType

from services.abilities import Condition, Cost, Effect, Qualifier, TokenSpec, Trigger, UnsupportedRemainder


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def normalize(value):
    """Normalize the declared evidence tree, never arbitrary iterables or sets."""
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) in (tuple, list):
        return [normalize(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: normalize(item) for key, item in value.items()}
    raise ValueError("evidence is not a normalized JSON tree")


def _closed(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("evidence record has missing or unexpected fields")
    return value


@lru_cache(maxsize=None)
def _types(cls):
    return get_type_hints(cls)


def _typed(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        return any(_typed(value, item) for item in args)
    if origin is tuple:
        return type(value) is list and all(_typed(item, args[0]) for item in value)
    if is_dataclass(annotation):
        if type(value) is not dict:
            return False
        _record(value, annotation)
        return True
    return type(value) is annotation


def _record(value, cls):
    _closed(value, [field.name for field in fields(cls)])
    if any(not _typed(value[key], annotation) for key, annotation in _types(cls).items()):
        raise ValueError("evidence scalar or sequence has an invalid type")
    encoded(value)
    return value


def context_from_ability(ability, effect=None):
    return require_context({"origin": {
        "kind": "ability", "ability_id": ability.ability_id,
        "source_index": ability.source_index, "ability_kind": ability.kind,
        "parse_status": ability.parse_status,
        "trigger": asdict(ability.trigger) if ability.trigger else None,
        "costs": [asdict(item) for item in ability.costs],
        "qualifiers": [asdict(item) for item in ability.qualifiers],
        "unsupported_remainder": [asdict(item) for item in ability.unsupported_remainder],
    }, "effect": asdict(effect) if effect else None})


def structural_context(types):
    if type(types) not in (list, tuple, set, frozenset) or any(type(item) is not str for item in types):
        raise ValueError("structural types must be declared membership")
    if len(types) != len(set(types)):
        raise ValueError("structural type membership is duplicated")
    return require_context({"origin": {"kind": "card_type", "types": sorted(set(types))},
                            "effect": None})


def require_context(value):
    value = normalize(value)
    _closed(value, ("origin", "effect"))
    origin = value["origin"]
    if type(origin) is not dict:
        raise ValueError("origin is required")
    if origin.get("kind") == "card_type":
        _closed(origin, ("kind", "types"))
        types = origin["types"]
        if (type(types) is not list or not types or
                any(type(item) is not str or not item for item in types) or
                types != sorted(set(types)) or value["effect"] is not None):
            raise ValueError("structural origin is malformed")
        return value
    _closed(origin, ("kind", "ability_id", "source_index", "ability_kind", "parse_status",
                     "trigger", "costs", "qualifiers", "unsupported_remainder"))
    if origin["kind"] != "ability" or type(origin["source_index"]) is not int or origin["source_index"] < 1:
        raise ValueError("ability origin is malformed")
    if origin["ability_id"] != f"ability.{origin['source_index']:03d}":
        raise ValueError("ability reference contradicts source index")
    if origin["ability_kind"] not in ("unsupported", "spell_effect", "static_keyword", "triggered", "activated"):
        raise ValueError("unsupported originating kind contract")
    if origin["parse_status"] not in ("supported", "partial", "unsupported", "anomalous"):
        raise ValueError("unsupported parse status contract")
    for key in ("costs", "qualifiers", "unsupported_remainder"):
        if type(origin[key]) is not list:
            raise ValueError("origin sequences must be lists")
    for item in origin["costs"]:
        _record(item, Cost)
    for item in origin["qualifiers"]:
        _record(item, Qualifier)
    for item in origin["unsupported_remainder"]:
        _record(item, UnsupportedRemainder)
        if item["scope"] not in ("ability", "trigger", "cost", "effect") or item["reason"] not in (
            "unsupported_modal", "canonical_anomaly", "unsupported_trigger", "unsupported_cost",
            "unsupported_clause", "unsupported_token", "unsupported_noncreature_token",
        ):
            raise ValueError("unsupported remainder contract")
    if ((origin["parse_status"] == "partial" and not origin["unsupported_remainder"]) or
            (origin["parse_status"] == "supported" and origin["unsupported_remainder"])):
        raise ValueError("parse status contradicts unsupported remainder")
    trigger = origin["trigger"]
    if trigger is not None:
        _record(trigger, Trigger)
        for condition in trigger["conditions"]:
            _record(condition, Condition)
    if origin["ability_kind"] == "triggered" and trigger is None:
        raise ValueError("triggered origin lacks trigger")
    if origin["ability_kind"] == "activated" and not origin["costs"]:
        # Contract Amendment 1: preserve an unparsed activation cost only as
        # explicit partial source evidence, never as established availability.
        explained_cost = any(item["scope"] == "cost" and item["reason"] == "unsupported_cost"
                             for item in origin["unsupported_remainder"])
        if origin["parse_status"] != "partial" or not explained_cost:
            raise ValueError("activated origin lacks parsed costs or partial unsupported-cost evidence")
    if origin["ability_kind"] == "spell_effect" and (trigger is not None or any(
            item["timing"] == "activated" for item in origin["costs"])):
        raise ValueError("spell origin contradicts prerequisites")
    effect = value["effect"]
    if effect is not None:
        _record(effect, Effect)
        if not re.fullmatch(re.escape(origin["ability_id"]) + r"\.effect\.[0-9]{3,}", effect["effect_id"]):
            raise ValueError("effect reference contradicts origin")
        for key, cls in (("conditions", Condition), ("qualifiers", Qualifier)):
            if type(effect[key]) is not list:
                raise ValueError("effect sequence is malformed")
            for item in effect[key]:
                _record(item, cls)
        if effect["token"] is not None:
            _record(effect["token"], TokenSpec)
    return value


def assessment(value):
    value = require_context(value)
    origin, effect = value["origin"], value["effect"]
    prerequisites = {"trigger": None, "costs": [], "conditions": [], "qualifiers": []}
    reasons = []
    if origin["kind"] == "ability":
        prerequisites = {
            "trigger": origin["trigger"], "costs": origin["costs"],
            "conditions": effect["conditions"] if effect else [],
            "qualifiers": origin["qualifiers"] + (effect["qualifiers"] if effect else []),
        }
        if origin["ability_kind"] == "unsupported":
            reasons.append("unsupported_ability_kind")
        if origin["parse_status"] in ("unsupported", "anomalous"):
            reasons.append("unsupported_parse_status")
        if origin["unsupported_remainder"]:
            reasons.append("unsupported_remainder_present")
    return {
        "establishment": "unestablished" if reasons else "established", "reasons": reasons,
        "availability": "unestablished" if reasons else "conditional" if any(prerequisites.values()) else "unconditional",
        "prerequisites": prerequisites,
    }


def context_view(value):
    """Owner-derived compatibility view; never a wire contract or writable fact."""
    value = require_context(value)
    origin = value["origin"]
    return {**assessment(value), "effect": value["effect"],
            "ability_kind": origin.get("ability_kind", "card_type"),
            "parse_status": origin.get("parse_status", "supported"),
            "unsupported_remainder": origin.get("unsupported_remainder", [])}


def semantic_feature(feature):
    keys = ("rule_id", "dimension", "label", "relationship", "evidence")
    if type(feature) is not dict or any(type(feature.get(key)) is not str or not feature[key] for key in keys):
        raise ValueError("semantic feature fields must be nonempty strings")
    result = {key: feature[key] for key in keys}
    if "dependency_context" in feature:
        result["dependency_context"] = require_context(feature["dependency_context"])
        validate_feature_context(result)
    return normalize(result)


def validate_feature_context(feature):
    """Reconcile declared dependency features with their captured source records."""
    context = require_context(feature["dependency_context"])
    origin, effect = context["origin"], context["effect"]
    rule = feature["rule_id"]
    if rule == "type.spells.v1":
        if origin["kind"] != "card_type" or not set(origin["types"]) & {"Instant", "Sorcery"}:
            raise ValueError("spell membership contradicts structural origin")
        return context
    if origin["kind"] != "ability":
        raise ValueError("ability feature has a structural origin")
    effect_kinds = {"effect.lifegain.v1": "gain_life", "effect.counter.v1": "put_counter",
                    "effect.token.v1": "create_creature_token", "effect.noncreature_token.v1": "create_noncreature_token"}
    if rule in effect_kinds and (effect is None or effect["kind"] != effect_kinds[rule]):
        raise ValueError("recognized effect contradicts feature rule")
    trigger_events = {"trigger.lifegain.v1": "life_gained", "trigger.counter.v1": "counter_placed",
                      "trigger.spells.v1": "spell_cast", "trigger.token_draw.v1": "token_enters"}
    if rule in trigger_events and (origin["trigger"] is None or origin["trigger"]["event"] != trigger_events[rule]):
        raise ValueError("recognized trigger contradicts feature rule")
    if rule.startswith("effect.") and effect is None:
        raise ValueError("effect feature lacks its effect")
    if rule.startswith(("trigger.", "cost.")) and effect is not None:
        raise ValueError("trigger/cost feature must retain its origin without an effect")
    from services.dependencies import _context_matches_rule
    if not _context_matches_rule(feature, context_view(context), payoff=rule.startswith(("trigger.", "cost."))):
        raise ValueError("feature contradicts its reviewed effect/trigger/cost predicate")
    return context


def feature_sort_key(feature):
    context = feature.get("dependency_context", {})
    origin, effect = context.get("origin", {}), context.get("effect") or {}
    return (origin.get("source_index", 0), origin.get("ability_id", ""),
            effect.get("effect_id", ""), feature["rule_id"], feature["dimension"],
            feature["relationship"], encoded(feature))


def _side(side):
    return {
        "relationship": side["relationship"],
        "acceptable_feature_rule_ids": sorted(side["acceptable_feature_rule_ids"]),
        "matching_semantics": side["matching_semantics"], "copy_count": side["copy_count"],
        "cards": [{"title_id": card["title_id"], "quantity": card["quantity"],
                   "evidence": sorted([semantic_feature(feature) for feature in card["evidence"]], key=feature_sort_key)}
                  for card in sorted(side["cards"], key=lambda card: card["title_id"])],
    }


def source_need(analysis, zone, finding):
    from mtgadb.deck_identity import require_deck_snapshot_identity
    boundary = {key: item for key, item in finding["evidence_boundary"].items() if key != "rules_text_coverage"}
    boundary["rules_text_coverage"] = {key: item for key, item in finding["evidence_boundary"]["rules_text_coverage"].items()
                                       if key not in ("structural_recognition", "meaningful_understanding")}
    boundary = normalize(boundary)
    result = {key: finding[key] for key in (
        "finding_id", "finding_type", "dependency_id", "dependency_state", "dependency_policy",
        "existing_side_name", "missing_side_name")}
    result.update(zone=zone, existing_side=_side(finding["existing_side"]),
                  missing_side=_side(finding["missing_side"]), evidence_boundary=boundary,
                  source_identity={"analyzed_deck_identity": require_deck_snapshot_identity(analysis["analyzed_deck_identity"]),
                                   "analysis_version": analysis["analysis_version"],
                                   "dependency_model_version": analysis["dependency_model_version"],
                                   "needs_model_version": analysis["needs_model_version"]})
    return require_source_need(result)


def require_source_need(value):
    from mtgadb.deck_identity import require_deck_snapshot_identity
    value = normalize(value)
    _closed(value, ("zone", "finding_id", "finding_type", "dependency_id", "dependency_state",
                   "dependency_policy", "existing_side_name", "existing_side", "missing_side_name",
                   "missing_side", "evidence_boundary", "source_identity"))
    if (value["zone"] not in ("main", "sideboard", "commander") or value["finding_type"] != "support_need"
            or value["dependency_state"] not in ("payoff_without_enabler", "payoff_without_compatible_enabler")
            or value["dependency_policy"] != "strict" or value["existing_side_name"] != "payoff"
            or value["missing_side_name"] != "enabler"):
        raise ValueError("source need semantics are malformed")
    identity = _closed(value["source_identity"], ("analyzed_deck_identity", "analysis_version", "dependency_model_version", "needs_model_version"))
    require_deck_snapshot_identity(identity["analyzed_deck_identity"])
    if (identity["analysis_version"], identity["dependency_model_version"], identity["needs_model_version"]) != ("5", "3", "3"):
        raise ValueError("source need versions are unsupported")
    from services.dependencies import DEPENDENCIES
    definition = next((item for item in DEPENDENCIES if item.dependency_id == value["dependency_id"]), None)
    if definition is None or value["finding_id"] != f"need.{definition.label}.enabler.v{definition.dependency_id.rsplit('.v', 1)[1]}":
        raise ValueError("source need identity contradicts registry")
    for name, relationship in (("existing_side", definition.payoff_relationship), ("missing_side", definition.enabler_relationship)):
        side = _closed(value[name], ("relationship", "acceptable_feature_rule_ids", "matching_semantics", "copy_count", "cards"))
        rule_ids = side["acceptable_feature_rule_ids"]
        if (side["relationship"] != relationship or side["matching_semantics"] != "any" or
                type(rule_ids) is not list or not rule_ids or any(type(rule) is not str for rule in rule_ids) or rule_ids != sorted(set(rule_ids)) or
                type(side["copy_count"]) is not int or side["copy_count"] < 0):
            raise ValueError("source side requirements are malformed")
        expected = definition.payoff_rule_ids if name == "existing_side" else definition.enabler_rule_ids
        if (name == "existing_side" and rule_ids != sorted(expected)) or not set(rule_ids) <= set(expected):
            raise ValueError("source rule requirements contradict registry")
        if type(side["cards"]) is not list:
            raise ValueError("source side cards must be a list")
        ids, total = [], 0
        for card in side["cards"]:
            _closed(card, ("title_id", "quantity", "evidence"))
            if type(card["title_id"]) is not int or card["title_id"] < 1 or type(card["quantity"]) is not int or card["quantity"] < 1:
                raise ValueError("source participant is malformed")
            ids.append(card["title_id"]); total += card["quantity"]
            if type(card["evidence"]) is not list or not card["evidence"]:
                raise ValueError("source participant lacks evidence")
            for feature in card["evidence"]:
                if encoded(feature) != encoded(semantic_feature(feature)) or feature["rule_id"] not in rule_ids or feature["relationship"] != relationship:
                    raise ValueError("source participant contradicts requirements")
            if card["evidence"] != sorted(card["evidence"], key=feature_sort_key):
                raise ValueError("source routes are not canonical")
        if ids != sorted(set(ids)) or total != side["copy_count"]:
            raise ValueError("source side quantities contradict participants")
    if not value["existing_side"]["cards"] or (value["dependency_state"] == "payoff_without_enabler" and value["missing_side"]["cards"]):
        raise ValueError("source state contradicts sides")
    if value["dependency_state"] == "payoff_without_compatible_enabler" and not value["missing_side"]["cards"]:
        raise ValueError("incompatible-enabler state requires observed enablers")
    from services.dependencies import _acceptable_enablers
    if value["missing_side"]["acceptable_feature_rule_ids"] != sorted(_acceptable_enablers(definition, value["existing_side"])):
        raise ValueError("source alternatives contradict reviewed payoff scope")
    boundary = _closed(value["evidence_boundary"], ("claim_scope", "zone_resolution", "unresolved_printing_copies", "unclassified_card_copies", "partially_classified_card_copies", "unsupported_text_card_copies", "rules_text_coverage"))
    if boundary["claim_scope"] != "reviewed_features_only" or boundary["zone_resolution"] not in ("resolved", "partial"):
        raise ValueError("source evidence boundary is malformed")
    for key in ("unresolved_printing_copies", "unclassified_card_copies", "partially_classified_card_copies", "unsupported_text_card_copies"):
        if type(boundary[key]) is not int or boundary[key] < 0:
            raise ValueError("source evidence count is malformed")
    coverage = boundary["rules_text_coverage"]
    counts = ("ability_count", "structurally_recognized_ability_count", "meaningfully_understood_ability_count", "supported_ability_count", "partial_ability_count", "unsupported_ability_count", "anomalous_ability_count")
    _closed(coverage, (*counts, "weighting"))
    if coverage["weighting"] != "card_copy_times_ability" or any(type(coverage[key]) is not int or coverage[key] < 0 for key in counts):
        raise ValueError("source coverage is malformed")
    if (sum(coverage[key] for key in counts[3:]) != coverage["ability_count"] or
            coverage["meaningfully_understood_ability_count"] != coverage["supported_ability_count"] + coverage["partial_ability_count"] or
            coverage["structurally_recognized_ability_count"] > coverage["ability_count"]):
        raise ValueError("source coverage counts contradict")
    return value


def retrieval(source):
    side = source["missing_side"]
    return {"feature_rule_ids": side["acceptable_feature_rule_ids"],
            "matching_semantics": side["matching_semantics"], "relationship": side["relationship"]}


def require_completeness(value, source):
    value = normalize(value)
    expected = {"status": "unknown", "reason": "reviewed_features_not_exhaustive",
                "source_evidence_boundary": source["evidence_boundary"]}
    if encoded(value) != encoded(expected):
        raise ValueError("scoped completeness contradicts source boundary")
    return value


def require_captured_evidence(source, evidence, support, completeness, deck_identity):
    source = require_source_need(source)
    if encoded(source["source_identity"]["analyzed_deck_identity"]) != encoded(deck_identity):
        raise ValueError("source scope deck contradicts captured context")
    evidence = normalize(evidence)
    if type(evidence) is not list or not evidence:
        raise ValueError("captured context requires matching routes")
    for feature in evidence:
        semantic_feature(feature)
        if (feature["rule_id"] not in retrieval(source)["feature_rule_ids"] or
                feature["relationship"] != retrieval(source)["relationship"]):
            raise ValueError("captured route contradicts source requirements")
    from services.candidate_comparison import _support_context
    if encoded(support) != encoded(_support_context(evidence, source)):
        raise ValueError("captured support contradicts matching evidence")
    require_completeness(completeness, source)
    return source


def verify_source_analysis(pools, analysis):
    if type(analysis) is not dict or (
        analysis.get("analysis_version"), analysis.get("dependency_model_version"), analysis.get("needs_model_version")
    ) != ("5", "3", "3"):
        raise ValueError("originating Analysis 5 / Dependencies 3 / Needs 3 is required")
    expected = {}
    for zone, data in analysis["zones"].items():
        for finding in data["needs"]:
            if finding["finding_type"] == "support_need":
                item = source_need(analysis, zone, finding)
                key = (zone, item["finding_id"], item["dependency_id"])
                if key in expected:
                    raise ValueError("source need is duplicated")
                expected[key] = item
    seen = set()
    for pool in pools:
        item = require_source_need(pool["source_need"])
        key = (item["zone"], item["finding_id"], item["dependency_id"])
        if key in seen or key not in expected or encoded(item) != encoded(expected[key]):
            raise ValueError("candidate scope contradicts originating analysis")
        seen.add(key)
    if seen != set(expected):
        raise ValueError("candidate pools omit originating support needs")

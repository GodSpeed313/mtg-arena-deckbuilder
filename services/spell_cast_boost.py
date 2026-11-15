"""Opt-in Run 25 cast relationships; no game-state or resolution simulator."""
from dataclasses import asdict
from contextlib import closing
import re

DEFAULT = "default"
EXPERIMENTAL = "experimental-spell-cast-boost-v1"
PROFILES = (DEFAULT, EXPERIMENTAL)
DEPENDENCY = "dependency.noncreature_cast_boost.v1"
ENABLER = "type.noncreature_spell.v1"
PAYOFF = "trigger.noncreature_cast_boost.v1"
EFFECT = "temporary_self_pt_boost"
CONTRACT = dict(profile=EXPERIMENTAL, evidence_version="run25-boost.1",
                dependency_model_version="5", base_dependency_model_version="4")
SPELL_TYPES = {"Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker", "Battle"}
BOUNDARY = ("Potential normal-route cast relationship only. Requires a legal cast by the "
            "payoff controller while the payoff is on the battlefield, then trigger resolution. "
            "No legal-target availability, actual cast, resolution, damage, or competitive value "
            "is established. A copy not cast creates no cast event; later target illegality "
            "does not undo an earlier legal cast. Alternative casting routes are unassessed.")


def check_profile(profile):
    if profile not in PROFILES:
        raise ValueError("unknown generator recognition profile")


def face_evidence(row):
    """Require explicit Arena structural facts, never infer a face from its name."""
    if (not isinstance(row, dict) or type(row.get("GrpId")) is not int
            or type(row.get("TitleId")) is not int
            or row.get("LinkedFaceGrpIds") != ""
            or type(row.get("LinkedFaceType")) is not int or row["LinkedFaceType"] != 0
            or type(row.get("IsPrimaryCard")) is not int or row["IsPrimaryCard"] != 1):
        return None
    return {k: row[k] for k in ("GrpId", "TitleId", "LinkedFaceGrpIds", "LinkedFaceType", "IsPrimaryCard")}


def load_cast_evidence(path):
    from mtgadb.standard_legality import readonly
    with closing(readonly(path)) as con:
        return {r["GrpId"]: face_evidence(dict(r)) for r in con.execute(
            'SELECT GrpId, TitleId, LinkedFaceGrpIds, LinkedFaceType, IsPrimaryCard FROM Cards')}


def parse_boost(text, index, name, types, *, profile):
    check_profile(profile)
    if profile == DEFAULT or "Creature" not in types:
        return None
    from services.abilities import Ability, Trigger, Effect, Qualifier
    prowess = re.fullmatch(r"Prowess(?: \(Whenever you cast a noncreature spell, this creature gets \+1/\+1 until end of turn\.\))?", text)
    match = re.fullmatch(r"Whenever you cast a noncreature spell, (?:this creature|" +
        re.escape(name) + r") gets \+(\d+)/\+(\d+) until end of turn\.", text)
    if not prowess and not match:
        return None
    power, toughness = (1, 1) if prowess else tuple(int(v) for v in match.groups())
    if power < 1:
        return None
    aid = f"ability.{index:03d}"
    qualifiers = tuple(Qualifier(k, v, text) for k, v in (
        ("power_delta", str(power)), ("toughness_delta", str(toughness)),
        ("duration", "until_end_of_turn"), ("mechanic_origin", "prowess" if prowess else "explicit_trigger")))
    return Ability(aid, index, "triggered", text, "supported",
        trigger=Trigger("spell_cast", "noncreature_spell", "you", True, text),
        effects=(Effect(aid + ".effect.001", EFFECT, text, target="self", source="ability_source",
                        qualifiers=qualifiers),))


def validate_feature(feature, *, profile):
    """Separate, closed experimental contract; legacy semantic decoding stays closed."""
    check_profile(profile)
    if profile != EXPERIMENTAL or feature.get("boost_contract") != CONTRACT:
        raise ValueError("boost evidence requires its experimental version contract")
    from services.evidence_scope import require_context, normalize
    allowed = {"rule_id", "dimension", "label", "relationship", "evidence", "explanation",
               "dependency_context", "boost_contract", "cast_evidence", "source_title_id", "source_name", "source_types"}
    if set(feature) != allowed or type(feature["source_title_id"]) is not int:
        raise ValueError("malformed boost feature")
    cast = face_evidence(feature["cast_evidence"])
    if cast is None or cast != feature["cast_evidence"] or cast["TitleId"] != feature["source_title_id"]:
        raise ValueError("unverified ordinary single-face origin")
    if type(feature["source_name"]) is not str or not feature["source_name"] or type(feature["source_types"]) is not str:
        raise ValueError("missing source characteristics")
    c = require_context(feature["dependency_context"])
    o, e = c["origin"], c["effect"]
    if feature["dimension"] != "theme" or feature["label"] != "spells" or feature["explanation"] != BOUNDARY:
        raise ValueError("contradictory boost scope")
    if feature["rule_id"] == ENABLER:
        if (feature["relationship"] != "enabler" or o["kind"] != "card_type" or e is not None
                or not set(o["types"]) & SPELL_TYPES or set(o["types"]) & {"Creature", "Land"}
                or feature["evidence"] != feature["source_types"]
                or sorted(feature["evidence"].split()) != o["types"]):
            raise ValueError("noncreature membership contradicts origin")
    elif feature["rule_id"] == PAYOFF:
        if ("Creature" not in feature["source_types"].split() or feature["relationship"] != "payoff" or o["kind"] != "ability"
                or o["ability_kind"] != "triggered" or o["parse_status"] != "supported"
                or o["costs"] or o["qualifiers"] or o["unsupported_remainder"] or e is None):
            raise ValueError("boost origin is not a supported triggered ability")
        t = o["trigger"]
        if t != dict(event="spell_cast", subject="noncreature_spell", controller="you", friendly=True,
                     evidence=feature["evidence"], conditions=[]):
            raise ValueError("boost trigger mismatch")
        # Exact source text is parsed, never generated explanations. This also reconciles deltas,
        # duration, source identity and prowess provenance against the reviewed grammar.
        source = feature["evidence"]
        parsed = parse_boost(source, o["source_index"], feature["source_name"], set(feature["source_types"].split()), profile=profile)
        if parsed is None or normalize(asdict(parsed.effects[0])) != e:
            raise ValueError("boost effect contradicts exact source evidence")
    else:
        raise ValueError("unknown boost feature")
    return c


def project_features(card, abilities, cast, *, profile):
    check_profile(profile)
    if profile == DEFAULT or face_evidence(cast) is None or cast["TitleId"] != card.title_id or card.anomaly_count:
        return []
    from services.evidence_scope import structural_context, context_from_ability
    types = set(card.types.split())
    features = []
    def add(rule, relationship, evidence, context):
        f = dict(rule_id=rule, dimension="theme", label="spells", relationship=relationship,
                 evidence=evidence, explanation=BOUNDARY, dependency_context=context,
                 boost_contract=dict(CONTRACT), cast_evidence=dict(cast), source_title_id=card.title_id, source_name=card.name, source_types=card.types)
        validate_feature(f, profile=profile)
        features.append(f)
    if types & SPELL_TYPES and not types & {"Creature", "Land"}:
        add(ENABLER, "enabler", card.types, structural_context(types))
    for a in abilities:
        for e in a.effects:
            if e.kind == EFFECT:
                add(PAYOFF, "payoff", a.raw_text, context_from_ability(a, e))
    return features


def matches(feature, *, payoff):
    try:
        validate_feature(feature, profile=EXPERIMENTAL)
        return feature["rule_id"] == (PAYOFF if payoff else ENABLER)
    except (ValueError, TypeError, KeyError):
        return False


def boost_findings(cards, *, profile):
    if profile == DEFAULT:
        return []
    from services.dependencies import DependencyDefinition, _pairs, _side
    definition = DependencyDefinition(DEPENDENCY, "noncreature_cast_boost", (ENABLER,), "enabler",
                                      (PAYOFF,), "payoff", BOUNDARY)
    scoped = []
    for card in cards:
        fs = []
        for f in card["features"]:
            if (f.get("source_title_id") == card["title_id"] and
                    (matches(f, payoff=False) or matches(f, payoff=True)) and f not in fs):
                fs.append(f)
        scoped.append({**card, "features": fs})
    enabler = _side(scoped, (ENABLER,), "enabler")
    payoff = _side(scoped, (PAYOFF,), "payoff")
    if not enabler["cards"] and not payoff["cards"]:
        return []
    pairs = [p for p in _pairs(scoped, definition) if p["enabler_title_id"] != p["payoff_title_id"]]
    # Availability of a legal cast and trigger resolution are deliberately not established.
    for p in pairs:
        p["support"] = "conditional"
    state = "conditionally_supported" if pairs else "payoff_without_enabler" if payoff["cards"] and not enabler["cards"] else "payoff_without_compatible_enabler" if payoff["cards"] else "enabler_without_payoff"
    return [dict(dependency_id=DEPENDENCY, label=definition.label, policy="strict", state=state,
                 explanation=BOUNDARY, enabler_side=enabler, payoff_side=payoff, compatible_pairs=pairs,
                 boost_contract=dict(CONTRACT))]

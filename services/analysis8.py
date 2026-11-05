"""Explicit opt-in recognition only; downstream results remain Analysis 5."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict

from mtgadb.query import CardQueryEngine
from services.abilities import decompose_abilities, _parse_line
from services.evidence_scope import normalize
from services.intelligence import analyze_deck

GRAMMAR_VERSION = "8"


def decompose_abilities8(text, *, card_name="", card_types="", canonical_anomaly=False):
    return decompose_abilities(text, card_name=card_name, card_types=card_types,
                               canonical_anomaly=canonical_anomaly, grammar_version=GRAMMAR_VERSION)


def parse_entry8(text, *, ability_id, source_index, card_name, card_types):
    """One already-normalized source entry; no splitting or generic-text fallback."""
    return _parse_line(text, ability_id=ability_id, source_index=source_index,
                       card_name=card_name, card_types=card_types, grammar_version=GRAMMAR_VERSION)


def family(ability):
    for remainder in ability["unsupported_remainder"]:
        prefix = "analysis8_unmodeled_"
        if remainder["reason"].startswith(prefix):
            return remainder["reason"][len(prefix):]
    return None


def recognition_record(ability):
    record = normalize(asdict(ability))
    name = family(record)
    detail = None
    if name:
        value = record["keywords"][0]["value"]
        detail = {"family": name, "completeness": "partial", "gameplay_state": "not_evaluated"}
        if name == "equip":
            detail["mana_cost"] = value
        elif name == "crew":
            detail["literal_integer"] = int(value)
        elif name == "enchant":
            detail["object_word"] = value
    return {"ability": record, "recognition_detail": detail}


def coverage(cards, weighted=False):
    counts = Counter(supported=0, partial=0, unsupported=0, anomalous=0)
    for card in cards:
        for record in card["entries"]:
            counts[record["ability"]["parse_status"]] += card["quantity"] if weighted else 1
    return {"total": sum(counts.values()), "outcomes": dict(counts),
            "weighting": "card_copy_times_ability" if weighted else "distinct_ability_per_title"}


def analyze_deck8(deck, con):
    """Add a nonpersistable recognition layer; never replace Analysis 5 evidence.

    Dependencies/Needs and every other base field retain their Analysis 5 source.
    This does not run downstream projections on the new recognition records.
    """
    if con.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("read-only canonical connection required")
    prior = analyze_deck(deck, con)
    result = deepcopy(prior)
    engine = CardQueryEngine(con)
    zones = {}
    for zone in ("main", "sideboard", "commander"):
        catalog = {}
        for gid in sorted(getattr(deck, zone)):
            card = engine.by_arena_id(gid)
            if card is not None:
                catalog[card.title_id] = card
        cards = []
        for old in prior["zones"][zone]["cards"]:
            card = catalog[old["title_id"]]
            abilities = decompose_abilities8(card.rules_text, card_name=card.name,
                                             card_types=card.types,
                                             canonical_anomaly=card.resolution.value == "anomaly")
            cards.append({"title_id": card.title_id, "quantity": old["quantity"],
                          "entries": [recognition_record(a) for a in abilities]})
        zones[zone] = {"cards": cards, "distinct": coverage(cards),
                       "copy_weighted": coverage(cards, True)}
    result["recognition_analysis"] = {
        "grammar_version": GRAMMAR_VERSION, "base_analysis_version": "5",
        "persistence": "measurement_only_not_accepted_as_evidence",
        "downstream_source": "unchanged_analysis5", "zones": zones,
    }
    return result

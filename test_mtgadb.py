"""Tests for the canonical layer.

Ground truth is real cards whose text and colours are known independently of
Arena's database, so a regression in the join or the normalizer shows up as a
wrong card rather than merely a different number.

Run: python test_mtgadb.py   (requires `python build_db.py` first)
"""

from __future__ import annotations

import sys
from pathlib import Path

from mtgadb import canonical
from mtgadb.model import Resolution
from mtgadb.providers.arena_cards import ArenaSQLiteCardProvider
from mtgadb.query import CardQueryEngine
from mtgadb.text import (
    cmc,
    normalize_colors,
    normalize_cost,
    normalize_text,
    parse_mana,
    self_reference_names,
    strip_markup,
)

RESULTS: list[bool] = []


def check(label: str, got, want) -> None:
    ok = got == want
    RESULTS.append(ok)
    status = "PASS" if ok else "FAIL"
    detail = "" if ok else f"  got {got!r} want {want!r}"
    print(f"  [{status}] {label}{detail}")


def test_text() -> None:
    print("text normalization:")
    check("mana cost", normalize_cost("o4oG"), "{4}{G}")
    check("hybrid cost", normalize_cost("o2o(G/W)oU"), "{2}{G/W}{U}")
    check("braced symbols", normalize_text("{oT}: Add {oG}."), "{T}: Add {G}.")
    check(
        "multi-symbol brace",
        normalize_text("{oBoB}: Regenerate."),
        "{B}{B}: Regenerate.",
    )
    check("nobr stripped", strip_markup("<nobr>Cori-Steel</nobr> Cutter"),
          "Cori-Steel Cutter")
    check("italics stripped", normalize_text("<i>Many eyes.</i>"), "Many eyes.")
    # Two printings of Smuggler's Copter differ only by U+200E.
    check("invisible char", strip_markup("discard a card.‎"), "discard a card.")
    check("cmc X is zero", cmc(parse_mana("oXoR")), 1)
    check("cmc hybrid numeric", cmc(parse_mana("o(2/R)")), 2)
    check("cmc hybrid colored", cmc(parse_mana("o(G/W)")), 1)


def test_colors() -> None:
    print("colour decoding:")
    check("single", normalize_colors("3"), "B")
    # Arena does not order these canonically: Boros Charm is '4,1'.
    check("unordered pair", normalize_colors("4,1"), "WR")
    check("same set, other order", normalize_colors("1,4"), "WR")
    check("colorless", normalize_colors(""), "")
    check("all five", normalize_colors("5,4,3,2,1"), "WUBRG")


def test_self_reference() -> None:
    print("self-reference names:")
    check("comma form", self_reference_names("Sheoldred, the Apocalypse")[1],
          "Sheoldred")
    # 'Loran of the Third Path' has no comma; the first word is the fallback.
    check("no-comma form", self_reference_names("Loran of the Third Path")[-1],
          "Loran")


def test_resolution() -> None:
    print("canonical text resolution:")
    r = ArenaSQLiteCardProvider._resolve_text
    single = r("X", [(1, "a")])
    check("single printing", single[2], Resolution.SINGLE)
    agree = r("X", [(1, "a"), (2, "a")])
    check("printings agree", agree[2], Resolution.SINGLE)
    majority = r("X", [(1, "a"), (2, "a"), (3, "b")])
    check("majority wins text", majority[0], "a")
    check("majority flagged", majority[2], Resolution.MAJORITY)
    check("majority counts anomalies", majority[3], 1)
    # A 1-1 tie falls through to self-reference.
    tie = r("Loran of the Third Path",
            [(1, "When Garnet enters"), (2, "When Loran enters")])
    check("tie uses self-reference", tie[0], "When Loran enters")
    check("tie flagged", tie[2], Resolution.SELF_REFERENCE)
    unresolvable = r("Zzz", [(1, "aaa"), (2, "bbb")])
    check("unresolvable flagged", unresolvable[2], Resolution.ANOMALY)


def test_database(db: Path) -> None:
    print("canonical database:")
    con = canonical.open_db(db)
    q = CardQueryEngine(con)

    check("schema version", canonical.get_meta(con, "schema_version"),
          canonical.SCHEMA_VERSION)
    check("cards populated", q.count("cards") > 15000, True)
    check("printings exceed cards", q.count("printings") > q.count("cards"), True)

    sheoldred = q.by_name("Sheoldred, the Apocalypse")
    check("known card found", sheoldred is not None, True)
    check("mana cost", sheoldred.mana_cost, "{2}{B}{B}")
    check("cmc", sheoldred.cmc, 4)
    check("colour identity", sheoldred.color_identity, "B")
    check("power/toughness", (sheoldred.power, sheoldred.toughness), ("4", "5"))
    check("oracle text", sheoldred.rules_text.splitlines()[0], "Deathtouch")

    # The reskin trap: FCA prints this as "Thrum of the Vestige".
    bolt = q.by_name("Lightning Bolt")
    check("reskin rejected", bolt.rules_text,
          "Lightning Bolt deals 3 damage to any target.")
    check("bolt resolved by majority", bolt.resolution, Resolution.MAJORITY)

    # Majority beats self-reference here: 'this land' is current templating.
    terra = q.by_name("Terramorphic Expanse")
    check("modern templating kept", "this land" in terra.rules_text, True)

    boros = q.by_name("Boros Charm")
    check("multicolour normalized", boros.color_identity, "WR")

    check("symbols normalized in text", "{T}: Add {G}." in
          q.by_name("Llanowar Elves").rules_text, True)
    check("no raw o-encoding leaked",
          any("{o" in c.rules_text for c in q.find(limit=500)), False)

    print("query engine:")
    exact = q.find(colors=["R", "W"], color_mode="exact", types__contains="Instant")
    check("exact colour match", any(c.name == "Boros Charm" for c in exact), True)
    check("exact excludes mono", all(c.color_identity == "WR" for c in exact), True)

    mono_g = q.find(colors=["G"], color_mode="exact", cmc__lte=2,
                    types__contains="Creature")
    check("mono-green all green", all(c.color_identity == "G" for c in mono_g), True)
    check("cmc predicate honoured", all(c.cmc <= 2 for c in mono_g), True)

    subset = q.find(colors=["G"], color_mode="subset", limit=200)
    check("subset admits colorless",
          any(c.color_identity == "" for c in subset), True)
    check("subset excludes off-colour",
          all(set(c.color_identity) <= set("G") for c in subset), True)

    drawers = q.find(text="draw a card", limit=50)
    check("text search works", len(drawers) > 0, True)
    check("text search matches", all("draw a card" in c.rules_text.lower()
                                     for c in drawers), True)

    check("by_arena_id resolves printing", q.by_arena_id(96269).name,
          "Lightning Bolt")
    check("owned_count with no data", q.owned_count(bolt.title_id), 0)


def main() -> int:
    db = Path(__file__).parent / "current.db"
    if not db.exists():
        print("current.db missing -- run `python build_db.py` first", file=sys.stderr)
        return 1

    print("mtgadb tests")
    print("-" * 62)
    test_text()
    test_colors()
    test_self_reference()
    test_resolution()
    test_database(db)
    print("-" * 62)
    failed = RESULTS.count(False)
    print(f"{RESULTS.count(True)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

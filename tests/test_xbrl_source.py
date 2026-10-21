"""Synthetic probes for edgar_mcp/edgar/xbrl_source.py: the lossless reader.

Run from the repository root (no network):
    python -X utf8 tests/test_xbrl_source.py

Each probe is a small made-up instance that a convenience parser would
"fix": typed dimensions, nil against zero against absent, several entities,
conflicting duplicates, one local name in two namespaces, sparse periods,
names whose suffix suggests a meaning, undeclared and conflicting prefixes.
The reader must return each as written and say what it could not resolve.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from edgar_mcp.edgar import xbrl_source as xs  # noqa: E402

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


A = "http://a.example/2025"
B = "http://b.example/2025"
HEAD = ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        f'xmlns:a="{A}" xmlns:b="{B}">')
USD = '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'


def context(cid, period="<xbrli:instant>2025-12-31</xbrli:instant>", segment="", scenario="",
            scheme="http://www.sec.gov/CIK", identifier="0000000001"):
    seg = f"<xbrli:segment>{segment}</xbrli:segment>" if segment else ""
    scen = f"<xbrli:scenario>{scenario}</xbrli:scenario>" if scenario else ""
    per = f"<xbrli:period>{period}</xbrli:period>" if period is not None else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="{scheme}">{identifier}'
            f"</xbrli:identifier>{seg}</xbrli:entity>{per}{scen}</xbrli:context>")


def instance(body, head=HEAD):
    return (head + body + "</xbrli:xbrl>").encode()


def facts(result):
    return [i for i in result["items"] if i["node"] == "fact"]


def item(result, cid):
    return next(i for i in result["items"] if i.get("id") == cid)


def typed_dimensions():
    typed = ('<xbrldi:typedMember dimension="a:ContractAxis"><a:ContractId scheme="x">C-17 &amp; 18</a:ContractId>'
             '</xbrldi:typedMember>')
    r = xs.read_instance(instance(context("T", segment=typed) + USD
                                  + '<a:Value contextRef="T" unitRef="usd" decimals="0">5</a:Value>'))
    member = item(r, "T")["segment"]["typed_members"][0]
    check("typed: the dimension resolves by namespace and local name",
          member["dimension"] == {"qname": "a:ContractAxis", "resolved": True, "namespace": A,
                                  "local_name": "ContractAxis"}, str(member["dimension"]))
    check("typed: the member value is returned as XML, attributes and escapes kept",
          member["xml"] == '<a:ContractId scheme="x">C-17 &amp; 18</a:ContractId>', member["xml"])
    wrapped = ET.fromstring(f'<w xmlns:a="{A}">{member["xml"]}</w>')[0]
    check("typed: the XML reads back to the same element and text",
          wrapped.tag == f"{{{A}}}ContractId" and wrapped.text == "C-17 & 18" and wrapped.get("scheme") == "x")
    check("typed: no explicit member is invented for it", item(r, "T")["segment"]["explicit_members"] == [])


def nil_zero_absent():
    body = (context("I") + USD
            + '<a:Debt contextRef="I" unitRef="usd" xsi:nil="true"/>'
            + '<a:Cash contextRef="I" unitRef="usd" decimals="-3">0</a:Cash>'
            + '<a:Note contextRef="I"></a:Note>')
    r = xs.read_instance(instance(body))
    by = {f["concept"]["local_name"]: f for f in facts(r)}
    check("nil: a nil fact keeps xsi:nil as written and has no text",
          by["Debt"]["nil"] == "true" and by["Debt"]["text"] is None and by["Debt"]["decimals"] is None, str(by["Debt"]))
    check("nil: zero is the text 0, not nil", by["Cash"]["text"] == "0" and by["Cash"]["nil"] is None)
    check("nil: an empty text fact is not nil and not dropped", "Note" in by and by["Note"]["nil"] is None
          and not by["Note"]["text"])
    items, matches, unresolved = xs.select_concepts(r, ["a:Absent", "Absent"])
    check("absent: a concept with no fact matches nothing and adds no fact",
          [m["matches"] for m in matches] == [[], []] and not facts({"items": items}) and not unresolved)


def multi_entity():
    body = (context("E1") + context("E2", identifier="0000000002")
            + context("E3", scheme="http://www.example.com/LEI", identifier="0000000001") + USD
            + "".join(f'<a:Cash contextRef="{c}" unitRef="usd" decimals="0">{v}</a:Cash>'
                      for c, v in (("E1", 1), ("E2", 2), ("E3", 3))))
    r = xs.read_instance(instance(body))
    entities = [item(r, c)["entity"] for c in ("E1", "E2", "E3")]
    check("entities: each context keeps its identifier and its scheme",
          entities == [{"scheme": "http://www.sec.gov/CIK", "identifier": "0000000001"},
                       {"scheme": "http://www.sec.gov/CIK", "identifier": "0000000002"},
                       {"scheme": "http://www.example.com/LEI", "identifier": "0000000001"}], str(entities))
    check("entities: the facts of every entity are returned", [f["text"] for f in facts(r)] == ["1", "2", "3"])


def duplicates():
    body = (context("D") + USD
            + '<a:Revenue id="f1" contextRef="D" unitRef="usd" decimals="-6">100000000</a:Revenue>'
            + '<a:Revenue id="f2" contextRef="D" unitRef="usd" decimals="0">100000123</a:Revenue>'
            + '<a:Revenue id="f3" contextRef="D" unitRef="usd" precision="3">95000000</a:Revenue>'
            + '<a:Revenue id="f4" contextRef="D" unitRef="usd" decimals="-6">100000000</a:Revenue>')
    r = xs.read_instance(instance(body))
    got = [(f["id"], f["text"], f["decimals"], f["precision"]) for f in facts(r)]
    check("duplicates: every duplicate, consistent or not, is returned in document order",
          got == [("f1", "100000000", "-6", None), ("f2", "100000123", "0", None), ("f3", "95000000", None, "3"),
                  ("f4", "100000000", "-6", None)], str(got))
    items, _, _ = xs.select_concepts(r, ["a:Revenue"])
    check("duplicates: selecting the concept keeps all four", [f["id"] for f in facts({"items": items})]
          == ["f1", "f2", "f3", "f4"])


def same_local_name():
    body = (context("D") + USD + '<a:Revenue contextRef="D" unitRef="usd" decimals="0">1</a:Revenue>'
            + '<b:Revenue contextRef="D" unitRef="usd" decimals="0">2</b:Revenue>')
    r = xs.read_instance(instance(body))
    _, matches, _ = xs.select_concepts(r, ["Revenue"])
    check("namespaces: a bare name returns every namespace that has it",
          matches[0]["matches"] == [{"namespace": A, "local_name": "Revenue"},
                                    {"namespace": B, "local_name": "Revenue"}], str(matches))
    for name, want in (("b:Revenue", ["2"]), (f"{{{A}}}Revenue", ["1"])):
        items, matches, _ = xs.select_concepts(r, [name])
        check(f"namespaces: {name} returns only its own namespace", [f["text"] for f in facts({"items": items})] == want)
    items, matches, unresolved = xs.select_concepts(r, ["zz:Revenue"])
    check("namespaces: an undeclared prefix is unresolved, not matched by local name",
          not facts({"items": items}) and matches[0]["resolved"] is False and unresolved
          and "not declared" in unresolved[0]["reason"], str(unresolved))


def sparse_periods():
    body = (context("F", period="<xbrli:forever/>") + context("N", period=None)
            + context("S", period="<xbrli:startDate>2025-01-01</xbrli:startDate>")
            + context("W", period="<xbrli:instant> 2025-12-31 </xbrli:instant>")
            + '<a:Policy contextRef="F">text</a:Policy><a:Policy contextRef="N">x</a:Policy>'
            + '<a:Policy contextRef="S">y</a:Policy><a:Policy contextRef="W">z</a:Policy>')
    r = xs.read_instance(instance(body))
    periods = {c: item(r, c)["period"] for c in "FNSW"}
    check("periods: forever, a missing period, a start with no end and spaces are kept as written",
          periods == {"F": [{"element": "forever", "text": None}], "N": None,
                      "S": [{"element": "startDate", "text": "2025-01-01"}],
                      "W": [{"element": "instant", "text": " 2025-12-31 "}]}, str(periods))
    check("periods: no fact is dropped for its period", len(facts(r)) == 4)


def misleading_names():
    names = ["RevenueAbstract", "TotalAssetsMember", "StatementTable", "NetIncomeLineItems", "SegmentAxis",
             "OpeningBalance", "ParentheticalTotal"]
    body = context("D") + USD + "".join(f'<a:{n} contextRef="D" unitRef="usd" decimals="0">1</a:{n}>' for n in names)
    r = xs.read_instance(instance(body))
    keys = {"seq", "node", "concept", "id", "contextRef", "unitRef", "decimals", "precision", "nil", "attributes",
            "text"}
    check("names: a name whose suffix suggests a role is returned as a plain fact, nothing added",
          [f["concept"]["local_name"] for f in facts(r)] == names and all(set(f) == keys for f in facts(r)),
          str([sorted(set(f) - keys) for f in facts(r)]))


def undeclared_and_conflicting_prefixes():
    seg = ('<xbrldi:explicitMember dimension="a:SegmentAxis">zz:RetailMember</xbrldi:explicitMember>'
           '<xbrldi:explicitMember dimension="q:OtherAxis">a:WholesaleMember</xbrldi:explicitMember>')
    body = (context("M", segment=seg)
            + '<xbrli:unit id="per"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure>'
              '</xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>zz:share</xbrli:measure>'
              '</xbrli:unitDenominator></xbrli:divide></xbrli:unit>'
            + '<a:EPS contextRef="M" unitRef="per" decimals="2">1.25</a:EPS>'
            + f'<c:Item xmlns:c="{A}" contextRef="M">first</c:Item>'
            + f'<c:Item xmlns:c="{B}" contextRef="M">second</c:Item>')
    r = xs.read_instance(instance(body))
    members = item(r, "M")["segment"]["explicit_members"]
    check("prefixes: an undeclared member prefix is kept as written and unresolved",
          members[0]["member"]["qname"] == "zz:RetailMember" and members[0]["member"]["resolved"] is False
          and members[1]["dimension"]["resolved"] is False, str(members))
    values = sorted(u["value"] for u in r["unresolved"] if "value" in u)
    check("prefixes: every unresolved QName is listed, a divide's measure included",
          values == ["q:OtherAxis", "zz:RetailMember", "zz:share"], str(r["unresolved"]))
    per = item(r, "per")["divide"]
    check("prefixes: a divide keeps numerator and denominator",
          per["numerator"][0]["local_name"] == "USD" and per["denominator"][0]["qname"] == "zz:share")
    items = facts(r)
    check("prefixes: a prefix bound twice resolves each fact by its own scope",
          [(f["concept"]["namespace"], f["text"]) for f in items if f["concept"]["local_name"] == "Item"]
          == [(A, "first"), (B, "second")])
    check("prefixes: a prefix bound to two namespaces is reported",
          any(u.get("prefix") == "c" and u.get("namespaces") == [A, B] for u in r["unresolved"]), str(r["unresolved"]))
    _, matches, unresolved = xs.select_concepts(r, ["c:Item"])
    check("prefixes: a name with that prefix is unresolved, not guessed",
          matches[0]["resolved"] is False and matches[0]["namespaces"] == [A, B]
          and any(u.get("name") == "c:Item" for u in unresolved), str(matches))


def references_and_order():
    body = (context("D") + USD + '<a:Cash contextRef="D" unitRef="usd" decimals="0">1</a:Cash>'
            + '<a:Cash contextRef="GONE" unitRef="eur" decimals="0">2</a:Cash>'
            + '<link:footnoteLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">'
              '<link:footnote xlink:type="resource" xlink:label="n1" xml:lang="fr">Note <b>un</b></link:footnote>'
              '</link:footnoteLink>'
            + '<a:Tuple><a:Inner contextRef="D">x</a:Inner></a:Tuple>')
    r = xs.read_instance(instance(body))
    seqs = [i["seq"] for i in r["items"]]
    check("order: items keep document order, each with its position", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))
    check("order: every top-level element is an item", [i["node"] for i in r["items"]]
          == ["context", "unit", "fact", "fact", "link", "other"], str([i["node"] for i in r["items"]]))
    link = r["items"][4]
    check("links: a footnote link is kept as XML with xml:lang and its markup",
          'xml:lang="fr"' in link["xml"] and "<b>un</b>" in link["xml"]
          and link["attributes"]["{http://www.w3.org/1999/xlink}role"] == "http://www.xbrl.org/2003/role/link",
          link["xml"][:160])
    check("other: an element without contextRef (a tuple) is kept as XML, not dropped",
          "<a:Inner" in r["items"][5]["xml"])
    _, _, unresolved = xs.select_concepts(r, ["a:Cash"])
    check("references: a contextRef or unitRef with no such context or unit is unresolved",
          {u.get("contextRef") or u.get("unitRef") for u in unresolved} == {"GONE", "eur"}, str(unresolved))


def bad_documents():
    for label, raw in (("not XML", b"<xbrli:xbrl"), ("not an instance", b"<html/>")):
        try:
            xs.read_instance(raw, "x.xml")
            check(f"documents: {label} is refused", False)
        except xs.FilingDataError as e:
            check(f"documents: {label} is refused with the file named", "x.xml" in str(e), str(e))


def main():
    typed_dimensions()
    nil_zero_absent()
    multi_entity()
    duplicates()
    same_local_name()
    sparse_periods()
    misleading_names()
    undeclared_and_conflicting_prefixes()
    references_and_order()
    bad_documents()
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

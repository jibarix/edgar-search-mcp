"""Synthetic probes for edgar_mcp/edgar/xbrl_source.py: the lossless reader.

Run from the repository root (no network):
    python -X utf8 tests/test_xbrl_source.py

Each probe is a small made-up instance that a convenience parser would
"fix": typed dimensions, nil against zero against absent, several entities,
conflicting duplicates, one local name in two namespaces, sparse periods,
names whose suffix suggests a meaning, undeclared and conflicting prefixes;
and in schemas and linkbases: labels in several languages or none, one
child under several calculation parents, prohibited and overriding arcs,
equal orders, misleading role titles, duplicate ids, generic links.
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
    entities = [{k: item(r, c)["entity"][k] for k in ("scheme", "identifier")} for c in ("E1", "E2", "E3")]
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


def period_shapes():
    body = (context("F", period="<xbrli:forever/>") + context("N", period=None)
            + context("S", period="<xbrli:startDate>2025-01-01</xbrli:startDate>")
            + context("W", period="<xbrli:instant> 2025-12-31 </xbrli:instant>")
            + '<a:Policy contextRef="F">text</a:Policy><a:Policy contextRef="N">x</a:Policy>'
            + '<a:Policy contextRef="S">y</a:Policy><a:Policy contextRef="W">z</a:Policy>')
    r = xs.read_instance(instance(body))
    periods = {c: item(r, c)["period"] and [{k: p[k] for k in ("element", "text")} for p in item(r, c)["period"]]
               for c in "FNSW"}
    check("periods: forever, a missing period, a start with no end and spaces are kept as written",
          periods == {"F": [{"element": "forever", "text": None}], "N": None,
                      "S": [{"element": "startDate", "text": "2025-01-01"}],
                      "W": [{"element": "instant", "text": " 2025-12-31 "}]}, str(periods))
    check("periods: no fact is dropped for its period", len(facts(r)) == 4)


def duration(start, end):
    return f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"


def unequal_coverage():
    """Sparse periods: concepts reported for different sets of periods. Each
    fact is returned as written; no concept is padded out to the periods
    another concept has, and no period is dropped for having few facts."""
    body = (context("FY25", period=duration("2025-01-01", "2025-12-31"))
            + context("FY24", period=duration("2024-01-01", "2024-12-31"))
            + context("FY23", period=duration("2023-01-01", "2023-12-31"))
            + context("FY22", period=duration("2022-01-01", "2022-12-31")) + USD
            + '<a:Revenue contextRef="FY25" unitRef="usd" decimals="0">30</a:Revenue>'
            + '<a:Revenue contextRef="FY24" unitRef="usd" decimals="0">20</a:Revenue>'
            + '<a:Revenue contextRef="FY23" unitRef="usd" decimals="0">10</a:Revenue>'
            + '<a:NewLine contextRef="FY25" unitRef="usd" decimals="0">5</a:NewLine>'
            + '<a:OldLine contextRef="FY24" unitRef="usd" decimals="0">4</a:OldLine>'
            + '<a:OldLine contextRef="FY23" unitRef="usd" decimals="0">3</a:OldLine>')
    r = xs.read_instance(instance(body))
    got = [(f["concept"]["local_name"], f["contextRef"], f["text"]) for f in facts(r)]
    check("sparse periods: every fact is returned once, in document order, with its own context",
          got == [("Revenue", "FY25", "30"), ("Revenue", "FY24", "20"), ("Revenue", "FY23", "10"),
                  ("NewLine", "FY25", "5"), ("OldLine", "FY24", "4"), ("OldLine", "FY23", "3")], str(got))
    covered = {}
    for name, cid, _ in got:
        covered.setdefault(name, []).append(cid)
    check("sparse periods: a concept reported for fewer periods is not padded to the others (no null or zero rows)",
          covered == {"Revenue": ["FY25", "FY24", "FY23"], "NewLine": ["FY25"], "OldLine": ["FY24", "FY23"]}
          and all(f["text"] not in (None, "", "0") and not f["nil"] for f in facts(r)), str(covered))
    check("sparse periods: a context no fact uses is still returned, with its period as written",
          [i["id"] for i in r["items"] if i["node"] == "context"] == ["FY25", "FY24", "FY23", "FY22"]
          and [p["text"] for p in item(r, "FY22")["period"]] == ["2022-01-01", "2022-12-31"],
          str([i.get("id") for i in r["items"]]))
    items, matches, unresolved = xs.select_concepts(r, ["a:NewLine"])
    check("sparse periods: selecting a concept reported for one period returns that fact and only its context",
          [(i["node"], i.get("id") or i.get("contextRef")) for i in items]
          == [("context", "FY25"), ("unit", "usd"), ("fact", "FY25")] and not unresolved,
          str([(i["node"], i.get("id"), i.get("contextRef")) for i in items]))
    items, _, _ = xs.select_concepts(r, ["a:OldLine", "a:NewLine"])
    check("sparse periods: selecting concepts of different coverage keeps each fact, the union of contexts, "
          "in document order",
          [i.get("id") or i.get("contextRef") for i in items] == ["FY25", "FY24", "FY23", "usd", "FY25", "FY24", "FY23"],
          str([(i["node"], i.get("id"), i.get("contextRef")) for i in items]))


def misleading_names():
    names = ["RevenueAbstract", "TotalAssetsMember", "StatementTable", "NetIncomeLineItems", "SegmentAxis",
             "OpeningBalance", "ParentheticalTotal"]
    body = context("D") + USD + "".join(f'<a:{n} contextRef="D" unitRef="usd" decimals="0">1</a:{n}>' for n in names)
    r = xs.read_instance(instance(body))
    keys = {"seq", "node", "concept", "id", "contextRef", "unitRef", "decimals", "precision", "nil", "attributes",
            "text", "lang"}
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


# ── schema and linkbase probes ─────────────────────────────────────────────

XL = "{http://www.w3.org/1999/xlink}"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
LB_NS = 'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"'


XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def lossless_instance():
    head = HEAD.replace(">", ' xml:lang="en-US" id="i1">', 1)
    body = ('<xbrli:context id="C"><xbrli:entity id="e"><xbrli:identifier scheme="s" id="i">1</xbrli:identifier>'
            '<xbrli:segment id="g"><xbrldi:explicitMember dimension="a:Axis">a:M</xbrldi:explicitMember>'
            '</xbrli:segment><a:EntityNote>n</a:EntityNote></xbrli:entity>'
            '<xbrli:period id="p"><xbrli:instant id="t">2025-12-31</xbrli:instant></xbrli:period>'
            '<xbrli:scenario id="sc"><a:Note>x</a:Note></xbrli:scenario><a:ContextNote>c</a:ContextNote>'
            '</xbrli:context>'
            '<xbrli:unit id="u"><xbrli:measure id="m">iso4217:USD</xbrli:measure></xbrli:unit>'
            '<a:Policy contextRef="C">text</a:Policy><a:Policy contextRef="C" xml:lang="fr">texte</a:Policy>')
    r = xs.read_instance(instance(body, head))
    c, got = item(r, "C"), facts(r)
    check("lossless: the instance's document element and its attributes are returned",
          (r.get("root") or {}).get("attributes") == {XML_LANG: "en-US", "id": "i1"}
          and (r.get("root") or {}).get("element", {}).get("local_name") == "xbrl", str(r.get("root")))
    check("lossless: a fact's language is the xml:lang in effect, inherited or its own, with where it is written",
          [f.get("lang") for f in got] == [{"value": "en-US", "seq": 0}, {"value": "fr", "seq": got[1]["seq"]}]
          and XML_LANG not in got[0]["attributes"], str([f.get("lang") for f in got]))
    entity = c.get("entity") or {}
    check("lossless: entity, identifier, period, period children, segment and scenario keep their attributes",
          entity.get("attributes") == {"id": "e"} and entity.get("identifier_attributes") == {"scheme": "s", "id": "i"}
          and (c.get("period_element") or {}).get("attributes") == {"id": "p"}
          and [p.get("attributes") for p in c["period"]] == [{"id": "t"}]
          and c["segment"].get("attributes") == {"id": "g"} and c["scenario"].get("attributes") == {"id": "sc"},
          str(c))
    check("lossless: a child of a context or entity of no XBRL kind is kept as XML",
          ["ContextNote" in o["xml"] for o in c.get("other", [])] == [True]
          and ["EntityNote" in o["xml"] for o in entity.get("other", [])] == [True], str(c))
    check("lossless: a measure keeps its attributes",
          item(r, "u")["measures"][0].get("attributes") == {"id": "m"}, str(item(r, "u")))


def lossless_schema_and_linkbase():
    body = ('<xs:annotation id="an"><xs:documentation xml:lang="en">About <b>this</b> schema.</xs:documentation>'
            '<xs:appinfo><a:note xmlns:a="http://a.example/2025">kept</a:note></xs:appinfo></xs:annotation>'
            '<xs:element xmlns:q="http://q.example" name="P" id="a_P" type="xbrli:monetaryItemType" '
            'substitutionGroup="xbrli:item"/>')
    r = xs.read_schema(schema(body, 'elementFormDefault="qualified"'), "a.xsd")
    check("lossless: the schema's document element and its attributes are returned",
          (r.get("root") or {}).get("attributes") == {"elementFormDefault": "qualified", "targetNamespace": A},
          str(r.get("root")))
    notes = [i for i in r["items"] if i["node"] == "annotation"]
    doc = notes[0].get("documentation", [{}])[0] if notes else {}
    check("lossless: a schema annotation is an item, its documentation as written with its language",
          notes and notes[0]["attributes"] == {"id": "an"} and doc.get("text") == "About "
          and "<b>this</b> schema." in doc.get("xml", "") and doc.get("lang") == {"value": "en", "seq": doc.get("seq")},
          str(notes))
    check("lossless: content of an appinfo of no XBRL kind is kept as XML",
          any(i["node"] == "other" and "kept" in i["xml"] for i in r["items"]), str(r["items"]))
    check("lossless: a namespace declared below the schema's document element is listed where it is written",
          [d["prefix"] for d in (r.get("namespace_declarations") or []) if d["seq"]] == ["a", "q"],
          str(r.get("namespace_declarations")))
    label = ('<link:label xlink:type="resource" xlink:label="l" xlink:role="http://www.xbrl.org/2003/role/label">'
             'P</link:label><link:label xlink:type="resource" xlink:label="l" xml:lang="de" '
             'xlink:role="http://www.xbrl.org/2003/role/label">P</link:label>')
    items, _, _ = linkbase('<link:documentation>About</link:documentation>' + ext("label", loc("P") + label),
                           "label", root_attrs='xml:lang="fr"')
    check("lossless: a label without xml:lang has the language inherited from the linkbase, the attribute as written",
          [(XML_LANG in i["attributes"], (i.get("lang") or {}).get("value")) for i in of(items, "resource")]
          == [(False, "fr"), (True, "de")], str(of(items, "resource")))
    check("lossless: a linkbase child that is no link is kept as XML",
          [("About" in i["xml"]) for i in of(items, "other")] == [True], str(items))
    lb = xs.XmlDoc(f'<link:linkbase {LB_NS} id="x">{ext("label", loc("P"))}{ext("label", loc("P"))}'
                   f'{ext("presentation", loc("P"))}</link:linkbase>'.encode(), "x.xml")
    counts = xs.extended_link_counts(lb, lb.root) if hasattr(xs, "extended_link_counts") else None
    check("lossless: the extended links of a linkbase are counted by element, whichever part returns them",
          counts == {"{http://www.xbrl.org/2003/linkbase}labelLink": 2,
                     "{http://www.xbrl.org/2003/linkbase}presentationLink": 1}, str(counts))
    root = xs.root_record(lb) if hasattr(xs, "root_record") else None
    check("lossless: a linkbase's document element is given with its attributes",
          root == {"seq": 0, "element": {"namespace": "http://www.xbrl.org/2003/linkbase", "local_name": "linkbase",
                                         "prefixes": ["link"]}, "attributes": {"id": "x"}}, str(root))


def tuples():
    body = (context("C") + context("P") + USD + '<a:Cash contextRef="C" unitRef="usd" decimals="0">1</a:Cash>'
            '<a:Holding><a:Amount contextRef="C" unitRef="usd" decimals="0">7</a:Amount>'
            '<a:Inner><a:Cash contextRef="P" unitRef="usd" decimals="0">3</a:Cash></a:Inner></a:Holding>')
    r = xs.read_instance(instance(body))
    holding = next(i for i in r["items"] if i["node"] == "other")
    nested = holding.get("nested_facts") or []
    check("tuples: the facts inside a tuple are listed with their nesting path, the tuple kept as XML",
          [(f["concept"]["local_name"], f["text"], [p["element"]["local_name"] for p in f.get("path", [])])
           for f in nested] == [("Amount", "7", ["Holding"]), ("Cash", "3", ["Holding", "Inner"])]
          and "<a:Amount" in holding["xml"], str(nested))
    items, matches, unresolved = xs.select_concepts(r, ["a:Amount"])
    got = [i for i in items if i["node"] == "fact"]
    check("tuples: a fact inside a tuple is selected by name, with its path, context and unit",
          [(f["text"], [p["seq"] for p in f.get("path", [])]) for f in got] == [("7", [holding["seq"]])]
          and [i["id"] for i in items if i["node"] in ("context", "unit")] == ["C", "usd"]
          and matches[0]["matches"] == [{"namespace": A, "local_name": "Amount"}] and not unresolved,
          str((items, matches)))
    items, _, _ = xs.select_concepts(r, ["Cash"])
    got = [(f["text"], len(f.get("path", []))) for f in items if f["node"] == "fact"]
    check("tuples: a name matches its facts at every depth, in document order",
          got == [("1", 0), ("3", 2)] and [i["seq"] for i in items] == sorted(i["seq"] for i in items), str(got))
    items, matches, unresolved = xs.select_concepts(r, ["a:Holding"])
    check("tuples: a name of an element that is no fact (a tuple) is said to be unsupported, never a silent empty match",
          not items and matches[0].get("unsupported")
          and matches[0]["unsupported"][0]["seqs"] == [holding["seq"]]
          and any(u.get("name") == "a:Holding" for u in unresolved), str((matches, unresolved)))


def schema(body, extra_ns=""):
    return (f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" {LB_NS} '
            f'xmlns:xbrli="http://www.xbrl.org/2003/instance" {extra_ns} targetNamespace="{A}">{body}</xs:schema>'
            ).encode()


FOLDER = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/"
# The schema the helper's locators point into: one element per id a test uses.
A_SCHEMA_IDS = ("Sales", "Unlabelled", "P", "Total", "Part", "X", "Y", "Z", "A", "M", "Child", "Parent")


def a_schema():
    return xs.read_schema(schema("".join(
        f'<xs:element name="{n}" id="a_{n}" type="xbrli:monetaryItemType" substitutionGroup="xbrli:item"/>'
        for n in A_SCHEMA_IDS)), "a.xsd", FOLDER + "a.xsd")


def linkbase(body, part, extra_ns="", root_attrs=""):
    """`body` read as the linkbase x_lab.xml of the folder: hrefs resolved against its URL and any xml:base,
    locators into a.xsd resolved through the parsed schema `a_schema()`."""
    doc = xs.XmlDoc(f'<link:linkbase {LB_NS} {extra_ns} {root_attrs}>{body}</link:linkbase>'.encode(), "x_lab.xml")
    seen = []
    resolve = xs.locator_resolver({FOLDER + "a.xsd": ("a.xsd", a_schema())})

    def target_of(href):
        seen.append(href)
        return resolve(href)
    items, unresolved = xs.read_linkbase(doc, doc.root, "x_lab.xml", part, target_of, url=FOLDER + "x_lab.xml")
    return items, unresolved, seen


def loc(label, href=None):
    return f'<link:loc xlink:type="locator" xlink:href="{href or "a.xsd#a_" + label}" xlink:label="{label}"/>'


def arc(kind, frm, to, attrs="", arcrole="http://www.xbrl.org/2003/arcrole/summation-item"):
    return f'<link:{kind}Arc xlink:type="arc" xlink:arcrole="{arcrole}" xlink:from="{frm}" xlink:to="{to}" {attrs}/>'


def ext(kind, body, role="http://a.example/role/R1"):
    return f'<link:{kind}Link xlink:type="extended" xlink:role="{role}">{body}</link:{kind}Link>'


def of(items, node):
    return [i for i in items if i["node"] == node]


def multilingual_and_missing_labels():
    label = lambda key, lang, text, role="label": (
        f'<link:label xlink:type="resource" xlink:label="{key}" '
        + (f'xml:lang="{lang}" ' if lang is not None else "")
        + f'xlink:role="http://www.xbrl.org/2003/role/{role}">{text}</link:label>')
    concept_label = "http://www.xbrl.org/2003/arcrole/concept-label"
    body = (loc("Sales") + label("l1", "de", "Umsatz") + label("l1", "en-US", "Net sales")
            + label("l1", "fr", "Ventes") + label("l1", "en-US", "Total net sales", "totalLabel")
            + label("l1", None, "No language")
            + '<link:label xlink:type="resource" xlink:label="l1" xml:lang="en" '
              'xlink:role="http://www.xbrl.org/2003/role/documentation">Sales <b>net</b> of returns</link:label>'
            + arc("label", "Sales", "l1", arcrole=concept_label)
            + loc("Unlabelled"))
    items, unresolved, _ = linkbase(ext("label", body, "http://www.xbrl.org/2003/role/link"), "label")
    labels = of(items, "resource")
    check("labels: every language as written and in document order, none preferred",
          [(x["attributes"].get(XML_LANG), x["text"]) for x in labels[:5]]
          == [("de", "Umsatz"), ("en-US", "Net sales"), ("fr", "Ventes"), ("en-US", "Total net sales"),
              (None, "No language")], str(labels)[:300])
    check("labels: a label with markup keeps its markup as XML and its leading text",
          labels[5]["text"] == "Sales " and "<b>net</b>" in labels[5]["xml"], str(labels[5]))
    check("labels: a concept with no label is a locator with no arc, nothing filled in",
          [l["attributes"][f"{XL}label"] for l in of(items, "loc")] == ["Sales", "Unlabelled"]
          and len(of(items, "arc")) == 1 and not unresolved, str(of(items, "loc")))


def multiple_calculation_parents():
    body1 = (loc("Total") + loc("Sub") + loc("Part")
             + arc("calculation", "Total", "Part", 'weight="1" order="1"')
             + arc("calculation", "Sub", "Part", 'weight="-1" order="1"'))
    body2 = loc("Other") + loc("Part") + arc("calculation", "Other", "Part", 'weight="0.5" order="2"')
    items, _, _ = linkbase(ext("calculation", body1) + ext("calculation", body2, "http://a.example/role/R2"),
                           "calculation")
    parents = [(a["link_seq"], a["attributes"][f"{XL}from"], a["attributes"]["weight"])
               for a in of(items, "arc") if a["attributes"][f"{XL}to"] == "Part"]
    links = [l["seq"] for l in of(items, "extendedLink")]
    check("calculation: one child under three parents in two links is three arcs, weights as written",
          parents == [(links[0], "Total", "1"), (links[0], "Sub", "-1"), (links[1], "Other", "0.5")], str(parents))


def prohibited_arcs():
    body = (loc("P") + loc("C")
            + arc("calculation", "P", "C", 'weight="1" order="1"')
            + arc("calculation", "P", "C", 'weight="1" order="1" use="prohibited" priority="1"')
            + arc("calculation", "P", "C", 'weight="-1" order="1" priority="2"'))
    items, unresolved, _ = linkbase(ext("calculation", body), "calculation")
    arcs = [(a["attributes"].get("use"), a["attributes"].get("priority"), a["attributes"]["weight"])
            for a in of(items, "arc")]
    check("prohibited: the arc, its prohibition and its override are all returned, none applied",
          arcs == [(None, None, "1"), ("prohibited", "1", "1"), (None, "2", "-1")] and not unresolved, str(arcs))


def equal_orders():
    parent_child = "http://www.xbrl.org/2003/arcrole/parent-child"
    body = (loc("H") + loc("Z") + loc("A") + loc("M")
            + arc("presentation", "H", "Z", 'order="1"', parent_child)
            + arc("presentation", "H", "A", 'order="1"', parent_child)
            + arc("presentation", "H", "M", 'order="1.0"', parent_child)
            + arc("presentation", "H", "A", "", parent_child))
    items, _, _ = linkbase(ext("presentation", body), "presentation")
    arcs = [(a["attributes"][f"{XL}to"], a["attributes"].get("order")) for a in of(items, "arc")]
    check("orders: equal orders stay in document order, order text as written, a missing order stays missing",
          arcs == [("Z", "1"), ("A", "1"), ("M", "1.0"), ("A", None)], str(arcs))
    seqs = [i["seq"] for i in items]
    check("orders: items come in document order", seqs == sorted(seqs), str(seqs))


def same_fragment_two_schemas():
    body = (loc("x1", "a.xsd#Revenue") + loc("x2", "https://b.example/b.xsd#Revenue")
            + arc("definition", "x1", "x2", arcrole="http://xbrl.org/int/dim/arcrole/domain-member"))
    items, _, seen = linkbase(ext("definition", body), "definition")
    locs = of(items, "loc")
    check("locators: the same fragment in two schemas is two locators, each href kept as written and resolved",
          [l["href"] for l in locs] == ["a.xsd#Revenue", "https://b.example/b.xsd#Revenue"]
          and seen == [FOLDER + "a.xsd#Revenue", "https://b.example/b.xsd#Revenue"]
          and [l["url"] for l in locs] == seen, str(seen))
    check("locators: each is looked up in its own schema: no element of a.xsd has id Revenue, b.xsd was not read",
          locs[0]["target"] == {"resolved": False, "file": "a.xsd", "fragment": "Revenue",
                                "reason": "no element of a.xsd has this id"}
          and locs[1]["target"]["schema"] == "https://b.example/b.xsd"
          and locs[1]["target"]["resolved"] is False, str([l["target"] for l in locs]))


def locators_and_base_uri():
    items, _, _ = linkbase(ext("presentation", loc("P")), "presentation")
    check("locators: a locator into a parsed schema resolves to the one element with that id",
          of(items, "loc")[0]["target"] == {"resolved": True, "file": "a.xsd", "namespace": A, "local_name": "P",
                                            "schema_seq": of(items, "loc")[0]["target"]["schema_seq"]},
          str(of(items, "loc")))
    nested = ('<link:presentationLink xlink:type="extended" xlink:role="http://a.example/role/R1" xml:base="b/">'
              + loc("P") + '</link:presentationLink>')
    items, _, seen = linkbase(nested, "presentation", root_attrs='xml:base="https://x.example/a/"')
    check("locators: the href is resolved against the inherited xml:base, outermost first",
          seen == ["https://x.example/a/b/a.xsd#a_P"] and of(items, "loc")[0]["target"]["resolved"] is False
          and of(items, "loc")[0]["target"]["schema"] == "https://x.example/a/b/a.xsd", str(seen))
    doc = xs.XmlDoc(f'<link:linkbase {LB_NS} xml:base="sub/"><link:roleRef xlink:type="simple" '
                    f'roleURI="http://a.example/role/R1" xlink:href="../a.xsd#R1"/></link:linkbase>'.encode(), "l.xml")
    check("locators: base_uri applies xml:base relative to the document URL",
          doc.base_uri(doc.root[0], FOLDER + "l.xml") == FOLDER + "sub/"
          and doc.resolve_href("../a.xsd#R1", doc.root[0], FOLDER + "l.xml") == FOLDER + "a.xsd#R1")
    twice = schema('<xs:element name="First" id="a_T" type="xbrli:monetaryItemType" substitutionGroup="xbrli:item"/>'
                   '<xs:element name="Second" id="a_T" type="xbrli:stringItemType" substitutionGroup="xbrli:item"/>')
    resolve = xs.locator_resolver({FOLDER + "a.xsd": ("a.xsd", xs.read_schema(twice, "a.xsd", FOLDER + "a.xsd"))})
    doc = xs.XmlDoc(f'<link:linkbase {LB_NS}>{ext("presentation", loc("T"))}</link:linkbase>'.encode(), "x.xml")
    items, _ = xs.read_linkbase(doc, doc.root, "x.xml", "presentation", resolve, url=FOLDER + "x.xml")
    target = of(items, "loc")[0]["target"]
    check("locators: an id two elements share is ambiguous, both named, neither chosen",
          target["resolved"] is False and target.get("ambiguous") is True
          and [c["local_name"] for c in target.get("candidates", [])] == ["First", "Second"], str(target))


def other_links_and_children():
    gen = 'xmlns:gen="http://xbrl.org/2008/generic"'
    body = (ext("presentation", loc("P") + '<link:title xlink:type="title">A title</link:title>')
            + ext("calculation", loc("P"))
            + '<gen:link xlink:type="extended" xlink:role="http://a.example/role/G"><link:loc xlink:type="locator" '
              'xlink:href="a.xsd#a_P" xlink:label="P"/></gen:link>')
    items, unresolved, _ = linkbase(body, "presentation", gen)
    check("links: a child of another xlink type is returned as XML",
          [(i["node"], "A title" in i.get("xml", "")) for i in items if i["node"] == "other"] == [("other", True)],
          str(items))
    check("links: a generic link is reported unresolved, a link of another part is left to its part",
          len(unresolved) == 1 and unresolved[0]["element"] == "{http://xbrl.org/2008/generic}link"
          and all(i["file"] == "x_lab.xml" for i in items) and len(of(items, "extendedLink")) == 1, str(unresolved))
    refs = '<link:roleRef roleURI="http://a.example/role/R1" xlink:type="simple" xlink:href="a.xsd#R1"/>'
    items, _, _ = linkbase(refs + ext("calculation", loc("P")), "presentation")
    check("links: a linkbase whose links are all of another part leaves its roleRefs to that part",
          items == [], str(items))
    items, _, _ = linkbase(refs + ext("calculation", loc("P")), "calculation")
    check("links: ... which returns them", of(items, "roleRef") and items[0]["href"] == "a.xsd#R1", str(items))
    for part in ("presentation", "label"):
        items, _, _ = linkbase(refs, part)
        check(f"links: a linkbase holding only roleRefs returns them ({part}), not nothing",
              [(i["node"], i["href"]) for i in items] == [("roleRef", "a.xsd#R1")], str(items))
    items, _, _ = linkbase(refs + '<gen:link xlink:type="extended" xlink:role="http://a.example/role/G"/>',
                           "presentation", gen)
    check("links: so does one whose only link is of an element no part returns",
          [i["node"] for i in items] == ["roleRef"], str(items))
    items, _, _ = linkbase(refs + ext("presentation", loc("P")), "presentation")
    check("links: with a link of this part, its roleRefs come first as written",
          items[0]["node"] == "roleRef" and items[0]["href"] == "a.xsd#R1", str(items[0]))


def schema_declarations():
    body = ('<xs:import namespace="http://www.xbrl.org/2003/instance" '
            'schemaLocation="http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"/>'
            '<xs:annotation><xs:appinfo>'
            '<link:roleType roleURI="http://a.example/role/BS" id="BS">'
            '<link:definition>100 - Statement - BALANCE SHEET (Parenthetical)</link:definition>'
            '<link:usedOn>link:calculationLink</link:usedOn><link:usedOn>undeclared:thing</link:usedOn></link:roleType>'
            '<link:linkbaseRef xlink:type="simple" xlink:href="a_pre.xml"/>'
            '</xs:appinfo></xs:annotation>'
            '<xs:element name="TotalAssetsParenthetical" id="a_T" type="xbrli:monetaryItemType" '
            'substitutionGroup="xbrli:item" xbrli:periodType="duration"/>'
            '<xs:element name="Second" id="a_T" type="xbrli:stringItemType" substitutionGroup="xbrli:item"/>'
            '<xs:element name="Typed" id="a_Ty" type="nope:thing" substitutionGroup="nope:item"/>'
            '<xs:element name="Untyped" id="a_U"/>'
            '<xs:complexType name="Cx"><xs:sequence/></xs:complexType>')
    r = xs.read_schema(schema(body), "a.xsd")
    nodes = [i["node"] for i in r["items"]]
    check("schema: declarations in document order, each by its XML construct",
          nodes == ["import", "annotation", "roleType", "linkbaseRef", "element", "element", "element", "element",
                    "other"], str(nodes))
    r["items"] = [i for i in r["items"] if i["node"] != "annotation"]
    role = r["items"][1]
    check("schema: a role's definition is as written, a misleading title not classified",
          role["definition"] == "100 - Statement - BALANCE SHEET (Parenthetical)"
          and set(role) == {"seq", "attributes", "node", "definition", "usedOn", "xml"}
          and "100 - Statement" in role["xml"], str(role))
    check("schema: usedOn QNames resolved where written, an undeclared prefix unresolved",
          role["usedOn"][0]["local_name"] == "calculationLink" and role["usedOn"][1]["resolved"] is False,
          str(role["usedOn"]))
    total = r["items"][3]
    check("schema: a name that sounds like a total or a parenthetical gets no meaning, attributes as written",
          total["name"] == "TotalAssetsParenthetical"
          and total["attributes"]["{http://www.xbrl.org/2003/instance}periodType"] == "duration"
          and "balance" not in total and "kind" not in total, str(total))
    check("schema: a duplicate id is reported, and both elements keep it; neither wins",
          [c["local_name"] for c in r["ids"]["a_T"]] == ["TotalAssetsParenthetical", "Second"]
          and any(u.get("id") == "a_T" for u in r["unresolved"])
          and xs.concept_at(r, "a.xsd", "a_T")["resolved"] is False, str(r["unresolved"]))
    typed = r["items"][5]
    check("schema: a type and substitution group with an undeclared prefix are unresolved, value kept",
          typed["type"] == {"qname": "nope:thing", "resolved": False,
                            "reason": "the prefix 'nope' is not declared where the value is written"}
          and sum(1 for u in r["unresolved"] if u.get("element") == "Typed") == 2, str(typed))
    check("schema: an element with no type has none, nothing filled in",
          r["items"][6]["type"] is None and r["items"][6]["substitutionGroup"] is None, str(r["items"][6]))
    check("schema: another top-level component is returned as XML", "complexType" in r["items"][7]["xml"],
          str(r["items"][7]))
    embedded = schema('<xs:annotation><xs:appinfo><link:linkbase>'
                      + ext("presentation", loc("P")) + '</link:linkbase></xs:appinfo></xs:annotation>')
    r = xs.read_schema(embedded, "a.xsd")
    check("schema: a linkbase written inside the schema is listed and handed back to read",
          [i["node"] for i in r["items"]] == ["annotation", "linkbase"] and len(r["linkbases"]) == 1
          and r["items"][1]["element_count"] == 2, str(r["items"]))
    r = xs.read_schema(embedded.replace(b"<xs:annotation>", b'<xs:element name="P" id="a_P" '
                                         b'substitutionGroup="xbrli:item"/><xs:annotation>'), "a.xsd", FOLDER + "a.xsd")
    items, _ = xs.read_linkbase(r["doc"], r["linkbases"][0], "a.xsd", "presentation",
                                xs.locator_resolver({FOLDER + "a.xsd": ("a.xsd", r)}), url=FOLDER + "a.xsd")
    check("schema: the embedded linkbase reads like any other, its locator resolved into the schema around it",
          [i["node"] for i in items] == ["extendedLink", "loc"] and items[1]["target"]["local_name"] == "P",
          str(items))
    for label, raw in (("a linkbase", f'<link:linkbase {LB_NS}/>'.encode()), ("not XML", b"<xs:schema")):
        try:
            xs.read_schema(raw, "a.xsd")
            check(f"schema: {label} in place of a schema is refused", False)
        except xs.FilingDataError as e:
            check(f"schema: {label} in place of a schema is refused with the file named", "a.xsd" in str(e), str(e))


def main():
    typed_dimensions()
    nil_zero_absent()
    multi_entity()
    duplicates()
    same_local_name()
    period_shapes()
    unequal_coverage()
    misleading_names()
    undeclared_and_conflicting_prefixes()
    references_and_order()
    bad_documents()
    multilingual_and_missing_labels()
    multiple_calculation_parents()
    prohibited_arcs()
    equal_orders()
    same_fragment_two_schemas()
    locators_and_base_uri()
    other_links_and_children()
    schema_declarations()
    lossless_instance()
    lossless_schema_and_linkbase()
    tuples()
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

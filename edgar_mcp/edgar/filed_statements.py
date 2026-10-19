"""The primary financial statements of one filing, as the company filed them.

Read from the XBRL files inside the filing's folder, not from Company Facts:
the company schema (.xsd) names the statements and defines the company's own
elements; the presentation linkbase gives each statement's lines in the
company's order and nesting; the label linkbase gives the company's wording;
the calculation linkbase says which lines add into which; the instance holds
the values. Nothing here maps a line to a standard category: every line comes
back with the element the company chose, standard or its own, so a caller can
do its own mapping and say why.

Every function below takes the bytes of the files and returns plain data, so
the parsing is tested without the network.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

_L = "{http://www.xbrl.org/2003/linkbase}"
_XL = "{http://www.w3.org/1999/xlink}"
_XS = "{http://www.w3.org/2001/XMLSchema}"
_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_XBRLI_ATTR = "{http://www.xbrl.org/2003/instance}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

_STANDARD_LABEL = "http://www.xbrl.org/2003/role/label"
_DOCUMENTATION = "http://www.xbrl.org/2003/role/documentation"
# The summation arc of the 2003 calculation linkbase, and of Calculation 1.1.
_SUMMATION = {"http://www.xbrl.org/2003/arcrole/summation-item": "1.0",
              "https://xbrl.org/2023/arcrole/summation-item": "1.1"}
# Definition arcs that only build the dimensions of a table; every other arc
# that touches a company's own element is reported with it.
_DIMENSIONAL = re.compile(r"http://xbrl\.org/int/dim/arcrole/(all|notAll|hypercube-dimension|dimension-domain|"
                          r"domain-member|dimension-default)")
# The EDGAR Filer Manual's role definition: "<sort code> - <category> - <title>".
_ROLE_DEFINITION = re.compile(r"^\s*(?P<code>\S+)\s+-\s+(?P<category>Statement|Disclosure|Document)\s+-\s+(?P<title>.+?)\s*$",
                              re.IGNORECASE)
_STRUCTURE = (("Abstract", "abstract"), ("LineItems", "line_items"), ("Table", "table"), ("Axis", "axis"),
              ("Domain", "member"), ("Member", "member"))
_DEI = ("DocumentType", "DocumentPeriodEndDate", "DocumentFiscalYearFocus", "DocumentFiscalPeriodFocus",
        "CurrentFiscalYearEndDate", "AmendmentFlag", "EntityRegistrantName", "EntityCentralIndexKey")


# An instance as filers name it before inline XBRL: "ko-20181231.xml".
_INSTANCE_NAME = re.compile(r"[A-Za-z0-9]+-\d{8}\.xml")


def files_from_folder(names: list, files: dict) -> dict:
    """Fill what the summary left out (summaries before 2012 list no input
    files) from the names in the filing's folder."""
    files = dict(files)
    for key, suffix in (("presentation", "_pre.xml"), ("calculation", "_cal.xml"), ("label", "_lab.xml"),
                        ("definition", "_def.xml"), ("schema", ".xsd")):
        found = [n for n in names if n.endswith(suffix)]
        if not files.get(key) and len(found) == 1:
            files[key] = found[0]
    plain = [n for n in names if _INSTANCE_NAME.fullmatch(n)]
    if not files.get("instance") and len(plain) == 1:
        files["instance"] = plain[0]
    return files


class FilingDataError(Exception):
    """A file of the filing could not be read as XBRL."""


def _xml(raw: bytes, name: str):
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise FilingDataError(f"{name} is not readable XML ({e})") from e


def input_files(filing_summary: bytes) -> dict:
    """The XBRL files of a filing, from the SEC's FilingSummary.xml: the
    instance (for an inline XBRL document, the instance the SEC extracted
    from it), the schema and the four linkbases. Also the R page of each
    statement role, for a reader who wants the SEC's rendering."""
    root = _xml(filing_summary, "FilingSummary.xml")
    names = [(f.text or "").strip() for f in root.iter("File") if f.text]
    inputs = root.find("InputFiles")
    listed = [(f.text or "").strip() for f in inputs.iter("File")] if inputs is not None else names
    main = next((f for f in (inputs.iter("File") if inputs is not None else []) if f.get("doctype")), None)
    files = {"document": None, "instance": None, "inline": False, "schema": None,
             "presentation": None, "calculation": None, "label": None, "definition": None}
    for name in listed:
        for key, suffix in (("presentation", "_pre.xml"), ("calculation", "_cal.xml"), ("label", "_lab.xml"),
                            ("definition", "_def.xml")):
            if name.endswith(suffix):
                files[key] = name
        if name.endswith(".xsd"):
            files["schema"] = name
    if main is None:
        # Before inline XBRL the summary names the instance without a document type:
        # the one listed XML file that is not a linkbase.
        plain = [n for n in listed if _INSTANCE_NAME.fullmatch(n)]
        if len(plain) == 1:
            files["instance"] = plain[0]
    if main is not None:
        doc = (main.text or "").strip()
        files["document"] = doc
        files["doctype"] = main.get("doctype")
        if re.search(r"\.html?$", doc, re.IGNORECASE):
            files["inline"] = True
            files["instance"] = re.sub(r"\.html?$", "_htm.xml", doc, flags=re.IGNORECASE)
        else:
            files["instance"] = doc
    files["reports"] = {(r.findtext("Role") or "").strip(): {"page": (r.findtext("HtmlFileName") or "").strip(),
                                                           "category": (r.findtext("MenuCategory") or "").strip()}
                        for r in root.iter("Report") if r.findtext("Role")}
    return files


def kind_of(title: str) -> str:
    """Which primary statement a title names, by its words; "other" if none."""
    t = re.sub(r"\s+", " ", title.upper().replace("’", "'"))
    if re.search(r"BALANCE SHEET|FINANCIAL POSITION|FINANCIAL CONDITION|STATEMENTS? OF CONDITION", t):
        return "balance_sheet"
    if "CASH FLOW" in t:
        return "cash_flow"
    if re.search(r"EQUITY|STOCKHOLDERS|SHAREHOLDERS|SHAREOWNERS|PARTNERS' CAPITAL|MEMBERS' CAPITAL|CHANGES IN", t):
        return "equity"
    if "COMPREHENSIVE" in t and not re.search(r"(OPERATIONS|EARNINGS|INCOME|PROFIT OR LOSS|LOSS) AND (OTHER )?"
                                              r"COMPREHENSIVE", t):
        return "comprehensive_income"
    if re.search(r"OPERATIONS|INCOME|EARNINGS|PROFIT|LOSS", t):
        return "income_statement"
    return "other"


def read_schema(raw: bytes) -> dict:
    """The company schema: its namespace, its statement roles and its own elements."""
    root = _xml(raw, "the company schema")
    roles = []
    for rt in root.iter(f"{_L}roleType"):
        definition = (rt.findtext(f"{_L}definition") or "").strip()
        m = _ROLE_DEFINITION.match(definition)
        roles.append({"role": rt.get("roleURI"), "definition": definition,
                      "category": m.group("category").capitalize() if m else None,
                      "sort_code": m.group("code") if m else None,
                      "title": m.group("title") if m else definition})
    elements = {}
    for el in root.iter(f"{_XS}element"):
        if el.get("id"):
            elements[el.get("id")] = {
                "name": el.get("name"), "type": el.get("type"),
                "abstract": el.get("abstract") == "true",
                "substitution_group": el.get("substitutionGroup"),
                "period_type": el.get(f"{_XBRLI_ATTR}periodType"),
                "balance": el.get(f"{_XBRLI_ATTR}balance"),
            }
    # Linkbases named by the schema, local files only; a linkbase can also sit inside the schema itself.
    refs = {}
    for ref in root.iter(f"{_L}linkbaseRef"):
        href = ref.get(f"{_XL}href") or ""
        kind = (ref.get(f"{_XL}role") or "").rsplit("/", 1)[-1].replace("LinkbaseRef", "")
        if kind in ("presentation", "calculation", "label", "definition") and not re.match(r"https?://", href):
            refs[kind] = href
    embedded = {kind for kind in ("presentation", "calculation", "label", "definition")
                if root.find(f".//{_L}{kind}Link") is not None}
    return {"namespace": root.get("targetNamespace"), "roles": roles, "elements": elements,
            "linkbase_refs": refs, "embedded": sorted(embedded)}


def _locators(link) -> dict:
    """xlink:label of each locator -> (schema file or URL, element id)."""
    out = {}
    for loc in link.iter(f"{_L}loc"):
        href = loc.get(f"{_XL}href") or ""
        where, _, fragment = href.partition("#")
        out[loc.get(f"{_XL}label")] = (where, fragment)
    return out


def _effective(link, arc_tag):
    """The arcs of one extended link that are in force: of equivalent arcs (same
    ends, arcrole, order, weight and preferred label: every attribute but use and
    priority, XBRL 2.1 section 3.5.3.9.7.4) the highest priority wins, and a
    prohibiting arc there removes the relationship. Two arcs that differ only
    in their preferred label are two lines (opening and closing cash)."""
    best = {}
    for a in link.iter(arc_tag):
        key = (a.get(f"{_XL}from"), a.get(f"{_XL}to"), a.get(f"{_XL}arcrole"), float(a.get("order") or 1),
               a.get("weight") and float(a.get("weight")), a.get("preferredLabel"))
        priority = int(a.get("priority") or 0)
        held = best.get(key)
        if held is None or priority > held[0]:
            best[key] = (priority, [a])
        elif priority == held[0]:
            held[1].append(a)
    for priority, arcs in best.values():
        if any(a.get("use") == "prohibited" for a in arcs):
            continue
        yield arcs[0]


def read_presentation(raw: bytes) -> dict:
    """role -> arcs (parent id, child id, order, preferred label role)."""
    out = {}
    for link in _xml(raw, "the presentation linkbase").iter(f"{_L}presentationLink"):
        locs = _locators(link)
        arcs = out.setdefault(link.get(f"{_XL}role"), [])
        for a in _effective(link, f"{_L}presentationArc"):
            parent, child = locs.get(a.get(f"{_XL}from")), locs.get(a.get(f"{_XL}to"))
            if parent and child:
                arcs.append({"parent": parent[1], "child": child[1], "order": float(a.get("order") or 0),
                             "preferred_label": a.get("preferredLabel"), "href": child[0]})
    return out


def read_calculation(raw: bytes | None) -> tuple[dict, set]:
    """role -> [(parent id, child id, weight)], and the calculation versions seen.
    One element can add into two totals in one role."""
    out, versions = {}, set()
    if raw is None:
        return out, versions
    for link in _xml(raw, "the calculation linkbase").iter(f"{_L}calculationLink"):
        locs = _locators(link)
        tree = out.setdefault(link.get(f"{_XL}role"), [])
        for a in _effective(link, f"{_L}calculationArc"):
            version = _SUMMATION.get(a.get(f"{_XL}arcrole"))
            parent, child = locs.get(a.get(f"{_XL}from")), locs.get(a.get(f"{_XL}to"))
            if version and parent and child:
                versions.add(version)
                tree.append((parent[1], child[1], float(a.get("weight") or 1)))
    return out, versions


def read_labels(raw: bytes | None) -> dict:
    """element id -> {label role: text}, English first."""
    out = {}
    if raw is None:
        return out
    for link in _xml(raw, "the label linkbase").iter(f"{_L}labelLink"):
        locs = _locators(link)
        resources = {}
        for lab in link.iter(f"{_L}label"):
            lang = (lab.get(_XML_LANG) or "").lower()
            resources.setdefault(lab.get(f"{_XL}label"), []).append(
                (not lang.startswith("en"), lab.get(f"{_XL}role") or _STANDARD_LABEL, "".join(lab.itertext()).strip()))
        for a in link.iter(f"{_L}labelArc"):
            loc = locs.get(a.get(f"{_XL}from"))
            if not loc:
                continue
            labels = out.setdefault(loc[1], {})
            for _, role, text in sorted(resources.get(a.get(f"{_XL}to"), [])):
                labels.setdefault(role, text)
    return out


def read_definition(raw: bytes | None) -> list:
    """Definition arcs other than the dimensional ones: (role, arcrole, from id, to id)."""
    out = []
    if raw is None:
        return out
    for link in _xml(raw, "the definition linkbase").iter(f"{_L}definitionLink"):
        locs = _locators(link)
        for a in _effective(link, f"{_L}definitionArc"):
            arcrole = a.get(f"{_XL}arcrole") or ""
            parent, child = locs.get(a.get(f"{_XL}from")), locs.get(a.get(f"{_XL}to"))
            if parent and child and not _DIMENSIONAL.match(arcrole):
                out.append((link.get(f"{_XL}role"), arcrole, parent[1], child[1]))
    return out


def _number(text: str):
    try:
        d = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        return None
    return int(d) if d == d.to_integral_value() else float(d)


def _half_width(decimals) -> float:
    """Half the rounding interval a fact's decimals allow (0 for INF or none)."""
    if decimals in (None, "", "INF"):
        return 0.0
    try:
        return 0.5 * 10 ** (-int(decimals))
    except ValueError:
        return 0.0


def read_instance(raw: bytes) -> dict:
    """Every fact of the instance with its context, unit and decimals."""
    namespaces, root = {}, None
    try:
        for event, item in ET.iterparse(io.BytesIO(raw), events=("start", "start-ns")):
            if event == "start-ns":
                namespaces.setdefault(item[0], item[1])
            elif root is None:
                root = item
    except ET.ParseError as e:
        raise FilingDataError(f"the instance is not readable XML ({e})") from e
    prefix_of = {uri: prefix for prefix, uri in namespaces.items()}

    def qname(text):
        prefix, _, local = (text or "").strip().rpartition(":")
        return f"{prefix}:{local}" if prefix in namespaces else (text or "").strip()

    units = {}
    for u in root.iter(f"{_XBRLI}unit"):
        divide = u.find(f"{_XBRLI}divide")
        if divide is not None:
            num = [qname(m.text) for m in divide.find(f"{_XBRLI}unitNumerator").iter(f"{_XBRLI}measure")]
            den = [qname(m.text) for m in divide.find(f"{_XBRLI}unitDenominator").iter(f"{_XBRLI}measure")]
            units[u.get("id")] = "*".join(num) + "/" + "*".join(den)
        else:
            units[u.get("id")] = "*".join(qname(m.text) for m in u.iter(f"{_XBRLI}measure"))

    contexts = {}
    for c in root.iter(f"{_XBRLI}context"):
        instant = c.findtext(f"{_XBRLI}period/{_XBRLI}instant")
        start = c.findtext(f"{_XBRLI}period/{_XBRLI}startDate")
        end = c.findtext(f"{_XBRLI}period/{_XBRLI}endDate")
        period = instant.strip() if instant else (f"{start.strip()}/{end.strip()}" if start and end else None)
        dims = {qname(m.get("dimension")): qname(m.text) for m in c.iter(f"{_XBRLDI}explicitMember")}
        typed = any(True for _ in c.iter(f"{_XBRLDI}typedMember"))
        contexts[c.get("id")] = {"period": period, "dimensions": dims, "typed": typed,
                                 "entity": (c.findtext(f"{_XBRLI}entity/{_XBRLI}identifier") or "").strip()}

    facts, nil = [], 0
    for el in root:
        uri, _, local = el.tag.lstrip("{").partition("}")
        ctx = contexts.get(el.get("contextRef"))
        if ctx is None or uri in (_XBRLI[1:-1], "http://www.xbrl.org/2003/linkbase"):
            continue
        if el.get(_XSI_NIL) == "true":
            nil += 1
            continue
        text = "".join(el.itertext()).strip()
        unit = el.get("unitRef")
        facts.append({"concept": f"{prefix_of.get(uri, uri)}:{local}", "local": local, "namespace": uri,
                      "period": ctx["period"], "dimensions": ctx["dimensions"], "typed": ctx["typed"],
                      "entity": ctx["entity"], "unit": units.get(unit) if unit else None,
                      "decimals": el.get("decimals"), "value": _number(text) if unit else text,
                      "id": el.get("id")})
    return {"facts": facts, "nil_facts": nil, "namespaces": namespaces}


def _structure(name: str, abstract: bool) -> str | None:
    for suffix, kind in _STRUCTURE:
        if name.endswith(suffix):
            return kind
    return "abstract" if abstract else None


def _days(period: str) -> int | None:
    from datetime import date
    if "/" not in period:
        return None
    a, b = period.split("/")
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _day_before(day: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _bound(label_role: str) -> str | None:
    """"start" for a period-start label (an opening balance), "end" for a
    period-end label (a closing balance), else None. Negated forms count too."""
    r = (label_role or "").lower()
    return "start" if "periodstart" in r else "end" if "periodend" in r else None


def group_facts(instance: dict) -> tuple[dict, list]:
    """One numeric fact per concept, period, dimensions and unit: duplicates that
    agree within their rounding become one, the most precise; a duplicate that
    disagrees is listed, and the fact kept carries all the values (`conflict`)."""
    facts_by_concept, conflicts, grouped = {}, [], {}
    for f in instance["facts"]:
        if f["typed"] or not isinstance(f["value"], (int, float)):
            continue
        key = (f["concept"], f["period"], tuple(sorted(f["dimensions"].items())), f["unit"])
        grouped.setdefault(key, []).append(f)
    for key, group in grouped.items():
        best = min(group, key=lambda f: _half_width(f["decimals"]) if f["decimals"] not in (None, "", "INF")
                   else -1)
        for other in group:
            if abs(other["value"] - best["value"]) > _half_width(other["decimals"]) + _half_width(best["decimals"]):
                values = sorted({g["value"] for g in group})
                conflicts.append({"concept": key[0], "period": key[1], "dimensions": dict(key[2]), "unit": key[3],
                                  "values": values})
                best = {**best, "conflict": values}
                break
        facts_by_concept.setdefault(key[0], []).append(best)
    return facts_by_concept, conflicts


def _identities(text: str, prefix_of: dict) -> dict:
    """{"elements": [prefix:name, ...]} for a text of element identities (namespace#name), else {}."""
    tokens = text.split()
    names = [f"{prefix_of[uri]}:{local}" for uri, _, local in (t.rpartition("#") for t in tokens)
             if uri in prefix_of and local]
    return {"elements": names} if tokens and len(names) == len(tokens) else {}


def named_facts(instance: dict, names: list) -> dict:
    """The whole-company facts of the elements asked for, wherever the filing
    tags them (a note as well as a statement): name -> {values, tagged_by_member}.
    A name without a prefix matches the one element of that name. A fact with no
    unit (an extensible list naming the lines a lease liability is in, a text
    block) is returned as its text, once per period and text; a text that is a
    list of element identities (namespace#name) also gives them as prefixed
    names (`elements`), when every namespace is one the instance declares."""
    facts_by_concept, _ = group_facts(instance)
    texts = {}
    for f in instance["facts"]:
        if not f["typed"] and f["unit"] is None and isinstance(f["value"], str) and f["value"]:
            texts.setdefault(f["concept"], []).append(f)
    by_local = {}
    for concept in {*facts_by_concept, *texts}:
        by_local.setdefault(concept.split(":", 1)[-1], []).append(concept)
    out = {}
    for name in names:
        concepts = [name] if ":" in name else by_local.get(name, [])
        if len(concepts) > 1:
            out[name] = {"values": [], "tagged_by_member": 0, "ambiguous": sorted(concepts)}
            continue
        found = facts_by_concept.get(concepts[0], []) if concepts else []
        values = [{"period": f["period"], "value": f["value"], "unit": f["unit"], "decimals": f["decimals"],
                   **({"conflict": f["conflict"]} if "conflict" in f else {})}
                  for f in found if not f["dimensions"]]
        if concepts and not found:
            found = texts.get(concepts[0], [])
            seen = {(f["period"], f["value"]) for f in found if not f["dimensions"]}
            prefix_of = {uri: prefix for prefix, uri in instance.get("namespaces", {}).items()}
            values = [{"period": period, "value": text, "unit": None, "decimals": None, **_identities(text, prefix_of)}
                      for period, text in seen]
        values.sort(key=lambda c: (c["period"].split("/")[-1], _days(c["period"]) or 0), reverse=True)
        out[name] = {"element": concepts[0] if concepts else None, "values": values,
                     "tagged_by_member": sum(1 for f in found if f["dimensions"])}
    return out


def _roll_forward(lines: list) -> list:
    """Opening balance plus the change for the period equals the closing balance,
    per column, for each element the statement shows with both a period-start
    and a period-end label (cash on a cash flow statement). The change is the
    line whose element names an increase or decrease for the period; the effect
    of exchange rates is added only when that element excludes it and the effect
    does not already add into it."""
    def cells(line):
        return {v["period"]: v for v in line.get("values", [])}
    starts = {l["element"]: l for l in lines if _bound(l["label_role"]) == "start" and l.get("values")}
    ends = {l["element"]: l for l in lines if _bound(l["label_role"]) == "end" and l.get("values")}
    flows = [l for l in lines if not l.get("structure") and not _bound(l["label_role"])
             and any("/" in v["period"] for v in l.get("values", []))]
    changes = [l for l in flows if "PeriodIncreaseDecrease" in l["element"]]
    # Including the effect first, then a name that says nothing, then one that excludes it.
    changes.sort(key=lambda l: 0 if "IncludingExchangeRateEffect" in l["element"] else
                 2 if "ExcludingExchangeRateEffect" in l["element"] else 1)
    fx = [l for l in flows if l["element"].split(":", 1)[-1].startswith("EffectOfExchangeRate")]
    out = []
    for element in sorted(set(starts) & set(ends)):
        opening, closing = cells(starts[element]), cells(ends[element])
        change = changes[0] if changes else None
        add_fx = None
        if change and fx and "ExcludingExchangeRateEffect" in change["element"] \
                and fx[0].get("calculation_parent") != change["element"]:
            add_fx = fx[0]
        for period in sorted(set(opening) & set(closing), reverse=True):
            o, c = opening[period], closing[period]
            row = {"balance": element, "period": period, "opening": o["value"], "opening_instant": o.get("instant"),
                   "closing": c["value"], "closing_instant": c.get("instant"),
                   "change_element": change and change["element"], "exchange_rate_element": add_fx and add_fx["element"]}
            ch = change and cells(change).get(period)
            ex = add_fx and cells(add_fx).get(period)
            if not ch or (add_fx and not ex):
                row.update(computed=None, difference=None, consistent=None,
                           missing=[n for n, v in (("change", ch), ("exchange_rate_effect", ex if add_fx else True)) if not v])
                out.append(row)
                continue
            parts = [o, ch] + ([ex] if ex else [])
            computed = sum(p["value"] for p in parts)
            slack = _half_width(c["decimals"]) + sum(_half_width(p["decimals"]) for p in parts)
            difference = c["value"] - computed
            row.update(change=ch["value"], exchange_rate_effect=ex and ex["value"], computed=round(computed, 6),
                       difference=round(difference, 6), consistent=abs(difference) <= slack + 1e-9, missing=[])
            out.append(row)
    return out


def build_statements(schema: dict, presentation: dict, calculation: dict, labels: dict,
                     definition: list, instance: dict, reports: dict) -> tuple[list, list]:
    """The primary statements of the filing, and the duplicate facts that disagree."""
    facts_by_concept, conflicts = group_facts(instance)
    # A locator names an element by its id, "prefix_Name"; the instance names it
    # "prefix:Name". If prefixes differ, an element whose name is unique still matches.
    by_local = {}
    for concept in facts_by_concept:
        by_local.setdefault(concept.split(":", 1)[-1], set()).add(concept)

    def concept_of(element_id: str) -> str:
        prefix, _, local = element_id.partition("_")
        name = f"{prefix}:{local}"
        if name in facts_by_concept or len(by_local.get(local, ())) != 1:
            return name
        return next(iter(by_local[local]))

    extension_defs = {}
    for role, arcrole, a, b in definition:
        for own, other, direction in ((a, b, "to"), (b, a, "from")):
            if own in schema["elements"]:
                extension_defs.setdefault(own, []).append(
                    {"arcrole": arcrole, direction: concept_of(other), "role": role})

    statements = []
    for role in schema["roles"]:
        # The SEC's summary files a role under "Statements" by its definition, title and order;
        # without a summary, the definition's own category decides.
        menu = reports.get(role["role"], {}).get("category")
        if role["role"] not in presentation or (menu != "Statements" if menu else
                                                (role["category"] or "").lower() != "statement"):
            continue
        parenthetical = "parenthetical" in role["title"].lower()
        arcs = presentation[role["role"]]
        children, has_parent = {}, set()
        for a in arcs:
            children.setdefault(a["parent"], []).append(a)
            has_parent.add(a["child"])
        roots = [p for p in children if p not in has_parent]
        arcs_calc = calculation.get(role["role"], [])
        calc = {}
        for parent, child, weight in arcs_calc:
            calc.setdefault(child, []).append((parent, weight))
        lines = []

        def walk(element_id, depth, preferred, href, seen):
            local = element_id.partition("_")[2]
            own = schema["elements"].get(element_id)
            extension = own is not None and not re.match(r"https?://", href or "")
            structure = _structure(local, bool(own and own["abstract"]))
            texts = labels.get(element_id, {})
            label_role = preferred or _STANDARD_LABEL
            line = {"element": concept_of(element_id), "extension": extension, "depth": depth,
                    "label": texts.get(label_role) or texts.get(_STANDARD_LABEL) or local,
                    "label_role": label_role.rsplit("/", 1)[-1]}
            if structure:
                line["structure"] = structure
            if element_id in calc:
                parent, weight = calc[element_id][0]
                line["calculation_parent"] = concept_of(parent)
                line["weight"] = weight
                if len(calc[element_id]) > 1:
                    line["also_adds_into"] = [{"total": concept_of(p), "weight": w} for p, w in calc[element_id][1:]]
            if extension:
                line["definition"] = {k: own[k] for k in ("type", "period_type", "balance", "abstract")}
                if texts.get(_DOCUMENTATION):
                    line["documentation"] = texts[_DOCUMENTATION]
                if element_id in extension_defs:
                    line["related"] = extension_defs[element_id]
            lines.append(line)
            if element_id in seen:
                return
            for a in sorted(children.get(element_id, []), key=lambda a: a["order"]):
                walk(a["child"], depth + 1, a["preferred_label"], a["href"], seen | {element_id})

        for r in roots:
            walk(r, 0, None, None, frozenset())

        # The role's axes and members: a dimensional value belongs to the
        # statement when every axis and member it carries appears in it.
        in_role = {l["element"] for l in lines}
        # Opening and closing balances (a period-start or period-end label on an
        # instant element, as cash on a cash flow statement) are read per column.
        bound_lines = {id(l) for l in lines if not l.get("structure") and _bound(l["label_role"]) and any(
            not f["dimensions"] and "/" not in f["period"] for f in facts_by_concept.get(l["element"], []))}

        def column_periods(skip):
            # The periods that carry whole-company values for at least half as many
            # lines as the period with the most, durations and instants counted
            # apart (a statement can have both).
            totals = {}
            for line in lines:
                if line.get("structure") or id(line) in skip:
                    continue
                for f in facts_by_concept.get(line["element"], []):
                    if not f["dimensions"]:
                        totals.setdefault(f["period"], set()).add(line["element"])
            out = []
            for instant in (False, True):
                counts = {p: len(c) for p, c in totals.items() if ("/" not in p) == instant}
                if counts:
                    top = max(counts.values())
                    out += [p for p, n in counts.items() if n * 2 >= top]
            return sorted(out, key=lambda p: (p.split("/")[-1], _days(p) or 0), reverse=True)

        columns = column_periods(bound_lines)
        durations = [c for c in columns if "/" in c]
        if not durations:
            bound_lines, columns = set(), column_periods(set())

        def cell_of(f, period):
            return {"period": period, "value": f["value"], "unit": f["unit"], "decimals": f["decimals"],
                    **({"conflict": f["conflict"]} if "conflict" in f else {})}

        def in_statement(f):
            return all(a in in_role and m in in_role for a, m in f["dimensions"].items())

        for line in lines:
            if line.get("structure"):
                continue
            values, members = [], []
            if id(line) in bound_lines:
                # An opening on 1 January is the balance at the end of 31 December
                # (XBRL dates an instant at the end of its day); the start date
                # itself is taken only when the filer tagged no day before.
                at = {}
                for f in facts_by_concept.get(line["element"], []):
                    if "/" not in f["period"]:
                        at.setdefault(f["period"], []).append(f)
                for col in durations:
                    start, end = col.split("/")
                    days = [_day_before(start), start] if _bound(line["label_role"]) == "start" else [end]
                    day = next((d for d in days if any(not f["dimensions"] for f in at.get(d, []))),
                               next((d for d in days if d in at), None))
                    for f in at.get(day, []):
                        cell = {**cell_of(f, col), "instant": day}
                        if not f["dimensions"]:
                            values.append(cell)
                        elif in_statement(f):
                            members.append({**cell, "dimensions": f["dimensions"]})
                line["values"] = sorted(values, key=lambda c: columns.index(c["period"]))
                if members:
                    line["by_member"] = members
                continue
            for f in facts_by_concept.get(line["element"], []):
                cell = cell_of(f, f["period"])
                if not f["dimensions"] and f["period"] in columns:
                    values.append(cell)
                elif f["dimensions"] and in_statement(f):
                    if f["period"] in columns or _bounds(f["period"], columns):
                        members.append({**cell, "dimensions": f["dimensions"]})
            # Some companies tag a face line only with a member of the statement's axis
            # (Tesla's "Operating lease vehicles, net"). When the line has no whole-company
            # value, one member per column and its total has no value under that member,
            # the member's value is the line's; a total filed under the member too
            # (JPM's consolidated VIEs) means a sub-statement, and the line is left as it is.
            if not values and members and "calculation_parent" in line:
                cols = {}
                for m in members:
                    if m["period"] in columns and len(m["dimensions"]) == 1:
                        cols.setdefault(m["period"], []).append(m)
                parent_members = {tuple(f["dimensions"].items())
                                  for f in facts_by_concept.get(line["calculation_parent"], []) if f["dimensions"]}
                if cols and all(len(v) == 1 for v in cols.values()) and not any(
                        tuple(v[0]["dimensions"].items()) in parent_members for v in cols.values()):
                    values = [{**v[0], "member": v[0]["dimensions"]} for v in cols.values()]
                    for v in values:
                        del v["dimensions"]
                    line["values_from_member"] = True
            line["values"] = sorted(values, key=lambda c: columns.index(c["period"]))
            if members:
                line["by_member"] = members

        statements.append({"role": role["role"], "title": role["title"], "sort_code": role["sort_code"],
                           "kind": kind_of(role["title"]), "parenthetical": parenthetical, "columns": columns,
                           "lines": lines,
                           "calculation_checks": _checks(lines, [(concept_of(p), concept_of(c), w)
                                                                 for p, c, w in arcs_calc]),
                           "roll_forward_checks": _roll_forward(lines)})
    # A closing balance is tied to the balance sheet when the balance sheet shows
    # the same element at that date: otherwise their scopes differ (restricted cash).
    sheets = [s for s in statements if s["kind"] == "balance_sheet" and not s["parenthetical"]]
    shown = {(l["element"], v["period"]): v["value"] for s in sheets for l in s["lines"] for v in l.get("values", [])}
    for s in statements:
        for row in s["roll_forward_checks"]:
            on_sheet = shown.get((row["balance"], row["closing_instant"]))
            row["closing_on_balance_sheet"] = None if on_sheet is None else on_sheet == row["closing"]
    return statements, conflicts


def _bounds(period: str, columns: list) -> bool:
    """An instant at the start or end of one of the statement's durations
    (the opening and closing balances of an equity statement)."""
    if "/" in period:
        return False
    return any("/" in c and period in c.split("/") for c in columns)


def _checks(lines: list, arcs: list) -> list:
    """Each total against the weighted sum of the lines the company says add
    into it, per column. Consistent when the two agree within the rounding
    their decimals allow (the interval rule of Calculation 1.1); None when
    they disagree and a missing line was tagged only under members. `complete`
    is false when a line that adds into the total has no value: a sum that
    agrees without it is consistent but does not show the lines are all there.
    `missing_in_every_column` names the missing lines with no value in any
    column (a line printed blank, such as commitments and contingencies).
    Opening and closing balances (read per duration column) are left out."""
    if not arcs:
        return []
    cells = {}
    for line in lines:
        for v in line.get("values", []):
            if "instant" not in v:
                cells.setdefault((line["element"], v["period"], v["unit"]), v)
    member_only = {(line["element"], m["period"]) for line in lines for m in line.get("by_member", [])}
    with_value = {e for (e, p, u) in cells}
    # Every relationship of the role counts, a line the statement does not show included.
    items = {}
    for parent, child, weight in arcs:
        items.setdefault(parent, {}).setdefault(child, weight)
    out = []
    for total, parts in items.items():
        periods = {(p, u) for (e, p, u) in cells if e == total}
        for period, unit in sorted(periods, reverse=True):
            reported = cells[(total, period, unit)]
            present = {e: w for e, w in parts.items() if (e, period, unit) in cells}
            if not present:
                continue
            computed = sum(w * cells[(e, period, unit)]["value"] for e, w in present.items())
            slack = _half_width(reported["decimals"]) + sum(
                abs(w) * _half_width(cells[(e, period, unit)]["decimals"]) for e, w in present.items())
            difference = reported["value"] - computed
            missing = sorted(e for e in parts if e not in present)
            # A line the company tagged only under members (GE's equipment and services,
            # Berkshire's business groups) has no whole-company figure to add: a gap is
            # then not checkable, rather than inconsistent.
            by_member = sorted(e for e in missing if (e, period) in member_only)
            consistent = abs(difference) <= slack + 1e-9
            out.append({"total": total, "period": period, "unit": unit, "reported": reported["value"],
                        "computed": round(computed, 6), "difference": round(difference, 6),
                        "consistent": True if consistent else (None if by_member else False),
                        "complete": not missing, "missing": missing,
                        "missing_in_every_column": [e for e in missing if e not in with_value], "missing_tagged_only_by_member": by_member})
    return out


def document_facts(instance: dict) -> dict:
    """The cover facts that say what the filing is: type, period, fiscal year."""
    out = {}
    for f in instance["facts"]:
        if f["local"] in _DEI and not f["dimensions"] and f["local"] not in out:
            out[f["local"]] = f["value"]
    return out


_SECURITY = {"Security12bTitle": "title", "TradingSymbol": "trading_symbol", "SecurityExchangeName": "exchange",
             "NoTradingSymbolFlag": "no_trading_symbol"}


def _registrant(dimensions: dict):
    return dimensions.get("dei:LegalEntityAxis") or dimensions.get("dei:EntityAxis")


def registered_securities(instance: dict) -> list:
    """The cover's table of securities registered under Section 12(b): one row per context, as tagged.

    A combined filing (Entergy and its utilities) puts each registrant on dei:LegalEntityAxis or
    dei:EntityAxis; the row then names that registrant's CIK, read from its own EntityCentralIndexKey."""
    dei = [f for f in instance["facts"] if f["namespace"].startswith("http://xbrl.sec.gov/dei/")]
    cik_of = {}
    for f in dei:
        member = _registrant(f["dimensions"])
        if f["local"] == "EntityCentralIndexKey" and member and len(f["dimensions"]) == 1:
            cik_of[member] = str(f["value"]).lstrip("0")
    rows = {}
    for f in dei:
        if f["local"] in _SECURITY:
            key = tuple(sorted(f["dimensions"].items()))
            rows.setdefault(key, {"dimensions": f["dimensions"]})[_SECURITY[f["local"]]] = f["value"]
    out = []
    for row in rows.values():
        if "title" not in row and "trading_symbol" not in row:
            continue
        member = _registrant(row["dimensions"])
        row["registrant"] = member
        row["registrant_cik"] = cik_of.get(member) if member else None
        out.append(row)
    return out


def cover_shares(instance: dict) -> list:
    """The cover's shares outstanding (dei:EntityCommonStockSharesOutstanding), one row per class and registrant
    as tagged: a company with several classes tags each on us-gaap:StatementClassOfStockAxis. A fact repeated with
    the same date, dimensions and unit is one row; repeats that disagree are one row with "value" None and every
    figure in "conflicting_values"."""
    dei = [f for f in instance["facts"] if f["namespace"].startswith("http://xbrl.sec.gov/dei/")]
    cik_of = {_registrant(f["dimensions"]): str(f["value"]).lstrip("0") for f in dei
              if f["local"] == "EntityCentralIndexKey" and _registrant(f["dimensions"]) and len(f["dimensions"]) == 1}
    rows = {}
    for f in dei:
        if f["local"] == "EntityCommonStockSharesOutstanding" and isinstance(f["value"], (int, float)):
            key = (f["period"], tuple(sorted(f["dimensions"].items())), f.get("unit"))
            if key in rows:
                rows[key]["values"].add(f["value"])
                continue
            member = _registrant(f["dimensions"])
            rows[key] = {"values": {f["value"]}, "date": f["period"], "dimensions": f["dimensions"],
                         "share_class": f["dimensions"].get("us-gaap:StatementClassOfStockAxis"),
                         "registrant": member, "registrant_cik": cik_of.get(member) if member else None}
    out = []
    for row in rows.values():
        values = row.pop("values")
        if len(values) == 1:
            out.append({"value": next(iter(values)), **row})
        else:
            out.append({"value": None, "conflicting_values": sorted(values), **row})
    return out

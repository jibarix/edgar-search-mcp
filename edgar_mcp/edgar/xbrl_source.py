"""XBRL files of one filing, parsed into records that keep what was written.

Nothing here decides what a concept, a line, a statement or a company is.
Each function takes the bytes of one file and returns its elements in
document order with every attribute and text value as written, plus
mechanical reference resolution only:

- a QName (an element name, or a QName written in an attribute or in text,
  such as an explicit member) is resolved to a namespace URI and local name
  through the namespace declarations in scope where it is written;
- where that cannot be done (an undeclared prefix, say), the value is kept as
  written and marked `resolved: false` with the reason.

Attribute names are given in Clark notation ("{namespace}local") when they are
in a namespace, so no prefix has to be chosen; `namespaces` lists the
document's own declarations. XML fragments (typed members, footnotes, any
content that is not plain text) are re-serialised with prefixes from the
document's declarations, declaring only the bindings that differ from those
of the document element.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from xml.sax.saxutils import escape, quoteattr

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XS = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class FilingDataError(Exception):
    """A file of the filing could not be read as XML."""


def split(tag: str) -> tuple[str, str]:
    """A Clark name "{ns}local" as (ns, local); ("", name) when it has no namespace."""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


class XmlDoc:
    """One parsed XML file: its elements, the namespace bindings in scope at
    each, and the position of each in document order."""

    def __init__(self, raw: bytes, name: str):
        self.name = name
        self.declarations = []      # every xmlns declaration, in document order
        self._scope = {}            # element -> {prefix: namespace} in scope there
        self._seq = {}              # element -> position in document order
        self._parent = {}           # element -> its parent element (None for the document element)
        pending, stack, seq, open_elements = [], [{"xml": XML_NS}], 0, []
        try:
            events = ET.iterparse(io.BytesIO(raw), events=("start-ns", "start", "end"))
            for event, obj in events:
                if event == "start-ns":
                    pending.append(obj)
                elif event == "start":
                    scope = stack[-1]
                    if pending:
                        scope = dict(scope)
                        for prefix, uri in pending:
                            scope[prefix] = uri
                            self.declarations.append({"prefix": prefix, "namespace": uri, "seq": seq})
                        pending = []
                    stack.append(scope)
                    self._scope[obj] = scope
                    self._seq[obj] = seq
                    self._parent[obj] = open_elements[-1] if open_elements else None
                    open_elements.append(obj)
                    seq += 1
                else:
                    stack.pop()
                    open_elements.pop()
            self.root = events.root
        except ET.ParseError as e:
            raise FilingDataError(f"{name} is not readable XML ({e})") from e

    def scope(self, element) -> dict:
        return self._scope[element]

    def seq(self, element) -> int:
        return self._seq[element]

    def parent(self, element):
        return self._parent[element]

    def ancestry(self, element) -> list:
        """`element` and its ancestors, the document element first."""
        chain = []
        while element is not None:
            chain.append(element)
            element = self._parent[element]
        return chain[::-1]

    def base_uri(self, element, document_url: str) -> str:
        """The base URI in effect at `element` (XML Base): the document's URL,
        changed by every xml:base written on the element or its ancestors, the
        outermost first. An href written on `element` is resolved against it."""
        base = document_url
        for e in self.ancestry(element):
            written = e.get(f"{{{XML_NS}}}base")
            if written is not None:
                base = urljoin(base, written)
        return base

    def resolve_href(self, href: str, element, document_url: str) -> str:
        """`href`, written on `element`, as an absolute URL (RFC 3986 against the effective base URI)."""
        return urljoin(self.base_uri(element, document_url), href)

    def namespaces(self) -> dict:
        """The bindings declared on the document element, as written."""
        return {p: u for p, u in self._scope[self.root].items() if p != "xml"}

    def conflicts(self) -> list:
        """Prefixes bound to more than one namespace somewhere in the document."""
        seen = {}
        for d in self.declarations:
            seen.setdefault(d["prefix"], set()).add(d["namespace"])
        return [{"prefix": p, "namespaces": sorted(u), "reason": "the prefix is bound to different namespaces "
                 "in different parts of the document"} for p, u in seen.items() if len(u) > 1]

    def qname(self, text: str | None, element) -> dict | None:
        """A QName written in an attribute value or in text, resolved through
        the bindings in scope at `element`. `qname` is the value as written."""
        if text is None:
            return None
        written = text.strip()  # QName values are whitespace-collapsed by XML Schema
        prefix, colon, local = written.rpartition(":")
        scope = self._scope[element]
        if not local or (colon and not prefix) or ":" in prefix:
            return {"qname": text, "resolved": False, "reason": "not a QName"}
        if prefix not in scope:
            return {"qname": text, "resolved": False,
                    "reason": f"the prefix {prefix!r} is not declared where the value is written" if prefix
                    else "no default namespace is declared where the value is written"}
        return {"qname": text, "resolved": True, "namespace": scope[prefix], "local_name": local}

    def prefixes_for(self, namespace: str, element) -> list:
        """Every prefix bound to `namespace` where `element` is written."""
        return sorted(p for p, u in self._scope[element].items() if u == namespace)

    def to_xml(self, element) -> str:
        """`element` and its content as XML, namespace-equivalent to the source."""
        out = []
        self._write(element, self.namespaces(), out)
        return "".join(out)

    def _write(self, element, outer: dict, out: list):
        scope = {p: u for p, u in self._scope[element].items() if p != "xml"}
        declare = {p: u for p, u in scope.items() if outer.get(p) != u}

        def name_of(clark, attribute):
            ns, local = split(clark)
            if not ns:
                return local
            if ns == XML_NS:
                return "xml:" + local
            choices = sorted(p for p, u in scope.items() if u == ns and (p or not attribute))
            if not choices:  # cannot happen for a parsed document; declare one rather than lose the namespace
                prefix = f"ns{len(scope)}"
                scope[prefix] = ns
                declare[prefix] = ns
                choices = [prefix]
            prefix = "" if "" in choices else choices[0]
            return f"{prefix}:{local}" if prefix else local

        tag = name_of(element.tag, False)
        attributes = "".join(f" {name_of(k, True)}={quoteattr(v)}" for k, v in element.attrib.items())
        declarations = "".join(f" xmlns{':' + p if p else ''}={quoteattr(u)}" for p, u in sorted(declare.items()))
        out.append(f"<{tag}{declarations}{attributes}")
        if element.text is None and len(element) == 0:
            out.append("/>")
        else:
            out.append(">" + escape(element.text or ""))
            for child in element:
                self._write(child, scope, out)
                out.append(escape(child.tail or ""))
            out.append(f"</{tag}>")


def element_name(doc: XmlDoc, element) -> dict:
    ns, local = split(element.tag)
    return {"namespace": ns, "local_name": local, "prefixes": doc.prefixes_for(ns, element) if ns else []}


def tree(doc: XmlDoc, element=None) -> dict:
    """An element as {tag, attributes, text, children}, every value as written."""
    element = doc.root if element is None else element
    node = {"seq": doc.seq(element), "tag": element.tag, "attributes": dict(element.attrib), "text": element.text,
            "children": [tree(doc, child) for child in element]}
    if element.tail is not None and element.tail.strip():
        node["tail"] = element.tail
    return node


# ── instance ───────────────────────────────────────────────────────────────

def _members(doc: XmlDoc, container) -> dict | None:
    """The content of a segment or scenario: explicit and typed members as
    written, and anything else as XML."""
    if container is None:
        return None
    explicit, typed, other = [], [], []
    for child in container:
        ns, local = split(child.tag)
        if ns == XBRLDI and local == "explicitMember":
            explicit.append({"seq": doc.seq(child), "dimension": doc.qname(child.get("dimension"), child),
                             "member": doc.qname(child.text, child), "attributes": dict(child.attrib)})
        elif ns == XBRLDI and local == "typedMember":
            typed.append({"seq": doc.seq(child), "dimension": doc.qname(child.get("dimension"), child),
                          "attributes": dict(child.attrib),
                          "xml": "".join(doc.to_xml(c) for c in child), "text": child.text})
        else:
            other.append({"seq": doc.seq(child), "xml": doc.to_xml(child)})
    return {"seq": doc.seq(container), "explicit_members": explicit, "typed_members": typed, "other": other}


def _context(doc: XmlDoc, element) -> dict:
    entity = element.find(f"{{{XBRLI}}}entity")
    identifier = entity.find(f"{{{XBRLI}}}identifier") if entity is not None else None
    period = element.find(f"{{{XBRLI}}}period")
    return {
        "seq": doc.seq(element), "node": "context", "id": element.get("id"), "attributes": dict(element.attrib),
        "entity": None if identifier is None else {"scheme": identifier.get("scheme"), "identifier": identifier.text},
        "segment": _members(doc, entity.find(f"{{{XBRLI}}}segment") if entity is not None else None),
        "scenario": _members(doc, element.find(f"{{{XBRLI}}}scenario")),
        "period": None if period is None else [{"element": split(p.tag)[1], "text": p.text} for p in period],
    }


def _measures(doc: XmlDoc, element) -> list:
    return [doc.qname(m.text, m) for m in element.findall(f"{{{XBRLI}}}measure")]


def _unit(doc: XmlDoc, element) -> dict:
    divide = element.find(f"{{{XBRLI}}}divide")
    out = {"seq": doc.seq(element), "node": "unit", "id": element.get("id"), "attributes": dict(element.attrib),
           "measures": _measures(doc, element), "divide": None}
    if divide is not None:
        numerator = divide.find(f"{{{XBRLI}}}unitNumerator")
        denominator = divide.find(f"{{{XBRLI}}}unitDenominator")
        out["divide"] = {"numerator": _measures(doc, numerator) if numerator is not None else [],
                         "denominator": _measures(doc, denominator) if denominator is not None else []}
    return out


def _fact(doc: XmlDoc, element) -> dict:
    out = {"seq": doc.seq(element), "node": "fact", "concept": element_name(doc, element),
           "id": element.get("id"), "contextRef": element.get("contextRef"), "unitRef": element.get("unitRef"),
           "decimals": element.get("decimals"), "precision": element.get("precision"),
           "nil": element.get(f"{{{XSI}}}nil"), "attributes": dict(element.attrib), "text": element.text}
    if len(element):
        out["xml"] = doc.to_xml(element)
    return out


def read_instance(raw: bytes | None, name: str = "instance", doc: XmlDoc | None = None) -> dict:
    """Every top-level element of an instance, in document order.

    `node` says which XBRL element it is, by its element name only:
    "context" (xbrli:context), "unit" (xbrli:unit), "fact" (an element
    outside the xbrli and link namespaces that carries contextRef), "link"
    (an element in the link namespace: schemaRef, footnoteLink, ...), or
    "other" (anything else, a tuple say), returned as XML. `doc` is the file
    already parsed, if it was.
    """
    doc = doc if doc is not None else XmlDoc(raw, name)
    root_ns, root_local = split(doc.root.tag)
    unresolved = list(doc.conflicts())
    if (root_ns, root_local) != (XBRLI, "xbrl"):
        raise FilingDataError(f"{name}: the document element is {doc.root.tag}, not xbrli:xbrl")
    items = []
    for element in doc.root:
        ns, local = split(element.tag)
        if ns == XBRLI and local == "context":
            items.append(_context(doc, element))
        elif ns == XBRLI and local == "unit":
            items.append(_unit(doc, element))
        elif ns == LINK:
            items.append({"seq": doc.seq(element), "node": "link", "element": element_name(doc, element),
                          "attributes": dict(element.attrib), "xml": doc.to_xml(element)})
        elif ns not in (XBRLI, LINK) and element.get("contextRef") is not None:
            items.append(_fact(doc, element))
        else:
            items.append({"seq": doc.seq(element), "node": "other", "element": element_name(doc, element),
                          "attributes": dict(element.attrib), "xml": doc.to_xml(element)})
    for item in items:
        for where in ("segment", "scenario"):
            members = item.get(where) or {}
            for m in members.get("explicit_members", []) + members.get("typed_members", []):
                for part in ("dimension", "member"):
                    if m.get(part) is not None and not m[part]["resolved"]:
                        unresolved.append({"seq": m["seq"], "context": item["id"], "value": m[part]["qname"],
                                           "reason": m[part]["reason"]})
        divide = item.get("divide") or {}
        for m in (item.get("measures") or []) + divide.get("numerator", []) + divide.get("denominator", []):
            if not m["resolved"]:
                unresolved.append({"seq": item["seq"], "unit": item["id"], "value": m["qname"], "reason": m["reason"]})
    return {"namespaces": doc.namespaces(), "declarations": doc.declarations, "items": items,
            "unresolved": unresolved, "doc": doc}


def select_concepts(instance: dict, names: list) -> tuple[list, list, list]:
    """Facts of the named concepts, with the contexts and units they use.

    A name is "{namespace}local", "prefix:local" (the prefix resolved through
    the instance's own declarations) or a bare local name, which matches that
    local name in every namespace. Returns (items in document order,
    concept_matches, unresolved)."""
    facts = [i for i in instance["items"] if i["node"] == "fact"]
    present = {}
    for f in facts:
        present.setdefault((f["concept"]["namespace"], f["concept"]["local_name"]), None)
    bound = {}
    for d in instance["declarations"]:
        bound.setdefault(d["prefix"], set()).add(d["namespace"])
    matches, unresolved, wanted = [], [], set()
    for name in names:
        if name.startswith("{"):
            ns, local = split(name)
            found = [(ns, local)] if (ns, local) in present else []
            entry = {"name": name, "resolved": True, "matches": found}
        elif ":" in name:
            prefix, _, local = name.partition(":")
            uris = bound.get(prefix, set())
            if len(uris) != 1:
                reason = (f"the prefix {prefix!r} is not declared in the instance" if not uris
                          else f"the prefix {prefix!r} is bound to several namespaces in the instance")
                entry = {"name": name, "resolved": False, "reason": reason, "namespaces": sorted(uris), "matches": []}
                unresolved.append({"name": name, "reason": reason})
            else:
                ns = next(iter(uris))
                entry = {"name": name, "resolved": True,
                         "matches": [(ns, local)] if (ns, local) in present else []}
        else:
            entry = {"name": name, "resolved": True, "matches": [k for k in present if k[1] == name]}
        entry["matches"] = [{"namespace": ns, "local_name": local} for ns, local in entry["matches"]]
        wanted |= {(m["namespace"], m["local_name"]) for m in entry["matches"]}
        matches.append(entry)
    chosen = [f for f in facts if (f["concept"]["namespace"], f["concept"]["local_name"]) in wanted]
    contexts = {f["contextRef"] for f in chosen}
    units = {f["unitRef"] for f in chosen if f["unitRef"] is not None}
    declared_contexts = {i["id"] for i in instance["items"] if i["node"] == "context"}
    declared_units = {i["id"] for i in instance["items"] if i["node"] == "unit"}
    for ref in sorted(contexts - declared_contexts):
        unresolved.append({"contextRef": ref, "reason": "no context with this id in the instance"})
    for ref in sorted(units - declared_units):
        unresolved.append({"unitRef": ref, "reason": "no unit with this id in the instance"})
    chosen_ids = {id(f) for f in chosen}
    items = [i for i in instance["items"]
             if id(i) in chosen_ids or (i["node"] == "context" and i["id"] in contexts)
             or (i["node"] == "unit" and i["id"] in units)]
    return items, matches, unresolved


# ── schema ─────────────────────────────────────────────────────────────────

def _definition_and_used_on(doc: XmlDoc, element) -> dict:
    definition = element.find(f"{{{LINK}}}definition")
    return {"definition": None if definition is None else definition.text,
            "usedOn": [doc.qname(u.text, u) for u in element.findall(f"{{{LINK}}}usedOn")]}


def read_schema(raw: bytes, name: str = "schema", url: str | None = None) -> dict:
    """The declarations of a company schema, in document order.

    `node` names the XML construct: "import", "include", "linkbaseRef",
    "roleType", "arcroleType", "element" (a top-level xs:element), "linkbase"
    (a linkbase written inside the schema), or "other" (any other top-level
    schema component, as XML). Each keeps every attribute; QNames in `type`
    and `substitutionGroup` are resolved where they are written. `ids` maps
    each element id to every element with that id (namespace, local name and
    seq), which is how a locator's href finds its concept; an id two elements
    share is listed in `unresolved` and resolves to neither (`concept_at`). With the schema's `url`, each import,
    include and linkbaseRef also has `url`, its schemaLocation or href
    resolved against the base URI in effect where it is written.
    """
    doc = XmlDoc(raw, name)

    def located(element, written):
        return {"url": doc.resolve_href(written, element, url) if written else None} if url is not None else {}
    if split(doc.root.tag) != (XS, "schema"):
        raise FilingDataError(f"{name}: the document element is {doc.root.tag}, not xs:schema")
    target = doc.root.get("targetNamespace")
    items, ids, unresolved, linkbases = [], {}, list(doc.conflicts()), []
    for element in doc.root.iter():
        ns, local = split(element.tag)
        base = {"seq": doc.seq(element), "attributes": dict(element.attrib)}
        if (ns, local) in ((XS, "import"), (XS, "include")):
            items.append({**base, "node": local, **located(element, element.get("schemaLocation"))})
        elif (ns, local) == (LINK, "linkbaseRef"):
            href = element.get(f"{{{XLINK}}}href")
            items.append({**base, "node": "linkbaseRef", "href": href, **located(element, href)})
        elif (ns, local) in ((LINK, "roleType"), (LINK, "arcroleType")):
            items.append({**base, "node": local, **_definition_and_used_on(doc, element)})
        elif (ns, local) == (LINK, "linkbase"):
            items.append({**base, "node": "linkbase", "element_count": sum(1 for _ in element.iter()) - 1})
            linkbases.append(element)
    for element in doc.root:
        ns, local = split(element.tag)
        base = {"seq": doc.seq(element), "attributes": dict(element.attrib)}
        if (ns, local) == (XS, "element"):
            name_attr = element.get("name")
            item = {**base, "node": "element", "namespace": target, "name": name_attr, "id": element.get("id"),
                    "type": doc.qname(element.get("type"), element),
                    "substitutionGroup": doc.qname(element.get("substitutionGroup"), element)}
            if len(element):
                item["xml"] = doc.to_xml(element)
            items.append(item)
            for key in ("type", "substitutionGroup"):
                if item[key] is not None and not item[key]["resolved"]:
                    unresolved.append({"seq": item["seq"], "element": name_attr, "value": item[key]["qname"],
                                       "reason": item[key]["reason"]})
            if item["id"] is not None:
                if item["id"] in ids:
                    unresolved.append({"seq": item["seq"], "id": item["id"],
                                       "reason": "more than one element of the schema has this id, so a locator "
                                                 "pointing to it is ambiguous and is not resolved"})
                ids.setdefault(item["id"], []).append({"namespace": target, "local_name": name_attr,
                                                       "seq": item["seq"]})
        elif (ns, local) not in ((XS, "import"), (XS, "include"), (XS, "annotation")):
            items.append({**base, "node": "other", "element": element_name(doc, element), "xml": doc.to_xml(element)})
    items.sort(key=lambda i: i["seq"])
    return {"target_namespace": target, "namespaces": doc.namespaces(), "items": items, "ids": ids,
            "unresolved": unresolved, "linkbases": linkbases, "doc": doc}


def concept_at(schema: dict, file: str, fragment: str) -> dict:
    """The element of a read schema whose id is `fragment`: resolved when
    exactly one element has it; ambiguous, with every candidate and none
    chosen, when several do; unresolved when none does."""
    found = schema["ids"].get(fragment, [])
    if len(found) == 1:
        return {"resolved": True, "file": file, "namespace": found[0]["namespace"],
                "local_name": found[0]["local_name"], "schema_seq": found[0]["seq"]}
    if found:
        return {"resolved": False, "ambiguous": True, "file": file, "fragment": fragment,
                "candidates": [{"namespace": c["namespace"], "local_name": c["local_name"], "schema_seq": c["seq"]}
                               for c in found],
                "reason": f"{len(found)} elements of {file} have this id; none is chosen"}
    return {"resolved": False, "file": file, "fragment": fragment, "reason": f"no element of {file} has this id"}


def locator_resolver(schemas: dict):
    """A `target_of` for `read_linkbase` over schemas already read: `schemas`
    maps a schema's absolute URL to (file name, `read_schema` result). The
    href it is given is absolute (see `read_linkbase`'s `url`)."""
    def resolve(href):
        if not href:
            return {"resolved": False, "reason": "the locator has no href"}
        url, _, fragment = href.partition("#")
        if url not in schemas:
            return {"resolved": False, "schema": url, "fragment": fragment, "reason": "the schema was not read"}
        name, schema = schemas[url]
        return concept_at(schema, name, fragment)
    return resolve


# ── linkbases ──────────────────────────────────────────────────────────────

# The extended-link element each part returns, by its XBRL 2.1 element name.
EXTENDED_LINKS = {"presentation": "presentationLink", "calculation": "calculationLink",
                  "definition": "definitionLink", "label": "labelLink", "reference": "referenceLink"}
_XLINK_TYPE = f"{{{XLINK}}}type"
_XLINK_HREF = f"{{{XLINK}}}href"


def read_linkbase(doc: XmlDoc, linkbase, file: str, part: str, target_of,
                  url: str | None = None) -> tuple[list, list]:
    """The extended links of one kind in one linkbase element, flattened in
    document order to items, and what could not be resolved.

    Every child of the extended link is an item by its xlink:type:
    "loc" (with `href` as written and `target`, what `target_of` says it
    points to), "arc", "resource" (with its text, and its markup as XML) or
    "other". Each keeps every attribute and the `seq` of its extended link
    (`link_seq`). Arcs are never combined, overridden or dropped: priority,
    use="prohibited", order and weight are attributes like any other.
    The roleRef and arcroleRef elements of the linkbase are returned with
    them when the linkbase holds at least one extended link of this kind.

    With `url`, the URL of the file `doc` was read from, each href is also
    resolved against the base URI in effect where it is written (the file's
    URL changed by every inherited xml:base): it is given as `url` and that
    absolute address is what `target_of` receives. Without it, `target_of`
    receives the href as written.
    """

    def located(element, href):
        return doc.resolve_href(href, element, url) if url is not None and href else href
    wanted = EXTENDED_LINKS[part]
    items, unresolved = [], []
    for element in linkbase:
        ns, local = split(element.tag)
        base = {"seq": doc.seq(element), "file": file, "attributes": dict(element.attrib)}
        if ns == LINK and local in ("roleRef", "arcroleRef"):
            href = element.get(_XLINK_HREF)
            items.append({**base, "node": local, "href": href,
                          **({"url": located(element, href)} if url is not None else {})})
            continue
        if element.get(_XLINK_TYPE) != "extended":
            continue
        if (ns, local) != (LINK, wanted):
            if local not in EXTENDED_LINKS.values() or ns != LINK:
                unresolved.append({"seq": base["seq"], "file": file, "element": element.tag,
                                   "reason": "an extended link of an element no part returns (a generic link, say)"})
            continue
        link_seq = base["seq"]
        items.append({**base, "node": "extendedLink", "element": element_name(doc, element)})
        for child in element:
            kind = child.get(_XLINK_TYPE)
            entry = {"seq": doc.seq(child), "file": file, "link_seq": link_seq, "element": element_name(doc, child),
                     "attributes": dict(child.attrib)}
            if kind == "locator":
                href = child.get(_XLINK_HREF)
                absolute = located(child, href)
                entry.update(node="loc", href=href, **({"url": absolute} if url is not None else {}),
                             target=target_of(absolute))
            elif kind == "arc":
                entry.update(node="arc")
            elif kind == "resource":
                entry.update(node="resource", text=child.text)
                if len(child):
                    entry["xml"] = doc.to_xml(child)
            else:
                entry.update(node="other", xml=doc.to_xml(child))
            items.append(entry)
    if not any(i["node"] == "extendedLink" for i in items):
        items = []
    return items, unresolved

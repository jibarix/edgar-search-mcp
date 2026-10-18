"""Parser for SEC XBRL Company Facts API data.

Classifies concepts into financial-statement categories via the
deterministic TagClassifier (builtin overrides + sec_tag_mapping.json).

Period selection uses each fact's duration and filing, never the month of
its end date: a fiscal year is a 52/53-week duration reported in an annual
form, and when a period was reported more than once the latest filing wins.

The Company Facts API carries facts for the whole filing entity in the
standard taxonomies. Facts split by share class, segment or geography, and
company-specific concepts, are not in it.
"""
from __future__ import annotations

import re
import logging
from collections import Counter, defaultdict
from datetime import date, timedelta

from edgar_mcp.edgar.tag_classifier import TagClassifier
from edgar_mcp.utils.helpers import iso_date

logger = logging.getLogger(__name__)

# Duration windows in days. Fiscal years run 52 or 53 weeks; fiscal quarters
# run 12 to 17 weeks (Costco: 12-week quarters and a 16- or 17-week fourth).
ANNUAL_DAYS = (340, 380)
QUARTER_DAYS = (75, 125)
ANNUAL_FORMS = {'10-K', '10-K/A', '10-KT', '10-KT/A', '20-F', '20-F/A', '40-F', '40-F/A'}
TRANSITION_FORMS = {'10-KT', '10-KT/A', '10-QT', '10-QT/A'}
# Periodic financial reports. Other forms repeat figures in rounded form (a
# proxy statement's pay-versus-performance table restates net income to the
# nearest million and is filed after the 10-K), so they are used only when no
# periodic report carries the fact.
PERIODIC_FORMS = ANNUAL_FORMS | {'10-Q', '10-Q/A', '10-QT', '10-QT/A', '6-K', '6-K/A'}
# Units the parser reads. Amounts in any other currency are reported as skipped.
SUPPORTED_UNITS = ('USD', 'USD/shares', 'pure', 'shares')
# Units for which a quarter may be derived by subtracting two cumulative facts.
ADDITIVE_UNITS = {'USD'}
SOURCE_FIELDS = ('accn', 'filed', 'form', 'start', 'end')
# A quarter end must be reported by at least this many different concepts, so
# that a stray 90-day disclosure (an acquisition's stub period) is not taken
# for one. Concepts are counted, not rows: one concept repeated in several
# filings is still one piece of evidence.
MIN_QUARTER_CONCEPTS = 5
# Share of the year to date by which the quarters may miss it before the
# difference is reported as a warning (quarters are rounded separately).
RECONCILE_TOLERANCE = 0.0005
# Days a year-to-date figure may start away from the day after the prior
# fiscal year end.
YEAR_START_TOLERANCE = 7


def _days(start, end):
    """Length of a duration fact in days, or None for an instant fact."""
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _in(days, window):
    return days is not None and window[0] <= days <= window[1]


def _on_file(fact, as_of):
    """Whether the fact had been filed by `as_of`. A fact with no filing date
    cannot be placed in time, so under a cutoff it is left out."""
    if not as_of:
        return True
    filed = fact.get('filed')
    return bool(filed) and filed <= as_of


def _latest_filed(facts):
    """The fact from the most recent periodic report (restated figures win)."""
    periodic = [f for f in facts if f.get('form') in PERIODIC_FORMS]
    return max(periodic or facts, key=lambda f: (f.get('filed') or '', f.get('accn') or ''))


def _source(fact, minus=None):
    """Where a value came from.

    A derived quarter carries its own interval, both figures it was computed
    from with their filings, and a warning when the earlier figure was
    restated after the cumulative one was filed.
    """
    src = {k: fact.get(k) for k in SOURCE_FIELDS}
    src['derived'] = minus is not None
    if minus is not None:
        src['start'] = (date.fromisoformat(minus['end']) + timedelta(days=1)).isoformat()
        src['cumulative'] = {**{k: fact.get(k) for k in SOURCE_FIELDS}, 'val': fact['val']}
        src['minus'] = {**{k: minus.get(k) for k in SOURCE_FIELDS}, 'val': minus['val']}
        if (minus.get('filed') or '') > (fact.get('filed') or ''):
            src['warnings'] = [
                "INCONSISTENT_REPORTING_BASIS: the earlier year-to-date figure was restated "
                "in a filing made after the cumulative figure; the two may not be on the same basis"
            ]
    return src


class XBRLParser:
    """Parser for financial data from SEC APIs."""

    def __init__(self):
        self.classifier = TagClassifier()

    def parse_company_facts(self, facts_data, statement_type=None, period_type='annual', num_periods=1, as_of=None):
        """
        Parse company facts JSON data from SEC API.

        Args:
            facts_data: JSON data from SEC Company Facts API
            statement_type: Type of financial statement (BS, IS, CF, ALL)
            period_type: 'annual', 'quarterly', or 'ytd'
            num_periods: Number of most recent periods to include
            as_of: optional YYYY-MM-DD date; facts filed after it, and facts
                with no filing date, are ignored, so the result is what was
                on file on that date

        Returns:
            dict: {'periods', 'metrics', 'metadata'}. When there is nothing
            to return, 'periods' is empty and metadata['empty_reason'] says
            why (no facts, unsupported units, nothing on file at the cutoff,
            or too little evidence of the fiscal calendar).

        Raises:
            ValueError: `as_of` is not a YYYY-MM-DD date
        """
        if as_of is not None:
            iso_date(as_of)

        if not facts_data or 'facts' not in facts_data:
            logger.error("Invalid company facts data")
            return self._empty('NO_FACTS', period_type, as_of)

        taxonomies = []
        if 'us-gaap' in facts_data['facts']:
            taxonomies.append('us-gaap')
        if 'ifrs-full' in facts_data['facts']:
            taxonomies.append('ifrs-full')

        if not taxonomies:
            logger.error("No supported taxonomies found in company facts")
            return self._empty('NO_SUPPORTED_TAXONOMY', period_type, as_of)

        entity_name = facts_data.get('entityName', '')
        logger.info(f"Processing XBRL data for {entity_name}")

        # Collect ALL concept names from the data
        all_concept_names = set()
        for taxonomy in taxonomies:
            all_concept_names.update(facts_data['facts'].get(taxonomy, {}).keys())

        logger.info(f"Found {len(all_concept_names)} unique concepts in data")

        # Classify all concepts via the builtin + SEC tag mapping.
        classifications, report = self.classifier.classify_with_report(
            list(all_concept_names),
            statement_type=statement_type
        )

        logger.info(f"Classified {len(classifications)} concepts for statement type '{statement_type}'")

        # The fiscal calendar comes from every concept the company reports,
        # not only the requested statement: a balance sheet has no durations.
        calendar = self._build_calendar(
            ((f"{taxonomy}:{concept}", fact)
             for taxonomy in taxonomies
             for concept, concept_data in facts_data['facts'].get(taxonomy, {}).items()
             for facts in concept_data.get('units', {}).values()
             for fact in facts),
            as_of,
        )

        # Extract values for classified concepts. A concept is identified by
        # taxonomy and name together, so the same name in two taxonomies is
        # never merged into one series.
        tag_values = defaultdict(list)
        skipped_units = {}
        filed_after_cutoff = 0

        for taxonomy in taxonomies:
            for concept, concept_data in facts_data['facts'].get(taxonomy, {}).items():
                if concept not in classifications:
                    continue
                tag = f"{taxonomy}:{concept}"

                units = concept_data.get('units', {})
                for unit_type, facts in units.items():
                    if unit_type not in SUPPORTED_UNITS:
                        skipped_units.setdefault(tag, []).append(unit_type)
                        continue

                    for fact in facts:
                        if 'val' not in fact or 'end' not in fact:
                            continue
                        if not _on_file(fact, as_of):
                            filed_after_cutoff += 1
                            continue

                        tag_values[tag].append({
                            'val': fact['val'],
                            'end': fact['end'],
                            'start': fact.get('start'),
                            'unit': unit_type,
                            'filed': fact.get('filed'),
                            'accn': fact.get('accn'),
                            'form': fact.get('form'),
                            'fy': fact.get('fy'),
                            'fp': fact.get('fp'),
                        })

        # Build financial_data grouped by category
        financial_data = {}
        tag_units = {}
        for tag in sorted(tag_values):
            info = classifications[tag.split(':', 1)[1]]
            # One unit per concept: values in different units are never mixed.
            # The unit used most often is kept; the others are named in the output.
            units_seen = [v['unit'] for v in tag_values[tag]]
            unit = max(sorted(set(units_seen)), key=units_seen.count)
            tag_units[tag] = {'unit': unit, 'other_units': sorted(set(units_seen) - {unit})}
            financial_data.setdefault(info['category'], {})[tag] = [
                v for v in tag_values[tag] if v['unit'] == unit]

        # Normalize
        normalized = self._normalize_api_data(
            financial_data, period_type, calendar,
            num_periods, classifications, tag_units, as_of
        )

        metadata = normalized['metadata']
        # What the company reports that is not here, and why: no mapping, a rule, or another statement.
        metadata['unclassified'] = {
            'unmapped': len(report['unmapped']),
            'excluded': report['excluded'],
            'other_statements': len(report['other_statements']),
        }
        if skipped_units:
            metadata['skipped_units'] = skipped_units
            metadata['warnings'].append(
                "amounts in units other than "
                f"{', '.join(SUPPORTED_UNITS)} were left out; see skipped_units")
        if not normalized['periods']:
            # Say why there is nothing, so an empty answer is never ambiguous.
            if tag_values:
                metadata['empty_reason'] = 'INSUFFICIENT_PERIOD_EVIDENCE'
            elif filed_after_cutoff:
                metadata['empty_reason'] = 'NOTHING_ON_FILE_AT_CUTOFF'
            elif skipped_units:
                metadata['empty_reason'] = 'UNSUPPORTED_UNITS'
            else:
                metadata['empty_reason'] = 'NO_FACTS_FOR_STATEMENT'
            logger.warning(f"No periods found: {metadata['empty_reason']}")
        else:
            logger.info(f"Retrieved data for {len(normalized['periods'])} periods: {normalized['periods']}")
        return normalized

    def _empty(self, reason, period_type, as_of):
        """A result with no periods and the reason there are none."""
        return {
            'periods': [],
            'metrics': {},
            'metadata': {'period_type': period_type, 'as_of': as_of, 'warnings': [], 'empty_reason': reason},
        }

    def _build_calendar(self, facts, as_of=None):
        """Find the company's fiscal year ends and fiscal quarter ends.

        `facts` yields (concept, fact) pairs. A fiscal year end is the end
        date of a 52/53-week duration reported in an annual form. A quarter
        end is a date on which 12-17 week durations of several different
        concepts end in periodic reports, or a fiscal year end. A shorter
        period reported on a transition form (10-KT, 10-QT) is not treated as
        a fiscal year; its end date is listed so the caller can say so.
        Facts not on file at `as_of` are ignored. `notes` says where the
        evidence was too thin to be sure.
        """
        annual_in_annual_form, annual_any, transition_ends = set(), set(), set()
        quarter_concepts = defaultdict(set)
        year_starts = defaultdict(Counter)
        for concept, fact in facts:
            if not _on_file(fact, as_of):
                continue
            days = _days(fact.get('start'), fact.get('end'))
            if days is None:
                continue
            form = fact.get('form')
            if _in(days, ANNUAL_DAYS):
                annual_any.add(fact['end'])
                year_starts[fact['end']][fact['start']] += 1
                if form in ANNUAL_FORMS:
                    annual_in_annual_form.add(fact['end'])
            elif _in(days, QUARTER_DAYS) and form in PERIODIC_FORMS:
                quarter_concepts[fact['end']].add(concept)
            if form in TRANSITION_FORMS and not _in(days, ANNUAL_DAYS):
                transition_ends.add(fact['end'])
        fiscal_year_ends = annual_in_annual_form or annual_any
        quarter_ends = {end for end, concepts in quarter_concepts.items()
                        if len(concepts) >= MIN_QUARTER_CONCEPTS}
        notes = []
        if quarter_concepts and not quarter_ends:
            quarter_ends = set(quarter_concepts)
            notes.append(
                f"INSUFFICIENT_PERIOD_EVIDENCE: no quarter end is reported by {MIN_QUARTER_CONCEPTS} or more "
                "concepts; every date on which a quarter-length figure ends was taken as a quarter end")
        return {
            'fiscal_year_ends': fiscal_year_ends,
            'quarter_ends': quarter_ends | fiscal_year_ends,
            'transition_ends': transition_ends - fiscal_year_ends,
            # The first day of each fiscal year, as most of its annual facts give it.
            'year_starts': {end: starts.most_common(1)[0][0]
                            for end, starts in year_starts.items() if end in fiscal_year_ends},
            'notes': notes,
        }

    def _year_start(self, period, calendar):
        """The first day of the fiscal year an interim `period` falls in, or None.

        It is the day after the prior fiscal year end; when that year is not
        on file, the start of the annual figures of the year that follows.
        """
        end = date.fromisoformat(period)
        prior = [e for e in map(date.fromisoformat, calendar['fiscal_year_ends'])
                 if 0 < (end - e).days < ANNUAL_DAYS[0]]
        if prior:
            return max(prior) + timedelta(days=1)
        following = sorted(e for e in calendar['fiscal_year_ends'] if e > period)
        if following:
            start = calendar.get('year_starts', {}).get(following[0])
            if start and start <= period:
                return date.fromisoformat(start)
        return None

    def _year_to_date(self, durations, period, calendar, notes=None):
        """The facts running from the start of the fiscal year to `period`.

        At a fiscal year end these are the 52/53-week facts. Before it, they
        are the facts starting on the first day of the fiscal year. When that
        day cannot be established nothing is returned and the period is
        recorded in `notes`: the longest figure on file may be a single
        quarter or a trailing twelve months, and is never guessed to be the
        year to date.
        """
        if period in calendar['fiscal_year_ends']:
            return [f for f, d in durations if _in(d, ANNUAL_DAYS)]

        interim = [(f, d) for f, d in durations if d < ANNUAL_DAYS[0]]
        if not interim:
            return []
        year_start = self._year_start(period, calendar)
        if year_start is None:
            if notes is not None:
                notes['no_anchor'].add(period)
            return []
        anchored = [
            (f, d) for f, d in interim
            if abs((date.fromisoformat(f['start']) - year_start).days) <= YEAR_START_TOLERANCE
        ]
        if not anchored:
            return []
        longest = max(d for _, d in anchored)
        return [f for f, d in anchored if d == longest]

    def _select_fact(self, facts, period, period_type, calendar, notes=None):
        """Pick the value of one concept for one period end.

        Returns (value, source), or (None, None) when the concept has no fact
        of the requested kind for that period.
        """
        candidates = [f for f in facts if f['end'] == period]
        if not candidates:
            return None, None

        instants = [f for f in candidates if not f.get('start')]
        if instants:
            fact = _latest_filed(instants)
            return fact['val'], _source(fact)

        durations = [(f, _days(f['start'], f['end'])) for f in candidates]
        durations = [(f, d) for f, d in durations if d is not None and d <= ANNUAL_DAYS[1]]
        if not durations:
            return None, None

        if period_type == 'annual':
            annual = [f for f, d in durations if _in(d, ANNUAL_DAYS)]
            if not annual:
                return None, None
            fact = _latest_filed(annual)
            return fact['val'], _source(fact)

        # quarterly: a reported quarter is used as filed
        if period_type == 'quarterly':
            quarters = [f for f, d in durations if _in(d, QUARTER_DAYS)]
            if quarters:
                fact = _latest_filed(quarters)
                return fact['val'], _source(fact)

        year_to_date = self._year_to_date(durations, period, calendar, notes)
        if not year_to_date:
            return None, None
        cumulative = _latest_filed(year_to_date)

        if period_type == 'ytd':
            return cumulative['val'], _source(cumulative)

        # quarterly, not reported on its own: cumulative minus the prior
        # cumulative. Both must be amounts in the same unit that start on the
        # same day, the earlier one ending a quarter before.
        if cumulative['unit'] not in ADDITIVE_UNITS:
            return None, None
        end = date.fromisoformat(period)
        prior = []
        for f in facts:
            if f.get('start') != cumulative['start'] or f['end'] >= period:
                continue
            try:
                gap = (end - date.fromisoformat(f['end'])).days
            except ValueError:
                continue
            if _in(gap, QUARTER_DAYS):
                prior.append(f)
        if not prior:
            return None, None
        # One vintage throughout: the earlier figure is the latest filed, as
        # the cumulative one is, so a derived quarter is always the difference
        # of the two year-to-date figures this parser returns. A restatement
        # of the earlier figure after the cumulative was filed is flagged.
        prior_fact = _latest_filed(prior)
        return cumulative['val'] - prior_fact['val'], _source(cumulative, minus=prior_fact)

    def _normalize_api_data(self, financial_data, period_type, calendar, num_periods, classifications, tag_units=None, as_of=None):
        """
        Normalize financial data into a structured format.

        Args:
            financial_data: Extracted financial data grouped by category
            period_type: 'annual' (fiscal years), 'quarterly' (single fiscal
                quarters) or 'ytd' (fiscal year to date at each quarter end)
            calendar: fiscal year ends and quarter ends from _build_calendar
            num_periods: Number of periods to return
            classifications: dict of concept_name -> classification info
            tag_units: dict of tag -> {'unit', 'other_units'}
            as_of: the filing cutoff applied, recorded in the metadata

        Returns:
            dict: Normalized financial data by period
        """
        if tag_units is None:
            tag_units = {}
        category_order = {
            'Assets': 0, 'Liabilities': 1, 'Equity': 2,
            'Revenue': 3, 'Income': 4, 'EPS': 5,
            'OperatingCashFlow': 6, 'InvestingCashFlow': 7, 'FinancingCashFlow': 8,
        }

        # Period ends present in the requested data
        periods = set()
        for category in financial_data:
            for tag in financial_data[category]:
                for fact in financial_data[category][tag]:
                    periods.add(fact['end'])

        fiscal_year_ends = calendar['fiscal_year_ends']
        wanted = fiscal_year_ends if period_type == 'annual' else calendar['quarter_ends']
        series = [
            (category, tag, financial_data[category][tag])
            for category in sorted(financial_data.keys(), key=lambda x: category_order.get(x, 99))
            for tag in financial_data[category]
        ]

        # Newest first, keeping only period ends for which some concept has a
        # figure of the requested kind, so no period is returned empty.
        notes = {'no_anchor': set()}
        selected = {}
        for period in sorted(periods & wanted, reverse=True):
            if len(selected) == num_periods:
                break
            found = {}
            for _, tag, facts in series:
                value, src = self._select_fact(facts, period, period_type, calendar, notes)
                if value is not None:
                    found[tag] = (value, src)
            if found:
                selected[period] = found
        limited_periods = list(selected)

        # Not guessed: with no fiscal year on file the month is unknown.
        fiscal_month = max(fiscal_year_ends).split('-')[1] if fiscal_year_ends else None

        normalized_data = {
            'periods': limited_periods,
            'metrics': {},
        }
        unreconciled = []

        for category, tag, facts in series:
            if not any(tag in selected[period] for period in limited_periods):
                continue
            concept_name = tag.split(':')[-1]
            info = classifications.get(concept_name, {})
            display_name = info.get('display_name', self._format_concept_name(concept_name))
            order = info.get('order', 50)
            units = tag_units.get(tag, {})
            metric_key = f"{category}_{concept_name}"
            # The same concept name in a second taxonomy keeps its own entry.
            if metric_key in normalized_data['metrics']:
                metric_key = f"{category}_{tag.replace(':', '_')}"

            values = {period: selected[period].get(tag, (None, None))[0] for period in limited_periods}
            sources = {period: selected[period][tag][1] for period in limited_periods
                       if tag in selected[period]}

            source = info.get('source', 'sec')
            reconciliation = {}
            if period_type == 'quarterly' and units.get('unit') in ADDITIVE_UNITS:
                reconciliation = self._reconcile(facts, values, sources, calendar)
                if any(abs(r['difference']) > RECONCILE_TOLERANCE * abs(r['year_to_date'])
                       for r in reconciliation.values()):
                    unreconciled.append(tag)

            normalized_data['metrics'][metric_key] = {
                'values': values,
                'sources': sources,
                'category': category,
                'tag': tag,
                'display_name': display_name,
                'order': order,
                'unit': units.get('unit', 'USD'),
                'other_units': units.get('other_units', []),
                'source': source,
                'indent': info.get('indent', 0),
                'is_subtotal': info.get('is_subtotal', False),
                'section': info.get('section', ''),
            }
            if reconciliation:
                normalized_data['metrics'][metric_key]['reconciliation'] = reconciliation

        warnings = list(calendar.get('notes', ()))
        transition_ends = sorted(calendar.get('transition_ends', ()))
        if transition_ends:
            warnings.append(
                "a change of fiscal year end was reported; the short transition "
                f"period ending {', '.join(transition_ends)} is not returned as a fiscal year")
        if notes['no_anchor']:
            warnings.append(
                "INSUFFICIENT_PERIOD_EVIDENCE: the start of the fiscal year is not on file for "
                f"{', '.join(sorted(notes['no_anchor']))}; no year-to-date figure or derived quarter "
                "is given for those dates")
        if unreconciled:
            warnings.append(
                f"INCONSISTENT_REPORTING_BASIS: for {len(unreconciled)} metric(s) the quarters do not add up "
                "to the year to date, usually because a figure was restated in a later filing; see each "
                f"metric's reconciliation: {', '.join(unreconciled[:10])}"
                + (" and others" if len(unreconciled) > 10 else ""))

        normalized_data['metadata'] = {
            'fiscal_month': fiscal_month,
            'period_type': period_type,
            'selection': 'latest periodic report (10-K, 10-Q, 20-F, 40-F, 6-K) for each period',
            'vintage': 'latest filed'
                       + (f' on or before {as_of}' if as_of else '')
                       + ': every figure, and both sides of a derived quarter, is the most recently filed'
                         ' value, so later restatements replace the figures first reported',
            'scope': 'whole-entity facts in standard taxonomies; no share-class, segment or geographic detail',
            'as_of': as_of,
            'warnings': warnings,
        }

        return normalized_data

    def _reconcile(self, facts, values, sources, calendar):
        """Where the returned quarters do not add up to the year to date.

        For each quarter end whose fiscal year is covered from its first day
        by the quarters returned, compares their sum with the year-to-date
        figure (the annual figure at a fiscal year end). Returns
        {period: {'year_to_date', 'sum_of_quarters', 'difference'}} for the
        dates on which the two differ.
        """
        differences = {}
        dated = sorted(p for p, src in sources.items() if src.get('start'))
        for period in dated:
            year_to_date, ytd_src = self._select_fact(facts, period, 'ytd', calendar)
            if year_to_date is None or not ytd_src.get('start'):
                continue
            expected = date.fromisoformat(ytd_src['start'])
            earliest = (expected - timedelta(days=YEAR_START_TOLERANCE)).isoformat()
            chain = [p for p in dated if p <= period and sources[p]['start'] >= earliest]
            contiguous = len(chain) > 1
            for p in chain:
                if abs((date.fromisoformat(sources[p]['start']) - expected).days) > YEAR_START_TOLERANCE:
                    contiguous = False
                    break
                expected = date.fromisoformat(p) + timedelta(days=1)
            if not contiguous:
                continue
            total = sum(values[p] for p in chain)
            if total != year_to_date:
                differences[period] = {
                    'year_to_date': year_to_date,
                    'sum_of_quarters': total,
                    'difference': total - year_to_date,
                }
        return differences

    def _format_concept_name(self, concept_name):
        """Fallback: convert CamelCase to human-readable label."""
        formatted = re.sub(r'([a-z])([A-Z])', r'\1 \2', concept_name)
        formatted = re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', formatted)
        return formatted
